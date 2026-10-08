"""SnapTrade source: activity mapping, signed requests, incremental sync, merging with file imports,
connection health and the reconnect flow. Uses synthetic fixtures and a mocked HTTP API only."""
import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.instruments import ExecRecord
from app.models import Account, Execution, SourceState, SyncRun, Trade, utcnow
from app.services import ingest_records, rebuild_trades
from app.sources.base import SyncContext
from app.sources.snaptrade import (SnapTradeClient, SnapTradeSource, parse_activities, refresh_status,
                                   sign)
from app.sync import execute_run, start_run, summarize

CID, KEY = "test-client", "test-consumer-key"
ACCOUNT_ID = "acc-0000"
AUTH_ID = "auth-0000"


@pytest.fixture()
def activities(fixture_text):
    return json.loads(fixture_text("snaptrade_activities.json"))


@pytest.fixture()
def st_env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "snaptrade_client_id", CID)
    monkeypatch.setattr(s, "snaptrade_consumer_key", KEY)
    return s


class FakeSnapTrade:
    """Mocked SnapTrade API that verifies every request signature."""

    def __init__(self, activities, disabled=False, through="2026-09-25"):
        self.activities = activities
        self.disabled = disabled
        self.through = through
        self.calls: list[httpx.Request] = []
        self.login_bodies: list[dict] = []
        self.updated = "2026-10-01T08:00:00Z"

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        query = req.url.query.decode()
        body = json.loads(req.content) if req.content else None
        expected = sign(KEY, req.url.path.removeprefix("/api/v1"), query, body)
        assert req.headers["Signature"] == expected, "bad signature"
        q = parse_qs(query)
        assert q["clientId"] == [CID] and "timestamp" in q and "userId" not in q and "userSecret" not in q
        p = req.url.path
        if p == "/api/v1/authorizations":
            return httpx.Response(200, json=[{
                "id": AUTH_ID, "name": "Connection-1", "type": "read", "disabled": self.disabled,
                "disabled_date": "2026-10-08T08:00:00Z" if self.disabled else None,
                "created_date": "2026-10-01T08:00:00Z", "updated_date": self.updated,
                "brokerage": {"name": "Schwab", "display_name": "Schwab", "slug": "SCHWAB"}}])
        if p == "/api/v1/accounts":
            return httpx.Response(200, json=[{
                "id": ACCOUNT_ID, "brokerage_authorization": AUTH_ID, "name": "Individual ...123",
                "number": "Individual ...123", "institution_name": "Schwab",
                "sync_status": {"transactions": {"last_successful_sync": self.through,
                                                 "first_transaction_date": "2026-01-05",
                                                 "initial_sync_completed": True}}}])
        if p == f"/api/v1/accounts/{ACCOUNT_ID}/activities":
            start = q.get("startDate", [None])[0]
            rows = [a for a in self.activities if not start or a["trade_date"][:10] >= start]
            off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["1000"])[0])
            return httpx.Response(200, json={"data": rows[off:off + lim],
                                             "pagination": {"offset": off, "limit": lim, "total": len(rows)}})
        if p == "/api/v1/snapTrade/login":
            self.login_bodies.append(body)
            return httpx.Response(200, json={"redirectURI": "https://app.snaptrade.com/portal?t=abc", "sessionId": "s"})
        return httpx.Response(404, json={"detail": "not found"})

    def client(self):
        return SnapTradeClient(CID, KEY, transport=httpx.MockTransport(self.handler))


# ------------------------------------------------------------------------------------ mapping
def test_signature_matches_documented_example():
    # Canonical JSON with sorted keys and no whitespace, HMAC-SHA256, base64.
    payload = '{"content":{"substring":"AAPL"},"path":"/api/v1/symbols","query":"clientId=C&timestamp=1"}'
    want = base64.b64encode(hmac.new(b"K", payload.encode(), hashlib.sha256).digest()).decode()
    assert sign("K", "/symbols", "clientId=C&timestamp=1", {"substring": "AAPL"}) == want
    empty = '{"content":null,"path":"/api/v1/accounts","query":"clientId=C&timestamp=1"}'
    assert sign("K", "/accounts", "clientId=C&timestamp=1", None) == \
        base64.b64encode(hmac.new(b"K", empty.encode(), hashlib.sha256).digest()).decode()


