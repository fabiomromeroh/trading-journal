"""AI review page: anonymised summary + prompt to copy into any AI chat. No third-party calls from the server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import ai_review, coach
from app.config import get_settings
from app.db import get_db
from app.routes.reports import default_risk, filtered_trades
from app.web import base_context, templates

router = APIRouter()


def _opts(request: Request):
    q = request.query_params
    return q.get("symbols", "1") != "0", q.get("notes", "1") != "0"


def _period(f) -> str:
    if f.start or f.end:
        return f"{f.start or 'start'} to {f.end or 'today'}"
    return {"7d": "last 7 days", "30d": "last 30 days", "90d": "last 90 days", "ytd": "year to date",
            "1y": "last 12 months"}.get(f.preset, "all time")


def _data(request: Request, db: Session):
    f, trades = filtered_trades(request, db)
    symbols, notes = _opts(request)
    risk = default_risk(db)
    tz = get_settings().display_tz
    return f, trades, ai_review.build(trades, tz, risk, period=_period(f), symbols=symbols, notes=notes), risk, tz


@router.get("/ai-review")
def ai_review_page(request: Request, db: Session = Depends(get_db)):
    from app.routes.trades import filter_options
    f, trades, data, risk, tz = _data(request, db)
    symbols, notes = _opts(request)
    setups, tags, mistakes = filter_options(db)
    q = request.query_params
    params = {k: v for k, v in q.items() if v and k not in ("symbols", "notes")}
    from urllib.parse import urlencode
    qs = urlencode(params) + ("&" if params else "")
    qs += ("" if symbols else "symbols=0&") + ("" if notes else "notes=0&")
    md = ai_review.to_markdown(data)
    return templates.TemplateResponse(request, "ai_review.html", base_context(
        request, db, nav="reports", f=f, q=q, tab=None, params=params, setups=setups, tags=tags, mistakes=mistakes,
        data=data, md=md, qs=qs, symbols=symbols, notes=notes, coach=coach.insights(trades, tz, risk),
        n_closed=data["overall"]["closed_trades"], chars=len(md)))


@router.get("/ai-review/export.md")
def export_md(request: Request, db: Session = Depends(get_db)):
    _, _, data, _, _ = _data(request, db)
    return Response(ai_review.to_markdown(data), media_type="text/markdown",
                    headers={"Content-Disposition": 'attachment; filename="trading-journal-ai-review.md"', "Cache-Control": "no-store"})


@router.get("/ai-review/export.json")
def export_json(request: Request, db: Session = Depends(get_db)):
    _, _, data, _, _ = _data(request, db)
    return Response(ai_review.to_json(data), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="trading-journal-ai-review.json"', "Cache-Control": "no-store"})
