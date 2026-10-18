"""Interactive Brokers Flex Web Service adapter (Activity Flex Query, XML).

Two-step HTTPS (no gateway): SendRequest -> ReferenceCode, then GetStatement.
The <Url> on SendRequest is legacy and is ignored; GetStatement always uses
DEFAULT_GET_URL. One token and one query id cover every account in the query.
Each FlexStatement becomes its own journal account. History is pulled in fd/td
windows of at most 365 days ending on the last completed US business day
(America/New_York), paced so a job stays under IBKR's 10 requests/minute.
A 1001 (or other temporary) window is retried on the next press as smaller
pieces (year -> quarters -> months). Fills dedupe on tradeID / transactionID
per account. Never invents fills. Token is Fernet-encrypted in app_state
(cred:ibkr; never in backups, logs, status, or HTML).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time as _time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets, symbolmap
from app.instruments import ExecRecord, option_symbol
from app.models import Account, Instrument, utcnow
from app.services import get_state, ingest_records, rebuild_trades, set_state
from app.sources.base import DataSource, SourceResult, SourceStatus, SyncContext
from app.timeutil import local_to_utc_naive

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
DEFAULT_GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
CRED_STATE = "cred:ibkr"          # encrypted {"token","query_id","currency"}; cred: prefix keeps it out of backups
LAST_STATE = "ibkr:last"          # last sync / test status (no secrets)
FULL_STATE = "ibkr:full"          # "1" → next sync is a full windowed history (unless a paused job resumes)
HISTORY_STATE = "ibkr:history"    # {start, next_fd, done_windows, total_windows, status, paused_until, error}
HISTORY_START_STATE = "ibkr:history_start"  # yyyy-mm-dd; Full history starts here (default 2022-01-01)
ACCOUNT_MAP_STATE = "ibkr:accounts"  # {IBKR accountId: journal account id}; survives a rename
SOURCE_KEY = "ibkr_flex"
STOCK_CATS = {"STK", "ETF", "FUND"}
OPTION_CATS = {"OPT"}
SUPPORTED_CATS = STOCK_CATS | OPTION_CATS
TRADE_LOD = {"", "EXECUTION"}
CASH_LOD = {"", "DETAIL"}
POS_LOD = {"", "SUMMARY"}
CA_LOD = {"", "DETAIL", "SUMMARY"}
RETRY_1019 = 3                    # GetStatement polls per fetch (5 s apart). No other retry loops.
POLL_GAP = 5.0
MAX_CALLS = 5                     # hard cap on HTTP requests per FlexClient (one fetch = 1 send + <=3 polls)
SEND_GAP = 15.0                   # minimum seconds between SendRequest calls (IBKR: 1/s, 10/min)
WINDOW_GAP = 30.0                 # minimum seconds between history windows (>= SEND_GAP)
RATE_BUDGET_PER_MIN = 6           # well under IBKR's 10 requests/minute; polls count toward this
OVERLAP_DAYS = 4                  # Sync new re-reads this many days before the last synced date
MAX_WINDOW_DAYS = 365             # inclusive fd..td length; IBKR rejects a longer range
MAX_QUARTER_DAYS = 92             # year/long windows split to these on the next press
MAX_MONTH_DAYS = 31               # a quarter that fails 1001 again splits to calendar months
SPLIT_QUARTER = "quarter"
SPLIT_MONTH = "month"
SPLIT_NONE = "none"
_POLL_AGAIN = {"1019", "1001"}    # GetStatement: statement not ready yet (bounded polls only)
NY = ZoneInfo("America/New_York")
DEFAULT_HISTORY_START = date(2022, 1, 1)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_FILES = 10
UPLOAD_CHUNK = 1024 * 1024
RAW_STORE_UNCOMPRESSED = 20 * 1024 * 1024
RAW_STORE_GZIP_MAX = 8 * 1024 * 1024
INGEST_BATCH = 500
UPLOAD_STATE = "ibkr:upload"      # background XML import progress (no secrets)
REPROCESS_STATE = "ibkr:reprocess"
REPROCESS_VERSION = "5"
CONID_SYMBOL_STATE = "ibkr:conid_symbols"  # {conid: {symbol, asof, prefer}}; latest IBKR symbol wins
COOLDOWN = 15 * 60                # pause all IBKR calls after a 1018 / 429 / 403 answer
_GATE = {"last": 0.0, "until": 0.0}
_GATE_LOCK = threading.Lock()
_UPLOAD_LOCK = threading.Lock()
_UPLOAD_ACTIVE = False
# Tests set True to run the worker in-process; False + patched start_upload_worker to assert 303 first.
UPLOAD_SYNC: bool | None = None


def cooldown_left() -> float:
    return max(0.0, _GATE["until"] - _time.monotonic())


def _trip(seconds: float = COOLDOWN) -> None:
    with _GATE_LOCK:
        _GATE["until"] = max(_GATE["until"], _time.monotonic() + seconds)


class _NoNetwork(httpx.BaseTransport):
    """Used under pytest when no fake transport is given: the real IBKR host is never called from tests."""
    def handle_request(self, request):
        raise httpx.ConnectError(f"test tried to reach {request.url.host}; pass a MockTransport", request=request)
HOW_TO = (
    "Client Portal → Performance & Reports → Flex Queries → Activity Flex Query (create). "
    "One query may cover several accounts: each IBKR account becomes its own journal account "
    "(named IBKR U1234567, and you can rename it). Selecting all fields in every section is fine; "
    "extra sections are ignored. Set the query format to XML. Date format yyyyMMdd, time HHmmss "
    "with ';' separator. Include Trades (level Executions), Cash Transactions, Open Positions and "
    "Corporate Actions, Cash Report, Equity Summary, Transfers and Option EAE. The query period "
    "can stay Last 365 days — Full history on this card does not use that period. It walks "
    "year-sized windows from the history start date (default 2022-01-01, editable on this card) "
    "through the previous US business day (IBKR Flex data is complete through the prior session). "
    "A year window that IBKR answers 1003 (no statement) is retried as quarter windows on the "
    "next Sync / Full history press; a quarter that is still 1003 is recorded as a history gap. "
    "A 1001 (statement could not be generated) window is retried as smaller pieces on the next "
    "press: year → quarters → months. You can also upload an Activity Statement XML (same parser "
    "and dedupe). Then "
    "Flex Web Service Configuration → enable and generate one token; copy the Query ID. That one "
    "token and Query ID sync every account in the query."
)
_UNSAFE_XML = re.compile(r"<!DOCTYPE|<!ENTITY", re.I)
_SECRET_RE = re.compile(r"([?&]t=)[^&\s]+", re.I)
_TOKEN_KEYS = re.compile(r"token|secret|password|authorization", re.I)

_TRANSPORT: httpx.BaseTransport | None = None

LISTING_TO_EXCHANGE = {
    "NASDAQ": "US", "NYSE": "US", "AMEX": "US", "ARCA": "US", "NYSEARCA": "US",
    "BATS": "US", "IEX": "US", "PINK": "US", "OTCBB": "US", "VALUE": "US",
    "ISLAND": "US", "DARK": "US", "SMART": "US", "CBOE": "US", "BEX": "US",
    "LSE": "LSE", "LON": "LSE", "LSEETF": "LSE",
    "IBIS": "XETRA", "IBIS2": "XETRA", "FWB": "XETRA", "XETRA": "XETRA", "GETTEX": "XETRA",
    "AEB": "AMS", "SBF": "PAR", "BVME": "MIL", "MIL": "MIL",
    "VSE": "SWX", "EBS": "SWX", "SWX": "SWX",
}


class FlexError(Exception):
    """kind: auth | query | ip | rate | temporary | unavailable | network | api"""

    def __init__(self, kind: str, message: str, code: str | None = None):
        super().__init__(message)
        self.kind, self.code = kind, code


def _enc():
    from app import security
    return security


def redact(text: str, token: str | None = None) -> str:
    """Strip a Flex token and t= query values from any string we might surface."""
    s = "" if text is None else str(text)
    if token:
        s = s.replace(token, "[redacted]")
    return _SECRET_RE.sub(r"\1[redacted]", s)


def _compact(value) -> str:
    """Drop whitespace and newlines from a pasted Flex token or query id."""
    return re.sub(r"\s+", "", "" if value is None else str(value))


def save_credentials(db: Session, token: str, query_id: str, currency: str = "USD") -> None:
    payload = json.dumps({
        "token": _compact(token), "query_id": _compact(query_id),
        "currency": ((currency or "USD").strip().upper() or "USD")[:3],
    })
    set_state(db, CRED_STATE, _enc().encrypt(payload))
    db.flush()


def load_credentials(db: Session) -> dict | None:
    raw = get_state(db, CRED_STATE)
    if not raw:
        return None
    try:
        data = json.loads(_enc().decrypt(raw))
    except Exception:  # noqa: BLE001 - key changed / corrupt: behave as not configured
        return None
    token = _compact(data.get("token"))
    query_id = _compact(data.get("query_id"))
    if not token or not query_id:
        return None
    data["token"] = token
    data["query_id"] = query_id
    return data


def clear_credentials(db: Session) -> None:
    set_state(db, CRED_STATE, None)
    db.flush()


def last_status(db: Session) -> dict:
    try:
        return json.loads(get_state(db, LAST_STATE) or "{}")
    except ValueError:
        return {}


def _save_last(db: Session, *, reset: bool = False, **kw) -> dict:
    cur = {} if reset else last_status(db)
    cur.update(kw)
    set_state(db, LAST_STATE, json.dumps(cur, default=str))
    return cur


def _now_ny() -> datetime:
    return datetime.now(NY)


def last_complete_biz_day(now: datetime | None = None) -> date:
    """Last completed US business day strictly before `now` in America/New_York.

    Saturdays and Sundays are skipped. A weekday uses the previous weekday:
    IBKR Flex data is complete through the prior session, not the current day.
    """
    if now is None:
        now = _now_ny()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    else:
        now = now.astimezone(NY)
    day = now.date() - timedelta(days=1)
    while day.weekday() >= 5:  # Saturday=5, Sunday=6
        day -= timedelta(days=1)
    return day


def _today() -> date:
    """End date for Flex fd/td windows: last completed US business day (NY)."""
    return last_complete_biz_day()


def _ymd(d: date) -> str:
    return f"{d:%Y%m%d}"


def _parse_iso(s) -> date | None:
    raw = ("" if s is None else str(s)).strip()
    if not raw:
        return None
    try:
        if len(raw) >= 8 and raw[:8].isdigit():
            return datetime.strptime(raw[:8], "%Y%m%d").date()
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def iter_windows(start: date, end: date, max_days: int = MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
    """Inclusive [fd, td] slices with no gaps and no slice longer than max_days."""
    if end < start:
        return []
    span = max(1, int(max_days))
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        td = min(end, cur + timedelta(days=span - 1))
        out.append((cur, td))
        cur = td + timedelta(days=1)
    return out


def iter_quarters(start: date, end: date) -> list[tuple[date, date]]:
    """Inclusive calendar-quarter slices, each at most 92 days."""
    if end < start:
        return []
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        qm = ((cur.month - 1) // 3) * 3 + 1
        if qm == 10:
            qend = date(cur.year, 12, 31)
        else:
            qend = date(cur.year, qm + 3, 1) - timedelta(days=1)
        td = min(end, qend)
        out.append((cur, td))
        cur = td + timedelta(days=1)
    return out


def iter_months(start: date, end: date) -> list[tuple[date, date]]:
    """Inclusive calendar-month slices, each at most 31 days."""
    if end < start:
        return []
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        td = min(end, month_end)
        out.append((cur, td))
        cur = td + timedelta(days=1)
    return out


def is_year_sized(fd: date, td: date) -> bool:
    return (td - fd).days + 1 > MAX_QUARTER_DAYS


def is_quarter_sized(fd: date, td: date) -> bool:
    days = (td - fd).days + 1
    return MAX_MONTH_DAYS < days <= MAX_QUARTER_DAYS


def clamp_window(fd: date, td: date, end: date) -> tuple[date, date] | None:
    """Drop or trim a window so td never exceeds the Flex end date."""
    td = min(td, end)
    if fd > end or fd > td:
        return None
    return fd, td


def split_level_of(fw: dict, fd: date, td: date) -> str:
    """How the next press should break this failed window.

    Explicit `split` on the stored entry wins. Otherwise a year/long window
    becomes quarters, a quarter becomes months, a month stays a month.
    1003 only splits year-sized windows (a 1003 quarter is a history gap).
    """
    explicit = str(fw.get("split") or "").strip().lower()
    if explicit in {SPLIT_QUARTER, SPLIT_MONTH, SPLIT_NONE}:
        return explicit
    code = str(fw.get("code") or "")
    if code == "1003":
        return SPLIT_QUARTER if is_year_sized(fd, td) else SPLIT_NONE
    if is_year_sized(fd, td):
        return SPLIT_QUARTER
    if is_quarter_sized(fd, td):
        return SPLIT_MONTH
    return SPLIT_NONE


def next_split_level(fd: date, td: date) -> str:
    """Split level to store when this window itself just failed."""
    if is_year_sized(fd, td):
        return SPLIT_QUARTER
    if is_quarter_sized(fd, td):
        return SPLIT_MONTH
    return SPLIT_NONE


def expand_failed_window(fw: dict, end: date) -> tuple[list[tuple[date, date]], dict | None]:
    """Turn a stored failed window into this press's sub-windows.

    Returns (pieces, kept_entry). kept_entry is None when the parent is
    replaced by sub-windows. A window whose fd is after `end` is dropped.
    """
    fd, td = _parse_iso(fw.get("fd")), _parse_iso(fw.get("td"))
    if not fd or not td:
        return [], None
    clamped = clamp_window(fd, td, end)
    if clamped is None:
        return [], None
    fd, td = clamped
    level = split_level_of(fw, fd, td)
    if level == SPLIT_QUARTER:
        return iter_quarters(fd, td), None
    if level == SPLIT_MONTH:
        return iter_months(fd, td), None
    kept = dict(fw)
    kept["fd"], kept["td"] = fd.isoformat(), td.isoformat()
    kept["split"] = SPLIT_NONE
    return [(fd, td)], kept


def mask_account_id(ext: str | None) -> str:
    """U1234567 → U***4567. Never log or report a full IBKR account number."""
    s = str(ext or "").strip()
    if len(s) >= 5:
        return f"{s[0]}***{s[-4:]}"
    if len(s) >= 4:
        return f"***{s[-4:]}"
    return "***" if s else ""


def _failed_windows(h: dict | None) -> list[dict]:
    raw = (h or {}).get("failed_windows") or []
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _gap_windows(h: dict | None) -> list[dict]:
    raw = (h or {}).get("gaps") or []
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _window_entry(fd: date, td: date, code: str | None, kind: str | None,
                  split: str | None = None, message: str | None = None) -> dict:
    out = {"fd": fd.isoformat(), "td": td.isoformat(), "code": str(code or ""), "kind": str(kind or "")}
    if split:
        out["split"] = str(split)
    if message:
        out["message"] = redact(str(message))[:300]
    return out


def _add_failed(failed: list[dict], entry: dict) -> list[dict]:
    want = (entry.get("fd"), entry.get("td"))
    if any((x.get("fd"), x.get("td")) == want for x in failed):
        return failed
    failed.append(entry)
    return failed


def _drop_failed(failed: list[dict], fd: date, td: date) -> list[dict]:
    want = (fd.isoformat(), td.isoformat())
    return [x for x in failed if (x.get("fd"), x.get("td")) != want]


def history_gap(http_calls: int) -> float:
    """Seconds to wait before the next SendRequest.

    GetStatement polls are part of http_calls, so a window that polled for 1019
    waits longer than a 2-call window. The floor is WINDOW_GAP (30s) and SEND_GAP (15s),
    which keeps the job well under 10 requests/minute.
    """
    per_call = 60.0 / RATE_BUDGET_PER_MIN
    return max(WINDOW_GAP, SEND_GAP, max(0, int(http_calls)) * per_call)


def _send_wait() -> float:
    last = _GATE["last"]
    if not last:
        return 0.0
    return max(0.0, SEND_GAP - (_time.monotonic() - last))


def _release_send_gate() -> None:
    """The history job already asked sleep() for the gap. Tests record that and do not wait,
    so the gate has to treat the gap as elapsed or the next SendRequest would raise."""
    with _GATE_LOCK:
        if _GATE["last"]:
            _GATE["last"] = min(_GATE["last"], _time.monotonic() - max(SEND_GAP, 0.0))


def load_history(db: Session) -> dict:
    try:
        data = json.loads(get_state(db, HISTORY_STATE) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_history(db: Session, data: dict) -> None:
    set_state(db, HISTORY_STATE, json.dumps(data, default=str))


def history_start_date(db: Session) -> date:
    return _parse_iso(get_state(db, HISTORY_START_STATE)) or DEFAULT_HISTORY_START


def save_history_start(db: Session, day: date) -> None:
    set_state(db, HISTORY_START_STATE, day.isoformat())


def parse_history_start(raw: str) -> date:
    day = _parse_iso(raw)
    if day is None:
        raise ValueError("History start must be a real date (yyyy-mm-dd).")
    if day.year < 1990 or day > _now_ny().date():
        raise ValueError("History start must be between 1990 and today.")
    return day


def load_upload(db: Session) -> dict:
    try:
        data = json.loads(get_state(db, UPLOAD_STATE) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def public_upload(data: dict | None) -> dict:
    """Progress for the card: no temp paths, no token, no full account numbers."""
    raw = data if isinstance(data, dict) else {}
    files = []
    for row in raw.get("files") or []:
        if not isinstance(row, dict):
            continue
        files.append({
            "name": _safe_filename(row.get("name")),
            "size": int(row.get("size") or 0),
            "status": row.get("status") or "",
            "fetched": int(row.get("fetched") or 0),
            "inserted": int(row.get("inserted") or 0),
            "error": row.get("error"),
            "note": row.get("note"),
        })
    return {
        "status": raw.get("status") or "",
        "files": files,
        "current": int(raw.get("current") or 0),
        "started": raw.get("started"),
        "finished": raw.get("finished"),
        "error": raw.get("error"),
        "queued_at": raw.get("queued_at"),
    }


def save_upload(db: Session, data: dict) -> None:
    set_state(db, UPLOAD_STATE, json.dumps(data, default=str))


def recover_stale_upload(db: Session) -> dict:
    """A 'running'/'queued' row after process death cannot finish. Mark error; caller uploads again."""
    data = load_upload(db)
    if (data.get("status") or "") not in {"running", "queued"}:
        return data
    data["status"] = "error"
    data["error"] = "interrupted by restart, upload again"
    data["finished"] = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    for row in data.get("files") or []:
        if isinstance(row, dict) and (row.get("status") or "") in {"queued", "running"}:
            row["status"] = "error"
            row["error"] = "interrupted by restart, upload again"
    save_upload(db, data)
    db.commit()
    _cleanup_upload_dir(data.get("dir"))
    return data


def _cleanup_upload_dir(path) -> None:
    raw = str(path or "").strip()
    if not raw:
        return
    try:
        p = Path(raw)
        if p.is_dir() and p.name.startswith("ibkr-up-"):
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _upload_inline() -> bool:
    if UPLOAD_SYNC is not None:
        return bool(UPLOAD_SYNC)
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def load_account_map(db: Session) -> dict:
    try:
        data = json.loads(get_state(db, ACCOUNT_MAP_STATE) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_account_map(db: Session, data: dict) -> None:
    set_state(db, ACCOUNT_MAP_STATE, json.dumps({str(k): int(v) for k, v in data.items() if str(k) and v}))


def _history_notes(h: dict | None) -> list[str]:
    raw = (h or {}).get("notes") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(n) for n in raw if n]


def _failed_progress(h: dict | None) -> str:
    """Clear card text for a 1001 (or other) failed window. Never includes the token."""
    failed = _failed_windows(h)
    if not failed:
        return ""
    end = _today()
    fw = failed[0]
    fd_d, td_d = _parse_iso(fw.get("fd")), _parse_iso(fw.get("td"))
    code = str(fw.get("code") or "")
    if fd_d and td_d:
        clamped = clamp_window(fd_d, td_d, end)
        if clamped:
            fd_d, td_d = clamped
    if not fd_d or not td_d:
        return ""
    fd_s, td_s = fd_d.isoformat(), td_d.isoformat()
    line = f"IBKR could not generate {fd_s}\u2013{td_s} ({code})."
    level = split_level_of(fw, fd_d, td_d)
    if level == SPLIT_QUARTER:
        n = len(iter_quarters(fd_d, td_d))
        word = "quarter" if n == 1 else "quarters"
        line += f" Next Sync will fetch it in smaller pieces: {n} {word} left."
    elif level == SPLIT_MONTH:
        n = len(iter_months(fd_d, td_d))
        word = "month" if n == 1 else "months"
        line += f" Next Sync will fetch it in smaller pieces: {n} {word} left."
    else:
        n = len(failed)
        extra = f" {n} windows queued." if n > 1 else ""
        line += f" Next Sync will retry this window.{extra}"
    return line


def data_available_line(end: date | None = None) -> str:
    day = end or _today()
    return (f"Data available through {day.isoformat()} "
            "(IBKR has data up to the previous business day).")


def progress_line(h: dict | None) -> str:
    """e.g. 'window 3 of 5 done, next 2024-01-01…' plus failed-window and data-available lines."""
    parts: list[str] = []
    if h and h.get("total_windows"):
        done = int(h.get("done_windows") or 0)
        total = int(h.get("total_windows") or 0)
        nxt = h.get("next_fd") or ""
        status = h.get("status") or ""
        base = f"window {done} of {total} done"
        if status == "done":
            line = base + (f" through {h['last_synced']}" if h.get("last_synced") else "")
        else:
            nxt_bit = f", next {nxt}…" if nxt else ""
            if status == "paused":
                until = f" Paused until {h['paused_until']}." if h.get("paused_until") else ""
                err = h.get("error") or "paused"
                line = f"{base}{nxt_bit}{until} {err}".strip()
            elif status == "error":
                line = f"{base}{nxt_bit} {h.get('error') or 'failed'}".strip()
            else:
                line = f"{base}{nxt_bit}"
        notes = _history_notes(h)
        if notes:
            line = f"{line} {'; '.join(notes)}".strip()
        parts.append(line)
    fail = _failed_progress(h)
    if fail:
        parts.append(fail)
    return " ".join(p for p in parts if p).strip()


def _paused_until_text() -> str:
    left = cooldown_left() or float(COOLDOWN)
    return (datetime.now(timezone.utc) + timedelta(seconds=left)).strftime("%Y-%m-%d %H:%M UTC")


def _is_hard_pause(exc: BaseException) -> bool:
    """True when IBKR (or our gate) has parked the process for the 15-minute cooldown.

    A SendRequest that only lost the 15s gap raises code 1018 without tripping the gate.
    That stops the job but does not pretend the IP is blocked.
    """
    if cooldown_left() > 0:
        return True
    if isinstance(exc, FlexError) and (exc.code or "") in {"429", "403"}:
        return True
    return False


# ------------------------------------------------------------------ HTTP
# Official Flex Web Service codes:
# https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes.md
# 1003 is not an auth failure. On a dated window it means that window has no statement.
_AUTH_CODES = {"1011", "1012", "1015"}
_QUERY_CODES = {"1010", "1014", "1016"}
_TEMPORARY_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}
_FALLBACK_MSG = {
    "1001": "Statement could not be generated at this time. Please try again shortly",
    "1003": "Statement is not available",
    "1004": "Statement is incomplete. Please try again shortly",
    "1005": "Settlement data is not ready",
    "1006": "FIFO P/L data is not ready",
    "1007": "MTM P/L data is not ready",
    "1008": "MTM and FIFO P/L data is not ready",
    "1009": "The server is under heavy load",
    "1010": "Legacy Flex queries are not supported",
    "1011": "Service account is inactive",
    "1012": "Token has expired",
    "1013": "IP restriction",
    "1014": "Query is invalid",
    "1015": "Token is invalid",
    "1016": "Account is invalid",
    "1017": "Reference code is invalid",
    "1018": "Too many requests have been made from this token",
    "1019": "Statement generation is in progress. Please try again shortly",
    "1020": "Invalid request or unable to validate",
    "1021": "Statement could not be retrieved. Please try again shortly",
}
_IP_HINT = ("The token has an IP restriction; remove it in Client Portal → Flex Web Service, "
            "or allow Render's outbound IPs.")


def _kind_for_code(code: str | None) -> str:
    c = (code or "").strip()
    if c == "1003":
        return "unavailable"
    if c in _AUTH_CODES:
        return "auth"
    if c in _QUERY_CODES:
        return "query"
    if c == "1013":
        return "ip"
    if c == "1018":
        return "rate"
    if c in _TEMPORARY_CODES:
        return "temporary"
    return "api"  # 1017, 1020, and anything else IBKR adds


def _error_text(code: str | None, message: str | None, token: str | None = None) -> str:
    """User-facing text. Always the IBKR code plus ErrorMessage. Never the token."""
    c = (code or "").strip()
    msg = redact((message or "").strip(), token)
    if not msg:
        msg = _FALLBACK_MSG.get(c, "Flex Web Service error")
    if msg[-1] not in ".!?":
        msg += "."
    text = f"IBKR error {c}: {msg}" if c else f"IBKR error: {msg}"
    if c == "1013":
        text += " " + _IP_HINT
    return text


def _note_code(code: str | None, message: str | None, token: str | None = None) -> None:
    """Log the Flex code at WARNING. The token and any t= URL stay out of the log."""
    c = (code or "").strip()
    safe = redact((message or "").strip(), token)
    if token:
        safe = safe.replace(token, "[redacted]")
    safe = _SECRET_RE.sub(r"\1[redacted]", safe)
    if re.search(r"https?://", safe):
        safe = re.sub(r"https?://\S+", "[url]", safe)
    if not c and not safe:
        return
    if c:
        log.warning("IBKR Flex error %s: %s", c, safe)
    else:
        log.warning("IBKR Flex error: %s", safe)


def _response_error(code: str | None, message: str | None, token: str | None = None) -> FlexError:
    c = (code or "").strip() or None
    _note_code(c, message, token)
    if c == "1018":
        _trip()
    return FlexError(_kind_for_code(c), _error_text(c, message, token), c)


class FlexClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None,
                 sleep=_time.sleep, timeout: float = 30):
        self.token = _compact(token)
        self.sleep = sleep
        self.calls = 0
        self.http = httpx.Client(
            timeout=httpx.Timeout(timeout),
            transport=transport or _TRANSPORT or (_NoNetwork() if os.environ.get("PYTEST_CURRENT_TEST") else None),
            headers={"User-Agent": "trading-journal/1.0", "Accept": "application/xml"},
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, url: str, extra: dict) -> str:
        """One HTTP request, no automatic retries (IBKR blocks IPs that hammer the Flex service)."""
        left = cooldown_left()
        if left > 0:
            raise FlexError("rate", f"IBKR calls are paused for {int(left // 60) + 1} more minute(s) after a rate-limit answer. "
                                    "Try again later.", "1018")
        if self.calls >= MAX_CALLS:
            raise FlexError("temporary", _error_text("1019", None), "1019")
        self.calls += 1
        params = {"t": self.token, "v": "3", **extra}
        try:
            resp = self.http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise FlexError("network", "Could not reach Interactive Brokers (network error). Try again later.") from exc
        sc = resp.status_code
        if sc in (403, 429):
            _trip()
            raise FlexError("rate", f"IBKR refused the request (HTTP {sc}); pausing IBKR calls for 15 minutes.", str(sc))
        if sc in (408, 500, 502, 503, 504):
            raise FlexError("temporary", f"Interactive Brokers is temporarily unavailable ({sc}). Try again in a few minutes.")
        if sc == 401:
            raise FlexError("auth", "IBKR rejected the Flex token (401). Generate a new token in Client Portal.", "401")
        if sc >= 400:
            raise FlexError("api", f"IBKR Flex HTTP {sc}.", str(sc))
        return resp.text or ""

    def send_request(self, query_id: str, fd: str | None = None, td: str | None = None) -> tuple[str, str]:
        """One SendRequest. fd and td (yyyyMMdd) are sent together and never with period (p)."""
        with _GATE_LOCK:
            wait = SEND_GAP - (_time.monotonic() - _GATE["last"])
            if _GATE["last"] and wait > 0:
                raise FlexError("rate", f"Please wait {int(wait) + 1} s before the next IBKR request.", "1018")
            _GATE["last"] = _time.monotonic()
        extra = {"q": _compact(query_id)}
        if fd or td:
            if not fd or not td:
                raise FlexError("api", "Flex date window needs both fd and td (yyyyMMdd).")
            extra["fd"] = fd
            extra["td"] = td
        xml = self._get(SEND_URL, extra)
        status, code, message, ref, _legacy_url = parse_flex_response(xml, self.token)
        # 1019 on SendRequest has no statement yet. GetStatement is where the bounded polls live.
        if code == "1019" or (status or "").lower() != "success" or not ref:
            raise _response_error(code, message, self.token)
        # IBKR docs: the <Url> on SendRequest is legacy and must be ignored.
        return ref, DEFAULT_GET_URL

    def get_statement(self, reference: str, url: str | None = None) -> str:
        """Poll DEFAULT_GET_URL. `url` from SendRequest is ignored (legacy)."""
        last: FlexError | None = None
        for attempt in range(RETRY_1019):
            xml = self._get(DEFAULT_GET_URL, {"q": str(reference)})
            if looks_like_statement(xml):
                return xml
            status, code, message, _ref, _url = parse_flex_response(xml, self.token)
            if code in _POLL_AGAIN:
                last = _response_error(code, message, self.token)
                if attempt + 1 < RETRY_1019:
                    self.sleep(POLL_GAP)
                continue
            raise _response_error(code, message, self.token)
        raise last or FlexError("temporary", _error_text("1019", None), "1019")

    def fetch(self, query_id: str, fd: str | None = None, td: str | None = None) -> str:
        ref, _url = self.send_request(query_id, fd=fd, td=td)
        return self.get_statement(ref)


# ------------------------------------------------------------------ XML helpers
def _local(tag: str) -> str:
    return (tag or "").rsplit("}", 1)[-1]


def fields_of(el: ET.Element) -> dict[str, str]:
    """Attribute-style row plus direct child text (both Flex layouts)."""
    out: dict[str, str] = {}
    for k, v in el.attrib.items():
        if v is not None and str(v) != "":
            out[_local(k)] = str(v)
    for child in list(el):
        if list(child):
            continue
        text = (child.text or "").strip()
        tag = _local(child.tag)
        if text and tag not in out:
            out[tag] = text
    return out


def _first_text(el: ET.Element | None, *names: str) -> str:
    if el is None:
        return ""
    want = {n.lower() for n in names}
    for k, v in el.attrib.items():
        if _local(k).lower() in want and v:
            return str(v).strip()
    for child in list(el):
        if _local(child.tag).lower() in want:
            t = (child.text or "").strip()
            if t:
                return t
    return ""


def parse_flex_response(xml_text: str, token: str | None = None
                        ) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Status, ErrorCode, ErrorMessage, ReferenceCode, Url from a FlexStatementResponse."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        snippet = redact((xml_text or "")[:80], token)
        raise FlexError("api", f"IBKR Flex returned something that is not XML ({snippet}).") from None
    status = _first_text(root, "Status") or (root.attrib.get("status") or "")
    code = _first_text(root, "ErrorCode", "errorCode") or None
    message = redact(_first_text(root, "ErrorMessage", "errorMessage") or "", token) or None
    ref = _first_text(root, "ReferenceCode", "referenceCode") or None
    url = _first_text(root, "Url", "url") or None
    if url:
        url = _strip_token_from_url(url)
    return status, code, message, ref, url


def _strip_token_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        q = parse_qs(p.query, keep_blank_values=True)
        q.pop("t", None)
        # flatten single values
        flat = []
        for k, vs in q.items():
            for v in vs:
                flat.append(f"{k}={v}")
        return urlunparse((p.scheme, p.netloc, p.path, p.params, "&".join(flat), p.fragment))
    except Exception:  # noqa: BLE001
        return _SECRET_RE.sub(r"\1", url).rstrip("?&")


def looks_like_statement(xml_text: str) -> bool:
    s = xml_text or ""
    return "<FlexQueryResponse" in s or "<FlexStatement " in s or "<FlexStatement>" in s


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def parse_ibkr_dt(s: str) -> tuple[datetime | None, bool]:
    """yyyyMMdd / yyyyMMdd;HHmmss / yyyy-MM-dd [HH:MM:SS] -> (naive local datetime, time_known)."""
    raw = (s or "").strip()
    if not raw:
        return None, False
    raw = raw.replace("T", " ")
    if ";" in raw:
        dpart, tpart = raw.split(";", 1)
        tpart = re.sub(r"\D", "", tpart)[:6].ljust(6, "0")
        dpart = re.sub(r"\D", "", dpart)[:8]
        try:
            return datetime.strptime(dpart + tpart, "%Y%m%d%H%M%S"), True
        except ValueError:
            return None, False
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 14:
        try:
            return datetime.strptime(digits[:14], "%Y%m%d%H%M%S"), True
        except ValueError:
            return None, False
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d"), False
        except ValueError:
            return None, False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19] if " " in raw else raw[:10], fmt)
            return dt, " " in raw
        except ValueError:
            continue
    return None, False


def parse_ibkr_date(s: str) -> date | None:
    dt, _ = parse_ibkr_dt(s)
    return dt.date() if dt else None


def listing_exchange(listing: str | None, isin: str | None) -> str | None:
    ex = LISTING_TO_EXCHANGE.get((listing or "").upper())
    if ex:
        return ex
    return markets.ISIN_PREFIX_TO_EXCHANGE.get((isin or "")[:2].upper())


def _lod(row: dict) -> str:
    return (row.get("levelOfDetail") or row.get("LevelOfDetail") or "").strip().upper()


def _skip_summary(counts: Counter, detail: dict[str, list[str]]) -> str:
    bits = []
    for k, n in sorted(counts.items()):
        ts = detail.get(k) or []
        shown = ", ".join(ts[:12])
        extra = f" +{len(ts) - 12}" if len(ts) > 12 else ""
        bits.append(f"{n} {k}" + (f" ({shown}{extra})" if shown else ""))
    return "; ".join(bits)


# ------------------------------------------------------------------ parse statement
@dataclass
class ParsedFlex:
    account_id: str | None = None
    currency: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    trades: list[dict] = field(default_factory=list)
    cash: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    corporate: list[dict] = field(default_factory=list)
    equity: dict | None = None
    equity_rows: list[dict] = field(default_factory=list)
    cash_report: list[dict] = field(default_factory=list)
    transfers: list[dict] = field(default_factory=list)
    option_eae: list[dict] = field(default_factory=list)


def _xml_root(xml_text: str) -> ET.Element:
    raw = xml_text or ""
    if _UNSAFE_XML.search(raw):
        raise FlexError("api", "IBKR Flex statement contained a DOCTYPE or ENTITY declaration.")
    try:
        parser = ET.XMLParser()
        # Python 3.8+ does not expand external entities; still reject DOCTYPE above.
        root = ET.fromstring(raw, parser=parser)
    except ET.ParseError as exc:
        raise FlexError("api", "IBKR Flex statement was not valid XML.") from exc
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = _local(el.tag)
    return root


def _elems(parent: ET.Element, tag: str):
    """Descendants with this local tag. Unknown sections are simply never asked for."""
    want = tag.lower()
    for el in parent.iter():
        if el is parent:
            continue
        if _local(el.tag).lower() == want:
            yield el


def _rows_in_section(stmt: ET.Element, section: str, row_tag: str) -> list[dict]:
    """Row elements under one known section, plus a row that is a direct child of the statement.

    Walking the whole statement for the row tag would treat a <Trade> inside <Transfers>
    (or any other select-all section) as a fill. Extra attributes on a real row are kept
    on the dict and ignored by the field readers.
    """
    want_s, want_r = section.lower(), row_tag.lower()
    rows: list[dict] = []
    seen: set[int] = set()

    def take(el: ET.Element) -> None:
        if id(el) in seen or _local(el.tag).lower() != want_r:
            return
        row = fields_of(el)
        if row:
            seen.add(id(el))
            rows.append(row)

    for sec in stmt.iter():
        if sec is stmt or _local(sec.tag).lower() != want_s:
            continue
        for el in sec.iter():
            if el is sec or _local(el.tag).lower() == want_s:
                continue
            take(el)
    for child in list(stmt):
        take(child)
    return rows


def _base_currency(sf: dict, info: dict, equity: dict | None) -> str | None:
    for src in (info, sf, equity or {}):
        ccy = (src.get("currency") or src.get("accountCurrency") or src.get("baseCurrency")
               or src.get("accountBaseCurrency") or "")
        ccy = str(ccy).strip().upper()
        if ccy:
            return ccy[:3]
    return None


def _parse_statement_el(stmt: ET.Element) -> ParsedFlex:
    """One FlexStatement. Only known tags are read; extra attributes and sections are ignored."""
    out = ParsedFlex()
    sf = fields_of(stmt)
    out.account_id = sf.get("accountId") or None
    out.from_date = sf.get("fromDate")
    out.to_date = sf.get("toDate")
    info: dict = {}
    for el in list(stmt):
        if _local(el.tag) == "AccountInformation":
            info = fields_of(el)
            break
    if not info:
        found = next(_elems(stmt, "AccountInformation"), None)
        if found is not None:
            info = fields_of(found)
    if info.get("accountId"):
        out.account_id = info.get("accountId")
    # Known sections only. A <Trade> (or cash/position/action) inside <Transfers>,
    # <SecuritiesInfo>, <ConversionRates>, or any other "select all" section is ignored.
    for section, tag, dest in (
            ("Trades", "Trade", out.trades),
            ("CashTransactions", "CashTransaction", out.cash),
            ("OpenPositions", "OpenPosition", out.positions),
            ("CorporateActions", "CorporateAction", out.corporate),
            ("Transfers", "Transfer", out.transfers),
            ("OptionEAE", "OptionEAE", out.option_eae)):
        dest.extend(_rows_in_section(stmt, section, tag))
    # OptionEAE rows are often the section element itself (no nested wrapper of the same name).
    if not out.option_eae:
        for el in _elems(stmt, "OptionEAE"):
            row = fields_of(el)
            if row and (row.get("transactionType") or row.get("symbol") or row.get("conid")):
                out.option_eae.append(row)
    best: tuple[date, dict] | None = None
    for el in list(_elems(stmt, "EquitySummaryByReportDateInBase")) + list(_elems(stmt, "EquitySummaryInBase")):
        row = fields_of(el)
        if not row:
            continue
        day = parse_ibkr_date(row.get("reportDate") or row.get("fromDate") or "") or date.min
        total = _num(row.get("total") or row.get("totalLong") or row.get("equity"))
        if total is None:
            continue
        cash = _num(row.get("cash"))
        stock = _num(row.get("stock") or row.get("securities"))
        cand = {"reportDate": day.isoformat() if day != date.min else None, "cash": cash,
                "invested": stock, "total": total, "currency": (row.get("currency") or "").upper() or None}
        out.equity_rows.append(cand)
        if best is None or day >= best[0]:
            best = (day, cand)
    out.equity = best[1] if best else None
    for section, tag in (("CashReport", "CashReportCurrency"), ("CashReport", "CashReport")):
        for row in _rows_in_section(stmt, section, tag):
            if row:
                out.cash_report.append(row)
    cash_end = _latest_cash_report(out.cash_report)
    if cash_end is not None and out.equity is not None and out.equity.get("cash") is None:
        out.equity["cash"] = cash_end
    elif cash_end is not None and out.equity is None:
        out.equity = {"reportDate": None, "cash": cash_end, "invested": None, "total": None, "currency": None}
    out.currency = _base_currency(sf, info, out.equity)
    return out


def _latest_cash_report(rows: list[dict]) -> float | None:
    best: tuple[date, float] | None = None
    for row in rows:
        lod = _lod(row)
        if lod and lod not in {"", "SUMMARY", "CURRENCY", "BASE_CURRENCY", "BASECURRENCY"}:
            continue
        cash = _num(row.get("endingCash") or row.get("endingSettledCash") or row.get("cash"))
        if cash is None:
            continue
        day = parse_ibkr_date(row.get("toDate") or row.get("reportDate") or row.get("fromDate") or "") or date.min
        if best is None or day >= best[0]:
            best = (day, float(cash))
    return best[1] if best else None


def parse_statements(xml_text: str) -> list[ParsedFlex]:
    """Every FlexStatement under the query. One statement's rows never leak into another."""
    root = _xml_root(xml_text)
    if _local(root.tag) == "FlexStatement":
        stmts = [root]
    else:
        stmts = [el for el in root.iter() if _local(el.tag) == "FlexStatement"]
    return [_parse_statement_el(s) for s in stmts]


