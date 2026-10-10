"""Restore a Trading Journal backup into an (empty) database.

  python -m scripts.restore_backup FILE [--database-url URL] [--force] [--dry-run]
         [--passphrase-env VAR | --key-env VAR]

FILE is a backup in the app's JSON format: plain ``.json``, gzipped ``.json.gz``, or the encrypted
``.json.gz.gpg`` the scheduled job stores (gpg needed; passphrase from --passphrase-env, or derived from the
app's TOKEN_ENCRYPTION_KEY with --key-env TOKEN_ENCRYPTION_KEY).

Target: --database-url, else $DATABASE_URL (postgres:// and postgresql:// are fine; Neon: direct host, sslmode=require).
The schema is created/upgraded with alembic first. The script refuses to touch a database that already has
data unless --force (which wipes every table first). Everything is written in one transaction.
Not in a backup (by design): broker OAuth credentials, price cache, password hashes: after a restore the login
password is APP_PASSWORD again (or use "forgot password"), and Schwab must be reconnected.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from datetime import date, datetime


def load_file(path: str, passphrase: str | None) -> dict:
    raw = open(path, "rb").read()
    if path.endswith(".gpg"):
        if not passphrase:
            raise SystemExit("this file is encrypted: pass --passphrase-env VAR or --key-env VAR")
        p = subprocess.run(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
                            "--decrypt", path], input=(passphrase + "\n").encode(), capture_output=True)
        if p.returncode:
            raise SystemExit("gpg could not decrypt the file (wrong passphrase / key?)")
        raw = p.stdout
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    if not isinstance(data, dict) or "tables" not in data:
        raise SystemExit("not a Trading Journal backup (no 'tables')")
    return data


def coerce(column, value):
    """JSON text -> the Python type SQLAlchemy expects for this column."""
    from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric
    if value is None:
        return None
    t = column.type
    if isinstance(t, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(t, Date) and not isinstance(t, DateTime) and isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(t, Boolean) and isinstance(value, str):
        return value.lower() in ("1", "true", "t", "yes")
    if isinstance(t, (Integer,)) and isinstance(value, str):
        return int(value)
    if isinstance(t, (Float, Numeric)) and isinstance(value, str):
        return float(value)
    return value


def restore(data: dict, engine, *, force: bool = False, dry_run: bool = False) -> dict[str, int]:
    from sqlalchemy import delete, func, select, text
    from app.db import Base
    tables = Base.metadata.sorted_tables
    with engine.connect() as conn:
        trans = conn.begin()
        have = {t.name: conn.scalar(select(func.count()).select_from(t)) for t in tables}
        if any(have.values()) and not force:
            filled = ", ".join(f"{k}={v}" for k, v in have.items() if v)
            raise SystemExit(f"database is not empty ({filled}); use --force to wipe it first")
        if any(have.values()):
            for t in reversed(tables):
                conn.execute(delete(t))
        done: dict[str, int] = {}
        for t in tables:
            rows = data["tables"].get(t.name)
            if not isinstance(rows, list):  # omitted (credentials / cache) or missing
                done[t.name] = 0
                continue
            cols = {c.name: c for c in t.columns}
            batch = [{k: coerce(cols[k], v) for k, v in r.items() if k in cols} for r in rows]
            if batch:
                conn.execute(t.insert(), batch)
            done[t.name] = len(batch)
        if engine.dialect.name == "postgresql":
            for t in tables:
                for c in t.columns:
                    if c.primary_key and c.autoincrement is not False and c.type.python_type is int:
                        seq = conn.scalar(text("select pg_get_serial_sequence(:t, :c)"), {"t": f'"{t.name}"', "c": c.name})
                        if seq:
                            conn.execute(text(f'select setval(:s, greatest(coalesce((select max("{c.name}") from "{t.name}"), 0), 1), '
                                              f'(select count(*) > 0 from "{t.name}"))'), {"s": seq})
        if dry_run:
            trans.rollback()
        else:
            trans.commit()
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--database-url")
    ap.add_argument("--force", action="store_true", help="wipe a non-empty database first")
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    ap.add_argument("--passphrase-env", help="env var holding the gpg passphrase")
    ap.add_argument("--key-env", help="env var holding TOKEN_ENCRYPTION_KEY (passphrase is derived from it)")
    a = ap.parse_args(argv)
    if a.database_url:
        os.environ["DATABASE_URL"] = a.database_url
    from app.backup import passphrase_from_key
    pw = os.getenv(a.passphrase_env) if a.passphrase_env else None
    if a.key_env and os.getenv(a.key_env):
        pw = passphrase_from_key(os.environ[a.key_env])
    data = load_file(a.file, pw)
    from app.config import get_settings
    from app.db import make_engine
    from app.migrate import upgrade
    url = get_settings().database_url
    if not a.dry_run:
        upgrade()
    engine = make_engine(url)
    done = restore(data, engine, force=a.force, dry_run=a.dry_run)
    print(("DRY RUN (rolled back): " if a.dry_run else "restored: ") + ", ".join(f"{k}={v}" for k, v in done.items()))
    print(f"backup exported_at={data.get('exported_at')} alembic={data.get('alembic_version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
