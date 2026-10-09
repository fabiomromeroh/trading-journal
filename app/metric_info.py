"""One catalog of metric descriptions + how each is calculated. Used by the (i) tooltips on the
dashboard widgets, the Reports stat cards and the trade page stat bar, and by the widget catalog.

Conventions used everywhere:
* Realized P&L = closing fills' (exit - FIFO entry) x qty x multiplier, minus fees, on the fill's day;
  includes partial exits of trades that are still open. Total realized = closed trades + realized
  part of open trades.
* Trade statistics (win rate, expectancy, profit factor, averages, streaks) use CLOSED trades only:
  an open trade has no final result yet.
"""
from __future__ import annotations

REALIZED_RULE = ("Realized on a day = for every closing fill that day, (exit − FIFO entry price) × qty × multiplier "
                 "(reversed for shorts), minus the fees charged that day. Partial exits of trades that are still "
                 "open count on the day you sold. Days are New York dates.")

I = {
    # ---------------------------------------------------------------- P&L
    "realized": ("Total realized P&L", "Everything you've locked in: closed trades plus partial profit-taking/stop-outs inside trades that are still open, after fees.",
                 "Σ realized of all closing fills in view − fees = closed trades' net + realized part of open trades. Equals the sum of the daily P&L."),
    "closed_net": ("Closed trades net P&L", "Net P&L of fully closed trades only (what trade statistics use). Excludes partial exits of trades still open.",
                   "Σ net P&L of closed trades (gross − fees)."),
    "unrealized": ("Unrealized (open)", "Paper P&L of the shares/contracts you still hold, at the latest price.",
                   "Σ open qty × (latest price − FIFO avg cost of the remaining lots) × multiplier (reversed for shorts). Prices: Yahoo last trade incl. pre/after-hours, refreshed on page load (≤ 2 min old); SnapTrade price as fallback."),
    "total_pnl": ("Total P&L", "Realized + unrealized, checked against the broker account value.",
                  "Total realized + unrealized. Account check: account value − net deposits (SnapTrade)."),
    "gross_net": ("Gross vs net P&L", "P&L before and after commissions/fees for closed trades.", "Gross = Σ (exit − entry) × qty × mult; net = gross − fees."),
    "fees": ("Commissions & fees", "Total commissions and regulatory fees on closed trades.", "Σ fees of closed trades; open trades' fees shown separately."),
    "long_short": ("Long vs short", "Net P&L of closed long trades vs closed short trades.", "Σ net P&L by direction (closed trades)."),
    # ---------------------------------------------------------------- win/loss
    "win_rate": ("Win rate", "Share of closed trades that made money. Break-even trades are left out.", "wins ÷ (wins + losses) × 100, closed trades."),
    "profit_factor": ("Profit factor", "Money won per dollar lost. Above 1 = profitable.", "Σ net of winning trades ÷ |Σ net of losing trades| (closed trades)."),
    "expectancy": ("Expectancy", "Average result per closed trade.", "Σ net P&L of closed trades ÷ number of closed trades."),
    "avg_win_loss": ("Avg win / avg loss", "Average winning and losing closed trade, and their ratio.", "Σ wins ÷ #wins, Σ losses ÷ #losses; ratio = avg win ÷ |avg loss|."),
    "pl_ratio": ("Profit/loss ratio", "How big the average win is versus the average loss.", "avg win ÷ |avg loss| (closed trades)."),
    "win_loss_be_pct": ("Win % / loss % / BE %", "Split of closed trades by outcome.", "Win % and loss % over decided trades; BE % over all closed trades."),
    "median_trade": ("Median trade", "Middle closed-trade result (not skewed by outliers).", "Median of closed trades' net P&L."),
    "largest_win": ("Largest win", "Best single closed trade.", "max net P&L over closed trades."),
    "largest_loss": ("Largest loss", "Worst single closed trade.", "min net P&L over closed trades."),
    "largest": ("Largest win / loss", "Best and worst single closed trade.", "max / min net P&L over closed trades."),
    "streaks": ("Max consecutive wins / losses", "Longest run of winning and losing closed trades (in exit order).", "Break-even trades don't break a streak."),
    # ---------------------------------------------------------------- activity
    "total_trades": ("Trades", "Count of trades in view. Closed trades have a final result; open trades are still running.",
                     "closed + open = all trades. Trade statistics use the closed ones."),
    "open_trades": ("Open trades", "Trades still holding a position, and what they've already realized via partial exits.", "count of open trades; Σ net P&L of open trades."),
    "avg_hold": ("Avg hold time", "Average time from first entry to final exit of closed trades.", "mean(closed_at − opened_at); trades without times count by date."),
    "hold_win_loss": ("Hold: winners vs losers", "Average holding time of winning vs losing closed trades.", "mean(closed_at − opened_at) per outcome."),
    "avg_size": ("Avg position size", "Average maximum shares/contracts held per closed trade.", "Σ max size ÷ closed trades."),
    # ---------------------------------------------------------------- returns
    "avg_return_pct": ("Avg return %", "Average % return per closed trade.", "mean(net P&L ÷ cost of entries × 100)."),
    "best_worst_pct": ("Biggest % profit / loser", "Best and worst % return of a closed trade.", "max / min return %."),
    "return_per_share": ("Return per share", "Net P&L per share (or contract unit) traded.", "Σ net ÷ Σ (size × multiplier), closed trades."),
    # ---------------------------------------------------------------- risk
    "max_drawdown": ("Max drawdown", "Biggest peak-to-trough fall of cumulative closed-trade P&L.", "min over time of (cumulative net − running peak), closed trades in exit order."),
    "current_dd": ("Current drawdown", "How far cumulative closed-trade P&L is below its peak now.", "cumulative net − peak."),
    "dd_duration": ("Longest drawdown", "Longest time spent below a previous equity peak.", "calendar days from peak to recovery (or to now)."),
    "recovery_factor": ("Recovery factor", "Net profit relative to the worst drawdown.", "closed net P&L ÷ |max drawdown|."),
    "std_dev": ("P&L standard deviation", "How spread out closed-trade results are.", "population std dev of closed trades' net P&L."),
    "sqn": ("System Quality Number", "Van Tharp's SQN: consistency of results. ≥ 2.5 good, needs many trades.", "avg net P&L × √n ÷ std dev (closed trades)."),
    "kelly": ("Kelly %", "Theoretical optimal fraction of capital to risk per trade.", "W − (1 − W) ÷ (avg win ÷ |avg loss|), W = win rate."),
    # ---------------------------------------------------------------- days
    "best_worst_day": ("Best / worst day", "Best and worst day by realized P&L (incl. partial exits).", REALIZED_RULE),
    "day_stats": ("Winning days %", "Share of trading days with positive realized P&L.", "green days ÷ (green + red days); " + REALIZED_RULE),
    "avg_day": ("Avg daily P&L", "Average realized P&L per trading day.", "Σ daily realized ÷ number of days with realized P&L."),
    "trading_days": ("Trading days", "Days with realized P&L (a closing fill or fees).", REALIZED_RULE),
    # ---------------------------------------------------------------- R & excursions
    "avg_r": ("Avg R-multiple", "Average result in units of planned risk.", "mean(net P&L ÷ risk $); risk = Risk $, else |entry − stop| × size, else default risk."),
    "total_r": ("Total R", "Sum of R-multiples of closed trades with a risk.", "Σ net ÷ risk."),
    "mfe_eff": ("MFE capture", "Share of the best open-trade run you kept.", "Σ net P&L ÷ Σ MFE over closed trades with MFE."),
    "avg_mfe_mae": ("Avg MFE / MAE", "Average best and worst running P&L while trades were open.", "mean MFE, mean MAE (closed trades measured from price bars)."),
    "left_on_table": ("Left on the table", "Profit you could have taken at the best price but didn't.", "Σ (MFE − gross P&L)."),
    # ---------------------------------------------------------------- charts
    "chart_equity": ("Equity curve", "Cumulative realized P&L by day (incl. partial exits). Hover for the date, the running total and that day's P&L.", "running Σ of daily realized P&L."),
    "chart_winloss": ("Win / loss donut", "Closed trades by outcome.", "count of winning, losing and break-even closed trades."),
    "chart_daily": ("Daily realized P&L", "Realized P&L per day; use the arrows to move through time.", REALIZED_RULE),
    "calendar": ("P&L calendar", "Realized P&L per day for one month, with weekly totals. Arrows move months; click a day to see its trades.", REALIZED_RULE),
    "chart_symbol": ("P&L by symbol", "Net P&L of closed trades per underlying symbol (top 12).", "Σ net per symbol."),
    "chart_weekday": ("P&L by day of week", "Closed trades' net P&L by the weekday they were entered.", "Σ net grouped by entry weekday (NY time)."),
    "chart_hour": ("P&L by hour", "Closed trades' net P&L by entry hour (needs execution times).", "Σ net grouped by entry hour (NY time)."),
    "chart_hold": ("P&L by holding time", "Closed trades' net P&L by how long they were held.", "Σ net grouped by hold-time bucket."),
    "chart_cum_gross": ("Cumulative net vs gross", "Running realized P&L by day before and after fees.", "running Σ of daily realized gross and net."),
    "chart_drawdown": ("Drawdown (underwater)", "Distance of cumulative closed-trade P&L below its peak after each trade.", "cumulative net − running peak."),
    "chart_month": ("P&L by month", "Closed trades' net P&L by exit month.", "Σ net grouped by exit month."),
    "chart_price": ("P&L by entry price", "Closed trades' net P&L by average entry price.", "Σ net per price bucket."),
    "chart_size": ("P&L by position size", "Closed trades' net P&L by max size.", "Σ net per size bucket."),
    "chart_pnl_dist": ("Trade P&L distribution", "How many closed trades fell in each P&L range.", "count per net-P&L bucket."),
    "tbl_direction": ("Long vs short table", "Closed trades split by direction.", "count, win % and Σ net per direction."),
    "tbl_asset": ("Stocks vs options table", "Closed trades split by instrument.", "count, win % and Σ net per asset type."),
    "tbl_setup": ("By setup table", "Closed trades by the setup you journaled.", "count, win % and Σ net per setup."),
    "tbl_tag": ("By tag table", "Closed trades by tag (a trade counts once per tag).", "count, win % and Σ net per tag."),
    "recent": ("Recent closed trades", "The latest fully closed trades.", "sorted by exit time."),
    "positions": ("Open positions", "Line-by-line open P/L to compare with thinkorswim's Position Statement.",
                  "open qty × (last − FIFO avg cost of remaining lots). thinkorswim's P/L Open uses the mark (bid/ask mid) and its own cost basis setting, so small differences are normal."),
    # ---------------------------------------------------------------- trade page
    "t_net": ("Net P&L", "This trade's realized P&L after fees (for an open trade: what partial exits realized so far).", "gross − fees."),
    "t_gross": ("Gross P&L", "Realized P&L before fees.", "Σ over closing fills of (exit − FIFO entry) × qty × multiplier."),
    "t_fees": ("Fees", "Commissions and regulatory fees on all fills.", "Σ fill fees."),
    "t_return": ("Return %", "Net P&L relative to the cost of the entries.", "net ÷ (Σ entry price × qty × multiplier) × 100."),
    "t_entry_exit": ("Entry / exit", "Average entry and exit price.", "qty-weighted average of opening and closing fills."),
    "t_max_size": ("Max size", "Largest position held during the trade.", "max |running position|."),
    "t_hold": ("Hold time", "First entry to final exit.", "closed_at − opened_at."),
    "t_mfe_mae": ("MFE / MAE", "Best and worst running P&L while the trade was open.", "realized so far + open qty marked at each bar's high/low; finest bars available."),
    "t_r_multiple": ("R-multiple", "Result in units of planned risk.", "net ÷ risk $."),
    "t_mfe_eff": ("MFE efficiency", "Share of the best run you kept.", "net ÷ MFE."),
    "t_mae_eff": ("MAE efficiency", "Net P&L relative to the worst drawdown.", "net ÷ |MAE|."),
    "t_left_on_table": ("Left on the table", "Gross profit missed vs the best price.", "MFE − gross P&L."),
    "t_planned_rr": ("Planned R:R", "Reward-to-risk of your plan.", "|target − entry| ÷ |entry − stop|."),
    "t_return_per_share": ("Return / share", "Net P&L per share or contract unit.", "net ÷ (max size × multiplier)."),
    "t_entry": ("Avg entry", "Average price of the opening fills.", "Σ price × qty ÷ Σ qty (opening fills)."),
    "t_exit": ("Avg exit", "Average price of the closing fills.", "Σ price × qty ÷ Σ qty (closing fills)."),
    "t_mfe": ("MFE", "Best running P&L while the trade was open.", "realized so far + open qty marked at each bar's high/low."),
    "t_mae": ("MAE", "Worst running P&L while the trade was open.", "realized so far + open qty marked at each bar's high/low."),
    "t_best_exit": ("Best exit possible", "P&L at the best price between entry and exit (= MFE).", "MFE."),
    "t_risk": ("Risk $", "Planned dollar risk of the trade.", "Risk $ field, else |entry − stop| × size × multiplier, else the default risk."),
    "t_stop": ("Initial stop", "Stop price you planned at entry (journal field).", "entered on the Risk & target form."),
    "t_target": ("Profit target", "Target price you planned (journal field).", "entered on the Risk & target form."),
    "t_target_pnl": ("Profit aim $", "P&L if the target had been hit with the full size.", "(target − entry) × size × multiplier."),
    "t_position_value": ("Position value", "Cost of the entries.", "Σ entry price × qty × multiplier."),
    "t_fills": ("Executions", "Number of fills in the trade.", "count of opening + closing fills."),
    "t_opened": ("Opened", "Time of the first entry (New York time).", "first opening fill."),
    "t_closed": ("Closed", "Time of the final exit (New York time).", "last closing fill."),
    "realized_view": ("Total realized in view", "Realized P&L of the trades listed (all pages), including partial exits of trades still open, after fees.",
                      "Σ net P&L of closed trades + Σ realized part of open trades matching the filters. Date filters select trades by open date here; the dashboard/calendar attribute P&L to the day of each exit fill."),
    # ---------------------------------------------------------------- reports-only
    "be_trades": ("Break-even trades", "Closed trades whose net P&L is within ±$0.50 of zero.", "count of closed trades with |net| ≤ $0.50; fees paid on them shown below."),
    "fees_long_short": ("Fees long / short", "Fees paid on closed long vs closed short trades.", "Σ fees by direction (closed trades)."),
    "fees_open": ("Fees on open trades", "Commissions already paid on trades that are still open.", "Σ fees of open trades (counted in total realized on the day charged)."),
    "mfe_coverage": ("MFE/MAE coverage", "How many closed trades have MFE/MAE measured from price bars.", "closed trades with MFE ÷ closed trades."),
    "avg_mfe": ("Avg MFE", "Average best running P&L of closed trades.", "Σ MFE ÷ trades measured."),
    "avg_mae": ("Avg MAE", "Average worst running P&L of closed trades.", "Σ MAE ÷ trades measured."),
    "mfe_eff_median": ("MFE efficiency", "Typical share of the best run you kept per trade.", "median of (net ÷ MFE) per trade."),
    "mae_eff_median": ("MAE efficiency", "Typical net P&L relative to the worst drawdown per trade.", "median of (net ÷ |MAE|) per trade."),
    "expectancy_r": ("Expectancy (R)", "Average R-multiple per closed trade with a risk set.", "Σ R ÷ trades with risk; R = net ÷ risk $."),
    "t_account": ("Account", "Brokerage account of the trade.", "from the import/sync."),
    "t_setup": ("Setup", "Setup you journaled for this trade.", "journal field."),
    "t_rating": ("Rating", "Your 1–5 star rating.", "journal field."),
}

