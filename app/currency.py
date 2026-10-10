"""Display-currency conversion (USD <-> EUR <-> GBP ...) for the combined / per-account views.

Method (also shown in tooltips):
  * ECB euro reference rates from the free Frankfurter API (https://api.frankfurter.dev/v1), cached in ``fx_rates``.
    Weekends/holidays use the last published business day.
  * A CLOSED trade is converted with the rate of its close date; an OPEN trade (realized-so-far and unrealized)
    with the latest rate. Every amount of a trade (fills, P&L, fees, stop, risk) uses the same rate, so R multiples,
    win rates and percentages are unchanged by the conversion.
  * Trades already in the display currency are returned untouched (the Schwab/USD path stays byte-for-byte identical).
  * If no rate is available the trade is shown in its own currency and a banner says so.
Conversion is a read-only view: the database keeps native amounts."""
from __future__ import annotations

import bisect
import json
import time
import urllib.request
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, AppState, FxRate, Trade

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF ", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
CHOICES = ("USD", "EUR")
API = "https://api.frankfurter.dev/v1"
CHUNK_DAYS = 80          # Frankfurter returns weekly points for ranges over ~90 days
_last_try: dict[str, float] = {}


def symbol(ccy: str | None) -> str:
    return SYMBOLS.get((ccy or "USD").upper(), f"{(ccy or '').upper()} ")


# ------------------------------------------------------------------ display choice (remembered)
def get_display(db: Session) -> str:
    v = db.scalar(select(AppState.value).where(AppState.key == "display_currency"))
    return v if v in CHOICES else "USD"


def set_display(db: Session, ccy: str) -> str:
    ccy = ccy.upper()
    if ccy not in CHOICES:
        raise ValueError(f"unsupported display currency {ccy}")
    row = db.get(AppState, "display_currency")
    if row:
        row.value = ccy
    else:
        db.add(AppState(key="display_currency", value=ccy))
    db.commit()
    return ccy


# ------------------------------------------------------------------ rates
def _http_get(url: str) -> dict:  # patched in tests
    req = urllib.request.Request(url, headers={"User-Agent": "trading-journal/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https endpoint
        return json.loads(r.read().decode())


def fetch_range(db: Session, quotes: list[str], start: date, end: date, http=None) -> int:
    """Download ECB rates for ``quotes`` (per 1 EUR) between the dates and cache them. Returns rows stored."""
    http = http or _http_get
    quotes = sorted({q.upper() for q in quotes if q and q.upper() != "EUR"})
    if not quotes:
        return 0
    have = {(r.day, r.quote) for r in db.scalars(select(FxRate).where(FxRate.day >= start, FxRate.day <= end))}
    n, cur = 0, start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS), end)
        data = http(f"{API}/{cur.isoformat()}..{stop.isoformat()}?base=EUR&symbols={','.join(quotes)}")
        for d, rates in (data.get("rates") or {}).items():
            day = date.fromisoformat(d)
            for q, r in rates.items():
                if (day, q) not in have and r:
                    db.add(FxRate(day=day, quote=q, rate=float(r)))
                    have.add((day, q))
                    n += 1
        cur = stop + timedelta(days=1)
    db.commit()
    return n


class RateBook:
    """In-memory view of ``fx_rates`` with lazy network top-up (at most one attempt per currency per 30 min)."""

    def __init__(self, db: Session, http=None):
        self.db, self.http = db, http
        self.data: dict[str, tuple[list[date], list[float]]] = {}
        self._load()

    def _load(self):
        rows = self.db.execute(select(FxRate.quote, FxRate.day, FxRate.rate).order_by(FxRate.quote, FxRate.day)).all()
        self.data = {}
        for q, d, r in rows:
            days, rates = self.data.setdefault(q, ([], []))
            days.append(d)
            rates.append(r)

    def covers(self, ccy: str, start: date, end: date) -> bool:
        if ccy == "EUR":
            return True
        days = self.data.get(ccy, ([], []))[0]
        return bool(days) and days[0] <= start + timedelta(days=5) and days[-1] >= end - timedelta(days=5)

    def ensure(self, ccys, start: date, end: date, fetch: bool = True) -> None:
        end = min(end, datetime.now().date())
        need = [c for c in {c.upper() for c in ccys if c} if c != "EUR" and not self.covers(c, start, end)]
        if not need or not fetch:
            return
        key = ",".join(sorted(need))
        if time.time() - _last_try.get(key, 0) < 1800:
            return
        _last_try[key] = time.time()
        try:
            fetch_range(self.db, need, start - timedelta(days=7), end, self.http)
        except Exception:  # noqa: BLE001 - offline / API trouble -> native-currency fallback + banner
            self.db.rollback()
        self._load()

    def per_eur(self, ccy: str, day: date) -> float | None:
        ccy = (ccy or "USD").upper()
        if ccy == "EUR":
            return 1.0
        days, rates = self.data.get(ccy, ([], []))
        if not days:
            return None
        i = bisect.bisect_right(days, day) - 1
        if i < 0:
            i = 0 if (days[0] - day).days <= 7 else -1
        return rates[i] if i >= 0 else None

    def rate(self, frm: str, to: str, day: date) -> float | None:
        """Multiply an amount in ``frm`` by this to get ``to`` (None when a rate is missing)."""
        frm, to = (frm or "USD").upper(), (to or "USD").upper()
        if frm == to:
            return 1.0
        a, b = self.per_eur(frm, day), self.per_eur(to, day)
        return None if not a or not b else b / a


