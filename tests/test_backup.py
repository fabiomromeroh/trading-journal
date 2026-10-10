"""Scheduled backups: token auth, rate limit, no secrets in the export, keep-awake exclusion, restore round trip."""
import gzip
import json
import os
import subprocess
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from app import backup, keepawake
from app.models import Account, AppState, Execution, OAuthToken, PriceCache, Trade, utcnow
from app.services import set_state
from tests.test_be_drilldown_layout import four, web  # noqa: F401  (fixtures)

SECRET_ROWS = {"auth:password_hash": "argon2-hash-should-not-leave", "auth:code:change": "123456"}


@pytest.fixture()
def anon(db):
    from app.main import create_app
    from app.routes import backup as routes
    routes._throttle.failures.clear()
    backup.reset_rate_limit()
    return TestClient(create_app())


@pytest.fixture()
def seeded(four, db):
    for k, v in SECRET_ROWS.items():
        set_state(db, k, v)
    set_state(db, "layout:dashboard", '["kpi"]')
    db.add(OAuthToken(provider="schwab", access_token_enc="SECRET-ACCESS", refresh_token_enc="SECRET-REFRESH",
                      access_expires_at=utcnow(), refresh_expires_at=utcnow()))
    db.add(PriceCache(cache_key="k", provider="yahoo", payload="[]"))
    db.commit()
    return four


