"""Internal IBKR transfers, conid identity, Flex facts, and the rebuild button.

Synthetic XML only. FlexClient.fetch and any Interactive Brokers host fail the test.
"""
from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.balance import series
from app.models import (
    Account, BalancePoint, CashFlow, CorporateAdjustment, Execution, IbkrFlexFact, Instrument,
    SyncPayload, Trade,
)
from app.services import get_state, rebuild_trades, set_state
from app.sources import ibkr_flex as ibkr
from app.transfers import business_days


def _source():
    return ibkr.IbkrFlexSource(sleep=lambda *_a, **_k: None, split_fetch=lambda *_a, **_k: [])


def _import(db, xml: str):
    return _source().import_xml(db, xml, filename="stmt.xml")


def _wrap(*statements: str) -> str:
    body = "\n".join(statements)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<FlexQueryResponse queryName="Activity" type="AF"><FlexStatements count="{len(statements)}">'
        f"{body}</FlexStatements></FlexQueryResponse>"
    )


def _stmt(acct: str, inner: str, fr: str = "20210624", to: str = "20261016") -> str:
    return (
        f'<FlexStatement accountId="{acct}" fromDate="{fr}" toDate="{to}" whenGenerated="{to};160000">'
        f'<AccountInformation accountId="{acct}" currency="USD" accountType="Individual" />'
        f"{inner}</FlexStatement>"
    )


def _trade(acct, tid, sym, conid, qty, price, when, side, effect) -> str:
    return (
        f'<Trade accountId="{acct}" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="{sym}" '
        f'description="{sym} INC" conid="{conid}" tradeID="{tid}" dateTime="{when};100000" '
        f'quantity="{qty}" tradePrice="{price}" ibCommission="0" taxes="0" buySell="{side}" '
        f'openCloseIndicator="{effect}" levelOfDetail="EXECUTION" multiplier="1" />'
    )


def _xfer(acct, sym, conid, qty, when, direction, txn, other, cost=None) -> str:
    cost_attr = "" if cost is None else f' costBasis="{cost}"'
    return (
        f'<Transfer accountId="{acct}" currency="USD" assetCategory="STK" symbol="{sym}" conid="{conid}" '
        f'quantity="{qty}" direction="{direction}" type="INTERNAL" dateTime="{when};120000" '
        f'transferAccount="{other}" transactionID="{txn}"{cost_attr} />'
    )


def _acct(db, ext: str) -> Account:
    row = db.scalar(select(Account).where(Account.external_ref == ext))
    assert row is not None, ext
    return row


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    client = TestClient(create_app())
    assert client.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return client


