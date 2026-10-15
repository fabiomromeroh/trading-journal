"""Interactive Brokers Flex Web Service adapter (Activity Flex Query, XML).

Two-step HTTPS (no gateway): SendRequest -> ReferenceCode/Url, then GetStatement.
A Flex query covers its configured period; every sync re-fetches that window and
dedupes on tradeID / transactionID. Never invents fills. Token is Fernet-encrypted
in app_state (cred:ibkr; never in backups, logs, status, or HTML).
"""
from __future__ import annotations

import json
import logging
import re
import time as _time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
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
FULL_STATE = "ibkr:full"          # "1" → next sync notes that the Flex query period is the history window
SOURCE_KEY = "ibkr_flex"
STOCK_CATS = {"STK", "ETF", "FUND"}
OPTION_CATS = {"OPT"}
SUPPORTED_CATS = STOCK_CATS | OPTION_CATS
TRADE_LOD = {"", "EXECUTION"}
CASH_LOD = {"", "DETAIL"}
POS_LOD = {"", "SUMMARY"}
CA_LOD = {"", "DETAIL", "SUMMARY"}
RETRY_1019 = 6
HOW_TO = (
    "Client Portal → Performance & Reports → Flex Queries → Activity Flex Query (create): "
    "sections Trades (Executions level; fields incl. TradeID, Date/Time, Symbol, Underlying Symbol, "
    "Asset Class, Buy/Sell, Quantity, TradePrice, IB Commission, Taxes, Currency, FX Rate To Base, "
    "Open/Close Indicator, Put/Call, Strike, Expiry, Multiplier, ISIN, Conid, Listing Exchange, "
    "Level of Detail), Cash Transactions (Transaction ID, Type, Date/Time, Amount, Currency, Symbol, "
    "Conid, Description), Open Positions, Corporate Actions; format XML, period Last 365 days, "
    "date format yyyyMMdd, time HHmmss with ';' separator; then Flex Web Service Configuration → "
    "enable and generate the token; copy the Query ID."
)
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
    """kind: auth | query | rate | temporary | network | api"""

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