def test_parse_activities_mapping(activities):
    recs, ignored = parse_activities(activities)
    assert ignored == {"TRANSFER": 1, "DIVIDEND": 1}
    by_ext = {r.external_id: r for r in recs}
    assert len(recs) == 8

    buy = by_ext["REF-BUY-1"]
    assert (buy.symbol, buy.side, buy.quantity, buy.price, buy.fees) == ("EXMP", "BUY", 10, 100.25, 0)
    assert buy.asset_type == "STOCK" and buy.position_effect is None
    # date-only: stamped 16:00 New York (20:00 UTC in September), time unknown, like the CSV path
    assert buy.executed_at == datetime(2026, 9, 14, 20, 0) and buy.time_known is False
    assert buy.trade_date == date(2026, 9, 14)
    assert buy.raw["snaptrade_id"] == "00000000-0000-4000-8000-000000000002"

    sell = by_ext["REF-SELL-1"]
    assert sell.side == "SELL" and sell.quantity == 10 and sell.fees == pytest.approx(0.02)
    assert sell.position_effect == "CLOSE"

    sto = by_ext["REF-STO-1"]
    assert sto.symbol == "SPY 2026-09-18 580P" and sto.asset_type == "OPTION"
    assert (sto.side, sto.position_effect, sto.quantity, sto.price, sto.multiplier) == ("SELL", "OPEN", 2, 1.25, 100)
    assert sto.option_type == "PUT" and sto.strike == 580 and sto.expiration == date(2026, 9, 18)

    exp = by_ext["REF-EXP-1"]
    assert exp.kind == "EXPIRATION" and exp.side is None and exp.price == 0 and exp.quantity == 2

    # two legs sharing one Schwab reference id get deterministic, distinct ids
    legs = sorted(r.external_id for r in recs if r.external_id.startswith("REF-SPREAD-1"))
    assert legs == ["REF-SPREAD-1#0", "REF-SPREAD-1#1"]
    again, _ = parse_activities(list(reversed(activities)))
    assert {r.external_id: r.symbol for r in again} == {r.external_id: r.symbol for r in recs}


def test_assignment_stock_leg_and_fee_rows():
    acts = [
        {"id": "1", "type": "OPTIONASSIGNMENT", "description": "ASSIGNED", "symbol": None, "units": 1, "price": 0,
         "amount": 0, "fee": 0, "external_reference_id": "A1", "trade_date": "2026-09-18T00:00:00Z",
         "option_symbol": {"ticker": "XYZ   260918P00050000", "option_type": "PUT", "strike_price": 50,
                           "expiration_date": "2026-09-18", "underlying_symbol": {"symbol": "XYZ"}}},
        {"id": "2", "type": "OPTIONASSIGNMENT", "description": "ASSIGNED SHARES", "units": 100, "price": 50,
         "amount": -5000, "fee": 0, "external_reference_id": "A2", "trade_date": "2026-09-18T00:00:00Z",
         "symbol": {"symbol": "XYZ"}, "option_symbol": None},
        {"id": "3", "type": "BUY", "description": "XYZ", "units": 10, "price": 49, "amount": -490, "fee": 0,
         "external_reference_id": "B1", "trade_date": "2026-09-19T00:00:00Z", "symbol": {"symbol": "XYZ"},
         "option_symbol": None},
        {"id": "4", "type": "FEE", "description": "REG FEE", "units": 0, "price": 0, "amount": -0.03, "fee": 0,
         "external_reference_id": "B1", "trade_date": "2026-09-19T00:00:00Z", "symbol": None, "option_symbol": None},
    ]
    recs, ignored = parse_activities(acts)
    by = {r.external_id: r for r in recs}
    assert by["A1"].kind == "ASSIGNMENT" and by["A1"].side is None
    assert by["A2"].kind == "TRADE" and by["A2"].side == "BUY" and by["A2"].symbol == "XYZ"
    assert by["B1"].fees == pytest.approx(0.03)
    assert not ignored


# ------------------------------------------------------------------------------------ sync
def _run(db, fake):
    run, _ = start_run(db, "manual")
    return execute_run(run.id, sources=[SnapTradeSource(client=fake.client())])


