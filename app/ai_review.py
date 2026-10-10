"""AI review export: a compact, anonymised summary of the journal plus a ready-to-use coaching prompt, to paste into
any AI chat (ChatGPT / Claude / Grok free tiers...). Built on this server from your own data; NOTHING is sent to a
third-party service by the app. Wiring an API key for automatic feedback is a possible future option (not built).

Anonymised: no account names or numbers, no broker ids, no dates beyond day precision. Symbols can be masked
(T1, T2, ...) and free-text notes can be left out."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from app import coach, metrics, outcome
from app.timeutil import utc_naive_to_tz

PROMPT = """You are my trading coach. Below is a privacy-safe summary of my own trading journal (JSON). Analyse it like a
strict but constructive mentor who only trusts the numbers.

1. What am I doing well? Back each point with a number from the data.
2. What am I doing wrong? Rank the top 3 problems by dollar cost or R lost (mistakes, setups, exits, sizing, timing, stops).
3. Look for patterns across my own notes (what went well / wrong / lessons / emotions) and say which ones repeat.
4. Give me 5 specific, measurable rules to follow for the next 20 trades (e.g. "no entries after 11:00", "max 1 trade per setup per day").
5. Tell me what extra data would make this review better, and flag any conclusion that rests on a small sample (<20 trades).

