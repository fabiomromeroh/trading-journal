"""Rebuild IBKR accounts from statements already stored in the database.

Nothing here calls Interactive Brokers. A backup file is written before any row is deleted.
If that backup fails, the accounts are left untouched.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

STATE_KEY = "ibkr:rebuild"
_LOCK = threading.Lock()
_ACTIVE = False


def _inline() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def load(db: Session) -> dict:
    from app.services import get_state
    import json
    try:
        data = json.loads(get_state(db, STATE_KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save(db: Session, data: dict) -> None:
    import json
    from app.services import set_state
    set_state(db, STATE_KEY, json.dumps(data, default=str))


def public(data: dict | None) -> dict:
    raw = data if isinstance(data, dict) else {}
    return {
        "status": raw.get("status") or "",
        "scope": raw.get("scope") or "",
        "step": raw.get("step") or "",
        "error": raw.get("error"),
        "message": raw.get("message"),
        "account_name": raw.get("account_name") or "",
    }


def public_state(db: Session) -> dict:
    return public(load(db))


def running(db: Session) -> bool:
    if _ACTIVE:
        return True
    return (load(db).get("status") or "") in {"queued", "running"}


def _backup_path() -> Path:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        folder = Path("/tmp/ibkr-rebuild-backups")
    else:
        folder = Path("backups")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"ibkr-rebuild-{stamp}.json.gz"


def _targets(db: Session, account_id: int, scope: str) -> list[int]:
    from app.models import Account
    rows = list(db.scalars(select(Account).where(Account.source_key == "ibkr_flex").order_by(Account.id)))
    if scope == "all":
        return [a.id for a in rows]
    return [account_id] if any(a.id == account_id for a in rows) else []


def _wipe(db: Session, account_ids: list[int]) -> None:
    """Derived rows for these accounts only. Payloads, cash, dividends, and other brokers stay."""
    from app.models import (
        AppState, BalancePoint, CorporateAdjustment, Execution, IbkrFlexFact, Trade, TradeFill,
        TradeResult,
    )
    if not account_ids:
        return
    trade_ids = list(db.scalars(select(Trade.id).where(Trade.account_id.in_(account_ids))))
    if trade_ids:
        from app.models import TradeMistake, trade_tags
        db.execute(delete(trade_tags).where(trade_tags.c.trade_id.in_(trade_ids)))
        db.execute(delete(TradeMistake).where(TradeMistake.trade_id.in_(trade_ids)))
        db.execute(delete(TradeFill).where(TradeFill.trade_id.in_(trade_ids)))
        db.execute(delete(TradeResult).where(TradeResult.trade_id.in_(trade_ids)))
        db.execute(delete(Trade).where(Trade.id.in_(trade_ids)))
    db.execute(delete(Execution).where(Execution.account_id.in_(account_ids)))
    db.execute(delete(CorporateAdjustment).where(CorporateAdjustment.account_id.in_(account_ids)))
    db.execute(delete(BalancePoint).where(BalancePoint.account_id.in_(account_ids)))
    db.execute(delete(IbkrFlexFact).where(IbkrFlexFact.account_id.in_(account_ids)))
    for aid in account_ids:
        for key in (f"orphans:{aid}", f"balance:ibkr:{aid}", f"balance:ibkr:hist:{aid}"):
            row = db.get(AppState, key)
            if row is not None:
                db.delete(row)
    db.flush()


def _reimport(db: Session, account_ids: list[int]) -> None:
    """Replay stored JSON/XML for these accounts. Does not store another copy and does not call IBKR."""
    import json
    from app.adjustments import payload_body
    from app.models import SyncPayload
    from app.sources.ibkr_flex import (
        FlexError, IbkrFlexSource, SyncContext, SourceResult, load_credentials, parse_statements,
        parsed_from_payload,
    )
    src = IbkrFlexSource(sleep=lambda _n: None, split_fetch=lambda *_a, **_k: [])
    rows = list(db.scalars(select(SyncPayload).where(
        SyncPayload.source == "ibkr_flex", SyncPayload.account_id.in_(account_ids)).order_by(SyncPayload.id)))
    creds = load_credentials(db) or {}
    default_ccy = (creds.get("currency") or "USD").upper()
    ctx = SyncContext()
    res = SourceResult()
    seen_xml: set[tuple[int, str]] = set()
    for row in rows:
        body = payload_body(row)
        stmts = []
        if (row.content_type or "").find("xml") >= 0 or (row.filename or "").endswith(".xml"):
            try:
                stmts = parse_statements(body.decode("utf-8", errors="replace"))
            except FlexError:
                continue
            seen_xml.add((row.account_id or 0, (row.filename or "").replace(".xml", "")))
        else:
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            stem = (row.filename or "").replace(".json", "")
            if (row.account_id or 0, stem) in seen_xml:
                continue
            stmts = [parsed_from_payload(data)]
        if not stmts:
            continue
        src._import_statements(
            db, stmts, ctx, default_ccy, res, final=True,
            xml_text=None, xml_gzip=None, store_json=False, defer_build=True)


def _finish(db: Session, account_ids: list[int]) -> None:
    from app import balance, pagecache
    from app.models import Account
    from app.services import rebuild_trades
    from app.sources.ibkr_flex import IbkrFlexSource
    from app.transfers import pair_transfers
    touched = set(account_ids)
    touched.update(pair_transfers(db, None))
    ids = [aid for aid in sorted(touched) if db.get(Account, aid) is not None]
    src = IbkrFlexSource(sleep=lambda _n: None, split_fetch=lambda *_a, **_k: [])
    src._refresh_qty_flags(db, ids)
    rebuild_trades(db, ids)
    try:
        balance.rebuild(db, ids, fetch=lambda *_a, **_k: {})
    except Exception:  # noqa: BLE001 — the curve is extra; the trades are already rebuilt
        log.warning("IBKR rebuild balance pass failed", exc_info=True)
    from app.sources.ibkr_flex import finish_import_attention
    for aid in ids:
        acct = db.get(Account, aid)
        if acct is None or acct.source_key != "ibkr_flex":
            continue
        src._flag_orphans(db, aid, [])
        finish_import_attention(db, aid, [])
    pagecache.bump()


def run_job(db: Session | None = None) -> None:
    """One rebuild. Tests and the request path pass their own session; a background run opens one."""
    global _ACTIVE
    if not _LOCK.acquire(blocking=False):
        return
    _ACTIVE = True
    own = db is None
    if own:
        from app import db as dbmod
        db = dbmod.SessionLocal()
    try:
        data = load(db)
        if (data.get("status") or "") not in {"queued", "running"}:
            return
        data["status"] = "running"
        data["step"] = "backup"
        data["error"] = None
        save(db, data)
        db.commit()
        try:
            from app.backup import build, encode
            path = _backup_path()
            path.write_bytes(encode(build(db, include_cache=False)))
            data["backup"] = path.name
        except Exception:  # noqa: BLE001
            log.warning("IBKR rebuild backup failed", exc_info=True)
            data["status"] = "error"
            data["step"] = "backup"
            data["error"] = "Backup failed. Nothing was deleted."
            data["finished"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            save(db, data)
            db.commit()
            return
        scope = data.get("scope") or "account"
        account_id = int(data.get("account_id") or 0)
        targets = _targets(db, account_id, scope)
        if scope == "all":
            # Replay every account so the latest symbol for a conid wins. A one-account rebuild
            # must not wipe symbols learned from the other accounts.
            from app.services import set_state
            from app.sources.ibkr_flex import CONID_SYMBOL_STATE
            set_state(db, CONID_SYMBOL_STATE, None)
        data["step"] = "import"
        save(db, data)
        try:
            _wipe(db, targets)
            _reimport(db, targets)
            data["step"] = "trades"
            _finish(db, targets)
            from app.models import Execution
            n = db.scalar(select(Execution.id).where(Execution.account_id.in_(targets or [-1])).limit(1))
            data["status"] = "done"
            data["step"] = "done"
            data["error"] = None
            count = len(targets)
            data["message"] = f"Rebuilt {count} IBKR account{'s' if count != 1 else ''} from stored statements."
            if n is None and targets:
                data["message"] = (
                    f"Rebuilt {count} IBKR account{'s' if count != 1 else ''} from stored statements. "
                    "No fills were stored for this account.")
            data["finished"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            save(db, data)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("IBKR rebuild failed")
            data = load(db)
            data["status"] = "error"
            data["step"] = data.get("step") or "import"
            data["error"] = "Rebuild failed. Stored data was left as it was."
            data["finished"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            save(db, data)
            db.commit()
    finally:
        if own and db is not None:
            db.close()
        _ACTIVE = False
        _LOCK.release()


def request_rebuild(db: Session, account_id: int, scope: str, confirm: str) -> str:
    """Check the typed confirmation, then rebuild. A wrong confirmation deletes nothing."""
    from app.models import Account
    scope = (scope or "account").strip().lower()
    if scope not in {"account", "all"}:
        scope = "account"
    acct = db.get(Account, account_id)
    if acct is None or acct.source_key != "ibkr_flex":
        return "Choose an Interactive Brokers account to rebuild."
    token = (confirm or "").strip()
    if scope == "all":
        if token != "REBUILD":
            return "Type REBUILD to rebuild every IBKR account. Nothing was deleted."
    elif token != (acct.name or "") and token != "REBUILD":
        return f"Type {acct.name} or REBUILD to confirm. Nothing was deleted."
    if running(db):
        return "An IBKR rebuild is already running."
    save(db, {
        "status": "queued",
        "scope": scope,
        "account_id": acct.id,
        "account_name": acct.name,
        "step": "queued",
        "error": None,
        "message": None,
        "started": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "finished": None,
    })
    db.commit()
    if _inline():
        run_job(db)
        data = load(db)
        if data.get("status") == "error":
            return data.get("error") or "Rebuild failed. Nothing was deleted."
        return data.get("message") or "Rebuilt IBKR accounts from stored statements."
    threading.Thread(target=run_job, name="ibkr-rebuild", daemon=True).start()
    return "Rebuild started. Progress shows on the accounts page."
