"""Neon egress cuts: memory hits, one balance rebuild, cheap page SQL, compression, backup skip.

No test here opens a network connection or a Neon URL. DATABASE_URL stays unset.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine

from app import backup, balance, currency, pagecache, prices, results
from app.config import get_settings
from app.models import Account, DailyClose, Execution, FxRate, PriceCache, Trade, TradeFill, TradeResult, utcnow
from app.sources import ibkr_flex as ibkr
from app.sources.base import SyncContext
from tests.test_ibkr import TOKEN, QUERY_ID, FakeFlex, load, nop_sleep


def _enc(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(ibkr, "_TRANSPORT", None)
    monkeypatch.setattr(ibkr._time, "sleep", lambda _n: None)
    monkeypatch.setattr(ibkr, "SEND_GAP", 0.0)


def _sql(fn):
    seen: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", before)
    try:
        out = fn()
    finally:
        event.remove(Engine, "before_cursor_execute", before)
    return out, seen


def _norm(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").replace("`", "").split())


def _trade():
    return SimpleNamespace(is_demo=False, currency="USD", instrument=None, underlying="AAPL",
                           symbol="AAPL", asset_type="STOCK")


def _bars(n=120):
    base = datetime(2024, 1, 2, tzinfo=timezone.utc)
    return [{"time": int((base + timedelta(days=i)).timestamp()), "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.0 + (i % 7) * 0.1, "volume": 1000 + i} for i in range(n)]


def test_price_lru_skips_the_row_and_legacy_json_still_reads(db, monkeypatch):
    candles = _bars(400)
    calls = {"n": 0}

    def download(*_a, **_k):
        calls["n"] += 1
        return candles

    monkeypatch.setattr(prices, "_download", download)
    prices._CANDLE_LRU.clear()
    prices._MEM.clear()
    now = datetime(2026, 6, 15, 16, 0)
    start, end = datetime(2024, 1, 1), datetime(2024, 6, 1, 23, 0)
    trade = _trade()
    assert prices._fetch(db, "yahoo", trade, "1d", start, end, now) == candles
    assert calls["n"] == 1
    row = db.scalar(select(PriceCache).where(PriceCache.cache_key.contains("AAPL")))
    assert row.payload.startswith("gz:")
    plain = json.dumps(candles, separators=(",", ":"))
    assert len(row.payload) < len(plain)

    def second():
        return prices._fetch(db, "yahoo", trade, "1d", start, end, now)

    got, sql = _sql(second)
    assert got == candles and calls["n"] == 1
    assert not any("price_cache" in _norm(s) for s in sql)

    prices._CANDLE_LRU.clear()
    legacy = [{"time": 1700000000, "close": 3.5, "open": 3.0, "high": 4.0, "low": 2.0, "volume": 1}]
    row.payload = json.dumps(legacy)
    db.commit()
    assert prices._fetch(db, "yahoo", trade, "1d", start, end, now) == legacy
    assert calls["n"] == 1

    _, sql = _sql(lambda: prices._store_recent(db, "recent|unit|AAPL", "yahoo", candles))
    assert sql and all(not s.lstrip().upper().startswith("SELECT") for s in sql)
    stored = db.scalar(select(PriceCache).where(PriceCache.cache_key == "recent|unit|AAPL"))
    assert stored.payload.startswith("gz:")


def test_balance_rebuild_reads_daily_closes_not_candle_payloads(db, monkeypatch):
    today = datetime.now(timezone.utc).date()
    start = date(today.year - 1, 6, 1)
    candles = []
    d = date(today.year - 1, 1, 1)
    i = 0
    while d <= today:
        if d.weekday() < 5:
            candles.append({"time": int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()),
                            "open": 10, "high": 11, "low": 9, "close": 10 + i * 0.01, "volume": 1})
        d += timedelta(days=1)
        i += 1
    monkeypatch.setattr(prices, "_download", lambda *_a, **_k: candles)
    prices._CANDLE_LRU.clear()
    prices._MEM.clear()
    balance._tail_seen.clear()
    trade = _trade()
    monkeypatch.setattr(prices, "_provider_order", lambda _t: [])
    _, sql = _sql(lambda: balance._yahoo_closes(db, trade, start, today))
    assert not any("price_cache" in _norm(s) or "daily_closes" in _norm(s) for s in sql)

    monkeypatch.setattr(prices, "_provider_order", lambda _t: ["yahoo"])
    first = balance._yahoo_closes(db, trade, start, today)
    assert first and db.scalar(select(func.count()).select_from(DailyClose)) > 10
    prices._CANDLE_LRU.clear()
    prices._MEM.clear()
    balance._tail_seen.clear()
    second, sql = _sql(lambda: balance._yahoo_closes(db, trade, start, today))
    assert second[max(second)] == first[max(first)]
    assert not any("price_cache" in _norm(s) for s in sql)
    assert any("daily_closes" in _norm(s) for s in sql)


def test_ibkr_sync_rebuilds_the_curve_once(db, monkeypatch):
    from app.services import set_state
    _enc(monkeypatch)
    calls = []
    monkeypatch.setattr("app.balance.rebuild", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(ibkr, "_today", lambda: date(2026, 10, 16))
    monkeypatch.setattr("app.currency.fetch_range", lambda *_a, **_k: 0)
    ibkr.save_history_start(db, date(2025, 1, 1))
    set_state(db, ibkr.FULL_STATE, "1")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    fake = FakeFlex(statement=load("multi.xml"))
    src = ibkr.IbkrFlexSource(transport=fake.transport(), sleep=nop_sleep, split_fetch=lambda *_a: [])
    src.sync(db, SyncContext())
    assert len(calls) == 1
    assert len(fake.calls) >= 4  # two windows, send + get each


def test_execute_run_does_not_rebuild_again(db, monkeypatch):
    from app.services import set_state
    _enc(monkeypatch)
    from app.sync import execute_run, start_run
    calls = []
    monkeypatch.setattr("app.balance.rebuild", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr("app.results.rebuild", lambda *_a, **_k: 0)
    monkeypatch.setattr("app.prices.prefetch_recent", lambda *_a, **_k: None)
    monkeypatch.setattr("app.stops.backfill", lambda *_a, **_k: None)
    monkeypatch.setattr(ibkr, "_today", lambda: date(2026, 10, 16))
    monkeypatch.setattr(ibkr, "_TRANSPORT", FakeFlex(statement=load("multi.xml")).transport())
    monkeypatch.setattr("app.currency.fetch_range", lambda *_a, **_k: 0)
    ibkr.save_history_start(db, date(2025, 1, 1))
    set_state(db, ibkr.FULL_STATE, "1")
    ibkr.save_credentials(db, TOKEN, QUERY_ID, "USD")
    db.commit()
    run, created = start_run(db, "manual")
    assert created
    done = execute_run(run.id, only={"ibkr_flex"})
    assert done.status == "success"
    assert calls == [1]


def _seed_page(db):
    acct = Account(name="Euro", broker="schwab", currency="EUR", is_demo=False)
    db.add(acct)
    db.flush()
    opened = datetime(2026, 3, 2, 15, 0)
    ex = Execution(account_id=acct.id, source="schwab_csv", external_id="e1", match_key="m1", symbol="AAA",
                   underlying="AAA", asset_type="STOCK", multiplier=1, quantity=10, price=20, fees=1,
                   executed_at=opened, time_known=True, seq=0, kind="TRADE", currency="EUR",
                   raw="{" + '"note":"' + ("x" * 4000) + '"}', created_at=utcnow())
    db.add(ex)
    db.flush()
    t = Trade(key="k-aaa", account_id=acct.id, symbol="AAA", underlying="AAA", asset_type="STOCK",
              direction="LONG", status="CLOSED", opened_at=opened, closed_at=opened + timedelta(hours=2),
              quantity=10, open_quantity=0, entry_price=20, exit_price=21, gross_pnl=10, fees=1, net_pnl=9,
              multiplier=1, currency="EUR", time_known=True)
    db.add(t)
    db.flush()
    db.add(TradeFill(trade_id=t.id, execution_id=ex.id, position=0, side="BUY", role="OPEN", quantity=10,
                     price=20, fees=1, executed_at=opened))
    db.add(TradeFill(trade_id=t.id, execution_id=ex.id, position=1, side="SELL", role="CLOSE", quantity=10,
                     price=21, fees=0, executed_at=opened + timedelta(hours=2)))
    db.commit()
    results.rebuild(db)
    results._MEM.clear()
    return t


def test_page_views_skip_execution_bodies_and_call_trades_once(db, monkeypatch):
    monkeypatch.setenv("PAGE_CACHE", "off")
    _seed_page(db)
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    real = results.trades
    for path in ("/", "/trades", "/reports"):
        calls = {"n": 0}

        def wrapped(db_, stmt, display=None, _real=real, _calls=calls):
            _calls["n"] += 1
            return _real(db_, stmt, display)

        monkeypatch.setattr(results, "trades", wrapped)

        def hit():
            r = c.get(path)
            assert r.status_code == 200
            return r

        _, sql = _sql(hit)
        blob = "\n".join(_norm(s) for s in sql)
        for col in ("executions.raw", "executions.price", "executions.description", "trade_fills.price"):
            assert col not in blob, col
        assert calls["n"] == 1, path
    row = db.scalar(select(TradeResult.payload).where(TradeResult.currency == "NATIVE"))
    assert row.startswith("gz:")
    loaded = results._load(row)
    assert loaded["t"]["symbol"] == "AAA"
    assert results._load('{"t": {"symbol": "OLD"}}')["t"]["symbol"] == "OLD"


def test_rate_book_reuses_memory_for_the_same_stamp(db):
    db.add(FxRate(day=date(2026, 1, 5), quote="USD", rate=1.2))
    db.commit()
    currency._fx_mem.clear()
    book = currency.RateBook(db)
    assert book.per_eur("USD", date(2026, 1, 6)) == 1.2

    def again():
        return currency.RateBook(db).per_eur("USD", date(2026, 1, 6))

    got, sql = _sql(again)
    assert got == 1.2
    blob = "\n".join(_norm(s) for s in sql)
    assert "fx_rates.rate" not in blob
    assert "max(" in blob


def test_export_omits_derived_tables_and_skips_when_unchanged(db):
    from app.main import create_app
    from app.models import BalancePoint
    acct = Account(name="A", broker="schwab", currency="USD")
    db.add(acct)
    db.flush()
    t = Trade(key="k", account_id=acct.id, symbol="AAA", underlying="AAA", asset_type="STOCK", direction="LONG",
              status="CLOSED", opened_at=datetime(2026, 1, 2, 15), closed_at=datetime(2026, 1, 2, 16),
              quantity=1, entry_price=1, net_pnl=1, gross_pnl=1, fees=0, multiplier=1, time_known=True)
    db.add(t)
    db.flush()
    db.add(PriceCache(cache_key="k", provider="yahoo", payload="[]"))
    db.add(FxRate(day=date(2026, 1, 2), quote="USD", rate=1.1))
    db.add(DailyClose(symbol="AAA", day=date(2026, 1, 2), close=1.5, provider="yahoo"))
    db.add(BalancePoint(account_id=acct.id, currency="USD", day=date(2026, 1, 2), net_deposits=0, realized_cum=1,
                        dividends_cum=0, other_cum=0, unrealized=0, balance=1))
    db.add(TradeResult(trade_id=t.id, currency="NATIVE", sig="s", payload="{}"))
    db.commit()
    backup.reset_rate_limit()
    token = backup.regenerate_token(db)
    c = TestClient(create_app())
    h = {"Authorization": f"Bearer {token}"}
    first = c.get("/api/backup/export", headers=h)
    assert first.status_code == 200 and first.headers["content-type"].startswith("application/gzip")
    import gzip
    data = json.loads(gzip.decompress(first.content))
    for name in ("price_cache", "fx_rates", "balance_points", "trade_results", "daily_closes"):
        assert data["tables"][name]["omitted"] == "cache", name
    assert data["tables"]["trades"][0]["symbol"] == "AAA"
    backup.reset_rate_limit()
    second = c.get("/api/backup/export", headers=h)
    assert second.status_code == 200 and second.headers["content-type"].startswith("application/json")
    body = second.json()
    assert body["unchanged"] is True and body["stamp"] and len(second.content) < 500
    t.notes = "note-after-stamp"
    db.commit()
    backup.reset_rate_limit()
    third = c.get("/api/backup/export", headers=h)
    assert third.headers["content-type"].startswith("application/gzip")
    text = gzip.decompress(third.content).decode()
    assert "note-after-stamp" in text


def test_prewarm_is_two_pages_and_only_while_active(db):
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    for path in ("/trades", "/trades?status=OPEN", "/trades?status=CLOSED", "/reports"):
        assert c.get(path).status_code == 200
    pagecache.bump()
    pagecache._prewarm()
    assert c.get("/reports").headers.get("x-page-cache") == "hit"
    assert c.get("/trades?status=CLOSED").headers.get("x-page-cache") == "hit"
    assert c.get("/trades").headers.get("x-page-cache") == "miss"
    pagecache._USER["seen"] = 0.0
    pagecache._CACHE.clear()
    pagecache._prewarm()
    assert c.get("/reports").headers.get("x-page-cache") != "hit"


def test_migration_0015_keeps_old_execution_and_downgrades(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from app.migrate import ROOT
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "0014")
    eng = create_engine(url)
    with eng.begin() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(executions)"))]
        assert "updated_at" not in cols
        assert "daily_closes" not in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        c.execute(text("INSERT INTO accounts (name, broker, is_demo, created_at) VALUES ('A','schwab',0,'2026-01-01')"))
        c.execute(text(
            "INSERT INTO executions (account_id, source, external_id, match_key, symbol, underlying, asset_type, "
            "multiplier, quantity, price, fees, executed_at, time_known, seq, kind, created_at) "
            "VALUES (1,'schwab_csv','e1','m1','AAA','AAA','STOCK',1,1,10,0,'2026-01-02',1,0,'TRADE','2026-01-02')"))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        row = c.execute(text("SELECT external_id, updated_at FROM executions")).one()
        assert row[0] == "e1" and row[1] is None
        fills = [r[1] for r in c.execute(text("PRAGMA table_info(trade_fills)"))]
        assert "updated_at" in fills
        assert c.execute(text("SELECT count(*) FROM daily_closes")).scalar() == 0
    command.downgrade(cfg, "0014")
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(executions)"))]
        assert "updated_at" not in cols
        names = c.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        assert "daily_closes" not in names
        assert c.execute(text("SELECT symbol FROM executions")).scalar() == "AAA"
