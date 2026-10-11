"""Group executions into round-trip trades.

Pure functions, no database access, so it can be unit tested exhaustively.

Rules
-----
* Executions are grouped per (account, instrument symbol) and processed in time order.
* A trade starts when the position goes from flat to non-flat and ends when it returns to flat.
  Scaling in/out is part of the same trade.
* An execution that crosses through zero (e.g. long 100, sell 150) is split: 100 closes the
  current trade and 50 opens a new trade in the other direction. Fees are split pro-rata.
* Realized P&L uses FIFO lot matching (matters for partially-closed open trades).
* Expirations / assignments / exercises close the remaining (or given) quantity at the given
  price (usually 0). Their side is derived from the current position.
* A closing execution with no open position (history started mid-position) is reported as an
  orphan and not turned into a trade.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from app.corporate import POSITION_KINDS, choose_split_ratio

EPS = 1e-9
CLOSING_KINDS = {"EXPIRATION", "ASSIGNMENT", "EXERCISE"}
_EFFECT_RANK = {"OPEN": 0, None: 1, "CLOSE": 2}


@dataclass
class BuilderExec:
    id: int
    account_id: int
    symbol: str
    side: str | None
    quantity: float
    price: float
    executed_at: datetime
    fees: float = 0.0
    multiplier: float = 1.0
    position_effect: str | None = None
    kind: str = "TRADE"
    time_known: bool = True
    seq: int = 0


@dataclass
class BuiltFill:
    execution_id: int | None
    side: str
    role: str  # OPEN | CLOSE
    quantity: float
    price: float
    fees: float
    executed_at: datetime


@dataclass
class BuiltTrade:
    key: str
    account_id: int
    symbol: str
    direction: str  # LONG | SHORT
    multiplier: float
    fills: list[BuiltFill] = field(default_factory=list)
    status: str = "OPEN"
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    max_quantity: float = 0.0
    open_quantity: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    time_known: bool = True
    close_reason: str | None = None

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def opening_fills(self) -> list[BuiltFill]:
        return [f for f in self.fills if f.role == "OPEN"]

    @property
    def closing_fills(self) -> list[BuiltFill]:
        return [f for f in self.fills if f.role == "CLOSE"]

    @staticmethod
    def _avg(fills: list[BuiltFill]) -> float | None:
        q = sum(f.quantity for f in fills)
        return sum(f.quantity * f.price for f in fills) / q if q > EPS else None

    @property
    def entry_price(self) -> float:
        return self._avg(self.opening_fills) or 0.0

    @property
    def exit_price(self) -> float | None:
        return self._avg(self.closing_fills)

    @property
    def cost_basis(self) -> float:
        return sum(f.quantity * f.price for f in self.opening_fills) * self.multiplier

    @property
    def return_pct(self) -> float | None:
        cb = self.cost_basis
        return (self.net_pnl / cb * 100.0) if cb > EPS else None


@dataclass
class Orphan:
    execution_id: int
    symbol: str
    quantity: float
    reason: str


@dataclass
class BuildResult:
    trades: list[BuiltTrade]
    orphans: list[Orphan]


def sort_key(e: BuilderExec):
    # For date-only rows (CSV without times) put explicit opens before ambiguous before closes,
    # and position events (expirations etc.) last, so same-day round trips build correctly.
    if e.time_known:
        rank = 0
    else:
        rank = 3 if e.kind in CLOSING_KINDS else _EFFECT_RANK.get(e.position_effect, 1)
    return (e.executed_at, rank, e.seq, e.id)


def build_trades(executions: list[BuilderExec],
                 expirations: dict[str, datetime] | None = None,
                 as_of: datetime | None = None) -> BuildResult:
    """expirations: option symbol -> expiry close time (UTC). Option trades still open after
    their expiry (as of `as_of`) are closed at 0 with close_reason EXPIRATION (inferred), which
    covers sources that don't report expirations (e.g. thinkorswim trade history)."""
    groups: dict[tuple[int, str], list[BuilderExec]] = defaultdict(list)
    for e in executions:
        groups[(e.account_id, e.symbol)].append(e)
    trades: list[BuiltTrade] = []
    orphans: list[Orphan] = []
    for (_acct, _sym), execs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        t, o = _build_symbol(sorted(execs, key=sort_key))
        exp_at = (expirations or {}).get(_sym)
        if exp_at is not None and as_of is not None and exp_at < as_of:
            for tr in t:
                if tr.status == "OPEN" and (tr.opened_at is None or tr.opened_at <= exp_at):
                    _close_inferred_expiration(tr, exp_at)
        trades.extend(t)
        orphans.extend(o)
    trades.sort(key=lambda t: (t.opened_at or datetime.min, t.key))
    return BuildResult(trades=trades, orphans=orphans)


