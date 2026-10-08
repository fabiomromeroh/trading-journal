"""thinkorswim fill emails: parser, ingest API (auth, idempotency) and the three-way merge with
SnapTrade same-day orders and next-day activities. Synthetic data only (fake account suffix 901).

NOTE: the email format is UNVERIFIED (no real fill email seen yet); fixtures follow thinkorswim's
documented order notation."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

import tests.test_merge_regression as mr
from app import email_sync
from app.importers import tos_email, tos_statement
from app.models import Account, Execution, InboundEmail, Trade
from app.services import ingest_records
from tests.test_merge_regression import act
from tests.test_order_fills import (DAY1_EVENING, DAY2_MORNING, execs, history, order, sell_activity, sync,
                                    tos_text)
from tests.test_renames_and_pnl import web  # noqa: F401
from tests.test_snaptrade_source import st_env  # noqa: F401

RECV = datetime(2026, 10, 8, 18, 35, 40)          # UTC = 14:35:40 ET


def email(mid, body, recv=RECV, subject="thinkorswim: order filled"):
    return {"message_id": mid, "received_at": recv.isoformat() + "Z", "subject": subject, "body": body,
            "from": "thinkorswim <alerts@thinkorswim.com>"}


def ingest(db, mid, body, recv=RECV):
    return email_sync.ingest_email(db, email(mid, body, recv))


# ----------------------------------------------------------------------------- parser
def test_parse_stock_long_and_short():
    p = tos_email.parse("", "#2000000001 tIP BOT +100 ZETA @52.10 LMT, ACCOUNT ******901", RECV, "<m1>")
    (r,) = p.records
    assert (r.symbol, r.side, r.quantity, r.price, r.asset_type, r.executed_at, r.time_known, str(r.trade_date)) == (
        "ZETA", "BUY", 100, 52.10, "STOCK", RECV, True, "2026-10-08")
    assert r.external_id == "<m1>#0" and p.account_suffix == "901" and r.raw["order"] == "2000000001"
    p = tos_email.parse("SOLD -1,500 XYZ @12.34", "SOLD -1,500 XYZ @12.34 MKT", RECV, "<m2>")  # short sale
    assert [(r.symbol, r.side, r.quantity, r.price, r.position_effect) for r in p.records] == [
        ("XYZ", "SELL", 1500, 12.34, None)]                                       # subject repeat deduped


def test_parse_partial_fill_and_option():
    p = tos_email.parse("", "#2000000002 TOSWeb SOLD -2 ZETA @50.25 LMT", RECV, "<m3>")
    assert [(r.side, r.quantity) for r in p.records] == [("SELL", 2)]
    p = tos_email.parse("", "#1432618991 PAPERMONEY BOT +2 SPY 100 (Weeklys) 12 JUL 19 298.5 CALL "
                            "@.72MARK=298.67 IMPL VOL=13.01% , ACCOUNT D-******00", RECV, "<m4>")
    (r,) = p.records
    assert (r.asset_type, r.underlying, r.option_type, r.strike, r.expiration, r.multiplier, r.quantity, r.price) == (
        "OPTION", "SPY", "CALL", 298.5, date(2019, 7, 12), 100, 2, 0.72)


def test_parse_multiple_fills_html_body_time(fixture_text):
    p = tos_email.parse("Order filled", fixture_text("tos_email_fill.html"), RECV, "<m5>")
    assert [(r.symbol, r.side, r.quantity, r.price) for r in p.records] == [
        ("ZETA", "BUY", 100, 52.10), (p.records[1].symbol, "SELL", 2, 1.35)]
    assert p.records[1].asset_type == "OPTION" and p.records[1].expiration == date(2026, 10, 16)
    assert p.body_time == datetime(2026, 10, 8, 18, 35, 32)                       # 2:35:32 PM ET
    assert all(r.executed_at == p.body_time for r in p.records) and p.account_suffix == "901"


def test_parse_ignores_spreads_and_non_fills():
    p = tos_email.parse("", "#2000000003 SOLD -1 VERTICAL QQQ 100 20 SEP 24 470/475 CALL @1.20 CBOE", RECV, "<m6>")
    assert p.records == [] and len(p.skipped) == 1
    p = tos_email.parse("thinkorswim confirmation code", "Your confirmation code is 123456.", RECV, "<m7>")
    assert p.records == [] and p.skipped == []


def test_body_time_without_zone_or_far_off_is_ignored():
    assert tos_email.parse("", "BOT +1 ZETA @1.00 10/8/26 2:35:32 PM", RECV, "a").body_time is None
    assert tos_email.parse("", "BOT +1 ZETA @1.00 10/1/26 2:35:32 PM ET", RECV, "b").body_time is None


def test_received_at_formats():
    assert email_sync.parse_received("2026-10-08T14:35:40-04:00") == RECV
    assert email_sync.parse_received(1791484540000) == RECV == email_sync.parse_received("1791484540")
    with pytest.raises(ValueError):
        email_sync.parse_received("2026-10-08T14:35:40")


# ----------------------------------------------------------------------------- API
def test_api_auth_idempotency(db, web):  # noqa: F811
    from fastapi.testclient import TestClient
    from app.main import create_app
    anon = TestClient(create_app())                       # the Apps Script has no login session
    body = email("<fill-1@thinkorswim>", "#2000000011 tIP BOT +10 ZETA @52.10 LMT")
    assert anon.post("/api/ingest/tos-email", json=body).status_code == 401          # no token exists yet
    db.add(Account(name="Schwab", broker="schwab"))
    db.commit()
    assert web.post("/settings/tos-email/token", follow_redirects=False).status_code == 303
    token = email_sync.get_token(db)
    assert token and email_sync.get_state(db, email_sync.TOKEN_STATE) != token        # stored encrypted
    assert anon.post("/api/ingest/tos-email", json=body).status_code == 401
    assert anon.post("/api/ingest/tos-email", json=body,
                     headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert db.scalar(select(InboundEmail)) is None
    h = {"Authorization": f"Bearer {token}"}
    r = anon.post("/api/ingest/tos-email", json=body, headers=h)
    assert r.status_code == 200 and r.json()["fills"] == 1 and not r.json()["duplicate"]
    r = anon.post("/api/ingest/tos-email", json=body, headers={"X-Ingest-Token": token})
    assert r.status_code == 200 and r.json() == {"ok": True, "duplicate": True, "status": "fills", "fills": 1}
    db.expire_all()
    assert [(e.source, e.symbol, e.executed_at) for e in execs(db)] == [("tos_email", "ZETA", RECV)]
    stored = db.scalar(select(InboundEmail))
    assert stored.body == body["body"] and stored.status == "fills"
    other = anon.post("/api/ingest/tos-email", json=email("<code@x>", "Your confirmation code is 1."), headers=h)
    assert other.json()["status"] == "ignored" and other.json()["fills"] == 0
    assert anon.post("/api/ingest/tos-email", json={"body": "x"}, headers=h).status_code == 400
    # regenerating invalidates the old token
    web.post("/settings/tos-email/token")
    db.expire_all()
    assert anon.post("/api/ingest/tos-email", json=email("<n>", "x"), headers=h).status_code == 401
    assert len(list(db.scalars(select(Trade)))) == 1
    # discarding the email removes its still-unconfirmed fill; re-posting it stays a no-op
    html = web.get("/settings").text
    assert "Recent emails" in html and f"/settings/tos-email/{stored.id}/discard" in html
    assert web.post(f"/settings/tos-email/{stored.id}/discard", follow_redirects=False).status_code == 303
    db.expire_all()
    assert execs(db) == [] and list(db.scalars(select(Trade))) == []
    assert db.get(InboundEmail, stored.id).status == "discarded"
    assert email_sync.ingest_email(db, body)["duplicate"] and execs(db) == []
    assert email_sync.status(db)["emails"] == 1                                       # the ignored one


def test_settings_section_renders(db, web):  # noqa: F811
    html = web.get("/settings").text
    assert "thinkorswim email sync" in html and "Create ingest token" in html and "Working orders filling" in html
    web.post("/settings/tos-email/token")
    db.expire_all()
    token = email_sync.get_token(db)
    html = web.get("/settings").text
    assert "Regenerate token" in html and "Trading Journal/Fills" in html and "Skip the Inbox" in html
    assert "http://testserver/api/ingest/tos-email" in html and html.count(token) == 1     # only in the script
    assert "moveToArchive" in html and "in:inbox" not in html and "No fill emails received yet" in html


def test_apps_script_contract():
    js = email_sync.apps_script("https://example.test/api/ingest/tos-email", "TKN")
    assert "const TOKEN = 'TKN'" in js and "everyMinutes(1)" in js and "from:' + SENDER" in js
    assert "if (allOk && anyFill)" in js and "thread.moveToArchive()" in js and "markRead" in js
    assert "getUserLabelByName('Trading Journal')" in js and "in:inbox" not in js
    assert "console.log(TOKEN" not in js and "TOKEN)" not in js.replace("'Bearer ' + TOKEN }", "")


# ----------------------------------------------------------------------------- merge
SELL_EMAIL = "#2000000021 tIP SOLD -4 ZETA @50.25 LMT, ACCOUNT ******901"


def _one_timed_sell(db, source, when=RECV, fees=None):
    sells = [e for e in execs(db) if e.side == "SELL"]
    assert len(sells) == 1, [(e.source, e.quantity) for e in sells]
    s = sells[0]
    assert (s.source, s.time_known, s.executed_at) == (source, True, when)
    if fees is not None:
        assert s.fees == pytest.approx(fees)
    trades = list(db.scalars(select(Trade)))
    assert len(trades) == 1 and trades[0].status == "CLOSED"
    return s, trades[0]


@pytest.mark.parametrize("email_first", [True, False])
def test_email_order_activity_become_one_fill(db, st_env, monkeypatch, email_first):  # noqa: F811
    o = order("OID1", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    if email_first:
        ingest(db, "<e1>", SELL_EMAIL)
        sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
    else:
        sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
        ingest(db, "<e1>", SELL_EMAIL)
    db.expire_all()
    s, t = _one_timed_sell(db, "tos_email")
    t.notes = "kept through the merge"
    db.commit()
    # syncing again the same evening changes nothing
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
    _one_timed_sell(db, "tos_email")
    # next day: the activity (fees) becomes the fill, keeping the email's exact time and the journal
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08", orders=[o])
    s, t = _one_timed_sell(db, "snaptrade", fees=0.05)
    assert s.external_id == "OID1" and t.notes == "kept through the merge"
    assert t.net_pnl == pytest.approx((50.25 - 52.10) * 4 - 0.05)
    # the email posted again (e.g. script state lost) is a no-op
    assert ingest(db, "<e1>", SELL_EMAIL)["duplicate"]
    _one_timed_sell(db, "snaptrade")


@pytest.mark.parametrize("seq", ["e1,o,e2", "o,e1,e2", "e1,e2,o"])
def test_partial_fill_emails_order_activity(db, st_env, monkeypatch, seq):  # noqa: F811
    o = order("OID1", "2026-10-08", "SELL", "ZETA", 4, 50.25)
    t1, t2 = datetime(2026, 10, 8, 18, 35, 40), datetime(2026, 10, 8, 18, 36, 5)
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    for step in seq.split(","):
        if step == "o":
            sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07", orders=[o], recent=[o])
        elif step == "e1":
            ingest(db, "<p1>", "#2000000031 tIP SOLD -1 ZETA @50.20 LMT", t1)
        else:
            ingest(db, "<p2>", "#2000000031 tIP SOLD -3 ZETA @50.2667 LMT", t2)
    db.expire_all()
    sells = [e for e in execs(db) if e.side == "SELL"]
    assert sorted((e.source, e.quantity, e.executed_at) for e in sells) == [
        ("tos_email", 1, t1), ("tos_email", 3, t2)]
    assert sum(e.quantity for e in execs(db) if e.side == "SELL") == 4
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08", orders=[o])
    s, _ = _one_timed_sell(db, "snaptrade", when=t1, fees=0.05)
    assert s.quantity == 4


def test_missing_partial_email_is_absorbed_by_activity(db, st_env, monkeypatch):  # noqa: F811
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    ingest(db, "<p1>", "#2000000031 tIP SOLD -1 ZETA @50.20 LMT")           # the 2nd partial email never came
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08")
    _one_timed_sell(db, "snaptrade", fees=0.05)


def test_unconfirmed_email_fill_stays_when_activity_lacks_it(db, st_env, monkeypatch):  # noqa: F811
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    ingest(db, "<x1>", "#2000000041 tIP BOT +5 QQQQ @10.00 LMT")
    sync(db, monkeypatch, DAY2_MORNING, history(), "2026-10-08")
    assert [e.source for e in execs(db) if e.symbol == "QQQQ"] == ["tos_email"]


@pytest.mark.parametrize("tos_first", [True, False])
def test_email_and_tos_statement_do_not_duplicate(db, st_env, monkeypatch, tos_first):  # noqa: F811
    sync(db, monkeypatch, DAY1_EVENING, history(), "2026-10-07")
    acct = db.scalar(select(Account))
    recs = tos_statement.parse(tos_text()).records
    tos_time = next(r.executed_at for r in recs if r.side == "SELL")
    if tos_first:
        ingest_records(db, acct.id, "tos_statement", recs)
        db.commit()
        ingest(db, "<e1>", SELL_EMAIL)
    else:
        ingest(db, "<e1>", SELL_EMAIL)
        ingest_records(db, acct.id, "tos_statement", recs)
        db.commit()
    db.expire_all()
    _one_timed_sell(db, "tos_statement" if tos_first else "tos_email", when=tos_time if tos_first else RECV)
    sync(db, monkeypatch, DAY2_MORNING, history() + [sell_activity()], "2026-10-08")
    _one_timed_sell(db, "snaptrade", when=tos_time if tos_first else RECV, fees=0.05)


def test_email_matches_renamed_ticker(db, st_env, monkeypatch):  # noqa: F811
    mr._n = 600
    hist = [act("2026-10-06", "BUY", "SATS", 4, 30.0)]
    sync(db, monkeypatch, DAY1_EVENING, hist, "2026-10-07")
    ingest(db, "<r1>", "#2000000051 tIP SOLD -4 ECHO @31.00 LMT")
    sync(db, monkeypatch, DAY2_MORNING, hist + [sell_activity(ref="OIDR", sym="SATS", price=31.0)], "2026-10-08")
    sells = [e for e in execs(db) if e.side == "SELL"]
    assert [(e.source, e.symbol, e.executed_at) for e in sells] == [("snaptrade", "SATS", RECV)]
    assert len(list(db.scalars(select(Trade)))) == 1
