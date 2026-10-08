"""Regression: a thinkorswim statement imported after a SnapTrade sync duplicated every fill.

Causes reproduced here with synthetic data (no real account data):
  * the statement has no account number matching the synced account, so 'Auto' created a new
    "Schwab" account and per-account dedupe never saw the SnapTrade fills;
  * thinkorswim wrote Exec Time in the platform's zone (Europe/Dublin), read as New York time;
  * thinkorswim lists partial fills separately (3 + 1 + 1) where Schwab/SnapTrade has one row.
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.importers import tos_statement
from app.matching import Item, match
from app.models import Account, Execution, ImportBatch, Tag, Trade
from app.services import ingest_records, rebuild_trades
from app.sources.snaptrade import SnapTradeSource
from app.sync import execute_run, start_run
from tests.test_snaptrade_source import ACCOUNT_ID, FakeSnapTrade, st_env  # noqa: F401

_n = 0


def act(day, typ, sym, units, price, fee=0.0):
    global _n
    _n += 1
    amt = round(-units * price - fee, 2)
    return {"id": f"00000000-0000-4000-9000-{_n:012d}", "type": typ, "description": f"{sym} SAMPLE CORP",
            "symbol": {"symbol": sym, "raw_symbol": sym}, "option_symbol": None, "option_type": "",
            "units": units, "price": price, "amount": amt, "fee": fee,
            "external_reference_id": f"REF{_n:04d}", "trade_date": f"{day}T00:00:00Z",
            "settlement_date": f"{day}T00:00:00Z"}


def snaptrade_rows():
    global _n
    _n = 0  # deterministic ids: every call returns the same SnapTrade activities
    return [
        act("2026-09-01", "BUY", "AAAA", 10, 50.10), act("2026-09-01", "SELL", "AAAA", -10, 50.40, 0.03),
        act("2026-09-08", "BUY", "BBBB", 30, 20.00),
        act("2026-09-08", "SELL", "BBBB", -10, 20.50, 0.01), act("2026-09-08", "SELL", "BBBB", -10, 20.30, 0.01),
        act("2026-09-09", "SELL", "BBBB", -10, 19.80, 0.02),          # one row for three ToS partials
        act("2026-09-08", "BUY", "CCCC", 2, 80.00),
        act("2026-09-09", "SELL", "CCCC", -2, 78.00, 0.01),           # sold at the open ...
        act("2026-09-09", "BUY", "CCCC", 3, 77.50),                   # ... re-bought later that day
    ]


# Exec times as thinkorswim writes them on a computer set to Dublin (UTC+1 in September).
TOS = """This document was exported from the thinkorswim platform.

Account Statement for 77770123 (individual) since 9/1/26 through 9/9/26

