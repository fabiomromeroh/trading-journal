"""Load clearly-labelled SAMPLE data so the UI can be previewed before any real import.

    python -m app.seed_demo          # add sample account + trades (idempotent)
    python -m app.seed_demo --clear  # remove all sample data
"""
from __future__ import annotations

import argparse
import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.instruments import OPTION_MULTIPLIER, ExecRecord, option_symbol
from app.models import Account, Tag, Trade
from app.services import ingest_records, rebuild_trades
from app.timeutil import ET, local_to_utc_naive

DEMO_NAME = "Sample account (demo)"
SYMBOLS = {"NVDA": 128, "TSLA": 255, "AAPL": 228, "AMD": 152, "META": 565, "PLTR": 42, "SMCI": 41,
           "HOOD": 31, "COIN": 225, "CELH": 31, "ANF": 142, "SOFI": 11, "MSTR": 210, "APP": 120}
SETUPS = ["Breakout", "Episodic pivot", "Flag", "Parabolic short", "Earnings gap", "VWAP reclaim"]
TAGS = ["A+ setup", "FOMO", "early exit", "followed plan", "oversized", "news", "chased"]
NOTES = [
    "Clean ORH breakout on volume; trailed stop under the 10 EMA.",
    "Entered early before confirmation. Should have waited for the 5-min range high.",
    "Gap-and-go after earnings; partials into strength, runner stopped at break-even.",
    "Faded the parabolic move after the third push, covered into VWAP.",
    "Chased the move, poor R:R. Rule: no entries > 3% from the pivot.",
]


