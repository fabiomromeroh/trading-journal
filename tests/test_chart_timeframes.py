"""Trade chart timeframes: default timeframe, availability windows, provider requests, 4h/weekly
aggregation and marker placement (incl. date-only fills). Synthetic data only; no network."""
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import prices
from app.instruments import ExecRecord
from app.models import Account, Trade
from app.services import ingest_records, rebuild_trades
from app.timeutil import ET, local_to_utc_naive

NOW = datetime(2026, 10, 8, 17, 0)  # naive UTC, a Thursday (13:00 ET)
_n = 0


def _rec(sym, side, qty, px, when, known=True):
    global _n
    _n += 1
    return ExecRecord(external_id=f"t:{_n}", symbol=sym, underlying=sym, asset_type="STOCK", side=side,
                      quantity=qty, price=px, executed_at=when, time_known=known, seq=_n,
                      trade_date=when.replace(tzinfo=timezone.utc).astimezone(ET).date())


def _et(y, m, d, hh=16, mm=0):
    return local_to_utc_naive(datetime(y, m, d).date(), time(hh, mm), ET)


def _trade(db, recs) -> Trade:
    acct = db.scalar(select(Account)) or Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    ingest_records(db, acct.id, "snaptrade", recs)
    rebuild_trades(db, [acct.id])
    db.commit()
    sym = recs[0].symbol
    return db.scalar(select(Trade).where(Trade.symbol == sym))


def _bars(start: datetime, end: datetime, minutes: int, price=100.0):
    """Regular-session bars every `minutes` (Yahoo style timestamps in seconds)."""
    out, d = [], start.date() - timedelta(days=1)
    while d <= end.date() + timedelta(days=1):
        if d.weekday() < 5:
            t = local_to_utc_naive(d, time(9, 30), ET)
            close = local_to_utc_naive(d, time(16, 0), ET)
            while t < close:
                if start <= t <= end:
                    ts = int(t.replace(tzinfo=timezone.utc).timestamp())
                    out.append({"time": ts, "open": price, "high": price + 1, "low": price - 1,
                                "close": price, "volume": 1000})
                t += timedelta(minutes=minutes)
        d += timedelta(days=1)
    return out


def test_default_timeframe_rules(db):
    swing = _trade(db, [_rec("SWNG", "BUY", 10, 50, _et(2026, 9, 1), False),
                        _rec("SWNG", "SELL", 10, 55, _et(2026, 9, 10), False)])
    intraday = _trade(db, [_rec("INTR", "BUY", 10, 50, _et(2026, 10, 6, 10, 2)),
                           _rec("INTR", "SELL", 10, 51, _et(2026, 10, 6, 11, 17))])
    old_intraday = _trade(db, [_rec("OLDI", "BUY", 10, 50, _et(2026, 5, 5, 10, 2)),
                               _rec("OLDI", "SELL", 10, 51, _et(2026, 5, 5, 11, 17))])
    dateonly_intraday = _trade(db, [_rec("DOIN", "BUY", 10, 50, _et(2026, 10, 6), False),
                                    _rec("DOIN", "SELL", 10, 51, _et(2026, 10, 6), False)])
    open_today = _trade(db, [_rec("OPNT", "BUY", 10, 50, _et(2026, 10, 8, 9, 45))])
    open_swing = _trade(db, [_rec("OPNS", "BUY", 10, 50, _et(2026, 10, 1), False)])
    assert prices.is_swing(swing, NOW) and prices.default_timeframe(swing, NOW) == "1D"
    assert not prices.is_swing(intraday, NOW) and prices.default_timeframe(intraday, NOW) == "5m"
    assert prices.default_timeframe(old_intraday, NOW) == "1h"  # 5m history (60 days) is gone
    assert prices.default_timeframe(dateonly_intraday, NOW) == "1D"  # no times -> daily
    assert prices.default_timeframe(open_today, NOW) == "5m"
    assert prices.default_timeframe(open_swing, NOW) == "1D"


