"""Reports module, widget layouts (dashboard + trade stat bar) and per-trade planned risk."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import widgets
from app.models import Trade

TABS = ["overview", "timing", "price", "instrument", "tags", "winloss", "drawdown", "excursion"]


@pytest.fixture()
def client(db):
    from app.main import create_app
    c = TestClient(create_app())
    r = c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture()
def demo(client):
    client.post("/settings/demo/load")
    return client


def test_layout_clean_save_reset(db):
    d = widgets.default_layout("dashboard")
    assert widgets.get_layout(db, "dashboard") == d
    assert widgets.clean("dashboard", ["sqn", "bogus", "sqn", "realized", 5]) == ["sqn", "realized"]
    assert widgets.save_layout(db, "dashboard", ["kelly", "realized"]) == ["kelly", "realized"]
    assert widgets.get_layout(db, "dashboard") == ["kelly", "realized"]
    ctx = widgets.layout_ctx(db, "dashboard")
    assert ctx["shown"] == ["kelly", "realized"] and set(ctx["order"]) == set(ctx["catalog"])
    assert ctx["order"][:2] == ["kelly", "realized"]
    assert widgets.reset_layout(db, "dashboard") == d
    assert widgets.get_layout(db, "trade") == widgets.default_layout("trade")


def test_layout_api(client):
    assert client.get("/layout/nope").status_code == 404
    r = client.post("/layout/trade", json={"widgets": ["r_multiple", "net", "zzz"]})
    assert r.json() == {"ok": True, "widgets": ["r_multiple", "net"]}
    assert client.get("/layout/trade").json()["widgets"] == ["r_multiple", "net"]
    assert client.post("/layout/trade", content=b"{not json").status_code == 400
    assert client.post("/layout/trade/reset").json()["widgets"] == widgets.default_layout("trade")


def test_layout_requires_login(db):
    from app.main import create_app
    c = TestClient(create_app())
    r = c.post("/layout/dashboard", json={"widgets": []}, follow_redirects=False)
    assert r.status_code == 303


def test_dashboard_default_and_custom_layout(demo):
    html = demo.get("/").text
    assert 'id="dash-grid"' in html and 'data-wid="realized"' in html and 'data-wid="sqn"' in html
    # default: catalog extras are rendered hidden so they can be added without a reload
    assert 'data-wid="sqn"' in html.split('data-wid="recent"')[1]
    demo.post("/layout/dashboard", json={"widgets": ["sqn", "chart_drawdown", "realized"]})
    html = demo.get("/").text
    i_sqn, i_dd, i_real = (html.index(f'data-wid="{w}"') for w in ("sqn", "chart_drawdown", "realized"))
    assert i_sqn < i_dd < i_real
    seg = html[i_sqn:i_dd]
    assert "System quality" in seg or "SQN" in seg
    assert " hidden" not in html[i_sqn:html.index(">", i_sqn)]
    i_win = html.index('data-wid="win_rate"')
    assert " hidden" in html[i_win:html.index(">", i_win)]


@pytest.mark.parametrize("tab", TABS)
def test_reports_tabs_render(demo, tab):
    r = demo.get(f"/reports?tab={tab}")
    assert r.status_code == 200
    assert "data-autofilter" in r.text


def test_reports_filters_apply(demo, db):
    base = demo.get("/reports?tab=overview&direction=LONG&status=CLOSED").text
    n_long = len(db.scalars(select(Trade).where(Trade.direction == "LONG", Trade.status == "CLOSED")).all())
    assert f">{n_long}<" in base.replace(" ", "")
    assert demo.get("/reports?tab=instrument&symbol=zzzz").status_code == 200
    assert demo.get("/reports?tab=bogus").status_code == 200  # unknown tab falls back


def test_default_risk_and_trade_risk(demo, db):
    r = demo.post("/reports/risk", data={"default_risk": "100"}, headers={"referer": "/reports?tab=winloss"},
                  follow_redirects=False)
    assert r.status_code == 303
    from app.routes.reports import default_risk
    assert default_risk(db) == 100
    t = db.scalars(select(Trade).where(Trade.status == "CLOSED", Trade.asset_type == "STOCK")).first()
    r = demo.post(f"/trades/{t.id}/risk", data={"initial_stop": "", "risk_amount": "50", "profit_target": ""})
    assert r.status_code == 200 and "Saved" in r.text
    db.expire_all()
    t = db.get(Trade, t.id)
    assert t.risk_amount == 50 and t.initial_stop is None
    assert f"{t.net_pnl / 50:.2f}R" in r.text
    assert demo.post("/trades/999999/risk", data={}).status_code == 404


def test_risk_fields_survive_rebuild(demo, db):
    from app.services import rebuild_trades
    t = db.scalars(select(Trade).where(Trade.status == "CLOSED", Trade.asset_type == "STOCK")).first()
    key = (t.account_id, t.symbol, t.opened_at)
    t.initial_stop, t.risk_amount, t.profit_target = 1.5, 40.0, 9.0
    db.commit()
    rebuild_trades(db)
    db.expire_all()
    t2 = db.scalars(select(Trade).where(Trade.account_id == key[0], Trade.symbol == key[1],
                                        Trade.opened_at == key[2])).one()
    assert (t2.initial_stop, t2.risk_amount, t2.profit_target) == (1.5, 40.0, 9.0)
