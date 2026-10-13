"""Display-currency conversion (USD <-> EUR <-> GBP ...) for the combined / per-account views.

Method (also the tooltip text): converted at the rate on each trade date; open positions at the latest rate.

  * Realized gross, net and fees: each fill is converted on its own execution date.
    Instrument currency -> account currency uses the broker rate stored on the fill (``fx_to_account``,
    Trading 212's fxRate) when there is one, otherwise the ECB reference rate that day.
    Account currency -> display currency uses the ECB rate that same day.
    FIFO P&L is (exit price x exit rate - entry price x entry rate) x qty, so two currencies are never added raw.
  * Unrealized P&L and open market value use the latest ECB chain (not a historical broker rate).
  * MFE/MAE use the close-date chain for a closed trade and the latest rate while the trade is open.
  * Prices (entry, exit, stop, targets, chart) are not converted. R and return % stay in instrument terms.
  * A trade already in the display currency is returned untouched (the Schwab/USD path stays identical).
  * If a rate is missing the trade stays in its own currency and a banner says so.
ECB rates: Frankfurter (https://api.frankfurter.dev/v1), cached in ``fx_rates``. Weekends use the previous business day.
Conversion is a read-only view: the database keeps native amounts."""
from __future__ import annotations

import bisect
import json
import threading
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
_FX_TTL = 60.0           # in-memory copy of fx_rates; a row-count/max-day stamp drops it when new rates land
_fx_lock = threading.Lock()
_fx_mem: dict[str, tuple] = {}


def _fx_key(db: Session) -> str:
    try:
        return str(db.get_bind().url)
    except Exception:  # noqa: BLE001
        return "fx"


def symbol(ccy: str | None) -> str:
    return SYMBOLS.get((ccy or "USD").upper(), f"{(ccy or '').upper()} ")


# ------------------------------------------------------------------ display choice (remembered)
# The account the current request shows (header switcher cookie or ?account=), set by middleware.
# One account selected: its own saved choice, else its account currency (Trading 212 EUR, Schwab USD).
# All: the global choice, as before.
from contextvars import ContextVar

VIEW_ACCOUNT: ContextVar[int | None] = ContextVar("view_account", default=None)
ACCT_KEY = "display_currency:acct:"


def get_display(db: Session, account_id: int | None = None) -> str:
    aid = account_id if account_id is not None else VIEW_ACCOUNT.get()
    if aid:
        v = db.scalar(select(AppState.value).where(AppState.key == ACCT_KEY + str(aid)))
        if v in CHOICES:
            return v
        from app.models import Account
        acct = db.get(Account, aid)
        if acct is not None and (acct.currency or "").upper() in CHOICES:
            return acct.currency.upper()
    v = db.scalar(select(AppState.value).where(AppState.key == "display_currency"))
    return v if v in CHOICES else "USD"


def set_display(db: Session, ccy: str, account_id: int | None = None) -> str:
    """Manual toggle: remembered for the selected account only, or globally when All is shown."""
    ccy = ccy.upper()
    if ccy not in CHOICES:
        raise ValueError(f"unsupported display currency {ccy}")
    key = ACCT_KEY + str(account_id) if account_id else "display_currency"
    row = db.get(AppState, key)
    if row:
        row.value = ccy
    else:
        db.add(AppState(key=key, value=ccy))
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
        key = _fx_key(self.db)
        n, last = self.db.execute(select(func.count(), func.max(FxRate.day))).one()
        stamp = (int(n or 0), last)
        now = time.monotonic()
        with _fx_lock:
            hit = _fx_mem.get(key)
            if hit and hit[0] == stamp and now - hit[2] < _FX_TTL:
                self.data = hit[1]
                return
        rows = self.db.execute(select(FxRate.quote, FxRate.day, FxRate.rate).order_by(FxRate.quote, FxRate.day)).all()
        data: dict[str, tuple[list[date], list[float]]] = {}
        for q, d, r in rows:
            days, rates = data.setdefault(q, ([], []))
            days.append(d)
            rates.append(r)
        with _fx_lock:
            _fx_mem[key] = (stamp, data, time.monotonic())
        self.data = data

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
# Money results only. Prices (entry/exit/stop/targets/fill prices) stay in the instrument currency.
_MONEY = ("gross_pnl", "fees", "net_pnl", "mfe", "mae", "cost_basis", "risk_amount")
FX_PHRASE = "converted at the rate on each trade date; open positions at the latest rate"