# ------------------------------------------------------------------ converted view of trades
MONEY_TRADE = {"entry_price", "exit_price", "cost_basis", "gross_pnl", "fees", "net_pnl", "mfe", "mae", "initial_stop",
               "risk_amount", "profit_target", "stop_raw", "stop_prev"}
MONEY_FILL = {"price", "fees"}


class CFill:
    def __init__(self, f, rate: float):
        self._f, self._r = f, rate

    def __getattr__(self, n):
        v = getattr(self._f, n)
        return v * self._r if n in MONEY_FILL and v is not None else v


class CTrade:
    """A Trade seen in another currency: money and price fields scaled by one rate, everything else delegated."""

    def __init__(self, t: Trade, rate: float, native: str, shown: str):
        self.__dict__["_t"], self.__dict__["_r"] = t, rate
        self.__dict__["native_currency"], self.__dict__["shown_currency"] = native, shown
        self.__dict__["fx_rate"] = rate
        self.__dict__["_fills"] = None

    def __getattr__(self, n):
        t = self.__dict__["_t"]
        if n == "fills":
            if self.__dict__["_fills"] is None:
                self.__dict__["_fills"] = [CFill(f, self._r) for f in t.fills]
            return self.__dict__["_fills"]
        v = getattr(t, n)
        return v * self._r if n in MONEY_TRADE and v is not None else v

    def __setattr__(self, n, v):
        self.__dict__[n] = v

    def __eq__(self, o):
        return getattr(o, "id", None) == self._t.id if isinstance(o, CTrade) else self._t is o

    def __hash__(self):
        return hash(self._t)


def trade_ccy(t) -> str:
    return (t.currency or (t.account.currency if t.account is not None else None) or "USD").upper()


def raw(t):
    return t._t if isinstance(t, CTrade) else t


def view(db: Session, trades: list, display: str | None = None, http=None) -> list:
    """Trades in the display currency (see module doc). Unchanged list when nothing needs converting."""
    trades = list(trades)
    display = display or get_display(db)
    ccys = {trade_ccy(t) for t in trades}
    if ccys <= {display}:
        return trades
    today = datetime.now().date()
    days = [(t.closed_at or t.opened_at).date() for t in trades if trade_ccy(t) != display and t.closed_at]
    book = RateBook(db, http)
    book.ensure(ccys | {display}, min(days) if days else today - timedelta(days=7), today)
    out = []
    for t in trades:
        c = trade_ccy(t)
        if c == display:
            out.append(t)
            continue
        day = t.closed_at.date() if (t.status == "CLOSED" and t.closed_at) else today
        r = book.rate(c, display, day)
        out.append(t if r is None else CTrade(t, r, c, display))
    return out


def problem(db: Session, display: str) -> str | None:
    """A banner text when some trade currency has no cached rate to the display currency."""
    ccys = {(c or "USD") for c in db.scalars(select(func.distinct(func.coalesce(Trade.currency, Account.currency)))
                                             .select_from(Trade).join(Account, Account.id == Trade.account_id))}
    foreign = sorted(c for c in ccys if c != display)
    if not foreign:
        return None
    book = RateBook(db)
    miss = [c for c in foreign if book.per_eur(c, datetime.now().date()) is None
            or (display != "EUR" and book.per_eur(display, datetime.now().date()) is None)]
    if not miss:
        return None
    return (f"No FX rates cached for {', '.join(miss)} → {display} (ECB/Frankfurter unreachable?). "
            f"Those trades are shown in their own currency, so combined totals mix currencies.")


def native_money_label(ccy: str) -> str:
    return symbol(ccy)


def needs_conversion(db: Session, display: str | None = None) -> bool:
    """True when some stored trade is not in the display currency (otherwise every view is the untouched native one)."""
    display = display or get_display(db)
    q = (select(func.count(Trade.id)).select_from(Trade).join(Account, Account.id == Trade.account_id)
         .where(func.coalesce(Trade.currency, Account.currency) != display))
    return bool(db.scalar(q))
