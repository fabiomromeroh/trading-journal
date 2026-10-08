"""End-to-end smoke tests of every page with the sample data and a CSV import."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import SyncRun, Trade


@pytest.fixture()
def client(db):
    from app.main import create_app
    c = TestClient(create_app())
    return c


def login(c):
    r = c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/healthz").json() == {"ok": True}
    assert client.post("/login", data={"password": "wrong"}).status_code == 401


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"password": "test-pass", "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_empty_state_pages(client):
    login(client)
    for path in ("/", "/trades", "/import", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "No trades yet" in client.get("/").text


def test_demo_data_all_pages(client, db):
    login(client)
    r = client.post("/settings/demo/load", follow_redirects=True)
    assert r.status_code == 200 and "Sample data" in r.text
    assert "Equity curve" in r.text
    for path in ("/?preset=30d", "/trades", "/trades?sort=pnl&dir=asc&outcome=win", "/trades?asset=OPTION",
                 "/settings", "/import"):
        assert client.get(path).status_code == 200, path
    t = db.scalar(select(Trade).where(Trade.status == "CLOSED", Trade.asset_type == "STOCK"))
    r = client.get(f"/trades/{t.id}")
    assert r.status_code == 200 and "Executions" in r.text
    chart = client.get(f"/trades/{t.id}/chart.json").json()
    assert chart["provider"] == "demo" and chart["candles"] and chart["markers"]
    r = client.post(f"/trades/{t.id}/journal", data={"notes": "great trade", "setup": "Breakout",
                                                    "rating": "4", "tags": "A+ setup, new-tag"})
    assert r.status_code == 200 and "Saved" in r.text
    db.expire_all()
    t2 = db.get(Trade, t.id)
    assert t2.notes == "great trade" and t2.rating == 4 and {x.name for x in t2.tags} == {"A+ setup", "new-tag"}
    # rebuild keeps journal
    client.post("/settings/rebuild")
    db.expire_all()
    assert db.get(Trade, t.id).notes == "great trade"
    # clear sample data
    client.post("/settings/demo/clear")
    db.expire_all()
    assert db.scalar(select(Trade)) is None
    from app.models import Tag
    assert db.scalar(select(Tag)) is None  # sample-only tags removed too


def test_csv_upload_preview_commit_undo(client, db, fixture_text):
    login(client)
    data = fixture_text("schwab_transactions.csv").encode()
    r = client.post("/import/upload", files={"file": ("history.csv", data, "text/csv")}, follow_redirects=True)
    assert r.status_code == 200 and "Preview" in r.text and "Import 8 new" in r.text
    batch_id = int(str(r.url).split("/")[-2])
    r = client.post(f"/import/{batch_id}/commit", follow_redirects=True)
    assert "Import complete" in r.text
    assert db.query(Trade).count() == 4
    # re-upload shows duplicates
    r = client.post("/import/upload", files={"file": ("history.csv", data, "text/csv")}, follow_redirects=True)
    assert "Import 0 new" in r.text
    client.post(f"/import/{int(str(r.url).split('/')[-2])}/discard")
    assert client.get("/").status_code == 200
    client.post(f"/import/{batch_id}/undo")
    db.expire_all()
    assert db.query(Trade).count() == 0


def test_bad_upload(client):
    login(client)
    r = client.post("/import/upload", files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert r.status_code == 400 and "Unrecognised file" in r.text


def test_sync_now_without_sources(client, db):
    login(client)
    r = client.post("/sync")
    assert r.status_code == 200
    for _ in range(50):
        db.expire_all()
        run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        if run.status != "running":
            break
        time.sleep(0.1)
    assert run.status == "skipped"
    assert client.get(f"/sync/{run.id}/status").status_code == 200


def test_cli_sync(db):
    from app.sync import main
    assert main(["--trigger", "cron", "--no-reminders"]) == 0
