"""Alembic environment — resolves the DB URL via anneal_db.engine.db_url (ANNEAL_DB_URL, else the
split DB_* vars, else the compose postgres) so migrations and the app share one resolution."""
from __future__ import annotations

from alembic import context
from anneal_db.engine import db_url as _url
from sqlalchemy import engine_from_config, pool


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = context.config.get_section(context.config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