def parse_statement(xml_text: str) -> ParsedFlex:
    """First FlexStatement (callers that only need one account). Rows are scoped to that statement."""
    stmts = parse_statements(xml_text)
    return stmts[0] if stmts else ParsedFlex()


def _xml_parse_error(exc: ET.ParseError) -> FlexError:
    line = None
    pos = getattr(exc, "position", None)
    if isinstance(pos, tuple) and pos:
        line = pos[0]
    if line is None:
        m = re.search(r"line\s+(\d+)", str(exc), re.I)
        if m:
            line = int(m.group(1))
    if line:
        return FlexError("api", f"IBKR Flex statement was not valid XML (parse error at line {line}).")
    return FlexError("api", "IBKR Flex statement was not valid XML.")


def _reject_unsafe_xml_file(path: Path) -> None:
    with open(path, "rb") as f:
        head = f.read(65536)
    text = head.decode("utf-8", errors="replace")
    if _UNSAFE_XML.search(text):
        raise FlexError("api", "IBKR Flex statement contained a DOCTYPE or ENTITY declaration.")


def looks_like_flex_bytes(data: bytes | str) -> bool:
    if isinstance(data, bytes):
        s = data.decode("utf-8", errors="replace")
    else:
        s = data or ""
    return looks_like_statement(s)


