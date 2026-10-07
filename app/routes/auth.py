from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.security import check_password, throttle
from app.web import templates

router = APIRouter()


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": None, "configured": bool(get_settings().app_password)})


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        ip = fwd.split(",")[0].strip()
    configured = bool(get_settings().app_password)
    if throttle.blocked(ip):
        error = "Too many attempts. Try again in 15 minutes."
    elif check_password(password):
        throttle.reset(ip)
        pending = request.session.pop("pending_callback", None)
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse(pending or _safe_next(next), status_code=303)
    else:
        throttle.fail(ip)
        error = "Incorrect password." if configured else "APP_PASSWORD is not set on the server."
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "error": error, "configured": configured}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
