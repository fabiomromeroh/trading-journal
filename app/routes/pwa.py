"""Installable-app plumbing: web app manifest, service worker (root scope) and offline page.

All three are public (no login redirect) so the browser can fetch them before sign-in, and none of them
counts as a keep-awake visit. The service worker only ever caches /static/ files and the static offline
page; it never touches authenticated HTML, API/ingest calls, POSTs or cross-origin requests.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
THEME = "#0b0f17"
SW_TEMPLATE = STATIC / "sw.template.js"
# Public paths served by this module (also listed in main.PUBLIC_EXACT and keepawake.IGNORED_PREFIXES).
PUBLIC_PATHS = ("/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico", "/apple-touch-icon.png")


def _icon(src: str, size: int, purpose: str = "any") -> dict:
    return {"src": f"/static/icons/{src}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}


def manifest() -> dict:
    icons = [_icon("icon-192.png", 192), _icon("icon-512.png", 512), _icon("icon-maskable-512.png", 512, "maskable"),
             {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]
    return {
        "id": "/", "name": "Trading Journal", "short_name": "Journal",
        "description": "Private trading journal: trades, P&L reports and notes.",
        "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
        "background_color": THEME, "theme_color": THEME, "categories": ["finance", "productivity"],
        "icons": icons,
        "shortcuts": [
            {"name": "Trades", "short_name": "Trades", "url": "/trades", "icons": [_icon("icon-192.png", 192)]},
            {"name": "Reports", "short_name": "Reports", "url": "/reports", "icons": [_icon("icon-192.png", 192)]},
        ],
    }


@lru_cache(maxsize=1)
def sw_version() -> str:
    """Content hash of everything the worker may cache (+ the worker itself): any change to an icon, CSS or
    JS file yields a new version, so the browser installs the new worker and drops the old cache."""
    h = hashlib.sha256()
    for p in sorted(STATIC.rglob("*")):
        if p.is_file() and p.name != "sw.template.js":
            h.update(str(p.relative_to(STATIC)).encode())
            h.update(p.read_bytes())
    h.update(SW_TEMPLATE.read_bytes())
    return h.hexdigest()[:12]


@router.get("/manifest.webmanifest")
def web_manifest():
    return JSONResponse(manifest(), media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@router.get("/sw.js")
def service_worker():
    body = SW_TEMPLATE.read_text().replace("__VERSION__", sw_version())
    # no-cache: the browser must revalidate the worker on every navigation so updates roll out
    return Response(body, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache, max-age=0", "Service-Worker-Allowed": "/"})


@router.get("/offline", response_class=HTMLResponse)
def offline():
    return HTMLResponse(OFFLINE_HTML, headers={"Cache-Control": "no-cache"})


@router.get("/favicon.ico")
def favicon():
    return Response((STATIC / "icons" / "favicon.ico").read_bytes(), media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/apple-touch-icon.png")
def apple_touch_icon():
    return Response((STATIC / "icons" / "apple-touch-icon.png").read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


OFFLINE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline · Trading Journal</title><meta name="theme-color" content="#0b0f17">
<link rel="icon" href="/static/icons/favicon-32.png">
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f17;color:#e2e8f0;font-family:ui-sans-serif,system-ui,sans-serif;padding:24px;box-sizing:border-box}
 main{max-width:340px;text-align:center}
 img{width:84px;height:84px;border-radius:20px}
 h1{font-size:20px;margin:18px 0 6px;color:#fff} p{color:#94a3b8;font-size:14px;line-height:1.5;margin:0 0 20px}
 button{background:#4f46e5;color:#fff;border:0;border-radius:10px;padding:10px 20px;font-size:15px;font-weight:600;cursor:pointer}
</style></head><body><main>
<img src="/static/icons/icon-192.png" alt="">
<h1>You're offline</h1>
<p>Trading Journal needs a connection to load your trades. Nothing is stored on this device. Check your network and try again.</p>
<button onclick="location.reload()">Try again</button>
</main></body></html>
"""
