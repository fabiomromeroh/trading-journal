"""Trading 212 data source (official public API, Invest and Stocks ISA accounts, read-only key + secret).

Docs (verified Oct 2026): https://docs.trading212.com/api
  * Auth: HTTP Basic, API key = user, API secret = password; hosts live.trading212.com / demo.trading212.com, /api/v0.
  * GET /equity/account/summary          id + primary currency                      (1 req / 5 s)   scope: Account data
  * GET /equity/history/orders           {items:[{order, fill}], nextPagePath}     (20 req / min)  scope: History - orders
        fill: id, filledAt, price, quantity, type (TRADE | STOCK_SPLIT | SPIN_OFF | ...), walletImpact{currency, fxRate,
        netValue, realisedProfitLoss, taxes[{name, quantity, currency, chargedAt}]}; order: side, ticker, instrument{ticker,
        name, isin, currency}.  Pagination: follow nextPagePath until null (limit <= 50).
  * GET /equity/positions                open positions, used to reconcile          (1 req / s)    scope: Portfolio
  * GET /equity/metadata/instruments     shortName / isin / currencyCode            (1 req / 50 s) scope: Metadata (optional)
  * Rate limit headers: x-ratelimit-limit / -period / -remaining / -reset (unix seconds). 429 = back off until reset.
  * "Multi-currency accounts are not supported through the API": values are in the primary account currency.

Journal mapping: one Account per key (currency = account currency, market EU); every TRADE fill becomes an Execution priced
in the *instrument* currency (pence-quoted LSE lines are converted to pounds), with the broker's FX rate to the account
currency stored per execution and fees (taxes) converted to the instrument currency. A GBX ``fxRate`` is pence per one
account-currency unit (~85, not ~0.85); it is scaled to pounds before use, otherwise fees and P&L are ~100× too big.
Order-history rows whose fill type is a split, spin-off, distribution, scrip dividend or acquisition are stored as
corporate actions (they adjust quantity and cost, and they are not discretionary trades). Cash-only history
(``/equity/history/transactions``), dividend history and the async CSV export are read on a full re-scan when the
key allows it; none of those calls is repeated until the rate-limit window allows. A quantity the feed still
cannot explain stays a gap warning. Estimated reconciliation rows are not created. Credentials live
Fernet-encrypted in app_state (cred:t212; never in backups)."""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets, symbolmap
from app.instruments import ExecRecord
from app.models import Account, Instrument, utcnow
from app.services import get_state, ingest_records, rebuild_trades, set_state
from app.sources.base import DataSource, SourceResult, SourceStatus, SyncContext

log = logging.getLogger(__name__)
# httpx can log request lines; never let Basic-auth credentials reach the server log.
logging.getLogger("httpx").setLevel(logging.WARNING)

HOSTS = {"live": "https://live.trading212.com/api/v0", "demo": "https://demo.trading212.com/api/v0"}
CRED_STATE = "cred:t212"        # encrypted {"key","secret","env"}; the cred: prefix keeps it out of JSON backups
LAST_STATE = "t212:last"        # last sync / test status (no secrets)
CURSOR_STATE = "t212:cursor"    # newest filledAt stored (ISO, UTC)
FULL_STATE = "t212:full"        # "1" → next sync ignores the cursor (one-off full re-scan)
EXPORT_STATE = "t212:export"    # pending reportId, legacy "done:<id>", or "done2:<id>" after the current CSV parser
HUNT_STATE = "t212:hunt"        # find-missing progress (no secrets, no raw payloads)
RECON_STATE = "t212:recon"      # {"seen": {key: iso}, "dismissed": [key]} — not a secret
OVERLAP_DAYS = 3
PAGE_LIMIT = 50
SAFETY_PAGES = 500              # loop guard only; a short page must not stop the walk before this
CSV_DONE = "done2:"             # parser generation that keeps Dividend (DRIP) and split open/close rows
SOURCE_KEY = "t212"
SCOPE_HELP = ("In Trading 212 → Settings → API (Beta) create a key and tick: Account data, Portfolio, "
              "History, History - Orders, Metadata. Leave Orders - Execute (and every other trading / Pies "
              "permission) off.")


_TRANSPORT: httpx.BaseTransport | None = None   # tests inject a MockTransport here


class T212Error(Exception):
    """kind: auth | scope | rate | temporary | network | api"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status


# ------------------------------------------------------------------ credentials
def _enc():
    from app import security
    return security


def save_credentials(db: Session, key: str, secret: str, env: str = "live") -> None:
    payload = json.dumps({"key": key.strip(), "secret": secret.strip(), "env": env if env in HOSTS else "live"})
    set_state(db, CRED_STATE, _enc().encrypt(payload))
    set_state(db, CURSOR_STATE, None)  # new key: full resync (ingest is idempotent on fill id)
    db.flush()


def load_credentials(db: Session) -> dict | None:
    raw = get_state(db, CRED_STATE)
    if not raw:
        return None
    try:
        return json.loads(_enc().decrypt(raw))
    except Exception:  # noqa: BLE001 - key changed / corrupt: behave as not configured
        return None


def clear_credentials(db: Session) -> None:
    set_state(db, CRED_STATE, None)
    set_state(db, CURSOR_STATE, None)
    db.flush()


def mask(key: str) -> str:
    return (key[:4] + "…" + key[-3:]) if len(key) > 8 else "••••"


# ------------------------------------------------------------------ HTTP client
class T212Client:
    def __init__(self, key: str, secret: str, env: str = "live", transport: httpx.BaseTransport | None = None,
                 sleep=_time.sleep, clock=_time.time, timeout: float = 30):
        self.base = HOSTS.get(env, HOSTS["live"])
        self.http = httpx.Client(timeout=timeout, transport=transport or _TRANSPORT, auth=(key, secret),
                                 headers={"Accept": "application/json", "User-Agent": "trading-journal/1.0"})
        self.sleep, self.clock = sleep, clock
        self.calls = 0
        self.page_cap_hit = False

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait_for_reset(self, resp: httpx.Response, cap: float = 70.0) -> float:
        try:
            reset = float(resp.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            reset = 0.0
        wait = max(1.0, min(cap, reset - self.clock())) if reset else 5.0
        return wait

    def get(self, path: str, params: dict | None = None):
        return self._exchange("GET", path, params=params)

    def post(self, path: str, body: dict):
        return self._exchange("POST", path, body=body)

    def _exchange(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = path if path.startswith("http") else (self.base + path if path.startswith("/equity") else
                                                    self.base.rsplit("/api/v0", 1)[0] + path)
        last: Exception | None = None
        for attempt in range(4):
            try:
                if method == "POST":
                    resp = self.http.post(url, json=body)
                else:
                    resp = self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                last = exc
                self.sleep(2 ** attempt)
                continue
            self.calls += 1
            sc = resp.status_code
            if sc == 429:
                if attempt == 3:
                    raise T212Error("rate", "Trading 212 rate limit hit (history is limited to 20 requests per minute). "
                                    "Wait a minute and press Sync again.", 429)
                self.sleep(self._wait_for_reset(resp))
                continue
            if sc in (408, 500, 502, 503, 504):
                last = T212Error("temporary", f"Trading 212 is temporarily unavailable ({sc}). Try again in a few minutes.", sc)
                self.sleep(2 ** attempt)
                continue
            if sc == 401:
                raise T212Error("auth", "Trading 212 rejected the API key/secret (401). Create a new key and paste BOTH the "
                                "key and the secret (the secret is shown only once). Practice and live keys are separate.", 401)
            if sc == 403:
                body = resp.text[:200]
                raise T212Error("scope", f"Trading 212 refused the request (403): {body or 'missing permission'}. {SCOPE_HELP} "
                                "If you restricted the key to certain IP addresses, remove that restriction (Render's IP changes).", 403)
            if sc >= 400:
                raise T212Error("api", f"Trading 212 error {sc}: {resp.text[:200]}", sc)
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) <= 0:
                self.sleep(self._wait_for_reset(resp))
            try:
                return resp.json()
            except ValueError as exc:
                raise T212Error("api", "Trading 212 returned something that is not JSON.", sc) from exc
        if isinstance(last, T212Error):
            raise last
        raise T212Error("network", f"Could not reach Trading 212 ({type(last).__name__}). Check your connection.") from last

    def account_summary(self) -> dict:
        return self.get("/equity/account/summary")

    def positions(self) -> list[dict]:
        return self.get("/equity/positions") or []

    def instruments(self) -> list[dict]:
        return self.get("/equity/metadata/instruments") or []

    def history_pages(self):
        """Yield pages (lists of {order, fill}) newest-first, following nextPagePath."""
        yield from self.paged("/equity/history/orders")

    def paged(self, path: str, params: dict | None = None, start: str | None = None, on_page=None):
        """Follow nextPagePath, then a cursor field, until the feed ends.

        A short page or an empty page does not end the walk while the body still names a next
        page. ``start`` resumes from a saved nextPagePath. Repeated paths stop the walk. The
        safety cap is only a loop guard.
        """
        nxt = start or path
        query: dict | None = None if start else {"limit": PAGE_LIMIT, **(params or {})}
        seen: set[str] = set()
        n = 0
        self.page_cap_hit = False
        while nxt:
            sent_cursor = _cursor_of(nxt, query)
            key = nxt + "|" + _query_key(query)
            if key in seen:
                return
            seen.add(key)
            data = self.get(nxt, query)
            n += 1
            if on_page is not None:
                on_page(n, nxt)
            if isinstance(data, list):
                yield data
                return
            if not isinstance(data, dict):
                return
            items = None
            for field in ("items", "data", "orders", "transactions", "dividends"):
                if isinstance(data.get(field), list):
                    items = data[field]
                    break
            yield items or []
            nxt_path = data.get("nextPagePath") or data.get("nextPage") or ""
            cursor = data.get("cursor") or data.get("nextCursor")
            if isinstance(nxt_path, str) and nxt_path.strip():
                nxt, query = nxt_path.strip(), None
            elif cursor is not None and str(cursor) != "" and str(cursor) != str(sent_cursor or ""):
                base = (path if not start else path).split("?")[0]
                kept = {k: v for k, v in (params or {}).items() if k != "cursor"}
                kept["cursor"] = cursor
                kept.setdefault("limit", PAGE_LIMIT)
                nxt, query = base, kept
            else:
                return
            if n >= SAFETY_PAGES:
                self.page_cap_hit = True
                return


# ------------------------------------------------------------------ pure record mapping
def parse_ts(s: str) -> datetime:
    """ISO 8601 (Z or offset) -> naive UTC."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


