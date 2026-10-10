from __future__ import annotations

import time
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _pg_kwargs() -> dict:
    # Neon (and any serverless Postgres) suspends idle compute after ~5 min: recycle connections before
    # that, wait for the wake-up on connect (connect_timeout) and keep the pool small (single user).
    return {"pool_size": 3, "max_overflow": 2, "pool_recycle": 240, "pool_timeout": 30,
            "connect_args": {"connect_timeout": 15}}


def make_engine(url: str):
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        kwargs.update(_pg_kwargs())
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    elif url.startswith("postgresql"):
        @event.listens_for(eng, "do_connect")
        def _retry_connect(dialect, conn_rec, cargs, cparams):  # pragma: no cover - needs a real server
            """Retry a few times: a suspended Neon compute can refuse the first attempt while it wakes."""
            last = None
            for attempt in range(4):
                try:
                    return dialect.loaded_dbapi.connect(*cargs, **cparams)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
            raise last
    return eng


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure(url: str) -> None:
    """Re-point the app at another database (used by tests)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    from app import outcome
    outcome.load(db)  # break-even range for this request (Settings > Break-even range)
    try:
        yield db
    finally:
        db.close()