@pytest.fixture(autouse=True)
def _no_ibkr_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("test tried to call IBKR")

    monkeypatch.setattr(ibkr.FlexClient, "fetch", boom)
    monkeypatch.setattr("app.currency.fetch_range", lambda *_a, **_k: 0)

    real = httpx.Client.request

    def guarded(self, method, url, *args, **kwargs):
        text = str(url).lower()
        if any(host in text for host in ("interactivebrokers", "gdcdyn", "ibkr.com")):
            raise AssertionError(f"test tried to call IBKR: {url}")
        return real(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", guarded)


def test_business_days_and_transfer_direction():
    assert business_days(date(2023, 1, 3), date(2023, 1, 3)) == 0
    assert business_days(date(2023, 1, 3), date(2023, 1, 10)) == 5
    assert business_days(date(2023, 1, 3), date(2023, 1, 11)) == 6
    out = ibkr.transfer_to_record({
        "accountId": "U0000001", "symbol": "AAA", "conid": "1", "quantity": "4",
        "type": "INTERNAL", "direction": "OUT", "dateTime": "20230103;120000",
        "transactionID": "T1", "transferAccount": "U0000002", "amount": "10",
    })
    assert out.side == "SELL" and out.position_effect == "CLOSE" and out.kind == "TRANSFER"
    assert out.price == 0 and out.raw["xfer"]["cost"] is None
    assert out.raw["xfer"]["counterparty"] == "U0000002"
    inn = ibkr.transfer_to_record({
        "accountId": "U0000001", "symbol": "AAA", "conid": "1", "quantity": "4",
        "type": "INTERNAL", "date": "20230103", "transactionID": "T2", "costBasis": "0",
        "positionAmount": "40",
    })
    assert inn.side == "BUY" and inn.price == pytest.approx(10)
    assert inn.raw["xfer"]["direction"] == "IN"
    words = ibkr.transfer_to_record({
        "symbol": "AAA", "quantity": "1", "type": "POSITION TRANSFER OUT", "conid": "1",
        "dateTime": "20230103;120000", "transactionID": "T3", "value": "8",
    })
    assert words.side == "SELL" and words.price == pytest.approx(8)
    missing = ibkr.transfer_to_record({"symbol": "NOPE", "amount": "3", "transactionID": "T4"})
    assert isinstance(missing, ibkr.Skip) and missing.reason == "transfer incomplete"


# Margin (U0000001) bought these in 2021–2022 and moved them to Cash (U0000002) in 2023.
_AUDIT = [
    ("TAP", "33191300", 7, 50.0, "20210315", "20230103", "20230615", 60.0, False),
    ("META", "107113386", 3.0341, 180.0, "20210624", "20230110", "20230712", 200.0, True),
    ("PLTR", "444857009", 15, 22.0, "20210802", "20230117", "20230801", 25.0, True),
    ("NFLX", "15124833", 1, 400.0, "20210901", "20230124", "20230815", 450.0, True),
    ("INTC", "270639", 11, 30.0, "20211004", "20230201", "20230901", 35.0, True),
    ("UBER", "365207014", 8.3177, 35.0, "20211101", "20230208", "20230915", 40.0, True),
    ("TWLO", "237794430", 4, 250.0, "20211201", "20230215", "20231002", 260.0, True),
    ("BABA", "166090175", 3, 120.0, "20220104", "20230222", "20231016", 130.0, True),
    ("BAC", "10098", 6.3707, 40.0, "20220201", "20230301", "20231101", 42.0, True),
    ("GRFS", "110753297", 10, 8.0, "20220301", "20230308", "20231115", 9.0, True),
    ("PARA", "393897513", 17, 25.0, "20220401", "20230315", "20231201", 28.0, True),
    ("HBI", "40369429", 15, 15.0, "20220502", "20230403", "20240102", 16.0, True),
    ("INL", "12177196", 8, 4.0, "20220601", "20230502", "20240116", 5.0, True),
]


def _audit_xml() -> str:
    margin_trades, cash_trades, margin_xfers, cash_xfers = [], [], [], []
    for sym, conid, qty, px, buy, moved, sold, sell_px, with_cost in _AUDIT:
        margin_trades.append(_trade("U0000001", f"B{sym}", sym, conid, qty, px, buy, "BUY", "O"))
        cash_trades.append(_trade("U0000002", f"S{sym}", sym, conid, qty, sell_px, sold, "SELL", "C"))
        cost = None if not with_cost else f"{qty * px:.6f}"
        margin_xfers.append(_xfer("U0000001", sym, conid, qty, moved, "OUT", f"O{sym}", "U0000002", cost))
        cash_xfers.append(_xfer("U0000002", sym, conid, qty, moved, "IN", f"I{sym}", "U0000001", cost))
    margin = _stmt("U0000001", (
        "<Trades>" + "".join(margin_trades) + "</Trades>"
        "<Transfers>" + "".join(margin_xfers) + "</Transfers>"
        "<OpenPositions></OpenPositions>"
    ))
    cash = _stmt("U0000002", (
        "<Trades>" + "".join(cash_trades) + "</Trades>"
        "<Transfers>" + "".join(cash_xfers) + "</Transfers>"
        "<OpenPositions></OpenPositions>"
    ))
    return _wrap(margin, cash)


def _as_cash(db):
    cash = _acct(db, "U0000002")
    cash.kind = "cash"
    db.commit()
    rebuild_trades(db, [cash.id])
    db.commit()
    return cash


def test_audit_transfers_keep_cost_and_separate_inl(db):
    _import(db, _audit_xml())
    margin, cash = _acct(db, "U0000001"), _as_cash(db)
    db.expire_all()
    trades = list(db.scalars(select(Trade)))
    by_sym = {}
    for trade in trades:
        by_sym.setdefault((trade.account_id, trade.symbol), []).append(trade)

    zero = [e for e in db.scalars(select(Execution))
            if e.kind == "TRANSFER" and (e.price or 0) == 0]
    assert zero == []

    for sym, conid, qty, px, _buy, _moved, _sold, _sell, _cost in _AUDIT:
        margin_trades = by_sym[margin.id, sym]
        cash_trades = by_sym[cash.id, sym]
        assert len(margin_trades) == 1 and len(cash_trades) == 1
        assert margin_trades[0].status == "CLOSED" and margin_trades[0].net_pnl == pytest.approx(0, abs=1e-4)
        assert margin_trades[0].open_quantity == pytest.approx(0)
        assert cash_trades[0].status == "CLOSED"
        assert cash_trades[0].quantity == pytest.approx(qty)
        assert cash_trades[0].entry_price == pytest.approx(px, rel=1e-6, abs=1e-4)
        buy = db.scalar(select(Execution).where(
            Execution.account_id == margin.id, Execution.symbol == sym, Execution.kind == "TRADE",
            Execution.side == "BUY"))
        assert cash_trades[0].opened_at == buy.executed_at
        out_row = db.scalar(select(Execution).where(
            Execution.account_id == margin.id, Execution.symbol == sym, Execution.kind == "TRANSFER"))
        in_row = db.scalar(select(Execution).where(
            Execution.account_id == cash.id, Execution.symbol == sym, Execution.external_id == f"I{sym}"))
        assert out_row.transfer_pair_id and out_row.transfer_pair_id == in_row.transfer_pair_id
        assert out_row.price == pytest.approx(px, rel=1e-6, abs=1e-4)
        inst = db.get(Instrument, buy.instrument_id)
        assert inst.conid == conid

    inl = db.scalar(select(Instrument).where(Instrument.conid == "12177196"))
    intc = db.scalar(select(Instrument).where(Instrument.conid == "270639"))
    assert inl.symbol == "INL" and intc.symbol == "INTC" and inl.id != intc.id
    assert {t.symbol for t in trades if t.symbol in {"INL", "INTC"}} == {"INL", "INTC"}
    for acct in (margin, cash):
        orphans = json.loads(get_state(db, f"orphans:{acct.id}") or "[]")
        assert orphans == []
        assert not [t for t in by_sym[acct.id, "INL"]] or by_sym[acct.id, "INL"][0].instrument_id == inl.id

    # A second upload resets from the stored transfer fields and lands on the same books.
    before = db.scalar(select(func.count()).select_from(Execution))
    pnls = sorted((t.account_id, t.symbol, round(t.net_pnl, 4), t.opened_at) for t in trades)
    _import(db, _audit_xml())
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Execution)) == before
    again = list(db.scalars(select(Trade)))
    assert sorted((t.account_id, t.symbol, round(t.net_pnl, 4), t.opened_at) for t in again) == pnls
    assert not [e for e in db.scalars(select(Execution)) if e.kind == "TRANSFER" and (e.price or 0) == 0]


