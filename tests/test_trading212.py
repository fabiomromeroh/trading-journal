"""Trading 212 adapter: ticker mapping, fill conversion, mocked HTTP (pagination, 401/403/429),
encrypted credentials, incremental idempotent sync, Settings connect card. No real API key is used."""
from __future__ import annotations

import base64
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.instruments import ExecRecord
from app.models import Account, Execution, Instrument, Trade
from app.services import get_state, ingest_records, rebuild_trades
from app.sources.base import SyncContext
from app.sources import trading212 as t212
from app import backup, markets, stops, symbolmap

FIX = Path(__file__).parent / "fixtures" / "t212"
KEY, SECRET = "t212_test_key_abc", "t212_test_secret_xyz"


def load(name: str):
    return json.loads((FIX / name).read_text())


def _auth_ok(req: httpx.Request) -> bool:
    h = req.headers.get("authorization") or ""
    if not h.lower().startswith("basic "):
        return False
    raw = base64.b64decode(h.split()[1]).decode()
    return raw == f"{KEY}:{SECRET}"


class FakeT212:
    """Recorded-fixture Trading 212 API. Optional per-path status overrides for error tests."""

    def __init__(self, status: dict | None = None, pages=None, rate_then_ok: int = 0):
        self.status = status or {}
        self.calls: list[httpx.Request] = []
        self.pages = pages or [load("history_page1.json"), load("history_page2.json")]
        self.rate_then_ok = rate_then_ok
        self._rated = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if not _auth_ok(req):
            return httpx.Response(401, json={"message": "Invalid API key"})
        path = req.url.path
        for suffix, code in self.status.items():
            if path.endswith(suffix):
                if code == 429:
                    return httpx.Response(429, headers={"x-ratelimit-reset": "0"}, json={"message": "rate"})
                if code == 403:
                    return httpx.Response(403, json={"message": "Scope(history:orders) missing"})
                return httpx.Response(code, json={"message": f"err {code}"})
        if self.rate_then_ok and self._rated < self.rate_then_ok and path.endswith("/equity/account/summary"):
            self._rated += 1
            return httpx.Response(429, headers={"x-ratelimit-reset": "0"}, json={"message": "rate"})
        if path.endswith("/equity/account/summary"):
            return httpx.Response(200, json=load("summary.json"))
        if path.endswith("/equity/history/orders"):
            cursor = req.url.params.get("cursor")
            page = self.pages[1] if cursor else self.pages[0]
            return httpx.Response(200, json=page)
        if path.endswith("/equity/positions"):
            return httpx.Response(200, json=load("positions.json"))
        if path.endswith("/equity/metadata/instruments"):
            return httpx.Response(200, json=load("instruments.json"))
        return httpx.Response(404, json={"message": "not found"})

    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture()