@dataclass
class Skip:
    reason: str


# Fill types that change the share count without being a buy or a sell. Cancelled / rejected orders
# never get a fill and are counted separately. Anything else with a price and a size is a trade,
# even when the payload puts the order type (LIMIT) or the trading method (TOTV/OTC) in `type`.
_NON_TRADE_TYPES = {
    "STOCK_SPLIT", "SPIN_OFF", "SPINOFF", "STOCK_DISTRIBUTION", "FOP", "FOP_CORRECTION",
    "CUSTOM_STOCK_DISTRIBUTION", "EQUITY_RIGHTS", "SCRIP_STOCK_DIVIDENDS", "STOCK_DIVIDENDS",
    "STOCK_ACQUISITION", "CASH_AND_STOCK_ACQUISITION",
}
_METHOD_OR_SIDE = {"TRADE", "TOTV", "OTC", "BUY", "SELL", "MARKET", "LIMIT", "STOP", "STOP_LIMIT"}
_FILLED_STATUS = {"FILLED", "PARTIALLY_FILLED"}
_SECRET_KEYS = {"key", "secret", "password", "authorization", "token", "api_key", "apisecret"}


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_type(v) -> str:
    return (v or "").upper().replace("-", "_").replace(" ", "_")


def _order_of(item: dict) -> dict:
    order = item.get("order")
    return order if isinstance(order, dict) else {}


def _ticker_of(item: dict) -> str:
    order = _order_of(item)
    ii = order.get("instrument") if isinstance(order.get("instrument"), dict) else {}
    if not ii and isinstance(item.get("instrument"), dict):
        ii = item["instrument"]
    return order.get("ticker") or ii.get("ticker") or item.get("ticker") or ""


def _skip_summary(counts: Counter, detail: dict[str, list[str]]) -> str:
    bits = []
    for k, n in sorted(counts.items()):
        ts = detail.get(k) or []
        shown = ", ".join(ts[:12])
        extra = f" +{len(ts) - 12}" if len(ts) > 12 else ""
        bits.append(f"{n} {k}" + (f" ({shown}{extra})" if shown else ""))
    return "; ".join(bits)


def fmt_sh(v: float) -> str:
    """Share count without trailing zeros, enough places for a fractional Trading 212 position."""
    s = f"{float(v):.8f}".rstrip("0").rstrip(".")
    return "0" if s in {"", "-0"} else s


def _qty_diff(sym: str, theirs: float, journal: float) -> list[str]:
    delta = theirs - journal
    if abs(delta) <= 1e-4:
        return []
    if delta > 0:
        return [f"{sym}: missing {fmt_sh(delta)} sh vs Trading 212"]
    return [f"{sym}: journal has {fmt_sh(-delta)} sh more than Trading 212"]


def _cursor_of(path: str, query: dict | None) -> str | None:
    if query and query.get("cursor") is not None:
        return str(query.get("cursor"))
    if "cursor=" in (path or ""):
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(path).query).get("cursor") or [None])[0]
    return None


def _query_key(query: dict | None) -> str:
    if not query:
        return ""
    return "&".join(f"{k}={query[k]}" for k in sorted(query))


def _isin_of(item: dict) -> str:
    order = _order_of(item)
    ii = order.get("instrument") if isinstance(order.get("instrument"), dict) else {}
    if not ii and isinstance(item.get("instrument"), dict):
        ii = item["instrument"]
    return (ii.get("isin") or item.get("isin") or "") or ""


def field_names(item: dict, limit: int = 40) -> list[str]:
    """Dotted field names only. Values are never copied."""
    out: list[str] = []

    def walk(obj, prefix: str, depth: int) -> None:
        if not isinstance(obj, dict) or depth > 3 or len(out) >= limit:
            return
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                continue
            path = f"{prefix}.{k}" if prefix else str(k)
            if path not in out:
                out.append(path)
            if len(out) >= limit:
                return
            if isinstance(v, dict):
                walk(v, path, depth + 1)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                walk(v[0], path + "[]", depth + 1)

    if isinstance(item, dict):
        walk(item, "", 0)
    return out[:limit]


def skip_ticker(item: dict) -> str:
    """Journal symbol for a skip line (CGC, not the raw CGC_US_EQ ticker)."""
    order = _order_of(item)
    ii = order.get("instrument") if isinstance(order.get("instrument"), dict) else {}
    if not ii and isinstance(item.get("instrument"), dict):
        ii = item["instrument"]
    ticker = _ticker_of(item)
    if not ticker:
        return "?"
    return markets.parse_t212_ticker(ticker, ii.get("isin")).symbol or ticker


def sample_fields(item: dict, limit: int = 12) -> list[str]:
    """Dotted field names from one raw history item. Values are never copied (no secrets)."""
    if not isinstance(item, dict):
        return []
    out: list[str] = []

    def add(path: str) -> None:
        if path not in out and path.split(".")[-1].lower() not in _SECRET_KEYS and len(out) < limit:
            out.append(path)

    for k in list(item)[:6]:
        add(str(k))
    for prefix in ("fill", "order", "execution"):
        sub = item.get(prefix)
        if isinstance(sub, dict):
            for k in ("type", "status", "side", "filledAt", "dateExecuted", "fillType", "quantity", "id"):
                if k in sub:
                    add(f"{prefix}.{k}")
    for k in ("fillType", "dateExecuted", "filledAt", "status", "type", "side", "filledQuantity", "filledValue"):
        if k in item:
            add(k)
    return out[:limit]


def _fill_type(order: dict, fill: dict, top: dict) -> str:
    ft = _norm_type(fill.get("type"))
    fill_method = _norm_type(fill.get("fillType") or order.get("fillType") or top.get("fillType"))
    order_type = _norm_type(order.get("type") or top.get("type"))
    if ft in _NON_TRADE_TYPES:
        return ft
    if ft in _METHOD_OR_SIDE:
        return "TRADE"
    if not ft:
        if order_type in _NON_TRADE_TYPES:
            return order_type
        if fill_method in _NON_TRADE_TYPES:
            return fill_method
        return "TRADE"
    # Unknown label (not a documented corporate action). A buy/sell still has price and size.
    if _has_economics(order, fill, top):
        return "TRADE"
    return ft


def _has_economics(order: dict, fill: dict, top: dict) -> bool:
    price = _num(fill.get("price"))
    if price is None:
        price = _num(fill.get("fillPrice") or order.get("fillPrice") or top.get("fillPrice"))
    qty = _num(fill.get("quantity"))
    if qty is None:
        qty = _num(fill.get("filledQuantity") or order.get("filledQuantity") or top.get("filledQuantity"))
    fv = _num(fill.get("filledValue") if fill.get("filledValue") is not None else order.get("filledValue"))
    if fv is None:
        fv = _num(top.get("filledValue"))
    return bool(price) and (bool(qty) or bool(fv))


def _looks_executed(order: dict, fill: dict, top: dict) -> bool:
    status = _norm_type(order.get("status") or top.get("status"))
    qty = _num(fill.get("quantity"))
    if qty is None:
        qty = _num(fill.get("filledQuantity") or order.get("filledQuantity") or top.get("filledQuantity"))
    fv = _num(order.get("filledValue") if order.get("filledValue") is not None else top.get("filledValue"))
    has_fill_id = fill.get("id") is not None or fill.get("fillId") is not None or order.get("fillId") is not None or top.get("fillId") is not None
    if status in ("CANCELLED", "CANCELED", "REJECTED", "REPLACED"):
        return bool(has_fill_id and qty)
    if status in _FILLED_STATUS:
        return True
    if has_fill_id and (qty or fv or _num(fill.get("price"))):
        return True
    if qty or fv:
        return status not in ("LOCAL", "UNCONFIRMED", "CONFIRMED", "NEW", "CANCELLING", "")
    return False


def _infer_side(order: dict, fill: dict, top: dict, qty: float | None) -> str:
    for src in (order.get("side"), fill.get("side"), top.get("side")):
        s = (src or "").strip().upper()
        if s in ("BUY", "SELL"):
            return s
    if qty is not None and qty < 0:
        return "SELL"
    wallet = fill.get("walletImpact") or order.get("walletImpact") or top.get("walletImpact") or {}
    net = _num(wallet.get("netValue")) if isinstance(wallet, dict) else None
    if net is not None and net != 0 and (qty is None or qty > 0):
        return "SELL" if net > 0 else "BUY"
    if qty is not None and qty > 0:
        return "BUY"
    fv = _num(top.get("filledValue"))
    if fv is None:
        fv = _num(order.get("filledValue"))
    if fv is not None and fv != 0:
        return "SELL" if fv < 0 else "BUY"
    return ""


def item_time(item: dict) -> datetime | None:
    """Execution time used to decide whether a history page is older than the cursor.

    ``filledAt`` and ``dateExecuted`` are both read. The later one wins, so a row whose fill time
    lives only in ``dateExecuted`` (or is newer there) does not look old just because ``filledAt``
    is missing or stale. ``createdAt`` is used only when neither execution field is present.
    """
    order = _order_of(item)
    fill = item.get("fill") if isinstance(item.get("fill"), dict) else {}
    fills = item.get("fills") if isinstance(item.get("fills"), list) else []
    if not fill and fills and isinstance(fills[0], dict):
        fill = fills[0]
    exec_vals = [
        fill.get("filledAt"), fill.get("dateExecuted"), order.get("filledAt"), order.get("dateExecuted"),
        item.get("dateExecuted"), item.get("filledAt"),
    ]
    parsed: list[datetime] = []
    for v in exec_vals:
        if not isinstance(v, str) or not v.strip():
            continue
        try:
            parsed.append(parse_ts(v))
        except (TypeError, ValueError):
            continue
    if parsed:
        return max(parsed)
    for v in (order.get("createdAt"), item.get("dateCreated"), item.get("createdAt"),
              fill.get("dateModified"), order.get("dateModified"), item.get("dateModified")):
        if isinstance(v, str) and v.strip():
            try:
                return parse_ts(v)
            except (TypeError, ValueError):
                continue
    return None


def _maybe_fill(item: dict) -> bool:
    """True when the row looks like a real execution even if it has no parseable time yet."""
    order = _order_of(item) or item
    status = _norm_type(order.get("status") or item.get("status"))
    if status in _FILLED_STATUS:
        return True
    if item.get("fillId") or (isinstance(order, dict) and order.get("fillId")):
        return True
    if _num(item.get("filledValue")) or (isinstance(order, dict) and _num(order.get("filledValue"))):
        return True
    fill = item.get("fill")
    if isinstance(fill, dict) and (fill.get("id") is not None or _num(fill.get("quantity"))):
        return True
    return False


