"""Account balance curve, like Trading 212's account value chart.

Balance on a day = net deposits so far (real deposits - withdrawals the broker reported)
                 + realized P&L so far (by fill day, after fees)
                 + dividends paid so far
                 + other cash so far (interest - cash fees, when the broker reports them)
                 + unrealized P&L of the positions held at that day's close (daily close prices).

Nothing is estimated. A position without a close price on a day (no candle within the last
``CARRY_DAYS`` days, e.g. an option) is left out of that day's unrealized figure and its symbol is
stored in ``missing`` so the widget can say so. The series is computed once per sync (and on demand
when it is missing) and stored in ``balance_points``; page loads only read it.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Account, BalancePoint, CashFlow, DailyClose, Dividend, Trade, utcnow
from app.services import get_state, set_state

log = logging.getLogger(__name__)

CURRENCIES = ("USD", "EUR")
CARRY_DAYS = 6           # weekends / holidays: the last real close stays valid this many days
T212_SUMMARY_STATE = "balance:t212:"   # + account id: latest real cash / invested / total (no secrets)
IBKR_SUMMARY_STATE = "balance:ibkr:"   # + account id: latest Flex equity summary (no secrets)
FLOW_KINDS = ("deposit", "withdrawal", "transfer")   # count as net deposits
OTHER_KINDS = ("interest", "fee", "proceeds")         # count as return (cash the broker paid / charged / CA cash)


# ------------------------------------------------------------------ cash flows
def ingest_flows(db: Session, account_id: int, source: str, rows: list[dict]) -> dict:
    """Upsert real cash movements by (account, source, broker id). Returns {inserted, merged}."""
    if not rows:
        return {"inserted": 0, "merged": 0}
    ids = [r["external_id"] for r in rows]
    have = {c.external_id: c for c in db.scalars(select(CashFlow).where(
        CashFlow.account_id == account_id, CashFlow.source == source, CashFlow.external_id.in_(ids)))}
    ins = mer = 0
    for r in rows:
        fields = {k: r.get(k) for k in ("occurred_at", "amount", "currency", "kind", "description")}
        row = have.get(r["external_id"])
        if row is None:
            db.add(CashFlow(account_id=account_id, source=source, external_id=r["external_id"], **fields))
            have[r["external_id"]] = True  # duplicate ids inside one batch
            ins += 1
        elif row is not True and any(getattr(row, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(row, k, v)
            mer += 1
    db.flush()
    return {"inserted": ins, "merged": mer}


_T212_KINDS = {"DEPOSIT": "deposit", "WITHDRAW": "withdrawal", "WITHDRAWAL": "withdrawal",
               "TRANSFER": "transfer", "INTEREST": "interest", "INTEREST_ON_CASH": "interest",
               "CASHBACK": "interest", "FEE": "fee"}


def raw_type(item) -> str:
    return str((item or {}).get("type") or (item or {}).get("transactionType") or "").upper().replace(" ", "_") \
        if isinstance(item, dict) else ""


def flow_kind(typ: str) -> str | None:
    kind = _T212_KINDS.get(typ)
    if kind is None and "INTEREST" in typ:
        kind = "interest"       # INTEREST_ON_CASH, LENDING_INTEREST, ...
    if kind is None and typ.endswith("_FEE"):
        kind = "fee"
    return kind


def t212_flow(item: dict, account_ccy: str | None) -> dict | None:
    """A Trading 212 cash-ledger row (/equity/history/transactions) as a cash flow, or None."""
    if not isinstance(item, dict):
        return None
    typ = raw_type(item)
    kind = flow_kind(typ)
    if not kind:
        return None
    try:
        amt = float(item.get("amount"))
    except (TypeError, ValueError):
        return None
    when_s = item.get("dateTime") or item.get("time") or item.get("createdAt")
    ext = item.get("reference") if item.get("reference") is not None else item.get("id")
    if not when_s or ext is None or not amt:
        return None
    from app.sources.trading212 import parse_ts
    if kind in ("withdrawal", "fee") and amt > 0:
        amt = -amt
    return {"external_id": f"{typ}:{ext}", "occurred_at": parse_ts(str(when_s)), "amount": round(amt, 2),
            "currency": (item.get("currency") or account_ccy or "").upper() or None, "kind": kind,
            "description": typ.replace("_", " ").title()}


def import_snaptrade_flows(db: Session, account_id: int) -> int:
    """Copy the external cash flows SnapTrade sync already records (app_state) into ``cash_flows``."""
    from app.sources.snaptrade import CASHFLOW_STATE
    raw = get_state(db, CASHFLOW_STATE + str(account_id))
    if not raw:
        return 0
    acct = db.get(Account, account_id)
    rows = []
    for ext, f in (json.loads(raw) or {}).items():
        try:
            d = datetime.strptime((f.get("date") or "")[:10], "%Y-%m-%d").replace(hour=12)
        except ValueError:
            continue
        amt = float(f.get("amount") or 0)
        if not amt:
            continue
        typ = (f.get("type") or "").upper()
        kind = "withdrawal" if typ == "WITHDRAWAL" else "deposit" if typ in ("DEPOSIT", "CONTRIBUTION") else "transfer"
        rows.append({"external_id": str(ext), "occurred_at": d, "amount": amt,
                     "currency": ((acct.currency if acct else None) or "USD").upper(), "kind": kind,
                     "description": ((f.get("description") or typ) or "")[:200]})
    return ingest_flows(db, account_id, "snaptrade", rows)["inserted"]


# ------------------------------------------------------------------ broker snapshot (check)
def save_t212_summary(db: Session, account_id: int, summary: dict, cash_fallback: dict | None = None) -> None:
    """Keep only the money fields of /equity/account/summary (or the legacy cash endpoint)."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    cash = summary.get("cash") if isinstance(summary.get("cash"), dict) else {}
    inv = summary.get("investments") if isinstance(summary.get("investments"), dict) else {}
    c = sum(x for x in (num(cash.get(k)) for k in ("availableToTrade", "inPies", "reservedForOrders")) if x is not None) if cash else None
    invested = num(inv.get("currentValue"))
    total = num(summary.get("totalValue"))
    if cash_fallback:
        c = num(cash_fallback.get("free")) if c is None else c
        invested = num(cash_fallback.get("invested")) if invested is None else invested
        total = num(cash_fallback.get("total")) if total is None else total
    if total is None and c is not None and invested is not None:
        total = c + invested
    if total is None:
        return
    set_state(db, T212_SUMMARY_STATE + str(account_id), json.dumps({
        "as_of": utcnow().isoformat(), "currency": (summary.get("currency") or "").upper() or None,
        "cash": c, "invested": invested, "total": total}))


