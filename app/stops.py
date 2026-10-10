"""Default initial stop: the LOW of the entry day for longs (HIGH of the day for shorts).

The entry day is the New York date the trade was opened; the extreme comes from that day's daily bar (regular
session, Yahoo / the configured price provider). A computed stop is stored on the trade with ``stop_auto = True``;
typing a stop (or Risk $) yourself always wins and clears the flag, and a manual stop is never overwritten.
Options are skipped (a stop on the underlying says nothing about the premium). While the entry day is still in
progress the extreme can still move, so an auto stop on today's entry is refreshed on each look.
Rule (Settings): ``low_of_day`` (default) or ``manual`` (never compute)."""
from __future__ import annotations

import logging
import time as time_mod
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Trade, utcnow
from app.services import get_state, set_state
from app.timeutil import ET, local_to_utc_naive, utc_naive_to_tz

log = logging.getLogger(__name__)
RULE_STATE = "stops:rule"
RULES = {"low_of_day": "Low of the entry day (high for shorts)", "manual": "Manual only"}
BACKFILL_STATE = "stops:backfilled"
_MISS: dict[str, float] = {}


def get_rule(db: Session) -> str:
    v = get_state(db, RULE_STATE)
    return v if v in RULES else "low_of_day"


def set_rule(db: Session, rule: str) -> str:
    rule = rule if rule in RULES else "low_of_day"
    set_state(db, RULE_STATE, rule)
    db.commit()
    return rule


def entry_day(t: Trade) -> date:
    return utc_naive_to_tz(t.opened_at, ET).date()


def eligible(t: Trade) -> bool:
    return t.asset_type == "STOCK" and bool(t.entry_price) and t.direction in ("LONG", "SHORT")


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


def apply_default(db: Session, t: Trade, now: datetime | None = None) -> str:
    """Set the default stop when allowed. Returns what happened:
    set | refreshed | kept | off | option | no-data | has-risk."""
    if get_rule(db) != "low_of_day":
        return "off"
    if not eligible(t):
        return "option" if t.asset_type != "STOCK" else "no-data"
    now = now or utcnow()
    if t.initial_stop is not None and not t.stop_auto:
        return "kept"                          # manual stop always wins
    if t.initial_stop is not None and t.stop_auto and entry_day(t) < last_final_day(now) - timedelta(days=1):
        return "kept"                          # auto stop whose day finished long ago: final
    if t.initial_stop is None and t.risk_amount:
        return "has-risk"                      # the user gave Risk $ directly
    if _MISS.get(t.key, 0) > time_mod.monotonic():
        return "no-data"
    bar = day_bar(db, t, now)
    if not bar:
        _MISS[t.key] = time_mod.monotonic() + 900     # don't hammer the provider on every page view
        return "no-data"
    value = bar["low"] if t.direction == "LONG" else bar["high"]
    refreshed = t.initial_stop is not None
    t.initial_stop, t.stop_auto = round(float(value), 4), True
    return "refreshed" if refreshed else "set"


def last_final_day(now: datetime) -> date:
    """First New York date whose daily bar can still change (today, once the session has started)."""
    lt = utc_naive_to_tz(now, ET)
    return lt.date() if lt.time() >= time(9, 30) else lt.date() - timedelta(days=1)


def startup_backfill() -> None:
    """One-time background pass after the update that introduced default stops: every existing stock trade gets
    its stop. Marker in app_state so it never repeats; failures just leave trades for the lazy/sync paths."""
    from app import db as dbmod
    from app.config import get_settings
    if get_settings().price_provider in ("none", "off", ""):
        return
    db = dbmod.SessionLocal()
    try:
        if get_rule(db) != "low_of_day" or get_state(db, BACKFILL_STATE):
            return
        res = backfill(db)
        set_state(db, BACKFILL_STATE, utcnow().isoformat())
        db.commit()
        log.info("default stops backfill: %s", res)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("default stops backfill failed: %s", exc)
    finally:
        db.close()


def backfill(db: Session, limit: int | None = None, now: datetime | None = None) -> dict:
    """Set the default stop on every eligible stock trade that has none (and refresh today's auto stops)."""
    done = {"set": 0, "refreshed": 0, "kept": 0, "no-data": 0, "option": 0, "off": 0, "has-risk": 0}
    q = select(Trade).where(Trade.initial_stop.is_(None) | (Trade.stop_auto.is_(True))).order_by(Trade.opened_at.desc())
    n = 0
    for t in db.scalars(q).all():
        if limit is not None and n >= limit:
            break
        res = apply_default(db, t, now)
        done[res] = done.get(res, 0) + 1
        n += 1
        if res in ("set", "refreshed"):
            db.commit()
    db.commit()
    return done