def _page_newest_first(page: list[dict]) -> bool | None:
    times = [t for t in (item_time(i) for i in page) if t is not None]
    if len(times) < 2 or times[0] == times[-1]:
        return None
    return times[0] > times[-1]


def _instrument_from(order: dict, top: dict) -> dict:
    ii = order.get("instrument") if isinstance(order.get("instrument"), dict) else None
    if ii:
        return ii
    ii = top.get("instrument") if isinstance(top.get("instrument"), dict) else None
    if ii:
        return ii
    ticker = order.get("ticker") or top.get("ticker") or ""
    if not ticker and not top.get("isin"):
        return {}
    return {"ticker": ticker, "name": top.get("name"), "isin": top.get("isin"),
            "currency": top.get("currency") or order.get("currency")}


def _synthesize_fill(order: dict, fill: dict, top: dict) -> dict:
    """Copy a possibly-flat or half-empty fill into the nested shape `to_record` expects."""
    fill = dict(fill or {})
    if fill.get("id") is None:
        fill["id"] = fill.get("fillId") if fill.get("fillId") is not None else (
            order.get("fillId") if order.get("fillId") is not None else top.get("fillId"))
    if fill.get("id") is None and (order.get("id") is not None or top.get("id") is not None):
        fill["id"] = order.get("id") if order.get("id") is not None else top.get("id")
    if not fill.get("filledAt"):
        for v in (fill.get("dateExecuted"), order.get("dateExecuted"), top.get("dateExecuted"),
                  order.get("filledAt"), fill.get("dateModified"), order.get("dateModified"),
                  top.get("dateModified"), order.get("createdAt"), top.get("dateCreated"), top.get("createdAt")):
            if isinstance(v, str) and v.strip():
                fill["filledAt"] = v
                break
    if _num(fill.get("price")) is None:
        for src in (fill.get("fillPrice"), order.get("fillPrice"), top.get("fillPrice"), order.get("averagePrice")):
            if _num(src) is not None:
                fill["price"] = src
                break
    qty = _num(fill.get("quantity"))
    if qty is None:
        qty = _num(fill.get("filledQuantity"))
    if qty is None:
        qty = _num(order.get("filledQuantity"))
    if qty is None:
        qty = _num(top.get("filledQuantity"))
    price = _num(fill.get("price"))
    if (qty is None or qty == 0) and price:
        fv = _num(fill.get("filledValue"))
        if fv is None:
            fv = _num(order.get("filledValue"))
        if fv is None:
            fv = _num(top.get("filledValue"))
        if fv:
            qty = fv / price
            if _num(fill.get("price")) is None:
                fill["price"] = abs(fv / qty) if qty else fill.get("price")
    if qty is not None and fill.get("quantity") is None:
        fill["quantity"] = qty
    if not fill.get("walletImpact"):
        w = order.get("walletImpact") or top.get("walletImpact")
        taxes = fill.get("taxes") or order.get("taxes") or top.get("taxes")
        if isinstance(w, dict):
            fill["walletImpact"] = w
        elif taxes:
            fill["walletImpact"] = {"taxes": taxes}
    fill["type"] = _fill_type(order, fill, top)
    if not (order.get("side") or fill.get("side")):
        side = _infer_side(order, fill, top, _num(fill.get("quantity")))
        if side:
            fill["side"] = side
    return fill


def history_rows(item: dict) -> list[dict]:
    """Flatten one history payload into `{order, fill}` rows.

    The live API mixes shapes in one account history:
      * `{order, fill}` (current docs)
      * `{order, fills:[...]}` or `execution` / `executions` (partials, nested differently)
      * a flat legacy row (`fillId`, `fillPrice`, `dateExecuted`, `fillType` = TOTV/OTC, no `side`)
      * `{order}` only, with the fill fields on the order (`status` FILLED, `type` LIMIT/MARKET,
        `dateExecuted` set, `filledAt` absent). Value orders often have null `filledQuantity`
        and a `filledValue` instead.
    Cancelled / rejected rows come back with an empty fill so the caller can count them.
    """
    if not isinstance(item, dict):
        return []
    nested = _order_of(item)
    flat = not nested and "fill" not in item and "fills" not in item
    top = item if flat else {}
    order = dict(nested or {})
    if flat:
        order = {
            "id": item.get("id"), "side": item.get("side"), "ticker": item.get("ticker"),
            "status": item.get("status"), "type": item.get("type"), "currency": item.get("currency"),
            "filledQuantity": item.get("filledQuantity"), "filledValue": item.get("filledValue"),
            "quantity": item.get("orderedQuantity") or item.get("quantity"),
            "createdAt": item.get("dateCreated") or item.get("createdAt"),
            "dateExecuted": item.get("dateExecuted"), "dateModified": item.get("dateModified"),
            "fillPrice": item.get("fillPrice"), "fillId": item.get("fillId"), "fillType": item.get("fillType"),
            "extendedHours": item.get("extendedHours"),
            "initiatedFrom": item.get("initiatedFrom") or item.get("executor"),
            "instrument": _instrument_from({}, item), "taxes": item.get("taxes"),
            "walletImpact": item.get("walletImpact"),
        }
    else:
        if "instrument" not in order:
            order["instrument"] = _instrument_from(order, item)
        for k in ("dateExecuted", "filledAt", "fillPrice", "fillId", "fillType", "filledQuantity", "filledValue"):
            if order.get(k) is None and item.get(k) is not None:
                order[k] = item.get(k)
    blobs: list[dict] = []
    if isinstance(item.get("fills"), list) and item["fills"]:
        blobs = [x for x in item["fills"] if isinstance(x, dict)]
    elif isinstance(item.get("executions"), list) and item["executions"]:
        blobs = [x for x in item["executions"] if isinstance(x, dict)]
    elif isinstance(item.get("fill"), dict):
        blobs = [item["fill"]]
    elif isinstance(item.get("execution"), dict):
        blobs = [item["execution"]]
    if not blobs:
        blobs = [{}]
    rows = []
    top_row = top if flat else item
    for blob in blobs:
        fill = _synthesize_fill(order, blob, top_row)
        row_order = dict(order)
        if not row_order.get("side") and fill.get("side"):
            row_order["side"] = fill["side"]
        # A cancelled/rejected order has no execution. Do not keep a fill id copied from the order id.
        if not _looks_executed(row_order, fill if blob else {}, top_row):
            rows.append({"order": row_order, "fill": {}})
        else:
            rows.append({"order": row_order, "fill": fill})
    return rows or [{"order": order, "fill": {}}]


def resolve_fx(gross_instr: float, wallet: dict, instr_ccy: str | None, *, pence: bool = False) -> float | None:
    """Instrument-currency -> account-currency rate for one fill.

    The docs do not state the direction of walletImpact.fxRate, so choose between fxRate and 1/fxRate the one that
    reconciles with the fill's wallet value (|netValue| ~ quantity x price x rate; fees make it close, not exact).

    ``pence`` is set once a GBX price has been divided by 100 and stored in pounds. T212 still quotes that fxRate in
    pence per one account-currency unit (about 85 for EUR, not 0.85). Trying fxRate/100 as well picks the pound rate
    (~1.17 EUR per GBP) instead of the pence rate (~0.0117), which made fees and P&L ~100× too big."""
    if (instr_ccy or "").upper() == (wallet.get("currency") or "").upper():
        return 1.0
    fx = wallet.get("fxRate")
    if not fx:
        return None
    fx = float(fx)
    net = abs(float(wallet.get("netValue") or 0.0))
    scales = (1.0, 0.01) if pence else (1.0,)
    if not net or not gross_instr:
        scale = 0.01 if (pence and abs(fx) >= 20) else 1.0
        quoted = fx * scale
        return (1.0 / quoted) if quoted else None
    best: float | None = None
    best_err: float | None = None
    gross = abs(gross_instr)
    for scale in scales:
        quoted = fx * scale
        if not quoted:
            continue
        for rate in (quoted, 1.0 / quoted):
            err = abs(net - gross * rate)
            if best is None or err < best_err:
                best, best_err = rate, err
    return best


def fees_in_instrument(wallet: dict, fx: float | None, instr_ccy: str | None) -> float:
    """Sum walletImpact.taxes into the instrument currency (GBX amounts become pounds)."""
    fees = 0.0
    acct = (wallet.get("currency") or "").upper()
    instr = (instr_ccy or "").upper()
    for tax in wallet.get("taxes") or []:
        raw_amt = tax.get("quantity") if tax.get("quantity") is not None else tax.get("q")
        amt = abs(float(raw_amt or 0.0))
        tccy = (tax.get("currency") or tax.get("ccy") or acct).upper()
        if tccy == "GBX":
            amt, tccy = amt / 100.0, "GBP"
        if tccy == instr or not fx:
            fees += amt
        else:
            fees += amt / fx
    return fees


def account_gross(fills: list[tuple[str, float, float, float]]) -> float:
    """FIFO gross P&L in account currency.

    Each fill is ``(side, quantity, price_in_instrument_ccy, fx_to_account)``. This is what Trading 212 reports as
    ``walletImpact.realisedProfitLoss`` on the closing fill: each leg converted at its own rate, taxes not subtracted.
    """
    from collections import deque
    lots: deque[list[float]] = deque()
    pos = 0.0
    pnl = 0.0
    for side, qty, price, fx in fills:
        if side not in ("BUY", "SELL") or qty <= 0:
            continue
        rate = 1.0 if not fx else float(fx)
        sgn = 1.0 if side == "BUY" else -1.0
        remaining = float(qty)
        if abs(pos) > 1e-12 and (pos > 0) != (sgn > 0):
            close_q = min(remaining, abs(pos))
            direction = 1.0 if pos > 0 else -1.0
            to_match = close_q
            while to_match > 1e-12 and lots:
                lot = lots[0]
                m = min(lot[0], to_match)
                pnl += (price * rate - lot[1] * lot[2]) * m * direction
                lot[0] -= m
                to_match -= m
                if lot[0] <= 1e-12:
                    lots.popleft()
            pos += sgn * close_q
            remaining -= close_q
            if abs(pos) <= 1e-12:
                pos = 0.0
                lots.clear()
        if remaining > 1e-12:
            lots.append([remaining, float(price), rate])
            pos += sgn * remaining
    return pnl


