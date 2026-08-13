"""Without migrations, schema drift has to be caught at startup or not at all.

``create_all`` adds missing tables and ignores missing columns, so a model that
gains a field leaves an existing database one column short. The failure then
surfaces mid-pipeline as an opaque "no such column", which is the worst place
for it. These tests pin the guard that turns it into a startup error.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from contract_intake.db.engine import StaleSchemaError, assert_schema_current, build_engine
from contract_intake.db.models import Base


@pytest.fixture
def engine(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_a_fresh_database_is_current(engine) -> None:
    assert_schema_current(engine)


def test_a_missing_column_is_a_startup_error(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE documents DROP COLUMN redactions"))

    with pytest.raises(StaleSchemaError) as raised:
        assert_schema_current(engine)

    message = str(raised.value)
    assert "documents.redactions" in message
    assert "recreate the database" in message, "the error must carry its own remedy"


def test_a_missing_table_is_left_to_create_all(engine) -> None:
    """A table that does not exist yet is not drift -- create_all will add it."""
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE review_items"))

    assert_schema_current(engine)
