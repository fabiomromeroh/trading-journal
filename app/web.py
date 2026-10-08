"""Shared web helpers: templates, filters, common context, query filters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, Trade
from app.stats import fmt_td
from app.timeutil import local_to_utc_naive, utc_naive_to_tz

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def money(v, signed: bool = False, decimals: int = 2) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    s = f"{abs(v):,.{decimals}f}"
    if v < 0:
        return f"-${s}"
    return f"+${s}" if signed and v > 0 else f"${s}"


def num(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:,.{decimals}f}"


def qty(v) -> str:
    return "—" if v is None else (f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.4f}".rstrip("0"))


def pnl_class(v) -> str:
    if v is None or abs(v) < 0.005:
        return "text-slate-400"
    return "text-emerald-400" if v > 0 else "text-rose-400"


def local_dt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    return utc_naive_to_tz(dt, get_settings().display_tz).strftime(fmt)


templates.env.filters.update(money=money, num=num, qty=qty, pnl_class=pnl_class, local_dt=local_dt, td=fmt_td)
templates.env.globals.update(settings=get_settings)


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


PRESETS = [("7d", "7D"), ("30d", "30D"), ("90d", "90D"), ("ytd", "YTD"), ("1y", "1Y"), ("all", "All")]


def parse_filters(request: Request) -> Filters:
    q = request.query_params
    f = Filters(preset=q.get("preset", "all"))
    try:
        f.account_id = int(q["account"]) if q.get("account") else None
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


def provisional_trade_ids(db: Session) -> set[int]:
    """Trades containing provisional fills (same-day orders whose fees haven't posted yet)."""
    from app.models import Execution, TradeFill
    from app.services import PROVISIONAL_SOURCES
    return set(db.scalars(select(TradeFill.trade_id).join(Execution, Execution.id == TradeFill.execution_id)
                          .where(Execution.source.in_(PROVISIONAL_SOURCES))))


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
    ctx = {"request": request, "accounts": accounts, "has_demo": has_demo, "banners": banners,
           "nav": kw.pop("nav", ""), "presets": PRESETS, "provisional_trades": provisional_trade_ids(db)}
    ctx.update(kw)
    return ctx


_ = or_
