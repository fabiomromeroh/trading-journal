"""Corporate actions and reconciliation adjustments.

A split, spin-off, stock distribution, conversion, free share or dividend reinvestment changes
the share count (and sometimes the average cost) without being a discretionary buy or sell.
The trade builder treats ``kind == "SPLIT"`` as a ratio applied to the open lots (no P&L) and
every other kind here as a buy or sell that must not open a short on its own.

When the broker feed does not contain the event, a sync books one estimated adjustment per
symbol (``external_id`` ``recon:<key>``, ``raw.estimated``) so the journal position matches the
broker. The row is editable and removable; a user edit is never overwritten.
"""
from __future__ import annotations

import re
from datetime import datetime

# Kinds that are not discretionary entries or exits. SPLIT rescales; the others are buys/sells.
NON_DISCRETIONARY_KINDS = frozenset({
    "SPLIT", "SPIN_OFF", "DISTRIBUTION", "CONVERSION", "DRIP", "FREE", "ADJUST", "TRANSFER",
})
# Share-count changes that go through the normal buy/sell path (SPLIT does not).
POSITION_KINDS = NON_DISCRETIONARY_KINDS - {"SPLIT"}

# Order-history fill.type -> journal kind. FOP is a fractional cash remainder, not shares.
FILL_KIND = {
    "STOCK_SPLIT": "SPLIT",
    "SPIN_OFF": "SPIN_OFF",
    "SPINOFF": "SPIN_OFF",
    "STOCK_DISTRIBUTION": "DISTRIBUTION",
    "CUSTOM_STOCK_DISTRIBUTION": "DISTRIBUTION",
    "EQUITY_RIGHTS": "DISTRIBUTION",
    "SCRIP_STOCK_DIVIDENDS": "DRIP",
    "STOCK_DIVIDENDS": "DISTRIBUTION",
    "STOCK_ACQUISITION": "CONVERSION",
    "CASH_AND_STOCK_ACQUISITION": "CONVERSION",
}

_CLEAN: list[float] = []
for _n in (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100):
    _CLEAN.append(float(_n))
    _CLEAN.append(1.0 / _n)
for _a, _b in ((3, 2), (2, 3), (4, 3), (3, 4), (5, 4), (4, 5), (5, 2), (2, 5), (5, 3), (3, 5)):
    _CLEAN.append(_a / _b)

_RATIO_NOTE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:for|:|to|-)\s*(\d+(?:\.\d+)?)", re.I)
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,8}([.\-][A-Z0-9]{1,4})?$")


def clean_ratio(r: float) -> float | None:
    """Snap ``r`` to a usual split ratio (2, 0.1, 3/2, …). 1.0 is not a split."""
    if r <= 0:
        return None
    if abs(r - 1.0) <= 0.02:
        return None
    best, best_err = None, 1e9
    for c in _CLEAN:
        err = abs(r - c) / c
        if err < 0.02 and err < best_err:
            best, best_err = c, err
    return best


def choose_split_ratio(pos: float, avg: float, reported_qty: float, reported_price: float) -> float | None:
    """Infer new/old from a broker split fill.

    The fill's quantity might be the new share count, the shares added or removed, or the ratio
    itself. A post-split price (about avg / ratio) picks the reading. Price 0 is not enough to
    guess, so the caller passes an explicit ratio (``position_effect == 'RATIO'``) instead.
    """
    p = abs(pos)
    q = abs(reported_qty)
    if q <= 0 or reported_price <= 0 or p <= 1e-12:
        return None
    cands = [q / p, (p + q) / p, q]
    if q < p - 1e-9:
        cands.append((p - q) / p)
    best, best_key = None, None
    for r in cands:
        if r <= 0 or r > 1000 or abs(r - 1.0) <= 0.02:
            continue
        snap = clean_ratio(r)
        use = snap or r
        expect = avg / use if avg else 0.0
        perr = abs(reported_price - expect) / expect if expect else 1.0
        if snap is None and perr > 0.15:
            continue
        key = (0 if snap else 1, perr)
        if best_key is None or key < best_key:
            best_key, best = key, use
    return best


