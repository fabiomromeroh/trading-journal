"""Balance curve: real cash flows (T212 ledger, SnapTrade state), stored daily series, broker check, widgets."""
from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import balance as bmod
from app.instruments import ExecRecord
from app.models import Account, BalancePoint, CashFlow
from app.services import ingest_records, rebuild_trades, set_state
from app.sources import trading212 as t212
from app.sources.base import SyncContext
from app.dividends import DividendIn, ingest as ingest_divs
from tests.test_trading212 import KEY, SECRET, FakeT212, enc, nop_sleep  # noqa: F401


def test_t212_flow_mapping():
    dep = bmod.t212_flow({"type": "DEPOSIT", "amount": 500, "reference": "r1", "dateTime": "2026-01-05T10:00:00Z"}, "EUR")
    assert dep["kind"] == "deposit" and dep["amount"] == 500 and dep["currency"] == "EUR"
    wd = bmod.t212_flow({"type": "WITHDRAW", "amount": 200, "reference": "r2", "dateTime": "2026-02-05T10:00:00Z"}, "EUR")
    assert wd["kind"] == "withdrawal" and wd["amount"] == -200
    assert bmod.t212_flow({"type": "FEE", "amount": 1, "reference": "f", "dateTime": "2026-02-05T10:00:00Z"}, "EUR")["amount"] == -1
    assert bmod.t212_flow({"type": "DIVIDEND", "amount": 1, "reference": "x", "dateTime": "2026-02-05T10:00:00Z"}, "EUR") is None
    assert bmod.t212_flow({"type": "DEPOSIT", "amount": 1}, "EUR") is None  # no id / time: never guessed


def _acct(db, ccy="USD", key="t212"):
    a = Account(name="A", broker="trading212", currency=ccy, source_key=key)
    db.add(a)
    db.flush()
    return a


def test_series_deposits_realized_dividends_unrealized(db):
    a = _acct(db)
    bmod.ingest_flows(db, a.id, "t212", [
        {"external_id": "d1", "occurred_at": datetime(2026, 9, 1, 15), "amount": 1000.0, "currency": "USD", "kind": "deposit"},
        {"external_id": "w1", "occurred_at": datetime(2026, 9, 8, 15), "amount": -100.0, "currency": "USD", "kind": "withdrawal"},
    ])
    ingest_records(db, a.id, "t212", [
        ExecRecord(external_id="1", symbol="AAA", underlying="AAA", asset_type="STOCK", side="BUY", quantity=10, price=10,
                   executed_at=datetime(2026, 9, 2, 15)),
        ExecRecord(external_id="2", symbol="AAA", underlying="AAA", asset_type="STOCK", side="SELL", quantity=5, price=12,
                   executed_at=datetime(2026, 9, 4, 15)),
    ])
    rebuild_trades(db, [a.id])
    ingest_divs(db, a.id, "t212", [DividendIn("v1", "AAA", datetime(2026, 9, 5, 15), 3.0, "USD", 3.0, "USD")])
    closes = {date(2026, 9, 2): 10.5, date(2026, 9, 3): 11.0, date(2026, 9, 4): 12.0, date(2026, 9, 7): 13.0}
    bmod.rebuild(db, [a.id], currencies=("USD",), fetch=lambda _db, _t, _s, _e: closes, today=date(2026, 9, 20))
    db.commit()
    pts = {p.day: p for p in db.scalars(select(BalancePoint).where(BalancePoint.account_id == a.id))}
    assert pts[date(2026, 9, 1)].balance == pytest.approx(1000)
    assert pts[date(2026, 9, 2)].unrealized == pytest.approx(5.0)          # 10 x (10.5 - 10)
    d4 = pts[date(2026, 9, 4)]
    assert d4.realized_cum == pytest.approx(10.0) and d4.unrealized == pytest.approx(10.0)  # 5 x (12 - 10)
    d8 = pts[date(2026, 9, 8)]
    assert d8.net_deposits == pytest.approx(900) and d8.dividends_cum == pytest.approx(3.0)
    assert d8.unrealized == pytest.approx(15.0) and d8.balance == pytest.approx(900 + 10 + 3 + 15)
    # more than CARRY_DAYS after the last close: the position is left out and named, not guessed
    late = pts[date(2026, 9, 20)]
    assert late.unrealized == 0 and late.missing == "AAA"
    s = bmod.series(db, a.id, "USD")
    assert s["latest"]["balance"] == pytest.approx(913) and s["gap_days"] > 0 and "AAA" in s["gap_symbols"]
    win = bmod.series(db, None, "USD", start=date(2026, 9, 3), end=date(2026, 9, 5))
    assert win["labels"] == ["2026-09-03", "2026-09-04", "2026-09-05"]


