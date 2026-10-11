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
Splits / spin-offs / acquisitions are counted and skipped (they are not trades). Credentials live Fernet-encrypted in
app_state (cred:t212; never in backups)."""
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
OVERLAP_DAYS = 3
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
        url = path if path.startswith("http") else (self.base + path if path.startswith("/equity") else
                                                    self.base.rsplit("/api/v0", 1)[0] + path)
        last: Exception | None = None
        for attempt in range(4):
            try:
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
        path, params = "/equity/history/orders", {"limit": PAGE_LIMIT}
        while path:
            data = self.get(path, params)
            yield data.get("items") or []
            path, params = data.get("nextPagePath"), None


# ------------------------------------------------------------------ pure record mapping
def parse_ts(s: str) -> datetime:
    """ISO 8601 (Z or offset) -> naive UTC."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


@dataclass
class Skip:
    reason: str


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


def to_record(item: dict, inst: Instrument | None = None, symbol: str | None = None) -> ExecRecord | Skip:
    """One history item -> ExecRecord (pure; ``inst`` supplies symbol/exchange/currency, else they are derived)."""
    fill, order = item.get("fill") or {}, item.get("order") or {}
    if not fill:
        return Skip("no fill")
    ftype = (fill.get("type") or "TRADE").upper()
    if ftype != "TRADE":
        return Skip(ftype.lower().replace("_", " "))       # split, spin-off, acquisition ... not a trade
    qty = abs(float(fill.get("quantity") or 0.0))
    price = float(fill.get("price") or 0.0)
    side = (order.get("side") or "").upper()
    if qty <= 0 or side not in ("BUY", "SELL") or not fill.get("filledAt") or fill.get("id") is None:
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
    when = parse_ts(fill["filledAt"])
    tz = markets.exchange_tz(exch)
    trade_date: date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date()
    return ExecRecord(
        external_id=str(fill["id"]), symbol=sym, underlying=sym, asset_type="STOCK", side=side,
        quantity=round(qty, 8), price=price, executed_at=when, fees=round(fees, 6), time_known=True,
        position_effect=None, kind="TRADE", multiplier=1.0, description=ii.get("name"),
        raw={"order_id": order.get("id"), "ticker": ticker, "isin": ii.get("isin"), "ccy": ccy,
             "wallet": {k: wallet.get(k) for k in ("currency", "fxRate", "netValue", "realisedProfitLoss")},
             "taxes": [{"name": t.get("name"), "q": t.get("quantity"), "ccy": t.get("currency")}
                       for t in wallet.get("taxes") or []],
             "extended_hours": order.get("extendedHours"), "initiated_from": order.get("initiatedFrom")},
        trade_date=trade_date, currency=ccy, fx_to_account=fx, instrument_id=inst.id if inst is not None else None)


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

    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=None):
        self._transport, self._sleep = transport, sleep or _time.sleep

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
            cursor = get_state(db, CURSOR_STATE)
            threshold = (datetime.fromisoformat(cursor) - timedelta(days=OVERLAP_DAYS)) if cursor else None
            items, newest_first = [], True
            for n, page in enumerate(cl.history_pages()):
                if n == 0 and len(page) > 1:
                    newest_first = parse_ts(page[0]["fill"]["filledAt"]) >= parse_ts(page[-1]["fill"]["filledAt"]) \
                        if page[0].get("fill") and page[-1].get("fill") else True
                items.extend(page)
                if threshold and newest_first and page and all(
                        (i.get("fill") or {}).get("filledAt") and parse_ts(i["fill"]["filledAt"]) < threshold for i in page):
                    break
            recs, skipped, newest = self.convert(db, cl, items)
            res.fetched = len(recs)
            stats = ingest_records(db, acct.id, SOURCE_KEY, recs)
            res.inserted, res.merged = stats.inserted, stats.merged
            if newest:
                set_state(db, CURSOR_STATE, max(newest, parse_ts(cursor) if cursor else newest).isoformat())
            rebuild_trades(db, [acct.id])
            recon = self.reconcile(db, cl, acct)
            sk = ", ".join(f"{n} {k}" for k, n in sorted(skipped.items()))
            res.message = f"Trading 212: {len(recs)} fills read, {stats.inserted} new" + (f"; skipped {sk} (not trades)" if sk else "")
            ctx.info(res.message)
            for line in recon:
                ctx.info("Trading 212 reconcile: " + line)
            _save_last(db, reset=True, ok=True, error=None, error_kind=None,
                       synced_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                       account_id=summary.get("id"), currency=acct.currency, fetched=len(recs), inserted=stats.inserted,
                       skipped=dict(skipped), reconcile=recon, api_calls=cl.calls)
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

    def convert(self, db: Session, cl: T212Client, items: list[dict]) -> tuple[list[ExecRecord], Counter, datetime | None]:
        skipped: Counter = Counter()
        recs: list[ExecRecord] = []
        newest: datetime | None = None
        cache: dict[str, Instrument] = {}
        meta: dict[str, dict] | None = None
        for it in items:
            order, fill = it.get("order") or {}, it.get("fill") or {}
            if not fill:
                skipped["unfilled"] += 1
                continue
            if (fill.get("type") or "TRADE").upper() != "TRADE":
                skipped[(fill.get("type") or "").lower().replace("_", " ")] += 1
                continue
            ii = order.get("instrument") or {}
            ticker = order.get("ticker") or ii.get("ticker")
            if not ticker:
                skipped["no ticker"] += 1
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
            r = to_record(it, inst)
            if isinstance(r, Skip):
                skipped[r.reason] += 1
                continue
            recs.append(r)
            newest = r.executed_at if newest is None or r.executed_at > newest else newest
        recs.sort(key=lambda r: (r.executed_at, r.external_id))
        return recs, skipped, newest

    def reconcile(self, db: Session, cl: T212Client, acct: Account) -> list[str]:
        """Compare T212's open positions with the journal's open trades; returns human-readable differences."""
        try:
            self._sleep(1.0)          # positions: 1 request / second
            pos = cl.positions()
        except T212Error:
            return []
        mine: dict[str, float] = {}
        for t in db.scalars(select(Trade).where(Trade.account_id == acct.id, Trade.status == "OPEN")):
            mine[t.symbol] = mine.get(t.symbol, 0.0) + (t.open_quantity or 0.0)
        theirs: dict[str, float] = {}
        for p in pos:
            ii = p.get("instrument") or {}
            inst = db.scalar(select(Instrument).where(Instrument.source_key == SOURCE_KEY,
                                                      Instrument.broker_symbol == ii.get("ticker")))
            sym = inst.symbol if inst else markets.parse_t212_ticker(ii.get("ticker") or "").symbol
            theirs[sym] = theirs.get(sym, 0.0) + float(p.get("quantity") or 0.0)
        out = []
        for sym in sorted(set(mine) | set(theirs)):
            a, b = theirs.get(sym, 0.0), mine.get(sym, 0.0)
            if abs(a - b) > 1e-4:
                out.append(f"{sym}: Trading 212 holds {a:g}, journal {b:g} (split, transfer or skipped corporate action?)")
        return out
