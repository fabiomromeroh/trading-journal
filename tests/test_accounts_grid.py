"""Accounts grid: one list, one selected account, add / edit / delete modals.

Interactive Brokers is mock-only. Opening or selecting an account must not call Flex.
"""
from datetime import datetime

from fastapi.testclient import TestClient

from app.models import (
    Account, CorporateAdjustment, Execution, SyncPayload, Trade,
)


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def _anon():
    from app.main import create_app
    return TestClient(create_app())


def test_grid_lists_every_account_and_detail_is_one_account(db):
    rows = [
        Account(name="Schwab main", broker="schwab", currency="USD", account_number_masked="…1111"),
        Account(name="T212 book", broker="trading212", source_key="t212", currency="EUR",
                account_number_masked="…2222"),
        Account(name="IBKR margin", broker="ibkr", source_key="ibkr_flex", currency="USD",
                external_ref="U99999999", account_number_masked="U***9999"),
        Account(name="CSV book", broker="other", currency="GBP"),
    ]
    db.add_all(rows)
    db.commit()
    c = _client(db)
    page = c.get("/settings").text
    for name in ("Schwab main", "T212 book", "IBKR margin", "CSV book"):
        assert name in page
    assert "U99999999" not in page and "U***9999" in page
    assert 'id="account-detail"' in page
    # Nothing selected: the other accounts' sync panels are not in the detail.
    detail_empty = page.split('id="account-detail"', 1)[1]
    assert "Select an account" in detail_empty
    assert "hx-post=\"/settings/brokers/ibkr/sync\"" not in detail_empty
    by_name = {a.name: a for a in db.query(Account)}
    expect = {
        "Schwab main": "Schwab",
        "T212 book": "Trading 212",
        "IBKR margin": "Interactive Brokers",
        "CSV book": "Other",
    }
    for name, label in expect.items():
        acct = by_name[name]
        body = c.get(f"/settings/accounts/{acct.id}/detail").text
        assert acct.name in body and label in body
        assert f'data-account="{acct.id}"' in body
        for other in by_name.values():
            if other.id != acct.id:
                assert other.name not in body
        if name == "IBKR margin":
            assert "U99999999" not in body and "U***9999" in body
            assert 'hx-post="/sync"' not in body
    kept = c.get("/settings?account=%d" % by_name["T212 book"].id).text
    assert "T212 book" in kept and 'id="account-detail"' in kept
    assert f'data-account="{by_name["T212 book"].id}"' in kept.split('id="account-detail"', 1)[1]


def test_add_edit_delete_modals_render_and_delete_names_what_goes(db):
    keep = Account(name="Keep me", broker="schwab", currency="USD")
    gone = Account(name="Drop me", broker="other", currency="EUR")
    db.add_all([keep, gone])
    db.flush()
    when = datetime(2024, 6, 1, 15, 0)
    db.add(Execution(
        account_id=gone.id, source="csv", external_id="e1", match_key="e1", symbol="AAA",
        underlying="AAA", asset_type="STOCK", quantity=2, price=10, executed_at=when, kind="TRADE"))
    db.add(Execution(
        account_id=keep.id, source="csv", external_id="e2", match_key="e2", symbol="BBB",
        underlying="BBB", asset_type="STOCK", quantity=1, price=5, executed_at=when, kind="TRADE"))
    db.add(Trade(
        key="drop-1", account_id=gone.id, symbol="AAA", underlying="AAA", asset_type="STOCK",
        direction="LONG", status="OPEN", opened_at=when, quantity=2, entry_price=10))
    db.add(Trade(
        key="keep-1", account_id=keep.id, symbol="BBB", underlying="BBB", asset_type="STOCK",
        direction="LONG", status="OPEN", opened_at=when, quantity=1, entry_price=5))
    db.add(CorporateAdjustment(
        account_id=gone.id, symbol="AAA", kind="SPLIT", source="manual", external_key="ca-drop",
        status="applied", note="split"))
    db.add(SyncPayload(
        account_id=gone.id, source="csv", filename="drop.csv", content_type="text/csv",
        nbytes=1, body=b"x"))
    db.add(SyncPayload(
        account_id=keep.id, source="csv", filename="keep.csv", content_type="text/csv",
        nbytes=1, body=b"y"))
    db.commit()
    gone_id, keep_id = gone.id, keep.id
    c = _client(db)
    add = c.get("/settings/accounts/new")
    assert add.status_code == 200
    assert "Add account" in add.text and 'action="/settings/accounts"' in add.text
    assert 'action="/settings/brokers/ibkr/connect"' in add.text
    assert 'action="/settings/brokers/t212/connect"' in add.text
    edit = c.get(f"/settings/accounts/{gone_id}/edit")
    assert edit.status_code == 200 and "Edit account" in edit.text
    assert f'action="/settings/accounts/{gone_id}"' in edit.text
    assert f'action="/settings/accounts/{gone_id}/meta"' in edit.text
    modal = c.get(f"/settings/accounts/{gone_id}/delete")
    assert modal.status_code == 200
    text = modal.text
    assert "This removes the account" in text and "Drop me" in text
    assert "1 execution" in text and "1 trade" in text
    assert "1 corporate action" in text and "1 sync file" in text
    assert "Other accounts are not removed." in text
    assert 'name="confirm"' in text and 'value="Drop me"' not in text
    bad = c.post(f"/settings/accounts/{gone_id}/delete", data={"confirm": "nope"}, follow_redirects=False)
    assert bad.status_code == 303
    db.expire_all()
    assert db.get(Account, gone_id) is not None and db.get(Account, keep_id) is not None
    ok = c.post(f"/settings/accounts/{gone_id}/delete", data={"confirm": "Drop me"}, follow_redirects=False)
    assert ok.status_code == 303
    db.expire_all()
    assert db.get(Account, gone_id) is None
    assert db.get(Account, keep_id) is not None
    assert db.query(Execution).filter(Execution.account_id == keep_id).count() == 1
    assert db.query(Trade).filter(Trade.account_id == keep_id).count() == 1
    assert db.query(SyncPayload).filter(SyncPayload.account_id == keep_id).count() == 1
    assert db.query(Execution).count() == 1
    assert db.query(CorporateAdjustment).count() == 0


