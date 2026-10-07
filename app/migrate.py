"""Run Alembic migrations programmatically (`python -m app.migrate`)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def upgrade(revision: str = "head") -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, revision)


if __name__ == "__main__":
    upgrade()
