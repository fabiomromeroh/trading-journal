from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Execution, ImportBatch, Trade
from app.routes.reports import dashboard_extras
from app.stats import calendar_months, compute
from app.web import apply_trade_filters, base_context, parse_filters, templates

router = APIRouter()


def _series(buckets):
    return {"labels": [b.label for b in buckets], "pnl": [round(b.pnl, 2) for b in buckets],
            "count": [b.count for b in buckets], "win_rate": [round(b.win_rate, 1) for b in buckets]}


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    f = parse_filters(request)
    tz = get_settings().display_tz
    stmt = apply_trade_filters(select(Trade), f)
    trades = list(db.scalars(stmt))
    open_count = sum(1 for t in trades if t.status == "OPEN")
    st = compute(trades, tz, open_count=open_count)
    has_any = db.scalar(select(func.count(Execution.id))) or 0
    last_import = db.scalar(select(ImportBatch).where(ImportBatch.status == "committed")
                            .order_by(ImportBatch.committed_at.desc()))
    from app.models import SyncRun
    last_sync = db.scalar(select(SyncRun).where(SyncRun.status.in_(("success", "partial")), SyncRun.sources.is_not(None))
                          .order_by(SyncRun.id.desc()))
    recent = sorted([t for t in trades if t.status == "CLOSED"], key=lambda t: t.closed_at, reverse=True)[:8]
    anchor = _anchor(db, f, trades, st)
    daily = list(st.daily.values())
    charts = {
        "equity": {"labels": [e[0] for e in st.equity], "values": [e[1] for e in st.equity]},
        "daily": {"labels": [d.label for d in daily[-60:]], "pnl": [round(d.pnl, 2) for d in daily[-60:]]},
        "symbol": _series(st.by_symbol[:12] + ([] if len(st.by_symbol) <= 12 else [])),
        "weekday": _series(st.by_weekday),
        "hour": _series(st.by_hour),
        "direction": _series(st.by_direction),
        "asset": _series(st.by_asset),
        "hold": _series(st.by_hold),
        "winloss": [st.wins, st.losses, st.scratches],
    }
    return templates.TemplateResponse(request, "dashboard.html", base_context(
        request, db, nav="dashboard", f=f, st=st, charts=charts, months=calendar_months(st.daily, max_months=3),
        recent=recent, has_any=has_any, last_import=last_import,
        last_sync=last_sync, anchor=anchor, **dashboard_extras(db, trades, tz)))


def _anchor(db: Session, f, trades, st):
    """Unrealized P&L of open trades at SnapTrade's latest prices, and the account check
    (value - net deposits vs journal realized + unrealized). Only for the unfiltered view."""
    from app.models import Account
    from app.sources.snaptrade import portfolio_summary
    from app.stats import unrealized
    from app.symbols import canonical_symbol, load_aliases
    if f.start or f.end:
        return None
    accts = [a.id for a in db.scalars(select(Account).where(Account.is_demo.is_(False)))
             if not f.account_id or a.id == f.account_id]
    port = portfolio_summary(db, accts)
    if port is None:
        return None
    aliases = load_aliases(db)
    prices = {canonical_symbol(sym, aliases): p["price"] for sym, p in port["positions"].items()}
    unreal, rows = unrealized(trades, prices)
    journal_total = st.realized + unreal
    return {"unrealized": unreal, "rows": rows, "value": port["value"], "cash": port["cash"],
            "net_deposits": port["net_deposits"], "total_pnl": port["total_pnl"], "as_of": port["as_of"],
            "journal_total": journal_total, "diff": journal_total - port["total_pnl"],
            "missing_prices": [r["symbol"] for r in rows if r["price"] is None],
            "securities_transfers": port["securities_transfers"]}
