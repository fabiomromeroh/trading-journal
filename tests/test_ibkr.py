"""Interactive Brokers Flex adapter: XML parse, mocked SendRequest/GetStatement (1019 retry),
encrypted token, incremental idempotent sync, Settings card. Synthetic fixtures only."""
from __future__ import annotations

import json
import logging
import tracemalloc
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.instruments import ExecRecord
from app.models import (
    Account, BalancePoint, CashFlow, CorporateAdjustment, Dividend, Execution, Instrument, SyncPayload, SyncRun, Trade,
)
from app.services import get_state
from app.sources.base import SyncContext
from app.sources import ibkr_flex as ibkr
from app import backup

FIX = Path(__file__).parent / "fixtures" / "ibkr"
TOKEN, QUERY_ID = "IBKR_FLEX_TOKEN_SECRET_XYZ", "123456"


def load(name: str) -> str:
    return (FIX / name).read_text()


class FakeFlex:
    """IBKR Flex Web Service. Optional 1019 then statement, or a canned error body."""

    def __init__(self, statement: str | None = None, send_xml: str | None = None,
                 get_bodies: list[str] | None = None):
        self.statement = statement if statement is not None else load("statement.xml")
        self.send_xml = send_xml if send_xml is not None else load("send_ok.xml")
        self.get_bodies = list(get_bodies) if get_bodies is not None else None
        self.calls: list[httpx.Request] = []
        self._get_i = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if path.endswith("SendRequest") or "SendRequest" in path:
            return httpx.Response(200, text=self.send_xml)
        if self.get_bodies is not None:
            i = min(self._get_i, len(self.get_bodies) - 1)
            body = self.get_bodies[i]
            self._get_i += 1
            return httpx.Response(200, text=body)
        return httpx.Response(200, text=self.statement)

    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture()
def enc(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(ibkr, "_TRANSPORT", None)
    monkeypatch.setattr(ibkr._time, "sleep", lambda _n: None)
    monkeypatch.setattr(ibkr, "SEND_GAP", 0.0)
    return s


def nop_sleep(_n=None):
    return None


def _run_sync(db, fake=None):
    fake = fake or FakeFlex()
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    return src.sync(db, SyncContext()), fake, src


def _trade(tid):
    parsed = ibkr.parse_statement(load("statement.xml"))
    for row in parsed.trades:
        if str(row.get("tradeID") or row.get("TradeID")) == str(tid):
            return row
    raise AssertionError(tid)


# --------------------------------------------------------------------------- parse
def test_parse_stock_long_partial_exits():
    buy = ibkr.trade_to_record(_trade("1001"))
    s1 = ibkr.trade_to_record(_trade("1002"))
    s2 = ibkr.trade_to_record(_trade("1003"))
    assert isinstance(buy, ExecRecord) and buy.symbol == "AAPL" and buy.side == "BUY"
    assert buy.quantity == 100 and buy.price == 150 and buy.position_effect == "OPEN"
    assert buy.fees == pytest.approx(1.05)  # commission 1 + tax 0.05
    assert buy.external_id == "1001" and buy.time_known is True
    assert buy.trade_date == date(2026, 9, 2)
    assert s1.side == "SELL" and s1.quantity == 40 and s1.position_effect == "CLOSE"
    assert s2.quantity == 60 and s2.price == 155
    order = ibkr.trade_to_record(_trade("1"))
    assert isinstance(order, ibkr.Skip) and "order" in order.reason


def test_parse_short_open_close():
    op = ibkr.trade_to_record(_trade("2001"))
    cl = ibkr.trade_to_record(_trade("2002"))
    assert op.symbol == "TSLA" and op.side == "SELL" and op.position_effect == "OPEN"
    assert op.quantity == 10 and op.price == 250
    assert cl.side == "BUY" and cl.position_effect == "CLOSE" and cl.quantity == 10


def test_parse_option_round_trip_multiplier():
    b = ibkr.trade_to_record(_trade("3001"))
    s = ibkr.trade_to_record(_trade("3002"))
    assert b.asset_type == "OPTION" and b.multiplier == 100 and b.option_type == "CALL"
    assert b.underlying == "AAPL" and b.strike == 150 and b.expiration == date(2026, 1, 17)
    assert "150C" in b.symbol and b.side == "BUY" and b.quantity == 2 and b.price == 3.50
    assert b.fees == pytest.approx(1.30) and b.position_effect == "OPEN"
    assert s.side == "SELL" and s.position_effect == "CLOSE" and s.multiplier == 100


def test_parse_non_usd_fx_and_element_style():
    sap = ibkr.trade_to_record(_trade("4001"))
    assert sap.symbol == "SAP" and sap.currency == "EUR" and sap.fx_to_account == pytest.approx(1.08)
    assert sap.price == 180 and sap.fees == pytest.approx(0.50)
    assert sap.trade_date == date(2026, 9, 18)
    msft = ibkr.trade_to_record(_trade("6001"))
    assert isinstance(msft, ExecRecord) and msft.symbol == "MSFT" and msft.quantity == 5
    assert msft.price == 400 and msft.fees == pytest.approx(0.35)
    fut = ibkr.trade_to_record(_trade("5001"))
    assert isinstance(fut, ibkr.Skip) and "future" in fut.reason
    summary = ibkr.trade_to_record(_trade("8001"))
    assert isinstance(summary, ibkr.Skip)


def test_parse_cash_dividends_interest_and_corporate():
    parsed = ibkr.parse_statement(load("statement.xml"))
    assert parsed.account_id == "U1234567" and parsed.currency == "USD"
    assert parsed.equity and parsed.equity["total"] == 13000
    flows = [ibkr.cash_to_flow(r, "USD") for r in parsed.cash]
    flows = [f for f in flows if f]
    kinds = {(f["kind"], round(f["amount"], 2)) for f in flows}
    assert ("deposit", 1000.0) in kinds and ("withdrawal", -200.0) in kinds
    assert ("interest", 2.25) in kinds and ("interest", -0.10) in kinds
    divs = [r for r in parsed.cash if ibkr._cash_type(r) in ibkr._DIV_TYPES]
    taxes = [r for r in parsed.cash if ibkr._cash_type(r) in ibkr._WH_TYPES]
    paired, leftover = ibkr.match_withholding(divs, taxes)
    aapl_net = next(net for row, net in paired if (row.get("symbol") or "") == "AAPL")
    assert aapl_net == pytest.approx(8.50)
    assert len(leftover) == 1 and leftover[0].get("symbol") == "IBM"
    events, splits = ibkr.corporate_events(parsed.corporate)
    kinds = {e["kind"] for e in events}
    assert any(e["kind"] == "SPLIT" and e["symbol"] == "AAPL" and e["ratio"] == 4 for e in events)
    assert any(e["kind"] == "RENAME" and e["symbol"] == "FB" and e["new_symbol"] == "META" for e in events)
    assert any(e["kind"] == "ATTENTION" and e["symbol"] == "ABCD" for e in events)
    assert splits and splits[0].kind == "SPLIT" and splits[0].quantity == 4
    pos = ibkr.position_map(parsed.positions)
    assert pos["MSFT"] == pytest.approx(5) and pos["NVDA"] == pytest.approx(10)
    assert "LOT" not in str(pos)


# --------------------------------------------------------------------------- HTTP
def test_send_request_get_statement_1019_retry(enc):
    fake = FakeFlex(get_bodies=[load("get_1019.xml"), load("statement.xml")])
    sleeps = []
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=sleeps.append)
    xml = cl.fetch(QUERY_ID)
    cl.close()
    assert "<FlexQueryResponse" in xml
    assert any("SendRequest" in c.url.path for c in fake.calls)
    gets = [c for c in fake.calls if "GetStatement" in c.url.path]
    assert len(gets) == 2
    assert sleeps  # waited between 1019 and the retry
    for c in fake.calls:
        assert TOKEN in str(c.url)  # the live request carries it
    # but our public helpers never echo it
    status, code, message, ref, url = ibkr.parse_flex_response(load("get_1019.xml"), TOKEN)
    assert code == "1019" and TOKEN not in (message or "")
    assert TOKEN not in (url or "")


def test_error_code_redacts_token(enc):
    fake = FakeFlex(get_bodies=[load("get_error.xml")])
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=nop_sleep)
    with pytest.raises(ibkr.FlexError) as ei:
        cl.fetch(QUERY_ID)
    cl.close()
    assert ei.value.code == "1099"
    assert TOKEN not in str(ei.value)
    assert "1099" in str(ei.value)
    res = ibkr.test_connection(TOKEN, QUERY_ID, transport=fake.transport(), sleep=nop_sleep)
    assert not res["ok"] and TOKEN not in json.dumps(res)


def test_token_never_in_logs(enc, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeFlex()
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=nop_sleep)
    cl.fetch(QUERY_ID)
    cl.close()
    blob = caplog.text
    assert TOKEN not in blob


# --------------------------------------------------------------------------- persist / sync
def test_credentials_encrypted_and_excluded_from_backup(db, enc):
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    raw = get_state(db, ibkr.CRED_STATE)
    assert raw and TOKEN not in raw
    loaded = ibkr.load_credentials(db)
    assert loaded == {"token": TOKEN, "query_id": QUERY_ID, "currency": "USD"}
    data = backup.build(db)
    keys = {x["key"] for x in data["tables"]["app_state"]}
    assert ibkr.CRED_STATE not in keys
    dumped = json.dumps(data)
    assert TOKEN not in dumped
    ibkr.clear_credentials(db)
    db.commit()
    assert ibkr.load_credentials(db) is None


def test_sync_ingests_and_dedupes(db, enc):
    res, fake, src = _run_sync(db)
    acct = db.scalar(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))
    assert acct is not None and acct.broker == "ibkr" and acct.currency == "USD"
    execs = list(db.scalars(select(Execution).where(Execution.account_id == acct.id,
                                                    Execution.source == ibkr.SOURCE_KEY)))
    ids = {e.external_id for e in execs}
    assert "1001" in ids and "1002" in ids and "1003" in ids
    assert "2001" in ids and "3001" in ids and "4001" in ids and "6001" in ids
    assert "1" not in ids and "8001" not in ids and "5001" not in ids  # ORDER / summary / FUT
    aapl = [e for e in execs if e.symbol == "AAPL" and e.kind == "TRADE"]
    assert {e.quantity for e in aapl} == {100.0, 40.0, 60.0}
    opt = next(e for e in execs if e.asset_type == "OPTION")
    assert opt.multiplier == 100 and opt.underlying == "AAPL"
    sap = next(e for e in execs if e.symbol == "SAP")
    assert sap.currency == "EUR" and sap.fx_to_account == pytest.approx(1.08)
    split = next(e for e in execs if e.kind == "SPLIT")
    assert split.symbol == "AAPL" and split.quantity == pytest.approx(4)

    trades = list(db.scalars(select(Trade).where(Trade.account_id == acct.id)))
    by = {t.symbol: t for t in trades if t.asset_type == "STOCK"}
    assert by["AAPL"].status == "CLOSED" and by["AAPL"].quantity == pytest.approx(100)
    assert by["TSLA"].direction == "SHORT" and by["TSLA"].status == "CLOSED"
    assert by["MSFT"].status == "OPEN" and by["MSFT"].open_quantity == pytest.approx(5)
    opt_t = next(t for t in trades if t.asset_type == "OPTION")
    assert opt_t.status == "CLOSED" and opt_t.quantity == pytest.approx(2)

    flows = list(db.scalars(select(CashFlow).where(CashFlow.account_id == acct.id)))
    assert {round(f.amount, 2) for f in flows if f.kind == "deposit"} == {1000.0}
    assert {round(f.amount, 2) for f in flows if f.kind == "withdrawal"} == {-200.0}
    ints = {round(f.amount, 2) for f in flows if f.kind == "interest"}
    assert 2.25 in ints and -0.10 in ints

    divs = list(db.scalars(select(Dividend).where(Dividend.account_id == acct.id)))
    aapl_d = next(d for d in divs if d.symbol == "AAPL")
    assert aapl_d.amount == pytest.approx(8.50)  # 10.00 + withholding -1.50
    ibm = next(d for d in divs if d.symbol == "IBM")
    assert ibm.amount == pytest.approx(-0.75)
    msft_d = next(d for d in divs if d.symbol == "MSFT")
    assert msft_d.amount == pytest.approx(1.25)

    last = ibkr.last_status(db)
    assert last["ok"] is True and TOKEN not in json.dumps(last)
    assert last["skipped"].get("futures") == 1 or last["skipped"].get("order")
    assert "NVDA" in json.dumps(last.get("adjustments") or [])
    adj = list(db.scalars(select(CorporateAdjustment).where(CorporateAdjustment.account_id == acct.id)))
    assert any(a.kind == "RENAME" and a.symbol == "FB" and a.new_symbol == "META" for a in adj)
    assert any(a.status == "needs_attention" and a.symbol == "ABCD" for a in adj)

    inserted_first = res.inserted
    assert inserted_first >= 8
    res2, _, _ = _run_sync(db, fake)
    assert res2.inserted == 0
    assert db.query(Execution).filter(Execution.account_id == acct.id,
                                      Execution.source == ibkr.SOURCE_KEY).count() == len(execs)
    assert db.query(Dividend).filter(Dividend.account_id == acct.id).count() == len(divs)
    assert db.query(CashFlow).filter(CashFlow.account_id == acct.id).count() == len(flows)


