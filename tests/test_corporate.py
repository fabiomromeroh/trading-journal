"""Corporate actions and the Trading 212 position gaps. Fixtures only — no live account."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.corporate import (
    audit_instruments, chart_symbol, classify_csv_action, omit_duplicate_corporate, open_state,
    plan_adjustment, transfer_matching_basis,
)
from app.importers.t212_csv import parse as parse_csv
from app.instruments import ExecRecord
from app.metrics import entry_split
from app.models import Account, Execution, Instrument, Trade
from app.services import get_state, ingest_records, rebuild_trades
from app.sources import trading212 as t212
from app import symbolmap
from app.trade_builder import BuilderExec, build_trades

# Journal quantity vs Trading 212 quantity, from the live Settings card.
GAPS = {
    "CGC": (50.0, 5.0),
    "CNQ": (3.81696, 5.22612),
    "WMT": (1.26316, 2.04172),
    "SOLS": (0.0, 0.0758234),
    "HONAV": (0.0, 0.151647),
    "GGB": (6.98817, 0.0),
    "HON": (0.151647, 0.0),
    "PHIA": (0.282153, 0.0),
    "SOLV": (0.968258, 0.0),
}


def _ex(i, sym, side, qty, price, kind="TRADE", effect=None, day=1):
    return BuilderExec(
        id=i, account_id=1, symbol=sym, side=side, quantity=qty, price=price,
        executed_at=datetime(2024, 1, day, 15, 0), kind=kind, position_effect=effect,
    )


def _one(execs):
    built = build_trades(execs)
    assert built.orphans == []
    assert len(built.trades) == 1
    return built.trades[0]


def test_reverse_split_scales_quantity_and_price_without_pnl():
    t = _one([
        _ex(1, "CGC", "BUY", 50, 1),
        _ex(2, "CGC", "BUY", 0.1, 0, kind="SPLIT", effect="RATIO", day=2),
    ])
    assert t.status == "OPEN" and t.open_quantity == pytest.approx(5)
    assert t.entry_price == pytest.approx(10) and t.cost_basis == pytest.approx(50)
    assert t.gross_pnl == pytest.approx(0)
    assert any(f.role == "ADJUST" for f in t.fills)

    # A sale before the split keeps its dollar P&L; the leftover is the post-split size.
    held = _one([
        _ex(1, "CGC", "BUY", 50, 1),
        _ex(2, "CGC", "SELL", 10, 2, day=2),
        _ex(3, "CGC", "BUY", 0.1, 0, kind="SPLIT", effect="RATIO", day=3),
    ])
    assert held.open_quantity == pytest.approx(4) and held.gross_pnl == pytest.approx(10)
    assert held.cost_basis == pytest.approx(50)

    # An order-history split that reports the post-split size and price (no explicit ratio).
    priced = _one([
        _ex(1, "CGC", "BUY", 50, 1),
        _ex(2, "CGC", "BUY", 5, 10, kind="SPLIT", day=2),
    ])
    assert priced.open_quantity == pytest.approx(5) and priced.entry_price == pytest.approx(10)
    assert priced.gross_pnl == pytest.approx(0)


def test_split_without_a_position_or_a_ratio_is_orphaned():
    built = build_trades([_ex(1, "NVDA", "BUY", 1, 0, kind="SPLIT")])
    assert built.trades == []
    assert built.orphans and "no open position" in built.orphans[0].reason


def test_spin_off_and_free_shares_add_at_zero_cost():
    built = build_trades([
        _ex(1, "HON", "BUY", 1, 100),
        _ex(2, "SOLS", "BUY", 0.15, 0, kind="SPIN_OFF", day=2),
    ])
    by = {t.symbol: t for t in built.trades}
    assert by["HON"].open_quantity == pytest.approx(1) and by["HON"].gross_pnl == pytest.approx(0)
    assert by["SOLS"].open_quantity == pytest.approx(0.15) and by["SOLS"].entry_price == 0
    assert by["SOLS"].cost_basis == 0 and by["SOLS"].direction == "LONG"

    free = _one([
        _ex(1, "MMM", "BUY", 10, 10),
        _ex(2, "MMM", "BUY", 2, 0, kind="DISTRIBUTION", day=2),
    ])
    assert free.open_quantity == pytest.approx(12) and free.cost_basis == pytest.approx(100)
    assert free.entry_price == pytest.approx(100 / 12) and free.gross_pnl == pytest.approx(0)
    assert free.direction == "LONG"


def test_drip_adds_cost_but_is_not_a_discretionary_entry():
    t = _one([
        _ex(1, "MMM", "BUY", 10, 10),
        _ex(2, "MMM", "BUY", 1, 12, kind="DRIP", day=2),
    ])
    assert t.open_quantity == pytest.approx(11) and t.cost_basis == pytest.approx(112)
    assert t.gross_pnl == pytest.approx(0)
    fills = []
    for i, f in enumerate(t.fills):
        if f.role != "OPEN":
            continue
        kind = "DRIP" if f.price == 12 else "TRADE"
        fills.append(SimpleNamespace(
            position=i, role="OPEN", quantity=f.quantity, price=f.price, executed_at=f.executed_at,
            time_known=True, execution=SimpleNamespace(kind=kind, time_known=True)))
    sp = entry_split(SimpleNamespace(fills=fills))
    assert sp["init_qty"] == pytest.approx(10) and sp["init_price"] == pytest.approx(10)
    assert sp["add_qty"] == 0


def test_conversion_closes_at_cost_and_does_not_open_a_short():
    built = build_trades([
        _ex(1, "HON", "BUY", 0.151647, 200),
        _ex(2, "HON", "SELL", 0.151647, 999, kind="CONVERSION", day=2),
        _ex(3, "HONAV", "BUY", 0.151647, 200, kind="CONVERSION", day=2),
    ])
    by = {t.symbol: t for t in built.trades}
    assert by["HON"].status == "CLOSED" and by["HON"].gross_pnl == pytest.approx(0)
    assert by["HONAV"].status == "OPEN" and by["HONAV"].direction == "LONG"
    assert by["HONAV"].open_quantity == pytest.approx(0.151647)
    assert all(t.direction != "SHORT" for t in built.trades)


def test_reconcile_gaps_become_adjustments_and_match():
    """Before: the nine gaps. After: one estimated event each, and the open size equals Trading 212."""
    plans = []
    for sym, (journal, broker) in GAPS.items():
        plan = plan_adjustment(journal, broker, 10.0)
        assert plan is not None, sym
        plan.update(symbol=sym, journal=journal, broker=broker)
        plans.append(plan)
    transfer_matching_basis(plans)
    by = {p["symbol"]: p for p in plans}
    assert by["CGC"]["kind"] == "SPLIT" and by["CGC"]["quantity"] == pytest.approx(0.1) and by["CGC"]["ratio"]
    assert by["CNQ"]["side"] == "BUY" and by["CNQ"]["quantity"] == pytest.approx(5.22612 - 3.81696)
    assert by["WMT"]["side"] == "BUY" and by["WMT"]["price"] == 0
    assert by["SOLS"]["price"] == 0 and by["SOLS"]["quantity"] == pytest.approx(0.0758234)
    assert by["HONAV"]["price"] == pytest.approx(10)  # same share count as the HON close
    assert "HON" in by["HONAV"]["note"]
    for sym in ("GGB", "HON", "PHIA", "SOLV"):
        assert by[sym]["side"] == "SELL" and by[sym]["quantity"] == pytest.approx(GAPS[sym][0])
        assert by[sym]["price"] == pytest.approx(10)

    execs = []
    n = 1
    for sym, (journal, broker) in GAPS.items():
        if journal > 0:
            execs.append(_ex(n, sym, "BUY", journal, 10))
            n += 1
        p = by[sym]
        execs.append(_ex(n, sym, p["side"], p["quantity"], p["price"], kind=p["kind"],
                         effect="RATIO" if p["ratio"] else None, day=2))
        n += 1
    built = build_trades(execs)
    assert built.orphans == []
    open_qty = {}
    for t in built.trades:
        assert t.direction != "SHORT"
        if t.status == "OPEN":
            open_qty[t.symbol] = open_qty.get(t.symbol, 0.0) + t.open_quantity
    for sym, (_journal, broker) in GAPS.items():
        assert open_qty.get(sym, 0.0) == pytest.approx(broker), sym


def test_csv_imports_corporate_rows_and_dedups_by_id():
    text = """Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,Currency (Price / share),Exchange rate,Result,Total,Withholding tax,Stamp duty reserve tax,Notes,ID
