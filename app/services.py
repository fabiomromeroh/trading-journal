"""Persistence services: storing executions (with cross-source dedupe) and rebuilding trades."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.instruments import ExecRecord
from app.matching import Item, day_from_match_key, match
from app.models import Account, Execution, Trade, TradeFill, utcnow
from app.timeutil import ET, et_date, local_to_utc_naive
from app.trade_builder import BuilderExec, build_trades

# Higher number = more authoritative when the same fill arrives from several sources.
SOURCE_QUALITY = {"demo": 0, "schwab_csv": 1, "snaptrade": 1, "tos_statement": 2, "schwab_api": 3}
# Sources whose records can be corrected after the fact; a re-delivered record refreshes the row.
REFRESHABLE_SOURCES = {"schwab_api", "snaptrade"}


@dataclass
class IngestStats:
    inserted: int = 0
    merged: int = 0
    duplicates: int = 0

    def as_dict(self):
        return {"inserted": self.inserted, "merged": self.merged, "duplicates": self.duplicates}


def _record_to_row(rec: ExecRecord, account_id: int, source: str, batch_id: int | None) -> Execution:
    return Execution(
        account_id=account_id, source=source, external_id=rec.external_id, match_key=rec.match_key(),
        import_batch_id=batch_id, symbol=rec.symbol, underlying=rec.underlying, asset_type=rec.asset_type,
        option_type=rec.option_type, strike=rec.strike, expiration=rec.expiration, multiplier=rec.multiplier,
        side=rec.side, quantity=rec.quantity, price=rec.price, fees=rec.fees, executed_at=rec.executed_at,
        time_known=rec.time_known, seq=rec.seq, position_effect=rec.position_effect, kind=rec.kind,
        description=rec.description, raw=json.dumps(rec.raw, default=str) if rec.raw is not None else None,
    )


def _merge_into(existing: Execution, rec: ExecRecord, source: str) -> bool:
    """Enrich an existing execution with better data from another source. Returns True if changed."""
    changed = False
    if rec.time_known and not existing.time_known:
        existing.executed_at, existing.time_known, existing.seq = rec.executed_at, True, rec.seq
        changed = True
    if rec.fees and not existing.fees:
        existing.fees = rec.fees
        changed = True
    if rec.position_effect and not existing.position_effect:
        existing.position_effect = rec.position_effect
        changed = True
    if SOURCE_QUALITY.get(source, 0) > SOURCE_QUALITY.get(existing.source, 0) and rec.raw is not None:
        existing.raw = json.dumps({"merged_from": source, "data": rec.raw}, default=str)
    return changed


def _rec_day(rec: ExecRecord):
    return rec.trade_date or (et_date(rec.executed_at) if rec.time_known else rec.executed_at.date())


def _row_day(row: Execution):
    return day_from_match_key(row.match_key) or (et_date(row.executed_at) if row.time_known
                                                  else row.executed_at.date())


def _aggregate(recs: list[ExecRecord]) -> ExecRecord:
    """Combine partial fills of one order into a single record (summed qty/fees, VWAP price,
    earliest exact time)."""
    if len(recs) == 1:
        return recs[0]
    timed = sorted((r for r in recs if r.time_known), key=lambda r: (r.executed_at, r.seq))
    lead = timed[0] if timed else recs[0]
    qty = sum(r.quantity for r in recs)
    px = sum(r.quantity * r.price for r in recs) / qty
    return replace(lead, quantity=qty, price=round(px, 6), fees=round(sum(r.fees for r in recs), 6),
                   raw={"partials": [r.raw for r in recs]} if lead.raw is not None else None)


@dataclass
class _Target:
    """Plan target for a matched record: the existing rows it corresponds to."""
    rows: list[Execution]
    agg: ExecRecord          # the incoming side, aggregated if several partial records
    lead: bool               # only the lead record of a group applies the merge
    group_size: int = 1
    outcome: dict | None = None   # shared by a group's records: {"changed": bool}


def plan_ingest(db: Session, account_id: int, source: str, records: list[ExecRecord]):
    """Classify records as new / merge / duplicate without writing (used for import preview).

    Returns (action, record, target) tuples; target is a _Target for merges."""
    ext_ids = {r.external_id for r in records}
    existing_ext = set()
    if ext_ids:
        existing_ext = set(db.scalars(select(Execution.external_id).where(
            Execution.account_id == account_id, Execution.source == source,
            Execution.external_id.in_(ext_ids))))
    fresh: list[ExecRecord] = []
    actions: dict[int, tuple] = {}
    seen_ext: set[str] = set()
    for idx, r in enumerate(records):
        if r.external_id in existing_ext or r.external_id in seen_ext:
            actions[idx] = ("duplicate", r, None)
        else:
            seen_ext.add(r.external_id)
            fresh.append(r)
    pos = {id(r): i for i, r in enumerate(records)}
    symbols = {r.symbol for r in fresh}
    cand_rows: list[Execution] = []
    if symbols:
        cand_rows = list(db.scalars(select(Execution).where(
            Execution.account_id == account_id, Execution.source != source,
            Execution.symbol.in_(symbols)).order_by(Execution.id)))
    inc = [Item(ref=r, day=_rec_day(r), symbol=r.symbol, side=r.side, kind=r.kind, qty=r.quantity,
                price=r.price, order=(r.executed_at, r.seq, pos[id(r)])) for r in fresh]
    exi = [Item(ref=e, day=_row_day(e), symbol=e.symbol, side=e.side, kind=e.kind, qty=e.quantity,
                price=e.price, order=(e.executed_at, e.seq or 0, e.id)) for e in cand_rows]
    for m in match(inc, exi):
        recs = [i.ref for i in m.incoming]
        rows = [e.ref for e in m.existing]
        agg = _aggregate(recs)
        outcome: dict = {}
        for k, r in enumerate(sorted(recs, key=lambda r: pos[id(r)])):
            actions[pos[id(r)]] = ("merge", r, _Target(rows=rows, agg=agg, lead=(k == 0), group_size=len(recs),
                                                       outcome=outcome))
    plan = []
    for idx, r in enumerate(records):
        plan.append(actions.get(idx) or ("new", r, None))
    return plan


def _replace_rows(db: Session, rows: list[Execution], rec: ExecRecord, account_id: int, source: str,
                  batch_id: int | None) -> Execution:
    """An authoritative API record supersedes file rows for the same fill(s): insert the API row
    (so later syncs recognise it by id) keeping the exact time / fees / position effect the file
    rows had, then drop the file rows."""
    timed = sorted((r for r in rows if r.time_known), key=lambda r: (r.executed_at, r.seq or 0))
    new = _record_to_row(rec, account_id, source, batch_id)
    if timed and not rec.time_known:
        new.executed_at, new.time_known, new.seq = timed[0].executed_at, True, timed[0].seq or 0
    if not new.fees:
        new.fees = round(sum(r.fees or 0 for r in rows), 6)
    if not new.position_effect:
        new.position_effect = next((r.position_effect for r in rows if r.position_effect), None)
    db.add(new)
    db.flush()
    _remember_trade_links(db, rows, new.id)
    for r in rows:
        db.delete(r)
    return new


def _remember_trade_links(db: Session, rows: list[Execution], new_exec_id: int) -> None:
    """Before dropping executions, note which trades used them so rebuild_trades can carry
    journal fields over to the trade that now contains the replacement execution."""
    links = db.info.setdefault("trade_exec_links", defaultdict(set))
    ids = [r.id for r in rows]
    for (trade_id,) in db.execute(select(TradeFill.trade_id).where(TradeFill.execution_id.in_(ids))):
        links[trade_id].add(new_exec_id)


def ingest_records(db: Session, account_id: int, source: str, records: list[ExecRecord],
                   batch_id: int | None = None, trace: dict | None = None) -> IngestStats:
    """Store records with cross-source dedupe. If `trace` is given it is filled with
    id(record) -> list of execution rows now representing that record."""
    stats = IngestStats()
    tr_ = trace if trace is not None else {}
    for action, rec, target in plan_ingest(db, account_id, source, records):
        if action == "duplicate":
            stats.duplicates += 1
            if source in REFRESHABLE_SOURCES:  # API data can be corrected after the fact; refresh it.
                row = db.scalar(select(Execution).where(
                    Execution.account_id == account_id, Execution.source == source,
                    Execution.external_id == rec.external_id))
                if row is not None:
                    row.fees, row.price, row.quantity = rec.fees, rec.price, rec.quantity
                    row.match_key = rec.match_key()
            if row_ := db.scalar(select(Execution).where(
                    Execution.account_id == account_id, Execution.source == source,
                    Execution.external_id == rec.external_id)):
                tr_[id(rec)] = [row_]
        elif action == "merge":
            tr_[id(rec)] = target.rows
            if not target.lead:            # partial fill folded into its group's merge
                if target.outcome.get("changed"):
                    stats.merged += 1
                else:
                    stats.duplicates += 1
                continue
            rows, agg = target.rows, target.agg
            replace_ok = (source in REFRESHABLE_SOURCES and target.group_size == 1
                          and all(r.source not in REFRESHABLE_SOURCES for r in rows))
            if replace_ok:
                new_row = _replace_rows(db, rows, rec, account_id, source, batch_id)
                tr_[id(rec)] = [new_row]
                target.outcome["changed"] = True
                stats.merged += 1
                continue
            changed = False
            for row in rows:
                part = agg
                if len(rows) > 1:  # one incoming row covers several partials: share fees by quantity
                    part = replace(agg, fees=round(agg.fees * row.quantity / agg.quantity, 6) if agg.quantity else 0)
                changed |= _merge_into(row, part, source)
            target.outcome["changed"] = changed
            if changed:
                stats.merged += 1
            else:
                stats.duplicates += 1
        else:
            new_row = _record_to_row(rec, account_id, source, batch_id)
            db.add(new_row)
            tr_[id(rec)] = [new_row]
            stats.inserted += 1
    db.flush()
    return stats


# ------------------------------------------------------------------ moving an import to another account
def _row_to_record(row: Execution, retime=None) -> ExecRecord:
    raw = None
    if row.raw:
        try:
            raw = json.loads(row.raw)
        except ValueError:
            raw = {"raw": row.raw}
    executed_at = retime(row.executed_at) if (retime and row.time_known) else row.executed_at
    day = et_date(executed_at) if row.time_known else _row_day(row)
    return ExecRecord(
        external_id=row.external_id, symbol=row.symbol, underlying=row.underlying, asset_type=row.asset_type,
        side=row.side, quantity=row.quantity, price=row.price, executed_at=executed_at, fees=row.fees or 0.0,
        time_known=row.time_known, seq=row.seq or 0, position_effect=row.position_effect, kind=row.kind,
        option_type=row.option_type, strike=row.strike, expiration=row.expiration,
        multiplier=row.multiplier or 1.0, description=row.description, raw=raw, trade_date=day)


def _tos_retimer(rows: list[Execution], assumed_tz: str):
    """Rows imported from a thinkorswim file were converted from `assumed_tz`. If the original local
    times fit US market hours better in another zone, return a function re-converting them."""
    from app.importers.tos_statement import detect_timezone
    from app.timeutil import utc_naive_to_tz, to_utc_naive
    from zoneinfo import ZoneInfo
    local = [utc_naive_to_tz(r.executed_at, assumed_tz).replace(tzinfo=None) for r in rows if r.time_known]
    tz = detect_timezone(local, assumed_tz)
    if tz == assumed_tz:
        return None, assumed_tz
    zi = ZoneInfo(tz)

    def retime(dt):
        return to_utc_naive(utc_naive_to_tz(dt, assumed_tz).replace(tzinfo=None).replace(tzinfo=zi))
    return retime, tz


@dataclass
class MoveResult:
    moved: int
    stats: IngestStats
    timezone: str | None
    journal_moved: int
    source_account_removed: bool


def reassign_batch(db: Session, batch, target_account_id: int) -> MoveResult:
    """Move a committed import's fills to another account, re-running cross-source matching there
    (so fills already present, e.g. from SnapTrade, are merged instead of duplicated). Journal fields
    on the old trades are carried to the matching trades in the target account."""
    from app.config import get_settings
    from app.models import ImportBatch
    src_id = batch.account_id
    rows = list(db.scalars(select(Execution).where(Execution.import_batch_id == batch.id)
                           .order_by(Execution.executed_at, Execution.seq, Execution.id)))
    retime, tz = (None, None)
    if batch.file_format == "tos_statement":
        retime, tz = _tos_retimer(rows, get_settings().tos_timezone)
    src_trades = list(db.scalars(select(Trade).where(Trade.account_id == src_id)))
    jexec = _journal_exec_sets(db, src_trades)
    journal = {t.id: t for t in src_trades if t.id in jexec}
    recs = [_row_to_record(r, retime) for r in rows]
    rec_by_row = {r.id: rec for r, rec in zip(rows, recs)}
    for r in rows:
        db.delete(r)
    db.flush()
    trace: dict = {}
    stats = ingest_records(db, target_account_id, batch.file_format, recs, batch_id=batch.id, trace=trace)
    batch.account_id = target_account_id
    batch.inserted, batch.merged, batch.duplicates = stats.inserted, stats.merged, stats.duplicates
    note = f"Moved from account #{src_id}" + (f"; exec times re-read as {tz}" if tz else "") + "."
    batch.notes = ((batch.notes or "") + " " + note).strip()
    rebuild_trades(db, [target_account_id])
    moved_j = 0
    if journal:
        tgt = list(db.scalars(select(Trade).where(Trade.account_id == target_account_id)))
        tgt_execs = {t.id: {f.execution_id for f in t.fills if f.execution_id} for t in tgt}
        for tid, old in journal.items():
            ids = set()
            for old_eid in jexec[tid]:
                rec = rec_by_row.get(old_eid)
                ids |= {x.id for x in trace.get(id(rec), [])} if rec is not None else set()
            before = (old.notes, old.setup, old.rating, len(old.tags))
            _carry_journal(old, ids, [(t, tgt_execs[t.id]) for t in tgt])
            moved_j += bool(ids) and any(before)
    rebuild_trades(db, [src_id])
    removed = False
    src = db.get(Account, src_id)
    if (src is not None and not src.is_demo and not src.external_ref
            and db.scalar(select(Execution.id).where(Execution.account_id == src_id).limit(1)) is None
            and db.scalar(select(ImportBatch.id).where(ImportBatch.account_id == src_id).limit(1)) is None):
        db.delete(src)
        removed = True
    db.flush()
    return MoveResult(moved=len(rows), stats=stats, timezone=tz, journal_moved=moved_j,
                      source_account_removed=removed)


def _expiry_close_utc(d):
    return local_to_utc_naive(d, time(16, 0), ET)


def rebuild_trades(db: Session, account_ids: list[int] | None = None) -> int:
    """Rebuild all trades for the given accounts from executions, preserving journal fields."""
    if account_ids is None:
        account_ids = list(db.scalars(select(Account.id)))
    total = 0
    for acct_id in account_ids:
        acct = db.get(Account, acct_id)
        if acct is None:
            continue
        execs = list(db.scalars(select(Execution).where(Execution.account_id == acct_id)))
        by_id = {e.id: e for e in execs}
        bexecs = [BuilderExec(
            id=e.id, account_id=e.account_id, symbol=e.symbol, side=e.side, quantity=e.quantity,
            price=e.price, executed_at=e.executed_at, fees=e.fees or 0.0, multiplier=e.multiplier or 1.0,
            position_effect=e.position_effect, kind=e.kind, time_known=e.time_known, seq=e.seq or 0,
        ) for e in execs]
        expirations = {e.symbol: _expiry_close_utc(e.expiration) for e in execs
                       if e.asset_type == "OPTION" and e.expiration}
        result = build_trades(bexecs, expirations=expirations, as_of=utcnow())

        existing = {t.key: t for t in db.scalars(select(Trade).where(Trade.account_id == acct_id))}
        old_execs = _journal_exec_sets(db, existing.values())
        db.execute(delete(TradeFill).where(TradeFill.trade_id.in_([t.id for t in existing.values()] or [-1])))
        seen = set()
        built: list[Trade] = []
        for bt in result.trades:
            first = by_id[bt.fills[0].execution_id]
            tr = existing.get(bt.key)
            if tr is None:
                tr = Trade(key=bt.key, account_id=acct_id)
                db.add(tr)
            seen.add(bt.key)
            built.append(tr)
            tr.symbol, tr.underlying, tr.asset_type = bt.symbol, first.underlying, first.asset_type
            tr.option_type, tr.strike, tr.expiration = first.option_type, first.strike, first.expiration
            tr.multiplier, tr.direction, tr.status = bt.multiplier, bt.direction, bt.status
            tr.opened_at, tr.closed_at, tr.time_known = bt.opened_at, bt.closed_at, bt.time_known
            tr.quantity, tr.open_quantity = bt.max_quantity, bt.open_quantity
            tr.entry_price, tr.exit_price, tr.cost_basis = bt.entry_price, bt.exit_price, bt.cost_basis
            tr.gross_pnl, tr.fees, tr.net_pnl = round(bt.gross_pnl, 6), round(bt.fees, 6), round(bt.net_pnl, 6)
            tr.return_pct, tr.close_reason, tr.is_demo = bt.return_pct, bt.close_reason, acct.is_demo
            tr.fills = [TradeFill(
                execution_id=f.execution_id, position=i, side=f.side, role=f.role, quantity=f.quantity,
                price=f.price, fees=f.fees, executed_at=f.executed_at) for i, f in enumerate(bt.fills)]
            total += 1
        new_execs = {tr.key: {f.execution_id for f in tr.fills if f.execution_id} for tr in built}
        for key, tr in existing.items():
            if key not in seen:
                _carry_journal(tr, old_execs.get(tr.id, set()),
                               [(x, new_execs[x.key]) for x in built])
                db.delete(tr)
        set_state(db, f"orphans:{acct_id}", json.dumps([o.__dict__ for o in result.orphans]))
    db.flush()
    db.info.pop("trade_exec_links", None)
    prune_unused_tags(db)
    return total


JOURNAL_FIELDS = ("notes", "setup", "rating")


def _has_journal(tr: Trade) -> bool:
    return any(getattr(tr, f) for f in JOURNAL_FIELDS) or bool(tr.tags)


def _journal_exec_sets(db: Session, trades) -> dict[int, set[int]]:
    """Execution ids of trades that carry journal data (incl. executions replaced during ingest)."""
    trades = [t for t in trades if _has_journal(t)]
    if not trades:
        return {}
    out: dict[int, set[int]] = defaultdict(set)
    for tid, eid in db.execute(select(TradeFill.trade_id, TradeFill.execution_id).where(
            TradeFill.trade_id.in_([t.id for t in trades]), TradeFill.execution_id.is_not(None))):
        out[tid].add(eid)
    links = db.info.get("trade_exec_links") or {}
    for t in trades:
        out[t.id] |= set(links.get(t.id, ()))
    return out


def _carry_journal(old: Trade, old_execs: set[int], candidates: list[tuple[Trade, set[int]]]) -> None:
    """A trade disappeared on rebuild (its identity changed, e.g. fills merged or re-ordered).
    Move its journal fields to the new trade sharing the most executions, without overwriting."""
    if not _has_journal(old) or not old_execs:
        return
    best, best_n = None, 0
    for tr, execs in candidates:
        n = len(old_execs & execs)
        if n > best_n and tr is not old:
            best, best_n = tr, n
    if best is None:
        return
    for f in JOURNAL_FIELDS:
        if getattr(old, f) and not getattr(best, f):
            setattr(best, f, getattr(old, f))
    for tag in old.tags:
        if tag not in best.tags:
            best.tags.append(tag)


def prune_unused_tags(db: Session) -> None:
    """Delete tags no longer attached to any trade (e.g. left over from cleared sample data)."""
    from app.models import Tag, trade_tags
    db.execute(delete(Tag).where(Tag.id.not_in(select(trade_tags.c.tag_id))))
    db.flush()


def get_state(db: Session, key: str, default: str | None = None) -> str | None:
    from app.models import AppState
    row = db.get(AppState, key)
    return row.value if row else default


def set_state(db: Session, key: str, value: str | None) -> None:
    from app.models import AppState
    row = db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value))
    else:
        row.value = value