def trade_ccy(t) -> str:
    return (t.currency or (t.account.currency if t.account is not None else None) or "USD").upper()


def account_ccy(t) -> str:
    acct = getattr(t, "account", None)
    return ((getattr(acct, "currency", None) if acct is not None else None) or trade_ccy(t) or "USD").upper()


def raw(t):
    return t._t if isinstance(t, CTrade) else t


def chain_rate(book: RateBook, instr: str, account: str, display: str, day: date,
               fx_to_account: float | None = None, *, latest: bool = False) -> float | None:
    """Multiply an instrument-currency amount to get ``display`` on ``day``.

    Instrument -> account: the fill's broker rate when it has one (and this is not the latest-rate mark),
    otherwise the ECB rate. Account -> display: the ECB rate. Either leg is 1 when the currencies match.
    """
    instr = (instr or account or "USD").upper()
    account = (account or instr or "USD").upper()
    display = (display or "USD").upper()
    if instr == display:
        return 1.0
    if instr == account:
        r1 = 1.0
    elif fx_to_account is not None and not latest:
        r1 = float(fx_to_account)
    else:
        r1 = book.rate(instr, account, day)
    if not r1:
        return None
    r2 = 1.0 if account == display else book.rate(account, display, day)
    if not r2:
        return None
    return r1 * r2


def _broker_fx(fill) -> float | None:
    ex = getattr(fill, "execution", None)
    return getattr(ex, "fx_to_account", None) if ex is not None else None


def _fill_rate(book: RateBook, t, fill, display: str, *, latest: bool = False) -> float | None:
    day = datetime.now().date() if latest or fill is None or not getattr(fill, "executed_at", None) else fill.executed_at.date()
    return chain_rate(book, trade_ccy(t), account_ccy(t), display, day, None if latest else _broker_fx(fill), latest=latest)


def _fifo(t, rate_of) -> tuple[float, float]:
    """(gross, fees) with each fill's own multiplier ``rate_of(fill)``. Same matching as ``app.realized``."""
    from collections import deque
    fills = sorted(getattr(t, "fills", None) or [], key=lambda f: f.position)
    sign = 1.0 if t.direction == "LONG" else -1.0
    mult = getattr(t, "multiplier", None) or 1.0
    lots: deque = deque()
    gross = fees = 0.0
    for f in fills:
        rate = rate_of(f) or 1.0
        if f.role == "OPEN":
            lots.append([f.quantity, f.price, rate])
            fees += (f.fees or 0.0) * rate
            continue
        q, pnl = f.quantity, 0.0
        while q > 1e-12 and lots:
            m = min(q, lots[0][0])
            pnl += (f.price * rate - lots[0][1] * lots[0][2]) * m * sign
            lots[0][0] -= m
            q -= m
            if lots[0][0] <= 1e-12:
                lots.popleft()
        gross += pnl * mult
        fees += (f.fees or 0.0) * rate
    return gross, fees


class _Conv:
    """Converted money for one trade. ``rates`` maps fill id -> instrument-to-display rate."""

    def __init__(self, money: dict, rates: dict, latest: float | None, entry: float | None):
        self.money, self.rates, self.latest, self.entry = money, rates, latest, entry


