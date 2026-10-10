from __future__ import annotations

import json
from datetime import timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import stops
from app.db import get_db
from app.models import Tag, Trade, TradeFill, TradeMistake
from app.prices import excursion_basis, excursion_note, get_chart, update_trade_excursions
from app.routes.reports import trade_extras
from app.web import apply_trade_filters, base_context, parse_filters, templates

router = APIRouter()
PAGE = 50
SORTS = {
    "opened": Trade.opened_at, "closed": Trade.closed_at, "symbol": Trade.symbol, "pnl": Trade.net_pnl,
    "return": Trade.return_pct, "qty": Trade.quantity, "fees": Trade.fees,
    "hold": func.coalesce(Trade.closed_at, Trade.opened_at),
}


LIST_KEYS = ("preset", "start", "end", "account", "symbol", "direction", "asset", "status", "outcome", "setup",
             "tag", "mistake", "sort", "dir", "realized_day")


def realized_on_day(db: Session, f, day) -> dict:
    """Trades with a realized event (closing fill, incl. partial exits of open trades, or fees) on
    ``day`` (display-tz date), with each trade's realized amount that day. Same rule as the calendar,
    so the total reconciles with the calendar cell / daily bar."""
    from dataclasses import replace
    from app import realized as rz
    from app.config import get_settings
    tz = get_settings().display_tz
    trades = list(db.scalars(apply_trade_filters(select(Trade), replace(f, start=None, end=None))))
    per: dict[int, dict] = {}
    for e in rz.clip(rz.events(trades), tz, day, day):
        if not e.is_close and abs(e.fees) < 0.004:
            continue  # an opening fill without fees realizes nothing (same rule as the calendar)
        r = per.setdefault(e.trade_id, {"net": 0.0, "gross": 0.0, "fees": 0.0, "qty": 0.0, "exits": 0,
                                        "partial": False, "final": False})
        r["net"] += e.net
        r["gross"] += e.gross
        r["fees"] += e.fees
        if e.is_close:
            r["qty"] += e.qty
            r["exits"] += 1
            r["final"] = r["final"] or e.final
            r["partial"] = r["partial"] or not e.final
    return per


def _day_param(q):
    from datetime import date
    try:
        return date.fromisoformat(q.get("realized_day", "")[:10]) if q.get("realized_day") else None
    except ValueError:
        return None


def filtered_trades(request: Request, db: Session | None = None):
    """Trades-list query (filters + sort) shared by the list and the trade page's sidebar.
    ``realized_day=YYYY-MM-DD`` selects the trades that realized P&L that day (calendar / daily bar
    drill-down) instead of filtering by open date."""
    f = parse_filters(request)
    q = request.query_params
    day = _day_param(q)
    if day is not None and db is not None:
        from dataclasses import replace
        per = realized_on_day(db, f, day)
        f = replace(f, start=None, end=None)
        stmt = apply_trade_filters(select(Trade), f).where(Trade.id.in_(list(per) or [-1]))
        request.state.realized_day = (day, per)
    else:
        stmt = apply_trade_filters(select(Trade), f)
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
    if q.get("mistake"):
        stmt = stmt.where(Trade.mistake_rows.any(TradeMistake.name == q["mistake"]))
    sort = q.get("sort", "opened")
    sort = sort if sort in SORTS else "opened"
    desc = q.get("dir", "desc") != "asc"
    col = SORTS[sort]
    stmt = stmt.order_by(col.desc() if desc else col.asc(), Trade.id.desc())
    list_qs = urlencode([(k, v) for k, v in q.multi_items() if k in LIST_KEYS and v != ""])
    return stmt, f, q, sort, desc, list_qs