def test_unpaired_and_distant_transfers(db):
    # Six business days apart: not a pair. Each side is booked from its own cost, with a note.
    far = _wrap(
        _stmt("U0000001", (
            "<Trades>" + _trade("U0000001", "B1", "FAR", "9001", 2, 10, "20220101", "BUY", "O") + "</Trades>"
            "<Transfers>" + _xfer("U0000001", "FAR", "9001", 2, "20230103", "OUT", "OFAR", "U0000002", "20") + "</Transfers>"
            "<OpenPositions></OpenPositions>"
        )),
        _stmt("U0000002", (
            "<Transfers>" + _xfer("U0000002", "FAR", "9001", 2, "20230111", "IN", "IFAR", "U0000001", "20") + "</Transfers>"
            "<OpenPositions></OpenPositions>"
        )),
    )
    _import(db, far)
    rows = list(db.scalars(select(Execution).where(Execution.symbol == "FAR", Execution.kind == "TRANSFER")))
    assert len(rows) == 2 and all(r.transfer_pair_id is None for r in rows)
    assert all(r.price == pytest.approx(10) for r in rows)
    notes = list(db.scalars(select(CorporateAdjustment).where(CorporateAdjustment.symbol == "FAR")))
    xnotes = [n for n in notes if "transfer" in (n.note or "")]
    assert len(xnotes) == 2 and all("unpaired" in n.note for n in xnotes)

    # No source lot and no cost: do not book a $0 open or a $0 close.
    bare = _wrap(_stmt("U0000001", (
        "<Transfers>"
        + _xfer("U0000001", "BARE", "9002", 1, "20230201", "IN", "IBARE", "U9999999")
        + _xfer("U0000001", "GONE", "9003", 1, "20230202", "OUT", "OGONE", "U9999999")
        + "</Transfers><OpenPositions></OpenPositions>"
    )))
    _import(db, bare)
    assert list(db.scalars(select(Execution).where(Execution.symbol.in_(("BARE", "GONE"))))) == []
    bare_notes = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.symbol.in_(("BARE", "GONE")), CorporateAdjustment.status == "needs_attention")))
    assert {n.symbol for n in bare_notes} == {"BARE", "GONE"}
    assert all("cost" in (n.note or "").lower() for n in bare_notes)


