"""Trades-list column picker."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import event

from app import trade_columns as tc
from app.models import Account, Dividend, Trade
from tests.test_companies import _trade, client  # noqa: F401  (fixtures)


def _seed(db):
    a = Account(name="Schwab", broker="schwab", currency="USD")
    db.add(a)
    db.flush()
    t = _trade(a, "AAPL", 50)
    t.initial_stop = 9.0
    t.mfe = 80.0
    db.add_all([t, _trade(a, "XOM", -20)])
    db.commit()
    return a, t


def test_default_and_clean():
    assert tc.default_columns()[:3] == ["opened", "closed", "symbol"]
    assert tc.clean(["pnl", "nope", "pnl", "r_multiple"]) == ["pnl", "r_multiple"]


def test_trades_page_picker_save_reorder_reset(db, client):  # noqa: F811
    _seed(db)
    page = client.get("/trades").text
    assert 'id="col-picker"' in page and 'data-colpick="r_multiple"' in page and "Reset to default" in page
    assert 'data-colrow="' + ",".join(tc.default_columns()) + '"' in page
    assert "trade_cols.js" in page and "sortable-1.15.6.min.js" in page
    r = client.post("/trades/columns", content=json.dumps({"columns": ["symbol", "r_multiple", "mfe_r", "pnl", "account"]}))
    assert r.status_code == 200 and r.json()["columns"] == ["symbol", "r_multiple", "mfe_r", "pnl", "account"]
    page = client.get("/trades?sort=pnl&dir=asc").text  # page cache dropped by the write; sort still works
    assert 'data-colrow="symbol,r_multiple,mfe_r,pnl,account"' in page
    assert ">R-multiple<" in page and "50.00R" in page  # net 50 / (1 share x 1.00 risk)
    assert "80.00R" in page and "Schwab" in page
    assert 'data-col="sector"' not in page
    assert page.index("XOM") < page.index("AAPL")  # ascending P&L
    assert client.post("/trades/columns", content=json.dumps({"columns": []})).status_code == 400
    assert client.post("/trades/columns/reset").json()["columns"] == tc.default_columns()
    assert 'data-colrow="' + ",".join(tc.default_columns()) + '"' in client.get("/trades").text


def test_dividend_column_batched(db, client):  # noqa: F811
    a, t = _seed(db)
    db.add(Dividend(account_id=a.id, symbol="AAPL", paid_on=datetime(2026, 9, 2, 10), amount=3.0, currency="USD",
                    source="manual", external_id="d1"))
    db.commit()
    from app.db import engine
    from app import pagecache

    def run(cols):
        client.post("/trades/columns", content=json.dumps({"columns": cols}))
        pagecache.bump()
        n = {"q": 0}

        def count(*_a, **_k):
            n["q"] += 1
        event.listen(engine, "before_cursor_execute", count)
        try:
            return client.get("/trades").text, n["q"]
        finally:
            event.remove(engine, "before_cursor_execute", count)
    _, base = run(["symbol", "pnl"])
    page, n = run(["symbol", "dividend", "pnl_div", "r_multiple", "mfe_r"])
    assert "$3.00" in page and "$53.00" in page
    assert n - base <= 4  # one dividend query + rates, not one per row


def test_for_trades_matches_for_trade(db):
    from app import dividends
    a, t = _seed(db)
    db.add_all([Dividend(account_id=a.id, symbol="AAPL", paid_on=datetime(2026, 9, 2, 10), amount=2.5, currency="USD",
                         source="manual", external_id="d2"),
                Dividend(account_id=a.id, symbol="AAPL", paid_on=datetime(2026, 8, 1), amount=9.0, currency="USD",
                         source="manual", external_id="d3")])
    db.commit()
    trades = list(db.query(Trade))
    batch = dividends.for_trades(db, trades)
    for tr in trades:
        assert batch[tr.id] == dividends.for_trade(db, tr)
