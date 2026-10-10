"""Default initial stop for the FIRST entry: the low of the day before your entry (high for shorts), minus a buffer.

Rule (Settings > "Default stop rule" = Low of day before entry; or Manual only):
* Longs: the lowest LOW of the regular-session 5-minute bars of the entry day (New York), from the 09:30 open up to
  and INCLUDING the 5-minute bar that contains the first entry fill. Lows printed after that bar are ignored.
  The entry bar is included because that is the bar you see on the chart and the low may have printed before your
  fill inside it. Shorts mirror this with the highest HIGH. Premarket bars are only used if the setting says so.
* Buffer: the stop sits a few cents beyond that extreme (Settings > Stop buffer: $ amount, default $0.05, or a %).
* Fallback: if 5-minute bars aren't available (the entry is older than ~58 days, the provider has none, or the first
  fill has no time of day) the DAILY low (high) of the entry day minus the buffer is used. That includes the part of
  the day after your entry, so it is flagged "auto: daily low (approx)".
* First entry = the first opening fill plus opening fills within 5 minutes of it before anything is sold
  (metrics.entry_split); later adds are measured against the same stop and never move it.
Stored on the trade as stop_auto = True with stop_src '5m' | 'daily'. A stop or Risk $ you type always wins and is never
overwritten. Options are skipped. Once the entry bar has closed a 5-minute stop is final; a 'daily' stop is retried
while 5-minute bars could still be fetched; auto stops made by the first (daily-low) version are recomputed once."""
from __future__ import annotations

import json
import logging
import time as time_mod
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.models import Trade, utcnow
from app.services import get_state, set_state
from app.timeutil import ET, local_to_utc_naive, utc_naive_to_tz

log = logging.getLogger(__name__)
RULE_STATE = "stops:rule"
BUFFER_STATE = "stops:buffer"
PREMARKET_STATE = "stops:premarket"
RULES = {"low_of_day": "Low of the day before entry (5-min chart; high for shorts)", "manual": "Manual only"}
BACKFILL_STATE = "stops:backfilled:v2"
INTRADAY_DAYS = 58          # Yahoo keeps ~60 days of 5-minute bars
_MISS: dict[str, float] = {}


def get_rule(db: Session) -> str:
    v = get_state(db, RULE_STATE)
    return v if v in RULES else "low_of_day"


def set_rule(db: Session, rule: str) -> str:
    rule = rule if rule in RULES else "low_of_day"
    set_state(db, RULE_STATE, rule)
    db.commit()
    return rule


def get_buffer(db: Session) -> dict:
    try:
        v = json.loads(get_state(db, BUFFER_STATE) or "null")
    except ValueError:
        v = None
    if isinstance(v, dict) and v.get("mode") in ("usd", "pct"):
        try:
            return {"mode": v["mode"], "value": max(0.0, float(v["value"]))}
        except (TypeError, ValueError, KeyError):
            pass
    return {"mode": "usd", "value": 0.05}


def set_buffer(db: Session, mode: str, value: float) -> dict:
    cfg = {"mode": mode if mode in ("usd", "pct") else "usd", "value": min(max(0.0, float(value)), 50.0)}
    set_state(db, BUFFER_STATE, json.dumps(cfg))
    db.commit()
    return cfg


def premarket_on(db: Session) -> bool:
    return get_state(db, PREMARKET_STATE) == "1"


def set_premarket(db: Session, on: bool) -> None:
    set_state(db, PREMARKET_STATE, "1" if on else "0")
    db.commit()


def with_buffer(db: Session, extreme: float, direction: str) -> float:
    """Extreme moved away from the entry by the buffer (longs: lower; shorts: higher), rounded to cents."""
    b = get_buffer(db)
    amt = extreme * b["value"] / 100 if b["mode"] == "pct" else b["value"]
    v = extreme - amt if direction == "LONG" else extreme + amt
    return round(v, 2 if v >= 1 else 4)


def entry_day(t: Trade) -> date:
    return utc_naive_to_tz(first_entry_at(t), ET).date()


def first_entry_at(t: Trade) -> datetime:
    sp = metrics.entry_split(t)
    return (sp["first_at"] if sp and sp["first_at"] else None) or t.opened_at


def first_entry_time_known(t: Trade) -> bool:
    sp = metrics.entry_split(t)
    return bool(sp["time_known"]) if sp else bool(t.time_known)


def eligible(t: Trade) -> bool:
    return t.asset_type == "STOCK" and bool(t.entry_price) and t.direction in ("LONG", "SHORT")


def can_5m(t: Trade, now: datetime) -> bool:
    return first_entry_time_known(t) and (now - first_entry_at(t)) < timedelta(days=INTRADAY_DAYS)


