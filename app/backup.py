"""Scheduled-backup support: JSON export of every table, a dedicated read-only backup token and bookkeeping.

The scheduled job (a GitHub Actions workflow in a private repo) calls ``GET /api/backup/export`` with the
backup token, encrypts the file and stores it, then calls ``POST /api/backup/confirm``. The token is stored
only as a SHA-256 hash, can export but nothing else, and is regenerable in Settings. Exports never contain
OAuth credentials, password hashes / pending login codes, or the backup token hash.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import utcnow
from app.services import get_state, set_state

FORMAT = 1
TOKEN_HASH_STATE = "backup:token_sha256"
TOKEN_CREATED_STATE = "backup:token_created_at"
LAST_STATE = "backup:last"           # json: exported_at, size, sha256, rows, tables, confirmed_at, result
OMIT_TABLES = {"oauth_tokens": "credentials", "sync_payloads": "raw sync files"}
CACHE_TABLES = {"price_cache", "fx_rates", "balance_points", "trade_results", "daily_closes"}
EXCLUDED_STATE_PREFIXES = ("auth:", "backup:", "cred:")  # cred:* = broker API keys (encrypted), re-enter after a restore
MIN_INTERVAL = 600                   # seconds between successful exports (rate limit)
_last_export_at = 0.0


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ------------------------------------------------------------------------------------ token
def regenerate_token(db: Session) -> str:
    token = "tjb_" + secrets.token_urlsafe(32)
    set_state(db, TOKEN_HASH_STATE, _sha(token))
    set_state(db, TOKEN_CREATED_STATE, utcnow().isoformat())
    db.commit()
    return token


def revoke_token(db: Session) -> None:
    set_state(db, TOKEN_HASH_STATE, "")
    db.commit()


def has_token(db: Session) -> bool:
    return bool(get_state(db, TOKEN_HASH_STATE))


def verify_token(db: Session, provided: str | None) -> bool:
    want = get_state(db, TOKEN_HASH_STATE)
    if not want or not provided:
        return False
    return hmac.compare_digest(_sha(provided.strip()), want)


# ------------------------------------------------------------------------------------ export
def build(db: Session, *, include_cache: bool = True) -> dict:
    from app.db import Base
    out: dict = {"format": FORMAT, "exported_at": utcnow().isoformat(), "tables": {}}
    for table in Base.metadata.sorted_tables:
        if table.name in OMIT_TABLES:
            out["tables"][table.name] = {"omitted": OMIT_TABLES[table.name], "rows": db.scalar(
                select(func.count()).select_from(table))}
            continue
        if table.name in CACHE_TABLES and not include_cache:
            out["tables"][table.name] = {"omitted": "cache", "rows": db.scalar(
                select(func.count()).select_from(table))}
            continue
        rows = [dict(r._mapping) for r in db.execute(table.select())]
        if table.name == "app_state":  # password hashes / pending codes / backup token hash stay out
            rows = [r for r in rows if not str(r.get("key", "")).startswith(EXCLUDED_STATE_PREFIXES)]
        for r in rows:
            for k in list(r):
                if "token" in k.lower() or "secret" in k.lower():
                    r[k] = None
        out["tables"][table.name] = rows
    try:
        out["alembic_version"] = db.execute(text("select max(version_num) from alembic_version")).scalar()
    except Exception:  # noqa: BLE001  (tables created without alembic, e.g. tests)
        db.rollback()
    return out


def row_counts(data: dict) -> dict[str, int]:
    return {t: (v["rows"] if isinstance(v, dict) else len(v)) for t, v in data["tables"].items()}


def encode(data: dict) -> bytes:
    return gzip.compress(json.dumps(data, default=str).encode(), compresslevel=9)


# ------------------------------------------------------------------------------------ bookkeeping
def rate_limited(now: float | None = None) -> int:
    """Seconds to wait before the next export is allowed (0 = go ahead)."""
    now = time.time() if now is None else now
    return max(0, int(MIN_INTERVAL - (now - _last_export_at)))


def _app_state_part(db: Session, table) -> str:
    """Identity of the settings a backup would keep. Token, auth and broker-credential rows are excluded
    from the count, the max key and the hash, so recording the export itself cannot change the stamp."""
    kept_only = [~table.c.key.startswith(prefix) for prefix in EXCLUDED_STATE_PREFIXES]
    rows = db.execute(select(table.c.key, table.c.value).where(*kept_only)).all()
    kept = sorted(
        (str(key), "" if value is None else value if isinstance(value, str) else str(value))
        for key, value in rows
    )
    digest = hashlib.sha256()
    for key, raw in kept:
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(raw.encode())
        digest.update(b"\0")
    max_key = kept[-1][0] if kept else ""
    return "|".join(["app_state", str(len(kept)), "key:" + max_key, digest.hexdigest()])


def _stamp_parts(db: Session) -> list[str]:
    """Pieces of the export stamp. Derived tables and secret app_state keys are left out."""
    from app.db import Base
    parts: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name in OMIT_TABLES or table.name in CACHE_TABLES:
            continue
        if table.name == "app_state":
            parts.append(_app_state_part(db, table))
            continue
        n = db.scalar(select(func.count()).select_from(table)) or 0
        bits = [table.name, str(int(n))]
        pk_names = {col.name for col in table.primary_key.columns}
        if "updated_at" in table.c:
            bits.append(str(db.scalar(select(func.max(table.c.updated_at)))))
        if "id" in table.c:
            bits.append(str(db.scalar(select(func.max(table.c.id)))))
        for name in sorted(pk_names - {"id"}):
            bits.append(name + ":" + str(db.scalar(select(func.max(table.c[name])))))
        parts.append("|".join(bits))
    return parts


def content_stamp(db: Session) -> str:
    """Cheap identity of the journal itself. Derived tables and secrets are left out.

    Each kept table contributes its row count, max(updated_at), max(id) and the other primary-key
    columns. ``app_state`` values are hashed in process (they have no updated_at). Nothing here is logged.
    """
    return hashlib.sha256("\n".join(_stamp_parts(db)).encode()).hexdigest()


def last_stamp(db: Session) -> str | None:
    raw = get_state(db, LAST_STATE)
    if not raw:
        return None
    try:
        return json.loads(raw).get("stamp")
    except ValueError:
        return None


def mark_export(db: Session, body: bytes, data: dict, now: float | None = None, stamp: str | None = None) -> dict:
    global _last_export_at
    _last_export_at = time.time() if now is None else now
    counts = row_counts(data)
    info = {"exported_at": data["exported_at"], "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "rows": sum(counts.values()), "counts": counts, "confirmed_at": None, "result": "exported"}
    if stamp is not None:
        info["stamp"] = stamp
    set_state(db, LAST_STATE, json.dumps(info))
    db.commit()
    return info


def confirm(db: Session, sha256: str) -> bool:
    info = json.loads(get_state(db, LAST_STATE) or "{}")
    if not info or not hmac.compare_digest(str(sha256).lower(), info.get("sha256", "")):
        return False
    info["confirmed_at"] = utcnow().isoformat()
    info["result"] = "stored"
    set_state(db, LAST_STATE, json.dumps(info))
    db.commit()
    return True


def last(db: Session) -> dict | None:
    raw = get_state(db, LAST_STATE)
    if not raw:
        return None
    info = json.loads(raw)
    for k in ("exported_at", "confirmed_at"):
        info[k + "_dt"] = datetime.fromisoformat(info[k]) if info.get(k) else None
    return info


def reset_rate_limit() -> None:  # tests
    global _last_export_at
    _last_export_at = 0.0


# ------------------------------------------------------------------------------------ encryption helper
def passphrase_from_key(key: str) -> str:
    """The workflow's gpg passphrase is derived from the app's TOKEN_ENCRYPTION_KEY, so the key you already keep
    in Render is enough to decrypt any backup (no extra secret to lose)."""
    return hashlib.sha256(b"journal-backup-v1:" + key.encode()).hexdigest()
