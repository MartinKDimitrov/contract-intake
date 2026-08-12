from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from contract_intake.config import Settings
from contract_intake.db.engine import build_engine
from contract_intake.db.models import Attachment, Base, Email
from contract_intake.status import Status


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "var",
        model="claude-opus-5",
        max_usd_per_document=0.50,
    )


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
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
    session.flush()
    return att
