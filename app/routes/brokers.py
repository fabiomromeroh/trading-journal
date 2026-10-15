"""Settings > Brokers: connect / test / sync / disconnect Trading 212 and IBKR Flex; instrument mappings."""
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
    tok = token.strip() or ((existing or {}).get("token") or "")
    qid = query_id.strip() or ((existing or {}).get("query_id") or "")
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
    acct = src.ensure_account(db, {"account_id": res["account_id"], "currency": res["currency"] or ccy}, ccy)
    ibkr._save_last(db, reset=True, ok=True, error=None, error_kind=None, account_id=res["account_id"],
                    currency=res["currency"] or ccy, query_id=qid)
    db.commit()
    return _flash(request, f"Interactive Brokers connected: account {res['account_id'] or acct.name} in "
                           f"{res['currency'] or ccy} → “{acct.name}”. Press Sync to import the Flex query period.")


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
        return _flash(request, f"Interactive Brokers connection OK (account {res['account_id']}, "
                               f"{res['currency'] or creds.get('currency') or 'USD'}).")
    return _flash(request, f"Interactive Brokers test failed: {res['error']}")


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


def settings_ctx(db: Session) -> dict:
    from app.corporate import audit_instruments
    creds = t212.load_credentials(db)
    insts = list(db.scalars(select(Instrument).where(Instrument.source_key == "t212").order_by(Instrument.symbol)))
    ib = ibkr.load_credentials(db)
    ib_insts = list(db.scalars(select(Instrument).where(Instrument.source_key == "ibkr_flex").order_by(Instrument.symbol)))
    return {"t212": {"connected": creds is not None, "masked": t212.mask(creds["key"]) if creds else None,
                     "practice": bool(creds and creds.get("env") == "demo"), "last": t212.last_status(db),
                     "instruments": insts, "instrument_issues": audit_instruments(db, fix=False),
                     "scope_help": t212.SCOPE_HELP,
                     "enc_key": bool(get_settings().token_encryption_key)},
            "ibkr": {"connected": ib is not None, "query_id": (ib or {}).get("query_id"),
                     "currency": (ib or {}).get("currency") or "USD", "last": ibkr.last_status(db),
                     "instruments": ib_insts, "how_to": ibkr.HOW_TO,
                     "enc_key": bool(get_settings().token_encryption_key)}}
