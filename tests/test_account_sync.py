"""Per-account Sync new (cursor) and Full history (one-off re-scan). No live broker calls."""
import json
from datetime import date, datetime

from fastapi.testclient import TestClient

from app.models import Account, SourceState, SyncRun
from app.services import get_state, set_state
from app.sources.base import SyncContext, account_is_target
from app.sources.snaptrade import CASHFLOW_STATE, STATUS_STATE, full_resync
from app.sources.trading212 import FULL_STATE
from app.sync import execute_run, source_key_for_account, start_run


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def _capture(monkeypatch, db):
    seen = {}

    def fake(trigger="manual", only=None, account_id=None):
        seen.update(trigger=trigger, only=only, account_id=account_id)
        run = SyncRun(trigger=trigger, status="success", message="ok")
        db.add(run)
        db.commit()
        return run.id

    monkeypatch.setattr("app.routes.settings.start_background", fake)
    return seen


def test_account_is_target_matches_ref_then_mask():
    linked = Account(external_ref="snaptrade:a", account_number_masked="...111")
    fresh = Account(external_ref=None, account_number_masked="...111")
    assert account_is_target(linked, external_ref="snaptrade:a", masked="...999")
    assert not account_is_target(linked, external_ref="snaptrade:b", masked="...111")
    assert account_is_target(fresh, external_ref="snaptrade:a", masked="...111")
    assert not account_is_target(fresh, external_ref="snaptrade:a", masked="...222")
    assert not account_is_target(None, external_ref="x", masked="...111")
    demo = Account(is_demo=True, external_ref="snaptrade:a")
    assert not account_is_target(demo, external_ref="snaptrade:a", masked=None)


def test_source_key_for_account(db):
    csv = Account(name="CSV", broker="schwab")
    t212 = Account(name="T", broker="trading212", source_key="t212", external_ref="123")
    snap = Account(name="S", broker="schwab", external_ref="snaptrade:acc")
    api = Account(name="A", broker="schwab", external_ref="hash-1")
    demo = Account(name="D", broker="schwab", is_demo=True, source_key="t212")
    db.add_all([csv, t212, snap, api, demo])
    db.commit()
    assert source_key_for_account(db, csv) is None
    assert source_key_for_account(db, t212) == "t212"
    assert source_key_for_account(db, snap) == "snaptrade"
    assert source_key_for_account(db, api) == "schwab_api"
    assert source_key_for_account(db, demo) is None


def test_sidebar_and_account_cards_explain_both_actions(db):
    t212 = Account(name="Trading 212 EUR", broker="trading212", source_key="t212", external_ref="123")
    demo = Account(name="Sample account (demo)", broker="demo", is_demo=True)
    db.add_all([t212, demo])
    db.commit()
    c = _client(db)
    home = c.get("/").text
    help_ = home.split('id="sync-help"', 1)[1].split("</p>", 1)[0]
    assert "every connected account" in help_
    assert "stored cursor" in help_ and "Full history" in help_
    side = home.split('id="side-sync-form"', 1)[1].split("</form>", 1)[0]
    assert 'name="account"' not in side
    assert 'name="mode" value="new"' in side and 'name="mode" value="full"' in side
    c.cookies.set("acct", str(t212.id))
    picked = c.get("/").text
    form = picked.split('id="side-sync-form"', 1)[1].split("</form>", 1)[0]
    assert f'name="account" value="{t212.id}"' in form
    assert "Trading 212 EUR" in picked.split('id="sync-help"', 1)[1].split("</p>", 1)[0]
    assert f'"account":"{t212.id}"' in picked  # mobile Sync new
    page = c.get(f"/settings?account={t212.id}").text
    assert "account-sync-help" in page and "stored cursor" in page and "full re-scan" in page
    assert f'"account":"{t212.id}","mode":"new"' in page
    assert f'"account":"{t212.id}","mode":"full"' in page
    assert f'"account":"{demo.id}"' not in page