@router.get("/trades")
def trades_list(request: Request, db: Session = Depends(get_db)):
    stmt, f, q, sort, desc, list_qs = filtered_trades(request, db)
    day, day_rows = getattr(request.state, "realized_day", (None, None))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    try:
        page = max(1, int(q.get("page", 1) or 1))
    except ValueError:
        page = 1
    rows = list(db.scalars(stmt.offset((page - 1) * PAGE).limit(PAGE)))
    sub = stmt.order_by(None).subquery()
    agg = db.execute(select(sub.c.status, func.coalesce(func.sum(sub.c.net_pnl), 0.0), func.count(sub.c.id))
                     .group_by(sub.c.status)).all()
    by_status = {st: (float(pnl or 0.0), int(n)) for st, pnl, n in agg}
    closed_pnl, n_closed = by_status.get("CLOSED", (0.0, 0))
    open_pnl, n_open = by_status.get("OPEN", (0.0, 0))
    setups, tags, mistakes = filter_options(db)
    params = {k: v for k, v in q.items() if k not in ("sort", "dir", "page")}
    return templates.TemplateResponse(request, "trades.html", base_context(
        request, db, nav="trades", f=f, trades=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
        sort=sort, desc=desc, params=params, q=q, setups=setups, tags=tags, mistakes=mistakes, list_qs=list_qs,
        filtered_pnl=closed_pnl, n_closed=n_closed, n_open=n_open, open_realized=open_pnl,
        realized_pnl=closed_pnl + open_pnl, day=day, day_rows=day_rows or {},
        day_total=sum(r["net"] for r in (day_rows or {}).values())))


def filter_options(db: Session):
    """(setups, tags, mistakes) for filter dropdowns: the option lists plus anything trades already carry."""
    from app import options
    setups = set(options.names(db, "setup")) | {x for x in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None)).distinct()) if x}
    tags = set(options.names(db, "tag")) | set(db.scalars(select(Tag.name)))
    mistakes = set(options.names(db, "mistake")) | set(db.scalars(select(TradeMistake.name).distinct()))
    key = lambda x: x.lower()  # noqa: E731
    return sorted(setups, key=key), sorted(tags, key=key), sorted(mistakes, key=key)


def _get_trade(db: Session, trade_id: int) -> Trade:
    t = db.get(Trade, trade_id)
    if t is None:
        raise HTTPException(404, "Trade not found")
    return t


SIDEBAR_LIMIT = 1000


@router.get("/trades/{trade_id}")
def trade_detail(trade_id: int, request: Request, db: Session = Depends(get_db)):
    t = _get_trade(db, trade_id)
    _auto_stop(db, t)
    stmt, f, q, sort, desc, list_qs = filtered_trades(request, db)
    cols = stmt.with_only_columns(Trade.id, Trade.symbol, Trade.opened_at, Trade.closed_at, Trade.direction,
                                  Trade.status, Trade.net_pnl, Trade.time_known, Trade.is_demo)
    nav_rows = db.execute(cols.limit(SIDEBAR_LIMIT)).all()
    ids = [r.id for r in nav_rows]
    in_list = t.id in ids
    if in_list:
        i = ids.index(t.id)
        prev_id = ids[i - 1] if i > 0 else None
        next_id = ids[i + 1] if i + 1 < len(ids) else None
    else:  # filters exclude this trade: chronological neighbours
        prev_id = db.scalar(select(Trade.id).where(Trade.opened_at > t.opened_at).order_by(Trade.opened_at.asc()))
        next_id = db.scalar(select(Trade.id).where(Trade.opened_at < t.opened_at).order_by(Trade.opened_at.desc()))
    filtered = any(k in q for k in LIST_KEYS if k not in ("sort", "dir"))
    return templates.TemplateResponse(request, "trade_detail.html", base_context(
        request, db, nav="trades", t=t, prev_id=prev_id, next_id=next_id,
        saved=False, fullscreen=True, **journal_ctx(db, t), stop_rule=stops.get_rule(db), opened_ts=int(t.opened_at.replace(tzinfo=timezone.utc).timestamp()), mfe_note=excursion_note(excursion_basis(t)), nav_rows=nav_rows,
        list_qs=list_qs, in_list=in_list, list_filtered=filtered, list_truncated=len(nav_rows) >= SIDEBAR_LIMIT,
        **trade_extras(db, t, live_price(t))))


def _auto_stop(db: Session, t: Trade) -> None:
    """Default initial stop (low of the entry day) when the trade has none yet; never blocks the page."""
    try:
        if stops.apply_default(db, t) in ("set", "refreshed"):
            db.commit()
    except Exception:  # noqa: BLE001  price provider trouble must not break the trade page
        db.rollback()


def live_price(t: Trade) -> float | None:
    """Latest price of an open stock trade (cached ~90 s) for Current R; None when unavailable."""
    if t.status != "OPEN":
        return None
    from app import quotes
    try:
        q = quotes.get_quotes([t]).get(t.symbol) if quotes.enabled() else None
    except Exception:  # noqa: BLE001
        return None
    return q.price if q else None


