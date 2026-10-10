"""Fill-level realized P&L (partial exits count on the day they happen), calendar/daily data,
metric tooltips, the trades header counts and live quotes for unrealized P&L. Synthetic data only;
every expected number below is worked out by hand in the comments."""
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import quotes, realized as rz
from app.instruments import ExecRecord
from app.models import Account, Trade
from app.services import ingest_records, rebuild_trades, set_state
from app.stats import compute
from app import metrics

NY = "America/New_York"


def rec(i, sym, side, q, p, day, hour=15, fees=0.0, month=9, **kw):
    return ExecRecord(external_id=f"r{i}", symbol=kw.pop("symbol_full", sym), underlying=sym,
                      asset_type=kw.pop("asset_type", "STOCK"), side=side, quantity=q, price=p, fees=fees,
                      executed_at=datetime(2026, month, day, hour), time_known=True, **kw)


@pytest.fixture()
def book(db):
    """ZZZ: buy 10 @100 (Sep 1, fee 1), sell 4 @110 (Sep 2, fee .5), sell 6 @95 (Sep 4, fee .5) -> closed.
         realized Sep 2: 4 x 10 = +40 - .5 = 39.5; Sep 4: 6 x -5 = -30 - .5 = -30.5; Sep 1: fee -1.
         trade net = 40 - 30 - 2 = 8.
       YYY: buy 5 @50 (Sep 2), sell 2 @60 (Sep 3) -> still open; Sep 3: +20. (the "no green day" case)
       SSS (short): sell 3 @20 (Sep 3), buy 3 @25 (Sep 4) -> closed, -15 on Sep 4.
       Totals: closed = 8 - 15 = -7; open part = 20; total realized = 13.
       Days: Sep1 -1, Sep2 +39.5, Sep3 +20, Sep4 -30.5 - 15 = -45.5  (sum = 13)."""
    acct = Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    ingest_records(db, acct.id, "tos_statement", [
        rec(1, "ZZZ", "BUY", 10, 100.0, 1, fees=1.0), rec(2, "ZZZ", "SELL", 4, 110.0, 2, fees=0.5),
        rec(3, "ZZZ", "SELL", 6, 95.0, 4, fees=0.5),
        rec(4, "YYY", "BUY", 5, 50.0, 2), rec(5, "YYY", "SELL", 2, 60.0, 3),
        rec(6, "SSS", "SELL", 3, 20.0, 3, position_effect="OPEN"), rec(7, "SSS", "BUY", 3, 25.0, 4, position_effect="CLOSE"),
    ])
    rebuild_trades(db, [acct.id])
    db.commit()
    return list(db.scalars(select(Trade)))


def test_partial_exits_are_realized_on_their_day(book):
    by = {t.symbol: t for t in book}
    assert by["ZZZ"].net_pnl == pytest.approx(8) and by["YYY"].status == "OPEN" and by["YYY"].net_pnl == pytest.approx(20)
    assert by["SSS"].net_pnl == pytest.approx(-15)
    days = rz.daily(rz.events(book), NY)
    got = {d.isoformat(): round(v.net, 2) for d, v in days.items()}
    assert got == {"2026-09-01": -1.0, "2026-09-02": 39.5, "2026-09-03": 20.0, "2026-09-04": -45.5}
    assert days[date(2026, 9, 3)].partial == 1 and days[date(2026, 9, 3)].closed == 0
    assert days[date(2026, 9, 4)].closed == 2 and days[date(2026, 9, 2)].partial == 1
    s = rz.summary(rz.events(book))
    assert s["total"] == pytest.approx(13) and s["closed_part"] == pytest.approx(-7) and s["open_part"] == pytest.approx(20)
    # reconciliation: sum of daily realized == total realized == sum of every trade's net
    assert sum(v.net for v in days.values()) == pytest.approx(s["total"]) == pytest.approx(sum(t.net_pnl for t in book))
    cum = rz.cumulative(days)
    assert cum["net"] == [-1.0, 38.5, 58.5, 13.0] and cum["day"] == [-1.0, 39.5, 20.0, -45.5]


