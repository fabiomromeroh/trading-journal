"""Extended trade metrics (TraderSync-style reports): summary statistics, bucketed breakdowns,
drawdown, R-multiples and MFE/MAE efficiency. Pure functions over Trade-like objects so they
can be tested with simple fixtures.

Conventions (same as the dashboard): statistics use CLOSED trades and their net P&L (after
fees); a trade whose net P&L is inside the break-even range (Settings; default exactly $0)
is BE and is excluded from Win %/Loss % denominators, gross profit/loss and avg win/loss like
TraderSync does (see app.outcome). Times are bucketed in the display time zone; hour/weekday
use the entry time, month/year/day the exit time.
"""
from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from statistics import median

from app import outcome
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


CLUSTER_MINUTES = 5


def _fill_known(f) -> bool:
    ex = getattr(f, "execution", None)
    return bool(ex.time_known) if ex is not None else bool(getattr(f, "time_known", True))


def _same_cluster(first, f) -> bool:
    if _fill_known(first) and _fill_known(f):
        return abs((f.executed_at - first.executed_at).total_seconds()) <= CLUSTER_MINUTES * 60
    return first.executed_at.date() == f.executed_at.date()  # no time of day: same date counts as one entry


def entry_split(t) -> dict | None:
    """First entry vs adds. The first entry = the first opening fill plus any further opening fills within
    5 minutes of it, as long as nothing has been sold yet (fills without a time of day: same date). Every other
    opening fill is an add. None for a trade without fills."""
    fills = sorted(getattr(t, "fills", None) or [], key=lambda f: f.position)
    opens = [f for f in fills if f.role == "OPEN"]
    if not opens:
        return None
    first_close = next((f.position for f in fills if f.role == "CLOSE"), None)
    first, init, adds = opens[0], [opens[0]], []
    for f in opens[1:]:
        if (first_close is None or f.position < first_close) and _same_cluster(first, f):
            init.append(f)
        else:
            adds.append(f)
    q = sum(f.quantity for f in init)
    return {"init": init, "adds": adds, "init_qty": q, "init_price": sum(f.price * f.quantity for f in init) / q,
            "add_qty": sum(f.quantity for f in adds), "first_at": getattr(first, "executed_at", None), "time_known": _fill_known(first)}


def first_entry(t) -> tuple[float | None, float]:
    """(first-entry average price, first-entry size); falls back to the trade's average entry / max size."""
    sp = entry_split(t)
    return (sp["init_price"], sp["init_qty"]) if sp else (t.entry_price, t.quantity or 0.0)


def stop_side_ok(t) -> bool:
    """A stop must be below the first entry for longs and above it for shorts."""
    stop = getattr(t, "initial_stop", None)
    px = first_entry(t)[0]
    if stop is None or not px:
        return False
    return stop < px if t.direction == "LONG" else stop > px


def risk_per_share(t) -> float | None:
    """1R per share = |first entry price - stop| (valid stops only)."""
    return abs(first_entry(t)[0] - t.initial_stop) if stop_side_ok(t) else None


STOP_SOURCES = {"5m": "auto: 5m low of day before entry", "daily": "auto: daily low (approx)"}


def stop_label(t) -> str | None:
    """How the stop got there: manual, or which auto rule / data (None without a stop)."""
    if getattr(t, "initial_stop", None) is None:
        return None
    if not getattr(t, "stop_auto", None):
        return "manual"
    src = STOP_SOURCES.get(getattr(t, "stop_src", None) or "", "auto")
    if getattr(t, "direction", "LONG") == "SHORT":
        src = src.replace("low of day", "high of day").replace("daily low", "daily high")
    return src


