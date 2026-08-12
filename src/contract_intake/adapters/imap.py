"""IMAP mailbox client.

Uses the standard library rather than a wrapper: the connection handling here is
small, and the one piece of behaviour that matters most -- the personal-mailbox
guard below -- needs direct control over folder selection anyway.

Messages are only marked ``\\Seen`` once the caller has durably persisted them,
so a crash between fetch and commit re-delivers rather than silently drops.
"""

from __future__ import annotations

import email
import imaplib
import logging
import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from email.message import Message

from contract_intake.adapters.models import (
    InboundAttachment,
    InboundEmail,
    decode_mime_header,
    parse_date,
    parse_sender,
)
from contract_intake.config import Settings

log = logging.getLogger(__name__)

SOCKET_TIMEOUT_SECONDS = 40

#: A folder holding more than this is assumed to be a real mailbox rather than a
#: dedicated intake label. See UnsafeMailboxError.
CROWDED_FOLDER_THRESHOLD = 100


class UnsafeMailboxError(RuntimeError):
    """The configured folder looks like somebody's personal inbox.

    Pointing the poller at INBOX means walking into private correspondence,
    extracting whatever files are attached and feeding them to a model. That is
    a privacy problem and a cost problem, and a misconfigured environment
    variable is all it takes. Intake refuses to start instead.

    The intended setup is a dedicated label fed by a provider-side filter --
    see README.md.
    """


class ImapMailbox:
    """Read messages from one folder, oldest first."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @contextmanager
    def _connect(self, *, readonly: bool) -> Iterator[imaplib.IMAP4_SSL]:
        socket.setdefaulttimeout(SOCKET_TIMEOUT_SECONDS)
        s = self._settings
        client = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
        try:
            client.login(s.imap_user, s.imap_password.get_secret_value())
            self._select(client, readonly=readonly)
            yield client
        finally:
            with suppress(imaplib.IMAP4.error, OSError):  # already gone; nothing to salvage
                client.logout()

    def _select(self, client: imaplib.IMAP4_SSL, *, readonly: bool) -> int:
        folder = self._settings.imap_folder
        typ, data = client.select(f'"{folder}"', readonly=readonly)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot select folder {folder!r}: {data!r}")

        count = int(data[0] or 0)
        self._guard_against_personal_mailbox(folder, count)
        return count

    def _guard_against_personal_mailbox(self, folder: str, count: int) -> None:
        if folder.upper() != "INBOX":
            return
        if count > CROWDED_FOLDER_THRESHOLD:
            raise UnsafeMailboxError(
                f"refusing to poll INBOX: it holds {count} messages, which looks "
                "like a personal mailbox rather than a dedicated intake folder. "
                "Point CI_IMAP_FOLDER at a label fed by a provider-side filter."
            )
        log.warning("polling INBOX directly (%d messages). A dedicated label is safer.", count)

    def fetch_unseen(self, limit: int = 25) -> list[tuple[str, InboundEmail]]:
        """Return unread messages as (uid, parsed) pairs, oldest first."""
        with self._connect(readonly=True) as client:
            typ, ids = client.search(None, "UNSEEN")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"search failed: {ids!r}")

            uids = [uid.decode() for uid in (ids[0] or b"").split()[:limit]]
            out: list[tuple[str, InboundEmail]] = []
            for uid in uids:
                typ, payload = client.fetch(uid, "(RFC822)")
                if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                    log.warning("could not fetch message %r; leaving it unread", uid)
                    continue
                raw = payload[0][1]
                if not isinstance(raw, bytes):
                    continue
                out.append((uid, parse_message(raw, source="imap")))
            return out

    def mark_seen(self, uids: list[str]) -> None:
        """Flag messages as read. Called only after they are safely persisted."""
        if not uids:
            return
        with self._connect(readonly=False) as client:
            for uid in uids:
                client.store(uid, "+FLAGS", "\\Seen")

    def probe(self) -> dict[str, object]:
        """Connectivity check for /healthz and for the CLI."""
        with self._connect(readonly=True) as client:
            typ, ids = client.search(None, "UNSEEN")
            unseen = len((ids[0] or b"").split()) if typ == "OK" else -1
            return {
                "folder": self._settings.imap_folder,
                "unseen": unseen,
                "host": self._settings.imap_host,
            }


def parse_message(raw: bytes, *, source: str) -> InboundEmail:
    """Turn RFC-822 bytes into an InboundEmail, attachments included."""
    message = email.message_from_bytes(raw)
    now = datetime.now(UTC)

    return InboundEmail(
        message_id=(message.get("Message-ID") or "").strip() or f"<generated-{hash(raw)}>",
        sender=parse_sender(message.get("From")),
        subject=decode_mime_header(message.get("Subject")),
        received_at=parse_date(message.get("Date"), fallback=now),
        source=source,
        attachments=list(_iter_attachments(message)),
    )


def _iter_attachments(message: Message) -> Iterator[InboundAttachment]:
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue

        try:
            content = part.get_payload(decode=True)
        except (TypeError, ValueError):
            log.warning("undecodable attachment part; skipping")
            continue
        if not isinstance(content, bytes) or not content:
            continue

        yield InboundAttachment(
            filename=decode_mime_header(filename) or "unnamed",
            content=content,
            declared_mime=part.get_content_type(),
        )