def test_multi_lot_transfer_exits_at_weighted_cost(db):
    xml = _wrap(
        _stmt("U0000001", (
            "<Trades>"
            + _trade("U0000001", "L1", "MIX", "777001", 2, 10, "20210802", "BUY", "O")
            + _trade("U0000001", "L2", "MIX", "777001", 3, 20, "20210901", "BUY", "O")
            + "</Trades><Transfers>"
            + _xfer("U0000001", "MIX", "777001", 5, "20230201", "OUT", "OMIX", "U0000002")
            + "</Transfers><OpenPositions></OpenPositions>"
        )),
        _stmt("U0000002", (
            "<Transfers>" + _xfer("U0000002", "MIX", "777001", 5, "20230201", "IN", "IMIX", "U0000001")
            + "</Transfers><OpenPositions></OpenPositions>"
        )),
    )
    _import(db, xml)
    cash = _as_cash(db)
    margin = _acct(db, "U0000001")
    db.expire_all()
    out_row = db.scalar(select(Execution).where(
        Execution.account_id == margin.id, Execution.kind == "TRANSFER"))
    assert out_row.quantity == pytest.approx(5) and out_row.price == pytest.approx(16)
    margin_trade = db.scalar(select(Trade).where(Trade.account_id == margin.id, Trade.symbol == "MIX"))
    assert margin_trade.status == "CLOSED" and margin_trade.net_pnl == pytest.approx(0, abs=1e-4)
    dest = list(db.scalars(select(Execution).where(
        Execution.account_id == cash.id, Execution.kind == "TRANSFER").order_by(Execution.executed_at, Execution.id)))
    assert [(round(e.quantity, 4), round(e.price, 4)) for e in dest] == [(2.0, 10.0), (3.0, 20.0)]
    first_buy = db.scalar(select(Execution).where(Execution.external_id == "L1"))
    opened = db.scalar(select(Trade).where(Trade.account_id == cash.id, Trade.symbol == "MIX"))
    assert opened.opened_at == first_buy.executed_at
    assert opened.entry_price == pytest.approx(16)


def test_amz_and_amzn_stay_apart_when_aliased(db):
    set_state(db, "symbol_aliases", json.dumps({"AMZ": "AMZN"}))
    db.commit()
    xml = _wrap(_stmt("U0000001", (
        "<Trades>"
        + _trade("U0000001", "Z1", "AMZ", "13013290", 2, 10, "20240102", "BUY", "O")
        + _trade("U0000001", "Z2", "AMZN", "3691937", 5, 100, "20240103", "BUY", "O")
        + "</Trades><OpenPositions>"
        + '<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="AMZ" conid="13013290" position="2" levelOfDetail="SUMMARY" reportDate="20261016" />'
        + '<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="AMZN" conid="3691937" position="5" levelOfDetail="SUMMARY" reportDate="20261016" />'
        + "</OpenPositions>"
    )))
    _import(db, xml)
    db.expire_all()
    trades = list(db.scalars(select(Trade).where(Trade.symbol.in_(("AMZ", "AMZN")))))
    assert len(trades) == 2
    assert sorted((t.symbol, t.quantity) for t in trades) == [("AMZ", 2.0), ("AMZN", 5.0)]
    conids = {i.conid: i.symbol for i in db.scalars(select(Instrument).where(Instrument.conid.in_(("13013290", "3691937"))))}
    assert conids == {"13013290": "AMZ", "3691937": "AMZN"}
    notes = [n.note or "" for n in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.status == "needs_attention"))]
    assert not any("ambiguous" in n for n in notes)
    acct = _acct(db, "U0000001")
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="AMZ", kind="CHECK", source="broker", status="needs_attention",
        external_key="attn:AMZ:rename", note="AMZ: rename between AMZ and AMZN is ambiguous."))
    db.commit()
    from app.adjustments import clear_stale_attention
    assert clear_stale_attention(db, acct.id) == 1
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.external_key == "attn:AMZ:rename")) is None


def test_same_conid_follows_the_latest_symbol(db):
    xml = _wrap(_stmt("U0000001", (
        "<Trades>" + _trade("U0000001", "P1", "PSKY", "804144296", 4, 12, "20240102", "BUY", "O") + "</Trades>"
        "<OpenPositions>"
        '<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="SKYD" conid="804144296" '
        'position="4" levelOfDetail="SUMMARY" reportDate="20250601" />'
        "</OpenPositions>"
    )))
    _import(db, xml)
    db.expire_all()
    inst = db.scalar(select(Instrument).where(Instrument.conid == "804144296"))
    assert inst.symbol == "SKYD"
    assert {e.symbol for e in db.scalars(select(Execution).where(Execution.instrument_id == inst.id))} == {"SKYD"}
    trade = db.scalar(select(Trade).where(Trade.instrument_id == inst.id))
    assert trade.symbol == "SKYD" and trade.quantity == pytest.approx(4)
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "RENAME")) is None


