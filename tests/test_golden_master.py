"""Phase 0 regression harness for the multi-broker work.

1. Fixture dataset: Schwab CSV + thinkorswim statement (merged) + SnapTrade activities -> executions, trades, metrics,
   summary. Baseline committed in tests/golden/fixtures_baseline.json (generated BEFORE any multi-broker change).
2. Real-data snapshot (optional, local only): a restore of the live JSON backup compared with
   backups/golden-real-baseline.json (both gitignored, they contain real trades). Skipped when absent.
Refresh a baseline deliberately with UPDATE_GOLDEN=1 (never to make a failing diff pass)."""
from __future__ import annotations

import glob
import gzip
import json
import os
from pathlib import Path

import pytest

from app import db as dbmod
from app.importers import parse
from app.models import Account
from app.services import ingest_records, rebuild_trades
from app.sources.snaptrade import parse_activities
from tests.golden import dumps, snapshot, subset_diff

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "golden" / "fixtures_baseline.json"
REAL_BASELINE = ROOT / "backups" / "golden-real-baseline.json"
REAL_BACKUP = os.environ.get("GOLDEN_REAL_BACKUP") or (ROOT / "backups" / "golden-real-source.json.gz")


def build_fixture_dataset(db, fixture_text):
    a = Account(name="Schwab fixtures", broker="schwab")
    b = Account(name="Schwab snaptrade", broker="schwab")
    db.add_all([a, b])
    db.flush()
    ingest_records(db, a.id, "schwab_csv", parse(fixture_text("schwab_transactions.csv")).records)
    ingest_records(db, a.id, "tos_statement", parse(fixture_text("tos_statement.csv")).records)
    recs, _ = parse_activities(json.loads(fixture_text("snaptrade_activities.json")))
    ingest_records(db, b.id, "snaptrade", recs)
    rebuild_trades(db)
    db.commit()


def test_fixture_dataset_matches_golden_baseline(db, fixture_text):
    build_fixture_dataset(db, fixture_text)
    snap = snapshot(db)
    assert snap["executions"] and snap["trades"], "fixtures produced nothing"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        BASELINE.write_text(dumps(snap))
        pytest.skip("baseline written")
    expected = json.loads(BASELINE.read_text())
    diff = subset_diff(expected, snap)
    assert not diff, "Schwab/SnapTrade/ToS behaviour changed:\n" + "\n".join(diff[:25])


def _real_data(path):
    with (gzip.open(path, "rt") if str(path).endswith(".gz") else open(path)) as f:
        return json.load(f)


@pytest.mark.skipif(not (Path(REAL_BACKUP).exists() and REAL_BASELINE.exists() or os.environ.get("UPDATE_GOLDEN") == "1" and Path(REAL_BACKUP).exists()),
                    reason="no local real-data backup/baseline (backups/ is gitignored)")
def test_real_data_snapshot_unchanged(tmp_path):
    from app import outcome
    from app.db import Base
    from scripts.restore_backup import restore
    dbmod.configure(f"sqlite:///{tmp_path / 'real.db'}")
    Base.metadata.create_all(dbmod.engine)
    restore(_real_data(REAL_BACKUP), dbmod.engine, force=True)
    outcome.set_range(0, 0)
    db = dbmod.SessionLocal()
    try:
        snap = snapshot(db)
    finally:
        db.close()
    assert snap["trades"]
    if os.environ.get("UPDATE_GOLDEN") == "1":
        REAL_BASELINE.write_text(dumps(snap))
        pytest.skip("real baseline written")
    diff = subset_diff(json.loads(REAL_BASELINE.read_text()), snap)
    assert not diff, "real-data numbers changed:\n" + "\n".join(diff[:25])
