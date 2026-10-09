"""Break-even range, calendar day drill-down, widget widths, uniform cards, fill markers at price.
Synthetic data only; expected numbers worked out by hand in the comments."""
import json
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import metrics, outcome, prices, widgets
from app.instruments import ExecRecord
from app.models import Account, Trade
from app.services import ingest_records, rebuild_trades
from app.stats import compute
from tests.test_realized_daily import book  # noqa: F401  (fixture)
from tests.test_chart_timeframes import ET, _bars, _et, _rec, _trade

NY = "America/New_York"


@pytest.fixture()
def web(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


@pytest.fixture()
def four(db):
    """Closed trades with net +10, +2, -2, -10 (no fees)."""
    acct = Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    recs, n = [], 0
    for i, (sym, exit_px) in enumerate((("WA", 110), ("WB", 102), ("LA", 98), ("LB", 90))):
        recs += [ExecRecord(external_id=f"b{i}", symbol=sym, underlying=sym, asset_type="STOCK", side="BUY", quantity=1,
                            price=100.0, executed_at=datetime(2026, 9, 1 + i, 14), time_known=True),
                 ExecRecord(external_id=f"s{i}", symbol=sym, underlying=sym, asset_type="STOCK", side="SELL", quantity=1,
                            price=float(exit_px), executed_at=datetime(2026, 9, 1 + i, 15), time_known=True)]
    ingest_records(db, acct.id, "tos_statement", recs)
    rebuild_trades(db, [acct.id])
    db.commit()
    return list(db.scalars(select(Trade)))


# ------------------------------------------------------------------ break-even range
def test_default_range_keeps_old_behaviour(four):
    st = compute(four, NY)
    assert (st.wins, st.losses, st.scratches) == (2, 2, 0) and st.win_rate == 50
    assert outcome.classify(0) == "be" and outcome.classify(0.01) == "win" and outcome.classify(-0.01) == "loss"


def test_be_range_changes_every_statistic(four):
    outcome.set_range(-3, 3)
    st = compute(four, NY)
    assert (st.wins, st.losses, st.scratches) == (1, 1, 2)
    assert st.win_rate == 50 and st.profit_factor == pytest.approx(1.0)          # 10 / |-10|
    assert st.avg_win == 10 and st.avg_loss == -10 and st.net_pnl == 0              # BE P&L still in net
    m = metrics.summarize(four, NY, None)
    assert (m["wins"], m["losses"], m["be"]) == (1, 1, 2) and m["be_pct"] == 50
    assert m["gross_profit"] == 10 and m["gross_loss"] == -10 and m["expectancy"] == 0
    assert m["largest_win"] == 10 and m["largest_loss"] == -10
    # daily: Sep 2 (+2) and Sep 3 (-2) are BE days now
    assert (m["win_days"], m["loss_days"]) == (1, 1)
    with pytest.raises(ValueError):
        outcome.set_range(1, 3)


def test_be_setting_page_badges_filters_and_tooltips(four, web, db):
    r = web.post("/settings/break-even", data={"lower": "-3", "upper": "3"}, follow_redirects=True)
    assert r.status_code == 200 and "BE = net between -$3.00 and +$3.00" in r.text
    html = web.get("/trades").text
    assert html.count(">BE<") >= 2 and html.count(">WIN<") >= 1
    be_only = web.get("/trades?outcome=be").text
    assert "WB" in be_only and "LA" in be_only and "WA" not in be_only and "LB" not in be_only
    dash = web.get("/").text
    C = json.loads(re.search(r"const C = (\{.*?\});\n", dash).group(1))
    assert C["be"] == [-3.0, 3.0] and C["winloss"] == [1, 1, 2]
    assert "BE = net between -$3.00 and +$3.00" in dash  # in the win-rate tooltip
    # invalid range is rejected and the old one kept
    r = web.post("/settings/break-even", data={"lower": "2", "upper": "3"}, follow_redirects=True)
    assert "must include $0" in r.text and outcome.get() == (-3.0, 3.0)
    # stored server-side: a fresh request reloads it
    outcome.set_range(0, 0)
    assert "BE = net between -$3.00 and +$3.00" in web.get("/settings").text


# ------------------------------------------------------------------ day drill-down
def test_realized_day_lists_trades_with_events_that_day(book, web):  # noqa: F811
    # Sep 2: only ZZZ's partial exit (+40 - .5 fee = 39.5); ZZZ opened Sep 1 and closed Sep 4.
    html = web.get("/trades?realized_day=2026-09-02").text
    assert "Realized on <b class=\"text-white\">2026-09-02</b>" in html and "+$39.50" in html
    assert "1 trade with a closing fill or fees that day" in html and ">ZZZ<" in html
    # Sep 3: YYY partial exit of a still-open trade (+20)
    html = web.get("/trades?realized_day=2026-09-03").text
    assert "+$20.00" in html and ">YYY<" in html and ">ZZZ<" not in html
    # Sep 4: ZZZ final exit -30.5 + SSS -15 = -45.5, both listed with their own day amounts
    html = web.get("/trades?realized_day=2026-09-04").text
    assert "-$45.50" in html and "-$30.50" in html and "-$15.00" in html and "2 trades" in html
    # calendar links use it
    dash = web.get("/").text
    assert "realized_day" in open("app/static/pnl_widgets.js").read() and 'id="pnl-calendar"' in dash


def test_realized_day_bad_value_is_ignored(book, web):  # noqa: F811
    assert "Realized on" not in web.get("/trades?realized_day=nope").text


# ------------------------------------------------------------------ widget widths
def test_widget_widths_saved_and_reset(db, web):
    assert widgets.get_sizes(db, "dashboard")["calendar"] == "l"          # compact by default
    assert widgets.get_sizes(db, "dashboard")["chart_daily"] == "l"
    r = web.post("/layout/dashboard", json={"widgets": ["realized", "calendar", "chart_daily"],
                                            "sizes": {"calendar": "m", "chart_daily": "full", "realized": "full", "x": "s",
                                                      "chart_symbol": "huge"}})
    assert r.json()["ok"]
    s = widgets.get_sizes(db, "dashboard")
    assert s["calendar"] == "m" and s["chart_daily"] == "full"
    assert s["realized"] == "kpi" and s["chart_symbol"] == "m"                # KPI not resizable; bad size ignored
    widgets.reset_layout(db, "dashboard")
    assert widgets.get_sizes(db, "dashboard")["calendar"] == "l"


def test_dashboard_renders_size_controls_and_uniform_cards(four, web):
    html = web.get("/").text
    assert 'data-we-size="m"' in html and 'data-widths=' in html
    assert re.search(r'data-wid="calendar"[^>]*data-size="l"[^>]*class="[^"]*xl:col-span-8', html)
    n_kpi = html.count('class="card kpi-card')
    assert n_kpi >= 10 and html.count('class="kpi-v') == n_kpi  # every KPI uses the fixed-height card
    rep = web.get("/reports").text
    assert rep.count("stat-card") >= 20


def test_positions_table_columns_aligned(book, web, db, monkeypatch):  # noqa: F811
    from app import quotes
    monkeypatch.setattr(quotes, "enabled", lambda: True)
    monkeypatch.setattr(quotes, "get_quotes", lambda trades: {"YYY": quotes.Quote(
        "YYY", 58.0, datetime(2026, 10, 9, 15, 19, tzinfo=timezone.utc), "regular", "regular")})
    html = web.get("/").text
    m = re.search(r'<table class="w-full text-sm tbl tbl-num table-auto">(.*?)</table>', html, re.S)
    assert m
    head = re.findall(r"<th[^>]*>", m.group(1).split("</thead>")[0].split("<tr>")[-1])
    row = re.findall(r"<td[^>]*>", m.group(1).split("<tbody>")[1].split("</tr>")[0])
    assert len(head) == len(row) == 11
    align = lambda tag: "right" if "text-right" in tag else "left"  # noqa: E731
    assert [align(h) for h in head] == [align(c) for c in row]
    css = open("app/static/app.css").read()
    assert ".tbl th.text-right" in css  # header alignment isn't overridden by `.tbl th {text-align:left}`


# ------------------------------------------------------------------ fill markers at price
def test_fill_markers_carry_price_and_kind(db):
    t = _trade(db, [_rec("RKX", "BUY", 10, 100, _et(2026, 10, 6, 10, 2)), _rec("RKX", "BUY", 5, 101, _et(2026, 10, 6, 10, 30)),
                    _rec("RKX", "SELL", 5, 103, _et(2026, 10, 6, 11, 0)), _rec("RKX", "SELL", 10, 104, _et(2026, 10, 6, 14, 0))])
    bars = _bars(_et(2026, 10, 5, 9, 30), _et(2026, 10, 7, 16, 0), 5)
    mk, hidden = prices.markers(t, bars, "5m")
    assert hidden == 0 and [m["kind"] for m in mk] == ["entry", "add", "partial", "exit"]
    assert [m["price"] for m in mk] == [100, 101, 103, 104] and [m["side"] for m in mk] == ["BUY", "BUY", "SELL", "SELL"]
    assert mk[2]["text"] == "S 5 @ 103 (partial)" and mk[0]["at"].endswith("ET")
    daily = prices.normalize(_bars(_et(2026, 10, 1, 9, 30), _et(2026, 10, 8, 9, 30), 390), "1D")
    mk, _ = prices.markers(t, daily, "1D")
    assert len(mk) == 4 and len({m["time"] for m in mk}) == 1  # all on the Oct 6 daily candle, each at its price
    js = open("app/static/trade_chart.js").read()
    assert "atPriceMiddle" in js and "atPriceBottom" in js and "data-fill-legend" in js