Be concrete. Do not give generic advice. R = profit or loss divided by the amount I planned to risk (initial stop).
MFE capture = share of the best open profit that I actually kept."""


def _round(v, n=2):
    return None if v is None else round(float(v), n)


def _dur(td) -> str | None:
    if td is None:
        return None
    m = td.total_seconds() / 60
    return f"{m:.0f}m" if m < 120 else f"{m / 60:.1f}h" if m < 2880 else f"{m / 1440:.1f}d"


def _group_rows(rows, risk_map=None, keep=12):
    out = []
    for r in rows[:keep]:
        out.append({"name": r["label"], "trades": r["trades"], "win_pct": _round(r["win_pct"], 0),
                    "net": _round(r["net"], 0), "avg": _round(r["avg"], 0),
                    **({"avg_r": _round(risk_map.get(r["label"]), 2)} if risk_map and r["label"] in risk_map else {})})
    return out


def _avg_r_by(closed, keyfn, risk):
    acc: dict[str, list] = defaultdict(list)
    for t in closed:
        r = metrics.r_multiple(t, risk)
        if r is None:
            continue
        keys = keyfn(t)
        for k in (keys if isinstance(keys, list) else [keys]):
            acc[k].append(r)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def build(trades, tz: str, risk: float | None, period: str = "all", symbols: bool = True, notes: bool = True,
          now: datetime | None = None, max_notes: int = 12) -> dict:
    closed = metrics.closed_sorted(trades)
    s = metrics.summarize(trades, tz, risk)
    b = metrics.breakdowns(trades, tz, risk)
    names: dict[str, str] = {}
    sym = (lambda x: x) if symbols else (lambda x: names.setdefault(x, f"T{len(names) + 1}"))
    rs = [(t, metrics.r_multiple(t, risk)) for t in closed]
    rvals = [r for _, r in rs if r is not None]
    mfe_pairs = [(t.net_pnl, t.mfe) for t in closed if t.mfe and t.mfe > 0 and outcome.is_win(t.net_pnl)]
    rps = lambda t: metrics.risk_of(t, None)[0]  # noqa: E731
    mfe_r = [t.mfe / rps(t) for t in closed if t.mfe is not None and rps(t)]
    mae_r = [t.mae / rps(t) for t in closed if t.mae is not None and rps(t)]
    data: dict = {
        "about": {"what": "Anonymised trading-journal summary generated locally by the journal app",
                  "period": period, "currency": "USD", "times": tz,
                  "generated": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
                  "privacy": "no account names/numbers; " + ("symbols masked" if not symbols else "symbols included")
                             + ("; free-text notes omitted" if not notes else "; free-text notes included")},
        "overall": {"closed_trades": s["closed"], "open_trades": s["open"], "win_rate_pct": _round(s["win_pct"], 1),
                    "net_pnl": _round(s["net"], 0), "fees": _round(s["fees"], 0), "profit_factor": _round(s["profit_factor"], 2),
                    "expectancy_per_trade": _round(s["expectancy"], 1), "avg_win": _round(s["avg_win"], 0),
                    "avg_loss": _round(s["avg_loss"], 0), "payoff_ratio": _round(s["pl_ratio"], 2),
                    "largest_win": _round(s["largest_win"], 0), "largest_loss": _round(s["largest_loss"], 0),
                    "max_drawdown": _round(s["max_dd"], 0), "max_consecutive_losses": s["max_consec_losses"],
                    "avg_hold_winners": _dur(s["avg_hold_win"]), "avg_hold_losers": _dur(s["avg_hold_loss"])},
        "risk_r": {"trades_with_risk": len(rvals), "trades_without_stop_or_risk": len(closed) - len(rvals),
                   "avg_r": _round(sum(rvals) / len(rvals), 2) if rvals else None,
                   "total_r": _round(sum(rvals), 1) if rvals else None,
                   "avg_r_winners": _round(s["avg_r_win"], 2), "avg_r_losers": _round(s["avg_r_loss"], 2),
                   "r_distribution": [{"bucket": r["label"], "trades": r["trades"]} for r in b["r"] if r["trades"]]},
        "exits_excursions": {"mfe_capture_on_winners_pct": _round(sum(p[0] for p in mfe_pairs) / sum(p[1] for p in mfe_pairs) * 100, 0)
                             if mfe_pairs else None,
                             "left_on_table_winners": _round(sum(p[1] - p[0] for p in mfe_pairs), 0) if mfe_pairs else None,
                             "avg_mfe_r": _round(sum(mfe_r) / len(mfe_r), 2) if mfe_r else None,
                             "avg_mae_r": _round(sum(mae_r) / len(mae_r), 2) if mae_r else None},
        "by_setup": _group_rows(b["setup"], _avg_r_by(closed, lambda t: t.setup or "(no setup)", risk)),
        "by_tag": _group_rows(b["tag"], _avg_r_by(closed, lambda t: [x.name for x in t.tags] or ["(untagged)"], risk)),
        "by_mistake": _group_rows(b["mistake"], _avg_r_by(closed, lambda t: t.mistakes or ["(no mistake)"], risk)),
        "by_weekday": _group_rows(b["weekday"], keep=7), "by_entry_hour": _group_rows(b["hour"], keep=14),
        "by_hold_time": _group_rows(b["hold"], keep=10), "by_side": _group_rows(b["side"], keep=2),
        "by_instrument": _group_rows(b["instrument"], keep=2),
    }
    data["recent_trades"] = [
        {"date": f"{utc_naive_to_tz(t.opened_at, tz):%Y-%m-%d}", "symbol": sym(t.underlying), "side": t.direction.lower(),
         "asset": t.asset_type.lower(), "setup": t.setup, "mistakes": t.mistakes or None, "net_pnl": _round(t.net_pnl, 0),
         "r": _round(metrics.r_multiple(t, risk), 2), "hold": _dur(metrics.hold_of(t)),
         "mfe_capture_pct": _round(metrics.mfe_efficiency(t), 0)} for t in reversed(closed[-25:])]
    cnt = Counter(m for t in closed for m in t.mistakes)
    data["most_repeated_mistakes"] = [{"mistake": m, "times": n,
                                       "total_pnl": _round(sum(t.net_pnl for t in closed if m in t.mistakes), 0)}
                                      for m, n in cnt.most_common(6)]
    plan = defaultdict(list)
    grade = defaultdict(list)
    for t in closed:
        c = t.answers.get("plan__choice")
        if c:
            plan[c].append(t.net_pnl)
        if t.exec_grade:
            grade[t.exec_grade].append(t.net_pnl)
    data["followed_plan"] = {k: {"trades": len(v), "avg_pnl": _round(sum(v) / len(v), 0)} for k, v in plan.items()}
    data["execution_grades"] = {k: {"trades": len(v), "avg_pnl": _round(sum(v) / len(v), 0)} for k, v in sorted(grade.items())}
    if notes:
        qlabel = {"thesis": "why_took_trade", "well": "went_well", "wrong": "went_wrong", "lesson": "lesson",
                  "emotions": "emotions"}
        snippets: dict[str, list] = defaultdict(list)
        for t in reversed(closed):  # most recent first
            r = metrics.r_multiple(t, risk)
            tag = f"{utc_naive_to_tz(t.opened_at, tz):%Y-%m-%d} {t.direction.lower()} {sym(t.underlying)} " \
                  f"{_usd(t.net_pnl)}{'' if r is None else f' {r:+.1f}R'}"
            for qid, text in t.answers.items():
                if qid in qlabel and text and len(snippets[qlabel[qid]]) < max_notes:
                    snippets[qlabel[qid]].append(f"[{tag}] {str(text)[:240]}")
            if t.notes and len(snippets["other_notes"]) < max_notes:
                snippets["other_notes"].append(f"[{tag}] {t.notes[:240]}")
        data["notes"] = dict(snippets)
    data["rule_based_insights"] = [f"{i['title']}: {i['text']}" for i in coach.insights(trades, tz, risk, limit=12)]
    return data


def _usd(v):
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def to_json(data: dict) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False)


def to_markdown(data: dict) -> str:
    """Prompt + data in one block, ready to paste into an AI chat."""
    return f"{PROMPT}\n\n## My journal data (JSON)\n\n```json\n{to_json(data)}\n```\n"
