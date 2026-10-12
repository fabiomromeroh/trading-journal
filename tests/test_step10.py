"""Step 10: real Trading 212 rows only. No estimated fills. Fixtures, no live key."""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.corporate import classify_csv_action
from app.importers.t212_csv import parse as parse_csv
from app.models import Account, Execution, Instrument, Trade
from app.services import rebuild_trades
from app.sources import trading212 as t212

ISIN = "US9311421039"


def _fill(fid, ticker, qty, when):
    return {
        "order": {
            "id": int(fid) + 100, "side": "BUY", "status": "FILLED", "type": "MARKET", "ticker": ticker,
            "instrument": {"ticker": ticker, "isin": ISIN, "currency": "USD", "name": "Walmart"},
        },
        "fill": {
            "id": str(fid), "filledAt": when, "price": 60.0, "quantity": qty, "type": "TRADE",
            "walletImpact": {"currency": "USD", "fxRate": 1, "netValue": -(qty * 60), "taxes": []},
        },
    }


def test_paged_follows_a_cursor_through_an_empty_page():
    """A short or empty page must not end the walk while the body still names a next cursor."""
    def handler(req: httpx.Request) -> httpx.Response:
        cursor = req.url.params.get("cursor")
        if cursor == "mid":
            return httpx.Response(200, json={"items": [], "cursor": "old"})
        if cursor == "old":
            return httpx.Response(200, json={"items": [{"fill": {"id": "oldest"}}], "nextPagePath": None})
        return httpx.Response(200, json={"items": [{"fill": {"id": "new"}}], "nextPagePath": None, "cursor": "mid"})

    cl = t212.T212Client("k", "s", transport=httpx.MockTransport(handler), sleep=lambda *_a, **_k: None)
    pages = list(cl.paged("/equity/history/orders"))
    cl.close()
    assert [len(p) for p in pages] == [1, 0, 1]
    assert pages[0][0]["fill"]["id"] == "new"
    assert pages[2][0]["fill"]["id"] == "oldest"


def test_hunt_imports_pre_split_ticker_from_the_oldest_page(db):
    """WMT_US_EQ and the older bare WMT ticker share US9311421039. The oldest page is only reached by cursor."""
    q1, q2 = 1.26316, 0.77856
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="WMT_US_EQ", isin=ISIN, symbol="WMT",
                      exchange="US", currency="USD", price_symbol="WMT", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="1", match_key="1", symbol="WMT", underlying="WMT",
        asset_type="STOCK", side="BUY", quantity=q1, price=60, executed_at=datetime(2024, 3, 1, 15),
        currency="USD", instrument_id=inst.id, fees=0))
    db.commit()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/equity/metadata/instruments"):
            return httpx.Response(200, json=[
                {"ticker": "WMT_US_EQ", "isin": ISIN, "shortName": "WMT"},
                {"ticker": "WMT", "isin": ISIN, "shortName": "WMT"},
            ])
        if path.endswith("/equity/history/orders"):
            ticker = req.url.params.get("ticker")
            cursor = req.url.params.get("cursor")
            if ticker == "WMT_US_EQ":
                return httpx.Response(200, json={"items": [_fill("1", "WMT_US_EQ", q1, "2024-03-01T15:00:00Z")]})
            if ticker == "WMT":
                if cursor == "old":
                    return httpx.Response(200, json={"items": [_fill("2", "WMT", q2, "2023-06-01T15:00:00Z")]})
                return httpx.Response(200, json={"items": [], "cursor": "old"})
            return httpx.Response(400, json={"message": "unknown ticker"})
        if path.endswith("/equity/history/transactions"):
            return httpx.Response(200, json={"items": [
                {"type": "DEPOSIT", "amount": 100, "currency": "EUR", "id": "dep1"},
            ]})
        if path.endswith("/equity/history/dividends"):
            return httpx.Response(200, json={"items": [
                {"type": "ORDINARY", "ticker": "WMT_US_EQ", "quantity": q1 + q2, "amount": 1.2,
                 "paidOn": "2024-09-01T00:00:00Z", "reference": "cash1",
                 "instrument": {"isin": ISIN, "currency": "USD"}},
            ]})
        if path.endswith("/equity/positions"):
            return httpx.Response(200, json=[{
                "quantity": q1 + q2, "instrument": {"ticker": "WMT_US_EQ", "isin": ISIN, "currency": "USD"},
            }])
        if path.endswith("/equity/history/exports"):
            return httpx.Response(404, json={"message": "no export"})
        return httpx.Response(404, json={"message": "no"})

    cl = t212.T212Client("k", "s", transport=httpx.MockTransport(handler), sleep=lambda *_a, **_k: None)
    src = t212.Trading212Source(sleep=lambda *_a, **_k: None)
    data = src.run_hunt(db, "WMT", client=cl)
    assert data["status"] == "done", data.get("note")
    db.expire_all()
    rows = {e.external_id: e for e in db.scalars(select(Execution))}
    assert rows["2"].symbol == "WMT" and rows["2"].quantity == pytest.approx(q2)
    assert rows["2"].price == pytest.approx(60)
    assert "div:cash1" not in rows and "cash1" not in rows
    assert not any(str(k).startswith("recon:") for k in rows)
    listed = {r["id"]: r for r in data["rows"]}
    assert listed["2"]["imported"] is True
    assert listed["1"]["in_journal"] is True
    assert any(r["id"] == "cash1" and r["in_journal"] is False and r["imported"] is False for r in data["rows"])
    assert "WMT" not in t212.gap_index(db)
    assert not any("secret" in json.dumps(data).lower() for _ in [0])