def test_full_history_note(db, enc):
    ibkr.save_credentials(db, TOKEN, QUERY_ID)
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=FakeFlex().transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    res = src.sync(db, SyncContext())
    assert "full history" in res.message.lower()
    assert "windows through" in res.message
    assert get_state(db, ibkr.FULL_STATE) in (None, "")


def test_settings_card_saves_without_echoing_token(db, enc, monkeypatch):
    monkeypatch.setattr(ibkr, "_TRANSPORT", FakeFlex().transport())
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    page = c.get("/settings").text
    assert "Interactive Brokers" in page
    assert "Flex token" in page and "Query ID" in page
    assert "coming soon" not in page.lower() or "ibkr" in page.lower()
    r = c.post("/settings/brokers/ibkr/connect",
               data={"token": TOKEN, "query_id": QUERY_ID, "currency": "USD"}, follow_redirects=True)
    assert r.status_code == 200 and "Interactive Brokers connected" in r.text
    assert TOKEN not in r.text
    creds = ibkr.load_credentials(db)
    assert creds["token"] == TOKEN and creds["query_id"] == QUERY_ID
    page = c.get("/settings").text
    assert TOKEN not in page
    assert "saved" in page
    assert QUERY_ID in page
    r = c.post("/settings/brokers/ibkr/test", follow_redirects=True)
    assert "connection OK" in r.text and TOKEN not in r.text
    r = c.post("/settings/brokers/ibkr/disconnect", follow_redirects=True)
    assert ibkr.load_credentials(db) is None
    assert TOKEN not in r.text


def test_connect_rejects_error_without_token(db, enc, monkeypatch):
    monkeypatch.setattr(ibkr, "_TRANSPORT", FakeFlex(get_bodies=[load("get_error.xml")]).transport())
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    r = c.post("/settings/brokers/ibkr/connect",
               data={"token": TOKEN, "query_id": QUERY_ID}, follow_redirects=True)
    assert "not connected" in r.text.lower()
    assert ibkr.load_credentials(db) is None
    assert TOKEN not in r.text


def test_all_sources_includes_ibkr_unconfigured(db, enc):
    from app.sources import all_sources, enabled_sources
    keys = [s.key for s in all_sources()]
    assert keys[:2] == ["snaptrade", "schwab_api"] and "t212" in keys and "ibkr_flex" in keys
    assert all(s.key != "ibkr_flex" for s in enabled_sources())
    ibkr.save_credentials(db, TOKEN, QUERY_ID)
    db.commit()
    assert any(s.key == "ibkr_flex" for s in enabled_sources())


def test_source_key_and_arm_full_history(db, enc):
    from app.sync import arm_full_history, source_key_for_account
    a = Account(name="IB", broker="ibkr", source_key="ibkr_flex", external_ref="U1234567")
    db.add(a)
    db.commit()
    assert source_key_for_account(db, a) == "ibkr_flex"
    arm_full_history(db, a)
    assert get_state(db, ibkr.FULL_STATE) == "1"


# ---------------------------------------------------------------- rate safety (IBKR blocks hammering IPs)
SEND_1018 = ('<FlexStatementResponse><Status>Fail</Status><ErrorCode>1018</ErrorCode>'
             '<ErrorMessage>Too many requests have been made from this token.</ErrorMessage></FlexStatementResponse>')


def test_1018_trips_cooldown_and_blocks_further_calls(enc):
    fake = FakeFlex(send_xml=SEND_1018)
    res = ibkr.test_connection("tok-secret", "123", transport=fake.transport(), sleep=lambda _n: None)
    assert not res["ok"] and res["kind"] == "rate" and len(fake.calls) == 1
    assert ibkr.cooldown_left() > 60
    ibkr._GATE["last"] = 0.0
    again = ibkr.test_connection("tok-secret", "123", transport=fake.transport(), sleep=lambda _n: None)
    assert not again["ok"] and len(fake.calls) == 1  # nothing sent during the pause


def test_no_automatic_retries_and_call_cap(enc):
    def boom(req):
        boom.n += 1
        return httpx.Response(503)
    boom.n = 0
    res = ibkr.test_connection("tok", "1", transport=httpx.MockTransport(boom), sleep=lambda _n: None)
    assert not res["ok"] and boom.n == 1
    ibkr._GATE["last"] = 0.0
    fake = FakeFlex(get_bodies=[load("get_1019.xml")])  # never ready
    res = ibkr.test_connection("tok", "1", transport=fake.transport(), sleep=lambda _n: None)
    assert not res["ok"] and len(fake.calls) <= ibkr.MAX_CALLS


def test_send_gap_between_requests(enc, monkeypatch):
    monkeypatch.setattr(ibkr, "SEND_GAP", 15.0)
    fake = FakeFlex()
    assert ibkr.test_connection("tok", "1", transport=fake.transport(), sleep=lambda _n: None)["ok"]
    res = ibkr.test_connection("tok", "1", transport=fake.transport(), sleep=lambda _n: None)
    assert not res["ok"] and res["kind"] == "rate"


def test_tests_never_reach_real_ibkr(enc):
    res = ibkr.test_connection("tok", "1")  # no transport given
    assert not res["ok"]


def test_generic_sync_skips_ibkr(db, enc, monkeypatch):
    from app import sync
    calls = []

    class Src:
        def __init__(self, key):
            self.key = key
    srcs = [Src("t212"), Src("ibkr_flex")]
    only_none = [s.key for s in srcs if s.key != "ibkr_flex"]
    assert only_none == ["t212"]
    import inspect
    assert 's.key != "ibkr_flex"' in inspect.getsource(sync.execute_run)


# ---------------------------------------------------------------- multi-account + windowed history
TODAY = date(2026, 10, 16)
TINY = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U1" fromDate="20220101" toDate="20261016">
      <AccountInformation accountId="U1" currency="USD"/>
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="AAPL" tradeID="1"
               dateTime="20260902;100000" quantity="1" tradePrice="10" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
      </Trades>
      <CashTransactions>
        <CashTransaction accountId="U1" currency="USD" description="WIRE IN" dateTime="20260901;090000"
                         amount="100" type="Deposits/Withdrawals" transactionID="C1"/>
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
ERR1099 = ('<FlexStatementResponse><Status>Fail</Status><ErrorCode>1099</ErrorCode>'
           '<ErrorMessage>Query failed.</ErrorMessage></FlexStatementResponse>')


class ScriptedFlex:
    """MockTransport stand-in for the Flex Web Service. Never opens a socket."""

    def __init__(self, statement: str, send_bodies: list[str | None] | None = None,
                 get_bodies: list[str] | None = None):
        self.statement = statement
        self.send_bodies = list(send_bodies or [])
        self.get_bodies = list(get_bodies or [])
        self.sent: list[tuple[str | None, str | None]] = []
        self.n_get = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        if "SendRequest" in req.url.path:
            fd, td = req.url.params.get("fd"), req.url.params.get("td")
            assert (fd is None) == (td is None)
            assert req.url.params.get("p") is None
            self.sent.append((fd, td))
            body = self.send_bodies.pop(0) if self.send_bodies else None
            return httpx.Response(200, text=body or load("send_ok.xml"))
        self.n_get += 1
        if self.get_bodies:
            return httpx.Response(200, text=self.get_bodies.pop(0))
        return httpx.Response(200, text=self.statement)

    def transport(self):
        return httpx.MockTransport(self.handler)


def _freeze(monkeypatch):
    monkeypatch.setattr(ibkr, "_today", lambda: TODAY)
    monkeypatch.setattr(ibkr, "SEND_GAP", 15.0)


def _no_fx(monkeypatch):
    monkeypatch.setattr("app.currency.fetch_range", lambda *a, **k: 0)


def _no_ibkr_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("test tried to call IBKR")
    monkeypatch.setattr(ibkr.FlexClient, "fetch", boom)


def _client():
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_parse_multi_account_ignores_unknown_sections():
    stmts = ibkr.parse_statements(load("multi.xml"))
    assert [s.account_id for s in stmts] == ["U1111111", "U2222222", "U3333333"]
    assert [s.currency for s in stmts] == ["USD", "EUR", None]
    ids = {row.get("tradeID") for s in stmts for row in s.trades}
    assert ids == {"9001"}
    assert stmts[0].equity and stmts[0].equity["total"] == 2000
    assert any(r.get("symbol") == "ORCL" for r in stmts[0].positions)
    aapl = ibkr.trade_to_record(stmts[0].trades[0])
    assert isinstance(aapl, ExecRecord) and aapl.symbol == "AAPL" and aapl.quantity == 10
    symbols = {row.get("symbol") for s in stmts for row in s.trades + s.cash + s.positions}
    assert "GHOST" not in symbols and "OUTSIDE" not in symbols


def test_multi_account_creates_maps_and_dedupes(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    fake = FakeFlex(statement=load("multi.xml"))
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    res = src.sync(db, SyncContext())
    accts = list(db.scalars(select(Account).where(Account.source_key == ibkr.SOURCE_KEY)))
    by = {a.external_ref: a for a in accts}
    assert set(by) == {"U1111111", "U2222222", "U3333333"}
    assert by["U1111111"].name == "IBKR U1111111" and by["U1111111"].currency == "USD"
    assert by["U2222222"].name == "IBKR U2222222" and by["U2222222"].currency == "EUR"
    assert by["U3333333"].name == "IBKR U3333333" and by["U3333333"].currency == "USD"
    amap = ibkr.load_account_map(db)
    assert amap["U1111111"] == by["U1111111"].id
    trades = {(e.account_id, e.external_id): e.symbol for e in db.scalars(select(Execution).where(Execution.kind == "TRADE"))}
    assert trades[by["U1111111"].id, "9001"] == "AAPL"
    assert trades[by["U2222222"].id, "9001"] == "SAP"
    assert trades[by["U3333333"].id, "9001"] == "MSFT"
    ghost = [e for e in db.scalars(select(Execution)) if e.symbol == "GHOST"]
    assert len(ghost) == 1 and ghost[0].kind == "TRANSFER" and ghost[0].quantity == pytest.approx(1)
    assert db.query(Execution).filter(Execution.account_id == by["U1111111"].id,
                                      Execution.kind == "SPLIT", Execution.symbol == "IBM").count() == 1

    dep = list(db.scalars(select(CashFlow).where(CashFlow.account_id == by["U1111111"].id, CashFlow.kind == "deposit")))
    assert {round(f.amount, 2) for f in dep} == {500.0}
    div = db.scalar(select(Dividend).where(Dividend.account_id == by["U1111111"].id))
    assert div is not None and div.symbol == "AAPL" and div.amount == pytest.approx(5)
    assert db.query(Dividend).filter(Dividend.account_id == by["U2222222"].id).count() == 0
    interest = db.scalar(select(CashFlow).where(CashFlow.account_id == by["U2222222"].id, CashFlow.kind == "interest"))
    assert interest is not None and interest.amount == pytest.approx(1.5)
    out = db.scalar(select(CashFlow).where(CashFlow.account_id == by["U3333333"].id))
    assert out is not None and out.kind == "withdrawal" and out.amount == pytest.approx(-40)

    flags = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == by["U1111111"].id, CorporateAdjustment.status == "needs_attention")))
    assert any(a.symbol == "ORCL" for a in flags)
    ren = db.scalar(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == by["U2222222"].id, CorporateAdjustment.kind == "RENAME"))
    assert ren is not None and ren.symbol == "FB" and ren.new_symbol == "META"
    from app.balance import IBKR_SUMMARY_STATE
    assert "2000" in (get_state(db, IBKR_SUMMARY_STATE + str(by["U1111111"].id)) or "")
    for acct in by.values():
        assert db.query(BalancePoint).filter(BalancePoint.account_id == acct.id).count() > 0

    first_id = by["U1111111"].id
    c = _client()
    assert c.post(f"/settings/accounts/{first_id}", data={"name": "Personal margin"},
                  follow_redirects=False).status_code == 303
    db.rollback()
    personal = db.get(Account, first_id)
    assert personal.name == "Personal margin"
    personal.external_ref = None  # the map, not the name, has to find it again
    db.commit()
    res2 = src.sync(db, SyncContext())
    assert res2.inserted == 0
    db.rollback()
    rows = list(db.scalars(select(Account).where(Account.source_key == ibkr.SOURCE_KEY)))
    assert len(rows) == 3
    personal = db.get(Account, first_id)
    assert personal.name == "Personal margin" and personal.external_ref == "U1111111"
    assert ibkr.load_account_map(db)["U1111111"] == first_id
    home = c.get("/").text
    switch = home.split('id="acct-switch"', 1)[1].split("</select>", 1)[0]
    assert "Personal margin" in switch and "IBKR U2222222" in switch and "IBKR U3333333" in switch
    assert res.inserted > 0


