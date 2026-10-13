"""User-set widget heights on the dashboard: saved server-side, clamped, reset, KPI cards excluded."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import widgets


def test_set_get_reset_heights(db):
    assert widgets.set_height(db, "dashboard", "positions", 480) == {"positions": 480}
    widgets.set_height(db, "dashboard", "chart_balance", 99999)
    assert widgets.get_heights(db, "dashboard")["chart_balance"] == widgets.MAX_H
    widgets.set_height(db, "dashboard", "positions", None)
    assert "positions" not in widgets.get_heights(db, "dashboard")
    try:
        widgets.set_height(db, "dashboard", "realized", 300)  # KPI: fixed height
        raise AssertionError("kpi accepted")
    except ValueError:
        pass
    widgets.reset_layout(db, "dashboard")
    assert widgets.get_heights(db, "dashboard") == {}


def test_height_route_and_render(db):
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    r = c.post("/layout/dashboard/height", json={"widget": "positions", "height": 520})
    assert r.status_code == 200 and r.json()["heights"] == {"positions": 520}
    assert c.post("/layout/dashboard/height", json={"widget": "win_rate", "height": 300}).status_code == 400
    assert c.post("/layout/nope/height", json={"widget": "x"}).status_code == 404
    lay = widgets.layout_ctx(db, "dashboard")
    assert lay["heights"] == {"positions": 520}
    # the dashboard markup carries the saved height and a grip on big widgets only (needs some data)
    from datetime import datetime
    from app.instruments import ExecRecord
    from app.models import Account
    from app.services import ingest_records, rebuild_trades
    a = Account(name="A", broker="tos", currency="USD", source_key="tos")
    db.add(a)
    db.flush()
    ingest_records(db, a.id, "tos", [ExecRecord(external_id="1", symbol="AAA", underlying="AAA", asset_type="STOCK",
                                                side="BUY", quantity=1, price=10, executed_at=datetime(2026, 9, 2, 15))])
    rebuild_trades(db, [a.id])
    db.commit()
    html = c.get("/").text
    i = html.index('data-wid="positions"')
    tag = html[i:html.index(">", i)]
    assert "we-sized" in tag and "--wh: 520px" in tag
    assert html.count('class="we-grip"') == len(widgets.layout_ctx(db, "dashboard")["resizable"])
    j = html.index('data-wid="win_rate"')
    assert "we-tall" not in html[j:html.index(">", j)]