_SECTION_ROW = {
    "Trades": "Trade",
    "CashTransactions": "CashTransaction",
    "OpenPositions": "OpenPosition",
    "CorporateActions": "CorporateAction",
    "Transfers": "Transfer",
    "OptionEAE": "OptionEAE",
    "CashReport": "CashReport",
}
_ROW_ATTR = {
    "Trade": "trades",
    "CashTransaction": "cash",
    "OpenPosition": "positions",
    "CorporateAction": "corporate",
    "Transfer": "transfers",
    "OptionEAE": "option_eae",
}
_EQUITY_TAGS = {"EquitySummaryByReportDateInBase", "EquitySummaryInBase"}
_CASH_REPORT_ROWS = {"CashReportCurrency", "CashReport"}


def _drop_elem(parent: ET.Element | None, elem: ET.Element) -> None:
    elem.clear()
    if parent is None:
        return
    try:
        parent.remove(elem)
    except ValueError:
        pass


def _take_equity(out: ParsedFlex, row: dict, best: list) -> None:
    if not row:
        return
    day = parse_ibkr_date(row.get("reportDate") or row.get("fromDate") or "") or date.min
    total = _num(row.get("total") or row.get("totalLong") or row.get("equity"))
    if total is None:
        return
    cash = _num(row.get("cash"))
    stock = _num(row.get("stock") or row.get("securities"))
    cand = {"reportDate": day.isoformat() if day != date.min else None, "cash": cash,
            "invested": stock, "total": total, "currency": (row.get("currency") or "").upper() or None}
    out.equity_rows.append(cand)
    if not best or day >= best[0]:
        best[:] = [day, cand]
        out.equity = cand


def _finish_parsed(out: ParsedFlex, stmt_fields: dict, info: dict) -> ParsedFlex:
    cash_end = _latest_cash_report(out.cash_report)
    if cash_end is not None and out.equity is not None and out.equity.get("cash") is None:
        out.equity["cash"] = cash_end
    elif cash_end is not None and out.equity is None:
        out.equity = {"reportDate": None, "cash": cash_end, "invested": None, "total": None, "currency": None}
    out.currency = _base_currency(stmt_fields, info, out.equity)
    return out


def iter_parsed_statements(path: str | os.PathLike):
    """Yield one ParsedFlex per FlexStatement using iterparse; row elements are cleared after use."""
    p = Path(path)
    _reject_unsafe_xml_file(p)
    try:
        yield from _iterparse_statements(p)
    except ET.ParseError as exc:
        raise _xml_parse_error(exc) from exc


def parse_statements_from_path(path: str | os.PathLike) -> list[ParsedFlex]:
    """Same rows as parse_statements(text) for a file on disk. Does not load the file as str."""
    return list(iter_parsed_statements(path))


def _iterparse_statements(path: Path):
    """Streaming parse. Known sections only; extra sections are dropped when they close."""
    el_stack: list[ET.Element] = []
    tag_stack: list[str] = []
    cur: ParsedFlex | None = None
    stmt_fields: dict = {}
    info: dict = {}
    equity_best: list = []
    section_stack: list[str] = []
    option_eae_count_at_section = 0
    cash_report_count_at_section = 0

    def in_section() -> str | None:
        return section_stack[-1] if section_stack else None

    context = ET.iterparse(os.fspath(path), events=("start", "end"))
    for event, elem in context:
        tag = _local(elem.tag)
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = tag
        if event == "start":
            el_stack.append(elem)
            tag_stack.append(tag)
            if tag == "FlexStatement":
                cur = ParsedFlex()
                stmt_fields = { _local(k): str(v) for k, v in elem.attrib.items() if v is not None and str(v) != "" }
                cur.account_id = stmt_fields.get("accountId") or None
                cur.from_date = stmt_fields.get("fromDate")
                cur.to_date = stmt_fields.get("toDate")
                info = {}
                equity_best = []
                section_stack = []
            elif cur is not None and tag in _SECTION_ROW:
                parent = tag_stack[-2] if len(tag_stack) >= 2 else ""
                if parent == "FlexStatement" or not section_stack:
                    section_stack.append(tag)
                    if tag == "OptionEAE":
                        option_eae_count_at_section = len(cur.option_eae)
                    if tag == "CashReport":
                        cash_report_count_at_section = len(cur.cash_report)
            continue

        # end
        el_stack.pop()
        tag_stack.pop()
        parent = el_stack[-1] if el_stack else None
        parent_tag = tag_stack[-1] if tag_stack else ""

        if cur is None:
            _drop_elem(parent, elem)
            continue

        if tag == "AccountInformation":
            info = fields_of(elem)
            if info.get("accountId"):
                cur.account_id = info.get("accountId")
            _drop_elem(parent, elem)
            continue

        if tag == "FlexStatement":
            yield _finish_parsed(cur, stmt_fields, info)
            cur = None
            section_stack = []
            _drop_elem(parent, elem)
            continue

        sec = in_section()
        row_tag = _SECTION_ROW.get(sec or "")

        if sec and tag == sec and parent_tag == "FlexStatement":
            if tag == "OptionEAE" and len(cur.option_eae) == option_eae_count_at_section:
                row = fields_of(elem)
                if row and (row.get("transactionType") or row.get("symbol") or row.get("conid")):
                    cur.option_eae.append(row)
            if tag == "CashReport" and len(cur.cash_report) == cash_report_count_at_section:
                row = fields_of(elem)
                if row:
                    cur.cash_report.append(row)
            if section_stack and section_stack[-1] == tag:
                section_stack.pop()
            _drop_elem(parent, elem)
            continue

        if sec and tag == row_tag:
            row = fields_of(elem)
            dest = _ROW_ATTR.get(tag)
            if row and dest:
                getattr(cur, dest).append(row)
            _drop_elem(parent, elem)
            continue

        if sec == "CashReport" and tag in _CASH_REPORT_ROWS:
            row = fields_of(elem)
            if row:
                cur.cash_report.append(row)
            _drop_elem(parent, elem)
            continue

        if parent_tag == "FlexStatement" and tag in _ROW_ATTR:
            row = fields_of(elem)
            if row:
                getattr(cur, _ROW_ATTR[tag]).append(row)
            _drop_elem(parent, elem)
            continue

        if tag in _EQUITY_TAGS:
            _take_equity(cur, fields_of(elem), equity_best)
            _drop_elem(parent, elem)
            continue

        if parent_tag == "FlexStatement":
            _drop_elem(parent, elem)

    if cur is not None:
        yield _finish_parsed(cur, stmt_fields, info)


