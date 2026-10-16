from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app import db as dbmod, keepawake, passwords
from app.routes import auth, backup as backup_routes, dashboard, journal as journal_routes, ai_review as ai_review_routes, prefs as prefs_routes, brokers as brokers_routes, email_sync, imports, pwa, reports, security, settings as settings_routes, trades

logging.basicConfig(level=logging.INFO)
PUBLIC_PREFIXES = ("/login", "/healthz", "/static", "/api/ingest/", "/api/backup/")  # /api/ingest, /api/backup: token auth
PUBLIC_EXACT = pwa.PUBLIC_PATHS  # manifest, service worker, offline page, favicon: must load before login


class RequireLogin(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.session.get("auth") and not path.startswith(("/static", "/healthz")) and path not in PUBLIC_EXACT:
            db = dbmod.SessionLocal()
            try:
                if not passwords.session_valid(db, request.session.get("av")):
                    request.session.clear()  # password changed elsewhere: sign this session out
            finally:
                db.close()
        if not path.startswith(PUBLIC_PREFIXES) and path not in PUBLIC_EXACT and not request.session.get("auth"):
            if request.headers.get("HX-Request"):
                return JSONResponse({"detail": "login required"}, status_code=401,
                                    headers={"HX-Redirect": "/login"})
            if path.startswith("/auth/schwab/callback"):
                request.session["pending_callback"] = str(request.url)
            return RedirectResponse(f"/login?next={path}", status_code=303)
        if keepawake.counts_as_visit(request.method, path, bool(request.session.get("auth"))):
            keepawake.state.touch()
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


def _repair_t212_gbx_fx() -> None:
    """Correct stored GBX fills (pence FX on a pound price). Idempotent; no secrets logged.

    Retries: Neon may still be waking when the process starts. Runs off the request path so /healthz can pass.
    """
    import time
    from app.sources.trading212 import repair_stored_fx
    log = logging.getLogger(__name__)
    for attempt in range(4):
        db = dbmod.SessionLocal()
        try:
            res = repair_stored_fx(db)
            if res["fixed"]:
                log.info("t212 GBX fx repair: %s executions on %s account(s)", res["fixed"], res["accounts"])
            return
        except Exception:  # noqa: BLE001 - a repair failure must not take the site down
            db.rollback()
            log.warning("t212 GBX fx repair failed (attempt %s)", attempt + 1, exc_info=True)
            time.sleep(3 * (attempt + 1))
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ka = keepawake.state
    task = None
    if ka.enabled and ka.url:
        task = asyncio.create_task(ka.run())
        logging.getLogger(__name__).info("keep-awake: %d min after each visit, ping every %d s", ka.minutes, ka.interval)
    import threading
    from app import stops
    threading.Thread(target=stops.startup_backfill, name="stops-backfill", daemon=True).start()
    threading.Thread(target=_repair_t212_gbx_fx, name="t212-gbx-fx", daemon=True).start()
    def _ibkr_reprocess():
        from app.sources.ibkr_flex import maybe_reprocess_on_startup
        maybe_reprocess_on_startup()
    threading.Thread(target=_ibkr_reprocess, name="ibkr-reprocess", daemon=True).start()
    from app import companies
    if companies.enabled():
        companies.request_full()  # missing / stale names only; Yahoo stays off the request path
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    from app import companies
    companies.stop()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Trading Journal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    from app import pagecache
    pagecache.install(app)  # inside the login check and the session (added first = innermost)
    app.add_middleware(RequireLogin)
    # Added last => runs first, so the session is available to RequireLogin.
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="tj_session",
                       max_age=60 * 60 * 24 * 14, same_site="lax", https_only=s.cookie_secure)
    from app import timing
    timing.install(app)  # outermost: times the whole request incl. login check
    @app.middleware("http")
    async def view_account(request, call_next):
        from app import currency
        from app.web import ACCOUNT_COOKIE
        raw = request.query_params.get("account") if "account" in request.query_params else request.cookies.get(ACCOUNT_COOKIE, "")
        tok = currency.VIEW_ACCOUNT.set(int(raw) if raw and raw.isdigit() else None)
        try:
            return await call_next(request)
        finally:
            currency.VIEW_ACCOUNT.reset(tok)

    @app.middleware("http")
    async def static_cache(request, call_next):
        # Versioned assets (vendor files and tw.<hash>.css carry the version in the name, our own files ?v=N):
        # cache for a year. Anything else under /static keeps the default revalidation.
        resp = await call_next(request)
        p = request.url.path
        if p.startswith("/static/") and resp.status_code == 200 and (
                p.startswith("/static/vendor/") or p.startswith("/static/tw.") or "v" in request.query_params):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)  # outermost: HTML, JSON and assets go out compressed

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for r in (auth.router, dashboard.router, trades.router, imports.router, settings_routes.router, email_sync.router, reports.router, security.router, pwa.router, backup_routes.router, journal_routes.router, ai_review_routes.router, prefs_routes.router, brokers_routes.router):
        app.include_router(r)

    @app.get("/healthz")
    def healthz():  # no DB; also the keep-awake ping target (never counts as a visit)
        return {"ok": True}

    return app


app = create_app()