def test_token_auth_required(db, web, anon, seeded):  # noqa: F811
    assert anon.get("/api/backup/export").status_code == 401                       # no token exists yet
    assert web.post("/settings/backup/token", follow_redirects=False).status_code == 303
    page = web.get("/settings").text
    token = __import__("re").search(r"tjb_[A-Za-z0-9_-]+", page).group(0)
    assert token.startswith("tjb_")
    assert "backup-token-once" not in web.get("/settings").text                       # shown once only
    stored = db.get(AppState, backup.TOKEN_HASH_STATE).value
    assert stored != token and token not in stored and len(stored) == 64               # hash only
    assert anon.get("/api/backup/export").status_code == 401
    assert anon.get("/api/backup/export", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anon.post("/api/backup/confirm", json={"sha256": "x"}).status_code == 401
    # the login-protected session cookie is not accepted as a substitute, and the token is useless elsewhere
    assert anon.get("/trades", headers={"Authorization": f"Bearer {token}"}, follow_redirects=False).status_code == 303
    assert anon.post("/settings/backup/token", headers={"Authorization": f"Bearer {token}"},
                     follow_redirects=False).status_code == 303                       # redirected to login, nothing changed
    r = anon.get("/api/backup/export", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.headers["x-backup-trades"] == "4"
    # regenerate: old token dies
    backup.reset_rate_limit()
    assert web.post("/settings/backup/token", follow_redirects=False).status_code == 303
    assert anon.get("/api/backup/export", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_failed_attempts_are_throttled(db, anon):
    for _ in range(5):
        assert anon.get("/api/backup/export", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert anon.get("/api/backup/export", headers={"Authorization": "Bearer bad"}).status_code == 429


def test_export_content_has_no_secrets_and_rate_limit(db, anon, seeded):
    token = backup.regenerate_token(db)
    h = {"Authorization": f"Bearer {token}"}
    r = anon.get("/api/backup/export", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert r.headers["cache-control"] == "no-store"
    text = gzip.decompress(r.content).decode()
    for secret in ("SECRET-ACCESS", "SECRET-REFRESH", "argon2-hash-should-not-leave", "123456", token, backup.TOKEN_HASH_STATE):
        assert secret not in text
    data = json.loads(text)
    assert data["format"] == 1 and len(data["tables"]["trades"]) == 4 and data["tables"]["accounts"]
    assert data["tables"]["oauth_tokens"] == {"omitted": "credentials", "rows": 1}
    assert data["tables"]["price_cache"]["omitted"] == "cache"
    keys = {x["key"] for x in data["tables"]["app_state"]}
    assert "layout:dashboard" in keys and not any(k.startswith(("auth:", "backup:")) for k in keys)
    # second export right away: rate limited with Retry-After
    r2 = anon.get("/api/backup/export", headers=h)
    assert r2.status_code == 429 and int(r2.headers["retry-after"]) > 0
    assert backup.rate_limited(now=1e12) == 0


def test_confirm_records_result_and_settings_shows_it(db, web, anon, seeded):  # noqa: F811
    token = backup.regenerate_token(db)
    h = {"Authorization": f"Bearer {token}"}
    assert "No scheduled backup has run yet" in web.get("/settings").text
    r = anon.get("/api/backup/export", headers=h)
    page = web.get("/settings").text
    assert "not confirmed as stored" in page and "4 trades" in page
    assert anon.post("/api/backup/confirm", headers=h, json={"sha256": "0" * 64}).status_code == 409
    sha = r.headers["x-backup-sha256"]
    assert sha == __import__("hashlib").sha256(r.content).hexdigest()
    assert anon.post("/api/backup/confirm", headers=h, json={"sha256": sha}).json() == {"ok": True}
    page = web.get("/settings").text
    assert "stored " in page and "not confirmed" not in page
    info = backup.last(db)
    assert info["result"] == "stored" and info["size"] == len(r.content) and info["counts"]["trades"] == 4


def test_backup_calls_never_extend_keep_awake(db, anon, web, seeded):  # noqa: F811
    assert not keepawake.counts_as_visit("GET", "/api/backup/export", True)
    assert not keepawake.counts_as_visit("POST", "/api/backup/confirm", True)
    keepawake.state.keep_until = 0.0
    token = backup.regenerate_token(db)
    anon.get("/api/backup/export", headers={"Authorization": f"Bearer {token}"})
    assert keepawake.state.keep_until == 0.0 and not keepawake.state.active()


def test_manual_download_still_works_without_secrets(db, web, seeded):  # noqa: F811
    r = web.get("/settings/backup.json")
    assert r.status_code == 200 and "argon2-hash" not in r.text and "SECRET-ACCESS" not in r.text
    assert len(r.json()["tables"]["trades"]) == 4 and r.json()["tables"]["price_cache"]  # manual copy keeps the cache


# ------------------------------------------------------------------ restore
def _counts(db):
    from app.db import Base
    return {t.name: db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def test_restore_round_trip_sqlite(db, seeded, tmp_path):
    from app.db import Base
    from scripts import restore_backup as rb
    before = _counts(db)
    data = backup.build(db, include_cache=False)
    f = tmp_path / "b.json.gz"
    f.write_bytes(backup.encode(data))
    pnl_before = sorted((t.symbol, t.net_pnl, t.opened_at, t.closed_at) for t in db.scalars(select(Trade)))
    fills_before = sorted((e.symbol, e.price, e.executed_at, e.side) for e in db.scalars(select(Execution)))

    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(fresh)
    loaded = rb.load_file(str(f), None)
    done = rb.restore(loaded, fresh)
    assert done["trades"] == 4 and done["executions"] == before["executions"] and done["oauth_tokens"] == 0
    from sqlalchemy.orm import Session
    with Session(fresh) as s2:
        assert sorted((t.symbol, t.net_pnl, t.opened_at, t.closed_at) for t in s2.scalars(select(Trade))) == pnl_before
        assert sorted((e.symbol, e.price, e.executed_at, e.side) for e in s2.scalars(select(Execution))) == fills_before
        assert s2.get(AppState, "layout:dashboard").value == '["kpi"]'
        assert s2.get(AppState, "auth:password_hash") is None
        assert s2.scalar(select(func.count()).select_from(Account)) == before["accounts"]
    # refuses a non-empty database unless --force; dry run changes nothing
    with pytest.raises(SystemExit, match="not empty"):
        rb.restore(loaded, fresh)
    rb.restore(loaded, fresh, force=True)
    with Session(fresh) as s2:
        assert s2.scalar(select(func.count()).select_from(Trade)) == 4
    empty = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(empty)
    rb.restore(loaded, empty, dry_run=True)
    with Session(empty) as s3:
        assert s3.scalar(select(func.count()).select_from(Trade)) == 0


def test_restore_reads_encrypted_files(db, seeded, tmp_path):
    from scripts import restore_backup as rb
    gz = tmp_path / "b.json.gz"
    gz.write_bytes(backup.encode(backup.build(db, include_cache=False)))
    pw = backup.passphrase_from_key("some-key")
    enc = tmp_path / "b.json.gz.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--symmetric",
                    "--cipher-algo", "AES256", "-o", str(enc), str(gz)], input=(pw + "\n").encode(), check=True,
                   capture_output=True)
    assert enc.read_bytes()[:2] != b"\x1f\x8b" and b"trades" not in enc.read_bytes()
    assert len(rb.load_file(str(enc), pw)["tables"]["trades"]) == 4
    with pytest.raises(SystemExit, match="could not decrypt"):
        rb.load_file(str(enc), "wrong")
    with pytest.raises(SystemExit, match="encrypted"):
        rb.load_file(str(enc), None)
