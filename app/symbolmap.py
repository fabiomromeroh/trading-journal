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
    sym = (short_name or info.symbol).strip() or info.symbol
    if short_name and info.exchange == "US":
        sym = short_name
    ccy = (currency or "").upper() or None
    scale = 1.0
    if ccy in ("GBX", "GBP") and info.exchange == "LSE":
        scale = 0.01 if ccy == "GBX" else 1.0
    if ccy == "GBX":
        ccy = "GBP"          # the journal stores pounds; prices are divided by 100 at ingest
    row = db.scalar(select(Instrument).where(Instrument.source_key == "t212", Instrument.broker_symbol == ticker))
    ysym = markets.yahoo_symbol_for(sym, info.exchange)
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


def instrument_of(t) -> Instrument | None:
    return getattr(t, "instrument", None)


def is_us(t) -> bool:
    """Schwab-style rows (no instrument) and US-listed instruments use the New York session logic."""
    inst = instrument_of(t)
    return inst is None or (inst.exchange or "US") == "US"


def price_symbol(t) -> str:
    """Symbol for the Yahoo price feeds: the mapped one (e.g. VUSA.L) or the journal underlying."""
    inst = instrument_of(t)
    if inst is not None and inst.price_symbol:
        return inst.price_symbol
    return t.underlying


def market_tz(t, default: str) -> str:
    inst = instrument_of(t)
    return markets.exchange_tz(inst.exchange, default) if inst is not None and inst.exchange else default