def _build_symbol(execs: list[BuilderExec]) -> tuple[list[BuiltTrade], list[Orphan]]:
    trades: list[BuiltTrade] = []
    orphans: list[Orphan] = []
    pos = 0.0  # signed position
    lots: deque[list[float]] = deque()  # [qty, price] FIFO, unsigned qty
    cur: BuiltTrade | None = None

    for e in execs:
        qty = abs(e.quantity)
        if qty < EPS:
            continue
        if e.kind == "SPLIT":
            pos, cur = _apply_split(e, pos, lots, cur, orphans)
            continue
        side = e.side
        px = e.price
        if e.kind in POSITION_KINDS:
            if side == "SELL" and abs(pos) <= EPS:
                orphans.append(Orphan(e.id, e.symbol, qty, f"{e.kind.lower()} sell with no open position"))
                continue
            if side == "SELL" and qty > abs(pos) + EPS:
                qty = abs(pos)  # an ISA corporate row must not open a short
            if e.kind == "CONVERSION" and side == "SELL" and abs(pos) > EPS:
                px = _lot_avg(lots)
        if e.kind in CLOSING_KINDS:
            if abs(pos) < EPS:
                orphans.append(Orphan(e.id, e.symbol, qty, f"{e.kind.lower()} with no open position"))
                continue
            side = "SELL" if pos > 0 else "BUY"
            if qty > abs(pos) + EPS:
                orphans.append(Orphan(e.id, e.symbol, qty - abs(pos),
                                      f"{e.kind.lower()} quantity exceeds open position"))
                qty = abs(pos)
        if side not in ("BUY", "SELL"):
            orphans.append(Orphan(e.id, e.symbol, qty, "unknown side"))
            continue
        sgn = 1.0 if side == "BUY" else -1.0
        fee_per_unit = (e.fees / abs(e.quantity)) if abs(e.quantity) > EPS else 0.0
        remaining = qty

        # 1) closing portion
        if abs(pos) > EPS and (pos > 0) != (sgn > 0):
            assert cur is not None
            close_q = min(remaining, abs(pos))
            direction = 1.0 if pos > 0 else -1.0
            pnl = 0.0
            to_match = close_q
            while to_match > EPS and lots:
                lot = lots[0]
                m = min(lot[0], to_match)
                pnl += (px - lot[1]) * m * direction
                lot[0] -= m
                to_match -= m
                if lot[0] <= EPS:
                    lots.popleft()
            cur.gross_pnl += pnl * cur.multiplier
            fees = fee_per_unit * close_q
            cur.fees += fees
            cur.fills.append(BuiltFill(e.id, side, "CLOSE", close_q, px, fees, e.executed_at))
            cur.time_known = cur.time_known and e.time_known
            pos += sgn * close_q
            remaining -= close_q
            if abs(pos) <= EPS:
                pos = 0.0
                lots.clear()
                cur.status = "CLOSED"
                cur.closed_at = e.executed_at
                cur.open_quantity = 0.0
                cur.close_reason = e.kind
                trades.append(cur)
                cur = None
            else:
                cur.open_quantity = abs(pos)

        # 2) opening portion (new trade, scale-in, or remainder after a flip)
        if remaining > EPS:
            if e.kind in CLOSING_KINDS:
                continue
            if abs(pos) <= EPS and e.position_effect == "CLOSE":
                orphans.append(Orphan(e.id, e.symbol, remaining, "closing fill with no open position"))
                continue
            if cur is None:
                part = ":flip" if remaining < qty - EPS else ""
                cur = BuiltTrade(
                    key=f"{e.account_id}:{e.symbol}:{e.id}{part}",
                    account_id=e.account_id, symbol=e.symbol,
                    direction="LONG" if sgn > 0 else "SHORT",
                    multiplier=e.multiplier, opened_at=e.executed_at, time_known=e.time_known,
                )
            fees = fee_per_unit * remaining
            cur.fees += fees
            cur.fills.append(BuiltFill(e.id, side, "OPEN", remaining, px, fees, e.executed_at))
            cur.time_known = cur.time_known and e.time_known
            lots.append([remaining, px])
            pos += sgn * remaining
            cur.open_quantity = abs(pos)
            cur.max_quantity = max(cur.max_quantity, abs(pos))

    if cur is not None:
        trades.append(cur)
    return trades, orphans


