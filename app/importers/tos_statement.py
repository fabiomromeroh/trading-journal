"""thinkorswim Account Statement export (Monitor > Account Statement > Export to file).

Only the "Account Trade History" section is read. It has real execution times:
  ,Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type
  ,9/27/24 10:31:05,SINGLE,BUY,+100,TO OPEN,AAPL,,,STOCK,227.50,227.50,LMT
  ,9/27/24 10:40:12,VERTICAL,SELL,-1,TO OPEN,SPY,18 OCT 24,580,CALL,2.15,.95,LMT
  ,,,BUY,+1,TO OPEN,SPY,18 OCT 24,585,CALL,1.20,.95,LMT      <- spread leg continuation
Fees are not in this section (they are in Cash Balance), so fees import as 0; a matching
Schwab.com CSV import will fill them in. Expirations are not listed here either.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from app.config import get_settings
from app.importers.base import ParseResult, assign_stable_ids, parse_money
from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol
from app.timeutil import to_utc_naive
from zoneinfo import ZoneInfo

ET_ZONE = ZoneInfo("America/New_York")

FORMAT = "tos_statement"
_ACCT_RE = re.compile(r"Account Statement for\s+([\w.*-]*\d{3,})", re.I)
# Time zones thinkorswim might have written the file in (it uses the platform/computer setting).
TZ_CANDIDATES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                 "Europe/Dublin", "Europe/London", "Europe/Lisbon", "Europe/Madrid", "Europe/Berlin",
                 "Europe/Athens", "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Hong_Kong",
                 "Asia/Tokyo", "Australia/Sydney", "America/Sao_Paulo", "America/Mexico_City",
                 "America/Bogota", "America/Buenos_Aires", "UTC")
_EXP_RE = re.compile(r"(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})")


def detect(text: str) -> bool:
    head = text[:200000]
    return "Account Trade History" in head and "Exec Time" in head


def _parse_exp(s: str):
    m = _EXP_RE.search((s or "").upper())
    if not m:
        return None
    v = m.group(1)
    for fmt in ("%d %b %y", "%d %b %Y"):
        try:
            return datetime.strptime(v.title(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(s: str) -> datetime | None:
    for fmt in ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _session_score(times: list[datetime], tz: str) -> tuple[int, int]:
    """(fills inside the extended session 04:00-20:00 ET, fills inside regular hours 09:30-16:00 ET)."""
    zi, ext, rth = ZoneInfo(tz), 0, 0
    for t in times:
        et = t.replace(tzinfo=zi).astimezone(ET_ZONE)
        if et.weekday() >= 5:
            continue
        m = et.hour * 60 + et.minute
        ext += 240 <= m <= 1200
        rth += 570 <= m <= 960
    return ext, rth


def detect_timezone(times: list[datetime], preferred: str) -> str:
    """thinkorswim writes Exec Time in the platform's time zone (often the computer's, e.g. Dublin),
    with no zone marker. Pick the zone that puts the fills inside US market hours; keep the
    configured zone unless another one fits clearly better."""
    if not times:
        return preferred
    cands = [preferred] + [c for c in TZ_CANDIDATES if c != preferred]
    scores = {c: _session_score(times, c) for c in cands}
    best = max(cands, key=lambda c: (scores[c][0], scores[c][1], c == preferred))
    if scores[best] > scores[preferred] and (scores[best][0] > scores[preferred][0]
                                             or scores[best][1] >= scores[preferred][1] + max(2, len(times) // 5)):
        return best
    return preferred


def parse(text: str, tz: str | None = None, **_) -> ParseResult:
    res = ParseResult(format=FORMAT)
    m = _ACCT_RE.search(text[:5000])
    if m:
        res.account_hint = m.group(1)
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().strip(",").strip('"') == "Account Trade History")
    except StopIteration:
        raise ValueError("No 'Account Trade History' section found")
    section: list[str] = []
    for l in lines[start + 1:]:
        if not l.strip() or not l.strip(","):
            if section:
                break
            continue
        section.append(l)
    rows = list(csv.reader(io.StringIO("\n".join(section))))
    if not rows:
        return res
    header = [c.strip().lower() for c in rows[0]]
    col = {name: i for i, name in enumerate(header)}
    required = ["exec time", "side", "qty", "symbol", "type", "price"]
    missing = [r for r in required if r not in col]
    if missing:
        raise ValueError(f"Account Trade History is missing columns: {', '.join(missing)}")

    def g(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    if tz:
        tz_name = tz
    else:
        times = [x for x in (_parse_time(g(r, "exec time")) for r in rows[1:] if g(r, "exec time")) if x]
        tz_name = detect_timezone(times, get_settings().tos_timezone)
    res.timezone = tz_name
    tzinfo = ZoneInfo(tz_name)
    last_time: datetime | None = None
    for seq, row in enumerate(rows[1:]):
        if not any(c.strip() for c in row):
            continue
        res.rows_total += 1
        t = _parse_time(g(row, "exec time")) if g(row, "exec time") else last_time
        if t is None:
            res.skipped["no exec time"] += 1
            continue
        last_time = t
        side = g(row, "side").upper()
        if side not in ("BUY", "SELL"):
            res.skipped[f"side {side or '(blank)'}"] += 1
            continue
        qty = abs(parse_money(g(row, "qty")) or 0)
        price = parse_money(g(row, "price"))
        sym = g(row, "symbol").upper()
        typ = g(row, "type").upper()
        if not qty or price is None or not sym:
            res.skipped["incomplete row"] += 1
            continue
        pe = g(row, "pos effect").upper()
        effect = "OPEN" if "OPEN" in pe else ("CLOSE" if "CLOSE" in pe else None)
        executed = to_utc_naive(t.replace(tzinfo=tzinfo))
        trade_date = t.date() if str(tzinfo) == "America/New_York" else \
            t.replace(tzinfo=tzinfo).astimezone(ZoneInfo("America/New_York")).date()
        if typ in ("CALL", "PUT"):
            exp = _parse_exp(g(row, "exp"))
            strike = parse_money(g(row, "strike"))
            if exp is None or strike is None:
                res.skipped["option without exp/strike"] += 1
                continue
            rec = ExecRecord(
                external_id="", symbol=option_symbol(sym, exp, typ, strike), underlying=sym,
                asset_type="OPTION", option_type=typ, strike=strike, expiration=exp,
                multiplier=OPTION_MULTIPLIER, side=side, quantity=qty, price=price,
                executed_at=executed, position_effect=effect, seq=seq, trade_date=trade_date,
                description=f"{g(row, 'spread')} {g(row, 'order type')}".strip(), raw={"row": row},
            )
        elif typ in ("STOCK", "ETF", ""):
            rec = ExecRecord(
                external_id="", symbol=sym, underlying=sym, asset_type="STOCK", side=side,
                quantity=qty, price=price, executed_at=executed, position_effect=effect, seq=seq,
                trade_date=trade_date, description=g(row, "order type"), raw={"row": row},
            )
        else:
            res.skipped[f"type {typ}"] += 1
            continue
        res.records.append(rec)
    assign_stable_ids("tos", res.records)
    if res.records and tz_name != get_settings().tos_timezone and not tz:
        res.warnings.append(f"Exec times read as {tz_name} time (detected from US market hours).")
    if res.records:
        res.warnings.append("thinkorswim trade history has no fees or expirations; import the matching "
                            "Schwab.com CSV too to fill those in (rows are merged, not duplicated).")
    return res
