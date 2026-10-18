"""Parsed IBKR Flex numbers for one account and day. No raw XML, no broker calls."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import Account, IbkrFlexFact, Instrument, Trade

_BASE_CCY = {"BASE_SUMMARY", "BASE_CURRENCY", "BASECURRENCY", "BASE"}
_POS_OK = {"", "SUMMARY"}


def _day(value, fallback: date | None = None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if len(raw) >= 10 and raw[4] == "-":
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return fallback
    digits = re.sub(r"\D", "", raw)[:8]
    if len(digits) != 8:
        return fallback
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return fallback


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _s(value) -> str:
    return str(value or "").strip()


def _floats(row: dict, skip: set[str] | None = None) -> dict[str, float]:
    skip = skip or set()
    out: dict[str, float] = {}
    for key, value in (row or {}).items():
        if key in skip or "date" in key.lower() or key.lower() in {"conid", "accountid", "symbol"}:
            continue
        number = _num(value)
        if number is None:
            continue
        out[key] = number
    return out


def realized_amount(row: dict) -> float | None:
    """IBKR realized P&L. A positive number on a *loss* field is a loss."""
    for key in ("totalRealizedPnl", "realizedPnl", "realized", "totalRealizedPNL", "realizedPNL"):
        if key in (row or {}) and row.get(key) not in (None, ""):
            number = _num(row.get(key))
            if number is not None:
                return number
    total = 0.0
    found = False
    for key, value in (row or {}).items():
        low = key.lower()
        if "realized" not in low:
            continue
        number = _num(value)
        if number is None:
            continue
        if "loss" in low and number > 0:
            number = -number
        total += number
        found = True
    return total if found else None


def _line_key(row: dict, *parts: str) -> str:
    for key in ("transactionID", "tradeID", "actionID"):
        token = _s(row.get(key))
        if token:
            return token[:80]
    raw = "|".join(p for p in parts if p)
    if not raw:
        blob = json.dumps(row, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:20]
    if len(raw) <= 80:
        return raw
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _stmt_day(parsed) -> date | None:
    return _day(getattr(parsed, "to_date", None)) or _day(getattr(parsed, "from_date", None))


class _Bucket:
    def __init__(self):
        self.rows: dict[tuple, dict] = {}

    def put(self, day: date | None, currency: str, symbol: str, conid: str, line_key: str,
            amount: float | None, numbers: dict | None) -> None:
        if day is None:
            return
        key = (day, currency or "", (symbol or "").upper(), conid or "", (line_key or "")[:80])
        self.rows[key] = {"amount": amount, "numbers": numbers or {}}


def _interesting_change(row: dict) -> bool:
    return any(_num(row.get(k)) is not None for k in (
        "startingValue", "endingValue", "mtm", "realized", "depositsWithdrawals", "dividends"))


def _collect(parsed) -> dict[str, _Bucket]:
    buckets = {name: _Bucket() for name in (
        "cash", "nav", "change_nav", "funds", "interest", "div_accrual", "performance", "position")}
    fallback = _stmt_day(parsed)

    for row in list(getattr(parsed, "cash_report", None) or []):
        lod = _s(row.get("levelOfDetail") or row.get("LevelOfDetail")).upper()
        if lod and lod not in {"", "SUMMARY", "CURRENCY", "BASE_CURRENCY", "BASECURRENCY"}:
            continue
        ending = _num(row.get("endingCash") if "endingCash" in row else row.get("endingSettledCash") or row.get("cash"))
        if ending is None and "endingCash" in row:
            ending = _num(row.get("endingCash"))
        if ending is None:
            continue
        day = _day(row.get("toDate") or row.get("reportDate") or row.get("fromDate"), fallback)
        currency = _s(row.get("currency") or row.get("fxCurrency")).upper()
        buckets["cash"].put(day, currency, "", "", "", ending, _floats(row, {"endingCash"}))

    nav_by_day: dict[date, dict] = {}
    for row in list(getattr(parsed, "equity_rows", None) or []):
        total = _num(row.get("total"))
        if total is None:
            continue
        day = _day(row.get("reportDate"), fallback)
        if day is None:
            continue
        nav_by_day[day] = row
    for row in list(getattr(parsed, "nav", None) or []):
        total = _num(row.get("total") or row.get("totalLong") or row.get("equity"))
        if total is None:
            continue
        day = _day(row.get("reportDate") or row.get("toDate") or row.get("fromDate"), fallback)
        if day is None:
            continue
        nav_by_day[day] = row
    for day, row in nav_by_day.items():
        total = _num(row.get("total") or row.get("totalLong") or row.get("equity"))
        currency = _s(row.get("currency")).upper()
        invested = _num(row.get("invested"))
        if invested is None:
            invested = _num(row.get("stock") or row.get("securities"))
        numbers = {key: value for key, value in (
            ("cash", _num(row.get("cash"))), ("invested", invested), ("total", total)) if value is not None}
        buckets["nav"].put(day, currency, "", "", "", total, numbers)

    for row in list(getattr(parsed, "change_nav", None) or []):
        if not _interesting_change(row):
            continue
        day = _day(row.get("toDate") or row.get("reportDate"), fallback)
        buckets["change_nav"].put(
            day, _s(row.get("currency")).upper(), "", "", _line_key(row, _s(row.get("fromDate"))),
            _num(row.get("endingValue")), _floats(row))

    for row in list(getattr(parsed, "funds", None) or []):
        amount = _num(row.get("amount"))
        if amount is None:
            credit, debit = _num(row.get("credit")), _num(row.get("debit"))
            if credit is None and debit is None:
                amount = _num(row.get("balance"))
            else:
                amount = (credit or 0.0) - (debit or 0.0)
        if amount is None:
            continue
        day = _day(row.get("reportDate") or row.get("dateTime") or row.get("date") or row.get("tradeDate"), fallback)
        buckets["funds"].put(
            day, _s(row.get("currency")).upper(), _s(row.get("symbol")).upper(), _s(row.get("conid")),
            _line_key(row, _s(row.get("date") or row.get("reportDate")), _s(row.get("description"))[:40]),
            amount, _floats(row))

    for row in list(getattr(parsed, "interest_accruals", None) or []):
        ending = _num(row.get("endingAccrualBalance"))
        if ending is None:
            ending = _num(row.get("interestAccrued"))
        if ending is None and _num(row.get("startingAccrualBalance")) is None:
            continue
        day = _day(row.get("toDate") or row.get("reportDate"), fallback)
        buckets["interest"].put(
            day, _s(row.get("currency")).upper(), "", "", _line_key(row, _s(row.get("fromDate"))),
            ending, _floats(row))

    for row in list(getattr(parsed, "dividend_accruals", None) or []):
        amount = _num(row.get("netAmount"))
        if amount is None:
            amount = _num(row.get("grossAmount"))
        if amount is None and _num(row.get("quantity")) is None:
            continue
        day = _day(row.get("payDate") or row.get("exDate") or row.get("reportDate") or row.get("toDate"), fallback)
        buckets["div_accrual"].put(
            day, _s(row.get("currency")).upper(), _s(row.get("symbol")).upper(), _s(row.get("conid")),
            _line_key(row, _s(row.get("code") or row.get("type")), _s(row.get("exDate"))),
            amount, _floats(row))

    perf_rows = []
    for row in list(getattr(parsed, "performance", None) or []):
        realized = realized_amount(row)
        if realized is None:
            continue
        perf_rows.append((row, realized))
    detailed = [item for item in perf_rows if _s(item[0].get("symbol") or item[0].get("underlyingSymbol") or item[0].get("conid"))]
    use = detailed or perf_rows
    for row, realized in use:
        day = _day(row.get("toDate") or row.get("reportDate"), fallback)
        buckets["performance"].put(
            day, _s(row.get("currency")).upper(),
            _s(row.get("symbol") or row.get("underlyingSymbol")).upper(), _s(row.get("conid")),
            _line_key(row, _s(row.get("fromDate"))), realized, _floats(row))

    pos_rows = []
    for row in list(getattr(parsed, "positions", None) or []):
        lod = _s(row.get("levelOfDetail") or row.get("LevelOfDetail")).upper()
        if lod and lod not in _POS_OK:
            continue
        cat = _s(row.get("assetCategory")).upper()
        if cat and cat not in {"STK", "ETF", "FUND"}:
            continue
        qty = _num(row.get("position") if row.get("position") not in (None, "") else row.get("quantity"))
        if qty is None:
            continue
        symbol = _s(row.get("symbol")).upper()
        if not symbol or " " in symbol:
            continue
        day = _day(row.get("reportDate"), fallback)
        pos_rows.append((day, symbol, _s(row.get("conid")), qty))
    if pos_rows or getattr(parsed, "has_positions", False):
        summed: dict[tuple, float] = {}
        for day, symbol, conid, qty in pos_rows:
            if day is None:
                continue
            key = (day, symbol, conid)
            summed[key] = summed.get(key, 0.0) + float(qty)
        days = {day for (day, _sbl, _con) in summed} or ({fallback} if fallback else set())
        for (day, symbol, conid), qty in summed.items():
            buckets["position"].put(day, "", symbol, conid, "", qty, {})
        for day in days:
            if day is not None:
                buckets["position"].put(day, "", "", "", "__snapshot__", 0.0, {})
    return buckets


def store_parsed(db: Session, account_id: int, parsed) -> int:
    """Replace this statement's days for each section it actually carries. Idempotent."""
    buckets = _collect(parsed)
    written = 0
    for section, bucket in buckets.items():
        if not bucket.rows:
            continue
        days = {key[0] for key in bucket.rows}
        db.execute(delete(IbkrFlexFact).where(
            IbkrFlexFact.account_id == account_id,
            IbkrFlexFact.section == section,
            IbkrFlexFact.day.in_(days)))
        for (day, currency, symbol, conid, line_key), payload in bucket.rows.items():
            db.add(IbkrFlexFact(
                account_id=account_id, section=section, day=day, currency=currency or "",
                symbol=symbol or "", conid=conid or "", line_key=line_key or "",
                amount=payload["amount"],
                numbers=json.dumps(payload["numbers"], separators=(",", ":")) if payload["numbers"] else None,
            ))
            written += 1
    if written:
        db.flush()
    return written