Market buy,2024-01-15 15:00:00,US67066G1040,NVDA,NVIDIA,10,100,USD,1,,-1000,0,0,,B1
Stock split,2024-06-01 15:00:00,US67066G1040,NVDA,NVIDIA,20,50,USD,1,,0,0,0,2 for 1,S1
Spin-off,2024-07-01 15:00:00,US4385161066,HON,Honeywell,0.15,0,USD,1,,0,0,0,spin-off,P1
Dividend reinvestment,2024-08-01 15:00:00,US67066G1040,NVDA,NVIDIA,0.5,120,USD,1,,-60,0,0,,D1
Dividend (Ordinary),2024-08-01 15:00:00,US67066G1040,NVDA,NVIDIA,0,1.2,USD,1,,12,1.8,0,,DIV1
Deposit,2024-01-01 10:00:00,,,,,0,0,EUR,1,,500,0,0,,C1
Market buy,2024-01-15 15:00:00,US67066G1040,NVDA,NVIDIA,10,100,USD,1,,-1000,0,0,,B1
"""
    parsed = parse_csv(text)
    kinds = {r.external_id: r.kind for r in parsed.records}
    assert kinds == {"B1": "TRADE", "S1": "SPLIT", "P1": "SPIN_OFF", "D1": "DRIP"}
    split = next(r for r in parsed.records if r.external_id == "S1")
    assert split.position_effect == "RATIO" and split.quantity == pytest.approx(2) and split.price == 0
    assert "DIV1" not in kinds and "C1" not in kinds
    assert parsed.records[0].symbol == "NVDA" and parsed.records[0].currency == "USD"
    assert classify_csv_action("Currency conversion") is None


def test_transaction_rows_keep_shares_and_drop_cash():
    assert t212._transaction_record({"type": "DEPOSIT", "amount": 100, "currency": "EUR"}) is None
    rec = t212._transaction_record({
        "type": "STOCK_SPLIT", "id": "tx1", "ticker": "CGC_US_EQ", "quantity": 5,
        "price": 10, "dateTime": "2024-06-01T15:00:00Z",
        "instrument": {"isin": "CA1380351009", "currency": "USD"},
    })
    assert isinstance(rec, ExecRecord) and rec.kind == "SPLIT" and rec.symbol == "CGC"
    assert rec.external_id == "tx:tx1" and rec.quantity == pytest.approx(5)


def test_instrument_check_fixes_isin_and_keeps_a_rename(db):
    assert chart_symbol("DMYI", "US", "IONQ") == "IONQ"
    assert chart_symbol("AAPL", "US", "Apple Inc") == "AAPL"
    bare = Instrument(source_key="t212", broker_symbol="VOD_EQ", isin="GB00BH4HKS39", symbol="VOD",
                      exchange=None, currency="GBX", price_symbol=None, asset_type="STOCK")
    renamed = Instrument(source_key="t212", broker_symbol="DMYI_US_EQ", isin="US46222L1089", symbol="DMYI",
                         exchange="US", currency="USD", price_symbol="IONQ", asset_type="STOCK")
    listed = Instrument(source_key="t212", broker_symbol="AAPL_US_EQ", isin="DE0000000000", symbol="AAPL",
                        exchange="US", currency="USD", price_symbol="AAPL", asset_type="STOCK")
    db.add_all([bare, renamed, listed])
    db.flush()
    issues = {i["symbol"]: i for i in audit_instruments(db, fix=True)}
    assert bare.exchange == "LSE" and bare.currency == "GBP" and bare.price_symbol == "VOD.L"
    assert "exchange" in " ".join(issues["VOD"]["fixed"]).lower() or issues["VOD"]["fixed"]
    assert renamed.symbol == "DMYI" and renamed.price_symbol == "IONQ"
    assert any("IONQ" in n for n in issues["DMYI"]["notes"])
    assert listed.exchange == "US"
    assert any("listing kept" in n for n in issues["AAPL"]["notes"])


def test_adjustment_page_save_and_remove(db):
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU")
    db.add(acct)
    db.flush()
    inst = symbolmap.upsert_t212(db, "CGC_US_EQ", "CA1380351009", "Canopy", "USD")
    ingest_records(db, acct.id, "t212", [ExecRecord(
        external_id="1", symbol="CGC", underlying="CGC", asset_type="STOCK", side="BUY",
        quantity=50, price=1, executed_at=datetime(2024, 1, 15, 15, 0), currency="USD",
        instrument_id=inst.id)])
    src = t212.Trading212Source(sleep=lambda *_a, **_k: None)
    lines = src.apply_adjustments(db, acct, [{
        "quantity": 5, "instrument": {"ticker": "CGC_US_EQ", "isin": "CA1380351009", "currency": "USD"},
    }])
    assert any(ln.startswith("CGC:") for ln in lines)
    rebuild_trades(db, [acct.id])
    db.commit()
    trade = db.scalar(select(Trade).where(Trade.symbol == "CGC"))
    assert trade.open_quantity == pytest.approx(5) and trade.gross_pnl == pytest.approx(0)
    adj = db.scalar(select(Execution).where(Execution.external_id == "recon:CGC"))
    assert adj.kind == "SPLIT" and adj.position_effect == "RATIO"
    adj_id = adj.id

    from app.main import create_app
    client = TestClient(create_app())
    assert client.post("/login", data={"password": "test-pass"}, follow_redirects=False).status_code == 303
    page = client.get(f"/trades/{trade.id}").text
    assert "US · CGC · USD" in page
    assert "estimated" in page and ">split<" in page
    assert f"/trades/{trade.id}/adjustments/{adj_id}" in page
    assert "symbol=CGC" in page  # chart and TradingView use the Yahoo symbol

    saved = client.post(f"/trades/{trade.id}/adjustments/{adj_id}", data={
        "action": "save", "quantity": "0.1", "price": "0", "when": "2024-03-01",
    }, follow_redirects=False)
    assert saved.status_code == 303
    db.expire_all()
    adj = db.get(Execution, adj_id)
    assert "user_edited" in (adj.raw or "")
    assert adj.executed_at.date().isoformat() == "2024-03-01"
    # A later sync must not overwrite the edited row.
    src.apply_adjustments(db, acct, [{
        "quantity": 5, "instrument": {"ticker": "CGC_US_EQ", "isin": "CA1380351009", "currency": "USD"},
    }])
    db.expire_all()
    assert db.get(Execution, adj_id).executed_at.date().isoformat() == "2024-03-01"

    gone = client.post(f"/trades/{trade.id}/adjustments/{adj_id}", data={"action": "remove"},
                       follow_redirects=False)
    assert gone.status_code == 303
    db.expire_all()
    assert db.scalar(select(Execution).where(Execution.id == adj_id)) is None
    state = __import__("json").loads(get_state(db, "t212:recon") or "{}")
    assert "CGC" in state["dismissed"]
    again = src.apply_adjustments(db, acct, [{
        "quantity": 5, "instrument": {"ticker": "CGC_US_EQ", "isin": "CA1380351009", "currency": "USD"},
    }])
    assert any("removed by you" in ln for ln in again)
    assert db.scalar(select(Execution).where(Execution.external_id == "recon:CGC")) is None


def test_open_state_ignores_recon_rows():
    buy = SimpleNamespace(
        id=1, account_id=1, external_id="1", symbol="WMT", side="BUY", quantity=1.26316, price=10,
        executed_at=datetime(2024, 1, 1), fees=0, multiplier=1, position_effect=None, kind="TRADE",
        time_known=True, seq=0)
    recon = SimpleNamespace(
        id=2, account_id=1, external_id="recon:WMT", symbol="WMT", side="BUY", quantity=0.77856, price=0,
        executed_at=datetime(2024, 6, 1), fees=0, multiplier=1, position_effect=None, kind="ADJUST",
        time_known=False, seq=0)
    qty, avg = open_state([buy, recon])
    assert qty == pytest.approx(1.26316) and avg == pytest.approx(10)


def test_omit_duplicate_corporate_same_day(db):
    acct = Account(name="T", broker="trading212", currency="EUR", source_key="t212")
    db.add(acct)
    db.flush()
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="1", match_key="k", symbol="NVDA", underlying="NVDA",
        asset_type="STOCK", side="BUY", quantity=2, price=0, executed_at=datetime(2024, 6, 1, 15),
        kind="SPLIT", position_effect="RATIO"))
    db.flush()
    fresh = ExecRecord(external_id="S1", symbol="NVDA", underlying="NVDA", asset_type="STOCK", side="BUY",
                       quantity=2, price=0, executed_at=datetime(2024, 6, 1, 16), kind="SPLIT",
                       trade_date=datetime(2024, 6, 1).date())
    other = ExecRecord(external_id="P1", symbol="SOLS", underlying="SOLS", asset_type="STOCK", side="BUY",
                       quantity=0.1, price=0, executed_at=datetime(2024, 6, 1, 16), kind="SPIN_OFF",
                       trade_date=datetime(2024, 6, 1).date())
    kept = omit_duplicate_corporate(db, acct.id, [fresh, other])
    assert [r.external_id for r in kept] == ["P1"]
