"""Interactive Brokers Flex adapter: XML parse, mocked SendRequest/GetStatement (1019 retry),
encrypted token, incremental idempotent sync, Settings card. Synthetic fixtures only."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
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
    assert fake.sent[1] == ("20250101", "20251231")  # retried the same window; the cursor stayed


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
    assert any(x.get("code") == "1001" for x in hist.get("failed_windows") or [])
    ibkr._GATE.update(last=0.0, until=0.0)
    src.sync(db, SyncContext())
    assert fake.sent[1] == ("20250101", "20251231")


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


def test_conid_maps_beri_beril(db, enc, monkeypatch):
    _no_fx(monkeypatch)
    xml = _stmt("""
      <Trades>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="BERIL" conid="80575143"
               tradeID="B1" dateTime="20260301;100000" quantity="8" tradePrice="10" ibCommission="0"
               buySell="BUY" openCloseIndicator="O" levelOfDetail="EXECUTION"/>
        <Trade accountId="U1" currency="USD" assetCategory="STK" symbol="BERI" conid="80575143"
               tradeID="B2" dateTime="20260310;100000" quantity="-3" tradePrice="12" ibCommission="0"
               buySell="SELL" openCloseIndicator="C" levelOfDetail="EXECUTION"/>
      </Trades>
      <OpenPositions>
        <OpenPosition accountId="U1" currency="USD" assetCategory="STK" symbol="BERI" conid="80575143"
                      position="5" markPrice="11" levelOfDetail="SUMMARY"/>
      </OpenPositions>""")
    src = ibkr.IbkrFlexSource(sleep=nop_sleep, split_fetch=lambda *a: [])
    src.import_xml(db, xml)
    insts = list(db.scalars(select(Instrument).where(Instrument.source_key == ibkr.SOURCE_KEY)))
    assert len(insts) == 1 and insts[0].conid == "80575143"
    execs = list(db.scalars(select(Execution).where(Execution.kind == "TRADE")))
    assert {e.instrument_id for e in execs} == {insts[0].id}
    assert {e.symbol for e in execs} == {insts[0].symbol}
    flags = [a for a in db.scalars(select(CorporateAdjustment)) if a.status == "needs_attention"]
    assert not any("BERI" in (a.symbol or "") and "qty" in (a.external_key or "") for a in flags)


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
