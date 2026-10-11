"""First paint stays off the quote network, and trade loads are not one query per fill."""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.models import Account, Execution, FxRate, Trade, TradeFill
from app.web import load_trades


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def _open(db):
    acct = Account(name="EUR", broker="trading212", currency="EUR", source_key="t212")
    db.add(acct)
    db.flush()
    t = Trade(key="k", account_id=acct.id, symbol="CGC", underlying="CGC", asset_type="STOCK",
              direction="LONG", status="OPEN", opened_at=datetime(2026, 1, 2, 15), quantity=5,
              open_quantity=5, entry_price=2.5, cost_basis=12.5, gross_pnl=0, fees=0, net_pnl=0, currency="USD")
    db.add(t)
    db.flush()
    ex = Execution(account_id=acct.id, source="t212", external_id="e1", match_key="m1", symbol="CGC",
                   underlying="CGC", asset_type="STOCK", side="BUY", quantity=5, price=2.5, fees=0,
                   executed_at=t.opened_at, currency="USD", fx_to_account=0.92)
    db.add(ex)
    db.flush()
    db.add(TradeFill(trade_id=t.id, execution_id=ex.id, position=0, side="BUY", role="OPEN",
                     quantity=5, price=2.5, fees=0, executed_at=t.opened_at))
    db.add(FxRate(day=t.opened_at.date(), quote="USD", rate=1.1))
    db.commit()
    return t


def test_pages_do_not_fetch_quotes_on_first_paint(db, monkeypatch):
    t = _open(db)
    calls = []
    monkeypatch.setenv("QUOTE_PROVIDER", "yahoo")
    import app.quotes as quotes
    monkeypatch.setattr(quotes, "_fetch", lambda ysym: calls.append(ysym) or {"chart": {}})
    monkeypatch.setattr(quotes, "_fetch_daily", lambda ysym: calls.append("d:" + ysym) or {"chart": {}})
    quotes.clear_cache()
    c = _client(db)
    page = c.get("/")
    assert page.status_code == 200
    assert calls == []
    assert 'id="top-progress"' in page.text
    assert 'id="live-boot"' in page.text and "/dashboard/live" in page.text
    live = c.get("/dashboard/live")
    assert live.status_code == 200 and calls and "Unrealized" in live.text
    after_live = len(calls)
    trade = c.get(f"/trades/{t.id}")
    assert trade.status_code == 200 and 'id="unrl"' in trade.text
    assert len(calls) == after_live


def test_trade_page_does_not_call_quotes(db, monkeypatch):
    t = _open(db)
    calls = []
    monkeypatch.setenv("QUOTE_PROVIDER", "yahoo")
    import app.quotes as quotes
    monkeypatch.setattr(quotes, "_fetch", lambda ysym: calls.append(ysym) or {"chart": {}})
    monkeypatch.setattr(quotes, "_fetch_daily", lambda ysym: (_ for _ in ()).throw(AssertionError("daily")))
    quotes.clear_cache()
    c = _client(db)
    before = len(calls)
    assert c.get(f"/trades/{t.id}").status_code == 200
    assert len(calls) == before
    c.get(f"/trades/{t.id}/r.json")
    assert calls  # the follow-up quote request does fetch


def test_loading_trades_is_a_handful_of_queries(db):
    acct = Account(name="A", broker="schwab", currency="USD")
    db.add(acct)
    db.flush()
    when = datetime(2026, 2, 2, 15)
    for i in range(12):
        t = Trade(key=f"k{i}", account_id=acct.id, symbol="AAA", underlying="AAA", asset_type="STOCK",
                  direction="LONG", status="CLOSED", opened_at=when, closed_at=when, quantity=1,
                  open_quantity=0, entry_price=1, exit_price=2, cost_basis=1, gross_pnl=1, fees=0,
                  net_pnl=1, currency="USD")
        db.add(t)
        db.flush()
        for k in range(3):
            ex = Execution(account_id=acct.id, source="s", external_id=f"{i}-{k}", match_key=f"m{i}-{k}",
                           symbol="AAA", underlying="AAA", asset_type="STOCK", side="BUY", quantity=1,
                           price=1, fees=0, executed_at=when)
            db.add(ex)
            db.flush()
            db.add(TradeFill(trade_id=t.id, execution_id=ex.id, position=k, side="BUY",
                             role="OPEN" if k == 0 else "CLOSE", quantity=1, price=1, fees=0, executed_at=when))
    db.commit()
    n = {"q": 0}

    def _count(*_a, **_k):
        n["q"] += 1

    event.listen(db.get_bind(), "before_cursor_execute", _count)
    try:
        rows = load_trades(db, select(Trade))
        from app import realized as rz
        rz.events(rows)
        for t in rows:
            for f in t.fills:
                _ = f.execution.fx_to_account if f.execution else None
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", _count)
    assert n["q"] < 12  # one round of selects, not one query per fill


