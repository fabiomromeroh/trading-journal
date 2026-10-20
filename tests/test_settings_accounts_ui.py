"""Accounts and sync settings stay compact and keep every previous endpoint.

The action and htmx list below was taken from the templates before this redesign:
settings.html, brokers_settings.html, ibkr_connect.html, ibkr_upload_progress.html,
t212_connect.html, tos_email_settings.html, sync_runs.html, sync_status.html.
"""
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import get_settings
from app.models import Account, CorporateAdjustment, InboundEmail, Instrument, OAuthToken, utcnow
from app.services import set_state
from app.sources import ibkr_flex as ibkr
from app.sources import trading212 as t212

# Present on a logged-in settings page with one ordinary account and no broker connected.
ALWAYS = [
    "/settings/brokers/ibkr/history-start",
    "/settings/brokers/ibkr/upload",
    "/settings/brokers/ibkr/reprocess",
    "/settings/brokers/ibkr/connect",
    "/settings/brokers/t212/connect",
    "/settings/tos-email/token",
    "/settings/adjustments",
    "/sync/runs",
    'hx-post="/sync"',
    'hx-get="/sync/runs"',
    'hx-trigger="sync-finished from:body"',
    'name="history_start"',
    'name="files"',
    'name="token"',
    'name="query_id"',
    'name="currency"',
    'name="key"',
    'name="secret"',
    'name="practice"',
    'enctype="multipart/form-data"',
    'name="account_id"',
    'name="kind"',
    'name="symbol"',
    'name="ratio"',
    'name="new_symbol"',
    'name="when"',
    'name="name"',
    'name="confirm"',
]

# Shown once that broker is connected (no live call; credentials are stored only).
CONNECTED = [
    'hx-post="/settings/brokers/ibkr/sync"',
    'hx-post="/settings/brokers/ibkr/resync"',
    'action="/settings/brokers/ibkr/test"',
    'action="/settings/brokers/ibkr/disconnect"',
    'hx-post="/settings/brokers/t212/sync"',
    'hx-post="/settings/brokers/t212/resync"',
    'action="/settings/brokers/t212/test"',
    'action="/settings/brokers/t212/disconnect"',
    "/settings/brokers/instruments/",
]

# SnapTrade / Schwab API blocks, rendered only when those sources are configured.
SCHWAB = [
    "/settings/snaptrade/reconnect?id=",
    "/settings/snaptrade/resync",
    "/auth/schwab/connect",
    "/auth/schwab/disconnect",
    "/auth/schwab/paste",
    'name="redirect_url"',
]


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_both_tabs_render_for_a_logged_in_user(db):
    db.add(Account(name="Main", broker="schwab", currency="USD", timezone="America/New_York", market="US"))
    db.commit()
    anon = TestClient(__import__("app.main", fromlist=["create_app"]).create_app())
    assert anon.get("/settings", follow_redirects=False).status_code == 303
    c = _client(db)
    page = c.get("/settings")
    assert page.status_code == 200
    html = page.text
    acct_id = db.query(Account).one().id
    for anchor in ("id=\"accounts\"", "id=\"accounts-list\"", "id=\"brokers\"", "id=\"sync\"",
                   "id=\"sync-history\"", "id=\"sync-attention\"", "id=\"tos-email\""):
        assert anchor in html, anchor
    assert "Accounts" in html and "Sync history" in html and "Corporate actions" in html and "Needs attention" in html
    assert html.index('id="accounts"') < html.index('id="brokers"') < html.index('id="tos-email"')
    assert html.index('id="tos-email"') < html.index('id="sync-attention"') < html.index('id="sync"')
    # Connection forms and the one account panel load on demand. The grid itself does not
    # render every account's sync, history, and corporate actions together.
    add = c.get("/settings/accounts/new").text
    edit = c.get(f"/settings/accounts/{acct_id}/edit").text
    delete = c.get(f"/settings/accounts/{acct_id}/delete").text
    detail = c.get(f"/settings/accounts/{acct_id}/detail").text
    for anchor in ("id=\"edit-connection-ibkr\"", "id=\"edit-connection-t212\"",
                   "id=\"edit-connection-schwab\"", "id=\"edit-connection-tos\""):
        assert anchor in add, anchor
    assert "Main" in detail and "Delete Main" in delete or "Delete" in delete
    blob = html + add + edit + delete + detail
    for bit in ALWAYS:
        assert bit in blob, bit