def to_records(item: dict, inst: Instrument | None = None, symbol: str | None = None) -> list[ExecRecord | Skip]:
    """One history payload -> one record per real fill (a `fills` array is several)."""
    rows = history_rows(item)
    if not rows:
        return [Skip("no fill")]
    return [_to_record_one(row, inst, symbol) for row in rows]


def to_record(item: dict, inst: Instrument | None = None, symbol: str | None = None) -> ExecRecord | Skip:
    """One history item -> ExecRecord (pure; ``inst`` supplies symbol/exchange/currency, else they are derived)."""
    return to_records(item, inst, symbol)[0]


def _to_record_one(item: dict, inst: Instrument | None, symbol: str | None) -> ExecRecord | Skip:
    from app.corporate import FILL_KIND

    fill, order = item.get("fill") or {}, item.get("order") or {}
    if not fill:
        return Skip("no fill")
    ftype = _norm_type(fill.get("type") or "TRADE")
    kind = FILL_KIND.get(ftype, "TRADE" if ftype == "TRADE" else "")
    if not kind:
        return Skip(ftype.lower().replace("_", " ") or "not a trade")
    qty = abs(float(fill.get("quantity") or 0.0))
    price = float(fill.get("price") or 0.0)
    side = (order.get("side") or fill.get("side") or "").upper()
    when_s = fill.get("filledAt") or order.get("dateExecuted")
    if kind != "TRADE":
        # A corporate row adjusts the position. Side is only required when it is not a split.
        if side not in ("BUY", "SELL"):
            side = "SELL" if kind == "CONVERSION" and (order.get("side") or "").upper() == "SELL" else "BUY"
        if kind == "SPLIT":
            side = "BUY"
    if qty <= 0 or side not in ("BUY", "SELL") or not when_s or fill.get("id") is None:
        return Skip("incomplete")
    ii = order.get("instrument") or {}
    ticker = order.get("ticker") or ii.get("ticker") or ""
    info = markets.parse_t212_ticker(ticker, ii.get("isin"))
    sym = symbol or (inst.symbol if inst is not None else info.symbol)
    exch = inst.exchange if inst is not None else info.exchange
    ccy = (ii.get("currency") or order.get("currency") or "").upper() or None
    pence = ccy == "GBX"
    if pence:                              # pence -> pounds; the journal keeps GBP
        price, ccy = price / 100.0, "GBP"
    wallet = fill.get("walletImpact") or {}
    fx = resolve_fx(qty * price, wallet, ccy, pence=pence)
    fees = fees_in_instrument(wallet, fx, ccy)
    when = parse_ts(when_s)
    tz = markets.exchange_tz(exch)
    trade_date: date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date()
    return ExecRecord(
        external_id=str(fill["id"]), symbol=sym, underlying=sym, asset_type="STOCK", side=side,
        quantity=round(qty, 8), price=price, executed_at=when, fees=round(fees, 6), time_known=True,
        position_effect=None, kind=kind, multiplier=1.0,
        description=ii.get("name") or kind.replace("_", " ").title(),
        raw={"order_id": order.get("id"), "ticker": ticker, "isin": ii.get("isin"), "ccy": ccy,
             "wallet": {k: wallet.get(k) for k in ("currency", "fxRate", "netValue", "realisedProfitLoss")},
             "taxes": [{"name": t.get("name"), "q": t.get("quantity"), "ccy": t.get("currency")}
                       for t in wallet.get("taxes") or []],
             "extended_hours": order.get("extendedHours"), "initiated_from": order.get("initiatedFrom")},
        trade_date=trade_date, currency=ccy, fx_to_account=fx, instrument_id=inst.id if inst is not None else None)


# Share-moving transaction types. Cash ledger rows (deposit, fee, cash dividend) stay out.
_TX_SHARE_KIND = {
    "DIVIDEND_REINVESTMENT": "DRIP", "REINVESTMENT": "DRIP", "DRIP": "DRIP",
    "PIE": "TRADE", "PIE_BUY": "TRADE", "PIE_REBALANCE": "TRADE", "AUTOINVEST": "TRADE",
    "TRANSFER_IN": "TRANSFER", "TRANSFER_OUT": "TRANSFER", "SHARE_TRANSFER": "TRANSFER",
    "FRACTIONAL": "TRADE", "FRACTIONAL_SHARE": "TRADE",
}
_SHARE_DIVIDEND_TYPES = {
    "REINVESTMENT", "DRIP", "DIVIDEND_REINVESTMENT", "SCRIP", "SCRIP_DIVIDEND",
    "STOCK", "STOCK_DIVIDEND", "STOCK_DIVIDENDS",
}


def _share_qty(item: dict, *, allow_quantity: bool) -> float | None:
    """Shares acquired or removed. ``quantity`` on a cash dividend is the holding, so it is opt-in."""
    for key in ("reinvestedQuantity", "shareQuantity", "shares"):
        qty = _num(item.get(key))
        if qty:
            return qty
    if allow_quantity:
        return _num(item.get("quantity"))
    return None


def _transaction_record(item: dict) -> ExecRecord | None:
    """A share-moving transaction, or None for cash (deposit, fee, cash dividend).

    ``/equity/history/transactions`` is a cash ledger. A row is kept only when it names a share
    type (split, spin-off, distribution, scrip, DRIP, pie buy, transfer) and a ticker and a
    share quantity. The cash ``amount`` is never turned into shares.
    """
    if not isinstance(item, dict):
        return None
    from app.corporate import FILL_KIND
    typ = _norm_type(item.get("type") or item.get("transactionType"))
    kind = FILL_KIND.get(typ) or _TX_SHARE_KIND.get(typ)
    ii = item.get("instrument") if isinstance(item.get("instrument"), dict) else {}
    ticker = item.get("ticker") or ii.get("ticker") or ""
    qty = _share_qty(item, allow_quantity=kind is not None)
    if kind is None:
        return None
    when_s = item.get("dateTime") or item.get("time") or item.get("createdAt") or item.get("paidOn")
    ext = item.get("reference") if item.get("reference") is not None else item.get("id")
    if not ticker or not qty or abs(qty) <= 0 or not when_s or ext is None:
        return None
    info = markets.parse_t212_ticker(ticker, ii.get("isin") or item.get("isin"))
    side = (item.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        side = "SELL" if typ.endswith("_OUT") or "SELL" in typ else "BUY"
    when = parse_ts(str(when_s))
    trade_date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(markets.exchange_tz(info.exchange))).date()
    price = _num(item.get("price") if item.get("price") is not None else item.get("pricePerShare"))
    return ExecRecord(
        external_id="tx:" + str(ext), symbol=info.symbol or ticker, underlying=info.symbol or ticker,
        asset_type="STOCK", side=side, quantity=round(abs(qty), 8),
        price=price or 0.0,
        executed_at=when, fees=0.0, time_known=True, kind=kind,
        description=kind.replace("_", " ").title(),
        raw={"ticker": ticker, "type": typ, "isin": ii.get("isin") or item.get("isin"),
             "price_absent": price is None},
        trade_date=trade_date, currency=(ii.get("currency") or item.get("currency") or "").upper() or None,
    )


def _dividend_record(item: dict) -> ExecRecord | None:
    """A dividend reinvestment / scrip row, or None for a cash dividend.

    On ``/equity/history/dividends`` the ``quantity`` field is the shares the cash dividend was
    paid on, not shares bought. Only an explicit reinvestment type or share field becomes a fill.
    A missing price is stored as 0 and flagged; it is not estimated.
    """
    if not isinstance(item, dict):
        return None
    typ = _norm_type(item.get("type") or item.get("dividendType"))
    reinvested = bool(item.get("reinvested")) or any(x in typ for x in ("REINVEST", "DRIP", "SCRIP"))
    if typ not in _SHARE_DIVIDEND_TYPES and not reinvested:
        return None
    ii = item.get("instrument") if isinstance(item.get("instrument"), dict) else {}
    ticker = item.get("ticker") or ii.get("ticker") or ""
    qty = _share_qty(item, allow_quantity=typ in _SHARE_DIVIDEND_TYPES or reinvested)
    when_s = item.get("paidOn") or item.get("dateTime") or item.get("time")
    ext = item.get("reference") if item.get("reference") is not None else item.get("id")
    if not ticker or not qty or abs(qty) <= 0 or not when_s or ext is None:
        return None
    isin = ii.get("isin") or item.get("isin")
    info = markets.parse_t212_ticker(ticker, isin)
    price = _num(item.get("price") if item.get("price") is not None else item.get("pricePerShare"))
    when = parse_ts(str(when_s))
    trade_date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(markets.exchange_tz(info.exchange))).date()
    return ExecRecord(
        external_id="div:" + str(ext), symbol=info.symbol or ticker, underlying=info.symbol or ticker,
        asset_type="STOCK", side="BUY", quantity=round(abs(qty), 8), price=price or 0.0,
        executed_at=when, fees=0.0, time_known=True, kind="DRIP",
        description="Dividend reinvestment",
        raw={"ticker": ticker, "type": typ or "DRIP", "isin": isin, "price_absent": price is None},
        trade_date=trade_date, currency=(ii.get("currency") or item.get("currency") or "").upper() or None,
    )


def _recon_key(e) -> str:
    """Journal symbol, so a fill and the broker position for that name meet in one bucket."""
    return (getattr(e, "symbol", None) or "?").upper()


def repair_stored_fx(db: Session) -> dict:
    """Recompute fx and instrument-currency fees on stored T212 fills from their wallet raw.

    Idempotent. A GBX fill was saved with a pound price but the pence-per-EUR rate, so fees (and therefore net P&L
    and return %) are ~100× too big. The raw wallet still has the broker fxRate, netValue and taxes, which is enough
    to correct the row without calling Trading 212. Journal notes, tags and stops are left to ``rebuild_trades``.
    """
    from app.models import Execution
    fixed = 0
    accounts: set[int] = set()
    rows = list(db.scalars(select(Execution).where(Execution.source == SOURCE_KEY)))
    for row in rows:
        if _repair_row(row):
            fixed += 1
            accounts.add(row.account_id)
    if accounts:
        rebuild_trades(db, sorted(accounts))
        db.commit()
    return {"fixed": fixed, "accounts": len(accounts)}