def plan_adjustment(journal: float, broker: float, avg_cost: float) -> dict | None:
    """One estimated event that moves ``journal`` shares to ``broker`` shares. None when they match.

    A clean ratio (50 -> 5 is 1-for-10) becomes a split, which keeps cost and books no P&L.
    Extra broker shares are added at cost 0 (a received spin-off, or a buy we never saw).
    Extra journal shares are removed at average cost so the close books no P&L and no short.
    """
    if abs(journal - broker) <= 1e-4:
        return None
    if journal > 1e-4 and broker > 1e-4:
        snap = clean_ratio(broker / journal)
        if snap is not None:
            return {"kind": "SPLIT", "side": "BUY", "quantity": snap, "price": 0.0, "ratio": True,
                    "note": f"Reconciliation adjustment (estimated): split ×{snap:g} so "
                            f"{journal:g} shares match Trading 212's {broker:g}. No P&L."}
    delta = broker - journal
    if delta > 1e-4:
        return {"kind": "ADJUST", "side": "BUY", "quantity": round(delta, 8), "price": 0.0, "ratio": False,
                "note": "Reconciliation adjustment (estimated): shares added to match Trading 212 "
                        "(missing buy, spin-off or free shares). Cost is 0 until you edit it."}
    remove = min(-delta, max(journal, 0.0))
    if remove <= 1e-4:
        return None
    return {"kind": "ADJUST", "side": "SELL", "quantity": round(remove, 8), "price": round(avg_cost or 0.0, 8),
            "ratio": False,
            "note": "Reconciliation adjustment (estimated): shares removed to match Trading 212 "
                    "(conversion or missing close), priced at cost so it books no P&L."}


def transfer_matching_basis(plans: list[dict]) -> None:
    """When a removed lot and a new lot have the same share count, copy the cost across.

    HON 0.151647 closed and HONAV 0.151647 received is a conversion, not a free share.
    """
    used: set[int] = set()
    for buy in plans:
        if buy["side"] != "BUY" or buy["kind"] == "SPLIT" or buy["price"]:
            continue
        for sell in plans:
            if sell["side"] != "SELL" or id(sell) in used:
                continue
            if sell["quantity"] <= 0:
                continue
            if abs(buy["quantity"] - sell["quantity"]) / sell["quantity"] <= 0.02:
                buy["price"] = sell["price"]
                buy["note"] += f" Cost copied from {sell['symbol']} (same share count)."
                used.add(id(sell))
                break


def open_state(exec_rows) -> tuple[float, float]:
    """(signed open quantity, average cost of the remaining long) ignoring ``recon:`` rows."""
    from app.trade_builder import BuilderExec, build_trades

    rows = [e for e in exec_rows if not str(e.external_id).startswith("recon:")]
    if not rows:
        return 0.0, 0.0
    built = build_trades([BuilderExec(
        id=e.id or 0, account_id=e.account_id, symbol=e.symbol, side=e.side, quantity=e.quantity,
        price=e.price or 0.0, executed_at=e.executed_at, fees=e.fees or 0.0, multiplier=e.multiplier or 1.0,
        position_effect=e.position_effect, kind=e.kind or "TRADE", time_known=bool(e.time_known), seq=e.seq or 0,
    ) for e in rows])
    qty = cost = 0.0
    for t in built.trades:
        if t.status != "OPEN":
            continue
        lots: list[list[float]] = []
        for f in t.fills:
            if f.role == "OPEN":
                lots.append([f.quantity, f.price])
            elif f.role == "CLOSE":
                q = f.quantity
                while q > 1e-12 and lots:
                    m = min(q, lots[0][0])
                    lots[0][0] -= m
                    q -= m
                    if lots[0][0] <= 1e-12:
                        lots.pop(0)
        q = sum(a for a, _ in lots)
        c = sum(a * p for a, p in lots)
        sign = 1.0 if t.direction == "LONG" else -1.0
        qty += sign * q
        if sign > 0:
            cost += c
    avg = cost / qty if qty > 1e-8 else 0.0
    return qty, avg


