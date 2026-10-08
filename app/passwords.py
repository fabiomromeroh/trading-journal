"""Login password (argon2id hash in app_state), session versioning and emailed one-time codes.

* First run: the hash is seeded from APP_PASSWORD, so login keeps working unchanged.
* Break-glass: changing APP_PASSWORD on the server later re-seeds the password (and signs every
  session out). Leaving it unchanged keeps the password set in the app.
* Changing/resetting the password bumps auth:version; sessions carrying an older version are
  signed out by the RequireLogin middleware.
* One-time codes: 6 digits, 10 minutes, single use, 5 wrong attempts max, resend throttled
  (60 s apart, 5 per hour). Only an HMAC of the code is stored. Codes and passwords are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from app import mailer
from app.config import get_settings
from app.services import get_state, set_state

PASSWORD_KEY = "auth:password_hash"
SEED_KEY = "auth:seed_hash"
VERSION_KEY = "auth:version"
EMAIL_KEY = "auth:2fa_email"
CODE_KEY = "auth:code:{}"
DEFAULT_2FA_EMAIL = "fabioromero14@gmail.com"
CODE_TTL = 600
MAX_ATTEMPTS = 5
RESEND_GAP = 60
MAX_SENDS_PER_HOUR = 5
MIN_PASSWORD_LEN = 10
PURPOSES = {"change": "change your Trading Journal password", "reset": "reset your Trading Journal password"}

_ph = PasswordHasher()
_seeded: set = set()
_VER: dict = {}  # engine id -> (version, fetched_at)


def _verify(h: str | None, pw: str) -> bool:
    if not h or not pw:
        return False
    try:
        return _ph.verify(h, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _key(db: Session) -> str:
    return str(db.get_bind().url)


# ------------------------------------------------------------------ password
def ensure_password(db: Session) -> None:
    """Seed the stored hash from APP_PASSWORD on first run, or re-seed when APP_PASSWORD changed."""
    env = get_settings().app_password
    memo = (_key(db), hashlib.sha256(env.encode()).hexdigest())
    if memo in _seeded and get_state(db, PASSWORD_KEY) is not None:
        return
    if env and not _verify(get_state(db, SEED_KEY), env):
        had = get_state(db, PASSWORD_KEY) is not None
        set_state(db, PASSWORD_KEY, _ph.hash(env))
        set_state(db, SEED_KEY, _ph.hash(env))
        if had:
            _bump(db)
        db.commit()
    _seeded.add(memo)


def login_configured(db: Session) -> bool:
    ensure_password(db)
    return get_state(db, PASSWORD_KEY) is not None


def verify_password(db: Session, candidate: str) -> bool:
    ensure_password(db)
    h = get_state(db, PASSWORD_KEY)
    ok = _verify(h, candidate)
    if ok and _ph.check_needs_rehash(h):
        set_state(db, PASSWORD_KEY, _ph.hash(candidate))
        db.commit()
    return ok


def password_problem(new: str, confirm: str) -> str | None:
    if len(new) < MIN_PASSWORD_LEN:
        return f"The new password must be at least {MIN_PASSWORD_LEN} characters."
    if len(new) > 256:
        return "The new password is too long."
    if new != confirm:
        return "The new passwords don't match."
    return None


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def set_password_hash(db: Session, h: str) -> int:
    """Store a new password hash and sign out every existing session. Returns the new version."""
    set_state(db, PASSWORD_KEY, h)
    v = _bump(db)
    db.commit()
    return v


# ------------------------------------------------------------------ sessions
def current_version(db: Session, max_age: float = 5.0) -> int:
    k = _key(db)
    hit = _VER.get(k)
    if hit and time.time() - hit[1] < max_age:
        return hit[0]
    v = int(get_state(db, VERSION_KEY) or 0)
    _VER[k] = (v, time.time())
    return v


def _bump(db: Session) -> int:
    v = int(get_state(db, VERSION_KEY) or 0) + 1
    set_state(db, VERSION_KEY, str(v))
    _VER[_key(db)] = (v, time.time())
    return v


def session_valid(db: Session, session_version) -> bool:
    return int(session_version or 0) == current_version(db)


# ------------------------------------------------------------------ 2FA email
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def twofa_email(db: Session) -> str:
    return get_state(db, EMAIL_KEY) or DEFAULT_2FA_EMAIL


def set_twofa_email(db: Session, email: str) -> str | None:
    email = (email or "").strip()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        return "Enter a valid email address."
    set_state(db, EMAIL_KEY, email)
    db.commit()
    return None


# ------------------------------------------------------------------ one-time codes
def _code_hmac(purpose: str, code: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), f"{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def _load(db, purpose) -> dict:
    try:
        return json.loads(get_state(db, CODE_KEY.format(purpose)) or "{}")
    except ValueError:
        return {}


def _save(db, purpose, rec: dict) -> None:
    set_state(db, CODE_KEY.format(purpose), json.dumps(rec))
    db.commit()


def issue_code(db: Session, purpose: str, extra: dict | None = None, now: float | None = None) -> str | None:
    """Email a fresh code (replacing any previous one). Returns an error message or None on success."""
    now = time.time() if now is None else now
    rec = _load(db, purpose)
    sends = [t for t in rec.get("sends", []) if now - t < 3600]
    if sends and now - sends[-1] < RESEND_GAP:
        return f"A code was just sent. Wait {int(RESEND_GAP - (now - sends[-1])) + 1} s before requesting another."
    if len(sends) >= MAX_SENDS_PER_HOUR:
        return "Too many codes requested. Try again in an hour."
    st = mailer.status()
    if not st.configured:
        return "Email is not configured on the server, so a code can't be sent. " + st.note
    code = f"{secrets.randbelow(10**6):06d}"
    to = twofa_email(db)
    try:
        mailer.send(to, "Trading Journal: your security code",
                    f"Your code to {PURPOSES[purpose]} is:\n\n    {code}\n\n"
                    f"It expires in {CODE_TTL // 60} minutes and works once.\n"
                    "If you didn't ask for this, ignore this email; your password stays the same.")
    except mailer.MailError as exc:
        _save(db, purpose, {"sends": sends + [now]})  # failed sends still count towards the throttle
        return str(exc)
    _save(db, purpose, {"h": _code_hmac(purpose, code), "exp": now + CODE_TTL, "attempts": 0,
                        "sends": sends + [now], **(extra or {})})
    return None


def pending(db: Session, purpose: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    rec = _load(db, purpose)
    if rec.get("h") and rec.get("exp", 0) > now and rec.get("attempts", 0) < MAX_ATTEMPTS:
        return rec
    return None


def check_code(db: Session, purpose: str, code: str, now: float | None = None) -> tuple[dict | None, str | None]:
    """Consume a code. Returns (record, None) on success, else (None, error)."""
    now = time.time() if now is None else now
    rec = _load(db, purpose)
    if not rec.get("h"):
        return None, "No active code. Request a new one."
    if rec.get("exp", 0) <= now:
        rec.pop("h", None)
        _save(db, purpose, rec)
        return None, "That code has expired. Request a new one."
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        return None, "Too many wrong attempts. Request a new code."
    code = re.sub(r"\D", "", code or "")
    if not hmac.compare_digest(rec["h"], _code_hmac(purpose, code)):
        rec["attempts"] = rec.get("attempts", 0) + 1
        left = MAX_ATTEMPTS - rec["attempts"]
        if left <= 0:
            rec.pop("h", None)
        _save(db, purpose, rec)
        return None, (f"Incorrect code. {left} attempt{'s' if left != 1 else ''} left." if left > 0
                      else "Too many wrong attempts. Request a new code.")
    result = dict(rec)
    _save(db, purpose, {"sends": rec.get("sends", [])})  # single use
    return result, None


def cancel(db: Session, purpose: str) -> None:
    rec = _load(db, purpose)
    _save(db, purpose, {"sends": rec.get("sends", [])})