def test_per_account_sync_one_fetch_one_statement(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    fake = FakeFlex(statement=load("multi.xml"))
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    for stmt in ibkr.parse_statements(load("multi.xml")):
        src.ensure_account(db, {"account_id": stmt.account_id, "currency": stmt.currency or "USD"})
    db.commit()
    u222 = db.scalar(select(Account).where(Account.external_ref == "U2222222"))
    src.sync(db, SyncContext(account_id=u222.id))
    sends = [c for c in fake.calls if "SendRequest" in c.url.path]
    assert len(sends) == 1
    assert sends[0].url.params.get("fd") and sends[0].url.params.get("td")
    assert sends[0].url.params.get("p") is None
    assert db.query(Execution).filter(Execution.account_id == u222.id, Execution.symbol == "SAP").count() == 1
    assert db.query(Execution).filter(Execution.account_id != u222.id).count() == 0
    assert db.query(CashFlow).filter(CashFlow.account_id != u222.id).count() == 0
    assert db.query(Dividend).filter(Dividend.account_id != u222.id).count() == 0


def test_windows_from_2022_have_no_gaps_and_fit_365_days():
    wins = ibkr.iter_windows(date(2022, 1, 1), TODAY)
    assert wins[0] == (date(2022, 1, 1), date(2022, 12, 31))
    assert wins[1] == (date(2023, 1, 1), date(2023, 12, 31))
    assert (date(2024, 1, 1), date(2024, 12, 30)) in wins
    assert (date(2024, 12, 31), date(2025, 12, 30)) in wins
    assert wins[-1] == (date(2025, 12, 31), TODAY)
    assert len(wins) == 5
    for i, (fd, td) in enumerate(wins):
        assert 1 <= (td - fd).days + 1 <= 365
        if i:
            assert fd == wins[i - 1][1] + timedelta(days=1)


def test_full_history_windows_are_sequential_and_paced(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    sleeps: list[float] = []
    saved: list[dict] = []
    real_save = ibkr.save_history

    def spy(sess, data):
        saved.append(dict(data))
        return real_save(sess, data)

    monkeypatch.setattr(ibkr, "save_history", spy)
    fake = ScriptedFlex(TINY, get_bodies=[load("get_1019.xml"), load("get_1019.xml")])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2022, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=sleeps.append, split_fetch=lambda *a: [])
    res = src.sync(db, SyncContext())
    expect = [(ibkr._ymd(a), ibkr._ymd(b)) for a, b in ibkr.iter_windows(date(2022, 1, 1), TODAY)]
    assert fake.sent == expect
    assert all(a <= b for a, b in fake.sent)
    assert sleeps == [ibkr.POLL_GAP, ibkr.POLL_GAP, ibkr.history_gap(4),
                      ibkr.history_gap(2), ibkr.history_gap(2), ibkr.history_gap(2)]
    assert ibkr.history_gap(4) >= 40 and ibkr.history_gap(2) >= 30
    assert [row["done_windows"] for row in saved] == [0, 1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert any(row["done_windows"] == 2 and row["next_fd"] == "2024-01-01" for row in saved)
    hist = ibkr.load_history(db)
    assert hist["status"] == "done" and hist["done_windows"] == 5 and hist["total_windows"] == 5
    assert hist["last_synced"] == TODAY.isoformat()
    assert "full history" in res.message and "5 windows" in res.message
    assert ibkr.progress_line(hist).startswith("window 5 of 5 done")


def test_1018_pauses_job_and_resume_skips_finished_windows(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    sleeps: list[float] = []
    fake = ScriptedFlex(TINY, send_bodies=[None, SEND_1018])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=sleeps.append, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    hist = ibkr.load_history(db)
    assert hist["status"] == "paused" and hist["done_windows"] == 1
    assert hist["next_fd"] == "2023-01-01" and hist.get("paused_until")
    assert len(fake.sent) == 2 and fake.n_get == 1  # the failed window was not retried
    assert sleeps == [ibkr.history_gap(2)]
    assert ibkr.cooldown_left() > 60
    during = len(fake.sent)
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())  # Sync while the cooldown is up: no HTTP
    assert len(fake.sent) == during
    ibkr._GATE.update(last=0.0, until=0.0)  # the 15-minute pause has elapsed; do not start over
    src.sync(db, SyncContext())
    resumed = fake.sent[during:]
    assert resumed and resumed[0] == ("20230101", "20231231")
    assert all(fd != "20220101" for fd, _td in resumed)
    hist = ibkr.load_history(db)
    assert hist["status"] == "done" and hist["done_windows"] == 5
    assert hist["next_fd"] == (TODAY + timedelta(days=1)).isoformat()


def test_failed_window_does_not_advance_the_cursor(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fake = ScriptedFlex(TINY, send_bodies=[None, ERR1099])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    hist = ibkr.load_history(db)
    assert hist["status"] == "error" and hist["done_windows"] == 1
    assert hist["next_fd"] == "2023-01-01"
    assert TOKEN not in json.dumps(hist)
    done_before = hist["done_windows"]
    ibkr._GATE.update(last=0.0, until=0.0)
    src.sync(db, SyncContext())
    assert fake.sent[2][0] == "20230101"
    assert all(fd != "20220101" for fd, _td in fake.sent[2:])
    hist = ibkr.load_history(db)
    assert hist["done_windows"] == 5 and hist["done_windows"] > done_before
    assert hist["status"] == "done"


def test_sync_new_starts_at_last_synced_minus_overlap(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fake = ScriptedFlex(TINY)
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "done", "last_synced": "2026-09-01", "next_fd": "2026-10-17",
        "done_windows": 4, "total_windows": 4, "start": "2022-01-01", "mode": "full",
    })
    db.commit()
    sleeps: list[float] = []
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=sleeps.append, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    assert fake.sent == [("20260828", "20261016")]
    assert sleeps == []  # one window: nothing to wait between
    hist = ibkr.load_history(db)
    assert hist["mode"] == "new" and hist["status"] == "done"
    assert hist["last_synced"] == TODAY.isoformat()


def test_card_shows_progress_and_how_to(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    monkeypatch.setattr(ibkr, "_TRANSPORT", FakeFlex(statement=load("multi.xml")).transport())
    c = _client()
    page = c.get("/settings").text
    assert "Selecting all fields" in page or "all fields" in page
    assert "several accounts" in page and "XML" in page
    assert "yyyyMMdd" in page and "HHmmss" in page
    assert "history start" in page.lower()
    assert "History start" in page and 'name="history_start"' in page
    assert 'name="files"' in page and "multiple" in page
    assert "processed one by one in the background" in page
    r = c.post("/settings/brokers/ibkr/connect",
               data={"token": TOKEN, "query_id": QUERY_ID, "currency": "USD"}, follow_redirects=True)
    assert "Interactive Brokers connected" in r.text and TOKEN not in r.text
    assert "IBKR U1111111" in r.text and "IBKR U2222222" in r.text and "IBKR U3333333" in r.text
    db.rollback()
    assert {a.currency for a in db.scalars(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))} == {"USD", "EUR"}
    r = c.post("/settings/brokers/ibkr/test", follow_redirects=True)
    assert "connection OK" in r.text and "U2222222" in r.text and TOKEN not in r.text
    called = []
    monkeypatch.setattr(ibkr, "test_connection", lambda *a, **k: called.append("x"))
    r = c.post("/settings/brokers/ibkr/history-start", data={"history_start": "2023-04-01"}, follow_redirects=True)
    assert called == [] and "2023-04-01" in r.text
    db.rollback()
    assert ibkr.history_start_date(db) == date(2023, 4, 1)
    ibkr.save_history(db, {
        "start": "2022-01-01", "next_fd": "2024-01-01", "done_windows": 3, "total_windows": 5,
        "status": "running", "last_synced": "2023-12-31", "mode": "full",
    })
    db.commit()
    page = c.get("/settings").text
    assert 'id="ibkr-progress"' in page
    assert "window 3 of 5 done, next 2024-01-01" in page
    run = SyncRun(trigger="manual", status="running", message="window 3 of 5 done, next 2024-01-01…")
    db.add(run)
    db.commit()
    body = c.get(f"/sync/{run.id}/status").text
    assert "Syncing" in body and "window 3 of 5 done, next 2024-01-01" in body


def test_ibkr_card_and_account_sync_are_the_only_callers(db, enc, monkeypatch):
    acct = Account(name="IBKR U1", broker="ibkr", source_key="ibkr_flex", external_ref="U1", currency="USD")
    db.add(acct)
    db.commit()
    seen = {}

    def fake(trigger="manual", only=None, account_id=None):
        seen.update(only=only, account_id=account_id)
        run = SyncRun(trigger=trigger, status="running", message="window 1 of 5 done, next 2023-01-01…")
        db.add(run)
        db.commit()
        return run.id

    monkeypatch.setattr("app.routes.settings.start_background", fake)
    monkeypatch.setattr("app.routes.brokers.start_background", fake)
    c = _client()
    assert c.post("/sync", data={"account": str(acct.id), "mode": "new"}).status_code == 200
    assert seen == {"only": {"ibkr_flex"}, "account_id": acct.id}
    db.rollback()
    assert get_state(db, ibkr.FULL_STATE) in (None, "")
    body = c.post("/sync", data={"account": str(acct.id), "mode": "full"}).text
    assert seen["only"] == {"ibkr_flex"} and seen["account_id"] == acct.id
    db.rollback()
    assert get_state(db, ibkr.FULL_STATE) == "1"
    assert "window 1 of 5 done" in body
    card = c.post("/settings/brokers/ibkr/sync")
    assert card.status_code == 200 and seen["only"] == {"ibkr_flex"} and seen["account_id"] is None
    assert "window 1 of 5 done" in card.text


def _flex_fail(code: str, message: str) -> str:
    return (f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode>"
            f"<ErrorMessage>{message}</ErrorMessage></FlexStatementResponse>")


# Official table: https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes.md
# 1003 is "statement not available" (not a bad token). 1099 stands in for any other code.
FLEX_CODES = [
    ("1001", "temporary", "Statement could not be generated at this time. Please try again shortly."),
    ("1003", "unavailable", "Statement is not available."),
    ("1004", "temporary", "Statement is incomplete. Please try again shortly."),
    ("1005", "temporary", "Settlement data is not ready."),
    ("1006", "temporary", "FIFO P/L data is not ready."),
    ("1007", "temporary", "MTM P/L data is not ready."),
    ("1008", "temporary", "MTM and FIFO P/L data is not ready."),
    ("1009", "temporary", "The server is under heavy load."),
    ("1010", "query", "Legacy Flex queries are not supported."),
    ("1011", "auth", "Service account is inactive."),
    ("1012", "auth", "Token has expired."),
    ("1013", "ip", "IP restriction."),
    ("1014", "query", "Query is invalid."),
    ("1015", "auth", "Token is invalid."),
    ("1016", "query", "Account is invalid."),
    ("1017", "api", "Reference code is invalid."),
    ("1018", "rate", "Too many requests have been made from this token."),
    ("1019", "temporary", "Statement generation in progress. Please try again shortly."),
    ("1020", "api", "Invalid request or unable to validate."),
    ("1021", "temporary", "Statement could not be retrieved. Please try again shortly."),
    ("1099", "api", "Query failed."),
]


@pytest.mark.parametrize("code,kind,message", FLEX_CODES)
def test_flex_error_code_maps_to_kind_and_message(enc, code, kind, message):
    """Each Flex code uses IBKR's kind. One SendRequest, no retry, no token in the text."""
    assert ibkr._kind_for_code(code) == kind
    fake = FakeFlex(send_xml=_flex_fail(code, message))
    res = ibkr.test_connection(TOKEN, QUERY_ID, transport=fake.transport(), sleep=nop_sleep)
    expected = f"IBKR error {code}: {message}"
    if code == "1013":
        expected += (" The token has an IP restriction; remove it in Client Portal → Flex Web Service, "
                     "or allow Render's outbound IPs.")
    assert res["kind"] == kind
    assert res["error"] == expected
    assert TOKEN not in (res["error"] or "")
    assert len(fake.calls) == 1  # no automatic retry
    if code == "1003":
        assert res["error"] == "IBKR error 1003: Statement is not available."
        assert "rejected the Flex token" not in res["error"]
    if code == "1015":
        assert res["kind"] == "auth"
    if code == "1018":
        assert ibkr.cooldown_left() > 60
    else:
        assert ibkr.cooldown_left() == 0


def test_get_statement_1004_is_temporary_and_not_retried(enc):
    fake = FakeFlex(get_bodies=[_flex_fail("1004", "Statement is incomplete. Please try again shortly.")])
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=nop_sleep)
    with pytest.raises(ibkr.FlexError) as ei:
        cl.fetch(QUERY_ID)
    cl.close()
    assert ei.value.kind == "temporary" and ei.value.code == "1004"
    assert str(ei.value) == "IBKR error 1004: Statement is incomplete. Please try again shortly."
    assert TOKEN not in str(ei.value)
    gets = [c for c in fake.calls if "GetStatement" in c.url.path]
    assert len(gets) == 1 and len(fake.calls) == 2  # send + one get, not the 1019 poll loop


def test_flex_error_log_has_code_not_token_or_url(enc, caplog):
    caplog.set_level(logging.DEBUG)
    leaked = (f"Token is invalid. {TOKEN} "
              f"https://ndcdyn.interactivebrokers.com/x?t={TOKEN}&amp;q=1")
    fake = FakeFlex(send_xml=_flex_fail("1015", leaked))
    res = ibkr.test_connection(TOKEN, QUERY_ID, transport=fake.transport(), sleep=nop_sleep)
    assert res["kind"] == "auth"
    assert res["error"].startswith("IBKR error 1015:")
    assert "Token is invalid" in res["error"]
    assert TOKEN not in res["error"]
    assert TOKEN not in caplog.text
    assert "t=" not in caplog.text
    assert "ndcdyn" not in caplog.text
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.message.startswith("IBKR Flex error 1015:") for r in warnings)


