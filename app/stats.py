"""Performance statistics over closed trades (TraderSync-style)."""
from __future__ import annotations

import calendar
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.timeutil import utc_naive_to_tz

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Bucket:
    label: str
    pnl: float = 0.0
    count: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.count * 100 if self.count else 0.0

    def add(self, pnl: float) -> None:
        self.pnl += pnl
        self.count += 1
        if pnl > 0:
            self.wins += 1


@dataclass
class Stats:
    total_trades: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_hold: timedelta | None = None
    avg_hold_win: timedelta | None = None
    avg_hold_loss: timedelta | None = None
    max_drawdown: float = 0.0
    max_consec_wins: int = 0
    max_consec_losses: int = 0
    best_day: tuple | None = None
    worst_day: tuple | None = None
    equity: list[tuple[str, float]] = field(default_factory=list)
    daily: "OrderedDict[date, Bucket]" = field(default_factory=OrderedDict)
    by_symbol: list[Bucket] = field(default_factory=list)
    by_weekday: list[Bucket] = field(default_factory=list)
    by_hour: list[Bucket] = field(default_factory=list)
    by_direction: list[Bucket] = field(default_factory=list)
    by_asset: list[Bucket] = field(default_factory=list)
    by_setup: list[Bucket] = field(default_factory=list)
    by_tag: list[Bucket] = field(default_factory=list)
    by_hold: list[Bucket] = field(default_factory=list)
    hours_known: bool = True

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided * 100 if decided else 0.0

    @property
    def profit_factor(self) -> float | None:
        if self.gross_losses == 0:
            return None if self.gross_wins == 0 else float("inf")
        return self.gross_wins / abs(self.gross_losses)

    @property
    def avg_win(self) -> float:
        return self.gross_wins / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.gross_losses / self.losses if self.losses else 0.0

    @property
    def expectancy(self) -> float:
        return self.net_pnl / self.total_trades if self.total_trades else 0.0

    @property
    def win_loss_ratio(self) -> float | None:
        return self.avg_win / abs(self.avg_loss) if self.avg_loss else None


def hold_bucket(td: timedelta) -> str:
    if td < timedelta(minutes=5):
        return "< 5 min"
    if td < timedelta(hours=1):
        return "5-60 min"
    if td < timedelta(days=1):
        return "1 h - 1 day"
    if td < timedelta(days=7):
        return "1-7 days"
    return "> 1 week"


