"""Schwab Trader API data source (optional).

Disabled unless SCHWAB_APP_KEY, SCHWAB_APP_SECRET and SCHWAB_CALLBACK_URL are set.
Note: Schwab only issues Trader API apps to US-domiciled retail accounts; Schwab One International
clients can't register an app, which is why CSV import is the primary path in this project.

Endpoints used (base https://api.schwabapi.com):
  GET  /v1/oauth/authorize, POST /v1/oauth/token
  GET  /trader/v1/accounts/accountNumbers
  GET  /trader/v1/accounts/{hash}/transactions?startDate&endDate&types
  GET  /marketdata/v1/pricehistory
Tokens: access 30 min, refresh 7 days from the initial login (refreshing does not extend it).
"""
from __future__ import annotations

import base64
import json
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol, parse_occ
from app.models import Account, OAuthToken, SourceState, utcnow
from app.security import decrypt, encrypt
from app.services import ingest_records
from app.sources.base import DataSource, SourceResult, SourceStatus, SyncContext
from app.timeutil import et_date, to_utc_naive

log = logging.getLogger(__name__)
API = "https://api.schwabapi.com"
PROVIDER = "schwab"
REFRESH_TOKEN_TTL = timedelta(days=7)
TX_TYPES = ["TRADE", "RECEIVE_AND_DELIVER"]
MAX_RESULTS = 3000
MIN_INTERVAL = 0.6  # seconds between calls -> <=100 req/min, under the 120/min limit


class SchwabAuthError(RuntimeError):
    pass