def test_drip_row_imports_shares_and_csv_shapes():
    cash = t212._dividend_record({
        "type": "ORDINARY", "ticker": "WMT_US_EQ", "quantity": 2.04172, "amount": 0.5,
        "paidOn": "2024-09-01T00:00:00Z", "reference": "cash",
        "instrument": {"isin": ISIN},
    })
    assert cash is None
    drip = t212._dividend_record({
        "type": "DRIP", "ticker": "WMT_US_EQ", "reinvestedQuantity": 0.01, "price": 60,
        "paidOn": "2024-12-01T00:00:00Z", "reference": "drip1",
        "instrument": {"isin": ISIN, "currency": "USD"},
    })
    assert drip is not None and drip.kind == "DRIP" and drip.quantity == pytest.approx(0.01)
    assert drip.external_id == "div:drip1" and drip.raw["price_absent"] is False
    assert classify_csv_action("Dividend (DRIP)") == ("DRIP", "BUY")
    assert classify_csv_action("Limit buy") == ("TRADE", "BUY")
    assert classify_csv_action("Stop buy") == ("TRADE", "BUY")
    assert classify_csv_action("Stock split close") == ("CONVERSION", "SELL")
    assert classify_csv_action("Stock split open") == ("DISTRIBUTION", "BUY")
    assert classify_csv_action("Dividend (Ordinary)") is None

    text = (
        "Action;Time;ISIN;Ticker;Name;No. of shares;Price / share;Currency (Price / share);"
        "Exchange rate;Result;Total;Notes;ID\n"
        "Limit buy;26.02.2024 14:30:00;US9311421039;WMT;Walmart;0,77856;60,5;USD;1;-47,10;;real limit;L1\n"
        "Stop buy;2024/03/02 15:00:00;US9311421039;WMT;Walmart;1;61;USD;1;-61;;stop;S1\n"
        "Dividend (DRIP);2024-12-01 15:00:00;US9311421039;WMT;Walmart;0,01;60;USD;1;-0,60;;reinvested;D1\n"
        "Stock split close;2024-02-26 14:30:00;US9311421039;WMT;Walmart;1,26316;0;USD;1;0;;close;C1\n"
        "Stock split open;2024-02-26 14:30:01;US9311421039;WMT;Walmart;3,78948;20;USD;1;0;;open;O1\n"
        "Market buy;2024-03-01 15:00:00;US9311421039;WMT;Walmart;1;60;USD;1;-60;;dup;L1\n"
    )
    parsed = parse_csv(text)
    by = {r.external_id: r for r in parsed.records}
    assert set(by) == {"L1", "S1", "D1", "C1", "O1"}
    assert by["L1"].kind == "TRADE" and by["L1"].quantity == pytest.approx(0.77856)
    assert by["L1"].price == pytest.approx(60.5)
    assert by["L1"].executed_at == datetime(2024, 2, 26, 14, 30)
    assert by["D1"].kind == "DRIP" and by["D1"].quantity == pytest.approx(0.01)
    assert by["C1"].kind == "CONVERSION" and by["C1"].side == "SELL"
    assert by["C1"].quantity == pytest.approx(1.26316)
    assert by["O1"].kind == "DISTRIBUTION" and by["O1"].quantity == pytest.approx(3.78948)
    assert by["S1"].executed_at == datetime(2024, 3, 2, 15, 0)
    assert parsed.skipped["duplicate id"] == 1


