"""Customisable widget layouts (dashboard and the trade page's stat bar), stored server-side in
app_state so the layout is the same on every device."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.services import get_state, set_state

# id, title, size (kpi | half | full), group, default?
DASHBOARD = [
    ("realized", "Total realized P&L", "kpi", "P&L", True),
    ("unrealized", "Unrealized (open)", "kpi", "P&L", True),
    ("total_pnl", "Total P&L + account check", "kpi", "P&L", True),
    ("positions", "Open positions (unrealized breakdown)", "full", "P&L", True),
    ("win_rate", "Win rate", "kpi", "Win/loss", True),
    ("profit_factor", "Profit factor", "kpi", "Win/loss", True),
    ("expectancy", "Expectancy", "kpi", "Win/loss", True),
    ("avg_win_loss", "Avg win / loss", "kpi", "Win/loss", True),
    ("total_trades", "Total trades", "kpi", "Activity", True),
    ("largest_win", "Largest win", "kpi", "Win/loss", True),
    ("largest_loss", "Largest loss", "kpi", "Win/loss", True),
    ("avg_hold", "Avg hold time", "kpi", "Activity", True),
    ("max_drawdown", "Max drawdown", "kpi", "Risk", True),
    ("streaks", "Max consecutive W/L", "kpi", "Win/loss", True),
    ("best_worst_day", "Best / worst day", "kpi", "Days", True),
    ("chart_equity", "Equity curve", "wide", "Charts", True),
    ("chart_winloss", "Win / loss donut", "third", "Charts", True),
    ("chart_daily", "Daily realized P&L", "wide", "Charts", True),
    ("calendar", "P&L calendar", "wide", "Charts", True),
    ("chart_symbol", "P&L by symbol", "half", "Charts", True),
    ("chart_weekday", "P&L by day of week", "half", "Charts", True),
    ("chart_hour", "P&L by hour", "half", "Charts", True),
    ("chart_hold", "P&L by holding time", "half", "Charts", True),
    ("tbl_direction", "Long vs short table", "quarter", "Tables", True),
    ("tbl_asset", "Stocks vs options table", "quarter", "Tables", True),
    ("tbl_setup", "By setup table", "quarter", "Tables", True),
    ("tbl_tag", "By tag table", "quarter", "Tables", True),
    ("recent", "Recent closed trades", "full", "Tables", True),
    # extra metrics (from the Reports module)
    ("closed_net", "Closed trades net P&L", "kpi", "P&L", False),
    ("gross_net", "Gross vs net P&L", "kpi", "P&L", False),
    ("fees", "Commissions & fees", "kpi", "P&L", False),
    ("long_short", "Long vs short return", "kpi", "P&L", False),
    ("pl_ratio", "Profit/loss ratio", "kpi", "Win/loss", False),
    ("win_loss_be_pct", "Win % / Loss % / BE %", "kpi", "Win/loss", False),
    ("median_trade", "Median trade", "kpi", "Win/loss", False),
    ("avg_return_pct", "Avg return %", "kpi", "Returns", False),
    ("best_worst_pct", "Biggest % profit / loser", "kpi", "Returns", False),
    ("return_per_share", "Return per share", "kpi", "Returns", False),
    ("avg_size", "Avg position size", "kpi", "Activity", False),
    ("open_trades", "Open trades", "kpi", "Activity", False),
    ("hold_win_loss", "Hold time winners vs losers", "kpi", "Activity", False),
    ("std_dev", "P&L standard deviation", "kpi", "Risk", False),
    ("sqn", "System Quality Number (SQN)", "kpi", "Risk", False),
    ("kelly", "Kelly %", "kpi", "Risk", False),
    ("current_dd", "Current drawdown", "kpi", "Risk", False),
    ("dd_duration", "Longest drawdown", "kpi", "Risk", False),
    ("recovery_factor", "Recovery factor", "kpi", "Risk", False),
    ("day_stats", "Winning days %", "kpi", "Days", False),
    ("avg_day", "Avg daily P&L", "kpi", "Days", False),
    ("avg_r", "Avg R-multiple", "kpi", "R & excursions", False),
    ("total_r", "Total R", "kpi", "R & excursions", False),
    ("mfe_eff", "MFE capture (efficiency)", "kpi", "R & excursions", False),
    ("avg_mfe_mae", "Avg MFE / MAE", "kpi", "R & excursions", False),
    ("left_on_table", "Left on the table (MFE − gross)", "kpi", "R & excursions", False),
    ("chart_cum_gross", "Cumulative net vs gross", "half", "Charts", False),
    ("chart_drawdown", "Drawdown (underwater)", "half", "Charts", False),
    ("chart_month", "P&L by month", "half", "Charts", False),
    ("chart_price", "P&L by entry price", "half", "Charts", False),
    ("chart_size", "P&L by position size", "half", "Charts", False),
    ("chart_pnl_dist", "Trade P&L distribution", "half", "Charts", False),
]

TRADE = [
    ("net", "Net P&L", True), ("gross", "Gross P&L", True), ("fees", "Fees", True), ("return", "Return %", True),
    ("entry_exit", "Entry / exit", True), ("max_size", "Max size", True), ("hold", "Hold time", True),
    ("mfe_mae", "MFE / MAE", True),
    ("entry", "Avg entry", False), ("exit", "Avg exit", False), ("mfe", "MFE", False), ("mae", "MAE", False),
    ("mfe_eff", "MFE efficiency", False), ("mae_eff", "MAE efficiency", False),
    ("best_exit", "Best exit possible", False), ("left_on_table", "Left on the table", False),
    ("r_multiple", "R-multiple", False), ("risk", "Risk $", False), ("stop", "Initial stop", False),
    ("target", "Profit target", False), ("planned_rr", "Planned R:R", False), ("target_pnl", "Profit aim $", False),
    ("position_value", "Position value", False), ("return_per_share", "Return / share", False),
    ("fills", "Executions", False), ("opened", "Opened", False), ("closed", "Closed", False),
    ("account", "Account", False), ("setup", "Setup", False), ("rating", "Rating", False),
]

TRADE_GROUPS = {**dict.fromkeys(("net", "gross", "fees", "return", "return_per_share", "position_value"), "P&L"),
                **dict.fromkeys(("entry_exit", "entry", "exit", "max_size", "hold", "fills", "opened", "closed"), "Execution"),
                **dict.fromkeys(("mfe_mae", "mfe", "mae", "mfe_eff", "mae_eff", "best_exit", "left_on_table"), "MFE / MAE"),
                **dict.fromkeys(("r_multiple", "risk", "stop", "target", "planned_rr", "target_pnl"), "Risk & R"),
                **dict.fromkeys(("account", "setup", "rating"), "Journal")}

def _desc(key: str, page: str | None = None) -> str:
    from app.metric_info import info
    i = info(key, page)
    return f"{i['desc']} {i['calc']}" if i else ""


CATALOGS = {
    "dashboard": [{"id": i, "title": t, "size": s, "group": g, "default": d, "info": _desc(i)} for i, t, s, g, d in DASHBOARD],
    "trade": [{"id": i, "title": t, "size": "kpi", "group": TRADE_GROUPS.get(i, "Trade"), "default": d, "info": _desc(i, "trade")}
              for i, t, d in TRADE],
}


# Width options for non-KPI dashboard widgets (Edit widgets › S / M / L / Full). Tailwind classes;
# phones are always full width. Catalog sizes map onto these as defaults.
WIDTHS = {
    "s": "col-span-12 md:col-span-6 xl:col-span-4",
    "m": "col-span-12 lg:col-span-6",
    "l": "col-span-12 xl:col-span-8",
    "full": "col-span-12",
}
WIDTH_LABELS = {"s": "S", "m": "M", "l": "L", "full": "Full"}
DEFAULT_WIDTH = {"third": "s", "half": "m", "wide": "l", "full": "full", "quarter": "quarter"}
FIXED_CLASSES = {"kpi": "col-span-6 md:col-span-3 xl:col-span-2", "quarter": "col-span-12 md:col-span-6 xl:col-span-3"}


def _sizes_key(page: str) -> str:
    return f"layout:{page}:sizes"


def get_sizes(db: Session, page: str) -> dict[str, str]:
    """Effective width per widget: the saved choice, else the catalog default."""
    out = {w["id"]: DEFAULT_WIDTH.get(w["size"], w["size"]) for w in CATALOGS[page] if "size" in w}
    try:
        saved = json.loads(get_state(db, _sizes_key(page)) or "{}")
    except ValueError:
        saved = {}
    for k, v in (saved.items() if isinstance(saved, dict) else []):
        if k in out and v in WIDTHS and resizable(page, k):
            out[k] = v
    return out


def resizable(page: str, wid: str) -> bool:
    w = next((w for w in CATALOGS[page] if w["id"] == wid), None)
    return bool(w and w.get("size") and w["size"] != "kpi")


def width_class(size: str) -> str:
    return WIDTHS.get(size) or FIXED_CLASSES.get(size) or WIDTHS["full"]


def default_layout(page: str) -> list[str]:
    return [w["id"] for w in CATALOGS[page] if w["default"]]


def _key(page: str) -> str:
    return f"layout:{page}"


# Default widgets added after layouts could be saved: shown once in saved layouts too (after `anchor`),
# unless the layout was saved when the widget already existed (then the user chose to hide it).
INTRODUCED = {"dashboard": {"positions": "total_pnl"}}


def get_layout(db: Session, page: str) -> list[str]:
    raw = get_state(db, _key(page))
    if not raw:
        return default_layout(page)
    try:
        ids = json.loads(raw)
    except ValueError:
        return default_layout(page)
    ids = clean(page, ids)
    try:
        known = set(json.loads(get_state(db, _key(page) + ":known") or "[]"))
    except ValueError:
        known = set()
    for wid, after in INTRODUCED.get(page, {}).items():
        if wid not in ids and wid not in known:
            ids.insert(ids.index(after) + 1 if after in ids else len(ids), wid)
    return ids


def clean(page: str, ids) -> list[str]:
    known = {w["id"] for w in CATALOGS[page]}
    out: list[str] = []
    for i in ids if isinstance(ids, list) else []:
        if isinstance(i, str) and i in known and i not in out:
            out.append(i)
    return out


def save_layout(db: Session, page: str, ids, sizes=None) -> list[str]:
    ids = clean(page, ids)
    if isinstance(sizes, dict):
        keep = {k: v for k, v in sizes.items() if isinstance(k, str) and v in WIDTHS and resizable(page, k)}
        set_state(db, _sizes_key(page), json.dumps(keep) if keep else None)
    set_state(db, _key(page), json.dumps(ids))
    set_state(db, _key(page) + ":known", json.dumps([w["id"] for w in CATALOGS[page]]))
    db.commit()
    return ids


def reset_layout(db: Session, page: str) -> list[str]:
    set_state(db, _key(page), None)
    set_state(db, _sizes_key(page), None)
    db.commit()
    return default_layout(page)


def layout_ctx(db: Session, page: str) -> dict:
    """Template context: catalog in display order (visible first, in layout order; then hidden)."""
    cat = {w["id"]: w for w in CATALOGS[page]}
    shown = get_layout(db, page)
    hidden = [w["id"] for w in CATALOGS[page] if w["id"] not in shown]
    sizes = get_sizes(db, page) if any("size" in w for w in CATALOGS[page]) else {}
    return {"page": page, "shown": shown, "order": shown + hidden, "catalog": cat, "sizes": sizes,
            "size_class": {k: width_class(v) for k, v in sizes.items()}, "widths": WIDTHS, "width_labels": WIDTH_LABELS,
            "resizable": {k for k in sizes if resizable(page, k)},
            "groups": sorted({w["group"] for w in CATALOGS[page]}, key=[w["group"] for w in CATALOGS[page]].index)}
