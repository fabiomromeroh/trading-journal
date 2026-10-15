"""Trades-list column picker: which columns show and in what order (saved server-side in app_state).

Every column is either a field already on the trade or a per-trade metric the trade page already
computes (``metrics.trade_metrics`` over the loaded fills, dividends from ``dividends.for_trades``).
Metrics are worked out only when one of their columns is visible, in memory, with no per-row queries.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

KEY = "layout:trades:columns"

# id, label, group, sort key (routes.trades SORTS / sector / industry) or None, right-aligned, default
COLUMNS: list[dict] = [
    {"id": "opened", "label": "Opened", "group": "Trade", "sort": "opened", "right": False, "default": True},
    {"id": "closed", "label": "Closed", "group": "Trade", "sort": "closed", "right": False, "default": True},
    {"id": "symbol", "label": "Symbol", "group": "Trade", "sort": "symbol", "right": False, "default": True},
    {"id": "sector", "label": "Sector", "group": "Company", "sort": "sector", "right": False, "default": True},
    {"id": "industry", "label": "Industry", "group": "Company", "sort": "industry", "right": False, "default": True},
    {"id": "side", "label": "Side", "group": "Trade", "sort": None, "right": False, "default": True},
    {"id": "status", "label": "Status", "group": "Trade", "sort": None, "right": False, "default": True},
    {"id": "qty", "label": "Qty", "group": "Trade", "sort": "qty", "right": True, "default": True},
    {"id": "entry", "label": "Entry", "group": "Trade", "sort": None, "right": True, "default": True},
    {"id": "exit", "label": "Exit", "group": "Trade", "sort": None, "right": True, "default": True},
    {"id": "fees", "label": "Fees", "group": "P&L", "sort": "fees", "right": True, "default": True},
    {"id": "pnl", "label": "Net P&L", "group": "P&L", "sort": "pnl", "right": True, "default": True},
    {"id": "return", "label": "Return", "group": "P&L", "sort": "return", "right": True, "default": True},
    {"id": "hold", "label": "Hold", "group": "Trade", "sort": None, "right": False, "default": True},
    {"id": "setup_tags", "label": "Setup / tags", "group": "Journal", "sort": None, "right": False, "default": True},
    {"id": "rating", "label": "Rating", "group": "Journal", "sort": None, "right": False, "default": True},
    # optional
    {"id": "account", "label": "Account", "group": "Trade", "sort": None, "right": False, "default": False},
    {"id": "currency", "label": "Currency", "group": "Trade", "sort": None, "right": False, "default": False},
    {"id": "asset", "label": "Asset type", "group": "Trade", "sort": None, "right": False, "default": False},
    {"id": "fills", "label": "Fills", "group": "Trade", "sort": None, "right": True, "default": False},
    {"id": "position_value", "label": "Position value", "group": "Trade", "sort": None, "right": True, "default": False},
    {"id": "gross", "label": "Gross P&L", "group": "P&L", "sort": None, "right": True, "default": False},
    {"id": "return_per_share", "label": "P&L / share", "group": "P&L", "sort": None, "right": True, "default": False},
    {"id": "dividend", "label": "Dividends", "group": "P&L", "sort": None, "right": True, "default": False},
    {"id": "pnl_div", "label": "P&L + dividends", "group": "P&L", "sort": None, "right": True, "default": False},
    {"id": "r_multiple", "label": "R-multiple", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "initial_risk", "label": "Initial risk", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "total_risk", "label": "Total risk", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "risk_source", "label": "Risk source", "group": "Risk", "sort": None, "right": False, "default": False},
    {"id": "stop", "label": "Initial stop", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "target", "label": "Profit target", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "planned_rr", "label": "Planned R:R", "group": "Risk", "sort": None, "right": True, "default": False},
    {"id": "mfe", "label": "MFE", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "mae", "label": "MAE", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "mfe_r", "label": "MFE R", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "mae_r", "label": "MAE R", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "mfe_eff", "label": "MFE efficiency", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "mae_eff", "label": "MAE efficiency", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "left_on_table", "label": "Left on table", "group": "Excursion", "sort": None, "right": True, "default": False},
    {"id": "setup", "label": "Setup", "group": "Journal", "sort": None, "right": False, "default": False},
    {"id": "tags", "label": "Tags", "group": "Journal", "sort": None, "right": False, "default": False},
    {"id": "mistakes", "label": "Mistakes", "group": "Journal", "sort": None, "right": False, "default": False},
    {"id": "exec_grade", "label": "Execution grade", "group": "Journal", "sort": None, "right": False, "default": False},
]
BY_ID = {c["id"]: c for c in COLUMNS}
METRIC_COLS = {"fills", "position_value", "return_per_share", "r_multiple", "initial_risk", "total_risk",
               "risk_source", "planned_rr", "mfe_r", "mae_r", "mfe_eff", "mae_eff", "left_on_table"}
DIVIDEND_COLS = {"dividend", "pnl_div"}


def default_columns() -> list[str]:
    return [c["id"] for c in COLUMNS if c["default"]]


def clean(ids) -> list[str]:
    out: list[str] = []
    for i in ids if isinstance(ids, (list, tuple)) else []:
        if isinstance(i, str) and i in BY_ID and i not in out:
            out.append(i)
    return out


def get_columns(db: Session) -> list[str]:
    from app.services import get_state
    try:
        saved = clean(json.loads(get_state(db, KEY) or "null"))
    except ValueError:
        saved = []
    return saved or default_columns()


def save_columns(db: Session, ids) -> list[str]:
    from app.services import set_state
    cols = clean(ids)
    if not cols:
        raise ValueError("at least one column")
    set_state(db, KEY, json.dumps(cols))
    db.commit()
    return cols


def reset_columns(db: Session) -> list[str]:
    from app.services import set_state
    set_state(db, KEY, None)
    db.commit()
    return default_columns()


def row_metrics(db: Session, trades, cols) -> dict:
    """trade id -> metrics for the visible metric/dividend columns (empty when none is visible)."""
    want = set(cols)
    out: dict[int, dict] = {}
    if not trades or not (want & (METRIC_COLS | DIVIDEND_COLS)):
        return out
    if want & METRIC_COLS:
        from app import metrics
        from app.routes.reports import default_risk
        risk = default_risk(db)
        for t in trades:
            out[t.id] = metrics.trade_metrics(t, risk)
    if want & DIVIDEND_COLS:
        from app import dividends
        divs = dividends.for_trades(db, trades)
        for t in trades:
            d = divs.get(t.id) or {}
            m = out.setdefault(t.id, {})
            m["dividend"] = d.get("display") if d.get("count") else None
            m["pnl_div"] = None if m["dividend"] is None else (t.net_pnl or 0.0) + m["dividend"]
    return out
