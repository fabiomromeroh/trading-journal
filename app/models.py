from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text,
    UniqueConstraint, Index, event, false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (all datetimes in the DB are naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    broker: Mapped[str] = mapped_column(String(40), default="schwab")
    account_number_masked: Mapped[str | None] = mapped_column(String(40))  # e.g. "...123"
    external_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. Schwab hashValue
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Multi-broker: defaults mean "a US/USD Schwab-style account", i.e. exactly the pre-multi-broker behaviour.
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    timezone: Mapped[str] = mapped_column(String(40), default="America/New_York", server_default="America/New_York")
    market: Mapped[str] = mapped_column(String(8), default="US", server_default="US")
    kind: Mapped[str | None] = mapped_column(String(16))        # invest | isa | margin | cash
    source_key: Mapped[str | None] = mapped_column(String(30))  # adapter that owns it: t212 | ibkr_flex | ...
    # 5-minute auto stop. Off unless the account is Schwab-family (see _account_auto_stop). New brokers stay off.
    auto_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false())


_SCHWAB_BROKERS = {"schwab", "snaptrade", "thinkorswim", "tos"}
_SCHWAB_SOURCES = {"snaptrade", "schwab", "schwab_api", "schwab_csv", "tos_email", "tos_statement"}
_OFF_BROKERS = {"trading212", "t212", "ibkr", "ibkr_flex", "interactivebrokers", "interactive brokers", "demo"}
_OFF_SOURCES = {"t212", "trading212", "ibkr", "ibkr_flex"}


def schwab_like(broker: str | None, source_key: str | None) -> bool:
    """True only for Schwab / SnapTrade / thinkorswim / Schwab CSV. Trading 212, IBKR, demo and anything else are off."""
    b = (broker or "").strip().lower()
    s = (source_key or "").strip().lower()
    if b in _OFF_BROKERS or s in _OFF_SOURCES:
        return False
    return b in _SCHWAB_BROKERS or s in _SCHWAB_SOURCES


@event.listens_for(Account, "init")
def _account_auto_stop(target, args, kwargs):
    """Fill auto_stop when the caller did not pass it. Explicit True/False is kept."""
    if kwargs.get("auto_stop") is None:
        kwargs["auto_stop"] = schwab_like(kwargs.get("broker", "schwab"), kwargs.get("source_key"))


class Instrument(Base):
    """Broker instrument -> market symbol mapping (ISIN, exchange, Yahoo price symbol, pence scaling)."""
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("source_key", "broker_symbol", name="uq_instrument_broker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(30))
    broker_symbol: Mapped[str] = mapped_column(String(64))      # e.g. Trading 212 "VUSAl_EQ"
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    symbol: Mapped[str] = mapped_column(String(32))             # display ticker, e.g. "VUSA"
    name: Mapped[str | None] = mapped_column(String(120))
    exchange: Mapped[str | None] = mapped_column(String(12))    # US | LSE | XETRA | AMS | PAR | ...
    currency: Mapped[str | None] = mapped_column(String(3))     # trading currency of the instrument
    asset_type: Mapped[str] = mapped_column(String(16), default="STOCK")
    price_symbol: Mapped[str | None] = mapped_column(String(32))  # Yahoo symbol, e.g. "VUSA.L"
    price_scale: Mapped[float] = mapped_column(Float, default=1.0)  # 0.01 when the price feed quotes pence (GBX)
    user_override: Mapped[bool] = mapped_column(Boolean, default=False)  # symbol/price_symbol typed by the user


class CompanyProfile(Base):
    """Company name, sector and industry for one Yahoo symbol. Filled in the background; kept 90 days."""
    __tablename__ = "company_profiles"
    __table_args__ = (
        Index("ix_company_profiles_sector", "sector"),
        Index("ix_company_profiles_industry", "industry"),
    )
    yahoo_symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(160))
    sector: Mapped[str | None] = mapped_column(String(80))
    industry: Mapped[str | None] = mapped_column(String(120))
    quote_type: Mapped[str | None] = mapped_column(String(20))  # EQUITY | ETF | ...
    source: Mapped[str | None] = mapped_column(String(20))      # yahoo | t212 | t212+yahoo | fallback | miss
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class FxRate(Base):
    """Daily ECB reference rate cache: 1 EUR = rate x quote (Frankfurter API). Weekends use the previous business day."""
    __tablename__ = "fx_rates"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    quote: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate: Mapped[float] = mapped_column(Float)


