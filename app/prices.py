"""Price history for the trade chart (and MFE/MAE).

Providers, tried in order when PRICE_PROVIDER=auto:
  demo    - synthetic candles for sample-data trades only (clearly labelled)
  schwab  - Schwab marketdata pricehistory, if the Schwab API source is connected
  polygon - Polygon.io aggregates, if POLYGON_API_KEY is set
  yahoo   - Yahoo Finance chart endpoint (unofficial, no key; best effort)
Set PRICE_PROVIDER=none to disable charts, or name a single provider.
Options are charted on their underlying (option price history isn't available from these sources).

Chart timeframes: 1m 5m 15m 30m 1h 4h 1D 1W. Intraday history is limited (Yahoo's limits, used for
every real provider): 1m for the last ~30 days (max ~7 days per request), 5m/15m/30m for the last
~60 days, 1h/4h for the last ~730 days. 4h bars are aggregated from 1h bars (09:30 and 13:30 ET).
Daily/weekly bar times are normalised to 00:00 UTC of their New York trading date.
"""
from __future__ import annotations

import json
import logging
import random
import time as _time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PriceCache, Trade, utcnow
from app.timeutil import ET, local_to_utc_naive, utc_naive_to_tz

log = logging.getLogger(__name__)
CACHE_VERSION = "v2"


@dataclass(frozen=True)
class TF:
    key: str            # public timeframe id
    label: str          # human label
    fetch: str          # provider interval actually requested
    bar: timedelta      # nominal bar length
    lookback_days: int | None  # how far back intraday history goes (None = unlimited)
    before: timedelta   # history fetched before entry (~200+ bars so a 200-period MA is warmed up)
    after: timedelta    # history fetched after exit
    max_span: timedelta | None = None  # max request span (1m)
    focus_before: int = 50  # bars shown before entry
    focus_after: int = 25   # bars shown after exit


TIMEFRAMES: dict[str, TF] = {t.key: t for t in (
    TF("1m", "1 minute", "1m", timedelta(minutes=1), 29, timedelta(days=1), timedelta(days=1),
       max_span=timedelta(days=7), focus_before=60, focus_after=30),
    TF("5m", "5 minutes", "5m", timedelta(minutes=5), 59, timedelta(days=7), timedelta(days=2),
       focus_before=50, focus_after=25),
    TF("15m", "15 minutes", "15m", timedelta(minutes=15), 59, timedelta(days=14), timedelta(days=4),
       focus_before=40, focus_after=20),
    TF("30m", "30 minutes", "30m", timedelta(minutes=30), 59, timedelta(days=30), timedelta(days=7),
       focus_before=40, focus_after=20),
    TF("1h", "1 hour", "60m", timedelta(hours=1), 729, timedelta(days=60), timedelta(days=15),
       focus_before=50, focus_after=20),
    TF("4h", "4 hours", "60m", timedelta(hours=4), 729, timedelta(days=200), timedelta(days=40),
       focus_before=50, focus_after=20),
    TF("1D", "Daily", "1d", timedelta(days=1), None, timedelta(days=420), timedelta(days=90),
       focus_before=60, focus_after=25),
    TF("1W", "Weekly", "1wk", timedelta(days=7), None, timedelta(days=6 * 365), timedelta(days=2 * 365),
       focus_before=40, focus_after=15),
)}
DAILYISH = {"1D", "1W"}
LOOKBACK_TEXT = {"1m": "1-minute bars only go back about 30 days",
                 "5m": "5-minute bars only go back about 60 days",
                 "15m": "15-minute bars only go back about 60 days",
                 "30m": "30-minute bars only go back about 60 days",
                 "1h": "hourly bars only go back about 2 years",
                 "4h": "4-hour bars only go back about 2 years"}


class Unavailable(Exception):
    pass


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _sec(dt: datetime) -> int:
    return _ms(dt) // 1000


def _date_key(d: date) -> int:
    """Daily bar time: 00:00 UTC of the New York trading date (what lightweight-charts expects)."""
    return int(datetime.combine(d, time(0, 0), tzinfo=timezone.utc).timestamp())


