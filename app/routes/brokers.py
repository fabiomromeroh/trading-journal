"""Settings > Brokers: connect / test / sync / disconnect Trading 212 and edit instrument mappings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import markets
from app.config import get_settings
from app.db import get_db
from app.models import Instrument
from app.services import set_state
from app.sources import trading212 as t212
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


@router.post("/settings/brokers/t212/find")
def find_missing(request: Request, symbol: str = Form(""), db: Session = Depends(get_db)):
    """Ask Trading 212 for every raw row on this ticker. Runs on the server; the key never leaves the DB."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return _flash(request, "Enter a ticker to search Trading 212.")
    t212.start_hunt(sym)
    db.expire_all()
    hunt = t212.hunt_view(db)
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "partials/t212_hunt.html", {"request": request, "hunt": hunt})
    return _flash(request, f"Looking up missing {sym} executions. The list updates on this page.")


@router.get("/settings/brokers/t212/hunt")
def hunt_status(request: Request, db: Session = Depends(get_db)):
    db.expire_all()
    return templates.TemplateResponse(request, "partials/t212_hunt.html",
                                      {"request": request, "hunt": t212.hunt_view(db)})


def settings_ctx(db: Session) -> dict:
    from app.corporate import audit_instruments
    creds = t212.load_credentials(db)
    insts = list(db.scalars(select(Instrument).where(Instrument.source_key == "t212").order_by(Instrument.symbol)))
    st = t212.last_status(db)
    return {"t212": {"connected": creds is not None, "masked": t212.mask(creds["key"]) if creds else None,
                     "practice": bool(creds and creds.get("env") == "demo"), "last": st,
                     "instruments": insts, "instrument_issues": audit_instruments(db, fix=False),
                     "scope_help": t212.SCOPE_HELP,
                     "enc_key": bool(get_settings().token_encryption_key),
                     "gaps": t212.gap_rows(db), "hunt": t212.hunt_view(db),
                     "broker_qty_known": isinstance(st.get("broker_qty"), dict)}}
