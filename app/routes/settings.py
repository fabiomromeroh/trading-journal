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


def keepawake_ctx() -> dict:
    from datetime import datetime
    from app.keepawake import state as ka
    tz = ka.tz
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else None  # noqa: E731
    fmt_dt = lambda dt: dt.strftime("%Y-%m-%d %H:%M") if dt else None  # noqa: E731
    nxt, end = ka.next_bounds()
    return {"enabled": ka.enabled, "window_enabled": ka.window_enabled, "pinging": bool(ka.url),
            "active": ka.active(), "window_active": ka.in_window(), "minutes": ka.minutes,
            "interval_min": round(ka.interval / 60, 1), "window_interval_min": round(ka.window_interval / 60, 1),
            "window_label": ka.window_label, "tz_name": ka.tz_name,
            "until": fmt(ka.keep_until) if ka.active() else None,
            "next_start": fmt_dt(nxt), "next_end": fmt_dt(end),
            "last_ping": fmt(ka.last_ping), "last_status": ka.last_ping_status,
            "last_scanner_ping": fmt(ka.last_scanner_ping), "last_scanner_status": ka.last_scanner_status,
            "scanner_enabled": bool(ka.scanner_url), "pings": ka.pings, "scanner_pings": ka.scanner_pings,
            "up_since": fmt(ka.started)}


def _journal_ctx(db):
    from app import options, stops
    from app.routes.journal import chart_r_config
    return {"stop_rule": stops.get_rule(db), "rules": stops.RULES, "buffer": stops.get_buffer(db), "premarket": stops.premarket_on(db), "r": chart_r_config(db),
            "questions": options.get_questions(db)}


_IBKR_KEYS = {"ibkr", "ibkr_flex", "interactivebrokers", "interactive brokers"}
_T212_KEYS = {"t212", "trading212", "trading 212"}
_SCHWAB_KEYS = {"schwab", "thinkorswim", "tos", "snaptrade", "schwab_api", "tos_email", "tos_statement", "schwab_csv"}


def _acct_bucket(account) -> str:
    sk = (account.source_key or "").strip().lower()
    br = (account.broker or "").strip().lower()
    if sk in _IBKR_KEYS or br in _IBKR_KEYS:
        return "ibkr"
    if sk in _T212_KEYS or br in _T212_KEYS:
        return "t212"
    if sk in _SCHWAB_KEYS or br in _SCHWAB_KEYS:
        return "schwab"
    return "other"


def _clip_result(text) -> str:
    s = ("" if text is None else str(text)).strip()
    return s if len(s) <= 180 else s[:180] + "…"


def _decorate_accounts(acct_rows, ibkr, t212, snap, sources):
    """Status dots from data Settings already loaded. No extra queries and no broker calls."""
    ib_last = (ibkr or {}).get("last") or {}
    if (ibkr or {}).get("cooldown_s") or (ibkr or {}).get("history_status") == "paused":
        ib_dot = "paused"
    elif ib_last.get("error") or (ibkr or {}).get("history_status") == "error":
        ib_dot = "error"
    elif not (ibkr or {}).get("connected") or not ib_last.get("synced_at"):
        ib_dot = "never"
    else:
        ib_dot = "ok"
    ib_result = _clip_result(ib_last.get("error")) if ib_last.get("error") else (
        f"{ib_last.get('fetched') or 0} read, {ib_last.get('inserted') or 0} new" if ib_last.get("synced_at") else "")

    t_last = (t212 or {}).get("last") or {}
    if not (t212 or {}).get("connected"):
        t_dot = "never"
    elif t_last.get("error"):
        t_dot = "error"
    elif not t_last.get("synced_at"):
        t_dot = "never"
    else:
        t_dot = "ok"
    t_result = _clip_result(t_last.get("error")) if t_last.get("error") else (
        f"{t_last.get('fetched') or 0} read, {t_last.get('inserted') or 0} new" if t_last.get("synced_at") else "")

    snap_st = next((st for src, st in sources if getattr(src, "key", None) == "snaptrade"), None)
    api_st = next((st for src, st in sources if getattr(src, "key", None) == "schwab_api"), None)
    disabled = bool(snap and any(c.get("disabled") for c in (snap.get("connections") or [])))
    if disabled or (snap_st and snap_st.configured and snap_st.level == "error") or (
            api_st and api_st.configured and api_st.level == "error"):
        s_dot = "error"
    elif (snap_st and snap_st.ready) or (api_st and api_st.ready):
        s_dot = "ok"
    else:
        s_dot = "never"
    s_when = snap.get("last_success") if snap else None
    s_result = _clip_result(snap.get("error")) if snap and snap.get("error") else ("login expired" if disabled else "")

    if ibkr is not None:
        ibkr["dot"] = ib_dot
    if t212 is not None:
        t212["dot"] = t_dot
    groups = {"ibkr": [], "t212": [], "schwab": [], "other": []}
    for row in acct_rows:
        bucket = _acct_bucket(row["a"])
        if bucket == "ibkr":
            row.update(group=bucket, dot=ib_dot, last_sync=ib_last.get("synced_at"), result=ib_result)
        elif bucket == "t212":
            row.update(group=bucket, dot=t_dot, last_sync=t_last.get("synced_at"), result=t_result)
        elif bucket == "schwab":
            row.update(group=bucket, dot=s_dot, last_sync=s_when, result=s_result)
        else:
            row.update(group=bucket, dot="never", last_sync=None, result="")
        groups[bucket].append(row)
    return groups, {"ibkr": ib_dot, "t212": t_dot, "schwab": s_dot}


