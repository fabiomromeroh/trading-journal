"""Installable app: manifest, service worker, offline page, icons, public access, keep-awake exclusion."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import keepawake
from app.routes import pwa

ICONS = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"


@pytest.fixture()
def anon(db):
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture()
def authed(anon):
    assert anon.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return anon


def test_public_routes_need_no_login(anon):
    for path in ("/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico", "/apple-touch-icon.png",
                 "/static/icons/icon-192.png", "/static/icons/icon.svg"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 200, path
    assert anon.get("/", follow_redirects=False).status_code == 303   # real pages still protected


def test_manifest(anon):
    r = anon.get("/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert (m["name"], m["short_name"], m["start_url"], m["display"]) == ("Trading Journal", "Journal", "/", "standalone")
    assert m["theme_color"] == m["background_color"] == "#0b0f17"
    purposes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert ("192x192", "any") in purposes and ("512x512", "any") in purposes and ("512x512", "maskable") in purposes
    assert {s["url"] for s in m["shortcuts"]} == {"/trades", "/reports"}
    for i in m["icons"]:                          # every referenced icon is really served
        assert anon.get(i["src"]).status_code == 200, i["src"]


def test_icon_files_have_expected_sizes():
    from PIL import Image
    for name, px in (("favicon-16.png", 16), ("favicon-32.png", 32), ("favicon-48.png", 48), ("apple-touch-icon.png", 180),
                     ("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512)):
        assert Image.open(ICONS / name).size == (px, px), name
    ico = Image.open(ICONS / "favicon.ico")
    assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info["sizes"])
    assert Image.open(ICONS / "icon-maskable-512.png").convert("RGBA").getpixel((0, 0))[3] == 255   # full-bleed
    assert Image.open(ICONS / "icon-512.png").convert("RGBA").getpixel((0, 0))[3] == 0              # rounded corners


def test_service_worker_is_versioned_and_safe(anon):
    r = anon.get("/sw.js")
    assert r.headers["content-type"].startswith("text/javascript")
    assert "no-cache" in r.headers["cache-control"] and r.headers["service-worker-allowed"] == "/"
    js = r.text
    ver = pwa.sw_version()
    assert ver in js and "__VERSION__" not in js and len(ver) == 12
    # the safety rules are in the worker: GET-only, same origin, navigations never cached, only /static/ cached
    assert "req.method !== 'GET'" in js and "url.origin !== self.location.origin" in js
    assert "startsWith('/static/')" in js and "/offline" in js
    assert "/api/" not in js.replace("/api/*", "")        # (only mentioned in the comment)
    # cache-busting: the version follows the content of the static files
    probe = pwa.STATIC / "zz_probe_asset.txt"
    try:
        probe.write_text("a"); pwa.sw_version.cache_clear(); v1 = pwa.sw_version()
        probe.write_text("b"); pwa.sw_version.cache_clear(); v2 = pwa.sw_version()
    finally:
        probe.unlink(missing_ok=True); pwa.sw_version.cache_clear()
    assert v1 != v2 != ver


def test_offline_page_is_self_contained(anon):
    html = anon.get("/offline").text
    assert "You're offline" in html and "https://" not in html.replace("http://www.w3.org", "")


def test_pages_link_manifest_icons_and_theme(authed, anon):
    for html in (authed.get("/settings").text, anon.get("/login").text, anon.get("/login/forgot").text):
        for needle in ('rel="manifest" href="/manifest.webmanifest"', 'rel="apple-touch-icon"', 'href="/favicon.ico"',
                       'name="theme-color"', 'name="mobile-web-app-capable"', "serviceWorker.register('/sw.js'"):
            assert needle in html, needle


def test_settings_install_hint(authed):
    html = authed.get("/settings").text
    assert 'id="install-app"' in html and "Add to Home Screen" in html and "Install app" in html


def test_keepawake_ignores_pwa_requests(authed, monkeypatch):
    for p in ("/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico", "/apple-touch-icon.png",
              "/static/icons/icon-192.png"):
        assert not keepawake.counts_as_visit("GET", p, True), p
    assert keepawake.counts_as_visit("GET", "/settings", True)
    ka = keepawake.KeepAwake()
    monkeypatch.setattr(keepawake, "state", ka)
    for p in ("/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico", "/static/icons/icon-512.png"):
        authed.get(p)
    assert ka.keep_until == 0.0 and ka.last_visit is None      # none of them opened a keep-awake window
    authed.get("/settings")
    assert ka.keep_until > 0 and ka.last_visit is not None