def latest_position_map(db: Session, account_id: int) -> dict[str, float] | None:
    """Open quantity by the conid's current symbol, from the newest stored snapshot.

    ``None`` means no snapshot has been stored (the caller may use the file just read).
    ``{}`` means the newest snapshot is present and flat.
    """
    day = db.scalar(select(func.max(IbkrFlexFact.day)).where(
        IbkrFlexFact.account_id == account_id, IbkrFlexFact.section == "position"))
    if day is None:
        return None
    rows = list(db.scalars(select(IbkrFlexFact).where(
        IbkrFlexFact.account_id == account_id, IbkrFlexFact.section == "position",
        IbkrFlexFact.day == day)))
    conids = {r.conid for r in rows if r.conid}
    symbols: dict[str, str] = {}
    if conids:
        for inst in db.scalars(select(Instrument).where(
                Instrument.source_key == "ibkr_flex", Instrument.conid.in_(conids))):
            if inst.conid and inst.symbol:
                symbols[inst.conid] = inst.symbol.upper()
    labelled: list[tuple[str, str, float]] = []
    for row in rows:
        if row.line_key == "__snapshot__":
            continue
        symbol = symbols.get(row.conid) or (row.symbol or "").upper()
        if not symbol:
            continue
        labelled.append((symbol, row.conid or "", float(row.amount or 0.0)))
    shared: dict[str, set[str]] = {}
    for symbol, conid, _qty in labelled:
        if conid:
            shared.setdefault(symbol, set()).add(conid)
    out: dict[str, float] = {}
    for symbol, conid, qty in labelled:
        key = f"{symbol}#{conid}" if conid and len(shared.get(symbol) or ()) > 1 else symbol
        out[key] = out.get(key, 0.0) + qty
    return out


