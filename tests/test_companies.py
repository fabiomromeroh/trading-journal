"""Company name, sector and industry: Yahoo parsing, cache, filters, list, detail, reports."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import companies
from app.models import Account, CompanyProfile, FxRate, Instrument, Trade, utcnow

EQUITY = {
    "quoteSummary": {"result": [{
        "assetProfile": {"sector": "Technology", "industry": "Consumer Electronics"},
        "quoteType": {"longName": "Apple Inc.", "quoteType": "EQUITY"},
        "fundProfile": {},
    }]}
}
QUOTE = {"quoteResponse": {"result": [{"longName": "Apple Inc.", "quoteType": "EQUITY", "shortName": "Apple"}]}}
ETF = {
    "quoteSummary": {"result": [{
        "assetProfile": {},
        "quoteType": {"longName": "SPDR S&P 500 ETF Trust", "quoteType": "ETF"},
        "fundProfile": {"categoryName": "Large Blend"},
    }]}
}
CHART = {"chart": {"result": [{"meta": {"longName": "Apple Inc.", "instrumentType": "EQUITY"}}]}}


def test_parse_yahoo_equity_quote_and_etf():
    eq = companies.parse_yahoo(EQUITY, QUOTE)
    assert eq["ok"] and eq["name"] == "Apple Inc." and eq["sector"] == "Technology"
    assert eq["industry"] == "Consumer Electronics" and eq["quote_type"] == "EQUITY"
    # longName from the quote payload when quoteSummary has no name
    bare = {"quoteSummary": {"result": [{"assetProfile": {"sector": "Energy"}, "quoteType": {}}]}}
    q = {"quoteResponse": {"result": [{"longName": "Exxon Mobil Corporation", "quoteType": "EQUITY"}]}}
    got = companies.parse_yahoo(bare, q)
    assert got["name"] == "Exxon Mobil Corporation" and got["sector"] == "Energy"
    etf = companies.parse_yahoo(ETF, None)
    assert etf["sector"] == "ETF" and etf["industry"] == "Large Blend" and etf["quote_type"] == "ETF"
    assert etf["name"] == "SPDR S&P 500 ETF Trust"
    assert companies.parse_yahoo({"finance": {"error": {"code": "Unauthorized"}}}, None)["ok"] is False
    named = companies.parse_yahoo(None, None, CHART)
    assert named["name"] == "Apple Inc." and named["quote_type"] == "EQUITY" and named["ok"]


def test_merge_prefers_t212_name_then_yahoo_then_fallback():
    yahoo = {"ok": True, "name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics", "quote_type": "EQUITY"}
    both = companies.merge("AAPL", "Apple from 212", yahoo)
    assert both["name"] == "Apple from 212" and both["sector"] == "Technology" and both["source"] == "t212+yahoo"
    assert companies.merge("AAPL", "AAPL", yahoo)["name"] == "Apple Inc."  # ticker is not a company name
    missed = companies.merge("AAPL", None, {"ok": False})
    assert missed["source"] == "fallback" and missed["sector"] == "Technology" and missed["name"] == "Apple Inc."
    etf = companies.merge("SPY", None, {"ok": False})
    assert etf["sector"] == "ETF" and etf["industry"] == "Large Blend"
    # a failed refresh keeps the sector we already stored
    prev = SimpleNamespace(name="Kept", sector="Energy", industry="Oil", quote_type="EQUITY", source="yahoo")
    kept = companies.merge("ZZZZ", None, {"ok": False}, prev)
    assert kept["name"] == "Kept" and kept["sector"] == "Energy" and kept["source"] == "yahoo"
    assert companies.merge("ZZZZ", None, {"ok": False})["source"] == "miss"


def test_yahoo_key_schwab_t212_and_option():
    assert companies.yahoo_key(SimpleNamespace(symbol="BRK.B", underlying="BRK.B", asset_type="STOCK", instrument=None)) == "BRK-B"
    assert companies.yahoo_key(SimpleNamespace(symbol="AAPL", underlying="AAPL", asset_type="STOCK", instrument=None)) == "AAPL"
    inst = SimpleNamespace(price_symbol="VUSA.L", exchange="LSE", symbol="VUSA")
    assert companies.yahoo_key(SimpleNamespace(symbol="VUSA", underlying="VUSA", asset_type="STOCK", instrument=inst)) == "VUSA.L"
    sap = SimpleNamespace(price_symbol=None, exchange="XETRA", symbol="SAP")
    assert companies.yahoo_key(SimpleNamespace(symbol="SAP", underlying="SAP", asset_type="STOCK", instrument=sap)) == "SAP.DE"
    opt = SimpleNamespace(symbol="AAPL  260918C00100000", underlying="AAPL", asset_type="OPTION", instrument=None)
    assert companies.yahoo_key(opt) == "AAPL"


def test_needs_refresh_window():
    now = utcnow()
    fresh = SimpleNamespace(fetched_at=now - timedelta(days=10), name="A", sector="Technology", industry="X", source="yahoo")
    old = SimpleNamespace(fetched_at=now - timedelta(days=91), name="A", sector="Technology", industry="X", source="yahoo")
    miss = SimpleNamespace(fetched_at=now - timedelta(hours=2), name=None, sector=None, industry=None, source="miss")
    miss_due = SimpleNamespace(fetched_at=now - timedelta(days=2), name=None, sector=None, industry=None, source="miss")
    assert companies.needs_refresh(None, now) and companies.needs_refresh(old, now)
    assert not companies.needs_refresh(fresh, now)
    assert not companies.needs_refresh(miss, now) and companies.needs_refresh(miss_due, now)


def test_fill_uses_mock_yahoo_and_skips_a_fresh_row(db):
    calls = []

    def fetch(sym):
        calls.append(sym)
        if sym == "ZZZZ":
            return {"ok": False}
        return {"ok": True, "name": sym + " Co", "sector": "Technology", "industry": "Software", "quote_type": "EQUITY"}

    first = companies.fill_symbols(db, ["MSFT", "ZZZZ"], fetch=fetch, gap=0)
    assert first["filled"] == 1 and first["missed"] == 1 and calls == ["MSFT", "ZZZZ"]
    msft = db.get(CompanyProfile, "MSFT")
    assert msft.name == "MSFT Co" and msft.sector == "Technology" and msft.source == "yahoo"
    assert db.get(CompanyProfile, "ZZZZ").source == "miss"
    companies.fill_symbols(db, ["MSFT", "ZZZZ"], fetch=fetch, gap=0)
    assert calls == ["MSFT", "ZZZZ"]  # fresh success and a same-day miss are not fetched again
    db.get(CompanyProfile, "MSFT").fetched_at = utcnow() - timedelta(days=91)
    db.get(CompanyProfile, "ZZZZ").fetched_at = utcnow() - timedelta(days=2)
    db.commit()
    companies.fill_symbols(db, ["MSFT", "ZZZZ"], fetch=fetch, gap=0)
    assert calls.count("MSFT") == 2 and calls.count("ZZZZ") == 2


def test_fill_keeps_t212_name_and_rate_limits(db):
    a = Account(name="T", broker="trading212", currency="EUR", source_key="t212")
    db.add(a)
    db.flush()
    inst = Instrument(source_key="t212", broker_symbol="AAPL_US_EQ", symbol="AAPL", name="Apple from 212",
                      exchange="US", currency="USD", price_symbol="AAPL", asset_type="STOCK")
    db.add(inst)
    db.flush()
    db.add(_trade(a, "AAPL", 10, instrument_id=inst.id))
    db.commit()
    times = []

    def fetch(sym):
        times.append(time.monotonic())
        return {"ok": True, "name": "Apple Inc." if sym == "AAPL" else "NVDA Co",
                "sector": "Technology", "industry": "Consumer Electronics", "quote_type": "EQUITY"}

    companies.fill_symbols(db, ["AAPL", "NVDA"], fetch=fetch, gap=0.05)
    assert times[1] - times[0] >= 0.04
    row = db.get(CompanyProfile, "AAPL")
    assert row.name == "Apple from 212" and row.sector == "Technology" and row.source == "t212+yahoo"
    assert db.get(CompanyProfile, "NVDA").name == "NVDA Co" and db.get(CompanyProfile, "NVDA").source == "yahoo"


def test_yahoo_client_sends_crumb_and_retries(monkeypatch):
    state = {"crumb": 0, "summary": 0, "chart": 0}

    def handler(request: httpx.Request):
        url = str(request.url)
        if url.startswith("https://fc.yahoo.com") or url.startswith("https://finance.yahoo.com"):
            return httpx.Response(404, text="")
        if "getcrumb" in url:
            state["crumb"] += 1
            return httpx.Response(200, text="c1" if state["crumb"] == 1 else "c2")
        if "quoteSummary" in url:
            state["summary"] += 1
            if request.url.params.get("crumb") != "c2":
                return httpx.Response(401, json={"finance": {"error": {"code": "Unauthorized"}}})
            return httpx.Response(200, json=EQUITY)
        if "/v7/finance/quote" in url:
            if request.url.params.get("crumb") != "c2":
                return httpx.Response(401, json={"finance": {"error": {"code": "Unauthorized"}}})
            return httpx.Response(200, json=QUOTE)
        if "/v8/finance/chart/" in url:
            state["chart"] += 1
            return httpx.Response(200, json=CHART)
        return httpx.Response(404, text="no")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = companies.YahooProfiles(client).fetch("AAPL")
    assert got["name"] == "Apple Inc." and got["sector"] == "Technology"
    assert state["summary"] == 2 and state["crumb"] >= 2 and state["chart"] == 0

    def chart_only(request: httpx.Request):
        url = str(request.url)
        if "getcrumb" in url:
            return httpx.Response(200, text="okcrumb")
        if url.startswith("https://fc.yahoo.com") or url.startswith("https://finance.yahoo.com"):
            return httpx.Response(404, text="")
        if "quoteSummary" in url:
            return httpx.Response(200, json={"quoteSummary": {"result": None}})
        if "/v7/finance/quote" in url:
            return httpx.Response(200, json={"quoteResponse": {"result": []}})
        if "/v8/finance/chart/" in url:
            return httpx.Response(200, json=CHART)
        return httpx.Response(404, text="no")

    named = companies.YahooProfiles(httpx.Client(transport=httpx.MockTransport(chart_only))).fetch("AAPL")
    assert named["name"] == "Apple Inc." and named["quote_type"] == "EQUITY"


def test_migration_creates_company_profiles(tmp_path):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'm.db'}"
    subprocess.check_call([sys.executable, "-m", "app.migrate"], cwd=str(companies.__file__).rsplit("/app/", 1)[0], env=env)
    import sqlite3
    cols = [c[1] for c in sqlite3.connect(tmp_path / "m.db").execute("pragma table_info(company_profiles)")]
    assert "yahoo_symbol" in cols and "sector" in cols and "industry" in cols


@pytest.fixture()
def client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def _trade(account, symbol, net, *, asset="STOCK", underlying=None, currency="USD", instrument_id=None, when=None):
    when = when or datetime(2026, 9, 2, 14, 30)
    return Trade(key=f"{symbol}-{net}-{asset}-{currency}", account_id=account.id, symbol=symbol,
                 underlying=underlying or symbol, asset_type=asset, direction="LONG", status="CLOSED",
                 opened_at=when - timedelta(days=1), closed_at=when, quantity=1, open_quantity=0,
                 entry_price=10, exit_price=11, gross_pnl=net, fees=0, net_pnl=net, currency=currency,
                 instrument_id=instrument_id)


def _profile(symbol, name, sector, industry, source="yahoo"):
    return CompanyProfile(yahoo_symbol=symbol, name=name, sector=sector, industry=industry,
                          quote_type="EQUITY", source=source, fetched_at=utcnow())


def test_trades_list_detail_and_filters(db, client, monkeypatch):
    monkeypatch.setenv("COMPANY_FETCH", "on")
    monkeypatch.setenv("QUOTE_PROVIDER", "yahoo")
    queued = []
    monkeypatch.setattr(companies, "enqueue", lambda syms: queued.extend(syms))
    monkeypatch.setattr(companies.YahooProfiles, "fetch", lambda self, sym: (_ for _ in ()).throw(AssertionError("http")))
    a = Account(name="Schwab", broker="schwab", currency="USD")
    db.add(a)
    db.flush()
    apple = _trade(a, "AAPL", 100)
    oil = _trade(a, "XOM", -40)
    opt = _trade(a, "AAPL  260918C00100000", 25, asset="OPTION", underlying="AAPL")
    db.add_all([apple, oil, opt,
                _profile("AAPL", "Apple Inc.", "Technology", "Consumer Electronics"),
                _profile("XOM", "Exxon Mobil Corporation", "Energy", "Oil & Gas Integrated")])
    db.commit()
    page = client.get("/trades").text
    assert page.count("Apple Inc.") >= 2  # table cell and the phone card
    assert 'class="js-co' in page and "Consumer Electronics" in page
    assert "Oil &amp; Gas Integrated" in page
    assert 'aria-label="Sector"' in page and 'aria-label="Industry"' in page
    assert queued == []  # fresh rows are not fetched, and nothing is fetched on the request
    only = client.get("/trades?sector=Technology").text
    assert "Apple Inc." in only and "Exxon" not in only
    ind = client.get("/trades", params={"industry": "Oil & Gas Integrated"}).text
    assert "Exxon" in ind and "Apple Inc." not in ind
    assert "No trades match" in client.get("/trades?sector=Utilities").text
    detail = client.get(f"/trades/{apple.id}").text
    assert ">AAPL <span" in detail and ">- Apple Inc.</span>" in detail
    assert 'title="Sector">Technology</span>' in detail and 'title="Industry">Consumer Electronics</span>' in detail
    opt_page = client.get(f"/trades/{opt.id}").text
    assert "Apple Inc." in opt_page and 'title="Industry">Consumer Electronics</span>' in opt_page


def test_reports_sector_tables_follow_display_currency(db, client, monkeypatch):
    import app.currency as currency
    monkeypatch.setattr(currency, "_http_get", lambda url: (_ for _ in ()).throw(AssertionError(url)))
    a = Account(name="EUR acct", broker="trading212", currency="EUR", source_key="t212")
    db.add(a)
    db.flush()
    db.add(_trade(a, "SAP", 100, currency="EUR"))
    db.add(_profile("SAP", "SAP SE", "Technology", "Software - Application"))
    today = date.today()
    db.add_all([FxRate(day=date(2026, 9, 2), quote="USD", rate=1.25), FxRate(day=today, quote="USD", rate=1.25)])
    db.commit()
    html = client.get("/reports?tab=instrument").text
    sector = html.split("By sector", 1)[1].split("By industry", 1)[0]
    assert "Technology" in sector and "$125.00" in sector and "100%" in sector
    industry = html.split("By industry", 1)[1]
    assert "Software - Application" in industry and "$125.00" in industry
    assert "No closed trades match" in client.get("/reports?tab=instrument&sector=Energy").text


def test_refresh_button_does_not_fetch_inline(db, client, monkeypatch):
    monkeypatch.setattr(companies.YahooProfiles, "fetch", lambda self, sym: (_ for _ in ()).throw(AssertionError("http")))
    page = client.get("/settings").text
    assert "Refresh company info" in page and 'id="company-info"' in page
    off = client.post("/settings/companies/refresh", follow_redirects=False)
    assert off.status_code == 303
    assert "quotes disabled" in client.get("/settings").text
    monkeypatch.setenv("COMPANY_FETCH", "on")
    monkeypatch.setenv("QUOTE_PROVIDER", "yahoo")
    started = []
    monkeypatch.setattr(companies, "request_full", lambda: started.append(1) or True)
    on = client.post("/settings/companies/refresh", follow_redirects=False)
    assert on.status_code == 303 and started == [1]
    assert "background" in client.get("/settings").text


def test_name_only_row_is_retried_and_funds_count_as_complete():
    now = utcnow()
    name_only = SimpleNamespace(fetched_at=now - timedelta(days=2), name="Visa Inc.", sector=None, industry=None,
                                source="yahoo", quote_type="EQUITY")
    assert companies.needs_refresh(name_only, now)  # a throttled lookup stored only the name: try again
    etf = SimpleNamespace(fetched_at=now - timedelta(days=2), name="Vanguard FTSE All-World", sector="ETF",
                          industry=None, source="yahoo", quote_type="ETF")
    assert not companies.needs_refresh(etf, now)
    gone = SimpleNamespace(fetched_at=now - timedelta(days=2), name=None, sector=None, industry=None,
                           source="yahoo", quote_type="NONE")
    assert not companies.needs_refresh(gone, now)  # delisted: weekly
    gone.fetched_at = now - timedelta(days=8)
    assert companies.needs_refresh(gone, now)


def test_renamed_ticker_uses_new_symbols_profile(db):
    def fetch(sym):
        if sym == "HONA":
            return {"ok": True, "name": "Honeywell", "sector": "Industrials", "industry": "Conglomerates", "quote_type": "EQUITY"}
        return {"ok": True, "name": None, "sector": None, "industry": None, "quote_type": "NONE"}
    companies.fill_symbols(db, ["HONAV"], fetch=fetch, gap=0)
    from app.models import CompanyProfile
    row = db.get(CompanyProfile, "HONAV")
    assert row.sector == "Industrials" and row.industry == "Conglomerates"



def test_trades_list_sorts_by_sector_and_industry(db):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.models import Account, CompanyProfile, Trade
    from datetime import datetime
    a = Account(name="S", broker="schwab", currency="USD")
    db.add(a); db.flush()
    for sym, sec, ind in (("AAA", "Technology", "Software"), ("BBB", "Energy", "Oil"), ("CCC", None, None)):
        db.add(Trade(key=sym, account_id=a.id, symbol=sym, underlying=sym, asset_type="STOCK", direction="LONG",
                     status="CLOSED", opened_at=datetime(2026, 1, 2), closed_at=datetime(2026, 1, 3), quantity=1, net_pnl=1, entry_price=10, exit_price=11))
        if sec:
            db.add(CompanyProfile(yahoo_symbol=sym, name=sym + " Inc", sector=sec, industry=ind, source="yahoo", fetched_at=utcnow()))
    db.commit()
    c = TestClient(create_app()); c.post("/login", data={"password": "test-pass", "next": "/"})
    import re
    order = lambda html: [s for s in re.findall(r'hover:underline">(\w+)</a>', html) if s in ("AAA", "BBB", "CCC")]  # noqa: E731
    assert order(c.get("/trades?sort=sector&dir=asc").text) == ["BBB", "AAA", "CCC"]   # unknown last
    assert order(c.get("/trades?sort=industry&dir=desc").text) == ["AAA", "BBB", "CCC"]
    html = c.get("/trades").text
    assert "sort=sector" in html and "sort=industry" in html


# ---------------------------------------------------------------- finviz
FV_HTML = '''<h2 class="quote-header_ticker-wrapper_company text-lg">
<a class="tab-link" href="#">Dexcom Inc</a></h2>
<a href="screener?v=111&f=sec_healthcare" class="quote-header_category">Healthcare</a>
<a href="screener?v=111&f=ind_medicaldevices" class="quote-header_category" title="Medical Devices"><span class="min-w-0 truncate">Medical Devices</span></a>'''
FV_ETF = FV_HTML.replace("sec_healthcare", "sec_financial").replace(">Healthcare<", ">Financial<").replace(
    "Medical Devices", "Exchange Traded Fund")


def test_parse_finviz():
    got = companies.parse_finviz(FV_HTML)
    assert (got["name"], got["sector"], got["industry"], got["ok"], got["source"]) == (
        "Dexcom Inc", "Healthcare", "Medical Devices", True, "finviz")
    etf = companies.parse_finviz(FV_ETF)
    assert etf["sector"] == "ETF" and etf["quote_type"] == "ETF"
    assert companies.parse_finviz("<html>not found</html>")["ok"] is False


def test_finviz_symbol_and_name_guard():
    assert companies.finviz_symbol("DXCM") == "DXCM"
    assert companies.finviz_symbol("TTE.PA") == "TTE"
    assert companies.finviz_symbol("VUSA.L") is None
    assert companies.name_matches("Reckitt Benckiser", "Rocket Companies Inc") is False
    assert companies.name_matches("Intel", "Intel Corp") is True
    assert companies.name_matches(None, "Anything") is True


class _FakeFv:
    def __init__(self, data):
        self.data, self.calls = data, []

    def fetch(self, t):
        self.calls.append(t)
        return self.data.get(t)


class _FakeY:
    def __init__(self, data=None):
        self.data, self.calls = data or {}, []

    def fetch(self, s):
        self.calls.append(s)
        return self.data.get(s, {"name": None, "ok": False})


def test_profiles_finviz_first_then_fallback():
    dex = companies.parse_finviz(FV_HTML)
    rocket = {"name": "Rocket Companies Inc", "sector": "Financial", "industry": "Mortgage Finance", "ok": True,
              "source": "finviz", "quote_type": "EQUITY"}
    fv, y = _FakeFv({"DXCM": dex, "RKT": rocket}), _FakeY()
    p = companies.Profiles(fv=fv, yahoo=y)
    assert p.fetch("DXCM", "DexCom, Inc.")["sector"] == "Healthcare" and y.calls == []
    rec = companies.merge("DXCM", None, p.fetch("DXCM"))
    assert rec["source"] == "finviz" and rec["industry"] == "Medical Devices"
    miss = p.fetch("RKT", "Reckitt Benckiser")  # wrong company on Finviz -> Yahoo path
    assert miss.get("finviz_tried") and y.calls == ["RKT"]
    p.fetch("VUSA.L")
    assert "VUSA.L" not in fv.calls
    rec = companies.merge("RKT.L", None, {"name": "Reckitt", "ok": True, "finviz_tried": True})
    assert rec["sector"] == "Consumer Defensive" and rec["source"] == "fv-fallback"


def test_finviz_rows_permanent_and_blank_rows_backfilled(monkeypatch):
    monkeypatch.setenv("COMPANY_FETCH", "on")
    monkeypatch.setenv("QUOTE_PROVIDER", "yahoo")
    now = datetime(2026, 10, 15)
    old = now - timedelta(days=400)
    fv = SimpleNamespace(name="X", sector="Technology", industry="Software", quote_type="EQUITY", source="finviz", fetched_at=old)
    assert companies.needs_refresh(fv, now) is False
    blank = SimpleNamespace(name="X", sector=None, industry=None, quote_type="EQUITY", source="yahoo", fetched_at=now)
    assert companies.needs_refresh(blank, now) is True
    tried = SimpleNamespace(name="X", sector=None, industry=None, quote_type="EQUITY", source="fv-yahoo", fetched_at=now)
    assert companies.needs_refresh(tried, now) is False


def test_yahoo_wrong_company_is_dropped():
    y = _FakeY({"RKT": {"name": "Rocket Companies, Inc.", "sector": "Financial Services", "industry": "Mortgage Finance", "ok": True}})
    got = companies.Profiles(fv=_FakeFv({}), yahoo=y).fetch("RKT", "Reckitt Benckiser")
    assert got["sector"] is None and got["name"] is None
