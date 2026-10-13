from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import outcome
from app.config import get_settings
from app.db import get_db
from app.models import Execution, ImportBatch, Trade
from app.routes.reports import dashboard_extras
from app.stats import compute
from app.web import apply_trade_filters, base_context, load_trades, parse_filters, realized_scope, templates

router = APIRouter()


def _series(buckets):
    return {"labels": [b.label for b in buckets], "pnl": [round(b.pnl, 2) for b in buckets],
            "count": [b.count for b in buckets], "win_rate": [round(b.win_rate, 1) for b in buckets]}


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    f = parse_filters(request)
    tz = get_settings().display_tz
    stmt = apply_trade_filters(select(Trade), f)
    from app import currency
    from app import quotes as qmod
    from app import realized as rz
    trades = currency.view(db, load_trades(db, stmt))
    open_count = sum(1 for t in trades if t.status == "OPEN")
    # No date filter: this list is already every trade the calendar needs. A date filter still
    # reloads the wider set so a partial exit inside the window counts.
    if f.start or f.end:
        evs, all_evs, _ = realized_scope(db, f)
    else:
        all_evs = rz.events(trades)
        evs = all_evs
    st = compute(trades, tz, open_count=open_count, events=evs)
    all_days = rz.daily(all_evs, tz)  # calendar + daily bars navigate through every day
    has_any = db.scalar(select(func.count(Execution.id))) or 0
    last_import = db.scalar(select(ImportBatch).where(ImportBatch.status == "committed")
                            .order_by(ImportBatch.committed_at.desc()))
    from app.models import SyncRun
    last_sync = db.scalar(select(SyncRun).where(SyncRun.status.in_(("success", "partial")), SyncRun.sources.is_not(None))
                          .order_by(SyncRun.id.desc()))
    recent = sorted([t for t in trades if t.status == "CLOSED"], key=lambda t: t.closed_at, reverse=True)[:8]
    # Open positions ignore the date filter. Without one, they are already in ``trades``.
    opens = None if (f.start or f.end) else [t for t in trades if t.status == "OPEN"]
    anchor = _anchor(db, f, trades, st, quotes=False, open_trades=opens)
    quotes_on = bool(anchor) and qmod.enabled()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(tz)).date()
    focus = min(f.end or today, today).isoformat()
    charts = {
        "equity": {"labels": st.cumulative.get("labels", []), "values": st.cumulative.get("net", []),
                   "day": st.cumulative.get("day", [])},
        "days": [d.as_dict() for d in all_days.values()], "focus": focus, "today": today.isoformat(), "be": list(outcome.get()), "account": f.account_id,
        "range": [f.start.isoformat() if f.start else None, f.end.isoformat() if f.end else None],
        "symbol": _series(st.by_symbol[:12] + ([] if len(st.by_symbol) <= 12 else [])),
        "weekday": _series(st.by_weekday),
        "hour": _series(st.by_hour),
        "direction": _series(st.by_direction),
        "asset": _series(st.by_asset),
        "hold": _series(st.by_hold),
        "winloss": [st.wins, st.losses, st.scratches],
    }
    from app import dividends as dmod
    from app import balance as bmod
    display = currency.get_display(db)
    dividends_total = dmod.total(db, f.account_id, display, f.start, f.end, tz)
    interest = bmod.interest_total(db, f.account_id, display, f.start, f.end, tz)
    bal = bmod.series(db, f.account_id, display, f.start, f.end)
    charts["balance"] = {k: bal[k] for k in ("labels", "balance", "net_deposits", "missing")} if bal else None
    return templates.TemplateResponse(request, "dashboard.html", base_context(
        request, db, nav="dashboard", f=f, st=st, charts=charts, balance=bal, interest=interest, balance_build=has_any > 0 and not bmod.has_points(db, f.account_id, display),
        recent=recent, has_any=has_any, last_import=last_import, n_all=len(trades),
        last_sync=last_sync, anchor=anchor, quotes_on=quotes_on, dividends_total=dividends_total,
        **dashboard_extras(db, trades, tz, events=evs, cumulative=st.cumulative)))


_BUILDING = {"on": False, "error": None}
_INLINE = False  # tests build in the request thread


def _build_balance_bg() -> None:
    import logging
    from app import balance as bmod
    from app import db as dbmod
    s = dbmod.SessionLocal()
    try:
        bmod.rebuild(s, bmod.missing_accounts(s) or None)
        s.commit()
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        _BUILDING["error"] = str(exc)[:200]
        logging.getLogger(__name__).warning("balance build failed: %s", exc)
    finally:
        s.close()
        _BUILDING["on"] = False


@router.post("/dashboard/balance/build")
def balance_build(db: Session = Depends(get_db)):
    """First-time build of the stored balance series (later every sync rebuilds it). Runs in the
    background (daily closes for every held symbol can take a minute); the card polls the status."""
    import threading
    from fastapi.responses import HTMLResponse
    from app import balance as bmod
    from app import currency
    if bmod.has_points(db, None, currency.get_display(db)):
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    if not _BUILDING["on"]:
        _BUILDING.update(on=True, error=None)
        if _INLINE:
            _build_balance_bg()
        else:
            threading.Thread(target=_build_balance_bg, daemon=True).start()
    return balance_status(db)


