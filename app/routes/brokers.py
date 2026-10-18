"""Settings > Brokers: connect / test / sync / disconnect Trading 212 and IBKR Flex; instrument mappings."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets
from app.config import get_settings
from app.db import get_db
from app.models import Account, Instrument
from app.services import set_state
from app.sources import trading212 as t212
from app.sources import ibkr_flex as ibkr
from app.sync import start_background
from app.web import templates

router = APIRouter()
BACK = "/settings#brokers"


def _flash(request: Request, msg: str) -> RedirectResponse:
    request.session["flash"] = msg
    return RedirectResponse(BACK, status_code=303)


def _scope_text(res: dict) -> str:
    sc = res.get("scopes") or {}
    names = {"account": "Account data", "history": "History - Orders", "portfolio": "Portfolio"}
    return ", ".join(f"{names.get(k, k)} {'✓' if v else '✗' if v is False else '?'}" for k, v in sc.items())


@router.post("/settings/brokers/t212/connect")
def connect(request: Request, key: str = Form(""), secret: str = Form(""), practice: str = Form(""),
            db: Session = Depends(get_db)):
    if not get_settings().token_encryption_key:
        return _flash(request, "Set TOKEN_ENCRYPTION_KEY on the server first: API keys are stored encrypted with it.")
    if not key.strip() or not secret.strip():
        return _flash(request, "Paste both the API key and the API secret.")
    env = "demo" if practice else "live"
    res = t212.test_connection(key.strip(), secret.strip(), env)
    if not res["ok"]:
        t212._save_last(db, ok=False, error=res["error"], error_kind=res["kind"])
        db.commit()
        return _flash(request, f"Trading 212 not connected: {res['error']}")
    t212.save_credentials(db, key, secret, env)
    src = t212.Trading212Source()
    acct = src.ensure_account(db, {"id": res["account_id"], "currency": res["currency"]})
    t212._save_last(db, reset=True, ok=True, error=None, error_kind=None, account_id=res["account_id"],
                    currency=res["currency"], scopes=res["scopes"], warning=res.get("warning"))
    db.commit()
    return _flash(request, f"Trading 212 connected: account {res['account_id']} in {res['currency']} → “{acct.name}”. "
                           f"Permissions: {_scope_text(res)}. {res.get('warning') or ''} Press Sync to import your history.")


@router.post("/settings/brokers/t212/test")
def test(request: Request, db: Session = Depends(get_db)):
    creds = t212.load_credentials(db)
    if not creds:
        return _flash(request, "Trading 212 is not connected.")
    res = t212.test_connection(creds["key"], creds["secret"], creds.get("env", "live"))
    t212._save_last(db, ok=res["ok"], error=res["error"], error_kind=res["kind"], scopes=res["scopes"],
                    account_id=res["account_id"] or t212.last_status(db).get("account_id"),
                    currency=res["currency"] or t212.last_status(db).get("currency"), warning=res.get("warning"))
    db.commit()
    if res["ok"]:
        return _flash(request, f"Trading 212 connection OK (account {res['account_id']}, {res['currency']}). "
                               f"Permissions: {_scope_text(res)}. {res.get('warning') or ''}")
    return _flash(request, f"Trading 212 test failed: {res['error']}")


@router.post("/settings/brokers/t212/disconnect")
def disconnect(request: Request, db: Session = Depends(get_db)):
    t212.clear_credentials(db)
    t212._save_last(db, reset=True)
    db.commit()
    return _flash(request, "Trading 212 disconnected. The key is deleted from the server; imported trades are kept. "
                           "Also delete the key in Trading 212 → Settings → API.")


@router.post("/settings/brokers/t212/sync")
def sync(request: Request, db: Session = Depends(get_db)):
    from app.models import SyncRun
    run_id = start_background("manual", only={"t212"})
    run = db.get(SyncRun, run_id)
    return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})


@router.post("/settings/brokers/t212/resync")
def resync(request: Request, db: Session = Depends(get_db)):
    """One-off full history pull. Ingest is idempotent on fill id; nothing is deleted."""
    from app.models import SyncRun
    set_state(db, t212.FULL_STATE, "1")
    db.commit()
    run_id = start_background("manual", only={"t212"})
    run = db.get(SyncRun, run_id)
    return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})


@router.post("/settings/brokers/instruments/{iid}")
def set_instrument(iid: int, request: Request, price_symbol: str = Form(""), symbol: str = Form(""),
                   db: Session = Depends(get_db)):
    inst = db.get(Instrument, iid)
    if inst is None:
        return _flash(request, "Unknown instrument.")
    ps, sym = price_symbol.strip(), symbol.strip()
    if not ps and not sym:        # reset to the automatic mapping
        inst.user_override = False
        info = markets.parse_t212_ticker(inst.broker_symbol, inst.isin)
        inst.symbol = info.symbol
        inst.price_symbol = markets.yahoo_symbol_for(inst.symbol, info.exchange)
        db.commit()
        return _flash(request, f"{inst.symbol}: back to the automatic price symbol ({inst.price_symbol or 'none'}).")
    if sym:
        inst.symbol = sym.upper()[:32]
    inst.price_symbol = ps[:32] or None
    inst.user_override = True
    db.commit()
    return _flash(request, f"{inst.symbol}: price symbol set to {inst.price_symbol or 'none'}. New fills use symbol "
                           f"{inst.symbol}; existing trades keep theirs until the next rebuild.")


@router.post("/settings/brokers/ibkr/connect")
def ibkr_connect(request: Request, token: str = Form(""), query_id: str = Form(""),
                 currency: str = Form("USD"), db: Session = Depends(get_db)):
    if not get_settings().token_encryption_key:
        return _flash(request, "Set TOKEN_ENCRYPTION_KEY on the server first: the Flex token is stored encrypted with it.")
    existing = ibkr.load_credentials(db)
    tok = ibkr._compact(token) or ibkr._compact((existing or {}).get("token") or "")
    qid = ibkr._compact(query_id) or ibkr._compact((existing or {}).get("query_id") or "")
    ccy = (currency.strip().upper() or ((existing or {}).get("currency") or "USD"))[:3]
    if not tok or not qid:
        return _flash(request, "Paste both the Flex token and the Flex Query ID.")
    res = ibkr.test_connection(tok, qid)
    if not res["ok"]:
        ibkr._save_last(db, ok=False, error=res["error"], error_kind=res["kind"])
        db.commit()
        return _flash(request, f"Interactive Brokers not connected: {res['error']}")
    ibkr.save_credentials(db, tok, qid, ccy)
    src = ibkr.IbkrFlexSource()
    infos = list(res.get("accounts") or [])
    if not infos:
        infos = [{"account_id": res.get("account_id"), "currency": res.get("currency") or ccy}]
    names = []
    for info in infos:
        acct = src.ensure_account(db, {"account_id": info.get("account_id"),
                                        "currency": info.get("currency") or res.get("currency") or ccy}, ccy)
        names.append(acct.name)
    shown = ", ".join(names[:8]) or "Interactive Brokers"
    ibkr._save_last(db, reset=True, ok=True, error=None, error_kind=None, account_id=res.get("account_id"),
                    accounts=[i.get("account_id") for i in infos if i.get("account_id")],
                    currency=res.get("currency") or ccy, query_id=qid)
    db.commit()
    return _flash(request, f"Interactive Brokers connected: {shown} in {res.get('currency') or ccy}. "
                           "Press Sync to import. Set the history start date on this card, then Full history "
                           "walks year-sized windows from that date.")


@router.post("/settings/brokers/ibkr/test")
def ibkr_test(request: Request, db: Session = Depends(get_db)):
    creds = ibkr.load_credentials(db)
    if not creds:
        return _flash(request, "Interactive Brokers is not connected.")
    res = ibkr.test_connection(creds["token"], creds["query_id"])
    ibkr._save_last(db, ok=res["ok"], error=res["error"], error_kind=res["kind"],
                    account_id=res["account_id"] or ibkr.last_status(db).get("account_id"),
                    currency=res["currency"] or ibkr.last_status(db).get("currency"))
    db.commit()
    if res["ok"]:
        ids = ", ".join(a["account_id"] for a in (res.get("accounts") or []) if a.get("account_id")) or res.get("account_id")
        return _flash(request, f"Interactive Brokers connection OK (account {ids}, "
                               f"{res['currency'] or creds.get('currency') or 'USD'}).")
    return _flash(request, f"Interactive Brokers test failed: {res['error']}")


@router.post("/settings/brokers/ibkr/history-start")
def ibkr_history_start(request: Request, history_start: str = Form(""), db: Session = Depends(get_db)):
    """Store the Full history start date. Does not call IBKR."""
    try:
        day = ibkr.parse_history_start(history_start)
    except ValueError as exc:
        return _flash(request, str(exc))
    ibkr.save_history_start(db, day)
    db.commit()
    return _flash(request, f"IBKR history start set to {day.isoformat()}. Full history walks from that date to today.")


@router.post("/settings/brokers/ibkr/disconnect")
def ibkr_disconnect(request: Request, db: Session = Depends(get_db)):
    ibkr.clear_credentials(db)
    ibkr._save_last(db, reset=True)
    db.commit()
    return _flash(request, "Interactive Brokers disconnected. The Flex token is deleted from the server; imported trades are kept. "
                           "Also delete or rotate the token in Client Portal → Flex Web Service.")


@router.post("/settings/brokers/ibkr/sync")
def ibkr_sync(request: Request, db: Session = Depends(get_db)):
    from app.models import SyncRun
    run_id = start_background("manual", only={"ibkr_flex"})
    run = db.get(SyncRun, run_id)
    return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})


@router.post("/settings/brokers/ibkr/resync")
def ibkr_resync(request: Request, db: Session = Depends(get_db)):
    from app.models import SyncRun
    set_state(db, ibkr.FULL_STATE, "1")
    db.commit()
    run_id = start_background("manual", only={"ibkr_flex"})
    run = db.get(SyncRun, run_id)
    return templates.TemplateResponse(request, "partials/sync_status.html", {"request": request, "run": run})


def _collect_uploads(files: list[UploadFile] | None, file: UploadFile | None) -> list[UploadFile]:
    got: list[UploadFile] = []
    if files:
        got.extend(files if isinstance(files, (list, tuple)) else [files])
    if file is not None:
        got.append(file)
    out: list[UploadFile] = []
    seen: set[int] = set()
    for uf in got:
        if uf is None or id(uf) in seen:
            continue
        seen.add(id(uf))
        if not (uf.filename or "").strip() and getattr(uf, "size", None) == 0:
            continue
        out.append(uf)
    return out


@router.post("/settings/brokers/ibkr/upload")
def ibkr_upload(request: Request, files: list[UploadFile] | None = File(None),
                file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    """Stream Flex/Activity XML to disk and process in the background. Never sent to IBKR."""
    uploads = _collect_uploads(files, file)
    if not uploads:
        return _flash(request, "Choose one or more IBKR Activity Statement XML files.")
    if ibkr.upload_in_progress(db):
        return _flash(request, "An IBKR upload is already processing. Wait until it finishes, then upload again.")
    tmpdir = Path(tempfile.mkdtemp(prefix="ibkr-up-"))
    meta: list[dict] = []
    queued = 0
    for i, uf in enumerate(uploads):
        name = ibkr._safe_filename(uf.filename)
        row = {"name": name, "size": 0, "status": "queued", "fetched": 0, "inserted": 0,
               "error": None, "path": None, "dir": str(tmpdir)}
        if i >= ibkr.MAX_UPLOAD_FILES:
            row["status"] = "error"
            row["error"] = "too many files (10 max)"
            meta.append(row)
            continue
        dest = tmpdir / f"{i:02d}-{name}"
        try:
            src = getattr(uf, "file", uf)
            size = ibkr.stream_upload_to_temp(src, dest)
            row["size"] = size
            row["path"] = str(dest)
            if size <= 0:
                row["status"] = "error"
                row["error"] = "empty file"
                dest.unlink(missing_ok=True)
            else:
                with dest.open("rb") as fh:
                    head = fh.read(65536)
                if ibkr._UNSAFE_XML.search(head.decode("utf-8", errors="replace")):
                    row["status"] = "error"
                    row["error"] = "not XML"
                    dest.unlink(missing_ok=True)
                    row["path"] = None
                elif not ibkr.looks_like_flex_bytes(head):
                    row["status"] = "error"
                    row["error"] = "not XML"
                    dest.unlink(missing_ok=True)
                    row["path"] = None
                else:
                    queued += 1
        except ibkr.FlexError as exc:
            row["status"] = "error"
            row["error"] = "too large" if "too large" in str(exc).lower() else str(exc)
            row["path"] = None
        except Exception:  # noqa: BLE001
            row["status"] = "error"
            row["error"] = "not XML"
            row["path"] = None
        meta.append(row)
    if queued == 0:
        ibkr.queue_uploaded_files(db, meta)
        data = ibkr.load_upload(db)
        data["status"] = "error"
        data["error"] = next((r.get("error") for r in meta if r.get("error")), "not XML")
        data["finished"] = data.get("queued_at")
        ibkr.save_upload(db, data)
        db.commit()
        ibkr._cleanup_upload_dir(str(tmpdir))
        err = data["error"]
        if err == "too large":
            return _flash(request, "File too large (100 MB max).")
        if err == "too many files (10 max)":
            return _flash(request, "Too many files (10 max).")
        return _flash(request, "No valid IBKR XML files. Need a FlexQueryResponse or FlexStatement.")
    ibkr.queue_uploaded_files(db, meta)
    ibkr.start_upload_worker()
    n = queued
    return _flash(request, f"Upload received: {n} file(s), processing in background")


@router.get("/settings/brokers/ibkr/upload/status")
def ibkr_upload_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "partials/ibkr_upload_progress.html", {
        "request": request, "upload": ibkr.public_upload(ibkr.load_upload(db)),
    })


@router.post("/settings/brokers/ibkr/reprocess")
def ibkr_reprocess(request: Request, db: Session = Depends(get_db)):
    """Replay stored Flex JSON/XML. Does not call Interactive Brokers."""
    res = ibkr.reprocess_stored_payloads(db)
    return _flash(request, res.message or "Reprocessed stored IBKR history.")


def settings_ctx(db: Session) -> dict:
    from app.corporate import audit_instruments
    creds = t212.load_credentials(db)
    insts = list(db.scalars(select(Instrument).where(Instrument.source_key == "t212").order_by(Instrument.symbol)))
    ib = ibkr.load_credentials(db)
    ib_insts = list(db.scalars(select(Instrument).where(Instrument.source_key == "ibkr_flex").order_by(Instrument.symbol)))
    mapped = list(db.scalars(select(Account).where(Account.source_key == "ibkr_flex").order_by(Account.name)))
    return {"t212": {"connected": creds is not None, "masked": t212.mask(creds["key"]) if creds else None,
                     "practice": bool(creds and creds.get("env") == "demo"), "last": t212.last_status(db),
                     "instruments": insts, "instrument_issues": audit_instruments(db, fix=False),
                     "scope_help": t212.SCOPE_HELP,
                     "enc_key": bool(get_settings().token_encryption_key)},
            "ibkr": {"connected": ib is not None, "query_id": (ib or {}).get("query_id"),
                     "currency": (ib or {}).get("currency") or "USD", "last": ibkr.last_status(db),
                     "instruments": ib_insts, "how_to": ibkr.HOW_TO,
                     "history_start": ibkr.history_start_date(db).isoformat(),
                     "progress": ibkr.progress_line(ibkr.load_history(db)),
                     "flex_end": ibkr.last_complete_biz_day().isoformat(),
                     "mapped": mapped,
                     "upload": ibkr.public_upload(ibkr.load_upload(db)),
                     "balance_checks": _ibkr_balance_checks(db, mapped),
                     "enc_key": bool(get_settings().token_encryption_key)}}


def _ibkr_balance_checks(db: Session, mapped) -> list[dict]:
    """Per-account journal NAV vs IBKR ending NAV/cash. No estimation."""
    from app.balance import broker_snapshots
    from app.models import BalancePoint
    if not mapped:
        return []
    snaps = broker_snapshots(db, [a.id for a in mapped])
    out = []
    for a in mapped:
        snap = snaps.get(a.id) or {}
        latest = db.scalar(select(BalancePoint).where(
            BalancePoint.account_id == a.id).order_by(BalancePoint.day.desc()))
        journal_nav = latest.balance if latest is not None else None
        ibkr_nav = snap.get("total")
        ibkr_cash = snap.get("cash")
        diff = None
        if journal_nav is not None and ibkr_nav is not None:
            diff = round(float(journal_nav) - float(ibkr_nav), 2)
        out.append({
            "name": a.name, "currency": a.currency or snap.get("currency") or "USD",
            "journal_nav": journal_nav, "ibkr_nav": ibkr_nav, "ibkr_cash": ibkr_cash,
            "diff": diff, "as_of": snap.get("as_of"),
        })
    return out
