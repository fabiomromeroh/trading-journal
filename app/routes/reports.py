"""Reports module (TraderSync-style breakdowns), risk settings, per-trade planned risk and the
widget-layout API used by the dashboard and the trade page."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics, widgets
from app.config import get_settings
from app.db import get_db
from app.models import Tag, Trade
from app.services import get_state, set_state
from app.web import apply_trade_filters, base_context, parse_filters, templates

router = APIRouter()
DEFAULT_RISK_STATE = "risk:default_per_trade"
TABS = [("overview", "Overview"), ("timing", "Days & times"), ("price", "Price & volume"),
        ("instrument", "Instrument & symbol"), ("tags", "Setups & tags"), ("winloss", "Win/loss & expectancy"),
        ("drawdown", "Drawdown"), ("excursion", "MFE / MAE")]
# Query parameters shared with the Trades page filters.
EXTRA_PARAMS = ("symbol", "direction", "status", "asset", "outcome", "setup", "tag")


def default_risk(db: Session) -> float | None:
    v = get_state(db, DEFAULT_RISK_STATE)
    try:
        return float(v) if v else None
    except ValueError:
        return None


def apply_extra_filters(stmt, q):
    """Same semantics as the Trades page filters (symbol contains, side, status, ...)."""
    if q.get("symbol"):
        stmt = stmt.where(Trade.underlying.ilike(f"%{q['symbol'].strip().upper()}%"))
    if q.get("direction") in ("LONG", "SHORT"):
        stmt = stmt.where(Trade.direction == q["direction"])
    if q.get("status") in ("OPEN", "CLOSED"):
        stmt = stmt.where(Trade.status == q["status"])
    if q.get("asset") in ("STOCK", "OPTION"):
        stmt = stmt.where(Trade.asset_type == q["asset"])
    from app import outcome as _oc
    if q.get("outcome") == "win":
        stmt = stmt.where(Trade.status == "CLOSED", _oc.sql_win(Trade.net_pnl))
    elif q.get("outcome") == "loss":
        stmt = stmt.where(Trade.status == "CLOSED", _oc.sql_loss(Trade.net_pnl))
    elif q.get("outcome") == "be":
        stmt = stmt.where(Trade.status == "CLOSED", _oc.sql_be(Trade.net_pnl))
    if q.get("setup"):
        stmt = stmt.where(Trade.setup == q["setup"])
    if q.get("tag"):
        stmt = stmt.where(Trade.tags.any(Tag.name == q["tag"]))
    return stmt


def filtered_trades(request: Request, db: Session):
    f = parse_filters(request)
    stmt = apply_extra_filters(apply_trade_filters(select(Trade), f), request.query_params)
    return f, list(db.scalars(stmt))


def _series(rows, key="net"):
    return {"labels": [r["label"] for r in rows], "pnl": [round(r[key] or 0, 2) for r in rows],
            "count": [r["trades"] for r in rows],
            "win_rate": [round(r["win_pct"], 1) if r["win_pct"] is not None else None for r in rows]}


def report_data(trades, tz: str, risk: float | None, events=None) -> dict:
    from app import realized as rz
    closed = metrics.closed_sorted(trades)
    evs = rz.events(trades) if events is None else events
    s = metrics.summarize(trades, tz, risk, events=evs)
    b = metrics.breakdowns(trades, tz, risk)
    dd = metrics.drawdown(closed, tz)
    days = rz.daily(evs, tz)
    cum = rz.cumulative(days)
    exc = []
    for t in closed:
        if t.mfe is None and t.mae is None:
            continue
        exc.append({"id": t.id, "symbol": t.symbol, "closed": t.closed_at, "net": t.net_pnl, "gross": t.gross_pnl,
                    "mfe": t.mfe, "mae": t.mae, "eff": metrics.mfe_efficiency(t), "mae_eff": metrics.mae_efficiency(t),
                    "left": (t.mfe - t.gross_pnl) if t.mfe is not None else None})
    rtrades = []
    for t in closed:
        r = metrics.r_multiple(t, risk)
        if r is not None:
            rk, src = metrics.risk_of(t, risk)
            rtrades.append({"id": t.id, "symbol": t.symbol, "closed": t.closed_at, "net": t.net_pnl, "risk": rk,
                            "source": src, "r": r})
    charts = {
        "cum": cum, "drawdown": dd["series"],
        "daily": {"labels": [d.isoformat() for d in days], "pnl": [round(v.net, 2) for v in days.values()],
                  "count": [v.count for v in days.values()], "win_rate": [None for _ in days]},
        "mfe_scatter": [{"x": e["mfe"], "y": round(e["net"], 2), "s": e["symbol"]} for e in exc if e["mfe"] is not None],
        "mae_scatter": [{"x": e["mae"], "y": round(e["net"], 2), "s": e["symbol"]} for e in exc if e["mae"] is not None],
    }
    for k, rows in b.items():
        charts[k] = _series(rows)
    return {"s": s, "b": b, "dd": dd, "charts": charts, "exc": exc, "rtrades": rtrades, "n_closed": len(closed)}


@router.get("/reports")
def reports(request: Request, db: Session = Depends(get_db)):
    f, trades = filtered_trades(request, db)
    tab = request.query_params.get("tab", "overview")
    if tab not in dict(TABS):
        tab = "overview"
    tz = get_settings().display_tz
    risk = default_risk(db)
    from app.web import realized_scope
    evs, _, _ = realized_scope(db, f, lambda st: apply_extra_filters(st, request.query_params))
    data = report_data(trades, tz, risk, events=evs)
    q = request.query_params
    setups = sorted(s for s in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None)).distinct()) if s)
    tags = list(db.scalars(select(Tag.name).order_by(Tag.name)))
    params = {k: v for k, v in q.items() if k != "tab" and v}
    return templates.TemplateResponse(request, "reports.html", base_context(
        request, db, nav="reports", f=f, q=q, tab=tab, tabs=TABS, params=params, setups=setups, tags=tags,
        default_risk=risk, flash=request.session.pop("flash", None), **data))


@router.post("/reports/risk")
def save_default_risk(request: Request, default_risk_value: str = Form("", alias="default_risk"),
                      db: Session = Depends(get_db)):
    v = default_risk_value.strip().replace("$", "").replace(",", "")
    try:
        val = float(v) if v else None
    except ValueError:
        val = None
    set_state(db, DEFAULT_RISK_STATE, f"{val:.2f}" if val and val > 0 else None)
    db.commit()
    request.session["flash"] = (f"Default risk per trade set to ${val:,.2f}: trades without their own stop or risk use it for R-multiples."
                                if val and val > 0 else "Default risk per trade cleared.")
    back = request.headers.get("referer") or "/reports?tab=winloss"
    return RedirectResponse(back if back.startswith(str(request.base_url)) else "/reports?tab=winloss", status_code=303)


def _num(v: str):
    v = (v or "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x > 0 else None


@router.post("/trades/{trade_id}/risk")
def save_trade_risk(trade_id: int, request: Request, initial_stop: str = Form(""), risk_amount: str = Form(""),
                    profit_target: str = Form(""), db: Session = Depends(get_db)):
    t = db.get(Trade, trade_id)
    if t is None:
        raise HTTPException(404, "Trade not found")
    t.initial_stop, t.risk_amount, t.profit_target = _num(initial_stop), _num(risk_amount), _num(profit_target)
    db.commit()
    risk = default_risk(db)
    return templates.TemplateResponse(request, "partials/risk_form.html", {
        "request": request, "t": t, "tm": metrics.trade_metrics(t, risk), "default_risk": risk, "saved": True})


# ------------------------------------------------------------------------- widget layouts
@router.get("/layout/{page}")
def get_layout(page: str, db: Session = Depends(get_db)):
    if page not in widgets.CATALOGS:
        raise HTTPException(404)
    return {"page": page, "widgets": widgets.get_layout(db, page), "default": widgets.default_layout(page)}


@router.post("/layout/{page}")
async def save_layout(page: str, request: Request, db: Session = Depends(get_db)):
    if page not in widgets.CATALOGS:
        raise HTTPException(404)
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad JSON"}, status_code=400)
    ids = widgets.save_layout(db, page, body.get("widgets") if isinstance(body, dict) else None,
                              sizes=body.get("sizes") if isinstance(body, dict) else None)
    return {"ok": True, "widgets": ids}


@router.post("/layout/{page}/reset")
def reset_layout(page: str, db: Session = Depends(get_db)):
    if page not in widgets.CATALOGS:
        raise HTTPException(404)
    return {"ok": True, "widgets": widgets.reset_layout(db, page)}


def dashboard_extras(db: Session, trades, tz: str, events=None, cumulative=None) -> dict:
    """Extra metrics/charts + widget layout for the customisable dashboard."""
    from app import realized as rz
    risk = default_risk(db)
    closed = metrics.closed_sorted(trades)
    b = metrics.breakdowns(trades, tz, risk)
    evs = rz.events(trades) if events is None else events
    xcharts = {"cumgross": cumulative or rz.cumulative(rz.daily(evs, tz)), "drawdown": metrics.drawdown(closed, tz)["series"],
               "month": _series(b["month"]), "price": _series(b["entry_price"]), "size": _series(b["size"]),
               "pnldist": _series(b["pnl_dist"])}
    return {"m": metrics.summarize(trades, tz, risk, events=evs), "xcharts": xcharts, "L": widgets.layout_ctx(db, "dashboard")}


def trade_extras(db: Session, t: Trade) -> dict:
    """Per-trade stats + the stat-bar layout for the trade page."""
    risk = default_risk(db)
    return {"tm": metrics.trade_metrics(t, risk), "TL": widgets.layout_ctx(db, "trade"), "default_risk": risk}