def _settings_ctx(request, db, **kw):
    for src in all_sources():
        if src.key == "snaptrade" and src.is_configured():
            try:
                src.maybe_refresh(db)  # connection status: SnapTrade API at most every 6 h, not every page load
            except Exception:  # pragma: no cover - shown as status error instead
                db.rollback()
    sources = [(s, s.status(db)) for s in all_sources()]
    from app.adjustments import attention_view, payload_index
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(15)))
    payloads = payload_index(db)
    attention = attention_view(db)
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    acct_rows = []
    for a in accounts:
        n_exec = db.scalar(select(func.count(Execution.id)).where(Execution.account_id == a.id)) or 0
        n_trades = db.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id)) or 0
        orphans = json.loads(get_state(db, f"orphans:{a.id}") or "[]")
        acct_rows.append({"a": a, "executions": n_exec, "trades": n_trades, "orphans": orphans})
    s = get_settings()
    from app.routes.email_sync import settings_ctx as _tos_email_ctx
    from app.routes.security import security_ctx as _security_ctx
    from app.routes.backup import settings_ctx as _backup_ctx
    from app import brokers, companies
    from app.routes.brokers import settings_ctx as _brokers_ctx
    bctx = _brokers_ctx(db)
    snap = _snaptrade_ctx(db)
    acct_groups, conn_dots = _decorate_accounts(acct_rows, bctx.get("ibkr"), bctx.get("t212"), snap, sources)
    from app.account_grid import present_row
    attn_n: dict[int, int] = {}
    for item in attention:
        attn_n[item.account_id] = attn_n.get(item.account_id, 0) + 1
    for row in acct_rows:
        row["attention"] = attn_n.get(row["a"].id, 0)
        present_row(row)
    return base_context(request, db, nav="settings", broker_cards=brokers.cards(db), **bctx, sources=sources, runs=runs, payloads=payloads, attention=attention, acct_rows=acct_rows,
                        acct_groups=acct_groups, conn_dots=conn_dots,
                        company_stats=companies.stats(db),
                        active_run=running_sync(db), s=s, snaptrade=snap, **_alias_ctx(db),
                        tos_email=_tos_email_ctx(request, db), **_security_ctx(db), keepawake=keepawake_ctx(), backups=_backup_ctx(request, db), journal_cfg=_journal_ctx(db), **kw)


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    from app.account_grid import load_panel
    msg = request.session.pop("flash", None)
    ctx = _settings_ctx(request, db, flash=msg)
    key = (request.query_params.get("account") or "").strip()
    panel = load_panel(request, db, key) if key else None
    ctx["selected_key"] = key if panel else ""
    ctx["panel"] = panel
    return templates.TemplateResponse(request, "settings.html", ctx)


# ---------------------------------------------------------------- backup
@router.get("/settings/backup.json")
def backup(db: Session = Depends(get_db)):
    """Full JSON export of every table (journal, fills, trades, imports, settings). Secrets such as
    stored OAuth tokens and password hashes are left out."""
    import json
    from fastapi.responses import Response
    from app import backup as backup_mod
    body = json.dumps(backup_mod.build(db), default=str)
    fname = f"trading-journal-backup-{utcnow():%Y%m%d-%H%M%S}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------- sync
def _sync_note(db: Session, message: str) -> SyncRun:
    existing = running_sync(db)
    if existing:
        return existing
    run = SyncRun(trigger="manual", status="skipped", finished_at=utcnow(), message=message)
    db.add(run)
    db.commit()
    return run


