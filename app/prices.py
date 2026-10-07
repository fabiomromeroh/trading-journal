"""Price history for the trade chart (and MFE/MAE).

Providers, tried in order when PRICE_PROVIDER=auto:
  demo    - synthetic candles for sample-data trades only (clearly labelled)
  schwab  - Schwab marketdata pricehistory, if the Schwab API source is connected
  polygon - Polygon.io aggregates, if POLYGON_API_KEY is set
  yahoo   - Yahoo Finance chart endpoint (unofficial, no key; best effort)
Set PRICE_PROVIDER=none to disable charts, or name a single provider.
Options are charted on their underlying (option price history isn't available from these sources).
"""
from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PriceCache, Trade, utcnow
from app.timeutil import ET, local_to_utc_naive, utc_naive_to_tz

log = logging.getLogger(__name__)


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def chart_window(trade: Trade) -> tuple[str, datetime, datetime]:
    """(interval, start_utc, end_utc). Intraday trades -> 5m bars for the session(s)."""
    end_ref = trade.closed_at or utcnow()
    o_local = utc_naive_to_tz(trade.opened_at, ET)
    e_local = utc_naive_to_tz(end_ref, ET)
    if trade.time_known and (e_local.date() - o_local.date()).days <= 1:
        start = local_to_utc_naive(o_local.date(), time(9, 30), ET)
        end = local_to_utc_naive(e_local.date(), time(16, 0), ET)
        return "5m", start, end
    start = trade.opened_at - timedelta(days=45)
    end = min(end_ref + timedelta(days=15), utcnow())
    return "1d", start, end


# ---------------------------------------------------------------- providers
def _demo(trade: Trade, interval: str, start: datetime, end: datetime) -> list[dict]:
    """Synthetic candles that pass through the trade's fills (Brownian bridge between anchors)."""
    rng = random.Random(trade.id or 1)
    step = timedelta(minutes=5) if interval == "5m" else timedelta(days=1)
    times = []
    t = start
    while t <= end:
        if interval == "1d" and utc_naive_to_tz(t, ET).weekday() >= 5:
            t += step
            continue
        if interval == "5m":
            lt = utc_naive_to_tz(t, ET).time()
            if not (time(9, 30) <= lt < time(16, 0)):
                t += step
                continue
        times.append(t)
        t += step
    if not times:
        return []
    is_opt = trade.asset_type == "OPTION"
    base = trade.strike if is_opt and trade.strike else trade.entry_price
    anchors = {}
    if not is_opt:
        for f in trade.fills:
            idx = min(range(len(times)), key=lambda i: abs((times[i] - f.executed_at).total_seconds()))
            anchors[idx] = f.price
    anchors.setdefault(0, base * (1 + rng.uniform(-0.01, 0.01)))
    anchors.setdefault(len(times) - 1, (trade.exit_price or base) if not is_opt else base * (1 + rng.uniform(-.03, .03)))
    keys = sorted(anchors)
    vol = base * (0.0018 if interval == "5m" else 0.012)
    closes = [0.0] * len(times)
    for a, b in zip(keys, keys[1:]):
        n = b - a
        walk = [0.0]
        for _ in range(n):
            walk.append(walk[-1] + rng.gauss(0, vol))
        for i in range(n + 1):
            frac = i / n if n else 0
            closes[a + i] = anchors[a] + (anchors[b] - anchors[a]) * frac + walk[i] - walk[-1] * frac
    out, prev = [], closes[0]
    for tm, c in zip(times, closes):
        o = prev
        hi = max(o, c) + abs(rng.gauss(0, vol * 0.6))
        lo = min(o, c) - abs(rng.gauss(0, vol * 0.6))
        out.append({"time": _ms(tm) // 1000, "open": round(o, 2), "high": round(hi, 2),
                    "low": round(lo, 2), "close": round(c, 2)})
        prev = c
    return out


