"""Persistence services: storing executions (with cross-source dedupe) and rebuilding trades."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.instruments import ExecRecord
from app.matching import Item, Match, day_from_match_key, match
from app.models import Account, Execution, Trade, TradeFill, utcnow
from app.timeutil import ET, et_date, local_to_utc_naive
from app.trade_builder import BuilderExec, build_trades

# Higher number = more authoritative when the same fill arrives from several sources.
SOURCE_QUALITY = {"demo": 0, "schwab_csv": 1, "snaptrade": 1, "tos_statement": 2, "tos_email": 2, "schwab_api": 3}
# Sources whose records can be corrected after the fact; a re-delivered record refreshes the row.
REFRESHABLE_SOURCES = {"schwab_api", "snaptrade"}
# Provisional fills (e.g. built from same-day SnapTrade orders: no fees yet). The authoritative record
# from the linked source supersedes them; they are matched by shared id first, then like any fill.
PROVISIONAL_SOURCES = {"snaptrade_order"}
ID_LINKED_SOURCES = {"snaptrade": {"snaptrade_order"}, "snaptrade_order": {"snaptrade"}}


@dataclass
class IngestStats:
    inserted: int = 0
    merged: int = 0
    duplicates: int = 0
    detected_aliases: dict | None = None

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
    recs: list | None = None      # all incoming records of the group


class Plan(list):
    """List of (action, record, target) with the ticker renames detected while planning."""
    detected_aliases: dict


RENAME_PRICE_EPS = 0.00011


def _detect_renames(inc: list[Item], exi: list[Item], matched: list, source: str) -> dict[str, tuple[str, str]]:
    """Unmatched fills that agree exactly (day, side, qty, price to 4 dp) with an unmatched fill of a
    different ticker point to a ticker rename. Requires >= 2 such fills for the pair and no
    conflicting pairing. The thinkorswim ticker is taken as current (it rewrites history)."""
    used_in = {id(i) for m in matched for i in m.incoming}
    used_ex = {id(e) for m in matched for e in m.existing}
    li = [i for i in inc if id(i) not in used_in and " " not in i.symbol and i.kind == "TRADE"]
    le = [e for e in exi if id(e) not in used_ex and " " not in e.symbol and e.kind == "TRADE"]
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    for i in li:
        hits = {e.symbol for e in le if e.symbol != i.symbol and e.day == i.day and e.side == i.side
                and abs(e.qty - i.qty) < 1e-9 and abs(e.price - i.price) <= RENAME_PRICE_EPS}
        if len(hits) == 1:
            pairs[(i.symbol, hits.pop())] += 1
    out: dict[str, tuple[str, str]] = {}
    by_in: dict[str, list] = defaultdict(list)
    for (a, b), n in pairs.items():
        by_in[a].append((b, n))
    for a, lst in by_in.items():
        if len(lst) != 1 or lst[0][1] < 2:
            continue
        b, n = lst[0]
        if source == "tos_statement":
            old, new = b, a
        else:
            old, new = a, b
        exs = next((e for e in le if e.symbol == b), None)
        src_other = getattr(exs.ref, "source", "") if exs else ""
        if src_other == "tos_statement":
            old, new = a, b
        out[old] = (new, f"{n} fills identical except ticker ({a} in {source}, {b} in {src_other or 'existing'})")
    return out


def _base_id(ext: str | None) -> str:
    return (ext or "").split("#", 1)[0]


def _id_match(inc: list[Item], exi: list[Item], source: str) -> list[Match]:
    """Records and rows from linked sources that carry the same broker order id (e.g. a SnapTrade
    order and the activity it later becomes) are the same fill(s), whatever the price rounding.
    Multi-leg activity ids carry a '#n' suffix: all legs map to the one order row."""
    linked = ID_LINKED_SOURCES.get(source)
    if not linked:
        return []
    rows: dict[str, list[Item]] = defaultdict(list)
    for e in exi:
        if getattr(e.ref, "source", None) in linked:
            rows[_base_id(e.ref.external_id)].append(e)
    ins: dict[str, list[Item]] = defaultdict(list)
    for i in inc:
        if _base_id(i.ref.external_id) in rows:
            ins[_base_id(i.ref.external_id)].append(i)
    out = []
    for ref, group in ins.items():
        ex = rows[ref]
        if len({(x.symbol, x.side, x.kind) for x in group + ex}) != 1:
            continue  # ids agree but the fills don't: leave it to the regular matcher
        if abs(sum(x.qty for x in group) - sum(x.qty for x in ex)) > 1e-6:
            continue
        out.append(Match(list(group), list(ex)))
    return out


def plan_ingest(db: Session, account_id: int, source: str, records: list[ExecRecord],
                aliases: dict[str, str] | None = None) -> Plan:
    """Classify records as new / merge / duplicate without writing (used for import preview).

    Returns (action, record, target) tuples; target is a _Target for merges. Symbols are compared
    after applying ticker aliases; renames detected on the fly are applied and reported in
    plan.detected_aliases (persisted by ingest_records)."""
    from app.symbols import canonical_symbol, load_aliases, symbols_for
    aliases = dict(load_aliases(db) if aliases is None else aliases)
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
    cand_rows: list[Execution] = []
    if fresh:
        days = [_rec_day(r) for r in fresh]
        lo, hi = min(days) - timedelta(days=2), max(days) + timedelta(days=2)
        cand_rows = [e for e in db.scalars(select(Execution).where(
            Execution.account_id == account_id, Execution.source != source).order_by(Execution.id))
            if lo <= (_row_day(e) or lo) <= hi]

    def items(al):
        inc = [Item(ref=r, day=_rec_day(r), symbol=canonical_symbol(r.symbol, al), side=r.side, kind=r.kind,
                    qty=r.quantity, price=r.price, order=(r.executed_at, r.seq, pos[id(r)])) for r in fresh]
        exi = [Item(ref=e, day=_row_day(e), symbol=canonical_symbol(e.symbol, al), side=e.side, kind=e.kind,
                    qty=e.quantity, price=e.price, order=(e.executed_at, e.seq or 0, e.id)) for e in cand_rows]
        return inc, exi

    def match_all(inc, exi):
        by_id = _id_match(inc, exi, source)
        used = {id(x) for m in by_id for x in m.incoming + m.existing}
        return by_id + match([i for i in inc if id(i) not in used], [e for e in exi if id(e) not in used])

    inc, exi = items(aliases)
    matches = match_all(inc, exi)
    detected = _detect_renames(inc, exi, matches, source)
    if detected:
        aliases.update({old: new for old, (new, _e) in detected.items()})
        inc, exi = items(aliases)
        matches = match_all(inc, exi)
    for m in matches:
        recs = [i.ref for i in m.incoming]
        rows = [e.ref for e in m.existing]
        agg = _aggregate(recs)
        outcome: dict = {}
        for k, r in enumerate(sorted(recs, key=lambda r: pos[id(r)])):
            actions[pos[id(r)]] = ("merge", r, _Target(rows=rows, agg=agg, lead=(k == 0), group_size=len(recs),
                                                       outcome=outcome, recs=recs))
    plan = Plan(actions.get(idx) or ("new", r, None) for idx, r in enumerate(records))
    plan.detected_aliases = detected
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


def _supersede_provisional(db: Session, rows: list[Execution], recs: list[ExecRecord], account_id: int,
                           source: str, batch_id: int | None) -> list[Execution]:
    """Replace provisional rows by the authoritative records. Non-provisional rows in the match
    (e.g. a thinkorswim fill) are replaced as well, as _replace_rows does for file rows."""
    if len(recs) == 1:
        return [_replace_rows(db, rows, recs[0], account_id, source, batch_id)]
    timed = sorted((r for r in rows if r.time_known), key=lambda r: (r.executed_at, r.seq or 0))
    effect = next((r.position_effect for r in rows if r.position_effect), None)
    new_rows = []
    for rec in recs:
        new = _record_to_row(rec, account_id, source, batch_id)
        if timed and not rec.time_known:
            new.executed_at, new.time_known, new.seq = timed[0].executed_at, True, timed[0].seq or 0
        if not new.position_effect:
            new.position_effect = effect
        db.add(new)
        new_rows.append(new)
    db.flush()
    _remember_trade_links(db, rows, new_rows[0].id)
    for r in rows:
        db.delete(r)
    return new_rows


def prune_stale_provisional(db: Session, account_id: int, before_day) -> list[Execution]:
    """Provisional fills dated before `before_day` that no authoritative record superseded (the
    activity feed already covers that day): drop them so they can't double count."""
    stale = [e for e in db.scalars(select(Execution).where(
        Execution.account_id == account_id, Execution.source.in_(PROVISIONAL_SOURCES)))
        if (_row_day(e) or before_day) < before_day]
    for e in stale:
        db.delete(e)
    db.flush()
    return stale


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
    plan = plan_ingest(db, account_id, source, records)
    if plan.detected_aliases:
        from app.symbols import save_auto
        save_auto(db, plan.detected_aliases)
    stats.detected_aliases = plan.detected_aliases
    for action, rec, target in plan:
        if action == "duplicate":
            stats.duplicates += 1
            if source in REFRESHABLE_SOURCES | PROVISIONAL_SOURCES:  # API data can change later; refresh it.
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
            if source not in PROVISIONAL_SOURCES and any(r.source in PROVISIONAL_SOURCES for r in rows):
                # The authoritative fill(s) supersede provisional ones (keeping any exact time).
                new_rows = _supersede_provisional(db, rows, target.recs or [rec], account_id, source, batch_id)
                for r_, nr in zip(target.recs or [rec], new_rows):
                    tr_[id(r_)] = [nr]
                target.outcome["changed"] = True
                stats.merged += 1
                continue
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


