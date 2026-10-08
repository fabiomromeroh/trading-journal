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

    def info(self, msg: str) -> None:
        self.log.append(msg)


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