def test_1003_year_is_queued_and_next_window_imports(db, enc, monkeypatch, caplog):
    """1003 on a year window is recorded (no auto-retry). The next year still imports."""
    caplog.set_level(logging.DEBUG)
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    sleeps: list[float] = []
    fail = _flex_fail("1003", f"Statement is not available. {TOKEN}")
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2025, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=sleeps.append, split_fetch=lambda *a: [])
    res = src.sync(db, SyncContext())
    note = "IBKR 1003 for 20250101\u201320251231; will retry as quarters on the next Sync"
    hist = ibkr.load_history(db)
    assert fake.sent == [("20250101", "20251231"), ("20260101", "20261016")]
    assert fake.n_get == 1  # the 1003 window never fetched a statement
    assert sleeps == [ibkr.history_gap(1)]
    assert note in hist.get("notes", [])
    assert any(x.get("code") == "1003" and x.get("fd") == "2025-01-01" for x in hist.get("failed_windows") or [])
    assert note in ibkr.progress_line(hist)
    assert note in res.message
    assert hist["status"] == "done" and hist["done_windows"] == 2
    assert hist["last_synced"] == TODAY.isoformat()
    assert db.query(Execution).filter(Execution.symbol == "AAPL").count() == 1
    assert TOKEN not in json.dumps(hist) and TOKEN not in res.message and TOKEN not in caplog.text
    assert "t=" not in caplog.text and "ndcdyn" not in caplog.text
    assert any("1003" in r.message for r in caplog.records if r.levelno >= logging.WARNING)
    assert ibkr.cooldown_left() == 0


def test_1004_stops_job_and_keeps_cursor(db, enc, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fake = ScriptedFlex(TINY, send_bodies=[_flex_fail(
        "1004", f"Statement is incomplete. {TOKEN} Please try again shortly.")])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2025, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError) as ei:
        src.sync(db, SyncContext())
    msg = str(ei.value)
    assert msg == "IBKR error 1004: Statement is incomplete. [redacted] Please try again shortly."
    assert TOKEN not in msg and "rejected the Flex token" not in msg
    hist = ibkr.load_history(db)
    assert hist["status"] == "error" and hist["done_windows"] == 0
    assert hist["next_fd"] == "2025-01-01"
    assert not hist.get("paused_until")
    assert ibkr.cooldown_left() == 0
    assert fake.sent == [("20250101", "20251231")] and fake.n_get == 0
    assert TOKEN not in json.dumps(hist) and TOKEN not in caplog.text
    st = ibkr.last_status(db)
    assert st["error_kind"] == "temporary" and "1004" in st["error"] and TOKEN not in json.dumps(st)
    ibkr._GATE.update(last=0.0, until=0.0)
    src.sync(db, SyncContext())
    assert fake.sent[1] == ("20250101", "20250331")  # next press splits the year to quarters


def test_save_credentials_strips_whitespace_and_newlines(db, enc, monkeypatch):
    ibkr.save_credentials(db, f"  {TOKEN[:8]}\n{TOKEN[8:]}\r\n", f"{QUERY_ID[:2]} {QUERY_ID[2:]}\n", " usd ")
    db.commit()
    loaded = ibkr.load_credentials(db)
    assert loaded == {"token": TOKEN, "query_id": QUERY_ID, "currency": "USD"}
    seen: list[httpx.Request] = []
    base = FakeFlex()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return base.handler(req)

    monkeypatch.setattr(ibkr, "_TRANSPORT", httpx.MockTransport(handler))
    c = _client()
    pasted_token = "\n" + TOKEN[:6] + " \r\n" + TOKEN[6:] + "\n"
    pasted_query = "\t" + QUERY_ID[:3] + "\n" + QUERY_ID[3:] + " "
    r = c.post("/settings/brokers/ibkr/connect",
               data={"token": pasted_token, "query_id": pasted_query, "currency": "USD"},
               follow_redirects=True)
    assert "Interactive Brokers connected" in r.text and TOKEN not in r.text
    db.rollback()
    creds = ibkr.load_credentials(db)
    assert creds["token"] == TOKEN and creds["query_id"] == QUERY_ID
    sends = [req for req in seen if "SendRequest" in req.url.path]
    assert sends and sends[0].url.params.get("t") == TOKEN
    assert sends[0].url.params.get("q") == QUERY_ID
    assert TOKEN not in r.text


# ---------------------------------------------------------------- ibkr4: quarters, upload, conid, CA, parse extras
def _stmt(inner: str, account="U1") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<FlexQueryResponse>\n'
            f'<FlexStatements count="1"><FlexStatement accountId="{account}" '
            f'fromDate="20220101" toDate="20261016">\n'
            f'<AccountInformation accountId="{account}" currency="USD"/>\n'
            f'{inner}\n</FlexStatement></FlexStatements></FlexQueryResponse>')


def test_iter_quarters_are_at_most_92_days():
    q = ibkr.iter_quarters(date(2022, 1, 1), date(2022, 12, 31))
    assert q[0] == (date(2022, 1, 1), date(2022, 3, 31))
    assert q[1] == (date(2022, 4, 1), date(2022, 6, 30))
    assert q[-1] == (date(2022, 10, 1), date(2022, 12, 31))
    assert all(1 <= (td - fd).days + 1 <= 92 for fd, td in q)
    assert ibkr.is_year_sized(date(2022, 1, 1), date(2022, 12, 31))
    assert not ibkr.is_year_sized(date(2022, 1, 1), date(2022, 3, 31))
    assert ibkr.mask_account_id("U1234567") == "U***4567"


