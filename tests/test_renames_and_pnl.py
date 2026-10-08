"""Ticker renames (SATS -> ECHO style) and realized / unrealized / account-check P&L.
Synthetic data only."""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.importers import tos_statement
from app.models import Account, Execution, Trade
from app.services import get_state, ingest_records, rebuild_trades, set_state
from app.sources.snaptrade import CASHFLOW_STATE, PORTFOLIO_STATE, SnapTradeSource, portfolio_summary
from app.stats import compute, open_lots, unrealized
from app.symbols import AUTO_STATE, canonical_symbol, load_aliases, parse_alias_text
from app.sync import execute_run, start_run
from tests.test_merge_regression import act
from tests.test_snaptrade_source import ACCOUNT_ID, FakeSnapTrade, st_env  # noqa: F401
import tests.test_merge_regression as mr


def _rows(old="SATS"):
    mr._n = 100
    return [act("2026-04-10", "BUY", old, 1, 30.00), act("2026-04-22", "SELL", old, -1, 22.00, 0.01),
            act("2026-04-13", "BUY", "KEEP", 10, 5.00), act("2026-04-14", "SELL", "KEEP", -10, 5.50, 0.01)]


def _tos(new="ECHO"):
    return f"""Account Statement for 77770123 (individual) since 1/1/26 through 10/7/26

Account Trade History
,Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type
,4/22/26 14:00:00,STOCK,SELL,-1,TO CLOSE,{new},,,STOCK,22.00,22.00,STP
,4/14/26 15:01:00,STOCK,SELL,-10,TO CLOSE,KEEP,,,STOCK,5.50,5.50,LMT
,4/13/26 15:00:00,STOCK,BUY,+10,TO OPEN,KEEP,,,STOCK,5.00,5.00,LMT
,4/10/26 16:00:00,STOCK,BUY,+1,TO OPEN,{new},,,STOCK,30.00,30.00,LMT
"""


def _sync(db, rows):
    run, _ = start_run(db, "manual")
    return execute_run(run.id, sources=[SnapTradeSource(client=FakeSnapTrade(rows, through="2026-10-07").client())])


