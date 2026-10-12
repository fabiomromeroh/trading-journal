"""Settings is one full-width page of hash tabs. Every existing form stays in the HTML."""
import re

from fastapi.testclient import TestClient

from app.models import Account


def _client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


def test_settings_tabs_fill_the_page_and_keep_every_form(db):
    db.add(Account(name="Main", broker="schwab", currency="USD", timezone="America/New_York", market="US"))
    db.commit()
    html = _client(db).get("/settings").text
    page = html.split('id="settings-page"', 1)[1]
    assert "max-w-[1500px]" not in page
    assert "settings.css" in html
    assert page.count('class="stab') == 6
    for href in ("#accounts", "#sync", "#display", "#rules", "#data", "#security"):
        assert f'href="{href}"' in page
    # Deep links still name a real anchor, so reload and old bookmarks open that tab.
    for anchor in ("id=\"accounts\"", "id=\"brokers\"", "id=\"sync\"", "id=\"sync-attention\"",
                   "id=\"tos-email\"", "id=\"display\"", "id=\"keep-awake\"", "id=\"rules\"",
                   "id=\"journal-settings\"", "id=\"break-even\"", "id=\"data\"", "id=\"backups\"",
                   "id=\"aliases\"", "id=\"company-info\"", "id=\"security\"", "id=\"install-app\""):
        assert anchor in page, anchor
    assert page.index('id="accounts"') < page.index('id="brokers"') < page.index('id="sync"')
    assert page.index('id="sync"') < page.index('id="sync-attention"') < page.index('id="tos-email"')
    assert page.index('id="tos-email"') < page.index('id="display"') < page.index('name="timezone"')
    assert page.index('name="timezone"') < page.index('id="rules"')
    rules = page[page.index('id="rules"'):page.index('id="data"')]
    assert "Auto stop (5m low of day before entry)" in rules
    assert 'action="/settings/journal/save"' in rules and 'id="break-even"' in rules
    data = page[page.index('id="data"'):page.index('id="security"')]
    assert 'id="backups"' in data and 'id="aliases"' in data and "Refresh company info" in data
    assert 'action="/settings/security/password"' in page[page.index('id="security"'):]
    for action in ("/settings/accounts/", "/settings/accounts/", "/meta", "/auto-stop", "/delete",
                   "/settings/adjustments", "/settings/break-even", "/settings/aliases",
                   "/settings/backup/token", "/settings/demo/load", "/settings/rebuild",
                   "/settings/recompute-excursions", "/settings/companies/refresh",
                   "/prefs", "/sync"):
        assert action in page, action
    # Account rows are a wrapping grid, not a fixed-width table cell.
    assert "set-grid" in page and "set-cards" in page
    assert "w-56" not in page and "w-36" not in page and "w-28" not in page
    assert re.search(r'name="currency"', page)
    assert 'aria-label="Settings sections"' in page


def test_account_meta_returns_to_the_display_tab(db):
    a = Account(name="Main", broker="schwab", currency="USD", timezone="America/New_York", market="US")
    db.add(a)
    db.commit()
    c = _client(db)
    bad = c.post(f"/settings/accounts/{a.id}/meta", data={"currency": "EUR", "timezone": "Not/AZone", "market": "US"},
                 follow_redirects=False)
    assert bad.status_code == 303 and bad.headers["location"].endswith("#display")
    ok = c.post(f"/settings/accounts/{a.id}/meta", data={"currency": "EUR", "timezone": "Europe/Dublin", "market": "UK"},
                follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"].endswith("#display")
    db.expire_all()
    saved = db.get(Account, a.id)
    assert saved.currency == "EUR" and saved.timezone == "Europe/Dublin" and saved.market == "UK"
