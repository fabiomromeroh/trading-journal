"""Company name, sector and industry, one row per Yahoo symbol.

Stored in ``company_profiles`` and read on the request path. Nothing here calls Yahoo while rendering.
A background thread fills symbols that are missing, empty, or older than 90 days, with a short pause
between requests. Trading 212's instrument name is used when it is a real company name. Otherwise the
name comes from Yahoo (quoteSummary assetProfile / quote longName, then the chart meta). Sector and
industry come from Yahoo. ETFs are sector ``ETF`` plus the fund category when Yahoo has one. A small
built-in map is used only when Yahoo returns nothing. A failed refresh does not wipe a name we already
stored. Set COMPANY_FETCH=off or QUOTE_PROVIDER=none to disable the background fetch.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets, metrics
from app.models import CompanyProfile, Instrument, Trade, utcnow

log = logging.getLogger(__name__)

STALE_DAYS = 90
EMPTY_RETRY = timedelta(days=1)
DELISTED_RETRY = timedelta(days=7)
BATCH = 20
GAP = 0.8  # seconds between Yahoo calls
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_FRESH_SOURCES = {"yahoo", "t212+yahoo", "fallback", "t212+fallback"}

# Used only when Yahoo returns nothing. Sector names match Yahoo's assetProfile labels.
# (name, sector, industry)
FALLBACK: dict[str, tuple[str, str, str]] = {
    "AAPL": ("Apple Inc.", "Technology", "Consumer Electronics"),
    "MSFT": ("Microsoft Corporation", "Technology", "Software - Infrastructure"),
    "NVDA": ("NVIDIA Corporation", "Technology", "Semiconductors"),
    "AVGO": ("Broadcom Inc.", "Technology", "Semiconductors"),
    "AMD": ("Advanced Micro Devices, Inc.", "Technology", "Semiconductors"),
    "INTC": ("Intel Corporation", "Technology", "Semiconductors"),
    "ORCL": ("Oracle Corporation", "Technology", "Software - Infrastructure"),
    "CRM": ("Salesforce, Inc.", "Technology", "Software - Application"),
    "GOOGL": ("Alphabet Inc.", "Communication Services", "Internet Content & Information"),
    "GOOG": ("Alphabet Inc.", "Communication Services", "Internet Content & Information"),
    "META": ("Meta Platforms, Inc.", "Communication Services", "Internet Content & Information"),
    "NFLX": ("Netflix, Inc.", "Communication Services", "Entertainment"),
    "DIS": ("The Walt Disney Company", "Communication Services", "Entertainment"),
    "AMZN": ("Amazon.com, Inc.", "Consumer Cyclical", "Internet Retail"),
    "TSLA": ("Tesla, Inc.", "Consumer Cyclical", "Auto Manufacturers"),
    "KO": ("The Coca-Cola Company", "Consumer Defensive", "Beverages - Non-Alcoholic"),
    "PEP": ("PepsiCo, Inc.", "Consumer Defensive", "Beverages - Non-Alcoholic"),
    "COST": ("Costco Wholesale Corporation", "Consumer Defensive", "Discount Stores"),
    "JPM": ("JPMorgan Chase & Co.", "Financial Services", "Banks - Diversified"),
    "BAC": ("Bank of America Corporation", "Financial Services", "Banks - Diversified"),
    "V": ("Visa Inc.", "Financial Services", "Credit Services"),
    "MA": ("Mastercard Incorporated", "Financial Services", "Credit Services"),
    "GS": ("The Goldman Sachs Group, Inc.", "Financial Services", "Capital Markets"),
    "BRK-B": ("Berkshire Hathaway Inc.", "Financial Services", "Insurance - Diversified"),
    "BRK-A": ("Berkshire Hathaway Inc.", "Financial Services", "Insurance - Diversified"),
    "UNH": ("UnitedHealth Group Incorporated", "Healthcare", "Healthcare Plans"),
    "LLY": ("Eli Lilly and Company", "Healthcare", "Drug Manufacturers - General"),
    "JNJ": ("Johnson & Johnson", "Healthcare", "Drug Manufacturers - General"),
    "XOM": ("Exxon Mobil Corporation", "Energy", "Oil & Gas Integrated"),
    "BA": ("The Boeing Company", "Industrials", "Aerospace & Defense"),
    "SPY": ("SPDR S&P 500 ETF Trust", "ETF", "Large Blend"),
    "QQQ": ("Invesco QQQ Trust", "ETF", "Large Growth"),
    "IWM": ("iShares Russell 2000 ETF", "ETF", "Small Blend"),
    "VUSA.L": ("Vanguard S&P 500 UCITS ETF", "ETF", "Large Blend"),
    "SAP.DE": ("SAP SE", "Technology", "Software - Application"),
    "ASML.AS": ("ASML Holding N.V.", "Technology", "Semiconductor Equipment & Materials"),
    "AZN.L": ("AstraZeneca PLC", "Healthcare", "Drug Manufacturers - General"),
    "SHEL.L": ("Shell plc", "Energy", "Oil & Gas Integrated"),
    "MC.PA": ("LVMH Moët Hennessy Louis Vuitton", "Consumer Cyclical", "Luxury Goods"),
}


def enabled() -> bool:
    if os.environ.get("COMPANY_FETCH", "on").lower() in ("none", "off", "0", ""):
        return False
    return os.environ.get("QUOTE_PROVIDER", "yahoo").lower() not in ("none", "off", "0", "")


def yahoo_key(t) -> str:
    """Exact Yahoo symbol for the company behind a trade (the underlying, for an option)."""
    inst = getattr(t, "instrument", None)
    asset = getattr(t, "asset_type", None) or "STOCK"
    if asset != "OPTION" and inst is not None and getattr(inst, "price_symbol", None):
        return str(inst.price_symbol).strip()[:64]
    raw = (getattr(t, "underlying", None) or getattr(t, "symbol", None) or "").strip()
    if asset != "OPTION" and inst is not None and getattr(inst, "exchange", None):
        mapped = markets.yahoo_symbol_for(raw, inst.exchange)
        if mapped:
            return mapped[:64]
    return raw.replace(".", "-").replace("/", "-")[:64]


def complete(row) -> bool:
    """Name, sector and industry known (a fund has no industry unless Yahoo gives a category)."""
    if row is None or not (row.name and row.sector):
        return False
    return bool(getattr(row, "industry", None)) or (getattr(row, "quote_type", None) or "").upper() in ("ETF", "MUTUALFUND") or row.sector == "ETF"


def needs_refresh(row, now=None) -> bool:
    """True when the row is missing, older than 90 days, or incomplete and due for another try.
    Incomplete rows (a throttled or failed lookup stored only a name) are retried after a day, and a
    ticker Yahoo no longer knows (delisted) after a week, so nothing stays empty for good by accident."""
    now = now or utcnow()
    if row is None or row.fetched_at is None:
        return True
    age = now - row.fetched_at
    if age >= timedelta(days=STALE_DAYS):
        return True
    if complete(row):
        return False
    if (getattr(row, "quote_type", None) or "").upper() == "NONE":
        return age >= DELISTED_RETRY
    return age >= EMPTY_RETRY


def _alias(symbol: str) -> str | None:
    """The verified new ticker of a renamed one (symbols.BUILTIN, e.g. HONAV -> HONA)."""
    from app.symbols import BUILTIN
    hit = BUILTIN.get(symbol) or BUILTIN.get((symbol or "").upper())
    return hit[0] if hit else None


def fallback_for(symbol: str) -> tuple[str, str, str] | None:
    if not symbol:
        return None
    if symbol in FALLBACK:
        return FALLBACK[symbol]
    up = symbol.upper()
    return FALLBACK.get(up)


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _real_name(name: str | None, *tickers: str | None) -> str | None:
    text = _clean(name)
    if not text or len(text) < 2:
        return None
    folded = text.upper()
    for ticker in tickers:
        if not ticker:
            continue
        t = ticker.strip().upper()
        base = t.split(".")[0].replace("-", ".")
        if folded in (t, base, t.replace("-", "."), t.replace(".", "-")):
            return None
    return text


def _t212_name(inst) -> str | None:
    if inst is None or getattr(inst, "source_key", None) != "t212":
        return None
    return _real_name(getattr(inst, "name", None), getattr(inst, "symbol", None), getattr(inst, "price_symbol", None))


def _first(payload, key):
    if not isinstance(payload, dict):
        return None
    block = payload.get(key)
    if not isinstance(block, dict):
        return None
    res = block.get("result")
    if isinstance(res, list):
        return res[0] if res else None
    if isinstance(res, dict):
        return res
    return None


def _txt(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def parse_yahoo(summary=None, quote=None, chart=None) -> dict:
    """Pull name / sector / industry out of Yahoo JSON. ``ok`` is false when nothing usable came back."""
    mod = _first(summary, "quoteSummary") or {}
    if not isinstance(mod, dict):
        mod = {}
    ap = mod.get("assetProfile") if isinstance(mod.get("assetProfile"), dict) else {}
    qt = mod.get("quoteType") if isinstance(mod.get("quoteType"), dict) else {}
    fp = mod.get("fundProfile") if isinstance(mod.get("fundProfile"), dict) else {}
    q = _first(quote, "quoteResponse") or {}
    if not isinstance(q, dict):
        q = {}
    meta = {}
    ch = _first(chart, "chart") if chart else None
    if isinstance(ch, dict) and isinstance(ch.get("meta"), dict):
        meta = ch["meta"]
    qtype = _txt(qt.get("quoteType") or q.get("quoteType") or meta.get("instrumentType"))
    qtype = qtype.upper() if qtype else None
    name = _txt(qt.get("longName") or q.get("longName") or meta.get("longName")
                or qt.get("shortName") or q.get("shortName") or meta.get("shortName"))
    if qtype == "ETF":
        sector = "ETF"
        industry = _txt(fp.get("categoryName") or ap.get("category"))
    else:
        sector = _txt(ap.get("sector"))
        industry = _txt(ap.get("industry"))
    ok = bool(name or sector or industry or qtype)
    return {"name": name, "sector": sector, "industry": industry, "quote_type": qtype, "ok": ok}


def merge(symbol: str, t212_name: str | None, yahoo: dict | None, previous=None) -> dict:
    """Combine a Trading 212 name, a Yahoo parse, and (only if Yahoo failed) the built-in map."""
    y = yahoo or {}
    ok = bool(y.get("ok"))
    name = _real_name(t212_name, symbol)
    sector = industry = qtype = None
    source = "miss"
    if ok:
        name = name or _clean(y.get("name"))
        sector, industry, qtype = y.get("sector"), y.get("industry"), y.get("quote_type")
        source = "t212+yahoo" if _real_name(t212_name, symbol) else "yahoo"
    else:
        fb = fallback_for(symbol)
        if fb:
            name = name or fb[0]
            sector, industry = fb[1], fb[2]
            qtype = "ETF" if sector == "ETF" else "EQUITY"
            source = "t212+fallback" if _real_name(t212_name, symbol) else "fallback"
        elif name:
            source = "t212"
    if previous is not None:
        if not name and previous.name:
            name = previous.name
        if not sector and previous.sector:
            sector = previous.sector
            industry = industry or previous.industry
            qtype = qtype or previous.quote_type
            if source in ("miss", "t212") and previous.source:
                source = previous.source
    return {"name": _clip(name, 160), "sector": _clip(sector, 80), "industry": _clip(industry, 120),
            "quote_type": _clip(qtype, 20), "source": _clip(source, 20)}


def _clip(value, n: int) -> str | None:
    if not value:
        return None
    return str(value)[:n]


def _index(db: Session):
    """yahoo symbol -> Trading 212 name (or None), and yahoo symbol -> trade ids."""
    rows = db.execute(
        select(Trade.id, Trade.symbol.label("tsym"), Trade.underlying, Trade.asset_type,
               Instrument.price_symbol, Instrument.exchange, Instrument.name.label("iname"),
               Instrument.symbol.label("isym"), Instrument.source_key)
        .outerjoin(Instrument, Instrument.id == Trade.instrument_id)).all()
    names: dict[str, str | None] = {}
    ids: dict[str, list[int]] = {}
    for r in rows:
        inst = None
        if r.price_symbol or r.exchange or r.source_key or r.iname:
            inst = SimpleNamespace(price_symbol=r.price_symbol, exchange=r.exchange, name=r.iname,
                                   symbol=r.isym, source_key=r.source_key)
        key = yahoo_key(SimpleNamespace(symbol=r.tsym, underlying=r.underlying, asset_type=r.asset_type, instrument=inst))
        if not key:
            continue
        ids.setdefault(key, []).append(r.id)
        nm = _t212_name(inst)
        if key not in names or (not names[key] and nm):
            names[key] = nm
    return names, ids


def matching_ids(db: Session, sector: str | None, industry: str | None) -> list[int] | None:
    """Trade ids in this sector and/or industry. None when the filter is not set."""
    sector = (sector or "").strip()
    industry = (industry or "").strip()
    if not sector and not industry:
        return None
    q = select(CompanyProfile.yahoo_symbol)
    if sector:
        q = q.where(CompanyProfile.sector == sector)
    if industry:
        q = q.where(CompanyProfile.industry == industry)
    wanted = set(db.scalars(q))
    if not wanted:
        return []
    _names, ids = _index(db)
    out: list[int] = []
    for key, trade_ids in ids.items():
        if key in wanted:
            out.extend(trade_ids)
    return out


def restrict(stmt, db: Session | None, q):
    if db is None:
        return stmt
    ids = matching_ids(db, q.get("sector"), q.get("industry"))
    if ids is None:
        return stmt
    return stmt.where(Trade.id.in_(ids or [-1]))


def sort_expr(db: Session, field: str):
    """SQL expression with each trade's sector / industry (for ORDER BY); NULL when unknown."""
    from sqlalchemy import case, literal
    _names, ids = _index(db)
    prof = {r.yahoo_symbol: getattr(r, field) for r in db.scalars(select(CompanyProfile))}
    whens = {tid: prof.get(key) for key, tids in ids.items() if prof.get(key) for tid in tids}
    if not whens:
        return literal(None)
    return case(whens, value=Trade.id, else_=None)