def test_connected_broker_endpoints_stay(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(ibkr, "_TRANSPORT", None)
    monkeypatch.setattr("app.sources.snaptrade.SnapTradeSource.maybe_refresh", lambda self, db: None)
    s = get_settings()
    monkeypatch.setattr(s, "snaptrade_client_id", "cid")
    monkeypatch.setattr(s, "snaptrade_consumer_key", "ckey")
    monkeypatch.setattr(s, "schwab_app_key", "app")
    monkeypatch.setattr(s, "schwab_app_secret", "sec")
    monkeypatch.setattr(s, "schwab_callback_url", "https://example.test/cb")
    ibkr.save_credentials(db, "IBKR_FLEX_TOKEN_SECRET_XYZ", "999111", "USD")
    t212.save_credentials(db, "T212KEYVALUE99", "T212SECRETVALUE", "live")
    db.add(Account(name="T212 EUR", broker="trading212", source_key="t212", currency="EUR", external_ref="acct-1",
                   account_number_masked="…1111"))
    db.add(Instrument(source_key="t212", broker_symbol="AAA_EQ", symbol="AAA", currency="EUR"))
    set_state(db, "snaptrade:status", json.dumps({
        "connections": [{"id": "auth-1", "broker": "Schwab", "disabled": True, "connected_at": "2026-10-01T00:00:00"}],
        "accounts": [], "error": None, "checked_at": utcnow().isoformat(),
    }))
    db.add(OAuthToken(provider="schwab", access_token_enc="enc-access", refresh_token_enc="enc-refresh",
                      access_expires_at=utcnow() + timedelta(hours=2),
                      refresh_expires_at=utcnow() + timedelta(days=6)))
    db.add(InboundEmail(message_id="<m1>", received_at=datetime(2026, 10, 1, 12, 0), status="fills",
                        fills=1, subject="fill"))
    db.commit()
    c = _client(db)
    page = c.get("/settings").text
    add = c.get("/settings/accounts/new").text
    tos = c.get("/settings?account=tos-email").text
    blob = page + add + tos
    for bit in CONNECTED + SCHWAB:
        assert bit in blob, bit
    assert 'action="/settings/tos-email/' in tos and "/discard" in tos
    assert "T212KEYVALUE99" not in blob and "T212SECRETVALUE" not in blob
    assert "IBKR_FLEX_TOKEN_SECRET_XYZ" not in blob
    assert "enc-access" not in blob and "enc-refresh" not in blob


def test_three_ibkr_accounts_share_one_connection(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(ibkr, "_TRANSPORT", None)
    token = "IBKR_FLEX_TOKEN_SECRET_XYZ"
    full_ids = ["U12345678", "U23456789", "U34567890"]
    ibkr.save_credentials(db, token, "999111", "USD")
    for i, (name, ext) in enumerate(zip(("Personal", "ISA", "Margin"), full_ids)):
        db.add(Account(name=name, broker="ibkr", source_key="ibkr_flex", currency="USD" if i else "EUR",
                       external_ref=ext, account_number_masked=f"U***{ext[-4:]}"))
    db.commit()
    ibkr._trip(900)
    c = _client(db)
    html = c.get("/settings").text
    for name in ("Personal", "ISA", "Margin"):
        assert name in html
    assert token not in html
    for ext in full_ids:
        assert ext not in html
    assert "U***5678" in html and "U***6789" in html and "U***7890" in html
    assert 'data-status="paused"' in html
    assert "cooldown" in html
    # One shared connection. Opening one account does not render the other two, and IBKR rows
    # do not get the generic per-account /sync buttons.
    first = db.query(Account).filter(Account.name == "Personal").one()
    detail = c.get(f"/settings/accounts/{first.id}/detail").text
    assert detail.count('data-connection="ibkr"') == 1
    assert "Personal" in detail and "ISA" not in detail and "Margin" not in detail
    assert 'hx-post="/sync"' not in detail
    assert 'hx-post="/settings/brokers/ibkr/sync"' in detail
    assert 'hx-post="/settings/brokers/ibkr/resync"' in detail
    edit = c.get(f"/settings/accounts/{first.id}/edit").text
    block = edit.split('id="edit-connection-ibkr"', 1)[1]
    assert 'name="token"' in block and 'name="query_id"' in block and 'name="history_start"' in block
    assert 'name="currency"' in block
    assert token not in edit
    for ext in full_ids:
        assert ext not in edit and ext not in detail


def test_upload_polls_every_3s_while_running_and_stops_when_done(db, monkeypatch):
    monkeypatch.setattr(ibkr, "_TRANSPORT", None)
    ibkr.save_upload(db, {"status": "running", "files": [{"name": "a.xml", "status": "running", "size": 10}]})
    db.commit()
    c = _client(db)
    running = c.get("/settings").text
    assert 'id="ibkr-upload-progress"' in running
    assert 'hx-get="/settings/brokers/ibkr/upload/status"' in running
    assert 'hx-trigger="every 3s"' in running
    body = c.get("/settings/brokers/ibkr/upload/status").text
    assert 'hx-trigger="every 3s"' in body
    ibkr.save_upload(db, {"status": "done", "files": [{"name": "a.xml", "status": "done", "fetched": 1, "inserted": 0}]})
    db.commit()
    done = c.get("/settings").text
    assert 'id="ibkr-upload-progress"' in done
    assert 'hx-get="/settings/brokers/ibkr/upload/status"' not in done
    assert "every 3s" not in done
    status = c.get("/settings/brokers/ibkr/upload/status").text
    assert "every 3s" not in status


def test_needs_attention_counts_by_account_and_kind(db):
    a = Account(name="Schwab main", broker="schwab", currency="USD")
    b = Account(name="T212 book", broker="trading212", source_key="t212", currency="EUR")
    db.add_all([a, b])
    db.flush()
    db.add(CorporateAdjustment(account_id=a.id, symbol="AAA", kind="SPLIT", status="needs_attention",
                               source="yahoo", external_key="t:aaa", note="Suggest: split 3 on 2024-01-31 (yahoo)"))
    db.add(CorporateAdjustment(account_id=b.id, symbol="BBB", kind="RENAME", status="needs_attention",
                               source="broker", external_key="t:bbb", note="Suggest: rename BBB -> CCC on 2024-02-01 (broker)"))
    db.commit()
    c = _client(db)
    grid = c.get("/settings").text
    assert 'title="Needs attention: 1"' in grid
    assert "Schwab main" in grid and "T212 book" in grid
    a_html = c.get(f"/settings/accounts/{a.id}/detail").text
    b_html = c.get(f"/settings/accounts/{b.id}/detail").text
    a_block = a_html.split('id="attention-counts"', 1)[1].split("</div>", 1)[0]
    b_block = b_html.split('id="attention-counts"', 1)[1].split("</div>", 1)[0]
    assert 'data-attention-total="1"' in a_block and 'data-kind="SPLIT"' in a_block and "SPLIT 1" in a_block
    assert f'data-account="{a.id}"' in a_block and "Schwab main 1" in a_block
    assert "T212 book" not in a_html and 'data-kind="RENAME"' not in a_block
    assert 'data-attention-total="1"' in b_block and 'data-kind="RENAME"' in b_block and "RENAME 1" in b_block
    assert f'data-account="{b.id}"' in b_block and "T212 book 1" in b_block
    assert "Schwab main" not in b_html and 'data-kind="SPLIT"' not in b_block
    assert 'name="when" value="2024-01-31"' in a_html
    assert ">Save</button>" in a_html


def test_edit_modal_is_slim_and_opens_with_placeholder(db, monkeypatch):
    a = Account(name="IB slim", broker="ibkr", source_key="ibkr_flex", currency="USD")
    db.add(a)
    db.commit()
    import app.adjustments as adj
    import app.flex_facts as ff
    import app.routes.brokers as br

    def boom(*a, **k):
        raise AssertionError("Edit modal must not load the full panel")
    monkeypatch.setattr(adj, "attention_view", boom)
    monkeypatch.setattr(ff, "account_statement", boom)
    monkeypatch.setattr(br, "settings_ctx", boom)
    c = _client(db)
    html = c.get(f"/settings/accounts/{a.id}/edit").text
    assert "Edit account" in html and 'value="IB slim"' in html
    assert c.get("/settings/accounts/tos-email/edit").status_code == 200
    assert c.get("/settings/accounts/999999/edit").status_code == 404
    monkeypatch.undo()
    page = c.get("/settings").text
    assert "htmx:beforeRequest" in page and "Loading…" in page
