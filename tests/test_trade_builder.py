from datetime import datetime, timedelta

import pytest

from app.trade_builder import BuilderExec, build_trades

T0 = datetime(2026, 9, 14, 14, 0)
_id = iter(range(1, 10_000))


def ex(side, qty, price, minutes=0, sym="AAPL", fees=0.0, mult=1.0, effect=None, kind="TRADE",
       acct=1, time_known=True, seq=0, at=None):
    return BuilderExec(id=next(_id), account_id=acct, symbol=sym, side=side, quantity=qty, price=price,
                       executed_at=at or T0 + timedelta(minutes=minutes), fees=fees, multiplier=mult,
                       position_effect=effect, kind=kind, time_known=time_known, seq=seq)


def one(execs, **kw):
    r = build_trades(execs, **kw)
    assert len(r.trades) == 1, r.trades
    return r.trades[0]


def test_simple_long_round_trip():
    t = one([ex("BUY", 100, 10), ex("SELL", 100, 11, 30, fees=1.0)])
    assert t.direction == "LONG" and t.status == "CLOSED"
    assert t.gross_pnl == pytest.approx(100)
    assert t.fees == pytest.approx(1.0)
    assert t.net_pnl == pytest.approx(99)
    assert t.entry_price == 10 and t.exit_price == 11
    assert t.max_quantity == 100
    assert t.closed_at - t.opened_at == timedelta(minutes=30)
    assert t.return_pct == pytest.approx(99 / 1000 * 100)


def test_simple_short_round_trip():
    t = one([ex("SELL", 50, 20), ex("BUY", 50, 18, 10)])
    assert t.direction == "SHORT"
    assert t.gross_pnl == pytest.approx(100)


def test_losing_short():
    t = one([ex("SELL", 10, 20), ex("BUY", 10, 25, 10)])
    assert t.gross_pnl == pytest.approx(-50)


def test_scale_in_and_out_long_fifo():
    t = one([ex("BUY", 100, 10), ex("BUY", 100, 12, 5), ex("SELL", 50, 15, 10), ex("SELL", 150, 11, 20)])
    # FIFO: 50@10 sold at 15 (+250); then 50@10 at 11 (+50) and 100@12 at 11 (-100)
    assert t.gross_pnl == pytest.approx(200)
    assert t.max_quantity == 200
    assert t.entry_price == pytest.approx(11)
    assert t.exit_price == pytest.approx((50 * 15 + 150 * 11) / 200)
    assert len(t.opening_fills) == 2 and len(t.closing_fills) == 2


def test_partial_close_leaves_open_trade_with_realized_fifo_pnl():
    r = build_trades([ex("BUY", 100, 10), ex("BUY", 100, 20, 5), ex("SELL", 100, 15, 10)])
    t = r.trades[0]
    assert t.status == "OPEN"
    assert t.open_quantity == 100
    assert t.gross_pnl == pytest.approx(500)  # FIFO matches the 10.00 lot
    assert t.closed_at is None


def test_flip_long_to_short_splits_execution_and_fees():
    r = build_trades([ex("BUY", 100, 10), ex("SELL", 150, 12, 10, fees=3.0), ex("BUY", 50, 11, 20)])
    assert len(r.trades) == 2
    long_t, short_t = r.trades
    assert long_t.direction == "LONG" and long_t.status == "CLOSED"
    assert long_t.gross_pnl == pytest.approx(200)
    assert long_t.fees == pytest.approx(2.0)
    assert short_t.direction == "SHORT" and short_t.status == "CLOSED"
    assert short_t.gross_pnl == pytest.approx(50)
    assert short_t.fees == pytest.approx(1.0)
    assert short_t.key.endswith(":flip")
    assert short_t.opening_fills[0].quantity == 50


def test_multiple_round_trips_same_symbol():
    r = build_trades([ex("BUY", 10, 10), ex("SELL", 10, 11, 1), ex("BUY", 10, 12, 2), ex("SELL", 10, 11, 3)])
    assert [t.gross_pnl for t in r.trades] == [pytest.approx(10), pytest.approx(-10)]
    assert all(t.status == "CLOSED" for t in r.trades)
    assert r.trades[0].key != r.trades[1].key


def test_symbols_and_accounts_are_independent():
    r = build_trades([ex("BUY", 10, 10, sym="A"), ex("BUY", 5, 50, sym="B"), ex("SELL", 10, 12, 5, sym="A"),
                      ex("BUY", 10, 10, acct=2, sym="A")])
    by = {(t.account_id, t.symbol): t for t in r.trades}
    assert by[(1, "A")].status == "CLOSED"
    assert by[(1, "B")].status == "OPEN"
    assert by[(2, "A")].status == "OPEN"


def test_option_multiplier_and_fees():
    t = one([ex("BUY", 2, 1.50, sym="SPY C", mult=100, fees=1.32, effect="OPEN"),
             ex("SELL", 2, 2.25, 60, sym="SPY C", mult=100, fees=1.32, effect="CLOSE")])
    assert t.gross_pnl == pytest.approx(150)
    assert t.fees == pytest.approx(2.64)
    assert t.cost_basis == pytest.approx(300)
    assert t.net_pnl == pytest.approx(147.36)


