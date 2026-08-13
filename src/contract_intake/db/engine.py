"""Engine and session plumbing.

SQLite in WAL mode: the pipeline worker writes while the review UI reads, with
no extra infrastructure between `git clone` and a working demo. The only
SQLite-specific code lives in this file -- everything else goes through
SQLAlchemy, so moving to Postgres is a DATABASE_URL change plus a concurrency
strategy (see docs/TRADEOFFS.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from contract_intake.config import Settings, get_settings
from contract_intake.db.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def build_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


class StaleSchemaError(RuntimeError):
    """An existing database predates the current models."""


def assert_schema_current(engine: Engine) -> None:
    """Fail at startup if a table on disk is missing a column the models declare.

    ``create_all`` creates missing *tables* and silently ignores missing
    *columns*, so adding a field to a model leaves an existing database one
    column short and the mismatch surfaces much later as an opaque "no such
    column" in the middle of a pipeline run.

    There is no Alembic here on purpose (docs/TRADEOFFS.md), which makes this
    check the thing that keeps that decision honest: without migrations the
    remedy is to recreate the database, and the least a developer is owed is to
    be told that at startup rather than mid-document.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    stale: list[str] = []

    for name, table in Base.metadata.tables.items():
        if name not in existing:
            continue  # create_all handles a table that is not there at all
        on_disk = {column["name"] for column in inspector.get_columns(name)}
        missing = sorted(c.name for c in table.columns if c.name not in on_disk)
        stale.extend(f"{name}.{column}" for column in missing)

    if stale:
        raise StaleSchemaError(
            "database predates the current models -- missing "
            + ", ".join(stale)
            + ". There are no migrations by design: recreate the database "
            "(delete it, or point CI_DATABASE_URL elsewhere) and re-poll."
        )


def init_db(settings: Settings | None = None) -> Engine:
    """Create the engine, the data directories and any missing tables.

    Idempotent: safe to call on every process start. No Alembic on purpose --
    migrations are a production concern, tracked in docs/HAND_OVER.md.
    """
    global _engine, _session_factory

    settings = settings or get_settings()
    settings.ensure_dirs()

    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = build_engine(settings.database_url)
    Base.metadata.create_all(_engine)
    assert_schema_current(_engine)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return init_db()
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Commits on success, rolls back on any exception."""
    if _session_factory is None:
        init_db()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
