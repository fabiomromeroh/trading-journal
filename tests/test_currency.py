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
RATES = {"2026-10-01": 1.10, "2026-10-02": 1.12, "2026-10-05": 1.14, "2026-10-06": 1.16}


def fake_http(url):
    assert "base=EUR" in url and "USD" in url
    return {"base": "EUR", "rates": {d: {"USD": r} for d, r in RATES.items()}}


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


def test_view_converts_at_close_date_and_keeps_r(db):
    usd, eur = two_accounts(db)
    t_eur = db.scalar(select(Trade).where(Trade.account_id == eur.id))
    t_eur.initial_stop = 95.0
    assert t_eur.net_pnl == pytest.approx(100.0)
    [c] = currency.view(db, [t_eur], "USD", http=fake_http)
    assert isinstance(c, currency.CTrade) and c.fx_rate == pytest.approx(1.12)  # closed Fri 02 Oct
    assert c.net_pnl == pytest.approx(112.0) and c.entry_price == pytest.approx(112.0) and c.initial_stop == pytest.approx(106.4)
    assert c.fills[0].price == pytest.approx(112.0) and c.quantity == 10
    assert c.symbol == "AAA" and c.account.name == "Trading 212"          # delegated
    # R is currency agnostic
    assert metrics.trade_metrics(c, None, None)["r_multiple"] == pytest.approx(metrics.trade_metrics(t_eur, None, None)["r_multiple"])
    # the stored trade is untouched
    assert t_eur.net_pnl == pytest.approx(100.0)
    # EUR display of a USD trade
    t_usd = db.scalar(select(Trade).where(Trade.account_id == usd.id))
    [d] = currency.view(db, [t_usd], "EUR", http=fake_http)
    assert d.net_pnl == pytest.approx(20.0 / 1.12)


def test_open_trade_uses_latest_rate(db):
    usd, eur = two_accounts(db)
    t = db.scalar(select(Trade).where(Trade.account_id == eur.id))
    t.status, t.closed_at = "OPEN", None
    today = datetime.now().date()
    db.add_all([FxRate(day=today - timedelta(days=2), quote="USD", rate=1.30), FxRate(day=today - timedelta(days=60), quote="USD", rate=1.10)])
    db.commit()
    [c] = currency.view(db, [t], "USD", http=lambda u: pytest.fail("cached range must not hit the network"))
    assert c.fx_rate == pytest.approx(1.30)


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
    # combined realized in USD: EUR trade 100 * 1.12 + USD trade 20 = 132
    from app import realized as rz
    from app.web import Filters, realized_scope
    evs, _, trades = realized_scope(db, Filters())
    assert sum(e.net for e in evs) == pytest.approx(112.0 + 20.0)
    c.post("/prefs", data={"currency": "EUR", "next": "/"})
    html = c.get("/").text
    assert "window.CUR = \"\\u20ac\"" in html
    evs, _, _ = realized_scope(db, Filters())
    assert sum(e.net for e in evs) == pytest.approx(100.0 + 20.0 / 1.12)
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
    assert "window.CUR = \"\\u20ac\"" in html            # trade page speaks the account/instrument currency
    assert "+$112.00" in html or "$112.00" in html  # the sidebar list is in the display currency
