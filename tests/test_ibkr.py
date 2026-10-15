"""Interactive Brokers Flex adapter: XML parse, mocked SendRequest/GetStatement (1019 retry),
encrypted token, incremental idempotent sync, Settings card. Synthetic fixtures only."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.instruments import ExecRecord
from app.models import Account, CashFlow, CorporateAdjustment, Dividend, Execution, Trade
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
    assert "configured period" in res.message
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