class Dividend(Base):
    """A cash dividend the broker actually paid (never estimated). Amount is the net cash received."""
    __tablename__ = "dividends"
    __table_args__ = (
        UniqueConstraint("account_id", "source", "external_id", name="uq_div_source_ext"),
        Index("ix_div_account_symbol", "account_id", "symbol"),
        Index("ix_div_account_paid", "account_id", "paid_on"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    symbol: Mapped[str] = mapped_column(String(64))
    paid_on: Mapped[datetime] = mapped_column(DateTime)  # naive UTC
    amount: Mapped[float] = mapped_column(Float)  # net cash in ``currency`` (GBP not GBX)
    currency: Mapped[str | None] = mapped_column(String(3))
    amount_account: Mapped[float | None] = mapped_column(Float)  # net cash in the account currency
    account_currency: Mapped[str | None] = mapped_column(String(3))
    fx_to_account: Mapped[float | None] = mapped_column(Float)  # instrument -> account when the broker gave one
    quantity: Mapped[float | None] = mapped_column(Float)
    gross_per_share: Mapped[float | None] = mapped_column(Float)  # instrument currency, pounds not pence
    kind: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str | None] = mapped_column(String(64))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="SET NULL"))
    raw: Mapped[str | None] = mapped_column(Text)
    account: Mapped[Account] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)
    filename: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(40))  # schwab_csv | tos_statement
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|committed|undone
    content: Mapped[str | None] = mapped_column(Text)  # raw file text, kept while pending
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_trades: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    account: Mapped[Account | None] = relationship()


class Execution(Base):
    """A single fill / position event from any source (CSV, API, demo)."""
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("account_id", "source", "external_id", name="uq_exec_source_ext"),
        Index("ix_exec_account_symbol", "account_id", "symbol"),
        Index("ix_exec_match", "account_id", "match_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(30))  # schwab_csv|tos_statement|schwab_api|demo
    external_id: Mapped[str] = mapped_column(String(200))
    match_key: Mapped[str] = mapped_column(String(200))
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))
    symbol: Mapped[str] = mapped_column(String(64))
    underlying: Mapped[str] = mapped_column(String(32))
    asset_type: Mapped[str] = mapped_column(String(16))  # STOCK | OPTION
    option_type: Mapped[str | None] = mapped_column(String(4))  # CALL | PUT
    strike: Mapped[float | None] = mapped_column(Float)
    expiration: Mapped[date | None] = mapped_column(Date)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    side: Mapped[str | None] = mapped_column(String(4))  # BUY | SELL | None (derive for expirations)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(DateTime)  # naive UTC
    time_known: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str | None] = mapped_column(String(3))      # instrument currency (None = the account's)
    fx_to_account: Mapped[float | None] = mapped_column(Float)   # instrument ccy -> account ccy at the fill (None = 1)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="SET NULL"))
    settle_date: Mapped[date | None] = mapped_column(Date)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    position_effect: Mapped[str | None] = mapped_column(String(8))  # OPEN | CLOSE
    kind: Mapped[str] = mapped_column(String(16), default="TRADE")  # TRADE|EXPIRATION|ASSIGNMENT|EXERCISE
    description: Mapped[str | None] = mapped_column(String(300))
    raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    account: Mapped[Account] = relationship()


