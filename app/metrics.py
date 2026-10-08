"""Extended trade metrics (TraderSync-style reports): summary statistics, bucketed breakdowns,
drawdown, R-multiples and MFE/MAE efficiency. Pure functions over Trade-like objects so they
can be tested with simple fixtures.

Conventions (same as the dashboard): statistics use CLOSED trades and their net P&L (after
fees); a trade with net P&L == 0 is break-even (BE) and is excluded from Win %/Loss %
denominators like TraderSync does. Times are bucketed in the display time zone; hour/weekday
use the entry time, month/year/day the exit time.
"""
from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from statistics import median

from app.timeutil import utc_naive_to_tz

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PRICE_BUCKETS = [(0, 2, "< $2"), (2, 5, "$2–5"), (5, 10, "$5–10"), (10, 20, "$10–20"), (20, 50, "$20–50"),
                 (50, 100, "$50–100"), (100, 200, "$100–200"), (200, 500, "$200–500"), (500, math.inf, "$500+")]
SIZE_BUCKETS = [(0, 1, "1"), (1, 5, "2–5"), (5, 10, "6–10"), (10, 25, "11–25"), (25, 50, "26–50"),
                (50, 100, "51–100"), (100, 500, "101–500"), (500, math.inf, "500+")]
VALUE_BUCKETS = [(0, 250, "< $250"), (250, 500, "$250–500"), (500, 1000, "$500–1k"), (1000, 2500, "$1k–2.5k"),
                 (2500, 5000, "$2.5k–5k"), (5000, 10000, "$5k–10k"), (10000, math.inf, "$10k+")]
R_BUCKETS = [(-math.inf, -2, "< -2R"), (-2, -1, "-2R to -1R"), (-1, 0, "-1R to 0"), (0, 1, "0 to 1R"),
             (1, 2, "1R to 2R"), (2, 3, "2R to 3R"), (3, math.inf, "≥ 3R")]
HOLD_ORDER = ["< 1 min", "1–5 min", "5–15 min", "15–60 min", "1–4 h", "4 h – 1 day",
              "Same day (time unknown)", "1–7 days", "1–4 weeks", "> 4 weeks"]


# ----------------------------------------------------------------------------- helpers
def pstdev(xs: list[float]) -> float | None:
    """Population standard deviation (average of squared deviations), as TraderSync defines it."""
    if not xs:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _avg_td(v: list[timedelta]):
    return sum(v, timedelta()) / len(v) if v else None


def closed_sorted(trades):
    return sorted([t for t in trades if t.status == "CLOSED" and t.closed_at],
                  key=lambda t: (t.closed_at, t.id or 0))


def hold_of(t) -> timedelta | None:
    return (t.closed_at - t.opened_at) if t.closed_at else None


def hold_bucket(t) -> str:
    td = hold_of(t)
    if td is None:
        return "Open"
    if not t.time_known and td < timedelta(days=1):
        return "Same day (time unknown)"
    m = td.total_seconds() / 60
    for limit, label in ((1, "< 1 min"), (5, "1–5 min"), (15, "5–15 min"), (60, "15–60 min"), (240, "1–4 h"),
                         (1440, "4 h – 1 day"), (1440 * 7, "1–7 days"), (1440 * 28, "1–4 weeks")):
        if m < limit:
            return label
    return "> 4 weeks"


def _range_label(v, buckets, upper_inclusive: bool = False):
    """Bucket label for v: [lo, hi) ranges, or (lo, hi] with upper_inclusive (counts like sizes)."""
    if v is None:
        return None
    for lo, hi, label in buckets:
        if (lo < v <= hi) if upper_inclusive else (lo <= v < hi):
            return label
    return buckets[-1][2] if v > 0 else buckets[0][2]


def position_value(t) -> float:
    return t.cost_basis if getattr(t, "cost_basis", None) else (t.entry_price or 0) * (t.quantity or 0) * (t.multiplier or 1)


def risk_of(t, default_risk: float | None = None) -> tuple[float | None, str | None]:
    """Planned risk in $ for R-multiples: the trade's own Risk $, else |entry - initial stop| x size,
    else the default risk per trade (Settings on the Reports page). Returns (risk, source)."""
    r = getattr(t, "risk_amount", None)
    if r and r > 0:
        return float(r), "risk $"
    stop = getattr(t, "initial_stop", None)
    if stop and t.entry_price:
        per = abs(t.entry_price - stop)
        if per > 0:
            return per * (t.quantity or 0) * (t.multiplier or 1), "stop"
    if default_risk and default_risk > 0:
        return float(default_risk), "default"
    return None, None


