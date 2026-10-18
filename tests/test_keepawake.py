"""Keep-awake: visit window, plus the daily 13:30–21:30 Europe/Dublin window.

Ping functions and the clock are injected. Nothing here touches the network or sleeps for real.
"""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import keepawake
from app.keepawake import DEFAULT_SCANNER_URL, KeepAwake, counts_as_visit

DUBLIN = ZoneInfo("Europe/Dublin")
UTC = timezone.utc


def dublin(y, m, d, hh, mm, ss=0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=DUBLIN).timestamp()


def at_utc(ts) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


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


def _targets(ka):
    journal, scanner = [], []

    async def ping():
        journal.append(ka.clock())
        return "200"

    async def ping_scanner():
        scanner.append(ka.clock())
        return "200"

    return journal, scanner, ping, ping_scanner


def _sleeper(clock, sleeps, start_limit=None):
    async def sleep(seconds):
        if start_limit is not None and clock.t < start_limit:
            assert clock.t + seconds <= start_limit + 60  # must not overshoot the open by more than a minute
        sleeps.append(seconds)
        clock.t += seconds
    return sleep


def test_window_defaults_and_dst_boundaries():
    ka = KeepAwake(url=None, scanner_url=None)
    assert ka.window_label == "13:30-21:30"
    assert ka.tz_name == "Europe/Dublin"
    assert ka.window_interval == 600
    assert ka.lead_seconds == 300
    # Summer (IST, UTC+1) and winter (GMT, UTC+0). 13:30 and 21:30 are local, not UTC.
    for month in (7, 1):
        assert not ka.in_window(dublin(2026, month, 15, 13, 29))
        assert ka.in_window(dublin(2026, month, 15, 13, 30))
        assert ka.in_window(dublin(2026, month, 15, 21, 29, 59))
        assert not ka.in_window(dublin(2026, month, 15, 21, 30))
        assert ka.in_lead(dublin(2026, month, 15, 13, 25))
        assert ka.in_lead(dublin(2026, month, 15, 13, 28))
        assert not ka.in_lead(dublin(2026, month, 15, 13, 24, 59))
        assert not ka.in_lead(dublin(2026, month, 15, 13, 30))
    # A UTC clock would call 12:30 UTC "before 13:30" in summer. Dublin is already open.
    summer_open = dublin(2026, 7, 15, 13, 30)
    summer_close = dublin(2026, 7, 15, 21, 30)
    assert at_utc(summer_open).hour == 12 and at_utc(summer_open).minute == 30
    assert at_utc(summer_close).hour == 20 and at_utc(summer_close).minute == 30
    assert ka.in_window(summer_open) and not ka.in_window(summer_close)
    assert ka.in_window(summer_close - 1) and not ka.in_window(summer_open - 1)
    winter_open = dublin(2026, 1, 15, 13, 30)
    assert at_utc(winter_open).hour == 13 and at_utc(winter_open).minute == 30
    assert ka.in_window(winter_open) and not ka.in_window(winter_open - 60)


def test_scanner_url_is_off_unless_render_or_explicit(monkeypatch):
    for k in ("KEEP_AWAKE_URL", "RENDER_EXTERNAL_URL", "RENDER", "KEEP_AWAKE_SCANNER_URL"):
        monkeypatch.delenv(k, raising=False)
    assert keepawake.public_url() is None
    assert keepawake.scanner_target() is None          # local default must not be the prod scanner
    assert KeepAwake().scanner_url is None
    monkeypatch.setenv("RENDER", "true")
    assert keepawake.scanner_target() == DEFAULT_SCANNER_URL
    monkeypatch.setenv("KEEP_AWAKE_SCANNER_URL", "off")
    assert keepawake.scanner_target() is None
    monkeypatch.setenv("KEEP_AWAKE_SCANNER_URL", "https://example.test/health")
    assert keepawake.scanner_target() == "https://example.test/health"
    monkeypatch.delenv("RENDER", raising=False)
    assert keepawake.public_url() is None              # explicit URL opts in; the default still does not
    assert keepawake.scanner_target() == "https://example.test/health"


def test_env_window_off_keeps_visit_pings_and_skips_scanner(monkeypatch):
    monkeypatch.setenv("KEEP_AWAKE_WINDOW", "off")
    c = Clock(dublin(2026, 1, 15, 13, 28))             # would be the lead, but the window is disabled
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example")
    assert ka.window is None and not ka.in_window() and not ka.in_lead()
    journal, scanner, ping, ping_s = _targets(ka)
    sleeps = []
    ka.touch()
    asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, sleeps), max_loops=3))
    start = dublin(2026, 1, 15, 13, 28)
    assert sleeps == [780, 780, 780]                   # not pulled forward to 13:30
    assert journal == [start + 780, start + 1560, start + 2340]
    assert scanner == []
    assert ka.scanner_pings == 0


