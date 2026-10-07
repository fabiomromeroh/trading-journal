from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.db import Base, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("url") or get_settings().database_url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    connectable = make_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
