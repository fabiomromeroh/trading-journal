"""Rendered-page cache for the heavy GET pages (Dashboard, Trades, Reports, Settings, trade detail).

A page is rebuilt from executions only when the data changed: any committed INSERT / UPDATE /
DELETE through the app (sync, import, edit, merge, adjustment, settings, FX top-up) bumps
``GEN`` and every cached page is dropped. Live prices are not part of these pages
(/dashboard/live and the chart endpoints are never cached). Entries also expire after a short
TTL so relative times and "today" stay fresh. One process (Render runs a single uvicorn worker);
a write from another process is picked up by the TTL.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.engine import Engine

GEN = 0
_LOCK = threading.Lock()
_CACHE: "OrderedDict[tuple, tuple[float, int, bytes, dict]]" = OrderedDict()
MAX_ENTRIES = 80
# path regex -> TTL seconds
PAGES = [(re.compile(r"^/$"), 600), (re.compile(r"^/trades$"), 600), (re.compile(r"^/reports$"), 600),
         (re.compile(r"^/settings$"), 90), (re.compile(r"^/trades/\d+$"), 120)]
# writes that never change a page: price candles cache, raw sync files
_IGNORE = re.compile(r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(price_cache|sync_payloads|trade_results)\b", re.I)
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE|DROP)\b", re.I)
stats = {"hit": 0, "miss": 0}
_PLAIN_SESSION = {"auth", "av", "test_email_at"}


def enabled() -> bool:
    return os.environ.get("PAGE_CACHE", "on").lower() not in ("0", "off", "false")


_RECENT: "OrderedDict[tuple, list]" = OrderedDict()   # key -> request headers, most recent last (in memory only)
_APP = None
_WARM = {"timer": None}
WARM_PAGES = 6
WARM_DELAY = 4.0   # seconds after the last write (a sync commits many times)


def bump() -> None:
    global GEN
    with _LOCK:
        GEN += 1
        _CACHE.clear()
        _MEMO.clear()
        # never re-warm because of a write made while warming (no loop)
        if _APP is not None and _RECENT and enabled() and not os.environ.get("PYTEST_CURRENT_TEST") \
                and not _WARM.get("running") and time.monotonic() - _WARM.get("ended", -999) > 5:
            t = _WARM["timer"]
            if t is not None:
                t.cancel()
            _WARM["timer"] = threading.Timer(WARM_DELAY, _prewarm)
            _WARM["timer"].daemon = True
            _WARM["timer"].start()


def _prewarm() -> None:
    """Re-render the pages the user opened most recently, so the first click after a sync is fast too."""
    import asyncio

    import httpx
    with _LOCK:
        jobs = list(_RECENT.items())[-WARM_PAGES:]

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_APP), base_url="http://warm") as c:
            for key, headers in reversed(jobs):
                path, query = key[0], key[1]
                try:
                    await c.get(path + (("?" + query) if query else ""), headers=headers)
                except Exception:  # noqa: BLE001 - warming is best effort
                    pass
    _WARM["running"] = True
    try:
        asyncio.run(run())
    except Exception:  # noqa: BLE001
        pass
    finally:
        _WARM["running"] = False
        _WARM["ended"] = time.monotonic()


@event.listens_for(Engine, "before_cursor_execute")
def _mark(conn, cursor, statement, params, context, executemany):
    if _WRITE.match(statement) and not _IGNORE.match(statement):
        conn.info["_tj_dirty"] = True
        if conn.in_transaction() is False:  # autocommit-style write
            bump()


@event.listens_for(Engine, "commit")
def _commit(conn):
    if conn.info.pop("_tj_dirty", False):
        bump()


@event.listens_for(Engine, "rollback")
def _rollback(conn):
    conn.info.pop("_tj_dirty", None)


def ttl_for(path: str) -> int | None:
    for rx, ttl in PAGES:
        if rx.match(path):
            return ttl
    return None


def key_for(request) -> tuple:
    h = request.headers
    from app.web import ACCOUNT_COOKIE
    return (request.url.path, str(request.url.query), request.cookies.get(ACCOUNT_COOKIE, ""),
            h.get("hx-request", ""), h.get("hx-target", ""), h.get("hx-trigger", ""),
            datetime.now().strftime("%Y-%m-%d"))


def get(key: tuple):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        exp, gen, body, headers = hit
        if gen != GEN or time.monotonic() > exp:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return body, headers


def put(key: tuple, gen: int, ttl: int, body: bytes, headers: dict) -> None:
    with _LOCK:
        if gen != GEN:  # data changed while rendering: don't keep a stale page
            return
        _CACHE[key] = (time.monotonic() + ttl, gen, body, headers)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)


def install(app) -> None:
    global _APP
    from starlette.responses import Response
    _APP = app

    @app.middleware("http")
    async def page_cache(request, call_next):
        ttl = ttl_for(request.url.path) if request.method == "GET" and enabled() else None
        # one-time messages (flash, a backup token shown once, notices) live in the session: never
        # serve or store a cached page while the session carries anything but the login itself
        if ttl is None or not request.session.get("auth") or set(request.session) - _PLAIN_SESSION:
            return await call_next(request)
        key = key_for(request)
        if not request.headers.get("x-tj-warm"):
            with _LOCK:
                _RECENT[key] = {k: v for k, v in request.headers.items() if k.lower() in ("cookie", "hx-request", "hx-target", "hx-trigger", "accept")} | {"x-tj-warm": "1"}
                _RECENT.move_to_end(key)
                while len(_RECENT) > 20:
                    _RECENT.popitem(last=False)
        got = get(key)
        if got is not None:
            stats["hit"] += 1
            body, headers = got
            return Response(content=body, status_code=200, headers={**headers, "X-Page-Cache": "hit"})
        stats["miss"] += 1
        gen = GEN
        resp = await call_next(request)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", "") \
                or "set-cookie" in resp.headers:
            return resp
        body = b"".join([chunk async for chunk in resp.body_iterator])
        keep = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "cache-control", "x-frame-options", "x-content-type-options", "referrer-policy", "content-security-policy")}
        put(key, gen, ttl, body, keep)
        return Response(content=body, status_code=200, headers={**keep, "X-Page-Cache": "miss"})


_MEMO: "OrderedDict[tuple, tuple[float, int, object]]" = OrderedDict()


def memo(key: tuple, fn, ttl: int = 600):
    """Computed data (not HTML) reused until the data changes, e.g. the trades + stats behind
    /dashboard/live so only the live-price part is computed per call."""
    if not enabled():
        return fn()
    with _LOCK:
        hit = _MEMO.get(key)
        if hit and hit[1] == GEN and time.monotonic() < hit[0]:
            return hit[2]
        gen = GEN
    val = fn()
    with _LOCK:
        if gen == GEN:
            _MEMO[key] = (time.monotonic() + ttl, gen, val)
            while len(_MEMO) > 12:
                _MEMO.popitem(last=False)
    return val
