"""Stored per-trade results must equal the full computation (ORM load + live conversion).

Datasets: the Schwab / ToS / SnapTrade golden-master fixtures plus the recorded Trading 212 sync, with seeded
ECB rates so USD <-> EUR <-> GBP conversions are exercised. Checked: every trade's figures, the stats, the
realized events, filtered scopes, rendered pages, and incremental / full rebuilds."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import currency, results
from app import realized as rz
from app.models import Account, FxRate, Tag, Trade, TradeResult
from app.stats import compute
from app.web import Filters, apply_trade_filters
from tests.test_golden_master import build_fixture_dataset
from tests.test_trading212 import _run_sync, enc  # noqa: F401 - fixture

MONEY = ["net_pnl", "gross_pnl", "fees", "mfe", "mae", "risk_amount", "r_multiple", "status", "symbol", "opened_at",
         "closed_at", "quantity", "open_quantity", "avg_entry", "avg_exit", "setup", "direction", "account_id"]


@pytest.fixture()
def data(db, fixture_text, enc, monkeypatch):  # noqa: F811
    monkeypatch.setattr(currency, "fetch_range", lambda *a, **k: 0)
    build_fixture_dataset(db, fixture_text)
    _run_sync(db)
    d, end = date(2015, 1, 1), datetime.now().date()
    i = 0
    while d <= end:  # deterministic, day-varying rates (1 EUR = x quote)
        db.add(FxRate(day=d, quote="USD", rate=1.05 + (i % 37) / 400))
        db.add(FxRate(day=d, quote="GBP", rate=0.84 + (i % 23) / 900))
        d += timedelta(days=1)
        i += 1
    tag = Tag(name="breakout")
    db.add(tag)
    for t in db.scalars(select(Trade).order_by(Trade.id).limit(5)):
        t.tags.append(tag)
        t.setup = "ORB"
    db.commit()
    return db


def _fig(t):
    out = {k: getattr(t, k, None) for k in MONEY}
    for k in ("fx_latest", "fx_entry", "shown_currency", "native_currency"):
        out[k] = getattr(t, k, None)
    out["tags"] = sorted(x.name for x in t.tags)
    out["mistakes"] = list(t.mistakes)
    out["fills"] = [(f.id, f.role, f.quantity, f.price, f.fees, getattr(t, "fx_on", lambda _f: None)(f)) for f in t.fills]
    return out


def _stats(trades):
    st = compute(trades, "America/New_York", open_count=sum(1 for t in trades if t.status == "OPEN"),
                 events=rz.events(trades))
    return dataclasses.asdict(st) if dataclasses.is_dataclass(st) else vars(st)


def _scopes(db):
    acct = [a.id for a in db.scalars(select(Account))]
    yield select(Trade)
    yield apply_trade_filters(select(Trade), Filters(start=datetime.now().date() - timedelta(days=30)))
    yield select(Trade).where(Trade.setup == "ORB")
    yield select(Trade).where(Trade.tags.any(Tag.name == "breakout"))
    for a in acct:
        yield select(Trade).where(Trade.account_id == a)
    yield select(Trade).where(Trade.status == "OPEN")
    yield select(Trade).order_by(Trade.opened_at.desc()).limit(7)


@pytest.mark.parametrize("ccy", ["USD", "EUR"])
def test_stored_equals_full(data, ccy):
    db = data
    assert {currency.trade_ccy(t) for t in db.scalars(select(Trade))} >= {"USD", "EUR", "GBP"}
    assert any(isinstance(t, currency.CTrade) for t in results.view(db, select(Trade), ccy)), "no conversion exercised"
    for stmt in _scopes(db):
        for _round in range(2):  # 1st builds the rows, 2nd reads them back
            old = results.full_view(db, stmt, ccy)
            new = results.view(db, stmt, ccy)
            a, b = [_fig(t) for t in new], [_fig(t) for t in old]
            bad = [(x["symbol"], k, x[k], y[k]) for x, y in zip(a, b) for k in x if x[k] != y[k]]
            assert len(a) == len(b) and not bad, bad[:6]
            assert _stats(new) == _stats(old)
            assert _ev(new) == _ev(old)
    assert db.scalar(select(TradeResult).where(TradeResult.currency == ccy)) is not None


def _ev(trades):
    out = []
    for e in rz.events(trades):
        d = vars(e) if hasattr(e, "__dict__") else (dataclasses.asdict(e) if dataclasses.is_dataclass(e) else e)
        if isinstance(d, dict):
            d = {k: (getattr(v, "id", v) if not isinstance(v, (int, float, str, type(None), date)) else v) for k, v in d.items()}
        elif isinstance(d, tuple):
            d = tuple(getattr(v, "id", v) if not isinstance(v, (int, float, str, type(None), date)) else v for v in d)
        out.append(d)
    assert out, "no realized events"
    return out


def test_incremental_rebuild_on_edit_and_fx(data):
    db = data
    results.view(db, select(Trade), "EUR")
    stamp = {(r.trade_id, r.currency): r.computed_at for r in db.scalars(select(TradeResult))}
    t = db.scalars(select(Trade).order_by(Trade.id)).first()
    t.setup = "Pullback"
    t.risk_amount = 123.0
    db.commit()
    new = results.view(db, select(Trade), "EUR")
    after = {(r.trade_id, r.currency): r.computed_at for r in db.scalars(select(TradeResult))}
    changed = {k for k in after if after[k] != stamp.get(k)}
    assert changed == {(t.id, "NATIVE"), (t.id, "EUR")}
    assert [_fig(x) for x in new] == [_fig(x) for x in results.full_view(db, select(Trade), "EUR")]
    # a new FX rate (settings / rate refresh) rebuilds every conversion
    db.add(FxRate(day=date(2014, 12, 31), quote="USD", rate=1.2))
    db.commit()
    new = results.view(db, select(Trade), "EUR")
    assert [_fig(x) for x in new] == [_fig(x) for x in results.full_view(db, select(Trade), "EUR")]


def test_rebuild_all_and_off_switch(data, monkeypatch):
    db = data
    n = results.rebuild(db, full=True)
    assert n == db.query(Trade).count()
    assert results.rebuild(db) == n
    assert db.query(TradeResult).count() == 3 * n
    monkeypatch.setenv("TRADE_RESULTS", "off")
    assert not results.enabled()


def test_pages_render_identically(data, monkeypatch):
    monkeypatch.setenv("PAGE_CACHE", "off")
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    paths = ["/", "/trades", "/reports", "/?preset=30d", "/trades?setup=ORB", "/reports?preset=90d", "/reports?tag=breakout"]
    for p in paths:
        c.get(p)  # first render may store one-off state
    monkeypatch.setenv("TRADE_RESULTS", "off")
    old = {p: c.get(p).text for p in paths}
    monkeypatch.setenv("TRADE_RESULTS", "on")
    new = {p: c.get(p).text for p in paths}
    new2 = {p: c.get(p).text for p in paths}
    for p in paths:
        assert "trade" in old[p].lower() and len(old[p]) > 2000
        assert new[p] == old[p], p
        assert new2[p] == old[p], p
