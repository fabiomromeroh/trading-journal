"""thinkorswim fill-email ingest API (token-authenticated) and its Settings actions."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import LoginThrottle

router = APIRouter()
log = logging.getLogger(__name__)
_throttle = LoginThrottle(limit=20, window=900)
LOCK_TIMEOUT = 20  # seconds to wait for a running "Sync now" before asking the script to retry


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _provided_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-ingest-token")


def endpoint_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base + "/api/ingest/tos-email"


@router.post("/api/ingest/tos-email")
async def ingest_tos_email(request: Request, db: Session = Depends(get_db)):
    from app import email_sync
    ip = _client_ip(request)
    if _throttle.blocked(ip):
        return JSONResponse({"ok": False, "error": "too many failed attempts"}, status_code=429)
    if not email_sync.verify_token(db, _provided_token(request)):
        _throttle.fail(ip)
        return JSONResponse({"ok": False, "error": "invalid or missing ingest token"}, status_code=401)
    raw = await request.body()
    if len(raw) > email_sync.MAX_BODY + 20_000:
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": f"bad JSON: {exc}"}, status_code=400)
    return await run_in_threadpool(_ingest_locked, db, payload)


def _ingest_locked(db: Session, payload: dict) -> JSONResponse:
    """Runs in a worker thread: waits for a running sync, then stores the email."""
    from app import email_sync
    from app.sync import _lock
    if not _lock.acquire(timeout=LOCK_TIMEOUT):
        return JSONResponse({"ok": False, "error": "sync in progress, retry"}, status_code=503)
    try:
        return JSONResponse(email_sync.ingest_email(db, payload))
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:  # pragma: no cover
        db.rollback()
        log.exception("tos email ingest failed")
        return JSONResponse({"ok": False, "error": "internal error, retry later"}, status_code=500)
    finally:
        _lock.release()


@router.post("/settings/tos-email/token")
def regenerate(request: Request, db: Session = Depends(get_db)):
    from app import email_sync
    had = email_sync.get_token(db) is not None
    email_sync.regenerate_token(db)
    request.session["flash"] = ("New ingest token created. Paste the updated script into your Apps Script "
                                "project (the old token no longer works)." if had else
                                "Ingest token created. Copy the script below into Google Apps Script.")
    return RedirectResponse("/settings#tos-email", status_code=303)


@router.post("/settings/tos-email/{email_id}/discard")
def discard(email_id: int, request: Request, db: Session = Depends(get_db)):
    from app import email_sync
    from app.sync import _lock
    if not _lock.acquire(timeout=LOCK_TIMEOUT):
        request.session["flash"] = "A sync is running; try again in a moment."
        return RedirectResponse("/settings#tos-email", status_code=303)
    try:
        n = email_sync.discard_email(db, email_id)
    finally:
        _lock.release()
    request.session["flash"] = f"Email discarded; removed {n} fill{'s' if n != 1 else ''} it had added."
    return RedirectResponse("/settings#tos-email", status_code=303)


def settings_ctx(request: Request, db: Session) -> dict:
    from app import email_sync
    token = email_sync.get_token(db)
    url = endpoint_url(request)
    return {"url": url, "has_token": token is not None,
            "token_hint": f"…{token[-4:]}" if token else None,
            "script": email_sync.apps_script(url, token) if token else None,
            "status": email_sync.status(db), "sender": "alerts@thinkorswim.com"}