def journal_ctx(db: Session, t: Trade) -> dict:
    from app import options
    from app.routes.journal import chart_r_config
    return {"opt": {k: options.names(db, k) for k in options.KINDS},
            "opt_usage": {k: options.usage(db, k) for k in options.KINDS},
            "questions": options.get_questions(db), "answers": t.answers, "grades": options.GRADES,
            "plan_choices": options.PLAN_CHOICES, "r_cfg": chart_r_config(db)}


def _names(raw: str) -> list[str]:
    out: list[str] = []
    for n in (raw or "").split(","):
        n = " ".join(n.split())[:60]
        if n and n.lower() not in [x.lower() for x in out]:
            out.append(n)
    return out


@router.post("/trades/{trade_id}/journal")
async def save_journal(trade_id: int, request: Request, db: Session = Depends(get_db)):
    """Save the journal panel. Only the fields that were posted change (autosave posts everything it shows)."""
    from datetime import datetime as _dt
    from app import options
    t = _get_trade(db, trade_id)
    form = await request.form()
    has = lambda k: k in form  # noqa: E731
    if has("notes"):
        t.notes = str(form["notes"]).strip() or None
    if has("setup"):
        name = options.add(db, "setup", str(form["setup"])) if str(form["setup"]).strip() else None
        t.setup = (name or "")[:80] or None
    if has("rating"):
        r = str(form["rating"])
        t.rating = int(r) if r.isdigit() and 1 <= int(r) <= 5 else None
    if has("grade"):
        g = str(form["grade"]).strip().upper()
        t.exec_grade = g if g in options.GRADES else None
    if has("tags"):
        tag_objs = []
        for n in _names(str(form["tags"])):
            n = options.add(db, "tag", n) or n
            tag = db.scalar(select(Tag).where(func.lower(Tag.name) == n.lower()))
            if tag is None:
                tag = Tag(name=n)
                db.add(tag)
            tag_objs.append(tag)
        t.tags = tag_objs
    if has("mistakes"):
        want = [options.add(db, "mistake", n) or n for n in _names(str(form["mistakes"]))]
        have = {m.name: m for m in t.mistake_rows}
        for n, m in list(have.items()):
            if n not in want:
                t.mistake_rows.remove(m)
        for n in want:
            if n not in have:
                t.mistake_rows.append(TradeMistake(trade_id=t.id, name=n))
    answers = t.answers
    qids = {q["id"]: q for q in options.get_questions(db)}
    touched = False
    for key in form.keys():
        if key.startswith("ans_"):
            qid = key[4:]
            val = str(form[key]).strip()
            answers[qid] = val
            if not val:
                answers.pop(qid, None)
            touched = True
    if touched:
        t.journal = json.dumps(answers) if answers else None
    _ = qids
    db.commit()
    if request.headers.get("x-autosave"):
        return JSONResponse({"ok": True, "at": _dt.now(timezone.utc).isoformat()})
    return templates.TemplateResponse(request, "partials/journal_form.html", {
        "request": request, "t": t, "saved": True, **journal_ctx(db, t)})


@router.get("/trades/{trade_id}/r.json")
def trade_r(trade_id: int, db: Session = Depends(get_db)):
    """Current R of an open trade at the latest quote (polled by the stat bar)."""
    from app.metrics import trade_metrics
    from app.routes.reports import default_risk
    t = _get_trade(db, trade_id)
    m = trade_metrics(t, default_risk(db), live_price(t))
    return {"current_r": m["current_r"], "price": m["price"], "open_pnl": m["open_pnl"], "r_multiple": m["r_multiple"],
            "total_r": m["total_r"], "position": m["position"]["text"], "open_value": m["open_value"]}


@router.get("/trades/{trade_id}/chart.json")
def trade_chart(trade_id: int, tf: str | None = None, db: Session = Depends(get_db)):
    """Candles (+volume) for one timeframe, fill markers snapped to bars, and timeframe availability."""
    t = _get_trade(db, trade_id)
    data = get_chart(db, t, tf)
    exc = update_trade_excursions(db, t)
    db.commit()
    data["mfe"], data["mae"] = t.mfe, t.mae
    from app.metrics import trade_metrics
    from app.routes.reports import default_risk
    m = trade_metrics(t, default_risk(db))
    data["mfe_r"], data["mae_r"] = m["mfe_r"], m["mae_r"]
    data["excursion"] = {**exc, "note": excursion_note(exc)}
    return JSONResponse(json.loads(json.dumps(data, default=str)))


_ = TradeFill
