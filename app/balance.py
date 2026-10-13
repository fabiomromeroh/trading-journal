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
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Account, BalancePoint, CashFlow, Dividend, Trade, utcnow
from app.services import get_state, set_state

log = logging.getLogger(__name__)

CURRENCIES = ("USD", "EUR")
CARRY_DAYS = 6           # weekends / holidays: the last real close stays valid this many days
T212_SUMMARY_STATE = "balance:t212:"   # + account id: latest real cash / invested / total (no secrets)
FLOW_KINDS = ("deposit", "withdrawal", "transfer")   # count as net deposits
OTHER_KINDS = ("interest", "fee")                     # count as return (cash the broker paid / charged)


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


def t212_flow(item: dict, account_ccy: str | None) -> dict | None:
    """A Trading 212 cash-ledger row (/equity/history/transactions) as a cash flow, or None."""
    if not isinstance(item, dict):
        return None
    typ = str(item.get("type") or item.get("transactionType") or "").upper().replace(" ", "_")
    kind = _T212_KINDS.get(typ)
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
        raw = get_state(db, PORTFOLIO_STATE + str(aid))
        if raw:
            d = json.loads(raw)
            out[aid] = {"total": d.get("value"), "cash": d.get("cash"), "invested": d.get("market_value"),
                        "currency": "USD", "as_of": d.get("as_of"), "source": "SnapTrade"}
    return out


# ------------------------------------------------------------------ daily closes
def _yahoo_closes(db: Session, trade, start: date, end: date) -> dict[date, float]:
    from app import prices
    from app.config import get_settings
    if get_settings().price_provider in ("none", "off", ""):
        return {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = datetime.combine(start - timedelta(days=CARRY_DAYS + 1), datetime.min.time())
    cut = datetime.combine(min(end, now.date()) - timedelta(days=3), datetime.min.time())
    out: dict[date, float] = {}
    spans = [(s, cut), (max(s, cut - timedelta(days=7)), now)] if cut > s else [(s, now)]
    for a, b in spans:
        try:
            candles = prices._fetch(db, "yahoo", trade, "1d", a, b, now)
        except Exception as exc:  # noqa: BLE001 - a missing price is a gap, never a failed sync
            log.info("balance: no daily closes for %s: %s", getattr(trade, "underlying", "?"), exc)
            continue
        for c in candles or []:
            out[datetime.fromtimestamp(int(c["time"]), timezone.utc).date()] = float(c["close"])
    return out


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
    try:
        with db.begin_nested():
            ids = sorted(set(account_ids) | set(missing_accounts(db)))
            rebuild(db, ids or None)
    except Exception as exc:  # noqa: BLE001 - the curve is informational; never fail a sync
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
    return out


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