def choices(db: Session) -> tuple[list[str], list[str]]:
    sectors = [s for s in db.scalars(select(CompanyProfile.sector).where(CompanyProfile.sector.is_not(None)).distinct()) if s]
    industries = [s for s in db.scalars(select(CompanyProfile.industry).where(CompanyProfile.industry.is_not(None)).distinct()) if s]
    key = lambda s: s.lower()  # noqa: E731
    return sorted(set(sectors), key=key), sorted(set(industries), key=key)


def annotate(db: Session, trades) -> None:
    """Attach ``.company`` from the cache. Missing symbols are queued; this does not call Yahoo."""
    if not trades:
        return
    keys = []
    for t in trades:
        keys.append(yahoo_key(t))
    uniq = [k for k in dict.fromkeys(keys) if k]
    rows = {}
    if uniq:
        rows = {r.yahoo_symbol: r for r in db.scalars(select(CompanyProfile).where(CompanyProfile.yahoo_symbol.in_(uniq)))}
    now = utcnow()
    stale = []
    for t, key in zip(trades, keys):
        row = rows.get(key) if key else None
        t.company = row
        if key and needs_refresh(row, now):
            stale.append(key)
    if stale:
        enqueue(stale)


def _label(t, attr: str) -> str:
    c = getattr(t, "company", None)
    return (getattr(c, attr, None) if c is not None else None) or "(unknown)"