def r_multiple(t, default_risk: float | None = None) -> float | None:
    if t.status != "CLOSED":
        return None
    risk, _ = risk_of(t, default_risk)
    return (t.net_pnl / risk) if risk else None


def mfe_efficiency(t) -> float | None:
    """% of the trade's MFE kept (net P&L / MFE), per TraderSync; None without a positive MFE."""
    if t.status != "CLOSED" or t.mfe is None or t.mfe <= 0:
        return None
    return t.net_pnl / t.mfe * 100


def mae_efficiency(t) -> float | None:
    """Net P&L / |MAE| (the MAE tab's efficiency: can exceed 100%)."""
    if t.status != "CLOSED" or t.mae is None or t.mae >= 0:
        return None
    return t.net_pnl / abs(t.mae) * 100


def trade_metrics(t, default_risk: float | None = None) -> dict:
    """Per-trade stats for the trade page's stat bar."""
    risk, src = risk_of(t, default_risk)
    stop, target = getattr(t, "initial_stop", None), getattr(t, "profit_target", None)
    planned_rr = None
    if stop and target and t.entry_price and abs(t.entry_price - stop) > 0:
        planned_rr = abs(target - t.entry_price) / abs(t.entry_price - stop)
    qty = (t.quantity or 0) * (t.multiplier or 1)
    return {
        "risk": risk, "risk_source": src, "r_multiple": r_multiple(t, default_risk), "planned_rr": planned_rr,
        "mfe_eff": mfe_efficiency(t), "mae_eff": mae_efficiency(t),
        "best_exit": t.mfe if t.mfe is not None else None,
        "left_on_table": (t.mfe - t.gross_pnl) if (t.mfe is not None and t.status == "CLOSED") else None,
        "position_value": position_value(t), "hold": hold_of(t),
        "return_per_share": (t.net_pnl / qty) if qty and t.status == "CLOSED" else None,
        "fills": len(getattr(t, "fills", []) or []),
        "target_pnl": ((target - t.entry_price) * qty * (1 if t.direction == "LONG" else -1))
        if target and t.entry_price else None,
    }


