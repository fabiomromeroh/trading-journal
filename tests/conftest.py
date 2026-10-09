import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APP_PASSWORD"] = "test-pass"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["PRICE_PROVIDER"] = "none"
os.environ["QUOTE_PROVIDER"] = "none"
for k in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL", "SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY"):
    os.environ.pop(k, None)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db(tmp_path):
    from app import db as dbmod
    from app.db import Base
    from app import models  # noqa: F401
    url = os.environ.get("TEST_DATABASE_URL")  # e.g. a throwaway local Postgres, to test FK cascades
    dbmod.configure(url or f"sqlite:///{tmp_path / 'test.db'}")
    if url:
        Base.metadata.drop_all(dbmod.engine)
    Base.metadata.create_all(dbmod.engine)
    s = dbmod.SessionLocal()
    from app import outcome
    outcome.set_range(0, 0)
    yield s
    s.close()


@pytest.fixture()
def fixture_text():
    return lambda name: (FIX / name).read_text()
