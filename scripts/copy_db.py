"""One-off Postgres -> Postgres copy used for the Render Oregon -> Neon move.

Runs only when COPY_FROM_URL is set (otherwise exits 0 immediately, so it is harmless in any start command).
Source = the old database, destination = DATABASE_URL (schema must already exist: run `alembic upgrade head` first).

  * refuses to run if the destination already holds rows (set COPY_FORCE=1 to wipe and recopy)
  * reads the source in one REPEATABLE READ transaction (consistent snapshot), writes the destination in one
    transaction (all or nothing), tables in foreign-key order, text COPY, then resets sequences
  * prints per-table row counts and an md5 checksum of every row on both sides; exits 1 on any difference
  * never prints connection strings or row contents
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

import psycopg
from sqlalchemy import MetaData, create_engine

from app import models  # noqa: F401  (register tables)
from app.config import normalize_db_url
from app.db import Base


def plain(url: str) -> str:
    return normalize_db_url(url).replace("postgresql+psycopg://", "postgresql://", 1)


def egress_ip() -> str:
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def connect(url: str, label: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return psycopg.connect(plain(url), connect_timeout=15)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    print(f"copy_db: cannot connect to {label}: {type(last).__name__}: {str(last).splitlines()[0][:160] if str(last) else ''}", flush=True)
    return None


def _is_int(c) -> bool:
    try:
        return c.type.python_type is int
    except NotImplementedError:
        return False


def pk_cols(table) -> list[str]:
    cols = [c.name for c in table.primary_key.columns]
    return cols or [c.name for c in table.columns]


def checksum(cur, table) -> str:
    order = ", ".join(f'"{c}"' for c in pk_cols(table))
    cur.execute(f'select coalesce(md5(string_agg(t::text, \'|\' order by {order})), \'empty\') from "{table.name}" t')
    return cur.fetchone()[0]


def main() -> int:
    src_url = os.getenv("COPY_FROM_URL", "")
    if not src_url:
        return 0
    dst_url = os.getenv("DATABASE_URL", "")
    print(f"copy_db: starting; this host's outbound IP is {egress_ip()}", flush=True)
    src = connect(src_url, "source")
    if src is None:
        return 1
    dst = connect(dst_url, "destination")
    if dst is None:
        return 1
    tables = [t for t in Base.metadata.sorted_tables]
    names = [t.name for t in tables]
    src.autocommit = False
    scur = src.cursor()
    scur.execute("set transaction isolation level repeatable read read only")
    dcur = dst.cursor()
    # destination must be empty
    nonempty = []
    for t in tables:
        dcur.execute(f'select count(*) from "{t.name}"')
        if dcur.fetchone()[0]:
            nonempty.append(t.name)
    if nonempty:
        if os.getenv("COPY_FORCE") != "1":
            print(f"copy_db: destination not empty ({', '.join(nonempty)}); refusing (COPY_FORCE=1 to wipe)", flush=True)
            return 1
        for t in reversed(tables):
            dcur.execute(f'delete from "{t.name}"')
    # source tables / columns actually present
    scur.execute("select table_name, column_name from information_schema.columns where table_schema='public'")
    have: dict[str, set[str]] = {}
    for tn, cn in scur.fetchall():
        have.setdefault(tn, set()).add(cn)
    scur.execute("select version_num from alembic_version")
    src_rev = [r[0] for r in scur.fetchall()]
    dcur.execute("select version_num from alembic_version")
    dst_rev = [r[0] for r in dcur.fetchall()]
    print(f"copy_db: alembic source={src_rev} destination={dst_rev}", flush=True)
    if src_rev != dst_rev:
        print("copy_db: schema revisions differ; refusing", flush=True)
        return 1
    for t in tables:
        if t.name not in have:
            print(f"copy_db: source lacks table {t.name}; refusing", flush=True)
            return 1
        cols = [c.name for c in t.columns]
        missing = [c for c in cols if c not in have[t.name]]
        if missing:
            print(f"copy_db: source table {t.name} lacks columns {missing}; refusing", flush=True)
            return 1
    for t in tables:
        cols = ", ".join(f'"{c.name}"' for c in t.columns)
        with scur.copy(f'COPY (select {cols} from "{t.name}") TO STDOUT') as out, \
                dcur.copy(f'COPY "{t.name}" ({cols}) FROM STDIN') as inn:
            for chunk in out:
                inn.write(bytes(chunk))
    # sequences
    for t in tables:
        for c in t.columns:
            if c.primary_key and c.autoincrement is not False and _is_int(c):
                dcur.execute("select pg_get_serial_sequence(%s, %s)", (f'"{t.name}"', c.name))
                seq = dcur.fetchone()[0]
                if seq:
                    dcur.execute(f'select setval(%s, greatest(coalesce((select max("{c.name}") from "{t.name}"), 0), 1), '
                                 f'(select count(*) > 0 from "{t.name}"))', (seq,))
    # verify before committing
    ok = True
    print("copy_db: table | source rows | destination rows | md5", flush=True)
    for t in tables:
        scur.execute(f'select count(*) from "{t.name}"')
        sn = scur.fetchone()[0]
        dcur.execute(f'select count(*) from "{t.name}"')
        dn = dcur.fetchone()[0]
        sm, dm = checksum(scur, t), checksum(dcur, t)
        same = sn == dn and sm == dm
        ok &= same
        print(f"copy_db: {t.name} | {sn} | {dn} | {'match' if sm == dm else 'DIFFERENT'}{'' if sn == dn else ' COUNT-MISMATCH'}", flush=True)
    src.rollback()
    if not ok:
        dst.rollback()
        print("copy_db: VERIFY FAILED; destination rolled back", flush=True)
        return 1
    dst.commit()
    print("copy_db: DONE; destination committed, all tables identical", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