def _et_date_of_sec(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).date()


def _now(now: datetime | None) -> datetime:
    return now or utcnow()


# ---------------------------------------------------------------- timeframe logic
def is_swing(trade: Trade, now: datetime | None = None) -> bool:
    """True if the trade spans more than one New York trading day (or is still open from an earlier day)."""
    end = trade.closed_at or _now(now)
    return utc_naive_to_tz(end, ET).date() > utc_naive_to_tz(trade.opened_at, ET).date()


def tf_window(trade: Trade, tf: str, now: datetime | None = None,
              limited: bool = True) -> tuple[datetime, datetime, list[str]]:
    """(start_utc, end_utc, notes) for a timeframe, or raise Unavailable(reason)."""
    spec = TIMEFRAMES[tf]
    now = _now(now)
    o, c = trade.opened_at, trade.closed_at or now
    start, end = o - spec.before, min(c + spec.after, now)
    notes: list[str] = []
    if limited and spec.lookback_days:
        earliest = now - timedelta(days=spec.lookback_days)
        if c < earliest + timedelta(hours=1):
            raise Unavailable(f"{LOOKBACK_TEXT[tf]}, and this trade is older")
        if start < earliest:
            start = earliest
            if o < earliest:
                notes.append(f"Entry is older than the {spec.label} history ({LOOKBACK_TEXT[tf]}).")
    if spec.max_span and end - start > spec.max_span:
        start = max(start, min(o, end) - timedelta(hours=18))
        end = min(end, start + spec.max_span)
        if end < c:
            notes.append(f"{spec.label} chart covers only the first days of this trade.")
    if end <= start:
        raise Unavailable("no market data for this period")
    return start, end, notes


def timeframe_options(trade: Trade, now: datetime | None = None, limited: bool = True) -> list[dict]:
    out = []
    for key, spec in TIMEFRAMES.items():
        try:
            tf_window(trade, key, now, limited)
            out.append({"tf": key, "label": spec.label, "available": True, "reason": None})
        except Unavailable as exc:
            out.append({"tf": key, "label": spec.label, "available": False, "reason": str(exc)})
    return out


def default_timeframe(trade: Trade, now: datetime | None = None, limited: bool = True) -> str:
    """Swing trades (more than one trading day) -> 1D. Intraday trades with real fill times -> 5m
    (or the finest intraday timeframe still available). Intraday trades without times -> 1D."""
    if is_swing(trade, now) or not trade.time_known:
        return "1D"
    avail = {o["tf"] for o in timeframe_options(trade, now, limited) if o["available"]}
    for tf in ("5m", "15m", "30m", "1h"):
        if tf in avail:
            return tf
    return "1D"


# ---------------------------------------------------------------- candle helpers
def normalize(candles: list[dict], tf: str) -> list[dict]:
    """Sort, dedupe (keep last) and, for daily/weekly bars, pin times to 00:00 UTC of the NY date."""
    by_time: dict[int, dict] = {}
    for c in candles:
        c = dict(c)
        c.setdefault("volume", 0)
        if c["volume"] is None:
            c["volume"] = 0
        if tf in DAILYISH and int(c["time"]) % 86400:  # exact 00:00 UTC = already keyed by date
            c["time"] = _date_key(_et_date_of_sec(int(c["time"])))
        by_time[int(c["time"])] = c
    return [by_time[k] for k in sorted(by_time)]


def _merge(group: list[dict]) -> dict:
    return {"time": group[0]["time"], "open": group[0]["open"], "high": max(g["high"] for g in group),
            "low": min(g["low"] for g in group), "close": group[-1]["close"],
            "volume": sum(g.get("volume") or 0 for g in group)}


