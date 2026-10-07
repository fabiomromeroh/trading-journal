from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.stats import calendar_months, compute


def tr(pnl, day, hours=1, direction="LONG", asset="STOCK", setup=None, tags=(), sym="AAPL"):
    o = datetime(2026, 9, day, 14, 0)
    return SimpleNamespace(id=day, status="CLOSED", opened_at=o, closed_at=o + timedelta(hours=hours),
                           net_pnl=pnl, gross_pnl=pnl + 1, fees=1.0, underlying=sym, direction=direction,
                           asset_type=asset, setup=setup, tags=[SimpleNamespace(name=t) for t in tags],
                           time_known=True)


def test_core_metrics():
    trades = [tr(100, 1), tr(-50, 2), tr(200, 3), tr(-50, 4), tr(0, 7)]
    st = compute(trades, "America/New_York")
    assert st.total_trades == 5 and st.wins == 2 and st.losses == 2 and st.scratches == 1
    assert st.net_pnl == 200 and st.gross_pnl == 205 and st.fees == 5
    assert st.win_rate == pytest.approx(50)
    assert st.profit_factor == pytest.approx(3)
    assert st.avg_win == 150 and st.avg_loss == -50
    assert st.expectancy == pytest.approx(40)
    assert st.largest_win == 200 and st.largest_loss == -50
    assert st.max_drawdown == pytest.approx(-50)
    assert st.equity[-1][1] == 200
    assert st.avg_hold == timedelta(hours=1)
    assert "Tue" in [b.label for b in st.by_weekday]  # 2026-09-01 is a Tuesday


def test_breakdowns():
    st = compute([tr(10, 1, direction="SHORT", asset="OPTION", setup="EP", tags=["A"]), tr(-5, 2, tags=["A", "B"])],
                 "America/New_York")
    assert {b.label for b in st.by_direction} == {"Short", "Long"}
    assert {b.label for b in st.by_asset} == {"Options", "Stocks"}
    assert {b.label: b.pnl for b in st.by_tag}["A"] == 5
    assert st.by_hour[0].label == "10:00"


def test_profit_factor_no_losses_and_empty():
    assert compute([tr(10, 1)], "UTC").profit_factor == float("inf")
    assert compute([], "UTC").profit_factor is None


def test_calendar():
    st = compute([tr(10, 1), tr(-5, 15)], "America/New_York")
    months = calendar_months(st.daily)
    assert months[0]["title"] == "September 2026" and months[0]["pnl"] == 5