@router.post("/sync")
def sync_now(request: Request, account: str = Form(""), mode: str = Form("new"), db: Session = Depends(get_db)):
    """Sync every connected account, or the one account the switcher / Accounts card named.

    mode=new continues from the stored cursor. mode=full arms a one-off full re-scan first
    (SnapTrade / Schwab cursor reset, Trading 212 FULL_STATE).
    """
    from app import quotes
    from app.sync import arm_full_history, source_key_for_account

    acct = None
    only = None
    raw = (account or "").strip()
    if raw:
        if not raw.isdigit():
            run = _sync_note(db, "That account cannot be synced.")
            return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})
        acct = db.get(Account, int(raw))
        if acct is None or acct.is_demo:
            run = _sync_note(db, "That account cannot be synced.")
            return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})
        key = source_key_for_account(db, acct)
        if key is None:
            run = _sync_note(db, f"{acct.name} has no broker connection. Sync new and Full history run for a "
                             "connected account (SnapTrade, Schwab API, Trading 212, or Interactive Brokers).")
            return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})
        only = {key}
    if mode == "full":
        arm_full_history(db, acct)
    quotes.clear_cache()  # next dashboard load fetches fresh prices for the unrealized P&L
    run_id = start_background("manual", only=only, account_id=acct.id if acct else None)
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
    from app.adjustments import attention_view, payload_index
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(15)))
    return templates.TemplateResponse(request, "partials/sync_runs.html", {
        "request": request, "runs": runs, "payloads": payload_index(db), "attention": attention_view(db),
        "accounts": list(db.scalars(select(Account).order_by(Account.name)))})