def maybe_gzip_xml_file(path: str | os.PathLike) -> tuple[bytes | None, int, str | None]:
    """Stream path → gzip on disk. Skip storing when raw > 20 MB and gzip > 8 MB."""
    p = Path(path)
    size = p.stat().st_size
    fd, tmp = tempfile.mkstemp(prefix="ibkr-gz-")
    os.close(fd)
    try:
        with open(p, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            while True:
                chunk = src.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        gz_size = os.path.getsize(tmp)
        if size > RAW_STORE_UNCOMPRESSED and gz_size > RAW_STORE_GZIP_MAX:
            return None, size, "raw XML not stored (gzipped size over 8 MB)"
        with open(tmp, "rb") as f:
            return f.read(), size, None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def parsed_as_comparable(p: ParsedFlex) -> dict:
    """Stable dict for parser-equivalence tests."""
    return {
        "account_id": p.account_id, "currency": p.currency,
        "from_date": p.from_date, "to_date": p.to_date,
        "trades": p.trades, "cash": p.cash, "positions": p.positions,
        "corporate": p.corporate, "equity": p.equity, "equity_rows": p.equity_rows,
        "cash_report": p.cash_report, "transfers": p.transfers, "option_eae": p.option_eae,
    }


# ------------------------------------------------------------------ trades
@dataclass
class Skip:
    reason: str


def _side_of(row: dict, qty: float | None) -> str:
    raw = (row.get("buySell") or row.get("BuySell") or "").strip().upper()
    token = raw.split()[0] if raw else ""
    if token in {"BUY", "BOT", "SLONG"}:
        return "BUY"
    if token in {"SELL", "SLD", "SSHORT"}:
        return "SELL"
    if qty is not None and qty < 0:
        return "SELL"
    if qty is not None and qty > 0:
        return "BUY"
    return ""


def _effect_of(row: dict) -> str | None:
    raw = (row.get("openCloseIndicator") or row.get("OpenCloseIndicator") or "").strip().upper()
    if not raw:
        return None
    letters = {p.strip()[0] for p in raw.replace(",", ";").split(";") if p.strip()}
    if letters == {"O"}:
        return "OPEN"
    if letters == {"C"}:
        return "CLOSE"
    return None


def _when_of(row: dict) -> tuple[datetime | None, bool]:
    dt, known = parse_ibkr_dt(row.get("dateTime") or row.get("DateTime") or "")
    if dt is not None:
        return dt, known
    d = row.get("tradeDate") or row.get("TradeDate") or row.get("reportDate") or ""
    t = row.get("tradeTime") or row.get("TradeTime") or ""
    if d and t:
        return parse_ibkr_dt(f"{d};{t}")
    return parse_ibkr_dt(d)


def _fees_of(row: dict) -> float:
    comm = abs(_num(row.get("ibCommission") or row.get("IBCommission") or row.get("commission")) or 0.0)
    tax = abs(_num(row.get("taxes") or row.get("tax") or row.get("Taxes")) or 0.0)
    return round(comm + tax, 6)


def _asset_skip(cat: str) -> str | None:
    c = (cat or "").upper()
    if c in SUPPORTED_CATS or c == "":
        return None
    names = {"FUT": "futures", "FOP": "futures options", "CASH": "forex", "BOND": "bonds",
             "BILL": "bills", "CMDTY": "commodities", "CFD": "CFDs", "WAR": "warrants",
             "IOPT": "warrants", "BAG": "combos", "FUND": None}
    if c in names and names[c] is None:
        return None
    return names.get(c) or f"unsupported {c or 'asset'}"


def trade_to_record(row: dict, inst: Instrument | None = None, symbol: str | None = None
                    ) -> ExecRecord | Skip:
    lod = _lod(row)
    if lod and lod not in TRADE_LOD:
        return Skip(lod.lower().replace("_", " ") or "not execution")
    cat = (row.get("assetCategory") or row.get("AssetCategory") or "").upper()
    reason = _asset_skip(cat)
    if reason:
        return Skip(reason)
    ext = row.get("tradeID") or row.get("TradeID") or row.get("ibExecID") or row.get("ibexecid")
    if ext is None or str(ext).strip() == "":
        return Skip("no trade id")
    qty_raw = _num(row.get("quantity") or row.get("Quantity"))
    price = _num(row.get("tradePrice") or row.get("TradePrice") or row.get("price"))
    side = _side_of(row, qty_raw)
    when_local, time_known = _when_of(row)
    if qty_raw is None or abs(qty_raw) <= 0 or price is None or not side or when_local is None:
        return Skip("incomplete")
    qty = abs(qty_raw)
    und = (row.get("underlyingSymbol") or row.get("UnderlyingSymbol") or "").strip().upper()
    raw_sym = (row.get("symbol") or row.get("Symbol") or "").strip().upper()
    isin = (row.get("isin") or row.get("ISIN") or row.get("securityID") or "") or None
    listing = row.get("listingExchange") or row.get("ListingExchange") or row.get("exchange")
    exch = listing_exchange(listing, isin)
    ccy = (row.get("currency") or row.get("Currency") or "").upper() or None
    fx = _num(row.get("fxRateToBase") or row.get("fxRateToBase".lower()) or row.get("FXRateToBase"))
    if inst is not None:
        exch = inst.exchange or exch
        ccy = inst.currency or ccy
    tz = markets.exchange_tz(exch, "America/New_York")
    executed = local_to_utc_naive(when_local.date(), when_local.time() if time_known else time(16, 0), tz)
    trade_date = when_local.date()
    settle = parse_ibkr_date(row.get("settleDateTarget") or row.get("settleDate") or "")
    fees = _fees_of(row)
    slim = {k: row.get(k) for k in ("tradeID", "symbol", "underlyingSymbol", "assetCategory", "buySell",
                                    "quantity", "tradePrice", "ibCommission", "taxes", "currency",
                                    "fxRateToBase", "openCloseIndicator", "conid", "isin",
                                    "listingExchange", "dateTime", "putCall", "strike", "expiry",
                                    "multiplier", "levelOfDetail") if row.get(k) not in (None, "")}
    if cat in OPTION_CATS or (row.get("putCall") or row.get("strike") or row.get("expiry")):
        pc_raw = (row.get("putCall") or row.get("PutCall") or "").strip().upper()
        if pc_raw.startswith("C"):
            pc = "CALL"
        elif pc_raw.startswith("P"):
            pc = "PUT"
        else:
            return Skip("incomplete option")
        strike = _num(row.get("strike") or row.get("Strike"))
        expiry = parse_ibkr_date(row.get("expiry") or row.get("Expiry") or row.get("lastTradeDate") or "")
        if strike is None or expiry is None:
            return Skip("incomplete option")
        und = und or raw_sym.split()[0] if raw_sym else und
        if not und:
            return Skip("no symbol")
        mult = _num(row.get("multiplier") or row.get("Multiplier")) or 100.0
        sym = symbol or option_symbol(und, expiry, pc, strike)
        return ExecRecord(
            external_id=str(ext), symbol=sym, underlying=und, asset_type="OPTION",
            option_type=pc, strike=strike, expiration=expiry, multiplier=float(mult),
            side=side, quantity=round(qty, 8), price=float(price), executed_at=executed,
            fees=fees, time_known=time_known, position_effect=_effect_of(row), kind="TRADE",
            description=(row.get("description") or "")[:300] or None, raw=slim,
            trade_date=trade_date, currency=ccy, fx_to_account=fx, settle_date=settle,
            instrument_id=inst.id if inst is not None else None,
        )
    sym = symbol or (inst.symbol if inst is not None else None) or raw_sym
    if not sym:
        return Skip("no symbol")
    return ExecRecord(
        external_id=str(ext), symbol=sym, underlying=sym, asset_type="STOCK", side=side,
        quantity=round(qty, 8), price=float(price), executed_at=executed, fees=fees,
        time_known=time_known, position_effect=_effect_of(row), kind="TRADE",
        description=(row.get("description") or "")[:300] or None, raw=slim,
        trade_date=trade_date, currency=ccy, fx_to_account=fx, settle_date=settle,
        instrument_id=inst.id if inst is not None else None, multiplier=1.0,
    )


def skip_ticker(row: dict) -> str:
    return (row.get("symbol") or row.get("underlyingSymbol") or row.get("assetCategory") or "?")[:32]


# ------------------------------------------------------------------ cash / dividends / interest
_DIV_TYPES = {"DIVIDENDS", "DIVIDEND", "PAYMENT IN LIEU OF DIVIDENDS", "PAYMENTINLIEU",
              "PAYMENT IN LIEU OF DIVIDEND"}
_WH_TYPES = {"WITHHOLDING TAX", "WITHHOLDINGTAX", "WITHHOLDING"}
_DEP_TYPES = {"DEPOSITS/WITHDRAWALS", "DEPOSIT/WITHDRAWAL", "DEPOSITS", "WITHDRAWALS",
              "DEPOSIT", "WITHDRAWAL", "TRANSFERS", "TRANSFER"}
_INT_RECV = {"BROKER INTEREST RECEIVED", "BROKERINTERESTRECEIVED", "INTEREST RECEIVED"}
_INT_PAID = {"BROKER INTEREST PAID", "BROKERINTERESTPAID", "INTEREST PAID"}


def _cash_type(row: dict) -> str:
    return re.sub(r"\s+", " ", (row.get("type") or row.get("Type") or "")).strip().upper()


def _cash_when(row: dict) -> datetime | None:
    dt, known = parse_ibkr_dt(row.get("dateTime") or row.get("reportDate") or row.get("date") or "")
    if dt is None:
        return None
    if known:
        return dt.replace(tzinfo=None)
    return datetime.combine(dt.date(), time(12, 0))


def _cash_id(row: dict) -> str | None:
    v = row.get("transactionID") or row.get("TransactionID") or row.get("actionID") or row.get("tradeID")
    if v is None or str(v).strip() == "":
        return None
    return str(v)


def cash_to_flow(row: dict, account_ccy: str | None) -> dict | None:
    if _lod(row) and _lod(row) not in CASH_LOD:
        return None
    typ = _cash_type(row)
    amt = _num(row.get("amount") or row.get("Amount"))
    when = _cash_when(row)
    ext = _cash_id(row)
    if amt is None or not amt or when is None or ext is None:
        return None
    ccy = (row.get("currency") or account_ccy or "").upper() or None
    fx = _num(row.get("fxRateToBase"))
    acct_amt = amt * fx if fx and ccy and account_ccy and ccy != (account_ccy or "").upper() else amt
    if typ in _DEP_TYPES:
        kind = "deposit" if acct_amt > 0 else "withdrawal"
        return {"external_id": ext, "occurred_at": when, "amount": round(acct_amt, 6),
                "currency": (account_ccy or ccy), "kind": kind,
                "description": (row.get("description") or typ.title())[:200]}
    if typ in _INT_RECV or typ in _INT_PAID or "INTEREST" in typ:
        if typ in _INT_PAID and acct_amt > 0:
            acct_amt = -acct_amt
        return {"external_id": ext, "occurred_at": when, "amount": round(acct_amt, 6),
                "currency": (account_ccy or ccy), "kind": "interest",
                "description": (row.get("description") or typ.title())[:200]}
    return None


def cash_to_dividend(row: dict, inst: Instrument | None = None, symbol: str | None = None,
                     account_ccy: str | None = None, net_amount: float | None = None):
    from app.dividends import DividendIn
    if _lod(row) and _lod(row) not in CASH_LOD:
        return None
    typ = _cash_type(row)
    is_div = typ in _DIV_TYPES or typ in _WH_TYPES
    if not is_div:
        return None
    ext = _cash_id(row)
    when = _cash_when(row)
    amt = net_amount if net_amount is not None else _num(row.get("amount") or row.get("Amount"))
    if ext is None or when is None or amt is None:
        return None
    sym = symbol or (inst.symbol if inst is not None else None) or (row.get("symbol") or "").strip().upper()
    if not sym:
        return None
    ccy = (row.get("currency") or "").upper() or None
    fx = _num(row.get("fxRateToBase"))
    acct_ccy = (account_ccy or ccy or "").upper() or None
    acct_amt = amt
    if fx and ccy and acct_ccy and ccy != acct_ccy:
        acct_amt = amt * fx
    elif ccy == acct_ccy:
        fx = 1.0
    kind = "WITHHOLDING" if typ in _WH_TYPES else (typ.title() if typ else "DIVIDEND")
    return DividendIn(
        external_id=ext, symbol=sym, paid_on=when, amount=float(amt), currency=ccy,
        amount_account=float(acct_amt) if acct_amt is not None else None, account_currency=acct_ccy,
        fx_to_account=fx, quantity=_num(row.get("quantity")), kind=kind,
        ticker=row.get("symbol"), instrument_id=inst.id if inst is not None else None,
        raw={"type": typ, "conid": row.get("conid"), "isin": row.get("isin"), "amount": amt},
    )


def match_withholding(divs: list[dict], taxes: list[dict]) -> tuple[list[tuple[dict, float]], list[dict]]:
    """Pair withholding rows to a dividend on the same conid/symbol + date. Unmatched taxes are returned."""
    used: set[int] = set()
    paired: list[tuple[dict, float]] = []
    for d in divs:
        gross = _num(d.get("amount")) or 0.0
        dday = (_cash_when(d) or datetime.min).date()
        dcon = str(d.get("conid") or "")
        dsym = (d.get("symbol") or "").upper()
        extra = 0.0
        for i, t in enumerate(taxes):
            if i in used:
                continue
            tday = (_cash_when(t) or datetime.min).date()
            if tday != dday:
                continue
            tcon = str(t.get("conid") or "")
            tsym = (t.get("symbol") or "").upper()
            if dcon and tcon and dcon == tcon:
                extra += _num(t.get("amount")) or 0.0
                used.add(i)
            elif dsym and tsym and dsym == tsym:
                extra += _num(t.get("amount")) or 0.0
                used.add(i)
        paired.append((d, gross + extra))
    leftover = [t for i, t in enumerate(taxes) if i not in used]
    return paired, leftover


# ------------------------------------------------------------------ corporate actions
_SPLIT_CODES = {"FS", "RS", "SD", "SPLIT", "REVERSE SPLIT", "REVERSE_SPLIT", "STOCK SPLIT", "STOCK_SPLIT",
                "STOCK DIVIDEND", "STOCK_DIVIDEND"}
_FORWARD_SPLIT = {"FS", "SPLIT", "STOCK SPLIT", "STOCK_SPLIT", "SD", "STOCK DIVIDEND", "STOCK_DIVIDEND"}
_REVERSE_SPLIT = {"RS", "REVERSE SPLIT", "REVERSE_SPLIT"}
_RENAME_CODES = {"IC", "SYMBOL CHANGE", "SYMBOL_CHANGE", "TICKER CHANGE", "RENAME", "CUSIP CHANGE"}
_MERGE_CODES = {"MA", "TC", "TO", "MERGER", "ACQUISITION"}
_SPIN_CODES = {"SO", "SPIN", "SPINOFF", "SPIN OFF", "SPIN_OFF"}
_WORTHLESS_CODES = {"DW", "DELIST", "WORTHLESS", "DELISTED"}
_PARENS_TICKER = re.compile(r"\(([A-Z][A-Z0-9.]{0,7})\s*,")
_CHANGE_TO = re.compile(
    r"(?:SYMBOL|TICKER|NAME|CUSIP/?ISIN)\s+CHANGE\s+TO\s+([A-Z][A-Z0-9.]{0,7})\b", re.I)
_MERGE_TO = re.compile(
    r"\b(?:MERGED\s+INTO|ACQUIRED\s+BY)\s+([A-Z][A-Z0-9.]{0,7})\b", re.I)
_CASH_LEG = re.compile(r"(?:USD|EUR|GBP)\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_NOT_TICKER = {"INC", "CORP", "LTD", "LLC", "PLC", "THE", "CLASS", "ORD", "FOR", "CASH"}
_EAE_TRADE_CODES = {"A", "EX", "EP", "ASSIGN", "ASSIGNMENT", "EXERCISE", "EXPIRATION"}


def _ca_code(row: dict) -> str:
    return (row.get("type") or row.get("code") or row.get("Type") or "").strip().upper()


def _ca_when(row: dict) -> datetime | None:
    dt, _ = parse_ibkr_dt(row.get("dateTime") or row.get("reportDate") or row.get("exDate") or "")
    return dt


def _ca_id(row: dict) -> str:
    return str(row.get("actionID") or row.get("transactionID") or row.get("tradeID")
               or row.get("conid") or row.get("symbol") or "ca")


def _ca_action_id(row: dict) -> str:
    return str(row.get("actionID") or row.get("transactionID") or "")


def _conid_of(row: dict) -> str:
    return str(row.get("conid") or row.get("conId") or "").strip()


_CANCEL_TOKENS = {"CA", "CANCEL", "CANCELLED", "CANCELED"}
# "C" is a closing trade. "Ca" is a cancelled corporate booking.
_MERGER_WORD = re.compile(r"\b(merger|merged(?:\s+with|\s+into)?|acquired by)\b", re.I)
_SRC_CASH_STOCK = re.compile(
    r"^\s*([A-Z][A-Z0-9.]{0,7})\s*\([^)]*\)\s+CASH\s+AND\s+STOCK\s+MERGER\b", re.I)
_SRC_LEAD_PAREN = re.compile(
    r"^\s*([A-Z][A-Z0-9.]{0,7})\s*\([^)]*\)\s+MERGED\b", re.I)
_SRC_PAREN = re.compile(r"\(([A-Z][A-Z0-9.]{0,7})\)\s+MERGED\b", re.I)
_SRC_LEAD = re.compile(r"^\s*([A-Z][A-Z0-9.]{0,7})\s+MERGED\b", re.I)


def _row_cancelled(row: dict) -> bool:
    return bool(_trade_codes(row) & _CANCEL_TOKENS)


def _qty_sign(row: dict) -> str:
    qty = _num(row.get("quantity"))
    if qty is None or abs(qty) <= 1e-12:
        return ""
    return "-" if qty < 0 else "+"


def _ca_desc(row: dict) -> str:
    return row.get("description") or row.get("actionDescription") or row.get("Description") or ""


def dedupe_corporate(rows: list[dict]) -> list[dict]:
    """Same actionID + conid + quantity sign: keep DETAIL, drop SUMMARY duplicates.

    Opposite-sign rows both survive so a cancelled booking can be told from the live leg.
    A cancelled row is dropped when that action and conid also has a live row.
    """
    indexed: list[tuple[int, dict]] = []
    for i, row in enumerate(rows):
        lod = _lod(row)
        if lod and lod not in CA_LOD:
            continue
        indexed.append((i, row))
    by_leg: dict[tuple[str, str], list[int]] = {}
    for n, (_i, row) in enumerate(indexed):
        aid = str(_ca_action_id(row) or _ca_id(row))
        con = str(_conid_of(row) or (row.get("symbol") or ""))
        by_leg.setdefault((aid, con), []).append(n)
    drop: set[int] = set()
    for idxs in by_leg.values():
        live = [n for n in idxs if not _row_cancelled(indexed[n][1])]
        if live and len(live) < len(idxs):
            for n in idxs:
                if _row_cancelled(indexed[n][1]):
                    drop.add(n)
    ranked: dict[tuple, dict] = {}
    order: list[tuple] = []
    for n, (_i, row) in enumerate(indexed):
        if n in drop:
            continue
        aid = _ca_action_id(row) or _ca_id(row)
        con = _conid_of(row) or (row.get("symbol") or "")
        extra = "" if aid else f"{row.get('dateTime') or ''}|{row.get('quantity') or ''}"
        key = (str(aid), str(con), _qty_sign(row), extra)
        lod = _lod(row)
        prev = ranked.get(key)
        if prev is None:
            ranked[key] = row
            order.append(key)
            continue
        prev_lod = _lod(prev)
        if lod == "DETAIL" and prev_lod != "DETAIL":
            ranked[key] = row
        elif lod not in {"SUMMARY", ""} and prev_lod == "SUMMARY":
            ranked[key] = row
    return [ranked[k] for k in order]


def _looks_ticker(s: str | None) -> bool:
    t = (s or "").upper()
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", t)) and t not in _NOT_TICKER


def _new_ticker(symbol: str, desc: str) -> str:
    for rx in (_PARENS_TICKER, _CHANGE_TO, _MERGE_TO):
        m = rx.search(desc or "")
        if not m:
            continue
        new = m.group(1).upper()
        if new != (symbol or "").upper() and _looks_ticker(new):
            return new
    return ""


def _ca_stamp(when: datetime) -> datetime:
    if when.hour or when.minute or when.second:
        return when
    return datetime.combine(when.date(), time(20, 0))


def _ca_record(row: dict, *, ext: str, symbol: str, side: str, qty: float, price: float,
               kind: str, effect: str | None, when: datetime, desc: str) -> ExecRecord:
    return ExecRecord(
        external_id=ext[:200], symbol=symbol, underlying=symbol, asset_type="STOCK",
        side=side, quantity=round(abs(qty), 8), price=float(price or 0.0),
        executed_at=_ca_stamp(when), fees=0.0,
        time_known=bool(when.hour or when.minute), position_effect=effect, kind=kind,
        description=(desc or "")[:300] or None,
        raw={"type": _ca_code(row), "description": desc, "conid": row.get("conid"),
             "actionID": row.get("actionID") or row.get("transactionID"),
             "quantity": row.get("quantity"), "proceeds": row.get("proceeds") or row.get("amount"),
             "levelOfDetail": row.get("levelOfDetail")},
        trade_date=when.date(), currency=(row.get("currency") or "").upper() or None,
    )


def _cash_per_share(desc: str) -> float | None:
    m = _CASH_LEG.search(desc or "")
    return float(m.group(1)) if m else None


def _tc_group(gid: str, group: list[dict]) -> tuple[list[dict], list[ExecRecord]]:
    """Close old conid, open new at IBKR quantities, carry cost (priced later). Cash is recorded separately."""
    from app.corporate import ratio_from_text
    events: list[dict] = []
    recs: list[ExecRecord] = []
    when = next((_ca_when(r) for r in group if _ca_when(r)), None) or utcnow()
    old_legs, new_legs = [], []
    cash_amt = 0.0
    cash_ccy = None
    cash_sym = None
    for row in group:
        qty = _num(row.get("quantity"))
        proceeds = _num(row.get("proceeds"))
        amount = _num(row.get("amount"))
        ccy = (row.get("currency") or "").upper() or None
        desc = _ca_desc(row)
        per = _cash_per_share(desc)
        if proceeds is not None and abs(proceeds) > 1e-9:
            cash_amt += proceeds
            cash_ccy = ccy or cash_ccy
            cash_sym = (row.get("symbol") or cash_sym or "").upper() or None
        elif amount is not None and qty is not None and abs(qty) < 1e-9 and abs(amount) > 1e-9:
            cash_amt += amount
            cash_ccy = ccy or cash_ccy
        if qty is None or abs(qty) <= 1e-12:
            continue
        if qty < 0:
            old_legs.append(row)
        else:
            new_legs.append(row)
        if per and qty is not None and qty < 0 and (proceeds is None or abs(proceeds) <= 1e-9):
            cash_amt += per * abs(qty)
            cash_ccy = ccy or cash_ccy
            cash_sym = (row.get("symbol") or cash_sym or "").upper() or None
    if old_legs and not new_legs:
        for row in old_legs:
            sym = (row.get("symbol") or "").strip().upper()
            desc = _ca_desc(row)
            new = _new_ticker(sym, desc)
            ratio = ratio_from_text(desc)
            old_qty = abs(_num(row.get("quantity")) or 0.0)
            new_qty = (old_qty * ratio) if (ratio and old_qty) else old_qty
            if new and new_qty:
                synth = dict(row)
                synth["symbol"] = new
                synth["quantity"] = new_qty
                new_legs.append(synth)
    if not old_legs and new_legs:
        # only the new ticker was listed; description may name the old one
        for row in new_legs:
            # leave as a received conversion; old close is missing
            pass
    old_sym = (old_legs[0].get("symbol") or "").strip().upper() if old_legs else ""
    new_sym = (new_legs[0].get("symbol") or "").strip().upper() if new_legs else ""
    if not new_sym and old_legs:
        new_sym = _new_ticker(old_sym, _ca_desc(old_legs[0]))
    for row in old_legs:
        sym = (row.get("symbol") or old_sym).strip().upper()
        qty = abs(_num(row.get("quantity")) or 0.0)
        if not sym or qty <= 0:
            continue
        recs.append(_ca_record(
            row, ext=f"ca:{_ca_id(row)}:{_conid_of(row) or sym}:C", symbol=sym, side="SELL",
            qty=qty, price=0.0, kind="CONVERSION", effect="CLOSE", when=when,
            desc=_ca_desc(row) or f"{sym} conversion out"))
    for row in new_legs:
        sym = (row.get("symbol") or new_sym).strip().upper()
        qty = abs(_num(row.get("quantity")) or 0.0)
        if not sym or qty <= 0:
            continue
        recs.append(_ca_record(
            row, ext=f"ca:{_ca_id(row)}:{_conid_of(row) or sym}:O", symbol=sym, side="BUY",
            qty=qty, price=0.0, kind="CONVERSION", effect="OPEN", when=when,
            desc=_ca_desc(row) or f"{sym} conversion in"))
    text_ratio = None
    for row in list(old_legs) + list(new_legs):
        found = ratio_from_text(_ca_desc(row))
        if found:
            text_ratio = found
            break
    old_qty = abs(_num(old_legs[0].get("quantity")) or 0.0) if old_legs else 0.0
    new_qty = abs(_num(new_legs[0].get("quantity")) or 0.0) if new_legs else 0.0
    qty_ratio = (new_qty / old_qty) if old_qty > 1e-12 and new_qty > 1e-12 else None
    # Keep 102 FOR 1000 as 0.102. Do not snap it to a nearby "clean" split ratio.
    ratio = text_ratio if text_ratio is not None else qty_ratio
    pure_rename = abs(cash_amt) <= 1e-9 and (ratio is None or abs(float(ratio) - 1.0) <= 1e-9)
    if old_sym and new_sym and old_sym != new_sym:
        base = {"symbol": old_sym, "new_symbol": new_sym, "when": when, "merger": True,
                "old_conid": _conid_of(old_legs[0]) if old_legs else "",
                "new_conid": _conid_of(new_legs[0]) if new_legs else ""}
        day = when.date().isoformat()
        if pure_rename:
            events.append({**base, "kind": "RENAME", "key": f"rename:{old_sym}:{new_sym}:{day}"})
        else:
            events.append({**base, "kind": "MERGER", "ratio": float(ratio) if ratio else None,
                           "key": f"merger:{old_sym}:{new_sym}:{day}"})
    if abs(cash_amt) > 1e-9:
        events.append({"kind": "CASH", "symbol": cash_sym or old_sym or new_sym or "?",
                       "amount": round(cash_amt, 6), "currency": cash_ccy, "when": when,
                       "key": f"ibkr:ca-cash:{gid}",
                       "note": f"{cash_sym or old_sym}: merger cash proceeds {cash_amt:.2f}"})
    if old_legs and not new_sym:
        events.append({"kind": "ATTENTION", "symbol": old_sym or "?", "code": "merger",
                       "note": f"{old_sym or 'A ticker'}: IBKR merger/acquisition needs attention "
                               f"({(_ca_desc(old_legs[0]) or _ca_code(old_legs[0]))})."})
    return events, recs


def corporate_events(rows: list[dict]) -> tuple[list[dict], list[ExecRecord]]:
    """Splits / renames / TC mergers / SO / SD / DW. Unrecognised -> needs-attention. No invented fills."""
    from app.corporate import ratio_from_text, clean_ratio
    events: list[dict] = []
    recs: list[ExecRecord] = []
    rows = dedupe_corporate(rows)
    groups: dict[str, list[dict]] = {}
    rest: list[dict] = []
    for row in rows:
        code = _ca_code(row)
        desc = _ca_desc(row)
        blob = f"{code} {desc}"
        if code in _MERGE_CODES or _MERGER_WORD.search(blob):
            gid = _ca_action_id(row) or f"tc:{_ca_id(row)}"
            groups.setdefault(gid, []).append(row)
        else:
            rest.append(row)
    for row in rest:
        if _lod(row) and _lod(row) not in CA_LOD:
            continue
        sym = (row.get("symbol") or "").strip().upper()
        desc = row.get("description") or row.get("actionDescription") or row.get("Description") or ""
        code = _ca_code(row)
        when = _ca_when(row) or utcnow()
        key = f"ibkr:{_ca_id(row)}:{code or 'ca'}"
        blob = f"{code} {desc}"
        qty = _num(row.get("quantity"))
        if code in _SPIN_CODES or re.search(r"\bspin[- ]?off\b", blob, re.I):
            new = _new_ticker(sym, desc) or _new_ticker(sym, blob) or sym
            share_qty = abs(qty) if qty and abs(qty) > 1e-9 else None
            if new and share_qty:
                recs.append(_ca_record(
                    row, ext=f"ca:{_ca_id(row)}:{_conid_of(row) or new}:SO", symbol=new, side="BUY",
                    qty=share_qty, price=0.0, kind="SPIN_OFF", effect="OPEN", when=when, desc=desc or f"{new} spin-off"))
                if new != sym and sym:
                    events.append({"kind": "RENAME", "symbol": sym, "new_symbol": new, "when": when,
                                   "key": f"spin:{sym}:{new}:{when.date().isoformat()}"} )
            elif sym:
                events.append({"kind": "ATTENTION", "symbol": sym, "code": "ca",
                               "note": f"{sym}: IBKR spin-off has no usable quantity ({desc or code})."})
            continue
        if code in _WORTHLESS_CODES or re.search(r"\b(worthless|delist)", blob, re.I):
            share_qty = abs(qty) if qty and abs(qty) > 1e-9 else None
            if sym and share_qty:
                recs.append(_ca_record(
                    row, ext=f"ca:{_ca_id(row)}:{_conid_of(row) or sym}:DW", symbol=sym, side="SELL",
                    qty=share_qty, price=0.0, kind="ADJUST", effect="CLOSE", when=when,
                    desc=desc or f"{sym} delisted/worthless"))
            elif sym:
                events.append({"kind": "ATTENTION", "symbol": sym, "code": "ca",
                               "note": f"{sym}: IBKR delist/worthless has no quantity ({desc or code})."})
            continue
        if code in _SPLIT_CODES or re.search(r"\b(split|reverse split|stock dividend)\b", blob, re.I):
            ratio = ratio_from_text(desc) or ratio_from_text(blob)
            if ratio:
                ratio = clean_ratio(ratio) or ratio
            if ratio and abs(ratio - 1.0) > 1e-9 and sym:
                events.append({"kind": "SPLIT", "symbol": sym, "ratio": float(ratio), "when": when,
                               "key": f"broker:{key}:{ratio}"})
                recs.append(ExecRecord(
                    external_id=f"ca:{_ca_id(row)}", symbol=sym, underlying=sym, asset_type="STOCK",
                    side="BUY", quantity=float(ratio), price=0.0, executed_at=_ca_stamp(when),
                    fees=0.0, time_known=bool(when.hour or when.minute), position_effect="RATIO",
                    kind="SPLIT", description=(desc or f"Split ×{ratio:g}")[:300],
                    raw={"type": code, "description": desc, "conid": row.get("conid"),
                         "actionID": row.get("actionID") or row.get("transactionID")},
                    trade_date=when.date(), currency=(row.get("currency") or "").upper() or None,
                ))
            elif code in {"SD", "STOCK DIVIDEND", "STOCK_DIVIDEND"} and sym and qty and abs(qty) > 1e-9:
                recs.append(_ca_record(
                    row, ext=f"ca:{_ca_id(row)}:{_conid_of(row) or sym}:SD", symbol=sym, side="BUY",
                    qty=abs(qty), price=0.0, kind="DISTRIBUTION", effect="OPEN", when=when,
                    desc=desc or f"{sym} stock dividend"))
            elif sym:
                events.append({"kind": "ATTENTION", "symbol": sym, "code": "ratio",
                               "note": f"{sym}: IBKR split/corporate action has no usable ratio ({desc or code})."})
            continue
        if code in _RENAME_CODES or re.search(r"symbol change|ticker change|name change|cusip", blob, re.I):
            new = _new_ticker(sym, desc) or _new_ticker(sym, blob)
            if sym and new and when:
                events.append({"kind": "RENAME", "symbol": sym, "new_symbol": new, "when": when,
                               "key": f"rename:{sym}:{new}:{when.date().isoformat()}"})
            elif sym:
                events.append({"kind": "ATTENTION", "symbol": sym, "code": "rename",
                               "note": f"{sym}: IBKR symbol change is ambiguous ({desc or code})."})
            continue
        if not sym:
            continue
        events.append({"kind": "ATTENTION", "symbol": sym, "code": "ca",
                       "note": f"{sym}: unrecognised IBKR corporate action {code or ''} {desc}".strip()})
    for gid, group in _collapse_merger_groups(_orient_merger_groups(groups)).items():
        ev, rr = _tc_group(gid, group)
        events.extend(ev)
        recs.extend(rr)
    return events, recs


def _merger_source(desc: str) -> str:
    """Ticker that is merged away: the 'X MERGED WITH Y' / 'X CASH AND STOCK MERGER' source."""
    text = desc or ""
    for rx in (_SRC_CASH_STOCK, _SRC_LEAD_PAREN, _SRC_PAREN, _SRC_LEAD):
        matched = rx.search(text) if rx is _SRC_PAREN else rx.match(text)
        if not matched:
            continue
        token = matched.group(1).upper()
        if _looks_ticker(token):
            return token
    return ""


def _leg_symbol(row: dict) -> str:
    return (row.get("symbol") or "").strip().upper()


def _orient_merger_groups(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """One direction per action. old = negative quantity of the description's source; new = the other conid.

    A group whose negative leg is the other ticker is the reverse booking and is dropped entirely.
    """
    out: dict[str, list[dict]] = {}
    for gid, group in groups.items():
        source = ""
        for row in group:
            source = _merger_source(_ca_desc(row))
            if source:
                break
        by_con: dict[str, list[dict]] = {}
        for row in group:
            con = str(_conid_of(row) or _leg_symbol(row))
            by_con.setdefault(con, []).append(row)
        kept: list[dict] = []
        for rows_c in by_con.values():
            signs = {_qty_sign(r) for r in rows_c if _qty_sign(r)}
            if len(signs) < 2 or not source:
                kept.extend(rows_c)
                continue
            sym = _leg_symbol(rows_c[0])
            want = "-" if sym == source else "+"
            chosen = [r for r in rows_c if _qty_sign(r) == want]
            kept.extend(chosen or rows_c)
        symbols = {_leg_symbol(r) for r in kept if _leg_symbol(r)}
        neg = {_leg_symbol(r) for r in kept if (_num(r.get("quantity")) or 0) < -1e-12 and _leg_symbol(r)}
        if source and source in symbols and neg and source not in neg:
            continue
        if kept:
            out[gid] = kept
    return out


def _merger_fingerprint(group: list[dict]) -> tuple:
    parts = []
    for row in group:
        qty = _num(row.get("quantity"))
        if qty is None or abs(qty) <= 1e-12:
            continue
        digits = re.sub(r"\D", "", str(row.get("dateTime") or row.get("reportDate") or ""))[:8]
        sign = "-" if qty < 0 else "+"
        parts.append((digits, str(_conid_of(row) or _leg_symbol(row)), sign, round(abs(qty), 6)))
    return tuple(sorted(parts))


def _merger_cash_weight(group: list[dict]) -> float:
    total = 0.0
    for row in group:
        for key in ("proceeds", "amount"):
            value = _num(row.get(key))
            if value:
                total += abs(value)
    return total


def _collapse_merger_groups(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Same shares booked under two action ids count once. The row with real cash wins; else the first."""
    best: dict[tuple, tuple[float, str]] = {}
    order: list[tuple] = []
    for gid, group in groups.items():
        fp = _merger_fingerprint(group)
        weight = _merger_cash_weight(group)
        prev = best.get(fp)
        if prev is None:
            best[fp] = (weight, gid)
            order.append(fp)
        elif weight > prev[0] + 1e-9:
            best[fp] = (weight, gid)
    return {best[fp][1]: groups[best[fp][1]] for fp in order}


def _merger_row_ids(rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for row in rows or []:
        code = _ca_code(row)
        desc = _ca_desc(row)
        if code not in _MERGE_CODES and not _MERGER_WORD.search(f"{code} {desc}"):
            continue
        ident = str(_ca_id(row) or "")
        action = str(_ca_action_id(row) or "")
        if ident:
            ids.add(ident)
        if action:
            ids.add(action)
    return ids


def _drop_stale_merger_legs(db: Session, account_id: int, corporate_rows: list[dict],
                            kept_recs: list[ExecRecord], events: list[dict]) -> None:
    """Remove conversion legs and cash keys from a merger action this pass did not keep.

    Reprocess then drops a stored reverse booking without calling IBKR. The legs this pass
    emits are left in place and upserted by ingest.
    """
    from app.models import CashFlow, Execution, TradeFill
    ids = _merger_row_ids(corporate_rows)
    if not ids:
        return
    kept_exec = {r.external_id for r in kept_recs if r.kind == "CONVERSION" and r.external_id}
    kept_cash = {str(ev.get("key")) for ev in events if ev.get("kind") == "CASH" and ev.get("key")}
    doomed: list[Execution] = []
    for row in db.scalars(select(Execution).where(
            Execution.account_id == account_id, Execution.kind == "CONVERSION",
            Execution.source == SOURCE_KEY)):
        ext = row.external_id or ""
        parts = ext.split(":")
        if len(parts) < 3 or parts[0] != "ca":
            continue
        if parts[1] not in ids or ext in kept_exec:
            continue
        doomed.append(row)
    if doomed:
        ids_doomed = [r.id for r in doomed]
        for fill in db.scalars(select(TradeFill).where(TradeFill.execution_id.in_(ids_doomed))):
            db.delete(fill)
        for row in doomed:
            db.delete(row)
    cash_keys = [f"ibkr:ca-cash:{ident}" for ident in ids]
    for flow in db.scalars(select(CashFlow).where(
            CashFlow.account_id == account_id, CashFlow.source == SOURCE_KEY,
            CashFlow.external_id.in_(cash_keys))):
        if flow.external_id not in kept_cash:
            db.delete(flow)
    db.flush()


def _asof_digits(row: dict) -> str:
    raw = str(row.get("dateTime") or row.get("reportDate") or row.get("tradeDate") or "")
    return re.sub(r"\D", "", raw)


def _asof_wins(prev: dict, asof: str, prefer: bool) -> bool:
    """A later calendar day wins. The same day prefers an Open Positions row over a trade."""
    old_asof = str(prev.get("asof") or "")
    old_prefer = bool(prev.get("prefer"))
    if not old_asof and asof:
        return True
    if old_asof and not asof:
        return False
    if asof[:8] > old_asof[:8]:
        return True
    if asof[:8] < old_asof[:8]:
        return False
    if prefer and not old_prefer:
        return True
    if old_prefer and not prefer:
        return False
    return asof >= old_asof


def _conid_symbol_ok(row: dict, symbol: str) -> bool:
    cat = (row.get("assetCategory") or "").upper()
    if cat and cat not in STOCK_CATS:
        return False
    if not symbol or symbol.isdigit() or " " in symbol or not _looks_ticker(symbol):
        return False
    return True


def observe_conid_rows(db: Session, rows: list[dict], *, prefer: bool) -> None:
    """Remember IBKR's symbol for each conid. A later date replaces an earlier one."""
    if not rows:
        return
    try:
        cur = json.loads(get_state(db, CONID_SYMBOL_STATE) or "{}")
    except ValueError:
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    changed = False
    for row in rows:
        con = _conid_of(row)
        sym = (row.get("symbol") or "").strip().upper()
        if not con or not _conid_symbol_ok(row, sym):
            continue
        asof = _asof_digits(row)
        prev = cur.get(con)
        if isinstance(prev, dict) and not _asof_wins(prev, asof, prefer):
            continue
        cur[con] = {"symbol": sym, "asof": asof, "prefer": bool(prefer)}
        changed = True
    if changed:
        set_state(db, CONID_SYMBOL_STATE, json.dumps(cur))


def apply_conid_symbols(db: Session) -> list[int]:
    """Point each IBKR instrument and its stock fills at the current symbol for that conid.

    A manual override is left alone. Returns account ids whose fills changed.
    """
    try:
        cur = json.loads(get_state(db, CONID_SYMBOL_STATE) or "{}")
    except ValueError:
        return []
    if not isinstance(cur, dict) or not cur:
        return []
    from app.models import Execution
    touched: set[int] = set()
    insts = list(db.scalars(select(Instrument).where(
        Instrument.source_key == SOURCE_KEY, Instrument.conid.is_not(None))))
    for inst in insts:
        if inst.user_override or not inst.conid:
            continue
        info = cur.get(inst.conid)
        if not isinstance(info, dict):
            continue
        sym = (info.get("symbol") or "").strip().upper()
        if not sym:
            continue
        execs = list(db.scalars(select(Execution).where(Execution.instrument_id == inst.id)))
        changed = False
        if (inst.symbol or "").upper() != sym:
            inst.symbol = sym
            if inst.exchange:
                inst.price_symbol = markets.yahoo_symbol_for(sym, inst.exchange)
            changed = True
        for ex in execs:
            if (ex.asset_type or "") == "OPTION" or " " in (ex.symbol or ""):
                continue
            if (ex.symbol or "").upper() != sym or (ex.underlying or "").upper() != sym:
                ex.symbol = sym
                ex.underlying = sym
                changed = True
        if changed:
            touched.update(ex.account_id for ex in execs)
    if touched:
        db.flush()
    return sorted(touched)


def earliest_imported_day(db: Session, account_id: int, extra: list | None = None) -> date | None:
    """First date this account actually has a fill, cash row, or dividend. Not the history-start setting."""
    from sqlalchemy import func
    from app.models import CashFlow, Dividend, Execution
    days: list[date] = []
    for model, col in (
            (Execution, Execution.executed_at),
            (CashFlow, CashFlow.occurred_at),
            (Dividend, Dividend.paid_on)):
        value = db.scalar(select(func.min(col)).where(model.account_id == account_id))
        if value is None:
            continue
        days.append(value.date() if isinstance(value, datetime) else value)
    for item in extra or []:
        if isinstance(item, datetime):
            days.append(item.date())
        elif isinstance(item, date):
            days.append(item)
    return min(days) if days else None


def history_gap_sentence(db: Session) -> str:
    """Mention a 1003 gap only from history notes, gaps, and failed windows — not the whole job JSON."""
    hist = load_history(db)
    blobs: list[str] = []
    for note in _history_notes(hist):
        if "1003" in note:
            blobs.append(note)
    for item in _gap_windows(hist) + _failed_windows(hist):
        text = json.dumps(item, default=str)
        if "1003" in text:
            blobs.append(text)
    if not blobs:
        return ""
    years: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        for match in re.finditer(r"20\d\d", blob):
            year = match.group(0)
            if year not in seen:
                seen.add(year)
                years.append(year)
    if len(years) == 1:
        return f"{years[0]} had no statement (IBKR 1003) and can be uploaded as XML."
    if years:
        return f"{', '.join(years)} had no statement (IBKR 1003) and can be uploaded as XML."
    return "A window had no statement (IBKR 1003) and can be uploaded as XML."


def _with_gap(note: str, gap: str) -> str:
    if not gap or gap in note:
        return note
    return note.rstrip() + " " + gap


def outside_note(symbol: str, day: date | None, gap: str) -> str:
    when = day.isoformat() if day else "the earliest imported date"
    return _with_gap(
        f"{symbol}: opening fill is before {when} (earliest imported IBKR data).", gap)


def orphan_note(symbol: str, day: date | None, gap: str, *, exceeds: bool = False) -> str:
    when = day.isoformat() if day else "the earliest imported date"
    if exceeds:
        text = (f"{symbol}: closing fill exceeds the open position; opening buy is before "
                f"{when} (earliest imported IBKR data).")
    else:
        text = (f"{symbol}: closing fill with no open position; opening buy is before "
                f"{when} (earliest imported IBKR data).")
    return _with_gap(text, gap)


def _history_check(row) -> bool:
    note = (row.note or "").lower()
    key = row.external_key or ""
    return ("opening fill" in note or "opening buy is before" in note or "outside imported" in note
            or "earliest imported" in note or key.endswith(":outside") or key.endswith(":orphan"))


def _merger_check(row) -> bool:
    note = (row.note or "").lower()
    key = row.external_key or ""
    return "merger/acquisition needs attention" in note or key.endswith(":merger")


def finish_import_attention(db: Session, account_id: int, events: list[dict]) -> list[str]:
    """Drop stale merger warnings, keep one outside-history note per merger, and fix the date wording."""
    from app.adjustments import dedupe_checks
    from app.models import CorporateAdjustment
    pairs: list[tuple[str, str]] = []
    outside: set[str] = set()
    for ev in events or []:
        if ev.get("merger") and ev.get("symbol") and ev.get("new_symbol"):
            pairs.append(((ev.get("symbol") or "").upper(), (ev.get("new_symbol") or "").upper()))
        if ev.get("kind") == "ATTENTION" and (ev.get("code") or "") == "outside":
            outside.add((ev.get("symbol") or "").upper())
    if pairs:
        rows = list(db.scalars(select(CorporateAdjustment).where(
            CorporateAdjustment.account_id == account_id, CorporateAdjustment.kind == "CHECK")))
        doomed: set[int] = set()
        for old, new in pairs:
            for row in rows:
                if row.id in doomed:
                    continue
                sym = (row.symbol or "").upper()
                if sym not in {old, new}:
                    continue
                if _merger_check(row):
                    doomed.add(row.id)
                    continue
                if not _history_check(row):
                    continue
                # One outside-history note, on the old symbol, and only when its opening buy is missing.
                if old not in outside or sym != old or (row.external_key or "").endswith(":orphan"):
                    doomed.add(row.id)
        for row in rows:
            if row.id in doomed:
                db.delete(row)
        if doomed:
            db.flush()
    day = earliest_imported_day(db, account_id)
    gap = history_gap_sentence(db)
    rows = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == account_id, CorporateAdjustment.kind == "CHECK",
        CorporateAdjustment.status == "needs_attention")))
    for row in rows:
        if not _history_check(row):
            continue
        low = (row.note or "").lower()
        key = row.external_key or ""
        sym = row.symbol or "?"
        if key.endswith(":orphan") or ("closing fill" in low and not key.endswith(":outside")):
            row.note = orphan_note(sym, day, gap, exceeds="exceeds" in low)
        else:
            row.note = outside_note(sym, day, gap)
    dedupe_checks(db, account_id)
    db.flush()
    return []


def position_map(rows: list[dict], instruments: dict[str, Instrument] | None = None) -> dict[str, float]:
    """Open quantity by journal symbol. ``instruments`` maps conid -> Instrument (conid first)."""
    out: dict[str, float] = {}
    for row in rows:
        lod = _lod(row)
        if lod and lod not in POS_LOD:
            continue
        cat = (row.get("assetCategory") or "").upper()
        if cat and cat not in STOCK_CATS:
            continue
        sym = (row.get("symbol") or "").strip().upper()
        qty = _num(row.get("position") or row.get("quantity"))
        if qty is None:
            continue
        con = _conid_of(row)
        inst = (instruments or {}).get(con) if con else None
        key = ((inst.symbol if inst is not None else None) or sym or "").strip().upper()
        if not key or " " in key:
            continue
        out[key] = out.get(key, 0.0) + float(qty)
    return out


def _trade_codes(row: dict) -> set[str]:
    raw = row.get("code") or row.get("codes") or row.get("notes") or ""
    return {p.strip().upper() for p in re.split(r"[;,]", str(raw)) if p.strip()}


def transfer_to_record(row: dict, inst: Instrument | None = None, symbol: str | None = None
                       ) -> ExecRecord | Skip | None:
    """Position transfer in/out. Needs quantity (and cost when present); otherwise Skip."""
    qty = _num(row.get("quantity") or row.get("position") or row.get("shares"))
    if qty is None or abs(qty) <= 1e-12:
        return Skip("transfer incomplete")
    typ = (row.get("type") or row.get("direction") or row.get("transferType") or "").upper()
    if "OUT" in typ:
        side = "SELL"
    elif "IN" in typ:
        side = "BUY"
    else:
        side = "SELL" if qty < 0 else "BUY"
    qty = abs(qty)
    cost = _num(row.get("costBasis") or row.get("positionAmount") or row.get("value"))
    price = (abs(cost) / qty) if cost is not None and qty else 0.0
    when_local, time_known = _when_of(row)
    if when_local is None:
        return Skip("transfer incomplete")
    ext = _cash_id(row) or row.get("transactionID") or row.get("tradeID") or row.get("actionID")
    if ext is None or str(ext).strip() == "":
        return Skip("transfer incomplete")
    raw_sym = (row.get("symbol") or "").strip().upper()
    sym = symbol or (inst.symbol if inst is not None else None) or raw_sym
    if not sym:
        return Skip("no symbol")
    isin = (row.get("isin") or row.get("ISIN") or "") or None
    exch = listing_exchange(row.get("listingExchange") or row.get("exchange"), isin)
    tz = markets.exchange_tz(exch or (inst.exchange if inst is not None else None), "America/New_York")
    executed = local_to_utc_naive(when_local.date(), when_local.time() if time_known else time(16, 0), tz)
    ccy = (row.get("currency") or (inst.currency if inst is not None else None) or "").upper() or None
    return ExecRecord(
        external_id=str(ext), symbol=sym, underlying=sym, asset_type="STOCK", side=side,
        quantity=round(qty, 8), price=float(price or 0.0), executed_at=executed, fees=0.0,
        time_known=time_known, position_effect="OPEN" if side == "BUY" else "CLOSE", kind="TRANSFER",
        description=(row.get("description") or typ or "Transfer")[:300] or None,
        raw={"type": typ, "conid": row.get("conid"), "quantity": row.get("quantity"),
             "costBasis": row.get("costBasis") or row.get("positionAmount")},
        trade_date=when_local.date(), currency=ccy,
        instrument_id=inst.id if inst is not None else None, multiplier=1.0,
    )


def option_eae_to_records(row: dict, inst: Instrument | None = None) -> list[ExecRecord | Skip]:
    """Assignment / exercise / expiration as IBKR reported them. No invented stock fills."""
    typ = (row.get("transactionType") or row.get("type") or row.get("code") or "").strip().upper()
    if typ in {"ASSIGN", "A"}:
        typ = "ASSIGNMENT"
    elif typ in {"EX", "EXERCISE"}:
        typ = "EXERCISE"
    elif typ in {"EP", "EXPIRATION", "EXPIRED", "L"}:
        typ = "EXPIRATION"
    if typ not in {"ASSIGNMENT", "EXERCISE", "EXPIRATION"}:
        return [Skip("option eae type")]
    qty = _num(row.get("quantity"))
    when_local, time_known = _when_of(row)
    ext = row.get("transactionID") or row.get("tradeID") or row.get("actionID") or row.get("conid")
    if qty is None or abs(qty) <= 1e-12 or when_local is None or not ext:
        return [Skip("incomplete option eae")]
    und = (row.get("underlyingSymbol") or "").strip().upper()
    raw_sym = (row.get("symbol") or "").strip().upper()
    pc_raw = (row.get("putCall") or "").strip().upper()
    if pc_raw.startswith("C"):
        pc = "CALL"
    elif pc_raw.startswith("P"):
        pc = "PUT"
    else:
        pc = None
    strike = _num(row.get("strike"))
    expiry = parse_ibkr_date(row.get("expiry") or row.get("lastTradeDate") or "")
    und = und or (raw_sym.split()[0] if raw_sym else "")
    if not und:
        return [Skip("no symbol")]
    tz = markets.exchange_tz("US", "America/New_York")
    executed = local_to_utc_naive(when_local.date(), when_local.time() if time_known else time(16, 0), tz)
    mult = _num(row.get("multiplier")) or 100.0
    px = _num(row.get("tradePrice") or row.get("closePrice") or row.get("markPrice")) or 0.0
    opt_sym = option_symbol(und, expiry, pc, strike) if (pc and strike is not None and expiry) else raw_sym
    kind = typ
    recs: list[ExecRecord | Skip] = [ExecRecord(
        external_id=f"eae:{ext}", symbol=opt_sym, underlying=und, asset_type="OPTION",
        option_type=pc, strike=strike, expiration=expiry, multiplier=float(mult),
        side=None, quantity=round(abs(qty), 8), price=float(px), executed_at=executed,
        fees=abs(_num(row.get("commisionsAndTax") or row.get("ibCommission")) or 0.0),
        time_known=time_known, position_effect="CLOSE", kind=kind,
        description=(row.get("description") or typ.title())[:300],
        raw={"transactionType": typ, "conid": row.get("conid"), "code": row.get("code")},
        trade_date=when_local.date(), currency=(row.get("currency") or "").upper() or None,
    )]
    # Resulting stock, only when IBKR reports it on this row (never invented).
    stock_qty = _num(row.get("underlyingQuantity") or row.get("deliveredQuantity"))
    stock_px = _num(row.get("underlyingPrice") or row.get("assignedPrice") or row.get("strike"))
    if stock_qty and abs(stock_qty) > 1e-12:
        side = "BUY" if stock_qty > 0 else "SELL"
        recs.append(ExecRecord(
            external_id=f"eae:{ext}:stk", symbol=und, underlying=und, asset_type="STOCK",
            side=side, quantity=round(abs(stock_qty), 8),
            price=float(stock_px if stock_px is not None else (strike or 0.0)),
            executed_at=executed, fees=0.0, time_known=time_known,
            position_effect="OPEN", kind="TRADE",
            description=(f"{typ} stock {und}")[:300],
            raw={"transactionType": typ, "from": str(ext), "conid": row.get("underlyingConid")},
            trade_date=when_local.date(), currency=(row.get("currency") or "").upper() or None,
            instrument_id=inst.id if inst is not None else None, multiplier=1.0,
        ))
    return recs


# ------------------------------------------------------------------ status / card
def test_connection(token: str, query_id: str, transport=None, sleep=None) -> dict:
    """Probe SendRequest + GetStatement. Returns {ok, account_id, currency, error, kind} with no token."""
    sleep = sleep or _time.sleep
    out = {"ok": False, "account_id": None, "currency": None, "error": None, "kind": None}
    cl = FlexClient(token, transport=transport, sleep=sleep)
    try:
        xml = cl.fetch(query_id)
        stmts = parse_statements(xml)
        first = stmts[0] if stmts else ParsedFlex()
        out["account_id"] = first.account_id
        out["currency"] = first.currency
        out["accounts"] = [{"account_id": s.account_id, "currency": s.currency or "USD"}
                           for s in stmts if s.account_id]
        out["ok"] = True
        return out
    except FlexError as exc:
        out["error"], out["kind"] = str(exc), exc.kind
        return out
    finally:
        cl.close()


def card(db: Session):
    from app.brokers import BrokerCard
    creds = load_credentials(db)
    st = last_status(db)
    form = "ibkr_connect.html"
    if not creds:
        return BrokerCard("ibkr", "Interactive Brokers", "Flex Web Service (token + query id)", "USD",
                          "not_connected", "Paste a Flex token and Query ID to sync executions.", form=form)
    state, detail = "connected", "Flex token saved."
    if st.get("error_kind") in ("auth", "query", "ip"):
        state, detail = "needs_login", st.get("error") or "Token or query rejected."
    elif st.get("accounts"):
        detail += f" {len(st['accounts'])} accounts · {st.get('currency') or creds.get('currency') or 'USD'}."
    elif st.get("account_id"):
        detail += f" Account {st['account_id']} · {st.get('currency') or creds.get('currency') or 'USD'}."
    elif creds.get("query_id"):
        detail += f" Query {creds['query_id']}."
    return BrokerCard("ibkr", "Interactive Brokers", "Flex Web Service (token + query id)",
                      st.get("currency") or creds.get("currency") or "USD",
                      state, detail, last_sync=st.get("synced_at"), form=form)


# ------------------------------------------------------------------ data source
class IbkrFlexSource(DataSource):
    key = SOURCE_KEY
    name = "Interactive Brokers"
    description = ("IBKR Flex Web Service: one Activity Flex Query (XML) for every account on the token. "
                   "Executions, cash, dividends, interest, open positions and corporate actions. "
                   "Full history walks year-sized windows from the start date on the card. Manual sync only.")

    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=None, split_fetch=None):
        self._transport, self._sleep = transport, sleep or _time.sleep
        # IBKR reports FS/RS itself. Yahoo guesses are not evidence.
        self.split_fetch = split_fetch if split_fetch is not None else (lambda *_a, **_k: [])

    def is_configured(self) -> bool:
        from app.config import get_settings
        if not get_settings().token_encryption_key:
            return False
        from app import db as dbmod
        db = dbmod.SessionLocal()
        try:
            return load_credentials(db) is not None
        except Exception:  # noqa: BLE001
            return False
        finally:
            db.close()

    def client(self, db: Session) -> FlexClient:
        c = load_credentials(db)
        if not c:
            raise FlexError("auth", "Interactive Brokers is not connected (no Flex token stored).")
        return FlexClient(c["token"], transport=self._transport, sleep=self._sleep)

    def status(self, db: Session) -> SourceStatus:
        creds = load_credentials(db)
        if not creds:
            return SourceStatus(False, False, "Not connected: add the Flex token and Query ID in Settings → Brokers.")
        st = last_status(db)
        if st.get("error_kind") in ("auth", "query", "ip"):
            return SourceStatus(True, False, st.get("error") or "Token or query rejected.", "error",
                                banner={"level": "error", "text": "Interactive Brokers: " + (st.get("error") or "token rejected"),
                                        "link": "/settings#brokers", "link_text": "Fix token"})
        return SourceStatus(True, True, "Connected" + (f" · last sync {st['synced_at']}" if st.get("synced_at") else ""))

    def ensure_account(self, db: Session, info: dict, default_ccy: str = "USD") -> Account:
        """Map an IBKR accountId to one journal account. The name is set only on creation
        (IBKR U1234567) so a later rename sticks. The id is stored on the row and in app_state."""
        ext = str(info.get("account_id") or info.get("id") or "").strip()
        ccy = ((info.get("currency") or default_ccy or "USD").strip().upper() or "USD")[:3]
        amap = load_account_map(db)
        a = None
        if ext and str(amap.get(ext) or "").isdigit():
            found = db.get(Account, int(amap[ext]))
            if found is not None and (found.source_key or SOURCE_KEY) in {SOURCE_KEY, "ibkr", ""}:
                a = found
        if a is None and ext:
            a = db.scalar(select(Account).where(Account.source_key == SOURCE_KEY, Account.external_ref == ext))
        if a is None:
            masked = ("…" + ext[-4:]) if len(ext) >= 4 else (ext or None)
            a = Account(name=(f"IBKR {ext}" if ext else "Interactive Brokers"), broker="ibkr",
                        external_ref=ext or None, source_key=SOURCE_KEY, account_number_masked=masked,
                        currency=ccy, timezone="America/New_York", market="US", kind="margin")
            db.add(a)
            db.flush()
        else:
            if ext and not a.external_ref:
                a.external_ref = ext
            if not a.source_key:
                a.source_key = SOURCE_KEY
            if not a.broker:
                a.broker = "ibkr"
        if ext:
            amap[ext] = a.id
            save_account_map(db, amap)
        return a

    def _wanted_ibkr_id(self, db: Session, ctx: SyncContext) -> str | None:
        """None imports every statement. A string imports only that IBKR accountId."""
        if ctx.account_id is None:
            return None
        acct = db.get(Account, ctx.account_id)
        if acct is None:
            return ""
        for ibkr_id, db_id in load_account_map(db).items():
            try:
                if int(db_id) == acct.id:
                    return str(ibkr_id)
            except (TypeError, ValueError):
                continue
        return str(acct.external_ref or "")

    def _pace(self, *, between: bool, prev_calls: int) -> None:
        gap = history_gap(prev_calls) if between else 0.0
        gap = max(gap, _send_wait())
        if gap > 0:
            self._sleep(gap)
            _release_send_gate()

    def _publish(self, db: Session, ctx: SyncContext, hist: dict, account_ids: list[int] | None = None) -> None:
        save_history(db, hist)
        text = progress_line(hist)
        if ctx.run_id and text:
            from app.models import SyncRun
            run = db.get(SyncRun, ctx.run_id)
            if run is not None and run.status == "running":
                run.message = text
        # The curve is rebuilt once, after every window, from sync(). account_ids is unused here.
        db.commit()

    def _fail_window(self, db: Session, ctx: SyncContext, hist: dict, exc: BaseException, token: str,
                     fd: date | None = None, td: date | None = None,
                     remaining: list[tuple[date, date]] | None = None) -> str:
        msg = redact(str(exc), token)
        hard = _is_hard_pause(exc)
        hist["status"] = "paused" if hard or (isinstance(exc, FlexError) and exc.kind == "rate") else "error"
        hist["error"] = msg[:500]
        hist["paused_until"] = _paused_until_text() if hard else None
        code = exc.code if isinstance(exc, FlexError) else None
        kind = exc.kind if isinstance(exc, FlexError) else "api"
        is_temp = isinstance(exc, FlexError) and (
            exc.kind == "temporary" or (code or "") in {"1001", "1003"})
        failed = _failed_windows(hist)
        if fd and td and (is_temp or remaining):
            split = next_split_level(fd, td) if is_temp else SPLIT_NONE
            failed = _add_failed(failed, _window_entry(fd, td, code, kind, split=split, message=msg))
            for fd2, td2 in remaining or []:
                failed = _add_failed(
                    failed, _window_entry(fd2, td2, code, kind, split=SPLIT_NONE, message=msg))
            hist["failed_windows"] = failed
        save_history(db, hist)
        text = progress_line(hist)
        _save_last(db, ok=False, error=msg, error_kind=kind,
                   failed_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))
        if ctx.run_id and text:
            from app.models import SyncRun
            run = db.get(SyncRun, ctx.run_id)
            if run is not None:
                run.message = text
        if text:
            ctx.info(text)
        db.commit()
        return msg

    def _plan(self, db: Session, full_flag: bool) -> tuple[list[tuple[date, date]], dict, str]:
        today = _today()
        start = history_start_date(db)
        prev = load_history(db)
        status = prev.get("status") or ""
        nxt = _parse_iso(prev.get("next_fd"))
        covered_from = _parse_iso(prev.get("covered_from") or prev.get("start"))
        resuming = status in {"paused", "error", "running"} and nxt is not None
        retry: list[tuple[date, date]] = []
        kept_failed: list[dict] = []
        seen_retry: set[tuple[date, date]] = set()
        for fw in _failed_windows(prev):
            pieces, kept = expand_failed_window(fw, today)
            if kept is not None:
                kept_failed = _add_failed(kept_failed, kept)
            for w in pieces:
                if w not in seen_retry:
                    seen_retry.add(w)
                    retry.append(w)
        completed_full = (
            (not resuming) and full_flag and prev.get("status") == "done"
            and prev.get("mode") == "full" and covered_from is not None
        )
        if resuming:
            cap = today
            if covered_from is not None and nxt is not None and nxt < covered_from:
                cap = min(today, covered_from - timedelta(days=1))
            sequential = [] if nxt > cap else iter_windows(nxt, cap)
            done = int(prev.get("done_windows") or 0)
            mode = prev.get("mode") or ("full" if full_flag else "new")
        elif completed_full:
            if start < covered_from:
                sequential = iter_windows(start, covered_from - timedelta(days=1))
            else:
                sequential = []
            done = int(prev.get("done_windows") or 0)
            mode = "full"
        elif full_flag:
            sequential = iter_windows(start, today)
            done = 0
            mode = "full"
        else:
            last = _parse_iso(prev.get("last_synced"))
            if last:
                origin = last - timedelta(days=OVERLAP_DAYS)
            else:
                origin = max(start, today - timedelta(days=MAX_WINDOW_DAYS - 1))
            if origin > today:
                sequential = []
            else:
                sequential = iter_windows(origin, today)
            done = 0
            mode = "new"
        if retry and sequential:
            origin = sequential[0][0]
            if any(fd <= origin <= td for fd, td in retry):
                after = max(td for _fd, td in retry) + timedelta(days=1)
                sequential = [] if after > today else iter_windows(after, today)
        seen: set[tuple[date, date]] = set()
        windows: list[tuple[date, date]] = []
        for w in retry + sequential:
            if w in seen:
                continue
            seen.add(w)
            windows.append(w)
        seq_next = sequential[0][0] if sequential else (
            nxt if (resuming and nxt) else (today + timedelta(days=1)))
        retry_n = 0
        for w in windows:
            if w in seen_retry:
                retry_n += 1
            else:
                break
        hist = {
            "start": start.isoformat(),
            "covered_from": (covered_from or start).isoformat(),
            "next_fd": seq_next.isoformat(),
            "seq_fd": seq_next.isoformat(),
            "done_windows": done,
            "total_windows": done + len(windows),
            "status": "running" if windows else "done",
            "paused_until": None,
            "error": None,
            "last_synced": prev.get("last_synced"),
            "mode": mode,
            "end": today.isoformat(),
            "notes": _history_notes(prev) if (resuming or retry or completed_full) else [],
            "failed_windows": kept_failed,
            "gaps": _gap_windows(prev),
            "retry_n": retry_n,
        }
        return windows, hist, mode

    def _seal_history(self, hist: dict, today: date) -> bool:
        """After filling a hole before already-covered dates, restore the cursor. Never deletes data."""
        if (hist.get("status") or "") in {"error", "paused"}:
            return False
        nxt = _parse_iso(hist.get("next_fd"))
        cf = _parse_iso(hist.get("covered_from"))
        last = _parse_iso(hist.get("last_synced"))
        start = _parse_iso(hist.get("start"))
        if cf and nxt and nxt >= cf and last is not None:
            restore = last + timedelta(days=1)
            if restore > nxt:
                hist["next_fd"] = restore.isoformat()
                hist["seq_fd"] = restore.isoformat()
                hist["status"] = "done" if restore > today else "running"
                if start and start < cf:
                    hist["covered_from"] = start.isoformat()
                return True
        return False

    def _is_retry_window(self, hist: dict, fd: date) -> bool:
        seq = _parse_iso(hist.get("seq_fd"))
        return seq is not None and fd < seq

    def _finish_window(self, hist: dict, fd: date, td: date, today: date, *, drop_failed: bool = True) -> None:
        hist["done_windows"] = int(hist.get("done_windows") or 0) + 1
        hist["error"] = None
        hist["paused_until"] = None
        if drop_failed:
            hist["failed_windows"] = _drop_failed(_failed_windows(hist), fd, td)
        last = _parse_iso(hist.get("last_synced"))
        if last is None or td > last:
            hist["last_synced"] = td.isoformat()
        seq = _parse_iso(hist.get("seq_fd"))
        if seq is None or fd <= seq:
            nxt = td + timedelta(days=1)
            hist["next_fd"] = nxt.isoformat()
            hist["seq_fd"] = nxt.isoformat()
        else:
            nxt = seq
            hist["next_fd"] = seq.isoformat()
        hist["status"] = "done" if nxt > today else "running"

    def _on_1003(self, db: Session, ctx: SyncContext, hist: dict, fd: date, td: date, today: date) -> None:
        """No automatic retry. A year window is queued as quarters; a quarter becomes a history gap."""
        retry = self._is_retry_window(hist, fd)
        if is_year_sized(fd, td) and not retry:
            failed = _failed_windows(hist)
            entry = _window_entry(fd, td, "1003", "unavailable")
            if not any(x.get("fd") == entry["fd"] and x.get("td") == entry["td"] for x in failed):
                failed.append(entry)
            hist["failed_windows"] = failed
            note = f"IBKR 1003 for {_ymd(fd)}\u2013{_ymd(td)}; will retry as quarters on the next Sync"
        else:
            gaps = _gap_windows(hist)
            gap = {"fd": fd.isoformat(), "td": td.isoformat(), "code": "1003"}
            if not any(x.get("fd") == gap["fd"] and x.get("td") == gap["td"] for x in gaps):
                gaps.append(gap)
            hist["gaps"] = gaps
            note = f"no statement for {_ymd(fd)}\u2013{_ymd(td)} (IBKR 1003)"
            self._flag_history_gap(db, fd, td)
        notes = _history_notes(hist)
        if note not in notes:
            notes.append(note)
        hist["notes"] = notes
        # A year 1003 stays in failed_windows so the next press retries it as quarters.
        drop = not (is_year_sized(fd, td) and not retry)
        self._finish_window(hist, fd, td, today, drop_failed=drop)
        self._publish(db, ctx, hist)
        ctx.info(note)
        log.warning("IBKR Flex error 1003: %s", note)

    def _flag_history_gap(self, db: Session, fd: date, td: date) -> None:
        from app.adjustments import upsert
        note = (f"IBKR has no statement for {fd.isoformat()}..{td.isoformat()} (history gap). "
                "Upload an Activity Statement XML covering that window.")
        key = f"attn:gap:{fd.isoformat()}:{td.isoformat()}"
        for acct in db.scalars(select(Account).where(Account.source_key == SOURCE_KEY)):
            upsert(db, acct.id, key=key, symbol="HISTORY", kind="CHECK",
                   status="needs_attention", source="broker", note=note, when=datetime.combine(fd, time(0, 0)))

    def _import_statements(self, db: Session, statements: list[ParsedFlex], ctx: SyncContext,
                           default_ccy: str, res: SourceResult, *, final: bool,
                           xml_text: str | None = None, window: tuple[date, date] | None = None,
                           xml_gzip: bytes | None = None, xml_nbytes: int | None = None,
                           store_json: bool = True, commit_each: bool = False):
        from app.adjustments import detect_and_apply, record_payload, record_payload_gzipped
        from app.balance import save_ibkr_summary
        from app.services import IngestStats
        want = self._wanted_ibkr_id(db, ctx)
        touched: list[int] = []
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        notes: list[str] = []
        corps: list[str] = []
        pending: dict[int, list] = {}
        seen = False
        win_name = ""
        if window:
            win_name = f"{window[0].isoformat()}_{window[1].isoformat()}"
        for parsed in statements:
            if want is not None and (parsed.account_id or "") != want:
                continue
            seen = True
            acct = self.ensure_account(
                db, {"account_id": parsed.account_id, "currency": parsed.currency or default_ccy}, default_ccy)
            if acct.id not in res.accounts:
                res.accounts.append(acct.id)
            if acct.id not in touched:
                touched.append(acct.id)
            observe_conid_rows(db, parsed.trades, prefer=False)
            observe_conid_rows(db, parsed.positions, prefer=True)
            recs, sk, det = self.convert(db, parsed.trades, acct.id)
            xfer_recs, xfer_sk, xfer_det, xfer_attn = self._convert_transfers(db, parsed.transfers, acct.id)
            recs.extend(xfer_recs)
            sk.update(xfer_sk)
            for reason, tickers in xfer_det.items():
                lst = det.setdefault(reason, [])
                for t in tickers:
                    if t not in lst and len(lst) < 100:
                        lst.append(t)
            eae_recs, eae_sk = self._convert_option_eae(db, parsed.option_eae, parsed.trades, acct.id)
            recs.extend(eae_recs)
            sk.update(eae_sk)
            events, ca_recs = corporate_events(parsed.corporate)
            _drop_stale_merger_legs(db, acct.id, parsed.corporate, ca_recs, events)
            recs.extend(ca_recs)
            self._price_conversions(db, acct.id, recs, events)
            recs.sort(key=lambda r: (r.executed_at, r.external_id))
            payload = {
                "trades": parsed.trades, "cash": parsed.cash, "positions": parsed.positions,
                "corporate": parsed.corporate, "account_id": parsed.account_id,
                "equity": parsed.equity, "equity_rows": parsed.equity_rows,
                "cash_report": parsed.cash_report, "transfers": parsed.transfers,
                "option_eae": parsed.option_eae,
            }
            if store_json:
                record_payload(db, acct.id, SOURCE_KEY, payload, run_id=ctx.run_id,
                               filename=f"ibkr-{acct.id}-{win_name or 'stmt'}.json")
            if xml_gzip:
                record_payload_gzipped(
                    db, acct.id, SOURCE_KEY, xml_gzip, nbytes=int(xml_nbytes or 0),
                    run_id=ctx.run_id, filename=f"ibkr-{acct.id}-{win_name or 'stmt'}.xml")
            elif xml_text:
                record_payload(db, acct.id, SOURCE_KEY, xml_text, run_id=ctx.run_id, kind="xml",
                               filename=f"ibkr-{acct.id}-{win_name or 'stmt'}.xml")
            stats = IngestStats()
            if len(recs) <= INGEST_BATCH:
                stats = ingest_records(db, acct.id, SOURCE_KEY, recs)
            else:
                for off in range(0, len(recs), INGEST_BATCH):
                    part = ingest_records(db, acct.id, SOURCE_KEY, recs[off:off + INGEST_BATCH])
                    stats.inserted += part.inserted
                    stats.merged += part.merged
                    stats.duplicates += part.duplicates
                    if commit_each:
                        db.commit()
            res.fetched += len(recs)
            res.inserted += stats.inserted
            res.merged += stats.merged
            skipped.update(sk)
            for reason, tickers in det.items():
                lst = detail.setdefault(reason, [])
                for t in tickers:
                    if t not in lst and len(lst) < 100:
                        lst.append(t)
            cash_note = self._import_cash(db, acct, parsed.cash)
            if cash_note:
                notes.append(cash_note)
            cash_ca = self._import_ca_cash(db, acct, events)
            if cash_ca:
                notes.append(cash_ca)
            events = [e for e in events if e.get("kind") != "CASH"]
            pending.setdefault(acct.id, []).extend(events)
            for aid in apply_conid_symbols(db):
                if aid not in touched:
                    touched.append(aid)
            by_conid = {i.conid: i for i in db.scalars(select(Instrument).where(
                Instrument.source_key == SOURCE_KEY, Instrument.conid.is_not(None))) if i.conid}
            pos = position_map(parsed.positions, by_conid)
            if xfer_attn:
                from app.adjustments import upsert as _upsert
                for item in xfer_attn:
                    _upsert(db, acct.id, key=item["key"], symbol=item["symbol"], kind="CHECK",
                            status="needs_attention", source="broker", note=item["note"])
                    corps.append(item["note"])
            if final:
                corp = detect_and_apply(db, acct.id, fetch=self.split_fetch, positions=pos,
                                        broker_events=events)
            else:
                corp = detect_and_apply(db, acct.id, fetch=lambda *_a, **_k: [], positions=None,
                                        broker_events=events)
            corps.extend(corp)
            for line in corp:
                ctx.info("IBKR corporate: " + line)
            if parsed.equity:
                eq = dict(parsed.equity)
                ending = _latest_cash_report(parsed.cash_report)
                if ending is not None:
                    eq["cash"] = ending
                save_ibkr_summary(db, acct.id, eq, parsed.currency or acct.currency)
            self._save_equity_history(db, acct.id, parsed.equity_rows)
            if commit_each:
                db.commit()
        if touched:
            rebuild_trades(db, touched)
            if final:
                for aid in touched:
                    corps.extend(self._flag_orphans(db, aid, []))
            for aid in touched:
                finish_import_attention(db, aid, pending.get(aid) or [])
        if want is not None and not seen:
            ctx.info(f"No Flex statement for IBKR account {mask_account_id(want) or '(unknown)'} in this window.")
        return touched, skipped, detail, notes, corps

    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        """Fetch fd/td windows one at a time. A failed window does not move the cursor.

        Full history (FULL_STATE, or the card's Full history button) walks from the history
        start date through the last completed US business day. Sync new walks from the last
        synced date minus a few days. A paused or failed job resumes from the last completed
        window instead of starting over. One query fetch per window is shared by every
        account; a per-account sync keeps only that account's statement. Error 1003 on a
        year window is recorded and retried as quarters on the next press (no automatic
        retry). A quarter that is still 1003 is a history gap. Temporary errors (1001, …)
        stop the job; the next press retries a year as quarters and a quarter as months.
        """
        res = SourceResult()
        creds = load_credentials(db)
        if not creds:
            raise RuntimeError("Interactive Brokers is not connected (no Flex token stored).")
        token, qid = creds["token"], str(creds["query_id"])
        default_ccy = (creds.get("currency") or "USD").upper()
        full_flag = get_state(db, FULL_STATE) == "1"
        windows, hist, mode = self._plan(db, full_flag)
        retry_n = int(hist.get("retry_n") or 0)
        if full_flag:
            set_state(db, FULL_STATE, None)
        if not windows:
            hist["status"] = "done"
            self._publish(db, ctx, hist)
            end = hist.get("end") or _today().isoformat()
            res.message = f"Up to date through {end}"
            extra = progress_line(hist)
            if extra and extra not in res.message:
                res.message = f"{res.message}. {extra}"
            ctx.info(res.message.strip())
            return res
        if cooldown_left() > 0:
            hist["status"] = "paused"
            hist["paused_until"] = _paused_until_text()
            hist["error"] = (f"IBKR calls are paused for {int(cooldown_left() // 60) + 1} more minute(s) "
                             "after a rate-limit answer.")
            self._publish(db, ctx, hist)
            ctx.info(progress_line(hist))
            raise RuntimeError(hist["error"])
        all_skipped: Counter = Counter()
        all_detail: dict[str, list[str]] = {}
        cash_notes: list[str] = []
        corp_lines: list[str] = []
        prev_calls = 0
        today = _today()
        try:
            for i, (fd, td) in enumerate(windows):
                clamped = clamp_window(fd, td, today)
                if clamped is None:
                    continue
                fd, td = clamped
                hist["status"] = "running"
                hist["next_fd"] = fd.isoformat()
                hist["error"] = None
                hist["paused_until"] = None
                self._publish(db, ctx, hist)  # card poll shows the window we're about to fetch
                try:
                    if cooldown_left() > 0:
                        raise FlexError("rate", f"IBKR calls are paused for {int(cooldown_left() // 60) + 1} "
                                        "more minute(s) after a rate-limit answer.", "1018")
                    self._pace(between=i > 0, prev_calls=prev_calls)
                    if cooldown_left() > 0:
                        raise FlexError("rate", f"IBKR calls are paused for {int(cooldown_left() // 60) + 1} "
                                        "more minute(s) after a rate-limit answer.", "1018")
                    cl = FlexClient(token, transport=self._transport, sleep=self._sleep)
                    try:
                        try:
                            xml = cl.fetch(qid, fd=_ymd(fd), td=_ymd(td))
                        except FlexError as exc:
                            if (exc.code or "") != "1003":
                                raise
                            prev_calls = cl.calls
                            self._on_1003(db, ctx, hist, fd, td, today)
                            continue
                        prev_calls = cl.calls
                    finally:
                        cl.close()
                    final = td >= today or i + 1 == len(windows)
                    touched, skipped, detail, notes, corps = self._import_statements(
                        db, parse_statements(xml), ctx, default_ccy, res, final=final,
                        xml_text=xml, window=(fd, td))
                    all_skipped.update(skipped)
                    for reason, tickers in detail.items():
                        lst = all_detail.setdefault(reason, [])
                        for t in tickers:
                            if t not in lst and len(lst) < 100:
                                lst.append(t)
                    cash_notes.extend(notes)
                    corp_lines.extend(corps)
                    self._finish_window(hist, fd, td, today)
                    self._publish(db, ctx, hist, touched)
                except Exception as exc:
                    db.rollback()
                    hist = load_history(db)
                    seq = _parse_iso(hist.get("seq_fd"))
                    if seq is not None:
                        hist["next_fd"] = seq.isoformat()
                    elif (_parse_iso(hist.get("next_fd")) or date.min) > fd:
                        hist["next_fd"] = fd.isoformat()
                    elif not hist.get("next_fd"):
                        hist["next_fd"] = fd.isoformat()
                    hist.setdefault("done_windows", 0)
                    hist.setdefault("total_windows", len(windows))
                    hist.setdefault("start", history_start_date(db).isoformat())
                    rest = windows[i + 1:retry_n] if i < retry_n else []
                    rest = [clamp_window(a, b, today) for a, b in rest]
                    rest = [w for w in rest if w is not None]
                    msg = self._fail_window(db, ctx, hist, exc, token, fd, td, remaining=rest)
                    raise RuntimeError(msg) from exc
        except RuntimeError:
            raise
        except FlexError as exc:
            db.rollback()
            msg = self._fail_window(db, ctx, load_history(db) or hist, exc, token)
            raise RuntimeError(msg) from exc
        if self._seal_history(hist, today):
            save_history(db, hist)
        sk = _skip_summary(all_skipped, all_detail)
        accounts_n = len(res.accounts)
        res.message = (f"Interactive Brokers: {res.fetched} fills read, {res.inserted} new"
                       + (f" across {accounts_n} accounts" if accounts_n > 1 else ""))
        if sk:
            res.message += f"; skipped {sk}"
        if cash_notes:
            res.message += "; " + "; ".join(dict.fromkeys(cash_notes))
        if corp_lines:
            res.message += "; " + "; ".join(dict.fromkeys(corp_lines))[:500]
        if mode == "full":
            res.message += f" (full history: {hist.get('done_windows')} windows through {hist.get('last_synced')})"
        else:
            res.message += f" ({progress_line(hist)})"
        prog = progress_line(hist)
        if prog and prog not in res.message:
            res.message += " " + prog
        ctx.info(res.message)
        ids = []
        amap = load_account_map(db)
        for ibkr_id, db_id in amap.items():
            if int(db_id) in res.accounts:
                ids.append(str(ibkr_id))
        _save_last(db, reset=True, ok=True, error=None, error_kind=None,
                   synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                   account_id=(ids[0] if ids else None), accounts=ids,
                   currency=default_ccy, fetched=res.fetched, inserted=res.inserted,
                   skipped=dict(all_skipped), skipped_detail=all_detail,
                   adjustments=list(dict.fromkeys(corp_lines))[:30],
                   full=(mode == "full"), query_id=qid, last_synced=hist.get("last_synced"))
        db.commit()
        if res.accounts:
            from app import balance
            balance.after_sync(db, list(res.accounts))
        return res

    def _instrument_for(self, db: Session, row: dict, cache: dict[str, Instrument]) -> Instrument | None:
        raw_sym = (row.get("symbol") or "").strip().upper()
        conid = _conid_of(row)
        key = conid or raw_sym
        if not key:
            return None
        inst = cache.get(key) or (cache.get(conid) if conid else None)
        if inst is None:
            inst = symbolmap.upsert_ibkr(
                db, key, row.get("isin"), row.get("description") or raw_sym,
                row.get("currency"), listing_exchange(row.get("listingExchange"), row.get("isin")),
                display_symbol=raw_sym, conid=conid or None)
            cache[key] = inst
            if conid:
                cache[conid] = inst
        return inst

    def _open_qty_map(self, db: Session, account_id: int) -> dict[str, float]:
        from collections import defaultdict
        from app.corporate import open_state
        from app.models import Execution
        execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
        by: dict[str, list] = defaultdict(list)
        for e in execs:
            by[(e.symbol or "").upper()].append(e)
        return {sym: open_state(rows)[0] for sym, rows in by.items()}

    def convert(self, db: Session, rows: list[dict], account_id: int | None = None
                ) -> tuple[list[ExecRecord], Counter, dict]:
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        recs: list[ExecRecord] = []
        cache: dict[str, Instrument] = {}
        open_qty = self._open_qty_map(db, account_id) if account_id else {}

        def skip(reason: str, raw: dict) -> None:
            skipped[reason] += 1
            ticker = skip_ticker(raw)
            lst = detail.setdefault(reason, [])
            if ticker not in lst and len(lst) < 100:
                lst.append(ticker)

        ordered = sorted(rows, key=lambda r: (_when_of(r)[0] or datetime.min, str(r.get("tradeID") or "")))
        for row in ordered:
            lod = _lod(row)
            if lod and lod not in TRADE_LOD:
                skip(lod.lower().replace("_", " ") or "not execution", row)
                continue
            cat = (row.get("assetCategory") or "").upper()
            reason = _asset_skip(cat)
            if reason:
                skip(reason, row)
                continue
            inst = None
            if cat in STOCK_CATS or not cat:
                inst = self._instrument_for(db, row, cache)
            r = trade_to_record(row, inst, inst.symbol if inst is not None and cat not in OPTION_CATS else None)
            if isinstance(r, Skip):
                skip(r.reason, row)
                continue
            parts = self._split_co(r, row, open_qty)
            recs.extend(parts)
            for p in parts:
                key = (p.symbol or "").upper()
                signed = abs(p.quantity) * (1.0 if p.side == "BUY" else -1.0)
                open_qty[key] = open_qty.get(key, 0.0) + signed
        return recs, skipped, detail

    def _split_co(self, rec: ExecRecord, row: dict, open_qty: dict[str, float]) -> list[ExecRecord]:
        """Split openCloseIndicator 'C;O' into a close up to the open quantity and an opening remainder."""
        raw = (row.get("openCloseIndicator") or row.get("OpenCloseIndicator") or "").strip().upper()
        letters = {p.strip()[0] for p in raw.replace(",", ";").split(";") if p.strip()}
        if letters != {"C", "O"}:
            return [rec]
        key = (rec.symbol or "").upper()
        pos = open_qty.get(key, 0.0)
        close_q = min(rec.quantity, abs(pos))
        open_q = rec.quantity - close_q
        if close_q <= 1e-12:
            rec.position_effect = "OPEN"
            return [rec]
        if open_q <= 1e-12:
            rec.position_effect = "CLOSE"
            return [rec]
        from dataclasses import replace
        fee_c = rec.fees * close_q / rec.quantity if rec.quantity else 0.0
        fee_o = rec.fees - fee_c
        close_rec = replace(rec, quantity=round(close_q, 8), fees=round(fee_c, 6), position_effect="CLOSE")
        open_rec = replace(rec, external_id=f"{rec.external_id}:O", quantity=round(open_q, 8),
                           fees=round(fee_o, 6), position_effect="OPEN")
        return [close_rec, open_rec]

    def _convert_transfers(self, db: Session, rows: list[dict], account_id: int
                           ) -> tuple[list[ExecRecord], Counter, dict, list[dict]]:
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        recs: list[ExecRecord] = []
        attn: list[dict] = []
        cache: dict[str, Instrument] = {}
        for row in rows:
            inst = self._instrument_for(db, row, cache)
            r = transfer_to_record(row, inst, inst.symbol if inst is not None else None)
            if r is None:
                continue
            if isinstance(r, Skip):
                skipped[r.reason] += 1
                ticker = skip_ticker(row)
                lst = detail.setdefault(r.reason, [])
                if ticker not in lst and len(lst) < 100:
                    lst.append(ticker)
                if r.reason == "transfer incomplete":
                    sym = (row.get("symbol") or ticker or "?").upper()
                    attn.append({
                        "key": f"attn:{sym}:transfer:{row.get('transactionID') or row.get('tradeID') or 'x'}",
                        "symbol": sym,
                        "note": f"{sym}: IBKR transfer has no quantity/cost; not imported.",
                    })
                continue
            recs.append(r)
        return recs, skipped, detail, attn

    def _convert_option_eae(self, db: Session, rows: list[dict], trades: list[dict], account_id: int
                            ) -> tuple[list[ExecRecord], Counter]:
        skipped: Counter = Counter()
        recs: list[ExecRecord] = []
        # Skip OptionEAE that duplicate a Trades row already coded A / Ex / Ep.
        covered: set[tuple] = set()
        for t in trades:
            codes = _trade_codes(t)
            if not (codes & _EAE_TRADE_CODES):
                continue
            day = (_when_of(t)[0] or datetime.min).date()
            covered.add(((t.get("symbol") or "").upper(), day, round(abs(_num(t.get("quantity")) or 0), 4)))
            covered.add((_conid_of(t), day, round(abs(_num(t.get("quantity")) or 0), 4)))
        cache: dict[str, Instrument] = {}
        for row in rows:
            day = (_when_of(row)[0] or datetime.min).date()
            qty = round(abs(_num(row.get("quantity")) or 0), 4)
            if ((row.get("symbol") or "").upper(), day, qty) in covered or (_conid_of(row), day, qty) in covered:
                continue
            und_row = dict(row)
            und_row["symbol"] = row.get("underlyingSymbol") or row.get("symbol")
            inst = self._instrument_for(db, und_row, cache) if und_row.get("symbol") else None
            for r in option_eae_to_records(row, inst):
                if isinstance(r, Skip):
                    skipped[r.reason] += 1
                    continue
                recs.append(r)
        return recs, skipped

    def _price_conversions(self, db: Session, account_id: int, recs: list[ExecRecord],
                           events: list[dict]) -> None:
        """Carry cost basis from the old lots onto the new conversion BUY. Never invent an opening buy."""
        from collections import defaultdict
        from app.corporate import open_state
        from app.models import Execution
        sells = [r for r in recs if r.kind == "CONVERSION" and r.side == "SELL"]
        buys = [r for r in recs if r.kind == "CONVERSION" and r.side == "BUY"]
        if not sells and not buys:
            return
        existing = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
        by_sym: dict[str, list] = defaultdict(list)
        for e in existing:
            by_sym[(e.symbol or "").upper()].append(e)
        # Incoming trades in this batch are not in the DB yet.
        from types import SimpleNamespace
        for r in recs:
            if r.kind not in {"TRADE", "TRANSFER", "SPLIT", "SPIN_OFF", "DISTRIBUTION"}:
                continue
            by_sym[(r.symbol or "").upper()].append(SimpleNamespace(
                id=0, account_id=account_id, external_id=r.external_id, symbol=r.symbol,
                side=r.side, quantity=r.quantity, price=r.price, executed_at=r.executed_at,
                fees=r.fees or 0.0, multiplier=r.multiplier or 1.0, position_effect=r.position_effect,
                kind=r.kind or "TRADE", time_known=bool(r.time_known), seq=0))
        # Pair sells and buys on the same day (one TC group).
        for sell in sells:
            rows = [e for e in by_sym.get((sell.symbol or "").upper(), [])
                    if getattr(e, "executed_at", None) is None or e.executed_at <= sell.executed_at]
            qty, avg = open_state(rows)
            if abs(qty) <= 1e-8:
                day = earliest_imported_day(db, account_id, [r.executed_at for r in recs])
                events.append({
                    "kind": "ATTENTION", "symbol": sell.symbol, "code": "outside",
                    "note": outside_note(sell.symbol, day, history_gap_sentence(db)),
                })
                sell.price = 0.0
                continue
            sell.price = round(avg, 8)
            cost = avg * min(abs(qty), sell.quantity)
            same_day_buys = [b for b in buys if b.executed_at.date() == sell.executed_at.date()]
            new_qty = sum(b.quantity for b in same_day_buys) or 0.0
            # Full old cost stays on the new shares (cash is recorded separately as proceeds).
            if same_day_buys and new_qty > 1e-12:
                px = cost / new_qty
                for b in same_day_buys:
                    b.price = round(px, 8)
            # If the old lots don't cover the conversion qty, still don't invent a buy.

    def _import_ca_cash(self, db: Session, acct: Account, events: list[dict]) -> str:
        from app.balance import ingest_flows
        flows = []
        for ev in events:
            if ev.get("kind") != "CASH" or not ev.get("amount"):
                continue
            when = ev.get("when") or utcnow()
            flows.append({
                "external_id": str(ev.get("key") or f"ca-cash:{ev.get('symbol')}")[:200],
                "occurred_at": when, "amount": round(float(ev["amount"]), 6),
                "currency": ev.get("currency") or acct.currency, "kind": "proceeds",
                "description": (ev.get("note") or "Merger cash proceeds")[:200],
            })
        if not flows:
            return ""
        st = ingest_flows(db, acct.id, SOURCE_KEY, flows)
        if st["inserted"]:
            return f"merger cash {st['inserted']} new"
        return ""

    def _save_equity_history(self, db: Session, account_id: int, rows: list[dict]) -> None:
        if not rows:
            return
        key = f"balance:ibkr:hist:{account_id}"
        try:
            have = json.loads(get_state(db, key) or "{}")
        except ValueError:
            have = {}
        if not isinstance(have, dict):
            have = {}
        for r in rows:
            day = r.get("reportDate")
            if not day or r.get("total") is None:
                continue
            have[str(day)] = {"cash": r.get("cash"), "total": r.get("total"),
                              "invested": r.get("invested")}
        if len(have) > 800:
            keep = sorted(have)[-800:]
            have = {k: have[k] for k in keep}
        set_state(db, key, json.dumps(have))

    def _flag_orphans(self, db: Session, account_id: int, extra: list[dict]) -> list[str]:
        from app.adjustments import upsert
        from app.models import CorporateAdjustment
        from app.services import get_state as _gs
        lines = []
        day = earliest_imported_day(db, account_id)
        gap = history_gap_sentence(db)
        try:
            orphans = json.loads(_gs(db, f"orphans:{account_id}") or "[]")
        except ValueError:
            orphans = []
        current: set[str] = set()
        for o in orphans or []:
            reason = (o.get("reason") or "") if isinstance(o, dict) else str(getattr(o, "reason", ""))
            sym = (o.get("symbol") if isinstance(o, dict) else getattr(o, "symbol", None)) or "?"
            low = reason.lower()
            if "no open position" not in low and "exceeds open" not in low:
                continue
            # The conversion's own outside-history note already covers a merger with no opening buy.
            if low.startswith("conversion") and "no open position" in low:
                continue
            current.add(str(sym).upper())
            note = orphan_note(str(sym).upper(), day, gap, exceeds="exceeds open" in low and "no open position" not in low)
            _, created = upsert(db, account_id, key=f"attn:{str(sym).upper()}:orphan", symbol=str(sym).upper(),
                                kind="CHECK", status="needs_attention", source="broker", note=note)
            if created:
                lines.append(note)
        stale = list(db.scalars(select(CorporateAdjustment).where(
            CorporateAdjustment.account_id == account_id,
            CorporateAdjustment.kind == "CHECK",
            CorporateAdjustment.external_key.like("attn:%:orphan"))))
        removed = False
        for row in stale:
            if (row.symbol or "").upper() not in current:
                db.delete(row)
                removed = True
        if removed:
            db.flush()
        return lines

    def import_xml(self, db: Session, xml_text: str, *, filename: str | None = None,
                   run_id: int | None = None) -> SourceResult:
        """Feed an uploaded Activity Statement through the same parser, mapping and dedupe as Flex sync."""
        if xml_text is None or not str(xml_text).strip():
            raise FlexError("api", "The uploaded file was empty.")
        if len(xml_text.encode("utf-8", errors="replace")) > MAX_UPLOAD_BYTES:
            raise FlexError("api", "File too large (100 MB max).")
        stmts = parse_statements(xml_text)
        if not stmts:
            raise FlexError("api", "No FlexStatement in the uploaded XML.")
        res = SourceResult()
        ctx = SyncContext(run_id=run_id)
        creds = load_credentials(db) or {}
        default_ccy = (creds.get("currency") or "USD").upper()
        touched, skipped, detail, notes, corps = self._import_statements(
            db, stmts, ctx, default_ccy, res, final=True, xml_text=xml_text, window=None)
        sk = _skip_summary(skipped, detail)
        res.message = (f"Uploaded IBKR statement: {res.fetched} fills read, {res.inserted} new"
                       + (f"; skipped {sk}" if sk else ""))
        if notes:
            res.message += "; " + "; ".join(dict.fromkeys(notes))
        if corps:
            res.message += "; " + "; ".join(dict.fromkeys(corps))[:400]
        _save_last(db, ok=True, error=None, error_kind=None,
                   synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                   fetched=res.fetched, inserted=res.inserted, skipped=dict(skipped),
                   skipped_detail=detail, adjustments=list(dict.fromkeys(corps))[:30],
                   upload=True, filename=(filename or "")[:80])
        db.commit()
        return res

    def import_xml_path(self, db: Session, path: str | os.PathLike, *, filename: str | None = None,
                        run_id: int | None = None) -> SourceResult:
        """Parse a Flex XML file from disk (iterparse). Never holds the whole file as str/bytes."""
        p = Path(path)
        size = p.stat().st_size if p.is_file() else 0
        if size <= 0:
            raise FlexError("api", "The uploaded file was empty.")
        if size > MAX_UPLOAD_BYTES:
            raise FlexError("api", "File too large (100 MB max).")
        xml_gzip, xml_nbytes, xml_note = maybe_gzip_xml_file(p)
        store_json = size <= RAW_STORE_UNCOMPRESSED
        res = SourceResult()
        ctx = SyncContext(run_id=run_id)
        creds = load_credentials(db) or {}
        default_ccy = (creds.get("currency") or "USD").upper()
        pending: ParsedFlex | None = None
        n = 0
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        notes: list[str] = []
        corps: list[str] = []
        for parsed in iter_parsed_statements(p):
            if pending is not None:
                t, sk, det, nt, cp = self._import_statements(
                    db, [pending], ctx, default_ccy, res, final=False,
                    xml_gzip=xml_gzip, xml_nbytes=xml_nbytes, store_json=store_json,
                    commit_each=True)
                skipped.update(sk)
                for reason, tickers in det.items():
                    lst = detail.setdefault(reason, [])
                    for x in tickers:
                        if x not in lst and len(lst) < 100:
                            lst.append(x)
                notes.extend(nt)
                corps.extend(cp)
                db.commit()
            pending = parsed
            n += 1
        if pending is None:
            raise FlexError("api", "No FlexStatement in the uploaded XML.")
        t, sk, det, nt, cp = self._import_statements(
            db, [pending], ctx, default_ccy, res, final=True,
            xml_gzip=xml_gzip, xml_nbytes=xml_nbytes, store_json=store_json,
            commit_each=True)
        skipped.update(sk)
        for reason, tickers in det.items():
            lst = detail.setdefault(reason, [])
            for x in tickers:
                if x not in lst and len(lst) < 100:
                    lst.append(x)
        notes.extend(nt)
        corps.extend(cp)
        if xml_note:
            notes.append(xml_note)
        sks = _skip_summary(skipped, detail)
        res.message = (f"Uploaded IBKR statement: {res.fetched} fills read, {res.inserted} new"
                       + (f"; skipped {sks}" if sks else ""))
        if res.fetched and res.inserted == 0:
            res.message += "; nothing new"
        if notes:
            res.message += "; " + "; ".join(dict.fromkeys(notes))
        if corps:
            res.message += "; " + "; ".join(dict.fromkeys(corps))[:400]
        _save_last(db, ok=True, error=None, error_kind=None,
                   synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                   fetched=res.fetched, inserted=res.inserted, skipped=dict(skipped),
                   skipped_detail=detail, adjustments=list(dict.fromkeys(corps))[:30],
                   upload=True, filename=(filename or "")[:80])
        db.commit()
        return res

    def _import_cash(self, db: Session, acct: Account, rows: list[dict]) -> str:
        from app.balance import ingest_flows
        from app.dividends import ingest as ingest_divs
        notes = []
        flows, div_rows, tax_rows = [], [], []
        for row in rows:
            if _lod(row) and _lod(row) not in CASH_LOD:
                continue
            typ = _cash_type(row)
            if typ in _DIV_TYPES:
                div_rows.append(row)
                continue
            if typ in _WH_TYPES:
                tax_rows.append(row)
                continue
            fl = cash_to_flow(row, acct.currency)
            if fl:
                flows.append(fl)
        paired, leftover = match_withholding(div_rows, tax_rows)
        recs = []
        for row, net in paired:
            rec = cash_to_dividend(row, None, None, acct.currency, net_amount=net)
            if rec:
                recs.append(rec)
        for row in leftover:
            rec = cash_to_dividend(row, None, None, acct.currency)
            if rec:
                recs.append(rec)
        st = ingest_flows(db, acct.id, SOURCE_KEY, flows)
        if st["inserted"]:
            kinds = Counter(f["kind"] for f in flows)
            notes.append("cash flows " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()) if n)
                         + f" ({st['inserted']} new)")
        ds = ingest_divs(db, acct.id, SOURCE_KEY, recs)
        if recs or ds["inserted"]:
            notes.append(f"dividends {len(recs)} read, {ds['inserted']} new")
        return "; ".join(notes)

    def reprocess_stored(self, db: Session) -> SourceResult:
        """Replay stored IBKR JSON/XML. Idempotent. Never calls Interactive Brokers."""
        return reprocess_stored_payloads(db, source=self)