# ----------------------------------------------------------------------------- summary
def summarize(trades, tz: str = "America/New_York", default_risk: float | None = None) -> dict:
    """All headline statistics for a set of trades (closed ones; open ones only counted)."""
    closed = closed_sorted(trades)
    n_open = sum(1 for t in trades if t.status == "OPEN")
    pnl = [t.net_pnl for t in closed]
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl < 0]
    be = [t for t in closed if t.net_pnl == 0]
    n, nw, nl = len(closed), len(wins), len(losses)
    gp, gl = sum(t.net_pnl for t in wins), sum(t.net_pnl for t in losses)
    decided = nw + nl
    win_rate = nw / decided * 100 if decided else None
    avg_win = gp / nw if nw else None
    avg_loss = gl / nl if nl else None
    pl_ratio = (avg_win / abs(avg_loss)) if (avg_win and avg_loss) else None
    kelly = None
    if win_rate is not None and pl_ratio:
        w = win_rate / 100
        kelly = (w - (1 - w) / pl_ratio) * 100
    sd = pstdev(pnl)
    sqn = (sum(pnl) / n) * math.sqrt(n) / sd if (n >= 2 and sd) else None
    # streaks
    cw = cl = mw = ml = 0
    for t in closed:
        if t.net_pnl > 0:
            cw, cl = cw + 1, 0
        elif t.net_pnl < 0:
            cl, cw = cl + 1, 0
        mw, ml = max(mw, cw), max(ml, cl)
    rets = lambda ts: [t.return_pct for t in ts if t.return_pct is not None]  # noqa: E731
    longs = [t for t in closed if t.direction == "LONG"]
    shorts = [t for t in closed if t.direction == "SHORT"]
    rs = [r for r in (r_multiple(t, default_risk) for t in closed) if r is not None]
    mfe_t = [t for t in closed if t.mfe is not None]
    mfe_eff = [e for e in (mfe_efficiency(t) for t in closed) if e is not None]
    mae_eff = [e for e in (mae_efficiency(t) for t in closed) if e is not None]
    vol = sum((t.quantity or 0) for t in closed)
    total_units = sum((t.quantity or 0) * (t.multiplier or 1) for t in closed)
    dd = drawdown(closed, tz)
    days = daily(closed, tz)
    day_pnls = [v["net"] for v in days.values()]
    out = {
        "closed": n, "open": n_open, "total": n + n_open,
        "wins": nw, "losses": nl, "be": len(be),
        "win_pct": win_rate, "loss_pct": (nl / decided * 100) if decided else None,
        "be_pct": (len(be) / n * 100) if n else None,
        "open_pct": (n_open / (n + n_open) * 100) if (n + n_open) else None,
        "net": sum(pnl), "gross": sum(t.gross_pnl for t in closed), "fees": sum(t.fees or 0 for t in closed),
        "gross_profit": gp, "gross_loss": gl,
        "profit_factor": (gp / abs(gl)) if gl else (math.inf if gp else None),
        "avg_trade": (sum(pnl) / n) if n else None, "expectancy": (sum(pnl) / n) if n else None,
        "median_trade": median(pnl) if pnl else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
        "largest_win": max(pnl) if wins else None, "largest_loss": min(pnl) if losses else None,
        "best_pct": max(rets(closed)) if rets(closed) else None,
        "worst_pct": min(rets(closed)) if rets(closed) else None,
        "avg_ret_pct": _avg(rets(closed)), "avg_ret_pct_win": _avg(rets(wins)),
        "avg_ret_pct_loss": _avg(rets(losses)), "avg_ret_pct_long": _avg(rets(longs)),
        "avg_ret_pct_short": _avg(rets(shorts)),
        "ret_long": sum(t.net_pnl for t in longs), "ret_short": sum(t.net_pnl for t in shorts),
        "n_long": len(longs), "n_short": len(shorts),
        "win_pct_long": _pct([t for t in longs if t.net_pnl > 0], [t for t in longs if t.net_pnl != 0]),
        "win_pct_short": _pct([t for t in shorts if t.net_pnl > 0], [t for t in shorts if t.net_pnl != 0]),
        "std": sd, "std_win": pstdev([t.net_pnl for t in wins]), "std_loss": pstdev([t.net_pnl for t in losses]),
        "sqn": sqn, "kelly": kelly,
        "max_consec_wins": mw, "max_consec_losses": ml,
        "avg_hold": _avg_td([hold_of(t) for t in closed]), "avg_hold_win": _avg_td([hold_of(t) for t in wins]),
        "avg_hold_loss": _avg_td([hold_of(t) for t in losses]), "avg_hold_be": _avg_td([hold_of(t) for t in be]),
        "volume": vol, "avg_size": (vol / n) if n else None,
        "return_per_share": (sum(pnl) / total_units) if total_units else None,
        "avg_fees": (sum(t.fees or 0 for t in closed) / n) if n else None,
        "fees_win": sum(t.fees or 0 for t in wins), "fees_loss": sum(t.fees or 0 for t in losses),
        "fees_be": sum(t.fees or 0 for t in be), "fees_long": sum(t.fees or 0 for t in longs),
        "fees_short": sum(t.fees or 0 for t in shorts),
        "fees_open": sum(t.fees or 0 for t in trades if t.status == "OPEN"),
        "open_realized": sum(t.net_pnl or 0 for t in trades if t.status == "OPEN"),
        "r_count": len(rs), "total_r": sum(rs) if rs else None, "avg_r": _avg(rs),
        "avg_r_win": _avg([r for r in rs if r > 0]), "avg_r_loss": _avg([r for r in rs if r < 0]),
        "mfe_count": len(mfe_t), "avg_mfe": _avg([t.mfe for t in mfe_t]),
        "avg_mae": _avg([t.mae for t in mfe_t if t.mae is not None]),
        "max_mfe": max((t.mfe for t in mfe_t), default=None),
        "max_mae": min((t.mae for t in mfe_t if t.mae is not None), default=None),
        "mfe_eff": _avg(mfe_eff), "mae_eff": _avg(mae_eff),
        "left_on_table": sum(t.mfe - t.gross_pnl for t in mfe_t) if mfe_t else None,
        "max_dd": dd["max_dd"], "current_dd": dd["current_dd"], "max_dd_days": dd["max_dd_days"],
        "max_dd_trades": dd["max_dd_trades"], "recovery_factor": (sum(pnl) / abs(dd["max_dd"])) if dd["max_dd"] else None,
        "days": len(days), "win_days": sum(1 for x in day_pnls if x > 0), "loss_days": sum(1 for x in day_pnls if x < 0),
        "avg_day": _avg(day_pnls), "best_day": max(day_pnls) if day_pnls else None,
        "worst_day": min(day_pnls) if day_pnls else None,
        "hours_known": any(t.time_known for t in closed) if closed else True,
    }
    out["day_win_pct"] = (out["win_days"] / (out["win_days"] + out["loss_days"]) * 100
                          if (out["win_days"] + out["loss_days"]) else None)
    return out