def test_full_then_incremental_sync(db, st_env, activities):
    fake = FakeSnapTrade(activities)
    run = _run(db, fake)
    db.expire_all()
    assert run.status == "success", run.error
    assert run.inserted == 8
    assert run.message.splitlines()[0] == "Added 8 new fills; 5 new trades, 0 updated."
    acct = db.scalar(select(Account))
    assert acct.name == "Schwab Individual ...123" and acct.account_number_masked == "...123"
    assert acct.external_ref == f"snaptrade:{ACCOUNT_ID}"
    trades = {t.symbol: t for t in db.scalars(select(Trade))}
    assert trades["EXMP"].status == "CLOSED"
    assert trades["EXMP"].net_pnl == pytest.approx(10 * (105.5 - 100.25) - 0.02)
    assert trades["SMPL"].net_pnl == pytest.approx(-5 - 0.01)  # same-day round trip, buy sorted first
    assert trades["SPY 2026-09-18 580P"].close_reason == "EXPIRATION"
    first_calls = [c for c in fake.calls if c.url.path.endswith("/activities")]
    assert "startDate" not in first_calls[0].url.query.decode()  # first sync pulls full history
    st = db.scalar(select(SourceState))
    assert st.synced_through == datetime(2026, 9, 25)

    # Second sync: incremental from synced_through - overlap, nothing duplicated.
    fake.calls.clear()
    fake.through = "2026-09-26"
    fake.activities = activities + [{
        "id": "00000000-0000-4000-8000-000000000011", "type": "BUY", "description": "SAMPLE INC",
        "symbol": {"symbol": "SMPL"}, "option_symbol": None, "option_type": "", "units": 3.0, "price": 41.0,
        "amount": -123.0, "fee": 0.0, "external_reference_id": "REF-BUY-3",
        "trade_date": "2026-09-26T00:00:00Z", "settlement_date": "2026-09-29T00:00:00Z"}]
    run2 = _run(db, fake)
    db.expire_all()
    assert run2.status == "success" and run2.inserted == 1
    assert run2.message.splitlines()[0] == "Added 1 new fill; 1 new trade, 0 updated."
    q = [c for c in fake.calls if c.url.path.endswith("/activities")][0].url.query.decode()
    assert "startDate=2026-09-22" in q  # 3-day overlap
    assert db.query(Execution).count() == 9

    run3 = _run(db, fake)
    assert run3.inserted == 0 and run3.message.splitlines()[0] == "No new fills. You're up to date."


def test_merges_with_csv_and_tos_imports(db, st_env, activities):
    from app.importers import schwab_csv
    acct = Account(name="Schwab ...123", broker="schwab", account_number_masked="...123")
    db.add(acct)
    db.commit()
    csv_text = ('"Transactions for account Individual ...123 as of 09/30/2026"\n'
                '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
                '"09/16/2026","Sell","EXMP","EXAMPLE CORP","10","$105.50","$0.02","$1054.98"\n'
                '"09/14/2026","Buy","EXMP","EXAMPLE CORP","10","$100.25","","-$1002.50"\n')
    res = schwab_csv.parse(csv_text)
    stats = ingest_records(db, acct.id, "schwab_csv", res.records)
    assert stats.inserted == 2
    db.commit()

    run = _run(db, FakeSnapTrade(activities))
    db.expire_all()
    assert run.status == "success", run.error
    assert db.scalar(select(Account.id).where(Account.id != acct.id)) is None  # same account reused
    # The two EXMP fills already came from the CSV: matched, not inserted again.
    assert db.query(Execution).filter(Execution.symbol == "EXMP").count() == 2
    assert run.inserted == 6

    # A thinkorswim statement adds exact times to the SnapTrade fills.
    tos = ExecRecord(external_id="tos-1", symbol="SMPL", underlying="SMPL", asset_type="STOCK", side="BUY",
                     quantity=5, price=40.0, executed_at=datetime(2026, 9, 16, 14, 31, 5), time_known=True,
                     trade_date=date(2026, 9, 16))
    stats = ingest_records(db, acct.id, "tos_statement", [tos])
    assert stats.merged == 1 and stats.inserted == 0
    row = db.scalar(select(Execution).where(Execution.symbol == "SMPL", Execution.side == "BUY"))
    assert row.source == "snaptrade" and row.time_known and row.executed_at == datetime(2026, 9, 16, 14, 31, 5)
    db.commit()

    # Re-syncing later keeps the enriched time.
    run2 = _run(db, FakeSnapTrade(activities))
    assert run2.inserted == 0
    db.expire_all()
    row = db.get(Execution, row.id)
    assert row.time_known and row.executed_at == datetime(2026, 9, 16, 14, 31, 5)


