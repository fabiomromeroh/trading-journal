"""Schwab.com transaction history export (Accounts > History > Transactions > Export > CSV).

Known layout (2022-2026), optionally preceded by a title row such as
  "Transactions  for account ...285 as of 10/07/2026 08:53:37 AM ET"
then
  "Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
and optionally a trailing "Transactions Total" row.

* Date: "MM/DD/YYYY" or "MM/DD/YYYY as of MM/DD/YYYY" (the "as of" date is the trade date).
* Options symbol: "AAPL 01/17/2025 150.00 C"; Price is per share; Quantity is contracts.
* Times are not included, so executions are stamped at 16:00 ET with time_known=False.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time

from app.importers.base import ParseResult, assign_stable_ids, parse_money
from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol
from app.timeutil import ET, local_to_utc_naive

FORMAT = "schwab_csv"
HEADER = ["date", "action", "symbol", "description", "quantity", "price", "fees & comm", "amount"]

# action -> (side, position_effect, kind)
TRADE_ACTIONS: dict[str, tuple[str | None, str | None, str]] = {
    "buy": ("BUY", None, "TRADE"),
    "sell": ("SELL", "CLOSE", "TRADE"),
    "sell short": ("SELL", "OPEN", "TRADE"),
    "buy to cover": ("BUY", "CLOSE", "TRADE"),
    "buy to open": ("BUY", "OPEN", "TRADE"),
    "buy to close": ("BUY", "CLOSE", "TRADE"),
    "sell to open": ("SELL", "OPEN", "TRADE"),
    "sell to close": ("SELL", "CLOSE", "TRADE"),
    "expired": (None, "CLOSE", "EXPIRATION"),
    "assigned": (None, "CLOSE", "ASSIGNMENT"),
    "exchange or exercise": (None, "CLOSE", "EXERCISE"),
}

_OPT_SYM_RE = re.compile(r"^([A-Z0-9./]+)\s+(\d{1,2}/\d{1,2}/\d{4})\s+([\d.]+)\s+([CP])$")
_OPT_DESC_RE = re.compile(r"\b(CALL|PUT)\b.*?\$([\d.]+)\s+EXP\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
_ACCT_RE = re.compile(r"account\s+([.\w-]*\d{3,})", re.I)


def _norm_header(row: list[str]) -> list[str]:
    return [c.strip().strip('"').lower() for c in row]


def detect(text: str) -> bool:
    for row in list(csv.reader(io.StringIO(text)))[:15]:
        h = _norm_header(row)
        if len(h) >= 8 and h[:8] == HEADER:
            return True
    return False


def _parse_date(s: str) -> date | None:
    found = _DATE_RE.findall(s or "")
    if not found:
        return None
    return datetime.strptime(found[-1], "%m/%d/%Y").date()  # "as of" date wins


def _parse_option(symbol: str, description: str):
    m = _OPT_SYM_RE.match(symbol.strip().upper())
    if m:
        und, exp, strike, cp = m.groups()
        return und, datetime.strptime(exp, "%m/%d/%Y").date(), ("CALL" if cp == "C" else "PUT"), float(strike)
    m = _OPT_DESC_RE.search(description or "")
    if m and symbol.strip():
        cp, strike, exp = m.groups()
        fmt = "%m/%d/%Y" if len(exp.split("/")[-1]) == 4 else "%m/%d/%y"
        und = symbol.strip().split()[0].upper()
        return und, datetime.strptime(exp, fmt).date(), cp.upper(), float(strike)
    return None


def parse(text: str, **_) -> ParseResult:
    res = ParseResult(format=FORMAT)
    rows = list(csv.reader(io.StringIO(text)))
    start = None
    for i, row in enumerate(rows[:15]):
        if row and _ACCT_RE.search(row[0]):
            res.account_hint = _ACCT_RE.search(row[0]).group(1)
        h = _norm_header(row)
        if len(h) >= 8 and h[:8] == HEADER:
            start = i + 1
            break
    if start is None:
        raise ValueError("Schwab CSV header row not found")

    parsed: list[tuple[int, ExecRecord]] = []
    dates_in_order: list[date] = []
    for idx, row in enumerate(rows[start:]):
        if not row or not any(c.strip() for c in row):
            continue
        cells = (row + [""] * 8)[:8]
        d_s, action, symbol, desc, qty_s, price_s, fees_s, amount_s = [c.strip() for c in cells]
        if d_s.lower().startswith("transactions total"):
            continue
        res.rows_total += 1
        d = _parse_date(d_s)
        if d is None:
            res.skipped["unparseable date"] += 1
            continue
        dates_in_order.append(d)
        key = action.lower()
        if key not in TRADE_ACTIONS:
            res.skipped[action or "(blank)"] += 1
            continue
        side, effect, kind = TRADE_ACTIONS[key]
        qty = abs(parse_money(qty_s) or 0.0)
        if qty <= 0 or not symbol:
            res.skipped[f"{action} (no quantity/symbol)"] += 1
            continue
        price = parse_money(price_s) or 0.0
        fees = abs(parse_money(fees_s) or 0.0)
        amount = parse_money(amount_s)
        opt = _parse_option(symbol, desc)
        if opt:
            und, exp, cp, strike = opt
            rec = ExecRecord(
                external_id="", symbol=option_symbol(und, exp, cp, strike), underlying=und,
                asset_type="OPTION", option_type=cp, strike=strike, expiration=exp,
                multiplier=OPTION_MULTIPLIER, side=side, quantity=qty, price=price, fees=fees,
                executed_at=local_to_utc_naive(d, time(16, 0), ET), time_known=False,
                position_effect=effect, kind=kind, description=desc[:300], trade_date=d,
                raw={"row": cells},
            )
            if kind in ("EXPIRATION", "ASSIGNMENT", "EXERCISE"):
                rec.price = 0.0
        else:
            if kind != "TRADE":
                # Stock leg of an assignment/exercise: infer side from cash flow.
                kind, effect = "TRADE", None
                side = "BUY" if (amount or 0) < 0 else "SELL"
            rec = ExecRecord(
                external_id="", symbol=symbol.upper(), underlying=symbol.upper(), asset_type="STOCK",
                side=side, quantity=qty, price=price, fees=fees,
                executed_at=local_to_utc_naive(d, time(16, 0), ET), time_known=False,
                position_effect=effect, kind=kind, description=desc[:300], trade_date=d,
                raw={"row": cells},
            )
        parsed.append((idx, rec))

    # Schwab exports newest-first by default; order rows oldest-first for sequencing.
    descending = len(dates_in_order) > 1 and dates_in_order[0] > dates_in_order[-1]
    n = len(parsed)
    for i, (_, rec) in enumerate(parsed):
        rec.seq = (n - i) if descending else i
    records = [r for _, r in sorted(parsed, key=lambda p: p[1].seq)]
    assign_stable_ids("schwabcsv", records)
    res.records = records
    return res