def entry_bar_passed(t: Trade, now: datetime) -> bool:
    from app import prices
    start = prices._sec(first_entry_at(t)) // 300 * 300
    return prices._sec(now) >= start + 300


def day_bar(db: Session, t: Trade, now: datetime | None = None) -> dict | None:
    """The daily candle of the trade's entry day (None when no provider has it)."""
    from app import prices
    now = now or utcnow()
    day = entry_day(t)
    start = local_to_utc_naive(day, time(0, 0), ET)
    end = min(start + timedelta(days=2), now)
    if end <= start:
        return None
    key = prices._date_key(day)
    for prov in prices._provider_order(t):
        try:
            candles = prices.normalize(prices._fetch(db, prov, t, "1d", start, end, now), "1D")
        except Exception as exc:  # noqa: BLE001  network / auth: try the next provider
            log.info("default stop: provider %s failed for %s: %s", prov, t.underlying, exc)
            continue
        for c in candles:
            if int(c["time"]) == key:
                return c
    return None


def intraday_bars(db: Session, t: Trade, now: datetime) -> list[dict]:
    """5-minute bars of the entry day (session only, or from 04:00 when premarket is on), oldest first."""
    from app import prices
    day = entry_day(t)
    pre = premarket_on(db)
    start = local_to_utc_naive(day, time(4, 0) if pre else time(9, 30), ET)
    end = min(local_to_utc_naive(day, time(16, 0), ET), now)
    if end <= start:
        return []
    for prov in prices._provider_order(t):
        try:
            if pre and prov == "yahoo":
                candles = prices._yahoo(t.underlying, "5m", start, end, True)
            else:
                candles = prices._fetch(db, prov, t, "5m", start, end, now)
            candles = prices.normalize(candles, "5m")
        except Exception as exc:  # noqa: BLE001
            log.info("default stop: provider %s 5m failed for %s: %s", prov, t.underlying, exc)
            continue
        lo, hi = prices._sec(start), prices._sec(end)
        candles = [c for c in candles if lo <= int(c["time"]) <= hi]
        if candles:
            return candles
    return []


def clear_auto(t: Trade) -> None:
    """Forget how an auto stop was computed (the stop is being replaced by a manual one or removed)."""
    t.stop_auto = t.stop_src = t.stop_raw = t.stop_bar = t.stop_at = None


def extreme_before_entry(db: Session, t: Trade, now: datetime, allow_daily: bool = True) -> tuple[float, str, int | None] | None:
    """(raw low-of-day-before-entry / high-of-day, source, epoch of the bar that printed it) from 5-minute bars, else
    (allow_daily) the daily bar of the entry day."""
    from app import prices
    long = t.direction == "LONG"
    if can_5m(t, now):
        bars = intraday_bars(db, t, now)
        if bars:
            ts = prices._sec(first_entry_at(t))
            upto = [b for b in bars if int(b["time"]) <= ts] or bars[:1]   # fill before the open: the opening bar
            pick = min(upto, key=lambda b: b["low"]) if long else max(upto, key=lambda b: b["high"])
            return float(pick["low"] if long else pick["high"]), "5m", int(pick["time"])
    if not allow_daily:
        return None
    bar = day_bar(db, t, now)
    if bar:
        return float(bar["low"] if long else bar["high"]), "daily", int(bar["time"]) if bar.get("time") else None
    return None


def decide(db: Session, t: Trade, now: datetime | None = None, force: bool = False) -> tuple[str, dict | None]:
    """What apply_default would do, without changing the trade: (result, plan) where plan = stop / raw / src / bar.
    Results: set | refreshed | unchanged | kept | off | option | no-data | has-risk.
    A stored 5-minute stop is never replaced by worse (daily) data; with force it is recomputed from 5-minute bars if
    they are still available, otherwise only the buffer is re-applied to the stored raw low/high."""
    if get_rule(db) != "low_of_day":
        return "off", None
    if not eligible(t):
        return ("option" if t.asset_type != "STOCK" else "no-data"), None
    now = now or utcnow()
    if t.initial_stop is not None and not t.stop_auto:
        return "kept", None                    # manual stop always wins
    if t.initial_stop is None and t.risk_amount:
        return "has-risk", None                # the user gave Risk $ directly
    have5 = t.initial_stop is not None and t.stop_auto and t.stop_src == "5m"
    if t.initial_stop is not None and t.stop_auto and t.stop_src and not force:
        if (entry_bar_passed(t, now) if have5 else not can_5m(t, now)):
            return "kept", None                # final: 5m stop after its entry bar closed / daily with no 5m possible
    if not force and _MISS.get(t.key, 0) > time_mod.monotonic():
        return "no-data", None
    got = extreme_before_entry(db, t, now, allow_daily=not have5)
    if not got and have5 and force and t.stop_raw is not None:
        got = (float(t.stop_raw), "5m", t.stop_bar)    # window gone: re-apply the buffer to the stored low
    if not got:
        if have5:
            return "kept", None
        if not force:
            _MISS[t.key] = time_mod.monotonic() + 900     # don't hammer the provider on every page view
        return "no-data", None
    raw, src, bar = got
    new = with_buffer(db, raw, t.direction)
    plan = {"stop": new, "raw": raw, "src": src, "bar": bar}
    if t.initial_stop is not None and abs(t.initial_stop - new) < 1e-9 and t.stop_src == src and t.stop_raw is not None:
        return "unchanged", plan
    return ("refreshed" if t.initial_stop is not None else "set"), plan