def risk_detail(t, default_risk: float | None = None) -> dict:
    """Initial risk $ = (first entry price - stop) x first entry size x multiplier (a typed Risk $ replaces it).
    Total risk $ = initial risk + every add measured against the SAME stop: sum of (add price - stop) x size
    (never negative), i.e. the open risk at maximum exposure. Longs; shorts mirror it."""
    price, qty = first_entry(t)
    mult = getattr(t, "multiplier", 1) or 1
    sp = entry_split(t)
    manual = getattr(t, "risk_amount", None)
    out = {"initial": None, "total": None, "source": None, "rps": None, "first_price": price, "init_qty": qty,
           "add_qty": sp["add_qty"] if sp else 0.0}
    if manual and manual > 0:
        total_qty = qty + out["add_qty"]
        out.update(initial=float(manual), source="risk $", total=float(manual) * (total_qty / qty) if qty else float(manual))
        out["rps"] = risk_per_share(t)
        return out
    rps = risk_per_share(t)
    if rps:
        sign = 1 if t.direction == "LONG" else -1
        initial = rps * qty * mult
        adds = sum(max(0.0, sign * (f.price - t.initial_stop)) * f.quantity * mult for f in (sp["adds"] if sp else []))
        out.update(initial=initial, total=initial + adds, rps=rps, source="auto stop" if getattr(t, "stop_auto", None) else "stop")
    elif default_risk and default_risk > 0:
        out.update(initial=float(default_risk), total=float(default_risk), source="default")
    return out


def risk_of(t, default_risk: float | None = None) -> tuple[float | None, str | None]:
    """INITIAL risk in $ (first entry only) for R-multiples, and where it came from."""
    d = risk_detail(t, default_risk)
    return d["initial"], d["source"]


def position_of(t) -> dict:
    """Current open position: signed quantity (long +, short -), average cost of what's left, text."""
    unit = "contracts" if getattr(t, "asset_type", "STOCK") == "OPTION" else "sh"
    if t.status != "OPEN":
        return {"qty": 0.0, "avg_cost": None, "text": "0 (closed)", "cost": 0.0}
    from app.stats import open_lots
    lots = open_lots(t)
    q = sum(x for x, _ in lots)
    if not q:
        return {"qty": 0.0, "avg_cost": None, "text": "0 (flat)", "cost": 0.0}
    cost = sum(x * p for x, p in lots)
    sign = 1 if t.direction == "LONG" else -1
    return {"qty": sign * q, "avg_cost": cost / q, "cost": cost * (getattr(t, "multiplier", 1) or 1),
            "text": f"{'+' if sign > 0 else '−'}{q:,.10g} {unit} {t.direction.lower()}"}


def risk_warnings(t) -> list[str]:
    out = []
    stop = getattr(t, "initial_stop", None)
    px = first_entry(t)[0]
    if stop is not None and px:
        if not stop_side_ok(t):
            out.append(f"Stop {stop:,.2f} is {'above' if t.direction == 'LONG' else 'below'} the first entry "
                       f"{px:,.2f}: invalid for a {t.direction.lower()}, so it isn't used.")
        else:
            pct = abs(px - stop) / px * 100
            if pct < 0.15:
                out.append(f"Risk is tiny ({pct:.2f}% of the entry): R-multiples will look huge.")
            elif pct > 20:
                out.append(f"Risk is huge ({pct:.0f}% of the entry): check the stop.")
    return out


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


def _bar_dt(t):
    """Naive-UTC datetime of the 5-minute bar that printed the stored low/high (None for daily / manual stops)."""
    from datetime import datetime
    b = getattr(t, "stop_bar", None)
    return datetime.utcfromtimestamp(b) if b and getattr(t, "stop_src", None) == "5m" else None