def test_sync_new_one_account_does_not_arm_full_history(db, monkeypatch):
    t212 = Account(name="T", broker="trading212", source_key="t212", external_ref="123")
    snap = Account(name="S", broker="schwab", external_ref="snaptrade:a")
    db.add_all([t212, snap])
    db.flush()
    db.add(SourceState(source="snaptrade", account_id=snap.id, synced_through=datetime(2026, 9, 1)))
    set_state(db, FULL_STATE, None)
    db.commit()
    seen = _capture(monkeypatch, db)
    r = _client(db).post("/sync", data={"account": str(t212.id), "mode": "new"})
    assert r.status_code == 200
    assert seen["only"] == {"t212"} and seen["account_id"] == t212.id
    db.expire_all()
    assert get_state(db, FULL_STATE) in (None, "")
    assert db.get(SourceState, db.query(SourceState).one().id).synced_through == datetime(2026, 9, 1)


def test_full_history_one_account_leaves_the_other_cursor(db, monkeypatch):
    t212 = Account(name="T", broker="trading212", source_key="t212", external_ref="123")
    snap = Account(name="S", broker="schwab", external_ref="snaptrade:a")
    other = Account(name="O", broker="schwab", external_ref="snaptrade:b")
    api = Account(name="A", broker="schwab", external_ref="hash")
    db.add_all([t212, snap, other, api])
    db.flush()
    db.add_all([
        SourceState(source="snaptrade", account_id=snap.id, synced_through=datetime(2026, 1, 2)),
        SourceState(source="snaptrade", account_id=other.id, synced_through=datetime(2026, 3, 4)),
        SourceState(source="schwab_api", account_id=api.id, synced_through=datetime(2026, 5, 6)),
    ])
    db.commit()
    seen = _capture(monkeypatch, db)
    c = _client(db)
    assert c.post("/sync", data={"account": str(snap.id), "mode": "full"}).status_code == 200
    db.expire_all()
    assert seen["only"] == {"snaptrade"} and seen["account_id"] == snap.id
    by = {(s.source, s.account_id): s.synced_through for s in db.query(SourceState)}
    assert by[("snaptrade", snap.id)] is None
    assert by[("snaptrade", other.id)] == datetime(2026, 3, 4)
    assert by[("schwab_api", api.id)] == datetime(2026, 5, 6)
    assert get_state(db, FULL_STATE) in (None, "")

    assert c.post("/sync", data={"account": str(t212.id), "mode": "full"}).status_code == 200
    db.expire_all()
    assert seen["only"] == {"t212"} and seen["account_id"] == t212.id
    assert get_state(db, FULL_STATE) == "1"
    by = {(s.source, s.account_id): s.synced_through for s in db.query(SourceState)}
    assert by[("snaptrade", other.id)] == datetime(2026, 3, 4)


def test_full_history_all_arms_every_source(db, monkeypatch):
    snap = Account(name="S", broker="schwab", external_ref="snaptrade:a")
    api = Account(name="A", broker="schwab", external_ref="hash")
    db.add_all([snap, api])
    db.flush()
    db.add_all([
        SourceState(source="snaptrade", account_id=snap.id, synced_through=datetime(2026, 1, 2)),
        SourceState(source="schwab_api", account_id=api.id, synced_through=datetime(2026, 5, 6)),
    ])
    db.commit()
    seen = _capture(monkeypatch, db)
    assert _client(db).post("/sync", data={"mode": "full"}).status_code == 200
    db.expire_all()
    assert seen["only"] is None and seen["account_id"] is None
    assert all(s.synced_through is None for s in db.query(SourceState))
    assert get_state(db, FULL_STATE) == "1"


def test_file_account_does_not_start_a_broker_sync(db, monkeypatch):
    csv = Account(name="Imported", broker="schwab")
    db.add(csv)
    db.commit()
    seen = _capture(monkeypatch, db)
    r = _client(db).post("/sync", data={"account": str(csv.id), "mode": "full"})
    assert r.status_code == 200 and "no broker connection" in r.text
    assert seen == {}
    db.expire_all()
    assert get_state(db, FULL_STATE) in (None, "")
    assert db.query(SyncRun).one().status == "skipped"
    assert _client(db).post("/sync", data={"account": "999"}).status_code == 200
    assert "cannot be synced" in _client(db).post("/sync", data={"account": "999"}).text


