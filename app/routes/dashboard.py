from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Execution, ImportBatch, Trade
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
        last_sync=last_sync))