def parsed_from_payload(data: dict) -> ParsedFlex:
    p = ParsedFlex()
    p.account_id = data.get("account_id")
    p.currency = data.get("currency")
    p.trades = list(data.get("trades") or [])
    p.cash = list(data.get("cash") or [])
    p.positions = list(data.get("positions") or [])
    p.corporate = list(data.get("corporate") or [])
    p.equity = data.get("equity") if isinstance(data.get("equity"), dict) else None
    p.equity_rows = list(data.get("equity_rows") or [])
    p.cash_report = list(data.get("cash_report") or [])
    p.transfers = list(data.get("transfers") or [])
    p.option_eae = list(data.get("option_eae") or [])
    return p


def _backfill_conids(db: Session) -> int:
    n = 0
    rows = list(db.scalars(select(Instrument).where(Instrument.source_key == SOURCE_KEY)))
    for inst in rows:
        if inst.conid:
            continue
        key = (inst.broker_symbol or "").strip()
        if key.isdigit():
            inst.conid = key
            n += 1
    if n:
        db.flush()
    return n


def reprocess_stored_payloads(db: Session, source: IbkrFlexSource | None = None) -> SourceResult:
    """Re-run conid mapping, corporate dedupe and TC mergers from stored payloads. No IBKR HTTP."""
    from app.adjustments import payload_body
    from app.models import SyncPayload
    src = source or IbkrFlexSource(sleep=lambda _n: None, split_fetch=lambda *_a, **_k: [])
    _backfill_conids(db)
    rows = list(db.scalars(select(SyncPayload).where(SyncPayload.source == SOURCE_KEY)
                           .order_by(SyncPayload.id)))
    res = SourceResult()
    if not rows:
        set_state(db, REPROCESS_STATE, REPROCESS_VERSION)
        db.commit()
        res.message = "No stored IBKR payloads to reprocess."
        return res
    ctx = SyncContext()
    creds = load_credentials(db) or {}
    default_ccy = (creds.get("currency") or "USD").upper()
    # Rescan stored payloads from scratch so an older window cannot keep a stale symbol.
    set_state(db, CONID_SYMBOL_STATE, "{}")
    # Prefer XML (complete) over JSON when both exist for the same payload id order.
    seen_xml: set[tuple[int, str]] = set()
    for row in rows:
        body = payload_body(row)
        xml_text = None
        stmts: list[ParsedFlex] = []
        if (row.content_type or "").find("xml") >= 0 or (row.filename or "").endswith(".xml"):
            try:
                xml_text = body.decode("utf-8", errors="replace")
                stmts = parse_statements(xml_text)
            except FlexError:
                continue
            seen_xml.add((row.account_id or 0, (row.filename or "").replace(".xml", "")))
        else:
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            stem = (row.filename or "").replace(".json", "")
            if (row.account_id or 0, stem) in seen_xml:
                continue
            stmts = [parsed_from_payload(data)]
        if not stmts:
            continue
        src._import_statements(db, stmts, ctx, default_ccy, res, final=True,
                               xml_text=xml_text, window=None)
    set_state(db, REPROCESS_STATE, REPROCESS_VERSION)
    db.commit()
    res.message = (f"Reprocessed stored IBKR history: {res.fetched} fills read, {res.inserted} new. "
                   "Schwab and Trading 212 data were not touched.")
    return res