def test_snaptrade_sync_new_uses_cursor_and_skips_the_other_account(db):
    keep = Account(name="Keep", broker="schwab", external_ref="snaptrade:keep", account_number_masked="...111")
    db.add(keep)
    db.flush()
    db.add(SourceState(source="snaptrade", account_id=keep.id, synced_through=datetime(2026, 9, 1)))
    set_state(db, CASHFLOW_STATE + str(keep.id), "{}")
    set_state(db, STATUS_STATE, json.dumps({"accounts": [
        {"id": "keep", "name": "Keep", "masked": "...111", "tx_synced_through": "2026-09-01"},
        {"id": "skip", "name": "Skip", "masked": "...222", "tx_synced_through": "2026-09-01"},
    ], "connections": []}))
    db.commit()
    calls = []

    class Client:
        def activities(self, aid, start=None):
            calls.append((aid, start))
            return []

        def orders(self, aid):
            return []

        def recent_orders(self, aid):
            return []

        def balances(self, aid):
            return []

        def positions(self, aid):
            return []

    from app.sources.snaptrade import SnapTradeSource
    SnapTradeSource(client=Client()).sync(db, SyncContext(account_id=keep.id))
    assert calls == [("keep", date(2026, 8, 29))]
    assert db.query(Account).filter(Account.external_ref == "snaptrade:skip").count() == 0

    calls.clear()
    full_resync(db, keep.id)
    db.expire_all()
    SnapTradeSource(client=Client()).sync(db, SyncContext(account_id=keep.id))
    assert calls == [("keep", None)]


def test_schwab_sync_skips_other_account(db, monkeypatch):
    keep = Account(name="Keep", broker="schwab", external_ref="h1", account_number_masked="...111")
    skip = Account(name="Skip", broker="schwab", external_ref="h2", account_number_masked="...222")
    db.add_all([keep, skip])
    db.commit()
    seen = []
    from app.sources.schwab_api import SchwabApiSource, SchwabClient
    monkeypatch.setattr(SchwabClient, "account_numbers", lambda self: [
        {"accountNumber": "999111", "hashValue": "h1"},
        {"accountNumber": "999222", "hashValue": "h2"},
    ])
    monkeypatch.setattr(SchwabClient, "transactions", lambda self, h, start, end, tx_type: seen.append(h) or [])
    res = SchwabApiSource().sync(db, SyncContext(account_id=keep.id))
    assert set(seen) == {"h1"}
    assert res.accounts == [keep.id]
    assert db.query(SourceState).filter(SourceState.account_id == skip.id).count() == 0


def test_t212_full_flag_is_not_consumed_for_another_account(db, monkeypatch):
    from app.config import get_settings
    import app.sources.trading212 as t212
    from tests.test_trading212 import KEY, SECRET, FakeT212
    monkeypatch.setattr(get_settings(), "token_encryption_key", "unit-test-key")
    t212.save_credentials(db, KEY, SECRET, "live")
    other = Account(name="Other", broker="schwab", external_ref="snaptrade:x")
    db.add(other)
    set_state(db, FULL_STATE, "1")
    db.commit()
    fake = FakeT212()
    src = t212.Trading212Source(transport=fake.transport(), sleep=lambda *_: None)
    res = src.sync(db, SyncContext(account_id=other.id))
    assert res.accounts == []
    assert get_state(db, FULL_STATE) == "1"
    assert not any(str(c.url.path).endswith("/equity/history/orders") for c in fake.calls)


def test_execute_run_limits_the_sync_to_one_account(db):
    from app.sources.base import SourceResult, SourceStatus
    seen = {}

    class Src:
        key = "t212"
        name = "Trading 212"

        def is_configured(self):
            return True

        def refresh(self, db_):
            return None

        def status(self, db_):
            return SourceStatus(True, True, "ok")

        def sync(self, db_, ctx):
            seen["account_id"] = ctx.account_id
            return SourceResult()

    run, _ = start_run(db, "manual")
    out = execute_run(run.id, sources=[Src()], only={"t212"}, account_id=4)
    assert seen["account_id"] == 4 and out.status == "success"