def _pct(a, b):
    return len(a) / len(b) * 100 if b else None


# ----------------------------------------------------------------------------- series
def daily(closed, tz: str) -> "OrderedDict[date, dict]":
    out: OrderedDict = OrderedDict()
    for t in closed:
        d = utc_naive_to_tz(t.closed_at, tz).date()
        row = out.setdefault(d, {"net": 0.0, "gross": 0.0, "trades": 0})
        row["net"] += t.net_pnl
        row["gross"] += t.gross_pnl
        row["trades"] += 1
    return out


def cumulative(closed, tz: str) -> dict:
    labels, net, gross = [], [], []
    cn = cg = 0.0
    for t in closed:
        cn += t.net_pnl
        cg += t.gross_pnl
        labels.append(utc_naive_to_tz(t.closed_at, tz).strftime("%Y-%m-%d %H:%M"))
        net.append(round(cn, 2))
        gross.append(round(cg, 2))
    return {"labels": labels, "net": net, "gross": gross}


def drawdown(closed, tz: str = "America/New_York") -> dict:
    """Peak-to-trough on cumulative closed net P&L (starting at 0). Duration = calendar days from
    the peak to recovery (or to the last trade if not recovered)."""
    cum = peak = 0.0
    peak_at = None
    peak_i = 0
    max_dd, max_days, max_trades = 0.0, 0, 0
    series, labels = [], []
    episodes: list[dict] = []
    cur = None
    for i, t in enumerate(closed):
        cum += t.net_pnl
        when = utc_naive_to_tz(t.closed_at, tz)
        if peak_at is None:
            peak_at = when
        if cum >= peak:
            if cur:
                cur["recovered"] = when.date()
                cur["days"] = (when.date() - cur["start"]).days
                episodes.append(cur)
                cur = None
            peak, peak_at, peak_i = cum, when, i
        else:
            dd = cum - peak
            if cur is None:
                cur = {"start": peak_at.date(), "peak": peak, "trough": dd, "trough_at": when.date(),
                       "recovered": None, "days": 0, "trades": 0}
            if dd < cur["trough"]:
                cur["trough"], cur["trough_at"] = dd, when.date()
            cur["trades"] = i - peak_i
            cur["days"] = (when.date() - cur["start"]).days
            max_dd = min(max_dd, dd)
        series.append(round(cum - peak, 2))
        labels.append(when.strftime("%Y-%m-%d %H:%M"))
    if cur:
        episodes.append(cur)
    for e in episodes:
        max_days = max(max_days, e["days"])
        max_trades = max(max_trades, e["trades"])
    episodes.sort(key=lambda e: e["trough"])
    return {"max_dd": max_dd, "current_dd": (cum - peak) if closed else 0.0, "max_dd_days": max_days,
            "max_dd_trades": max_trades, "series": {"labels": labels, "values": series},
            "episodes": episodes[:10]}


# ----------------------------------------------------------------------------- breakdowns
def bucket_row(label, ts) -> dict:
    pnl = [t.net_pnl for t in ts]
    w = [x for x in pnl if x > 0]
    lo = [x for x in pnl if x < 0]
    gl = sum(lo)
    return {"label": label, "trades": len(ts), "wins": len(w), "losses": len(lo),
            "win_pct": (len(w) / (len(w) + len(lo)) * 100) if (w or lo) else None,
            "net": sum(pnl), "gross": sum(t.gross_pnl for t in ts), "fees": sum(t.fees or 0 for t in ts),
            "avg": (sum(pnl) / len(ts)) if ts else None,
            "avg_win": (sum(w) / len(w)) if w else None, "avg_loss": (gl / len(lo)) if lo else None,
            "pf": (sum(w) / abs(gl)) if gl else (math.inf if w else None),
            "volume": sum(t.quantity or 0 for t in ts),
            "largest_win": max(w) if w else None, "largest_loss": min(lo) if lo else None}


