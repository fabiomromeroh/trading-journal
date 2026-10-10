"""Golden-master helpers: a deterministic JSON snapshot of everything Schwab-style data produces
(executions, built trades + fills, per-trade metrics, the dashboard/report summary, realized events).

The multi-broker work must leave these numbers identical: new keys may appear in a snapshot, but every key that
was in the baseline must keep its value (see ``subset_diff``)."""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime

from sqlalchemy import select

from app import metrics, realized
from app.models import Account, Execution, Trade


def norm(v):
    if is_dataclass(v) and not isinstance(v, type):
        v = asdict(v)
    if isinstance(v, (dict, OrderedDict)):
        return {str(k): norm(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(norm(x) for x in v)
    if isinstance(v, float):
        return None if v != v else ("inf" if v in (float("inf"), float("-inf")) else round(v, 6))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is None or isinstance(v, (int, str, bool)):
        return v
    return str(v)


EXEC_FIELDS = ("source", "external_id", "symbol", "underlying", "asset_type", "option_type", "strike", "expiration",
               "multiplier", "side", "quantity", "price", "fees", "executed_at", "time_known", "seq", "position_effect", "kind")
TRADE_FIELDS = ("key", "symbol", "underlying", "asset_type", "option_type", "direction", "status", "opened_at", "closed_at",
                "quantity", "entry_price", "exit_price", "gross_pnl", "net_pnl", "fees", "multiplier", "return_pct",
                "cost_basis", "setup", "rating", "initial_stop", "risk_amount", "profit_target", "mfe", "mae")


def snapshot(db, tz: str = "America/New_York") -> dict:
    accts = {a.id: a.name for a in db.scalars(select(Account))}
    execs = sorted(({"account": accts[e.account_id], **{f: getattr(e, f) for f in EXEC_FIELDS}}
                    for e in db.scalars(select(Execution))), key=lambda d: (d["account"], d["source"], d["external_id"]))
    trades = list(db.scalars(select(Trade).order_by(Trade.key)))
    tr = []
    for t in trades:
        m = metrics.trade_metrics(t, None)
        tr.append({"account": accts.get(t.account_id), **{f: getattr(t, f, None) for f in TRADE_FIELDS},
                   "fills": [{"role": f.role, "side": f.side, "quantity": f.quantity, "price": f.price, "fees": f.fees,
                              "executed_at": f.executed_at, "position": f.position} for f in t.fills],
                   "metrics": {k: v for k, v in m.items() if k not in ("fills",)},
                   "r_multiple": metrics.r_multiple(t, None), "hold": metrics.hold_of(t)})
    closed = [t for t in trades if t.status == "CLOSED"]
    try:
        events = realized.events(trades)
        ev = [{"day": getattr(e, "day", None), "pnl": getattr(e, "pnl", None)} for e in events]
    except Exception:  # noqa: BLE001
        ev = []
    summary = metrics.summarize(trades, tz, None)
    return norm({"executions": execs, "trades": tr, "summary": summary, "events": ev,
                 "n_closed": len(closed), "n_open": len(trades) - len(closed)})


def subset_diff(expected, actual, path="") -> list[str]:
    """Every key/value in ``expected`` must be present and equal in ``actual`` (extra keys in actual are fine)."""
    out: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected dict, got {type(actual).__name__}"]
        for k, v in expected.items():
            if k not in actual:
                out.append(f"{path}/{k}: missing")
            else:
                out += subset_diff(v, actual[k], f"{path}/{k}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [f"{path}: list length {len(expected)} != {len(actual) if isinstance(actual, list) else '?'}"]
        for i, (a, b) in enumerate(zip(expected, actual)):
            out += subset_diff(a, b, f"{path}[{i}]")
    elif expected != actual:
        out.append(f"{path}: {expected!r} != {actual!r}")
    return out


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)