def test_1003_year_retried_as_quarters_then_quarter_gap(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1003", "Statement is not available.")
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2025, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    hist = ibkr.load_history(db)
    assert any(x.get("fd") == "2025-01-01" and x.get("code") == "1003" for x in hist["failed_windows"])
    ibkr._GATE.update(last=0.0, until=0.0)
    fake2 = ScriptedFlex(TINY, send_bodies=[fail])  # first quarter 1003; rest succeed
    src = ibkr.IbkrFlexSource(transport=fake2.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    expect_q = [(ibkr._ymd(a), ibkr._ymd(b)) for a, b in ibkr.iter_quarters(date(2025, 1, 1), date(2025, 12, 31))]
    assert fake2.sent[0] == expect_q[0]
    assert fake2.sent[:4] == expect_q
    hist = ibkr.load_history(db)
    assert any(g.get("fd") == "2025-01-01" for g in hist.get("gaps") or [])
    flags = list(db.scalars(select(CorporateAdjustment).where(CorporateAdjustment.status == "needs_attention")))
    assert any("history gap" in (a.note or "").lower() for a in flags)
    assert TOKEN not in json.dumps(hist)


def test_1001_is_recorded_and_retried_without_advancing(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2025, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    hist = ibkr.load_history(db)
    assert hist["status"] == "error" and hist["next_fd"] == "2025-01-01"
    fw = next(x for x in hist.get("failed_windows") or [] if x.get("code") == "1001")
    assert fw.get("split") == "quarter"
    ibkr._GATE.update(last=0.0, until=0.0)
    src.sync(db, SyncContext())
    assert fake.sent[1] == ("20250101", "20250331")  # next press: quarters, not the year again


def test_upload_xml_feeds_parser_and_dedupes(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    xml = load("statement.xml")
    res = src.import_xml(db, xml, filename="stmt.xml")
    assert res.inserted > 0
    n = db.query(Execution).count()
    payloads = list(db.scalars(select(SyncPayload)))
    xml_rows = [p for p in payloads if p.content_type == "application/xml"]
    assert xml_rows and xml_rows[0].gzipped
    from app.adjustments import payload_body
    raw = payload_body(xml_rows[0])
    assert raw.startswith(b"<?xml") or b"<FlexQueryResponse" in raw[:80]
    res2 = src.import_xml(db, xml, filename="stmt.xml")
    assert res2.inserted == 0
    assert db.query(Execution).count() == n


def test_upload_rejects_doctype_and_non_xml(db, enc, monkeypatch):
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(ibkr.FlexError):
        src.import_xml(db, '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x "y">]><x/>')
    c = _client()
    r = c.post("/settings/brokers/ibkr/upload", files={"file": ("note.txt", b"not xml", "text/plain")},
               follow_redirects=True)
    assert "XML" in r.text
    assert "DOCTYPE" not in r.text or "not" in r.text.lower()
    prog = ibkr.load_upload(db)
    assert prog.get("status") == "error"
    assert any((f.get("error") or "").lower() == "not xml" for f in prog.get("files") or [])


def test_conid_maps_beri_beril(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="BERIL" conid="80575143"
               listingExchange="LSE" tradeID="B1" dateTime="20260301;100000" quantity="8" tradePrice="10"
               ibCommission="0" buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="BERI" conid="80575143"
               listingExchange="LSE" tradeID="B2" dateTime="20260310;100000" quantity="-3" tradePrice="12"
               ibCommission="0" buySell="SELL" openCloseIndicator="C" levelOfDetail="EXECUTION"/>
      </Trades>
      <OpenPositions>
        <OpenPosition accountId="U1" currency="USD" assetCategory="STK" symbol="BERI" conid="80575143"
                      listingExchange="LSE" reportDate="20260320" position="5" markPrice="11"
                      levelOfDetail="SUMMARY"/>
      </OpenPositions>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    insts = list(db.scalars(select(Instrument).where(Instrument.source_key == ibkr.SOURCE_KEY)))
    assert len(insts) == 1 and insts[0].conid == "80575143"
    assert insts[0].symbol == "BERI" and insts[0].price_symbol == "BERI.L"
    execs = list(db.scalars(select(Execution).where(Execution.kind == "TRADE")))
    assert {e.instrument_id for e in execs} == {insts[0].id}
    assert {e.symbol for e in execs} == {"BERI"}
    assert {t.symbol for t in db.scalars(select(Trade))} == {"BERI"}
    flags = [a for a in db.scalars(select(CorporateAdjustment)) if a.status == "needs_attention"]
    assert not any("BERI" in (a.symbol or "") and "qty" in (a.external_key or "") for a in flags)
    insts[0].symbol = "BERIL"
    for ex in execs:
        ex.symbol = "BERIL"
        ex.underlying = "BERIL"
    from app.services import set_state
    set_state(db, ibkr.CONID_SYMBOL_STATE, json.dumps(
        {"80575143": {"symbol": "BERIL", "asof": "20990101", "prefer": True}}))
    db.commit()
    ibkr.reprocess_stored_payloads(db)
    inst = db.scalars(select(Instrument).where(Instrument.conid == "80575143")).one()
    assert inst.symbol == "BERI" and inst.price_symbol == "BERI.L"
    assert {e.symbol for e in db.scalars(select(Execution).where(Execution.kind == "TRADE"))} == {"BERI"}
    inst.user_override = True
    inst.symbol = "KEEP"
    db.commit()
    ibkr.reprocess_stored_payloads(db)
    inst = db.scalars(select(Instrument).where(Instrument.conid == "80575143")).one()
    assert inst.symbol == "KEEP"


def test_corporate_dedupe_by_action_id():
    rows = [
        {"symbol": "PARA", "conid": "1", "type": "TC", "quantity": "-17", "actionID": "A1",
         "levelOfDetail": "SUMMARY", "description": "X(PSKY, Y) 1 FOR 1", "dateTime": "20250806;200000"},
        {"symbol": "PARA", "conid": "1", "type": "TC", "quantity": "-17", "actionID": "A1",
         "levelOfDetail": "DETAIL", "description": "X(PSKY, Y) 1 FOR 1", "dateTime": "20250806;200000"},
        {"symbol": "PARA", "conid": "1", "type": "TC", "quantity": "-17", "actionID": "A1",
         "levelOfDetail": "SUMMARY", "description": "X(PSKY, Y) 1 FOR 1", "dateTime": "20250806;200000"},
        {"symbol": "PSKY", "conid": "2", "type": "TC", "quantity": "17", "actionID": "A1",
         "levelOfDetail": "DETAIL", "description": "X(PSKY, Y) 1 FOR 1", "dateTime": "20250806;200000"},
        {"symbol": "PSKY", "conid": "2", "type": "TC", "quantity": "17", "actionID": "A1",
         "levelOfDetail": "SUMMARY", "description": "X(PSKY, Y) 1 FOR 1", "dateTime": "20250806;200000"},
    ]
    got = ibkr.dedupe_corporate(rows)
    assert len(got) == 2
    assert {ibkr._lod(r) for r in got} == {"DETAIL"}
    events, recs = ibkr.corporate_events(rows)
    sells = [r for r in recs if r.side == "SELL"]
    buys = [r for r in recs if r.side == "BUY"]
    assert len(sells) == 1 and len(buys) == 1
    assert sells[0].symbol == "PARA" and buys[0].symbol == "PSKY"
    assert any(e.get("kind") == "RENAME" and e.get("new_symbol") == "PSKY" for e in events)


def test_para_psky_one_for_one_carries_cost(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="PARA" conid="393897513"
               tradeID="P1" dateTime="20250701;100000" quantity="17" tradePrice="20" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
      </Trades>
      <CorporateActions>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="PARA" conid="393897513"
          type="TC" quantity="-17" actionID="M1" levelOfDetail="DETAIL" dateTime="20250806;200000"
          description="PARAMOUNT GLOBAL(PARA) MERGED WITH PARAMOUNT SKYDANCE(PSKY, US123) 1 FOR 1"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="PSKY" conid="804144296"
          type="TC" quantity="17" actionID="M1" levelOfDetail="DETAIL" dateTime="20250806;200000"
          description="PARAMOUNT GLOBAL(PARA) MERGED WITH PARAMOUNT SKYDANCE(PSKY, US123) 1 FOR 1"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="PARA" conid="393897513"
          type="TC" quantity="-17" actionID="M1" levelOfDetail="SUMMARY" dateTime="20250806;200000"
          description="PARAMOUNT GLOBAL(PARA) MERGED WITH PARAMOUNT SKYDANCE(PSKY, US123) 1 FOR 1"/>
      </CorporateActions>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    conv = list(db.scalars(select(Execution).where(Execution.kind == "CONVERSION")))
    assert len(conv) == 2
    buy = next(e for e in conv if e.side == "BUY")
    sell = next(e for e in conv if e.side == "SELL")
    assert buy.symbol == "PSKY" and sell.symbol == "PARA"
    assert buy.quantity == pytest.approx(17) and sell.quantity == pytest.approx(17)
    assert buy.price == pytest.approx(20)
    trades = list(db.scalars(select(Trade)))
    psky = next(t for t in trades if t.symbol == "PSKY" and t.status == "OPEN")
    assert psky.open_quantity == pytest.approx(17)
    assert psky.entry_price == pytest.approx(20) or psky.cost_basis == pytest.approx(340)
    para = next((t for t in trades if t.symbol == "PARA"), None)
    if para is not None:
        assert para.status == "CLOSED"
        assert abs(para.net_pnl or 0) < 1e-4 or abs(para.gross_pnl or 0) < 1e-4


def test_hbi_gil_cash_and_stock(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="HBI" conid="40369429"
               tradeID="H1" dateTime="20251001;100000" quantity="15" tradePrice="10" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
      </Trades>
      <CorporateActions>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="HBI" conid="40369429"
          type="TC" quantity="-15" proceeds="12.00" actionID="HBI1" levelOfDetail="DETAIL"
          dateTime="20251201;200000"
          description="HANESBRANDS(HBI) MERGED WITH GILDAN(GIL, CA) 102 FOR 1000 AND USD 0.80"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="GIL" conid="6227314"
          type="TC" quantity="1.53" actionID="HBI1" levelOfDetail="DETAIL" dateTime="20251201;200000"
          description="HANESBRANDS(HBI) MERGED WITH GILDAN(GIL, CA) 102 FOR 1000 AND USD 0.80"/>
      </CorporateActions>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    buy = next(e for e in db.scalars(select(Execution).where(Execution.kind == "CONVERSION", Execution.side == "BUY")))
    assert buy.symbol == "GIL" and buy.quantity == pytest.approx(1.53)
    assert buy.price == pytest.approx(150 / 1.53)
    cash = list(db.scalars(select(CashFlow).where(CashFlow.kind == "proceeds")))
    assert len(cash) == 1 and cash[0].amount == pytest.approx(12.0)


def test_merger_missing_buy_flags_and_does_not_invent(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <CorporateActions>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="PARA" conid="393897513"
          type="TC" quantity="-17" actionID="M9" levelOfDetail="DETAIL" dateTime="20250806;200000"
          description="PARA MERGED WITH PSKY(PSKY, US) 1 FOR 1"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="PSKY" conid="804144296"
          type="TC" quantity="17" actionID="M9" levelOfDetail="DETAIL" dateTime="20250806;200000"
          description="PARA MERGED WITH PSKY(PSKY, US) 1 FOR 1"/>
      </CorporateActions>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    buys = [e for e in db.scalars(select(Execution)) if e.side == "BUY" and e.kind == "TRADE"]
    assert buys == []
    flags = list(db.scalars(select(CorporateAdjustment).where(CorporateAdjustment.status == "needs_attention")))
    assert any("outside" in (a.note or "").lower() or "opening fill" in (a.note or "").lower() for a in flags)
    assert any(a.kind == "RENAME" or (a.new_symbol == "PSKY") for a in
               db.scalars(select(CorporateAdjustment)))
    psky = [e for e in db.scalars(select(Execution)) if e.symbol == "PSKY"]
    assert psky and psky[0].kind == "CONVERSION"


def test_fs_rs_so_sd_dw_types():
    rows = [
        {"symbol": "AAA", "type": "FS", "description": "AAA SPLIT 2 FOR 1", "quantity": "2",
         "dateTime": "20260101;200000", "actionID": "1"},
        {"symbol": "BBB", "type": "RS", "description": "BBB REVERSE SPLIT 1 FOR 10", "quantity": "-9",
         "dateTime": "20260102;200000", "actionID": "2"},
        {"symbol": "CCC", "type": "SO", "description": "CCC SPIN-OFF OF DDD(DDD, US1)", "quantity": "4",
         "dateTime": "20260103;200000", "actionID": "3", "conid": "30"},
        {"symbol": "EEE", "type": "SD", "description": "EEE STOCK DIVIDEND", "quantity": "2",
         "dateTime": "20260104;200000", "actionID": "4"},
        {"symbol": "FFF", "type": "DW", "description": "FFF WORTHLESS", "quantity": "-5",
         "dateTime": "20260105;200000", "actionID": "5"},
        {"symbol": "GGG", "type": "ZZ", "description": "mystery", "quantity": "1",
         "dateTime": "20260106;200000", "actionID": "6"},
    ]
    events, recs = ibkr.corporate_events(rows)
    kinds = {r.kind for r in recs}
    assert "SPLIT" in kinds and "SPIN_OFF" in kinds and "DISTRIBUTION" in kinds and "ADJUST" in kinds
    assert any(e["kind"] == "ATTENTION" and e["symbol"] == "GGG" for e in events)
    so = next(r for r in recs if r.kind == "SPIN_OFF")
    assert so.symbol == "DDD" and so.quantity == pytest.approx(4)


def test_equity_and_cash_report_balance_check(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="AAPL" conid="265598"
               tradeID="1" dateTime="20260902;100000" quantity="1" tradePrice="10" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
      </Trades>
      <CashTransactions>
        <CashTransaction accountId="U1" currency="USD" description="WIRE IN" dateTime="20260901;090000"
                         amount="100" type="Deposits/Withdrawals" transactionID="C1"/>
      </CashTransactions>
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase accountId="U1" reportDate="20261014" cash="40" stock="60" total="100"/>
      </EquitySummaryInBase>
      <CashReport>
        <CashReportCurrency accountId="U1" currency="USD" endingCash="42" toDate="20261014" levelOfDetail="Currency"/>
      </CashReport>""")
    parsed = ibkr.parse_statement(xml)
    assert parsed.equity and parsed.equity["total"] == 100
    assert parsed.cash_report and ibkr._latest_cash_report(parsed.cash_report) == pytest.approx(42)
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    from app.balance import IBKR_SUMMARY_STATE
    acct = db.scalar(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))
    raw = get_state(db, IBKR_SUMMARY_STATE + str(acct.id))
    snap = json.loads(raw)
    assert snap["total"] == 100 and snap["cash"] == pytest.approx(42)
    c = _client()
    page = c.get("/settings").text
    assert "journal NAV" in page and "IBKR" in page


def test_option_eae_and_skip_duplicate_trade_code():
    eae = {"transactionType": "Expiration", "symbol": "AAPL  260117C00150000", "underlyingSymbol": "AAPL",
           "quantity": "2", "dateTime": "20260117;160000", "transactionID": "E1", "putCall": "C",
           "strike": "150", "expiry": "20260117", "multiplier": "100"}
    recs = [r for r in ibkr.option_eae_to_records(eae) if not isinstance(r, ibkr.Skip)]
    assert recs and recs[0].kind == "EXPIRATION" and recs[0].asset_type == "OPTION"
    covered = {("AAPL  260117C00150000", date(2026, 1, 17), 2.0)}
    day = (ibkr._when_of(eae)[0] or datetime.min).date()
    assert ((eae.get("symbol") or "").upper(), day, round(abs(ibkr._num(eae.get("quantity")) or 0), 4)) in covered
    assert "EP" in ibkr._EAE_TRADE_CODES


def test_option_eae_skipped_when_trade_has_code(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="OPT" symbol="AAPL  260117C00150000"
               underlyingSymbol="AAPL" tradeID="T9" dateTime="20260117;160000" quantity="-2" tradePrice="0"
               ibCommission="0" buySell="SELL" openCloseIndicator="C" putCall="C" strike="150"
               expiry="20260117" multiplier="100" levelOfDetail="EXECUTION" code="Ep"/>
      </Trades>
      <OptionEAE>
        <OptionEAE accountId="U1" transactionType="Expiration" symbol="AAPL  260117C00150000"
                   underlyingSymbol="AAPL" quantity="2" dateTime="20260117;160000" transactionID="E1"
                   putCall="C" strike="150" expiry="20260117" multiplier="100"/>
      </OptionEAE>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    eae = [e for e in db.scalars(select(Execution)) if str(e.external_id).startswith("eae:")]
    assert eae == []
    assert db.query(Execution).filter(Execution.external_id == "T9").count() == 1


def test_co_indicator_splits_close_and_open(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="TSLA" conid="76792991"
               tradeID="T1" dateTime="20260903;093100" quantity="10" tradePrice="250" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="TSLA" conid="76792991"
               tradeID="T2" dateTime="20260908;160000" quantity="-15" tradePrice="240" ibCommission="-1.50"
               buySell="SELL" openCloseIndicator="C;O" levelOfDetail="EXECUTION"/>
      </Trades>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    tsla = list(db.scalars(select(Execution).where(Execution.symbol == "TSLA", Execution.kind == "TRADE")))
    tsla.sort(key=lambda e: (e.executed_at, e.external_id))
    assert [e.external_id for e in tsla] == ["T1", "T2", "T2:O"]
    assert tsla[1].quantity == pytest.approx(10) and tsla[1].position_effect == "CLOSE"
    assert tsla[2].quantity == pytest.approx(5) and tsla[2].position_effect == "OPEN"
    trades = [t for t in db.scalars(select(Trade)) if t.symbol == "TSLA"]
    assert any(t.status == "CLOSED" and t.direction == "LONG" for t in trades)
    assert any(t.status == "OPEN" and t.direction == "SHORT" for t in trades)


def test_reprocess_is_idempotent(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = load("statement.xml")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    n_exec = db.query(Execution).count()
    n_div = db.query(Dividend).count()
    n_cash = db.query(CashFlow).count()
    res = ibkr.reprocess_stored_payloads(db)
    assert "Reprocessed" in res.message
    assert db.query(Execution).count() == n_exec
    assert db.query(Dividend).count() == n_div
    assert db.query(CashFlow).count() == n_cash
    res2 = ibkr.reprocess_stored_payloads(db)
    assert db.query(Execution).count() == n_exec
    assert get_state(db, ibkr.REPROCESS_STATE) == ibkr.REPROCESS_VERSION


def test_upload_form_on_card(db, enc, monkeypatch):
    c = _client()
    page = c.get("/settings").text
    assert "Upload IBKR Activity Statement (XML)" in page
    assert 'enctype="multipart/form-data"' in page
    assert "/settings/brokers/ibkr/upload" in page
    assert "Reprocess stored history" in page or "ibkr/reprocess" in page


def _merger_xml(actions: str, trades: str = "") -> str:
    body = ""
    if trades:
        body += f"<Trades>{trades}</Trades>\n"
    body += f"<CorporateActions>{actions}</CorporateActions>"
    return _stmt(body)


def test_tc_merger_is_one_direction_and_reprocess_drops_the_reverse(db, enc, monkeypatch):
    """Cancelled reverse legs and a second action id must not book new→old."""
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    desc = "OLDQ(US111) MERGED(Acquisition) WITH US222 1 FOR 1 (NEWQ, EXAMPLE, US222)"
    rows = [
        {"symbol": "OLDQ", "conid": "111", "type": "TC", "quantity": "10", "code": "Ca",
         "actionID": "REV1", "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
        {"symbol": "NEWQ", "conid": "222", "type": "TC", "quantity": "-10", "code": "Ca",
         "actionID": "REV1", "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
        {"symbol": "OLDQ", "conid": "111", "type": "TC", "quantity": "-10", "actionID": "OK1",
         "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
        {"symbol": "NEWQ", "conid": "222", "type": "TC", "quantity": "10", "actionID": "OK1",
         "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
        {"symbol": "OLDQ", "conid": "111", "type": "TC", "quantity": "-10", "actionID": "OK2",
         "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
        {"symbol": "NEWQ", "conid": "222", "type": "TC", "quantity": "10", "actionID": "OK2",
         "levelOfDetail": "DETAIL", "dateTime": "20250806;200000", "description": desc},
    ]
    events, recs = ibkr.corporate_events(rows)
    sells = [r for r in recs if r.side == "SELL"]
    buys = [r for r in recs if r.side == "BUY"]
    assert len(sells) == 1 and sells[0].symbol == "OLDQ" and sells[0].quantity == pytest.approx(10)
    assert len(buys) == 1 and buys[0].symbol == "NEWQ" and buys[0].quantity == pytest.approx(10)
    renames = [e for e in events if e.get("kind") == "RENAME"]
    assert len(renames) == 1 and renames[0]["symbol"] == "OLDQ" and renames[0]["new_symbol"] == "NEWQ"
    assert not any(e.get("symbol") == "NEWQ" and e.get("new_symbol") == "OLDQ" for e in events)

    actions = "".join(
        f'<CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="{r["symbol"]}" '
        f'conid="{r["conid"]}" type="TC" quantity="{r["quantity"]}" actionID="{r["actionID"]}" '
        f'levelOfDetail="DETAIL" dateTime="20250806;200000" description="{desc}"'
        + (f' code="{r["code"]}"' if r.get("code") else "") + "/>"
        for r in rows)
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, _merger_xml(actions))
    acct = db.scalar(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))
    directions = {(a.symbol, a.new_symbol) for a in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct.id, CorporateAdjustment.kind == "RENAME",
        CorporateAdjustment.status == "applied"))}
    assert directions == {("OLDQ", "NEWQ")}
    conv = list(db.scalars(select(Execution).where(Execution.kind == "CONVERSION")))
    assert len(conv) == 2
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="NEWQ", new_symbol="OLDQ", kind="RENAME", status="applied",
        source="broker", external_key="rename:NEWQ:OLDQ:2025-08-06",
        effective_on=datetime(2025, 8, 6, 20, 0), note="NEWQ renamed to OLDQ"))
    from app.services import set_state
    from app.symbols import AUTO_STATE, load_aliases
    set_state(db, AUTO_STATE, json.dumps({"NEWQ": {"to": "OLDQ", "evidence": "stored reverse"}}))
    from app.instruments import ExecRecord
    from app.services import ingest_records
    ingest_records(db, acct.id, ibkr.SOURCE_KEY, [
        ExecRecord(external_id="ca:REV1:111:O", symbol="OLDQ", underlying="OLDQ", asset_type="STOCK",
                   side="BUY", quantity=10, price=1, executed_at=datetime(2025, 8, 6, 20, 0),
                   kind="CONVERSION", position_effect="OPEN"),
        ExecRecord(external_id="ca:REV1:222:C", symbol="NEWQ", underlying="NEWQ", asset_type="STOCK",
                   side="SELL", quantity=10, price=1, executed_at=datetime(2025, 8, 6, 20, 0),
                   kind="CONVERSION", position_effect="CLOSE"),
    ])
    db.commit()
    n_exec = db.query(Execution).count()
    ibkr.reprocess_stored_payloads(db)
    directions = {(a.symbol, a.new_symbol) for a in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct.id, CorporateAdjustment.kind == "RENAME",
        CorporateAdjustment.status == "applied"))}
    assert directions == {("OLDQ", "NEWQ")}
    assert load_aliases(db).get("NEWQ") != "OLDQ"
    assert load_aliases(db).get("OLDQ") == "NEWQ"
    assert db.scalar(select(Execution).where(Execution.external_id == "ca:REV1:111:O")) is None
    assert db.query(Execution).count() == n_exec - 2
    again = db.query(Execution).count()
    ibkr.reprocess_stored_payloads(db)
    assert db.query(Execution).count() == again
    assert {(a.symbol, a.new_symbol) for a in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.kind == "RENAME", CorporateAdjustment.status == "applied"))} == {("OLDQ", "NEWQ")}


def test_cash_and_stock_merger_carries_basis_and_cash(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    desc = "CASHQ(US410) CASH and STOCK MERGER (Acquisition) CA999 102 FOR 1000 AND USD 0.80"
    actions = f"""
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="CASHQ" conid="40369429"
          type="TC" quantity="-15" proceeds="12.00" amount="-12" actionID="M1" levelOfDetail="DETAIL"
          dateTime="20251201;200000" description="{desc}"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="STKQ" conid="6227314"
          type="TC" quantity="1.53" actionID="M1" levelOfDetail="DETAIL" dateTime="20251201;200000"
          description="{desc}"/>
    """
    trades = """
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="CASHQ" conid="40369429"
               tradeID="H1" dateTime="20251001;100000" quantity="15" tradePrice="10" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
    """
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, _merger_xml(actions, trades))
    acct = db.scalar(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))
    merger = db.scalar(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct.id, CorporateAdjustment.kind == "MERGER"))
    assert merger is not None and merger.symbol == "CASHQ" and merger.new_symbol == "STKQ"
    assert merger.ratio == pytest.approx(0.102)
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "RENAME")) is None
    buy = db.scalar(select(Execution).where(Execution.kind == "CONVERSION", Execution.side == "BUY"))
    assert buy.symbol == "STKQ" and buy.quantity == pytest.approx(1.53)
    assert buy.price == pytest.approx(150 / 1.53)
    sell = db.scalar(select(Execution).where(Execution.kind == "CONVERSION", Execution.side == "SELL"))
    assert sell.symbol == "CASHQ" and sell.quantity == pytest.approx(15)
    cash = list(db.scalars(select(CashFlow).where(CashFlow.kind == "proceeds")))
    assert len(cash) == 1 and cash[0].amount == pytest.approx(12.0)
    assert cash[0].occurred_at.date().isoformat() == "2025-12-01"
    closed = db.scalar(select(Trade).where(Trade.symbol == "CASHQ"))
    assert closed is not None and closed.status == "CLOSED"
    assert abs(closed.net_pnl or 0) < 1e-4
    opened = db.scalar(select(Trade).where(Trade.symbol == "STKQ", Trade.status == "OPEN"))
    assert opened.open_quantity == pytest.approx(1.53)
    assert opened.entry_price == pytest.approx(150 / 1.53)
    assert opened.cost_basis == pytest.approx(150, abs=0.01)
    # A plain rename and stale merger warnings from an older import are removed, once.
    db.add_all([
        CorporateAdjustment(account_id=acct.id, symbol="CASHQ", new_symbol="STKQ", kind="RENAME",
                           status="applied", source="broker", external_key="rename:CASHQ:STKQ:2025-12-01",
                           note="CASHQ renamed to STKQ"),
        CorporateAdjustment(account_id=acct.id, symbol="CASHQ", kind="CHECK", status="needs_attention",
                           source="broker", external_key="attn:CASHQ:merger",
                           note="CASHQ: IBKR merger/acquisition needs attention (cash and stock)."),
        CorporateAdjustment(account_id=acct.id, symbol="STKQ", kind="CHECK", status="needs_attention",
                           source="broker", external_key="attn:STKQ:merger",
                           note="STKQ: IBKR merger/acquisition needs attention (cash and stock)."),
        CorporateAdjustment(account_id=acct.id, symbol="CASHQ", kind="CHECK", status="needs_attention",
                           source="broker", external_key="attn:CASHQ:outside",
                           note="CASHQ: opening fill is before 2022-01-01 (outside imported history)."),
        CorporateAdjustment(account_id=acct.id, symbol="CASHQ", kind="CHECK", status="needs_attention",
                           source="broker", external_key="attn:CASHQ:outside2",
                           note="CASHQ: opening fill is outside imported history."),
        CorporateAdjustment(account_id=acct.id, symbol="STKQ", kind="CHECK", status="needs_attention",
                           source="broker", external_key="attn:STKQ:outside",
                           note="STKQ: opening fill is before 2022-01-01 (outside imported history)."),
    ])
    from app.services import set_state
    from app.symbols import AUTO_STATE, load_aliases
    set_state(db, AUTO_STATE, json.dumps({"CASHQ": {"to": "STKQ", "evidence": "old rename"}}))
    db.commit()
    ibkr.reprocess_stored_payloads(db)
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "RENAME")) is None
    assert load_aliases(db).get("CASHQ") != "STKQ"
    notes = [a.note or "" for a in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.kind == "CHECK", CorporateAdjustment.status == "needs_attention"))]
    assert not any("merger/acquisition needs attention" in n.lower() for n in notes)
    assert not any("opening fill" in n.lower() for n in notes)
    merger = db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "MERGER"))
    assert merger.ratio == pytest.approx(0.102) and merger.new_symbol == "STKQ"
    n_exec = db.query(Execution).count()
    n_cash = db.query(CashFlow).count()
    ibkr.reprocess_stored_payloads(db)
    assert db.query(Execution).count() == n_exec
    assert db.query(CashFlow).count() == n_cash
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "MERGER")).ratio == pytest.approx(0.102)


def test_cash_leg_from_description_when_proceeds_absent():
    desc = "CASHQ(US1) CASH and STOCK MERGER (Acquisition) CA1 102 FOR 1000 AND USD 0.80"
    rows = [
        {"symbol": "CASHQ", "conid": "1", "type": "TC", "quantity": "-15", "actionID": "M",
         "levelOfDetail": "DETAIL", "dateTime": "20251201;200000", "description": desc},
        {"symbol": "STKQ", "conid": "2", "type": "TC", "quantity": "1.53", "actionID": "M",
         "levelOfDetail": "DETAIL", "dateTime": "20251201;200000", "description": desc},
    ]
    events, recs = ibkr.corporate_events(rows)
    cash = next(e for e in events if e["kind"] == "CASH")
    assert cash["amount"] == pytest.approx(12.0)
    merger = next(e for e in events if e["kind"] == "MERGER")
    assert merger["ratio"] == pytest.approx(0.102) and merger["new_symbol"] == "STKQ"
    assert recs and next(r for r in recs if r.side == "BUY").quantity == pytest.approx(1.53)
    assert not any(e["kind"] == "RENAME" for e in events)


def test_merger_without_opening_keeps_one_history_note(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    ibkr.save_history(db, {
        "start": "2022-01-01", "end": "2026-10-16",
        "notes": ["no statement for 20220101\u201320221231 (IBKR 1003)"],
        "gaps": [{"fd": "2022-01-01", "td": "2022-12-31", "code": "1003"}],
    })
    desc = "OLDQ(US111) MERGED(Acquisition) WITH US222 1 FOR 1 (NEWQ, EXAMPLE, US222)"
    actions = f"""
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="OLDQ" conid="111"
          type="TC" quantity="-10" actionID="M9" levelOfDetail="DETAIL" dateTime="20240601;200000"
          description="{desc}"/>
        <CorporateAction accountId="U1" currency="USD" assetCategory="STK" symbol="NEWQ" conid="222"
          type="TC" quantity="10" actionID="M9" levelOfDetail="DETAIL" dateTime="20240601;200000"
          description="{desc}"/>
    """
    trades = """
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="KEEP" conid="555"
               tradeID="K1" dateTime="20230104;100000" quantity="1" tradePrice="5" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="ZORP" conid="556"
               tradeID="Z1" dateTime="20240201;100000" quantity="-4" tradePrice="8" ibCommission="0"
               buySell="SELL" openCloseIndicator="C" levelOfDetail="EXECUTION"/>
    """
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, _merger_xml(actions, trades))
    acct = db.scalar(select(Account).where(Account.source_key == ibkr.SOURCE_KEY))
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="OLDQ", kind="CHECK", status="needs_attention", source="broker",
        external_key="attn:OLDQ:outside-dup",
        note="OLDQ: opening fill is before 2022-01-01 (outside imported history)."))
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="NEWQ", kind="CHECK", status="needs_attention", source="broker",
        external_key="attn:NEWQ:merger",
        note="NEWQ: IBKR merger/acquisition needs attention (merged)."))
    db.commit()
    ibkr.reprocess_stored_payloads(db)
    flags = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct.id, CorporateAdjustment.status == "needs_attention")))
    opening = [a for a in flags if a.symbol == "OLDQ" and "opening fill" in (a.note or "").lower()]
    assert len(opening) == 1
    note = opening[0].note
    assert "before 2023-01-04 (earliest imported IBKR data)" in note
    assert "2022 had no statement (IBKR 1003) and can be uploaded as XML." in note
    assert "before 2022-01-01" not in note
    assert "2026 had no statement" not in note
    assert not any("merger/acquisition needs attention" in (a.note or "").lower() for a in flags)
    assert not any(a.symbol == "NEWQ" and "opening fill" in (a.note or "").lower() for a in flags)
    assert not any(e.kind == "TRADE" and e.symbol == "OLDQ" and e.side == "BUY"
                   for e in db.scalars(select(Execution)))
    newq = [e for e in db.scalars(select(Execution)) if e.symbol == "NEWQ"]
    assert newq and newq[0].kind == "CONVERSION" and newq[0].quantity == pytest.approx(10)
    zorp = next(a for a in flags if a.symbol == "ZORP")
    assert "before 2023-01-04 (earliest imported IBKR data)" in (zorp.note or "")
    assert "closing fill with no open position" in (zorp.note or "")
    assert "2022 had no statement (IBKR 1003)" in (zorp.note or "")
    # A second pass does not duplicate the note.
    ibkr.reprocess_stored_payloads(db)
    opening = [a for a in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.status == "needs_attention"))
               if a.symbol == "OLDQ" and "opening fill" in (a.note or "").lower()]
    assert len(opening) == 1


# ---------------------------------------------------------------- ibkr6: td cap, 1001 split, ignore Url
NY = ZoneInfo("America/New_York")
LEGACY_SEND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<FlexStatementResponse><Status>Success</Status>'
    '<ReferenceCode>9876543210</ReferenceCode>'
    '<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/'
    'FlexStatementService.GetStatement?t=LEAKEDTOKEN</Url>'
    '</FlexStatementResponse>'
)


def _freeze_now(monkeypatch, dt: datetime):
    monkeypatch.setattr(ibkr, "_now_ny", lambda: dt)
    monkeypatch.setattr(ibkr, "SEND_GAP", 15.0)


def test_td_capped_last_complete_us_business_day():
    cases = [
        (datetime(2026, 10, 17, 12, 0, tzinfo=NY), date(2026, 10, 16)),  # Saturday
        (datetime(2026, 10, 18, 18, 0, tzinfo=NY), date(2026, 10, 16)),  # Sunday
        (datetime(2026, 10, 19, 8, 15, tzinfo=NY), date(2026, 10, 16)),  # Monday morning
        (datetime(2026, 10, 14, 15, 30, tzinfo=NY), date(2026, 10, 13)),  # Wednesday → Tuesday
        (datetime(2026, 10, 16, 9, 0, tzinfo=NY), date(2026, 10, 15)),  # Friday → Thursday
    ]
    for now, expect in cases:
        assert ibkr.last_complete_biz_day(now) == expect, now
    naive = datetime(2026, 10, 17, 12, 0)
    assert ibkr.last_complete_biz_day(naive) == date(2026, 10, 16)


def test_full_history_never_sends_td_past_cap(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    fake = ScriptedFlex(TINY)
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2022, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    assert fake.sent
    assert all(td <= "20261016" for _fd, td in fake.sent)
    assert fake.sent[-1] == ("20251231", "20261016")
    hist = ibkr.load_history(db)
    assert hist["end"] == "2026-10-16" and hist["last_synced"] == "2026-10-16"


def test_up_to_date_when_fd_after_cap(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    fake = ScriptedFlex(TINY)
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "running", "last_synced": "2026-10-16", "next_fd": "2026-10-17",
        "seq_fd": "2026-10-17", "done_windows": 5, "total_windows": 5,
        "start": "2022-01-01", "mode": "full", "end": "2026-10-16",
        "failed_windows": [{"fd": "2026-10-17", "td": "2026-10-17", "code": "1001"}],
    })
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    res = src.sync(db, SyncContext())
    assert fake.sent == []
    assert "Up to date through 2026-10-16" in res.message


def test_1001_year_next_press_quarters_stop_at_first_failure(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history_start(db, date(2025, 1, 1))
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    sleeps: list[float] = []
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=sleeps.append, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    assert fake.sent == [("20250101", "20251231")]  # no automatic retry
    hist = ibkr.load_history(db)
    fw = hist["failed_windows"][0]
    assert fw["code"] == "1001" and fw["split"] == "quarter"
    ibkr._GATE.update(last=0.0, until=0.0)
    quarters = ibkr.iter_quarters(date(2025, 1, 1), date(2025, 12, 31))
    fake2 = ScriptedFlex(TINY, send_bodies=[None, fail])  # Q1 ok, Q2 1001
    sleeps2: list[float] = []
    src = ibkr.IbkrFlexSource(transport=fake2.transport(), sleep=sleeps2.append, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    assert fake2.sent == [(ibkr._ymd(a), ibkr._ymd(b)) for a, b in quarters[:2]]
    assert len(fake2.sent) == 2  # stopped; Q3/Q4 not sent
    assert sleeps2 == [ibkr.history_gap(2)]
    assert sleeps2[0] >= ibkr.SEND_GAP
    hist = ibkr.load_history(db)
    assert hist["status"] == "error"
    assert hist["last_synced"] == "2025-03-31"
    failed = hist["failed_windows"]
    assert failed[0]["fd"] == "2025-04-01" and failed[0]["split"] == "month"
    assert [x["fd"] for x in failed] == ["2025-04-01", "2025-07-01", "2025-10-01"]
    assert all(x.get("split") == "none" for x in failed[1:])
    assert TOKEN not in json.dumps(hist)


def test_1001_quarter_next_press_months(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "error", "last_synced": "2025-03-31", "next_fd": "2025-04-01",
        "seq_fd": "2025-04-01", "done_windows": 1, "total_windows": 4,
        "start": "2025-01-01", "mode": "full", "end": TODAY.isoformat(),
        "failed_windows": [
            {"fd": "2025-04-01", "td": "2025-06-30", "code": "1001", "kind": "temporary", "split": "month"},
            {"fd": "2025-07-01", "td": "2025-09-30", "code": "1001", "kind": "temporary", "split": "none"},
            {"fd": "2025-10-01", "td": "2025-12-31", "code": "1001", "kind": "temporary", "split": "none"},
        ],
    })
    db.commit()
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    months = ibkr.iter_months(date(2025, 4, 1), date(2025, 6, 30))
    assert fake.sent[0] == (ibkr._ymd(months[0][0]), ibkr._ymd(months[0][1]))
    assert fake.sent == [(ibkr._ymd(months[0][0]), ibkr._ymd(months[0][1]))]  # stop at first month
    hist = ibkr.load_history(db)
    fds = [x["fd"] for x in hist["failed_windows"]]
    assert "2025-04-01" in fds  # failed month retried as-is next time
    assert "2025-05-01" in fds and "2025-06-01" in fds
    assert "2025-07-01" in fds and "2025-10-01" in fds
    assert not any(x["fd"] == "2025-04-01" and x["td"] == "2025-06-30" for x in hist["failed_windows"])


def test_1001_month_retried_as_is(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "error", "last_synced": "2025-03-31", "next_fd": "2025-04-01",
        "seq_fd": "2025-04-01", "done_windows": 1, "total_windows": 2,
        "start": "2025-01-01", "mode": "full", "end": TODAY.isoformat(),
        "failed_windows": [
            {"fd": "2025-04-01", "td": "2025-04-30", "code": "1001", "kind": "temporary", "split": "none"},
        ],
    })
    db.commit()
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    assert fake.sent == [("20250401", "20250430")]
    hist = ibkr.load_history(db)
    assert hist["failed_windows"][0]["fd"] == "2025-04-01"
    assert hist["failed_windows"][0]["td"] == "2025-04-30"
    assert hist["failed_windows"][0]["split"] == "none"


def test_migrate_stored_1001_clamps_td_and_splits_to_quarters(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "error", "last_synced": "2025-12-30", "next_fd": "2025-12-31",
        "seq_fd": "2025-12-31", "done_windows": 4, "total_windows": 5,
        "start": "2022-01-01", "mode": "full",
        "failed_windows": [{"fd": "2025-12-31", "td": "2026-10-17", "code": "1001"}],
    })
    db.commit()
    fake = ScriptedFlex(TINY, send_bodies=[fail])
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    with pytest.raises(RuntimeError):
        src.sync(db, SyncContext())
    capped = date(2026, 10, 16)
    quarters = ibkr.iter_quarters(date(2025, 12, 31), capped)
    assert fake.sent[0] == (ibkr._ymd(quarters[0][0]), ibkr._ymd(quarters[0][1]))
    assert all(td <= "20261016" for _fd, td in fake.sent)
    hist = ibkr.load_history(db)
    assert not any(x.get("td") == "2026-10-17" for x in hist["failed_windows"])
    assert not any(x.get("fd") == "2025-12-31" and x.get("td") == "2026-10-16" for x in hist["failed_windows"])
    # remaining quarters queued; parent replaced
    assert len(hist["failed_windows"]) == len(quarters)  # current fail + remaining
    assert hist["failed_windows"][0]["fd"] == quarters[0][0].isoformat()


def test_1003_on_subwindow_is_gap_and_advances(db, enc, monkeypatch):
    _freeze(monkeypatch)
    _no_fx(monkeypatch)
    fail = _flex_fail("1003", "Statement is not available.")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    ibkr.save_history(db, {
        "status": "error", "last_synced": "2024-12-31", "next_fd": "2025-01-01",
        "seq_fd": "2025-01-01", "done_windows": 3, "total_windows": 5,
        "start": "2022-01-01", "mode": "full", "end": TODAY.isoformat(),
        "failed_windows": [
            {"fd": "2025-01-01", "td": "2025-12-31", "code": "1001", "kind": "temporary", "split": "quarter"},
        ],
    })
    db.commit()
    fake = ScriptedFlex(TINY, send_bodies=[fail])  # first quarter 1003; rest succeed
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    quarters = ibkr.iter_quarters(date(2025, 1, 1), date(2025, 12, 31))
    assert fake.sent[0] == (ibkr._ymd(quarters[0][0]), ibkr._ymd(quarters[0][1]))
    assert len(fake.sent) >= 4  # gap on Q1, then remaining quarters (and 2026)
    hist = ibkr.load_history(db)
    assert any(g.get("fd") == "2025-01-01" for g in hist.get("gaps") or [])
    assert hist["status"] == "done"
    assert hist["last_synced"] == TODAY.isoformat()


def test_get_statement_ignores_legacy_url(enc):
    fake = FakeFlex(send_xml=LEGACY_SEND, statement=load("statement.xml"))
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=nop_sleep)
    xml = cl.fetch(QUERY_ID)
    cl.close()
    assert "<FlexQueryResponse" in xml or "<FlexStatement" in xml
    gets = [c for c in fake.calls if "GetStatement" in c.url.path or "FlexStatementService" in str(c.url)]
    assert gets
    for g in gets:
        assert g.url.host == "ndcdyn.interactivebrokers.com"
        assert g.url.path.endswith("/FlexWebService/GetStatement")
        assert "gdcdyn" not in str(g.url)
        assert "FlexStatementService" not in str(g.url)
        assert g.url.params.get("v") == "3"


def test_get_statement_1001_polls_then_succeeds(enc):
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    fake = FakeFlex(get_bodies=[fail, fail, load("statement.xml")])
    sleeps: list[float] = []
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=sleeps.append)
    xml = cl.fetch(QUERY_ID)
    cl.close()
    assert "<FlexQueryResponse" in xml or "<FlexStatement" in xml
    gets = [c for c in fake.calls if "GetStatement" in c.url.path]
    assert len(gets) == 3
    assert sleeps == [ibkr.POLL_GAP, ibkr.POLL_GAP]
    assert len(fake.calls) <= ibkr.MAX_CALLS
    assert not any("SendRequest" in c.url.path for c in fake.calls[1:] if "GetStatement" in c.url.path)
    sends = [c for c in fake.calls if "SendRequest" in c.url.path]
    assert len(sends) == 1


def test_get_statement_1001_exhausted_is_window_failure(enc):
    fail = _flex_fail("1001", "Statement could not be generated at this time. Please try again shortly.")
    fake = FakeFlex(get_bodies=[fail, fail, fail])
    cl = ibkr.FlexClient(TOKEN, transport=fake.transport(), sleep=nop_sleep)
    with pytest.raises(ibkr.FlexError) as ei:
        cl.fetch(QUERY_ID)
    cl.close()
    assert ei.value.code == "1001" and ei.value.kind == "temporary"
    gets = [c for c in fake.calls if "GetStatement" in c.url.path]
    assert len(gets) == ibkr.RETRY_1019
    assert len(fake.calls) <= ibkr.MAX_CALLS
    assert TOKEN not in str(ei.value)


def test_card_message_1001_split_and_data_available(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    monkeypatch.setattr(ibkr, "_TRANSPORT", FakeFlex(statement=load("multi.xml")).transport())
    c = _client()
    r = c.post("/settings/brokers/ibkr/connect",
               data={"token": TOKEN, "query_id": QUERY_ID, "currency": "USD"}, follow_redirects=True)
    assert TOKEN not in r.text
    ibkr.save_history(db, {
        "status": "error", "last_synced": "2025-12-30", "next_fd": "2025-12-31",
        "seq_fd": "2025-12-31", "done_windows": 4, "total_windows": 5,
        "start": "2022-01-01", "mode": "full", "end": "2026-10-16",
        "error": "IBKR error 1001: Statement could not be generated at this time. Please try again shortly.",
        "failed_windows": [{"fd": "2025-12-31", "td": "2026-10-17", "code": "1001", "kind": "temporary"}],
    })
    db.commit()
    page = c.get("/settings").text
    assert TOKEN not in page
    assert "IBKR could not generate 2025-12-31–2026-10-16 (1001)" in page
    n = len(ibkr.iter_quarters(date(2025, 12, 31), date(2026, 10, 16)))
    assert f"Next Sync will fetch it in smaller pieces: {n} quarters left." in page
    assert "Data available through 2026-10-16" in page
    assert "IBKR has data up to the previous business day" in page
    assert "1001" in page
    assert "Statement could not be generated" in page


# ---------------------------------------------------------------- ibkr7: streaming upload, iterparse, history start
def _tiny_stmt(trade_id: str, account="U1") -> bytes:
    return _stmt(f'''
      <Trades>
        <Trade accountId="{account}" currency="USD" assetCategory="STK" symbol="AAA" conid="1"
               tradeID="{trade_id}" dateTime="20260902;143100" quantity="1" tradePrice="10"
               ibCommission="0" buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
      </Trades>''', account=account).encode()


def test_iterparse_matches_dom_on_all_fixtures(tmp_path):
    for path in sorted(FIX.glob("*.xml")):
        text = path.read_text()
        dest = tmp_path / path.name
        dest.write_text(text)
        try:
            dom = [ibkr.parsed_as_comparable(p) for p in ibkr.parse_statements(text)]
        except ibkr.FlexError:
            with pytest.raises(ibkr.FlexError):
                ibkr.parse_statements_from_path(dest)
            continue
        streamed = [ibkr.parsed_as_comparable(p) for p in ibkr.parse_statements_from_path(dest)]
        assert streamed == dom, path.name


def test_iterparse_peak_memory_on_large_synthetic(tmp_path):
    """Generate a large Flex XML on the fly; iterparse peak stays well under the file size."""
    n_stmts, n_trades = 3, 40_000
    path = tmp_path / "large.xml"
    with path.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<FlexQueryResponse>\n')
        f.write(f'<FlexStatements count="{n_stmts}">\n')
        for s in range(n_stmts):
            acct = f"U{s + 1}000001"
            f.write(f'<FlexStatement accountId="{acct}" fromDate="20220101" toDate="20261016">\n')
            f.write(f'<AccountInformation accountId="{acct}" currency="USD"/>\n<Trades>\n')
            line = (
                '<Trade accountId="{acct}" currency="USD" assetCategory="STK" symbol="AAA" '
                'tradeID="{tid}" dateTime="20240101;100000" quantity="1" tradePrice="10" '
                'ibCommission="0" buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>\n'
            )
            for i in range(n_trades):
                f.write(line.format(acct=acct, tid=f"{s}-{i}"))
            f.write("</Trades></FlexStatement>\n")
        f.write("</FlexStatements></FlexQueryResponse>\n")
    size = path.stat().st_size
    assert size > 8 * 1024 * 1024
    tracemalloc.start()
    n = 0
    stmts = 0
    for parsed in ibkr.iter_parsed_statements(path):
        stmts += 1
        n += len(parsed.trades)
        del parsed
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert stmts == n_stmts and n == n_stmts * n_trades
    assert peak < 150 * 1024 * 1024
    # 120k trades / 27 MB file: peak is one statement of row dicts, not the whole tree.
    assert peak < 80 * 1024 * 1024
    assert peak / size < 2.0


def test_multi_file_upload_returns_303_and_processes_sequentially(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    order: list[str] = []
    real = ibkr.IbkrFlexSource.import_xml_path

    def spy(self, sess, path, *, filename=None, run_id=None):
        order.append(filename or Path(path).name)
        return real(self, sess, path, filename=filename, run_id=run_id)

    monkeypatch.setattr(ibkr.IbkrFlexSource, "import_xml_path", spy)
    kicked: list[str] = []
    monkeypatch.setattr(ibkr, "UPLOAD_SYNC", False)
    monkeypatch.setattr(ibkr, "start_upload_worker", lambda: kicked.append("go"))
    c = _client()
    r = c.post("/settings/brokers/ibkr/upload", files=[
        ("files", ("a.xml", _tiny_stmt("A1"), "application/xml")),
        ("files", ("b.xml", _tiny_stmt("B1"), "application/xml")),
    ], follow_redirects=False)
    assert r.status_code == 303
    assert kicked == ["go"]
    prog = ibkr.load_upload(db)
    assert prog["status"] == "queued" and len(prog["files"]) == 2
    assert order == []
    ibkr.run_upload_job()
    assert order[0].startswith("a") and order[1].startswith("b")
    prog = ibkr.load_upload(db)
    assert prog["status"] == "done"
    pub = ibkr.public_upload(prog)
    assert "path" not in json.dumps(pub)
    assert TOKEN not in json.dumps(pub)
    page = c.get("/settings").text
    assert "Upload received: 2 file(s), processing in background" in page or "Upload finished" in page
    assert "ibkr-upload-progress" in page
    assert TOKEN not in page


def test_upload_progress_and_errors(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    monkeypatch.setattr(ibkr, "MAX_UPLOAD_BYTES", 1500)
    c = _client()
    r = c.post("/settings/brokers/ibkr/upload", files=[
        ("files", ("ok.xml", _tiny_stmt("OK1"), "application/xml")),
        ("files", ("bad.txt", b"hello world", "text/plain")),
        ("files", ("big.xml", b"<FlexQueryResponse>" + b"x" * 2000 + b"</FlexQueryResponse>", "application/xml")),
        ("files", ("broken.xml", b"<FlexQueryResponse><FlexStatement accountId=\"U1\"><Trades>",
                   "application/xml")),
    ], follow_redirects=True)
    assert r.status_code == 200
    prog = ibkr.load_upload(db)
    errors = {f["name"]: (f.get("status"), f.get("error") or "") for f in prog["files"]}
    assert errors["bad.txt"][0] == "error" and "not xml" in errors["bad.txt"][1].lower()
    assert errors["big.xml"][0] == "error" and "too large" in errors["big.xml"][1].lower()
    broken = errors["broken.xml"]
    assert broken[0] == "error"
    assert "parse error" in broken[1].lower() or "line" in broken[1].lower() or "xml" in broken[1].lower()
    page = c.get("/settings").text
    assert "not XML" in page or "not xml" in page.lower()
    assert TOKEN not in page
    body = c.get("/settings/brokers/ibkr/upload/status").text
    assert "every 3s" not in body or prog.get("status") not in {"queued", "running"}
    assert "hx-get=\"/settings/brokers/ibkr/upload/status\"" in c.get("/settings").text or prog.get("status") not in {"queued", "running"}


def test_upload_old_file_field_name_still_works(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    c = _client()
    r = c.post("/settings/brokers/ibkr/upload",
               files={"file": ("stmt.xml", _tiny_stmt("LEGACY"), "application/xml")},
               follow_redirects=False)
    assert r.status_code == 303
    prog = ibkr.load_upload(db)
    assert prog["status"] == "done"
    assert db.query(Execution).count() >= 1


def test_upload_busy_rejected(db, enc, monkeypatch):
    ibkr.save_upload(db, {"status": "running", "files": [{"name": "a.xml", "status": "running"}]})
    db.commit()
    c = _client()
    r = c.post("/settings/brokers/ibkr/upload",
               files={"files": ("b.xml", _tiny_stmt("X"), "application/xml")},
               follow_redirects=True)
    assert "already processing" in r.text.lower()


def test_restart_marks_running_upload_interrupted(db, enc):
    ibkr.save_upload(db, {
        "status": "running", "current": 0,
        "files": [{"name": "a.xml", "status": "running", "size": 10}],
    })
    db.commit()
    out = ibkr.recover_stale_upload(db)
    assert out["status"] == "error"
    assert "interrupted by restart" in (out.get("error") or "")
    assert "upload again" in (out.get("error") or "")
    assert out["files"][0]["status"] == "error"


def test_reupload_dedupes(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    _no_ibkr_network(monkeypatch)
    xml = load("statement.xml").encode()
    c = _client()
    c.post("/settings/brokers/ibkr/upload", files={"file": ("stmt.xml", xml, "application/xml")},
           follow_redirects=True)
    n = db.query(Execution).count()
    assert n > 0
    c.post("/settings/brokers/ibkr/upload", files={"file": ("stmt.xml", xml, "application/xml")},
           follow_redirects=True)
    assert db.query(Execution).count() == n
    prog = ibkr.load_upload(db)
    row = prog["files"][0]
    assert row["inserted"] == 0
    assert row.get("error") == "nothing new" or row["status"] == "done"


def test_history_start_earlier_does_not_reset_or_call_ibkr(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, load("statement.xml"), filename="seed.xml")
    n = db.query(Execution).count()
    hist = {
        "status": "done", "start": "2022-01-01", "covered_from": "2022-01-01",
        "end": "2026-10-16", "last_synced": "2026-10-16",
        "next_fd": "2026-10-17", "seq_fd": "2026-10-17",
        "done_windows": 9, "total_windows": 9, "mode": "full",
        "notes": ["2022 had no statement (IBKR 1003)"],
        "gaps": [{"fd": "2022-01-01", "td": "2022-12-31", "code": "1003"}],
        "failed_windows": [],
    }
    ibkr.save_history(db, hist)
    ibkr.save_history_start(db, date(2022, 1, 1))
    db.commit()
    called = []
    monkeypatch.setattr(ibkr, "test_connection", lambda *a, **k: called.append("x"))
    c = _client()
    r = c.post("/settings/brokers/ibkr/history-start", data={"history_start": "2021-06-24"},
               follow_redirects=True)
    assert called == []
    assert "2021-06-24" in r.text
    assert ibkr.history_start_date(db) == date(2021, 6, 24)
    after = ibkr.load_history(db)
    assert after["status"] == "done" and after["done_windows"] == 9
    assert after["last_synced"] == "2026-10-16"
    assert any(g.get("fd") == "2022-01-01" for g in after.get("gaps") or [])
    assert "2022 had no statement (IBKR 1003)" in (after.get("notes") or [])
    assert db.query(Execution).count() == n
    fake = ScriptedFlex(TINY)
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    assert fake.sent == [("20210624", "20211231")]
    hist2 = ibkr.load_history(db)
    assert hist2["status"] == "done"
    assert hist2["last_synced"] == "2026-10-16"
    assert any(g.get("fd") == "2022-01-01" for g in hist2.get("gaps") or [])
    assert "2022 had no statement" in " ".join(hist2.get("notes") or [])
    assert db.query(Execution).count() >= n


def test_history_start_later_does_not_delete(db, enc, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 17, 12, 0, tzinfo=NY))
    _no_fx(monkeypatch)
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, load("statement.xml"), filename="seed.xml")
    n = db.query(Execution).count()
    ibkr.save_history(db, {
        "status": "done", "start": "2022-01-01", "covered_from": "2022-01-01",
        "end": "2026-10-16", "last_synced": "2026-10-16",
        "next_fd": "2026-10-17", "seq_fd": "2026-10-17",
        "done_windows": 9, "total_windows": 9, "mode": "full",
    })
    db.commit()
    c = _client()
    c.post("/settings/brokers/ibkr/history-start", data={"history_start": "2024-01-01"},
           follow_redirects=True)
    assert ibkr.history_start_date(db) == date(2024, 1, 1)
    assert db.query(Execution).count() == n
    fake = ScriptedFlex(TINY)
    from app.services import set_state
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *a: [])
    src.sync(db, SyncContext())
    assert fake.sent == []
    assert db.query(Execution).count() == n
    hist = ibkr.load_history(db)
    assert hist["status"] == "done" and hist["last_synced"] == "2026-10-16"


def test_upload_gzip_skip_note_for_large_raw(db, enc, monkeypatch, tmp_path):
    _no_fx(monkeypatch)
    monkeypatch.setattr(ibkr, "RAW_STORE_UNCOMPRESSED", 50)
    monkeypatch.setattr(ibkr, "RAW_STORE_GZIP_MAX", 1)
    path = tmp_path / "skip.xml"
    path.write_bytes(_tiny_stmt("GZ1") + b"<!--" + b"x" * 200 + b"-->")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    res = src.import_xml_path(db, path, filename="skip.xml")
    assert "raw XML not stored" in (res.message or "")
    xml_rows = [p for p in db.scalars(select(SyncPayload)) if p.content_type == "application/xml"]
    assert xml_rows == []
    assert db.query(Execution).count() >= 1


def test_iter_months_cover_a_quarter():
    m = ibkr.iter_months(date(2025, 4, 1), date(2025, 6, 30))
    assert m == [
        (date(2025, 4, 1), date(2025, 4, 30)),
        (date(2025, 5, 1), date(2025, 5, 31)),
        (date(2025, 6, 1), date(2025, 6, 30)),
    ]
    assert all(1 <= (td - fd).days + 1 <= 31 for fd, td in m)
    q = ibkr.iter_quarters(date(2025, 12, 31), date(2026, 10, 16))
    assert q[0] == (date(2025, 12, 31), date(2025, 12, 31))
    assert q[-1] == (date(2026, 10, 1), date(2026, 10, 16))
    assert not any(td > date(2026, 10, 16) for _fd, td in q)