def test_corporate_codes_instrument_and_duplicate_split(db):
    xml = _wrap(_stmt("U0000001", (
        "<Trades>"
        + _trade("U0000001", "B1", "AMZN", "3691937", 1, 100, "20220104", "BUY", "O")
        + _trade("U0000001", "B2", "TEND", "555001", 10, 5, "20220104", "BUY", "O")
        + _trade("U0000001", "B3", "BOND", "555002", 2, 100, "20220104", "BUY", "O")
        + _trade("U0000001", "B4", "RIGHT", "555003", 1, 3, "20220104", "BUY", "O")
        + "</Trades><CorporateActions>"
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="AMZN" conid="3691937" '
          'type="FS" description="AMZN SPLIT 2 FOR 1" quantity="2" dateTime="20220606;090000" transactionID="S1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="AMZN" conid="3691937" '
          'type="FS" description="AMZN(US0231351067) SPLIT 2 FOR 1 (AMZN, AMAZON, US0231351067)" quantity="2" '
          'dateTime="20220606;090000" transactionID="S2" actionID="S2B" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="TEND" conid="555001" '
          'type="TO" description="TEND TENDER OFFER" quantity="10" amount="80" dateTime="20220607;090000" transactionID="TO1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="BOND" conid="555002" '
          'type="BM" description="BOND bond maturity" quantity="2" amount="210" dateTime="20220608;090000" transactionID="BM1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="LIEU" conid="555004" '
          'type="DI" description="LIEU cash in lieu" amount="3.5" dateTime="20220609;090000" transactionID="DI1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="RIGHT" conid="555003" '
          'type="RI" description="RIGHT rights issue" quantity="2" dateTime="20220610;090000" transactionID="RI1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="NAME" conid="555005" '
          'type="HI" description="NAME name change only" quantity="1" dateTime="20220611;090000" transactionID="HI1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="ICNO" conid="555006" '
          'type="IC" description="ICNO CUSIP CHANGE" quantity="1" dateTime="20220612;090000" transactionID="IC1" />'
        + '<CorporateAction accountId="U0000001" currency="USD" assetCategory="STK" symbol="ZZZ" conid="555007" '
          'type="ZZ" description="ZZZ mystery action" quantity="1" dateTime="20220613;090000" transactionID="ZZ1" />'
        + "</CorporateActions><OpenPositions>"
        + '<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="AMZN" conid="3691937" position="2" levelOfDetail="SUMMARY" reportDate="20261016" />'
        + "</OpenPositions>"
    )))
    _import(db, xml)
    db.expire_all()
    splits = list(db.scalars(select(Execution).where(Execution.kind == "SPLIT", Execution.symbol == "AMZN")))
    assert len(splits) == 1
    assert splits[0].instrument_id is not None
    assert db.get(Instrument, splits[0].instrument_id).conid == "3691937"
    applied = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.kind == "SPLIT", CorporateAdjustment.status == "applied",
        CorporateAdjustment.symbol == "AMZN")))
    assert len(applied) == 1
    tender = db.scalar(select(Execution).where(Execution.symbol == "TEND", Execution.kind == "ADJUST"))
    assert tender is not None and tender.side == "SELL" and tender.position_effect == "CLOSE"
    assert tender.price == pytest.approx(8)
    assert db.scalar(select(CorporateAdjustment).where(CorporateAdjustment.kind == "MERGER")) is None
    mature = db.scalar(select(Execution).where(Execution.symbol == "BOND", Execution.kind == "ADJUST"))
    assert mature is not None and mature.price == pytest.approx(105)
    cash = db.scalar(select(CashFlow).where(CashFlow.description.contains("lieu")))
    assert cash is not None and cash.amount == pytest.approx(3.5)
    rights = db.scalar(select(Execution).where(Execution.symbol == "RIGHT", Execution.kind == "DISTRIBUTION"))
    assert rights is not None and rights.price == 0 and rights.quantity == pytest.approx(2)
    notes = {n.symbol: n.note or "" for n in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.status == "needs_attention"))}
    assert "NAME" not in notes
    assert "unrecognised" in notes["ZZZ"].lower()
    assert "ambiguous" in notes["ICNO"].lower() or "symbol change" in notes["ICNO"].lower()


def test_quantity_uses_the_latest_position_snapshot(db):
    def xml(to, position):
        pos = ""
        if position is not None:
            pos = (
                f'<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="SNAP" '
                f'conid="555111" position="{position}" levelOfDetail="SUMMARY" reportDate="{to}" />'
            )
        return _wrap(_stmt("U0000001", (
            "<Trades>" + _trade("U0000001", "SN1", "SNAP", "555111", 10, 9, "20230601", "BUY", "O") + "</Trades>"
            f"<OpenPositions>{pos}</OpenPositions>"
        ), to=to))

    def qty_notes():
        return list(db.scalars(select(CorporateAdjustment).where(
            CorporateAdjustment.status == "needs_attention", CorporateAdjustment.symbol == "SNAP",
            CorporateAdjustment.note.contains("quantity still does not match"))))

    _import(db, xml("20240115", 100))
    assert qty_notes()
    _import(db, xml("20240601", 10))
    db.expire_all()
    assert qty_notes() == []
    _import(db, xml("20240115", 100))
    db.expire_all()
    assert qty_notes() == []
    _import(db, xml("20240701", None))
    db.expire_all()
    notes = qty_notes()
    assert len(notes) == 1
    assert "0" in (notes[0].note or "")
    marker = db.scalar(select(IbkrFlexFact).where(
        IbkrFlexFact.section == "position", IbkrFlexFact.line_key == "__snapshot__",
        IbkrFlexFact.day == date(2024, 7, 1)))
    assert marker is not None