def trade_metrics(t, default_risk: float | None = None, price: float | None = None) -> dict:
    """Per-trade stats for the trade page's stat bar. ``price`` = latest price (open trades: current R)."""
    rd = risk_detail(t, default_risk)
    risk, src, total = rd["initial"], rd["source"], rd["total"]
    stop, target = getattr(t, "initial_stop", None), getattr(t, "profit_target", None)
    fp = rd["first_price"]
    planned_rr = None
    if stop and target and fp and abs(fp - stop) > 0:
        planned_rr = abs(target - fp) / abs(fp - stop)
    qty = (t.quantity or 0) * (t.multiplier or 1)
    pos = position_of(t)
    open_pnl = current_r = total_r = None
    sign = 1 if t.direction == "LONG" else -1
    if t.status == "OPEN" and price is not None and pos["qty"]:
        open_pnl = sign * (price * abs(pos["qty"]) - pos["cost"] / (t.multiplier or 1)) * (t.multiplier or 1)
    rps = rd["rps"]
    if t.status == "OPEN" and price is not None and rps and fp:
        current_r = sign * (price - fp) / rps           # first-entry lot: price move in units of 1R/share
    pnl_all = (t.net_pnl or 0.0) + (open_pnl or 0.0) if (t.status == "CLOSED" or open_pnl is not None) else None
    if risk and pnl_all is not None:
        total_r = pnl_all / risk
    r_on_total = (pnl_all / total) if (total and pnl_all is not None) else None
    return {
        "risk": risk, "risk_source": src, "r_multiple": r_multiple(t, default_risk), "planned_rr": planned_rr,
        "initial_risk": risk, "total_risk": total, "init_qty": rd["init_qty"], "add_qty": rd["add_qty"],
        "first_price": fp, "stop_label": stop_label(t), "stop_bar_at": _bar_dt(t),
        "risk_per_share": rps, "open_pnl": open_pnl, "current_r": current_r, "price": price,
        "total_r": total_r, "r_on_total": r_on_total,
        "r_now": current_r if t.status == "OPEN" else r_multiple(t, default_risk),
        "position": pos, "open_value": (abs(pos["qty"]) * price * (t.multiplier or 1)) if (price is not None and pos["qty"]) else (pos["cost"] or None),
        "mfe_r": (t.mfe / risk) if (risk and t.mfe is not None) else None,
        "mae_r": (t.mae / risk) if (risk and t.mae is not None) else None,
        "warnings": risk_warnings(t),
        "mfe_eff": mfe_efficiency(t), "mae_eff": mae_efficiency(t),
        "best_exit": t.mfe if t.mfe is not None else None,
        "left_on_table": (t.mfe - t.gross_pnl) if (t.mfe is not None and t.status == "CLOSED") else None,
        "position_value": position_value(t), "hold": hold_of(t),
        "return_per_share": (t.net_pnl / qty) if qty and t.status == "CLOSED" else None,
        "fills": len(getattr(t, "fills", []) or []),
        "target_pnl": ((target - fp) * qty * sign) if target and fp else None,
    }


def r_levels(t, multiples) -> list[dict]:
    """Price of each R multiple, anchored at the FIRST entry: first entry price +/- n x (first entry price - stop)
    (minus for shorts). Adds don't move the levels."""
    rps = risk_per_share(t)
    if not rps:
        return []
    sign = 1 if t.direction == "LONG" else -1
    fp = first_entry(t)[0]
    return [{"r": n, "price": round(fp + sign * n * rps, 4)} for n in multiples]