def aggregate_4h(candles: list[dict]) -> list[dict]:
    """Hourly -> 4-hour bars per New York session: 09:30-13:30 and 13:30-16:00 (TradingView alignment)."""
    groups: dict[tuple, list[dict]] = {}
    for c in candles:
        lt = datetime.fromtimestamp(c["time"], timezone.utc).astimezone(ET)
        mins = (lt.hour * 60 + lt.minute) - (9 * 60 + 30)
        groups.setdefault((lt.date(), mins // 240), []).append(c)
    out = []
    for (d, slot), g in sorted(groups.items(), key=lambda kv: kv[1][0]["time"]):
        bar = _merge(g)
        if slot >= 0:  # stamp with the bucket start (09:30 / 13:30 ET) even if the first hour is missing
            bar["time"] = _sec(local_to_utc_naive(d, time(9, 30), ET) + timedelta(minutes=240 * slot))
        out.append(bar)
    return out


def aggregate_weekly(candles: list[dict]) -> list[dict]:
    """Daily (00:00 UTC keyed) -> weekly bars keyed by the week's first trading day."""
    groups: dict[tuple, list[dict]] = {}
    for c in candles:
        d = datetime.fromtimestamp(c["time"], timezone.utc).date()
        groups.setdefault(tuple(d.isocalendar())[:2], []).append(c)
    return [_merge(g) for _, g in sorted(groups.items(), key=lambda kv: kv[1][0]["time"])]


# ---------------------------------------------------------------- providers
_INTRADAY_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}


def _demo(trade: Trade, interval: str, start: datetime, end: datetime) -> list[dict]:
    """Synthetic candles that pass through the trade's fills (Brownian bridge between anchors)."""
    if interval == "1wk":
        return aggregate_weekly(normalize(_demo(trade, "1d", start, end), "1D"))
    rng = random.Random((trade.id or 1) * 31 + len(interval))
    intraday = interval in _INTRADAY_MIN
    step = timedelta(minutes=_INTRADAY_MIN[interval]) if intraday else timedelta(days=1)
    times = []
    if intraday:
        d = utc_naive_to_tz(start, ET).date()
        while d <= utc_naive_to_tz(end, ET).date():
            if d.weekday() < 5:
                t = local_to_utc_naive(d, time(9, 30), ET)
                close = local_to_utc_naive(d, time(16, 0), ET)
                while t < close:
                    if start <= t <= end:
                        times.append(t)
                    t += step
            d += timedelta(days=1)
    else:
        t = start
        while t <= end:
            if utc_naive_to_tz(t, ET).weekday() < 5:
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
    vol = base * ((0.0018 * (_INTRADAY_MIN[interval] / 5) ** 0.5) if intraday else 0.012)
    closes = [anchors[keys[0]]] * len(times)
    for a, b in zip(keys, keys[1:]):
        n = b - a
        walk = [0.0]
        for _ in range(n):
            walk.append(walk[-1] + rng.gauss(0, vol))
        for i in range(n + 1):
            frac = i / n if n else 0
            closes[a + i] = anchors[a] + (anchors[b] - anchors[a]) * frac + walk[i] - walk[-1] * frac
    out, prev = [], closes[0]
    base_vol = 2_000_000 if not intraday else 2_000_000 * _INTRADAY_MIN[interval] / 390
    for tm, c in zip(times, closes):
        o = prev
        hi = max(o, c) + abs(rng.gauss(0, vol * 0.6))
        lo = min(o, c) - abs(rng.gauss(0, vol * 0.6))
        out.append({"time": _sec(tm), "open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2),
                    "close": round(c, 2), "volume": int(base_vol * rng.lognormvariate(0, 0.5))})
        prev = c
    return out


def _schwab(db: Session, symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    from app.sources.schwab_api import SchwabApiSource, SchwabClient
    freq = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15), "30m": ("minute", 30),
            "1d": ("daily", 1), "1wk": ("weekly", 1)}.get(interval)
    if not freq:
        return []
    src = SchwabApiSource()
    if not src.is_configured() or not src.status(db).ready:
        return []
    candles = SchwabClient(db).price_history(symbol, start, end, *freq)
    return [{"time": c["datetime"] // 1000, "open": c["open"], "high": c["high"], "low": c["low"],
             "close": c["close"], "volume": c.get("volume") or 0} for c in candles]


