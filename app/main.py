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
from app.routes import auth, dashboard, email_sync, imports, pwa, reports, security, settings as settings_routes, trades

logging.basicConfig(level=logging.INFO)
PUBLIC_PREFIXES = ("/login", "/healthz", "/static", "/api/ingest/")  # /api/ingest: token auth
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    ka = keepawake.state
    task = None
    if ka.enabled and ka.url:
        task = asyncio.create_task(ka.run())
        logging.getLogger(__name__).info("keep-awake: %d min after each visit, ping every %d s", ka.minutes, ka.interval)
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Trading Journal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(RequireLogin)
    # Added last => runs first, so the session is available to RequireLogin.
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="tj_session",
                       max_age=60 * 60 * 24 * 14, same_site="lax", https_only=s.cookie_secure)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for r in (auth.router, dashboard.router, trades.router, imports.router, settings_routes.router, email_sync.router, reports.router, security.router, pwa.router):
        app.include_router(r)

    @app.get("/healthz")
    def healthz():  # no DB; also the keep-awake ping target (never counts as a visit)
        return {"ok": True}

    return app


app = create_app()