class SchwabClient:
    def __init__(self, db: Session, transport: httpx.BaseTransport | None = None):
        self.db = db
        self.s = get_settings()
        self.http = httpx.Client(base_url=API, timeout=30, transport=transport)
        self._last_call = 0.0

    # ---- OAuth -------------------------------------------------------------------------
    def authorize_url(self, state: str | None = None) -> str:
        params = {"client_id": self.s.schwab_app_key, "redirect_uri": self.s.schwab_callback_url}
        if state:
            params["state"] = state
        return f"{API}/v1/oauth/authorize?{urlencode(params)}"

    def _basic(self) -> dict:
        raw = f"{self.s.schwab_app_key}:{self.s.schwab_app_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
                "Content-Type": "application/x-www-form-urlencoded"}

    @staticmethod
    def code_from_redirect(url_or_code: str) -> str:
        """Accept either the bare code or the full redirected URL pasted by the user."""
        v = url_or_code.strip()
        if v.startswith("http"):
            qs = parse_qs(urlparse(v).query)
            if "code" not in qs:
                raise SchwabAuthError("No ?code= parameter in the pasted URL")
            return qs["code"][0]  # parse_qs already URL-decodes (%40 -> @)
        return v

    def exchange_code(self, code: str) -> OAuthToken:
        resp = self.http.post("/v1/oauth/token", headers=self._basic(), data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.s.schwab_callback_url})
        if resp.status_code != 200:
            raise SchwabAuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
        return self._store(resp.json(), initial=True)

    def _store(self, data: dict, initial: bool) -> OAuthToken:
        now = utcnow()
        tok = self.db.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        if tok is None:
            tok = OAuthToken(provider=PROVIDER)
            self.db.add(tok)
        tok.access_token_enc = encrypt(data["access_token"])
        tok.access_expires_at = now + timedelta(seconds=int(data.get("expires_in", 1800)))
        if data.get("refresh_token"):
            tok.refresh_token_enc = encrypt(data["refresh_token"])
        if initial or tok.refresh_expires_at is None:
            tok.refresh_expires_at = now + REFRESH_TOKEN_TTL
        self.db.commit()
        return tok

    def token(self) -> OAuthToken | None:
        return self.db.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))

    def access_token(self, force_refresh: bool = False) -> str:
        tok = self.token()
        if tok is None:
            raise SchwabAuthError("Schwab is not connected")
        now = utcnow()
        if tok.refresh_expires_at <= now:
            raise SchwabAuthError("Schwab login expired (7-day limit). Reconnect in Settings.")
        if force_refresh or tok.access_expires_at - timedelta(seconds=60) <= now:
            resp = self.http.post("/v1/oauth/token", headers=self._basic(), data={
                "grant_type": "refresh_token", "refresh_token": decrypt(tok.refresh_token_enc)})
            if resp.status_code != 200:
                raise SchwabAuthError(f"Token refresh failed ({resp.status_code}); reconnect in Settings.")
            tok = self._store(resp.json(), initial=False)
        return decrypt(tok.access_token_enc)

    # ---- HTTP --------------------------------------------------------------------------
    def get(self, path: str, params: dict | None = None):
        retried_auth = False
        for attempt in range(5):
            wait = MIN_INTERVAL - (_time.monotonic() - self._last_call)
            if wait > 0:
                _time.sleep(wait)
            self._last_call = _time.monotonic()
            resp = self.http.get(path, params=params, headers={
                "Authorization": f"Bearer {self.access_token(force_refresh=retried_auth)}"})
            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                _time.sleep(min(30, 2 ** attempt))
                continue
            return resp
        return resp

    def account_numbers(self) -> list[dict]:
        r = self.get("/trader/v1/accounts/accountNumbers")
        r.raise_for_status()
        return r.json()

    def transactions(self, account_hash: str, start: datetime, end: datetime, tx_type: str) -> list[dict]:
        r = self.get(f"/trader/v1/accounts/{account_hash}/transactions", params={
            "startDate": _iso(start), "endDate": _iso(end), "types": tx_type})
        if r.status_code == 400:
            raise RangeRejected(r.text[:300])
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def price_history(self, symbol: str, start: datetime, end: datetime, frequency_type: str,
                      frequency: int) -> list[dict]:
        period_type = "day" if frequency_type == "minute" else "month"
        r = self.get("/marketdata/v1/pricehistory", params={
            "symbol": symbol, "periodType": period_type, "frequencyType": frequency_type,
            "frequency": frequency, "startDate": int(start.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "endDate": int(end.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "needExtendedHoursData": "false"})
        r.raise_for_status()
        return r.json().get("candles", [])


class RangeRejected(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return to_utc_naive(datetime.strptime(s, fmt))
        except ValueError:
            continue
    try:
        return to_utc_naive(datetime.fromisoformat(s))
    except ValueError:
        return None


def parse_transaction(tx: dict) -> list[ExecRecord]:
    """Schwab transaction JSON -> one ExecRecord per security leg.

    UNVERIFIED against a live account: sign conventions of transferItems[].amount/cost. We take
    side from the sign of `amount` (negative = sell), falling back to `cost` (positive cash = sell).
    """
    ts = _parse_ts(tx.get("time")) or _parse_ts(tx.get("tradeDate"))
    if ts is None:
        return []
    items = tx.get("transferItems") or []
    fee_total = sum(abs(float(i.get("cost") or 0)) for i in items if i.get("feeType"))
    sec_items = [i for i in items if not i.get("feeType")
                 and (i.get("instrument") or {}).get("assetType") in ("EQUITY", "OPTION", "COLLECTIVE_INVESTMENT", "ETF")]
    abs_cost = sum(abs(float(i.get("cost") or 0)) for i in sec_items) or float(len(sec_items) or 1)
    desc = (tx.get("description") or "")
    ttype = tx.get("type")
    out = []
    for idx, it in enumerate(sec_items):
        inst = it.get("instrument") or {}
        amount = float(it.get("amount") or 0)
        cost = float(it.get("cost") or 0)
        qty = abs(amount)
        if qty == 0:
            continue
        if amount < 0:
            side = "SELL"
        elif amount > 0 and cost > 0:
            side = "SELL"
        else:
            side = "BUY"
        pe = (it.get("positionEffect") or "").upper()
        effect = "OPEN" if pe.startswith("OPEN") else ("CLOSE" if pe.startswith("CLOS") else None)
        share = (abs(cost) / abs_cost) if abs_cost else 1 / len(sec_items)
        kind = "TRADE"
        if ttype == "RECEIVE_AND_DELIVER":
            d = desc.upper()
            kind = "ASSIGNMENT" if "ASSIGN" in d else "EXERCISE" if "EXERC" in d else "EXPIRATION"
            if inst.get("assetType") != "OPTION":
                kind = "TRADE"  # stock delivered from assignment/exercise
        if inst.get("assetType") == "OPTION":
            occ = parse_occ(inst.get("symbol", ""))
            und = inst.get("underlyingSymbol") or (occ[0] if occ else inst.get("symbol", "?").split()[0])
            exp_s = inst.get("expirationDate")
            exp = _parse_ts(exp_s).date() if exp_s and _parse_ts(exp_s) else (occ[1] if occ else None)
            pc = inst.get("putCall") or (occ[2] if occ else None)
            strike = inst.get("strikePrice") or (occ[3] if occ else None)
            if not (exp and pc and strike):
                continue
            mult = float((inst.get("optionDeliverables") or [{}])[0].get("deliverableUnits") or OPTION_MULTIPLIER)
            rec = ExecRecord(
                external_id=f"{tx.get('activityId')}:{idx}", symbol=option_symbol(und, exp, pc, float(strike)),
                underlying=und, asset_type="OPTION", option_type=pc, strike=float(strike), expiration=exp,
                multiplier=mult, side=None if kind != "TRADE" else side, quantity=qty,
                price=0.0 if kind != "TRADE" else float(it.get("price") or 0), fees=fee_total * share,
                executed_at=ts, position_effect=effect, kind=kind, description=desc[:300],
                trade_date=et_date(ts), raw=tx)
        else:
            sym = inst.get("symbol", "?")
            rec = ExecRecord(
                external_id=f"{tx.get('activityId')}:{idx}", symbol=sym, underlying=sym, asset_type="STOCK",
                side=side, quantity=qty, price=float(it.get("price") or 0), fees=fee_total * share,
                executed_at=ts, position_effect=effect, kind=kind, description=desc[:300],
                trade_date=et_date(ts), raw=tx)
        out.append(rec)
    return out


class SchwabApiSource(DataSource):
    key = "schwab_api"
    name = "Schwab Trader API"
    description = "Automatic sync via Schwab's official API (US retail accounts only)."

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def is_configured(self) -> bool:
        s = get_settings()
        return s.schwab_configured and bool(s.token_encryption_key)

    def status(self, db: Session) -> SourceStatus:
        s = get_settings()
        if not s.schwab_configured:
            return SourceStatus(False, False, "Not configured (set SCHWAB_APP_KEY / SCHWAB_APP_SECRET / SCHWAB_CALLBACK_URL).")
        if not s.token_encryption_key:
            return SourceStatus(False, False, "TOKEN_ENCRYPTION_KEY is required to store Schwab tokens.", "error")
        tok = db.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        if tok is None:
            return SourceStatus(True, False, "Not connected yet. Click Connect.", "warning")
        left = (tok.refresh_expires_at - utcnow()).total_seconds()
        if left <= 0:
            return SourceStatus(True, False, "Schwab login expired. Reconnect to resume syncing.", "error", left)
        level = "warning" if left < 86400 else "info"
        return SourceStatus(True, True, "Connected", level, left)

    def sync(self, db: Session, ctx: SyncContext) -> SourceResult:
        s = get_settings()
        client = SchwabClient(db, transport=self.transport)
        res = SourceResult()
        now = utcnow()
        for a in client.account_numbers():
            number, hash_value = str(a.get("accountNumber", "")), a.get("hashValue")
            acct = db.scalar(select(Account).where(Account.external_ref == hash_value)) or db.scalar(
                select(Account).where(Account.broker == "schwab", Account.is_demo.is_(False),
                                      Account.account_number_masked == f"...{number[-3:]}"))
            if acct is None:
                acct = Account(name=f"Schwab ...{number[-3:]}", broker="schwab",
                               account_number_masked=f"...{number[-3:]}")
                db.add(acct)
                db.flush()
            acct.external_ref = hash_value
            st = db.scalar(select(SourceState).where(SourceState.source == self.key,
                                                     SourceState.account_id == acct.id))
            if st is None:
                st = SourceState(source=self.key, account_id=acct.id)
                db.add(st)
            if st.synced_through:
                windows = _chunks(st.synced_through - timedelta(days=s.sync_overlap_days), now, s.schwab_chunk_days)
                backfill = False
            else:
                windows = list(reversed(_chunks(now - timedelta(days=s.schwab_max_lookback_days), now,
                                                 s.schwab_chunk_days)))
                backfill = True
            records: list[ExecRecord] = []
            earliest = None
            for start, end in windows:
                try:
                    txs = self._fetch_window(client, hash_value, start, end)
                except RangeRejected as exc:
                    if backfill:
                        ctx.info(f"{acct.name}: API refused dates before {end:%Y-%m-%d}; history limit reached ({exc}).")
                        break
                    raise
                earliest = start
                res.fetched += len(txs)
                for tx in txs:
                    records.extend(parse_transaction(tx))
            stats = ingest_records(db, acct.id, self.key, records)
            res.inserted += stats.inserted
            res.merged += stats.merged
            st.synced_through = now
            st.last_success_at = now
            if backfill and earliest:
                st.earliest_reached = earliest
            res.accounts.append(acct.id)
            ctx.info(f"{acct.name}: {len(records)} executions ({stats.inserted} new, {stats.merged} merged)")
            db.commit()
        return res

    def _fetch_window(self, client: SchwabClient, h: str, start: datetime, end: datetime) -> list[dict]:
        out: list[dict] = []
        for t in TX_TYPES:
            out.extend(self._fetch_split(client, h, start, end, t))
        return out

    def _fetch_split(self, client, h, start, end, t, depth=0) -> list[dict]:
        txs = client.transactions(h, start, end, t)
        if len(txs) >= MAX_RESULTS and depth < 8 and (end - start) > timedelta(hours=2):
            mid = start + (end - start) / 2
            return self._fetch_split(client, h, start, mid, t, depth + 1) + \
                self._fetch_split(client, h, mid, end, t, depth + 1)
        return txs


def _chunks(start: datetime, end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    out = []
    cur = start
    step = timedelta(days=max(1, min(days, 365)))
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out


def save_tokens_json(db: Session, data: dict) -> None:  # pragma: no cover - helper for manual testing
    SchwabClient(db)._store(data, initial=True)


__all__ = ["SchwabApiSource", "SchwabClient", "SchwabAuthError", "parse_transaction", "json"]
