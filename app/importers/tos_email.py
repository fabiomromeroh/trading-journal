"""thinkorswim order-fill notification emails (Setup > Application Settings > Notifications >
"Working orders filling" > Send email; sender alerts@thinkorswim.com).

UNVERIFIED FORMAT: built from documented/public examples, not from a real email of this account
yet. The fill line uses the same notation as thinkorswim's order/trade descriptions, e.g.

  #1432618991 PAPERMONEY BOT +2 SPY 100 (Weeklys) 12 JUL 19 298.5 CALL @.72MARK=298.67 IMPL VOL=13.01% , ACCOUNT D-******00
  tIP BOT +100 AAPL @227.50 LMT
  SOLD -4 ALAB @366.88
  SOLD -1 VERTICAL QQQ 100 20 SEP 24 470/475 CALL @1.20 CBOE      (multi-leg: not imported here)

so the parser looks for every "BOT|SOLD <signed qty> <instrument> @<price>" in the text and is
tolerant of prefixes (order number, platform tags like tIP/TOSWeb/PAPERMONEY), suffixes (order
type, exchange, MARK=..., ACCOUNT ...), HTML and line wrapping. The fill time is taken from the
body only when it carries an explicit time zone; otherwise the email's received time is used
(thinkorswim sends these within seconds of the fill).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol
from app.timeutil import et_date

SENDER = "alerts@thinkorswim.com"
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}
NUM = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?"
_FILL_RE = re.compile(
    r"(?:#(?P<order>\d{5,})\s+)?(?:(?:PAPERMONEY|tIP|tIPAD|TOSWeb|tAndroid|tWeb|tMobile|tRAD|MOBILE)\s+)*"
    r"\b(?P<action>BOT|SOLD)\s+(?P<qty>[+-]?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s+"
    r"(?P<inst>[^@\n]{1,160}?)\s*@\s*(?P<price>" + NUM + r")", re.I)
_OPT_RE = re.compile(
    r"^(?P<und>[A-Z][A-Z0-9.]{0,9})\s+(?P<mult>\d+)(?:\s*\([^)]*\))*\s+(?P<day>\d{1,2})\s+(?P<mon>[A-Z]{3})\s+"
    r"(?P<yr>\d{2,4})(?:\s*\([^)]*\))*\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<cp>CALL|PUT)$", re.I)
_STOCK_RE = re.compile(r"^(?P<sym>[A-Z][A-Z0-9./]{0,9})$", re.I)
_SPREADS = ("VERTICAL", "IRON", "CONDOR", "BUTTERFLY", "STRANGLE", "STRADDLE", "CALENDAR", "DIAGONAL",
            "COVERED", "COMBO", "COLLAR", "CUSTOM", "BACKRATIO", "UNBALANCED", "VERT ROLL", "DBL DIAG")
_TZ = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
       "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
       "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
       "UTC": "UTC", "GMT": "UTC"}
_BODY_TIME_RE = re.compile(
    r"(?P<mo>\d{1,2})/(?P<dd>\d{1,2})/(?P<y>\d{2,4})[ ,]+(?P<H>\d{1,2}):(?P<M>\d{2}):(?P<S>\d{2})\s*"
    r"(?P<ampm>AM|PM)?\s*(?P<tz>ET|EST|EDT|CT|CST|CDT|PT|PST|PDT|UTC|GMT)\b", re.I)
_ACCT_RE = re.compile(r"ACCOUNT\s+[\w*.-]*?(\d{3})\b", re.I)


@dataclass
class EmailParse:
    records: list[ExecRecord] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)    # fill-like lines we could not import
    account_suffix: str | None = None
    body_time: datetime | None = None                   # naive UTC, when the body had one


def to_text(body: str) -> str:
    """HTML or plain text -> single-spaced plain text."""
    t = body or ""
    if "<" in t and ">" in t:
        t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", t)
        t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", t)
        t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\u00a0", " ")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _num(s: str) -> float:
    return float(s.replace(",", "").replace(" ", "") or 0)


def _body_time(text: str, received_utc: datetime) -> datetime | None:
    """A fill time in the body (M/D/YY h:mm:ss [AM|PM] <zone>), used only with an explicit zone
    and when it is close to the received time."""
    from zoneinfo import ZoneInfo
    for m in _BODY_TIME_RE.finditer(text):
        y = int(m["y"]) + (2000 if len(m["y"]) == 2 else 0)
        h = int(m["H"])
        if m["ampm"]:
            h = h % 12 + (12 if m["ampm"].upper() == "PM" else 0)
        try:
            local = datetime(y, int(m["mo"]), int(m["dd"]), h, int(m["M"]), int(m["S"]))
        except ValueError:
            continue
        dt = local.replace(tzinfo=ZoneInfo(_TZ[m["tz"].upper()])).astimezone(timezone.utc).replace(tzinfo=None)
        if abs(dt - received_utc) <= timedelta(hours=6):
            return dt
    return None


def parse(subject: str, body: str, received_utc: datetime, message_id: str) -> EmailParse:
    """-> fills found in one notification email. `received_utc` is naive UTC."""
    text = to_text(f"{subject or ''}\n{body or ''}")
    out = EmailParse()
    if m := _ACCT_RE.search(text):
        out.account_suffix = m.group(1)
    out.body_time = _body_time(text, received_utc)
    when = out.body_time or received_utc
    day = et_date(when)
    seen: set[tuple] = set()
    n = 0
    for m in _FILL_RE.finditer(text):
        action = m["action"].upper()
        qty = abs(_num(m["qty"]))
        price = _num(m["price"])
        inst = re.sub(r"\s+", " ", m["inst"]).strip().upper()
        inst = re.sub(r"\s+(LMT|MKT|STP|STP LMT|TRSTP|MOC|LOC|NET)$", "", inst)
        line = m.group(0).strip()
        key = (m["order"], action, qty, inst, price)
        if key in seen:      # the subject often repeats the body line
            continue
        seen.add(key)
        if qty <= 0 or price <= 0:
            out.skipped.append(line)
            continue
        side = "BUY" if action == "BOT" else "SELL"
        if any(w in inst for w in _SPREADS) or "/" in inst.split(" ", 1)[-1] or inst.startswith("/"):
            out.skipped.append(line)          # spreads / futures: left to the broker data
            continue
        raw = {"message_id": message_id, "order": m["order"], "line": line[:300]}
        if om := _OPT_RE.match(inst):
            yr = int(om["yr"]) + (2000 if len(om["yr"]) == 2 else 0)
            try:
                exp = date(yr, MONTHS[om["mon"].upper()], int(om["day"]))
            except (KeyError, ValueError):
                out.skipped.append(line)
                continue
            und, strike, cp = om["und"].upper(), float(om["strike"]), om["cp"].upper()
            rec = ExecRecord(external_id=f"{message_id}#{n}", symbol=option_symbol(und, exp, cp, strike),
                             underlying=und, asset_type="OPTION", option_type=cp, strike=strike, expiration=exp,
                             multiplier=float(om["mult"]) or OPTION_MULTIPLIER, side=side, quantity=qty,
                             price=price, executed_at=when, time_known=True, description=line[:300],
                             trade_date=day, raw=raw)
        elif sm := _STOCK_RE.match(inst):
            sym = sm["sym"].upper()
            rec = ExecRecord(external_id=f"{message_id}#{n}", symbol=sym, underlying=sym, asset_type="STOCK",
                             side=side, quantity=qty, price=price, executed_at=when, time_known=True,
                             description=line[:300], trade_date=day, raw=raw)
        else:
            out.skipped.append(line)
            continue
        rec.seq = n
        n += 1
        out.records.append(rec)
    return out
