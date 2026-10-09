"""Keep-awake window: visits extend it, pings run only inside it, ignored routes don't count."""
import asyncio

from fastapi.testclient import TestClient

from app import keepawake
from app.keepawake import KeepAwake, counts_as_visit


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_deadline_extends_and_never_shrinks():
    c = Clock(1000)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://x")
    assert not ka.active()
    ka.touch()
    assert ka.keep_until == 1000 + 3600 and ka.active()
    c.t = 1000 + 1800
    ka.touch()
    assert ka.keep_until == 1000 + 1800 + 3600
    ka.touch(now=0)  # an older timestamp never shortens the window
    assert ka.keep_until == 1000 + 1800 + 3600
    c.t = ka.keep_until
    assert not ka.active()


def test_disabled_with_zero_minutes():
    ka = KeepAwake(minutes=0, clock=Clock(5), url="https://x")
    ka.touch()
    assert not ka.active() and not ka.enabled


def test_counts_as_visit_rules():
    assert counts_as_visit("GET", "/", True)
    assert counts_as_visit("GET", "/trades/5", True)
    assert counts_as_visit("POST", "/sync", True)            # manual Sync now
    assert not counts_as_visit("GET", "/", False)             # not logged in
    for p in ("/healthz", "/static/app.js", "/api/ingest/tos-email", "/robots.txt", "/favicon.ico"):
        assert not counts_as_visit("GET", p, True) and not counts_as_visit("POST", p, True)
    assert not counts_as_visit("GET", "/sync/12/status", True)  # htmx poll while a sync runs
    assert not counts_as_visit("GET", "/sync/runs", True)
    assert not counts_as_visit("HEAD", "/", True)


def test_ping_loop_stops_after_window_and_resumes_on_visit():
    c = Clock(0)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://x")
    pinged = []

    async def sleep(s):
        c.t += s

    async def ping():
        pinged.append(c.t)
        return "200"

    ka.touch()  # visit at t=0
    asyncio.run(ka.run(ping=ping, sleep=sleep, max_loops=10))   # t = 0 .. 7800
    assert pinged == [780, 1560, 2340, 3120]                   # 13, 26, 39, 52 min; none after 60
    assert all(b - a < 900 for a, b in zip([0] + pinged, pinged))  # never 15 min without traffic
    assert ka.pings == 4 and ka.last_ping_status == "200"
    ka.touch()  # visit at t=7800 opens a new hour
    asyncio.run(ka.run(ping=ping, sleep=sleep, max_loops=6))
    assert pinged[4:] == [7800 + 780 * k for k in (1, 2, 3, 4)]


def test_ping_errors_do_not_kill_the_loop():
    c = Clock(0)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://x")

    async def sleep(s):
        c.t += s

    async def boom():
        raise OSError("down")

    ka.touch()
    asyncio.run(ka.run(ping=boom, sleep=sleep, max_loops=2))
    assert ka.pings == 2 and ka.last_ping_status.startswith("error")


def test_middleware_marks_visits_only_for_real_pages(db, monkeypatch):
    from app.main import create_app
    ka = KeepAwake(minutes=60, interval=780, clock=Clock(100), url="https://x")
    monkeypatch.setattr(keepawake, "state", ka)
    c = TestClient(create_app())
    c.get("/healthz")
    c.get("/login")
    c.post("/api/ingest/tos-email", json={})
    assert ka.last_visit is None                     # nothing counted yet
    c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    c.get("/healthz"); c.get("/static/app.js"); c.get("/sync/runs")
    assert ka.last_visit is None
    c.get("/settings")
    assert ka.keep_until == 100 + 3600
    assert "Keep-awake" in c.get("/settings").text and "Awake until" in c.get("/settings").text


def test_public_url(monkeypatch):
    for k in ("KEEP_AWAKE_URL", "RENDER_EXTERNAL_URL", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    assert keepawake.public_url() is None             # local runs never ping production
    monkeypatch.setenv("RENDER", "true")
    assert keepawake.public_url() == keepawake.FALLBACK_URL
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://svc.onrender.com/")
    assert keepawake.public_url() == "https://svc.onrender.com"