def _repair_row(row) -> bool:
    if not row.raw:
        return False
    try:
        raw = json.loads(row.raw)
    except ValueError:
        return False
    wallet = dict(raw.get("wallet") or {})
    if not wallet.get("fxRate"):
        return False
    taxes = []
    for tax in raw.get("taxes") or []:
        taxes.append({
            "name": tax.get("name"),
            "quantity": tax.get("quantity") if tax.get("quantity") is not None else tax.get("q"),
            "currency": tax.get("currency") or tax.get("ccy"),
        })
    wallet["taxes"] = taxes
    ccy = (row.currency or raw.get("ccy") or "").upper() or None
    # Stored LSE prices are already pounds. Passing pence=True tries fxRate/100 as well; a true GBP quote
    # (fxRate ~ 0.85) still reconciles on the unscaled rate, so those rows are left alone.
    gross = abs((row.quantity or 0.0) * (row.price or 0.0))
    fx = resolve_fx(gross, wallet, ccy, pence=(ccy == "GBP"))
    fees = round(fees_in_instrument(wallet, fx, ccy), 6)
    changed = False
    if fx is not None and (row.fx_to_account is None or abs(row.fx_to_account - fx) > 1e-8):
        row.fx_to_account = fx
        changed = True
    if abs((row.fees or 0.0) - fees) > 5e-7:
        row.fees = fees
        changed = True
    return changed


# ------------------------------------------------------------------ status helpers
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


def test_connection(key: str, secret: str, env: str = "live", transport=None, sleep=None) -> dict:
    """Probe required scopes. Returns {ok, account_id, currency, scopes:{account,history,portfolio}, error}.

    History - Orders is required (that is the fill feed). Portfolio is optional (reconcile only).
    Metadata is not probed here: the instruments catalogue is large and rate-limited to 1 req / 50 s."""
    sleep = sleep or _time.sleep
    cl = T212Client(key, secret, env, transport=transport, sleep=sleep)
    out = {"ok": False, "account_id": None, "currency": None, "scopes": {}, "error": None, "kind": None}
    try:
        s = cl.account_summary()
        out["account_id"], out["currency"] = s.get("id"), (s.get("currency") or "").upper() or None
        out["scopes"]["account"] = True
        cl.get("/equity/history/orders", {"limit": 1})
        out["scopes"]["history"] = True
        try:
            sleep(1.0)
            cl.positions()
            out["scopes"]["portfolio"] = True
        except T212Error as exc:
            out["scopes"]["portfolio"] = False     # optional: only used to reconcile
            out["warning"] = "Portfolio permission missing: positions can't be reconciled (sync still works)."
            if exc.kind in ("auth", "rate"):
                out["scopes"]["portfolio"] = None
                out["error"], out["kind"] = str(exc), exc.kind
                return out
        out["ok"] = True
        return out
    except T212Error as exc:
        if "account" not in out["scopes"]:
            pass
        elif "history" not in out["scopes"]:
            out["scopes"]["history"] = False
        out["error"], out["kind"] = str(exc), exc.kind
        return out
    finally:
        cl.close()


def card(db: Session):
    from app.brokers import BrokerCard
    creds = load_credentials(db)
    st = last_status(db)
    if not creds:
        return BrokerCard("t212", "Trading 212", "Official API key + secret (read-only) · Invest & Stocks ISA", "EUR",
                          "not_connected", "Paste a read-only API key and secret to sync your fills.", form="t212_connect.html")
    state, detail = "connected", f"Key {mask(creds['key'])} ({'practice' if creds['env'] == 'demo' else 'live'})."
    if st.get("error_kind") in ("auth", "scope"):
        state, detail = "needs_login", st.get("error") or "Key rejected."
    elif st.get("account_id"):
        detail += f" Account {st['account_id']} · {st.get('currency') or '?'}."
    last = st.get("synced_at")
    return BrokerCard("t212", "Trading 212", "Official API key + secret (read-only) · Invest & Stocks ISA",
                      st.get("currency") or "EUR", state, detail, last_sync=last, form="t212_connect.html")