def test_timeframe_availability_and_windows(db):
    t = _trade(db, [_rec("AVL", "BUY", 10, 50, _et(2026, 7, 20, 10, 0)),
                    _rec("AVL", "SELL", 10, 51, _et(2026, 7, 21, 15, 0))])
    opts = {o["tf"]: o for o in prices.timeframe_options(t, NOW)}
    assert [o for o in opts] == ["1m", "5m", "15m", "30m", "1h", "4h", "1D", "1W"]
    for tf in ("1m", "5m", "15m", "30m"):
        assert not opts[tf]["available"] and "go back" in opts[tf]["reason"]
    for tf in ("1h", "4h", "1D", "1W"):
        assert opts[tf]["available"]
    # 1m: within the last 29 days and at most 7 days per request
    r = _trade(db, [_rec("ONEM", "BUY", 10, 50, _et(2026, 9, 14, 10, 0)),
                    _rec("ONEM", "SELL", 10, 51, _et(2026, 10, 2, 15, 0))])
    start, end, notes = prices.tf_window(r, "1m", NOW)
    assert start >= NOW - timedelta(days=29) and end - start <= timedelta(days=7)
    assert any("older" in n for n in notes) or any("first days" in n for n in notes)
    # daily: plenty of history before entry for 200-period indicators, capped at now
    start, end, _ = prices.tf_window(r, "1D", NOW)
    assert start <= r.opened_at - timedelta(days=400) and end <= NOW


def test_aggregation_and_normalisation():
    hourly = _bars(_et(2026, 10, 5, 9, 30), _et(2026, 10, 6, 16, 0), 60)
    four = prices.aggregate_4h(hourly)
    assert len(four) == 4  # 09:30 and 13:30 bars on two days
    t0 = datetime.fromtimestamp(four[0]["time"], timezone.utc).astimezone(ET)
    t1 = datetime.fromtimestamp(four[1]["time"], timezone.utc).astimezone(ET)
    assert (t0.hour, t0.minute, t1.hour, t1.minute) == (9, 30, 13, 30)
    assert four[0]["volume"] == 4000 and four[1]["volume"] == 3000
    # daily bars stamped at the 09:30 ET open (Yahoo) -> 00:00 UTC of the NY date; duplicates collapse
    daily = [{"time": int(_et(2026, 10, d, 9, 30).replace(tzinfo=timezone.utc).timestamp()), "open": 1,
              "high": 2, "low": 0.5, "close": 1.5, "volume": None} for d in (5, 6, 6)]
    n = prices.normalize(daily, "1D")
    assert [datetime.fromtimestamp(x["time"], timezone.utc).strftime("%Y-%m-%d %H:%M") for x in n] == \
        ["2026-10-05 00:00", "2026-10-06 00:00"] and n[0]["volume"] == 0
    weekly = prices.aggregate_weekly(prices.normalize(
        [{**daily[0], "time": int(datetime(2026, 10, d, tzinfo=timezone.utc).timestamp())} for d in (5, 6, 7, 12, 13)], "1D"))
    assert len(weekly) == 2


def test_markers_snap_to_bars_and_date_only_fills(db):
    timed = _trade(db, [_rec("MRK", "BUY", 10, 50, _et(2026, 10, 6, 10, 2)),
                        _rec("MRK", "SELL", 10, 51, _et(2026, 10, 6, 11, 17))])
    bars5 = _bars(_et(2026, 10, 2, 9, 30), _et(2026, 10, 8, 16, 0), 5)
    mk, hidden = prices.markers(timed, bars5, "5m")
    assert hidden == 0 and len(mk) == 2
    times = {b["time"] for b in bars5}
    assert all(m["time"] in times for m in mk)
    entry_bar = datetime.fromtimestamp(mk[0]["time"], timezone.utc).astimezone(ET)
    assert (entry_bar.hour, entry_bar.minute) == (10, 0) and "time n/a" not in mk[0]["text"]
    # daily: on the trade date's bar
    daily = prices.normalize(_bars(_et(2026, 9, 28, 9, 30), _et(2026, 10, 8, 9, 30), 390), "1D")
    mk, _ = prices.markers(timed, daily, "1D")
    assert {datetime.fromtimestamp(m["time"], timezone.utc).date().isoformat() for m in mk} == {"2026-10-06"}
    # date-only fills on an intraday chart: day's last bar, labelled
    dated = _trade(db, [_rec("DTO", "BUY", 5, 20, _et(2026, 10, 5), False),
                        _rec("DTO", "SELL", 5, 21, _et(2026, 10, 7), False)])
    mk, hidden = prices.markers(dated, bars5, "5m")
    assert hidden == 0 and all("time n/a" in m["text"] for m in mk)  # prices never traded -> day fallback
    first = datetime.fromtimestamp(mk[0]["time"], timezone.utc).astimezone(ET)  # opening fill: day's first bar
    last = datetime.fromtimestamp(mk[1]["time"], timezone.utc).astimezone(ET)  # closing fill: day's last bar
    assert first.date().isoformat() == "2026-10-05" and (first.hour, first.minute) == (9, 30)
    assert last.date().isoformat() == "2026-10-07" and (last.hour, last.minute) == (15, 55)
    # a date-only fill whose price traded that day is placed at the first bar that traded it
    traded = _trade(db, [_rec("DTP", "BUY", 5, 100.5, _et(2026, 10, 5), False),
                         _rec("DTP", "SELL", 5, 100.5, _et(2026, 10, 7), False)])
    mk, _ = prices.markers(traded, bars5, "5m")
    assert all("time est." in m["text"] for m in mk)
    assert datetime.fromtimestamp(mk[1]["time"], timezone.utc).astimezone(ET).strftime("%m-%d %H:%M") == "10-07 09:30"
    f = prices.focus(traded, bars5, "5m")
    assert f["from"] == mk[0]["time"] and f["to"] == mk[1]["time"]