def test_env_overrides_window_and_zone(monkeypatch):
    monkeypatch.setenv("KEEP_AWAKE_WINDOW", "09:30-16:00")
    monkeypatch.setenv("KEEP_AWAKE_TZ", "America/New_York")
    ka = KeepAwake(url="https://journal.example")
    ny = ZoneInfo("America/New_York")
    assert ka.tz_name == "America/New_York" and ka.window_label == "09:30-16:00"
    assert ka.in_window(datetime(2026, 1, 15, 9, 30, tzinfo=ny).timestamp())
    assert not ka.in_window(datetime(2026, 1, 15, 9, 29, tzinfo=ny).timestamp())
    assert not ka.in_window(datetime(2026, 1, 15, 16, 0, tzinfo=ny).timestamp())
    # 13:30 Dublin in January is 08:30 in New York, outside the overridden window.
    assert not ka.in_window(dublin(2026, 1, 15, 13, 30))
    monkeypatch.setenv("KEEP_AWAKE_WINDOW", "OFF")
    assert KeepAwake(url="https://journal.example").window is None
    monkeypatch.setenv("KEEP_AWAKE_WINDOW", "nope")
    assert KeepAwake(url="https://journal.example").window_label == "13:30-21:30"


def test_dst_open_pings_both_on_dublin_clock_not_utc():
    for month, utc_hour in ((7, 12), (1, 13)):
        open_at = dublin(2026, month, 15, 13, 30)
        c = Clock(dublin(2026, month, 15, 13, 28))
        ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                       scanner_url="https://scanner.example/api/market/health")
        journal, scanner, ping, ping_s = _targets(ka)
        sleeps = []
        asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, sleeps, open_at), max_loops=3))
        assert sleeps[0] == 120                       # 13:28 → 13:30, not a 600s or 780s sleep
        assert journal[:2] == [open_at, open_at + 600]
        assert scanner[:2] == journal[:2]
        assert at_utc(journal[0]).hour == utc_hour and at_utc(journal[0]).minute == 30
        assert all(t >= open_at for t in scanner)


def test_long_sleep_does_not_overshoot_the_open():
    open_at = dublin(2026, 7, 15, 13, 30)
    c = Clock(dublin(2026, 7, 15, 12, 0))              # 90 min out; a naive 780s loop would step past 13:30
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    journal, scanner, ping, ping_s = _targets(ka)
    sleeps = []
    asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, sleeps, open_at), max_loops=12))
    assert journal[0] == open_at and scanner[0] == open_at
    assert journal[1] == open_at + 600


def test_window_stops_at_close_so_both_can_sleep():
    close_at = dublin(2026, 1, 15, 21, 30)
    c = Clock(dublin(2026, 1, 15, 21, 20))
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    journal, scanner, ping, ping_s = _targets(ka)
    asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, []), max_loops=4))
    assert journal == [close_at - 600]                 # 21:20 only; nothing at or after 21:30
    assert scanner == journal
    assert not ka.in_window()


def test_visit_past_close_keeps_journal_pings_only():
    close_at = dublin(2026, 7, 15, 21, 30)
    start = dublin(2026, 7, 15, 21, 0)
    c = Clock(start)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    ka.touch()                                         # visit window runs until 22:00
    journal, scanner, ping, ping_s = _targets(ka)
    asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, []), max_loops=8))
    assert scanner == [start, start + 600, start + 1200]
    assert all(t < close_at for t in scanner)
    assert journal[:3] == scanner
    after = [t for t in journal if t >= close_at]
    assert after == [close_at + 180, close_at + 180 + 780, close_at + 180 + 1560]
    assert all(t < start + 3600 for t in journal)      # stop once keep_until passes
    assert all(b - a <= 780 for a, b in zip(journal, journal[1:]))
    assert ka.scanner_pings == len(scanner) and ka.pings == len(journal)


def test_one_ping_failing_does_not_stop_the_other():
    c = Clock(dublin(2026, 1, 15, 14, 0))
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    calls = []

    async def boom():
        calls.append("journal")
        raise OSError("down")

    async def ok():
        calls.append("scanner")
        return "200"

    asyncio.run(ka.run(ping=boom, ping_scanner=ok, sleep=_sleeper(c, []), max_loops=1))
    assert sorted(calls) == ["journal", "scanner"]
    assert ka.last_ping_status.startswith("error") and ka.last_scanner_status == "200"
    assert ka.pings == 1 and ka.scanner_pings == 1

    calls.clear()
    c.t = dublin(2026, 1, 15, 15, 0)

    async def ok_j():
        calls.append("journal")
        return "200"

    async def boom_s():
        calls.append("scanner")
        raise TimeoutError("cold")

    asyncio.run(ka.run(ping=ok_j, ping_scanner=boom_s, sleep=_sleeper(c, []), max_loops=1))
    assert set(calls) == {"journal", "scanner"}
    assert ka.last_ping_status == "200" and ka.last_scanner_status.startswith("error")
    assert ka.pings == 2 and ka.scanner_pings == 2


