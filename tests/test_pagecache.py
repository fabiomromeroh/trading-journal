"""Rendered-page cache: hit on repeat, dropped on any data write, never with one-time session messages."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import pagecache
from app.models import Account


def _client():
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    return c


def test_hit_then_invalidated_by_write(db):
    pagecache.bump()
    c = _client()
    c.get("/trades")  # first render may store one-off state (that write drops the entry)
    a = c.get("/trades")
    b = c.get("/trades")
    assert b.headers.get("x-page-cache") == "hit" and b.text == a.text
    db.add(Account(name="Zed Broker", broker="tos", currency="USD", source_key="tos"))
    db.commit()  # any committed write drops every cached page
    d = c.get("/trades")
    assert d.headers.get("x-page-cache") == "miss"
    # same page with different filters / account is a different entry
    assert c.get("/trades?symbol=AAA").headers.get("x-page-cache") == "miss"


def test_live_and_posts_never_cached(db):
    c = _client()
    assert "x-page-cache" not in c.get("/dashboard/live").headers
    assert pagecache.ttl_for("/dashboard/live") is None and pagecache.ttl_for("/trades/12") == 120


def test_flash_bypasses_cache(db):
    pagecache.bump()
    c = _client()
    c.get("/settings")
    c.get("/settings")
    r = c.post("/settings/backup/token", follow_redirects=False)
    assert r.status_code in (200, 303)
    page = c.get("/settings")
    assert "x-page-cache" not in page.headers or page.headers["x-page-cache"] == "miss"
    assert "backup-token-once" in page.text


def test_disabled_by_env(db, monkeypatch):
    monkeypatch.setenv("PAGE_CACHE", "off")
    c = _client()
    c.get("/trades")
    assert "x-page-cache" not in c.get("/trades").headers