@pytest.fixture()
def yahoo_client(db, monkeypatch):
    monkeypatch.setenv("PRICE_PROVIDER", "yahoo")
    from app.config import get_settings
    get_settings.cache_clear()
    calls = []

    def fake_yahoo(symbol, interval, start, end):
        calls.append((symbol, interval, start, end))
        step = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(interval)
        if step:
            return _bars(start, end, step)
        days = _bars(start, end, 390)
        return days if interval == "1d" else days[::5]

    monkeypatch.setattr(prices, "_yahoo", fake_yahoo)
    monkeypatch.setattr(prices, "utcnow", lambda: NOW)
    prices._MEM.clear()
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    yield c, calls
    get_settings.cache_clear()


def test_chart_endpoint_timeframes(yahoo_client, db):
    c, calls = yahoo_client
    t = _trade(db, [_rec("EPT", "BUY", 10, 100, _et(2026, 10, 6, 10, 2)),
                    _rec("EPT", "SELL", 10, 101, _et(2026, 10, 6, 11, 17))])
    d = c.get(f"/trades/{t.id}/chart.json").json()
    assert d["tf"] == "5m" and d["default_tf"] == "5m" and d["intraday"] and not d["swing"]
    assert d["candles"] and all("volume" in k for k in d["candles"]) and len(d["markers"]) == 2
    assert "5m" in [x[1] for x in calls] and d["focus"]["from"] <= d["focus"]["to"]
    assert d["excursion"]["tf"] == "1m" and "MFE / MAE" in d["excursion"]["note"]
    calls.clear()
    d = c.get(f"/trades/{t.id}/chart.json?tf=4h").json()
    assert d["tf"] == "4h" and calls[0][1] == "60m"
    assert all(datetime.fromtimestamp(k["time"], timezone.utc).astimezone(ET).strftime("%H:%M") in ("09:30", "13:30")
               for k in d["candles"])
    d = c.get(f"/trades/{t.id}/chart.json?tf=1D").json()
    assert d["tf"] == "1D" and not d["intraday"]
    assert all(k["time"] % 86400 == 0 for k in d["candles"])
    calls.clear()
    d = c.get(f"/trades/{t.id}/chart.json?tf=1W").json()
    assert d["tf"] == "1W" and calls[0][1] == "1wk" and d["markers"]
    d = c.get(f"/trades/{t.id}/chart.json?tf=bogus").json()
    assert d["tf"] == "5m"
    # an old swing trade: defaults to 1D, 1m is refused with a note and falls back
    s = _trade(db, [_rec("OLDS", "BUY", 10, 100, _et(2026, 3, 2), False),
                    _rec("OLDS", "SELL", 10, 90, _et(2026, 3, 20), False)])
    d = c.get(f"/trades/{s.id}/chart.json?tf=1m").json()
    assert d["tf"] == "1D" and d["default_tf"] == "1D" and any("1m isn't available" in n for n in d["notes"])
    assert not {o["tf"]: o for o in d["timeframes"]}["1m"]["available"]
    assert d["mfe"] is not None  # computed on the default timeframe
    page = c.get(f"/trades/{s.id}")
    assert page.status_code == 200 and "trade_chart.js" in page.text and 'id="tf-bar"' in page.text
    assert "max-w-[1500px]" not in page.text and "tradingview.com/chart/?symbol=OLDS" in page.text
    assert "side-collapsed" in page.text and "side-collapsed" not in c.get("/trades").text.split("<body")[1][:80]


def test_demo_trade_every_timeframe(db):
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    c.post("/settings/demo/load")
    t = db.scalar(select(Trade).where(Trade.is_demo.is_(True), Trade.asset_type == "STOCK", Trade.status == "CLOSED"))
    for tf in prices.TIMEFRAMES:
        d = c.get(f"/trades/{t.id}/chart.json?tf={tf}").json()
        assert d["tf"] == tf and d["provider"] == "demo" and d["candles"], tf
        times = [k["time"] for k in d["candles"]]
        assert times == sorted(set(times)), tf
        assert d["markers"] and all(m["time"] in set(times) for m in d["markers"]), tf