def convert(book: RateBook, t, display: str) -> _Conv | None:
    """Per-execution money in ``display``, or None when a required rate is missing (keep the native figures)."""
    display = display.upper()
    native = trade_ccy(t)
    if native == display:
        return None
    fills = list(getattr(t, "fills", None) or [])
    today = datetime.now().date()
    rates: dict = {}
    if fills:
        for f in fills:
            r = _fill_rate(book, t, f, display)
            if not r:
                return None
            rates[f.id] = r
        g_nat, f_nat = _fifo(t, lambda f: 1.0)
        g_fx, f_fx = _fifo(t, lambda f: rates.get(f.id) or 1.0)
        last_r = rates.get(fills[-1].id) or 1.0
        # rounding residue the native builder left on the stored totals, taken at the last fill's rate
        gross = g_fx + ((t.gross_pnl or 0.0) - g_nat) * last_r
        fees = f_fx + (((t.gross_pnl or 0.0) - (t.net_pnl or 0.0)) - f_nat) * last_r
        entry_r = rates.get(next((f.id for f in fills if f.role == "OPEN"), fills[0].id))
    else:
        day = t.closed_at.date() if (t.status == "CLOSED" and t.closed_at) else today
        r = chain_rate(book, native, account_ccy(t), display, day, None, latest=(t.status != "CLOSED"))
        if not r:
            return None
        gross, fees, entry_r = (t.gross_pnl or 0.0) * r, (t.fees or 0.0) * r, r
    net = gross - fees
    if t.status == "CLOSED" and t.closed_at:
        fx = _broker_fx(fills[-1]) if fills else None
        mark = chain_rate(book, native, account_ccy(t), display, t.closed_at.date(), fx)
    else:
        mark = chain_rate(book, native, account_ccy(t), display, today, None, latest=True)
    latest = chain_rate(book, native, account_ccy(t), display, today, None, latest=True)
    if not mark or not latest or not entry_r:
        return None
    opens = [f for f in fills if f.role == "OPEN"]
    if opens:
        cost = sum(f.quantity * f.price * (getattr(t, "multiplier", None) or 1.0) * rates[f.id] for f in opens)
    else:
        cost = (t.cost_basis or 0.0) * entry_r
    money = {
        "gross_pnl": gross, "fees": fees, "net_pnl": net,
        "mfe": None if t.mfe is None else t.mfe * mark,
        "mae": None if t.mae is None else t.mae * mark,
        "cost_basis": cost,
        "risk_amount": None if t.risk_amount is None else t.risk_amount * entry_r,
    }
    return _Conv(money, rates, latest, entry_r)


class CTrade:
    """A trade whose money results are in the display currency. Prices and the rest delegate to the stored trade."""

    def __init__(self, t: Trade, display: str, conv: _Conv):
        self._t = t
        self.native_trade = t
        self.native_currency = trade_ccy(t)
        self.shown_currency = display
        self.fx_latest = conv.latest          # instrument -> display, latest rate (unrealized / open value)
        self.fx_rate = conv.latest or 1.0     # same; prices are NOT scaled by this
        self.fx_entry = conv.entry            # rate of the opening fill (risk $)
        self._money = conv.money
        self._rates = conv.rates

    def fx_on(self, fill) -> float:
        """Instrument -> display rate of one fill (used by the realized-P&L engine)."""
        return self._rates.get(getattr(fill, "id", None), 1.0)

    def __getattr__(self, n):
        if n in _MONEY and n in self._money:
            return self._money[n]
        return getattr(self._t, n)

    def __setattr__(self, n, v):
        self.__dict__[n] = v

    def __eq__(self, o):
        return getattr(o, "id", None) == self._t.id if isinstance(o, CTrade) else self._t is o

    def __hash__(self):
        return hash(self._t)


def _span(trades) -> tuple[date, date]:
    today = datetime.now().date()
    days = []
    for t in trades:
        for f in getattr(t, "fills", None) or []:
            if getattr(f, "executed_at", None):
                days.append(f.executed_at.date())
        if t.closed_at:
            days.append(t.closed_at.date())
        elif t.opened_at:
            days.append(t.opened_at.date())
    return (min(days) if days else today - timedelta(days=7)), today


