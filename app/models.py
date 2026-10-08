from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text,
    UniqueConstraint, Index,
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
    # Journal fields (preserved across rebuilds)
    notes: Mapped[str | None] = mapped_column(Text)
    setup: Mapped[str | None] = mapped_column(String(80))
    rating: Mapped[int | None] = mapped_column(Integer)
    mfe: Mapped[float | None] = mapped_column(Float)  # $ max favourable excursion
    mae: Mapped[float | None] = mapped_column(Float)  # $ max adverse excursion
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    account: Mapped[Account] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=trade_tags, lazy="selectin")
    fills: Mapped[list["TradeFill"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", order_by="TradeFill.position")


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