# ------------------------------------------------------------------ the data source
class Trading212Source(DataSource):
    key = SOURCE_KEY
    name = "Trading 212"
    description = ("Official API (read-only key + secret): fills with execution times, ISIN, instrument currency, fees and "
                   "FX per fill. Manual sync only; history is fetched once, later syncs only re-read the last few days.")

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

    def client(self, db: Session) -> T212Client:
        c = load_credentials(db)
        if not c:
            raise T212Error("auth", "Trading 212 is not connected (no API key stored).")
        return T212Client(c["key"], c["secret"], c.get("env", "live"), transport=self._transport, sleep=self._sleep)

    def status(self, db: Session) -> SourceStatus:
        creds = load_credentials(db)
        if not creds:
            return SourceStatus(False, False, "Not connected: add the API key and secret in Settings → Brokers.")
        st = last_status(db)
        if st.get("error_kind") in ("auth", "scope"):
            return SourceStatus(True, False, st.get("error") or "Key rejected.", "error",
                                banner={"level": "error", "text": "Trading 212: " + (st.get("error") or "key rejected"),
                                        "link": "/settings#brokers", "link_text": "Fix key"})
        return SourceStatus(True, True, "Connected" + (f" · last sync {st['synced_at']}" if st.get("synced_at") else ""))

    # -- account
    def ensure_account(self, db: Session, summary: dict) -> Account:
        ext = str(summary.get("id"))
        ccy = (summary.get("currency") or "EUR").upper()
        a = db.scalar(select(Account).where(Account.source_key == SOURCE_KEY, Account.external_ref == ext))
        if a is None:
            a = Account(name="Trading 212", broker="trading212", external_ref=ext, source_key=SOURCE_KEY,
                        account_number_masked="…" + ext[-4:], currency=ccy, timezone="Europe/London", market="EU",
                        kind="invest")
            db.add(a)
            db.flush()
        elif a.currency != ccy:
            a.currency = ccy
        return a

    # -- sync
    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        res = SourceResult()
        cl = None
        try:
            cl = self.client(db)
            summary = cl.account_summary()
            acct = self.ensure_account(db, summary)
            res.accounts.append(acct.id)
            full = get_state(db, FULL_STATE) == "1"
            if full:
                set_state(db, FULL_STATE, None)
            cursor = None if full else get_state(db, CURSOR_STATE)
            threshold = (datetime.fromisoformat(cursor) - timedelta(days=OVERLAP_DAYS)) if cursor else None
            items: list[dict] = []
            newest_first: bool | None = None
            for page in cl.history_pages():
                if newest_first is None:
                    newest_first = _page_newest_first(page)
                items.extend(page)
                # Stop only when this page is entirely older than the overlap window. A row that looks
                # filled but has no filledAt (dateExecuted, flat legacy item) must not be cut off, and
                # an oldest-first page must not be treated as newest-first just because its last item
                # has no fill.
                if threshold and newest_first and page:
                    times = [t for t in (item_time(i) for i in page) if t is not None]
                    blind = any(item_time(i) is None and _maybe_fill(i) for i in page)
                    if times and not blind and max(times) < threshold:
                        break
            recs, skipped, newest, detail, fields = self.convert(db, cl, items)
            extra_api, csv_recs, extra_note = self._extra_history(db, cl, full)
            recs.extend(extra_api)
            res.fetched = len(recs)
            stats = ingest_records(db, acct.id, SOURCE_KEY, recs)
            if csv_recs:
                from app.corporate import omit_duplicate_corporate
                csv_recs = omit_duplicate_corporate(db, acct.id, csv_recs)
                if csv_recs:
                    ingest_records(db, acct.id, "t212_csv", csv_recs)
            res.inserted, res.merged = stats.inserted, stats.merged
            if newest:
                set_state(db, CURSOR_STATE, max(newest, parse_ts(cursor) if cursor else newest).isoformat())
            rebuild_trades(db, [acct.id])
            prev_status = last_status(db)
            held = self._positions(cl)
            recon_before = self.reconcile(db, cl, acct, positions=held) if held is not None else (prev_status.get("reconcile") or [])
            adjustments = self.retire_estimated(db, acct, held)
            from app.corporate import audit_instruments
            issues = audit_instruments(db, fix=True)
            # A removed estimated row must rebuild even when nothing new was booked.
            rebuild_trades(db, [acct.id])
            recon = self.reconcile(db, cl, acct, positions=held) if held is not None else recon_before
            broker_qty = _broker_qty(db, acct.id, held) if held is not None else prev_status.get("broker_qty")
            sk = _skip_summary(skipped, detail)
            res.message = f"Trading 212: {len(recs)} fills read, {stats.inserted} new" + (f"; skipped {sk} (not trades)" if sk else "")
            if adjustments:
                res.message += f"; {len(adjustments)} estimated row(s) cleared or flagged"
            if recon:
                res.message += f"; {len(recon)} quantity gap(s) not booked as fills"
            if extra_note:
                res.message += "; " + extra_note
            if full:
                res.message += " (full history)"
            ctx.info(res.message)
            for reason, names in fields.items():
                ctx.info(f"Skipped {reason} fields: {', '.join(names)}")
            for line in recon:
                ctx.info("Trading 212 reconcile: " + line)
            saved = dict(
                ok=True, error=None, error_kind=None,
                synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                account_id=summary.get("id"), currency=acct.currency, fetched=len(recs), inserted=stats.inserted,
                skipped=dict(skipped), skipped_detail=detail, skip_fields=fields,
                reconcile=recon, reconcile_before=recon_before, adjustments=adjustments,
                instrument_issues=[{"symbol": i["symbol"], "notes": i["notes"], "fixed": i["fixed"]}
                                   for i in issues if i["notes"] or i["fixed"]],
                api_calls=cl.calls, full=full)
            if broker_qty is not None:
                saved["broker_qty"] = broker_qty
            _save_last(db, reset=True, **saved)
            db.commit()
            return res
        except T212Error as exc:
            db.rollback()
            _save_last(db, ok=False, error=str(exc), error_kind=exc.kind, failed_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))
            db.commit()
            raise RuntimeError(str(exc)) from exc
        finally:
            if cl is not None:
                cl.close()

    def convert(self, db: Session, cl: T212Client, items: list[dict]
                ) -> tuple[list[ExecRecord], Counter, datetime | None, dict, dict]:
        skipped: Counter = Counter()
        detail: dict[str, list[str]] = {}
        fields: dict[str, list[str]] = {}
        recs: list[ExecRecord] = []
        newest: datetime | None = None
        cache: dict[str, Instrument] = {}
        meta: dict[str, dict] | None = None

        def skip(reason: str, raw: dict) -> None:
            skipped[reason] += 1
            ticker = skip_ticker(raw)
            lst = detail.setdefault(reason, [])
            if ticker not in lst and len(lst) < 100:
                lst.append(ticker)
            fields.setdefault(reason, sample_fields(raw))

        for it in items:
            for row in history_rows(it):
                order, fill = row.get("order") or {}, row.get("fill") or {}
                if not fill:
                    status = _norm_type((_order_of(it).get("status") or it.get("status")))
                    skip("cancelled" if status in ("CANCELLED", "CANCELED") else
                         "rejected" if status == "REJECTED" else "unfilled", it)
                    continue
                ii = order.get("instrument") or {}
                ticker = order.get("ticker") or ii.get("ticker")
                if not ticker:
                    skip("no ticker", it)
                    continue
                inst = cache.get(ticker)
                if inst is None:
                    known = db.scalar(select(Instrument).where(Instrument.source_key == SOURCE_KEY, Instrument.broker_symbol == ticker))
                    short = None
                    if known is None:       # new instrument: ask the metadata endpoint once for the real short name
                        if meta is None:
                            meta = {}
                            try:
                                meta = {m["ticker"]: m for m in cl.instruments() if m.get("ticker")}
                            except T212Error:
                                pass        # optional scope / rate limit: fall back to the ticker's own base
                        short = (meta.get(ticker) or {}).get("shortName")
                    inst = symbolmap.upsert_t212(db, ticker, ii.get("isin"), ii.get("name"), ii.get("currency"), short)
                    cache[ticker] = inst
                r = _to_record_one(row, inst, None)
                if isinstance(r, Skip):
                    skip(r.reason if r.reason != "no fill" else "unfilled", it)
                    continue
                recs.append(r)
                newest = r.executed_at if newest is None or r.executed_at > newest else newest
        recs.sort(key=lambda r: (r.executed_at, r.external_id))
        return recs, skipped, newest, detail, fields

    def _positions(self, cl: T212Client) -> list[dict] | None:
        try:
            self._sleep(1.0)          # positions: 1 request / second
            return cl.positions() or []
        except T212Error:
            return None

    def _extra_history(self, db: Session, cl: T212Client, full: bool) -> tuple[list[ExecRecord], list[ExecRecord], str]:
        """Transactions and dividends (full re-scan) and at most one CSV-export step.

        History endpoints are about 20 requests/minute and the export endpoints are stricter
        (POST about 1/30s, GET about 1/min). A feed that is not ready is collected on the next sync.
        Failures are notes, not errors. Pagination walks every page the body points at.
        """
        notes = []
        api: list[ExecRecord] = []
        if full:
            for path, label, parser in (
                ("/equity/history/transactions", "transactions", _transaction_record),
                ("/equity/history/dividends", "dividends", _dividend_record),
            ):
                try:
                    n = 0
                    for page in cl.paged(path):
                        n += 1
                        for item in page:
                            rec = parser(item)
                            if isinstance(rec, ExecRecord):
                                api.append(rec)
                    if n:
                        notes.append(f"{label} {n} page(s)")
                    if cl.page_cap_hit:
                        notes.append(f"{label} stopped at the page cap")
                except T212Error as exc:
                    notes.append(f"{label} unavailable" if exc.kind in ("scope", "api") else f"{label}: {exc.kind}")
        csv_recs, export_note = self._csv_export(db, cl, full)
        if export_note:
            notes.append(export_note)
        return api, csv_recs, "; ".join(notes)

    def _csv_export(self, db: Session, cl: T212Client, full: bool) -> tuple[list[ExecRecord], str]:
        """Request or collect one CSV report. A finished file is imported once per parser generation.

        A report marked done by the previous parser (``done:<id>``) is downloaded again so
        Dividend (DRIP) and split open/close rows that were skipped can be imported. The id dedupes
        rows already stored. If Trading 212 has not finished the file, the next sync collects it.
        """
        from app.importers.t212_csv import parse as parse_csv
        pending = get_state(db, EXPORT_STATE) or ""
        legacy = pending.startswith("done:") and not pending.startswith(CSV_DONE)
        done_id = pending[len(CSV_DONE):] if pending.startswith(CSV_DONE) else (pending[5:] if legacy else "")
        report_id = pending if pending.isdigit() else ""
        reparse = legacy and bool(done_id)
        try:
            if full and not report_id and not reparse:
                created = cl.post("/equity/history/exports", {
                    "timeFrom": "2016-01-01T00:00:00Z",
                    "timeTo": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dataIncluded": {"includeDividends": True, "includeInterest": True,
                                     "includeOrders": True, "includeTransactions": True},
                })
                report_id = str((created or {}).get("reportId") or "")
                if report_id:
                    set_state(db, EXPORT_STATE, report_id)
            if reparse:
                report_id = done_id
            if not report_id:
                return [], ""
            reports = cl.get("/equity/history/exports")
        except T212Error as exc:
            return [], ("CSV export unavailable" if exc.kind in ("scope", "api") else f"CSV export: {exc.kind}")
        if not isinstance(reports, list):
            return [], "CSV export returned nothing"
        match = next((r for r in reports if str(r.get("reportId")) == report_id), None)
        if match is None:
            if reparse:
                set_state(db, EXPORT_STATE, None)
            return [], "CSV export queued"
        status = (match.get("status") or "")
        if status != "Finished":
            return [], f"CSV export {status or 'queued'} (imported on the next sync)"
        if done_id == report_id and not reparse:
            return [], ""
        link = match.get("downloadLink") or ""
        if not link:
            return [], "CSV export finished without a download link"
        try:
            resp = cl.http.get(link)
        except httpx.HTTPError:
            return [], "CSV download failed"
        if resp.status_code != 200 or not resp.content:
            return [], f"CSV download HTTP {resp.status_code}"
        text = resp.content.decode("utf-8-sig", errors="replace")
        try:
            parsed = parse_csv(text)
        except ValueError:
            return [], "CSV export was not a Trading 212 history file"
        set_state(db, EXPORT_STATE, CSV_DONE + report_id)
        return parsed.records, f"CSV export {len(parsed.records)} rows"

    def _listing_splits(self, db: Session, account_id: int) -> list[str]:
        """Pull split events for the listing the fills used. Tests skip the network unless a fetch is injected."""
        import os

        from app.corporate import import_listing_splits
        from app.prices import yahoo_split_events

        def fetch(symbol, start, end):
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return []
            try:
                return yahoo_split_events(symbol, start, end)
            except Exception:  # noqa: BLE001
                return []

        try:
            return import_listing_splits(db, account_id, self.split_fetch or fetch)
        except Exception:  # noqa: BLE001
            return []

    def retire_estimated(self, db: Session, acct: Account, positions: list[dict] | None) -> list[str]:
        """Delete auto-created estimated rows. Never create one.

        A row you edited is kept and flagged, unless real executions already match Trading 212
        (then it is removed so the estimate and the real fill are not both kept). ``positions``
        None means the broker quantity is unknown: unedited estimates are still deleted.
        """
        from app.corporate import open_state, user_edited

        groups, labels, _, _ = _exec_groups(db, acct.id)
        broker = _broker_qty(db, acct.id, positions) if positions is not None else None
        lines = []
        for key, rows in groups.items():
            est = [e for e in rows if _is_estimated_row(e)]
            if not est:
                continue
            label = labels.get(key) or (rows[0].symbol if rows else key)
            journal, _avg = open_state(rows)
            covered = False
            if broker is not None:
                covered = abs(journal - float(broker.get(label, 0.0))) <= 1e-4
            for e in list(est):
                raw = _load_raw(e)
                edited = user_edited(raw)
                if covered or not edited:
                    db.delete(e)
                    if covered:
                        lines.append(f"{e.symbol}: estimated row removed (a real Trading 212 fill covers this position)")
                    else:
                        lines.append(f"{e.symbol}: estimated row removed")
                else:
                    raw["estimated"] = True
                    raw["flagged"] = True
                    raw.setdefault("note", "Estimated row you edited. It is not a Trading 212 execution.")
                    e.raw = json.dumps(raw)
                    lines.append(f"{e.symbol}: estimated row kept (you edited it)")
        db.flush()
        return lines

    def apply_adjustments(self, db: Session, acct: Account, positions: list[dict] | None) -> list[str]:
        """Old name. Does not create fills."""
        return self.retire_estimated(db, acct, positions)

    def reconcile(self, db: Session, cl: T212Client, acct: Account, positions: list[dict] | None = None) -> list[str]:
        """Compare Trading 212 positions with executions. Estimated rows are not part of the journal side.

        A renamed listing (fills on DMYI, position still on that instrument) stays one symbol via the
        ISIN group. The line is a warning. It is not an execution. Pass ``positions`` to avoid a second
        Portfolio request in the same sync.
        """
        if positions is None:
            positions = self._positions(cl)
        if positions is None:
            return []
        broker = _broker_qty(db, acct.id, positions, upsert=True)
        journal = _journal_qty(db, acct.id)
        lines = []
        for sym in sorted(set(broker) | set(journal), key=str):
            lines.extend(_qty_diff(sym, broker.get(sym, 0.0), journal.get(sym, 0.0)))
        return lines

    def run_hunt(self, db: Session, symbol: str, client: T212Client | None = None) -> dict:
        """List every raw Trading 212 row for one symbol and import real position changes that are missing.

        Orders are read per ticker variant (no type, status or side filter), then transactions,
        dividends and one CSV-export step. A rate limit pauses; the next Find resumes that ticker.
        """
        sym = (symbol or "").strip().upper()
        data = _hunt_load(db)
        resume = (data.get("symbol") or "").upper() == sym and data.get("status") in ("running", "paused")
        if not resume:
            data = {"symbol": sym, "ticker_index": 0, "tickers": [], "isins": [], "rows": [],
                    "rows_total": 0, "imported": 0, "seen": [], "pages": 0, "note": ""}
        data["status"] = "running"
        data["symbol"] = sym
        cl = client
        own = client is None
        try:
            if cl is None:
                cl = self.client(db)
            acct = db.scalar(select(Account).where(Account.source_key == SOURCE_KEY).order_by(Account.id))
            if acct is None:
                acct = self.ensure_account(db, cl.account_summary())
                db.commit()
            if data.get("tickers"):
                tickers = list(data["tickers"])
                isins = {s.upper() for s in (data.get("isins") or [])}
            else:
                tickers, isins = _ticker_variants(db, cl, sym)
                data["tickers"] = tickers
                data["isins"] = sorted(isins)
            _hunt_store(db, data)
            known = _known_ids(db, acct.id)
            cache: dict = {}
            start = int(data.get("ticker_index") or 0)
            for i, ticker in enumerate(tickers):
                if i < start:
                    continue
                data["phase"] = "orders"
                data["ticker_index"] = i
                data["progress"] = f"Orders for {ticker} (about 20 requests/minute)"
                _hunt_store(db, data)
                batch: list[ExecRecord] = []
                try:
                    for page in cl.paged("/equity/history/orders", {"ticker": ticker}, on_page=_page_cb(db, data, ticker)):
                        for item in page:
                            if not isinstance(item, dict):
                                continue
                            if not _hunt_match(item, tickers, isins, sym):
                                continue
                            recs = _order_records(db, item, cache)
                            _remember(data, item, "orders", known, recs)
                            for rec in recs:
                                _force_symbol(rec, sym, isins)
                                if rec.external_id not in known:
                                    batch.append(rec)
                                    known.add(rec.external_id)
                except T212Error as exc:
                    if exc.kind == "rate":
                        return _hunt_pause(db, data, i)
                    data["note"] = f"{ticker}: {exc.kind}"
                    _hunt_store(db, data)
                self._take(db, acct, batch, data)
                data["ticker_index"] = i + 1
            for path, label, parser in (
                ("/equity/history/transactions", "transactions", _transaction_record),
                ("/equity/history/dividends", "dividends", _dividend_record),
            ):
                data["phase"] = label
                data["progress"] = f"{label} (about 20 requests/minute)"
                _hunt_store(db, data)
                batch = []
                try:
                    for page in cl.paged(path, on_page=_page_cb(db, data, label)):
                        for item in page:
                            if not isinstance(item, dict) or not _hunt_match(item, tickers, isins, sym):
                                continue
                            rec = parser(item)
                            recs = [rec] if isinstance(rec, ExecRecord) else []
                            _remember(data, item, label, known, recs)
                            for rec in recs:
                                _force_symbol(rec, sym, isins)
                                if rec.external_id not in known:
                                    batch.append(rec)
                                    known.add(rec.external_id)
                    if cl.page_cap_hit:
                        data["note"] = f"{label} stopped at the page cap"
                except T212Error as exc:
                    if exc.kind == "rate":
                        return _hunt_pause(db, data, len(tickers))
                    data["note"] = f"{label}: {exc.kind}"
                self._take(db, acct, batch, data)
            data["phase"] = "csv"
            data["progress"] = "CSV export (strict rate limit; the next sync collects it if it is not ready)"
            _hunt_store(db, data)
            csv_batch: list[ExecRecord] = []
            try:
                csv_recs, export_note = self._csv_export(db, cl, True)
            except T212Error as exc:
                if exc.kind == "rate":
                    return _hunt_pause(db, data, len(tickers))
                csv_recs, export_note = [], exc.kind
            if export_note:
                data["note"] = export_note
            elif (get_state(db, EXPORT_STATE) or "").startswith(CSV_DONE):
                data["note"] = "CSV report already imported. Upload a newer History export on the Import page if a row is still missing."
            for rec in csv_recs:
                _force_symbol(rec, sym, isins)
                if not _record_match(rec, sym, isins):
                    continue
                _remember_record(data, rec, known)
                if rec.external_id not in known:
                    csv_batch.append(rec)
                    known.add(rec.external_id)
            if csv_batch:
                from app.corporate import omit_duplicate_corporate
                csv_batch = omit_duplicate_corporate(db, acct.id, csv_batch)
                self._take(db, acct, csv_batch, data, source="t212_csv")
            rebuild_trades(db, [acct.id])
            held = self._positions(cl)
            adjustments = self.retire_estimated(db, acct, held)
            rebuild_trades(db, [acct.id])
            recon = self.reconcile(db, cl, acct, positions=held) if held is not None else []
            broker_qty = _broker_qty(db, acct.id, held) if held is not None else None
            saved = {"reconcile": recon, "adjustments": adjustments}
            if broker_qty is not None:
                saved["broker_qty"] = broker_qty
            _save_last(db, **saved)
            data["status"] = "done"
            data["phase"] = "done"
            data["imported"] = int(data.get("imported") or 0)
            data["progress"] = f"Done. {data['imported']} new row(s) imported, {data.get('rows_total') or 0} listed."
            if recon:
                extra = (" Trading 212 did not return a position-changing row for the remaining gap. "
                         "Upload History → Export on the Import page.")
                data["note"] = ((data.get("note") or "") + " " + "; ".join(recon) + "." + extra).strip()
            _hunt_store(db, data)
            return data
        except T212Error as exc:
            if exc.kind == "rate":
                return _hunt_pause(db, data, int(data.get("ticker_index") or 0))
            data["status"] = "error"
            data["note"] = exc.kind
            _hunt_store(db, data)
            return data
        except Exception:
            data["status"] = "error"
            data["note"] = "error"
            _hunt_store(db, data)
            return data
        finally:
            if own and cl is not None:
                cl.close()

    def _take(self, db: Session, acct: Account, batch: list[ExecRecord], data: dict, source: str = SOURCE_KEY) -> None:
        if not batch:
            return
        stats = ingest_records(db, acct.id, source, batch)
        got = {r.external_id for r in batch}
        for row in data.get("rows") or []:
            if row.get("ext") in got:
                row["imported"] = True
        data["imported"] = int(data.get("imported") or 0) + int(stats.inserted or 0)
        _hunt_store(db, data)


