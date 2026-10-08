from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Account, Execution, ImportBatch, OAuthToken, SyncRun, Trade, utcnow
from app.services import get_state, rebuild_trades
from app.sources import all_sources
from app.sync import running_sync, start_background
from app.web import base_context, templates

router = APIRouter()


def _snaptrade_ctx(db) -> dict | None:
    from app.models import SourceState
    from app.sources.snaptrade import SOURCE_KEY, load_status, relogin_due, _parse_dt
    snap = load_status(db)
    if not snap:
        return None
    conns = []
    for c in snap.get("connections", []):
        conns.append({**c, "relogin_due": relogin_due(c), "connected_at_dt": _parse_dt(c.get("connected_at"))})
    last = db.scalar(select(func.max(SourceState.last_success_at)).where(SourceState.source == SOURCE_KEY))
    return {"connections": conns, "accounts": snap.get("accounts", []), "error": snap.get("error"),
            "checked_at": _parse_dt(snap.get("checked_at")), "last_success": last}


def _alias_ctx(db):
    from app.services import get_state
    from app.symbols import USER_STATE, describe
    user = json.loads(get_state(db, USER_STATE) or "{}")
    return {"alias_rows": describe(db),
            "alias_text": "\n".join(f"{k}={v}" for k, v in user.items())}


def _settings_ctx(request, db, **kw):
    for src in all_sources():
        if src.key == "snaptrade" and src.is_configured():
            try:
                src.refresh(db)
            except Exception:  # pragma: no cover - shown as status error instead
                db.rollback()
    sources = [(s, s.status(db)) for s in all_sources()]
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(15)))
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    acct_rows = []
    for a in accounts:
        n_exec = db.scalar(select(func.count(Execution.id)).where(Execution.account_id == a.id)) or 0
        n_trades = db.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id)) or 0
        orphans = json.loads(get_state(db, f"orphans:{a.id}") or "[]")
        acct_rows.append({"a": a, "executions": n_exec, "trades": n_trades, "orphans": orphans})
    s = get_settings()
    from app.routes.email_sync import settings_ctx as _tos_email_ctx
    return base_context(request, db, nav="settings", sources=sources, runs=runs, acct_rows=acct_rows,
                        active_run=running_sync(db), s=s, snaptrade=_snaptrade_ctx(db), **_alias_ctx(db),
                        tos_email=_tos_email_ctx(request, db), **kw)


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    msg = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "settings.html", _settings_ctx(request, db, flash=msg))


# ---------------------------------------------------------------- backup
@router.get("/settings/backup.json")
def backup(db: Session = Depends(get_db)):
    """Full JSON export of every table (journal, fills, trades, imports, settings). Secrets such as
    stored OAuth tokens are left out."""
    import json
    from fastapi.responses import Response
    from app.db import Base
    out: dict = {"exported_at": utcnow().isoformat(), "tables": {}}
    for table in Base.metadata.sorted_tables:
        if table.name == OAuthToken.__tablename__:
            out["tables"][table.name] = {"omitted": "credentials", "rows": db.scalar(
                select(func.count()).select_from(table))}
            continue
        rows = [dict(r._mapping) for r in db.execute(table.select())]
        for r in rows:
            for k in list(r):
                if "token" in k.lower() or "secret" in k.lower():
                    r[k] = None
        out["tables"][table.name] = rows
    body = json.dumps(out, default=str)
    fname = f"trading-journal-backup-{utcnow():%Y%m%d-%H%M%S}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------- sync
@router.post("/sync")
def sync_now(request: Request, db: Session = Depends(get_db)):
    run_id = start_background("manual")
    run = db.get(SyncRun, run_id)
    return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})


@router.get("/sync/{run_id}/status")
def sync_status(run_id: int, request: Request, db: Session = Depends(get_db)):
    db.expire_all()
    run = db.get(SyncRun, run_id)
    resp = templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})
    if run and run.status != "running":
        resp.headers["HX-Trigger"] = "sync-finished"
    return resp