def _polygon(symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    key = get_settings().polygon_api_key
    if not key:
        return []
    mult, span = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"), "30m": (30, "minute"),
                  "60m": (1, "hour"), "1d": (1, "day"), "1wk": (1, "week")}[interval]
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{mult}/{span}/{_ms(start)}/{_ms(end)}"
    r = httpx.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}, timeout=20)
    r.raise_for_status()
    return [{"time": x["t"] // 1000, "open": x["o"], "high": x["h"], "low": x["l"], "close": x["c"],
             "volume": x.get("v") or 0} for x in r.json().get("results", [])]


def _yahoo(symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params={
        "period1": _sec(start), "period2": _sec(end), "interval": interval,
        "includePrePost": "false"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return []
    q = res["indicators"]["quote"][0]
    vols = q.get("volume") or [0] * len(res["timestamp"])
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4),
                    "close": round(c, 4), "volume": vols[i] or 0})
    return out


PROVIDER_LABEL = {"demo": "Synthetic sample prices (not real market data)", "schwab": "Schwab market data",
                  "polygon": "Polygon.io", "yahoo": "Yahoo Finance (unofficial)"}
_MEM: dict[str, tuple[float, list[dict]]] = {}  # short-lived cache for windows that end "now"
_MEM_TTL = 600


def _provider_order(trade: Trade) -> list[str]:
    s = get_settings()
    if trade.is_demo:
        return ["demo"]
    if s.price_provider in ("none", "off", ""):
        return []
    if s.price_provider == "auto":
        return ["schwab", "polygon", "yahoo"]
    return [s.price_provider]


def _fetch(db: Session, prov: str, trade: Trade, interval: str, start: datetime, end: datetime,
           now: datetime) -> list[dict]:
    symbol = trade.underlying
    if prov == "demo":
        return _demo(trade, interval, start, end)
    key = f"{CACHE_VERSION}|{prov}|{symbol}|{interval}|{start:%Y%m%d%H%M}|{end:%Y%m%d%H%M}"
    persistent = end < now - timedelta(hours=12)
    if persistent:
        cached = db.scalar(select(PriceCache).where(PriceCache.cache_key == key))
        if cached:
            return json.loads(cached.payload)
    mem_key = f"{CACHE_VERSION}|{prov}|{symbol}|{interval}|{start:%Y%m%d%H}|{end:%Y%m%d%H}"
    hit = _MEM.get(mem_key)
    if hit and _time.monotonic() - hit[0] < _MEM_TTL:
        return hit[1]
    candles = (_schwab(db, symbol, interval, start, end) if prov == "schwab" else
               _polygon(symbol, interval, start, end) if prov == "polygon" else
               _yahoo(symbol, interval, start, end) if prov == "yahoo" else [])
    if candles:
        if persistent:
            db.add(PriceCache(cache_key=key, provider=prov, payload=json.dumps(candles)))
            db.commit()
        else:
            if len(_MEM) > 200:
                _MEM.clear()
            _MEM[mem_key] = (_time.monotonic(), candles)
    return candles


def get_chart(db: Session, trade: Trade, tf: str | None = None, now: datetime | None = None) -> dict:
    now = _now(now)
    limited = not trade.is_demo  # synthetic sample prices exist for any timeframe
    default = default_timeframe(trade, now, limited)
    options = timeframe_options(trade, now, limited)
    avail = {o["tf"]: o for o in options}
    notes: list[str] = []
    if tf not in TIMEFRAMES:
        tf = default
    elif not avail[tf]["available"]:
        notes.append(f"{tf} isn't available for this trade ({avail[tf]['reason']}); showing {default}.")
        tf = default
    base = {"symbol": trade.underlying, "tf": tf, "interval": tf, "default_tf": default,
            "timeframes": options, "swing": is_swing(trade, now), "time_known": trade.time_known,
            "intraday": tf not in DAILYISH}
    order = _provider_order(trade)
    errors: list[str] = []
    tried = [tf] + (["1D"] if tf != "1D" else [])
    for cur in tried:
        spec = TIMEFRAMES[cur]
        start, end, wnotes = tf_window(trade, cur, now, limited)
        for prov in order:
            try:
                candles = _fetch(db, prov, trade, spec.fetch, start, end, now)
                if cur == "4h":
                    candles = aggregate_4h(normalize(candles, "1h"))
                candles = normalize(candles, cur)
            except Exception as exc:  # network / auth errors -> try next provider
                log.info("price provider %s failed for %s %s: %s", prov, trade.underlying, cur, exc)
                errors.append(f"{prov}: {exc}"[:300])
                continue
            if candles:
                if cur != tf:
                    notes.append(f"No {tf} price data was returned; showing {cur}.")
                mk, hidden = markers(trade, candles, cur)
                if hidden:
                    notes.append(f"{hidden} fill(s) fall outside the {cur} chart window.")
                if cur not in DAILYISH and any(not _fill_time_known(f) for f in trade.fills):
                    notes.append("Some fills have a date but no time of day; each is drawn at the first bar "
                                 "that traded at its price that day, marked \"time est.\".")
                return {**base, "tf": cur, "interval": cur, "intraday": cur not in DAILYISH,
                        "candles": candles, "markers": mk, "focus": focus(trade, candles, cur),
                        "focus_bars": [spec.focus_before, spec.focus_after], "provider": prov,
                        "provider_label": PROVIDER_LABEL.get(prov, prov), "notes": notes + wnotes}
    return {**base, "candles": [], "markers": [], "provider": None, "notes": notes, "errors": errors}