def test_all_accounts_sum_and_broker_check(db):
    a, b = _acct(db), _acct(db, key="snaptrade")
    bmod.ingest_flows(db, a.id, "t212", [{"external_id": "d", "occurred_at": datetime(2026, 9, 1, 15), "amount": 100.0,
                                           "currency": "USD", "kind": "deposit"}])
    from app.sources.snaptrade import CASHFLOW_STATE, PORTFOLIO_STATE
    set_state(db, CASHFLOW_STATE + str(b.id), json.dumps({"x1": {"type": "CONTRIBUTION", "date": "2026-09-03", "amount": 50.0}}))
    set_state(db, PORTFOLIO_STATE + str(b.id), json.dumps({"as_of": "2026-09-05T10:00:00", "cash": 50, "market_value": 0, "value": 50.0}))
    bmod.save_t212_summary(db, a.id, json.loads('{"currency":"USD","cash":{"availableToTrade":90},"investments":{"currentValue":10}}'))
    bmod.rebuild(db, None, currencies=("USD",), fetch=lambda *_: {}, today=date(2026, 9, 5))
    db.commit()
    assert db.scalar(select(CashFlow).where(CashFlow.account_id == b.id)).amount == 50
    s = bmod.series(db, None, "USD")
    assert s["latest"]["balance"] == pytest.approx(150)
    assert s["balance"][0] == pytest.approx(100) and s["balance"][-1] == pytest.approx(150)
    assert s["check"]["broker_total"] == pytest.approx(150) and s["check"]["diff"] == pytest.approx(0)


def test_t212_sync_reads_cash_ledger_incrementally(db, enc):
    fake = FakeT212()
    ledger = {"items": [
        {"type": "DEPOSIT", "amount": 1000, "reference": "dep-1", "dateTime": "2026-01-05T10:00:00Z", "currency": "EUR"},
        {"type": "WITHDRAW", "amount": -50, "reference": "wd-1", "dateTime": "2026-02-05T10:00:00Z", "currency": "EUR"},
    ], "nextPagePath": None}
    base = fake.handler

    def handler(req: httpx.Request):
        if req.url.path.endswith("/equity/history/transactions"):
            fake.calls.append(req)
            return httpx.Response(200, json=ledger)
        return base(req)
    t212.save_credentials(db, KEY, SECRET, "live")
    db.commit()
    src = t212.Trading212Source(transport=httpx.MockTransport(handler), sleep=nop_sleep)
    res = src.sync(db, SyncContext())
    assert "cash flows 2 new" in res.message
    flows = list(db.scalars(select(CashFlow)))
    assert sorted(f.amount for f in flows) == [-50, 1000]
    assert db.query(CashFlow).count() == 2
    src.sync(db, SyncContext())  # idempotent
    assert db.query(CashFlow).count() == 2
    from app.services import get_state
    assert get_state(db, t212.CASH_CURSOR_STATE)
    snap = bmod.broker_snapshots(db, [flows[0].account_id])
    assert snap == {}  # fixture summary has no total / currentValue and no legacy cash endpoint: no check, not guessed