def view(db: Session, trades: list, display: str | None = None, http=None) -> list:
    """Trades with money results in the display currency. Same objects when every trade is already in it."""
    trades = list(trades)
    display = (display or get_display(db)).upper()
    if {trade_ccy(t) for t in trades} <= {display}:
        return trades
    book = RateBook(db, http)
    start, today = _span([t for t in trades if trade_ccy(t) != display])
    book.ensure({trade_ccy(t) for t in trades} | {account_ccy(t) for t in trades} | {display}, start, today)
    out = []
    for t in trades:
        if trade_ccy(t) == display:
            out.append(t)
            continue
        conv = convert(book, t, display)
        out.append(t if conv is None else CTrade(t, display, conv))
    return out


class Presentation:
    """Display-currency money for one trade page, next to the stored (instrument-currency) figures.

    ``show`` is false when the trade is already in the display currency, or when a rate is missing
    (the page then shows the native amount once, in its own symbol)."""

    def __init__(self, show: bool, money: dict, rates: dict):
        self.show, self._m, self.rates = show, money, rates

    def __getattr__(self, n):
        if n in self._m:
            return self._m[n]
        raise AttributeError(n)

    def fill_amount(self, fill, native: float | None) -> float | None:
        """A single fill's notional or fee in the display currency (that fill's own rate)."""
        if not self.show or native is None:
            return None
        r = self.rates.get(getattr(fill, "id", None))
        return None if not r else native * r


def presentation(db: Session, t, tm: dict | None = None) -> Presentation:
    """Both-currency figures for the trade page. ``tm`` is ``metrics.trade_metrics`` of the raw trade."""
    display = get_display(db)
    native = trade_ccy(t)
    tm = tm or {}
    blank = {k: None for k in ("net", "gross", "fees", "mfe", "mae", "risk", "total_risk", "target",
                               "unrealized", "open_value", "position_value", "rps", "left", "best",
                               "dividend", "pnl_div")}
    if native == display:
        return Presentation(False, blank, {})
    book = RateBook(db)
    start, today = _span([t])
    book.ensure({native, account_ccy(t), display}, start, today)
    conv = convert(book, t, display)
    if conv is None:
        return Presentation(False, blank, {})
    m = conv.money
    entry, latest = conv.entry or 1.0, conv.latest or 1.0
    qty = (t.quantity or 0) * (getattr(t, "multiplier", None) or 1.0)

    def at(v, rate):
        return None if v is None else v * rate

    unreal = at(tm.get("open_pnl"), latest)
    mfe, gross = m["mfe"], m["gross_pnl"]
    div_d = tm.get("dividend_fx")
    pnl_div_d = None if div_d is None else m["net_pnl"] + div_d
    return Presentation(True, {
        "net": m["net_pnl"], "gross": gross, "fees": m["fees"], "mfe": mfe, "mae": m["mae"],
        "risk": at(tm.get("risk"), entry), "total_risk": at(tm.get("total_risk"), entry),
        "target": at(tm.get("target_pnl"), entry), "unrealized": unreal,
        "open_value": at(tm.get("open_value"), latest), "position_value": m["cost_basis"],
        "rps": (m["net_pnl"] / qty) if qty and t.status == "CLOSED" else None,
        "left": (mfe - gross) if (mfe is not None and t.status == "CLOSED") else None,
        "best": mfe, "dividend": div_d, "pnl_div": pnl_div_d,
    }, conv.rates)


def scale_open(t, native_amount: float | None) -> float | None:
    """Unrealized / open P&L in the display currency: the instrument amount at the latest rate (1 when already there)."""
    if native_amount is None:
        return None
    r = getattr(t, "fx_latest", None)
    return native_amount if r is None else native_amount * r


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