def save_ibkr_summary(db: Session, account_id: int, equity: dict, account_ccy: str | None = None) -> None:
    """Keep the latest Flex EquitySummaryInBase money fields (cash / stock / total)."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    total = num(equity.get("total"))
    if total is None:
        return
    set_state(db, IBKR_SUMMARY_STATE + str(account_id), json.dumps({
        "as_of": utcnow().isoformat(), "currency": (equity.get("currency") or account_ccy or "").upper() or None,
        "cash": num(equity.get("cash")), "invested": num(equity.get("invested")), "total": total}))


def broker_snapshots(db: Session, account_ids: list[int]) -> dict[int, dict]:
    """{account id: {total, cash, invested, currency, as_of, source}} from the latest sync of each broker."""
    from app.sources.snaptrade import PORTFOLIO_STATE
    out = {}
    for aid in account_ids:
        raw = get_state(db, T212_SUMMARY_STATE + str(aid))
        if raw:
            d = json.loads(raw)
            acct = db.get(Account, aid)
            d["currency"] = d.get("currency") or (acct.currency if acct else "EUR")
            d["source"] = "Trading 212"
            out[aid] = d
            continue
        raw = get_state(db, IBKR_SUMMARY_STATE + str(aid))
        if raw:
            d = json.loads(raw)
            acct = db.get(Account, aid)
            d["currency"] = d.get("currency") or (acct.currency if acct else "USD")
            d["source"] = "Interactive Brokers"
            out[aid] = d
            continue
        raw = get_state(db, PORTFOLIO_STATE + str(aid))
        if raw:
            d = json.loads(raw)
            out[aid] = {"total": d.get("value"), "cash": d.get("cash"), "invested": d.get("market_value"),
                        "currency": "USD", "as_of": d.get("as_of"), "source": "SnapTrade"}
    return out


# ------------------------------------------------------------------ daily closes
# Completed calendar years use a stable persistent candle window, copied once into daily_closes.
# The current year is refreshed only when the last session's close is missing, and then at most
# once per process per few minutes so a second rebuild the same day does not re-read candles.
_TAIL_FRESH = 600.0
_tail_seen: dict[str, float] = {}


def _tail_key(db: Session, symbol: str) -> str:
    try:
        url = str(db.get_bind().url)
    except Exception:  # noqa: BLE001
        url = ""
    return url + "|" + symbol


def _tail_fresh(db: Session, symbol: str) -> bool:
    seen = _tail_seen.get(_tail_key(db, symbol))
    return seen is not None and (time.monotonic() - seen) < _TAIL_FRESH


def _mark_tail(db: Session, symbol: str) -> None:
    _tail_seen[_tail_key(db, symbol)] = time.monotonic()


def _gaps_ok(days: list[date]) -> bool:
    return all((b - a).days <= CARRY_DAYS + 1 for a, b in zip(days, days[1:]))


def _history_ok(days: list[date], span_start: date) -> bool:
    return bool(days) and (days[0] - span_start).days <= CARRY_DAYS and _gaps_ok(days)


def _range_ok(days: list[date], span_start: date, span_end: date) -> bool:
    return _history_ok(days, span_start) and (span_end - days[-1]).days <= CARRY_DAYS


def _candles_to_closes(candles) -> dict[date, float]:
    out: dict[date, float] = {}
    for c in candles or []:
        t = int(c["time"])
        t = t // 1000 if t > 10**11 else t  # some providers use ms
        out.setdefault(datetime.fromtimestamp(t, timezone.utc).date(), float(c["close"]))
    return out


def _load_daily(db: Session, symbol: str, start: date, end: date) -> dict[date, float]:
    lo = start - timedelta(days=CARRY_DAYS + 1)
    rows = db.execute(select(DailyClose.day, DailyClose.close).where(
        DailyClose.symbol == symbol, DailyClose.day >= lo, DailyClose.day <= end)).all()
    return {d: float(c) for d, c in rows}


def _upsert_daily(db: Session, symbol: str, provider: str, mapping: dict[date, float]) -> None:
    if not mapping:
        return
    from app.payloads import dialect_insert
    dia = dialect_insert(db)
    now = utcnow()
    items = list(mapping.items())
    for i in range(0, len(items), 80):
        chunk = items[i:i + 80]
        stmt = dia(DailyClose).values([{
            "symbol": symbol, "day": d, "close": float(c), "provider": (provider or "")[:20], "updated_at": now,
        } for d, c in chunk])
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "day"],
            set_={"close": stmt.excluded.close, "provider": stmt.excluded.provider,
                  "updated_at": stmt.excluded.updated_at},
        )
        db.execute(stmt)
    db.commit()


def _fetch_year(db: Session, trade, provs: list[str], year: int, now: datetime):
    from app import prices
    y_start = datetime(year, 1, 1)
    y_end = datetime(year, 12, 31, 23, 0) if year < now.year else now
    for prov in provs:
        try:
            candles = prices._fetch(db, prov, trade, "1d", y_start, y_end, now)
        except Exception as exc:  # noqa: BLE001 - a missing price is a gap, never a failed sync
            log.info("balance: no %s daily closes for %s: %s", prov, getattr(trade, "underlying", "?"), exc)
            continue
        got = _candles_to_closes(candles)
        if got:
            return prov, got
    return None


def _yahoo_closes(db: Session, trade, start: date, end: date) -> dict[date, float]:
    """Daily closes for one symbol. Reads ``daily_closes`` when the span is already covered."""
    from app import prices, symbolmap
    provs = prices._provider_order(trade)  # same order as the charts (Schwab / Polygon / Yahoo)
    if not provs:
        return {}
    symbol = symbolmap.price_symbol(trade) or getattr(trade, "underlying", None) or getattr(trade, "symbol", None)
    if not symbol:
        return {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    end_d = min(end, now.date())
    if end_d < start:
        end_d = start
    have = _load_daily(db, symbol, start, end_d)
    missing: list[int] = []
    for year in range(start.year, end_d.year + 1):
        span_start = max(start, date(year, 1, 1))
        y_end = date(year, 12, 31) if year < now.year else end_d
        lo = span_start - timedelta(days=CARRY_DAYS + 1)
        days = sorted(d for d in have if lo <= d <= y_end)
        if year < now.year:
            if not _range_ok(days, span_start, y_end):
                missing.append(year)
            continue
        newest = days[-1] if days else None
        tail_ok = (newest is not None and newest >= prices.last_session_date(now)) or _tail_fresh(db, symbol)
        if not (_history_ok(days, span_start) and tail_ok):
            missing.append(year)
    for year in missing:
        fetched = _fetch_year(db, trade, provs, year, now)
        if fetched:
            prov, got = fetched
            _upsert_daily(db, symbol, prov, got)
            have.update(got)
        if year == now.year:
            _mark_tail(db, symbol)
    lo = start - timedelta(days=CARRY_DAYS + 1)
    return {d: c for d, c in have.items() if lo <= d <= end_d}


class Closes:
    """Close on or before a day (within CARRY_DAYS) per price symbol, fetched once per symbol."""

    def __init__(self, db: Session, fetch=None):
        self.db, self.fetch = db, fetch or _yahoo_closes
        self.data: dict[str, tuple[list[date], dict[date, float]]] = {}
        self.span: dict[str, date] = {}

    def prime(self, key: str, trade, start: date, end: date) -> None:
        """Fetch once per symbol; fetch again only when a later trade needs an earlier start."""
        have = self.span.get(key)
        if have is not None and have <= start:
            return
        got = self.fetch(self.db, trade, start, end) or {}
        if key in self.data:
            got = {**self.data[key][1], **got}
        self.data[key] = (sorted(got), got)
        self.span[key] = start if have is None else min(have, start)

    def on(self, key: str, day: date) -> float | None:
        import bisect
        days, got = self.data.get(key, ([], {}))
        i = bisect.bisect_right(days, day) - 1
        if i < 0 or (day - days[i]).days > CARRY_DAYS:
            return None
        return got[days[i]]


# ------------------------------------------------------------------ compute + store
def _local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()


def compute_account(db: Session, acct: Account, ccy: str, closes: Closes, tz: str, today: date | None = None
                    ) -> list[dict]:
    """Daily rows for one account in ``ccy``: first cash/trade day through today."""
    from sqlalchemy.orm import selectinload
    from app import currency, dividends as dmod, realized as rz, symbolmap
    from app.models import TradeFill
    Z = ZoneInfo(tz)
    today = today or datetime.now(Z).date()
    trades = list(db.scalars(select(Trade).where(Trade.account_id == acct.id).options(
        selectinload(Trade.fills).selectinload(TradeFill.execution), selectinload(Trade.instrument))))
    flows = list(db.scalars(select(CashFlow).where(CashFlow.account_id == acct.id)))
    divs = list(db.scalars(select(Dividend).where(Dividend.account_id == acct.id)))
    starts = [_local_day(f.occurred_at, Z) for f in flows] + [_local_day(t.opened_at, Z) for t in trades if t.opened_at] \
        + [_local_day(d.paid_on, Z) for d in divs if d.paid_on]
    if not starts:
        return []
    start = min(starts)
    book = currency.RateBook(db)
    ccys = {ccy, (acct.currency or "USD").upper()} | {(f.currency or "").upper() for f in flows} \
        | {currency.trade_ccy(t) for t in trades} | {dmod.cash(d)[1] for d in divs}
    ccys.discard("")
    if ccys - {ccy}:
        book.ensure(ccys, start, today)
    views = currency.view(db, trades, ccy)

    realized = defaultdict(float)
    for d, row in rz.daily(rz.events(views), tz).items():
        realized[d] += row.net
    dividends = defaultdict(float)
    for d in divs:
        v = dmod.convert_one(d, ccy, book)
        if v is not None:
            dividends[_local_day(d.paid_on, Z)] += v
    deposits, other = defaultdict(float), defaultdict(float)
    flow_gaps = 0
    for f in flows:
        day = _local_day(f.occurred_at, Z)
        r = book.rate(f.currency or acct.currency or "USD", ccy, day)
        if r is None:
            flow_gaps += 1
            continue
        (deposits if f.kind in FLOW_KINDS else other)[day] += f.amount * r

    # unrealized: per trade, open lots after each day's fills, marked at that day's close
    unrl = defaultdict(float)
    missing: dict[date, set] = defaultdict(set)
    for ct in views:
        t = currency.raw(ct)
        fills = sorted(t.fills or [], key=lambda f: f.position)
        if not fills or not t.opened_at:
            continue
        first = _local_day(fills[0].executed_at, Z)
        last = _local_day(t.closed_at, Z) if t.status == "CLOSED" and t.closed_at else today
        if last < first:
            continue
        is_opt = (t.asset_type or "").upper() == "OPTION"
        key = symbolmap.price_symbol(t) or t.underlying
        if not is_opt:
            closes.prime(key, t, first, today)
        sign = 1.0 if t.direction == "LONG" else -1.0
        mult = t.multiplier or 1.0
        fx_on = getattr(ct, "fx_on", None)
        instr, accy = currency.trade_ccy(t), currency.account_ccy(t)
        lots: list[list[float]] = []
        i, day = 0, first
        while day <= last:
            while i < len(fills) and _local_day(fills[i].executed_at, Z) <= day:
                f = fills[i]
                rate = fx_on(f) if fx_on else 1.0
                if f.role == "OPEN":
                    lots.append([f.quantity, f.price, rate])
                else:
                    q = f.quantity
                    while q > 1e-9 and lots:
                        m = min(q, lots[0][0])
                        lots[0][0] -= m
                        q -= m
                        if lots[0][0] <= 1e-9:
                            lots.pop(0)
                i += 1
            qty = sum(l[0] for l in lots)
            if qty > 1e-9:
                px = None if is_opt else closes.on(key, day)
                mark = currency.chain_rate(book, instr, accy, ccy, day, latest=True) if px is not None else None
                if px is None or mark is None:
                    missing[day].add(t.symbol + (" (option)" if is_opt else ""))
                else:
                    cost = sum(l[0] * l[1] * l[2] for l in lots)
                    unrl[day] += sign * (qty * px * mark - cost) * mult
            day += timedelta(days=1)

    rows, nd, rc, dc, oc = [], 0.0, 0.0, 0.0, 0.0
    day = start
    while day <= today:
        nd += deposits.get(day, 0.0)
        rc += realized.get(day, 0.0)
        dc += dividends.get(day, 0.0)
        oc += other.get(day, 0.0)
        u = round(unrl.get(day, 0.0), 2)
        miss = sorted(missing.get(day, ()))
        rows.append({"day": day, "net_deposits": round(nd, 2), "realized_cum": round(rc, 2),
                     "dividends_cum": round(dc, 2), "other_cum": round(oc, 2), "unrealized": u,
                     "balance": round(nd + rc + dc + oc + u, 2), "missing": ",".join(miss) or None})
        day += timedelta(days=1)
    if flow_gaps and rows:
        rows[-1]["missing"] = ",".join(filter(None, [rows[-1]["missing"], f"{flow_gaps} cash flow(s) without FX rate"]))
    return rows


def rebuild(db: Session, account_ids: list[int] | None = None, currencies=None, fetch=None,
            today: date | None = None) -> dict:
    """Recompute and store the balance series. Never raises into a sync."""
    from app.config import get_settings
    tz = get_settings().display_tz
    accts = list(db.scalars(select(Account) if not account_ids else select(Account).where(Account.id.in_(account_ids))))
    closes = Closes(db, fetch)
    currencies = currencies or CURRENCIES
    done = {}
    for a in accts:
        try:
            import_snaptrade_flows(db, a.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("balance: snaptrade flows for %s: %s", a.id, exc)
        for ccy in currencies:
            rows = compute_account(db, a, ccy, closes, tz, today)
            db.execute(delete(BalancePoint).where(BalancePoint.account_id == a.id, BalancePoint.currency == ccy))
            now = utcnow()
            db.add_all([BalancePoint(account_id=a.id, currency=ccy, computed_at=now, **r) for r in rows])
            done[(a.id, ccy)] = len(rows)
    db.flush()
    return done


def missing_accounts(db: Session, ccy: str | None = None) -> list[int]:
    """Accounts with no stored balance points yet (in ``ccy``, or in any of CURRENCIES)."""
    have = set(db.scalars(select(BalancePoint.account_id).where(
        BalancePoint.currency.in_([ccy] if ccy else list(CURRENCIES))).distinct()))
    return [a for a in db.scalars(select(Account.id)) if a not in have]


def after_sync(db: Session, account_ids) -> None:
    # No SAVEPOINT: the price cache and FX top-up commit on their own, which would close one.
    # The sync's own work is committed first so a failure here can only lose the curve.
    # ``balance_fresh`` lets a caller that already rebuilt (IBKR, once at the end) skip a second pass.
    db.commit()
    try:
        ids = sorted(set(account_ids) | set(missing_accounts(db)))
        rebuild(db, ids or None)
        db.commit()
        fresh = set(db.info.get("balance_fresh") or ())
        fresh.update(ids or list(db.scalars(select(Account.id))))
        db.info["balance_fresh"] = fresh
    except Exception as exc:  # noqa: BLE001 - the curve is informational; never fail a sync
        db.rollback()
        log.warning("balance rebuild failed: %s", exc)


# ------------------------------------------------------------------ read for the dashboard
def has_points(db: Session, account_id: int | None, ccy: str) -> bool:
    if account_id is None:
        return not missing_accounts(db, ccy)
    q = select(BalancePoint.id).where(BalancePoint.currency == ccy)
    if account_id:
        q = q.where(BalancePoint.account_id == account_id)
    return db.scalar(q.limit(1)) is not None


def series(db: Session, account_id: int | None, ccy: str, start: date | None = None, end: date | None = None) -> dict | None:
    """Summed (All) or single-account daily series, plus the latest point and broker check."""
    ccy = (ccy or "USD").upper()
    q = select(BalancePoint).where(BalancePoint.currency == ccy)
    if account_id:
        q = q.where(BalancePoint.account_id == account_id)
    pts = list(db.scalars(q.order_by(BalancePoint.day)))
    if not pts:
        return None
    by_acct: dict[int, dict[date, BalancePoint]] = defaultdict(dict)
    for p in pts:
        by_acct[p.account_id][p.day] = p
    days = sorted({p.day for p in pts})
    last_seen: dict[int, BalancePoint] = {}
    out = {"labels": [], "balance": [], "net_deposits": [], "missing": []}
    latest = None
    gap_days, gap_syms = 0, set()
    for d in days:
        for aid, m in by_acct.items():
            if d in m:
                last_seen[aid] = m[d]
        cur = list(last_seen.values())
        agg = {k: sum(getattr(p, k) or 0.0 for p in cur) for k in
               ("net_deposits", "realized_cum", "dividends_cum", "other_cum", "unrealized", "balance")}
        miss = sorted({s for p in cur if p.day == d and p.missing for s in p.missing.split(",")})
        latest = {**agg, "day": d, "missing": miss}
        if (start and d < start) or (end and d > end):
            continue
        out["labels"].append(d.isoformat())
        out["balance"].append(round(agg["balance"], 2))
        out["net_deposits"].append(round(agg["net_deposits"], 2))
        out["missing"].append(", ".join(miss) if miss else None)
        if miss:
            gap_days += 1
            gap_syms.update(miss)
    out["latest"] = latest
    out["total_return"] = round(latest["balance"] - latest["net_deposits"], 2)
    out["gap_days"], out["gap_symbols"] = gap_days, sorted(gap_syms)
    out["computed_at"] = max(p.computed_at for p in pts if p.computed_at) if any(p.computed_at for p in pts) else None
    out["check"] = check(db, list(by_acct), ccy, latest["balance"])
    _overlay_ibkr_nav(db, account_id, out)
    return out


def _overlay_ibkr_nav(db: Session, account_id: int | None, out: dict) -> None:
    """Replace the curve with stored IBKR NAV inside the dates IBKR reported. One account only.

    The stored BalancePoint rows, latest, total return, and the broker check stay as the journal computed them.
    """
    if not account_id or not out.get("labels"):
        return
    acct = db.get(Account, account_id)
    if acct is None or acct.source_key != "ibkr_flex":
        return
    from app.models import IbkrFlexFact
    rows = list(db.scalars(select(IbkrFlexFact).where(
        IbkrFlexFact.account_id == account_id, IbkrFlexFact.section == "nav",
        IbkrFlexFact.symbol == "", IbkrFlexFact.conid == "", IbkrFlexFact.line_key == "",
        IbkrFlexFact.amount.is_not(None)).order_by(IbkrFlexFact.day)))
    if not rows:
        return
    by_day = {row.day: float(row.amount) for row in rows}
    nav_days = sorted(by_day)
    first, last = nav_days[0], nav_days[-1]
    balance = list(out["balance"])
    index = 0
    last_nav = None
    for i, label in enumerate(out["labels"]):
        day = date.fromisoformat(label)
        while index < len(nav_days) and nav_days[index] <= day:
            last_nav = by_day[nav_days[index]]
            index += 1
        if first <= day <= last and last_nav is not None:
            balance[i] = round(last_nav, 2)
    out["balance"] = balance


def check(db: Session, account_ids: list[int], ccy: str, journal_balance: float) -> dict | None:
    """Broker cash + holdings (latest sync) vs the journal's latest balance."""
    from app import currency
    snaps = broker_snapshots(db, account_ids)
    if not snaps:
        return None
    book = currency.RateBook(db)
    today = datetime.now().date()
    need = {(s.get("currency") or "USD").upper() for s in snaps.values()}
    if need - {ccy}:
        book.ensure(need | {ccy}, today - timedelta(days=10), today)
    total, ok = 0.0, True
    for s in snaps.values():
        r = book.rate((s.get("currency") or "USD"), ccy, today)
        if r is None or s.get("total") is None:
            ok = False
            continue
        total += float(s["total"]) * r
    if not ok and not total:
        return None
    covered = len(snaps) == len(account_ids)
    diff = round(journal_balance - total, 2) if covered else None
    return {"broker_total": round(total, 2), "diff": diff, "covered": covered,
            "sources": sorted({s["source"] for s in snaps.values()}),
            "as_of": max((s.get("as_of") or "") for s in snaps.values()) or None,
            "missing_accounts": len(account_ids) - len(snaps)}