# ---------------------------------------------------------------- markers, focus, excursions
def _fill_time_known(f) -> bool:
    return bool(f.execution.time_known) if f.execution is not None else False


def _bar_index(times: list[int], ts_utc: datetime, tf: str, time_known: bool = True) -> int | None:
    """Index of the bar containing the timestamp (date-only fills: the day's last bar)."""
    d = utc_naive_to_tz(ts_utc, ET).date()
    if tf in DAILYISH:
        i = bisect_right(times, _date_key(d)) - 1
        if i < 0 or (tf == "1D" and times[i] != _date_key(d)):
            return None
        if tf == "1W" and _date_key(d) - times[i] >= 7 * 86400:
            return None
        return i
    key = _sec(local_to_utc_naive(d, time(16, 0), ET)) - 1 if not time_known else _sec(ts_utc)
    i = bisect_right(times, key) - 1
    if i < 0 or _et_date_of_sec(times[i]) != d:
        return None
    return i


def place_fills(trade: Trade, candles: list[dict], tf: str) -> list[tuple]:
    """[(fill, bar index | None, how)] in trade order. how: 'time' (bar containing the fill time),
    'price' (date-only fill: first bar that day, not before the previous fill, whose range contains
    the fill price), 'day' (date-only fill whose price never traded in the bars: first bar of the
    day for opening fills, last bar for closing fills), 'date' (daily/weekly charts)."""
    times = [c["time"] for c in candles]
    out, prev = [], 0
    for f in trade.fills:
        known = _fill_time_known(f)
        if not times:
            out.append((f, None, "none"))
            continue
        if tf in DAILYISH:
            i, how = _bar_index(times, f.executed_at, tf, known), "date"
        elif known:
            i, how = _bar_index(times, f.executed_at, tf, True), "time"
        else:
            d = utc_naive_to_tz(f.executed_at, ET).date()
            lo = bisect_right(times, _sec(local_to_utc_naive(d, time(0, 0), ET)) - 1)
            hi = bisect_right(times, _sec(local_to_utc_naive(d, time(23, 59), ET)))
            day = [j for j in range(lo, hi) if j >= prev]
            tol = max(0.01, abs(f.price) * 0.0005)
            hit = next((j for j in day if candles[j]["low"] - tol <= f.price <= candles[j]["high"] + tol), None)
            if hit is not None and trade.asset_type == "STOCK":
                i, how = hit, "price"
            elif day:
                i, how = (day[0] if f.role == "OPEN" else day[-1]), "day"
            else:
                i, how = None, "none"
        if i is not None:
            prev = max(prev, i)
        out.append((f, i, how))
    return out


