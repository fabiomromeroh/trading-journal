from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Execution, ImportBatch, Trade
from app.routes.reports import dashboard_extras
from app.stats import compute
from app.web import apply_trade_filters, base_context, parse_filters, realized_scope, templates

router = APIRouter()


def _series(buckets):
    return {"labels": [b.label for b in buckets], "pnl": [round(b.pnl, 2) for b in buckets],
            "count": [b.count for b in buckets], "win_rate": [round(b.win_rate, 1) for b in buckets]}


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    f = parse_filters(request)
    tz = get_settings().display_tz
    stmt = apply_trade_filters(select(Trade), f)
    trades = list(db.scalars(stmt))
    open_count = sum(1 for t in trades if t.status == "OPEN")
    from app import realized as rz
    evs, all_evs, _ = realized_scope(db, f)
    st = compute(trades, tz, open_count=open_count, events=evs)
    all_days = rz.daily(all_evs, tz)  # calendar + daily bars navigate through every day
    has_any = db.scalar(select(func.count(Execution.id))) or 0
    last_import = db.scalar(select(ImportBatch).where(ImportBatch.status == "committed")
                            .order_by(ImportBatch.committed_at.desc()))
    from app.models import SyncRun
    last_sync = db.scalar(select(SyncRun).where(SyncRun.status.in_(("success", "partial")), SyncRun.sources.is_not(None))
                          .order_by(SyncRun.id.desc()))
    recent = sorted([t for t in trades if t.status == "CLOSED"], key=lambda t: t.closed_at, reverse=True)[:8]
    anchor = _anchor(db, f, trades, st)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(tz)).date()
    focus = min(f.end or today, today).isoformat()
    charts = {
        "equity": {"labels": st.cumulative.get("labels", []), "values": st.cumulative.get("net", []),
                   "day": st.cumulative.get("day", [])},
        "days": [d.as_dict() for d in all_days.values()], "focus": focus, "today": today.isoformat(),
        "range": [f.start.isoformat() if f.start else None, f.end.isoformat() if f.end else None],
        "symbol": _series(st.by_symbol[:12] + ([] if len(st.by_symbol) <= 12 else [])),
        "weekday": _series(st.by_weekday),
        "hour": _series(st.by_hour),
        "direction": _series(st.by_direction),
        "asset": _series(st.by_asset),
        "hold": _series(st.by_hold),
        "winloss": [st.wins, st.losses, st.scratches],
    }
    return templates.TemplateResponse(request, "dashboard.html", base_context(
        request, db, nav="dashboard", f=f, st=st, charts=charts,
        recent=recent, has_any=has_any, last_import=last_import, n_all=len(trades),
        last_sync=last_sync, anchor=anchor, **dashboard_extras(db, trades, tz, events=evs, cumulative=st.cumulative)))