def interest_total(db: Session, account_id: int | None, ccy: str, start: date | None = None, end: date | None = None,
                   tz: str = "America/New_York") -> dict:
    """Real interest (and cash fees) the broker reported in the period, in ``ccy``."""
    from app import currency
    q = select(CashFlow).where(CashFlow.kind.in_(OTHER_KINDS))
    if account_id:
        q = q.where(CashFlow.account_id == account_id)
    rows = list(db.scalars(q))
    Z = ZoneInfo(tz)
    rows = [r for r in rows if not ((start and _local_day(r.occurred_at, Z) < start) or (end and _local_day(r.occurred_at, Z) > end))]
    out = {"interest": 0.0, "fees": 0.0, "count": 0, "unconverted": 0}
    if not rows:
        return out
    book = currency.RateBook(db)
    need = {(r.currency or "USD").upper() for r in rows}
    if need - {ccy}:
        days = [r.occurred_at.date() for r in rows]
        book.ensure(need | {ccy}, min(days), max(days))
    for r in rows:
        rate = book.rate(r.currency or "USD", ccy, r.occurred_at.date())
        if rate is None:
            out["unconverted"] += 1
            continue
        out["interest" if r.kind == "interest" else "fees"] += r.amount * rate
        out["count"] += 1
    return out