def _lot_avg(lots) -> float:
    q = sum(lot[0] for lot in lots)
    return sum(lot[0] * lot[1] for lot in lots) / q if q > EPS else 0.0


def _apply_split(e: BuilderExec, pos: float, lots, cur: BuiltTrade | None, orphans: list[Orphan]):
    """Scale the open position by new/old. Cost basis and P&L already booked stay the same."""
    if abs(pos) <= EPS or cur is None:
        orphans.append(Orphan(e.id, e.symbol, abs(e.quantity), "split with no open position"))
        return pos, cur
    if e.position_effect == "RATIO" and e.quantity > 0 and abs(e.quantity - 1.0) > 1e-6:
        ratio = e.quantity
    elif e.price > 0:
        ratio = choose_split_ratio(pos, _lot_avg(lots), e.quantity, e.price)
    else:
        ratio = None
    if not ratio or abs(ratio - 1.0) <= 1e-6:
        orphans.append(Orphan(e.id, e.symbol, abs(e.quantity), "split ratio not reliable"))
        return pos, cur
    for lot in lots:
        lot[0] *= ratio
        lot[1] /= ratio
    for f in cur.fills:
        if f.role in ("OPEN", "CLOSE"):
            f.quantity *= ratio
            f.price /= ratio
    pos *= ratio
    cur.open_quantity = abs(pos)
    cur.max_quantity *= ratio
    cur.fills.append(BuiltFill(e.id, "BUY", "ADJUST", abs(pos), cur.entry_price, 0.0, e.executed_at))
    return pos, cur


def _close_inferred_expiration(tr: BuiltTrade, when: datetime) -> None:
    qty = tr.open_quantity
    side = "SELL" if tr.direction == "LONG" else "BUY"
    # FIFO: remaining lots are the last opened quantities; P&L at price 0.
    remaining_cost = 0.0
    closed = sum(f.quantity for f in tr.closing_fills)
    to_skip = closed
    for f in tr.opening_fills:
        take = max(0.0, f.quantity - to_skip)
        to_skip = max(0.0, to_skip - f.quantity)
        remaining_cost += take * f.price
    direction = 1.0 if tr.direction == "LONG" else -1.0
    tr.gross_pnl += (0.0 * qty - remaining_cost) * direction * tr.multiplier
    tr.fills.append(BuiltFill(None, side, "CLOSE", qty, 0.0, 0.0, when))
    tr.status = "CLOSED"
    tr.closed_at = when
    tr.open_quantity = 0.0
    tr.close_reason = "EXPIRATION"
