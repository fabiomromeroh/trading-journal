"""Journal v2: option lists, mistakes, question boxes, default (low-of-day) stop, R / current R / R levels,
coach insights and the AI review export. Numbers in the fixtures are hand-checked (see comments)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import ai_review, coach, metrics, options, stops
from app.models import Account, JournalOption, Tag, Trade, TradeFill, TradeMistake

D0 = datetime(2026, 3, 10, 14, 30)  # 10:30 New York (EDT)


def mk(db, symbol="AAA", direction="LONG", entry=100.0, qty=100.0, net=0.0, status="CLOSED", opened=D0, asset="STOCK",
       stop=None, **kw):
    acct = db.scalar(select(Account)) or Account(name="Test")
    db.add(acct)
    db.flush()
    t = Trade(key=f"{symbol}-{opened.isoformat()}-{direction}-{entry}-{len(db.new)}-{id(kw)}", account_id=acct.id, symbol=symbol, underlying=symbol,
              asset_type=asset, direction=direction, status=status, opened_at=opened,
              closed_at=opened + timedelta(hours=2) if status == "CLOSED" else None, quantity=qty, entry_price=entry,
              exit_price=entry if status == "CLOSED" else None, gross_pnl=net, net_pnl=net, initial_stop=stop,
              multiplier=1.0 if asset == "STOCK" else 100.0, **kw)
    db.add(t)
    db.flush()
    return t


@pytest.fixture()
def client(db):
    from app.main import create_app
    c = TestClient(create_app())
    assert c.post("/login", data={"password": "test-pass", "next": "/"}, follow_redirects=False).status_code == 303
    return c


# ----------------------------------------------------------------------------- default stop
def test_low_of_day_stop_long_and_r(db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 98.0, "high": 104.0})
    t = mk(db, entry=100, qty=100, net=600)
    assert stops.apply_default(db, t) == "set"
    assert (t.initial_stop, t.stop_auto) == (98.0, True)
    # risk/share 2 x 100 sh = $200; 600 / 200 = 3.0R
    assert metrics.risk_per_share(t) == 2.0
    assert metrics.risk_of(t) == (200.0, "auto stop")
    assert metrics.r_multiple(t) == 3.0


def test_high_of_day_stop_short(db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 98.0, "high": 104.0})
    t = mk(db, direction="SHORT", entry=100, qty=50, net=-100)
    stops.apply_default(db, t)
    # short: stop = high 104; risk/share 4 x 50 = 200; -100 / 200 = -0.5R
    assert t.initial_stop == 104.0 and metrics.risk_of(t)[0] == 200.0 and metrics.r_multiple(t) == -0.5


def test_manual_stop_wins_and_rules(db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 98.0, "high": 104.0})
    t = mk(db, entry=100, stop=97.0)
    assert stops.apply_default(db, t) == "kept" and t.initial_stop == 97.0 and not t.stop_auto
    t2 = mk(db, symbol="BBB", entry=50)
    stops.set_rule(db, "manual")
    assert stops.apply_default(db, t2) == "off" and t2.initial_stop is None
    stops.set_rule(db, "low_of_day")
    opt = mk(db, symbol="OPT", asset="OPTION", entry=2.0, qty=1)
    assert stops.apply_default(db, opt) == "option" and opt.initial_stop is None
    rk = mk(db, symbol="CCC", entry=10, risk_amount=75.0)
    assert stops.apply_default(db, rk) == "has-risk" and rk.initial_stop is None


def test_auto_stop_refreshes_only_while_day_is_young(db, monkeypatch):
    bars = iter([{"low": 98.0, "high": 104.0}, {"low": 97.0, "high": 104.0}])
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: next(bars))
    t = mk(db, opened=D0)
    now = D0 + timedelta(hours=1)           # entry day still running: the day's low can still fall
    assert stops.apply_default(db, t, now) == "set" and t.initial_stop == 98.0
    assert stops.apply_default(db, t, now) == "refreshed" and t.initial_stop == 97.0
    later = D0 + timedelta(days=5)          # long after the day closed: final
    assert stops.apply_default(db, t, later) == "kept"


def test_entry_day_uses_new_york_date_and_daily_bar(db, monkeypatch):
    from app import prices
    t = mk(db, opened=datetime(2026, 3, 11, 1, 30))   # 21:30 EDT on Mar 10 = New York date Mar 10
    assert str(stops.entry_day(t)) == "2026-03-10"
    day9 = int(datetime(2026, 3, 9, 14, 30).timestamp())   # naive local ts is fine: normalize re-keys by ET date
    from datetime import timezone
    ts = lambda d, h, m: int(datetime(2026, 3, d, h, m, tzinfo=timezone.utc).timestamp())  # noqa: E731
    _ = day9
    candles = [{"time": ts(9, 13, 30), "open": 1, "high": 5, "low": 0.5, "close": 2, "volume": 1},
               {"time": ts(10, 13, 30), "open": 100, "high": 104, "low": 98, "close": 101, "volume": 1},
               {"time": ts(11, 13, 30), "open": 100, "high": 110, "low": 90, "close": 101, "volume": 1}]
    monkeypatch.setattr(prices, "_provider_order", lambda trade: ["yahoo"])
    monkeypatch.setattr(prices, "_fetch", lambda *a, **k: candles)
    bar = stops.day_bar(db, t, now=datetime(2026, 3, 20))
    assert (bar["low"], bar["high"]) == (98, 104)


def test_backfill_counts(db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 90.0, "high": 120.0} if t.symbol != "NOD" else None)
    mk(db, symbol="AAA")
    mk(db, symbol="NOD")
    mk(db, symbol="MAN", stop=95.0)
    mk(db, symbol="OPT", asset="OPTION", entry=2, qty=1)
    res = stops.backfill(db, now=datetime(2026, 4, 1))
    assert res["set"] == 1 and res["no-data"] == 1 and res["option"] == 1
    assert db.scalar(select(Trade).where(Trade.symbol == "MAN")).initial_stop == 95.0


# ----------------------------------------------------------------------------- R maths
def test_current_r_open_trade_with_partial_exit():
    # 100 sh @100, sold 40 @105 (net realized 198 after 2 fees), 60 sh left; stop 98 -> risk 2 x 100 = 200
    fills = [NS(role="OPEN", position=0, quantity=100.0, price=100.0),
             NS(role="CLOSE", position=1, quantity=40.0, price=105.0)]
    t = NS(status="OPEN", direction="LONG", entry_price=100.0, quantity=100.0, multiplier=1.0, net_pnl=198.0, gross_pnl=200.0,
           initial_stop=98.0, stop_auto=True, risk_amount=None, profit_target=None, mfe=500.0, mae=-100.0, fills=fills,
           cost_basis=0, fees=2.0, closed_at=None, opened_at=D0, exit_price=None)
    m = metrics.trade_metrics(t, None, price=103.0)
    # open pnl = 60 x 3 = 180; (198 + 180) / 200 = 1.89R
    assert m["open_pnl"] == 180.0 and round(m["current_r"], 2) == 1.89 and m["r_now"] == m["current_r"]
    assert m["mfe_r"] == 2.5 and m["mae_r"] == -0.5          # 500/200, -100/200
    assert metrics.trade_metrics(t, None)["current_r"] is None   # no live price -> unknown, never guessed


def test_open_short_current_r():
    fills = [NS(role="OPEN", position=0, quantity=50.0, price=100.0)]
    t = NS(status="OPEN", direction="SHORT", entry_price=100.0, quantity=50.0, multiplier=1.0, net_pnl=0.0, gross_pnl=0.0,
           initial_stop=104.0, stop_auto=None, risk_amount=None, profit_target=None, mfe=None, mae=None, fills=fills,
           cost_basis=0, fees=0, closed_at=None, opened_at=D0, exit_price=None)
    # risk 4 x 50 = 200; price 96 -> open pnl +200 -> +1R
    assert metrics.trade_metrics(t, None, price=96.0)["current_r"] == 1.0


def test_stop_warnings_and_invalid_stop_ignored():
    def tr(stop, d="LONG"):
        return NS(direction=d, entry_price=100.0, initial_stop=stop, quantity=10.0, multiplier=1.0, risk_amount=None,
                  stop_auto=None, net_pnl=50.0, status="CLOSED")
    bad = tr(101.0)
    assert metrics.risk_of(bad) == (None, None) and "above" in metrics.risk_warnings(bad)[0]
    assert "below" in metrics.risk_warnings(tr(99.0, "SHORT"))[0]
    assert "tiny" in metrics.risk_warnings(tr(99.95))[0]       # 0.05% of entry
    assert "huge" in metrics.risk_warnings(tr(70.0))[0]        # 30%
    assert metrics.risk_warnings(tr(97.0)) == []


def test_r_levels_prices():
    t = NS(direction="LONG", entry_price=100.0, initial_stop=98.0, quantity=10.0, multiplier=1.0)
    assert [x["price"] for x in metrics.r_levels(t, [1, 3, 8, 10])] == [102.0, 106.0, 116.0, 120.0]
    s = NS(direction="SHORT", entry_price=100.0, initial_stop=104.0, quantity=10.0, multiplier=1.0)
    assert [x["price"] for x in metrics.r_levels(s, [3, 8, 10])] == [88.0, 68.0, 60.0]
    assert metrics.r_levels(NS(direction="LONG", entry_price=100.0, initial_stop=None, quantity=1, multiplier=1), [3]) == []


# ----------------------------------------------------------------------------- option lists
def test_options_seed_add_remove_rename(db):
    t = mk(db, setup="Breakout")
    t.tags = [Tag(name="A+")]
    db.commit()
    assert options.names(db, "mistake") == options.DEFAULT_MISTAKES        # seeded defaults
    assert options.names(db, "setup") == ["Breakout"] and options.names(db, "tag") == ["A+"]   # from existing trades
    assert options.add(db, "setup", "  Episodic   Pivot ") == "Episodic Pivot"
    assert options.add(db, "setup", "episodic pivot") == "Episodic Pivot"   # case-insensitive, no duplicate
    db.commit()
    assert options.names(db, "setup") == ["Breakout", "Episodic Pivot"]
    # remove = list only; the trade keeps its value
    assert options.remove(db, "setup", "Breakout")
    db.commit()
    assert options.names(db, "setup") == ["Episodic Pivot"] and db.get(Trade, t.id).setup == "Breakout"
    # rename propagates to trades, and merges into an existing name
    options.add(db, "mistake", "Hesitated")
    t.mistake_rows.append(TradeMistake(trade_id=t.id, name="FOMO"))
    db.commit()
    assert options.rename(db, "mistake", "FOMO", "Fear of missing out") == "Fear of missing out"
    db.commit()
    db.expire_all()
    assert db.get(Trade, t.id).mistakes == ["Fear of missing out"]
    assert "FOMO" not in options.names(db, "mistake")
    assert options.rename(db, "mistake", "Hesitated", "Oversized") == "Oversized"   # merge
    assert options.names(db, "mistake").count("Oversized") == 1


def test_options_endpoints_and_seed_once(client, db):
    r = client.post("/options/mistake", content=json.dumps({"name": "Fat finger"})).json()
    assert r["ok"] and r["name"] == "Fat finger" and r["options"][-1] == "Fat finger"
    assert client.post("/options/mistake/remove", content=json.dumps({"name": "FOMO"})).json()["ok"]
    # removed defaults do not come back on the next read
    assert "FOMO" not in client.post("/options/mistake", content=json.dumps({"name": "x"})).json()["options"]
    assert client.post("/options/nope", content="{}").status_code == 404


# ----------------------------------------------------------------------------- journal panel
def test_journal_save_mistakes_answers_grade_and_autosave(client, db):
    t = mk(db, notes="old single note", setup="Breakout")
    db.commit()
    page = client.get(f"/trades/{t.id}").text
    # old notes text lives on in "Other notes" (no data loss); question boxes exist and are empty
    assert "Other notes" in page and "old single note" in page and "Why did I take this trade?" in page
    assert "What went well?" in page and "What went wrong?" in page and "Emotions / state of mind" in page
    r = client.post(f"/trades/{t.id}/journal", headers={"x-autosave": "1"}, data={
        "mistakes": "Chased entry, Brand new mistake", "grade": "b", "ans_well": "Patient entry", "ans_plan__choice": "partly",
        "ans_plan": "moved stop once"})
    assert r.json()["ok"]
    db.expire_all()
    t = db.get(Trade, t.id)
    assert t.mistakes == ["Brand new mistake", "Chased entry"] and t.exec_grade == "B"
    assert t.answers == {"well": "Patient entry", "plan__choice": "partly", "plan": "moved stop once"}
    assert t.notes == "old single note" and t.setup == "Breakout"        # untouched: only posted fields change
    assert "Brand new mistake" in options.names(db, "mistake")            # typed option auto-added
    client.post(f"/trades/{t.id}/journal", headers={"x-autosave": "1"}, data={"mistakes": "", "ans_well": ""})
    db.expire_all()
    assert db.get(Trade, t.id).mistakes == [] and "well" not in db.get(Trade, t.id).answers


def test_question_config_rename_keeps_answers(client, db):
    t = mk(db)
    t.journal = json.dumps({"well": "x"})
    db.commit()
    qs = options.get_questions(db)
    qs[1]["label"] = "Best part?"
    qs.append({"id": "", "label": "Did I size right?", "type": "text"})
    qs = [q for q in qs if q["id"] != "emotions"]
    assert client.post("/settings/journal-questions", content=json.dumps({"questions": qs})).json()["ok"]
    page = client.get(f"/trades/{t.id}").text
    assert "Best part?" in page and "Did I size right?" in page and "Emotions / state" not in page
    assert 'name="ans_well"' in page and ">x</textarea>" in page      # id kept -> old answer still there


def test_trade_page_filters_and_reports_mistakes(client, db):
    a = mk(db, symbol="AAA", net=300, setup="Breakout")
    b = mk(db, symbol="BBB", net=-200)
    c = mk(db, symbol="CCC", net=-100)
    a.mistake_rows.append(TradeMistake(trade_id=a.id, name="FOMO"))
    b.mistake_rows.append(TradeMistake(trade_id=b.id, name="FOMO"))
    c.mistake_rows.append(TradeMistake(trade_id=c.id, name="No stop"))
    db.commit()
    html = client.get("/trades?mistake=FOMO").text
    assert "AAA" in html and "BBB" in html and "CCC" not in html
    rep = client.get("/reports?tab=tags").text
    assert "Mistakes" in rep and "FOMO" in rep and "No stop" in rep
    b_rows = {r["label"]: r for r in metrics.breakdowns([a, b, c], "America/New_York")["mistake"]}
    assert b_rows["FOMO"]["trades"] == 2 and b_rows["FOMO"]["net"] == 100 and b_rows["FOMO"]["avg"] == 50
    assert client.get("/reports?tab=tags&mistake=FOMO").status_code == 200


def test_risk_form_manual_stop_wins_and_reset(client, db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 98.0, "high": 104.0})
    t = mk(db, entry=100, qty=100, net=600, opened=datetime(2026, 1, 5, 15, 0))
    db.commit()
    client.get(f"/trades/{t.id}")       # page load applies the default
    db.expire_all()
    assert db.get(Trade, t.id).initial_stop == 98.0 and db.get(Trade, t.id).stop_auto
    page = client.get(f"/trades/{t.id}").text
    assert "auto: low of entry day" in page and "3.00R" in page
    client.post(f"/trades/{t.id}/risk", data={"initial_stop": "95", "risk_amount": "", "profit_target": ""})
    db.expire_all()
    assert db.get(Trade, t.id).initial_stop == 95.0 and not db.get(Trade, t.id).stop_auto
    client.get(f"/trades/{t.id}")
    db.expire_all()
    assert db.get(Trade, t.id).initial_stop == 95.0                  # still manual
    client.post(f"/trades/{t.id}/risk", data={"initial_stop": "95", "reset_auto": "1"})
    db.expire_all()
    assert db.get(Trade, t.id).initial_stop == 98.0 and db.get(Trade, t.id).stop_auto


def test_chart_page_carries_stop_and_r_levels(client, db, monkeypatch):
    monkeypatch.setattr(stops, "day_bar", lambda db_, t, now=None: {"low": 98.0, "high": 104.0})
    t = mk(db, entry=100, qty=10, net=10, opened=datetime(2026, 1, 5, 15, 0))
    db.commit()
    page = client.get(f"/trades/{t.id}").text
    assert "stop: 98.0" in page and "rLevels: [3, 8, 10]" in page and "showR: true" in page
    client.post("/settings/journal/save", data={"r_form": "1", "r_levels": "1, 2, 5", "r_show": "on"})
    assert "rLevels: [1, 2, 5]" in client.get(f"/trades/{t.id}").text
    assert client.post("/chart/r-levels", content=json.dumps({"show": False, "levels": [4]})).json()["levels"] == [4]
    assert "showR: false" in client.get(f"/trades/{t.id}").text


# ----------------------------------------------------------------------------- coach + AI review
def _book(db):
    """12 closed trades: setup A = +3R wins, setup B = -1R losses; FOMO on the B trades."""
    ts = []
    for i in range(6):
        t = mk(db, symbol=f"W{i}", entry=100, qty=10, net=300, stop=99.0, setup="A+", opened=D0 + timedelta(days=i), mfe=1000.0)
        ts.append(t)
    for i in range(6):
        t = mk(db, symbol=f"L{i}", entry=100, qty=10, net=-100, stop=99.0, setup="B", opened=D0 + timedelta(days=10 + i))
        t.mistake_rows.append(TradeMistake(trade_id=t.id, name="FOMO"))
        ts.append(t)
    db.commit()
    return ts


def test_coach_insights_hand_checked(db):
    ts = _book(db)
    # risk = 1 x 10 = $10 per trade -> winners +30R, losers -10R (stop is 1 below entry, size 10)
    got = {i["title"]: i["text"] for i in coach.insights(ts, "America/New_York")}
    assert "Best setup: A+" in got and "+30.00R" in got["Best setup: A+"] and "-10.00R" in got["Best setup: A+"]
    assert "Losing setup: B" in got and "-$600" in got["Losing setup: B"]
    assert "Mistake: FOMO" in got and "-$600" in got["Mistake: FOMO"] and "6 trades" in got["Mistake: FOMO"]
    # winners keep 300 of 1000 MFE = 30%: "cut winners early"
    assert "You cut winners early" in got and "30%" in got["You cut winners early"] and "$4,200" in got["You cut winners early"]
    # avg win 300 vs avg loss 100: good payoff; break-even win rate 100/400 = 25%, yours 50%
    assert "Winners outsize losers" in got and "25%" in got["Winners outsize losers"] and "50%" in got["Winners outsize losers"]


def test_coach_needs_a_sample_and_flags_no_stop(db):
    few = [mk(db, symbol=f"X{i}", net=10) for i in range(3)]
    assert "Not enough" in coach.insights(few)[0]["title"]
    many = [mk(db, symbol=f"Y{i}", net=10 + i, opened=D0 + timedelta(days=i)) for i in range(6)]
    titles = [i["title"] for i in coach.insights(many)]
    assert "Trades without a stop" in titles


def test_ai_review_summary_is_anonymous_and_complete(db, client):
    ts = _book(db)
    ts[0].notes = "private note about my day"
    ts[0].journal = json.dumps({"well": "waited for the pullback", "plan__choice": "yes"})
    ts[6].journal = json.dumps({"wrong": "chased it", "emotions": "anxious"})
    ts[6].exec_grade = "D"
    db.commit()
    data = ai_review.build(ts, "America/New_York", None, period="all time", symbols=False, notes=True)
    blob = json.dumps(data)
    assert "W0" not in blob and "L0" not in blob and "T1" in blob                  # symbols masked
    assert "Test" not in data["about"]["privacy"] and "account" in data["about"]["privacy"]
    assert data["overall"]["closed_trades"] == 12 and data["overall"]["win_rate_pct"] == 50.0
    assert data["risk_r"]["avg_r"] == 10.0 and data["risk_r"]["trades_without_stop_or_risk"] == 0   # (30 - 10)/2
    assert data["by_mistake"][0]["name"] in ("FOMO", "(no mistake)")
    assert data["most_repeated_mistakes"] == [{"mistake": "FOMO", "times": 6, "total_pnl": -600.0}]
    assert data["followed_plan"] == {"yes": {"trades": 1, "avg_pnl": 300.0}}
    assert data["exits_excursions"]["mfe_capture_on_winners_pct"] == 30.0
    assert any("waited for the pullback" in s for s in data["notes"]["went_well"])
    assert any("anxious" in s for s in data["notes"]["emotions"])
    no_notes = ai_review.build(ts, "America/New_York", None, symbols=True, notes=False)
    assert "notes" not in no_notes and "private note" not in json.dumps(no_notes) and "W0" in json.dumps(no_notes)
    md = ai_review.to_markdown(data)
    assert md.startswith("You are my trading coach") and "```json" in md


def test_ai_review_page_and_downloads(client, db):
    _book(db)
    page = client.get("/ai-review").text
    assert "Copy prompt + data" in page and "You are my trading coach" in page and "Nothing is sent from this server" in page
    assert "Coach insights" in page
    md = client.get("/ai-review/export.md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"] and "trading coach" in md.text
    js = client.get("/ai-review/export.json?symbols=0&notes=0")
    assert js.json()["overall"]["closed_trades"] == 12 and "W0" not in js.text
    assert client.get("/ai-review/export.json?preset=7d").json()["overall"]["closed_trades"] == 0
    # requires login
    anon = TestClient(client.app)
    assert anon.get("/ai-review", follow_redirects=False).status_code == 303


def test_dashboard_has_coach_widget_and_stat_widgets(client, db):
    from app.models import Execution
    _book(db)
    db.add(Execution(account_id=1, source="demo", external_id="x", match_key="x", symbol="W0", underlying="W0", asset_type="STOCK",
                     side="BUY", quantity=1, price=1, executed_at=D0))
    db.commit()
    dash = client.get("/").text
    assert "Coach insights" in dash and "Best setup: A+" in dash
    from app import widgets
    ids = {w["id"] for w in widgets.CATALOGS["trade"]}
    assert {"r_now", "stop", "risk", "mfe_mae_r", "mistakes", "exec_grade"} <= ids
    assert "coach" in {w["id"] for w in widgets.CATALOGS["dashboard"]} and "tbl_mistake" in {w["id"] for w in widgets.CATALOGS["dashboard"]}


# ----------------------------------------------------------------------------- migration
def test_migration_0004_keeps_old_notes(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from app.migrate import ROOT
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "0003")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO accounts (id, name, broker, is_demo, created_at) VALUES (1,'A','schwab',0,'2026-01-01')"))
        c.execute(text("INSERT INTO trades (key, account_id, symbol, underlying, asset_type, multiplier, direction, status, opened_at, "
                       "time_known, quantity, open_quantity, entry_price, cost_basis, gross_pnl, fees, net_pnl, is_demo, notes, setup, updated_at) "
                       "VALUES ('k',1,'AAA','AAA','STOCK',1,'LONG','CLOSED','2026-01-02',1,10,0,5,50,1,0,1,0,'my old note','Breakout','2026-01-02')"))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        row = c.execute(text("SELECT notes, setup, stop_auto, exec_grade, journal FROM trades")).one()
        assert tuple(row) == ("my old note", "Breakout", None, None, None)
        assert c.execute(text("SELECT count(*) FROM journal_options")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM trade_mistakes")).scalar() == 0
    command.downgrade(cfg, "0003")           # reversible
    with eng.connect() as c:
        assert c.execute(text("SELECT notes FROM trades")).scalar() == "my old note"


_ = (JournalOption, TradeFill)
