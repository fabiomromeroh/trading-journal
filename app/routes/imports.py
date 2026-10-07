from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.importers import LABELS, decode, detect_format, parse
from app.importers.base import UnknownFormat
from app.models import Account, Execution, ImportBatch, utcnow
from app.services import ingest_records, plan_ingest, rebuild_trades
from app.web import base_context, templates

router = APIRouter()
MAX_BYTES = 15 * 1024 * 1024


def _history(db: Session):
    return list(db.scalars(select(ImportBatch).where(ImportBatch.status != "pending")
                           .order_by(ImportBatch.created_at.desc()).limit(50)))


@router.get("/import")
def import_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "import.html", base_context(
        request, db, nav="import", history=_history(db), error=None, labels=LABELS))


def _resolve_account(db: Session, account_choice: str, new_name: str, hint: str | None) -> Account:
    if account_choice and account_choice.isdigit():
        acct = db.get(Account, int(account_choice))
        if acct and not acct.is_demo:
            return acct
    masked = None
    if hint:
        digits = "".join(ch for ch in hint if ch.isdigit())
        masked = f"...{digits[-3:]}" if digits else None
    if masked and not new_name:
        acct = db.scalar(select(Account).where(Account.account_number_masked == masked, Account.is_demo.is_(False)))
        if acct:
            return acct
    acct = Account(name=(new_name.strip() or (f"Schwab {masked}" if masked else "Schwab")),
                   broker="schwab", account_number_masked=masked)
    db.add(acct)
    db.flush()
    return acct


@router.post("/import/upload")
async def upload(request: Request, file: UploadFile = File(...), account: str = Form(""),
                 new_account_name: str = Form(""), db: Session = Depends(get_db)):
    data = await file.read()
    ctx = base_context(request, db, nav="import", history=_history(db), labels=LABELS)
    if len(data) > MAX_BYTES:
        return templates.TemplateResponse(request, "import.html", {**ctx, "error": "File too large (15 MB max)."},
                                          status_code=400)
    text = decode(data)
    try:
        fmt = detect_format(text)
        result = parse(text, fmt)
    except (UnknownFormat, ValueError) as exc:
        return templates.TemplateResponse(request, "import.html", {**ctx, "error": str(exc)}, status_code=400)
    acct = _resolve_account(db, account, new_account_name, result.account_hint)
    batch = ImportBatch(filename=(file.filename or "upload.csv")[:255], file_format=fmt, account_id=acct.id,
                        status="pending", content=text, rows_total=result.rows_total,
                        rows_trades=len(result.records), skipped=sum(result.skipped.values()))
    batch.date_from, batch.date_to = result.date_range
    db.add(batch)
    db.commit()
    return RedirectResponse(f"/import/{batch.id}/preview", status_code=303)


@router.get("/import/{batch_id}/preview")
def preview(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.status != "pending":
        return RedirectResponse("/import", status_code=303)
    result = parse(batch.content, batch.file_format)
    plan = plan_ingest(db, batch.account_id, batch.file_format, result.records)
    counts = Counter(a for a, _, _ in plan)
    rows = [{"action": a, "r": r} for a, r, _ in plan]
    return templates.TemplateResponse(request, "import_preview.html", base_context(
        request, db, nav="import", batch=batch, result=result, counts=counts, rows=rows[:500],
        more=max(0, len(rows) - 500), label=LABELS.get(batch.file_format, batch.file_format),
        symbols=len({r.underlying for r in result.records})))


@router.post("/import/{batch_id}/commit")
def commit(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.status != "pending":
        raise HTTPException(404)
    result = parse(batch.content, batch.file_format)
    stats = ingest_records(db, batch.account_id, batch.file_format, result.records, batch_id=batch.id)
    batch.inserted, batch.merged, batch.duplicates = stats.inserted, stats.merged, stats.duplicates
    batch.status, batch.committed_at, batch.content = "committed", utcnow(), None
    if result.warnings:
        batch.notes = " ".join(result.warnings)
    rebuild_trades(db, [batch.account_id])
    db.commit()
    return RedirectResponse(f"/import?done={batch.id}", status_code=303)


@router.post("/import/{batch_id}/discard")
def discard(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is not None and batch.status == "pending":
        db.delete(batch)
        db.commit()
    return RedirectResponse("/import", status_code=303)


@router.post("/import/{batch_id}/undo")
def undo(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.status != "committed":
        raise HTTPException(404)
    db.execute(delete(Execution).where(Execution.import_batch_id == batch.id))
    batch.status = "undone"
    rebuild_trades(db, [batch.account_id])
    db.commit()
    return RedirectResponse("/import", status_code=303)
