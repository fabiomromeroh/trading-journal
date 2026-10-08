from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import mailer, passwords
from app.db import get_db
from app.security import throttle
from app.web import templates

router = APIRouter()


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


def client_ip(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else ip


@router.get("/login")
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": None, "notice": request.session.pop("login_notice", None),
        "configured": passwords.login_configured(db)})


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/"), db: Session = Depends(get_db)):
    ip = client_ip(request)
    configured = passwords.login_configured(db)
    if throttle.blocked(ip):
        error = "Too many attempts. Try again in 15 minutes."
    elif configured and passwords.verify_password(db, password):
        throttle.reset(ip)
        pending = request.session.pop("pending_callback", None)
        request.session.clear()
        request.session["auth"] = True
        request.session["av"] = passwords.current_version(db, max_age=0)
        return RedirectResponse(pending or _safe_next(next), status_code=303)
    else:
        throttle.fail(ip)
        error = "Incorrect password." if configured else "APP_PASSWORD is not set on the server."
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": error, "notice": None, "configured": configured}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- forgot password (emailed code)
def _reset_ctx(db, **kw):
    return {"email_masked": mailer.mask(passwords.twofa_email(db)), "mail": mailer.status(),
            "min_len": passwords.MIN_PASSWORD_LEN, "error": None, "notice": None,
            "has_pending": passwords.pending(db, "reset") is not None, **kw}


@router.get("/login/forgot")
def forgot_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth_forgot.html", _reset_ctx(db))


@router.post("/login/forgot")
def forgot_send(request: Request, db: Session = Depends(get_db)):
    if throttle.blocked("reset:" + client_ip(request)):
        return templates.TemplateResponse(request, "auth_forgot.html", _reset_ctx(
            db, error="Too many attempts. Try again in 15 minutes."), status_code=429)
    err = passwords.issue_code(db, "reset")
    if err:
        return templates.TemplateResponse(request, "auth_forgot.html", _reset_ctx(db, error=err), status_code=400)
    request.session["reset_notice"] = "Code sent. Check your email (and the spam folder)."
    return RedirectResponse("/login/reset", status_code=303)


@router.get("/login/reset")
def reset_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth_reset.html", _reset_ctx(
        db, notice=request.session.pop("reset_notice", None)))


@router.post("/login/reset")
def reset_submit(request: Request, code: str = Form(""), new_password: str = Form(""),
                 confirm_password: str = Form(""), db: Session = Depends(get_db)):
    ip = "reset:" + client_ip(request)
    if throttle.blocked(ip):
        err = "Too many attempts. Try again in 15 minutes."
    else:
        err = passwords.password_problem(new_password, confirm_password)
        if not err:
            rec, err = passwords.check_code(db, "reset", code)
            if rec:
                throttle.reset(ip)
                passwords.set_password_hash(db, passwords.hash_password(new_password))
                request.session.clear()
                request.session["login_notice"] = "Password changed. Log in with your new password; other sessions were signed out."
                return RedirectResponse("/login", status_code=303)
            throttle.fail(ip)
    return templates.TemplateResponse(request, "auth_reset.html", _reset_ctx(db, error=err), status_code=400)