def _anchor(db: Session, f, trades, st):
    """Unrealized P&L of the open positions at the freshest free price (Yahoo incl. pre/after-hours,
    SnapTrade's last-sync price as fallback), a per-position breakdown compared with SnapTrade's positions,
    and the account check (value - net deposits vs journal realized + unrealized, unfiltered view only).
    Open positions are "now": the date filter is ignored, the account filter applies."""
    from dataclasses import replace
    from app.models import Account
    from app.sources.snaptrade import portfolio_summary
    from app.stats import open_lots
    from app.symbols import canonical_symbol, load_aliases
    from app import quotes as qmod
    open_trades = list(db.scalars(apply_trade_filters(select(Trade).where(Trade.status == "OPEN"),
                                                      replace(f, start=None, end=None))))
    accts = [a.id for a in db.scalars(select(Account).where(Account.is_demo.is_(False)))
             if not f.account_id or a.id == f.account_id]
    port = portfolio_summary(db, accts) if accts else None
    if port is None and not open_trades:
        return None
    aliases = load_aliases(db)
    snap = {canonical_symbol(sym, aliases): p for sym, p in (port["positions"].items() if port else [])}
    live = qmod.get_quotes(open_trades) if qmod.enabled() else {}
    snap_as_of = _utc(port["as_of"]) if port and port.get("as_of") else None
    rows, total, total_snap, seen = [], 0.0, 0.0, set()
    for t in sorted(open_trades, key=lambda t: t.symbol):
        lots = open_lots(t)
        qty = sum(q for q, _ in lots)
        cost = sum(q * p for q, p in lots)
        mult = t.multiplier or 1
        sign = 1 if t.direction == "LONG" else -1
        sp = snap.get(t.symbol)
        seen.add(t.symbol)
        q = live.get(t.symbol)
        if q:
            sess = {"pre": " (pre-market)", "post": " (after-hours)", "closed": " (last close)"}.get(q.session, "")
            px, at, src = q.price, q.at, "Yahoo" + sess
        elif sp and sp.get("price"):
            px, at, src = sp["price"], snap_as_of, "SnapTrade (last sync)"
        else:
            px, at, src = None, None, None
        pnl = sign * (px * qty - cost) * mult if (px is not None and qty) else None
        if pnl is not None:
            total += pnl
        snap_pnl = None
        if sp and sp.get("price") and qty:
            snap_pnl = sign * (sp["price"] * qty - cost) * mult
            total_snap += snap_pnl
        flags = []
        if sp is None:
            if port:
                flags.append("not in SnapTrade positions")
        else:
            units = abs(sp.get("units") or 0)
            if abs(units - qty) > 1e-6:
                flags.append(f"qty differs: SnapTrade {units:g}")
            cb = sp.get("cost_basis")
            if cb and qty and abs(cb - cost / qty) > max(0.01, 0.002 * cb):
                flags.append(f"avg cost differs: SnapTrade {cb:,.4f}")
        rows.append({"symbol": t.symbol, "trade_id": t.id, "direction": t.direction, "qty": qty,
                     "avg": cost / qty if qty else None, "price": px, "at": at, "source": src, "pnl": pnl,
                     "multiplier": mult, "realized": t.net_pnl or 0.0,
                     "snap_qty": sp.get("units") if sp else None, "snap_price": sp.get("price") if sp else None,
                     "snap_cost": sp.get("cost_basis") if sp else None, "snap_pnl": snap_pnl, "flags": flags})
    for sym, sp in sorted(snap.items()):
        if sym not in seen and (sp.get("units") or 0):
            rows.append({"symbol": sym, "trade_id": None, "direction": None, "qty": 0, "avg": None, "price": None,
                         "at": None, "source": None, "pnl": None, "multiplier": 1, "realized": 0.0,
                         "snap_qty": sp.get("units"), "snap_price": sp.get("price"), "snap_cost": sp.get("cost_basis"),
                         "snap_pnl": None, "flags": ["held at the broker but no open trade in the journal"]})
    times = [r["at"] for r in rows if r["at"]]
    out = {"unrealized": total, "rows": rows,
           "missing_prices": [r["symbol"] for r in rows if r["price"] is None and r["qty"]],
           "price_at": max(times) if times else None, "oldest_at": min(times) if times else None,
           "sources": sorted({r["source"].split(" (")[0] for r in rows if r["source"]}),
           "discrepancies": sum(1 for r in rows if r["flags"]), "unrealized_snap": total_snap,
           "snap_as_of": snap_as_of, "account_check": False, "open_count": len(open_trades)}
    if port and not (f.start or f.end):
        journal_snap = st.realized + total_snap  # same prices as the account value -> apples to apples
        out.update({"value": port["value"], "cash": port["cash"], "net_deposits": port["net_deposits"],
                    "total_pnl": port["total_pnl"], "as_of": port["as_of"], "account_check": True,
                    "journal_total": st.realized + total, "diff": journal_snap - port["total_pnl"],
                    "securities_transfers": port["securities_transfers"]})
    return out


def _utc(s):
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