def test_clip_to_date_range_and_stats(book):
    evs = rz.clip(rz.events(book), NY, date(2026, 9, 2), date(2026, 9, 3))
    assert rz.summary(evs)["total"] == pytest.approx(59.5)
    st = compute(book, NY, open_count=1, events=rz.events(book))
    assert st.realized == pytest.approx(13) and st.net_pnl == pytest.approx(-7)  # closed-only stays separate
    assert st.best_day[1] == pytest.approx(39.5) and st.worst_day[1] == pytest.approx(-45.5)
    m = metrics.summarize([t for t in book], NY, None, events=rz.events(book))
    assert m["realized"] == pytest.approx(13) and m["realized_open"] == pytest.approx(20)
    assert m["win_days"] == 2 and m["loss_days"] == 2


def test_option_multiplier_partial(db):
    """2 calls bought @1.50, 1 sold @2.00 -> (2.00-1.50) x 1 x 100 = +50 realized while still open."""
    acct = Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    kw = dict(asset_type="OPTION", option_type="CALL", strike=30.0, expiration=date(2026, 10, 16), multiplier=100.0,
              symbol_full="ABC 2026-10-16 30C")
    ingest_records(db, acct.id, "tos_statement", [rec(1, "ABC", "BUY", 2, 1.5, 1, **kw), rec(2, "ABC", "SELL", 1, 2.0, 2, **kw)])
    rebuild_trades(db, [acct.id])
    t = db.scalar(select(Trade))
    assert t.status == "OPEN"
    days = rz.daily(rz.events([t]), NY)
    assert [round(v.net, 2) for v in days.values()] == [50.0]
    assert quotes.yahoo_symbol(t) == "ABC261016C00030000"


@pytest.fixture()
def web(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def _charts(html):
    import re
    return json.loads(re.search(r"const C = (\{.*?\});\n", html).group(1))


def test_dashboard_calendar_and_daily_data(book, web):
    html = web.get("/").text
    assert 'id="pnl-calendar"' in html and 'data-cal="prev"' in html and 'data-cal="next"' in html and 'data-cal="today"' in html
    assert 'id="daily-widget"' in html and 'data-dw="prev"' in html
    C = _charts(html)
    days = {d["d"]: d for d in C["days"]}
    assert days["2026-09-03"]["net"] == 20.0 and days["2026-09-03"]["partial"] == 1   # a green day from a partial exit
    assert sum(d["net"] for d in C["days"]) == pytest.approx(13)
    assert C["equity"]["values"][-1] == pytest.approx(13) and C["equity"]["day"] == [-1.0, 39.5, 20.0, -45.5]
    assert "Total realized P&amp;L" in html and "+$13.00" in html and "Σ daily = total ✓" in html
    assert "2 closed · 1 open" in html


def test_dashboard_date_filter_clips_realized(book, web):
    html = web.get("/?preset=custom&start=2026-09-03&end=2026-09-03").text
    assert "+$20.00" in html  # only the partial exit of YYY happened that day
    assert _charts(html)["focus"] == "2026-09-03"


def test_tooltips_everywhere(book, web):
    from app.metric_info import info
    from app.widgets import CATALOGS
    for page, cat in CATALOGS.items():
        for w in cat:
            assert w.get("info"), (page, w["id"])
    for path, minimum in (("/", 20), ("/reports", 25), (f"/trades/{book[0].id}", 10)):
        html = web.get(path).text
        assert html.count('class="tip-btn"') >= minimum, path
        assert 'aria-label="About ' in html
    # every Reports stat card label resolves to a definition
    import re
    src = open("app/templates/reports.html").read()
    for label in set(re.findall(r"stat\('([^']+)'", src)):
        assert info(label), label
    assert info("realized")["calc"].startswith("Σ realized")


def test_trades_header_counts(book, web):
    html = web.get("/trades").text
    assert "3 trades (2 closed, 1 open)" in html
    assert "+$13.00" in html and "-$7.00" in html and "+$20.00" in html


def test_reports_headline_is_total_realized(book, web):
    html = web.get("/reports").text
    assert "Total realized P&amp;L" in html and "+$13.00" in html and "Closed trades net P&amp;L" in html
    assert "3 trades (2 closed, 1 open)" in html


# ------------------------------------------------------------------ quotes
def _chart(reg_px, reg_t, bars, pre=(1000, 2000), regular=(2000, 3000), post=(3000, 4000), prev=10.0):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": reg_px, "regularMarketTime": reg_t, "chartPreviousClose": prev,
            "currentTradingPeriod": {"pre": {"start": pre[0], "end": pre[1]}, "regular": {"start": regular[0], "end": regular[1]},
                                     "post": {"start": post[0], "end": post[1]}}},
            "timestamp": [t for t, _ in bars], "indicators": {"quote": [{"close": [c for _, c in bars]}]}}]}}


