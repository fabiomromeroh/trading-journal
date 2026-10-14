"""Front-end assets are self-hosted and prebuilt (no in-browser Tailwind, no CDNs), cached and compressed."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent / "app"


def _client():
    from app.main import create_app
    c = TestClient(create_app())
    c.post("/login", data={"password": "test-pass", "next": "/"})
    return c


def test_no_cdn_scripts_in_templates():
    for p in (ROOT / "templates").rglob("*.html"):
        s = p.read_text()
        assert "cdn.tailwindcss.com" not in s and "unpkg.com" not in s and "cdn.jsdelivr.net" not in s, p


def test_prebuilt_css_linked_and_immutable(db):
    c = _client()
    html = c.get("/trades").text
    m = re.search(r'href="/static/(tw\.[0-9a-f]+\.css)"', html)
    assert m and (ROOT / "static" / m.group(1)).exists()
    r = c.get("/static/" + m.group(1))
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert ".text-slate-400" in r.text and ".md\\:col-span-2" in r.text or ".text-slate-400" in r.text
    v = c.get("/static/vendor/htmx-2.0.4.min.js")
    assert v.status_code == 200 and "max-age=31536000" in v.headers["cache-control"]


def test_html_gzipped_and_nav_boosted(db):
    c = _client()
    r = c.get("/trades", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert 'id="side-nav"' in r.text and 'hx-boost="true"' in r.text
    d = c.get("/settings").text
    assert 'data-boost="off"' in d