def maybe_reprocess_on_startup() -> None:
    """One-off after deploy. Idempotent; skips under pytest. Never calls IBKR."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    log = logging.getLogger(__name__)
    from app import db as dbmod
    db = dbmod.SessionLocal()
    try:
        recover_stale_upload(db)
        if get_state(db, REPROCESS_STATE) == REPROCESS_VERSION:
            return
        res = reprocess_stored_payloads(db)
        log.info("IBKR reprocess: %s", (res.message or "")[:200])
    except Exception:  # noqa: BLE001 — must not take the site down
        db.rollback()
        log.warning("IBKR stored-payload reprocess failed", exc_info=True)
    finally:
        db.close()


def recover_stale_upload_on_startup() -> None:
    """Mark a half-finished upload as interrupted. Safe to call on every boot."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    from app import db as dbmod
    db = dbmod.SessionLocal()
    try:
        recover_stale_upload(db)
    except Exception:  # noqa: BLE001 — must not take the site down
        db.rollback()
        log.warning("IBKR upload-state recover failed", exc_info=True)
    finally:
        db.close()


def _safe_filename(name: str | None) -> str:
    base = Path(name or "statement.xml").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-") or "statement.xml"
    return base[:80]


def stream_upload_to_temp(file_obj, dest: Path, cap: int = MAX_UPLOAD_BYTES,
                          chunk: int = UPLOAD_CHUNK) -> int:
    """Copy an UploadFile (or file-like) to dest in chunks. Raises FlexError if over cap."""
    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                data = file_obj.read(chunk)
                if not data:
                    break
                size += len(data)
                if size > cap:
                    raise FlexError("api", "File too large (100 MB max).")
                out.write(data)
    except FlexError:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return size


