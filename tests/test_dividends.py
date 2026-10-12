"""Cash dividends: T212 API mapping (GBX, pagination), SnapTrade/Schwab rows, per-trade window, total return."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.dividends import DividendIn, for_trade, ingest, scale_gbx, total
from app.instruments import ExecRecord
from app.models import Account, Dividend, FxRate, Trade
from app.services import ingest_records, rebuild_trades
from app.sources import trading212 as t212
from app.sources.base import SyncContext
from app.sources.snaptrade import parse_activities, parse_dividends
from tests.test_trading212 import KEY, SECRET, FakeT212, enc, load, nop_sleep  # noqa: F401


def test_scale_gbx():
    assert scale_gbx(1250.0, "GBX") == (12.50, "GBP")
    assert scale_gbx(1.5, "USD") == (1.5, "USD")
    assert scale_gbx(None, "GBX") == (None, "GBP")


def test_t212_dividend_record_us_and_gbx():
    us = {
        "ticker": "AAPL_US_EQ", "type": "ORDINARY", "amount": 2.22, "amountInEuro": 2.22,
        "currency": "EUR", "tickerCurrency": "USD", "grossAmountPerShare": 0.25,
        "quantity": 10.0, "paidOn": "2026-09-10T00:00:00Z", "reference": "DIV-AAPL-1",
        "instrument": {"ticker": "AAPL_US_EQ", "isin": "US0378331005", "currency": "USD", "name": "Apple"},
    }
    r = t212.dividend_record(us)
    assert r is not None and r.symbol == "AAPL" and r.external_id == "DIV-AAPL-1"
    assert r.amount == pytest.approx(2.22) and r.currency == "EUR"
    assert r.amount_account == pytest.approx(2.22) and r.account_currency == "EUR"
    assert r.gross_per_share == pytest.approx(0.25) and r.quantity == pytest.approx(10.0)
    gbx = {
        "ticker": "VUSAl_EQ", "type": "ORDINARY", "amount": 11.65, "amountInEuro": 11.65,
        "currency": "EUR", "tickerCurrency": "GBX", "grossAmountPerShare": 12.5,
        "quantity": 10.0, "paidOn": "2026-09-15T00:00:00Z", "reference": "DIV-VUSA-1",
        "instrument": {"ticker": "VUSAl_EQ", "isin": "IE00B3XXRP09", "currency": "GBX", "name": "VUSA"},
    }
    v = t212.dividend_record(gbx)
    assert v.symbol == "VUSA" and v.currency == "EUR" and v.amount == pytest.approx(11.65)
    assert v.gross_per_share == pytest.approx(0.125)  # pence -> pounds
    assert t212.dividend_record({"ticker": "X", "amount": 1}) is None  # no id / date


def test_t212_sync_imports_paginated_dividends(db, enc):
    fake = FakeT212()
    fake.div_pages = [
        {"items": [{
            "ticker": "AAPL_US_EQ", "type": "ORDINARY", "amount": 2.22, "currency": "EUR",
            "tickerCurrency": "USD", "grossAmountPerShare": 0.25, "quantity": 2.5,
            "paidOn": "2026-09-10T00:00:00Z", "reference": "DIV-AAPL-1",
            "instrument": {"ticker": "AAPL_US_EQ", "isin": "US0378331005", "currency": "USD"},
        }], "nextPagePath": "/api/v0/equity/history/dividends?cursor=1&limit=50"},
        {"items": [{
            "ticker": "VUSAl_EQ", "type": "ORDINARY", "amount": 11.65, "currency": "EUR",
            "tickerCurrency": "GBX", "grossAmountPerShare": 12.5, "quantity": 10.0,
            "paidOn": "2026-09-15T00:00:00Z", "reference": "DIV-VUSA-1",
            "instrument": {"ticker": "VUSAl_EQ", "isin": "IE00B3XXRP09", "currency": "GBX"},
        }], "nextPagePath": None},
    ]
    t212.save_credentials(db, KEY, SECRET, "live")
    db.commit()
    src = t212.Trading212Source(transport=fake.transport(), sleep=nop_sleep)
    res = src.sync(db, SyncContext())
    assert "dividends 2 read, 2 new" in (res.message or "")
    rows = list(db.scalars(select(Dividend)))
    assert {d.external_id for d in rows} == {"DIV-AAPL-1", "DIV-VUSA-1"}
    vusa = next(d for d in rows if d.symbol == "VUSA")
    assert vusa.gross_per_share == pytest.approx(0.125) and vusa.amount == pytest.approx(11.65)
    paths = [c.url.path for c in fake.calls if c.url.path.endswith("/history/dividends")]
    assert len(paths) == 2
    # idempotent
    src.sync(db, SyncContext())
    assert db.query(Dividend).count() == 2


def test_per_trade_window_and_total(db):
    acct = Account(name="T", broker="trading212", currency="EUR", source_key="t212")
    db.add(acct)
    db.flush()
    recs = [
        ExecRecord(external_id="1", symbol="AAPL", underlying="AAPL", asset_type="STOCK", side="BUY",
                   quantity=10, price=100, executed_at=datetime(2026, 9, 2, 14, 0), currency="USD"),
        ExecRecord(external_id="2", symbol="AAPL", underlying="AAPL", asset_type="STOCK", side="SELL",
                   quantity=10, price=110, executed_at=datetime(2026, 9, 16, 15, 0), currency="USD"),
        ExecRecord(external_id="3", symbol="MSFT", underlying="MSFT", asset_type="STOCK", side="BUY",
                   quantity=5, price=400, executed_at=datetime(2026, 9, 1, 14, 0), currency="USD"),
    ]
    ingest_records(db, acct.id, "t212", recs)
    rebuild_trades(db, [acct.id])
    ingest(db, acct.id, "t212", [
        DividendIn("d1", "AAPL", datetime(2026, 9, 10, 0, 0), 5.0, "USD", 5.0, "USD"),
        DividendIn("d2", "AAPL", datetime(2026, 8, 1, 0, 0), 9.0, "USD", 9.0, "USD"),  # before entry
        DividendIn("d3", "AAPL", datetime(2026, 9, 20, 0, 0), 3.0, "USD", 3.0, "USD"),  # after exit
        DividendIn("d4", "MSFT", datetime(2026, 9, 10, 0, 0), 1.5, "USD", 1.5, "USD"),
    ])
    db.commit()
    aapl = db.scalar(select(Trade).where(Trade.symbol == "AAPL"))
    msft = db.scalar(select(Trade).where(Trade.symbol == "MSFT"))
    assert for_trade(db, aapl)["native"] == pytest.approx(5.0) and for_trade(db, aapl)["count"] == 1
    assert for_trade(db, msft)["native"] == pytest.approx(1.5)
    opt = Trade(key="o", account_id=acct.id, symbol="AAPL 2026-09-18 130C", underlying="AAPL",
                asset_type="OPTION", direction="LONG", status="CLOSED",
                opened_at=datetime(2026, 9, 2), closed_at=datetime(2026, 9, 16),
                quantity=1, open_quantity=0, entry_price=2, cost_basis=200, currency="USD")
    db.add(opt)
    db.flush()
    assert for_trade(db, opt)["count"] == 0
    assert total(db, acct.id, "USD") == pytest.approx(5 + 9 + 3 + 1.5)
    assert total(db, acct.id, "USD", start=date(2026, 9, 1), end=date(2026, 9, 16)) == pytest.approx(6.5)


def test_snaptrade_and_schwab_csv_dividends(db, fixture_text):
    import json
    from pathlib import Path
    acts = json.loads((Path(__file__).parent / "fixtures" / "snaptrade_activities.json").read_text())
    recs, ignored = parse_activities(acts)
    assert ignored.get("DIVIDEND") == 1
    divs = parse_dividends(acts)
    assert len(divs) == 1 and divs[0].symbol == "EXMP" and divs[0].amount == pytest.approx(1.5)
    from app.importers import parse
    csv = parse(fixture_text("schwab_transactions.csv"))
    assert csv.dividends[0].symbol == "AAPL" and csv.dividends[0].amount == pytest.approx(12.50)
    acct = Account(name="S", broker="schwab")
    db.add(acct)
    db.flush()
    ingest(db, acct.id, "snaptrade", divs)
    ingest(db, acct.id, "schwab_csv", csv.dividends)
    db.commit()
    assert db.query(Dividend).count() == 2


def test_trade_page_and_dashboard_widgets(db):
    acct = Account(name="S", broker="schwab", currency="USD")
    db.add(acct)
    db.flush()
    ingest_records(db, acct.id, "schwab_csv", [
        ExecRecord(external_id="1", symbol="AAA", underlying="AAA", asset_type="STOCK", side="BUY",
                   quantity=10, price=10, executed_at=datetime(2026, 9, 1, 14, 0), currency="USD"),
        ExecRecord(external_id="2", symbol="AAA", underlying="AAA", asset_type="STOCK", side="SELL",
                   quantity=10, price=12, executed_at=datetime(2026, 9, 10, 14, 0), currency="USD"),
    ])
    rebuild_trades(db, [acct.id])
    ingest(db, acct.id, "schwab_csv", [
        DividendIn("d1", "AAA", datetime(2026, 9, 5, 16, 0), 4.0, "USD", 4.0, "USD"),
    ])
    db.commit()
    t = db.scalar(select(Trade).where(Trade.symbol == "AAA"))
    assert t.net_pnl == pytest.approx(20.0)
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    html = c.get(f"/trades/{t.id}").text
    assert 'data-wid="dividend"' in html and "Dividend" in html
    assert "+$4.00" in html
    assert 'data-wid="pnl_div"' in html  # optional, hidden by default
    i = html.index('data-wid="pnl_div"')
    assert " hidden" in html[i:html.index(">", i)]
    dash = c.get("/").text
    assert 'data-wid="total_return"' in html or 'data-wid="total_return"' in dash
    assert "Total return" in dash and "Div " in dash
    # realized 20 + div 4 = 24 (no open)
    assert "+$24.00" in dash


def test_display_currency_conversion(db):
    from app import currency
    acct = Account(name="T", broker="trading212", currency="EUR", source_key="t212")
    db.add(acct)
    db.add(FxRate(day=date(2026, 9, 10), quote="USD", rate=1.10))
    db.flush()
    ingest_records(db, acct.id, "t212", [
        ExecRecord(external_id="1", symbol="AAA", underlying="AAA", asset_type="STOCK", side="BUY",
                   quantity=10, price=10, executed_at=datetime(2026, 9, 1, 9, 0), currency="EUR"),
        ExecRecord(external_id="2", symbol="AAA", underlying="AAA", asset_type="STOCK", side="SELL",
                   quantity=10, price=11, executed_at=datetime(2026, 9, 12, 9, 0), currency="EUR"),
    ])
    rebuild_trades(db, [acct.id])
    ingest(db, acct.id, "t212", [
        DividendIn("d1", "AAA", datetime(2026, 9, 10, 0, 0), 10.0, "EUR", 10.0, "EUR"),
    ])
    db.commit()
    t = db.scalar(select(Trade).where(Trade.symbol == "AAA"))
    d = for_trade(db, t, display="USD")
    assert d["native"] == pytest.approx(10.0)
    assert d["display"] == pytest.approx(11.0)  # 10 EUR × 1.10
    currency.set_display(db, "USD")
    assert total(db, acct.id, "USD") == pytest.approx(11.0)