def group(closed, keyfn, order: list[str] | None = None, sort: str = "order") -> list[dict]:
    """Bucket trades by keyfn (may return a list for multi-valued keys, or None to skip)."""
    g: dict[str, list] = defaultdict(list)
    first: dict[str, int] = {}
    for i, t in enumerate(closed):
        keys = keyfn(t)
        if keys is None:
            continue
        for k in (keys if isinstance(keys, list) else [keys]):
            g[k].append(t)
            first.setdefault(k, i)
    labels = list(g)
    if order:
        labels = [k for k in order if k in g] + [k for k in g if k not in order]
    elif sort == "net":
        labels.sort(key=lambda k: sum(t.net_pnl for t in g[k]), reverse=True)
    else:
        labels.sort()
    return [bucket_row(k, g[k]) for k in labels]


def breakdowns(trades, tz: str, default_risk: float | None = None) -> dict:
    closed = closed_sorted(trades)
    loc_open = lambda t: utc_naive_to_tz(t.opened_at, tz)  # noqa: E731
    loc_close = lambda t: utc_naive_to_tz(t.closed_at, tz)  # noqa: E731

    def r_key(t):
        r = r_multiple(t, default_risk)
        return _range_label(r, R_BUCKETS) if r is not None else None

    return {
        "weekday": group(closed, lambda t: WEEKDAYS[loc_open(t).weekday()], WEEKDAYS),
        "hour": group(closed, lambda t: f"{loc_open(t).hour:02d}:00" if t.time_known else "Time unknown"),
        "month": group(closed, lambda t: loc_close(t).strftime("%Y-%m")),
        "month_of_year": group(closed, lambda t: MONTHS[loc_close(t).month - 1], MONTHS),
        "year": group(closed, lambda t: str(loc_close(t).year)),
        "hold": group(closed, hold_bucket, HOLD_ORDER),
        "symbol": group(closed, lambda t: t.underlying, sort="net"),
        "setup": group(closed, lambda t: t.setup or "(no setup)", sort="net"),
        "tag": group(closed, lambda t: [x.name for x in t.tags] or ["(untagged)"], sort="net"),
        "side": group(closed, lambda t: t.direction.title(), ["Long", "Short"]),
        "instrument": group(closed, lambda t: "Options" if t.asset_type == "OPTION" else "Stocks", ["Stocks", "Options"]),
        "call_put": group(closed, lambda t: (t.option_type or "?").title() + "s" if t.asset_type == "OPTION" else None,
                          ["Calls", "Puts"]),
        "status": group(closed, lambda t: "Winners" if t.net_pnl > 0 else "Losers" if t.net_pnl < 0 else "Break-even",
                        ["Winners", "Losers", "Break-even"]),
        "entry_price": group(closed, lambda t: _range_label(t.entry_price, PRICE_BUCKETS), [b[2] for b in PRICE_BUCKETS]),
        "size": group(closed, lambda t: _range_label(t.quantity, SIZE_BUCKETS, True), [b[2] for b in SIZE_BUCKETS]),
        "value": group(closed, lambda t: _range_label(position_value(t), VALUE_BUCKETS), [b[2] for b in VALUE_BUCKETS]),
        "r": group(closed, r_key, [b[2] for b in R_BUCKETS]),
        "pnl_dist": group(closed, _pnl_bucket, PNL_ORDER),
    }


PNL_EDGES = [-1000, -500, -250, -100, -50, 0, 50, 100, 250, 500, 1000]


def _pnl_label(lo, hi):
    f = lambda v: f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"  # noqa: E731
    if lo is None:
        return f"< {f(hi)}"
    if hi is None:
        return f"≥ {f(lo)}"
    return f"{f(lo)} to {f(hi)}"


PNL_ORDER = [_pnl_label(None, PNL_EDGES[0])] + [_pnl_label(a, b) for a, b in zip(PNL_EDGES, PNL_EDGES[1:])] \
    + [_pnl_label(PNL_EDGES[-1], None)]


def _pnl_bucket(t) -> str:
    v = t.net_pnl
    if v < PNL_EDGES[0]:
        return PNL_ORDER[0]
    for a, b in zip(PNL_EDGES, PNL_EDGES[1:]):
        if a <= v < b:
            return _pnl_label(a, b)
    return PNL_ORDER[-1]
