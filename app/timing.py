"""Server-Timing header: total, DB (query count + time) and outbound HTTP per request. No query text,
URLs or values are recorded, only counts and durations."""
from __future__ import annotations

import time
from contextvars import ContextVar

import httpx
from sqlalchemy import event
from sqlalchemy.engine import Engine

CUR: ContextVar[dict | None] = ContextVar("req_timing", default=None)


@event.listens_for(Engine, "before_cursor_execute")
def _q0(conn, cursor, statement, params, context, executemany):
    conn.info["_tj_t0"] = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _q1(conn, cursor, statement, params, context, executemany):
    st = CUR.get()
    t0 = conn.info.pop("_tj_t0", None)
    if st is not None and t0 is not None:
        st["dbn"] += 1
        st["db"] += time.perf_counter() - t0


_send = httpx.Client.send


def _timed_send(self, request, *a, **kw):
    st = CUR.get()
    t0 = time.perf_counter()
    try:
        return _send(self, request, *a, **kw)
    finally:
        if st is not None:
            st["extn"] += 1
            st["ext"] += time.perf_counter() - t0


httpx.Client.send = _timed_send


def install(app) -> None:
    @app.middleware("http")
    async def server_timing(request, call_next):
        st = {"dbn": 0, "db": 0.0, "extn": 0, "ext": 0.0}
        tok = CUR.set(st)
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        finally:
            CUR.reset(tok)
        total = (time.perf_counter() - t0) * 1000
        db, ext = st["db"] * 1000, st["ext"] * 1000
        resp.headers["Server-Timing"] = (
            f'total;dur={total:.0f}, db;dur={db:.0f};desc="{st["dbn"]} queries", '
            f'ext;dur={ext:.0f};desc="{st["extn"]} http", app;dur={max(0.0, total - db - ext):.0f};desc="python+templates"')
        return resp
