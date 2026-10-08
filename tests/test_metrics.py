"""Reports metrics on a small hand-checked fixture (values worked out by hand in comments)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import metrics as M

TZ = "America/New_York"


def tr(i, net, gross, fees, opened, hold, direction="LONG", qty=10, entry=50.0, ret=None, sym="AAPL", mfe=None,
       mae=None, stop=None, risk=None, target=None, status="CLOSED", setup=None, tags=(), asset="STOCK", opt=None):
    return SimpleNamespace(
        id=i, status=status, net_pnl=net, gross_pnl=gross, fees=fees, opened_at=opened,
        closed_at=(opened + hold) if status == "CLOSED" else None, time_known=True, direction=direction,
        quantity=qty, entry_price=entry, multiplier=1.0, cost_basis=qty * entry, return_pct=ret, underlying=sym,
        symbol=sym, mfe=mfe, mae=mae, initial_stop=stop, risk_amount=risk, profit_target=target, setup=setup,
        tags=[SimpleNamespace(name=t) for t in tags], asset_type=asset, option_type=opt, fills=[])


# Entries at 14:00 UTC = 10:00 ET (EDT) Mon-Fri 2026-09-07..11; T3 at 18:00 UTC = 14:00 ET.
T1 = tr(1, 100, 101, 1, datetime(2026, 9, 7, 14), timedelta(minutes=30), ret=20, mfe=150, mae=-20,
        setup="Breakout", tags=("A",))
T2 = tr(2, -50, -49, 1, datetime(2026, 9, 8, 14), timedelta(hours=2), ret=-10, mfe=10, mae=-80, stop=45, target=60,
        tags=("A", "B"))
T3 = tr(3, 200, 202, 2, datetime(2026, 9, 9, 18), timedelta(days=3), direction="SHORT", qty=5, entry=200, ret=20,
        sym="TSLA", risk=100)
T4 = tr(4, 0, 1, 1, datetime(2026, 9, 10, 14), timedelta(minutes=10), qty=1, entry=400, ret=0, sym="MSFT")
T5 = tr(5, -150, -148, 2, datetime(2026, 9, 11, 14), timedelta(hours=1), qty=20, entry=30, ret=-25)
OPEN = tr(6, 0, 0, 1, datetime(2026, 9, 14, 14), None, status="OPEN", sym="NVDA")
ALL = [T3, T1, OPEN, T5, T2, T4]   # unsorted on purpose


def test_summary_core():
    s = M.summarize(ALL, TZ)
    assert (s["closed"], s["open"], s["wins"], s["losses"], s["be"]) == (5, 1, 2, 2, 1)
    assert s["net"] == 100 and s["gross"] == 107 and s["fees"] == 7 and s["fees_open"] == 1
    assert s["win_pct"] == 50 and s["loss_pct"] == 50 and s["be_pct"] == 20
    assert s["open_pct"] == pytest.approx(100 / 6)
    assert (s["gross_profit"], s["gross_loss"], s["profit_factor"]) == (300, -200, 1.5)
    assert (s["avg_win"], s["avg_loss"], s["pl_ratio"]) == (150, -100, 1.5)
    assert s["expectancy"] == 20 and s["median_trade"] == 0
    assert (s["largest_win"], s["largest_loss"]) == (200, -150)
    assert s["kelly"] == pytest.approx((0.5 - 0.5 / 1.5) * 100)                    # 16.67 %
    assert s["std"] == pytest.approx(14600 ** 0.5)                                  # population std 120.83
    assert s["sqn"] == pytest.approx(20 * 5 ** 0.5 / 14600 ** 0.5)                  # 0.370
    assert s["std_win"] == 50 and s["std_loss"] == 50
    # close order W, L, BE, L, W: break-even doesn't break the losing streak
    assert (s["max_consec_wins"], s["max_consec_losses"]) == (1, 2)
    assert s["avg_hold_win"] == (timedelta(minutes=30) + timedelta(days=3)) / 2
    assert s["avg_hold_loss"] == timedelta(minutes=90) and s["avg_hold_be"] == timedelta(minutes=10)
    assert s["volume"] == 46 and s["return_per_share"] == pytest.approx(100 / 46)
    assert (s["avg_ret_pct"], s["best_pct"], s["worst_pct"]) == (1, 20, -25)
    assert (s["avg_ret_pct_win"], s["avg_ret_pct_loss"]) == (20, -17.5)
    assert (s["ret_long"], s["ret_short"], s["n_long"], s["n_short"]) == (-100, 200, 4, 1)
    assert s["win_pct_long"] == pytest.approx(100 / 3) and s["win_pct_short"] == 100
    assert (s["fees_win"], s["fees_loss"], s["fees_be"], s["fees_long"], s["fees_short"]) == (3, 3, 1, 5, 2)
    assert (s["days"], s["win_days"], s["loss_days"], s["day_win_pct"], s["avg_day"]) == (5, 2, 2, 50, 20)
    assert (s["best_day"], s["worst_day"]) == (200, -150)


def test_drawdown():
    # cumulative 100, 50, 50, -100, 100 -> max DD -200 from the Sep 7 peak, recovered Sep 12
    s = M.summarize(ALL, TZ)
    assert (s["max_dd"], s["current_dd"], s["max_dd_days"], s["max_dd_trades"]) == (-200, 0, 5, 3)
    assert s["recovery_factor"] == 0.5
    dd = M.drawdown(M.closed_sorted(ALL), TZ)
    assert dd["series"]["values"] == [0, -50, -50, -200, 0]
    (e,) = dd["episodes"]
    assert (str(e["start"]), e["trough"], str(e["trough_at"]), str(e["recovered"])) == (
        "2026-09-07", -200, "2026-09-11", "2026-09-12")


def test_r_multiples_and_default_risk():
    s = M.summarize(ALL, TZ)
    assert M.r_multiple(T2) == -1 and M.r_multiple(T3) == 2       # stop 45 on entry 50 x10 = $50; Risk $100
    assert (s["r_count"], s["total_r"], s["avg_r"], s["avg_r_win"], s["avg_r_loss"]) == (2, 1, 0.5, 2, -1)
    s = M.summarize(ALL, TZ, default_risk=25)                      # the others use $25: 4R, 0R, -6R
    assert (s["r_count"], s["total_r"], s["avg_r"]) == (5, -1, -0.2)
    assert M.r_multiple(OPEN, 25) is None
    tm = M.trade_metrics(T2)
    assert (tm["risk"], tm["risk_source"], tm["r_multiple"], tm["planned_rr"], tm["target_pnl"]) == (50, "stop", -1, 2, 100)
    assert tm["left_on_table"] == 59 and tm["best_exit"] == 10 and tm["return_per_share"] == -5
    assert M.trade_metrics(T4, 40)["risk_source"] == "default"


def test_mfe_mae_efficiency():
    s = M.summarize(ALL, TZ)
    assert (s["mfe_count"], s["avg_mfe"], s["avg_mae"], s["max_mfe"], s["max_mae"]) == (2, 80, -50, 150, -80)
    assert M.mfe_efficiency(T1) == pytest.approx(100 / 150 * 100)
    assert s["mfe_eff"] == pytest.approx((100 / 150 * 100 + -50 / 10 * 100) / 2)   # per-trade average
    assert s["mae_eff"] == pytest.approx((500 + -62.5) / 2)
    assert s["mfe_eff_median"] == pytest.approx((100 / 150 * 100 + -50 / 10 * 100) / 2)
    assert s["mfe_capture"] == pytest.approx((100 - 50) / (150 + 10) * 100)   # Σ net ÷ Σ MFE
    assert s["left_on_table"] == (150 - 101) + (10 + 49)


def rows(b, key):
    return {r["label"]: (r["trades"], r["net"]) for r in b[key]}


def test_breakdowns():
    b = M.breakdowns(ALL, TZ)
    assert [r["label"] for r in b["weekday"]] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert rows(b, "hour") == {"10:00": (4, -100), "14:00": (1, 200)}
    h = next(r for r in b["hour"] if r["label"] == "10:00")
    assert h["win_pct"] == pytest.approx(100 / 3) and h["pf"] == 0.5 and h["avg_win"] == 100 and h["avg_loss"] == -100
    assert rows(b, "hold") == {"5–15 min": (1, 0), "15–60 min": (1, 100), "1–4 h": (2, -200), "1–7 days": (1, 200)}
    assert [r["label"] for r in b["hold"]] == ["5–15 min", "15–60 min", "1–4 h", "1–7 days"]
    assert rows(b, "entry_price") == {"$20–50": (1, -150), "$50–100": (2, 50), "$200–500": (2, 200)}
    assert rows(b, "size") == {"1": (1, 0), "2–5": (1, 200), "6–10": (2, 50), "11–25": (1, -150)}
    # position values: T4 $400, T1/T2 $500, T5 $600, T3 $1,000
    assert rows(b, "value") == {"$250–500": (1, 0), "$500–1k": (3, -100), "$1k–2.5k": (1, 200)}
    assert rows(b, "symbol") == {"TSLA": (1, 200), "MSFT": (1, 0), "AAPL": (3, -100)}
    assert [r["label"] for r in b["symbol"]] == ["TSLA", "MSFT", "AAPL"]        # by net P&L
    assert rows(b, "tag") == {"A": (2, 50), "B": (1, -50), "(untagged)": (3, 50)}
    assert rows(b, "setup") == {"Breakout": (1, 100), "(no setup)": (4, 0)}
    assert rows(b, "side") == {"Long": (4, -100), "Short": (1, 200)}
    assert rows(b, "status") == {"Winners": (2, 300), "Losers": (2, -200), "Break-even": (1, 0)}
    assert rows(b, "month") == {"2026-09": (5, 100)} and rows(b, "year") == {"2026": (5, 100)}
    assert rows(b, "r") == {"-1R to 0": (1, -50), "2R to 3R": (1, 200)}          # -1R falls in [-1, 0)
    assert rows(b, "pnl_dist") == {"-$250 to -$100": (1, -150), "-$50 to $0": (1, -50), "$0 to $50": (1, 0),
                                   "$100 to $250": (2, 300)}
    assert b["call_put"] == []


def test_empty_and_no_losses():
    s = M.summarize([], TZ)
    assert s["closed"] == 0 and s["profit_factor"] is None and s["sqn"] is None and s["max_dd"] == 0
    s = M.summarize([T1, T3], TZ)
    assert s["profit_factor"] == float("inf") and s["kelly"] is None and s["loss_pct"] == 0
