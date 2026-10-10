"""Journal options (dropdown lists), note questions, default-stop settings and chart R levels."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import options, stops
from app.db import get_db
from app.services import get_state, set_state

router = APIRouter()
R_STATE = "chart:r_levels"
DEFAULT_R = {"show": True, "levels": [3, 8, 10]}


def chart_r_config(db: Session) -> dict:
    try:
        v = json.loads(get_state(db, R_STATE) or "null")
    except ValueError:
        v = None
    if not isinstance(v, dict):
        return dict(DEFAULT_R)
    return {"show": bool(v.get("show", True)), "levels": _levels(v.get("levels")) or list(DEFAULT_R["levels"])}


def _levels(raw) -> list[float]:
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    out: list[float] = []
    for x in raw if isinstance(raw, list) else []:
        try:
            v = float(str(x).strip().rstrip("Rr"))
        except ValueError:
            continue
        v = int(v) if v == int(v) else round(v, 2)
        if 0 < v <= 100 and v not in out:
            out.append(v)
    return sorted(out)[:8]


async def _json(request: Request) -> dict:
    try:
        v = json.loads(await request.body() or b"{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


@router.post("/options/{kind}")
async def option_add(kind: str, request: Request, db: Session = Depends(get_db)):
    if kind not in options.KINDS:
        return JSONResponse({"ok": False}, status_code=404)
    name = options.add(db, kind, str((await _json(request)).get("name", "")))
    db.commit()
    return {"ok": bool(name), "name": name, "options": options.names(db, kind)}


@router.post("/options/{kind}/rename")
async def option_rename(kind: str, request: Request, db: Session = Depends(get_db)):
    body = await _json(request)
    if kind not in options.KINDS:
        return JSONResponse({"ok": False}, status_code=404)
    new = options.rename(db, kind, str(body.get("old", "")), str(body.get("new", "")))
    db.commit()
    return {"ok": bool(new), "name": new, "options": options.names(db, kind), "usage": options.usage(db, kind)}


@router.post("/options/{kind}/remove")
async def option_remove(kind: str, request: Request, db: Session = Depends(get_db)):
    """Takes the option off the list only; trades that already carry it keep it."""
    body = await _json(request)
    if kind not in options.KINDS:
        return JSONResponse({"ok": False}, status_code=404)
    ok = options.remove(db, kind, str(body.get("name", "")))
    db.commit()
    return {"ok": ok, "options": options.names(db, kind), "usage": options.usage(db, kind)}


@router.post("/settings/journal-questions")
async def save_questions(request: Request, db: Session = Depends(get_db)):
    body = await _json(request)
    qs = options.save_questions(db, body.get("questions"))
    return {"ok": True, "questions": qs}


@router.post("/settings/journal/save")
async def save_journal_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if "stop_rule" in form:
        rule = stops.set_rule(db, str(form["stop_rule"]))
        request.session["flash"] = f"Default stop rule: {stops.RULES[rule]}."
    if "r_levels" in form or "r_show" in form or "r_form" in form:
        cfg = {"show": "r_show" in form, "levels": _levels(str(form.get("r_levels", ""))) or DEFAULT_R["levels"]}
        set_state(db, R_STATE, json.dumps(cfg))
        db.commit()
        request.session["flash"] = "R levels saved: " + ", ".join(f"{x}R" for x in cfg["levels"]) + ("" if cfg["show"] else " (hidden on the chart)")
    return RedirectResponse("/settings#journal-settings", status_code=303)


@router.post("/settings/journal/backfill-stops")
def backfill_stops(request: Request, db: Session = Depends(get_db)):
    res = stops.backfill(db)
    request.session["flash"] = (f"Default stops: {res['set']} set, {res['refreshed']} refreshed, {res['no-data']} without price data, "
                                f"{res['option']} options skipped; manual stops untouched.")
    return RedirectResponse("/settings#journal-settings", status_code=303)


@router.post("/chart/r-levels")
async def chart_r_levels(request: Request, db: Session = Depends(get_db)):
    """Saved from the chart's R-levels menu (so phone and desktop match)."""
    body = await _json(request)
    cfg = {"show": bool(body.get("show", True)), "levels": _levels(body.get("levels")) or list(DEFAULT_R["levels"])}
    set_state(db, R_STATE, json.dumps(cfg))
    db.commit()
    return {"ok": True, **cfg}
