"""Rule-based "coach insights": deterministic patterns found in your own trades (no AI, no network, nothing leaves
the server). Each insight is a dict {level: good | warn | info, title, text}. Groups need a minimum sample so a
single trade never produces a "pattern"."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from app import metrics, outcome
from app.timeutil import utc_naive_to_tz

MIN_GROUP = 4          # trades in a setup / hour / weekday before it is compared
MIN_MISTAKE = 2        # trades with a mistake before its cost is reported
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _usd(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _r(t, risk):
    return metrics.r_multiple(t, risk)


def insights(trades, tz: str = "America/New_York", default_risk: float | None = None, limit: int = 10) -> list[dict]:
    closed = [t for t in trades if t.status == "CLOSED"]
    out: list[dict] = []
    if len(closed) < 5:
        return [{"level": "info", "title": "Not enough closed trades yet",
                 "text": f"Insights start at 5 closed trades in view (now {len(closed)})."}]

    def add(level, title, text):
        out.append({"level": level, "title": title, "text": text})

    # --- setups: best vs the rest (R when available, else $ per trade)
    by_setup: dict[str, list] = defaultdict(list)
    for t in closed:
        if t.setup:
            by_setup[t.setup].append(t)
    scored = []
    for name, ts in by_setup.items():
        if len(ts) >= MIN_GROUP:
            rs = [x for x in (_r(t, default_risk) for t in ts) if x is not None]
            scored.append((name, ts, mean(rs) if len(rs) >= MIN_GROUP else None, mean(t.net_pnl for t in ts)))
    if len(scored) >= 1:
        use_r = all(s[2] is not None for s in scored)
        key = (lambda s: s[2]) if use_r else (lambda s: s[3])
        best = max(scored, key=key)
        rest = [t for t in closed if t.setup != best[0]]
        if rest:
            rest_rs = [x for x in (_r(t, default_risk) for t in rest) if x is not None]
            if use_r and len(rest_rs) >= MIN_GROUP:
                add("good", f"Best setup: {best[0]}", f"{best[0]} averages {best[2]:+.2f}R over {len(best[1])} trades vs "
                    f"{mean(rest_rs):+.2f}R for the others. Take more of these.")
            else:
                add("good", f"Best setup: {best[0]}", f"{best[0]} averages {_usd(best[3])} per trade over {len(best[1])} trades vs "
                    f"{_usd(mean(t.net_pnl for t in rest))} for the others.")
        worst = min(scored, key=key)
        if worst[0] != best[0] and worst[3] < 0:
            add("warn", f"Losing setup: {worst[0]}", f"{worst[0]} lost {_usd(sum(t.net_pnl for t in worst[1]))} over "
                f"{len(worst[1])} trades ({_usd(worst[3])} average). Cut it or tighten the rules.")

    # --- mistakes
    cost: dict[str, list] = defaultdict(list)
    for t in closed:
        for m in t.mistakes:
            cost[m].append(t)
    ranked = sorted(((m, ts, sum(t.net_pnl for t in ts)) for m, ts in cost.items() if len(ts) >= MIN_MISTAKE),
                    key=lambda x: x[2])
    for m, ts, total in ranked[:2]:
        if total < 0:
            add("warn", f"Mistake: {m}", f"Tagged on {len(ts)} trades, costing {_usd(total)} in total ({_usd(total / len(ts))} each).")
    tagged = sum(1 for t in closed if t.mistakes)
    if tagged and len(closed) >= 8:
        clean = [t for t in closed if not t.mistakes]
        bad = [t for t in closed if t.mistakes]
        if len(clean) >= MIN_GROUP and len(bad) >= MIN_GROUP and mean(t.net_pnl for t in clean) > mean(t.net_pnl for t in bad):
            add("info", "Clean trades pay more", f"Trades without a tagged mistake average {_usd(mean(t.net_pnl for t in clean))} vs "
                f"{_usd(mean(t.net_pnl for t in bad))} with one.")

    # --- plan adherence (journal question "Did I follow my plan?")
    plan = defaultdict(list)
    for t in closed:
        c = t.answers.get("plan__choice")
        if c in ("yes", "partly", "no"):
            plan[c].append(t.net_pnl)
    if len(plan["yes"]) >= 3 and len(plan["no"]) + len(plan["partly"]) >= 3:
        off = plan["no"] + plan["partly"]
        add("good" if mean(plan["yes"]) > mean(off) else "info", "Following the plan",
            f"Trades where you followed the plan average {_usd(mean(plan['yes']))} ({len(plan['yes'])}) vs "
            f"{_usd(mean(off))} when you didn't fully ({len(off)}).")

    # --- payoff: loss size vs win size
    wins = [t.net_pnl for t in closed if outcome.is_win(t.net_pnl)]
    losses = [t.net_pnl for t in closed if outcome.is_loss(t.net_pnl)]
    if len(wins) >= 3 and len(losses) >= 3:
        aw, al = mean(wins), abs(mean(losses))
        wr = len(wins) / (len(wins) + len(losses))
        need = al / (aw + al)
        if al > aw:
            add("warn", "Losses bigger than wins", f"Average loss {_usd(-al)} vs average win {_usd(aw)} (payoff {aw / al:.2f}). "
                f"At that size you need a {need:.0%} win rate to break even; you're at {wr:.0%}.")
        else:
            add("good", "Winners outsize losers", f"Average win {_usd(aw)} vs average loss {_usd(-al)} (payoff {aw / al:.2f}); "
                f"break-even win rate {need:.0%}, yours {wr:.0%}.")

    # --- cutting winners early (MFE capture)
    pairs = [(t.net_pnl, t.mfe) for t in closed if t.mfe and t.mfe > 0 and outcome.is_win(t.net_pnl)]
    if len(pairs) >= 4:
        cap = sum(p[0] for p in pairs) / sum(p[1] for p in pairs)
        left = sum(p[1] - p[0] for p in pairs)
        if cap < 0.5:
            add("warn", "You cut winners early", f"On winners you keep {cap:.0%} of the best open profit (MFE capture); "
                f"about {_usd(left)} left on the table over {len(pairs)} trades.")
        elif cap >= 0.7:
            add("good", "Good exits on winners", f"You keep {cap:.0%} of the best open profit on winners.")

    # --- holding losers longer than winners
    hw = [metrics.hold_of(t) for t in closed if outcome.is_win(t.net_pnl) and metrics.hold_of(t)]
    hl = [metrics.hold_of(t) for t in closed if outcome.is_loss(t.net_pnl) and metrics.hold_of(t)]
    if len(hw) >= 3 and len(hl) >= 3:
        aw_h, al_h = sum(hw, hw[0] - hw[0]) / len(hw), sum(hl, hl[0] - hl[0]) / len(hl)
        if aw_h.total_seconds() > 0 and al_h > aw_h * 1.5:
            add("warn", "Losers held longer than winners", f"Losing trades are held {al_h / aw_h:.1f}× longer than winners: "
                "hoping, not following a stop.")

    # --- stops
    no_stop = [t for t in closed if metrics.risk_of(t, None)[0] is None]
    if no_stop:
        add("warn" if len(no_stop) >= max(2, len(closed) // 4) else "info", "Trades without a stop",
            f"{len(no_stop)} of {len(closed)} closed trades have no stop or Risk $, so no R. Options need a Risk $; "
            "stocks get the low of the entry day automatically when price data is available.")
    rs = [x for x in (_r(t, default_risk) for t in closed) if x is not None]
    if len(rs) >= 5:
        big = [x for x in rs if x <= -1.5]
        if big:
            add("warn", "Losses beyond 1R", f"{len(big)} of {len(rs)} trades lost 1.5R or more (worst {min(rs):.1f}R): stops were "
                "wider than planned, moved or ignored.")

    # --- time of day / weekday
    hour, wday = defaultdict(list), defaultdict(list)
    for t in closed:
        lt = utc_naive_to_tz(t.opened_at, tz)
        if t.time_known:
            hour[lt.hour].append(t.net_pnl)
        wday[lt.weekday()].append(t.net_pnl)
    hs = {h: v for h, v in hour.items() if len(v) >= MIN_GROUP}
    if len(hs) >= 2:
        bh = max(hs, key=lambda h: sum(hs[h]))
        wh = min(hs, key=lambda h: sum(hs[h]))
        if bh != wh:
            add("info", "Best / worst entry hour", f"Best: {bh:02d}:00 ({_usd(sum(hs[bh]))}, {len(hs[bh])} trades). "
                f"Worst: {wh:02d}:00 ({_usd(sum(hs[wh]))}, {len(hs[wh])} trades).")
    ws = {d: v for d, v in wday.items() if len(v) >= MIN_GROUP}
    if len(ws) >= 2:
        bd = max(ws, key=lambda d: sum(ws[d]))
        wd = min(ws, key=lambda d: sum(ws[d]))
        if bd != wd:
            add("info", "Best / worst weekday", f"Best: {WEEKDAYS[bd]} ({_usd(sum(ws[bd]))}); worst: {WEEKDAYS[wd]} ({_usd(sum(ws[wd]))}).")

    # --- overtrading
    per_day: dict = defaultdict(list)
    for t in closed:
        per_day[utc_naive_to_tz(t.opened_at, tz).date()].append(t.net_pnl)
    busy = [v for v in per_day.values() if len(v) >= 4]
    calm = [v for v in per_day.values() if len(v) < 4]
    if len(busy) >= 2 and len(calm) >= 2:
        b_avg, c_avg = mean(sum(v) for v in busy), mean(sum(v) for v in calm)
        if b_avg < c_avg:
            add("warn", "Overtrading days", f"Days with 4+ trades average {_usd(b_avg)} vs {_usd(c_avg)} on quieter days.")

    order = {"warn": 0, "good": 1, "info": 2}
    out.sort(key=lambda i: order[i["level"]])
    return out[:limit]