trade_tags = Table(
    "trade_tags", Base.metadata,
    Column("trade_id", ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)


class Trade(Base):
    """A round-trip trade built from executions by the trade builder."""
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)  # stable identity across rebuilds
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    asset_type: Mapped[str] = mapped_column(String(16))
    option_type: Mapped[str | None] = mapped_column(String(4))
    strike: Mapped[float | None] = mapped_column(Float)
    expiration: Mapped[date | None] = mapped_column(Date)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    direction: Mapped[str] = mapped_column(String(5))  # LONG | SHORT
    status: Mapped[str] = mapped_column(String(8), index=True)  # OPEN | CLOSED
    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    time_known: Mapped[bool] = mapped_column(Boolean, default=True)
    quantity: Mapped[float] = mapped_column(Float)  # max position size
    open_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    return_pct: Mapped[float | None] = mapped_column(Float)
    close_reason: Mapped[str | None] = mapped_column(String(16))  # TRADE|EXPIRATION|ASSIGNMENT|EXERCISE
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str | None] = mapped_column(String(3))      # instrument currency (None = the account's)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="SET NULL"))
    instrument: Mapped["Instrument | None"] = relationship(foreign_keys=[instrument_id])
    # Journal fields (preserved across rebuilds)
    notes: Mapped[str | None] = mapped_column(Text)
    setup: Mapped[str | None] = mapped_column(String(80))
    rating: Mapped[int | None] = mapped_column(Integer)
    mfe: Mapped[float | None] = mapped_column(Float)  # $ max favourable excursion
    mae: Mapped[float | None] = mapped_column(Float)  # $ max adverse excursion
    # Planned risk (journal, preserved across rebuilds): R-multiple = net P&L / risk
    initial_stop: Mapped[float | None] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float)   # $ risk; overrides the stop-based risk
    profit_target: Mapped[float | None] = mapped_column(Float)
    stop_auto: Mapped[bool | None] = mapped_column(Boolean)      # True: initial_stop was set by the default-stop rule
    stop_src: Mapped[str | None] = mapped_column(String(8))      # how the auto stop was computed: '5m' | 'daily'
    stop_raw: Mapped[float | None] = mapped_column(Float)        # the day's low/high before entry, without the buffer
    stop_bar: Mapped[int | None] = mapped_column(Integer)        # epoch of the bar that printed it (UTC seconds; daily: NY date key)
    stop_at: Mapped[datetime | None] = mapped_column(DateTime)   # when the auto stop was computed (UTC)
    stop_prev: Mapped[float | None] = mapped_column(Float)       # the stop it replaced (kept for reference)
    exec_grade: Mapped[str | None] = mapped_column(String(2))    # optional execution grade A-F
    journal: Mapped[str | None] = mapped_column(Text)            # JSON {question id: answer} (notes stays "Other notes")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    account: Mapped[Account] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=trade_tags, lazy="selectin")
    fills: Mapped[list["TradeFill"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", order_by="TradeFill.position")
    mistake_rows: Mapped[list["TradeMistake"]] = relationship(
        cascade="all, delete-orphan", order_by="TradeMistake.name", lazy="selectin")

    @property
    def mistakes(self) -> list[str]:
        return [m.name for m in self.mistake_rows]

    @property
    def answers(self) -> dict:
        import json
        try:
            v = json.loads(self.journal) if self.journal else {}
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}


class TradeMistake(Base):
    """A mistake tagged on a trade (the option list lives in journal_options; names are kept on old trades
    even after an option is removed from the list)."""
    __tablename__ = "trade_mistakes"
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), primary_key=True)


class JournalOption(Base):
    """Persistent dropdown options: kind = setup | tag | mistake."""
    __tablename__ = "journal_options"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_journal_option"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)


class TradeFill(Base):
    """Portion of an execution allocated to a trade (an execution can be split on a flip)."""
    __tablename__ = "trade_fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), index=True)  # None = inferred expiration
    position: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(4))
    role: Mapped[str] = mapped_column(String(6))  # OPEN | CLOSE
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    executed_at: Mapped[datetime] = mapped_column(DateTime)
    trade: Mapped[Trade] = relationship(back_populates="fills")
    execution: Mapped[Execution | None] = relationship()


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # manual | cli
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|partial|failed|skipped
    sources: Mapped[str | None] = mapped_column(String(200))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    trades_built: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class SourceState(Base):
    """Per data-source / per-account sync cursor."""
    __tablename__ = "source_state"
    __table_args__ = (UniqueConstraint("source", "account_id", name="uq_source_account"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    synced_through: Mapped[datetime | None] = mapped_column(DateTime)
    earliest_reached: Mapped[datetime | None] = mapped_column(DateTime)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), unique=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PriceCache(Base):
    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_price_cache_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(30))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class InboundEmail(Base):
    """A fill notification email posted by the user's Gmail Apps Script (idempotent per message)."""
    __tablename__ = "inbound_emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), unique=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    received_at: Mapped[datetime] = mapped_column(DateTime)  # naive UTC
    sender: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))  # fills | ignored | error
    fills: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