def enc(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(t212, "_TRANSPORT", None)
    monkeypatch.setattr(t212._time, "sleep", lambda _n: None)
    return s


def nop_sleep(_n=None):
    return None


# --------------------------------------------------------------------------- mapping
def test_parse_t212_tickers():
    a = markets.parse_t212_ticker("AAPL_US_EQ", "US0378331005")
    assert (a.base, a.exchange, a.symbol) == ("AAPL", "US", "AAPL")
    assert markets.yahoo_symbol_for(a.symbol, a.exchange) == "AAPL"
    brk = markets.parse_t212_ticker("BRK_B_US_EQ")
    assert brk.symbol == "BRK.B" and markets.yahoo_symbol_for(brk.symbol, brk.exchange) == "BRK-B"
    vusa = markets.parse_t212_ticker("VUSAl_EQ", "IE00B3XXRP09")
    assert (vusa.base, vusa.exchange, vusa.symbol) == ("VUSA", "LSE", "VUSA")
    assert markets.yahoo_symbol_for("VUSA", "LSE") == "VUSA.L"
    sap = markets.parse_t212_ticker("SAPd_EQ")
    assert sap.exchange == "XETRA" and markets.yahoo_symbol_for("SAP", "XETRA") == "SAP.DE"
    asml = markets.parse_t212_ticker("ASMLa_EQ")
    assert asml.exchange == "AMS" and markets.yahoo_symbol_for("ASML", "AMS") == "ASML.AS"
    vod = markets.parse_t212_ticker("VOD_EQ", "GB00BH4HKS39")
    assert vod.exchange == "LSE"
    unk = markets.parse_t212_ticker("ZZZ_EQ", "XX000")
    assert unk.exchange is None and markets.yahoo_symbol_for("ZZZ", None) is None


def test_pence_factor():
    assert markets.pence_factor("GBp", "GBP") == 0.01
    assert markets.pence_factor("GBX", "GBP") == 0.01
    assert markets.pence_factor("GBP", "GBP") == 1.0
    assert markets.pence_factor("USD", "USD") == 1.0


def _item(page, fill_id):
    for it in page["items"]:
        if (it.get("fill") or {}).get("id") == fill_id:
            return it
    raise AssertionError(fill_id)


def test_to_record_us_fractional_fees_and_fx():
    it = _item(load("history_page1.json"), 1007)  # AAPL sell 2.5 @ 196 USD
    r = t212.to_record(it)
    assert isinstance(r, ExecRecord)
    assert r.symbol == "AAPL" and r.side == "SELL" and r.quantity == 2.5
    assert r.price == 196.0 and r.currency == "USD"
    assert r.fx_to_account == pytest.approx(0.8896)
    assert r.fees == pytest.approx((0.65 + 0.01) / 0.8896)
    assert r.trade_date == date(2026, 9, 16)  # 15:45Z -> 11:45 EDT
    assert r.external_id == "1007" and r.time_known is True
    assert r.raw["isin"] == "US0378331005"


def test_to_record_gbx_scaled_to_gbp():
    it = _item(load("history_page1.json"), 1009)  # VUSA sell 10 @ 9710 GBX
    r = t212.to_record(it)
    assert r.symbol == "VUSA" and r.currency == "GBP"
    assert r.price == pytest.approx(97.10)
    assert r.fx_to_account == pytest.approx(1.165)  # GBP -> EUR
    assert r.trade_date == date(2026, 9, 24)  # 13:40+01:00 London


def test_to_record_eu_session_dates():
    sap = t212.to_record(_item(load("history_page1.json"), 1008))
    assert sap.symbol == "SAP" and sap.currency == "EUR" and sap.fx_to_account == 1.0
    assert sap.trade_date == date(2026, 9, 18)
    asml = t212.to_record(_item(load("history_page1.json"), 1010))
    assert asml.symbol == "ASML" and asml.trade_date == date(2026, 10, 1)


def test_to_record_skips_splits_and_unfilled():
    split = _item(load("history_page2.json"), 1005)
    r = t212.to_record(split)
    assert isinstance(r, t212.Skip) and r.reason == "stock split"
    unfilled = load("history_page2.json")["items"][0]  # cancelled TSLA, no fill
    assert isinstance(t212.to_record(unfilled), t212.Skip)


def test_fx_picks_direction_that_matches_wallet():
    # 10 * 97.10 GBP, net 1131.2 EUR, listed fxRate 1.165 (= GBP->EUR)
    assert t212.resolve_fx(971.0, {"currency": "EUR", "fxRate": 1.165, "netValue": 1131.2}, "GBP") == pytest.approx(1.165)
    # inverted listing would still resolve
    assert t212.resolve_fx(971.0, {"currency": "EUR", "fxRate": 1 / 1.165, "netValue": 1131.2}, "GBP") == pytest.approx(1.165)
    assert t212.resolve_fx(100.0, {"currency": "EUR", "fxRate": 1.0, "netValue": -100}, "EUR") == 1.0
    # a true GBP quote (pounds per EUR ~ 0.80) must not be treated as pence
    assert t212.resolve_fx(100.0, {"currency": "EUR", "fxRate": 0.8, "netValue": 125.15}, "GBP", pence=True) == pytest.approx(1.25)


def _gbx(side, fid, qty, price_pence, fx_rate, net, rpl, taxes, when="2026-09-02T08:00:00Z"):
    """Hand-built GBX fill. fx_rate is pence per 1 EUR, the way Trading 212 quotes LSE lines."""
    return {
        "order": {
            "id": fid, "side": side, "ticker": "GBXl_EQ",
            "instrument": {"ticker": "GBXl_EQ", "name": "Pence Test", "isin": "GB0000000001", "currency": "GBX"},
        },
        "fill": {
            "id": fid, "filledAt": when, "price": price_pence, "quantity": qty, "type": "TRADE",
            "walletImpact": {
                "currency": "EUR", "fxRate": fx_rate, "netValue": net, "realisedProfitLoss": rpl,
                "taxes": [{"name": n, "quantity": q, "currency": "EUR"} for n, q in taxes],
            },
        },
    }


def test_gbx_round_trip_realized_matches_broker():
    """10 shares bought at 1000 pence and sold at 1200, fxRate 80 pence per EUR.

    Price stored in pounds is 10.00 then 12.00. GBP->EUR = 100/80 = 1.25.
    Buy cash = 10 * 10.00 * 1.25 = 125 EUR, plus 1.00 EUR stamp (net 126).
    Sell cash = 10 * 12.00 * 1.25 = 150 EUR, minus 0.50 EUR FX fee (net 149.50).
    Broker realisedProfitLoss = 150 - 125 = 25.00 EUR (price result, taxes excluded).
    Fees in pounds are 1.00/1.25 = 0.80 and 0.50/1.25 = 0.40, not the ~100× pence-rate figures 80 and 40.
    The trade itself stays in GBP: gross 20, net 18.80, return 18.8%."""
    buy = _gbx("BUY", 1, 10, 1000, 80, net=-126.0, rpl=None, taxes=[("STAMP_DUTY_RESERVE_TAX", 1.00)],
               when="2026-09-02T08:00:00Z")
    sell = _gbx("SELL", 2, 10, 1200, 80, net=149.50, rpl=25.0, taxes=[("CURRENCY_CONVERSION_FEE", 0.50)],
                when="2026-09-10T15:00:00Z")
    rb, rs = t212.to_record(buy), t212.to_record(sell)
    assert isinstance(rb, ExecRecord) and isinstance(rs, ExecRecord)
    assert rb.currency == "GBP" and rs.currency == "GBP"
    assert rb.price == pytest.approx(10.0) and rs.price == pytest.approx(12.0)
    assert rb.fx_to_account == pytest.approx(1.25) and rs.fx_to_account == pytest.approx(1.25)
    assert rb.fees == pytest.approx(0.80)
    assert rs.fees == pytest.approx(0.40)
    # the unscaled pence rate (1/80) would have turned the 1 EUR stamp into 80 GBP
    assert rb.fees * 100 == pytest.approx(80.0)
    got = t212.account_gross([
        (rb.side, rb.quantity, rb.price, rb.fx_to_account),
        (rs.side, rs.quantity, rs.price, rs.fx_to_account),
    ])
    assert got == pytest.approx(sell["fill"]["walletImpact"]["realisedProfitLoss"])
    assert got == pytest.approx(25.0)


# --------------------------------------------------------------------------- HTTP client
def test_401_403_429_mapping(enc):
    cl = t212.T212Client(KEY, SECRET, transport=FakeT212(status={"/equity/account/summary": 401}).transport(),
                         sleep=nop_sleep)
    with pytest.raises(t212.T212Error) as ei:
        cl.account_summary()
    assert ei.value.kind == "auth" and ei.value.status == 401 and "401" in str(ei.value)
    cl.close()

    cl = t212.T212Client(KEY, SECRET, transport=FakeT212(status={"/equity/history/orders": 403}).transport(),
                         sleep=nop_sleep)
    with pytest.raises(t212.T212Error) as ei:
        cl.get("/equity/history/orders", {"limit": 1})
    assert ei.value.kind == "scope" and "403" in str(ei.value) and "Scope" in str(ei.value)
    cl.close()

    sleeps = []
    cl = t212.T212Client(KEY, SECRET, transport=FakeT212(status={"/equity/account/summary": 429}).transport(),
                         sleep=sleeps.append, clock=lambda: 0)
    with pytest.raises(t212.T212Error) as ei:
        cl.account_summary()
    assert ei.value.kind == "rate" and ei.value.status == 429
    assert sleeps  # backed off
    cl.close()


def test_429_retries_then_succeeds(enc):
    fake = FakeT212(rate_then_ok=1)
    cl = t212.T212Client(KEY, SECRET, transport=fake.transport(), sleep=nop_sleep, clock=lambda: 0)
    s = cl.account_summary()
    assert s["id"] == 12345678
    cl.close()


def test_wrong_secret_is_401(enc):
    cl = t212.T212Client(KEY, "nope", transport=FakeT212().transport(), sleep=nop_sleep)
    with pytest.raises(t212.T212Error) as ei:
        cl.account_summary()
    assert ei.value.kind == "auth"
    cl.close()


def test_pagination_follows_next_page_path(enc):
    fake = FakeT212()
    cl = t212.T212Client(KEY, SECRET, transport=fake.transport(), sleep=nop_sleep)
    pages = list(cl.history_pages())
    assert len(pages) == 2
    assert {i["fill"]["id"] for p in pages for i in p if i.get("fill")} == {1010, 1009, 1008, 1007, 1005, 1004, 1003, 1002, 1001}
    paths = [c.url.path for c in fake.calls]
    assert all(p.endswith("/equity/history/orders") for p in paths)
    assert fake.calls[1].url.params.get("cursor") == "1757500000000"
    cl.close()


def test_test_connection_ok_and_missing_history(enc):
    fake = FakeT212()
    res = t212.test_connection(KEY, SECRET, transport=fake.transport(), sleep=nop_sleep)
    assert res["ok"] and res["account_id"] == 12345678 and res["currency"] == "EUR"
    assert res["scopes"] == {"account": True, "history": True, "portfolio": True}
    assert KEY not in json.dumps(res) and SECRET not in json.dumps(res)

    res = t212.test_connection(KEY, SECRET, transport=FakeT212(status={"/equity/history/orders": 403}).transport(),
                               sleep=nop_sleep)
    assert not res["ok"] and res["kind"] == "scope" and res["scopes"]["history"] is False


# --------------------------------------------------------------------------- persist / sync
def test_credentials_encrypted_and_excluded_from_backup(db, enc):
    t212.save_credentials(db, KEY, SECRET, "live")
    db.commit()
    raw = get_state(db, t212.CRED_STATE)
    assert raw and KEY not in raw and SECRET not in raw
    loaded = t212.load_credentials(db)
    assert loaded == {"key": KEY, "secret": SECRET, "env": "live"}
    assert t212.mask(KEY).startswith("t212") and SECRET not in t212.mask(KEY)
    data = backup.build(db)
    keys = {x["key"] for x in data["tables"]["app_state"]}
    assert t212.CRED_STATE not in keys
    dumped = json.dumps(data)
    assert KEY not in dumped and SECRET not in dumped
    t212.clear_credentials(db)
    db.commit()
    assert t212.load_credentials(db) is None


def _run_sync(db, fake=None):
    fake = fake or FakeT212()
    t212.save_credentials(db, KEY, SECRET, "live")
    db.commit()
    src = t212.Trading212Source(transport=fake.transport(), sleep=nop_sleep)
    return src.sync(db, SyncContext()), fake, src


def test_sync_ingests_trades_skips_splits_idempotent(db, enc):
    res, fake, src = _run_sync(db)
    # 8 TRADE fills (page1: 4, page2: 4); 1 STOCK_SPLIT and 1 unfilled order are skipped
    assert res.inserted == 8
    acct = db.scalar(select(Account).where(Account.source_key == "t212"))
    assert acct.currency == "EUR" and acct.broker == "trading212"
    execs = list(db.scalars(select(Execution).where(Execution.account_id == acct.id)))
    assert {e.external_id for e in execs} == {"1010", "1009", "1008", "1007", "1004", "1003", "1002", "1001"}
    vusa = [e for e in execs if e.symbol == "VUSA"]
    assert {e.quantity for e in vusa} == {4.0, 6.0, 10.0}
    assert all(e.currency == "GBP" and e.price == pytest.approx(96.50 if e.side == "BUY" else 97.10) for e in vusa)
    aapl = next(e for e in execs if e.external_id == "1007")
    assert aapl.quantity == 2.5 and aapl.currency == "USD"
    insts = {i.broker_symbol: i for i in db.scalars(select(Instrument))}
    assert insts["VUSAl_EQ"].price_symbol == "VUSA.L"
    assert insts["SAPd_EQ"].price_symbol == "SAP.DE"
    assert insts["ASMLa_EQ"].price_symbol == "ASML.AS"
    assert insts["AAPL_US_EQ"].price_symbol == "AAPL"
    last = t212.last_status(db)
    assert last["ok"] is True and last["skipped"]["stock split"] == 1 and last["skipped"]["unfilled"] == 1
    assert KEY not in json.dumps(last) and SECRET not in json.dumps(last)

    trades = list(db.scalars(select(Trade).where(Trade.account_id == acct.id)))
    by = {t.symbol: t for t in trades}
    assert by["ASML"].status == "OPEN" and by["ASML"].open_quantity == pytest.approx(3.0)
    assert by["AAPL"].status == "CLOSED" and by["AAPL"].quantity == pytest.approx(2.5)
    assert by["VUSA"].status == "CLOSED" and by["VUSA"].quantity == pytest.approx(10.0)
    assert by["SAP"].status == "CLOSED"

    # second sync is idempotent
    res2, _, _ = _run_sync(db, fake)
    assert res2.inserted == 0
    assert db.query(Execution).filter(Execution.account_id == acct.id).count() == 8


def test_incremental_stops_after_old_page(db, enc):
    _run_sync(db)
    # cursor is newest fill (ASML Oct 1). A later sync still reads the overlap window.
    fake = FakeT212()
    src = t212.Trading212Source(transport=fake.transport(), sleep=nop_sleep)
    src.sync(db, SyncContext())
    assert any(c.url.path.endswith("/equity/history/orders") for c in fake.calls)


def test_us_auto_stop_skipped_for_lse(db, enc):
    acct = Account(name="T", broker="trading212", currency="EUR", source_key="t212", market="EU")
    db.add(acct)
    db.flush()
    us = symbolmap.upsert_t212(db, "AAPL_US_EQ", "US0378331005", "Apple", "USD")
    lse = symbolmap.upsert_t212(db, "VUSAl_EQ", "IE00B3XXRP09", "VUSA", "GBX")
    recs = [
        ExecRecord(external_id="1", symbol="AAPL", underlying="AAPL", asset_type="STOCK", side="BUY",
                   quantity=1, price=100, executed_at=datetime(2026, 9, 2, 14, 31), instrument_id=us.id,
                   currency="USD"),
        ExecRecord(external_id="2", symbol="VUSA", underlying="VUSA", asset_type="STOCK", side="BUY",
                   quantity=1, price=97.1, executed_at=datetime(2026, 9, 2, 7, 5), instrument_id=lse.id,
                   currency="GBP"),
    ]
    ingest_records(db, acct.id, "t212", recs)
    rebuild_trades(db, [acct.id])
    db.commit()
    aapl = db.scalar(select(Trade).where(Trade.symbol == "AAPL"))
    vusa = db.scalar(select(Trade).where(Trade.symbol == "VUSA"))
    assert stops.eligible(aapl) is True
    assert stops.eligible(vusa) is False
    schwab = Trade(key="s", account_id=acct.id, symbol="MSFT", underlying="MSFT", asset_type="STOCK",
                   direction="LONG", status="OPEN", opened_at=datetime(2026, 9, 2, 14, 0),
                   quantity=1, open_quantity=1, entry_price=100, cost_basis=100)
    db.add(schwab)
    db.flush()
    assert stops.eligible(schwab) is True  # no instrument: Schwab/US path


def test_instrument_override_not_clobbered(db, enc):
    inst = symbolmap.upsert_t212(db, "VUSAl_EQ", "IE00B3XXRP09", "VUSA", "GBX")
    inst.price_symbol = "VUAA.L"
    inst.user_override = True
    db.flush()
    again = symbolmap.upsert_t212(db, "VUSAl_EQ", "IE00B3XXRP09", "Vanguard", "GBX")
    assert again.id == inst.id and again.price_symbol == "VUAA.L" and again.user_override


def test_settings_connect_test_disconnect(db, enc, monkeypatch):
    fake = FakeT212()
    monkeypatch.setattr(t212, "_TRANSPORT", fake.transport())
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    page = c.get("/settings").text
    assert "Trading 212" in page and "Connect Trading 212" in page
    assert "Orders - Execute" in page
    r = c.post("/settings/brokers/t212/connect", data={"key": KEY, "secret": SECRET}, follow_redirects=True)
    assert r.status_code == 200 and "Trading 212 connected:" in r.text
    assert t212.load_credentials(db)["key"] == KEY
    assert KEY not in r.text and SECRET not in r.text  # raw key never rendered
    page = c.get("/settings").text
    assert t212.mask(KEY) in page
    r = c.post("/settings/brokers/t212/test", follow_redirects=True)
    assert "connection OK" in r.text
    r = c.post("/settings/brokers/t212/disconnect", follow_redirects=True)
    assert t212.load_credentials(db) is None
    assert "disconnected" in r.text.lower()


def test_connect_rejects_401(db, enc, monkeypatch):
    monkeypatch.setattr(t212, "_TRANSPORT", FakeT212(status={"/equity/account/summary": 401}).transport())
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    r = c.post("/settings/brokers/t212/connect", data={"key": KEY, "secret": SECRET}, follow_redirects=True)
    assert "not connected" in r.text.lower()
    assert t212.load_credentials(db) is None
    assert KEY not in r.text and SECRET not in r.text


def test_all_sources_includes_t212_but_unconfigured_is_skipped(db, enc):
    from app.sources import all_sources, enabled_sources
    keys = [s.key for s in all_sources()]
    assert keys[:2] == ["snaptrade", "schwab_api"] and "t212" in keys
    assert all(s.key != "t212" for s in enabled_sources())
    t212.save_credentials(db, KEY, SECRET)
    db.commit()
    assert any(s.key == "t212" for s in enabled_sources())


def test_gbx_trade_stays_in_pounds_and_repair_keeps_journal(db, enc):
    buy = _gbx("BUY", 1, 10, 1000, 80, net=-126.0, rpl=None, taxes=[("STAMP_DUTY_RESERVE_TAX", 1.00)],
               when="2026-09-02T08:00:00Z")
    sell = _gbx("SELL", 2, 10, 1200, 80, net=149.50, rpl=25.0, taxes=[("CURRENCY_CONVERSION_FEE", 0.50)],
                when="2026-09-10T15:00:00Z")
    acct = Account(name="T", broker="trading212", currency="EUR", source_key="t212", market="EU")
    db.add(acct)
    db.flush()
    recs = [t212.to_record(buy), t212.to_record(sell)]
    ingest_records(db, acct.id, "t212", recs)
    rebuild_trades(db, [acct.id])
    tr = db.scalar(select(Trade).where(Trade.account_id == acct.id))
    assert tr.currency == "GBP" and tr.status == "CLOSED"
    assert tr.entry_price == pytest.approx(10.0) and tr.exit_price == pytest.approx(12.0)
    assert tr.gross_pnl == pytest.approx(20.0)
    assert tr.fees == pytest.approx(1.20)
    assert tr.net_pnl == pytest.approx(18.80)
    assert tr.return_pct == pytest.approx(18.8)
    tr.notes, tr.initial_stop, tr.setup = "keep me", 9.5, "breakout"
    from app.models import Tag
    tr.tags.append(Tag(name="gbx-note"))
    db.commit()

    # replay the old bug on the stored rows: pound price, pence-per-EUR rate, fees ~100×
    for e in db.scalars(select(Execution).where(Execution.account_id == acct.id)):
        e.fx_to_account = 1.0 / 80.0
        e.fees = round(e.fees * 100, 6)
    db.commit()
    res = t212.repair_stored_fx(db)
    assert res["fixed"] == 2
    db.expire_all()
    tr = db.scalar(select(Trade).where(Trade.account_id == acct.id))
    assert tr.currency == "GBP"
    assert tr.notes == "keep me" and tr.initial_stop == 9.5 and tr.setup == "breakout"
    assert [t.name for t in tr.tags] == ["gbx-note"]
    assert tr.net_pnl == pytest.approx(18.80) and tr.return_pct == pytest.approx(18.8)
    execs = list(db.scalars(select(Execution).where(Execution.account_id == acct.id)))
    assert {round(e.fx_to_account, 2) for e in execs} == {1.25}
    assert sorted(e.fees for e in execs) == pytest.approx([0.40, 0.80])
    # second pass changes nothing
    assert t212.repair_stored_fx(db)["fixed"] == 0

    # a pounds quote (fxRate already ~0.8 GBP per EUR) is not divided by 100 again
    gbp = Execution(account_id=acct.id, source="t212", external_id="gbp1", match_key="gbp1",
                    symbol="VUKE", underlying="VUKE", asset_type="STOCK", side="BUY", quantity=2, price=50.0,
                    fees=0.12, currency="GBP", fx_to_account=1.25, executed_at=datetime(2026, 9, 3, 9, 0),
                    raw=json.dumps({"ccy": "GBP", "wallet": {"currency": "EUR", "fxRate": 0.8, "netValue": 125.15,
                                                             "realisedProfitLoss": None},
                                    "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "q": -0.15, "ccy": "EUR"}]}))
    db.add(gbp)
    db.commit()
    assert t212.repair_stored_fx(db)["fixed"] == 0
    db.refresh(gbp)
    assert gbp.fx_to_account == pytest.approx(1.25)
    assert gbp.fees == pytest.approx(0.15 / 1.25)


def test_resync_refreshes_fx_rate(db, enc):
    _run_sync(db)
    acct = db.scalar(select(Account).where(Account.source_key == "t212"))
    row = db.scalar(select(Execution).where(Execution.external_id == "1007"))
    row.fx_to_account = 99.0
    row.fees = 0.0
    db.commit()
    _run_sync(db)
    db.expire_all()
    row = db.scalar(select(Execution).where(Execution.external_id == "1007"))
    assert row.fx_to_account == pytest.approx(0.8896)
    assert row.fees == pytest.approx((0.65 + 0.01) / 0.8896)
    assert db.query(Execution).filter(Execution.account_id == acct.id).count() == 8


def test_sync_now_runs_t212_when_connected(db, enc, monkeypatch):
    fake = FakeT212()
    t212.save_credentials(db, KEY, SECRET)
    db.commit()
    monkeypatch.setattr(t212, "_TRANSPORT", fake.transport())
    from app.sync import execute_run, start_run
    run, _ = start_run(db, "manual")
    db.commit()
    src = t212.Trading212Source(transport=fake.transport(), sleep=nop_sleep)
    out = execute_run(run.id, sources=[src])
    assert out.status in ("success", "partial")
    assert db.query(Execution).count() == 8
