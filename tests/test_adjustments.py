"""Stored splits and renames, needs-attention, and raw sync files. Synthetic data only."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adjustments import (
    detect_and_apply, events_from_activities, events_from_schwab, record_payload, save_manual,
)
from app.instruments import ExecRecord
from app.models import Account, CorporateAdjustment, Execution, SyncPayload, Trade
from app.services import ingest_records, rebuild_trades


def _acct(db, name="Journal"):
    acct = Account(name=name, broker="schwab", currency="USD")
    db.add(acct)
    db.flush()
    return acct


def _buy(db, acct, sym, qty, price, day=15, month=1, year=2024):
    ingest_records(db, acct.id, "schwab_csv", [ExecRecord(
        external_id=f"b-{sym}-{day}", symbol=sym, underlying=sym, asset_type="STOCK", side="BUY",
        quantity=qty, price=price, executed_at=datetime(year, month, day, 15, 0), currency="USD")])
    return acct


def _web(db):
    from app.main import create_app
    client = TestClient(create_app())
    assert client.post("/login", data={"password": "test-pass"}, follow_redirects=False).status_code == 303
    return client


def test_wmt_three_for_one_adjusts_qty_and_price_not_cost(db):
    """0.3892773 shares before the 31 Jan 2024 3-for-1 become 1.1678319. Cost basis is unchanged."""
    acct = _acct(db)
    qty, price = 0.3892773, 48.0
    _buy(db, acct, "WMT", qty, price)
    fetch = lambda sym, start, end: [(datetime(2024, 1, 31, 14, 30), 3.0)] if sym == "WMT" else []
    assert detect_and_apply(db, acct.id, fetch=fetch)
    assert detect_and_apply(db, acct.id, fetch=fetch) == []
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 1
    rebuild_trades(db, [acct.id])
    db.commit()
    trade = db.scalar(select(Trade).where(Trade.symbol == "WMT"))
    assert trade.open_quantity == pytest.approx(1.1678319)
    assert trade.entry_price == pytest.approx(price / 3)
    assert trade.cost_basis == pytest.approx(qty * price)
    assert trade.gross_pnl == pytest.approx(0)
    page = _web(db).get(f"/trades/{trade.id}").text
    assert "split" in page
    assert "cost basis is unchanged" in page
    assert "Split ×3" in page


def test_split_is_not_applied_when_the_broker_quantity_already_matches(db):
    acct = _acct(db)
    _buy(db, acct, "WMT", 1.1678319, 16.0)
    fetch = lambda *_a, **_k: [(datetime(2024, 1, 31, 14, 30), 3.0)]
    detect_and_apply(db, acct.id, fetch=fetch, positions={"WMT": 1.1678319})
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade))
    assert trade.open_quantity == pytest.approx(1.1678319)
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 0


def test_unknown_ratio_flags_attention_and_a_manual_split_is_idempotent(db):
    acct = _acct(db)
    _buy(db, acct, "WMT", 0.3892773, 48.0)
    lines = detect_and_apply(db, acct.id, fetch=lambda *_a, **_k: [], positions={"WMT": 1.1678319})
    assert any("does not match" in ln for ln in lines)
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind != "TRADE")) == 0
    rebuild_trades(db, [acct.id])
    db.commit()
    web = _web(db)
    assert "Needs attention" in web.get("/trades").text
    assert "Needs attention" in web.get("/settings").text
    saved = web.post("/settings/adjustments", data={
        "account_id": str(acct.id), "kind": "split", "symbol": "WMT", "ratio": "3 for 1",
        "when": "2024-01-31",
    }, follow_redirects=False)
    assert saved.status_code == 303
    again = web.post("/settings/adjustments", data={
        "account_id": str(acct.id), "kind": "split", "symbol": "WMT", "ratio": "3",
        "when": "2024-01-31",
    }, follow_redirects=False)
    assert again.status_code == 303
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 1
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade).where(Trade.symbol == "WMT"))
    assert trade.open_quantity == pytest.approx(1.1678319)
    assert trade.cost_basis == pytest.approx(0.3892773 * 48.0)
    assert db.scalar(select(func.count()).select_from(CorporateAdjustment).where(
        CorporateAdjustment.status == "needs_attention")) == 0
    # A later sync applies the stored row and does not scale a second time.
    detect_and_apply(db, acct.id, fetch=lambda *_a, **_k: [(datetime(2024, 1, 31), 3.0)],
                     positions={"WMT": 1.1678319})
    rebuild_trades(db, [acct.id])
    db.expire_all()
    trade = db.scalar(select(Trade).where(Trade.symbol == "WMT"))
    assert trade.open_quantity == pytest.approx(1.1678319)
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 1


def test_rename_merges_one_trade_and_an_ambiguous_one_does_not(db):
    acct = _acct(db)
    _buy(db, acct, "OLDX", 2, 10.0, day=10)
    ingest_records(db, acct.id, "schwab_csv", [ExecRecord(
        external_id="s", symbol="NEWX", underlying="NEWX", asset_type="STOCK", side="SELL",
        quantity=2, price=12, executed_at=datetime(2024, 2, 2, 15, 0), currency="USD")])
    save_manual(db, acct.id, "rename", "OLDX", datetime(2024, 2, 1), new_symbol="NEWX")
    save_manual(db, acct.id, "rename", "OLDX", datetime(2024, 2, 1), new_symbol="NEWX")
    rebuild_trades(db, [acct.id])
    trades = list(db.scalars(select(Trade)))
    assert [(t.symbol, t.status) for t in trades] == [("NEWX", "CLOSED")]
    assert trades[0].net_pnl == pytest.approx(4)
    # ambiguous: do not guess
    acct2 = _acct(db, "Other")
    _buy(db, acct2, "OLDY", 1, 5, day=3)
    when = datetime(2024, 3, 1)
    detect_and_apply(db, acct2.id, broker_events=[
        {"kind": "RENAME", "symbol": "OLDY", "new_symbol": "AAA", "when": when, "key": "r1"},
        {"kind": "RENAME", "symbol": "OLDY", "new_symbol": "BBB", "when": when, "key": "r2"},
    ])
    assert db.scalar(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct2.id, CorporateAdjustment.status == "needs_attention")) is not None
    assert db.scalar(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == acct2.id, CorporateAdjustment.kind == "RENAME",
        CorporateAdjustment.status == "applied")) is None


def test_broker_events_extract_ratio_or_ask(db):
    ev = events_from_activities([{
        "type": "STOCK_SPLIT", "description": "3 for 1", "id": "s1",
        "symbol": {"symbol": "WMT"}, "trade_date": "2024-01-31",
    }])
    assert ev[0]["kind"] == "SPLIT" and ev[0]["ratio"] == pytest.approx(3)
    missing = events_from_activities([{
        "type": "SPLIT", "description": "stock split", "id": "s2", "symbol": "XYZ",
        "trade_date": "2024-01-31",
    }])
    assert missing[0]["kind"] == "ATTENTION"
    found = events_from_schwab([{
        "activityId": 9, "time": "2024-01-31T14:30:00Z", "type": "JOURNAL",
        "description": "WMT 1 for 10 reverse split",
        "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "WMT"}, "amount": 1}],
    }])
    assert found[0]["kind"] == "SPLIT" and found[0]["ratio"] == pytest.approx(0.1)
    acct = _acct(db)
    _buy(db, acct, "WMT", 10, 5, year=2023, month=6)
    detect_and_apply(db, acct.id, broker_events=found)
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade))
    assert trade.open_quantity == pytest.approx(1)
    assert trade.entry_price == pytest.approx(50)
    assert trade.cost_basis == pytest.approx(50)


def test_sync_payload_is_gzipped_pruned_and_downloadable(db):
    acct = _acct(db)
    db.commit()
    for i in range(51):
        record_payload(db, acct.id, "snaptrade", {"n": i})
    db.commit()
    ids = list(db.scalars(select(SyncPayload.id).where(SyncPayload.account_id == acct.id)))
    assert len(ids) == 50
    assert min(ids) == 2  # the first insert was dropped
    big = record_payload(db, acct.id, "t212_csv", "x" * 5000, kind="csv", filename="history.csv")
    db.commit()
    assert big.gzipped and big.nbytes == 5000 and len(big.body) < 5000
    web = _web(db)
    anon = TestClient(__import__("app.main", fromlist=["create_app"]).create_app())
    assert anon.get(f"/settings/sync-payloads/{big.id}", follow_redirects=False).status_code == 303
    got = web.get(f"/settings/sync-payloads/{big.id}")
    assert got.status_code == 200 and got.content == b"x" * 5000
    assert "history.csv" in got.headers["content-disposition"]
    page = web.get("/settings").text
    assert "Download" in page and "history.csv" in page
    assert len(list(db.scalars(select(SyncPayload.id).where(SyncPayload.account_id == acct.id)))) == 50


def test_csv_import_stores_the_file_and_applies_a_split(db, monkeypatch):
    monkeypatch.setattr(
        "app.adjustments.default_fetch",
        lambda sym, start, end: [(datetime(2024, 1, 31, 14, 30), 3.0)] if sym == "WMT" else [])
    text = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/15/2024","Buy","WMT","WALMART INC","0.3892773","$48.00","$0.00","-$18.69"\n'
    )
    web = _web(db)
    up = web.post("/import/upload", data={"new_account_name": "CSV"},
                  files={"file": ("wmt.csv", text, "text/csv")}, follow_redirects=False)
    assert up.status_code == 303
    batch_id = up.headers["location"].rstrip("/").split("/")[-2]
    done = web.post(f"/import/{batch_id}/commit", follow_redirects=False)
    assert done.status_code == 303
    db.expire_all()
    trade = db.scalar(select(Trade).where(Trade.symbol == "WMT"))
    assert trade.open_quantity == pytest.approx(1.1678319)
    assert trade.entry_price == pytest.approx(16)
    assert trade.cost_basis == pytest.approx(0.3892773 * 48)
    payload = db.scalar(select(SyncPayload).where(SyncPayload.source == "schwab_csv"))
    assert payload is not None and b"WMT" in __import__("app.adjustments", fromlist=["payload_body"]).payload_body(payload)