def apply_default(db: Session, t: Trade, now: datetime | None = None, force: bool = False) -> str:
    """Set / refresh the default stop when allowed and store how it was computed (stop_src, stop_raw, stop_bar,
    stop_at; the replaced value goes to stop_prev). Manual stops are never touched."""
    res, plan = decide(db, t, now, force)
    if plan and res in ("set", "refreshed"):
        if t.initial_stop is not None:
            t.stop_prev = t.initial_stop
        t.initial_stop, t.stop_auto, t.stop_src = plan["stop"], True, plan["src"]
        t.stop_raw, t.stop_bar, t.stop_at = plan["raw"], plan["bar"], now or utcnow()
        if plan["src"] == "daily":
            _MISS[t.key] = time_mod.monotonic() + 900     # retry 5-minute data later, not on every view
    elif plan and res == "unchanged" and t.stop_at is None:
        t.stop_raw, t.stop_bar, t.stop_at = plan["raw"], plan["bar"], now or utcnow()
    return res


def preview(db: Session, now: datetime | None = None) -> list[dict]:
    """Dry run of "Recompute stops": every auto stop (and every stock trade without a stop) with old -> new."""
    out = []
    q = select(Trade).where(Trade.initial_stop.is_(None) | (Trade.stop_auto.is_(True))).order_by(Trade.opened_at.desc())
    for t in db.scalars(q).all():
        res, plan = decide(db, t, now, force=True)
        if plan and res in ("set", "refreshed", "unchanged"):
            out.append({"id": t.id, "symbol": t.symbol, "opened": t.opened_at, "direction": t.direction, "result": res,
                        "old": t.initial_stop, "old_src": t.stop_src, "new": plan["stop"], "new_src": plan["src"]})
    return out


def startup_backfill() -> None:
    """One-time background pass after the update that introduced the 5-minute rule: every stock trade without a
    manual stop is (re)computed. Marker in app_state so it never repeats."""
    from app import db as dbmod
    from app.config import get_settings
    if get_settings().price_provider in ("none", "off", ""):
        return
    db = dbmod.SessionLocal()
    try:
        if get_rule(db) != "low_of_day" or get_state(db, BACKFILL_STATE):
            return
        res = backfill(db, force=True)
        set_state(db, BACKFILL_STATE, utcnow().isoformat())
        db.commit()
        log.info("default stops backfill (5-minute rule): %s", res)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("default stops backfill failed: %s", exc)
    finally:
        db.close()


def backfill(db: Session, limit: int | None = None, now: datetime | None = None, force: bool = False) -> dict:
    """Set the default stop on every eligible stock trade that has none and refresh auto stops that are stale
    (old rule, daily fallback that could now use 5-minute bars, entry bar still open). Manual stops are never
    touched. force=True recomputes every auto stop (after changing the buffer / premarket setting)."""
    done = {"set": 0, "refreshed": 0, "unchanged": 0, "kept": 0, "no-data": 0, "option": 0, "off": 0, "has-risk": 0}
    q = select(Trade).where(Trade.initial_stop.is_(None) | (Trade.stop_auto.is_(True))).order_by(Trade.opened_at.desc())
    n = 0
    for t in db.scalars(q).all():
        if limit is not None and n >= limit:
            break
        res = apply_default(db, t, now, force=force)
        done[res] = done.get(res, 0) + 1
        n += 1
        if res in ("set", "refreshed"):
            db.commit()
    db.commit()
    done["by_src"] = source_counts(db)
    return done


def source_counts(db: Session) -> dict:
    """Stock trades by how their stop was made: 5m (persisted from 5-minute bars), daily (approx), manual, none."""
    from collections import Counter
    c: Counter = Counter()
    for t in db.scalars(select(Trade).where(Trade.asset_type == "STOCK")).all():
        c["none" if t.initial_stop is None else "manual" if not t.stop_auto else (t.stop_src or "old")] += 1
    return dict(c)