# ------------------------------------------------------------------ quantity gaps (warnings, not fills)
def _load_raw(e) -> dict:
    raw = getattr(e, "raw", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_estimated_row(e) -> bool:
    return str(getattr(e, "external_id", "") or "").startswith("recon:") or bool(_load_raw(e).get("estimated"))


def _exec_groups(db: Session, account_id: int):
    """Executions grouped by the symbol the journal builds. Estimated rows do not name the group."""
    from app.corporate import group_symbols
    from app.models import Execution
    from app.symbols import load_aliases

    execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
    inst_ids = {e.instrument_id for e in execs if e.instrument_id}
    instruments = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(inst_ids or [-1])))}
    grouped = group_symbols(execs, instruments, load_aliases(db))
    groups: dict[str, list] = {}
    labels: dict[str, str] = {}
    inst_group: dict[int, str] = {}
    for e in execs:
        name = grouped.get(e.id) or e.symbol or "?"
        key = name.upper()
        groups.setdefault(key, []).append(e)
        if not _is_estimated_row(e):
            labels.setdefault(key, name)
            if e.instrument_id and not str(e.external_id or "").startswith("recon:"):
                inst_group.setdefault(e.instrument_id, key)
    return groups, labels, inst_group, instruments


def _broker_qty(db: Session, account_id: int, positions: list[dict] | None, upsert: bool = True) -> dict[str, float]:
    """Trading 212 position quantity by the journal symbol. Empty when the broker returned no rows."""
    if not positions:
        return {}
    _groups, labels, inst_group, _instruments = _exec_groups(db, account_id)
    out: dict[str, float] = {}
    for p in positions:
        ii = p.get("instrument") or {}
        ticker = ii.get("ticker") or ""
        if not ticker:
            continue
        inst = db.scalar(select(Instrument).where(Instrument.source_key == SOURCE_KEY,
                                                  Instrument.broker_symbol == ticker))
        if inst is None and upsert:
            inst = symbolmap.upsert_t212(db, ticker, ii.get("isin"), ii.get("name"), ii.get("currency"))
        parsed = markets.parse_t212_ticker(ticker, ii.get("isin")).symbol or "?"
        if inst is not None and inst.id in inst_group:
            key = inst_group[inst.id]
        else:
            sym = (inst.symbol if inst is not None else None) or parsed
            key = (sym or "?").upper()
        label = labels.get(key) or (inst.symbol if inst is not None else None) or parsed
        out[label] = out.get(label, 0.0) + float(p.get("quantity") or 0.0)
    return out


def _journal_qty(db: Session, account_id: int) -> dict[str, float]:
    """Open quantity from real executions. ``recon:`` rows are ignored, so an estimate cannot hide a gap."""
    from app.corporate import open_state

    groups, labels, _, _ = _exec_groups(db, account_id)
    out: dict[str, float] = {}
    for key, rows in groups.items():
        qty, _avg = open_state(rows)
        if abs(qty) <= 1e-8:
            continue
        label = labels.get(key) or (rows[0].symbol if rows else key)
        out[label] = out.get(label, 0.0) + qty
    return out


_OLD_HOLDS = __import__("re").compile(
    r"^(?P<sym>[^:]+):\s+missing buys\s+[—-]\s+Trading 212 holds\s+(?P<b>[-0-9.eE]+)"
)


def _stored_broker_qty(db: Session) -> tuple[dict[str, float], bool]:
    """(quantities, known). Missing key means the account has not been reconciled since this change."""
    st = last_status(db)
    raw = st.get("broker_qty") if isinstance(st, dict) else None
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out, True
    out = {}
    for line in (st.get("reconcile") or []):
        m = _OLD_HOLDS.search(str(line))
        if m:
            out[m.group("sym").strip()] = float(m.group("b"))
    return out, False


def gap_index(db: Session) -> dict:
    """Symbol → gap dict. Keys include the display symbol and its uppercase form."""
    broker, _known = _stored_broker_qty(db)
    journal: dict[str, float] = {}
    for acct_id in db.scalars(select(Account.id).where(Account.source_key == SOURCE_KEY)):
        for sym, qty in _journal_qty(db, acct_id).items():
            journal[sym] = journal.get(sym, 0.0) + float(qty)

    def fold(src: dict[str, float]) -> tuple[dict[str, float], dict[str, str]]:
        totals: dict[str, float] = {}
        labels: dict[str, str] = {}
        for k, v in src.items():
            key = k.upper()
            totals[key] = totals.get(key, 0.0) + float(v)
            labels.setdefault(key, k)
        return totals, labels

    bmap, blabels = fold(broker)
    jmap, jlabels = fold(journal)
    out: dict = {}
    for key in set(bmap) | set(jmap):
        b, j = bmap.get(key, 0.0), jmap.get(key, 0.0)
        delta = b - j
        if abs(delta) <= 1e-4:
            continue
        label = blabels.get(key) or jlabels.get(key) or key
        if delta > 0:
            text = f"missing {fmt_sh(delta)} sh vs Trading 212"
            badge = f"missing {fmt_sh(delta)} sh"
        else:
            text = f"journal has {fmt_sh(-delta)} sh more than Trading 212"
            badge = f"{fmt_sh(-delta)} sh over"
        detail = (f"Trading 212 holds {fmt_sh(b)} sh; executions add up to {fmt_sh(j)} sh; "
                  f"{text} — not booked as a fill")
        row = {"symbol": label, "broker": b, "journal": j, "delta": delta,
               "text": text, "badge": badge, "detail": detail}
        out[label] = row
        out[key] = row
    return out


