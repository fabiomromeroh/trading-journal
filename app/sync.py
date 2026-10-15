"""Sync engine: runs every enabled data source, then rebuilds trades.

Entry points (manual only; there is deliberately no scheduled sync):
  * "Sync now" button (background thread, progress via HTMX polling)
  * `python -m app.sync` CLI (e.g. run by hand from a shell)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import traceback
from datetime import timedelta

from sqlalchemy import select

from app import db as dbmod
from app.models import Account, SourceState, SyncRun, Trade, utcnow
from app.services import rebuild_trades
from app.sources import SyncContext, all_sources

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(minutes=30)
_lock = threading.Lock()


def running_sync(db) -> SyncRun | None:
    run = db.scalar(select(SyncRun).where(SyncRun.status == "running").order_by(SyncRun.id.desc()))
    if run and utcnow() - run.started_at > STALE_AFTER:
        run.status, run.finished_at, run.error = "failed", utcnow(), "Timed out / interrupted"
        db.commit()
        return None
    return run


def start_run(db, trigger: str) -> tuple[SyncRun, bool]:
    """Create a run, or return the one already in progress. Returns (run, created)."""
    existing = running_sync(db)
    if existing:
        return existing, False
    run = SyncRun(trigger=trigger, status="running")
    db.add(run)
    db.commit()
    return run, True


def _trade_snapshot(db) -> dict[str, tuple]:
    rows = db.execute(select(Trade.key, Trade.status, Trade.open_quantity, Trade.quantity, Trade.net_pnl,
                             Trade.closed_at, Trade.opened_at).where(Trade.is_demo.is_(False)))
    return {r[0]: (r[1], round(r[2] or 0, 6), round(r[3] or 0, 6), round(r[4] or 0, 2), r[5], r[6]) for r in rows}


def summarize(inserted: int, merged: int, before: dict, after: dict) -> str:
    new_trades = len(after.keys() - before.keys())
    updated = sum(1 for k in after.keys() & before.keys() if after[k] != before[k])
    removed = len(before.keys() - after.keys())
    if not inserted and not merged and not new_trades and not updated and not removed:
        return "No new fills. You're up to date."
    parts = [f"Added {inserted} new fill{'s' if inserted != 1 else ''}"]
    if merged:
        parts.append(f"{merged} merged into imported fills")
    trades = f"{new_trades} new trade{'s' if new_trades != 1 else ''}, {updated} updated"
    if removed:
        trades += f", {removed} replaced"
    return ", ".join(parts) + f"; {trades}."


def source_key_for_account(db, account: Account | None) -> str | None:
    """Adapter that can pull this account, or None for a file-import / sample account."""
    if account is None or account.is_demo:
        return None
    sk = (account.source_key or "").strip().lower()
    broker = (account.broker or "").strip().lower()
    ref = account.external_ref or ""
    if sk in {"t212", "trading212"} or broker in {"trading212", "t212", "trading 212"}:
        return "t212"
    if sk in {"ibkr", "ibkr_flex"} or broker in {"ibkr", "ibkr_flex", "interactivebrokers", "interactive brokers"}:
        return "ibkr_flex"
    if sk == "snaptrade" or ref.startswith("snaptrade:"):
        return "snaptrade"
    if sk == "schwab_api":
        return "schwab_api"
    found = db.scalar(select(SourceState.source).where(
        SourceState.account_id == account.id,
        SourceState.source.in_(("snaptrade", "schwab_api", "t212", "ibkr_flex"))))
    if found:
        return found
    # The Schwab API stores its account hash in external_ref. SnapTrade ids are prefixed and handled above.
    if ref and broker in {"schwab", "thinkorswim", "tos"}:
        return "schwab_api"
    return None


def _clear_synced_through(db, source: str, account_id: int | None) -> None:
    stmt = select(SourceState).where(SourceState.source == source)
    if account_id is not None:
        stmt = stmt.where(SourceState.account_id == account_id)
    for st in db.scalars(stmt):
        st.synced_through = None


def arm_full_history(db, account: Account | None) -> None:
    """Arm a one-off full re-scan. None means every connected source.

    SnapTrade and the Schwab API forget synced_through. Trading 212 / IBKR set FULL_STATE, which the next
    sync consumes. Other accounts keep their cursors when one account is chosen.
    """
    from app.services import set_state
    from app.sources.snaptrade import full_resync
    from app.sources.trading212 import FULL_STATE
    from app.sources.ibkr_flex import FULL_STATE as IBKR_FULL

    if account is None:
        full_resync(db)
        _clear_synced_through(db, "schwab_api", None)
        set_state(db, FULL_STATE, "1")
        set_state(db, IBKR_FULL, "1")
        db.commit()
        return
    key = source_key_for_account(db, account)
    if key == "t212":
        set_state(db, FULL_STATE, "1")
        db.commit()
    elif key == "ibkr_flex":
        set_state(db, IBKR_FULL, "1")
        db.commit()
    elif key == "snaptrade":
        full_resync(db, account.id)
    elif key == "schwab_api":
        _clear_synced_through(db, "schwab_api", account.id)
        db.commit()


def execute_run(run_id: int, sources=None, send_reminders: bool = False, only: set[str] | None = None,
                account_id: int | None = None) -> SyncRun:
    if not _lock.acquire(blocking=False):
        db = dbmod.SessionLocal()
        run = db.get(SyncRun, run_id)
        db.close()
        return run
    db = dbmod.SessionLocal()
    try:
        run = db.get(SyncRun, run_id)
        ctx = SyncContext(trigger=run.trigger, run_id=run.id, account_id=account_id)
        sources = sources if sources is not None else all_sources()
        if only:  # per-broker "Sync now"
            sources = [s for s in sources if s.key in only]
        statuses, errors, used, touched = [], [], [], set()
        before = _trade_snapshot(db)
        for src in sources:
            if src.is_configured():
                try:
                    src.refresh(db)
                except Exception as exc:  # status check failures are reported via status()
                    db.rollback()
                    log.warning("status refresh for %s failed: %s", src.key, exc)
            st = src.status(db)
            statuses.append((src.name, st))
            if not st.configured:
                continue
            if not st.ready:
                errors.append(f"{src.name}: {st.message}")
                continue
            used.append(src.name)
            try:
                r = src.sync(db, ctx)
                run.fetched += r.fetched
                run.inserted += r.inserted
                run.merged += r.merged
                touched.update(r.accounts)
            except Exception as exc:  # keep going with other sources
                db.rollback()
                log.exception("source %s failed", src.key)
                errors.append(f"{src.name}: {exc}")
        if touched:  # fold thinkorswim fill-email rows into the broker records that just arrived
            try:
                from app.email_sync import reconcile_after_sync
                with db.begin_nested():
                    reconcile_after_sync(db, touched)
            except Exception as exc:  # pragma: no cover
                log.warning("email fill reconcile failed: %s", exc)
        run.trades_built = rebuild_trades(db)
        db.flush()
        if touched:  # stored balance curve (deposits + realized + dividends + unrealized at daily closes)
            from app import balance
            balance.after_sync(db, touched)
            try:  # stored per-trade results for the touched accounts (pages then only read them)
                from app import results
                results.rebuild(db, sorted(touched) if all(isinstance(x, int) for x in touched) else None)
                from app import prices
                prices.prefetch_recent()
            except Exception as exc:  # noqa: BLE001 - pages rebuild stale rows on read anyway
                db.rollback()
                log.warning("trade results rebuild failed: %s", exc)
        summary = summarize(run.inserted, run.merged, before, _trade_snapshot(db)) if used else None
        run.sources = ", ".join(used) or None
        if not used and not errors:
            run.status = "skipped"
            ctx.info("No automated data source is enabled. Trades were rebuilt from imported files.")
        elif errors and not used:
            run.status = "failed"
        elif errors:
            run.status = "partial"
        else:
            run.status = "success"
        run.error = "\n".join(errors) or None
        run.message = "\n".join(([summary] if summary else []) + ctx.log) or None
        if send_reminders:
            try:
                from app.notify import maybe_send_reminders
                sent = maybe_send_reminders(db, statuses)
                if sent:
                    run.message = ((run.message or "") + "\nReminders: " + "; ".join(sent)).strip()
            except Exception as exc:  # pragma: no cover
                log.warning("reminder failed: %s", exc)
        try:  # new trades get the default stop (low of the entry day); price trouble never fails a sync
            from app import stops
            stops.backfill(db, limit=40)
        except Exception as exc:  # pragma: no cover
            db.rollback()
            log.warning("default-stop backfill failed: %s", exc)
        run.finished_at = utcnow()
        db.commit()
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(SyncRun, run_id)
        run.status, run.finished_at = "failed", utcnow()
        run.error = f"{exc}\n{traceback.format_exc()[-1500:]}"
        db.commit()
        return run
    finally:
        db.close()
        _lock.release()


def start_background(trigger: str = "manual", only: set[str] | None = None,
                     account_id: int | None = None) -> int:
    db = dbmod.SessionLocal()
    try:
        run, created = start_run(db, trigger)
        run_id = run.id
    finally:
        db.close()
    if created:
        threading.Thread(target=execute_run, args=(run_id,),
                         kwargs={"only": only, "account_id": account_id}, daemon=True).start()
    return run_id


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Run all enabled data-source syncs and rebuild trades.")
    p.add_argument("--trigger", default="cli")
    p.add_argument("--no-reminders", action="store_true")
    args = p.parse_args(argv)
    db = dbmod.SessionLocal()
    run, created = start_run(db, args.trigger)
    if not created:
        print(f"Another sync (#{run.id}) is already running; exiting.")
        db.close()
        return 0
    run_id = run.id
    db.close()
    run = execute_run(run_id, send_reminders=not args.no_reminders)
    print(f"Sync #{run.id}: {run.status} | sources={run.sources or 'none'} fetched={run.fetched} "
          f"inserted={run.inserted} merged={run.merged} trades={run.trades_built}")
    if run.message:
        print(run.message)
    if run.error:
        print("Errors:\n" + run.error, file=sys.stderr)
    return 1 if run.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