def is_estimated(raw: dict | None) -> bool:
    return bool(raw and raw.get("estimated"))


def user_edited(raw: dict | None) -> bool:
    return bool(raw and raw.get("user_edited"))


def adjustment_when(rows, side: str, kind: str, now: datetime) -> datetime:
    """When an estimated adjustment should hit the position.

    A buy that covers shares already sold is placed one second before the first sell that would
    otherwise go short, so it extends the long instead of closing a phantom short later.
    A sell is placed one second after the last real fill (after the buys it offsets).
    A split is placed just after the last buy it should rescale. ``now`` is only a fallback when
    the symbol has no fills yet. The 'first seen' sync time is not an execution time.
    """
    from datetime import timedelta

    real = [e for e in rows if not str(getattr(e, "external_id", "") or "").startswith("recon:")]
    real.sort(key=lambda e: (e.executed_at, getattr(e, "id", 0) or 0))
    fills = [e for e in real if e.kind != "SPLIT"]
    if kind == "SPLIT":
        buys = [e.executed_at for e in fills if e.side == "BUY"]
        return (max(buys) + timedelta(seconds=1)) if buys else now
    if side == "SELL":
        return (max(e.executed_at for e in fills) + timedelta(seconds=1)) if fills else now
    pos = 0.0
    for e in real:
        if e.kind == "SPLIT" and e.position_effect == "RATIO" and e.quantity and abs(e.quantity - 1.0) > 1e-9:
            if pos > 1e-8:
                pos *= float(e.quantity)
            continue
        q = abs(e.quantity or 0.0)
        if e.side == "BUY":
            pos += q
        elif e.side == "SELL" and pos - q < -1e-6:
            return e.executed_at - timedelta(seconds=1)
        elif e.side == "SELL":
            pos -= q
    return max(e.executed_at for e in fills) if fills else now


def group_symbols(execs, instruments: dict, aliases: dict) -> dict[int, str]:
    """One position key per execution.

    Fills that share an ISIN but were stored under two tickers build as one trade, named with the
    symbol those fills actually used (the execution listing). A catalogue ticker that no longer
    matches the fills (T212 ``SNII`` while the Nasdaq line and the orders are ``RGTI``) is not
    adopted. A built-in alias still folds the old ticker into the current one (FB → META).
    Reconciliation rows follow that name. A user override is left alone.
    """
    from collections import defaultdict

    from app.symbols import canonical_symbol

    def base(e) -> str:
        return canonical_symbol(e.symbol or "", aliases) or (e.symbol or "")

    real_by_isin: dict[str, set[str]] = defaultdict(set)
    isin_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in execs:
        if str(e.external_id or "").startswith("recon:"):
            continue
        inst = instruments.get(e.instrument_id) if e.instrument_id else None
        if inst is not None and inst.isin and (e.kind or "TRADE") == "TRADE":
            name = base(e).upper()
            real_by_isin[inst.isin].add(name)
            isin_counts[inst.isin][name] += 1
    majority: dict[str, str] = {}
    for isin, counts in isin_counts.items():
        if len(real_by_isin.get(isin, ())) > 1:
            majority[isin] = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    out: dict[int, str] = {}
    renamed: dict[str, str] = {}
    for e in execs:
        b = base(e)
        inst = instruments.get(e.instrument_id) if e.instrument_id else None
        chosen = b
        if (inst is not None and inst.isin and not inst.user_override
                and inst.isin in majority
                and not str(e.external_id or "").startswith("recon:")):
            chosen = majority[inst.isin]
        out[e.id] = chosen
        if chosen.upper() != (e.symbol or "").upper() and not str(e.external_id or "").startswith("recon:"):
            renamed[(e.symbol or "").upper()] = chosen
    for e in execs:
        if not str(e.external_id or "").startswith("recon:"):
            continue
        alt = renamed.get((e.symbol or "").upper())
        if alt:
            out[e.id] = alt
    return out