# ----------------------------------------------------------------------------- summary
def summarize(trades, tz: str = "America/New_York", default_risk: float | None = None, events=None) -> dict:
    """All headline statistics for a set of trades (closed ones; open ones only counted).
    Day statistics and ``realized`` use fill-level realized events (app.realized), which include
    partial exits of open trades; pass ``events`` already limited to the date range."""
    from app import realized as rz
    evs = rz.events(trades) if events is None else events
    closed = closed_sorted(trades)
    n_open = sum(1 for t in trades if t.status == "OPEN")
    pnl = [t.net_pnl for t in closed]
    wins = [t for t in closed if outcome.is_win(t.net_pnl)]
    losses = [t for t in closed if outcome.is_loss(t.net_pnl)]
    be = [t for t in closed if outcome.is_be(t.net_pnl)]
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
        o = outcome.classify(t.net_pnl)
        if o == "win":
            cw, cl = cw + 1, 0
        elif o == "loss":
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
    days = rz.daily(evs, tz)
    day_pnls = [v.net for v in days.values()]
    rsum = rz.summary(evs)
    out = {
        "closed": n, "open": n_open, "total": n + n_open,
        "wins": nw, "losses": nl, "be": len(be),
        "win_pct": win_rate, "loss_pct": (nl / decided * 100) if decided else None,
        "be_pct": (len(be) / n * 100) if n else None,
        "open_pct": (n_open / (n + n_open) * 100) if (n + n_open) else None,
        "realized": rsum["total"], "realized_closed": rsum["closed_part"], "realized_open": rsum["open_part"],
        "realized_fees": rsum["fees"], "partial_exits": rsum["partial_exits"],
        "net": sum(pnl), "gross": sum(t.gross_pnl for t in closed), "fees": sum(t.fees or 0 for t in closed),
        "gross_profit": gp, "gross_loss": gl,
        "profit_factor": (gp / abs(gl)) if gl else (math.inf if gp else None),
        "avg_trade": (sum(pnl) / n) if n else None, "expectancy": (sum(pnl) / n) if n else None,
        "median_trade": median(pnl) if pnl else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
        "largest_win": max(t.net_pnl for t in wins) if wins else None,
        "largest_loss": min(t.net_pnl for t in losses) if losses else None,
        "best_pct": max(rets(closed)) if rets(closed) else None,
        "worst_pct": min(rets(closed)) if rets(closed) else None,
        "avg_ret_pct": _avg(rets(closed)), "avg_ret_pct_win": _avg(rets(wins)),
        "avg_ret_pct_loss": _avg(rets(losses)), "avg_ret_pct_long": _avg(rets(longs)),
        "avg_ret_pct_short": _avg(rets(shorts)),
        "ret_long": sum(t.net_pnl for t in longs), "ret_short": sum(t.net_pnl for t in shorts),
        "n_long": len(longs), "n_short": len(shorts),
        "win_pct_long": _pct([t for t in longs if outcome.is_win(t.net_pnl)], [t for t in longs if not outcome.is_be(t.net_pnl)]),
        "win_pct_short": _pct([t for t in shorts if outcome.is_win(t.net_pnl)], [t for t in shorts if not outcome.is_be(t.net_pnl)]),
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
        "mfe_eff_median": median(mfe_eff) if mfe_eff else None, "mae_eff_median": median(mae_eff) if mae_eff else None,
        # robust aggregate: share of the total favourable excursion that was kept (tiny MFEs can't blow it up)
        "mfe_capture": (sum(t.net_pnl for t in closed if t.mfe and t.mfe > 0)
                        / sum(t.mfe for t in closed if t.mfe and t.mfe > 0) * 100)
        if any(t.mfe and t.mfe > 0 for t in closed) else None,
        "left_on_table": sum(t.mfe - t.gross_pnl for t in mfe_t) if mfe_t else None,
        "max_dd": dd["max_dd"], "current_dd": dd["current_dd"], "max_dd_days": dd["max_dd_days"],
        "max_dd_trades": dd["max_dd_trades"], "recovery_factor": (sum(pnl) / abs(dd["max_dd"])) if dd["max_dd"] else None,
        "days": len(days), "win_days": sum(1 for x in day_pnls if outcome.is_win(x)),
        "loss_days": sum(1 for x in day_pnls if outcome.is_loss(x)),
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
    w = [x for x in pnl if outcome.is_win(x)]
    lo = [x for x in pnl if outcome.is_loss(x)]
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
        "mistake": group(closed, lambda t: getattr(t, "mistakes", None) or ["(no mistake)"], sort="net"),
        "side": group(closed, lambda t: t.direction.title(), ["Long", "Short"]),
        "instrument": group(closed, lambda t: "Options" if t.asset_type == "OPTION" else "Stocks", ["Stocks", "Options"]),
        "call_put": group(closed, lambda t: (t.option_type or "?").title() + "s" if t.asset_type == "OPTION" else None,
                          ["Calls", "Puts"]),
        "status": group(closed, lambda t: {"win": "Winners", "loss": "Losers", "be": "Break-even"}[outcome.classify(t.net_pnl)],
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