# Reports-only labels that map to the same definitions
ALIASES = {"win_pct": "win_rate", "loss_be_pct": "win_loss_be_pct", "max_dd": "max_drawdown", "mfe_capture": "mfe_eff",
           "avg_hold_trade": "avg_hold", "closed_trades": "total_trades", "net": "closed_net"}


# Reports stat-card labels → catalog ids
LABELS = {
    "Net P&L": "closed_net", "Total realized P&L": "realized", "Closed trades net P&L": "closed_net",
    "Trades": "total_trades", "Win %": "win_rate", "Loss % / BE %": "win_loss_be_pct", "Profit factor": "profit_factor",
    "Expectancy": "expectancy", "Avg win / avg loss": "avg_win_loss", "Median trade": "median_trade",
    "Largest win / loss": "largest", "Biggest % profit / loser": "best_worst_pct", "Avg return %": "avg_return_pct",
    "Return per share": "return_per_share", "Long / short": "long_short", "Avg hold": "avg_hold",
    "Max consecutive": "streaks", "Max drawdown": "max_drawdown", "P&L std dev": "std_dev", "SQN": "sqn",
    "Kelly %": "kelly", "Trading days": "trading_days", "Avg daily P&L": "avg_day", "Avg R-multiple": "avg_r",
    "MFE capture": "mfe_eff", "Commissions & fees": "fees", "Expectancy (R)": "expectancy_r", "P/L ratio": "pl_ratio",
    "Total R": "total_r", "BE trades": "be_trades", "Fees long / short": "fees_long_short",
    "Fees on open trades": "fees_open", "Current drawdown": "current_dd", "Longest drawdown": "dd_duration",
    "Recovery factor": "recovery_factor", "Coverage": "mfe_coverage", "Avg MFE": "avg_mfe", "Avg MAE": "avg_mae",
    "MFE efficiency": "mfe_eff_median", "MAE efficiency": "mae_eff_median", "Left on the table": "left_on_table",
}


def info(key: str, page: str | None = None) -> dict | None:
    k = ALIASES.get(key, LABELS.get(key, key))
    if page == "trade" and "t_" + k in I:
        k = "t_" + k
    v = I.get(k) or I.get("t_" + k)
    if not v:
        return None
    return {"key": k, "name": v[0], "desc": v[1], "calc": v[2]}


def all_info() -> dict:
    return {k: info(k) for k in I}
