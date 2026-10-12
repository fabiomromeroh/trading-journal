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
(``/equity/history/transactions``) and the async CSV export are read on a full re-scan when the key allows it;
neither call is repeated until the rate-limit window allows. A position the feed still cannot explain becomes one
estimated reconciliation adjustment. Credentials live Fernet-encrypted in app_state (cred:t212; never in backups)."""
from __future__ import annotations

import json
import logging
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
from app.models import Account, Instrument, Trade, utcnow
from app.services import get_state, ingest_records, rebuild_trades, set_state
from app.sources.base import DataSource, SourceResult, SourceStatus, SyncContext

log = logging.getLogger(__name__)
# httpx can log request lines; never let Basic-auth credentials reach the server log.
logging.getLogger("httpx").setLevel(logging.WARNING)

HOSTS = {"live": "https://live.trading212.com/api/v0", "demo": "https://demo.trading212.com/api/v0"}
CRED_STATE = "cred:t212"        # encrypted {"key","secret","env"}; the cred: prefix keeps it out of JSON backups
LAST_STATE = "t212:last"        # last sync / test status (no secrets)
CURSOR_STATE = "t212:cursor"    # newest filledAt stored (ISO, UTC)
DIV_CURSOR_STATE = "t212:div_cursor"  # newest dividend paidOn stored (ISO, UTC)
FULL_STATE = "t212:full"        # "1" → next sync ignores the cursor (one-off full re-scan)
EXPORT_STATE = "t212:export"    # pending CSV reportId, or "done:<id>" once that file was imported
RECON_STATE = "t212:recon"      # {"seen": {key: iso}, "dismissed": [key]} — not a secret
OVERLAP_DAYS = 3
MAX_EXTRA_PAGES = 40            # transactions on a full re-scan; history is 20 req/min
PAGE_LIMIT = 50
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
        yield from self.paged("/equity/history/orders", limit=None)

    def paged(self, path: str, limit: int | None = MAX_EXTRA_PAGES):
        """Follow nextPagePath. ``limit`` caps pages so a full re-scan cannot sit on the 20/min bucket."""
        nxt, params, n = path, {"limit": PAGE_LIMIT}, 0
        while nxt:
            data = self.get(nxt, params)
            n += 1
            if isinstance(data, list):
                yield data
                return
            yield data.get("items") or []
            nxt, params = data.get("nextPagePath"), None
            if limit is not None and n >= limit:
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


def _qty_diff(sym: str, theirs: float, journal: float) -> list[str]:
    if abs(theirs - journal) <= 1e-4:
        return []
    if theirs > journal:
        return [f"{sym}: missing buys — Trading 212 holds {theirs:g}, journal {journal:g}"]
    return [f"{sym}: journal holds more — Trading 212 {theirs:g}, journal {journal:g} (sell, split or transfer?)"]


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


def _div_time(item: dict) -> datetime | None:
    if not isinstance(item, dict):
        return None
    v = item.get("paidOn") or item.get("paid_on") or item.get("date")
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return parse_ts(v)
    except (TypeError, ValueError):
        return None


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


def dividend_record(item: dict, inst: Instrument | None = None, symbol: str | None = None):
    """One T212 ``/equity/history/dividends`` (or ``/api/v0/history/dividends``) item -> DividendIn.

    ``amount`` is the net cash paid, in the account's primary currency. GBX per-share figures are
    stored in pounds. The cash amount itself is never estimated.
    """
    from app.dividends import DividendIn, scale_gbx
    if not isinstance(item, dict):
        return None
    ref = item.get("reference")
    if ref is None or ref == "":
        ref = item.get("id")
    paid_s = item.get("paidOn") or item.get("paid_on") or item.get("date")
    amt = _num(item.get("amount"))
    if ref is None or not paid_s or amt is None:
        return None
    try:
        when = parse_ts(str(paid_s))
    except (TypeError, ValueError):
        return None
    ii = item.get("instrument") if isinstance(item.get("instrument"), dict) else {}
    ticker = item.get("ticker") or ii.get("ticker") or ""
    info = markets.parse_t212_ticker(ticker, ii.get("isin")) if ticker else None
    sym = symbol or (inst.symbol if inst is not None else None) or (info.symbol if info else None) or ticker
    if not sym:
        return None
    acct_amt, acct_ccy = scale_gbx(amt, item.get("currency"))
    gps_raw = _num(item.get("grossAmountPerShare") if item.get("grossAmountPerShare") is not None
                   else item.get("gross_amount_per_share"))
    instr_ccy = (item.get("tickerCurrency") or ii.get("currency") or "").upper() or None
    gps, instr_ccy = scale_gbx(gps_raw, instr_ccy)
    euro = _num(item.get("amountInEuro") if item.get("amountInEuro") is not None else item.get("amount_in_euro"))
    # Paid cash stays the broker's ``amount`` (account currency). amountInEuro is the same cash
    # when the account is not EUR. Per-share × quantity is gross, so it is never used as the total.
    if instr_ccy and acct_ccy and instr_ccy == acct_ccy:
        amount, currency, fx = acct_amt, acct_ccy, 1.0
    elif euro is not None and (acct_ccy or "") != "EUR" and instr_ccy == "EUR":
        amount, currency = euro, "EUR"
        fx = (acct_amt / euro) if euro else None
    else:
        amount, currency, fx = acct_amt, acct_ccy, None
    return DividendIn(
        external_id=str(ref), symbol=sym, paid_on=when, amount=float(amount), currency=currency,
        amount_account=float(acct_amt) if acct_amt is not None else None, account_currency=acct_ccy,
        fx_to_account=fx, quantity=_num(item.get("quantity")), gross_per_share=gps,
        kind=(item.get("type") or "DIVIDEND"), ticker=ticker or None,
        instrument_id=inst.id if inst is not None else None,
        raw={"ticker": ticker, "type": item.get("type"), "ccy": acct_ccy, "instr_ccy": instr_ccy,
             "amount": amt, "amountInEuro": euro, "quantity": item.get("quantity"),
             "grossAmountPerShare": gps_raw},
    )


def _transaction_record(item: dict) -> ExecRecord | None:
    """A share-moving transaction, or None for cash (deposit, fee, cash dividend).

    ``/equity/history/transactions`` is a cash ledger. A row is kept only when it names a known
    share type (split, spin-off, distribution, scrip) and a ticker and a share quantity.
    """
    if not isinstance(item, dict):
        return None
    from app.corporate import FILL_KIND
    kind = FILL_KIND.get(_norm_type(item.get("type") or item.get("transactionType")))
    if not kind:
        return None
    ii = item.get("instrument") if isinstance(item.get("instrument"), dict) else {}
    ticker = item.get("ticker") or ii.get("ticker") or ""
    qty = _num(item.get("quantity") if item.get("quantity") is not None else item.get("shares"))
    when_s = item.get("dateTime") or item.get("time") or item.get("createdAt")
    ext = item.get("reference") if item.get("reference") is not None else item.get("id")
    if not ticker or not qty or abs(qty) <= 0 or not when_s or ext is None:
        return None
    info = markets.parse_t212_ticker(ticker, ii.get("isin"))
    side = (item.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"
    when = parse_ts(str(when_s))
    trade_date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(markets.exchange_tz(info.exchange))).date()
    return ExecRecord(
        external_id="tx:" + str(ext), symbol=info.symbol or ticker, underlying=info.symbol or ticker,
        asset_type="STOCK", side=side, quantity=round(abs(qty), 8),
        price=_num(item.get("price") or item.get("pricePerShare")) or 0.0,
        executed_at=when, fees=0.0, time_known=True, kind=kind,
        description=kind.replace("_", " ").title(),
        raw={"ticker": ticker, "type": _norm_type(item.get("type") or item.get("transactionType"))},
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
            div_note = self._import_dividends(db, cl, acct, full)
            recs.extend(extra_api)
            res.fetched = len(recs)
            from app.adjustments import record_payload
            record_payload(db, acct.id, SOURCE_KEY, {"history": items, "extra": len(extra_api)},
                           run_id=ctx.run_id)
            stats = ingest_records(db, acct.id, SOURCE_KEY, recs)
            if csv_recs:
                from app.corporate import omit_duplicate_corporate
                csv_recs = omit_duplicate_corporate(db, acct.id, csv_recs)
                if csv_recs:
                    ingest_records(db, acct.id, "t212_csv", csv_recs)
            res.inserted, res.merged = stats.inserted, stats.merged
            if newest:
                set_state(db, CURSOR_STATE, max(newest, parse_ts(cursor) if cursor else newest).isoformat())
            held = self._positions(cl)
            from app.adjustments import detect_and_apply, t212_positions
            corp = detect_and_apply(db, acct.id, fetch=self.split_fetch, positions=t212_positions(db, held))
            split_notes = self._listing_splits(db, acct.id)
            split_notes = corp + split_notes
            rebuild_trades(db, [acct.id])
            recon_before = self.reconcile(db, cl, acct, positions=held)
            adjustments = self.apply_adjustments(db, acct, held)
            # The new gap is often the missing split (WMT 3-for-1, CGC 1-for-10). Fetch again
            # in this same re-scan, then drop the estimate if the listing split closed the gap.
            again = self._listing_splits(db, acct.id)
            if again:
                split_notes.extend(again)
                adjustments = self.apply_adjustments(db, acct, held)
            from app.corporate import audit_instruments
            issues = audit_instruments(db, fix=True)
            # A removed adjustment (the gap closed) must rebuild even when nothing new was booked.
            rebuild_trades(db, [acct.id])
            recon = self.reconcile(db, cl, acct, positions=held)
            sk = _skip_summary(skipped, detail)
            res.message = f"Trading 212: {len(recs)} fills read, {stats.inserted} new" + (f"; skipped {sk} (not trades)" if sk else "")
            if adjustments:
                res.message += f"; {len(adjustments)} reconciliation adjustment(s)"
            if split_notes:
                res.message += "; " + "; ".join(split_notes)
            if extra_note:
                res.message += "; " + extra_note
            if div_note:
                res.message += "; " + div_note
            if full:
                res.message += " (full history)"
            ctx.info(res.message)
            for reason, names in fields.items():
                ctx.info(f"Skipped {reason} fields: {', '.join(names)}")
            for line in recon:
                ctx.info("Trading 212 reconcile: " + line)
            _save_last(db, reset=True, ok=True, error=None, error_kind=None,
                       synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                       account_id=summary.get("id"), currency=acct.currency, fetched=len(recs), inserted=stats.inserted,
                       skipped=dict(skipped), skipped_detail=detail, skip_fields=fields,
                       reconcile=recon, reconcile_before=recon_before, adjustments=adjustments,
                       instrument_issues=[{"symbol": i["symbol"], "notes": i["notes"], "fixed": i["fixed"]}
                                          for i in issues if i["notes"] or i["fixed"]],
                       api_calls=cl.calls, full=full)
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

    def _import_dividends(self, db: Session, cl: T212Client, acct: Account, full: bool) -> str:
        """Paid-out cash dividends. Missing scope / 404 is a note, not a failed sync."""
        from app.dividends import ingest as ingest_divs
        cursor = None if full else get_state(db, DIV_CURSOR_STATE)
        threshold = (datetime.fromisoformat(cursor) - timedelta(days=OVERLAP_DAYS)) if cursor else None
        items: list[dict] = []
        newest: datetime | None = None
        try:
            for page in self._dividend_pages(cl, threshold):
                items.extend(page)
        except T212Error as exc:
            return "dividends unavailable" if exc.kind in ("scope", "api") else f"dividends: {exc.kind}"
        recs = []
        cache: dict[str, Instrument | None] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            ii = it.get("instrument") if isinstance(it.get("instrument"), dict) else {}
            ticker = it.get("ticker") or ii.get("ticker") or ""
            inst = cache.get(ticker)
            if inst is None and ticker:
                inst = db.scalar(select(Instrument).where(
                    Instrument.source_key == SOURCE_KEY, Instrument.broker_symbol == ticker))
                if inst is None:
                    inst = symbolmap.upsert_t212(db, ticker, ii.get("isin"), ii.get("name"),
                                                 ii.get("currency") or it.get("tickerCurrency"))
                cache[ticker] = inst
            rec = dividend_record(it, inst, None)
            if rec is None:
                continue
            recs.append(rec)
            newest = rec.paid_on if newest is None or rec.paid_on > newest else newest
        stats = ingest_divs(db, acct.id, SOURCE_KEY, recs)
        if newest:
            prev = parse_ts(cursor) if cursor else newest
            set_state(db, DIV_CURSOR_STATE, max(newest, prev).isoformat())
        if not recs and not stats["inserted"] and not stats["merged"]:
            return "dividends 0"
        return f"dividends {len(recs)} read, {stats['inserted']} new"

    def _dividend_pages(self, cl: T212Client, threshold: datetime | None):
        """Yield dividend pages from the current then the legacy path. Stop at the overlap window."""
        last_err: T212Error | None = None
        for path in ("/equity/history/dividends", "/api/v0/history/dividends"):
            try:
                newest_first: bool | None = None
                n = 0
                for page in cl.paged(path, limit=None):
                    n += 1
                    if newest_first is None:
                        times = [t for t in (_div_time(i) for i in page) if t is not None]
                        newest_first = times[0] > times[-1] if len(times) >= 2 and times[0] != times[-1] else True
                    yield page
                    if threshold and newest_first and page:
                        times = [t for t in (_div_time(i) for i in page) if t is not None]
                        if times and max(times) < threshold:
                            return
                if n:
                    return
            except T212Error as exc:
                last_err = exc
                if exc.status == 404 or exc.kind in ("scope", "api"):
                    continue
                raise
        if last_err:
            raise last_err

    def _extra_history(self, db: Session, cl: T212Client, full: bool) -> tuple[list[ExecRecord], list[ExecRecord], str]:
        """Transactions (full re-scan only) and at most one CSV-export step. Failures are notes, not errors.

        History endpoints are about 20 requests/minute and the export endpoints are stricter
        (POST 1/30s, GET 1/min), so this never loops and never runs on an incremental sync
        except to collect a report requested earlier.
        """
        notes = []
        api: list[ExecRecord] = []
        if full:
            try:
                n = 0
                for page in cl.paged("/equity/history/transactions"):
                    n += 1
                    for item in page:
                        rec = _transaction_record(item)
                        if isinstance(rec, ExecRecord):
                            api.append(rec)
                if n:
                    notes.append(f"transactions {n} page(s)")
            except T212Error as exc:
                notes.append("transactions unavailable" if exc.kind in ("scope", "api") else f"transactions: {exc.kind}")
        csv_recs, export_note = self._csv_export(db, cl, full)
        if export_note:
            notes.append(export_note)
        return api, csv_recs, "; ".join(notes)

    def _csv_export(self, db: Session, cl: T212Client, full: bool) -> tuple[list[ExecRecord], str]:
        """Request or collect one CSV report. A finished file is imported once."""
        from app.importers.t212_csv import parse as parse_csv
        pending = get_state(db, EXPORT_STATE) or ""
        done_id = pending[5:] if pending.startswith("done:") else ""
        report_id = pending if pending.isdigit() else ""
        try:
            if full and not report_id:
                created = cl.post("/equity/history/exports", {
                    "timeFrom": "2016-01-01T00:00:00Z",
                    "timeTo": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dataIncluded": {"includeDividends": True, "includeInterest": True,
                                     "includeOrders": True, "includeTransactions": True},
                })
                report_id = str((created or {}).get("reportId") or "")
                if report_id:
                    set_state(db, EXPORT_STATE, report_id)
            if not report_id:
                return [], ""
            reports = cl.get("/equity/history/exports")
        except T212Error as exc:
            return [], ("CSV export unavailable" if exc.kind in ("scope", "api") else f"CSV export: {exc.kind}")
        if not isinstance(reports, list):
            return [], "CSV export returned nothing"
        match = next((r for r in reports if str(r.get("reportId")) == report_id), None)
        if match is None:
            return [], "CSV export queued"
        status = (match.get("status") or "")
        if status != "Finished":
            return [], f"CSV export {status or 'queued'} (imported on the next sync)"
        if done_id == report_id:
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
        set_state(db, EXPORT_STATE, "done:" + report_id)
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

    def apply_adjustments(self, db: Session, acct: Account, positions: list[dict] | None) -> list[str]:
        """Book or refresh one estimated adjustment per symbol whose quantity still differs.

        Dated the first time the gap is seen. A row the user edited is left alone. A dismissed
        symbol is not recreated. When the gap closes, an unedited adjustment is removed.
        """
        import json as _json
        from collections import defaultdict

        from app.corporate import (
            adjustment_when, group_symbols, is_estimated, open_state, plan_adjustment,
            transfer_matching_basis, user_edited,
        )
        from app.models import Execution, Instrument
        from app.symbols import load_aliases

        if positions is None:
            return []
        try:
            state = _json.loads(get_state(db, RECON_STATE) or "{}")
        except ValueError:
            state = {}
        seen: dict = state.get("seen") or {}
        dismissed = set(state.get("dismissed") or [])
        execs = list(db.scalars(select(Execution).where(Execution.account_id == acct.id)))
        inst_ids = {e.instrument_id for e in execs if e.instrument_id}
        instruments = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(inst_ids or [-1])))}
        grouped = group_symbols(execs, instruments, load_aliases(db))
        groups: dict[str, list] = defaultdict(list)
        for e in execs:
            groups[(grouped.get(e.id) or _recon_key(e)).upper()].append(e)
        # Fills and the broker position for one instrument must share a key, including after a rename.
        inst_group = {}
        for e in execs:
            if e.instrument_id and e.id in grouped and not str(e.external_id or "").startswith("recon:"):
                inst_group[e.instrument_id] = grouped[e.id]
        broker: dict[str, tuple[float, Instrument | None, str]] = {}
        for p in positions:
            ii = p.get("instrument") or {}
            ticker = ii.get("ticker") or ""
            inst = None
            if ticker:
                inst = db.scalar(select(Instrument).where(Instrument.source_key == SOURCE_KEY,
                                                          Instrument.broker_symbol == ticker))
                if inst is None:
                    inst = symbolmap.upsert_t212(db, ticker, ii.get("isin"), ii.get("name"), ii.get("currency"))
                    if inst is not None:
                        instruments[inst.id] = inst
            parsed = markets.parse_t212_ticker(ticker, ii.get("isin")).symbol if ticker else None
            # The symbol the fills are grouped under (current instrument symbol, or the history ticker
            # when the user kept it). New fills are stored under the instrument symbol.
            sym = (inst_group.get(inst.id) if inst is not None else None) or (inst.symbol if inst else None) or parsed or "?"
            key = (sym or "?").upper()
            qty, prev, _prev_sym = broker.get(key, (0.0, inst, sym))
            broker[key] = (qty + float(p.get("quantity") or 0.0), inst or prev, sym)
        plans = []
        keys = set(groups) | set(broker)
        for key in sorted(keys):
            rows = groups.get(key) or []
            journal, avg = open_state(rows)
            theirs, inst, sym = broker.get(key, (0.0, None, (rows[0].symbol if rows else key)))
            plan = plan_adjustment(journal, theirs, avg)
            if plan is None:
                continue
            plan.update(key=key, symbol=sym, instrument_id=inst.id if inst else None,
                        journal=journal, broker=theirs,
                        currency=(inst.currency if inst is not None and inst.currency else None))
            plans.append(plan)
        transfer_matching_basis(plans)
        lines = []
        now = utcnow().replace(microsecond=0)
        wanted = {p["key"] for p in plans}
        for p in plans:
            if p["key"] in dismissed:
                lines.append(f"{p['symbol']}: gap left (adjustment removed by you)")
                continue
            seen.setdefault(p["key"], now.isoformat())
            when = adjustment_when(groups.get(p["key"], []), p["side"], p["kind"], now)
            if p["instrument_id"] is None:
                for src_row in groups.get(p["key"], []):
                    if src_row.instrument_id and src_row.instrument_id in instruments:
                        p["instrument_id"] = src_row.instrument_id
                        inst = instruments[src_row.instrument_id]
                        if not p.get("currency") and inst.currency:
                            p["currency"] = inst.currency
                        break
            ext = "recon:" + p["key"]
            row = next((e for e in groups.get(p["key"], []) if e.external_id == ext and e.source == SOURCE_KEY), None)
            raw = {}
            if row is not None and row.raw:
                try:
                    raw = _json.loads(row.raw)
                except ValueError:
                    raw = {}
            if row is not None and user_edited(raw):
                lines.append(f"{p['symbol']}: estimated adjustment kept (you edited it)")
                continue
            payload = {"estimated": True, "note": p["note"], "journal_qty": p["journal"], "t212_qty": p["broker"],
                       "known_on": seen[p["key"]], "applied_at": when.isoformat()}
            if row is None:
                row = Execution(
                    account_id=acct.id, source=SOURCE_KEY, external_id=ext, symbol=p["symbol"],
                    underlying=p["symbol"], asset_type="STOCK", side=p["side"], quantity=p["quantity"],
                    price=p["price"], fees=0.0, executed_at=when, time_known=False,
                    currency=p.get("currency") or acct.currency,
                    instrument_id=p["instrument_id"],
                    position_effect="RATIO" if p["ratio"] else ("OPEN" if p["side"] == "BUY" else "CLOSE"),
                    kind=p["kind"], description=p["note"][:300], multiplier=1.0,
                )
                db.add(row)
            else:
                row.symbol = row.underlying = p["symbol"]
                row.side, row.quantity, row.price = p["side"], p["quantity"], p["price"]
                row.kind = p["kind"]
                row.position_effect = "RATIO" if p["ratio"] else ("OPEN" if p["side"] == "BUY" else "CLOSE")
                row.instrument_id = p["instrument_id"] or row.instrument_id
                if p.get("currency"):
                    row.currency = p["currency"]
                row.description = p["note"][:300]
                row.executed_at = when
            row.raw = _json.dumps(payload)
            row.match_key = f"{when.date().isoformat()}|{p['symbol']}|{p['side']}|{p['quantity']:.4f}|{p['price']:.4f}|{p['kind']}"
            lines.append(f"{p['symbol']}: {p['note']}")
        for key, rows in groups.items():
            if key in wanted:
                continue
            for e in rows:
                if e.source == SOURCE_KEY and str(e.external_id).startswith("recon:"):
                    raw = {}
                    try:
                        raw = _json.loads(e.raw or "{}")
                    except ValueError:
                        raw = {}
                    if is_estimated(raw) and not user_edited(raw):
                        db.delete(e)
        set_state(db, RECON_STATE, _json.dumps({"seen": seen, "dismissed": sorted(dismissed)}))
        db.flush()
        return lines

    def reconcile(self, db: Session, cl: T212Client, acct: Account, positions: list[dict] | None = None) -> list[str]:
        """Compare each T212 open position with journal open trades.

        A renamed listing (history ticker DMYI, journal symbol IONQ) is matched on instrument id so it
        is not reported as a missing buy. Where Trading 212 still holds more shares, the line says so
        per ticker. Pass ``positions`` to avoid a second Portfolio request in the same sync.
        """
        if positions is None:
            positions = self._positions(cl)
        if positions is None:
            return []
        pos = positions
        opens = list(db.scalars(select(Trade).where(Trade.account_id == acct.id, Trade.status == "OPEN")))
        by_inst: dict[int, list[Trade]] = {}
        for t in opens:
            if t.instrument_id:
                by_inst.setdefault(t.instrument_id, []).append(t)
        claimed: set[int] = set()
        out = []
        for p in pos or []:
            ii = p.get("instrument") or {}
            ticker = ii.get("ticker") or ""
            inst = db.scalar(select(Instrument).where(Instrument.source_key == SOURCE_KEY,
                                                      Instrument.broker_symbol == ticker)) if ticker else None
            parsed = markets.parse_t212_ticker(ticker, ii.get("isin")).symbol if ticker else "?"
            sym = (inst.symbol if inst else None) or parsed or "?"
            theirs = float(p.get("quantity") or 0.0)
            rows: list[Trade] = []
            if inst is not None and inst.id in by_inst:
                rows = by_inst[inst.id]
            else:
                rows = [t for t in opens if t.symbol in {sym, parsed} and t.id not in claimed]
            journal = sum((t.open_quantity or 0.0) for t in rows)
            claimed.update(t.id for t in rows)
            out.extend(_qty_diff(sym, theirs, journal))
        left: dict[str, float] = {}
        for t in opens:
            if t.id in claimed or abs(t.open_quantity or 0.0) <= 1e-4:
                continue
            left[t.symbol] = left.get(t.symbol, 0.0) + (t.open_quantity or 0.0)
        for sym in sorted(left):
            out.extend(_qty_diff(sym, 0.0, left[sym]))
        return out