def breakdown(trades) -> tuple[list[dict], list[dict]]:
    """Closed-trade rows for sector and industry (P&L is whatever currency is already on the trade)."""
    closed = metrics.closed_sorted(trades)
    return (metrics.group(closed, lambda t: _label(t, "sector"), sort="net"),
            metrics.group(closed, lambda t: _label(t, "industry"), sort="net"))


def stats(db: Session) -> dict:
    names, _ids = _index(db)
    if not names:
        return {"symbols": 0, "cached": 0, "missing": 0}
    rows = list(db.scalars(select(CompanyProfile).where(CompanyProfile.yahoo_symbol.in_(list(names)))))
    now = utcnow()
    cached = sum(1 for r in rows if not needs_refresh(r, now))
    return {"symbols": len(names), "cached": cached, "missing": len(names) - cached}


def _save(db: Session, symbol: str, rec: dict, now) -> None:
    row = db.get(CompanyProfile, symbol)
    if row is None:
        row = CompanyProfile(yahoo_symbol=symbol)
        db.add(row)
    row.name = rec["name"]
    row.sector = rec["sector"]
    row.industry = rec["industry"]
    row.quote_type = rec["quote_type"]
    row.source = rec["source"]
    row.fetched_at = now


def fill_symbols(db: Session, symbols: list[str] | None = None, *, fetch=None, gap: float | None = None, now=None) -> dict:
    """Fill missing or stale profiles. ``fetch(symbol) -> dict`` is the Yahoo parse (tests pass a fake).

    The real fetch is used only when company info is enabled. Commits after each symbol so a long
    run does not hold one open transaction, and sleeps ``gap`` seconds between Yahoo calls.
    """
    now = now or utcnow()
    names, _ids = _index(db)
    if symbols is None:
        todo = list(names)
    else:
        todo = [s for s in dict.fromkeys(symbols) if s]
        for s in todo:
            names.setdefault(s, None)
    existing = {}
    if todo:
        existing = {r.yahoo_symbol: r for r in db.scalars(
            select(CompanyProfile).where(CompanyProfile.yahoo_symbol.in_(todo)))}
    pending = [s for s in todo if needs_refresh(existing.get(s), now)]
    if not pending:
        return {"checked": len(todo), "filled": 0, "missed": 0, "skipped": len(todo)}
    client = None
    if fetch is None:
        if not enabled():
            return {"checked": len(todo), "filled": 0, "missed": 0, "skipped": len(todo)}
        client = YahooProfiles()
        fetch = client.fetch
        if gap is None:
            gap = GAP
    elif gap is None:
        gap = 0.0
    filled = missed = 0
    try:
        for i, sym in enumerate(pending):
            if i and gap:
                time.sleep(gap)
            if i and gap and i % BATCH == 0:
                time.sleep(gap)
            try:
                yahoo = fetch(sym)
                alias = _alias(sym)
                if alias and not (isinstance(yahoo, dict) and yahoo.get("sector")):
                    via = fetch(alias)  # renamed ticker: the new symbol's profile (same company)
                    if isinstance(via, dict) and (via.get("sector") or via.get("name")):
                        yahoo = via
            except Exception as exc:  # one symbol must not stop the batch
                log.info("company lookup failed: %s", type(exc).__name__)
                yahoo = None
            if not isinstance(yahoo, dict):
                yahoo = None
            prev = existing.get(sym)
            rec = merge(sym, names.get(sym), yahoo, prev)
            _save(db, sym, rec, now)
            db.commit()
            saved = db.get(CompanyProfile, sym)
            if saved is not None:
                existing[sym] = saved
            if rec["source"] == "miss" or not (rec["name"] or rec["sector"]):
                missed += 1
            else:
                filled += 1
    finally:
        if client is not None:
            client.close()
    if symbols is None:
        log.info("company info: filled %s, missed %s, skipped %s", filled, missed, len(todo) - len(pending))
    return {"checked": len(todo), "filled": filled, "missed": missed, "skipped": len(todo) - len(pending)}


