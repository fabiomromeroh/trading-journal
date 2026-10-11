"""Display currency (USD/EUR), account switcher, ECB rate cache and the converted trade view."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import currency, metrics
from app.importers.base import ExecRecord  # noqa: F401  (re-export check)
from app.instruments import ExecRecord as ER
from app.models import Account, FxRate, Trade
from app.services import ingest_records, rebuild_trades

# ECB-style rates per 1 EUR; 2026-10-03/04 is a weekend (no rate -> Friday's applies)
RATES = {
    "USD": {"2026-10-01": 1.10, "2026-10-02": 1.12, "2026-10-05": 1.14, "2026-10-06": 1.16},
    "GBP": {"2026-10-01": 0.86, "2026-10-02": 0.87, "2026-10-05": 0.88, "2026-10-06": 0.89},
}


def fake_http(url):
    assert "base=EUR" in url
    wanted = [q for q in RATES if q in url]
    assert wanted
    days = sorted({d for q in wanted for d in RATES[q]})
    return {"base": "EUR", "rates": {d: {q: RATES[q][d] for q in wanted if d in RATES[q]} for d in days}}


def two_accounts(db):
    usd = Account(name="Schwab", broker="schwab")
    eur = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU",
                  timezone="Europe/Dublin")
    db.add_all([usd, eur])
    db.flush()

    def rec(i, side, qty, price, when):
        return ER(external_id=f"x{i}", symbol="AAA", underlying="AAA", asset_type="STOCK", side=side, quantity=qty,
                  price=price, executed_at=when, position_effect="OPEN" if side == "BUY" else "CLOSE")
    # EUR trade: +100 EUR, closed Fri 2026-10-02 -> rate 1.12 USD per EUR
    ingest_records(db, eur.id, "t212", [rec(1, "BUY", 10, 100.0, datetime(2026, 10, 1, 9, 0)),
                                        rec(2, "SELL", 10, 110.0, datetime(2026, 10, 2, 15, 0))])
    ingest_records(db, usd.id, "schwab_csv", [rec(3, "BUY", 10, 50.0, datetime(2026, 10, 1, 14, 0)),
                                              rec(4, "SELL", 10, 52.0, datetime(2026, 10, 2, 15, 0))])
    rebuild_trades(db)
    db.commit()
    return usd, eur


def test_rate_lookup_weekend_and_cross(db):
    currency.fetch_range(db, ["USD"], date(2026, 10, 1), date(2026, 10, 6), http=fake_http)
    book = currency.RateBook(db)
    assert book.per_eur("USD", date(2026, 10, 3)) == 1.12      # Saturday -> Friday
    assert book.per_eur("USD", date(2026, 10, 4)) == 1.12
    assert book.rate("EUR", "USD", date(2026, 10, 2)) == pytest.approx(1.12)
    assert book.rate("USD", "EUR", date(2026, 10, 2)) == pytest.approx(1 / 1.12)
    assert book.rate("USD", "USD", date(2026, 10, 2)) == 1.0
    assert book.per_eur("USD", date(2026, 9, 1)) is None       # long before the cache: no guess
    n = currency.fetch_range(db, ["USD"], date(2026, 10, 1), date(2026, 10, 6), http=fake_http)
    assert n == 0 and db.query(FxRate).count() == 4             # idempotent cache


def test_view_same_currency_is_identity(db):
    usd, eur = two_accounts(db)
    only_usd = list(db.scalars(select(Trade).where(Trade.account_id == usd.id)))
    assert currency.view(db, only_usd, "USD", http=fake_http) == only_usd
    assert all(not isinstance(t, currency.CTrade) for t in currency.view(db, only_usd, "USD"))


def test_view_converts_per_fill_and_keeps_prices_and_r(db):
    usd, eur = two_accounts(db)
    t_eur = db.scalar(select(Trade).where(Trade.account_id == eur.id))
    t_eur.initial_stop = 95.0
    assert t_eur.net_pnl == pytest.approx(100.0)
    [c] = currency.view(db, [t_eur], "USD", http=fake_http)
    assert isinstance(c, currency.CTrade)
    # (110 × 1.12 − 100 × 1.10) × 10 = 132. Prices stay in EUR.
    assert c.net_pnl == pytest.approx(132.0)
    assert c.entry_price == pytest.approx(100.0) and c.initial_stop == pytest.approx(95.0)
    assert c.fills[0].price == pytest.approx(100.0) and c.quantity == 10
    latest = currency.RateBook(db).rate("EUR", "USD", datetime.now().date())
    assert c.fx_rate == pytest.approx(latest) and c.fx_latest == pytest.approx(latest)
    assert c.symbol == "AAA" and c.account.name == "Trading 212"          # delegated
    # R is currency agnostic (computed on the stored trade)
    assert metrics.trade_metrics(c, None, None)["r_multiple"] == pytest.approx(metrics.trade_metrics(t_eur, None, None)["r_multiple"])
    # the stored trade is untouched
    assert t_eur.net_pnl == pytest.approx(100.0)
    # EUR display of a USD trade: (52 / 1.12 − 50 / 1.10) × 10
    t_usd = db.scalar(select(Trade).where(Trade.account_id == usd.id))
    [d] = currency.view(db, [t_usd], "EUR", http=fake_http)
    assert d.net_pnl == pytest.approx((52 / 1.12 - 50 / 1.10) * 10)
    assert d.entry_price == pytest.approx(50.0)


def test_open_trade_realized_uses_fill_dates_latest_is_separate(db):
    usd, eur = two_accounts(db)
    t = db.scalar(select(Trade).where(Trade.account_id == eur.id))
    t.status, t.closed_at = "OPEN", None
    today = datetime.now().date()
    db.add_all([FxRate(day=today - timedelta(days=2), quote="USD", rate=1.30), FxRate(day=today - timedelta(days=60), quote="USD", rate=1.10)])
    db.commit()
    [c] = currency.view(db, [t], "USD", http=lambda u: pytest.fail("cached range must not hit the network"))
    # Both fills (1–2 Oct) walk back to the 1.10 quote. The 1.30 quote is only the latest mark.
    assert c.fx_rate == pytest.approx(1.30) and c.fx_latest == pytest.approx(1.30)
    assert c.net_pnl == pytest.approx(110.0)


def test_missing_rates_fall_back_to_native_and_banner(db):
    usd, eur = two_accounts(db)

    def boom(url):
        raise OSError("offline")
    trades = list(db.scalars(select(Trade)))
    out = currency.view(db, trades, "EUR", http=boom)
    assert all(not isinstance(t, currency.CTrade) for t in out)          # shown unconverted, never guessed
    assert "No FX rates" in currency.problem(db, "EUR")
    assert currency.problem(db, "USD") is not None                       # the EUR account has no rate either


def test_prefs_currency_and_account_cookie_drive_pages(db, monkeypatch):
    usd, eur = two_accounts(db)
    monkeypatch.setattr(currency, "_http_get", fake_http)
    currency._last_try.clear()
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    r = c.post("/prefs", data={"currency": "EUR", "next": "/trades?account=1&status=CLOSED"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trades?status=CLOSED"   # account override dropped
    assert db.get(__import__("app.models", fromlist=["AppState"]).AppState, "display_currency").value == "EUR"
    page = c.get("/trades").text
    assert "€" in page and 'value="EUR"' in page
    # account switcher: only the Schwab account, remembered by cookie
    c.post("/prefs", data={"account": str(usd.id), "next": "/trades"})
    page = c.get("/trades").text
    assert "Trading 212" in page            # still in the switcher list
    assert page.count("AAA") >= 1
    assert c.cookies.get("acct") == str(usd.id)
    # All again
    c.post("/prefs", data={"account": "", "next": "/"})
    assert c.cookies.get("acct") is None
    # open redirect protection
    r = c.post("/prefs", data={"currency": "USD", "next": "https://evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    # unsupported currency is ignored
    c.post("/prefs", data={"currency": "XXX", "next": "/"})
    assert currency.get_display(db) == "USD"


def test_dashboard_totals_in_eur_and_usd(db, monkeypatch):
    usd, eur = two_accounts(db)
    monkeypatch.setattr(currency, "_http_get", fake_http)
    currency._last_try.clear()
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    c.post("/prefs", data={"currency": "USD", "next": "/"})
    html = c.get("/").text
    assert "window.CUR = \"$\"" in html
    assert currency.FX_PHRASE in html
    # combined realized in USD: EUR trade (110×1.12 − 100×1.10)×10 + USD trade 20 = 152
    from app.web import Filters, realized_scope
    evs, _, trades = realized_scope(db, Filters())
    assert sum(e.net for e in evs) == pytest.approx(132.0 + 20.0)
    c.post("/prefs", data={"currency": "EUR", "next": "/"})
    html = c.get("/").text
    assert "window.CUR = \"\\u20ac\"" in html
    assert currency.FX_PHRASE in html
    evs, _, _ = realized_scope(db, Filters())
    usd_in_eur = (52 / 1.12 - 50 / 1.10) * 10
    assert sum(e.net for e in evs) == pytest.approx(100.0 + usd_in_eur)
    # per-account view in EUR: only the T212 trade, unchanged by conversion
    evs, _, _ = realized_scope(db, Filters(account_id=eur.id))
    assert sum(e.net for e in evs) == pytest.approx(100.0)
    for url in ("/reports", "/trades", "/ai-review", f"/trades/{trades[0].id}"):
        assert c.get(url).status_code == 200


def test_trade_page_native_symbol_and_converted_sidebar(db, monkeypatch):
    usd, eur = two_accounts(db)
    monkeypatch.setattr(currency, "_http_get", fake_http)
    currency._last_try.clear()
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    c.post("/prefs", data={"currency": "USD", "next": "/"})
    t = db.scalar(select(Trade).where(Trade.account_id == eur.id))
    html = c.get(f"/trades/{t.id}").text
    assert "window.CUR = \"$\"" in html                       # selected currency
    assert "window.NATIVE_CUR = \"\\u20ac\"" in html          # instrument currency, shown beside it
    assert "+$132.00" in html and "(+€100.00)" in html        # selected first, original in brackets
    assert 'title="+$132.00 (+€100.00)"' in html             # the tooltip is text, not the inner markup
    assert "Prices in EUR" in html and "€100.00" in html      # entry price stays in the instrument currency
    assert "Converted at the rate on each trade date; open positions at the latest rate." in html


def _rec(i, sym, side, qty, price, when, ccy=None, fx=None, fees=0.0):
    return ER(external_id=f"m{i}", symbol=sym, underlying=sym, asset_type="STOCK", side=side, quantity=qty,
              price=price, executed_at=when, fees=fees, position_effect="OPEN" if side == "BUY" else "CLOSE",
              currency=ccy, fx_to_account=fx)


def test_mixed_gbp_eur_usd_sums_reconcile(db):
    """Hand-checked: GBP (broker fx) + EUR + USD, summed in EUR and in USD. Per-trade nets equal the dashboard total."""
    currency._last_try.clear()
    eur_acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU",
                       timezone="Europe/Dublin")
    usd_acct = Account(name="Schwab", broker="schwab")
    db.add_all([eur_acct, usd_acct])
    db.flush()
    d1, d2 = datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 2, 15, 0)
    ingest_records(db, eur_acct.id, "t212", [
        _rec(1, "SMT", "BUY", 10, 10.0, d1, "GBP", 1.16, 0.20),
        _rec(2, "SMT", "SELL", 10, 12.0, d2, "GBP", 1.18, 0.30),
        _rec(3, "AAA", "BUY", 10, 100.0, d1, "EUR"),
        _rec(4, "AAA", "SELL", 10, 110.0, d2, "EUR"),
    ])
    ingest_records(db, usd_acct.id, "schwab_csv", [
        _rec(5, "BBB", "BUY", 10, 50.0, d1, "USD"),
        _rec(6, "BBB", "SELL", 10, 60.0, d2, "USD"),
    ])
    rebuild_trades(db)
    db.commit()
    gbp = db.scalar(select(Trade).where(Trade.symbol == "SMT"))
    eura = db.scalar(select(Trade).where(Trade.symbol == "AAA"))
    usda = db.scalar(select(Trade).where(Trade.symbol == "BBB"))
    assert gbp.currency == "GBP" and gbp.gross_pnl == pytest.approx(20.0)
    assert gbp.fees == pytest.approx(0.50) and gbp.net_pnl == pytest.approx(19.50)
    assert eura.net_pnl == pytest.approx(100.0) and usda.net_pnl == pytest.approx(100.0)

    # GBP: (12×1.18 − 10×1.16)×10 = 25.60 gross, fees 0.20×1.16 + 0.30×1.18 = 0.586, net 25.014
    # USD into EUR: (60/1.12 − 50/1.10)×10
    eur_nets = {"SMT": 25.014, "AAA": 100.0, "BBB": (60 / 1.12 - 50 / 1.10) * 10}
    # GBP into USD: entry 1.16×1.10, exit 1.18×1.12 → gross 30.992, fees 0.65168, net 30.34032
    # EUR into USD: (110×1.12 − 100×1.10)×10 = 132
    usd_nets = {"SMT": 30.34032, "AAA": 132.0, "BBB": 100.0}
    from app.realized import trade_events
    from app.web import Filters, realized_scope

    def check(display, expected):
        currency.set_display(db, display)
        currency._last_try.clear()
        viewed = {t.symbol: t for t in currency.view(db, [gbp, eura, usda], display, http=fake_http)}
        total = 0.0
        for sym, exp in expected.items():
            t = viewed[sym]
            assert t.net_pnl == pytest.approx(exp, rel=1e-9, abs=1e-9)
            assert sum(e.net for e in trade_events(t)) == pytest.approx(t.net_pnl, abs=1e-6)
            if sym == "SMT":
                assert t.entry_price == pytest.approx(10.0) and t.exit_price == pytest.approx(12.0)
            total += t.net_pnl
        evs, _, trades = realized_scope(db, Filters())
        assert sum(e.net for e in evs) == pytest.approx(total, abs=1e-6)
        assert sum(t.net_pnl or 0.0 for t in trades) == pytest.approx(total, abs=1e-6)

    check("EUR", eur_nets)
    check("USD", usd_nets)
    assert sum(eur_nets.values()) == pytest.approx(25.014 + 100.0 + (60 / 1.12 - 50 / 1.10) * 10)
    assert sum(usd_nets.values()) == pytest.approx(30.34032 + 132.0 + 100.0)


def test_open_unrealized_uses_latest_ecb_not_broker_fx(db):
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU",
                   timezone="Europe/Dublin")
    db.add(acct)
    db.flush()
    ingest_records(db, acct.id, "t212", [_rec(1, "SMT", "BUY", 10, 10.0, datetime(2026, 10, 1, 9, 0), "GBP", 1.16)])
    rebuild_trades(db)
    today = datetime.now().date()
    db.add_all([
        FxRate(day=date(2026, 10, 1), quote="GBP", rate=0.86),
        FxRate(day=today, quote="GBP", rate=0.85),
        FxRate(day=date(2026, 10, 1), quote="USD", rate=1.10),
        FxRate(day=today, quote="USD", rate=1.20),
    ])
    db.commit()
    t = db.scalar(select(Trade).where(Trade.symbol == "SMT"))
    assert t.status == "OPEN"
    tm = {"open_pnl": 10.0, "open_value": 110.0}
    currency.set_display(db, "EUR")
    fx = currency.presentation(db, t, tm)
    assert fx.show
    # latest ECB GBP→EUR is 1/0.85, not the fill's broker 1.16
    assert fx.unrealized == pytest.approx(10 / 0.85)
    assert fx.unrealized != pytest.approx(10 * 1.16)
    assert fx.open_value == pytest.approx(110 / 0.85)
    [c] = currency.view(db, [t], "EUR", http=lambda u: pytest.fail("cached range must not hit the network"))
    assert currency.scale_open(c, 10.0) == pytest.approx(10 / 0.85)
    currency.set_display(db, "USD")
    fx = currency.presentation(db, t, tm)
    assert fx.unrealized == pytest.approx(10 * (1.20 / 0.85))
    assert fx.open_value == pytest.approx(110 * (1.20 / 0.85))