def test_stale_history_notes_clear_and_ordinary_notes_stay(db):
    xml = _wrap(_stmt("U0000001", (
        "<Trades>" + _trade("U0000001", "H1", "HOLD", "42", 1, 10, "20210802", "BUY", "O") + "</Trades>"
        "<OpenPositions></OpenPositions>"
    )))
    _import(db, xml)
    acct = _acct(db, "U0000001")
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="HISTORY", kind="CHECK", source="broker", status="needs_attention",
        external_key="attn:HISTORY:1003", note="2021 had no statement (IBKR 1003) and can be uploaded as XML."))
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="HISTORY", kind="CHECK", source="broker", status="needs_attention",
        external_key="attn:gap:2021-06-01:2021-12-31",
        note="IBKR has no statement for 2021-06-01..2021-12-31 (history gap)."))
    db.add(CorporateAdjustment(
        account_id=acct.id, symbol="HOLD", kind="CHECK", source="broker", status="needs_attention",
        external_key="attn:HOLD:outside", note="HOLD: opening fill is before the imported window."))
    db.commit()
    ibkr.finish_import_attention(db, acct.id, [])
    db.expire_all()
    left = {row.external_key: row.note for row in db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct.id, CorporateAdjustment.status == "needs_attention"))}
    assert "attn:HISTORY:1003" not in left
    assert "attn:gap:2021-06-01:2021-12-31" not in left
    assert "opening fill is before" in (left.get("attn:HOLD:outside") or "")
    ibkr.save_history(db, {"notes": ["IBKR 1003 for 2021 and 2019"], "gaps": [], "failed_windows": []})
    db.commit()
    sentence = ibkr.history_gap_sentence(db)
    assert "2021" not in sentence and "2019" in sentence


def _sections_xml() -> str:
    inner = (
        "<Trades>"
        + _trade("U0000001", "A1", "AMZN", "3691937", 1, 10, "20260901", "BUY", "O")
        + _trade("U0000001", "A2", "AMZN", "3691937", 1, 12, "20260902", "SELL", "C")
        + "</Trades><OpenPositions></OpenPositions>"
        '<CashReport>'
        '<CashReportCurrency currency="USD" endingCash="12" toDate="20261016" fromDate="20261001" />'
        '<CashReportCurrency currency="EUR" endingCash="3" toDate="20261016" fromDate="20261001" />'
        '<CashReportCurrency currency="BASE_SUMMARY" endingCash="15" toDate="20261016" fromDate="20261001" />'
        "</CashReport>"
        '<ChangeInNAV fromDate="20261001" toDate="20261016" startingValue="90" endingValue="100" '
        'mtm="1" realized="2" currency="USD" />'
        "<StatementOfFunds>"
        '<StatementOfFundsLine currency="USD" symbol="AMZN" conid="3691937" amount="4.5" '
        'reportDate="20261016" description="fee" transactionID="F1" />'
        "</StatementOfFunds>"
        "<InterestAccruals>"
        '<InterestAccrualsCurrency currency="USD" endingAccrualBalance="0.25" toDate="20261016" fromDate="20261001" />'
        "</InterestAccruals>"
        "<ChangeInDividendAccruals>"
        '<ChangeInDividendAccrual currency="USD" symbol="AMZN" conid="3691937" netAmount="1.1" '
        'payDate="20261016" exDate="20261001" code="Po" />'
        "</ChangeInDividendAccruals>"
        "<FIFOPerformanceSummaryInBase>"
        '<FIFOPerformanceSummaryUnderlying symbol="AMZN" conid="3691937" totalRealizedPnl="1" '
        'toDate="20261016" currency="USD" />'
        '<FIFOPerformanceSummaryInBase totalRealizedPnl="999" toDate="20261016" currency="USD" />'
        "</FIFOPerformanceSummaryInBase>"
        "<NetAssetValueInBase>"
        '<NAV currency="USD" reportDate="20261016" cash="12" stock="88" total="100" />'
        "</NetAssetValueInBase>"
    )
    return _wrap(_stmt("U0000001", inner, fr="20260901", to="20261016"))