def _money(number: float) -> str:
    return f"${number:,.2f}"


def _open_journal(db: Session, account_id: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for trade in db.scalars(select(Trade).where(Trade.account_id == account_id, Trade.status == "OPEN")):
        symbol = (trade.symbol or "").upper()
        if not symbol or " " in symbol:
            continue
        qty = abs(float(trade.open_quantity or 0.0))
        if (trade.direction or "") == "SHORT":
            qty = -qty
        out[symbol] = out.get(symbol, 0.0) + qty
    return out


def _journal_realized(db: Session, account_id: int) -> tuple[float, dict[str, float]]:
    total = 0.0
    by_symbol: dict[str, float] = {}
    rows = db.execute(select(Trade.symbol, Trade.net_pnl).where(
        Trade.account_id == account_id, Trade.status == "CLOSED")).all()
    for symbol, net in rows:
        value = float(net or 0.0)
        total += value
        key = (symbol or "").upper()
        if key:
            by_symbol[key] = by_symbol.get(key, 0.0) + value
    return total, by_symbol


def account_statement(db: Session, account_id: int) -> dict:
    """Cash, NAV, and reconciliation for the account panel. A handful of queries, no broker call."""
    acct = db.get(Account, account_id)
    latest = dict(db.execute(select(IbkrFlexFact.section, func.max(IbkrFlexFact.day)).where(
        IbkrFlexFact.account_id == account_id,
        IbkrFlexFact.section.in_(("cash", "nav", "position")),
    ).group_by(IbkrFlexFact.section)).all())
    cash_line = nav_line = None
    hint = None
    if not latest.get("cash") and not latest.get("nav"):
        hint = "Add the Cash Report / NAV sections to your Flex Query"
    wanted = [(section, day) for section, day in latest.items() if day is not None]
    rows: list[IbkrFlexFact] = []
    if wanted:
        rows = list(db.scalars(select(IbkrFlexFact).where(
            IbkrFlexFact.account_id == account_id,
            or_(*[and_(IbkrFlexFact.section == section, IbkrFlexFact.day == day) for section, day in wanted]))))
    by_section: dict[str, list[IbkrFlexFact]] = defaultdict(list)
    for row in rows:
        if latest.get(row.section) == row.day:
            by_section[row.section].append(row)
    if latest.get("cash"):
        cash_rows = [r for r in by_section["cash"] if (r.currency or "").upper() not in (_BASE_CCY | {""})]
        if not cash_rows:
            cash_rows = list(by_section["cash"])
        parts = []
        for row in sorted(cash_rows, key=lambda r: r.currency or ""):
            label = row.currency or "BASE"
            parts.append(f"{label} {float(row.amount or 0.0):,.2f}")
        if parts:
            cash_line = f"Cash: {' · '.join(parts)} as of {latest['cash'].isoformat()}, end of day"
    if latest.get("nav"):
        nav_rows = [r for r in by_section["nav"] if not r.symbol and not r.conid and not r.line_key]
        acct_ccy = ""
        acct_row = db.get(Account, account_id)
        if acct_row is not None and acct_row.currency:
            acct_ccy = acct_row.currency.upper()
        pick = None
        for token in ("", "BASE", acct_ccy):
            for row in nav_rows:
                if (row.currency or "").upper() == token and row.amount is not None:
                    pick = row
                    break
            if pick is not None:
                break
        if pick is None and nav_rows:
            pick = nav_rows[-1]
        elif pick is None and by_section["nav"]:
            pick = by_section["nav"][-1]
        if pick is not None and pick.amount is not None:
            nav_line = f"NAV {float(pick.amount):,.2f} as of {latest['nav'].isoformat()}, end of day"

    ibkr_rows = list(db.scalars(select(IbkrFlexFact).where(
        IbkrFlexFact.account_id == account_id, IbkrFlexFact.section == "performance",
        IbkrFlexFact.amount.is_not(None))))
    ibkr_total = None
    ibkr_by: dict[str, float] = {}
    if ibkr_rows:
        ibkr_total = 0.0
        for row in ibkr_rows:
            ibkr_total += float(row.amount or 0.0)
            if row.symbol:
                ibkr_by[row.symbol.upper()] = ibkr_by.get(row.symbol.upper(), 0.0) + float(row.amount or 0.0)
    journal_total, journal_by = _journal_realized(db, account_id)
    recon_bits = []
    if ibkr_total is not None:
        diff = journal_total - ibkr_total
        sign = "+" if diff >= 0 else "-"
        recon_bits.append(
            f"Realized P&L: journal {_money(journal_total)} vs IBKR {_money(ibkr_total)} "
            f"(diff {sign}${abs(diff):,.2f})")
        mismatches = []
        for symbol in sorted(set(journal_by) | set(ibkr_by)):
            if abs(journal_by.get(symbol, 0.0) - ibkr_by.get(symbol, 0.0)) > 0.05:
                mismatches.append(symbol)
        if mismatches:
            recon_bits.append("mismatch: " + ", ".join(mismatches[:8]))
    if latest.get("position") is not None:
        remapped = latest_position_map(db, account_id) or {}
        journal = _open_journal(db, account_id)
        symbols = sorted({s for s in set(remapped) | set(journal)
                          if abs(remapped.get(s, 0.0)) > 1e-4 or abs(journal.get(s, 0.0)) > 1e-4})
        if not symbols:
            recon_bits.append("Positions: no open positions")
        else:
            matched = 0
            for symbol in symbols:
                left, right = journal.get(symbol, 0.0), remapped.get(symbol, 0.0)
                tol = max(1e-4, 1e-4 * max(abs(left), abs(right), 1.0))
                if abs(left - right) <= tol:
                    matched += 1
            recon_bits.append(f"Positions: {matched}/{len(symbols)} match")
    recon_line = " · ".join(recon_bits) if recon_bits else None
    return {
        "cash_line": cash_line, "nav_line": nav_line, "recon_line": recon_line, "hint": hint,
        "currency": (acct.currency if acct is not None else None) or "USD",
    }
