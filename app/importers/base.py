from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

from app.instruments import ExecRecord


class UnknownFormat(ValueError):
    pass


@dataclass
class ParseResult:
    format: str
    records: list[ExecRecord] = field(default_factory=list)
    rows_total: int = 0
    skipped: Counter = field(default_factory=Counter)  # action -> count of non-trade rows
    warnings: list[str] = field(default_factory=list)
    account_hint: str | None = None  # e.g. "...285" from a file header

    @property
    def date_range(self):
        dates = [r.trade_date or r.executed_at.date() for r in self.records]
        return (min(dates), max(dates)) if dates else (None, None)


_MONEY_RE = re.compile(r"[^0-9.\-]")


def parse_money(val: str | None) -> float | None:
    if val is None:
        return None
    s = val.strip()
    if not s or s in {"-", "--", "N/A"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = _MONEY_RE.sub("", s)
    if s in {"", "-", "."}:
        return None
    v = float(s)
    return -abs(v) if neg else v


def assign_stable_ids(prefix: str, records: list[ExecRecord]) -> None:
    """IDs for rows without a broker id: hash of match key + occurrence number within the file.
    Stable across re-exports that fully include the same trading day."""
    seen: Counter = Counter()
    for r in records:
        mk = r.match_key()
        seen[mk] += 1
        h = hashlib.sha1(f"{mk}#{seen[mk]}".encode()).hexdigest()[:20]
        r.external_id = f"{prefix}:{h}"