def test_flex_sections_panel_and_reconciliation(db, tmp_path):
    xml = _sections_xml()
    dom = ibkr.parse_statements(xml)[0]
    path = tmp_path / "sections.xml"
    path.write_text(xml)
    streamed = ibkr.parse_statements_from_path(path)[0]
    assert ibkr.parsed_as_comparable(dom) == ibkr.parsed_as_comparable(streamed)
    assert len(dom.performance) == 1 and dom.performance[0].get("symbol") == "AMZN"
    assert dom.change_nav and dom.funds and dom.interest_accruals and dom.dividend_accruals and dom.nav
    assert dom.has_positions is True

    _import(db, xml)
    db.expire_all()
    acct = _acct(db, "U0000001")
    sections = set(db.scalars(select(IbkrFlexFact.section).where(IbkrFlexFact.account_id == acct.id)))
    assert sections >= {"cash", "nav", "change_nav", "funds", "interest", "div_accrual", "performance", "position"}
    realized = db.scalar(select(func.sum(IbkrFlexFact.amount)).where(
        IbkrFlexFact.account_id == acct.id, IbkrFlexFact.section == "performance"))
    assert realized == pytest.approx(1)
    from app.flex_facts import account_statement
    view = account_statement(db, acct.id)
    assert view["cash_line"] == "Cash: EUR 3.00 · USD 12.00 as of 2026-10-16, end of day"
    assert view["nav_line"] == "NAV 100.00 as of 2026-10-16, end of day"
    assert view["hint"] is None
    assert view["recon_line"] == (
        "Realized P&L: journal $2.00 vs IBKR $1.00 (diff +$1.00) · mismatch: AMZN · Positions: no open positions"
    )
    n_facts = db.scalar(select(func.count()).select_from(IbkrFlexFact))
    _import(db, xml)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(IbkrFlexFact)) == n_facts

    bare = _wrap(_stmt("U0000003", (
        "<Trades>" + _trade("U0000003", "Q1", "QQQ", "999", 1, 10, "20260901", "BUY", "O") + "</Trades>"
    ), to="20261016"))
    _import(db, bare)
    other = _acct(db, "U0000003")
    bare_view = account_statement(db, other.id)
    assert bare_view["hint"] == "Add the Cash Report / NAV sections to your Flex Query"
    assert bare_view["cash_line"] is None and bare_view["nav_line"] is None

    client = _client()
    page = client.get(f"/settings/accounts/{acct.id}/detail")
    assert page.status_code == 200
    assert "Cash: EUR 3.00 · USD 12.00 as of 2026-10-16, end of day" in page.text
    assert "NAV 100.00 as of 2026-10-16, end of day" in page.text
    assert "Realized P&amp;L: journal $2.00 vs IBKR $1.00 (diff +$1.00)" in page.text
    assert "Positions: no open positions" in page.text
    assert "Add the Cash Report / NAV sections to your Flex Query" not in page.text
    assert "Rebuild this account" in page.text and "Rebuild all IBKR accounts" in page.text
    other_page = client.get(f"/settings/accounts/{other.id}/detail")
    assert "Add the Cash Report / NAV sections to your Flex Query" in other_page.text
    modal = client.get(f"/settings/accounts/{acct.id}/ibkr-rebuild?scope=all")
    assert modal.status_code == 200 and "Type REBUILD to confirm." in modal.text
    assert "Nothing is downloaded from IBKR." in modal.text
    assert client.get("/settings/accounts/999999/ibkr-rebuild").status_code == 404


def test_nav_overlay_leaves_stored_balance_points(db):
    ib = Account(name="IBKR U0000009", broker="ibkr", source_key="ibkr_flex", currency="USD",
                 external_ref="U0000009", kind="margin")
    other = Account(name="T212", broker="trading212", source_key="t212", currency="USD")
    db.add_all([ib, other])
    db.flush()
    for day, bal in ((date(2026, 1, 1), 10.0), (date(2026, 1, 2), 20.0), (date(2026, 1, 3), 30.0)):
        db.add(BalancePoint(
            account_id=ib.id, currency="USD", day=day, net_deposits=10, realized_cum=0,
            dividends_cum=0, other_cum=0, unrealized=0, balance=bal))
    db.add(BalancePoint(
        account_id=other.id, currency="USD", day=date(2026, 1, 2), net_deposits=1, realized_cum=0,
        dividends_cum=0, other_cum=0, unrealized=0, balance=5))
    db.add(IbkrFlexFact(
        account_id=ib.id, section="nav", day=date(2026, 1, 2), currency="USD",
        symbol="", conid="", line_key="", amount=99))
    db.commit()
    stored = [(row.account_id, row.day, row.balance) for row in db.scalars(select(BalancePoint))]
    one = series(db, ib.id, "USD")
    assert one["balance"] == [10.0, 99.0, 30.0]
    assert one["latest"]["balance"] == 30.0
    assert one["total_return"] == pytest.approx(20.0)
    assert one["check"] is None
    combined = series(db, None, "USD")
    assert 99.0 not in combined["balance"]
    other_series = series(db, other.id, "USD")
    assert other_series["balance"] == [5.0]
    assert [(row.account_id, row.day, row.balance) for row in db.scalars(select(BalancePoint))] == stored