def upload_in_progress(db: Session) -> bool:
    if _UPLOAD_ACTIVE:
        return True
    return (load_upload(db).get("status") or "") in {"queued", "running"}


def queue_uploaded_files(db: Session, files: list[dict]) -> dict:
    """Persist queued progress. ``files`` items: name, size, path, status, error."""
    now = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    data = {
        "status": "queued",
        "files": files,
        "current": 0,
        "started": None,
        "finished": None,
        "error": None,
        "queued_at": now,
        "dir": files[0].get("dir") if files else None,
    }
    save_upload(db, data)
    db.commit()
    return data


def start_upload_worker() -> None:
    """Kick the background importer. Tests set UPLOAD_SYNC or patch this."""
    if _upload_inline():
        run_upload_job()
        return
    threading.Thread(target=run_upload_job, name="ibkr-upload", daemon=True).start()


def run_upload_job() -> None:
    """Process queued files one at a time. Single-flight via _UPLOAD_LOCK."""
    global _UPLOAD_ACTIVE
    if not _UPLOAD_LOCK.acquire(blocking=False):
        return
    _UPLOAD_ACTIVE = True
    from app import db as dbmod
    db = dbmod.SessionLocal()
    try:
        data = load_upload(db)
        if (data.get("status") or "") not in {"queued", "running"}:
            return
        files = list(data.get("files") or [])
        data["status"] = "running"
        data["started"] = data.get("started") or utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        data["error"] = None
        save_upload(db, data)
        db.commit()
        src = IbkrFlexSource(sleep=lambda _n: None, split_fetch=lambda *_a, **_k: [])
        any_ok = False
        for i, row in enumerate(files):
            if (row.get("status") or "") != "queued":
                continue
            data["current"] = i
            row["status"] = "running"
            data["files"] = files
            save_upload(db, data)
            db.commit()
            path = row.get("path")
            try:
                if not path or not Path(path).is_file():
                    raise FlexError("api", "Upload file was missing on disk.")
                res = src.import_xml_path(db, path, filename=row.get("name"))
                row["status"] = "done"
                row["fetched"] = res.fetched
                row["inserted"] = res.inserted
                if res.fetched and res.inserted == 0:
                    row["error"] = "nothing new"
                else:
                    row["error"] = None
                if res.message and "raw XML not stored" in res.message:
                    row["note"] = "raw XML not stored (gzipped size over 8 MB)"
                any_ok = True
            except FlexError as exc:
                db.rollback()
                row["status"] = "error"
                row["error"] = str(exc)
                row["fetched"] = row.get("fetched") or 0
                row["inserted"] = row.get("inserted") or 0
            except Exception as exc:  # noqa: BLE001 — surface a clean error; never include secrets
                db.rollback()
                log.exception("IBKR XML upload failed for %s", _safe_filename(row.get("name")))
                row["status"] = "error"
                row["error"] = "parse error"
                row["fetched"] = 0
                row["inserted"] = 0
            finally:
                try:
                    if path:
                        Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            data["files"] = files
            save_upload(db, data)
            db.commit()
        statuses = {r.get("status") for r in files}
        if statuses <= {"done"} or (any_ok and "error" not in statuses):
            data["status"] = "done"
            data["error"] = None
        elif any_ok:
            data["status"] = "done"
            data["error"] = "some files failed"
        else:
            data["status"] = "error"
            data["error"] = next((r.get("error") for r in files if r.get("error")), "upload failed")
        data["finished"] = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        save_upload(db, data)
        db.commit()
        _cleanup_upload_dir(data.get("dir"))
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("IBKR XML upload job failed")
        try:
            data = load_upload(db)
            data["status"] = "error"
            data["error"] = data.get("error") or "upload failed"
            data["finished"] = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            save_upload(db, data)
            db.commit()
            _cleanup_upload_dir(data.get("dir"))
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        db.close()
        _UPLOAD_ACTIVE = False
        _UPLOAD_LOCK.release()
