from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from contract_intake.adapters.models import InboundAttachment, InboundEmail
from contract_intake.db.models import Attachment, Email
from contract_intake.pipeline.stage_01_receive import ingest_email
from contract_intake.status import Status


def make_email(
    *,
    message_id: str = "<one@example.com>",
    files: tuple[tuple[str, bytes], ...] = (("msa.pdf", b"%PDF-1.7 contract"),),
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        sender="ap@nordwind.example",
        subject="Signed MSA",
        received_at=datetime.now(UTC),
        source="imap",
        attachments=[
            InboundAttachment(filename=n, content=c, declared_mime="application/pdf")
            for n, c in files
        ],
    )


def test_ingest_persists_email_attachment_and_file(session, settings) -> None:
    created = ingest_email(make_email(), session=session, settings=settings)
    assert len(created) == 1

    row = session.get(Attachment, created[0])
    assert row.status == Status.RECEIVED
    assert row.filename == "msa.pdf"
    assert row.size_bytes == len(b"%PDF-1.7 contract")

    from pathlib import Path

    stored = Path(row.stored_path)
    assert stored.exists(), "the bytes must survive the process that received them"
    assert stored.read_bytes() == b"%PDF-1.7 contract"
    assert row.sha256 in stored.name, "content-addressed on disk"


def test_same_message_delivered_twice_is_ingested_once(session, settings) -> None:
    assert ingest_email(make_email(), session=session, settings=settings)
    assert ingest_email(make_email(), session=session, settings=settings) == []
    assert len(session.scalars(select(Email)).all()) == 1


def test_same_file_under_a_different_message_is_not_reprocessed(session, settings) -> None:
    """Content-level dedupe: a forwarded contract must not be paid for twice."""
    ingest_email(make_email(message_id="<a@x>"), session=session, settings=settings)
    again = ingest_email(
        make_email(message_id="<b@x>", files=(("forwarded-copy.pdf", b"%PDF-1.7 contract"),)),
        session=session,
        settings=settings,
    )

    assert again == [], "identical bytes, so no new work"
    assert len(session.scalars(select(Attachment)).all()) == 1
    assert len(session.scalars(select(Email)).all()) == 2, "both emails are still recorded"


def test_email_without_attachments_creates_nothing(session, settings) -> None:
    assert ingest_email(make_email(files=()), session=session, settings=settings) == []
    assert session.scalars(select(Email)).all() == []


def test_each_attachment_of_one_email_gets_its_own_row(session, settings) -> None:
    created = ingest_email(
        make_email(files=(("msa.pdf", b"%PDF-a"), ("annex.pdf", b"%PDF-b"))),
        session=session,
        settings=settings,
    )
    assert len(created) == 2
    assert {session.get(Attachment, i).filename for i in created} == {"msa.pdf", "annex.pdf"}


def test_odd_filenames_do_not_escape_the_storage_directory(session, settings) -> None:
    created = ingest_email(
        make_email(files=(("../../etc/passwd", b"%PDF-x"),)),
        session=session,
        settings=settings,
    )
    from pathlib import Path

    stored = Path(session.get(Attachment, created[0]).stored_path).resolve()
    assert stored.is_relative_to(settings.attachments_dir.resolve())
