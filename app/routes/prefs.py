"""Header preferences: which account the whole app shows (All / one) and the display currency (USD / EUR)."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import currency
from app.db import get_db
from app.models import Account
from app.web import ACCOUNT_COOKIE

router = APIRouter()


def safe_next(nxt: str) -> str:
    """Same-site path only; the ?account= override is dropped so the new choice takes effect."""
    p = urlsplit(nxt or "/")
    if p.scheme or p.netloc or not (p.path or "/").startswith("/") or (p.path or "").startswith("//"):
        return "/"
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k not in ("account", "page", "realized_day")]
    return (p.path or "/") + (("?" + urlencode(q)) if q else "")


@router.post("/prefs")
def set_prefs(request: Request, db: Session = Depends(get_db), nxt: str = Form("/", alias="next"),
              currency_choice: str | None = Form(None, alias="currency"), account: str | None = Form(None)):
    resp = RedirectResponse(safe_next(nxt), status_code=303)
    if currency_choice:
        if currency_choice.upper() in currency.CHOICES:
            currency.set_display(db, currency_choice)
    if account is not None:
        if account.isdigit() and db.get(Account, int(account)):
            resp.set_cookie(ACCOUNT_COOKIE, account, max_age=60 * 60 * 24 * 365, samesite="lax", httponly=True)
        else:
            resp.delete_cookie(ACCOUNT_COOKIE)
    return resp