def parse_yahoo_splits(payload: dict) -> list[tuple[datetime, float]]:
    """``[(ex-date UTC naive, new/old)]`` from a Yahoo v8 chart body. Cash dividends are ignored."""
    from datetime import timezone
    res = ((payload or {}).get("chart") or {}).get("result") or [None]
    res = res[0] if res else None
    if not res:
        return []
    out = []
    for ev in ((res.get("events") or {}).get("splits") or {}).values():
        try:
            num, den = float(ev["numerator"]), float(ev["denominator"])
            when = datetime.fromtimestamp(int(ev["date"]), timezone.utc).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if den <= 0 or num <= 0:
            continue
        ratio = num / den
        if abs(ratio - 1.0) <= 1e-6 or ratio > 1000 or ratio < 0.001:
            continue
        out.append((when, ratio))
    out.sort(key=lambda t: t[0])
    return out


def select_listing_splits(rows, splits: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Listing splits that land while this symbol is long, skipping ones already stored.

    ``rows`` are executions for one instrument. Reconciliation rows are ignored: they are the
    guess this split replaces. A split after the position is flat is not inserted (it would be
    an orphan and must not rescale a later, unrelated trade).
    """
    real = [e for e in rows if not str(e.external_id or "").startswith(("recon:", "ysplit:"))]
    existing: list[tuple[datetime, float | None]] = []
    events: list[tuple[datetime, str, float]] = []
    for e in real:
        if e.kind == "SPLIT" and e.position_effect == "RATIO" and e.quantity and abs(e.quantity - 1.0) > 1e-6:
            existing.append((e.executed_at, float(e.quantity)))
            events.append((e.executed_at, "SPLIT", float(e.quantity)))
        elif e.kind == "SPLIT":
            existing.append((e.executed_at, None))
        elif e.side in ("BUY", "SELL"):
            events.append((e.executed_at, e.side, abs(e.quantity or 0.0)))
    for when, ratio in splits:
        events.append((when, "NEW", float(ratio)))
    events.sort(key=lambda t: (t[0], {"BUY": 0, "NEW": 1, "SPLIT": 1, "SELL": 2}.get(t[1], 1)))
    pos = 0.0
    keep: list[tuple[datetime, float]] = []
    for when, kind, val in events:
        if kind == "BUY":
            pos += val
        elif kind == "SELL":
            pos -= val
        elif kind in ("SPLIT", "NEW") and pos > 1e-6:
            if kind == "NEW":
                covered = any(
                    abs((when - ew).total_seconds()) < 7 * 86400 and (er is None or abs(er - val) / val < 0.02)
                    for ew, er in existing)
                if not covered:
                    keep.append((when, val))
            pos *= val
    return keep


def needs_split_check(rows) -> bool:
    """True when a symbol already has an estimated row, or a sell runs past the shares we hold."""
    pos = 0.0
    for e in sorted(rows, key=lambda e: (e.executed_at, e.id or 0)):
        if str(e.external_id or "").startswith("recon:"):
            return True
        if e.kind == "SPLIT":
            if e.position_effect == "RATIO" and e.quantity and pos > 0:
                pos *= float(e.quantity)
            continue
        q = abs(e.quantity or 0.0)
        if e.side == "BUY":
            pos += q
        elif e.side == "SELL":
            if q > pos + 1e-4:
                return True
            pos -= q
    return False


def import_listing_splits(db, account_id: int, fetch) -> list[str]:
    """Insert one SPLIT row per listing split (Yahoo or ``fetch``) that the broker feed missed.

    Idempotent on ``ysplit:<symbol>:<date>``. An unedited estimated split on that symbol is removed
    when a listing split is inserted, so the position is not scaled twice. Journal notes live on
    trades and are untouched. ``fetch(symbol, start, end) -> [(datetime, ratio)]``; failures skip
    that symbol.
    """
    import json
    from collections import defaultdict
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Execution, Instrument

    execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
    by_inst: dict[int, list] = defaultdict(list)
    for e in execs:
        if e.instrument_id:
            by_inst[e.instrument_id].append(e)
    if not by_inst:
        return []
    instruments = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(list(by_inst))))}
    lines = []
    for iid, rows in by_inst.items():
        inst = instruments.get(iid)
        if inst is None or not inst.price_symbol or not needs_split_check(rows):
            continue
        start = min(e.executed_at for e in rows) - timedelta(days=7)
        end = max(e.executed_at for e in rows) + timedelta(days=2)
        try:
            found = list(fetch(inst.price_symbol, start, end) or [])
        except Exception:  # noqa: BLE001 — a quote miss must not abort the sync
            continue
        keep = select_listing_splits(rows, found)
        if not keep:
            continue
        symbol = next((e.symbol for e in rows if e.symbol and not str(e.external_id).startswith("recon:")), inst.symbol)
        removed = False
        for e in rows:
            if e.kind != "SPLIT" or not str(e.external_id or "").startswith("recon:"):
                continue
            try:
                raw = json.loads(e.raw or "{}")
            except ValueError:
                raw = {}
            if is_estimated(raw) and not user_edited(raw):
                db.delete(e)
                removed = True
        for when, ratio in keep:
            day = when.date().isoformat()
            ext = f"ysplit:{symbol}:{day}"
            if any(e.external_id == ext and e.source == "t212" for e in rows):
                continue
            row = Execution(
                account_id=account_id, source="t212", external_id=ext, symbol=symbol, underlying=symbol,
                asset_type="STOCK", side="BUY", quantity=ratio, price=0.0, fees=0.0,
                executed_at=when, time_known=True, currency=inst.currency, instrument_id=inst.id,
                position_effect="RATIO", kind="SPLIT", multiplier=1.0,
                description=f"Split ×{ratio:.6g} ({inst.price_symbol} listing)",
                match_key=f"{day}|{symbol}|BUY|{ratio:.6f}|0|SPLIT",
                raw=json.dumps({"listing_split": True, "price_symbol": inst.price_symbol, "ratio": ratio}),
            )
            db.add(row)
            from app.adjustments import remember_split
            remember_split(db, account_id, symbol, when, ratio, source="yahoo",
                           external_key=f"yahoo:{symbol}:{day}:{ratio:.8g}",
                           note=f"Split ×{ratio:.6g} ({inst.price_symbol} listing)")
            lines.append(f"{symbol}: split ×{ratio:.6g} on {day}")
        if removed and not lines:
            lines.append(f"{symbol}: estimated split replaced")
    if lines:
        db.flush()
    return lines


def omit_duplicate_corporate(db, account_id: int, records: list) -> list:
    """Drop a CSV corporate row when that symbol already has the same kind on the same day.

    The orders feed and the history CSV both describe one split. Applying both would scale the
    position twice. A cash or trade row is left for the normal match-key dedupe.
    """
    from sqlalchemy import select

    from app.models import Execution

    have = {
        ((s or "").upper(), k, (d.date() if d is not None else None))
        for s, k, d in db.execute(select(Execution.symbol, Execution.kind, Execution.executed_at).where(
            Execution.account_id == account_id, Execution.kind.in_(NON_DISCRETIONARY_KINDS)))
    }
    out = []
    for rec in records:
        if rec.kind in NON_DISCRETIONARY_KINDS:
            day = rec.trade_date or rec.executed_at.date()
            key = ((rec.symbol or "").upper(), rec.kind, day)
            if key in have:
                continue
            have.add(key)
        out.append(rec)
    return out


# ---------------------------------------------------------------- instruments
def _yahoo_base(price_symbol: str | None, exchange: str | None) -> str:
    from app.markets import EXCHANGES
    ps = price_symbol or ""
    suf = EXCHANGES[exchange].yahoo if exchange in EXCHANGES else ""
    if suf and ps.endswith(suf):
        return ps[: -len(suf)]
    return ps


def _majority_fill_symbol(counts: dict[str, int] | None) -> str | None:
    """The ticker the orders used, when one symbol has strictly more fills than the rest."""
    if not counts:
        return None
    top, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if n <= sum(counts.values()) - n:
        return None
    return top


def audit_instruments(db, *, fix: bool = False) -> list[dict]:
    """List instruments whose symbol, ISIN, currency or exchange disagree.

    With ``fix``, fill a missing exchange from the ISIN prefix, turn GBX into GBP, and rewrite a
    Yahoo symbol whose suffix does not match the exchange. A catalogue rename (price symbol base
    differs from the journal symbol) is kept: the chart follows the new ticker. A user override
    is never changed. Conflicting ISIN country vs an explicit ticker market is reported only.
    """
    from collections import defaultdict

    from sqlalchemy import func, select

    from app import markets
    from app.models import Execution, Instrument

    fill_counts: dict[int, dict[str, int]] = defaultdict(dict)
    for iid, sym, n in db.execute(select(Execution.instrument_id, Execution.symbol, func.count()).where(
            Execution.instrument_id.is_not(None),
            Execution.kind == "TRADE",
            ~Execution.external_id.like("recon:%"),
            ~Execution.external_id.like("ysplit:%")).group_by(Execution.instrument_id, Execution.symbol)):
        if iid and sym:
            fill_counts[iid][sym.upper()] = int(n)
    out = []
    rows = list(db.scalars(select(Instrument).order_by(Instrument.symbol)))
    for inst in rows:
        info = markets.parse_t212_ticker(inst.broker_symbol or "", inst.isin)
        isin_ex = markets.ISIN_PREFIX_TO_EXCHANGE.get((inst.isin or "")[:2].upper())
        notes, fixed = [], []
        if fix and not inst.user_override:
            if not inst.exchange:
                ex = info.exchange or isin_ex
                if ex:
                    inst.exchange = ex
                    fixed.append("exchange from " + ("ticker" if info.exchange else "ISIN"))
            if (inst.currency or "").upper() == "GBX":
                inst.currency = "GBP"
                if inst.exchange == "LSE":
                    inst.price_scale = 0.01
                fixed.append("GBX → GBP")
            base = _yahoo_base(inst.price_symbol, inst.exchange)
            journal = (inst.symbol or "").replace(".", "-")
            renamed = bool(base) and base.upper() not in {journal.upper(), (inst.symbol or "").upper()}
            if inst.exchange and not renamed:
                y = markets.yahoo_symbol_for(inst.symbol, inst.exchange)
                if y and inst.price_symbol != y:
                    inst.price_symbol = y
                    fixed.append("Yahoo symbol")
        if inst.isin and isin_ex and inst.exchange and isin_ex != inst.exchange and not info.exchange:
            notes.append(f"ISIN {inst.isin[:2]} implies {isin_ex} but exchange is {inst.exchange}")
        elif inst.isin and isin_ex and inst.exchange and isin_ex != inst.exchange:
            notes.append(f"listed on {inst.exchange}; ISIN country is {isin_ex} (listing kept)")
        if not inst.exchange:
            notes.append("no exchange")
        if not inst.price_symbol:
            notes.append("no Yahoo symbol")
        else:
            base = _yahoo_base(inst.price_symbol, inst.exchange)
            if base and base.upper() not in {(inst.symbol or "").upper(), (inst.symbol or "").replace(".", "-").upper()}:
                notes.append(f"chart {inst.price_symbol} (journal symbol {inst.symbol})")
            elif inst.exchange:
                y = markets.yahoo_symbol_for(inst.symbol, inst.exchange)
                if y and inst.price_symbol != y:
                    notes.append(f"Yahoo symbol {inst.price_symbol} should be {y}")
        if (inst.currency or "").upper() == "GBX":
            notes.append("currency still GBX")
        if inst.symbol and info.symbol and inst.symbol.upper() != info.symbol.upper() and not inst.user_override:
            notes.append(f"symbol {inst.symbol} != ticker base {info.symbol}")
        # The orders name the listing that was traded. A later catalogue ticker (SNII, XPOA, FB)
        # must not move the chart off that line. A user override (DMYI charted as IONQ) stays.
        fill_top = _majority_fill_symbol(fill_counts.get(inst.id))
        if fill_top and not inst.user_override and inst.exchange:
            y = markets.yahoo_symbol_for(fill_top, inst.exchange)
            if y and (inst.price_symbol or "").upper() != y.upper():
                notes.append(f"fills are {fill_top}; chart {inst.price_symbol or '—'} does not match")
                if fix:
                    inst.price_symbol = y
                    fixed.append(f"chart {y} from fills")
        if notes or fixed:
            out.append({"id": inst.id, "symbol": inst.symbol, "broker_symbol": inst.broker_symbol,
                        "isin": inst.isin, "exchange": inst.exchange, "price_symbol": inst.price_symbol,
                        "notes": notes, "fixed": fixed})
    if fix:
        db.flush()
    return out


def chart_symbol(symbol: str, exchange: str | None, short_name: str | None) -> str | None:
    """Yahoo symbol. A ticker-shaped catalogue shortName that differs is a rename (DMYI -> IONQ)."""
    from app.markets import yahoo_symbol_for
    sn = (short_name or "").strip().upper()
    if sn and _TICKER_RE.match(sn) and sn != (symbol or "").upper():
        return yahoo_symbol_for(sn, exchange)
    return yahoo_symbol_for(symbol, exchange)


# ---------------------------------------------------------------- CSV actions
_CASH_ACTIONS = ("deposit", "withdrawal", "interest", "cashback", "lending", "currency conversion",
                 "card ", "spending", "new card")


def classify_csv_action(action: str) -> tuple[str, str] | None:
    """(kind, side) for a Trading 212 CSV Action cell, or None when the row is not a share change."""
    a = " ".join((action or "").lower().split())
    if not a:
        return None
    if any(x in a for x in _CASH_ACTIONS):
        return None
    if "reinvest" in a:
        return ("DRIP", "BUY")
    if a.startswith("dividend") or a.startswith("withholding"):
        return None
    if "split" in a:
        return ("SPLIT", "BUY")
    if "spin" in a:
        return ("SPIN_OFF", "BUY")
    if "distribution" in a or "free share" in a or "bonus issue" in a:
        return ("DISTRIBUTION", "BUY")
    if "conversion" in a or "ticker change" in a or "rename" in a or "isin change" in a:
        return ("CONVERSION", "SELL" if "out" in a or "from" in a else "BUY")
    if "transfer in" in a:
        return ("TRANSFER", "BUY")
    if "transfer out" in a:
        return ("TRANSFER", "SELL")
    if "buy" in a:
        return ("TRADE", "BUY")
    if "sell" in a:
        return ("TRADE", "SELL")
    return None


def ratio_from_text(*parts: str) -> float | None:
    text = " ".join(p for p in parts if p)
    m = _RATIO_NOTE.search(text)
    if not m:
        return None
    new, old = float(m.group(1)), float(m.group(2))
    if old <= 0 or new <= 0:
        return None
    return new / old


def parse_csv_time(value: str) -> datetime | None:
    s = (value or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        from datetime import timezone
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d
