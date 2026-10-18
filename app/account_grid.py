"""Accounts grid: one selected account's panel, and the add / edit / delete modals.

Read paths use the database only. They must not call a broker, and they must not start a sync.
Interactive Brokers in particular is never contacted just because an account panel opened.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account, BalancePoint, CashFlow, CorporateAdjustment, Dividend, Execution, ImportBatch,
    SourceState, SyncPayload, SyncRun, Trade,
)
from app.services import get_state


def broker_label(account) -> str:
    b = (getattr(account, "broker", None) or "").strip().lower()
    s = (getattr(account, "source_key", None) or "").strip().lower()
    blob = b or s
    if blob in {"ibkr", "ibkr_flex", "interactivebrokers", "interactive brokers"}:
        return "Interactive Brokers"
    if blob in {"t212", "trading212", "trading 212"}:
        return "Trading 212"
    if blob in {"schwab", "thinkorswim", "tos", "snaptrade", "schwab_api", "schwab_csv", "tos_email"}:
        return "Schwab"
    if blob == "demo" or getattr(account, "is_demo", False):
        return "Sample"
    if blob in {"other", "csv"}:
        return "Other"
    return (getattr(account, "broker", None) or "Account").strip() or "Account"


def status_label(dot: str | None) -> str:
    return {"ok": "OK", "error": "Error", "paused": "Paused"}.get(dot or "never", "Never synced")


def present_row(row: dict) -> dict:
    a = row["a"]
    row["broker_label"] = broker_label(a)
    row["masked"] = a.account_number_masked or ""
    row["status_label"] = status_label(row.get("dot"))
    return row


def safe_next(raw: str, default: str) -> str:
    """Same-site settings path only. Anything else keeps the endpoint's old redirect."""
    text = (raw or "").strip()
    if not text.startswith("/settings") or text.startswith("//"):
        return default
    if any(ch in text for ch in ("\\", "\n", "\r")) or ".." in text:
        return default
    return text


def removal_counts(db: Session, account_id: int) -> dict:
    def n(model):
        return db.scalar(select(func.count()).select_from(model).where(model.account_id == account_id)) or 0

    return {
        "executions": n(Execution),
        "trades": n(Trade),
        "corporate_actions": n(CorporateAdjustment),
        "sync_files": n(SyncPayload),
        "sync_cursors": n(SourceState),
        "dividends": n(Dividend),
        "cash_flows": n(CashFlow),
        "balances": n(BalancePoint),
        "imports": n(ImportBatch),
    }


def removal_bits(counts: dict) -> list[str]:
    labels = (
        ("executions", "execution", "executions"),
        ("trades", "trade", "trades"),
        ("corporate_actions", "corporate action", "corporate actions"),
        ("sync_files", "sync file", "sync files"),
        ("sync_cursors", "sync cursor", "sync cursors"),
        ("dividends", "dividend", "dividends"),
        ("cash_flows", "cash movement", "cash movements"),
        ("balances", "balance point", "balance points"),
        ("imports", "import", "imports"),
    )
    bits = []
    for key, one, many in labels:
        c = int(counts.get(key) or 0)
        if c:
            bits.append(f"{c} {one if c == 1 else many}")
    return bits


def removal_text(name: str, counts: dict) -> str:
    bits = removal_bits(counts)
    head = f"This removes the account “{name}” only."
    if not bits:
        return head + " It has no executions, trades, sync history, or corporate actions. Other accounts are not removed."
    return head + " Also removed: " + ", ".join(bits) + ". Other accounts are not removed."


def _sources(db: Session):
    from app.sources import all_sources
    # status() reads stored credentials and the last result. It does not call the broker.
    return [(s, s.status(db)) for s in all_sources()]


def _snap(db: Session):
    from app.routes.settings import _snaptrade_ctx
    return _snaptrade_ctx(db)