@router.get("/settings/sync-payloads/{payload_id}")
def download_payload(payload_id: int, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    from fastapi.responses import Response
    from app.adjustments import payload_body
    from app.models import SyncPayload
    row = db.get(SyncPayload, payload_id)
    if row is None:
        raise HTTPException(404, "Sync file not found")
    body = payload_body(row)
    return Response(body, media_type=row.content_type, headers={
        "Content-Disposition": f'attachment; filename="{row.filename}"'})


@router.post("/settings/adjustments")
def add_adjustment(request: Request, account_id: str = Form(""), kind: str = Form("split"),
                  symbol: str = Form(""), ratio: str = Form(""), new_symbol: str = Form(""),
                  when: str = Form(""), db: Session = Depends(get_db)):
    from app.adjustments import ratio_text, save_manual
    from app.corporate import parse_csv_time
    acct = db.get(Account, int(account_id)) if account_id.isdigit() else None
    if acct is None or acct.is_demo:
        request.session["flash"] = "Pick an account for the adjustment."
        return RedirectResponse("/settings#sync-attention", status_code=303)
    parsed = parse_csv_time(when) or parse_csv_time(when + " 12:00:00")
    try:
        if parsed is None:
            raise ValueError("Enter the date.")
        if kind == "rename":
            msg = save_manual(db, acct.id, "rename", symbol, parsed, new_symbol=new_symbol)
        else:
            msg = save_manual(db, acct.id, "split", symbol, parsed, ratio=ratio_text(ratio))
    except ValueError as exc:
        request.session["flash"] = str(exc)
        back = f"/settings?account={acct.id}#sync-attention" if acct is not None else "/settings#sync-attention"
        return RedirectResponse(back, status_code=303)
    rebuild_trades(db, [acct.id])
    db.commit()
    request.session["flash"] = msg
    return RedirectResponse(f"/settings?account={acct.id}#sync-attention", status_code=303)


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
        request.session["flash"] = "Schwab connected. Run Sync new to pull your history."
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
        request.session["flash"] = "Schwab reconnected. Click Sync new to pull new trades."
    elif id and id in conns:
        request.session["flash"] = "SnapTrade still reports the Schwab connection as disabled. Try Reconnect again."
    else:
        request.session["flash"] = "Back from SnapTrade. Click Sync new to pull your trades."
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
def rename_account(account_id: int, request: Request, name: str = Form(...), next: str = Form(""),
                   db: Session = Depends(get_db)):
    from app.account_grid import safe_next
    a = db.get(Account, account_id)
    if a and name.strip():
        a.name = name.strip()[:120]
        db.commit()
    return RedirectResponse(safe_next(next, "/settings"), status_code=303)


@router.post("/settings/accounts/{account_id}/meta")
def account_meta(account_id: int, request: Request, currency: str = Form("USD"), timezone: str = Form(""),
                 market: str = Form("US"), next: str = Form(""), db: Session = Depends(get_db)):
    """Account currency / time zone / market (defaults USD, America/New_York, US = the original Schwab behaviour)."""
    from zoneinfo import ZoneInfo
    from app.account_grid import safe_next
    a = db.get(Account, account_id)
    if a is None:
        return RedirectResponse("/settings", status_code=303)
    tz = timezone.strip() or a.timezone
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        request.session["flash"] = f"Unknown time zone “{tz}” (use e.g. Europe/Dublin)."
        return RedirectResponse("/settings#display", status_code=303)
    if currency.upper() in ("USD", "EUR", "GBP"):
        a.currency = currency.upper()
    a.timezone, a.market = tz, market.upper()[:8]
    db.commit()
    request.session["flash"] = f"{a.name}: {a.currency}, {a.timezone}, market {a.market}. Existing trades keep their stored currency."
    return RedirectResponse(safe_next(next, "/settings#display"), status_code=303)


@router.post("/settings/accounts/{account_id}/auto-stop")
def account_auto_stop(account_id: int, request: Request, auto_stop: str = Form(""), db: Session = Depends(get_db)):
    """Per-account 5-minute auto stop. An unchecked box is omitted from the form, which means off."""
    a = db.get(Account, account_id)
    if a is None:
        return RedirectResponse("/settings#rules", status_code=303)
    on = (auto_stop or "").strip().lower() in ("1", "on", "true", "yes")
    a.auto_stop = on
    from app import stops
    if on:
        db.commit()
        res = stops.backfill(db, account_id=a.id)
        request.session["flash"] = f"{a.name}: auto stop on ({res.get('set', 0)} set)."
    else:
        n = stops.clear_auto_stops(db, a.id)
        db.commit()
        request.session["flash"] = f"{a.name}: auto stop off. Cleared {n} automatic stop{'s' if n != 1 else ''}."
    return RedirectResponse("/settings#rules", status_code=303)


@router.get("/settings/accounts/new")
def account_add_modal(request: Request, db: Session = Depends(get_db)):
    from app.account_grid import modal_brokers
    return templates.TemplateResponse(request, "partials/account_modal_add.html",
                                      {"request": request, **modal_brokers(request, db)})


@router.post("/settings/accounts")
def create_account(request: Request, name: str = Form(""), broker: str = Form("other"),
                   currency: str = Form("USD"), timezone: str = Form("America/New_York"),
                   market: str = Form("US"), db: Session = Depends(get_db)):
    """A journal account with no broker call. Connecting Schwab, Trading 212, or IBKR still uses those endpoints."""
    from zoneinfo import ZoneInfo
    label = (name or "").strip()[:120]
    if not label:
        request.session["flash"] = "Enter an account name."
        return RedirectResponse("/settings#accounts", status_code=303)
    allowed = {"schwab": "schwab", "trading212": "trading212", "t212": "trading212",
               "ibkr": "ibkr", "other": "other"}
    broker_name = allowed.get((broker or "").strip().lower(), "other")
    tz = (timezone or "").strip() or "America/New_York"
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        request.session["flash"] = f"Unknown time zone “{tz}” (use e.g. America/New_York)."
        return RedirectResponse("/settings#accounts", status_code=303)
    ccy = currency.upper() if currency.upper() in ("USD", "EUR", "GBP") else "USD"
    acct = Account(name=label, broker=broker_name, currency=ccy, timezone=tz, market=(market or "US").upper()[:8])
    db.add(acct)
    db.commit()
    request.session["flash"] = f"Added account {acct.name}. Connect a broker from Edit connection, or import a CSV."
    return RedirectResponse(f"/settings?account={acct.id}#accounts", status_code=303)


@router.get("/settings/accounts/{key}/detail")
def account_detail(key: str, request: Request, db: Session = Depends(get_db)):
    """The one account panel. Opening it does not sync and does not call a broker."""
    from app.account_grid import load_panel
    panel = load_panel(request, db, key)
    if panel is None:
        from fastapi import HTTPException
        raise HTTPException(404, "Unknown account")
    return templates.TemplateResponse(request, "partials/account_detail.html", {"request": request, "panel": panel})


@router.get("/settings/accounts/{key}/edit")
def account_edit_modal(key: str, request: Request, db: Session = Depends(get_db)):
    from app.account_grid import load_panel, modal_brokers
    panel = load_panel(request, db, key)
    if panel is None or panel.get("kind") == "empty":
        from fastapi import HTTPException
        raise HTTPException(404, "Unknown account")
    ctx = {"request": request, "panel": panel, **modal_brokers(request, db)}
    if panel.get("kind") == "account":
        ctx["ibkr"] = panel["ibkr"]
        ctx["t212"] = panel["t212"]
    return templates.TemplateResponse(request, "partials/account_modal_edit.html", ctx)


@router.get("/settings/accounts/{account_id}/ibkr-rebuild")
def ibkr_rebuild_modal(account_id: int, request: Request, scope: str = "account",
                       db: Session = Depends(get_db)):
    from fastapi import HTTPException
    a = db.get(Account, account_id)
    if a is None or a.source_key != "ibkr_flex":
        raise HTTPException(404, "Unknown IBKR account")
    which = "all" if (scope or "").lower() == "all" else "account"
    return templates.TemplateResponse(request, "partials/ibkr_rebuild_modal.html", {
        "request": request, "account": a, "scope": which})


@router.get("/settings/accounts/{account_id}/delete")
def delete_account_modal(account_id: int, request: Request, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    from app.account_grid import removal_counts, removal_text
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Unknown account")
    counts = removal_counts(db, a.id)
    return templates.TemplateResponse(request, "partials/account_modal_delete.html", {
        "request": request, "account": a, "counts": counts, "removal": removal_text(a.name, counts)})


@router.post("/settings/accounts/{account_id}/delete")
def delete_account(account_id: int, request: Request, confirm: str = Form(""), next: str = Form(""),
                   db: Session = Depends(get_db)):
    from app.account_grid import safe_next
    a = db.get(Account, account_id)
    if a and confirm == a.name:
        db.execute(delete(ImportBatch).where(ImportBatch.account_id == a.id))
        db.delete(a)
        db.commit()
        request.session["flash"] = f"Deleted account {a.name} and all its trades."
        return RedirectResponse(safe_next(next, "/settings#accounts"), status_code=303)
    request.session["flash"] = "Type the account name exactly to confirm deletion."
    back = f"/settings?account={account_id}#accounts" if a is not None else "/settings#accounts"
    return RedirectResponse(safe_next(next, back), status_code=303)


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


@router.post("/settings/break-even")
def save_break_even(request: Request, lower: str = Form("0"), upper: str = Form("0"), db: Session = Depends(get_db)):
    """Break-even range in $ for closed trades (and days). Nothing is stored per trade, so every
    statistic, badge and filter uses the new range on the next page load; no rebuild needed."""
    from app import outcome
    try:
        lo = float((lower or "0").replace("$", "").replace(",", "").strip() or 0)
        hi = float((upper or "0").replace("$", "").replace(",", "").strip() or 0)
        if abs(lo) > 100000 or abs(hi) > 100000:
            raise ValueError("Break-even range is too large.")
        lo, hi = outcome.save(db, lo, hi)
        db.commit()
        request.session["flash"] = f"Break-even range saved: {outcome.describe()}."
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc) if "range" in str(exc) else "Enter numbers like -3 and 3."
    return RedirectResponse("/settings#break-even", status_code=303)


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


@router.post("/settings/companies/refresh")
def refresh_companies(request: Request, db: Session = Depends(get_db)):
    """Fill missing or stale company names in the background. Never calls Yahoo in this request."""
    from app import companies
    if not companies.enabled():
        request.session["flash"] = "Company info is off on this server (quotes disabled)."
        return RedirectResponse("/settings", status_code=303)
    missing = companies.stats(db)["missing"]
    companies.request_full()
    request.session["flash"] = f"Refreshing company info in the background ({missing} symbol(s) to fill)."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/rebuild")
def rebuild(request: Request, db: Session = Depends(get_db)):
    from app.services import rematch_imports
    rematch_imports(db)
    n = rebuild_trades(db)
    db.commit()
    request.session["flash"] = f"Rebuilt {n} trades from executions."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/recompute-excursions")
def recompute_excursions(request: Request, db: Session = Depends(get_db)):
    """Recompute MFE/MAE for every trade from the finest available price bars."""
    from app.prices import recompute_all_excursions
    r = recompute_all_excursions(db)
    request.session["flash"] = (f"Recomputed MFE/MAE for {r['trades']} trades ({r['changed']} changed"
                                + (f"; no price data for {r['no_data']}" if r["no_data"] else "") + ").")
    return RedirectResponse("/settings", status_code=303)
