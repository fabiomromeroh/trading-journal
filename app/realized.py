"""Realized P&L by fill: the single rule behind the calendar, the daily P&L bars, the cumulative
curves, the dashboard's Realized card and the Reports day statistics.

Rule
----
* Every closing fill realizes (exit price - FIFO entry price) x quantity x multiplier (sign-flipped
  for shorts) on the day it executed - also when the trade stays open (partial profit taking).
* Fees count on the day they were charged (opening fees on the entry day, closing fees on the exit
  day). So the realized P&L of a day = gross realized on that day's closing fills - fees of that
  day's fills.
* Days are calendar days in the display time zone (America/New_York by default).
* Reconciliation: for any set of trades, sum(daily realized) == sum(trade net P&L)
  == closed trades' net P&L + realized part of open trades. Rounding residues are put on the
  trade's last event so the totals match the trade figures to the cent.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime

from app.timeutil import utc_naive_to_tz

EPS = 1e-9


@dataclass
class Event:
    when: datetime            # naive UTC
    trade_id: int | None
    symbol: str
    gross: float
    fees: float
    qty: float                # quantity closed (0 for an opening fill)
    is_close: bool
    final: bool = False       # this fill closed the trade completely
    trade_open: bool = False  # the trade is still open (partial exit / fees of an open trade)

    @property
    def net(self) -> float:
        return self.gross - self.fees


def trade_events(t) -> list[Event]:
    fills = sorted(getattr(t, "fills", None) or [], key=lambda f: f.position)
    is_open = t.status == "OPEN"
    sym = getattr(t, "symbol", None) or getattr(t, "underlying", "")
    tid = getattr(t, "id", None)
    out: list[Event] = []
    if not fills:  # trades without stored fills: everything on the close (or open) time
        when = t.closed_at or t.opened_at
        gross, net = t.gross_pnl or 0.0, t.net_pnl or 0.0
        if gross or net:
            out.append(Event(when, tid, sym, gross, gross - net, getattr(t, "quantity", 0.0) or 0.0,
                             t.status == "CLOSED", t.status == "CLOSED", is_open))
        return out
    sign = 1.0 if t.direction == "LONG" else -1.0
    mult = getattr(t, "multiplier", None) or 1.0
    lots: deque[list[float]] = deque()
    for f in fills:
        if f.role == "OPEN":
            lots.append([f.quantity, f.price])
            out.append(Event(f.executed_at, tid, sym, 0.0, f.fees or 0.0, 0.0, False, False, is_open))
            continue
        q, pnl = f.quantity, 0.0
        while q > EPS and lots:
            m = min(q, lots[0][0])
            pnl += (f.price - lots[0][1]) * m * sign
            lots[0][0] -= m
            q -= m
            if lots[0][0] <= EPS:
                lots.popleft()
        out.append(Event(f.executed_at, tid, sym, pnl * mult, f.fees or 0.0, f.quantity, True, False, is_open))
    if t.status == "CLOSED":
        closes = [e for e in out if e.is_close]
        if closes:
            closes[-1].final = True
    # make the events add up exactly to the trade's stored gross and net P&L
    if out:
        last = out[-1]
        last.gross += (t.gross_pnl or 0.0) - sum(e.gross for e in out)
        last.fees += ((t.gross_pnl or 0.0) - (t.net_pnl or 0.0)) - sum(e.fees for e in out)
    return out


def events(trades) -> list[Event]:
    ev = [e for t in trades for e in trade_events(t)]
    ev.sort(key=lambda e: (e.when, e.trade_id or 0))
    return ev


def clip(evs: list[Event], tz: str, start: date | None = None, end: date | None = None) -> list[Event]:
    if not start and not end:
        return evs
    out = []
    for e in evs:
        d = utc_naive_to_tz(e.when, tz).date()
        if (start and d < start) or (end and d > end):
            continue
        out.append(e)
    return out


@dataclass
class Day:
    day: date
    net: float = 0.0
    gross: float = 0.0
    fees: float = 0.0
    exits: int = 0                       # closing fills
    trades: set = field(default_factory=set)   # trades with a closing fill that day
    closed: int = 0                      # trades fully closed that day
    partial: int = 0                     # partial exits of trades still open after that fill
    wins: int = 0                        # trades closed that day with net > 0 (trade-level)

    @property
    def label(self) -> str:
        return self.day.isoformat()

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def pnl(self) -> float:
        return self.net

    def as_dict(self) -> dict:
        return {"d": self.label, "net": round(self.net, 2), "gross": round(self.gross, 2), "fees": round(self.fees, 2),
                "trades": self.count, "exits": self.exits, "closed": self.closed, "partial": self.partial}


def daily(evs: list[Event], tz: str) -> "OrderedDict[date, Day]":
    out: dict[date, Day] = {}
    for e in evs:
        d = utc_naive_to_tz(e.when, tz).date()
        row = out.get(d) or out.setdefault(d, Day(d))
        row.gross += e.gross
        row.fees += e.fees
        row.net += e.net
        if e.is_close:
            row.exits += 1
            row.trades.add(e.trade_id)
            if e.final:
                row.closed += 1
            else:
                row.partial += 1
    # entry-only days with no fees aren't P&L days
    return OrderedDict(sorted((d, r) for d, r in out.items() if r.exits or abs(r.fees) > 0.004))


def cumulative(days: "OrderedDict[date, Day]") -> dict:
    """One point per day: cumulative net and gross realized P&L, plus that day's net (for tooltips)."""
    labels, net, gross, day = [], [], [], []
    cn = cg = 0.0
    for d, r in days.items():
        cn += r.net
        cg += r.gross
        labels.append(d.isoformat())
        net.append(round(cn, 2))
        gross.append(round(cg, 2))
        day.append(round(r.net, 2))
    return {"labels": labels, "net": net, "gross": gross, "day": day}


def summary(evs: list[Event]) -> dict:
    """Totals of a set of events, split into closed trades vs still-open trades (partial exits)."""
    tot = sum(e.net for e in evs)
    open_part = sum(e.net for e in evs if e.trade_open)
    return {"total": tot, "closed_part": tot - open_part, "open_part": open_part,
            "gross": sum(e.gross for e in evs), "fees": sum(e.fees for e in evs),
            "partial_exits": sum(1 for e in evs if e.is_close and not e.final)}