Account Trade History
,Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type
,9/9/26 20:14:05,STOCK,SELL,-2,TO CLOSE,BBBB,,,STOCK,19.80,19.80,LMT
,9/9/26 20:14:05,STOCK,SELL,-2,TO CLOSE,BBBB,,,STOCK,19.80,19.80,LMT
,9/9/26 20:14:05,STOCK,SELL,-6,TO CLOSE,BBBB,,,STOCK,19.80,19.80,LMT
,9/9/26 19:56:22,STOCK,BUY,+3,TO OPEN,CCCC,,,STOCK,77.50,77.50,LMT
,9/9/26 14:30:40,STOCK,SELL,-2,TO CLOSE,CCCC,,,STOCK,78.00,78.00,MKT
,9/8/26 21:01:45,STOCK,SELL,-10,TO CLOSE,BBBB,,,STOCK,20.30,20.30,LMT
,9/8/26 16:45:12,STOCK,SELL,-10,TO CLOSE,BBBB,,,STOCK,20.50,20.50,LMT
,9/8/26 14:53:30,STOCK,BUY,+2,TO OPEN,CCCC,,,STOCK,80.00,80.00,LMT
,9/8/26 14:36:35,STOCK,BUY,+30,TO OPEN,BBBB,,,STOCK,20.00,20.00,LMT
,9/1/26 22:13:59,STOCK,SELL,-10,TO CLOSE,AAAA,,,STOCK,50.40,50.40,LMT
,9/1/26 14:54:32,STOCK,BUY,+10,TO OPEN,AAAA,,,STOCK,50.10,50.10,LMT
"""


def _sync(db, rows):
    run, _ = start_run(db, "manual")
    return execute_run(run.id, sources=[SnapTradeSource(client=FakeSnapTrade(rows, through="2026-09-09").client())])


def _snapshot(db):
    trades = list(db.scalars(select(Trade)))
    return len(trades), round(sum(t.net_pnl for t in trades), 4)


@pytest.fixture()
def web(db):
    from app.main import create_app
    c = TestClient(create_app())
    r = c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def test_timezone_detection():
    times = [datetime(2026, 9, 9, 14, 30, 40), datetime(2026, 9, 9, 20, 14, 5), datetime(2026, 9, 1, 22, 13, 59),
             datetime(2026, 9, 8, 14, 36, 35), datetime(2026, 9, 8, 21, 1, 45)]
    assert tos_statement.detect_timezone(times, "America/New_York") == "Europe/Dublin"
    ny = [datetime(2026, 9, 11, 10, 1, 2), datetime(2026, 9, 11, 15, 20), datetime(2026, 9, 12, 9, 41, 30)]
    assert tos_statement.detect_timezone(ny, "America/New_York") == "America/New_York"
    res = tos_statement.parse(TOS)
    assert res.timezone == "Europe/Dublin" and res.account_hint == "77770123"
    first = next(r for r in res.records if r.symbol == "CCCC" and r.side == "SELL")
    assert first.executed_at == datetime(2026, 9, 9, 13, 30, 40)  # 09:30:40 ET
    assert first.trade_date == date(2026, 9, 9)


def test_matching_partials_tolerance_and_adjacent_day():
    d = date(2026, 9, 9)
    inc = [Item("p1", d, "X", "SELL", "TRADE", 3, 190.15, (1,)), Item("p2", d, "X", "SELL", "TRADE", 1, 190.16, (2,)),
           Item("p3", d, "X", "SELL", "TRADE", 1, 190.14, (3,)), Item("q", date(2026, 9, 10), "Y", "BUY", "TRADE", 2, 10.0, (4,))]
    ex = [Item("E", d, "X", "SELL", "TRADE", 5, 190.15, (1,)), Item("F", d, "Y", "BUY", "TRADE", 2, 10.004, (2,))]
    ms = {tuple(sorted(i.ref for i in m.incoming)): [e.ref for e in m.existing] for m in match(inc, ex)}
    assert ms == {("p1", "p2", "p3"): ["E"], ("q",): ["F"]}
    # different side / price outside tolerance never match
    assert match([Item("a", d, "X", "BUY", "TRADE", 5, 190.15)], ex) == []
    assert match([Item("a", d, "X", "SELL", "TRADE", 5, 191.0)], ex) == []


def test_tos_import_after_snaptrade_merges_instead_of_duplicating(db, web, st_env):  # noqa: F811
    rows = snaptrade_rows()
    run = _sync(db, rows)
    assert run.status == "success", run.error
    n_exec = db.query(Execution).count()
    before = _snapshot(db)
    acct = db.scalar(select(Account))

    r = web.post("/import/upload", files={"file": ("2026-09-10-AccountStatement.csv", TOS.encode(), "text/csv")},
                 data={"account": ""}, follow_redirects=True)
    assert r.status_code == 200 and "merge" in r.text
    batch = db.scalar(select(ImportBatch))
    assert batch.account_id == acct.id            # defaulted to the synced account, no new "Schwab" account
    web.post(f"/import/{batch.id}/commit")
    db.expire_all()
    batch = db.get(ImportBatch, batch.id)
    assert (batch.inserted, batch.merged, batch.duplicates) == (0, 11, 0)
    assert db.query(Account).count() == 1
    assert db.query(Execution).count() == n_exec   # nothing duplicated
    assert db.query(Execution).filter(Execution.time_known.is_(True)).count() == n_exec
    agg = db.scalar(select(Execution).where(Execution.symbol == "BBBB", Execution.quantity == 10,
                                            Execution.price == 19.80))
    assert agg.source == "snaptrade" and agg.executed_at == datetime(2026, 9, 9, 19, 14, 5)  # 15:14:05 ET
    assert agg.fees == pytest.approx(0.02)           # SnapTrade fee kept (ToS has none)
    count, pnl = _snapshot(db)
    assert pnl == pytest.approx(before[1])
    # With real times CCCC is sell-then-buy: 1 closed + 1 open trade (date-only guessed buy-first).
    cccc = sorted((t.status, t.quantity) for t in db.scalars(select(Trade).where(Trade.symbol == "CCCC")))
    assert cccc == [("CLOSED", 2.0), ("OPEN", 3.0)]

    # Later syncs neither re-add the fills nor lose the times.
    run2 = _sync(db, rows)
    assert run2.inserted == 0
    db.expire_all()
    assert db.query(Execution).count() == n_exec
    assert db.get(Execution, agg.id).executed_at == datetime(2026, 9, 9, 19, 14, 5)
    # Re-importing the same statement is a no-op.
    web.post("/import/upload", files={"file": ("again.csv", TOS.encode(), "text/csv")}, data={"account": ""})
    b2 = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc())).first()
    web.post(f"/import/{b2.id}/commit")
    db.expire_all()
    b2 = db.get(ImportBatch, b2.id)
    assert b2.inserted == 0 and b2.merged == 0 and b2.duplicates == 11


def test_snaptrade_sync_after_tos_import_replaces_file_rows(db, st_env):  # noqa: F811
    acct = Account(name="Schwab Individual ...123", broker="schwab", account_number_masked="...123",
                   external_ref=f"snaptrade:{ACCOUNT_ID}")
    db.add(acct)
    db.commit()
    stats = ingest_records(db, acct.id, "tos_statement", tos_statement.parse(TOS).records)
    assert stats.inserted == 11
    rebuild_trades(db, [acct.id])
    t = db.scalar(select(Trade).where(Trade.symbol == "AAAA"))
    t.notes = "journal survives"
    db.commit()
    run = _sync(db, snaptrade_rows())
    assert run.status == "success", run.error
    assert run.inserted == 0
    db.expire_all()
    execs = list(db.scalars(select(Execution)))
    assert len(execs) == 9 and {e.source for e in execs} == {"snaptrade"} and all(e.time_known for e in execs)
    assert db.scalar(select(Trade).where(Trade.symbol == "AAAA")).notes == "journal survives"
    assert _sync(db, snaptrade_rows()).inserted == 0
    assert db.query(Execution).count() == 9


def test_move_misassigned_import_repairs_duplicates(db, web, st_env, monkeypatch):  # noqa: F811
    """The live state: statement imported into a separate 'Schwab' account with NY-read times."""
    run = _sync(db, snaptrade_rows())
    assert run.status == "success"
    expected = _snapshot(db)
    real = db.scalar(select(Account))
    wrong = Account(name="Schwab", broker="schwab")
    db.add(wrong)
    db.flush()
    wrong_id = wrong.id
    batch = ImportBatch(filename="2026-09-10-AccountStatement.csv", file_format="tos_statement",
                        account_id=wrong.id, status="committed", rows_total=11, rows_trades=11)
    db.add(batch)
    db.flush()
    recs = tos_statement.parse(TOS, tz="America/New_York").records      # old behaviour
    batch.inserted = ingest_records(db, wrong.id, "tos_statement", recs, batch_id=batch.id).inserted
    rebuild_trades(db, [wrong.id])
    dup = db.scalar(select(Trade).where(Trade.account_id == wrong.id, Trade.symbol == "AAAA"))
    dup.notes, dup.rating = "note on the duplicate", 4
    dup.tags.append(Tag(name="breakout"))
    db.commit()
    assert db.query(Execution).count() == 9 + 11

    r = web.post(f"/import/{batch.id}/move", data={"account": str(real.id)}, follow_redirects=True)
    assert r.status_code == 200 and "Moved 11 fills" in r.text and "Europe/Dublin" in r.text
    db.expire_all()
    assert db.scalar(select(Account).where(Account.id == wrong_id)) is None  # empty import-created account removed
    assert db.query(Execution).count() == 9
    assert db.query(Execution).filter(Execution.time_known.is_(True)).count() == 9
    b = db.get(ImportBatch, batch.id)
    assert b.account_id == real.id and b.merged == 11 and b.inserted == 0
    t = db.scalar(select(Trade).where(Trade.symbol == "AAAA"))
    assert t.account_id == real.id and t.notes == "note on the duplicate" and t.rating == 4
    assert [x.name for x in t.tags] == ["breakout"]
    assert t.opened_at == datetime(2026, 9, 1, 13, 54, 32)       # 09:54:32 ET
    count, pnl = _snapshot(db)
    assert pnl == pytest.approx(expected[1])
    assert _sync(db, snaptrade_rows()).inserted == 0


def test_backup_export(db, web):
    r = web.get("/settings/backup.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert "executions" in data["tables"] and data["tables"]["oauth_tokens"]["omitted"] == "credentials"