def test_static_loading_feedback_respects_reduced_motion():
    from app.main import create_app
    c = TestClient(create_app())
    css = c.get("/static/app.css").text
    js = c.get("/static/app.js").text
    assert "#top-progress" in css and "prefers-reduced-motion: reduce" in css
    assert ".hx-busy" in css and "animation: none" in css
    assert "skel-ph" not in css and "skel-host" not in css and "skel-ph" not in js
    assert "getElementById('page')" not in js  # full-page loads keep only the 2px bar
    for ev in ("htmx:beforeRequest", "htmx:afterSwap", "htmx:afterSettle", "htmx:responseError", "htmx:sendError", "pageshow"):
        assert ev in js
    assert "15000" in js and "aria-busy" in js and "top-progress" in js


def test_stat_row_and_trades_summary_on_first_paint(db, monkeypatch):
    """Trade detail keeps its stat widgets in the HTML. The trades list has a summary and filters, not a widget row."""
    import re
    from datetime import date

    from app import currency
    from app.instruments import ExecRecord as ER
    from app.models import Account, FxRate, Trade
    from app.services import ingest_records, rebuild_trades

    def fake_http(url):
        today = date.today().isoformat()
        return {"base": "EUR", "rates": {
            "2026-10-01": {"USD": 1.10, "GBP": 0.86},
            "2026-10-02": {"USD": 1.12, "GBP": 0.87},
            today: {"USD": 1.16, "GBP": 0.85},
        }}

    monkeypatch.setattr(currency, "_http_get", fake_http)
    currency._last_try.clear()
    eur = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212", market="EU",
                  timezone="Europe/Dublin")
    usd = Account(name="Schwab", broker="schwab", currency="USD")
    db.add_all([eur, usd])
    db.flush()

    def rec(i, sym, side, qty, price, when, ccy=None, fx=None):
        return ER(external_id=f"s7{i}", symbol=sym, underlying=sym, asset_type="STOCK", side=side, quantity=qty,
                  price=price, executed_at=when, fees=0.1, position_effect="OPEN" if side == "BUY" else "CLOSE",
                  currency=ccy, fx_to_account=fx)

    d1, d2 = datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 2, 15, 0)
    ingest_records(db, eur.id, "t212", [
        rec(1, "SMT", "BUY", 10, 10.0, d1, "GBP", 1.16),
        rec(3, "AAA", "BUY", 10, 100.0, d1, "EUR"),
        rec(4, "AAA", "SELL", 10, 110.0, d2, "EUR"),
        rec(7, "VOD", "BUY", 5, 1.2, d1, "GBP", 1.15),
        rec(8, "VOD", "SELL", 5, 1.4, d2, "GBP", 1.17),
    ])
    ingest_records(db, usd.id, "schwab_csv", [
        rec(5, "BBB", "BUY", 10, 50.0, d1, "USD"),
        rec(9, "CCC", "BUY", 4, 20.0, d1, "USD"),
        rec(10, "CCC", "SELL", 4, 18.0, d2, "USD"),
    ])
    db.add(FxRate(day=date.today(), quote="GBP", rate=0.85))
    db.add(FxRate(day=date.today(), quote="USD", rate=1.16))
    rebuild_trades(db)
    db.commit()
    c = _client(db)
    listing = c.get("/trades")
    assert listing.status_code == 200
    assert "total realized in view" in listing.text
    assert 'data-autofilter="swap"' in listing.text and 'name="symbol"' in listing.text
    assert 'id="trade-stats"' not in listing.text
    dash = c.get("/")
    assert dash.status_code == 200
    assert 'id="dash-grid"' in dash.text and 'data-wid="realized"' in dash.text
    assert 'id="live-unrealized"' in dash.text and 'id="live-total_pnl"' in dash.text and 'id="live-positions"' in dash.text
    for status in ("OPEN", "CLOSED"):
        t = db.scalar(select(Trade).where(Trade.status == status))
        html = c.get(f"/trades/{t.id}").text
        assert 'id="trade-stats"' in html and "tstat-card" in html
        net = re.search(r'<div data-wid="net"([^>]*)>', html)
        assert net and "hidden" not in net.group(1)
        assert 'data-wid="unrealized"' in html and 'id="unrl"' in html
