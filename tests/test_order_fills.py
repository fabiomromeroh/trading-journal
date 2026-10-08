"""Same-day fills from SnapTrade orders (provisional) superseded by next-day activities.
Synthetic data only."""
from datetime import datetime

import pytest
from sqlalchemy import select

import app.sources.snaptrade as snap
import tests.test_merge_regression as mr
from app.importers import tos_statement
from app.models import Account, Execution, Trade
from app.services import ingest_records
from app.sources.snaptrade import SnapTradeSource, parse_orders
from app.sync import execute_run, start_run
from tests.test_merge_regression import act
from tests.test_renames_and_pnl import web  # noqa: F401
from tests.test_snaptrade_source import FakeSnapTrade, st_env  # noqa: F401

DAY1_EVENING = datetime(2026, 10, 8, 20, 30)   # UTC; 16:30 ET on Oct 8
DAY2_MORNING = datetime(2026, 10, 9, 13, 0)


def order(oid, day, action, sym, qty, price, status="EXECUTED"):
    return {"brokerage_order_id": oid, "status": status, "action": action, "symbol": "uuid-ignored",
            "universal_symbol": {"symbol": sym, "raw_symbol": sym}, "option_symbol": None,
            "total_quantity": f"{qty:.18f}", "filled_quantity": f"{qty:.18f}", "open_quantity": "0.00",
            "execution_price": f"{price:.10f}", "order_type": "Market", "time_in_force": "DAY",
            "time_placed": f"{day}T00:00:00Z", "time_updated": f"{day}T00:00:00Z",
            "time_executed": f"{day}T00:00:00Z"}


def history():
    mr._n = 500
    return [act("2026-10-06", "BUY", "ZETA", 4, 52.10, 0.0)]


def sell_activity(ref="OID1", sym="ZETA", price=50.25, fee=0.05):
    a = act("2026-10-08", "SELL", sym, -4, price, fee)
    a["external_reference_id"] = ref
    return a


def sync(db, monkeypatch, now, activities, through, orders=None, recent=None):
    monkeypatch.setattr(snap, "utcnow", lambda: now)
    fake = FakeSnapTrade(activities, through=through, orders=orders, recent=recent)
    run, _ = start_run(db, "manual")
    run = execute_run(run.id, sources=[SnapTradeSource(client=fake.client())])
    db.expire_all()
    assert run.status == "success", run.message
    return run, fake


def execs(db):
    return list(db.scalars(select(Execution).order_by(Execution.id)))


def tos_text(time_="14:35:32", sym="ZETA", price="50.25"):
    return f"""Account Statement for 77770123 (individual) since 10/1/26 through 10/8/26

Account Trade History
,Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type
,10/8/26 {time_},STOCK,SELL,-4,TO CLOSE,{sym},,,STOCK,{price},{price},MKT
,10/6/26 10:01:02,STOCK,BUY,+4,TO OPEN,{sym},,,STOCK,52.10,52.10,MKT
"""


def test_parse_orders_mapping():
    recs = parse_orders([order("A", "2026-10-08", "SELL", "zeta", 4, 50.25),
                         order("B", "2026-10-08", "BUY", "QQQ", 0, 1.0),               # nothing filled
                         order("C", "2026-10-08", "SELL_SHORT", "XX", 2, 10.0),
                         order("A", "2026-10-08", "SELL", "ZETA", 4, 50.25)])          # repeated id
    assert [(r.external_id, r.symbol, r.side, r.quantity, r.price, r.position_effect, str(r.trade_date),
             r.time_known, r.fees) for r in recs] == [
        ("A", "ZETA", "SELL", 4.0, 50.25, "CLOSE", "2026-10-08", False, 0.0),
        ("C", "XX", "SELL", 2.0, 10.0, "OPEN", "2026-10-08", False, 0.0)]