def test_manual_add_does_not_call_a_broker(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.sources.ibkr_flex.test_connection", lambda *a, **k: calls.append("test"))
    monkeypatch.setattr("app.sources.ibkr_flex.FlexClient.fetch", lambda *a, **k: calls.append("fetch"))
    c = _client(db)
    r = c.post("/settings/accounts", data={
        "name": "Paper", "broker": "other", "currency": "USD",
        "timezone": "America/New_York", "market": "US",
    }, follow_redirects=False)
    assert r.status_code == 303 and "account=" in r.headers["location"]
    made = db.query(Account).filter(Account.name == "Paper").one()
    assert made.broker == "other" and made.currency == "USD"
    assert calls == []


def test_unauthenticated_grid_routes_redirect_or_401(db):
    acct = Account(name="Main", broker="schwab")
    db.add(acct)
    db.commit()
    anon = _anon()
    paths = [
        "/settings/accounts/new",
        f"/settings/accounts/{acct.id}/detail",
        f"/settings/accounts/{acct.id}/edit",
        f"/settings/accounts/{acct.id}/delete",
    ]
    for path in paths:
        html = anon.get(path, follow_redirects=False)
        assert html.status_code == 303 and "/login" in html.headers["location"]
        hx = anon.get(path, headers={"HX-Request": "true"}, follow_redirects=False)
        assert hx.status_code == 401
        assert hx.json()["detail"] == "login required"
    posted = anon.post("/settings/accounts", data={"name": "X"}, follow_redirects=False)
    assert posted.status_code == 303 and "/login" in posted.headers["location"]
    posted_hx = anon.post("/settings/accounts", data={"name": "X"}, headers={"HX-Request": "true"},
                          follow_redirects=False)
    assert posted_hx.status_code == 401


def test_selecting_ibkr_account_makes_no_network_call(db, monkeypatch):
    calls = []

    def boom(*_a, **_k):
        calls.append("ibkr")
        raise AssertionError("IBKR network")

    monkeypatch.setattr("app.sources.ibkr_flex.test_connection", boom)
    monkeypatch.setattr("app.sources.ibkr_flex.FlexClient._get", boom)
    monkeypatch.setattr("app.sources.ibkr_flex.FlexClient.fetch", boom)
    monkeypatch.setattr("app.sources.ibkr_flex.FlexClient.send_request", boom)
    monkeypatch.setattr("app.sources.ibkr_flex._TRANSPORT", None)
    acct = Account(name="IBKR margin", broker="ibkr", source_key="ibkr_flex", currency="USD",
                   external_ref="U88888888", account_number_masked="U***8888")
    db.add(acct)
    db.commit()
    c = _client(db)
    detail = c.get(f"/settings/accounts/{acct.id}/detail")
    page = c.get(f"/settings?account={acct.id}")
    edit = c.get(f"/settings/accounts/{acct.id}/edit")
    assert detail.status_code == 200 and page.status_code == 200 and edit.status_code == 200
    assert calls == []
    assert "U88888888" not in detail.text and "U***8888" in detail.text
    assert "U88888888" not in page.text
    assert "Not connected" in detail.text
    assert 'hx-post="/settings/brokers/ibkr/sync"' not in detail.text