def test_provisional_order_fill_badge(yahoo_client, db):
    c, _ = yahoo_client
    acct = db.scalar(select(Account)) or Account(name="A", broker="schwab")
    db.add(acct)
    db.flush()
    ingest_records(db, acct.id, "snaptrade_order", [_rec("PROV", "BUY", 2, 10, _et(2026, 10, 8), False)])
    rebuild_trades(db, [acct.id])
    db.commit()
    t = db.scalar(select(Trade).where(Trade.symbol == "PROV"))
    page = c.get(f"/trades/{t.id}").text
    assert "provisional (fees pending)" in page and ">snaptrade_order<" not in page



class _F:
    def __init__(self, side, qty, price, role):
        self.side, self.quantity, self.price, self.role = side, qty, price, role


class _T:
    def __init__(self, closed=True, mult=1.0):
        self.closed_at, self.multiplier = (NOW if closed else None), mult


def _k(*hl):
    return [{"time": i * 60, "open": (h + l) / 2, "high": h, "low": l, "close": (h + l) / 2} for i, (h, l) in enumerate(hl)]


def test_running_excursions_scaling_and_short():
    # long 10 @ 10, add 10 @ 12, exit 20 @ 13: running P&L uses the real open size and average cost
    k = _k((10.5, 9.5), (12.5, 11.0), (14.0, 12.5), (13.5, 12.8))
    placed = [(_F("BUY", 10, 10, "OPEN"), 0, "time"), (_F("BUY", 10, 12, "OPEN"), 1, "time"),
              (_F("SELL", 20, 13, "CLOSE"), 3, "time")]
    mfe, mae = prices.running_excursions(_T(), k, placed)
    assert mfe == 60.0  # 20 sh, avg 11, high 14
    assert mae == -5.0  # bar 0: 10 sh @ 10, low 9.5
    # short 5 @ 50, cover 5 @ 48; bars after the exit are ignored
    k = _k((51.0, 49.5), (50.5, 47.0), (60.0, 30.0))
    placed = [(_F("SELL", 5, 50, "OPEN"), 0, "time"), (_F("BUY", 5, 48, "CLOSE"), 1, "time")]
    assert prices.running_excursions(_T(), k, placed) == (15.0, -5.0)
    # partial exit locks in realized P&L
    k = _k((10.0, 10.0), (12.0, 11.0), (12.0, 8.0))
    placed = [(_F("BUY", 10, 10, "OPEN"), 0, "time"), (_F("SELL", 5, 12, "CLOSE"), 1, "time"),
              (_F("SELL", 5, 9, "CLOSE"), 2, "time")]
    assert prices.running_excursions(_T(), k, placed) == (20.0, 0.0)  # worst: +10 realized + 5 x (8 - 10)


def test_alab_like_date_only_exit_ignores_moves_after_the_sale(db, monkeypatch):
    """Timed entry, date-only exit: the exit is placed at the first bar that traded the exit price,
    so a deeper low later that day (after the shares were sold) doesn't count as MAE."""
    t = _trade(db, [_rec("ALBX", "BUY", 4, 370.17, _et(2026, 10, 7, 9, 56, )),
                    _rec("ALBX", "SELL", 4, 366.88, _et(2026, 10, 8), False)])

    def bars(symbol, interval, start, end):
        assert interval == "1m"
        out = []
        for b in _bars(start, end, 1):
            lt = datetime.fromtimestamp(b["time"], timezone.utc).astimezone(ET)
            hm = lt.strftime("%m-%d %H:%M")
            px = 372.0
            if hm == "10-07 09:40":
                px = 360.0  # before the entry: not part of the trade
            elif hm == "10-07 15:40":
                px = 388.14
            elif hm == "10-08 09:55":
                px = 366.5  # first time the exit price trades
            elif hm == "10-08 13:25":
                px = 345.64  # after the sale
            out.append({**b, "open": px, "close": px, "high": px + 0.5, "low": px - 0.5})
        return out

    monkeypatch.setenv("PRICE_PROVIDER", "yahoo")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(prices, "_yahoo", bars)
    prices._MEM.clear()
    prices._EXC_MEMO.clear()
    try:
        res = prices.compute_trade_excursions(db, t, now=datetime(2026, 10, 8, 19, 0))
    finally:
        get_settings.cache_clear()
    assert res["tf"] == "1m" and res["estimated"] == 1
    assert res["mfe"] == round((388.64 - 370.17) * 4, 2)
    assert res["mae"] == round((366.0 - 370.17) * 4, 2)
