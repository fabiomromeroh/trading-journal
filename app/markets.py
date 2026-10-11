"""Exchange metadata for non-US listings: Yahoo suffix, time zone, regular session, and Trading 212 ticker parsing.

Trading 212 tickers are ``<BASE>_<CC>_EQ`` for US-style listings (``AAPL_US_EQ``) and ``<BASE><x>_EQ`` for
European listings where the lowercase letter marks the exchange (``VUSAl_EQ`` London, ``SAPd_EQ`` Xetra).
The letter convention is not in the official docs; it is observed from the instrument catalogue
(community-verified for l/d/a/p/m/s/b/i/h/c/o). Anything unrecognised keeps the ticker, gets no price symbol
and is flagged in Settings so the user can type the Yahoo symbol (manual override wins over this table)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class Exchange:
    code: str           # journal market code
    yahoo: str          # Yahoo symbol suffix ('' for US)
    tz: str
    open: time
    close: time
    name: str


US = Exchange("US", "", "America/New_York", time(9, 30), time(16, 0), "US (NYSE/Nasdaq)")
EXCHANGES: dict[str, Exchange] = {
    "US": US,
    "LSE": Exchange("LSE", ".L", "Europe/London", time(8, 0), time(16, 30), "London (LSE)"),
    "XETRA": Exchange("XETRA", ".DE", "Europe/Berlin", time(9, 0), time(17, 30), "Xetra (Frankfurt)"),
    "AMS": Exchange("AMS", ".AS", "Europe/Amsterdam", time(9, 0), time(17, 30), "Euronext Amsterdam"),
    "PAR": Exchange("PAR", ".PA", "Europe/Paris", time(9, 0), time(17, 30), "Euronext Paris"),
    "MIL": Exchange("MIL", ".MI", "Europe/Rome", time(9, 0), time(17, 30), "Borsa Italiana (Milan)"),
    "SWX": Exchange("SWX", ".SW", "Europe/Zurich", time(9, 0), time(17, 30), "SIX Swiss"),
    "BRU": Exchange("BRU", ".BR", "Europe/Brussels", time(9, 0), time(17, 30), "Euronext Brussels"),
    "DUB": Exchange("DUB", ".IR", "Europe/Dublin", time(8, 0), time(16, 30), "Euronext Dublin"),
    "HEL": Exchange("HEL", ".HE", "Europe/Helsinki", time(10, 0), time(18, 30), "Nasdaq Helsinki"),
    "CPH": Exchange("CPH", ".CO", "Europe/Copenhagen", time(9, 0), time(17, 0), "Nasdaq Copenhagen"),
    "OSL": Exchange("OSL", ".OL", "Europe/Oslo", time(9, 0), time(16, 20), "Oslo Børs"),
    "MAD": Exchange("MAD", ".MC", "Europe/Madrid", time(9, 0), time(17, 30), "Bolsa de Madrid"),
}
LETTER_TO_EXCHANGE = {"l": "LSE", "d": "XETRA", "a": "AMS", "p": "PAR", "m": "MIL", "s": "SWX", "b": "BRU",
                      "i": "DUB", "h": "HEL", "c": "CPH", "o": "OSL", "e": "MAD"}
COUNTRY_TO_EXCHANGE = {"US": "US", "GB": "LSE", "DE": "XETRA", "NL": "AMS", "FR": "PAR", "IT": "MIL", "CH": "SWX",
                       "BE": "BRU", "IE": "DUB", "FI": "HEL", "DK": "CPH", "NO": "OSL", "ES": "MAD"}
ISIN_PREFIX_TO_EXCHANGE = {"US": "US", "GB": "LSE", "DE": "XETRA", "NL": "AMS", "FR": "PAR", "IT": "MIL", "CH": "SWX"}


@dataclass
class TickerInfo:
    base: str               # T212 base (e.g. VUSA, SAP, AAPL, BRK_B)
    exchange: str | None    # key of EXCHANGES, None when unknown
    symbol: str             # journal display symbol ('BRK.B')


_US_RE = re.compile(r"^(?P<base>[A-Z0-9_]+?)_(?P<cc>[A-Z]{2})_(?P<kind>[A-Z]+)$")
_EU_RE = re.compile(r"^(?P<base>[A-Z0-9]+)(?P<ltr>[a-z])_(?P<kind>[A-Z]+)$")


def parse_t212_ticker(ticker: str, isin: str | None = None) -> TickerInfo:
    t = (ticker or "").strip()
    m = _US_RE.match(t)
    if m:
        ex = COUNTRY_TO_EXCHANGE.get(m["cc"])
        return TickerInfo(m["base"], ex, m["base"].replace("_", "."))
    m = _EU_RE.match(t)
    if m:
        ex = LETTER_TO_EXCHANGE.get(m["ltr"])
        if ex:
            return TickerInfo(m["base"], ex, m["base"])
    m = re.match(r"^(?P<base>[A-Za-z0-9]+)_(?P<kind>[A-Z]+)$", t)   # e.g. 'VOD_EQ' (no marker)
    if m:
        ex = ISIN_PREFIX_TO_EXCHANGE.get((isin or "")[:2].upper())
        return TickerInfo(m["base"], ex, m["base"])
    return TickerInfo(t.split("_")[0] or t, None, t.split("_")[0] or t)


def yahoo_symbol_for(symbol: str, exchange: str | None) -> str | None:
    if exchange is None:
        return None
    ex = EXCHANGES.get(exchange)
    if ex is None:
        return None
    base = symbol.replace(".", "-") if exchange == "US" else symbol
    return base + ex.yahoo


def exchange_tz(exchange: str | None, default: str = "America/New_York") -> str:
    ex = EXCHANGES.get(exchange or "US")
    return ex.tz if ex else default


_MAJOR = {"GBP", "USD", "EUR", "CHF", "CAD", "AUD", "JPY", "SEK", "NOK", "DKK", "HKD", "SGD"}


def pence_factor(meta_currency: str | None, trade_currency: str | None, price_scale: float | None = None) -> float:
    """Scale a Yahoo quote into the instrument's major currency, once.

    LSE lines come back as pence ('GBp' / 'GBX') even when the journal stores pounds. Divide those by 100
    regardless of the currency we passed in. When Yahoo already names a major currency (GBP, USD, EUR, …)
    do not also apply the instrument's stored price_scale — that would turn a pound quote into pence.
    price_scale (0.01 on known GBX listings) is used only when Yahoo omits the currency."""
    meta = (meta_currency or "").strip()
    if meta in ("GBp", "GBX", "GBx"):
        return 0.01
    if meta.upper() in _MAJOR:
        return 1.0
    try:
        sc = float(price_scale) if price_scale not in (None, "") else 1.0
    except (TypeError, ValueError):
        sc = 1.0
    if sc > 0 and sc != 1.0:
        return sc
    return 1.0
