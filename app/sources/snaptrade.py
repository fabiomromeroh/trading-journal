"""SnapTrade data source (Schwab via SnapTrade Personal API keys).

Enabled when SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY are set. With Personal keys the key
itself identifies the SnapTrade user, so no userId/userSecret is registered or sent.

Requests are signed per https://docs.snaptrade.com/docs/request-signatures:
  query gets clientId + timestamp; header Signature = base64(HMAC-SHA256(consumerKey,
  canonical JSON {"content": body|null, "path": "/api/v1/...", "query": "<exact query>"})).

Endpoints used (base https://api.snaptrade.com/api/v1):
  GET  /authorizations                       connections (disabled flag -> re-login needed)
  GET  /accounts                             accounts + sync_status
  GET  /accounts/{id}/activities             transaction history (paginated, 1000/page)
  POST /snapTrade/login                      Connection Portal URL (valid 5 minutes);
                                             body.reconnect=<authorization id> repairs a
                                             disabled connection instead of creating a new one.

Data notes (verified against a live Schwab International account):
  * activities are date-only (trade_date at 00:00Z) and SnapTrade refreshes them once a day,
    one day behind, so today's trades show up tomorrow;
  * Schwab's own id is `external_reference_id` (our dedupe key); SnapTrade's `id` is kept in raw
    because it can change if SnapTrade deletes and re-adds a transaction;
  * Schwab logins expire after 7 days -> the connection becomes `disabled` until reconnected.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time as _time
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol, parse_occ
from app.models import Account, SourceState, utcnow
from app.services import get_state, ingest_records, set_state
from app.sources.base import DataSource, SourceResult, SourceStatus, SyncContext
from app.timeutil import ET, local_to_utc_naive

log = logging.getLogger(__name__)
# httpx logs full request URLs at INFO; keep the SnapTrade clientId out of the server logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
API = "https://api.snaptrade.com/api/v1"
SOURCE_KEY = "snaptrade"
STATUS_STATE = "snaptrade:status"
CONNECTED_AT_STATE = "snaptrade:connected_at:"  # + authorization id
RELOGIN_DAYS = 7
STATUS_MAX_AGE = timedelta(hours=6)
PAGE = 1000

TRADE_TYPES = {"BUY", "SELL"}
CLOSING_TYPES = {"OPTIONEXPIRATION": "EXPIRATION", "OPTIONASSIGNMENT": "ASSIGNMENT",
                 "OPTIONEXERCISE": "EXERCISE"}
OPTION_EFFECT = {"BUY_TO_OPEN": "OPEN", "SELL_TO_OPEN": "OPEN",
                 "BUY_TO_CLOSE": "CLOSE", "SELL_TO_CLOSE": "CLOSE"}


class SnapTradeError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------ client
def sign(consumer_key: str, path: str, query: str, body: dict | None) -> str:
    payload = json.dumps({"content": body if body else None, "path": "/api/v1" + path, "query": query},
                         separators=(",", ":"), sort_keys=True)
    digest = hmac.new(consumer_key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class SnapTradeClient:
    def __init__(self, client_id: str | None = None, consumer_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30):
        s = get_settings()
        self.client_id = client_id or s.snaptrade_client_id
        self.consumer_key = consumer_key or s.snaptrade_consumer_key
        self.http = httpx.Client(timeout=timeout, transport=transport)

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        for attempt in range(4):
            q = {"clientId": self.client_id, "timestamp": int(_time.time())}
            q.update({k: v for k, v in (params or {}).items() if v is not None})
            query = urlencode(q)
            headers = {"Accept": "application/json",
                       "Signature": sign(self.consumer_key, path, query, body)}
            content = None
            if body:
                content = json.dumps(body, separators=(",", ":"))
                headers["Content-Type"] = "application/json"
            resp = self.http.request(method, f"{API}{path}?{query}", headers=headers, content=content)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry = resp.headers.get("Retry-After")
                _time.sleep(min(20.0, float(retry) if retry and retry.isdigit() else 2 ** attempt))
                continue
            break
        if resp.status_code >= 400:
            try:
                detail = resp.json()
                detail = detail.get("detail") or detail.get("message") or detail
            except ValueError:
                detail = resp.text[:200]
            raise SnapTradeError(f"SnapTrade {method} {path} failed ({resp.status_code}): {str(detail)[:200]}")
        return resp.json()

    def authorizations(self) -> list[dict]:
        return self.request("GET", "/authorizations")

    def accounts(self) -> list[dict]:
        return self.request("GET", "/accounts")

    def activities(self, account_id: str, start: date | None = None, end: date | None = None) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = self.request("GET", f"/accounts/{account_id}/activities", {
                "startDate": start.isoformat() if start else None,
                "endDate": end.isoformat() if end else None, "offset": offset, "limit": PAGE})
            page = data.get("data", []) if isinstance(data, dict) else (data or [])
            out.extend(page)
            total = (data.get("pagination") or {}).get("total") if isinstance(data, dict) else None
            offset += len(page)
            if len(page) < PAGE or (total is not None and offset >= total):
                return out

    def balances(self, account_id: str) -> list[dict]:
        return self.request("GET", f"/accounts/{account_id}/balances") or []

    def positions(self, account_id: str) -> list[dict]:
        data = self.request("GET", f"/accounts/{account_id}/positions/all") or {}
        return data.get("results", []) if isinstance(data, dict) else data

    def login_url(self, *, reconnect: str | None = None, broker: str | None = "SCHWAB",
                  redirect: str | None = None) -> str:
        body: dict = {"connectionType": "read", "darkMode": True}
        if reconnect:
            body["reconnect"] = reconnect
        elif broker:
            body["broker"] = broker
        if redirect:
            body["customRedirect"] = redirect
            body["immediateRedirect"] = True
        data = self.request("POST", "/snapTrade/login", body=body)
        url = data.get("redirectURI") if isinstance(data, dict) else None
        if not url:
            raise SnapTradeError("SnapTrade did not return a Connection Portal link")
        return url


# ------------------------------------------------------------------------------------ mapping
def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _trade_date(a: dict) -> date | None:
    s = (a.get("trade_date") or a.get("settlement_date") or "")[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _ticker(a: dict) -> str | None:
    sym = a.get("symbol")
    if isinstance(sym, dict):
        return (sym.get("symbol") or sym.get("raw_symbol") or "").strip().upper() or None
    return (sym or "").strip().upper() or None


def _option(a: dict):
    """-> (underlying, expiration, CALL|PUT, strike, multiplier) or None."""
    o = a.get("option_symbol")
    if not isinstance(o, dict):
        return None
    occ = parse_occ(o.get("ticker") or "")
    und = o.get("underlying_symbol")
    und = (und.get("symbol") if isinstance(und, dict) else und) or (occ[0] if occ else None)
    try:
        exp = date.fromisoformat(str(o.get("expiration_date"))[:10])
    except ValueError:
        exp = occ[1] if occ else None
    pc = (o.get("option_type") or (occ[2] if occ else "")).upper()
    strike = o.get("strike_price") or (occ[3] if occ else None)
    if not (und and exp and pc and strike):
        return None
    mult = 10.0 if o.get("is_mini_option") else OPTION_MULTIPLIER
    return und.upper(), exp, ("CALL" if pc.startswith("C") else "PUT"), float(strike), mult


def _slim(a: dict) -> dict:
    o = a.get("option_symbol") if isinstance(a.get("option_symbol"), dict) else {}
    return {"snaptrade_id": a.get("id"), "external_reference_id": a.get("external_reference_id"),
            "type": a.get("type"), "option_type": a.get("option_type"), "description": a.get("description"),
            "symbol": _ticker(a), "option": o.get("ticker"), "units": a.get("units"), "price": a.get("price"),
            "amount": a.get("amount"), "fee": a.get("fee"), "trade_date": a.get("trade_date"),
            "settlement_date": a.get("settlement_date")}


def parse_activities(activities: list[dict]) -> tuple[list[ExecRecord], Counter]:
    """SnapTrade UniversalActivity list -> ExecRecords (fills + option position events).

    Returns (records, ignored-type counter). Transfers, dividends, cash movements etc. are ignored.
    """
    ignored: Counter = Counter()
    fee_by_ref: dict[str, float] = defaultdict(float)
    keep: list[tuple[int, dict]] = []
    for i, a in enumerate(activities):
        t = (a.get("type") or "").upper()
        if t in TRADE_TYPES or t in CLOSING_TYPES:
            keep.append((i, a))
        elif t == "FEE" and a.get("external_reference_id"):
            fee_by_ref[a["external_reference_id"]] += abs(_f(a.get("amount")))
        else:
            ignored[t or "UNKNOWN"] += 1

    # Stable external ids: Schwab's reference id; legs sharing one get a deterministic suffix.
    groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, a in keep:
        groups[a.get("external_reference_id") or f"st:{a.get('id')}"].append((i, a))
    ext_ids: dict[int, str] = {}
    for ref, legs in groups.items():
        if len(legs) == 1:
            ext_ids[legs[0][0]] = ref
            continue
        legs.sort(key=lambda p: (_ticker(p[1]) or "", str((p[1].get("option_symbol") or {}).get("ticker")
                                 if isinstance(p[1].get("option_symbol"), dict) else ""),
                                 p[1].get("type") or "", _f(p[1].get("units")), _f(p[1].get("price"))))
        for n, (i, _) in enumerate(legs):
            ext_ids[i] = f"{ref}#{n}"

    records: list[ExecRecord] = []
    for seq, (i, a) in enumerate(sorted(keep, key=lambda p: (_trade_date(p[1]) or date.min, p[0]))):
        t = (a.get("type") or "").upper()
        d = _trade_date(a)
        units = _f(a.get("units"))
        qty = abs(units)
        if d is None or qty == 0:
            ignored[f"{t} (no date/units)"] += 1
            continue
        fees = abs(_f(a.get("fee")))
        ref = a.get("external_reference_id")
        if ref and fee_by_ref.get(ref) and len(groups.get(ref, [])) >= 1:
            fees += fee_by_ref[ref] / len(groups[ref])
        desc = (a.get("description") or "")[:300]
        ts = local_to_utc_naive(d, time(16, 0), ET)  # date-only, same convention as the CSV path
        opt = _option(a)
        kind = CLOSING_TYPES.get(t, "TRADE")
        if opt:
            und, exp, pc, strike, mult = opt
            effect = OPTION_EFFECT.get((a.get("option_type") or "").upper())
            if kind != "TRADE":
                side, price, effect = None, 0.0, "CLOSE"
            else:
                side, price = t, _f(a.get("price"))
            rec = ExecRecord(
                external_id=ext_ids[i], symbol=option_symbol(und, exp, pc, strike), underlying=und,
                asset_type="OPTION", option_type=pc, strike=strike, expiration=exp, multiplier=mult,
                side=side, quantity=qty, price=price, fees=fees, executed_at=ts, time_known=False,
                position_effect=effect, kind=kind, description=desc, trade_date=d, raw=_slim(a))
        else:
            sym = _ticker(a)
            if not sym:
                ignored[f"{t} (no symbol)"] += 1
                continue
            if kind != "TRADE":
                # Stock leg of an assignment/exercise: a normal fill; side from the share movement.
                kind = "TRADE"
                side = "BUY" if units > 0 else "SELL"
            else:
                side = t
            price = _f(a.get("price"))
            if not price and _f(a.get("amount")):
                price = abs(_f(a.get("amount"))) / qty
            d_up = desc.upper()
            if side == "SELL":
                effect = "OPEN" if "SHORT" in d_up else "CLOSE"  # mirrors the Schwab CSV mapping
            else:
                effect = "CLOSE" if "COVER" in d_up else None
            rec = ExecRecord(
                external_id=ext_ids[i], symbol=sym, underlying=sym, asset_type="STOCK", side=side,
                quantity=qty, price=price, fees=fees, executed_at=ts, time_known=False,
                position_effect=effect, kind=kind, description=desc, trade_date=d, raw=_slim(a))
        rec.seq = seq
        records.append(rec)
    return records, ignored


# ------------------------------------------------------------------------------------ status cache
def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _masked(number: str | None) -> str | None:
    digits = re.findall(r"\d+", number or "")
    return f"...{digits[-1][-3:]}" if digits else None


def load_status(db: Session) -> dict | None:
    raw = get_state(db, STATUS_STATE)
    return json.loads(raw) if raw else None


def refresh_status(db: Session, client: SnapTradeClient | None = None, *, commit: bool = True) -> dict:
    """Fetch connections + accounts from SnapTrade and cache them (no secrets stored)."""
    prev = load_status(db) or {}
    prev_conn = {c["id"]: c for c in prev.get("connections", [])}
    now = utcnow()
    snap: dict = {"checked_at": now.isoformat(), "error": None, "connections": [], "accounts": []}
    try:
        client = client or SnapTradeClient(timeout=10)
        auths = client.authorizations()
        accounts = client.accounts()
    except Exception as exc:  # network / auth problems are shown in Settings, never raised to pages
        snap.update(error=str(exc)[:300], connections=prev.get("connections", []),
                    accounts=prev.get("accounts", []))
        set_state(db, STATUS_STATE, json.dumps(snap))
        if commit:
            db.commit()
        return snap
    for a in auths:
        aid = a.get("id")
        disabled = bool(a.get("disabled"))
        updated = _parse_dt(a.get("updated_date")) or _parse_dt(a.get("created_date")) or now
        key = CONNECTED_AT_STATE + str(aid)
        connected_at = get_state(db, key)
        was_disabled = prev_conn.get(aid, {}).get("disabled")
        if not connected_at or (was_disabled and not disabled):
            connected_at = (now if was_disabled and not disabled else updated).isoformat()
            set_state(db, key, connected_at)
        broker = a.get("brokerage") or {}
        snap["connections"].append({
            "id": aid, "name": a.get("name"), "broker": broker.get("display_name") or broker.get("name"),
            "broker_slug": broker.get("slug"), "type": a.get("type"), "disabled": disabled,
            "disabled_date": a.get("disabled_date"), "created": a.get("created_date"),
            "connected_at": connected_at})
    for acc in accounts:
        tx = ((acc.get("sync_status") or {}).get("transactions") or {})
        snap["accounts"].append({
            "id": acc.get("id"), "connection": acc.get("brokerage_authorization"),
            "name": acc.get("name"), "institution": acc.get("institution_name"),
            "masked": _masked(acc.get("number") or acc.get("name")),
            "tx_synced_through": tx.get("last_successful_sync"),
            "first_tx": tx.get("first_transaction_date"),
            "initial_sync_completed": tx.get("initial_sync_completed")})
    set_state(db, STATUS_STATE, json.dumps(snap))
    if commit:
        db.commit()
    return snap


# --------------------------------------------------------------- portfolio anchor (value vs deposits)
CASHFLOW_STATE = "snaptrade:cashflows:"     # per account: {activity id: {...}} external money in/out
PORTFOLIO_STATE = "snaptrade:portfolio:"    # per account: latest balances + positions snapshot
CASHFLOW_TYPES = {"TRANSFER", "CONTRIBUTION", "DEPOSIT", "WITHDRAWAL", "EXTERNAL_ASSET_TRANSFER_IN",
                  "EXTERNAL_ASSET_TRANSFER_OUT", "JOURNAL"}


def _record_cashflows(db: Session, account_id: int, acts: list[dict]) -> None:
    """Remember deposits / withdrawals / transfers (ignored as fills) so net deposits can be shown.
    A transfer that moves securities (units != 0) is valued at units x price and flagged."""
    key = CASHFLOW_STATE + str(account_id)
    cur = json.loads(get_state(db, key) or "{}")
    for a in acts:
        typ = (a.get("type") or "").upper()
        if typ not in CASHFLOW_TYPES:
            continue
        units = float(a.get("units") or 0)
        sym = (a.get("symbol") or {}).get("symbol") if isinstance(a.get("symbol"), dict) else None
        amount = float(a.get("amount") or 0)
        securities = abs(units) > 1e-9 and not amount
        if securities:
            amount = units * float(a.get("price") or 0)
        cur[a.get("id") or f"{a.get('trade_date')}:{amount}"] = {
            "type": typ, "date": (a.get("trade_date") or "")[:10], "amount": round(amount, 2),
            "description": a.get("description"), "symbol": sym, "units": units, "securities": securities}
    set_state(db, key, json.dumps(cur))


def _snapshot_portfolio(db: Session, client: "SnapTradeClient", account_id: int, sa: dict, ctx=None) -> None:
    try:
        bals = client.balances(sa["id"])
        poss = client.positions(sa["id"])
    except Exception as exc:  # positions/balances are informational; never fail the sync
        if ctx is not None:
            ctx.info(f"Could not read balances/positions from SnapTrade: {str(exc)[:120]}")
        return
    cash = sum(float(b.get("cash") or 0) for b in bals if ((b.get("currency") or {}).get("code") or "USD") == "USD")
    positions = []
    for p in poss:
        inst = p.get("instrument") or {}
        positions.append({"symbol": inst.get("symbol") or inst.get("raw_symbol"), "kind": inst.get("kind"),
                          "units": float(p.get("units") or 0), "price": float(p.get("price") or 0),
                          "cost_basis": float(p.get("cost_basis") or 0) if p.get("cost_basis") else None})
    mv = sum(x["units"] * x["price"] * (100 if x["kind"] == "option" else 1) for x in positions)
    set_state(db, PORTFOLIO_STATE + str(account_id), json.dumps({
        "as_of": utcnow().isoformat(), "cash": round(cash, 2), "market_value": round(mv, 2),
        "value": round(cash + mv, 2), "positions": positions}))


def portfolio_summary(db: Session, account_ids: list[int]) -> dict | None:
    """Account value vs net deposits from the latest SnapTrade snapshot (None if unavailable)."""
    out = {"value": 0.0, "cash": 0.0, "net_deposits": 0.0, "positions": {}, "as_of": None,
           "securities_transfers": []}
    found = False
    for aid in account_ids:
        snap = get_state(db, PORTFOLIO_STATE + str(aid))
        flows = get_state(db, CASHFLOW_STATE + str(aid))
        if not snap or flows is None:
            continue
        found = True
        snap = json.loads(snap)
        out["value"] += snap["value"]
        out["cash"] += snap["cash"]
        out["as_of"] = max(filter(None, [out["as_of"], snap["as_of"]]))
        for p in snap["positions"]:
            out["positions"][p["symbol"]] = p
        for f in json.loads(flows).values():
            out["net_deposits"] += f["amount"]
            if f.get("securities"):
                out["securities_transfers"].append(f)
    if not found:
        return None
    out["total_pnl"] = round(out["value"] - out["net_deposits"], 2)
    return out


def mark_reconnected(db: Session, authorization_id: str | None) -> None:
    """Called when the user returns from the Connection Portal."""
    if authorization_id:
        set_state(db, CONNECTED_AT_STATE + authorization_id, utcnow().isoformat())
    db.commit()


def relogin_due(conn: dict) -> datetime | None:
    ca = _parse_dt(conn.get("connected_at"))
    return ca + timedelta(days=RELOGIN_DAYS) if ca else None


# ------------------------------------------------------------------------------------ source
class SnapTradeSource(DataSource):
    key = SOURCE_KEY
    name = "Schwab via SnapTrade"
    description = ("Pulls your Schwab transaction history through SnapTrade (read-only). "
                   "SnapTrade updates once a day, so today's trades appear tomorrow.")

    def __init__(self, client: SnapTradeClient | None = None):
        self._client = client

    def client(self) -> SnapTradeClient:
        return self._client or SnapTradeClient()

    def is_configured(self) -> bool:
        return get_settings().snaptrade_configured

    def refresh(self, db: Session) -> None:
        if self.is_configured():
            refresh_status(db, self._client)

    def maybe_refresh(self, db: Session) -> None:
        """Refresh the cached connection status when it's older than STATUS_MAX_AGE."""
        if not self.is_configured():
            return
        snap = load_status(db)
        checked = _parse_dt(snap.get("checked_at")) if snap else None
        if checked is None or utcnow() - checked > STATUS_MAX_AGE:
            refresh_status(db, self._client)

    def status(self, db: Session) -> SourceStatus:
        if not self.is_configured():
            return SourceStatus(False, False, "Not configured (set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY).")
        snap = load_status(db)
        if not snap:
            return SourceStatus(True, True, "Connection status not checked yet.")
        conns = [c for c in snap.get("connections", [])]
        if not conns:
            msg = "No brokerage is connected in SnapTrade yet. Click Connect Schwab."
            if snap.get("error"):
                msg = f"Couldn't reach SnapTrade: {snap['error']}"
            return SourceStatus(True, False, msg, "warning")
        disabled = [c for c in conns if c.get("disabled")]
        if disabled:
            c = disabled[0]
            return SourceStatus(True, False, f"{c.get('broker') or 'Schwab'} login expired; SnapTrade can't fetch "
                                "new data until you reconnect.", "error", 0,
                                banner={"level": "error", "text": "Schwab connection needs a new login "
                                        "(Schwab logins last 7 days). Sync can't fetch new trades until you reconnect.",
                                        "link": f"/settings/snaptrade/reconnect?id={c['id']}",
                                        "link_text": "Reconnect Schwab"})
        due = min((d for d in (relogin_due(c) for c in conns) if d), default=None)
        left = (due - utcnow()).total_seconds() if due else None
        msg = "Connected"
        if snap.get("error"):
            msg = f"Connected (last status check failed: {snap['error']})"
        banner = None
        level = "info"
        if left is not None and left < 86400:
            level = "warning"
            banner = {"level": "warning", "text": "Schwab login (via SnapTrade) is due for renewal "
                      + ("now" if left <= 0 else f"within {int(left // 3600)}h") + " (estimate; Schwab logins last 7 days).",
                      "link": f"/settings/snaptrade/reconnect?id={conns[0]['id']}", "link_text": "Reconnect Schwab"}
        return SourceStatus(True, True, msg, level, left, banner=banner)

    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        s = get_settings()
        client = self.client()
        res = SourceResult()
        snap = load_status(db) or refresh_status(db, client)
        now = utcnow()
        accounts = snap.get("accounts", [])
        if not accounts:
            raise SnapTradeError("No accounts are connected in SnapTrade.")
        for sa in accounts:
            acct = self._account(db, sa)
            st = db.scalar(select(SourceState).where(SourceState.source == self.key,
                                                     SourceState.account_id == acct.id))
            if st is None:
                st = SourceState(source=self.key, account_id=acct.id)
                db.add(st)
            start = None
            have_flows = get_state(db, CASHFLOW_STATE + str(acct.id)) is not None
            if st.synced_through and have_flows:  # (first run after an upgrade re-reads all history once)
                start = (st.synced_through - timedelta(days=s.sync_overlap_days)).date()
            acts = client.activities(sa["id"], start=start)
            _record_cashflows(db, acct.id, acts)
            _snapshot_portfolio(db, client, acct.id, sa, ctx)
            records, ignored = parse_activities(acts)
            stats = ingest_records(db, acct.id, self.key, records)
            res.fetched += len(acts)
            res.inserted += stats.inserted
            res.merged += stats.merged
            res.accounts.append(acct.id)
            through = sa.get("tx_synced_through")
            try:
                through_dt = datetime.fromisoformat(through[:10]) if through else None
            except ValueError:
                through_dt = None
            dates = [r.trade_date for r in records if r.trade_date]
            if through_dt is None and dates:
                through_dt = datetime.combine(max(dates), time())
            if through_dt is not None:
                st.synced_through = through_dt
            st.last_success_at = now
            if start is None and dates:
                st.earliest_reached = datetime.combine(min(dates), time())
            scope = "full history" if start is None else f"since {start:%b %d}"
            extra = f"; ignored {sum(ignored.values())} non-trade rows" if ignored else ""
            ctx.info(f"{acct.name}: {len(records)} fills fetched ({scope}), {stats.inserted} new, "
                     f"{stats.merged} merged with imports{extra}. "
                     f"SnapTrade data through {through or '?'}.")
            db.commit()
        return res

    def _account(self, db: Session, sa: dict) -> Account:
        ref = f"snaptrade:{sa['id']}"
        acct = db.scalar(select(Account).where(Account.external_ref == ref))
        masked = sa.get("masked")
        if acct is None and masked:
            acct = db.scalar(select(Account).where(Account.account_number_masked == masked,
                                                   Account.is_demo.is_(False)).order_by(Account.id))
        if acct is None:
            label = sa.get("name") or masked or "account"
            acct = Account(name=f"Schwab {label}"[:120], broker="schwab", account_number_masked=masked)
            db.add(acct)
            db.flush()
        if not acct.external_ref:
            acct.external_ref = ref
        return acct


def full_resync(db: Session) -> None:
    """Forget the incremental cursor so the next sync pulls the full history again."""
    for st in db.scalars(select(SourceState).where(SourceState.source == SOURCE_KEY)):
        st.synced_through = None
    db.commit()
