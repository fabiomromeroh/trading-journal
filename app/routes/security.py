"""Settings > Security: change the login password (confirmed with an emailed code) and the 2FA email."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import mailer, passwords
from app.db import get_db
from app.routes.auth import client_ip
from app.security import throttle
from app.web import base_context, templates

router = APIRouter()


def security_ctx(db: Session) -> dict:
    return {"email": passwords.twofa_email(db), "email_masked": mailer.mask(passwords.twofa_email(db)),
            "mail": mailer.status(), "min_len": passwords.MIN_PASSWORD_LEN,
            "default_email": passwords.DEFAULT_2FA_EMAIL}


def _back(request: Request, msg: str) -> RedirectResponse:
    request.session["flash"] = msg
    return RedirectResponse("/settings#security", status_code=303)


def _check_current(request: Request, db: Session, current: str) -> str | None:
    key = "pw:" + client_ip(request)
    if throttle.blocked(key):
        return "Too many wrong passwords. Try again in 15 minutes."
    if not passwords.verify_password(db, current):
        throttle.fail(key)
        return "Current password is incorrect."
    throttle.reset(key)
    return None


@router.post("/settings/security/password")
def change_password_start(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                          confirm_password: str = Form(""), db: Session = Depends(get_db)):
    err = _check_current(request, db, current_password) or passwords.password_problem(new_password, confirm_password)
    if not err and new_password == current_password:
        err = "The new password is the same as the current one."
    if err:
        return _back(request, err)
    sid = secrets.token_urlsafe(16)
    err = passwords.issue_code(db, "change", {"new": passwords.hash_password(new_password), "sid": sid})
    if err:
        return _back(request, err)
    request.session["pw_change_sid"] = sid
    request.session["flash"] = f"Code sent to {mailer.mask(passwords.twofa_email(db))}."
    return RedirectResponse("/settings/security/verify", status_code=303)


def _verify_page(request, db, error=None, status=200):
    rec = passwords.pending(db, "change")
    ok = bool(rec and rec.get("sid") == request.session.get("pw_change_sid"))
    return templates.TemplateResponse(request, "security_verify.html", base_context(
        request, db, nav="settings", error=error, has_pending=ok, flash=request.session.pop("flash", None),
        **security_ctx(db)), status_code=status)


@router.get("/settings/security/verify")
def change_password_verify_page(request: Request, db: Session = Depends(get_db)):
    return _verify_page(request, db)


@router.post("/settings/security/verify")
def change_password_verify(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    rec = passwords.pending(db, "change")
    if not rec or rec.get("sid") != request.session.get("pw_change_sid"):
        return _verify_page(request, db, "No pending password change from this browser. Start again.", 400)
    rec, err = passwords.check_code(db, "change", code)
    if err:
        return _verify_page(request, db, err, 400)
    v = passwords.set_password_hash(db, rec["new"])
    request.session.pop("pw_change_sid", None)
    request.session["av"] = v  # keep this session; every other one is signed out
    return _back(request, "Password changed. Other signed-in sessions were signed out.")


@router.post("/settings/security/resend")
def change_password_resend(request: Request, db: Session = Depends(get_db)):
    rec = passwords.pending(db, "change")
    if not rec or rec.get("sid") != request.session.get("pw_change_sid"):
        return _verify_page(request, db, "No pending password change from this browser. Start again.", 400)
    err = passwords.issue_code(db, "change", {"new": rec["new"], "sid": rec["sid"]})
    if err:
        return _verify_page(request, db, err, 400)
    request.session["flash"] = f"New code sent to {mailer.mask(passwords.twofa_email(db))}."
    return RedirectResponse("/settings/security/verify", status_code=303)


@router.post("/settings/security/cancel")
def change_password_cancel(request: Request, db: Session = Depends(get_db)):
    rec = passwords.pending(db, "change")
    if rec and rec.get("sid") == request.session.get("pw_change_sid"):
        passwords.cancel(db, "change")
    request.session.pop("pw_change_sid", None)
    return _back(request, "Password change cancelled.")


@router.post("/settings/security/email")
def change_email(request: Request, current_password: str = Form(""), email: str = Form(""),
                 db: Session = Depends(get_db)):
    err = _check_current(request, db, current_password) or passwords.set_twofa_email(db, email)
    return _back(request, err or f"Security email set to {email.strip()}.")


@router.post("/settings/security/test-email")
def test_email(request: Request, db: Session = Depends(get_db)):
    last = request.session.get("test_email_at", 0)
    if time.time() - last < 60:
        return _back(request, "A test email was just sent. Wait a minute.")
    request.session["test_email_at"] = time.time()
    to = passwords.twofa_email(db)
    try:
        mailer.send(to, "Trading Journal: test email",
                    "Email delivery works. Password codes will arrive at this address.")
    except mailer.MailError as exc:
        return _back(request, f"Test email failed: {exc}")
    return _back(request, f"Test email sent to {mailer.mask(to)}.")