def test_window_logs_open_close_and_urls_only(caplog):
    caplog.set_level(logging.INFO, logger="app.keepawake")
    close_at = dublin(2026, 11, 15, 21, 30)            # winter GMT after the October clock change
    c = Clock(close_at - 600)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    _j, _s, ping, ping_s = _targets(ka)
    asyncio.run(ka.run(ping=ping, ping_scanner=ping_s, sleep=_sleeper(c, []), max_loops=2))
    text = caplog.text
    assert "keep-awake window opened (13:30-21:30 Europe/Dublin)" in text
    assert "keep-awake window closed" in text
    assert "keep-awake ping https://journal.example/healthz -> 200" in text
    assert "keep-awake ping https://scanner.example/api/market/health -> 200" in text
    assert "Authorization" not in text and "User-Agent" not in text and "Bearer" not in text


def test_healthz_is_not_a_visit_but_wakes_the_lead(db, monkeypatch):
    c = Clock(dublin(2026, 1, 15, 13, 28))
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")

    async def stopped(*_a, **_k):
        return None

    monkeypatch.setattr(ka, "run", stopped)            # lifespan must not open a real socket
    monkeypatch.setattr(keepawake, "state", ka)
    from app.main import create_app
    client = TestClient(create_app())
    client.get("/healthz")
    client.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False)
    client.get("/healthz")
    assert ka.last_visit is None and ka.keep_until == 0.0
    assert ka._wake.is_set()                           # 13:28 is inside the five-minute lead

    noon = Clock(dublin(2026, 1, 15, 12, 0))
    ka2 = KeepAwake(minutes=60, interval=780, clock=noon, url=None, scanner_url=None)
    monkeypatch.setattr(keepawake, "state", ka2)
    TestClient(create_app()).get("/healthz")
    assert not ka2._wake.is_set() and ka2.last_visit is None


def test_request_wakes_a_sleep_in_the_lead():
    c = Clock(dublin(2026, 7, 15, 13, 28))
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    started = asyncio.Event()

    async def sleep(seconds):
        started.set()
        await asyncio.Event().wait()                  # parked until the lead wake cancels it

    async def main():
        task = asyncio.create_task(ka._sleep(780, sleep))
        await started.wait()
        assert ka.in_lead()
        ka.on_request()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(main())
    assert c.t == dublin(2026, 7, 15, 13, 28)         # the 780s sleep did not run


def test_settings_block_renders_the_daily_window(db, monkeypatch):
    inside = dublin(2026, 7, 15, 14, 5)
    c = Clock(inside)
    ka = KeepAwake(minutes=60, interval=780, clock=c, url="https://journal.example",
                   scanner_url="https://scanner.example/api/market/health")
    ka.last_ping = inside
    ka.last_ping_status = "200"
    ka.last_scanner_ping = inside
    ka.last_scanner_status = "200"
    ka.pings = 4
    ka.scanner_pings = 4
    ka.touch()

    async def stopped(*_a, **_k):
        return None

    monkeypatch.setattr(ka, "run", stopped)
    monkeypatch.setattr(keepawake, "state", ka)
    from app.main import create_app
    client = TestClient(create_app())
    assert client.post("/login", data={"password": "test-pass", "next": "/settings"}, follow_redirects=False).status_code == 303
    html = client.get("/settings").text
    assert 'id="keep-awake"' in html and 'id="keep-awake-window"' in html
    assert "Daily window 13:30-21:30 Europe/Dublin" in html
    assert "Daily window active" in html
    assert "· active now ·" in html
    assert "next start 2026-07-16 13:30" in html
    assert "next end 2026-07-15 21:30" in html
    assert "journal ping 14:05 (200)" in html
    assert "scanner ping 14:05 (200)" in html
    assert "Awake until" in html

    from app import pagecache
    pagecache.bump()  # /settings is cached; the next render must see the new clock
    outside = Clock(dublin(2026, 1, 15, 12, 0))
    ka.clock = outside
    ka.keep_until = 0
    ka.last_visit = None
    html = client.get("/settings").text
    assert "· inactive now ·" in html
    assert "next start 2026-01-15 13:30" in html
    assert "next end 2026-01-15 21:30" in html
    assert "Daily window active" not in html

    monkeypatch.setenv("KEEP_AWAKE_WINDOW", "off")
    off = KeepAwake(minutes=60, interval=780, clock=outside, url=None, scanner_url=None)
    monkeypatch.setattr(off, "run", stopped)
    monkeypatch.setattr(keepawake, "state", off)
    pagecache.bump()
    client2 = TestClient(create_app())
    client2.post("/login", data={"password": "test-pass", "next": "/settings"}, follow_redirects=False)
    assert "Daily window: off" in client2.get("/settings").text
