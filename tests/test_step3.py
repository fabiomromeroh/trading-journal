"""Step 3: GBX live quotes (pence -> pounds, display-currency unrealized) and per-account auto stop.

Numbers below are hand-checked. No network.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import currency, markets, metrics, quotes, stops
from app.instruments import ExecRecord
from app.models import Account, FxRate, Instrument, Trade
from app.services import ingest_records, rebuild_trades
from app.web import qty


def _chart(price, ccy="GBp", ts=1_700_000_000):
    meta = {"regularMarketPrice": price, "regularMarketTime": ts, "chartPreviousClose": price,
            "currentTradingPeriod": {"regular": {"start": ts - 100, "end": ts + 100}}}
    if ccy is not None:
        meta["currency"] = ccy
    return {"chart": {"result": [{"meta": meta, "timestamp": [ts],
                                  "indicators": {"quote": [{"close": [price]}]}}]}}


def test_pence_factor_scales_gbx_once():
    assert markets.pence_factor("GBp", "GBP") == 0.01
    assert markets.pence_factor("GBX", None) == 0.01          # even when the caller passed no currency
    assert markets.pence_factor("GBp", "EUR") == 0.01
    assert markets.pence_factor("GBP", "GBP", 0.01) == 1.0    # Yahoo already in pounds: do not apply price_scale
    assert markets.pence_factor("USD", "USD", 0.01) == 1.0
    assert markets.pence_factor(None, "GBP", 0.01) == 0.01    # currency missing: trust the stored scale once
    q = quotes.parse_chart("SMT", _chart(1500, "GBp"), ccy="GBP", price_scale=0.01)
    assert q.price == 15.0
    assert quotes.parse_chart("SMT", _chart(1500, "GBP"), ccy="GBP", price_scale=0.01).price == 1500.0


def test_qty_strips_trailing_zeros():
    assert qty(None) == "—"
    assert qty(10) == "10"
    assert qty(2.5) == "2.5"
    assert qty(14.29328162) == "14.2933"
    assert qty(1.2) == "1.2"
    t = NS(status="OPEN", direction="LONG", asset_type="STOCK", multiplier=1,
           fills=[NS(role="OPEN", quantity=14.29328162, price=14.73, position=0)])
    assert metrics.position_of(t)["text"] == "+14.2933 sh long"


def test_gbx_unrealized_in_gbp_and_eur(db):
    """Yahoo 1500 pence = £15. Qty 2.5, avg cost £10. Open P&L £12.50; EUR at 0.85 GBP per EUR = 12.50/0.85."""
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="SMTl_EQ", symbol="SMT", exchange="LSE", currency="GBP",
                      price_symbol="SMT.L", price_scale=0.01, asset_type="STOCK")
    db.add(inst)
    db.flush()
    ingest_records(db, acct.id, "t212", [
        ExecRecord(external_id="1", symbol="SMT", underlying="SMT", asset_type="STOCK", side="BUY",
                   quantity=2.5, price=10.0, executed_at=datetime(2026, 10, 1, 9, 0), currency="GBP",
                   fx_to_account=1.16, instrument_id=inst.id),
    ])
    rebuild_trades(db)
    today = datetime.now().date()
    db.add_all([
        FxRate(day=datetime(2026, 10, 1).date(), quote="GBP", rate=0.86),
        FxRate(day=today, quote="GBP", rate=0.85),
    ])
    db.commit()
    t = db.scalar(select(Trade).where(Trade.symbol == "SMT"))
    quotes.clear_cache()

    def fetch(ysym):
        assert ysym == "SMT.L"
        return _chart(1500, "GBp")

    q = quotes.get_quotes([t], fetch=fetch)["SMT"]
    assert q.price == 15.0                                     # pounds, not 1500 pence
    tm = metrics.trade_metrics(t, price=q.price)
    assert tm["open_pnl"] == pytest.approx(12.50)
    assert tm["position"]["text"] == "+2.5 sh long"
    assert tm["position"]["qty"] == pytest.approx(2.5)
    currency.set_display(db, "EUR")
    fx = currency.presentation(db, t, tm)
    assert fx.show
    assert fx.unrealized == pytest.approx(12.50 / 0.85)
    # a missing 1-minute chart falls back to the daily chart (default fetch only)
    quotes.clear_cache()

    def empty(_ysym):
        return {"chart": {"result": []}}

    def daily(ysym):
        assert ysym == "SMT.L"
        return _chart(1500, "GBp")

    import app.quotes as qmod
    orig, orig_d = qmod._fetch, qmod._fetch_daily
    qmod._fetch, qmod._fetch_daily = empty, daily
    try:
        again = quotes.get_quotes([t])["SMT"]
    finally:
        qmod._fetch, qmod._fetch_daily = orig, orig_d
    assert again.price == 15.0


def test_dashboard_sums_open_t212_positions(db, monkeypatch):
    """Two open T212 positions. Sum of converted per-position unrealized equals the dashboard total."""
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU")
    db.add(acct)
    db.flush()
    smt = Instrument(source_key="t212", broker_symbol="SMTl_EQ", symbol="SMT", exchange="LSE", currency="GBP",
                     price_symbol="SMT.L", price_scale=0.01, asset_type="STOCK")
    sap = Instrument(source_key="t212", broker_symbol="SAP_DE", symbol="SAP", exchange="XETRA", currency="EUR",
                     price_symbol="SAP.DE", price_scale=1.0, asset_type="STOCK")
    db.add_all([smt, sap])
    db.flush()
    when = datetime(2026, 10, 1, 9, 0)
    ingest_records(db, acct.id, "t212", [
        ExecRecord(external_id="1", symbol="SMT", underlying="SMT", asset_type="STOCK", side="BUY",
                   quantity=2.5, price=10.0, executed_at=when, currency="GBP", instrument_id=smt.id),
        ExecRecord(external_id="2", symbol="SAP", underlying="SAP", asset_type="STOCK", side="BUY",
                   quantity=4, price=100.0, executed_at=when, currency="EUR", instrument_id=sap.id),
    ])
    rebuild_trades(db)
    today = datetime.now().date()
    db.add_all([
        FxRate(day=when.date(), quote="GBP", rate=0.86),
        FxRate(day=today, quote="GBP", rate=0.85),
    ])
    db.commit()
    currency.set_display(db, "EUR")

    def fetch(ysym):
        if ysym == "SMT.L":
            return _chart(1500, "GBp")          # £15; P&L (15-10)*2.5 = £12.50
        if ysym == "SAP.DE":
            return _chart(110, "EUR")           # €110; P&L (110-100)*4 = €40
        raise AssertionError(ysym)

    monkeypatch.setattr(quotes, "enabled", lambda: True)
    monkeypatch.setattr(quotes, "_fetch", fetch)
    monkeypatch.setattr(currency, "fetch_range", lambda *a, **k: pytest.fail("rates are cached"))
    quotes.clear_cache()
    from app.routes.dashboard import _anchor
    from app.web import Filters
    anchor = _anchor(db, Filters(), [], NS(realized=0.0))
    assert anchor is not None
    by = {r["symbol"]: r for r in anchor["rows"] if r["qty"]}
    assert set(by) == {"SMT", "SAP"}
    assert by["SMT"]["price"] == 15.0 and by["SMT"]["ccy"] == "GBP"
    assert by["SMT"]["pnl"] == pytest.approx(12.50 / 0.85)
    assert by["SAP"]["price"] == 110.0 and by["SAP"]["pnl"] == pytest.approx(40.0)
    assert anchor["missing_prices"] == []
    assert anchor["unrealized"] == pytest.approx(sum(r["pnl"] for r in by.values()))
    assert "Yahoo" in anchor["sources"]


def test_reports_r_excludes_trades_without_a_stop():
    from app.routes.reports import report_data
    from tests.test_metrics import ALL, TZ
    data = report_data(ALL, TZ, 25)
    # Only T2 has a valid stop (entry 50, stop 45, net -50 -> -1R). Default risk must not fill the others in.
    assert data["s"]["r_count"] == 1
    assert data["s"]["total_r"] == pytest.approx(-1)
    assert data["s"]["avg_r"] == pytest.approx(-1)
    assert len(data["rtrades"]) == 1
    # The shared summary helper still honours default risk (golden / unit tests depend on it).
    s = metrics.summarize(ALL, TZ, default_risk=25)
    assert s["r_count"] == 5


def test_auto_stop_defaults_and_clears_only_auto(db, monkeypatch):
    assert Account(name="Schwab", broker="schwab").auto_stop is True
    assert Account(name="CSV", broker="schwab", source_key="schwab_csv").auto_stop is True
    assert Account(name="T", broker="trading212", source_key="t212").auto_stop is False
    assert Account(name="IB", broker="ibkr", source_key="ibkr_flex").auto_stop is False
    assert Account(name="Later", broker="some-future-broker").auto_stop is False
    assert Account(name="Off", broker="schwab", auto_stop=False).auto_stop is False
    assert Account(name="Default").auto_stop is True          # column default broker is schwab

    t212 = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212")
    sch = Account(name="Schwab acct", broker="schwab")
    db.add_all([t212, sch])
    db.flush()
    when = datetime(2026, 9, 2, 14, 0)

    def trade(key, acct, stop, auto, prev=None):
        row = Trade(key=key, account_id=acct.id, symbol=key, underlying=key, asset_type="STOCK",
                    direction="LONG", status="OPEN", opened_at=when, quantity=1, open_quantity=1,
                    entry_price=100, cost_basis=100, initial_stop=stop, stop_auto=auto,
                    stop_src="5m" if auto else None, stop_prev=prev)
        db.add(row)
        return row

    manual = trade("MAN", t212, 90.0, False, prev=80.0)
    auto = trade("AUTO", t212, 95.0, True, prev=94.0)
    kept = trade("MSFT", sch, 97.0, True)
    db.commit()
    n = stops.clear_auto_stops(db)
    db.commit()
    assert n == 1
    db.refresh(manual)
    db.refresh(auto)
    db.refresh(kept)
    assert manual.initial_stop == 90.0 and manual.stop_prev == 80.0 and not manual.stop_auto
    assert auto.initial_stop is None and auto.stop_auto is None and auto.stop_prev == 94.0
    assert kept.initial_stop == 97.0 and kept.stop_auto

    monkeypatch.setattr(stops, "extreme_before_entry", lambda *a, **k: (99.0, "5m", 1))
    assert stops.apply_default(db, auto) == "off" and auto.initial_stop is None
    bare = Trade(key="NEW", account_id=sch.id, symbol="NEW", underlying="NEW", asset_type="STOCK",
                 direction="LONG", status="OPEN", opened_at=when.replace(day=3), quantity=1, open_quantity=1,
                 entry_price=100, cost_basis=100)
    db.add(bare)
    db.commit()
    res = stops.backfill(db, limit=1)
    assert bare.initial_stop is not None and bare.stop_auto
    assert res["set"] == 1
    assert auto.initial_stop is None          # Trading 212 did not consume the limit and was not computed


def test_settings_auto_stop_toggle(db):
    t212 = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212")
    db.add(t212)
    db.flush()
    when = datetime(2026, 9, 2, 14, 0)
    auto = Trade(key="AUTO", account_id=t212.id, symbol="AAA", underlying="AAA", asset_type="STOCK",
                 direction="LONG", status="OPEN", opened_at=when, quantity=1, open_quantity=1,
                 entry_price=100, cost_basis=100, initial_stop=95.0, stop_auto=True, stop_src="5m")
    manual = Trade(key="MAN", account_id=t212.id, symbol="BBB", underlying="BBB", asset_type="STOCK",
                   direction="LONG", status="OPEN", opened_at=when, quantity=1, open_quantity=1,
                   entry_price=100, cost_basis=100, initial_stop=90.0, stop_auto=False)
    db.add_all([auto, manual])
    db.commit()
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    page = c.get("/settings")
    assert page.status_code == 200
    assert "Auto stop (5m low of day before entry)" in page.text
    off = c.post(f"/settings/accounts/{t212.id}/auto-stop", data={}, follow_redirects=False)
    assert off.status_code == 303 and off.headers["location"].endswith("#rules")
    db.expire_all()
    assert db.get(Account, t212.id).auto_stop is False
    assert db.get(Trade, auto.id).initial_stop is None
    assert db.get(Trade, manual.id).initial_stop == 90.0
    on = c.post(f"/settings/accounts/{t212.id}/auto-stop", data={"auto_stop": "1"}, follow_redirects=False)
    assert on.status_code == 303
    db.expire_all()
    assert db.get(Account, t212.id).auto_stop is True
    assert db.get(Trade, manual.id).initial_stop == 90.0
