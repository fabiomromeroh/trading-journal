"""Trades list instant filters / query-string carry-over and the trade page's trade list sidebar."""
import re
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.instruments import ExecRecord
from app.models import Account, Trade
from app.services import ingest_records, rebuild_trades


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    return c


def _seed(db):
    acct = Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    recs, n = [], 0
    base = datetime(2026, 9, 1, 14, 0)
    for i, (sym, exit_px) in enumerate([("AAA", 11), ("BBB", 9), ("CCC", 12), ("ABC", 8), ("DDD", 10.5)]):
        for side, px, dt in (("BUY", 10, base + timedelta(days=i)), ("SELL", exit_px, base + timedelta(days=i, hours=1))):
            n += 1
            recs.append(ExecRecord(external_id=f"n{n}", symbol=sym, underlying=sym, asset_type="STOCK", side=side,
                                   quantity=10, price=px, executed_at=dt, seq=n))
    ingest_records(db, acct.id, "tos_statement", recs)
    rebuild_trades(db, [acct.id])
    db.commit()
    return {t.symbol: t.id for t in db.scalars(select(Trade))}


def test_trades_list_carries_filters_into_trade_links(db):
    ids = _seed(db)
    c = _client(db)
    html = c.get("/trades?outcome=win&sort=pnl&dir=desc").text
    assert 'data-autofilter="swap"' in html and 'data-target="#trades-results"' in html
    assert f'/trades/{ids["CCC"]}?outcome=win&amp;sort=pnl&amp;dir=desc' in html
    assert "BBB" not in html.split('id="trades-results"')[1]
    assert 'class="btn btn-p text-xs nojs-only"' in html  # Filter button only as no-JS fallback


def test_trade_page_sidebar_follows_list_filter_and_sort(db):
    ids = _seed(db)
    c = _client(db)
    # winners sorted by P&L desc: CCC (+20), AAA (+10), DDD (+5)
    html = c.get(f"/trades/{ids['AAA']}?outcome=win&sort=pnl&dir=desc").text
    side = html.split('id="tl-items"')[1].split("</nav>")[0]
    order = re.findall(r'<span class="text-white font-medium truncate">(\w+)</span>', side)
    assert order == ["CCC", "AAA", "DDD"]
    assert 'aria-current="page"' in side and "filtered" in html
    assert re.search(rf'id="prev-trade" href="/trades/{ids["CCC"]}\?outcome=win', html)
    assert re.search(rf'id="next-trade" href="/trades/{ids["DDD"]}\?outcome=win', html)
    # a trade outside the filter still renders, with a note and chronological neighbours
    html = c.get(f"/trades/{ids['BBB']}?outcome=win").text
    assert "isn't in the current filter" in html and 'id="prev-trade"' in html
    # no filters: everything, newest first; first row has no Prev
    html = c.get(f"/trades/{ids['DDD']}").text
    side = html.split('id="tl-items"')[1].split("</nav>")[0]
    assert re.findall(r'truncate">(\w+)</span>', side) == ["DDD", "ABC", "CCC", "BBB", "AAA"]
    assert 'id="prev-trade"' not in html and 'id="next-trade"' in html
