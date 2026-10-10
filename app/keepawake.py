"""Keep the Render free instance awake for a while after the user visits, then let it sleep.

Render free web services sleep after 15 min without inbound traffic. Every real visit by the
logged-in user pushes ``keep_until`` to now + KEEP_AWAKE_MINUTES (default 60). While now < keep_until
a background task GETs the app's own *public* URL (/healthz) every KEEP_AWAKE_PING_SECONDS (default
780 s = 13 min), which goes through Render's edge and counts as inbound traffic. Once the window
passes the pings stop and Render puts the service to sleep ~15 min after the last request.

Not counted as visits: the pings themselves (/healthz), static files, /api/ingest/* (Gmail Apps
Script fill posts: they wake the service and run, but don't extend the window), robots.txt,
favicon/app icons, manifest, service worker, offline page, and the htmx background polls of a running sync. State is in memory (a restart/deploy
simply starts with no window; the next visit opens one).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

log = logging.getLogger(__name__)
FALLBACK_URL = "https://trading-journal-xjf0.onrender.com"
IGNORED_PREFIXES = ("/static", "/healthz", "/api/ingest/", "/robots.txt", "/favicon", "/apple-touch-icon",
                    "/manifest.webmanifest", "/sw.js", "/offline")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def public_url() -> str | None:
    """Where to ping. Only on Render (or when KEEP_AWAKE_URL is set), so local runs never ping prod."""
    url = os.getenv("KEEP_AWAKE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not url and os.getenv("RENDER"):
        url = FALLBACK_URL
    return url.rstrip("/") if url else None


def counts_as_visit(method: str, path: str, authed: bool) -> bool:
    if not authed or path.startswith(IGNORED_PREFIXES):
        return False
    if method == "GET" and path.startswith("/sync/") and (path.endswith("/status") or path == "/sync/runs"):
        return False  # htmx background refresh while a sync runs
    return method in ("GET", "POST", "PUT", "PATCH", "DELETE")


@dataclass
class KeepAwake:
    minutes: int = field(default_factory=lambda: _int_env("KEEP_AWAKE_MINUTES", 60))
    interval: int = field(default_factory=lambda: max(30, _int_env("KEEP_AWAKE_PING_SECONDS", 780)))
    clock: Callable[[], float] = time.time
    keep_until: float = 0.0
    last_visit: float | None = None
    last_ping: float | None = None
    last_ping_status: str | None = None
    pings: int = 0
    started: float = field(default_factory=time.time)
    url: str | None = field(default_factory=public_url)

    @property
    def enabled(self) -> bool:
        return self.minutes > 0

    def touch(self, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = self.clock() if now is None else now
        self.last_visit = now
        self.keep_until = max(self.keep_until, now + self.minutes * 60)

    def active(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return self.enabled and now < self.keep_until

    def until_dt(self) -> datetime | None:
        return datetime.fromtimestamp(self.keep_until, timezone.utc) if self.active() else None

    async def run(self, ping: Callable[[], Awaitable[str]] | None = None,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_loops: int | None = None) -> None:
        """Sleep one interval, ping if the window is still open, repeat. Never raises."""
        ping = ping or self._http_ping
        loops = 0
        while max_loops is None or loops < max_loops:
            loops += 1
            await sleep(self.interval)
            if not self.active():
                continue
            try:
                self.last_ping_status = await ping()
            except Exception as exc:  # network hiccup: try again next interval
                self.last_ping_status = f"error: {type(exc).__name__}"
            self.last_ping = self.clock()
            self.pings += 1
            log.info("keep-awake ping %s -> %s (awake until %s UTC)", self.url and self.url + "/healthz",
                     self.last_ping_status, datetime.fromtimestamp(self.keep_until, timezone.utc).strftime("%H:%M"))

    async def _http_ping(self) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "trading-journal-keepawake"}) as c:
            r = await c.get(f"{self.url}/healthz")
            return str(r.status_code)


state = KeepAwake()
