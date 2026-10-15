"""Real broker cash dividends: store, convert, attach to trades, dashboard total.

Amounts are never estimated. The stored figure is the net cash the broker paid (T212
``amount`` / SnapTrade or Schwab activity amount). GBX is scaled to pounds the same way
fills are. Display conversion uses the broker FX when the row has one, otherwise the ECB
rate on the payment date (same rule as other money fields).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Account, Dividend, Trade, utcnow
from app.timeutil import local_to_utc_naive


@dataclass
class DividendIn:
    """One broker dividend ready to store. ``amount`` is net cash in ``currency`` (never GBX)."""
    external_id: str
    symbol: str
    paid_on: datetime
    amount: float
    currency: str | None = None
    amount_account: float | None = None
    account_currency: str | None = None
    fx_to_account: float | None = None
    quantity: float | None = None
    gross_per_share: float | None = None
    kind: str | None = None
    ticker: str | None = None
    instrument_id: int | None = None
    raw: dict | None = None


def _ccy(v: str | None) -> str | None:
    v = (v or "").upper() or None
    return "GBP" if v == "GBX" else v


def scale_gbx(amount: float | None, ccy: str | None) -> tuple[float | None, str | None]:
    """Pence -> pounds when the broker quoted GBX. Other currencies are unchanged."""
    if amount is None:
        return None, _ccy(ccy)
    if (ccy or "").upper() == "GBX":
        return amount / 100.0, "GBP"
    return amount, _ccy(ccy)


def ingest(db: Session, account_id: int, source: str, rows: list[DividendIn]) -> dict:
    """Upsert by (account, source, broker id). Returns {inserted, merged}."""
    if not rows:
        return {"inserted": 0, "merged": 0}
    ids = [r.external_id for r in rows]
    existing = {
        e.external_id: e
        for e in db.scalars(
            select(Dividend).where(
                Dividend.account_id == account_id, Dividend.source == source, Dividend.external_id.in_(ids)))
    }
    ins = mer = 0
    for r in rows:
        payload = json.dumps(r.raw) if r.raw else None
        acct_amt = r.amount_account if r.amount_account is not None else r.amount
        acct_ccy = r.account_currency or r.currency
        row = existing.get(r.external_id)
        fields = dict(
            symbol=r.symbol, paid_on=r.paid_on, amount=r.amount, currency=r.currency,
            amount_account=acct_amt, account_currency=acct_ccy, fx_to_account=r.fx_to_account,
            quantity=r.quantity, gross_per_share=r.gross_per_share, kind=r.kind, ticker=r.ticker,
            instrument_id=r.instrument_id, raw=payload,
        )
        if row is None:
            db.add(Dividend(account_id=account_id, source=source, external_id=r.external_id, **fields))
            ins += 1
            continue
        changed = False
        for k, v in fields.items():
            if getattr(row, k) != v:
                setattr(row, k, v)
                changed = True
        if changed:
            mer += 1
    db.flush()
    return {"inserted": ins, "merged": mer}


def cash(div: Dividend) -> tuple[float, str]:
    """Net cash the broker paid, in the currency it was paid in."""
    if div.amount_account is not None and div.account_currency:
        return div.amount_account, div.account_currency.upper()
    return div.amount, (div.currency or "USD").upper()


def convert_one(div: Dividend, to_ccy: str, book) -> float | None:
    """Real paid cash in ``to_ccy``. None when a required rate is missing (do not guess the amount)."""
    amt, ccy = cash(div)
    to_ccy = (to_ccy or ccy).upper()
    ccy = (ccy or to_ccy).upper()
    if ccy == to_ccy:
        return amt
    fx = div.fx_to_account
    instr = (div.currency or "").upper()
    acct = (div.account_currency or "").upper()
    if fx:
        if instr == to_ccy and acct == ccy:
            return amt / fx if fx else None
        if acct == to_ccy and instr == ccy:
            return amt * fx
    if book is None:
        return None
    day = div.paid_on.date() if div.paid_on else datetime.now().date()
    r = book.rate(ccy, to_ccy, day)
    return None if not r else amt * r


def _book(db: Session, rows: list[Dividend], extra_ccys: set[str]):
    from app import currency
    if not rows:
        return None
    have = {cash(d)[1] for d in rows}
    extra = {c.upper() for c in extra_ccys if c}
    if len(have) == 1 and have >= extra:
        return None  # the paid cash is already in every currency the caller asked for
    ccys = extra | have | {(d.currency or "") for d in rows}
    days = [d.paid_on.date() for d in rows if d.paid_on]
    start, end = (min(days), max(days)) if days else (datetime.now().date(), datetime.now().date())
    book = currency.RateBook(db)
    book.ensure(ccys, start, end)
    return book


def for_trade(db: Session, t, display: str | None = None) -> dict:
    """Dividends for one stock trade while it was open. Options get nothing.

    Window: first entry ``opened_at`` through final exit ``closed_at``, or now if still open.
    """
    from app import currency
    raw = currency.raw(t)
    blank = {"native": None, "display": None, "count": 0}
    if getattr(raw, "asset_type", None) == "OPTION":
        return blank
    end = raw.closed_at or utcnow()
    cond = [
        Dividend.account_id == raw.account_id,
        Dividend.paid_on >= raw.opened_at,
        Dividend.paid_on <= end,
        Dividend.symbol == raw.symbol,
    ]
    rows = list(db.scalars(select(Dividend).where(*cond)))
    if raw.instrument_id:
        extra = list(db.scalars(select(Dividend).where(
            Dividend.account_id == raw.account_id, Dividend.instrument_id == raw.instrument_id,
            Dividend.paid_on >= raw.opened_at, Dividend.paid_on <= end,
            Dividend.symbol != raw.symbol)))
        rows.extend(extra)
    if not rows:
        return blank
    native_ccy = currency.trade_ccy(raw)
    display = (display or currency.get_display(db)).upper()
    book = _book(db, rows, {native_ccy, display})
    native = disp = 0.0
    miss_n = miss_d = False
    for d in rows:
        n = convert_one(d, native_ccy, book)
        if n is None:
            miss_n = True
        else:
            native += n
        if display == native_ccy:
            continue
        x = convert_one(d, display, book)
        if x is None:
            miss_d = True
        else:
            disp += x
    if display == native_ccy:
        disp, miss_d = native, miss_n
    return {
        "native": None if miss_n and native == 0 else native,
        "display": None if miss_d else disp,
        "count": len(rows),
    }


def for_trades(db: Session, trades, display: str | None = None) -> dict:
    """``for_trade`` for a page of trades with one dividend query and one rate book: trade id -> result."""
    from app import currency
    raws = [(t.id, currency.raw(t)) for t in trades]
    raws = [(i, r) for i, r in raws if getattr(r, "asset_type", None) != "OPTION"]
    blank = {"native": None, "display": None, "count": 0}
    out = {t.id: dict(blank) for t in trades}
    if not raws:
        return out
    accounts = {r.account_id for _, r in raws}
    start = min(r.opened_at for _, r in raws)
    rows = list(db.scalars(select(Dividend).where(Dividend.account_id.in_(accounts), Dividend.paid_on >= start)))
    if not rows:
        return out
    display = (display or currency.get_display(db)).upper()
    now = utcnow()
    picked = {}
    ccys = {display}
    for tid, raw in raws:
        end = raw.closed_at or now
        mine = [d for d in rows if d.account_id == raw.account_id and d.paid_on is not None
                and raw.opened_at <= d.paid_on <= end
                and (d.symbol == raw.symbol or (raw.instrument_id and d.instrument_id == raw.instrument_id))]
        if mine:
            picked[tid] = (raw, mine)
            ccys.add(currency.trade_ccy(raw))
    if not picked:
        return out
    book = _book(db, [d for _, m in picked.values() for d in m], ccys)
    for tid, (raw, mine) in picked.items():
        out[tid] = _sum(mine, currency.trade_ccy(raw), display, book)
    return out


def _sum(rows, native_ccy: str, display: str, book) -> dict:
    native = disp = 0.0
    miss_n = miss_d = False
    for d in rows:
        n = convert_one(d, native_ccy, book)
        if n is None:
            miss_n = True
        else:
            native += n
        if display == native_ccy:
            continue
        x = convert_one(d, display, book)
        if x is None:
            miss_d = True
        else:
            disp += x
    if display == native_ccy:
        disp, miss_d = native, miss_n
    return {"native": None if miss_n and native == 0 else native, "display": None if miss_d else disp,
            "count": len(rows)}


def account_ids(db: Session, account_id: int | None) -> list[int]:
    if account_id:
        return [account_id]
    return list(db.scalars(select(Account.id)))


def in_range(db: Session, account_id: int | None, start: date | None = None, end: date | None = None,
             tz: str = "America/New_York") -> list[Dividend]:
    ids = account_ids(db, account_id)
    if not ids:
        return []
    stmt = select(Dividend).where(Dividend.account_id.in_(ids))
    if start:
        stmt = stmt.where(Dividend.paid_on >= local_to_utc_naive(start, time(0, 0), tz))
    if end:
        stmt = stmt.where(Dividend.paid_on < local_to_utc_naive(end + timedelta(days=1), time(0, 0), tz))
    return list(db.scalars(stmt))


def total(db: Session, account_id: int | None, display: str, start: date | None = None,
          end: date | None = None, tz: str = "America/New_York") -> float:
    """Sum of real dividends in the display currency for the account switcher (and optional date window)."""
    rows = in_range(db, account_id, start, end, tz)
    if not rows:
        return 0.0
    display = display.upper()
    if {cash(d)[1] for d in rows} <= {display}:
        return sum(cash(d)[0] for d in rows)
    book = _book(db, rows, {display})
    s = 0.0
    for d in rows:
        v = convert_one(d, display, book)
        if v is not None:
            s += v
    return s