def _runs_for(db: Session, account_id: int):
    payloads = list(db.execute(select(
        SyncPayload.id, SyncPayload.account_id, SyncPayload.source, SyncPayload.created_at,
        SyncPayload.filename, SyncPayload.content_type, SyncPayload.gzipped, SyncPayload.nbytes,
        SyncPayload.sync_run_id,
    ).where(SyncPayload.account_id == account_id).order_by(SyncPayload.id.desc()).limit(20)).all())
    ids = [p.sync_run_id for p in payloads if p.sync_run_id]
    runs = []
    if ids:
        runs = list(db.scalars(select(SyncRun).where(SyncRun.id.in_(ids)).order_by(SyncRun.id.desc())))
    return runs, payloads


def _row_for(db: Session, account: Account, ibkr: dict, t212: dict, snap, sources) -> dict:
    from app.routes.settings import _decorate_accounts
    orphans = json.loads(get_state(db, f"orphans:{account.id}") or "[]")
    n_exec = db.scalar(select(func.count(Execution.id)).where(Execution.account_id == account.id)) or 0
    n_trades = db.scalar(select(func.count(Trade.id)).where(Trade.account_id == account.id)) or 0
    row = {"a": account, "executions": n_exec, "trades": n_trades, "orphans": orphans}
    _decorate_accounts([row], ibkr, t212, snap, sources)
    return present_row(row)


def _sync_kind(db: Session, bucket: str, account: Account) -> str:
    if account.is_demo:
        return "none"
    if bucket == "ibkr":
        return "ibkr"
    if bucket == "t212":
        return "t212"
    # Schwab, CSV, and any other non-demo account keep the per-account Sync new / Full history buttons.
    # source_key_for_account is read inside the sync endpoint, not here, so opening the panel stays local.
    return "generic"


def load_panel(request, db: Session, key: str) -> dict | None:
    """One account panel. ``None`` when the key is not an account. Never calls a broker."""
    key = (key or "").strip()
    if key in ("", "0", "none"):
        return {"kind": "empty", "key": ""}
    if key == "tos-email":
        from app.routes.email_sync import settings_ctx as tos_ctx
        return {"kind": "tos", "key": "tos-email", "bucket": "tos", "account": None,
                "tos_email": tos_ctx(request, db), "attention": [], "accounts": []}
    if not key.isdigit():
        return None
    account = db.get(Account, int(key))
    if account is None:
        return None
    from app.adjustments import attention_view
    from app.routes.brokers import settings_ctx as brokers_ctx
    from app.routes.settings import _acct_bucket
    from app.config import get_settings

    bctx = brokers_ctx(db)  # credentials, last status, balances already stored. No HTTP.
    sources = _sources(db)
    snap = _snap(db)
    ibkr = dict(bctx.get("ibkr") or {})
    t212 = dict(bctx.get("t212") or {})
    bucket = _acct_bucket(account)
    ibkr["balance_checks"] = [c for c in (ibkr.get("balance_checks") or []) if c.get("id") == account.id]
    ibkr["mapped"] = [account] if bucket == "ibkr" else []
    row = _row_for(db, account, ibkr, t212, snap, sources)
    attention = [a for a in attention_view(db) if a.account_id == account.id]
    runs, payloads = _runs_for(db, account.id)
    return {
        "kind": "account", "key": str(account.id), "bucket": bucket, "account": account, "row": row,
        "ibkr": ibkr, "t212": t212, "snaptrade": snap, "sources": sources,
        "runs": runs, "payloads": payloads, "attention": attention, "accounts": [account],
        "sync_kind": _sync_kind(db, bucket, account), "s": get_settings(),
        "connected": bool(ibkr.get("connected")) if bucket == "ibkr" else bool(t212.get("connected")) if bucket == "t212" else None,
    }


def modal_brokers(request, db: Session) -> dict:
    """Shared connection forms for the add modal. Database only."""
    from app.config import get_settings
    from app.routes.brokers import settings_ctx as brokers_ctx
    from app.routes.email_sync import settings_ctx as tos_ctx
    bctx = brokers_ctx(db)
    return {"ibkr": bctx.get("ibkr"), "t212": bctx.get("t212"), "snaptrade": _snap(db),
            "sources": _sources(db), "tos_email": tos_ctx(request, db), "s": get_settings()}
