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


async def test_one_bad_attachment_does_not_commit_half_an_email(session, settings, monkeypatch):
    """A failure part way through must leave nothing, or redelivery is a no-op.

    The batch used to be committed once at the end, so a failure on the second
    of three attachments still committed the `emails` row and the attachments
    written so far. The next poll then found the Message-ID already present,
    returned nothing, and marked the message read -- so the rest of that email
    was gone, with no row, no dead letter and nothing to grep for.
    """
    from sqlalchemy import func

    from contract_intake.pipeline import stage_01_receive as intake

    calls = {"n": 0}
    real = intake._persist_attachment

    def explode_on_the_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(intake, "_persist_attachment", explode_on_the_second)

    class OneMessage:
        """Records what it was told, so "not marked" is distinguishable from "not called"."""

        def __init__(self) -> None:
            self.seen: list[str] = []
            self.mark_seen_calls = 0

        def fetch_unseen(self, limit: int = 25):
            return [("101", make_email(files=(("a.pdf", b"%PDF-1.7 a"), ("b.pdf", b"%PDF-1.7 b"))))]

        def mark_seen(self, uids):
            self.mark_seen_calls += 1
            self.seen.extend(uids)

    mailbox = OneMessage()
    created = await intake.ImapSource(mailbox=mailbox).poll(session, settings)

    session.rollback()
    assert created == []
    assert session.scalar(func.count(Email.id)) == 0, "no half-written email may survive"
    assert session.scalar(func.count(Attachment.id)) == 0
    assert mailbox.seen == [], "the message must stay unread so it is delivered again"
    assert mailbox.mark_seen_calls == 1, "and the absence must be a call with nothing in it"
