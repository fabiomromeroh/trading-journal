"""One trade per continuous long, listing splits, and chart-price warnings. Fixtures only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.corporate import (
    adjustment_when, audit_instruments, group_symbols, import_listing_splits, parse_yahoo_splits,
    select_listing_splits,
)
from app.models import Account, Execution, Instrument, Tag, Trade
from app.prices import chart_price_off
from app.services import rebuild_trades
from app.sources import trading212 as t212
from app.trade_builder import BuilderExec, build_trades

T0 = datetime(2023, 1, 11, 16, 43)


def _ex(i, sym, side, qty, price, when, kind="TRADE", effect=None, time_known=True):
    return BuilderExec(
        id=i, account_id=1, symbol=sym, side=side, quantity=qty, price=price,
        executed_at=when, kind=kind, position_effect=effect, time_known=time_known,
    )


def _row(**kw):
    base = dict(id=1, external_id="1", symbol="GGB", side="BUY", quantity=1, price=1,
                executed_at=T0, kind="TRADE", position_effect=None, instrument_id=None)
    base.update(kw)
    return type("R", (), base)


def test_yahoo_split_payload_reads_ratio_and_skips_cash():
    body = {"chart": {"result": [{"events": {
        "splits": {"1": {"date": 1713447000, "numerator": 6, "denominator": 5, "splitRatio": "6:5"}},
        "dividends": {"2": {"date": 1710000000, "amount": 0.05}},
    }}]}}
    got = parse_yahoo_splits(body)
    assert len(got) == 1 and got[0][1] == pytest.approx(1.2)
    assert got[0][0] == datetime.fromtimestamp(1713447000, timezone.utc).replace(tzinfo=None)


def test_ggb_splits_are_one_long_and_prices_match_the_listing():
    """Buys, the 21-for-20 and the 6-for-5, then the post-split sell. No short, prices scaled."""
    buys = [
        (datetime(2023, 1, 11, 16, 43), 7.87218, 6.14),
        (datetime(2023, 2, 7, 19, 35), 4.76269, 6.05),
        (datetime(2023, 3, 15, 15, 24), 3.52447, 4.83),
        (datetime(2023, 3, 29, 13, 30), 3.84542, 4.79),
        (datetime(2023, 6, 16, 15, 56), 2.45462, 5.34),
        (datetime(2023, 11, 29, 17, 42), 7.63369, 4.57),
    ]
    splits = [
        (datetime(2023, 3, 22, 13, 30), 1.05),
        (datetime(2024, 4, 18, 13, 30), 1.2),
    ]
    rows = [_row(id=i, external_id=str(i), side="BUY", quantity=q, price=p, executed_at=w)
            for i, (w, q, p) in enumerate(buys, start=1)]
    sell_at = datetime(2026, 1, 30, 12, 45)
    rows.append(_row(id=9, external_id="9", side="SELL", quantity=37.0812444, price=4.28, executed_at=sell_at))
    kept = select_listing_splits(rows, splits)
    assert [round(r, 4) for _, r in kept] == [1.05, 1.2]

    execs = [_ex(i, "GGB", "BUY", q, p, w) for i, (w, q, p) in enumerate(buys, start=1)]
    for n, (w, ratio) in enumerate(kept, start=20):
        execs.append(_ex(n, "GGB", "BUY", ratio, 0, w, kind="SPLIT", effect="RATIO"))
    execs.append(_ex(9, "GGB", "SELL", 37.0812444, 4.28, sell_at))
    built = build_trades(execs, long_only=True)
    assert built.orphans == []
    assert len(built.trades) == 1
    t = built.trades[0]
    assert t.direction == "LONG" and t.status == "CLOSED"
    assert t.open_quantity == pytest.approx(0)
    assert t.max_quantity == pytest.approx(37.0812444, rel=1e-6)
    # First print 6.14, divided by both splits, sits on the split-adjusted NYSE candle.
    assert t.opening_fills[0].price == pytest.approx(6.14 / 1.05 / 1.2)
    assert t.exit_price == pytest.approx(4.28)


def test_phia_scrip_is_one_long():
    buys = [
        (datetime(2023, 1, 16, 8), 2.495006, 16.04),
        (datetime(2023, 2, 8, 8), 1.640847, 16.40),
        (datetime(2023, 3, 15, 15), 1.072274, 15.08),
        (datetime(2023, 3, 29, 11), 1.109439, 15.35),
        (datetime(2023, 6, 19, 7), 0.644567, 18.648),
        (datetime(2023, 11, 30, 8), 1.123869, 18.89),
    ]
    ratio = 23.3906 / 22.3906
    when = datetime(2023, 5, 11, 7, 0)
    execs = [_ex(i, "PHIA", "BUY", q, p, w) for i, (w, q, p) in enumerate(buys, start=1)]
    execs.append(_ex(20, "PHIA", "BUY", ratio, 0, when, kind="SPLIT", effect="RATIO"))
    execs.append(_ex(21, "PHIA", "SELL", 8.3681546, 20.005, datetime(2024, 1, 30, 11, 29)))
    # A later scrip, after the position is flat, must not be applied.
    rows = [_row(id=i, external_id=str(i), symbol="PHIA", side="BUY", quantity=q, price=p, executed_at=w)
            for i, (w, q, p) in enumerate(buys, start=1)]
    rows.append(_row(id=21, external_id="21", symbol="PHIA", side="SELL", quantity=8.3681546,
                     price=20.005, executed_at=datetime(2024, 1, 30, 11, 29)))
    kept = select_listing_splits(rows, [(when, ratio), (datetime(2024, 5, 9, 7), 30.2684 / 29.2684)])
    assert len(kept) == 1 and kept[0][0] == when
    built = build_trades(execs, long_only=True)
    assert len(built.trades) == 1 and built.trades[0].direction == "LONG"
    assert built.trades[0].status == "CLOSED"
    assert built.trades[0].max_quantity == pytest.approx(8.3681546, rel=1e-5)
    assert built.orphans == []


def test_buy_adjustment_is_dated_before_the_sell_it_covers():
    sell_at = datetime(2024, 8, 13, 18, 23)
    rows = [_row(id=1, external_id="1", symbol="SOLV", side="SELL", quantity=0.968258,
                 price=58.15, executed_at=sell_at)]
    when = adjustment_when(rows, "BUY", "ADJUST", datetime(2026, 10, 11, 22, 8))
    assert when < sell_at
    built = build_trades([
        _ex(2, "SOLV", "BUY", 0.968258, 0, when, kind="ADJUST", effect="OPEN", time_known=False),
        _ex(1, "SOLV", "SELL", 0.968258, 58.15, sell_at),
    ], long_only=True)
    assert len(built.trades) == 1
    assert built.trades[0].direction == "LONG" and built.trades[0].status == "CLOSED"
    assert built.trades[0].net_pnl == pytest.approx(0.968258 * 58.15)


def test_long_only_clamps_a_sell_instead_of_opening_a_short():
    r = build_trades([
        _ex(1, "GGB", "BUY", 30, 5, T0),
        _ex(2, "GGB", "SELL", 37, 4, T0 + timedelta(days=10)),
    ], long_only=True)
    assert len(r.trades) == 1 and r.trades[0].direction == "LONG" and r.trades[0].status == "CLOSED"
    assert r.orphans and "short" in r.orphans[0].reason and r.orphans[0].quantity == pytest.approx(7)
    # A margin account still records the flip.
    both = build_trades([
        _ex(1, "GGB", "BUY", 30, 5, T0),
        _ex(2, "GGB", "SELL", 37, 4, T0 + timedelta(days=10)),
    ])
    assert any(t.direction == "SHORT" for t in both.trades)


def test_same_day_undated_buys_precede_sells():
    day = datetime(2024, 1, 30, 15, 0)
    morning = datetime(2024, 1, 30, 9, 0)
    t = build_trades([
        _ex(1, "PHIA", "SELL", 1, 20, day, time_known=False),
        _ex(2, "PHIA", "BUY", 1, 16, morning, time_known=False),
    ]).trades[0]
    assert t.direction == "LONG" and t.status == "CLOSED"
    assert t.gross_pnl == pytest.approx(4)


def test_same_isin_ticker_change_is_one_trade(db):
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="SNII_US_EQ", isin="US76655K1034", symbol="SNII",
                      exchange="US", currency="USD", price_symbol="SNII", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="1", match_key="k", symbol="RGTI", underlying="RGTI",
        asset_type="STOCK", side="BUY", quantity=4, price=10, executed_at=datetime(2025, 1, 17, 14, 30),
        currency="USD", instrument_id=inst.id, kind="TRADE"))
    db.flush()
    assert rebuild_trades(db, [acct.id]) == 1
    trade = db.scalar(select(Trade).where(Trade.account_id == acct.id))
    # The catalogue ticker SNII is not the Nasdaq line the orders used. The trade stays RGTI.
    assert trade.symbol == "RGTI" and trade.direction == "LONG" and trade.open_quantity == pytest.approx(4)
    trade.notes = "kept"
    trade.tags.append(Tag(name="momentum"))
    db.commit()
    assert rebuild_trades(db, [acct.id]) == 1
    db.expire_all()
    again = db.scalar(select(Trade).where(Trade.account_id == acct.id))
    assert again.symbol == "RGTI" and again.notes == "kept" and [t.name for t in again.tags] == ["momentum"]
    execs = list(db.scalars(select(Execution).where(Execution.account_id == acct.id)))
    grouped = group_symbols(execs, {inst.id: inst}, {})
    assert set(grouped.values()) == {"RGTI"}
    issues = {i["symbol"]: i for i in audit_instruments(db, fix=True)}
    assert inst.price_symbol == "RGTI"
    assert any("RGTI" in f for f in issues["SNII"]["fixed"])


def test_listing_splits_are_idempotent_and_keep_the_journal(db):
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="GGB_US_EQ", isin="US3737371050", symbol="GGB",
                      exchange="US", currency="USD", price_symbol="GGB", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add_all([
        Execution(account_id=acct.id, source="t212", external_id="b", match_key="b", symbol="GGB",
                  underlying="GGB", asset_type="STOCK", side="BUY", quantity=10, price=6,
                  executed_at=datetime(2023, 1, 11, 16), currency="USD", instrument_id=inst.id),
        Execution(account_id=acct.id, source="t212", external_id="s", match_key="s", symbol="GGB",
                  underlying="GGB", asset_type="STOCK", side="SELL", quantity=12, price=4,
                  executed_at=datetime(2024, 6, 1, 15), currency="USD", instrument_id=inst.id),
    ])
    db.flush()
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade))
    trade.notes, trade.setup = "thesis", "breakout"
    db.commit()
    fetch = lambda *_a, **_k: [(datetime(2023, 6, 1, 13, 30), 1.2)]
    assert import_listing_splits(db, acct.id, fetch)
    assert import_listing_splits(db, acct.id, fetch) == []
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 1
    rebuild_trades(db, [acct.id])
    db.expire_all()
    trades = list(db.scalars(select(Trade)))
    assert len(trades) == 1 and trades[0].direction == "LONG" and trades[0].status == "CLOSED"
    assert trades[0].notes == "thesis" and trades[0].setup == "breakout"
    assert trades[0].quantity == pytest.approx(12)


def test_adjustment_for_an_oversell_does_not_become_a_short(db):
    """A missing receipt stays a gap. The sell is not paired with an invented buy, and it does not open a short."""
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="SOLV_US_EQ", isin="US83444M1018", symbol="SOLV",
                      exchange="US", currency="USD", price_symbol="SOLV", asset_type="STOCK")
    db.add(inst)
    db.flush()
    sell_at = datetime(2024, 8, 13, 18, 23)
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="s", match_key="s", symbol="SOLV", underlying="SOLV",
        asset_type="STOCK", side="SELL", quantity=0.968258, price=58.15, executed_at=sell_at,
        currency="USD", instrument_id=inst.id))
    db.flush()
    src = t212.Trading212Source(sleep=lambda *_a, **_k: None)
    lines = src.apply_adjustments(db, acct, [])  # broker is flat; the receipt is missing
    assert db.scalar(select(Execution).where(Execution.external_id == "recon:SOLV")) is None
    assert not any("kept" in ln for ln in lines)
    rebuild_trades(db, [acct.id])
    assert db.scalar(select(Trade)) is None


def test_one_rescan_does_not_invent_a_split_for_a_quantity_gap(db):
    """Broker holds more shares than the fills. A re-scan must not book a Yahoo split or an estimated buy."""
    acct = Account(name="Trading 212", broker="trading212", currency="EUR", source_key="t212",
                   market="EU", kind="invest")
    db.add(acct)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="WMT_US_EQ", isin="US9311421039", symbol="WMT",
                      exchange="US", currency="USD", price_symbol="WMT", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add(Execution(
        account_id=acct.id, source="t212", external_id="b", match_key="b", symbol="WMT", underlying="WMT",
        asset_type="STOCK", side="BUY", quantity=1, price=90, executed_at=datetime(2024, 1, 31, 15),
        currency="USD", instrument_id=inst.id))
    db.flush()
    held = [{"quantity": 3, "instrument": {"ticker": "WMT_US_EQ", "isin": "US9311421039", "currency": "USD"}}]
    src = t212.Trading212Source(sleep=lambda *_a, **_k: None, split_fetch=lambda *_a, **_k: [(datetime(2024, 2, 26, 14, 30), 3.0)])
    assert src.apply_adjustments(db, acct, held) == []
    assert db.scalar(select(Execution).where(Execution.external_id == "recon:WMT")) is None
    assert db.scalar(select(func.count()).select_from(Execution).where(Execution.kind == "SPLIT")) == 0
    rebuild_trades(db, [acct.id])
    trade = db.scalar(select(Trade))
    assert trade.symbol == "WMT" and trade.status == "OPEN"
    assert trade.open_quantity == pytest.approx(1)
    gaps = src.reconcile(db, None, acct, positions=held)
    assert any(g.startswith("WMT: missing 2 sh vs Trading 212") for g in gaps)


def test_chart_price_off_flags_a_different_scale_only():
    from types import SimpleNamespace
    day = datetime(2023, 1, 11, 16, 0)
    # Daily bar time is 00:00 UTC of the New York date. 16:43 UTC on 11 Jan is still 11 Jan in New York.
    bar_time = int(datetime(2023, 1, 11, tzinfo=timezone.utc).timestamp())
    fill = SimpleNamespace(price=6.14, executed_at=day, role="OPEN",
                           execution=SimpleNamespace(kind="TRADE", time_known=True))
    trade = SimpleNamespace(fills=[fill], asset_type="STOCK", direction="LONG", status="OPEN")
    near = [{"time": bar_time, "open": 6.0, "high": 6.3, "low": 5.9, "close": 6.1}]
    assert chart_price_off(trade, near, "1D") == []
    far = [{"time": bar_time, "open": 4.7, "high": 4.9, "low": 4.6, "close": 4.8}]
    off = chart_price_off(trade, far, "1D")
    assert off and off[0]["gap"] > 0.25
    fill.execution.kind = "ADJUST"
    assert chart_price_off(trade, far, "1D") == []
