from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Tag, Trade, TradeFill
from app.prices import compute_excursions, get_chart, markers
from app.web import apply_trade_filters, base_context, parse_filters, templates

router = APIRouter()
PAGE = 50
SORTS = {
    "opened": Trade.opened_at, "closed": Trade.closed_at, "symbol": Trade.symbol, "pnl": Trade.net_pnl,
    "return": Trade.return_pct, "qty": Trade.quantity, "fees": Trade.fees,
    "hold": func.coalesce(Trade.closed_at, Trade.opened_at),
}


@router.get("/trades")
def trades_list(request: Request, db: Session = Depends(get_db)):
    f = parse_filters(request)
    q = request.query_params
    stmt = apply_trade_filters(select(Trade), f)
    if q.get("symbol"):
        stmt = stmt.where(Trade.underlying.ilike(f"%{q['symbol'].strip().upper()}%"))
    if q.get("direction") in ("LONG", "SHORT"):
        stmt = stmt.where(Trade.direction == q["direction"])
    if q.get("status") in ("OPEN", "CLOSED"):
        stmt = stmt.where(Trade.status == q["status"])
    if q.get("asset") in ("STOCK", "OPTION"):
        stmt = stmt.where(Trade.asset_type == q["asset"])
    if q.get("outcome") == "win":
        stmt = stmt.where(Trade.status == "CLOSED", Trade.net_pnl > 0)
    elif q.get("outcome") == "loss":
        stmt = stmt.where(Trade.status == "CLOSED", Trade.net_pnl < 0)
    if q.get("setup"):
        stmt = stmt.where(Trade.setup == q["setup"])
    if q.get("tag"):
        stmt = stmt.where(Trade.tags.any(Tag.name == q["tag"]))
    sort = q.get("sort", "opened")
    desc = q.get("dir", "desc") != "asc"
    col = SORTS.get(sort, Trade.opened_at)
    stmt = stmt.order_by(col.desc() if desc else col.asc(), Trade.id.desc())
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = max(1, int(q.get("page", 1) or 1))
    rows = list(db.scalars(stmt.offset((page - 1) * PAGE).limit(PAGE)))
    sub = stmt.where(Trade.status == "CLOSED").order_by(None).subquery()
    agg = db.execute(select(func.sum(sub.c.net_pnl), func.count(sub.c.id))).first()
    setups = [s for s in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None)).distinct()) if s]
    tags = list(db.scalars(select(Tag.name).order_by(Tag.name)))
    params = {k: v for k, v in q.items() if k not in ("sort", "dir", "page")}
    return templates.TemplateResponse(request, "trades.html", base_context(
        request, db, nav="trades", f=f, trades=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
        sort=sort, desc=desc, params=params, q=q, setups=sorted(setups), tags=tags,
        filtered_pnl=(agg[0] or 0.0) if agg else 0.0))


def _get_trade(db: Session, trade_id: int) -> Trade:
    t = db.get(Trade, trade_id)
    if t is None:
        raise HTTPException(404, "Trade not found")
    return t


@router.get("/trades/{trade_id}")
def trade_detail(trade_id: int, request: Request, db: Session = Depends(get_db)):
    t = _get_trade(db, trade_id)
    prev_id = db.scalar(select(Trade.id).where(Trade.opened_at < t.opened_at).order_by(Trade.opened_at.desc()))
    next_id = db.scalar(select(Trade.id).where(Trade.opened_at > t.opened_at).order_by(Trade.opened_at.asc()))
    all_tags = list(db.scalars(select(Tag.name).order_by(Tag.name)))
    setups = sorted(s for s in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None)).distinct()) if s)
    return templates.TemplateResponse(request, "trade_detail.html", base_context(
        request, db, nav="trades", t=t, prev_id=prev_id, next_id=next_id, all_tags=all_tags, setups=setups,
        saved=False))


@router.post("/trades/{trade_id}/journal")
def save_journal(trade_id: int, request: Request, notes: str = Form(""), setup: str = Form(""),
                 rating: str = Form(""), tags: str = Form(""), db: Session = Depends(get_db)):
    t = _get_trade(db, trade_id)
    t.notes = notes.strip() or None
    t.setup = setup.strip()[:80] or None
    t.rating = int(rating) if rating.isdigit() and 1 <= int(rating) <= 5 else None
    names = []
    for n in tags.split(","):
        n = n.strip()[:60]
        if n and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    tag_objs = []
    for n in names:
        tag = db.scalar(select(Tag).where(func.lower(Tag.name) == n.lower()))
        if tag is None:
            tag = Tag(name=n)
            db.add(tag)
        tag_objs.append(tag)
    t.tags = tag_objs
    db.commit()
    return templates.TemplateResponse(request, "partials/journal_form.html", {
        "request": request, "t": t, "saved": True,
        "all_tags": list(db.scalars(select(Tag.name).order_by(Tag.name))),
        "setups": sorted(s for s in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None)).distinct()) if s)})


@router.get("/trades/{trade_id}/chart.json")
def trade_chart(trade_id: int, db: Session = Depends(get_db)):
    t = _get_trade(db, trade_id)
    data = get_chart(db, t)
    data["markers"] = markers(t, data["interval"]) if data["candles"] else []
    if data["candles"] and t.status == "CLOSED":
        mfe, mae = compute_excursions(t, data["candles"])
        if mfe is not None and (t.mfe != mfe or t.mae != mae):
            t.mfe, t.mae = mfe, mae
            db.commit()
    data["mfe"], data["mae"] = t.mfe, t.mae
    return JSONResponse(json.loads(json.dumps(data, default=str)))


_ = TradeFill
