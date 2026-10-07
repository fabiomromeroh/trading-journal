"""Sync engine: runs every enabled data source, then rebuilds trades.

Entry points:
  * "Sync now" button (background thread, progress via HTMX polling)
  * `python -m app.sync` for the Render cron job (twice daily on weekdays)
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
from app.models import SyncRun, utcnow
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


def execute_run(run_id: int, sources=None, send_reminders: bool = False) -> SyncRun:
    if not _lock.acquire(blocking=False):
        db = dbmod.SessionLocal()
        run = db.get(SyncRun, run_id)
        db.close()
        return run
    db = dbmod.SessionLocal()
    try:
        run = db.get(SyncRun, run_id)
        ctx = SyncContext(trigger=run.trigger)
        sources = sources if sources is not None else all_sources()
        statuses, errors, used, touched = [], [], [], set()
        for src in sources:
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
        run.trades_built = rebuild_trades(db)
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
        run.message = "\n".join(ctx.log) or None
        if send_reminders:
            try:
                from app.notify import maybe_send_reminders
                sent = maybe_send_reminders(db, statuses)
                if sent:
                    run.message = ((run.message or "") + "\nReminders: " + "; ".join(sent)).strip()
            except Exception as exc:  # pragma: no cover
                log.warning("reminder failed: %s", exc)
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


def start_background(trigger: str = "manual") -> int:
    db = dbmod.SessionLocal()
    try:
        run, created = start_run(db, trigger)
        run_id = run.id
    finally:
        db.close()
    if created:
        threading.Thread(target=execute_run, args=(run_id,), daemon=True).start()
    return run_id


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Run all enabled data-source syncs and rebuild trades.")
    p.add_argument("--trigger", default="cron")
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
