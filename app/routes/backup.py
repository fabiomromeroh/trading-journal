"""Scheduled-backup endpoints (token-authenticated, read-only export) and their Settings actions."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app import backup
from app.db import get_db
from app.models import utcnow
from app.routes.email_sync import _client_ip, _provided_token
from app.security import LoginThrottle

router = APIRouter()
_throttle = LoginThrottle(limit=5, window=900)   # failed token attempts per IP


def _auth(request: Request, db: Session) -> JSONResponse | None:
    ip = _client_ip(request)
    if _throttle.blocked(ip):
        return JSONResponse({"ok": False, "error": "too many failed attempts"}, status_code=429)
    if not backup.verify_token(db, _provided_token(request)):
        _throttle.fail(ip)
        return JSONResponse({"ok": False, "error": "invalid or missing backup token"}, status_code=401)
    return None


@router.get("/api/backup/export")
async def export(request: Request, db: Session = Depends(get_db)):
    """Gzipped JSON of the whole journal (no credentials / password hashes). Not a keep-awake visit."""
    bad = _auth(request, db)
    if bad:
        return bad
    wait = backup.rate_limited()
    if wait:
        return JSONResponse({"ok": False, "error": "rate limited", "retry_after": wait}, status_code=429,
                            headers={"Retry-After": str(wait)})

    def work():
        data = backup.build(db, include_cache=False)
        body = backup.encode(data)
        info = backup.mark_export(db, body, data)
        return body, info

    body, info = await run_in_threadpool(work)
    fname = f"trading-journal-{utcnow():%Y%m%d-%H%M%S}.json.gz"
    return Response(body, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{fname}"', "X-Backup-Sha256": info["sha256"],
        "X-Backup-Rows": str(info["rows"]), "X-Backup-Trades": str(info["counts"].get("trades", 0)),
        "Cache-Control": "no-store"})


@router.post("/api/backup/confirm")
async def confirm(request: Request, db: Session = Depends(get_db)):
    """The job reports the file it stored (SHA-256 of the exported bytes); Settings shows the result."""
    bad = _auth(request, db)
    if bad:
        return bad
    try:
        sha = (json.loads(await request.body() or b"{}") or {}).get("sha256", "")
    except ValueError:
        sha = ""
    if not sha or not backup.confirm(db, sha):
        return JSONResponse({"ok": False, "error": "sha256 does not match the last export"}, status_code=409)
    return {"ok": True}


# ------------------------------------------------------------------ Settings (login required)
@router.post("/settings/backup/token")
def new_token(request: Request, db: Session = Depends(get_db)):
    token = backup.regenerate_token(db)
    request.session["backup_token_once"] = token  # shown once on the Settings page, never stored in clear
    return RedirectResponse("/settings#backups", status_code=303)


@router.post("/settings/backup/revoke")
def revoke(request: Request, db: Session = Depends(get_db)):
    backup.revoke_token(db)
    request.session["flash"] = "Backup token revoked: scheduled backups will fail until you create a new one."
    return RedirectResponse("/settings#backups", status_code=303)


def settings_ctx(request: Request, db: Session) -> dict:
    from app.services import get_state
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return {"has_token": backup.has_token(db), "created": get_state(db, backup.TOKEN_CREATED_STATE),
            "last": backup.last(db), "url": base + "/api/backup/export",
            "token_once": request.session.pop("backup_token_once", None)}