def gap_rows(db: Session) -> list[dict]:
    seen: set[str] = set()
    rows = []
    for row in gap_index(db).values():
        if row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        rows.append(row)
    rows.sort(key=lambda r: r["symbol"])
    return rows


# ------------------------------------------------------------------ find-missing diagnostic
_HUNT_ROWS = 300
_HUNT_VARIANTS = 12
_WMT_ISIN = "US9311421039"


def _hunt_load(db: Session) -> dict:
    try:
        data = json.loads(get_state(db, HUNT_STATE) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _hunt_store(db: Session, data: dict) -> None:
    data["rows"] = list(data.get("rows") or [])[-_HUNT_ROWS:]
    data["seen"] = list(dict.fromkeys(data.get("seen") or []))[-4000:]
    data["updated"] = utcnow().replace(microsecond=0).isoformat()
    for key in list(data):
        if str(key).lower() in _SECRET_KEYS:
            data.pop(key, None)
    set_state(db, HUNT_STATE, json.dumps(data, default=str))
    db.commit()


def hunt_view(db: Session) -> dict:
    data = _hunt_load(db)
    data.setdefault("status", "")
    data.setdefault("rows", [])
    data.setdefault("symbol", "")
    return data


def _hunt_pause(db: Session, data: dict, ticker_index: int) -> dict:
    data["status"] = "paused"
    data["ticker_index"] = ticker_index
    data["note"] = "Rate limit. Progress is saved. Press Find again to resume."
    data["progress"] = data.get("progress") or "Paused"
    _hunt_store(db, data)
    return data


def start_hunt(symbol: str) -> None:
    """Background Find. The encrypted key stays in the app database; this only starts the thread."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return
    from app import db as dbmod
    db = dbmod.SessionLocal()
    try:
        cur = _hunt_load(db)
        if cur.get("status") == "running":
            updated = cur.get("updated") or ""
            try:
                age = utcnow() - datetime.fromisoformat(updated)
            except ValueError:
                age = timedelta(hours=1)
            if age < timedelta(minutes=15):
                return
        if cur.get("status") == "paused" and (cur.get("symbol") or "").upper() == sym:
            cur["status"] = "running"
            cur["progress"] = f"Resuming {sym}…"
            cur["note"] = ""
            _hunt_store(db, cur)
        else:
            _hunt_store(db, {
                "status": "running", "symbol": sym, "phase": "starting", "ticker_index": 0,
                "tickers": [], "isins": [], "pages": 0, "progress": f"Looking up {sym}…",
                "rows": [], "rows_total": 0, "imported": 0, "note": "", "seen": [],
            })
    finally:
        db.close()
    threading.Thread(target=_hunt_thread, args=(sym,), name="t212-hunt", daemon=True).start()


def _hunt_thread(symbol: str) -> None:
    from app import db as dbmod
    db = dbmod.SessionLocal()
    try:
        Trading212Source().run_hunt(db, symbol)
    except Exception:
        try:
            db.rollback()
            data = _hunt_load(db)
            data["status"] = "error"
            data["note"] = "error"
            _hunt_store(db, data)
        except Exception:
            pass
    finally:
        db.close()


def _page_cb(db: Session, data: dict, label: str):
    def on_page(n: int, _path: str) -> None:
        data["pages"] = int(data.get("pages") or 0) + 1
        data["progress"] = f"{label} page {n} (about 20 requests/minute)"
        _hunt_store(db, data)
    return on_page


def _ticker_variants(db: Session, cl: T212Client, sym: str) -> tuple[list[str], set[str]]:
    """Broker tickers for one company: stored rows, the ISIN, and the usual Trading 212 suffixes."""
    tickers: list[str] = []
    isins: set[str] = set()
    if sym == "WMT":
        isins.add(_WMT_ISIN)

    def add(ticker: str | None) -> None:
        t = (ticker or "").strip()
        if not t or len(tickers) >= _HUNT_VARIANTS:
            return
        if t.upper() in {x.upper() for x in tickers}:
            return
        tickers.append(t)

    for inst in db.scalars(select(Instrument).where(Instrument.source_key == SOURCE_KEY)):
        isin = (inst.isin or "").upper()
        base = markets.parse_t212_ticker(inst.broker_symbol or "", inst.isin).symbol
        if (inst.symbol or "").upper() == sym or (base or "").upper() == sym or (isin and isin in isins):
            add(inst.broker_symbol)
            if isin:
                isins.add(isin)
    try:
        for m in cl.instruments() or []:
            if not isinstance(m, dict):
                continue
            isin = (m.get("isin") or "").upper()
            ticker = m.get("ticker") or ""
            base = markets.parse_t212_ticker(ticker, isin or None).symbol
            if (isin and isin in isins) or (base or "").upper() == sym:
                if isin:
                    isins.add(isin)
                add(ticker)
    except T212Error:
        pass
    add(f"{sym}_US_EQ")
    add(f"{sym}_EQ")
    add(sym)
    add(f"{sym}.US")
    add(f"{sym}_US")
    return tickers[:_HUNT_VARIANTS], isins


def _hunt_match(item: dict, tickers: list[str], isins: set[str], sym: str) -> bool:
    ticker = (_ticker_of(item) or item.get("ticker") or "")
    wanted = {t.upper() for t in tickers}
    if ticker and ticker.upper() in wanted:
        return True
    isin = (_isin_of(item) or item.get("isin") or "")
    if isin and isin.upper() in isins:
        return True
    if ticker:
        base = markets.parse_t212_ticker(ticker, isin or None).symbol
        if (base or "").upper() == sym:
            return True
    return False


def _known_ids(db: Session, account_id: int) -> set[str]:
    from app.models import Execution
    return set(db.scalars(select(Execution.external_id).where(
        Execution.account_id == account_id, Execution.source.in_(("t212", "t212_csv")))))


def _order_records(db: Session, item: dict, cache: dict) -> list[ExecRecord]:
    """Map one history item to fills. Does not call the instruments catalogue (the variant walk already did)."""
    out = []
    for row in history_rows(item):
        order = row.get("order") or {}
        if not row.get("fill"):
            continue
        ii = order.get("instrument") or {}
        ticker = order.get("ticker") or ii.get("ticker")
        if not ticker:
            continue
        inst = cache.get(ticker)
        if inst is None:
            inst = symbolmap.upsert_t212(db, ticker, ii.get("isin"), ii.get("name"), ii.get("currency"))
            cache[ticker] = inst
        rec = _to_record_one(row, inst, None)
        if isinstance(rec, ExecRecord):
            out.append(rec)
    return out


def _item_ext(item: dict, recs: list[ExecRecord]) -> str:
    if recs:
        return recs[0].external_id
    order = _order_of(item)
    fill = item.get("fill") if isinstance(item.get("fill"), dict) else {}
    ext = fill.get("id")
    if ext is None:
        ext = item.get("reference") if item.get("reference") is not None else item.get("id")
    if ext is None:
        ext = order.get("id")
    return "" if ext is None else str(ext)


def _remember(data: dict, item: dict, source: str, known: set[str], recs: list[ExecRecord]) -> None:
    ext = _item_ext(item, recs)
    key = f"{source}:{ext}:{_ticker_of(item) or item.get('ticker') or ''}"
    seen = data.setdefault("seen", [])
    if key in seen:
        return
    seen.append(key)
    order = _order_of(item)
    fill = item.get("fill") if isinstance(item.get("fill"), dict) else {}
    when = (fill.get("filledAt") or fill.get("dateExecuted") or item.get("dateTime")
            or item.get("paidOn") or item.get("time") or order.get("dateExecuted") or "")
    typ = fill.get("type") or order.get("type") or item.get("type") or item.get("transactionType") or ""
    qty = _num(fill.get("quantity")) if fill else None
    if qty is None:
        qty = _num(item.get("reinvestedQuantity") if item.get("reinvestedQuantity") is not None else item.get("quantity"))
    price = _num(fill.get("price")) if fill else None
    if price is None:
        price = _num(item.get("price") if item.get("price") is not None else item.get("pricePerShare"))
    data.setdefault("rows", []).append({
        "date": str(when)[:32], "type": str(typ)[:48],
        "quantity": qty if qty is not None else "", "price": price if price is not None else "",
        "id": ext[:80], "ext": (recs[0].external_id if recs else ext)[:80],
        "ticker": str(_ticker_of(item) or item.get("ticker") or "")[:40],
        "source": source, "fields": field_names(item),
        "in_journal": bool(recs and recs[0].external_id in known) or (bool(ext) and ext in known),
        "imported": False,
    })
    data["rows_total"] = int(data.get("rows_total") or 0) + 1
    if len(data["rows"]) > _HUNT_ROWS:
        data["rows"] = data["rows"][-_HUNT_ROWS:]


def _remember_record(data: dict, rec: ExecRecord, known: set[str]) -> None:
    key = f"csv:{rec.external_id}"
    seen = data.setdefault("seen", [])
    if key in seen:
        return
    seen.append(key)
    raw = rec.raw or {}
    data.setdefault("rows", []).append({
        "date": rec.executed_at.isoformat(sep=" ", timespec="seconds"),
        "type": (raw.get("action") or rec.kind or "")[:48],
        "quantity": rec.quantity, "price": rec.price, "id": rec.external_id[:80], "ext": rec.external_id[:80],
        "ticker": str(raw.get("ticker") or rec.symbol)[:40], "source": "csv",
        "fields": sorted(raw.keys()),
        "in_journal": rec.external_id in known, "imported": False,
    })
    data["rows_total"] = int(data.get("rows_total") or 0) + 1


def _force_symbol(rec: ExecRecord, sym: str, isins: set[str]) -> None:
    isin = str((rec.raw or {}).get("isin") or "").upper()
    if rec.symbol.upper() == sym or (isin and isin in isins):
        rec.symbol = sym
        rec.underlying = sym


def _record_match(rec: ExecRecord, sym: str, isins: set[str]) -> bool:
    if (rec.symbol or "").upper() == sym:
        return True
    return str((rec.raw or {}).get("isin") or "").upper() in isins