def test_short_option_expires_worthless():
    t = one([ex("SELL", 2, 1.25, sym="P", mult=100, fees=1.32, effect="OPEN"),
             ex(None, 2, 0.0, 600, sym="P", mult=100, effect="CLOSE", kind="EXPIRATION")])
    assert t.direction == "SHORT"
    assert t.status == "CLOSED" and t.close_reason == "EXPIRATION"
    assert t.gross_pnl == pytest.approx(250)
    assert t.closing_fills[0].side == "BUY"


def test_long_option_expires_worthless():
    t = one([ex("BUY", 1, 0.80, sym="C", mult=100), ex(None, 1, 0, 600, sym="C", mult=100, kind="EXPIRATION")])
    assert t.gross_pnl == pytest.approx(-80)
    assert t.closing_fills[0].side == "SELL"


def test_assignment_closes_option_at_zero():
    t = one([ex("SELL", 1, 2.00, sym="P", mult=100), ex(None, 1, 0, 600, sym="P", mult=100, kind="ASSIGNMENT")])
    assert t.close_reason == "ASSIGNMENT"
    assert t.gross_pnl == pytest.approx(200)


def test_expiration_without_position_is_orphan():
    r = build_trades([ex(None, 1, 0, sym="X", kind="EXPIRATION")])
    assert not r.trades and len(r.orphans) == 1


def test_expiration_quantity_larger_than_position_is_capped():
    r = build_trades([ex("BUY", 1, 1.0, sym="C", mult=100), ex(None, 3, 0, 5, sym="C", mult=100, kind="EXPIRATION")])
    assert r.trades[0].status == "CLOSED"
    assert len(r.orphans) == 1 and r.orphans[0].quantity == 2


def test_closing_fill_without_open_position_is_orphan_not_short():
    r = build_trades([ex("SELL", 100, 10, effect="CLOSE"), ex("BUY", 10, 5, 5)])
    assert len(r.orphans) == 1
    assert len(r.trades) == 1 and r.trades[0].direction == "LONG"


def test_ambiguous_sell_without_position_becomes_short():
    t = one([ex("SELL", 100, 10), ex("BUY", 100, 9, 5)])
    assert t.direction == "SHORT"


def test_date_only_rows_open_before_close_on_same_day():
    day = datetime(2026, 9, 11, 20, 0)
    # File order newest-first gave the SELL a lower seq; ranking must still open first.
    t = one([ex("SELL", 50, 240, effect="CLOSE", time_known=False, seq=0, at=day),
             ex("BUY", 50, 245, time_known=False, seq=1, at=day)])
    assert t.direction == "LONG" and t.gross_pnl == pytest.approx(-250)
    assert t.time_known is False


def test_same_timestamp_uses_seq():
    t = one([ex("BUY", 10, 10, seq=1, at=T0), ex("SELL", 10, 12, seq=2, at=T0)])
    assert t.gross_pnl == pytest.approx(20)


def test_inferred_expiration_for_sources_without_expiry_rows():
    exp_at = datetime(2026, 9, 18, 20, 0)
    r = build_trades([ex("BUY", 2, 1.0, sym="Q", mult=100)], expirations={"Q": exp_at},
                     as_of=exp_at + timedelta(days=1))
    t = r.trades[0]
    assert t.status == "CLOSED" and t.close_reason == "EXPIRATION"
    assert t.gross_pnl == pytest.approx(-200)
    assert t.closing_fills[0].execution_id is None


def test_inferred_expiration_respects_partial_close_fifo():
    exp_at = datetime(2026, 9, 18, 20, 0)
    r = build_trades([ex("SELL", 2, 1.0, sym="Q", mult=100), ex("SELL", 1, 2.0, 1, sym="Q", mult=100),
                      ex("BUY", 2, 0.5, 2, sym="Q", mult=100)], expirations={"Q": exp_at},
                     as_of=exp_at + timedelta(hours=1))
    t = r.trades[0]
    # closed 2 @0.5 against the 1.00 lot (+100), remaining 1 @2.00 expires (+200)
    assert t.gross_pnl == pytest.approx(300)
    assert t.status == "CLOSED"


def test_not_expired_yet_stays_open():
    exp_at = datetime(2026, 9, 18, 20, 0)
    r = build_trades([ex("BUY", 1, 1.0, sym="Q", mult=100)], expirations={"Q": exp_at},
                     as_of=exp_at - timedelta(days=1))
    assert r.trades[0].status == "OPEN"


def test_fractional_shares():
    t = one([ex("BUY", 0.5, 100), ex("SELL", 0.5, 110, 5)])
    assert t.gross_pnl == pytest.approx(5)


def test_zero_quantity_ignored():
    r = build_trades([ex("BUY", 0, 10)])
    assert not r.trades and not r.orphans


def test_fees_on_scale_in_accumulate():
    t = one([ex("BUY", 10, 10, fees=1), ex("BUY", 10, 10, 1, fees=1), ex("SELL", 20, 10, 2, fees=2)])
    assert t.fees == pytest.approx(4) and t.net_pnl == pytest.approx(-4)
    assert t.return_pct == pytest.approx(-4 / 200 * 100)


def test_keys_are_stable_across_rebuilds():
    execs = [ex("BUY", 10, 10), ex("SELL", 10, 11, 1)]
    assert build_trades(execs).trades[0].key == build_trades(list(reversed(execs))).trades[0].key