def markers(trade: Trade, candles: list[dict], tf: str = "1D") -> tuple[list[dict], int]:
    times = [c["time"] for c in candles]
    out, hidden = [], 0
    for f, i, how in place_fills(trade, candles, tf):
        if i is None:
            hidden += 1
            continue
        buy = f.side == "BUY"
        text = f"{'B' if buy else 'S'} {f.quantity:g} @ {f.price:g}"
        if how == "price":
            text += " (time est.)"
        elif how == "day":
            text += " (time n/a)"
        out.append({"time": times[i], "position": "belowBar" if buy else "aboveBar",
                    "color": "#22c55e" if buy else "#ef4444", "shape": "arrowUp" if buy else "arrowDown",
                    "text": text})
    return sorted(out, key=lambda m: m["time"]), hidden


def focus(trade: Trade, candles: list[dict], tf: str) -> dict | None:
    """Bar times of entry and exit (or last bar for open trades), to frame the visible range."""
    times = [c["time"] for c in candles]
    if not times:
        return None
    idx = [i for _, i, _ in place_fills(trade, candles, tf) if i is not None]
    if not idx:
        i = 0 if _sec(trade.opened_at) < times[0] else len(times) - 1
        return {"from": times[i], "to": times[-1] if not trade.closed_at else times[i]}
    j = max(idx) if trade.closed_at else len(times) - 1
    return {"from": times[min(idx)], "to": times[max(min(idx), j)]}


# ---------------------------------------------------------------- MFE / MAE
def excursion_tf(trade: Trade, now: datetime | None = None) -> str:
    """Finest bars available for the whole holding period (Yahoo limits; sample data: 1m/5m)."""
    now = _now(now)
    d0 = utc_naive_to_tz(trade.opened_at, ET).date()
    d1 = utc_naive_to_tz(trade.closed_at or now, ET).date()
    start = local_to_utc_naive(d0, time(9, 30), ET)
    if (d1 - d0).days <= 6 and (trade.is_demo or start >= now - timedelta(days=29)):
        return "1m"
    if trade.is_demo or start >= now - timedelta(days=59):
        return "5m"
    if start >= now - timedelta(days=729):
        return "1h"
    return "1D"


def running_excursions(trade: Trade, candles: list[dict], placed: list[tuple]) -> tuple[float, float] | None:
    """Best / worst running P&L ($, before fees) of the trade, bar by bar from the first to the last fill:
    realized P&L so far + the open position marked at each bar's high and low. Opening fills are applied
    before a bar is evaluated and closing fills after it (bar precision)."""
    if not placed or any(i is None for _, i, _ in placed):
        return None
    by_bar: dict[int, list] = {}
    for f, i, _ in placed:
        by_bar.setdefault(i, []).append(f)
    first = min(by_bar)
    last = max(by_bar) if trade.closed_at else len(candles) - 1
    mult = trade.multiplier or 1.0
    pos = cost = realized = 0.0
    best, worst = 0.0, 0.0

    def apply(f):
        nonlocal pos, cost, realized
        signed = f.quantity if f.side == "BUY" else -f.quantity
        if pos == 0 or (pos > 0) == (signed > 0):
            cost = (cost * abs(pos) + f.price * abs(signed)) / (abs(pos) + abs(signed))
            pos += signed
        else:
            closed = min(abs(signed), abs(pos))
            realized += (f.price - cost) * closed * (1 if pos > 0 else -1) * mult
            pos += signed
            if abs(pos) > 1e-9 and (pos > 0) == (signed > 0):  # flipped through zero
                cost = f.price
            if abs(pos) < 1e-9:
                pos = 0.0

    for i in range(first, last + 1):
        fills = by_bar.get(i, [])
        for f in fills:
            if f.role == "OPEN":
                apply(f)
        if pos:
            for px in (candles[i]["high"], candles[i]["low"]):
                pnl = realized + (px - cost) * pos * mult
                best, worst = max(best, pnl), min(worst, pnl)
        for f in fills:
            if f.role != "OPEN":
                apply(f)
        best, worst = max(best, realized), min(worst, realized)
    return round(best, 2), round(worst, 2)


_EXC_MEMO: dict[tuple, tuple] = {}


