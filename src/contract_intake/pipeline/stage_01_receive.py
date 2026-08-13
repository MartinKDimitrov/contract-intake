"""Stage 01 -- Receive.

WHAT     Pull new mail, persist each attachment to disk, create one row per file.
IN       Nothing (this is a Source, not a Stage -- see base.py).
OUT      Status.RECEIVED
TOKENS   0
FAILS    IMAP auth failure, connection drop mid-fetch, malformed MIME,
         duplicate delivery of the same Message-ID, attachment write failure.
DEPENDS  adapters/imap.py, adapters/models.py

Deduplication happens twice on purpose: on ``Message-ID`` (the same mail
delivered twice) and on the attachment ``sha256`` (the same PDF arriving under
two different mails, or forwarded). The second is also a cost lever -- an
already-seen document never reaches a model again.

Messages are marked read only after their attachments are committed, so a crash
between fetch and commit re-delivers rather than silently drops.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from contract_intake.adapters.imap import ImapMailbox
from contract_intake.adapters.models import InboundAttachment, InboundEmail
from contract_intake.config import Settings
from contract_intake.db.models import Attachment, Email
from contract_intake.status import Status

log = logging.getLogger(__name__)


class ImapSource:
    """Polls an IMAP folder."""

    name: ClassVar[str] = "receive.imap"
    produces: ClassVar[Status] = Status.RECEIVED

    def __init__(self, mailbox: ImapMailbox | None = None) -> None:
        self._mailbox = mailbox

    async def poll(self, session: Session, settings: Settings) -> Sequence[int]:
        mailbox = self._mailbox or ImapMailbox(settings)
        fetched = mailbox.fetch_unseen()
        if not fetched:
            return []

        created: list[int] = []
        handled: list[str] = []
        for uid, inbound in fetched:
            # One transaction per message. Committing the batch at the end meant
            # a failure on the second of three attachments still committed the
            # `emails` row and the attachments written so far -- and the redelivery
            # this promises was then swallowed by the Message-ID dedup, which saw
            # the message as already handled. The rest of that email was lost with
            # no row, no dead letter and nothing to grep for.
            try:
                created.extend(ingest_email(inbound, session=session, settings=settings))
                session.commit()
            except Exception:
                session.rollback()
                # Left unread, and now genuinely re-ingestable: nothing about it
                # was committed, so the dedup will not turn the retry into a
                # silent no-op.
                log.exception("failed to ingest %s; leaving unread", inbound.message_id)
                continue
            handled.append(uid)

        mailbox.mark_seen(handled)
        return created


class WebhookSource:
    """Accepts provider-pushed mail (Mailgun-compatible multipart).

    Same output shape as ImapSource, so intake is not welded to polling. Needs a
    publicly reachable URL, which the IMAP path does not.
    """

    name: ClassVar[str] = "receive.webhook"
    produces: ClassVar[Status] = Status.RECEIVED

    def __init__(self) -> None:
        self._pending: list[InboundEmail] = []

    def offer(self, inbound: InboundEmail) -> None:
        self._pending.append(inbound)

    async def poll(self, session: Session, settings: Settings) -> Sequence[int]:
        created: list[int] = []
        while self._pending:
            # Read, ingest, commit, *then* drop. Popping first meant a raise
            # discarded the payload with no retry and no trace -- and a webhook
            # provider will not deliver it twice.
            inbound = self._pending[0]
            try:
                created.extend(ingest_email(inbound, session=session, settings=settings))
                session.commit()
            except Exception:
                session.rollback()
                log.exception("failed to ingest %s; leaving queued", inbound.message_id)
                raise
            self._pending.pop(0)
        return created


def ingest_email(
    inbound: InboundEmail,
    *,
    session: Session,
    settings: Settings,
) -> list[int]:
    """Persist one message and its attachments. Returns new attachment ids."""
    if session.scalar(select(Email).where(Email.message_id == inbound.message_id)):
        log.info("already seen message %s; skipping", inbound.message_id)
        return []

    if not inbound.attachments:
        log.info("message %s carries no attachments; nothing to do", inbound.message_id)
        return []

    email_row = Email(
        message_id=inbound.message_id,
        sender=inbound.sender,
        subject=inbound.subject,
        received_at=inbound.received_at,
        source=inbound.source,
    )
    session.add(email_row)
    session.flush()

    created: list[int] = []
    for attachment in inbound.attachments:
        row = _persist_attachment(
            attachment, email_id=email_row.id, session=session, settings=settings
        )
        if row is not None:
            created.append(row)
    return created


def _persist_attachment(
    attachment: InboundAttachment,
    *,
    email_id: int,
    session: Session,
    settings: Settings,
) -> int | None:
    digest = attachment.sha256

    # Same shape as every other phase's line, so a reader can follow one
    # document down the log from arrival to filing without changing how they
    # read. Intake is a Source rather than a Stage, so it announces itself.
    seen = session.scalar(select(Attachment).where(Attachment.sha256 == digest))
    if seen is not None:
        log.info("\n%s", attachment.filename)
        log.info("  %-11s x  identical to attachment %d; not reprocessing", "01 receive", seen.id)
        return None

    settings.ensure_dirs()
    destination = settings.attachments_dir / f"{digest}{_suffix(attachment.filename)}"
    if not destination.exists():
        destination.write_bytes(attachment.content)

    row = Attachment(
        email_id=email_id,
        filename=attachment.filename,
        sha256=digest,
        declared_mime=attachment.declared_mime,
        size_bytes=attachment.size_bytes,
        stored_path=str(destination),
        status=Status.RECEIVED,
    )
    session.add(row)
    session.flush()
    log.info("\n%s", attachment.filename)
    log.info(
        "  %-11s -> %-62s",
        "01 receive",
        f"stored as attachment {row.id}, {attachment.size_bytes / 1024:.1f} KB, "
        f"sha256 {digest[:12]}",
    )
    return row.id


def _suffix(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return f".{ext.lower()}" if dot and len(ext) <= 8 and ext.isalnum() else ""