def test_estimated_row_is_replaced_by_a_real_fill(db):
    """An unedited estimate is deleted even while the gap remains. A real fill then removes one you edited."""
    q_journal, q_gap, q_broker = 1.26316, 0.77856, 2.04172
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="WMT_US_EQ", isin=ISIN, symbol="WMT",
                      exchange="US", currency="USD", price_symbol="WMT", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="real", match_key="real", symbol="WMT", underlying="WMT",
        asset_type="STOCK", side="BUY", quantity=q_journal, price=55, executed_at=datetime(2024, 1, 15, 15),
        currency="USD", instrument_id=inst.id, fees=0))
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="recon:WMT", match_key="recon:WMT", symbol="WMT",
        underlying="WMT", asset_type="STOCK", side="BUY", quantity=q_gap, price=0, kind="ADJUST",
        executed_at=datetime(2024, 6, 1), currency="USD", instrument_id=inst.id, fees=0,
        raw=json.dumps({"estimated": True, "note": "invented"})))
    db.flush()
    held = [{"quantity": q_broker, "instrument": {"ticker": "WMT_US_EQ", "isin": ISIN, "currency": "USD"}}]
    src = t212.Trading212Source(sleep=lambda *_a, **_k: None)
    lines = src.retire_estimated(db, acct, held)
    assert db.scalar(select(Execution).where(Execution.external_id == "recon:WMT")) is None
    assert any("removed" in ln for ln in lines)
    gaps = src.reconcile(db, None, acct, positions=held)
    assert any(g.startswith("WMT: missing 0.77856 sh vs Trading 212") for g in gaps)

    db.add(Execution(
        account_id=acct.id, source="t212", external_id="recon:WMT", match_key="recon:WMT", symbol="WMT",
        underlying="WMT", asset_type="STOCK", side="BUY", quantity=q_gap, price=1, kind="ADJUST",
        executed_at=datetime(2024, 6, 2), currency="USD", instrument_id=inst.id, fees=0,
        raw=json.dumps({"estimated": True, "user_edited": True, "note": "I typed this"})))
    db.flush()
    kept = src.retire_estimated(db, acct, held)
    row = db.scalar(select(Execution).where(Execution.external_id == "recon:WMT"))
    assert row is not None and "flagged" in (row.raw or "")
    assert any("you edited it" in ln for ln in kept)

    db.add(Execution(
        account_id=acct.id, source="t212", external_id="fill-2", match_key="fill-2", symbol="WMT", underlying="WMT",
        asset_type="STOCK", side="BUY", quantity=q_gap, price=70, executed_at=datetime(2024, 8, 1, 15),
        currency="USD", instrument_id=inst.id, fees=0))
    db.flush()
    src.retire_estimated(db, acct, held)
    assert db.scalar(select(Execution).where(Execution.external_id == "recon:WMT")) is None
    assert db.scalar(select(Execution).where(Execution.external_id == "fill-2")).price == pytest.approx(70)
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade).where(Trade.symbol == "WMT"))
    assert trade.open_quantity == pytest.approx(q_broker)
    assert src.reconcile(db, None, acct, positions=held) == []