def compute(trades, tz: str, open_count: int = 0) -> Stats:
    st = Stats(open_trades=open_count)
    closed = sorted([t for t in trades if t.status == "CLOSED" and t.closed_at],
                    key=lambda t: (t.closed_at, t.id or 0))
    sym, wd, hr = defaultdict(lambda: None), {}, {}
    buckets: dict[str, dict[str, Bucket]] = defaultdict(dict)

    def b(group: str, label: str) -> Bucket:
        g = buckets[group]
        if label not in g:
            g[label] = Bucket(label)
        return g[label]

    holds, holds_w, holds_l = [], [], []
    cum = peak = 0.0
    streak_w = streak_l = 0
    st.hours_known = any(t.time_known for t in closed) if closed else True
    for t in closed:
        pnl = t.net_pnl
        st.total_trades += 1
        st.net_pnl += pnl
        st.gross_pnl += t.gross_pnl
        st.fees += t.fees
        if pnl > 0:
            st.wins += 1
            st.gross_wins += pnl
            st.largest_win = max(st.largest_win, pnl)
            streak_w, streak_l = streak_w + 1, 0
        elif pnl < 0:
            st.losses += 1
            st.gross_losses += pnl
            st.largest_loss = min(st.largest_loss, pnl)
            streak_l, streak_w = streak_l + 1, 0
        else:
            st.scratches += 1
        st.max_consec_wins = max(st.max_consec_wins, streak_w)
        st.max_consec_losses = max(st.max_consec_losses, streak_l)
        cum += pnl
        peak = max(peak, cum)
        st.max_drawdown = min(st.max_drawdown, cum - peak)
        closed_local = utc_naive_to_tz(t.closed_at, tz)
        opened_local = utc_naive_to_tz(t.opened_at, tz)
        st.equity.append((closed_local.strftime("%Y-%m-%d %H:%M"), round(cum, 2)))
        d = closed_local.date()
        if d not in st.daily:
            st.daily[d] = Bucket(d.isoformat())
        st.daily[d].add(pnl)
        hold = t.closed_at - t.opened_at
        holds.append(hold)
        (holds_w if pnl > 0 else holds_l if pnl < 0 else []).append(hold)
        b("symbol", t.underlying).add(pnl)
        b("weekday", WEEKDAYS[opened_local.weekday()]).add(pnl)
        if t.time_known:
            b("hour", f"{opened_local.hour:02d}:00").add(pnl)
        b("direction", t.direction.title()).add(pnl)
        b("asset", "Options" if t.asset_type == "OPTION" else "Stocks").add(pnl)
        b("setup", t.setup or "(no setup)").add(pnl)
        for tag in (t.tags or []):
            b("tag", tag.name).add(pnl)
        if not t.tags:
            b("tag", "(untagged)").add(pnl)
        b("hold", hold_bucket(hold) if t.time_known or hold >= timedelta(days=1) else "Same day (time unknown)").add(pnl)

    def avg_td(v):
        return sum(v, timedelta()) / len(v) if v else None

    st.avg_hold, st.avg_hold_win, st.avg_hold_loss = avg_td(holds), avg_td(holds_w), avg_td(holds_l)
    if st.daily:
        best = max(st.daily.values(), key=lambda x: x.pnl)
        worst = min(st.daily.values(), key=lambda x: x.pnl)
        st.best_day, st.worst_day = (best.label, best.pnl), (worst.label, worst.pnl)
    st.by_symbol = sorted(buckets["symbol"].values(), key=lambda x: x.pnl, reverse=True)
    st.by_weekday = [buckets["weekday"][d] for d in WEEKDAYS if d in buckets["weekday"]]
    st.by_hour = [buckets["hour"][h] for h in sorted(buckets["hour"])]
    st.by_direction = list(buckets["direction"].values())
    st.by_asset = list(buckets["asset"].values())
    st.by_setup = sorted(buckets["setup"].values(), key=lambda x: x.pnl, reverse=True)
    st.by_tag = sorted(buckets["tag"].values(), key=lambda x: x.pnl, reverse=True)
    order = ["< 5 min", "5-60 min", "1 h - 1 day", "Same day (time unknown)", "1-7 days", "> 1 week"]
    st.by_hold = [buckets["hold"][k] for k in order if k in buckets["hold"]]
    return st


def calendar_months(daily: "OrderedDict[date, Bucket]", max_months: int = 6) -> list[dict]:
    """Month grids (weeks x 7 days, Monday first) for the P&L calendar heatmap."""
    if not daily:
        return []
    last = max(daily)
    months = []
    y, m = last.year, last.month
    first_day = min(daily)
    for _ in range(max_months):
        cal = calendar.Calendar(firstweekday=0)
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for d in week:
                bk = daily.get(d)
                row.append({"date": d, "in_month": d.month == m, "pnl": bk.pnl if bk else None,
                            "count": bk.count if bk else 0})
            wk_pnl = sum(c["pnl"] or 0 for c in row if c["in_month"])
            weeks.append({"days": row, "pnl": wk_pnl,
                          "count": sum(c["count"] for c in row if c["in_month"])})
        month_pnl = sum(v.pnl for d, v in daily.items() if d.year == y and d.month == m)
        months.append({"title": f"{calendar.month_name[m]} {y}", "weeks": weeks, "pnl": month_pnl})
        if (y, m) <= (first_day.year, first_day.month):
            break
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return months


def fmt_td(td: timedelta | None) -> str:
    if td is None:
        return "—"
    secs = int(td.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"