def test_order_first_then_activity_next_day_is_one_fill(db, web, st_env, monkeypatch):  # noqa: F811
    o = order("OID1", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    run, fake = sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
    assert not any(c.url.path.endswith("/v2") for c in fake.calls)
    rows = execs(db)
    assert [(e.source, e.side, e.quantity, e.price, e.fees) for e in rows] == [
        ("snaptrade", "BUY", 4, 52.10, 0), ("snaptrade_order", "SELL", 4, 50.25, 0)]
    t = db.scalar(select(Trade))
    assert t.status == "CLOSED" and t.net_pnl == pytest.approx((50.25 - 52.10) * 4)
    assert "provisional" in run.message
    assert "provisional (fees pending)" in web.get("/trades").text
    # syncing again the same evening changes nothing
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
    assert len(execs(db)) == 2

    # next day: the activity (same Schwab id, with fees) supersedes the provisional fill
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08", orders=[o], recent=[])
    rows = execs(db)
    assert [(e.source, e.external_id, e.side, e.fees) for e in rows] == [
        ("snaptrade", "REF0501", "BUY", 0), ("snaptrade", "OID1", "SELL", 0.05)]
    t = db.scalar(select(Trade))
    assert t.net_pnl == pytest.approx((50.25 - 52.10) * 4 - 0.05)
    assert "provisional (fees pending)" not in web.get("/trades").text


def test_activity_with_other_id_still_merges_by_fill(db, st_env, monkeypatch):  # noqa: F811
    o = order("OID9", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o])
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity(ref="SCHWAB-REF", price=50.2501)],
         "2026-10-08", orders=[o])
    assert [(e.source, e.external_id) for e in execs(db)] == [("snaptrade", "REF0501"), ("snaptrade", "SCHWAB-REF")]


def test_activity_only(db, st_env, monkeypatch):  # noqa: F811
    acts = history() + [sell_activity()]
    sync(db, monkeypatch, DAY2_MORNING, acts, "2026-10-08")             # orders endpoint unavailable
    sync(db, monkeypatch, DAY2_MORNING, acts, "2026-10-08",
         orders=[order("OID1", "2026-10-08", "SELL", "ZETA", 4, 50.25)], recent=[])
    assert [e.source for e in execs(db)] == ["snaptrade", "snaptrade"]
    assert len(list(db.scalars(select(Trade)))) == 1


@pytest.mark.parametrize("tos_first", [True, False])
def test_tos_import_order_and_activity_make_one_timed_fill(db, st_env, monkeypatch, tos_first):  # noqa: F811
    o = order("OID1", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    acct = db.scalar(select(Account))
    tos_recs = tos_statement.parse(tos_text()).records
    want = next(r.executed_at for r in tos_recs if r.side == "SELL")
    if tos_first:
        ingest_records(db, acct.id, "tos_statement", tos_recs)
        db.commit()
        sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
    else:
        sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
        ingest_records(db, acct.id, "tos_statement", tos_recs)
        db.commit()
    sells = [e for e in execs(db) if e.side == "SELL"]
    assert len(sells) == 1 and sells[0].time_known and sells[0].executed_at == want
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08", orders=[o])
    sells = [e for e in execs(db) if e.side == "SELL"]
    assert [(e.source, e.external_id, e.time_known, e.executed_at, e.fees) for e in sells] == [
        ("snaptrade", "OID1", True, want, 0.05)]
    assert len(execs(db)) == 2 and len(list(db.scalars(select(Trade)))) == 1


def test_renamed_ticker_order_matches_old_ticker_activity(db, st_env, monkeypatch):  # noqa: F811
    """Built-in alias SATS -> ECHO: the order reports the new ticker, the activity the old one."""
    mr._n = 600
    hist = [act("2026-10-06", "BUY", "SATS", 4, 30.0)]
    o = order("OIDR", "2026-10-08", "SELL", "ECHO", 4, 31.0)
    sync(db, monkeypatch, DAY1_EVENING, hist, "2026-10-07", orders=[o])
    assert [t.status for t in db.scalars(select(Trade))] == ["CLOSED"]
    sync(db, monkeypatch, DAY2_MORNING, hist + [sell_activity(ref="OIDR", sym="SATS", price=31.0)], "2026-10-08",
         orders=[o])
    assert [(e.source, e.symbol) for e in execs(db)] == [("snaptrade", "SATS"), ("snaptrade", "SATS")]
    assert len(list(db.scalars(select(Trade)))) == 1


def test_unconfirmed_provisional_fill_is_pruned(db, st_env, monkeypatch):  # noqa: F811
    o = order("OIDP", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o])
    assert len(execs(db)) == 2
    # two days later the activity feed covers Oct 8 and never had this fill
    run, _ = sync(db, monkeypatch, datetime(2026, 10, 10, 14, 0), history(), "2026-10-09", orders=[])
    assert [e.source for e in execs(db)] == ["snaptrade"] and "removed 1 provisional" in run.message
