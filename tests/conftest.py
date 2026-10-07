import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APP_PASSWORD"] = "test-pass"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["PRICE_PROVIDER"] = "none"
for k in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL"):
    os.environ.pop(k, None)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db(tmp_path):
    from app import db as dbmod
    from app.db import Base
    from app import models  # noqa: F401
    dbmod.configure(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(dbmod.engine)
    s = dbmod.SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def fixture_text():
    return lambda name: (FIX / name).read_text()