def _schwab(db: Session, symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    from app.sources.schwab_api import SchwabApiSource, SchwabClient
    src = SchwabApiSource()
    if not src.is_configured() or not src.status(db).ready:
        return []
    ft, f = ("minute", 5) if interval == "5m" else ("daily", 1)
    candles = SchwabClient(db).price_history(symbol, start, end, ft, f)
    return [{"time": c["datetime"] // 1000, "open": c["open"], "high": c["high"], "low": c["low"],
             "close": c["close"]} for c in candles]


def _polygon(symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    key = get_settings().polygon_api_key
    if not key:
        return []
    mult, span = (5, "minute") if interval == "5m" else (1, "day")
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{mult}/{span}/{_ms(start)}/{_ms(end)}"
    r = httpx.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}, timeout=20)
    r.raise_for_status()
    return [{"time": x["t"] // 1000, "open": x["o"], "high": x["h"], "low": x["l"], "close": x["c"]}
            for x in r.json().get("results", [])]


def _yahoo(symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params={
        "period1": _ms(start) // 1000, "period2": _ms(end) // 1000, "interval": interval,
        "includePrePost": "false"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return []
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4)})
    return out


PROVIDER_LABEL = {"demo": "Synthetic sample prices (not real market data)", "schwab": "Schwab market data",
                  "polygon": "Polygon.io", "yahoo": "Yahoo Finance (unofficial)"}


def get_chart(db: Session, trade: Trade) -> dict:
    s = get_settings()
    interval, start, end = chart_window(trade)
    symbol = trade.underlying
    if trade.is_demo:
        order = ["demo"]
    elif s.price_provider in ("none", "off", ""):
        order = []
    elif s.price_provider == "auto":
        order = ["schwab", "polygon", "yahoo"]
    else:
        order = [s.price_provider]
    errors = []
    for prov in order:
        try:
            if prov == "demo":
                candles = _demo(trade, interval, start, end)
            else:
                key = f"{prov}|{symbol}|{interval}|{start:%Y%m%d%H%M}|{end:%Y%m%d%H%M}"
                cached = db.scalar(select(PriceCache).where(PriceCache.cache_key == key))
                if cached:
                    candles = json.loads(cached.payload)
                else:
                    candles = (_schwab(db, symbol, interval, start, end) if prov == "schwab" else
                               _polygon(symbol, interval, start, end) if prov == "polygon" else
                               _yahoo(symbol, interval, start, end) if prov == "yahoo" else [])
                    if candles and end < utcnow() - timedelta(hours=12):
                        db.add(PriceCache(cache_key=key, provider=prov, payload=json.dumps(candles)))
                        db.commit()
            if candles:
                return {"candles": candles, "interval": interval, "provider": prov,
                        "provider_label": PROVIDER_LABEL.get(prov, prov), "symbol": symbol}
        except Exception as exc:  # network / auth errors -> try next provider
            log.info("price provider %s failed: %s", prov, exc)
            errors.append(f"{prov}: {exc}")
    return {"candles": [], "interval": interval, "provider": None, "symbol": symbol, "errors": errors}


def compute_excursions(trade: Trade, candles: list[dict]) -> tuple[float | None, float | None]:
    """MFE/MAE in $ for stock trades, from candles inside the holding period."""
    if trade.asset_type != "STOCK" or not candles or not trade.closed_at:
        return None, None
    o, c = _ms(trade.opened_at) // 1000, _ms(trade.closed_at) // 1000
    inside = [k for k in candles if o - 300 <= k["time"] <= c]
    if not inside:
        return None, None
    hi, lo = max(k["high"] for k in inside), min(k["low"] for k in inside)
    q = trade.quantity * trade.multiplier
    if trade.direction == "LONG":
        mfe, mae = (hi - trade.entry_price) * q, (lo - trade.entry_price) * q
    else:
        mfe, mae = (trade.entry_price - lo) * q, (trade.entry_price - hi) * q
    return round(max(mfe, 0.0), 2), round(min(mae, 0.0), 2)


def markers(trade: Trade, interval: str) -> list[dict]:
    out = []
    for f in trade.fills:
        ts = _ms(f.executed_at) // 1000
        if interval == "1d":
            d = utc_naive_to_tz(f.executed_at, ET).date()
            ts = _ms(datetime.combine(d, time(0, 0))) // 1000
        buy = f.side == "BUY"
        out.append({"time": ts, "position": "belowBar" if buy else "aboveBar",
                    "color": "#22c55e" if buy else "#ef4444", "shape": "arrowUp" if buy else "arrowDown",
                    "text": f"{'B' if buy else 'S'} {f.quantity:g} @ {f.price:g}"})
    return sorted(out, key=lambda m: m["time"])


_ = math
