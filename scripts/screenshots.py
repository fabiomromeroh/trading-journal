"""Take UI screenshots with headless Chromium: python scripts/screenshots.py [base_url] [password]"""
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "demo-pass"
OUT = Path(__file__).resolve().parent.parent / "screenshots"
OUT.mkdir(exist_ok=True)


def pick_trade_id() -> int:
    con = sqlite3.connect(Path(__file__).resolve().parent.parent / "journal.db")
    row = con.execute("SELECT id FROM trades WHERE status='CLOSED' AND asset_type='STOCK' AND net_pnl > 0 "
                      "AND notes IS NOT NULL AND julianday(closed_at)-julianday(opened_at) < 0.3 "
                      "AND (SELECT count(*) FROM trade_fills f WHERE f.trade_id=trades.id) >= 3 "
                      "ORDER BY net_pnl DESC LIMIT 1").fetchone()
    return row[0]


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 950}, device_scale_factor=1)
    errors = []
    page.on("console", lambda m: errors.append(f"{page.url}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
    page.goto(f"{BASE}/login")
    page.screenshot(path=str(OUT / "00-login.png"))
    page.fill("input[name=password]", PASSWORD)
    page.click("button:has-text('Log in')")
    page.wait_for_url(f"{BASE}/")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / "01-dashboard.png"))
    page.screenshot(path=str(OUT / "01-dashboard-full.png"), full_page=True)
    page.goto(f"{BASE}/trades")
    page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "02-trades.png"), full_page=False)
    tid = pick_trade_id()
    page.goto(f"{BASE}/trades/{tid}")
    page.wait_for_timeout(3000)
    page.screenshot(path=str(OUT / "03-trade-detail.png"), full_page=True)
    page.goto(f"{BASE}/import")
    page.wait_for_timeout(1000)
    page.screenshot(path=str(OUT / "04-import.png"), full_page=True)
    page.goto(f"{BASE}/settings")
    page.wait_for_timeout(1000)
    page.click("button:has-text('Sync now') >> nth=1")
    page.wait_for_timeout(3500)
    page.screenshot(path=str(OUT / "05-settings.png"), full_page=True)
    browser.close()
    print("trade id", tid)
    print("console errors:", errors or "none")
