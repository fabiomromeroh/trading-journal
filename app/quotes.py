"""Latest free quotes for open positions (unrealized P&L).

Yahoo Finance's chart endpoint (no key) with 1-minute bars incl. pre/after-hours: the price is the last
1m close (extended hours included) or regularMarketPrice, whichever is newer. Cached in memory for
TTL seconds so a dashboard load costs at most one request per symbol every ~90 s; Sync now clears it.
thinkorswim's "P/L Open" uses the mark (bid/ask mid), which no free source offers, so small
differences vs ToS are expected; the time of the price is always shown.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

TTL = 90.0
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_cache: dict[str, tuple[float, "Quote | None"]] = {}
_lock = threading.Lock()


@dataclass
class Quote:
    symbol: str          # the journal symbol
    price: float
    at: datetime         # UTC time of that price
    session: str         # pre | regular | post | closed (when the price was printed)
    market: str          # pre | regular | post | closed (now)
    regular_price: float | None = None
    prev_close: float | None = None
    source: str = "Yahoo"


def yahoo_symbol(t) -> str:
    """Journal trade -> Yahoo ticker (OCC symbol for options, '-' for class shares)."""
    if getattr(t, "asset_type", "STOCK") == "OPTION" and getattr(t, "expiration", None) and getattr(t, "strike", None):
        cp = "C" if (t.option_type or "").upper().startswith("C") else "P"
        return f"{t.underlying}{t.expiration:%y%m%d}{cp}{int(round(t.strike * 1000)):08d}"
    return (getattr(t, "underlying", None) or t.symbol).replace(".", "-").replace("/", "-")


def _period(ts: int, tp: dict) -> str:
    for name in ("pre", "regular", "post"):
        p = tp.get(name) or {}
        if p.get("start") is not None and p["start"] <= ts < p.get("end", 0):
            return name
    return "closed"


def parse_chart(symbol: str, data: dict, now: float | None = None) -> Quote | None:
    """Pick the freshest price out of a v8 chart response."""
    res = ((data or {}).get("chart") or {}).get("result") or []
    if not res:
        return None
    res = res[0]
    meta = res.get("meta") or {}
    tp = meta.get("currentTradingPeriod") or {}
    best_ts, best_px = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
    for ts, c in zip(reversed(res.get("timestamp") or []), reversed(closes)):
        if c is not None:
            if best_ts is None or ts >= best_ts:
                best_ts, best_px = ts, c
            break
    if best_px is None or best_ts is None:
        return None
    now = time.time() if now is None else now
    return Quote(symbol=symbol, price=round(float(best_px), 4), at=datetime.fromtimestamp(best_ts, timezone.utc),
                 session=_period(int(best_ts), tp), market=_period(int(now), tp),
                 regular_price=meta.get("regularMarketPrice"), prev_close=meta.get("chartPreviousClose"))


def _fetch(ysym: str) -> dict:
    r = httpx.get(URL.format(sym=ysym), params={"interval": "1m", "range": "1d", "includePrePost": "true"},
                  headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
    r.raise_for_status()
    return r.json()


def get_quotes(trades, fetch=_fetch) -> dict[str, Quote]:
    """Quotes for the open trades' symbols (journal symbol -> Quote). Missing/failed symbols are omitted."""
    want = {}
    for t in trades:
        if getattr(t, "status", "OPEN") == "OPEN":
            want[t.symbol] = yahoo_symbol(t)
    out, todo, mono = {}, [], time.monotonic()
    with _lock:
        for sym, ysym in want.items():
            hit = _cache.get(ysym)
            if hit and mono - hit[0] < TTL:
                if hit[1]:
                    out[sym] = hit[1]
            else:
                todo.append((sym, ysym))

    def one(item):
        sym, ysym = item
        try:
            return sym, ysym, parse_chart(sym, fetch(ysym))
        except Exception:
            return sym, ysym, None

    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            results = list(ex.map(one, todo))
        with _lock:
            for sym, ysym, q in results:
                _cache[ysym] = (time.monotonic(), q)
                if q:
                    out[sym] = q
    return out


def enabled() -> bool:
    import os
    return os.environ.get("QUOTE_PROVIDER", "yahoo").lower() not in ("none", "off", "0", "")


def clear_cache() -> None:
    with _lock:
        _cache.clear()