def save_credentials(db: Session, token: str, query_id: str, currency: str = "USD") -> None:
    payload = json.dumps({
        "token": token.strip(), "query_id": str(query_id).strip(),
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
    if not data.get("token") or not data.get("query_id"):
        return None
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


# ------------------------------------------------------------------ HTTP
def _kind_for_code(code: str | None) -> str:
    c = (code or "").strip()
    if c in {"1003", "1004", "1012"}:
        return "auth"
    if c in {"1005", "1014", "1015", "1016"}:
        return "query"
    if c in {"1018"}:
        return "rate"
    if c in {"1019", "1001"}:
        return "temporary"
    if c in {"1020"}:
        return "auth"
    return "api"


def _error_text(code: str | None, message: str | None) -> str:
    msg = (message or "").strip() or "Flex Web Service error"
    if code == "1019":
        return "IBKR is still generating the Flex statement (error 1019). Wait a minute and press Sync again."
    if code == "1018":
        return "IBKR Flex rate limit (error 1018: 1 request/s, 10/min). Wait a minute and try again."
    if code == "1003" or code == "1004":
        return "IBKR rejected the Flex token (expired or invalid). Generate a new token in Client Portal → Flex Web Service."
    if code == "1020":
        return "IBKR refused this IP for the Flex token. Remove the IP restriction on the token, or add Render's current IP."
    if code in {"1005", "1014", "1015"}:
        return f"IBKR Flex query id was rejected (error {code}). Check the Query ID in Client Portal → Flex Queries."
    if code:
        return f"IBKR Flex error {code}: {msg}"
    return f"IBKR Flex error: {msg}"


class FlexClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None,
                 sleep=_time.sleep, timeout: float = 30):
        self.token = token
        self.sleep = sleep
        self.http = httpx.Client(
            timeout=httpx.Timeout(timeout),
            transport=transport or _TRANSPORT,
            headers={"User-Agent": "trading-journal/1.0", "Accept": "application/xml"},
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, url: str, extra: dict) -> str:
        params = {"t": self.token, "v": "3", **extra}
        last: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                last = exc
                self.sleep(2 ** attempt)
                continue
            sc = resp.status_code
            if sc in (408, 500, 502, 503, 504):
                last = FlexError("temporary", f"Interactive Brokers is temporarily unavailable ({sc}). Try again in a few minutes.")
                self.sleep(2 ** attempt)
                continue
            if sc == 401:
                raise FlexError("auth", "IBKR rejected the Flex token (401). Generate a new token in Client Portal.", "401")
            if sc >= 400:
                raise FlexError("api", f"IBKR Flex HTTP {sc}.", str(sc))
            return resp.text or ""
        if isinstance(last, FlexError):
            raise last
        raise FlexError("network", "Could not reach Interactive Brokers (network error).") from last

    def send_request(self, query_id: str) -> tuple[str, str]:
        xml = self._get(SEND_URL, {"q": str(query_id)})
        status, code, message, ref, url = parse_flex_response(xml, self.token)
        if code == "1019":
            raise FlexError("temporary", _error_text(code, message), code)
        if (status or "").lower() != "success" or not ref:
            raise FlexError(_kind_for_code(code), _error_text(code, message), code)
        return ref, url or DEFAULT_GET_URL

    def get_statement(self, reference: str, url: str) -> str:
        get_url = url or DEFAULT_GET_URL
        last: FlexError | None = None
        for attempt in range(RETRY_1019):
            xml = self._get(get_url, {"q": str(reference)})
            if looks_like_statement(xml):
                return xml
            status, code, message, _ref, _url = parse_flex_response(xml, self.token)
            if code == "1019":
                last = FlexError("temporary", _error_text(code, message), code)
                self.sleep(2)
                continue
            raise FlexError(_kind_for_code(code), _error_text(code, message), code)
        raise last or FlexError("temporary", _error_text("1019", None), "1019")

    def fetch(self, query_id: str) -> str:
        ref, url = self.send_request(query_id)
        return self.get_statement(ref, url)


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


def parse_statement(xml_text: str) -> ParsedFlex:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FlexError("api", "IBKR Flex statement was not valid XML.") from exc
    out = ParsedFlex()
    stmt = root.find(".//FlexStatement")
    if stmt is None and _local(root.tag) == "FlexStatement":
        stmt = root
    if stmt is not None:
        sf = fields_of(stmt)
        out.account_id = sf.get("accountId") or out.account_id
        out.from_date = sf.get("fromDate")
        out.to_date = sf.get("toDate")
    info = root.find(".//AccountInformation")
    if info is not None:
        inf = fields_of(info)
        out.account_id = inf.get("accountId") or out.account_id
        out.currency = (inf.get("currency") or "").upper() or None
    for el in root.findall(".//Trade"):
        row = fields_of(el)
        if row:
            out.trades.append(row)
    for el in root.findall(".//CashTransaction"):
        row = fields_of(el)
        if row:
            out.cash.append(row)
    for el in root.findall(".//OpenPosition"):
        row = fields_of(el)
        if row:
            out.positions.append(row)
    for el in root.findall(".//CorporateAction"):
        row = fields_of(el)
        if row:
            out.corporate.append(row)
    best: tuple[date, dict] | None = None
    for el in list(root.findall(".//EquitySummaryByReportDateInBase")) + list(root.findall(".//EquitySummaryInBase")):
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
        if best is None or day >= best[0]:
            best = (day, cand)
    out.equity = best[1] if best else None
    return out


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
_SPLIT_CODES = {"FS", "RS", "SD", "SPLIT", "REVERSE SPLIT", "REVERSE_SPLIT", "STOCK SPLIT", "STOCK_SPLIT"}
_RENAME_CODES = {"IC", "SYMBOL CHANGE", "SYMBOL_CHANGE", "TICKER CHANGE", "RENAME", "CUSIP CHANGE"}
_MERGE_CODES = {"MA", "TC", "TO", "MERGER", "ACQUISITION"}
_PARENS_TICKER = re.compile(r"\(([A-Z][A-Z0-9.]{0,7})\s*,")
_CHANGE_TO = re.compile(
    r"(?:SYMBOL|TICKER|NAME|CUSIP/?ISIN)\s+CHANGE\s+TO\s+([A-Z][A-Z0-9.]{0,7})\b", re.I)
_MERGE_TO = re.compile(
    r"\b(?:MERGED\s+INTO|ACQUIRED\s+BY)\s+([A-Z][A-Z0-9.]{0,7})\b", re.I)
_NOT_TICKER = {"INC", "CORP", "LTD", "LLC", "PLC", "THE", "CLASS", "ORD", "FOR", "CASH"}


def _ca_code(row: dict) -> str:
    return (row.get("type") or row.get("code") or row.get("Type") or "").strip().upper()


def _ca_when(row: dict) -> datetime | None:
    dt, _ = parse_ibkr_dt(row.get("dateTime") or row.get("reportDate") or row.get("exDate") or "")
    return dt


def _ca_id(row: dict) -> str:
    return str(row.get("transactionID") or row.get("actionID") or row.get("tradeID")
               or row.get("conid") or row.get("symbol") or "ca")


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


def corporate_events(rows: list[dict]) -> tuple[list[dict], list[ExecRecord]]:
    """Splits / renames / mergers -> adjustment events. Unrecognised -> needs-attention. No invented fills."""
    from app.corporate import ratio_from_text, clean_ratio
    events: list[dict] = []
    splits: list[ExecRecord] = []
    for row in rows:
        if _lod(row) and _lod(row) not in CA_LOD:
            continue
        sym = (row.get("symbol") or "").strip().upper()
        desc = row.get("description") or row.get("Description") or ""
        code = _ca_code(row)
        when = _ca_when(row) or utcnow()
        key = f"ibkr:{_ca_id(row)}:{code or 'ca'}"
        blob = f"{code} {desc}"
        if code in _SPLIT_CODES or re.search(r"\b(split|reverse split)\b", blob, re.I):
            ratio = ratio_from_text(desc) or ratio_from_text(blob)
            if ratio:
                ratio = clean_ratio(ratio) or ratio
            if ratio and abs(ratio - 1.0) > 1e-9 and sym:
                events.append({"kind": "SPLIT", "symbol": sym, "ratio": float(ratio), "when": when,
                               "key": f"broker:{key}:{ratio}"})
                splits.append(ExecRecord(
                    external_id=f"ca:{_ca_id(row)}", symbol=sym, underlying=sym, asset_type="STOCK",
                    side="BUY", quantity=float(ratio), price=0.0, executed_at=when if when.hour or when.minute else
                    datetime.combine(when.date(), time(20, 0)),
                    fees=0.0, time_known=bool(when.hour or when.minute), position_effect="RATIO",
                    kind="SPLIT", description=(desc or f"Split ×{ratio:g}")[:300],
                    raw={"type": code, "description": desc, "conid": row.get("conid")},
                    trade_date=when.date(), currency=(row.get("currency") or "").upper() or None,
                ))
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
        if code in _MERGE_CODES or re.search(r"\b(merger|merged into|acquired by)\b", blob, re.I):
            new = _new_ticker(sym, desc) or _new_ticker(sym, blob)
            ratio = ratio_from_text(desc)
            if sym and new and when and (ratio is None or abs(ratio - 1.0) <= 0.02):
                events.append({"kind": "RENAME", "symbol": sym, "new_symbol": new, "when": when,
                               "key": f"rename:{sym}:{new}:{when.date().isoformat()}"})
            elif sym:
                events.append({"kind": "ATTENTION", "symbol": sym, "code": "merger",
                               "note": f"{sym}: IBKR merger/acquisition needs attention ({desc or code})."})
            continue
        if not sym:
            continue
        events.append({"kind": "ATTENTION", "symbol": sym, "code": "ca",
                       "note": f"{sym}: unrecognised IBKR corporate action {code or ''} {desc}".strip()})
    return events, splits


def position_map(rows: list[dict]) -> dict[str, float]:
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
        if not sym or qty is None or " " in sym:
            continue
        out[sym] = out.get(sym, 0.0) + float(qty)
    return out


# ------------------------------------------------------------------ status / card
def test_connection(token: str, query_id: str, transport=None, sleep=None) -> dict:
    """Probe SendRequest + GetStatement. Returns {ok, account_id, currency, error, kind} with no token."""
    sleep = sleep or _time.sleep
    out = {"ok": False, "account_id": None, "currency": None, "error": None, "kind": None}
    cl = FlexClient(token, transport=transport, sleep=sleep)
    try:
        xml = cl.fetch(query_id)
        parsed = parse_statement(xml)
        out["account_id"] = parsed.account_id
        out["currency"] = parsed.currency
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
    if st.get("error_kind") in ("auth", "query"):
        state, detail = "needs_login", st.get("error") or "Token or query rejected."
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
    description = ("IBKR Flex Web Service: Activity Flex Query (XML). Token + query id; executions, "
                   "cash, dividends, interest, open positions and corporate actions. Manual sync only.")

    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=None, split_fetch=None):
        self._transport, self._sleep = transport, sleep or _time.sleep
        self.split_fetch = split_fetch

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
        if st.get("error_kind") in ("auth", "query"):
            return SourceStatus(True, False, st.get("error") or "Token or query rejected.", "error",
                                banner={"level": "error", "text": "Interactive Brokers: " + (st.get("error") or "token rejected"),
                                        "link": "/settings#brokers", "link_text": "Fix token"})
        return SourceStatus(True, True, "Connected" + (f" · last sync {st['synced_at']}" if st.get("synced_at") else ""))

    def ensure_account(self, db: Session, info: dict, default_ccy: str = "USD") -> Account:
        ext = str(info.get("account_id") or info.get("id") or "")
        ccy = (info.get("currency") or default_ccy or "USD").upper()
        a = None
        if ext:
            a = db.scalar(select(Account).where(Account.source_key == SOURCE_KEY, Account.external_ref == ext))
        if a is None:
            masked = ("…" + ext[-4:]) if len(ext) >= 4 else (ext or None)
            a = Account(name="Interactive Brokers", broker="ibkr", external_ref=ext or None,
                        source_key=SOURCE_KEY, account_number_masked=masked, currency=ccy,
                        timezone="America/New_York", market="US", kind="margin")
            db.add(a)
            db.flush()
        elif info.get("currency") and a.currency != ccy:
            a.currency = ccy
        return a

    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        res = SourceResult()
        cl = None
        creds = load_credentials(db)
        if not creds:
            raise RuntimeError("Interactive Brokers is not connected (no Flex token stored).")
        token, qid = creds["token"], creds["query_id"]
        default_ccy = (creds.get("currency") or "USD").upper()
        try:
            cl = FlexClient(token, transport=self._transport, sleep=self._sleep)
            xml = cl.fetch(qid)
            parsed = parse_statement(xml)
            if ctx.account_id is not None:
                wanted = db.get(Account, ctx.account_id)
                ext = parsed.account_id or ""
                if wanted is None or ((wanted.external_ref or "") and ext and wanted.external_ref != ext):
                    ctx.info("Interactive Brokers account does not match the selected account.")
                    return res
            acct = self.ensure_account(db, {"account_id": parsed.account_id, "currency": parsed.currency or default_ccy},
                                       default_ccy)
            res.accounts.append(acct.id)
            full = get_state(db, FULL_STATE) == "1"
            if full:
                set_state(db, FULL_STATE, None)
            recs, skipped, detail = self.convert(db, parsed.trades)
            events, split_recs = corporate_events(parsed.corporate)
            recs.extend(split_recs)
            recs.sort(key=lambda r: (r.executed_at, r.external_id))
            from app.adjustments import record_payload
            record_payload(db, acct.id, SOURCE_KEY, {
                "trades": parsed.trades, "cash": parsed.cash, "positions": parsed.positions,
                "corporate": parsed.corporate, "account_id": parsed.account_id,
            }, run_id=ctx.run_id)
            stats = ingest_records(db, acct.id, SOURCE_KEY, recs)
            res.fetched, res.inserted, res.merged = len(recs), stats.inserted, stats.merged
            cash_note = self._import_cash(db, acct, parsed.cash)
            pos = position_map(parsed.positions)
            from app.adjustments import detect_and_apply
            corp = detect_and_apply(db, acct.id, fetch=self.split_fetch, positions=pos, broker_events=events)
            if parsed.equity:
                from app.balance import save_ibkr_summary
                save_ibkr_summary(db, acct.id, parsed.equity, parsed.currency or acct.currency)
            rebuild_trades(db, [acct.id])
            sk = _skip_summary(skipped, detail)
            res.message = (f"Interactive Brokers: {len(recs)} fills read, {stats.inserted} new"
                           + (f"; skipped {sk}" if sk else ""))
            if cash_note:
                res.message += "; " + cash_note
            if corp:
                res.message += "; " + "; ".join(corp[:8])
            if full:
                res.message += (" (full history: Flex query covers its configured period; "
                                "lengthen the query in Client Portal for older fills)")
            ctx.info(res.message)
            for line in corp:
                ctx.info("IBKR corporate: " + line)
            _save_last(db, reset=True, ok=True, error=None, error_kind=None,
                       synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                       account_id=parsed.account_id, currency=acct.currency,
                       fetched=len(recs), inserted=stats.inserted, skipped=dict(skipped),
                       skipped_detail=detail, adjustments=corp, full=full, query_id=qid)
            db.commit()
            return res
        except FlexError as exc:
            db.rollback()
            _save_last(db, ok=False, error=redact(str(exc), token), error_kind=exc.kind,
                       failed_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))
            db.commit()
            raise RuntimeError(redact(str(exc), token)) from exc
        finally:
            if cl is not None:
                cl.close()

    def convert(self, db: Session, rows: list[dict]) -> tuple[list[ExecRecord], Counter, dict]:
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        recs: list[ExecRecord] = []
        cache: dict[str, Instrument] = {}

        def skip(reason: str, raw: dict) -> None:
            skipped[reason] += 1
            ticker = skip_ticker(raw)
            lst = detail.setdefault(reason, [])
            if ticker not in lst and len(lst) < 100:
                lst.append(ticker)

        for row in rows:
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
                raw_sym = (row.get("symbol") or "").strip().upper()
                conid = str(row.get("conid") or "")
                key = conid or raw_sym
                if key:
                    inst = cache.get(key)
                    if inst is None:
                        inst = symbolmap.upsert_ibkr(
                            db, key, row.get("isin"), row.get("description") or raw_sym,
                            row.get("currency"), listing_exchange(row.get("listingExchange"), row.get("isin")),
                            display_symbol=raw_sym)
                        cache[key] = inst
            r = trade_to_record(row, inst, inst.symbol if inst is not None and cat not in OPTION_CATS else None)
            if isinstance(r, Skip):
                skip(r.reason, row)
                continue
            recs.append(r)
        return recs, skipped, detail

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
