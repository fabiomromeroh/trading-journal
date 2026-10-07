from datetime import date

import pytest
from sqlalchemy import select

from app.importers import detect_format, parse
from app.importers.base import parse_money
from app.models import Account, Execution, Trade
from app.services import ingest_records, plan_ingest, rebuild_trades


def test_parse_money():
    assert parse_money("$1,234.50") == 1234.5
    assert parse_money("-$22,500.00") == -22500
    assert parse_money("($5.00)") == -5
    assert parse_money("") is None and parse_money("--") is None


def test_detect(fixture_text):
    assert detect_format(fixture_text("schwab_transactions.csv")) == "schwab_csv"
    assert detect_format(fixture_text("tos_statement.csv")) == "tos_statement"
    with pytest.raises(ValueError):
        detect_format("a,b,c\n1,2,3\n")


def test_schwab_csv_parse(fixture_text):
    r = parse(fixture_text("schwab_transactions.csv"))
    assert r.account_hint == "...285"
    assert r.rows_total == 10
    assert r.skipped["Qualified Dividend"] == 1 and r.skipped["MoneyLink Transfer"] == 1
    assert len(r.records) == 8
    syms = {x.symbol for x in r.records}
    assert "SPY 2026-09-19 580P" in syms and "NVDA 2026-09-18 130C" in syms
    sto = next(x for x in r.records if x.symbol.startswith("SPY") and x.kind == "TRADE")
    assert sto.trade_date == date(2026, 9, 12)  # "as of" date wins
    assert sto.side == "SELL" and sto.position_effect == "OPEN" and sto.multiplier == 100
    assert sto.fees == pytest.approx(1.32) and sto.price == 1.25 and not sto.time_known
    exp = next(x for x in r.records if x.kind == "EXPIRATION")
    assert exp.side is None and exp.price == 0
    # oldest first
    assert r.records[0].trade_date <= r.records[-1].trade_date
    assert len({x.external_id for x in r.records}) == len(r.records)


def test_tos_parse(fixture_text):
    r = parse(fixture_text("tos_statement.csv"))
    assert len(r.records) == 7
    aapl_buy = next(x for x in r.records if x.symbol == "AAPL" and x.side == "BUY")
    assert aapl_buy.time_known and aapl_buy.executed_at.hour == 13 and aapl_buy.executed_at.minute == 41  # 09:41 EDT
    legs = [x for x in r.records if x.underlying == "QQQ"]
    assert len(legs) == 2 and legs[0].executed_at == legs[1].executed_at  # continuation row inherits time
    assert {x.symbol for x in legs} == {"QQQ 2026-09-18 480C", "QQQ 2026-09-18 490C"}
    assert r.warnings


def _acct(db):
    a = Account(name="Schwab ...285", account_number_masked="...285")
    db.add(a)
    db.flush()
    return a


def test_schwab_import_builds_trades_and_is_idempotent(db, fixture_text):
    a = _acct(db)
    recs = parse(fixture_text("schwab_transactions.csv")).records
    s1 = ingest_records(db, a.id, "schwab_csv", recs)
    assert s1.inserted == 8
    rebuild_trades(db, [a.id])
    trades = list(db.scalars(select(Trade)))
    by = {t.symbol: t for t in trades}
    assert by["AAPL"].net_pnl == pytest.approx(650 - 0.03)
    assert by["TSLA"].net_pnl == pytest.approx(-250 - 0.02)
    assert by["SPY 2026-09-19 580P"].close_reason == "EXPIRATION"
    assert by["SPY 2026-09-19 580P"].net_pnl == pytest.approx(250 - 1.32)
    assert by["NVDA 2026-09-18 130C"].net_pnl == pytest.approx(390 - 3.96)
    # re-import same file -> all duplicates
    s2 = ingest_records(db, a.id, "schwab_csv", parse(fixture_text("schwab_transactions.csv")).records)
    assert s2.inserted == 0 and s2.duplicates == 8


def test_tos_merges_into_schwab_rows_and_adds_times(db, fixture_text):
    a = _acct(db)
    ingest_records(db, a.id, "schwab_csv", parse(fixture_text("schwab_transactions.csv")).records)
    rebuild_trades(db, [a.id])
    t = db.scalar(select(Trade).where(Trade.symbol == "AAPL"))
    t.notes = "keep me"
    db.flush()
    tos = parse(fixture_text("tos_statement.csv")).records
    plan = plan_ingest(db, a.id, "tos_statement", tos)
    actions = sorted(p[0] for p in plan)
    assert actions.count("merge") == 5 and actions.count("new") == 2  # QQQ spread legs are new
    s = ingest_records(db, a.id, "tos_statement", tos)
    assert s.merged == 5 and s.inserted == 2
    rebuild_trades(db, [a.id])
    t = db.scalar(select(Trade).where(Trade.symbol == "AAPL"))
    assert t.time_known and t.notes == "keep me"
    assert t.fees == pytest.approx(0.03)  # fees kept from Schwab CSV
    assert db.scalar(select(Execution).where(Execution.symbol == "AAPL", Execution.side == "BUY")).executed_at.hour == 13
    # importing ToS again changes nothing
    s3 = ingest_records(db, a.id, "tos_statement", parse(fixture_text("tos_statement.csv")).records)
    assert s3.inserted == 0 and s3.merged == 0


def test_schwab_after_tos_fills_fees(db, fixture_text):
    a = _acct(db)
    ingest_records(db, a.id, "tos_statement", parse(fixture_text("tos_statement.csv")).records)
    s = ingest_records(db, a.id, "schwab_csv", parse(fixture_text("schwab_transactions.csv")).records)
    # 5 fills matched; 3 gained fees (2 had no fees to add), 3 rows only exist in the CSV
    assert s.merged == 3 and s.duplicates == 2 and s.inserted == 3
    rebuild_trades(db, [a.id])
    t = db.scalar(select(Trade).where(Trade.symbol == "AAPL"))
    assert t.time_known and t.fees == pytest.approx(0.03)
