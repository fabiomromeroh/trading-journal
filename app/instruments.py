"""Normalized execution record shared by all importers / data sources."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

OPTION_MULTIPLIER = 100.0


def fmt_strike(strike: float) -> str:
    s = f"{strike:.3f}".rstrip("0").rstrip(".")
    return s


def option_symbol(underlying: str, expiration: date, option_type: str, strike: float) -> str:
    """Canonical option symbol used across sources, e.g. 'AAPL 2025-01-17 150C'."""
    cp = "C" if option_type.upper().startswith("C") else "P"
    return f"{underlying.upper()} {expiration.isoformat()} {fmt_strike(strike)}{cp}"


_OCC_RE = re.compile(r"^([A-Z0-9.$/]{1,6})\s*(\d{6})([CP])(\d{8})$")


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    """Parse an OCC-style option symbol ('AAPL  250117C00150000')."""
    m = _OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    und, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return und, exp, ("CALL" if cp == "C" else "PUT"), int(strike) / 1000.0


@dataclass
class ExecRecord:
    """A normalized fill/position event. Times are naive UTC."""
    external_id: str
    symbol: str
    underlying: str
    asset_type: str  # STOCK | OPTION
    side: str | None  # BUY | SELL | None
    quantity: float
    price: float
    executed_at: datetime
    fees: float = 0.0
    time_known: bool = True
    seq: int = 0
    position_effect: str | None = None  # OPEN | CLOSE
    kind: str = "TRADE"
    option_type: str | None = None
    strike: float | None = None
    expiration: date | None = None
    multiplier: float = 1.0
    description: str | None = None
    raw: dict | None = field(default=None, repr=False)
    trade_date: date | None = None  # exchange-local (ET) trade date, used for cross-source matching

    def match_key(self) -> str:
        """Key used to recognise the same fill coming from different sources/files."""
        d = (self.trade_date or self.executed_at.date()).isoformat()
        side = self.side or "-"
        return f"{d}|{self.symbol}|{side}|{self.quantity:.4f}|{self.price:.4f}|{self.kind}"