def _weekdays(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _t(d: date, minutes_after_open: int) -> datetime:
    return local_to_utc_naive(d, time(9, 30), ET) + timedelta(minutes=minutes_after_open)


def generate(rng: random.Random, today: date) -> list[ExecRecord]:
    recs: list[ExecRecord] = []
    days = _weekdays(85, today - timedelta(days=1))
    n = 0

    def add(sym, side, q, px, ts, fees=0.0, effect=None, **opt):
        nonlocal n
        n += 1
        recs.append(ExecRecord(external_id=f"demo:{n}", symbol=opt.pop("symbol", sym),
                               underlying=sym, asset_type=opt.pop("asset", "STOCK"), side=side, quantity=q,
                               price=round(px, 2), executed_at=ts, fees=round(fees, 2), position_effect=effect,
                               seq=n, **opt))

    for d in days:
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            sym = rng.choice(list(SYMBOLS))
            base = SYMBOLS[sym] * rng.uniform(0.9, 1.1)
            style = rng.random()
            win = rng.random() < 0.47
            if style < 0.55:  # intraday stock trade, maybe scaled
                short = rng.random() < 0.25
                sgn = -1 if short else 1
                q = rng.choice([50, 100, 150, 200, 300])
                t0 = rng.randint(3, 120)
                entry = base
                move = base * (rng.uniform(0.01, 0.045) if win else -rng.uniform(0.004, 0.018))
                open_side, close_side = ("SELL", "BUY") if short else ("BUY", "SELL")
                if rng.random() < 0.4:  # scale in
                    add(sym, open_side, q // 2, entry, _t(d, t0))
                    add(sym, open_side, q - q // 2, entry + sgn * base * 0.002, _t(d, t0 + rng.randint(3, 15)))
                else:
                    add(sym, open_side, q, entry, _t(d, t0))
                t1 = t0 + rng.randint(20, 240)
                if win and rng.random() < 0.6:  # partial profit then runner
                    add(sym, close_side, q // 2, entry + sgn * move * 0.6, _t(d, min(t1, 380)), fees=0.02)
                    add(sym, close_side, q - q // 2, entry + sgn * move, _t(d, min(t1 + rng.randint(10, 60), 389)), fees=0.02)
                else:
                    add(sym, close_side, q, entry + sgn * move, _t(d, min(t1, 389)), fees=0.03)
            elif style < 0.8:  # swing trade over several days
                idx = days.index(d)
                hold = rng.randint(2, 9)
                if idx + hold >= len(days):
                    continue
                q = rng.choice([30, 50, 80, 100])
                move = base * (rng.uniform(0.04, 0.20) if win else -rng.uniform(0.02, 0.06))
                add(sym, "BUY", q, base, _t(d, rng.randint(5, 60)))
                add(sym, "SELL", q, base + move, _t(days[idx + hold], rng.randint(30, 385)), fees=0.05)
            else:  # options
                exp = d + timedelta(days=(4 - d.weekday()) % 7 + 7)
                cp = "CALL" if rng.random() < 0.65 else "PUT"
                strike = round(base / 5) * 5
                osym = option_symbol(sym, exp, cp, strike)
                prem = round(base * rng.uniform(0.012, 0.03), 2)
                q = rng.choice([1, 2, 3, 5])
                fee = 0.66 * q
                common = dict(symbol=osym, asset="OPTION", option_type=cp, strike=float(strike),
                              expiration=exp, multiplier=OPTION_MULTIPLIER)
                if rng.random() < 0.2:  # short put left to expire
                    add(sym, "SELL", q, prem, _t(d, rng.randint(10, 200)), fees=fee, effect="OPEN", **common)
                    if exp < today:
                        add(sym, None, q, 0.0, local_to_utc_naive(exp, time(16, 0), ET), effect="CLOSE",
                            kind="EXPIRATION", **common)
                    continue
                exit_px = prem * (rng.uniform(1.3, 2.6) if win else rng.uniform(0.2, 0.8))
                add(sym, "BUY", q, prem, _t(d, rng.randint(5, 150)), fees=fee, effect="OPEN", **common)
                add(sym, "SELL", q, exit_px, _t(d, rng.randint(160, 385)), fees=fee, effect="CLOSE", **common)
    # one still-open position
    sym = "NVDA"
    add(sym, "BUY", 60, SYMBOLS[sym] * 1.02, _t(days[-2], 45))
    for r in recs:
        r.trade_date = r.executed_at.date()
    return recs


def clear(db: Session) -> int:
    n = 0
    for a in db.scalars(select(Account).where(Account.is_demo.is_(True))):
        db.delete(a)
        n += 1
    db.commit()
    return n


def seed(db: Session, today: date | None = None, rng_seed: int = 7) -> int:
    clear(db)
    rng = random.Random(rng_seed)
    acct = Account(name=DEMO_NAME, broker="demo", account_number_masked="...000", is_demo=True)
    db.add(acct)
    db.flush()
    recs = generate(rng, today or date.today())
    ingest_records(db, acct.id, "demo", recs)
    n = rebuild_trades(db, [acct.id])
    tag_objs = {}
    for name in TAGS:
        tag_objs[name] = db.scalar(select(Tag).where(Tag.name == name)) or Tag(name=name)
    for t in db.scalars(select(Trade).where(Trade.account_id == acct.id)):
        if rng.random() < 0.8:
            t.setup = rng.choice(SETUPS if t.direction == "LONG" else ["Parabolic short", "VWAP reclaim"])
        if rng.random() < 0.6:
            t.tags = [tag_objs[x] for x in rng.sample(TAGS, rng.choice([1, 1, 2]))]
        if rng.random() < 0.5:
            t.notes = rng.choice(NOTES)
        if rng.random() < 0.7:
            t.rating = rng.randint(2, 5) if t.net_pnl > 0 else rng.randint(1, 4)
    db.commit()
    return n


def main(argv=None) -> None:
    from app import db as dbmod
    from app.migrate import upgrade
    p = argparse.ArgumentParser()
    p.add_argument("--clear", action="store_true", help="remove sample data")
    args = p.parse_args(argv)
    upgrade()
    db = dbmod.SessionLocal()
    try:
        if args.clear:
            print(f"Removed {clear(db)} sample account(s).")
        else:
            print(f"Loaded sample data: {seed(db)} trades in '{DEMO_NAME}'.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