def compute_trade_excursions(db: Session, trade: Trade, now: datetime | None = None) -> dict:
    """MFE/MAE for a stock trade from the finest available bars over the actual holding period.
    Returns {"mfe", "mae", "tf", "estimated", "unplaced", "provider"} (mfe/mae None if unavailable)."""
    now = _now(now)
    out = {"mfe": None, "mae": None, "tf": None, "estimated": 0, "unplaced": 0, "provider": None}
    if trade.asset_type != "STOCK" or not trade.fills:
        return out
    tf = excursion_tf(trade, now)
    sig = (trade.id, tf, trade.closed_at, tuple((f.side, f.quantity, f.price, f.executed_at, _fill_time_known(f))
                                               for f in trade.fills), now.date() if not trade.closed_at else None)
    if sig in _EXC_MEMO and trade.closed_at:
        return dict(_EXC_MEMO[sig])
    d0 = utc_naive_to_tz(trade.opened_at, ET).date()
    d1 = utc_naive_to_tz(trade.closed_at or now, ET).date()
    if tf in DAILYISH:
        start, end = trade.opened_at - timedelta(days=3), min((trade.closed_at or now) + timedelta(days=3), now)
    else:
        start = local_to_utc_naive(d0, time(9, 25), ET)
        end = min(local_to_utc_naive(d1, time(16, 5), ET), now)
    fetch = {"1m": "1m", "5m": "5m", "1h": "60m", "1D": "1d"}[tf]
    for prov in _provider_order(trade):
        try:
            candles = normalize(_fetch(db, prov, trade, fetch, start, end, now), tf)
        except Exception as exc:
            log.info("excursions: provider %s failed for %s: %s", prov, trade.underlying, exc)
            continue
        if not candles:
            continue
        placed = place_fills(trade, candles, tf)
        res = running_excursions(trade, candles, placed)
        if res is None:
            out.update(tf=tf, unplaced=sum(1 for _, i, _ in placed if i is None), provider=prov)
            return out
        out.update(mfe=res[0], mae=res[1], tf=tf, provider=prov,
                   estimated=sum(1 for _, _, how in placed if how in ("price", "day")))
        if len(_EXC_MEMO) > 2000:
            _EXC_MEMO.clear()
        _EXC_MEMO[sig] = dict(out)
        return out
    return out


def update_trade_excursions(db: Session, trade: Trade, now: datetime | None = None) -> dict:
    res = compute_trade_excursions(db, trade, now)
    if res["tf"] and (trade.mfe != res["mfe"] or trade.mae != res["mae"]):
        trade.mfe, trade.mae = res["mfe"], res["mae"]
    return res


def recompute_all_excursions(db: Session, now: datetime | None = None) -> dict:
    """Recompute MFE/MAE for every trade (bars are cached, so re-runs are cheap)."""
    changed = total = failed = 0
    for t in db.scalars(select(Trade).order_by(Trade.opened_at)):
        before = (t.mfe, t.mae)
        res = update_trade_excursions(db, t, now)
        total += 1
        if t.asset_type == "STOCK" and not res["tf"]:
            failed += 1
        if (t.mfe, t.mae) != before:
            changed += 1
        db.commit()
    return {"trades": total, "changed": changed, "no_data": failed}


def excursion_basis(trade: Trade, now: datetime | None = None) -> dict:
    """What compute_trade_excursions will use, without fetching prices (for the page tooltip)."""
    if trade.asset_type != "STOCK":
        return {"tf": None}
    return {"tf": excursion_tf(trade, now), "estimated": sum(1 for f in trade.fills if not _fill_time_known(f))}


def excursion_note(res: dict | None) -> str:
    base = ("MFE / MAE: the best and worst running P&L ($, before fees) while the trade was open: "
            "realized P&L so far plus the open shares marked at each bar's high and low.")
    if not res or not res.get("tf"):
        return base + " Not available for this trade (options, or no price data)."
    name = {"1m": "1-minute", "5m": "5-minute", "1h": "hourly", "1D": "daily"}[res["tf"]]
    s = f" Computed from {name} bars (bar precision)."
    if res.get("estimated"):
        s += (f" {res['estimated']} fill(s) have no time of day, so each is placed at the first bar that traded "
              "at its price that day: an approximation.")
    return base + s