def test_parse_chart_prefers_freshest_price():
    # after-hours bar (3500) newer than the regular close (2999): use it, session = post
    q = quotes.parse_chart("X", _chart(11.0, 2999, [(2990, 10.9), (3500, 11.4), (3560, None)]), now=3600)
    assert q.price == 11.4 and q.session == "post" and q.market == "post"
    assert q.at == datetime.fromtimestamp(3500, timezone.utc) and q.regular_price == 11.0
    # during regular hours the last bar may lag regularMarketPrice: keep the newer one
    q = quotes.parse_chart("X", _chart(12.0, 2500, [(2440, 11.8)]), now=2510)
    assert q.price == 12.0 and q.session == "regular"
    assert quotes.parse_chart("X", {"chart": {"result": None}}) is None


def test_get_quotes_caches_and_survives_errors(monkeypatch):
    quotes.clear_cache()
    calls = []

    def fake(ysym):
        calls.append(ysym)
        if ysym == "BAD":
            raise RuntimeError("boom")
        return _chart(5.0, 2500, [(2500, 5.0)])
    from types import SimpleNamespace as NS
    trades = [NS(symbol="AAA", underlying="AAA", status="OPEN", asset_type="STOCK"),
              NS(symbol="BAD", underlying="BAD", status="OPEN", asset_type="STOCK"),
              NS(symbol="CCC", underlying="CCC", status="CLOSED", asset_type="STOCK")]
    got = quotes.get_quotes(trades, fetch=fake)
    assert set(got) == {"AAA"} and sorted(calls) == ["AAA", "BAD"]
    quotes.get_quotes(trades, fetch=fake)
    assert len(calls) == 2  # cached (incl. the failure) for TTL seconds
    quotes.clear_cache()
    quotes.get_quotes(trades, fetch=fake)
    assert len(calls) == 4


def test_positions_breakdown_uses_live_quotes_and_flags_mismatch(book, web, db, monkeypatch):
    """YYY: 3 left @50. Live 58 -> +24; SnapTrade (synced) 3 @55 -> +15, but SnapTrade says 4 shares."""
    acct = db.scalar(select(Account))
    from app.sources.snaptrade import CASHFLOW_STATE, PORTFOLIO_STATE
    set_state(db, PORTFOLIO_STATE + str(acct.id), json.dumps({"as_of": "2026-10-08T18:00:00", "cash": 0, "market_value": 0,
              "value": 0, "positions": [{"symbol": "YYY", "kind": "stock", "units": 4, "price": 55.0, "cost_basis": 50.0}]}))
    set_state(db, CASHFLOW_STATE + str(acct.id), json.dumps({}))
    db.commit()
    monkeypatch.setattr(quotes, "enabled", lambda: True)
    monkeypatch.setattr(quotes, "get_quotes", lambda trades: {"YYY": quotes.Quote(
        "YYY", 58.0, datetime(2026, 10, 9, 15, 19, tzinfo=timezone.utc), "regular", "regular")})
    html = web.get("/").text
    assert "+$24.00" in html and "+$15.00" in html
    assert "Oct 9 16:19 IST" in html            # price time in Irish time
    assert "qty differs: SnapTrade 4" in html
    # without live quotes the SnapTrade price is the fallback
    monkeypatch.setattr(quotes, "get_quotes", lambda trades: {})
    html = web.get("/").text
    assert "SnapTrade (last sync)" in html and "+$15.00" in html


def test_irish_time_filter():
    from app.web import irish_time
    assert irish_time(datetime(2026, 10, 9, 15, 19)) == "Oct 9 16:19 IST"
    assert irish_time(datetime(2026, 12, 1, 9, 5, tzinfo=timezone.utc)) == "Dec 1 09:05 GMT"


def test_new_default_widget_appears_in_saved_layouts(db):
    from app import widgets
    from app.services import set_state as ss
    ss(db, "layout:dashboard", json.dumps(["realized", "total_pnl", "win_rate"]))  # saved before "positions" existed
    db.commit()
    assert widgets.get_layout(db, "dashboard") == ["realized", "total_pnl", "positions", "win_rate", "coach"]  # + Coach insights (introduced later)
    widgets.save_layout(db, "dashboard", ["realized", "win_rate"])  # user hides it on purpose
    assert widgets.get_layout(db, "dashboard") == ["realized", "win_rate"]
