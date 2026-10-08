"""Cross-source fill matching.

The same fill can arrive from several places: SnapTrade / the Schwab API (date-only or exact time,
one row per order), a Schwab.com CSV (date-only) or a thinkorswim statement (exact times, sometimes
one row per partial fill). Matching is done per (symbol, side, kind) on the exchange (ET) trade date:

  1. one-to-one: same quantity, price within tolerance (closest price wins);
  2. many-to-one: partial fills whose quantities sum to one row and whose VWAP matches its price;
  3. one-to-many: the reverse (one incoming row covers several existing partials);
  4. one-to-one again on an adjacent date (+-1 day), to absorb a timezone slip.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

QTY_EPS = 1e-6
MAX_GROUP = 12          # max partial fills combined into one row
MAX_STEPS = 20000       # search budget per row


def price_tol(price: float) -> float:
    return max(0.01, abs(price) * 0.0005) + 1e-9


@dataclass
class Item:
    ref: object                 # the record / row this item stands for
    day: date                   # ET trade date
    symbol: str
    side: str
    kind: str
    qty: float
    price: float
    order: tuple = field(default=())   # tie-break / time ordering

    @property
    def group(self):
        return (self.symbol, self.side or "-", self.kind)


@dataclass
class Match:
    incoming: list[Item]
    existing: list[Item]


def vwap(items: list[Item]) -> float:
    q = sum(i.qty for i in items)
    return sum(i.qty * i.price for i in items) / q if q else 0.0


def _subset(target: Item, pool: list[Item]) -> list[Item] | None:
    """Find >=2 items in pool whose quantities sum to target.qty and whose VWAP matches its price."""
    pool = [p for p in pool if p.qty <= target.qty + QTY_EPS]
    if len(pool) < 2:
        return None
    pool.sort(key=lambda p: p.order)
    best: list[Item] | None = None
    steps = 0

    def dfs(start: int, chosen: list[Item], qty: float):
        nonlocal best, steps
        if best is not None or steps > MAX_STEPS:
            return
        steps += 1
        if abs(qty - target.qty) <= QTY_EPS:
            if len(chosen) >= 2 and abs(vwap(chosen) - target.price) <= price_tol(target.price):
                best = list(chosen)
            return
        if qty > target.qty + QTY_EPS or len(chosen) >= MAX_GROUP:
            return
        for i in range(start, len(pool)):
            chosen.append(pool[i])
            dfs(i + 1, chosen, qty + pool[i].qty)
            chosen.pop()
            if best is not None:
                return

    dfs(0, [], 0.0)
    return best


def match(incoming: list[Item], existing: list[Item]) -> list[Match]:
    out: list[Match] = []
    used_in: set[int] = set()
    used_ex: set[int] = set()
    by_group_in: dict[tuple, list[Item]] = defaultdict(list)
    by_group_ex: dict[tuple, list[Item]] = defaultdict(list)
    for i in incoming:
        by_group_in[i.group].append(i)
    for e in existing:
        by_group_ex[e.group].append(e)

    for g, ins in by_group_in.items():
        exs = by_group_ex.get(g, [])
        if not exs:
            continue
        ins = sorted(ins, key=lambda x: x.order)
        exs = sorted(exs, key=lambda x: x.order)

        def free_in(day=None):
            return [i for i in ins if id(i) not in used_in and (day is None or i.day == day)]

        def free_ex(day=None):
            return [e for e in exs if id(e) not in used_ex and (day is None or e.day == day)]

        def one_to_one(day_offsets):
            for i in free_in():
                best, best_d = None, None
                for e in free_ex():
                    if abs((e.day - i.day).days) not in day_offsets:
                        continue
                    if abs(e.qty - i.qty) > QTY_EPS:
                        continue
                    d = abs(e.price - i.price)
                    if d <= price_tol(e.price) and (best is None or d < best_d):
                        best, best_d = e, d
                if best is not None:
                    used_in.add(id(i))
                    used_ex.add(id(best))
                    out.append(Match([i], [best]))

        one_to_one({0})
        for e in free_ex():                         # many incoming -> one existing
            sub = _subset(e, free_in(e.day))
            if sub:
                used_ex.add(id(e))
                used_in.update(id(s) for s in sub)
                out.append(Match(sub, [e]))
        for i in free_in():                         # one incoming -> many existing
            sub = _subset(i, free_ex(i.day))
            if sub:
                used_in.add(id(i))
                used_ex.update(id(s) for s in sub)
                out.append(Match([i], sub))
        one_to_one({1})
    return out


def day_from_match_key(mk: str | None) -> date | None:
    try:
        return date.fromisoformat((mk or "")[:10])
    except ValueError:
        return None