# ------------------------------------------------------------------------------------ health
def test_status_disabled_connection_banner(db, st_env, activities):
    fake = FakeSnapTrade(activities, disabled=True)
    src = SnapTradeSource(client=fake.client())
    refresh_status(db, fake.client())
    st = src.status(db)
    assert st.configured and not st.ready and st.level == "error"
    assert st.banner and st.banner["link"] == f"/settings/snaptrade/reconnect?id={AUTH_ID}"
    run = _run(db, fake)
    assert run.status == "failed" and "reconnect" in run.error.lower()
    assert not [c for c in fake.calls if c.url.path.endswith("/activities")]


def test_status_relogin_estimate_and_reenable(db, st_env, activities, monkeypatch):
    fake = FakeSnapTrade(activities)
    fake.updated = (utcnow() - timedelta(days=6, hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = refresh_status(db, fake.client())
    st = SnapTradeSource(client=fake.client()).status(db)
    assert st.ready and st.level == "warning" and 0 < st.expires_in_seconds < 86400
    assert st.banner["level"] == "warning"
    # disabled -> enabled again counts as a fresh login (7 more days)
    fake.disabled = True
    refresh_status(db, fake.client())
    fake.disabled = False
    refresh_status(db, fake.client())
    st = SnapTradeSource(client=fake.client()).status(db)
    assert st.ready and st.expires_in_seconds > 6.9 * 86400 and st.banner is None
    assert snap["accounts"][0]["masked"] == "...123"


def test_not_configured(db):
    st = SnapTradeSource().status(db)
    assert not st.configured and not SnapTradeSource().is_configured()


def test_summarize():
    before = {"a": ("CLOSED", 0, 10, 1.0, None, None), "b": ("OPEN", 5, 5, 0.0, None, None)}
    after = {"a": ("CLOSED", 0, 10, 1.0, None, None), "b": ("CLOSED", 0, 5, 3.0, None, None), "c": ("OPEN", 1, 1, 0, None, None)}
    assert summarize(2, 0, before, after) == "Added 2 new fills; 1 new trade, 1 updated."
    assert summarize(0, 0, before, before) == "No new fills. You're up to date."


# ------------------------------------------------------------------------------------ web
def test_reconnect_route_uses_existing_connection(db, st_env, activities, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.sources.snaptrade as stmod

    fake = FakeSnapTrade(activities, disabled=True)
    orig_init = stmod.SnapTradeClient.__init__

    def init(self, client_id=None, consumer_key=None, transport=None, timeout=30):
        orig_init(self, CID, KEY, transport=httpx.MockTransport(fake.handler), timeout=timeout)
    monkeypatch.setattr(stmod.SnapTradeClient, "__init__", init)

    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    page = c.get("/settings").text
    assert "Reconnect Schwab" in page and "login expired" in page
    assert "needs a new login" in c.get("/").text  # banner on every page

    r = c.post(f"/settings/snaptrade/reconnect?id={AUTH_ID}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://app.snaptrade.com/portal")
    body = fake.login_bodies[-1]
    assert body["reconnect"] == AUTH_ID and "broker" not in body and body["connectionType"] == "read"
    assert body["customRedirect"].endswith(f"/settings/snaptrade/return?id={AUTH_ID}")
    assert c.post("/settings/snaptrade/reconnect?id=nope", follow_redirects=False).headers["location"] == "/settings"

    fake.disabled = False
    r = c.get(f"/settings/snaptrade/return?id={AUTH_ID}", follow_redirects=True)
    assert "Schwab reconnected" in r.text
    assert "needs a new login" not in c.get("/").text
    _ = SyncRun
