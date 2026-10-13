from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


@dataclass
class SourceStatus:
    configured: bool
    ready: bool
    message: str
    level: str = "info"  # info | warning | error
    expires_in_seconds: float | None = None
    banner: dict | None = None  # optional {"level","text","link","link_text"} shown on every page


@dataclass
class SyncContext:
    trigger: str = "manual"
    log: list[str] = field(default_factory=list)
    run_id: int | None = None
    account_id: int | None = None  # None = every account this source owns

    def info(self, msg: str) -> None:
        self.log.append(msg)


def account_is_target(account, *, external_ref: str | None, masked: str | None) -> bool:
    """True when a broker row is the account the user picked.

    A stored external_ref must match. Before the first sync links the account, the masked number is enough.
    """
    if account is None or getattr(account, "is_demo", False):
        return False
    ref = account.external_ref or ""
    if ref:
        return bool(external_ref) and ref == external_ref
    return bool(masked and account.account_number_masked == masked)


@dataclass
class SourceResult:
    fetched: int = 0
    inserted: int = 0
    merged: int = 0
    accounts: list[int] = field(default_factory=list)
    message: str = ""


class DataSource(ABC):
    key: str = "base"
    name: str = "Base"
    description: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """Env vars / credentials present."""

    def refresh(self, db: Session) -> None:
        """Re-check remote connection health before a sync (optional; default no-op)."""

    def maybe_refresh(self, db: Session) -> None:
        """Cheap periodic refresh used when rendering pages (optional; default no-op)."""

    @abstractmethod
    def status(self, db: Session) -> SourceStatus:
        """Connection health shown in Settings and the banner. Must not do slow network calls."""

    @abstractmethod
    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        """Pull new executions and store them with app.services.ingest_records."""
