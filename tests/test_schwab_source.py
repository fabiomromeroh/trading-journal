"""Schwab Trader API source against a mocked HTTP API (no real credentials needed)."""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Execution, OAuthToken, Trade, utcnow
from app.sources.schwab_api import SchwabApiSource, SchwabClient, parse_transaction
from app.sources.base import SyncContext

TX_BUY = {"activityId": 111, "time": "2026-09-14T13:45:00+0000", "type": "TRADE", "description": "",
          "netAmount": -2250.65, "transferItems": [
              {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}, "amount": 0, "cost": -0.65, "feeType": "COMMISSION"},
              {"instrument": {"assetType": "EQUITY", "symbol": "AAPL"}, "amount": 10, "cost": -2250, "price": 225.0, "positionEffect": "OPENING"}]}
TX_SELL = {"activityId": 112, "time": "2026-09-14T15:00:00+0000", "type": "TRADE", "netAmount": 2299.9, "transferItems": [
    {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}, "amount": 0, "cost": -0.10, "feeType": "SEC_FEE"},
    {"instrument": {"assetType": "EQUITY", "symbol": "AAPL"}, "amount": -10, "cost": 2300, "price": 230.0, "positionEffect": "CLOSING"}]}
TX_STO = {"activityId": 113, "time": "2026-09-10T14:00:00+0000", "type": "TRADE", "transferItems": [
    {"instrument": {"assetType": "OPTION", "symbol": "SPY   260918P00580000", "underlyingSymbol": "SPY", "putCall": "PUT",
                    "strikePrice": 580, "expirationDate": "2026-09-18T20:00:00+0000"},
     "amount": -2, "cost": 250, "price": 1.25, "positionEffect": "OPENING"}]}
TX_EXP = {"activityId": 114, "time": "2026-09-19T04:00:00+0000", "type": "RECEIVE_AND_DELIVER",
          "description": "REMOVAL OF OPTION DUE TO EXPIRATION", "transferItems": [
              {"instrument": {"assetType": "OPTION", "symbol": "SPY   260918P00580000", "underlyingSymbol": "SPY", "putCall": "PUT",
                              "strikePrice": 580, "expirationDate": "2026-09-18T20:00:00+0000"},
               "amount": 2, "cost": 0, "price": 0, "positionEffect": "CLOSING"}]}


@pytest.fixture()
def schwab_env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "schwab_app_key", "key")
    monkeypatch.setattr(s, "schwab_app_secret", "secret")
    monkeypatch.setattr(s, "schwab_callback_url", "https://127.0.0.1:8182")
    monkeypatch.setattr(s, "token_encryption_key", "unit-test-key")
    monkeypatch.setattr("app.sources.schwab_api.MIN_INTERVAL", 0)
    return s


def test_parse_transaction_equity_and_fees():
    (buy,) = parse_transaction(TX_BUY)
    assert buy.side == "BUY" and buy.quantity == 10 and buy.price == 225 and buy.fees == pytest.approx(0.65)
    assert buy.position_effect == "OPEN" and buy.external_id == "111:0"
    (sell,) = parse_transaction(TX_SELL)
    assert sell.side == "SELL" and sell.position_effect == "CLOSE"


def test_parse_transaction_option_and_expiration():
    (sto,) = parse_transaction(TX_STO)
    assert sto.symbol == "SPY 2026-09-18 580P" and sto.side == "SELL" and sto.multiplier == 100
    (exp,) = parse_transaction(TX_EXP)
    assert exp.kind == "EXPIRATION" and exp.side is None and exp.price == 0


def make_transport(calls):
    def handler(req: httpx.Request):
        calls.append(req)
        p = req.url.path
        if p == "/v1/oauth/token":
            body = dict(x.split("=") for x in req.content.decode().split("&"))
            assert req.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": f"AT-{len(calls)}", "refresh_token": "RT-1",
                                             "expires_in": 1800, "token_type": "Bearer", "grant": body["grant_type"]})
        assert req.headers["Authorization"].startswith("Bearer AT-")
        if p.endswith("/accountNumbers"):
            return httpx.Response(200, json=[{"accountNumber": "99990123", "hashValue": "HASH1"}])
        if p.endswith("/transactions"):
            t = req.url.params["types"]
            start = req.url.params["startDate"]
            if start < "2026-01-01":  # pretend the API refuses older ranges
                return httpx.Response(400, json={"message": "date range"})
            data = [TX_BUY, TX_SELL, TX_STO] if t == "TRADE" else [TX_EXP]
            return httpx.Response(200, json=data)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_oauth_exchange_refresh_and_sync(db, schwab_env, monkeypatch):
    calls = []
    transport = make_transport(calls)
    client = SchwabClient(db, transport=transport)
    assert "client_id=key" in client.authorize_url("st")
    code = client.code_from_redirect("https://127.0.0.1:8182/?code=C0.abc%40&session=xyz")
    assert code == "C0.abc@"
    tok = client.exchange_code(code)
    assert tok.refresh_expires_at - utcnow() > timedelta(days=6, hours=23)
    assert "RT-1" not in tok.refresh_token_enc  # stored encrypted

    src = SchwabApiSource(transport=transport)
    assert src.status(db).ready
    monkeypatch.setattr(schwab_env, "schwab_chunk_days", 120)
    res = src.sync(db, SyncContext())
    # dedupe: the same transactions come back for every window, stored once
    assert db.query(Execution).count() == 4
    assert res.inserted == 4
    from app.services import rebuild_trades
    rebuild_trades(db)
    trades = {t.symbol: t for t in db.scalars(select(Trade))}
    assert trades["AAPL"].net_pnl == pytest.approx(50 - 0.75)
    assert trades["SPY 2026-09-18 580P"].close_reason == "EXPIRATION"
    # second sync is incremental and idempotent
    res2 = src.sync(db, SyncContext())
    assert res2.inserted == 0 and db.query(Execution).count() == 4

    # expired refresh token -> not ready, banner-worthy status
    t = db.scalar(select(OAuthToken))
    t.refresh_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    st = src.status(db)
    assert not st.ready and st.level == "error"


def test_access_token_refresh_when_expired(db, schwab_env):
    calls = []
    client = SchwabClient(db, transport=make_transport(calls))
    client.exchange_code("c")
    t = db.scalar(select(OAuthToken))
    original_refresh_expiry = t.refresh_expires_at
    t.access_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    client.account_numbers()
    grants = [json.loads(c.content or b"{}") if False else c.content.decode() for c in calls if c.url.path == "/v1/oauth/token"]
    assert any("grant_type=refresh_token" in g for g in grants)
    # refreshing does NOT extend the 7-day refresh-token window
    assert db.scalar(select(OAuthToken)).refresh_expires_at == original_refresh_expiry


def test_source_disabled_without_env(db):
    st = SchwabApiSource().status(db)
    assert not st.configured