@router.get("/sync/runs")
def sync_runs(request: Request, db: Session = Depends(get_db)):
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(15)))
    return templates.TemplateResponse(request, "partials/sync_runs.html", {"request": request, "runs": runs})


# ---------------------------------------------------------------- Schwab OAuth (optional source)
@router.get("/auth/schwab/connect")
def schwab_connect(request: Request, db: Session = Depends(get_db)):
    from app.sources.schwab_api import SchwabApiSource, SchwabClient
    if not SchwabApiSource().is_configured():
        request.session["flash"] = "Schwab API is not configured on this server."
        return RedirectResponse("/settings", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["schwab_state"] = state
    return RedirectResponse(SchwabClient(db).authorize_url(state), status_code=303)


def _finish_schwab(request: Request, db: Session, code_or_url: str) -> None:
    from app.sources.schwab_api import SchwabAuthError, SchwabClient
    try:
        client = SchwabClient(db)
        client.exchange_code(client.code_from_redirect(code_or_url))
        request.session["flash"] = "Schwab connected. Run 'Sync now' to pull your history."
    except (SchwabAuthError, RuntimeError) as exc:
        request.session["flash"] = f"Schwab connection failed: {exc}"


@router.get("/auth/schwab/callback")
def schwab_callback(request: Request, code: str = "", state: str | None = None, db: Session = Depends(get_db)):
    expected = request.session.pop("schwab_state", None)
    # Schwab may not echo `state`; when it does, it must match.
    if state is not None and expected is not None and state != expected:
        request.session["flash"] = "Schwab callback rejected (state mismatch). Try connecting again."
    elif not code:
        request.session["flash"] = "Schwab callback had no code."
    else:
        _finish_schwab(request, db, code)
    return RedirectResponse("/settings", status_code=303)


@router.post("/auth/schwab/paste")
def schwab_paste(request: Request, redirect_url: str = Form(...), db: Session = Depends(get_db)):
    _finish_schwab(request, db, redirect_url)
    return RedirectResponse("/settings", status_code=303)


@router.post("/auth/schwab/disconnect")
def schwab_disconnect(request: Request, db: Session = Depends(get_db)):
    db.execute(delete(OAuthToken).where(OAuthToken.provider == "schwab"))
    db.commit()
    request.session["flash"] = "Schwab disconnected (tokens deleted)."
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- SnapTrade (Schwab) connection
def _portal_redirect(request: Request, *, reconnect: str | None):
    from app.sources.snaptrade import SnapTradeClient, SnapTradeError
    if not get_settings().snaptrade_configured:
        request.session["flash"] = "SnapTrade is not configured on this server."
        return RedirectResponse("/settings", status_code=303)
    back = str(request.base_url).rstrip("/") + "/settings/snaptrade/return"
    if reconnect:
        back += f"?id={reconnect}"
    try:
        url = SnapTradeClient(timeout=15).login_url(reconnect=reconnect, redirect=back)
    except (SnapTradeError, Exception) as exc:  # network errors too
        request.session["flash"] = f"Couldn't open the SnapTrade connection portal: {exc}"
        return RedirectResponse("/settings", status_code=303)
    # The portal link is single-use and expires after 5 minutes, so redirect straight away.
    return RedirectResponse(url, status_code=303)


@router.api_route("/settings/snaptrade/reconnect", methods=["GET", "POST"])
def snaptrade_reconnect(request: Request, id: str = "", db: Session = Depends(get_db)):
    from app.sources.snaptrade import load_status
    conns = (load_status(db) or {}).get("connections", [])
    ids = {c["id"] for c in conns}
    if id and id not in ids:
        request.session["flash"] = "Unknown SnapTrade connection."
        return RedirectResponse("/settings", status_code=303)
    target = id or next((c["id"] for c in conns if c.get("disabled")), None) or (conns[0]["id"] if conns else None)
    return _portal_redirect(request, reconnect=target)


@router.api_route("/settings/snaptrade/connect", methods=["GET", "POST"])
def snaptrade_connect(request: Request):
    return _portal_redirect(request, reconnect=None)


@router.get("/settings/snaptrade/return")
def snaptrade_return(request: Request, id: str = "", db: Session = Depends(get_db)):
    from app.sources.snaptrade import mark_reconnected, refresh_status
    snap = refresh_status(db)
    conns = {c["id"]: c for c in snap.get("connections", [])}
    if id and id in conns and not conns[id].get("disabled"):
        mark_reconnected(db, id)
        request.session["flash"] = "Schwab reconnected. Click Sync now to pull new trades."
    elif id and id in conns:
        request.session["flash"] = "SnapTrade still reports the Schwab connection as disabled. Try Reconnect again."
    else:
        request.session["flash"] = "Back from SnapTrade. Click Sync now to pull your trades."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/snaptrade/resync")
def snaptrade_resync(request: Request, db: Session = Depends(get_db)):
    from app.sources.snaptrade import full_resync
    full_resync(db)
    start_background("manual")
    request.session["flash"] = "Full re-sync started: pulling the complete SnapTrade history (duplicates are skipped)."
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- data management
@router.post("/settings/accounts/{account_id}")
def rename_account(account_id: int, request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    a = db.get(Account, account_id)
    if a and name.strip():
        a.name = name.strip()[:120]
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/accounts/{account_id}/delete")
def delete_account(account_id: int, request: Request, confirm: str = Form(""), db: Session = Depends(get_db)):
    a = db.get(Account, account_id)
    if a and confirm == a.name:
        db.execute(delete(ImportBatch).where(ImportBatch.account_id == a.id))
        db.delete(a)
        db.commit()
        request.session["flash"] = f"Deleted account {a.name} and all its trades."
    else:
        request.session["flash"] = "Type the account name exactly to confirm deletion."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/demo/clear")
def clear_demo(request: Request, db: Session = Depends(get_db)):
    from app.services import prune_unused_tags
    for a in db.scalars(select(Account).where(Account.is_demo.is_(True))):
        db.delete(a)
    db.flush()
    prune_unused_tags(db)  # tags that only existed on sample trades
    db.commit()
    request.session["flash"] = "Sample data removed."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/demo/load")
def load_demo(request: Request, db: Session = Depends(get_db)):
    from app.seed_demo import seed
    seed(db)
    request.session["flash"] = "Sample data loaded (clearly labelled; remove it any time)."
    return RedirectResponse("/", status_code=303)


@router.post("/settings/aliases")
def save_aliases(request: Request, aliases: str = Form(""), db: Session = Depends(get_db)):
    """Ticker renames (OLD=NEW). Saving re-matches imported fills and rebuilds trades."""
    from app.services import get_state, rematch_imports, set_state
    from app.symbols import USER_STATE, parse_alias_text
    try:
        table = parse_alias_text(aliases)
    except ValueError as exc:
        request.session["flash"] = str(exc)
        return RedirectResponse("/settings#aliases", status_code=303)
    set_state(db, USER_STATE, json.dumps(table))
    st = rematch_imports(db)
    n = rebuild_trades(db)
    db.commit()
    request.session["flash"] = (f"Ticker aliases saved. Re-matched imports ({st.merged} fills merged); "
                                f"rebuilt {n} trades.")
    return RedirectResponse("/settings#aliases", status_code=303)


@router.post("/settings/rebuild")
def rebuild(request: Request, db: Session = Depends(get_db)):
    from app.services import rematch_imports
    rematch_imports(db)
    n = rebuild_trades(db)
    db.commit()
    request.session["flash"] = f"Rebuilt {n} trades from executions."
    return RedirectResponse("/settings", status_code=303)