def rematch_imports(db: Session, account_ids: list[int] | None = None) -> IngestStats:
    """Re-run cross-source matching for every committed import (in its own account), e.g. after a
    ticker alias was added so fills recorded under the old and new ticker are merged."""
    from app.models import ImportBatch
    total = IngestStats()
    q = select(ImportBatch).where(ImportBatch.status == "committed").order_by(ImportBatch.id)
    for b in db.scalars(q):
        if account_ids and b.account_id not in account_ids:
            continue
        if db.scalar(select(Execution.id).where(Execution.import_batch_id == b.id).limit(1)) is None:
            continue
        r = reassign_batch(db, b, b.account_id)
        total.merged += r.stats.merged
        total.inserted += r.stats.inserted
    return total


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

    def new_ids(tid):
        ids = set()
        for old_eid in jexec[tid]:
            rec = rec_by_row.get(old_eid)
            ids |= {x.id for x in trace.get(id(rec), [])} if rec is not None else {old_eid}
        return ids

    if src_id == target_account_id:  # re-match in place (e.g. after adding a ticker alias)
        links = db.info.setdefault("trade_exec_links", defaultdict(set))
        for tid in journal:
            links[tid] |= new_ids(tid)
        if tz or stats.merged:
            note = "Re-matched" + (f"; exec times re-read as {tz}" if tz else "") + "."
            batch.notes = ((batch.notes or "") + " " + note).strip()
        rebuild_trades(db, [target_account_id])
        db.flush()
        return MoveResult(moved=len(rows), stats=stats, timezone=tz, journal_moved=0,
                          source_account_removed=False)
    note = f"Moved from account #{src_id}" + (f"; exec times re-read as {tz}" if tz else "") + "."
    batch.notes = ((batch.notes or "") + " " + note).strip()
    rebuild_trades(db, [target_account_id])
    moved_j = 0
    if journal:
        tgt = list(db.scalars(select(Trade).where(Trade.account_id == target_account_id)))
        tgt_execs = {t.id: {f.execution_id for f in t.fills if f.execution_id} for t in tgt}
        for tid, old in journal.items():
            ids = new_ids(tid)
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
    from app.symbols import canonical_symbol, canonical_ticker, load_aliases
    if account_ids is None:
        account_ids = list(db.scalars(select(Account.id)))
    aliases = load_aliases(db)
    total = 0
    for acct_id in account_ids:
        acct = db.get(Account, acct_id)
        if acct is None:
            continue
        execs = list(db.scalars(select(Execution).where(Execution.account_id == acct_id)))
        by_id = {e.id: e for e in execs}
        canon = {e.id: canonical_symbol(e.symbol, aliases) for e in execs}
        bexecs = [BuilderExec(
            id=e.id, account_id=e.account_id, symbol=canon[e.id], side=e.side, quantity=e.quantity,
            price=e.price, executed_at=e.executed_at, fees=e.fees or 0.0, multiplier=e.multiplier or 1.0,
            position_effect=e.position_effect, kind=e.kind, time_known=e.time_known, seq=e.seq or 0,
        ) for e in execs]
        expirations = {canon[e.id]: _expiry_close_utc(e.expiration) for e in execs
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
            tr.symbol, tr.underlying, tr.asset_type = (bt.symbol, canonical_ticker(first.underlying, aliases),
                                                       first.asset_type)
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


JOURNAL_FIELDS = ("notes", "setup", "rating", "initial_stop", "risk_amount", "profit_target")


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
        db.flush()  # so a later get_state/set_state in the same session sees it
    else:
        row.value = value
