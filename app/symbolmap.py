"""Instrument mapping (broker symbol -> journal symbol, ISIN, exchange, Yahoo price symbol) and per-trade lookups."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets
from app.models import Instrument


def upsert_t212(db: Session, ticker: str, isin: str | None, name: str | None, currency: str | None,
                short_name: str | None = None) -> Instrument:
    """Create/refresh the Instrument row for a Trading 212 ticker. Manual overrides are never overwritten."""
    info = markets.parse_t212_ticker(ticker, isin)
    # shortName usually repeats the ticker (AAPL). After a rename the catalogue's shortName is the
    # new symbol (DMYI order -> IONQ) while history and positions still use the old ticker. Adopting
    # that name splits one position across two symbols, so only an exact match is applied.
    sym = info.symbol
    if short_name and short_name.strip().upper() == (info.symbol or "").upper():
        sym = short_name.strip()
    ccy = (currency or "").upper() or None
    scale = 1.0
    if ccy in ("GBX", "GBP") and info.exchange == "LSE":
        scale = 0.01 if ccy == "GBX" else 1.0
    if ccy == "GBX":
        ccy = "GBP"          # the journal stores pounds; prices are divided by 100 at ingest
    row = db.scalar(select(Instrument).where(Instrument.source_key == "t212", Instrument.broker_symbol == ticker))
    # Journal symbol stays the history ticker. A renamed listing (DMYI order, catalogue IONQ) is only
    # the Yahoo symbol, so one position is not split across two symbols.
    from app.corporate import chart_symbol
    ysym = chart_symbol(sym, info.exchange, short_name)
    if row is None:
        row = Instrument(source_key="t212", broker_symbol=ticker, isin=isin, symbol=sym, name=name,
                         exchange=info.exchange, currency=ccy, asset_type="STOCK", price_symbol=ysym,
                         price_scale=scale)
        db.add(row)
    elif not row.user_override:
        row.isin, row.name, row.currency = isin or row.isin, name or row.name, ccy or row.currency
        row.symbol, row.exchange, row.price_symbol = sym, info.exchange, ysym
        row.price_scale = scale
    db.flush()
    return row


def upsert_ibkr(db: Session, broker_symbol: str, isin: str | None, name: str | None, currency: str | None,
                exchange: str | None, display_symbol: str | None = None) -> Instrument:
    """Create/refresh the Instrument row for an IBKR conid or ticker. Manual overrides are never overwritten."""
    key = (broker_symbol or "").strip()
    sym = (display_symbol or key).strip().upper() or key
    ccy = (currency or "").upper() or None
    if ccy == "GBX":
        ccy = "GBP"
    ysym = markets.yahoo_symbol_for(sym, exchange)
    row = db.scalar(select(Instrument).where(Instrument.source_key == "ibkr_flex", Instrument.broker_symbol == key))
    if row is None:
        row = Instrument(source_key="ibkr_flex", broker_symbol=key, isin=isin, symbol=sym, name=name,
                         exchange=exchange, currency=ccy, asset_type="STOCK", price_symbol=ysym, price_scale=1.0)
        db.add(row)
    elif not row.user_override:
        row.isin, row.name, row.currency = isin or row.isin, name or row.name, ccy or row.currency
        row.symbol, row.exchange, row.price_symbol = sym, exchange or row.exchange, ysym or row.price_symbol
    db.flush()
    return row


def instrument_of(t) -> Instrument | None:
    return getattr(t, "instrument", None)


def is_us(t) -> bool:
    """Schwab-style rows (no instrument) and US-listed instruments use the New York session logic."""
    inst = instrument_of(t)
    return inst is None or (inst.exchange or "US") == "US"


def price_symbol(t) -> str:
    """Symbol for the Yahoo price feeds: the mapped one (e.g. VUSA.L) or the journal underlying."""
    from app.symbols import BUILTIN
    inst = instrument_of(t)
    ps = inst.price_symbol if inst is not None and inst.price_symbol else t.underlying
    # a retired / when-issued ticker (HONAV) has no price feed: read the verified new one (HONA)
    return BUILTIN[ps][0] if ps in BUILTIN else ps


def market_tz(t, default: str) -> str:
    inst = instrument_of(t)
    return markets.exchange_tz(inst.exchange, default) if inst is not None and inst.exchange else default