@router.get("/dashboard/balance/status")
def balance_status(db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    from app import balance as bmod
    from app import currency
    if bmod.has_points(db, None, currency.get_display(db)):
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    if _BUILDING["on"]:
        return HTMLResponse('<div class="card-h">Balance</div><div class="kpi-v text-slate-500" hx-get="/dashboard/balance/status" '
                            'hx-trigger="every 4s" hx-target="closest .kpi-card" hx-swap="innerHTML">…</div>'
                            '<div class="kpi-sub">Building the balance history (daily closes)…</div>')
    msg = "Could not build: " + _BUILDING["error"] if _BUILDING["error"] else "No cash flows or trades yet"
    return HTMLResponse('<div class="card-h">Balance</div><div class="kpi-v">—</div><div class="kpi-sub">' + msg.replace("<", "&lt;") + '</div>')


@router.get("/dashboard/live")
def dashboard_live(request: Request, db: Session = Depends(get_db)):
    """Open-position prices after first paint. Same filters as the dashboard; Yahoo runs here, not on GET /."""
    f = parse_filters(request)
    from app import currency
    from app import realized as rz
    tz = get_settings().display_tz
    display = currency.get_display(db)

    def base():
        stmt = apply_trade_filters(select(Trade), f)
        if f.start or f.end:
            evs, _, _ = realized_scope(db, f)
            trades = currency.view(db, load_trades(db, stmt))
        else:
            trades = currency.view(db, load_trades(db, stmt))
            evs = rz.events(trades)
        open_count = sum(1 for t in trades if t.status == "OPEN")
        st = compute(trades, tz, open_count=open_count, events=evs)
        opens = None if (f.start or f.end) else [t for t in trades if t.status == "OPEN"]
        from app import balance as bmod
        from app import dividends as dmod
        return (trades, st, opens, dmod.total(db, f.account_id, display, f.start, f.end, tz),
                bmod.interest_total(db, f.account_id, display, f.start, f.end, tz))

    from app import pagecache
    # trades / stats / dividends only change with the data: reuse them; only live prices are fetched per call
    trades, st, opens, dividends_total, interest = pagecache.memo(
        ("dash_live", f.preset, f.start, f.end, f.account_id, display), base)
    anchor = _anchor(db, f, trades, st, quotes=True, open_trades=opens)
    return templates.TemplateResponse(request, "partials/dash_live.html", {
        "interest": interest,
        "request": request, "anchor": anchor, "st": st,
        "display_currency": display, "display_symbol": currency.symbol(display),
        "dividends_total": dividends_total,
    })


def _anchor(db: Session, f, trades, st, *, quotes: bool = True, open_trades=None):
    """Unrealized P&L of the open positions at the freshest free price (Yahoo incl. pre/after-hours,
    SnapTrade's last-sync price as fallback), a per-position breakdown compared with SnapTrade's positions,
    and the account check (value - net deposits vs journal realized + unrealized, unfiltered view only).
    Open positions are "now": the date filter is ignored, the account filter applies."""
    from dataclasses import replace
    from datetime import datetime
    from app.models import Account
    from app.sources.snaptrade import portfolio_summary
    from app.stats import open_lots
    from app.symbols import canonical_symbol, load_aliases
    from app import quotes as qmod
    from app import currency
    if open_trades is None:
        from app.web import load_trades
        open_trades = currency.view(db, load_trades(db, apply_trade_filters(
            select(Trade).where(Trade.status == "OPEN"), replace(f, start=None, end=None))))
    usd_fx = currency.RateBook(db).rate("USD", currency.get_display(db), datetime.now().date()) or 1.0
    accts = [a.id for a in db.scalars(select(Account).where(Account.is_demo.is_(False)))
             if not f.account_id or a.id == f.account_id]
    port = portfolio_summary(db, accts) if accts else None
    if port is None and not open_trades:
        return None
    aliases = load_aliases(db)
    snap = {canonical_symbol(sym, aliases): p for sym, p in (port["positions"].items() if port else [])}
    live = qmod.get_quotes(open_trades) if quotes and qmod.enabled() else {}
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
        # Prices stay in the instrument currency. Only the P&L (a money amount) is converted, at the latest rate.
        if q:
            sess = {"pre": " (pre-market)", "post": " (after-hours)", "closed": " (last close)"}.get(q.session, "")
            px, at, src = q.price, q.at, "Yahoo" + sess
        elif sp and sp.get("price"):
            px, at, src = sp["price"], snap_as_of, "SnapTrade (last sync)"
        else:
            px, at, src = None, None, None
        native_pnl = sign * (px * qty - cost) * mult if (px is not None and qty) else None
        pnl = currency.scale_open(t, native_pnl)
        if pnl is not None:
            total += pnl
        snap_pnl = None
        if sp and sp.get("price") and qty:
            snap_pnl = currency.scale_open(t, sign * (sp["price"] * qty - cost) * mult)
            if snap_pnl is not None:
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
                     "ccy": currency.trade_ccy(t),
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
        out.update({"value": port["value"] * usd_fx, "cash": (port["cash"] or 0) * usd_fx,
                    "net_deposits": (port["net_deposits"] or 0) * usd_fx,
                    "total_pnl": (port["total_pnl"] or 0) * usd_fx, "as_of": port["as_of"], "account_check": True,
                    "journal_total": st.realized + total, "diff": journal_snap - (port["total_pnl"] or 0) * usd_fx,
                    "securities_transfers": (port["securities_transfers"] or 0) * usd_fx})
    return out


def _utc(s):
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
