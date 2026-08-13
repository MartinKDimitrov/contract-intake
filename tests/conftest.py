from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from contract_intake.config import Settings
from contract_intake.db.engine import get_engine, init_db
from contract_intake.db.models import Attachment, Email
from contract_intake.status import Status


@pytest.fixture
def settings(tmp_path) -> Settings:
    # A file rather than ":memory:", because an in-memory SQLite database lives
    # per connection: anything the code opens a *second* session on -- the cost
    # ledger, a dead letter written after a rollback -- would silently address a
    # different, empty database and the test would prove nothing.
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "var",
        model="claude-opus-5",
        max_usd_per_document=0.50,
    )


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    """The test database, wired through init_db so tests take the real path.

    Building an engine directly here used to bypass `init_db`, and with it the
    schema guard -- which is exactly why a missing-column bug survived a green
    suite. Going through init_db means the tests exercise the startup checks
    too, and `session_scope()` inside the code under test addresses the same
    database the fixture does.
    """
    init_db(settings)
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    with factory() as s:
        yield s


@pytest.fixture
def attachment(session: Session) -> Attachment:
    """A minimal document sitting at the front of the pipeline."""
    from datetime import UTC, datetime

    email = Email(
        message_id="<test-1@example.com>",
        sender="vendor@example.com",
        subject="Signed MSA",
        received_at=datetime.now(UTC),
    )
    session.add(email)
    session.flush()

    att = Attachment(
        email_id=email.id,
        filename="msa.pdf",
        sha256="0" * 64,
        declared_mime="application/pdf",
        detected_mime="application/pdf",
        size_bytes=1234,
        stored_path="/tmp/msa.pdf",
        status=Status.RECEIVED,
    )
    session.add(att)
    # Committed, not merely flushed: the cost ledger writes on its own session
    # and a foreign key cannot see an uncommitted row. In the pipeline this is
    # already true -- `drain` commits after every stage -- so a fixture that
    # only flushes tests a state production never reaches.
    session.commit()
    return att
