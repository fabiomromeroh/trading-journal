"""Stored per-trade results for page loads.

Pages used to load every trade with its fills, executions, account and instrument through the ORM
and convert each into the display currency on every click. Now each trade is stored once in
``trade_results``:

* ``NATIVE`` row: a snapshot of the trade and everything the stats read (trade columns, fills,
  their executions, account, instrument, tags, mistakes);
* ``USD`` / ``EUR`` rows: the display-currency conversion (money fields, per-fill rates, latest and
  entry rates) exactly as ``currency.convert`` produced it, or "none" when the trade is already in that
  currency / a rate was missing (the old path then showed native figures too).

Each row carries a ``sig`` of its inputs (the trade's rows read as plain tuples, plus the FX table
stamp and today's date for conversions). A row whose sig no longer matches is rebuilt from the
ORM with the original code, so sync / import / edit / merge / adjustment / FX changes rebuild exactly
the trades they touched. Filters stay SQL on ``trades``; stats run over the rebuilt objects, which
behave like the ORM trades (same classes' properties, same CTrade wrapper).

``full_view`` keeps the old computation for verification (tests compare both).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.models import (Account, Execution, Instrument, Tag, Trade, TradeFill, TradeMistake, TradeResult,
                        trade_tags, utcnow)
from app.payloads import pack_json, unpack_json

NATIVE = "NATIVE"
_SKIP = {Execution: {"raw"}}


def _cols(model):
    skip = _SKIP.get(model, set())
    return [a.key for a in inspect(model).column_attrs if a.key not in skip]


def _enc(v):
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, date):
        return {"$d": v.isoformat()}
    return v


def _dec(v):
    if isinstance(v, dict):
        if "$dt" in v:
            return datetime.fromisoformat(v["$dt"])
        if "$d" in v:
            return date.fromisoformat(v["$d"])
    return v


def _row(obj, model) -> dict:
    return {k: _enc(getattr(obj, k)) for k in _cols(model)}


# ------------------------------------------------------------------ lightweight objects
def _props(model):
    """Python-level properties / methods of the mapped class (not its mapped attributes)."""
    out = {}
    for k, v in vars(model).items():
        if k.startswith("__"):
            continue
        if isinstance(v, (property, staticmethod, classmethod)) or (callable(v) and not hasattr(v, "property")
                                                                      and type(v).__name__ == "function"):
            out[k] = v
    return out


def _cls(model):
    return type("Stored" + model.__name__, (), {"__slots__": ("__dict__",), **_props(model),
                                                  "__repr__": lambda s: f"<stored {model.__name__} {getattr(s, 'id', '')}>"})


SAccount, SInstrument, STag, SMistake, SExecution, SFill, STrade = (
    _cls(Account), _cls(Instrument), _cls(Tag), _cls(TradeMistake), _cls(Execution), _cls(TradeFill), _cls(Trade))


def _make(cls, d: dict):
    """``d`` is already decoded (see ``_load``)."""
    o = cls.__new__(cls)
    o.__dict__.update(d)
    return o


def _load(text: str) -> dict:
    def hook(o):
        if isinstance(o, dict) and ("$dt" in o or "$d" in o) and len(o) == 1:
            return _dec(o)
        return o
    return unpack_json(text, object_hook=hook)


# decoded payloads of this process, keyed (trade id, currency) -> (sig, payload); rows are immutable per sig
_MEM: dict = {}
_MEM_MAX = 20000


# ------------------------------------------------------------------ snapshot <-> trade
def snapshot(t: Trade) -> dict:
    return {
        "t": _row(t, Trade),
        "account": _row(t.account, Account) if t.account is not None else None,
        "instrument": _row(t.instrument, Instrument) if t.instrument is not None else None,
        "tags": [_row(x, Tag) for x in t.tags],
        "mistakes": [_row(x, TradeMistake) for x in t.mistake_rows],
        "fills": [{**_row(f, TradeFill), "execution": _row(f.execution, Execution) if f.execution is not None else None}
                  for f in t.fills],
    }


def restore(p: dict, shared: dict):
    t = _make(STrade, p["t"])
    a = p["account"]
    if a is not None:
        key = ("a", a["id"])
        if key not in shared:
            shared[key] = _make(SAccount, a)
        t.account = shared[key]
    else:
        t.account = None
    i = p["instrument"]
    if i is not None:
        key = ("i", i["id"])
        if key not in shared:
            shared[key] = _make(SInstrument, i)
        t.instrument = shared[key]
    else:
        t.instrument = None
    t.tags = [_make(STag, x) for x in p["tags"]]
    t.mistake_rows = [_make(SMistake, x) for x in p["mistakes"]]
    fills = []
    for fd in p["fills"]:
        fd = dict(fd)
        ex = fd.pop("execution")
        f = _make(SFill, fd)
        f.execution = None if ex is None else _make(SExecution, ex)
        if f.execution is not None and getattr(f.execution, "account_id", None) == getattr(t.account, "id", None):
            f.execution.account = t.account
        f.trade = t
        fills.append(f)
    t.fills = fills
    return t


# ------------------------------------------------------------------ signatures
def _h(*parts) -> str:
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def native_sigs(db: Session, ids: list[int], trows: dict | None = None) -> dict[int, str]:
    """Cheap fingerprint: trade stamp, fill/execution aggregates, tags, and the small account/instrument rows.

    Fill and execution bodies (price, description, raw) are not selected. ``updated_at`` on those
    rows changes when the ORM writes them, which is every mutation path in this app.
    """
    if not ids:
        return {}
    if trows is None:
        trows = {r[0]: r for r in db.execute(select(
            Trade.id, Trade.updated_at, Trade.account_id, Trade.instrument_id).where(Trade.id.in_(ids)))}
    fills: dict[int, tuple] = {}
    for tid, n, mx, mu in db.execute(select(
            TradeFill.trade_id, func.count(TradeFill.id), func.max(TradeFill.id), func.max(TradeFill.updated_at))
            .where(TradeFill.trade_id.in_(ids)).group_by(TradeFill.trade_id)):
        fills[tid] = (int(n or 0), mx, mu)
    execs: dict[int, tuple] = {}
    for tid, n, mx, mu in db.execute(select(
            TradeFill.trade_id, func.count(Execution.id), func.max(Execution.id), func.max(Execution.updated_at))
            .select_from(TradeFill)
            .outerjoin(Execution, Execution.id == TradeFill.execution_id)
            .where(TradeFill.trade_id.in_(ids)).group_by(TradeFill.trade_id)):
        execs[tid] = (int(n or 0), mx, mu)
    tags: dict[int, list] = {}
    for tid, name in db.execute(select(trade_tags.c.trade_id, Tag.name).join(Tag, Tag.id == trade_tags.c.tag_id)
                                .where(trade_tags.c.trade_id.in_(ids))):
        tags.setdefault(tid, []).append(name)
    mis: dict[int, list] = {}
    for tid, name in db.execute(select(TradeMistake.trade_id, TradeMistake.name).where(TradeMistake.trade_id.in_(ids))):
        mis.setdefault(tid, []).append(name)
    aids = {r[2] for r in trows.values() if r[2] is not None}
    iids = {r[3] for r in trows.values() if r[3] is not None}
    accts: dict = {}
    if aids:
        keys = _cols(Account)
        for r in db.execute(select(*[getattr(Account, c) for c in keys]).where(Account.id.in_(aids))):
            accts[dict(zip(keys, r))["id"]] = tuple(r)
    insts: dict = {}
    if iids:
        keys = _cols(Instrument)
        for r in db.execute(select(*[getattr(Instrument, c) for c in keys]).where(Instrument.id.in_(iids))):
            insts[dict(zip(keys, r))["id"]] = tuple(r)
    out = {}
    for tid, r in trows.items():
        out[tid] = _h(tuple(r), fills.get(tid), execs.get(tid), sorted(tags.get(tid, [])), sorted(mis.get(tid, [])),
                      accts.get(r[2]), insts.get(r[3]))
    return out


def fx_stamp(db: Session) -> str:
    from app import currency
    return currency.fx_stamp(db)


# ------------------------------------------------------------------ build / read
def _conv_payload(c) -> dict | None:
    if c is None:
        return None
    return {"money": c.money, "rates": {str(k): v for k, v in c.rates.items()}, "latest": c.latest, "entry": c.entry}


def _store(db: Session, trades: list[Trade], ccys: set[str], sigs: dict[int, str], fx: str) -> dict:
    """Snapshot + conversions of ORM trades with the original code; returns {(id, ccy): payload}."""
    from app import currency
    out = {}
    book = None
    need = [t for t in trades if any(currency.trade_ccy(t) != c for c in ccys)]
    if need:
        book = currency.RateBook(db)
        start, today = currency._span(need)
        book.ensure({currency.trade_ccy(t) for t in need} | {currency.account_ccy(t) for t in need} | set(ccys), start, today)
        fx = fx_stamp(db)  # the top-up may have added rates
    existing = {(r.trade_id, r.currency): r for r in db.scalars(select(TradeResult).where(
        TradeResult.trade_id.in_([t.id for t in trades]), TradeResult.currency.in_([NATIVE, *ccys])))}
    now = utcnow()
    for t in trades:
        snap = snapshot(t)
        rows = {NATIVE: (sigs[t.id], snap, t)}
        for c in ccys:
            conv = None if currency.trade_ccy(t) == c else currency.convert(book, t, c)
            rows[c] = (_h(sigs[t.id], fx, c), {"conv": _conv_payload(conv)}, conv)
        for c, (sig, payload, _o) in rows.items():
            out[(t.id, c)] = payload
            m = payload["conv"]["money"] if c != NATIVE and payload.get("conv") else None
            src = m or {"net_pnl": t.net_pnl, "gross_pnl": t.gross_pnl, "fees": t.fees, "mfe": t.mfe, "mae": t.mae,
                        "risk_amount": t.risk_amount}
            r = existing.get((t.id, c))
            if r is None:
                r = TradeResult(trade_id=t.id, currency=c)
                db.add(r)
            r.sig, r.payload, r.computed_at = sig, pack_json(payload), now
            r.account_id, r.symbol, r.status, r.direction, r.setup = t.account_id, t.symbol, t.status, t.direction, t.setup
            r.opened_at, r.closed_at = t.opened_at, t.closed_at
            r.net, r.gross, r.fees = src.get("net_pnl"), src.get("gross_pnl"), src.get("fees")
            r.mfe, r.mae, r.risk = src.get("mfe"), src.get("mae"), src.get("risk_amount")
    return out


def _remember(k, sig, p):
    if sig is None:
        return
    if len(_MEM) >= _MEM_MAX:
        _MEM.clear()
    _MEM[k] = (sig, p)


def view(db: Session, stmt, display: str | None = None) -> list:
    """Same result as ``currency.view(db, load_trades(db, stmt))``, read from stored rows.
    ``stmt`` is any select(Trade) with filters / order / limit (run in SQL)."""
    from app import currency
    display = (display or currency.get_display(db)).upper()
    # the caller's statement, same FROM / WHERE / ORDER (so the same row order), as plain rows
    trows = {}
    ids = []
    for r in db.execute(stmt.with_only_columns(Trade.id, Trade.updated_at, Trade.account_id, Trade.instrument_id)):
        if r[0] not in trows:
            ids.append(r[0])
            trows[r[0]] = r
    if not ids:
        return []
    sigs = native_sigs(db, ids, trows)
    fx = fx_stamp(db)
    want = {}
    for i in ids:
        want[(i, NATIVE)] = sigs.get(i)
        want[(i, display)] = _h(sigs.get(i), fx, display)
    stored = {(a, b): sg for a, b, sg in db.execute(select(TradeResult.trade_id, TradeResult.currency, TradeResult.sig)
                                                   .where(TradeResult.trade_id.in_(ids),
                                                          TradeResult.currency.in_([NATIVE, display])))}
    payloads, fetch = {}, []
    for k, sg in want.items():
        if stored.get(k) != sg:
            continue
        m = _MEM.get(k)
        if m is not None and m[0] == sg:
            payloads[k] = m[1]
        else:
            fetch.append(k)
    if fetch:
        fids = sorted({k[0] for k in fetch})
        for a, b, sg, text in db.execute(select(TradeResult.trade_id, TradeResult.currency, TradeResult.sig,
                                                TradeResult.payload).where(TradeResult.trade_id.in_(fids),
                                                                           TradeResult.currency.in_([NATIVE, display]))):
            if want.get((a, b)) == sg:
                payloads[(a, b)] = p = _load(text)
                _remember((a, b), sg, p)
    stale = [i for i in ids if (i, NATIVE) not in payloads or (i, display) not in payloads]
    if stale:
        from app.web import trade_options
        orm = list(db.scalars(select(Trade).where(Trade.id.in_(stale)).options(*trade_options())))
        built = _store(db, orm, {display}, sigs, fx)
        for k, p in built.items():  # same decoded form as a stored read
            payloads[k] = p = _load(pack_json(p))
            _remember(k, want.get(k), p)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - a read never fails because the cache could not be written
            db.rollback()
    shared: dict = {}
    out = []
    for i in ids:
        if (i, NATIVE) not in payloads:
            continue  # deleted while reading
        t = restore(payloads[(i, NATIVE)], shared)
        conv = payloads[(i, display)].get("conv")
        if conv is None:
            out.append(t)
        else:
            c = currency._Conv(dict(conv["money"]), {int(k): v for k, v in conv["rates"].items()}, conv["latest"], conv["entry"])
            out.append(currency.CTrade(t, display, c))
    return out


def full_view(db: Session, stmt, display: str | None = None) -> list:
    """The old computation (ORM load + live conversion), kept for verification."""
    from app import currency
    from app.web import load_trades
    return currency.view(db, load_trades(db, stmt), display)


def rebuild(db: Session, account_ids: list[int] | None = None, currencies=("USD", "EUR"), full: bool = False) -> int:
    """Bring the stored rows up to date (after a sync / import / settings change) so the next page load is a
    read. Only trades whose inputs changed are recomputed, unless ``full``."""
    q = select(Trade).order_by(Trade.id)
    if account_ids:
        q = q.where(Trade.account_id.in_(account_ids))
    if full:
        d = db.query(TradeResult)
        if account_ids:
            d = d.filter(TradeResult.account_id.in_(account_ids))
        d.delete(synchronize_session=False)
        db.commit()
    n = 0
    for c in currencies:
        n = len(view(db, q, c))
    return n

def enabled() -> bool:
    import os
    return os.environ.get("TRADE_RESULTS", "on").lower() not in ("off", "0", "false")


def trades(db: Session, stmt, display: str | None = None) -> list:
    """Display-currency trades for ``stmt``: stored results, or the old full computation when TRADE_RESULTS=off."""
    return view(db, stmt, display) if enabled() else full_view(db, stmt, display)