def test_dashboard_widgets_render(db, monkeypatch):
    monkeypatch.setattr(bmod, "CURRENCIES", ("USD",))  # no ECB fetch from the test suite
    from app.routes import dashboard as dash
    monkeypatch.setattr(dash, "_INLINE", True)
    a = _acct(db)
    bmod.ingest_flows(db, a.id, "t212", [{"external_id": "d", "occurred_at": datetime(2026, 9, 1, 15), "amount": 250.0,
                                           "currency": "USD", "kind": "deposit"}])
    ingest_records(db, a.id, "t212", [ExecRecord(external_id="1", symbol="AAA", underlying="AAA", asset_type="STOCK",
                                                 side="BUY", quantity=1, price=10, executed_at=datetime(2026, 9, 2, 15))])
    rebuild_trades(db, [a.id])
    db.commit()
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    html = c.get("/").text
    assert 'data-wid="balance"' in html and 'data-wid="chart_balance"' in html
    assert "/dashboard/balance/build" in html          # nothing stored yet: built on first view
    r = c.post("/dashboard/balance/build")
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    html = c.get("/").text
    assert "ch-balance" in html and "Deposits $250.00" in html


def test_interest_types_and_total(db):
    for typ in ("INTEREST_ON_CASH", "LENDING_INTEREST", "INTEREST"):
        assert bmod.flow_kind(typ) == "interest"
    assert bmod.flow_kind("CURRENCY_CONVERSION_FEE") == "fee" and bmod.flow_kind("CARD_PURCHASE") is None
    a = _acct(db)
    bmod.ingest_flows(db, a.id, "t212", [
        bmod.t212_flow({"type": "INTEREST_ON_CASH", "amount": 1.25, "reference": "i1", "dateTime": "2026-09-02T00:00:00Z", "currency": "USD"}, "USD"),
        {"external_id": "d", "occurred_at": datetime(2026, 9, 1, 15), "amount": 100.0, "currency": "USD", "kind": "deposit"}])
    it = bmod.interest_total(db, a.id, "USD")
    assert it["interest"] == pytest.approx(1.25) and it["count"] == 1
    bmod.rebuild(db, [a.id], currencies=("USD",), fetch=lambda *_: {}, today=date(2026, 9, 3))
    s = bmod.series(db, a.id, "USD")
    assert s["latest"]["balance"] == pytest.approx(101.25) and s["latest"]["net_deposits"] == pytest.approx(100)


def test_csv_interest_rows():
    from app.importers.t212_csv import parse
    text = ("Action,Time,ISIN,Ticker,Name,ID,No. of shares,Price / share,Currency (Price / share),Total,Currency (Total)\n"
            "Interest on cash,2026-09-02 01:00:00,,,,int-1,,,,0.42,EUR\n")
    r = parse(text)
    assert r.cash_flows and r.cash_flows[0]["amount"] == pytest.approx(0.42) and r.cash_flows[0]["currency"] == "EUR"


def test_display_currency_follows_account(db):
    from app import currency
    eur = Account(name="T212", broker="trading212", currency="EUR", source_key="t212")
    usd = Account(name="Schwab", broker="schwab", currency="USD", source_key="snaptrade")
    db.add_all([eur, usd])
    db.commit()
    currency.set_display(db, "USD")
    assert currency.get_display(db) == "USD"
    assert currency.get_display(db, eur.id) == "EUR" and currency.get_display(db, usd.id) == "USD"
    currency.set_display(db, "USD", eur.id)  # manual override for that account only
    assert currency.get_display(db, eur.id) == "USD" and currency.get_display(db) == "USD"
    tok = currency.VIEW_ACCOUNT.set(usd.id)
    try:
        assert currency.get_display(db) == "USD"
    finally:
        currency.VIEW_ACCOUNT.reset(tok)


def test_honav_prices_from_hona():
    from types import SimpleNamespace
    from app import symbolmap
    t = SimpleNamespace(underlying="HONAV", instrument=None, instrument_id=None)
    assert symbolmap.price_symbol(t) == "HONA"
