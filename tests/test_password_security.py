"""Password stored as an argon2 hash, emailed one-time codes, session invalidation."""
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app import mailer, passwords
from app.config import get_settings
from app.services import get_state

SENT: list = []


@pytest.fixture()
def mail(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(mailer, "status", lambda: mailer.MailStatus(True, "resend", "x@resend.dev", "test"))
    monkeypatch.setattr(mailer, "send", lambda to, subject, text: SENT.append((to, subject, text)))
    return SENT


@pytest.fixture(autouse=True)
def _clean():
    from app.security import throttle
    throttle.failures.clear()
    passwords._VER.clear()
    yield


def last_code():
    return re.search(r"\b(\d{6})\b", SENT[-1][2]).group(1)


def client(db, pw="test-pass"):
    from app.main import create_app
    c = TestClient(create_app())
    r = c.post("/login", data={"password": pw, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return c


def test_seeded_from_app_password_and_hashed(db):
    c = client(db)
    h = get_state(db, passwords.PASSWORD_KEY)
    assert h.startswith("$argon2id$") and "test-pass" not in h
    assert c.get("/", follow_redirects=False).status_code == 200


def test_app_password_change_on_server_reseeds(db, monkeypatch):
    passwords.ensure_password(db)
    monkeypatch.setattr(get_settings(), "app_password", "brand-new-env-pass")
    assert passwords.verify_password(db, "brand-new-env-pass")
    assert not passwords.verify_password(db, "test-pass")
    assert get_state(db, passwords.VERSION_KEY) == "1"  # existing sessions signed out


def test_code_expiry(db, mail):
    assert passwords.issue_code(db, "reset", now=1000) is None
    code = last_code()
    rec, err = passwords.check_code(db, "reset", code, now=1000 + passwords.CODE_TTL + 1)
    assert rec is None and "expired" in err
    assert passwords.check_code(db, "reset", code, now=1001)[0] is None  # gone for good


def test_code_attempt_limit(db, mail):
    passwords.issue_code(db, "reset", now=1000)
    code = last_code()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for i in range(passwords.MAX_ATTEMPTS):
        rec, err = passwords.check_code(db, "reset", wrong, now=1001)
        assert rec is None
    assert "Too many" in err
    rec, err = passwords.check_code(db, "reset", code, now=1002)
    assert rec is None  # even the right code is dead now


def test_code_single_use_and_purpose_bound(db, mail):
    passwords.issue_code(db, "change", {"new": "h"}, now=1000)
    code = last_code()
    assert passwords.check_code(db, "reset", code, now=1001)[0] is None  # other purpose
    rec, err = passwords.check_code(db, "change", code, now=1001)
    assert err is None and rec["new"] == "h"
    rec, err = passwords.check_code(db, "change", code, now=1002)
    assert rec is None and "No active code" in err


def test_resend_rate_limit(db, mail):
    assert passwords.issue_code(db, "reset", now=1000) is None
    assert "Wait" in passwords.issue_code(db, "reset", now=1030)
    for i in range(1, passwords.MAX_SENDS_PER_HOUR):
        assert passwords.issue_code(db, "reset", now=1000 + 61 * i) is None
    assert "Too many codes" in passwords.issue_code(db, "reset", now=1000 + 61 * 10)
    assert passwords.issue_code(db, "reset", now=1000 + 3700) is None


def test_email_not_configured(db, monkeypatch):
    monkeypatch.setattr(mailer, "status", lambda: mailer.MailStatus(False, None, None, "Email is not configured on the server."))
    err = passwords.issue_code(db, "reset")
    assert "not configured" in err and passwords.pending(db, "reset") is None
    c = client(db)
    assert "email not configured" in c.get("/settings").text
    assert "isn't configured" in c.get("/login/forgot").text


def test_change_password_signs_out_other_sessions(db, mail, caplog):
    caplog.set_level(logging.DEBUG)
    a, b = client(db), client(db)
    r = a.post("/settings/security/password", data={"current_password": "wrong", "new_password": "new-password-1",
                                                    "confirm_password": "new-password-1"}, follow_redirects=False)
    assert r.headers["location"] == "/settings#security" and not SENT
    r = a.post("/settings/security/password", data={"current_password": "test-pass", "new_password": "new-password-1",
                                                    "confirm_password": "new-password-1"}, follow_redirects=False)
    assert r.headers["location"] == "/settings/security/verify" and SENT[-1][0] == passwords.DEFAULT_2FA_EMAIL
    code = last_code()
    # the code only works from the browser that started the change
    assert b.post("/settings/security/verify", data={"code": code}).status_code == 400
    r = a.post("/settings/security/verify", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#security"
    assert a.get("/", follow_redirects=False).status_code == 200          # this session stays
    r = b.get("/", follow_redirects=False)                                 # the other one is out
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass"}).status_code == 401
    assert c.post("/login", data={"password": "new-password-1"}, follow_redirects=False).status_code == 303
    logs = caplog.text
    assert code not in logs and "new-password-1" not in logs and "test-pass" not in logs


def test_change_password_validation(db, mail):
    a = client(db)
    for new, conf in (("short", "short"), ("long-enough-1", "long-enough-2")):
        a.post("/settings/security/password", data={"current_password": "test-pass", "new_password": new,
                                                    "confirm_password": conf})
    assert not SENT and passwords.verify_password(db, "test-pass")


def test_forgot_password_flow(db, mail):
    a = client(db)
    from app.main import create_app
    c = TestClient(create_app())
    assert "Forgot password?" in c.get("/login").text
    r = c.post("/login/forgot", follow_redirects=False)
    assert r.headers["location"] == "/login/reset"
    code = last_code()
    bad = c.post("/login/reset", data={"code": "000000" if code != "000000" else "111111",
                                       "new_password": "reset-password-1", "confirm_password": "reset-password-1"})
    assert bad.status_code == 400 and "Incorrect code" in bad.text
    r = c.post("/login/reset", data={"code": code, "new_password": "reset-password-1",
                                     "confirm_password": "reset-password-1"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert "Password changed" in c.get("/login").text
    assert a.get("/", follow_redirects=False).status_code == 303  # old session signed out
    assert c.post("/login", data={"password": "reset-password-1"}, follow_redirects=False).status_code == 303


def test_security_email_change_needs_password(db, mail):
    a = client(db)
    a.post("/settings/security/email", data={"email": "x@example.com", "current_password": "nope"})
    assert passwords.twofa_email(db) == passwords.DEFAULT_2FA_EMAIL
    a.post("/settings/security/email", data={"email": "x@example.com", "current_password": "test-pass"})
    db.expire_all()
    assert passwords.twofa_email(db) == "x@example.com"


def test_backup_excludes_auth_state(db):
    a = client(db)
    body = a.get("/settings/backup.json").text
    assert "argon2" not in body and "auth:" not in body


def test_mailer_providers(monkeypatch):
    s = get_settings()
    for k in ("resend_api_key", "brevo_api_key", "email_from", "smtp_host"):
        monkeypatch.setattr(s, k, "")
    assert not mailer.status().configured
    calls = []

    class R:
        status_code = 200

    monkeypatch.setattr(mailer.httpx, "post", lambda url, **kw: calls.append((url, kw)) or R())
    monkeypatch.setattr(s, "resend_api_key", "re_test")
    mailer.send("me@example.com", "Subj: x", "body 123456")
    url, kw = calls[-1]
    assert url == "https://api.resend.com/emails" and kw["json"]["from"].endswith("<onboarding@resend.dev>")
    assert kw["json"]["to"] == ["me@example.com"]
    monkeypatch.setattr(s, "resend_api_key", "")
    monkeypatch.setattr(s, "brevo_api_key", "xkeysib")
    assert not mailer.status().configured  # needs a verified sender
    monkeypatch.setattr(s, "email_from", "Journal <me@example.com>")
    mailer.send("me@example.com", "Subj", "body")
    url, kw = calls[-1]
    assert "brevo" in url and kw["json"]["sender"] == {"name": "Journal", "email": "me@example.com"}
    R.status_code = 403
    with pytest.raises(mailer.MailError):
        mailer.send("me@example.com", "Subj", "body")
    assert mailer.mask("fabioromero14@gmail.com").startswith("fa") and mailer.mask("fabioromero14@gmail.com").endswith("@gmail.com")