def _crumb_ok(text: str) -> str | None:
    crumb = (text or "").strip().strip('"')
    if not crumb or len(crumb) > 80 or "<" in crumb or " " in crumb:
        return None
    return crumb


def _unauthorized(payload, status: int) -> bool:
    if status in (401, 403):
        return True
    if not isinstance(payload, dict):
        return False
    err = (payload.get("finance") or {}).get("error") if isinstance(payload.get("finance"), dict) else None
    if isinstance(err, dict) and "unauth" in str(err.get("code", "")).lower():
        return True
    for key in ("quoteSummary", "quoteResponse"):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        e = block.get("error")
        if isinstance(e, dict) and ("crumb" in str(e.get("description", "")).lower() or str(e.get("code", "")).lower() == "unauthorized"):
            return True
    return False


class YahooProfiles:
    """quoteSummary + quote, with the cookie/crumb Yahoo started requiring. Chart meta is the name fallback."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(headers={"User-Agent": _UA}, timeout=12, follow_redirects=True)
        self._own = http is None
        self.crumb: str | None = None

    def close(self) -> None:
        if self._own:
            self.http.close()

    def _ensure_crumb(self, force: bool = False) -> str | None:
        if self.crumb and not force:
            return self.crumb
        for url in ("https://fc.yahoo.com", "https://finance.yahoo.com"):
            try:
                self.http.get(url)
            except httpx.HTTPError:
                continue
        try:
            r = self.http.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        except httpx.HTTPError:
            return self.crumb
        crumb = _crumb_ok(r.text) if r.status_code == 200 else None
        if crumb:
            self.crumb = crumb
        return self.crumb

    def _json(self, url: str, params: dict) -> tuple[int, dict | None]:
        try:
            r = self.http.get(url, params=params)
        except httpx.HTTPError:
            return 0, None
        if not r.content:
            return r.status_code, None
        try:
            data = r.json()
        except ValueError:
            return r.status_code, None
        return r.status_code, data if isinstance(data, dict) else None

    def _summary_and_quote(self, symbol: str) -> tuple[dict | None, dict | None]:
        crumb = self._ensure_crumb()
        summary, quote = self._modules(symbol, crumb)
        if _unauthorized(summary, 0) or _unauthorized(quote, 0) or (summary is None and quote is None):
            crumb = self._ensure_crumb(force=True)
            summary, quote = self._modules(symbol, crumb)
        return summary, quote

    def _modules(self, symbol: str, crumb: str | None) -> tuple[dict | None, dict | None]:
        path = quote(symbol, safe="")
        params = {"modules": "assetProfile,quoteType,fundProfile"}
        qparams = {"symbols": symbol}
        if crumb:
            params["crumb"] = crumb
            qparams["crumb"] = crumb
        st, summary = self._json(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{path}", params)
        if _unauthorized(summary, st):
            summary = {"finance": {"error": {"code": "Unauthorized"}}}
        stq, q = self._json("https://query1.finance.yahoo.com/v7/finance/quote", qparams)
        if _unauthorized(q, stq):
            q = {"finance": {"error": {"code": "Unauthorized"}}}
        return summary, q

    def _chart(self, symbol: str) -> dict | None:
        path = quote(symbol, safe="")
        _st, data = self._json(f"https://query1.finance.yahoo.com/v8/finance/chart/{path}",
                               {"interval": "1d", "range": "5d"})
        return data

    def fetch(self, symbol: str) -> dict:
        summary, quote = self._summary_and_quote(symbol)
        parsed = parse_yahoo(summary, quote, None)
        if not parsed.get("name"):
            parsed = parse_yahoo(summary, quote, self._chart(symbol))
        return parsed


# -------------------------------------------------------------------------- background
_cv = threading.Condition()
_pending: set[str] = set()
_full = False
_started = False
_stop = False


def enqueue(symbols) -> None:
    if not symbols or not enabled():
        return
    with _cv:
        _pending.update(s for s in symbols if s)
        _cv.notify()
    _ensure()


def request_full() -> bool:
    """Queue a pass over every missing or stale symbol. False when fetching is disabled."""
    if not enabled():
        return False
    global _full
    with _cv:
        _full = True
        _cv.notify()
    _ensure()
    return True


def stop() -> None:
    global _stop
    with _cv:
        _stop = True
        _cv.notify()


def _ensure() -> None:
    global _started
    with _cv:
        if _started or _stop:
            return
        _started = True
    threading.Thread(target=_loop, name="company-info", daemon=True).start()


def _loop() -> None:
    global _started, _full
    attempts = 0
    while True:
        with _cv:
            if _stop:
                _started = False
                return
            full = _full
            _full = False
            batch = list(_pending)[:BATCH]
            for s in batch:
                _pending.discard(s)
            if not full and not batch:
                _cv.wait(timeout=5)
                continue
        try:
            from app.db import SessionLocal
            db = SessionLocal()
            try:
                fill_symbols(db, None if full else batch)
            finally:
                db.close()
            attempts = 0
        except Exception as exc:  # noqa: BLE001 - keep the site up; the next pass retries
            attempts += 1
            log.warning("company info refresh failed: %s", type(exc).__name__)
            if full and attempts < 4 and not _stop:
                with _cv:
                    _full = True
                time.sleep(3 * attempts)