def test_rebuild_needs_confirmation_and_is_idempotent(db):
    xml = _wrap(
        _stmt("U0000001", (
            "<Trades>" + _trade("U0000001", "R1", "REB", "4242", 3, 10, "20240102", "BUY", "O") + "</Trades>"
            "<OpenPositions>"
            '<OpenPosition accountId="U0000001" currency="USD" assetCategory="STK" symbol="REB" conid="4242" '
            'position="3" levelOfDetail="SUMMARY" reportDate="20261016" />'
            "</OpenPositions>"
        )),
        _stmt("U0000002", (
            "<Trades>" + _trade("U0000002", "R2", "KEEP", "4243", 1, 8, "20240103", "BUY", "O") + "</Trades>"
            "<OpenPositions></OpenPositions>"
        )),
    )
    _import(db, xml)
    first = _acct(db, "U0000001")
    second = _acct(db, "U0000002")
    schwab = Account(name="Schwab", broker="schwab", source_key="schwab_csv", currency="USD")
    db.add(schwab)
    db.flush()
    db.add(Execution(
        account_id=schwab.id, source="schwab_csv", external_id="s1", match_key="s1",
        symbol="AAA", underlying="AAA", asset_type="STOCK", side="BUY", quantity=1, price=10,
        fees=0, executed_at=datetime(2024, 1, 2, 15), time_known=True, seq=0, kind="TRADE",
        position_effect="OPEN", multiplier=1))
    db.commit()
    payloads = db.scalar(select(func.count()).select_from(SyncPayload))
    kept_ids = [row.id for row in db.scalars(select(Execution).where(Execution.account_id == second.id))]
    schwab_n = db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == schwab.id))
    ib_n = db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == first.id))

    client = _client()
    bad = client.post("/settings/brokers/ibkr/rebuild", data={
        "account_id": str(first.id), "confirm": "nope", "scope": "account"}, follow_redirects=True)
    assert "Nothing was deleted" in bad.text
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == first.id)) == ib_n

    good = client.post("/settings/brokers/ibkr/rebuild", data={
        "account_id": str(first.id), "confirm": "REBUILD", "scope": "account"}, follow_redirects=True)
    assert good.status_code == 200
    assert "from stored statements." in good.text, good.text[good.text.find("flash"):][:600]
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == first.id)) == ib_n
    assert [row.id for row in db.scalars(select(Execution).where(Execution.account_id == second.id))] == kept_ids
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == schwab.id)) == schwab_n
    assert db.scalar(select(func.count()).select_from(SyncPayload)) == payloads
    trade = db.scalar(select(Trade).where(Trade.account_id == first.id, Trade.symbol == "REB"))
    assert trade is not None and trade.quantity == pytest.approx(3)

    again = client.post("/settings/brokers/ibkr/rebuild", data={
        "account_id": str(first.id), "confirm": first.name, "scope": "all"}, follow_redirects=True)
    assert "Type REBUILD" in again.text and "Nothing was deleted" in again.text
    all_ok = client.post("/settings/brokers/ibkr/rebuild", data={
        "account_id": str(first.id), "confirm": "REBUILD", "scope": "all"}, follow_redirects=True)
    assert "from stored statements." in all_ok.text
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SyncPayload)) == payloads
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.account_id == schwab.id)) == schwab_n
    assert db.scalar(select(Trade).where(Trade.account_id == second.id, Trade.symbol == "KEEP")) is not None
    status = client.get("/settings/brokers/ibkr/rebuild/status")
    assert "Rebuild finished" in status.text


def test_performance_periods_do_not_double_count():
    from datetime import date as _d
    from app.flex_facts import _non_overlapping_periods
    year = IbkrFlexFact(account_id=1, section="performance", day=_d(2025, 6, 23), symbol="AAA",
                        conid="1", line_key="20240624", amount=10.0)
    window = IbkrFlexFact(account_id=1, section="performance", day=_d(2025, 3, 1), symbol="AAA",
                          conid="1", line_key="20250101", amount=4.0)
    later = IbkrFlexFact(account_id=1, section="performance", day=_d(2026, 6, 23), symbol="AAA",
                         conid="1", line_key="20250624", amount=5.0)
    kept = _non_overlapping_periods([year, window, later])
    assert sorted(r.amount for r in kept) == [5.0, 10.0]
