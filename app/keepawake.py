"""Keep a Render free instance awake after a visit, and across the daily Dublin session.

Render free web services sleep after 15 min without inbound traffic. Two schedules share one loop:

* Visits. A logged-in page view pushes ``keep_until`` to now + KEEP_AWAKE_MINUTES (default 60).
  While that visit window is open and the daily window is not, the loop GETs this app's own public
  ``/healthz`` every KEEP_AWAKE_PING_SECONDS (default 780). The scanner is never pinged.
* Daily window. Defaults to 13:30–21:30 Europe/Dublin, every day, DST-correct (``zoneinfo``).
  Inside it the loop pings ``/healthz`` and the scanner's health route every 600 s. At 21:30 those
  window pings stop. A visit window that is still open keeps journal-only pings going until
  ``keep_until``. Override with KEEP_AWAKE_WINDOW (``13:30-21:30`` or ``off``), KEEP_AWAKE_TZ, and
  KEEP_AWAKE_SCANNER_URL (``off`` disables the scanner).

A ping counts as inbound traffic only when it hits the public URL. ``public_url()`` is set on Render
(or when KEEP_AWAKE_URL is set), so local and test runs do not ping production. The scanner is pinged
only in that case, or when KEEP_AWAKE_SCANNER_URL is an explicit URL.

Not counted as visits: ``/healthz`` (including the external 13:28 wake), static files, ingest, backup,
robots/icons/manifest/service worker, and htmx sync polls. ``/healthz`` still wakes the loop so a
cold start a few minutes before 13:30 can sleep until the open instead of missing it. State is in
memory; a restart starts with no visit window.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)
FALLBACK_URL = "https://trading-journal-fra.onrender.com"
DEFAULT_SCANNER_URL = "https://qullamaggie-scanner-b29q.onrender.com/api/market/health"
DEFAULT_WINDOW = "13:30-21:30"
DEFAULT_TZ = "Europe/Dublin"
WINDOW_PING_SECONDS = 600
LEAD_SECONDS = 300  # a 13:28 wake is inside this lead; pings start at the window open
IGNORED_PREFIXES = ("/static", "/healthz", "/api/ingest/", "/api/backup/", "/robots.txt", "/favicon", "/apple-touch-icon",
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


def scanner_target() -> str | None:
    """Scanner health URL. Default only when ``public_url()`` is set; an explicit env URL opts in; ``off`` disables."""
    raw = os.getenv("KEEP_AWAKE_SCANNER_URL")
    if raw is not None:
        raw = raw.strip()
        if raw.lower() in {"", "off"}:
            return None
        return raw
    if public_url():
        return DEFAULT_SCANNER_URL
    return None


def _default_window() -> tuple[dtime, dtime]:
    left, right = DEFAULT_WINDOW.split("-")
    return dtime.fromisoformat(left), dtime.fromisoformat(right)


def _parse_window(spec: str) -> tuple[dtime, dtime] | None:
    text = (spec or "").strip()
    if text.lower() == "off":
        return None
    try:
        left, right = text.split("-", 1)
        start = dtime.fromisoformat(left.strip())
        end = dtime.fromisoformat(right.strip())
    except ValueError:
        log.warning("keep-awake: bad KEEP_AWAKE_WINDOW %r, using %s", text, DEFAULT_WINDOW)
        return _default_window()
    if (start.hour, start.minute, start.second) >= (end.hour, end.minute, end.second):
        log.warning("keep-awake: window %r must start before it ends, using %s", text, DEFAULT_WINDOW)
        return _default_window()
    return start.replace(microsecond=0), end.replace(microsecond=0)


def _window_from_env() -> tuple[dtime, dtime] | None:
    return _parse_window(os.getenv("KEEP_AWAKE_WINDOW", DEFAULT_WINDOW))


def _tz_from_env() -> str:
    name = (os.getenv("KEEP_AWAKE_TZ") or DEFAULT_TZ).strip() or DEFAULT_TZ
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("keep-awake: bad KEEP_AWAKE_TZ %r, using %s", name, DEFAULT_TZ)
        return DEFAULT_TZ
    return name


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
    window_interval: int = WINDOW_PING_SECONDS
    lead_seconds: int = LEAD_SECONDS
    window: tuple[dtime, dtime] | None = field(default_factory=_window_from_env)
    tz_name: str = field(default_factory=_tz_from_env)
    clock: Callable[[], float] = time.time
    keep_until: float = 0.0
    last_visit: float | None = None
    last_ping: float | None = None
    last_ping_status: str | None = None
    last_scanner_ping: float | None = None
    last_scanner_status: str | None = None
    pings: int = 0
    scanner_pings: int = 0
    started: float = field(default_factory=time.time)
    url: str | None = field(default_factory=public_url)
    scanner_url: str | None = field(default_factory=scanner_target)

    def __post_init__(self) -> None:
        self._wake = asyncio.Event()
        self._window_open = False
        self._tz = ZoneInfo(self.tz_name)

    @property
    def enabled(self) -> bool:
        return self.minutes > 0

    @property
    def window_enabled(self) -> bool:
        return self.window is not None

    @property
    def should_run(self) -> bool:
        """Start the loop only when there is a target. No public URL and no explicit scanner URL → never ping prod."""
        if not self.url and not self.scanner_url:
            return False
        return self.enabled or self.window_enabled

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def window_label(self) -> str | None:
        if not self.window:
            return None
        start, end = self.window
        return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"

    def journal_ping_url(self) -> str | None:
        if not self.url:
            return None
        return self.url.rstrip("/") + "/healthz"

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

    def _bounds_on(self, when: datetime) -> tuple[datetime, datetime]:
        assert self.window is not None
        start_t, end_t = self.window
        start = when.replace(hour=start_t.hour, minute=start_t.minute, second=start_t.second, microsecond=0)
        end = when.replace(hour=end_t.hour, minute=end_t.minute, second=end_t.second, microsecond=0)
        return start, end

    def _now_dt(self, now: float) -> datetime:
        return datetime.fromtimestamp(now, self._tz)

    def in_window(self, now: float | None = None) -> bool:
        if not self.window:
            return False
        now = self.clock() if now is None else now
        now_dt = self._now_dt(now)
        start, end = self._bounds_on(now_dt)
        return start <= now_dt < end

    def in_lead(self, now: float | None = None) -> bool:
        """True in the few minutes before today's open, so a 13:28 wake arms the loop."""
        if not self.window:
            return False
        now = self.clock() if now is None else now
        now_dt = self._now_dt(now)
        start, _end = self._bounds_on(now_dt)
        return start - timedelta(seconds=self.lead_seconds) <= now_dt < start

    def seconds_to_start(self, now: float) -> float | None:
        if not self.window:
            return None
        now_dt = self._now_dt(now)
        start, end = self._bounds_on(now_dt)
        if now_dt < start:
            return (start - now_dt).total_seconds()
        if now_dt < end:
            return 0.0
        tomorrow = now_dt + timedelta(days=1)
        next_start, _next_end = self._bounds_on(tomorrow)
        return (next_start - now_dt).total_seconds()

    def seconds_to_end(self, now: float) -> float | None:
        if not self.in_window(now):
            return None
        now_dt = self._now_dt(now)
        _start, end = self._bounds_on(now_dt)
        return (end - now_dt).total_seconds()

    def next_bounds(self) -> tuple[datetime | None, datetime | None]:
        """Next open and next close in the configured zone. Inside the window the close is today's."""
        if not self.window:
            return None, None
        now_dt = self._now_dt(self.clock())
        start, end = self._bounds_on(now_dt)
        tomorrow = now_dt + timedelta(days=1)
        next_start, next_end = self._bounds_on(tomorrow)
        if now_dt < start:
            return start, end
        if now_dt < end:
            return next_start, end
        return next_start, next_end

    def delay_outside(self, now: float) -> float:
        """Visit cadence, but never sleep past the next open (so a 13:28 wake cannot overshoot 13:30).

        A fresh ping (elapsed 0) still waits a full visit interval, matching the original loop. A ping
        from inside the window that is only part-way through that interval waits the remainder, so the
        handoff at 21:30 does not leave a gap long enough for Render to sleep the journal.
        """
        base = float(self.interval)
        if self.active(now) and self.last_ping is not None:
            elapsed = now - self.last_ping
            if 0 <= elapsed < self.interval:
                base = self.interval - elapsed
        gap = self.seconds_to_start(now)
        if gap is None:
            return base
        return max(0.0, min(base, gap))

    def delay_inside(self, now: float) -> float:
        gap = self.seconds_to_end(now)
        interval = float(self.window_interval)
        if gap is None:
            return interval
        return max(0.0, min(interval, gap))

    def on_request(self) -> None:
        """Cheap window check (no I/O) on every request, including /healthz. Wakes a sleeping loop."""
        now = self.clock()
        if self.in_lead(now) or (self.in_window(now) and not self._window_open):
            self._wake.set()

    def startup_summary(self) -> str:
        scanner = "on" if self.scanner_url else "off"
        if not self.window_enabled:
            return f"daily window off; visit {self.minutes} min, ping every {self.interval}s; scanner {scanner}"
        state = "open" if self.in_window() else "closed"
        nxt, end = self.next_bounds()
        fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M") if dt else "-"  # noqa: E731
        return (f"window {state} {self.window_label} {self.tz_name}; next start {fmt(nxt)}; next end {fmt(end)}; "
                f"visit {self.minutes} min / {self.interval}s; window ping {self.window_interval}s; scanner {scanner}")

    def _sync_window_log(self, now: float) -> None:
        open_now = self.in_window(now)
        if open_now and not self._window_open:
            log.info("keep-awake window opened (%s %s)", self.window_label, self.tz_name)
        elif self._window_open and not open_now:
            log.info("keep-awake window closed")
        self._window_open = open_now

    async def _sleep(self, seconds: float, sleep: Callable[[float], Awaitable[None]]) -> None:
        """Sleep up to ``seconds``. A request that arms the window returns early so the open is not missed."""
        seconds = float(seconds)
        if seconds <= 0:
            return
        if self._wake.is_set():
            self._wake.clear()
            return
        sleep_task = asyncio.create_task(sleep(seconds))
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            done, pending = await asyncio.wait({sleep_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            sleep_task.cancel()
            wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sleep_task
            with contextlib.suppress(asyncio.CancelledError):
                await wake_task
            raise
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if wake_task in done:
            self._wake.clear()
        if sleep_task in done:
            exc = sleep_task.exception()
            if exc:
                raise exc

    async def _one(self, kind: str, url: str, ping: Callable[[], Awaitable[str]]) -> None:
        try:
            status = await ping()
        except Exception as exc:  # one target failing must not cancel the other
            status = f"error: {type(exc).__name__}"
        now = self.clock()
        if kind == "scanner":
            self.last_scanner_status = status
            self.last_scanner_ping = now
            self.scanner_pings += 1
        else:
            self.last_ping_status = status
            self.last_ping = now
            self.pings += 1
        log.info("keep-awake ping %s -> %s", url.split("?", 1)[0], status)

    async def _ping_journal(self, ping: Callable[[], Awaitable[str]]) -> None:
        url = self.journal_ping_url()
        if not url:
            return
        await self._one("journal", url, ping)

    async def _ping_window(self, ping: Callable[[], Awaitable[str]],
                           ping_scanner: Callable[[], Awaitable[str]] | None) -> None:
        jobs = []
        url = self.journal_ping_url()
        if url:
            jobs.append(self._one("journal", url, ping))
        if self.scanner_url and ping_scanner is not None:
            jobs.append(self._one("scanner", self.scanner_url, ping_scanner))
        if jobs:
            await asyncio.gather(*jobs)

    async def _tick(self, ping: Callable[[], Awaitable[str]],
                    ping_scanner: Callable[[], Awaitable[str]] | None,
                    sleep: Callable[[float], Awaitable[None]]) -> None:
        now = self.clock()
        self._sync_window_log(now)
        if self.in_window(now):
            self._wake.clear()  # the lead wake has done its job; don't skip the in-window interval
            await self._ping_window(ping, ping_scanner)
            delay = self.delay_inside(now)
            remain = delay - (self.clock() - now)
            await self._sleep(remain, sleep)
            return
        await self._sleep(self.delay_outside(now), sleep)
        now = self.clock()
        self._sync_window_log(now)
        if self.in_window(now):
            return  # next tick pings at the open, not after another full interval
        if self.active(now):
            await self._ping_journal(ping)

    async def run(self, ping: Callable[[], Awaitable[str]] | None = None,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_loops: int | None = None,
                  ping_scanner: Callable[[], Awaitable[str]] | None = None) -> None:
        """Ping on the visit cadence outside the daily window, and both targets inside it. Never raises on HTTP errors."""
        ping = ping or self._http_ping_journal
        if ping_scanner is None and self.scanner_url:
            ping_scanner = self._http_ping_scanner
        loops = 0
        while max_loops is None or loops < max_loops:
            loops += 1
            await self._tick(ping, ping_scanner, sleep)

    async def _http_get(self, url: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "trading-journal-keepawake"}) as client:
            response = await client.get(url)
            return str(response.status_code)

    async def _http_ping_journal(self) -> str:
        url = self.journal_ping_url()
        if not url:
            return "skipped"
        return await self._http_get(url)

    async def _http_ping_scanner(self) -> str:
        if not self.scanner_url:
            return "skipped"
        return await self._http_get(self.scanner_url)


state = KeepAwake()
