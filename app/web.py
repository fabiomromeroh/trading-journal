"""Shared web helpers: templates, filters, common context, query filters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import results as _results
from app.config import get_settings
from app.models import Account, Trade, TradeFill
from app.stats import fmt_td
from app.timeutil import local_to_utc_naive, utc_naive_to_tz

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_money(v, signed: bool = False, decimals: int = 2, sym: str = "$") -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    s = f"{abs(v):,.{decimals}f}"
    if v < 0:
        return f"-{sym}{s}"
    return f"+{sym}{s}" if signed and v > 0 else f"{sym}{s}"


@pass_context
def money(ctx, v, signed: bool = False, decimals: int = 2, sym: str | None = None) -> str:
    """Amount with the page's currency symbol (``display_symbol`` in the template context, '$' by default)."""
    return _fmt_money(v, signed, decimals, sym or ctx.get("display_symbol") or "$")


def num(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:,.{decimals}f}"


def qty(v) -> str:
    from app.metrics import fmt_qty
    return fmt_qty(v)


def pnl_class(v) -> str:
    if v is None or abs(v) < 0.005:
        return "text-slate-400"
    return "text-emerald-400" if v > 0 else "text-rose-400"


def local_dt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    return utc_naive_to_tz(dt, get_settings().display_tz).strftime(fmt)


def irish_time(dt, with_date: bool = True) -> str:
    """A UTC datetime (aware, or naive meaning UTC) in Irish time, e.g. 'Oct 9 16:21 IST'."""
    if dt is None:
        return "—"
    from datetime import timezone
    from zoneinfo import ZoneInfo
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(ZoneInfo("Europe/Dublin"))
    return (f"{d:%b} {d.day} " if with_date else "") + f"{d:%H:%M} {d.tzname()}"


from app import outcome as _outcome  # noqa: E402
templates.env.globals.update(outcome_label=_outcome.label, be_range=_outcome.get, be_text=_outcome.describe)
templates.env.filters.update(money=money, num=num, qty=qty, pnl_class=pnl_class, local_dt=local_dt, td=fmt_td,
                             irish_time=irish_time)
from app import metric_info as _mi  # noqa: E402
templates.env.globals.update(settings=get_settings, metric_info=_mi.info)


@dataclass
class Filters:
    preset: str = "all"
    start: date | None = None
    end: date | None = None
    account_id: int | None = None

    def query_string(self, **override) -> str:
        from urllib.parse import urlencode
        d = {"preset": self.preset, "start": self.start.isoformat() if self.start else "",
             "end": self.end.isoformat() if self.end else "", "account": self.account_id or ""}
        d.update(override)
        return urlencode({k: v for k, v in d.items() if v not in ("", None)})


ACCOUNT_COOKIE = "acct"
PRESETS = [("7d", "7D"), ("30d", "30D"), ("90d", "90D"), ("ytd", "YTD"), ("1y", "1Y"), ("all", "All")]


def parse_filters(request: Request) -> Filters:
    q = request.query_params
    f = Filters(preset=q.get("preset", "all"))
    # the header switcher keeps the chosen account in a cookie; an explicit ?account= (links, bookmarks) wins
    raw = q["account"] if "account" in q else request.cookies.get(ACCOUNT_COOKIE, "")
    try:
        f.account_id = int(raw) if raw else None
    except ValueError:
        f.account_id = None
    today = datetime.now().date()
    try:
        f.start = date.fromisoformat(q["start"]) if q.get("start") else None
        f.end = date.fromisoformat(q["end"]) if q.get("end") else None
    except ValueError:
        f.start = f.end = None
    if f.start or f.end:
        f.preset = "custom"
    elif f.preset == "7d":
        f.start = today - timedelta(days=7)
    elif f.preset == "30d":
        f.start = today - timedelta(days=30)
    elif f.preset == "90d":
        f.start = today - timedelta(days=90)
    elif f.preset == "ytd":
        f.start = date(today.year, 1, 1)
    elif f.preset == "1y":
        f.start = today - timedelta(days=365)
    return f


def trade_options():
    """Fills, their executions (broker FX), account and instrument in a handful of queries, not one per row."""
    return (
        selectinload(Trade.fills).selectinload(TradeFill.execution),
        selectinload(Trade.account),
        selectinload(Trade.instrument),
    )