@pytest.fixture()
def web(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_alias_parsing_and_canonical():
    assert parse_alias_text("sats = echo\n# note\nFB->META\nOLD=") == {"SATS": "ECHO", "FB": "META", "OLD": ""}
    with pytest.raises(ValueError):
        parse_alias_text("not a ticker!=X")
    al = {"SATS": "ECHO", "A": "B", "B": "C"}
    assert canonical_symbol("SATS", al) == "ECHO"
    assert canonical_symbol("SATS 2026-09-18 30C", al) == "ECHO 2026-09-18 30C"
    assert canonical_symbol("A", al) == "C" and canonical_symbol("MSFT", al) == "MSFT"


def test_builtin_rename_merges_old_and_new_ticker(db, st_env):  # noqa: F811
    """SnapTrade reports the old ticker (SATS), thinkorswim the new one (ECHO): one trade, not two."""
    assert _sync(db, _rows()).status == "success"
    acct = db.scalar(select(Account))
    stats = ingest_records(db, acct.id, "tos_statement", tos_statement.parse(_tos()).records)
    assert stats.inserted == 0 and stats.merged == 4
    rebuild_trades(db, [acct.id])
    db.commit()
    syms = sorted(t.symbol for t in db.scalars(select(Trade)))
    assert syms == ["ECHO", "KEEP"]
    echo = db.scalar(select(Trade).where(Trade.symbol == "ECHO"))
    assert echo.net_pnl == pytest.approx(22.00 - 30.00 - 0.01) and echo.time_known
    # stored fills keep the ticker their source reported
    assert {e.symbol for e in db.scalars(select(Execution))} == {"SATS", "KEEP"}
    assert _sync(db, _rows()).inserted == 0


def test_rename_detected_automatically(db, st_env):  # noqa: F811
    assert _sync(db, _rows(old="OLDX")).status == "success"
    acct = db.scalar(select(Account))
    stats = ingest_records(db, acct.id, "tos_statement", tos_statement.parse(_tos(new="NEWX")).records)
    assert stats.inserted == 0 and stats.merged == 4
    assert stats.detected_aliases and stats.detected_aliases["OLDX"][0] == "NEWX"
    assert json.loads(get_state(db, AUTO_STATE))["OLDX"]["to"] == "NEWX"
    rebuild_trades(db, [acct.id])
    assert sorted(t.symbol for t in db.scalars(select(Trade))) == ["KEEP", "NEWX"]


def test_position_continues_across_rename_and_settings_alias(db, web, st_env):  # noqa: F811
    mr._n = 200
    rows = [act("2026-06-20", "BUY", "OLDY", 10, 10.0), act("2026-07-01", "SELL", "NEWY", -10, 12.0, 0.02)]
    assert _sync(db, rows).status == "success"
    # without the alias the NEWY sale has no position to close (orphan) and OLDY stays open
    assert sorted((t.symbol, t.status) for t in db.scalars(select(Trade))) == [("OLDY", "OPEN")]
    r = web.post("/settings/aliases", data={"aliases": "OLDY=NEWY"}, follow_redirects=True)
    assert r.status_code == 200 and "Ticker aliases saved" in r.text and "OLDY" in r.text
    db.expire_all()
    trades = list(db.scalars(select(Trade)))
    assert [(t.symbol, t.status) for t in trades] == [("NEWY", "CLOSED")]
    assert trades[0].net_pnl == pytest.approx(20 - 0.02)
    # A built-in alias can be switched off
    web.post("/settings/aliases", data={"aliases": "OLDY=NEWY\nSATS="})
    db.expire_all()
    assert "SATS" not in load_aliases(db)


def test_realized_includes_partial_exits_and_unrealized(db):
    acct = Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    from app.instruments import ExecRecord
    def rec(i, side, q, p, d):
        return ExecRecord(external_id=f"x{i}", symbol="ZZZ", underlying="ZZZ", asset_type="STOCK", side=side,
                          quantity=q, price=p, executed_at=datetime(2026, 9, d, 15), time_known=True)
    ingest_records(db, acct.id, "tos_statement", [rec(1, "BUY", 7, 100.0, 1), rec(2, "SELL", 4, 110.0, 2)])
    rebuild_trades(db, [acct.id])
    trades = list(db.scalars(select(Trade)))
    st = compute(trades, "America/New_York", open_count=1)
    assert st.net_pnl == 0 and st.open_realized == pytest.approx(40) and st.realized == pytest.approx(40)
    assert open_lots(trades[0]) == [(3, 100.0)]
    u, rows = unrealized(trades, {"ZZZ": 120.0})
    assert u == pytest.approx(60) and rows[0]["avg"] == pytest.approx(100)


def test_dashboard_account_check(db, web, st_env):  # noqa: F811
    mr._n = 300
    rows = [act("2026-01-05", "TRANSFER", "CASHX0", 0, 0), act("2026-09-01", "BUY", "ZZZ", 7, 100.0),
            act("2026-09-02", "SELL", "ZZZ", -4, 110.0)]
    rows[0].update(amount=1000.0, units=0.0)
    assert _sync(db, rows).status == "success"
    acct = db.scalar(select(Account))
    flows = json.loads(get_state(db, CASHFLOW_STATE + str(acct.id)))
    assert [f["amount"] for f in flows.values()] == [1000.0]
    # balances/positions snapshot as the sync would store it: cash 1000 - 700 + 440 = 740, 3 ZZZ @ 120
    set_state(db, PORTFOLIO_STATE + str(acct.id), json.dumps({
        "as_of": "2026-10-08T07:40:00", "cash": 740.0, "market_value": 360.0, "value": 1100.0,
        "positions": [{"symbol": "ZZZ", "kind": "stock", "units": 3, "price": 120.0, "cost_basis": 100.0}]}))
    db.commit()
    p = portfolio_summary(db, [acct.id])
    assert p["net_deposits"] == 1000 and p["total_pnl"] == 100
    html = web.get("/").text
    assert "Realized P&amp;L" in html and "Unrealized (open)" in html and "Total P&amp;L" in html
    assert "+$100.00" in html and "✓" in html


def test_set_state_twice_in_one_session(db):
    set_state(db, "k", "1")
    set_state(db, "k", "2")
    db.commit()
    assert get_state(db, "k") == "2"
