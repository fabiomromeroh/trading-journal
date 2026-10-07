"""Persistence services: storing executions (with cross-source dedupe) and rebuilding trades."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.instruments import ExecRecord
from app.models import Account, Execution, Trade, TradeFill, utcnow
from app.timeutil import ET, local_to_utc_naive
from app.trade_builder import BuilderExec, build_trades

# Higher number = more authoritative when the same fill arrives from several sources.
SOURCE_QUALITY = {"demo": 0, "schwab_csv": 1, "tos_statement": 2, "schwab_api": 3}


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


def plan_ingest(db: Session, account_id: int, source: str, records: list[ExecRecord]):
    """Classify records as new / merge / duplicate without writing (used for import preview)."""
    ext_ids = {r.external_id for r in records}
    keys = {r.match_key() for r in records}
    existing_ext = set()
    if ext_ids:
        existing_ext = set(db.scalars(select(Execution.external_id).where(
            Execution.account_id == account_id, Execution.source == source,
            Execution.external_id.in_(ext_ids))))
    candidates: dict[str, list[Execution]] = defaultdict(list)
    if keys:
        for ex in db.scalars(select(Execution).where(
                Execution.account_id == account_id, Execution.source != source,
                Execution.match_key.in_(keys)).order_by(Execution.id)):
            candidates[ex.match_key].append(ex)
    plan = []
    seen_ext: set[str] = set()
    for r in records:
        if r.external_id in existing_ext or r.external_id in seen_ext:
            plan.append(("duplicate", r, None))
            continue
        seen_ext.add(r.external_id)
        cands = candidates.get(r.match_key())
        if cands:
            plan.append(("merge", r, cands.pop(0)))
        else:
            plan.append(("new", r, None))
    return plan


def ingest_records(db: Session, account_id: int, source: str, records: list[ExecRecord],
                   batch_id: int | None = None) -> IngestStats:
    stats = IngestStats()
    for action, rec, existing in plan_ingest(db, account_id, source, records):
        if action == "duplicate":
            stats.duplicates += 1
            if source == "schwab_api":  # API data can be corrected after the fact; refresh it.
                row = db.scalar(select(Execution).where(
                    Execution.account_id == account_id, Execution.source == source,
                    Execution.external_id == rec.external_id))
                if row is not None:
                    row.fees, row.price, row.quantity = rec.fees, rec.price, rec.quantity
        elif action == "merge":
            if _merge_into(existing, rec, source):
                stats.merged += 1
            else:
                stats.duplicates += 1
        else:
            db.add(_record_to_row(rec, account_id, source, batch_id))
            stats.inserted += 1
    db.flush()
    return stats


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
        db.execute(delete(TradeFill).where(TradeFill.trade_id.in_([t.id for t in existing.values()] or [-1])))
        seen = set()
        for bt in result.trades:
            first = by_id[bt.fills[0].execution_id]
            tr = existing.get(bt.key)
            if tr is None:
                tr = Trade(key=bt.key, account_id=acct_id)
                db.add(tr)
            seen.add(bt.key)
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
        for key, tr in existing.items():
            if key not in seen:
                db.delete(tr)
        set_state(db, f"orphans:{acct_id}", json.dumps([o.__dict__ for o in result.orphans]))
    db.flush()
    return total


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