def load_trades(db: Session, stmt) -> list:
    return list(db.scalars(stmt.options(*trade_options())))


def realized_scope(db: Session, f: Filters, extra=None):
    """Realized events for a view: built from every trade matching the non-date filters, then cut to
    the date range by fill date (so a partial exit inside the range counts even if the trade opened or
    closed outside it). ``extra`` applies more statement filters (e.g. the Reports symbol/side filters).
    Returns (events in range, all events, trades used)."""
    from dataclasses import replace
    from app import realized as rz
    stmt = apply_trade_filters(select(Trade), replace(f, start=None, end=None))
    if extra:
        stmt = extra(stmt)
    from app import currency
    trades = _results.trades(db, stmt)  # display-currency view (no-op for same-currency data)
    evs = rz.events(trades)
    return rz.clip(evs, get_settings().display_tz, f.start, f.end), evs, trades


def apply_trade_filters(stmt, f: Filters):
    from datetime import time
    tz = get_settings().display_tz
    ref = func.coalesce(Trade.closed_at, Trade.opened_at)
    if f.account_id:
        stmt = stmt.where(Trade.account_id == f.account_id)
    if f.start:
        stmt = stmt.where(ref >= local_to_utc_naive(f.start, time(0, 0), tz))
    if f.end:
        stmt = stmt.where(ref < local_to_utc_naive(f.end + timedelta(days=1), time(0, 0), tz))
    return stmt


def _attention_marks(db: Session) -> set[str]:
    from app.adjustments import attention_marks
    return attention_marks(db)


def provisional_trade_ids(db: Session) -> set[int]:
    """Trades containing provisional fills (same-day orders or thinkorswim fill emails whose
    fees haven't posted yet)."""
    from app.models import Execution, TradeFill
    from app.services import PROVISIONAL_SOURCES
    return set(db.scalars(select(TradeFill.trade_id).join(Execution, Execution.id == TradeFill.execution_id)
                          .where(Execution.source.in_(PROVISIONAL_SOURCES | {"tos_email"}))))


def base_context(request: Request, db: Session, **kw) -> dict:
    from app.sources import all_sources
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    has_demo = db.scalar(select(func.count(Trade.id)).where(Trade.is_demo.is_(True))) or 0
    banners = []
    for src in all_sources():
        try:
            src.maybe_refresh(db)
        except Exception:  # pragma: no cover - never break page rendering on a status check
            db.rollback()
        st = src.status(db)
        if st.banner:
            banners.append(st.banner)
        elif st.configured and st.expires_in_seconds is not None and st.expires_in_seconds < 86400:
            banners.append({"level": "error" if st.expires_in_seconds <= 0 else "warning",
                            "text": f"{src.name}: {st.message}" + (
                                "" if st.expires_in_seconds <= 0 else
                                f" Login expires in {fmt_td(timedelta(seconds=st.expires_in_seconds))}."),
                            "link": "/settings", "link_text": "Reconnect"})
    from app import currency
    display = currency.get_display(db)
    fx_problem = currency.problem(db, display)
    if fx_problem:
        banners.append({"level": "warning", "text": fx_problem, "link": "/settings#brokers", "link_text": "Details"})
    cur_f = parse_filters(request)
    fx_note = None
    if currency.needs_conversion(db, display):
        fx_note = (f"Amounts are shown in {display}, {currency.FX_PHRASE}. "
                   f"Realized P&L and fees use the broker rate stored on each fill when there is one, otherwise the ECB "
                   f"reference rate that day, from the instrument currency through the account currency into {display}. "
                   f"Open positions use the latest rate. Prices stay in the instrument currency. "
                   f"R multiples and return % are unchanged.")
    ctx = {"display_currency": display, "display_symbol": currency.symbol(display), "active_account": cur_f.account_id, "fx_note": fx_note,
           "currency_choices": currency.CHOICES,
           "request": request, "accounts": accounts, "has_demo": has_demo, "banners": banners,
           "nav": kw.pop("nav", ""), "presets": PRESETS, "provisional_trades": provisional_trade_ids(db),
           "attention_marks": _attention_marks(db)}
    ctx.update(kw)
    return ctx


_ = or_
