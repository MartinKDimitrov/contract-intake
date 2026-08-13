"""The shape intake produces, independent of where the mail came from.

Both adapters -- IMAP polling and the provider webhook -- normalise to these two
types, so stage 01 has exactly one code path and the rest of the pipeline never
learns how a document arrived.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    # fmt: off
    filename      : str
    content       : bytes
    declared_mime : str
    # fmt: on

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class InboundEmail:
    # fmt: off
    message_id  : str
    sender      : str
    subject     : str
    received_at : datetime
    source      : str
    attachments : list[InboundAttachment] = field(default_factory=list)
    # fmt: on


def decode_mime_header(raw: str | None) -> str:
    """Decode an RFC 2047 header into plain text.

    Real mail does not arrive in ASCII. A Bulgarian sender's name reaches us as
    ``=?UTF-8?B?0JzQsNGA0YLQuNC9?=``, and a counterparty name mangled here would
    poison vendor matching three stages later. Broken encodings degrade to the
    raw string rather than raising -- a malformed subject is not a reason to
    drop a contract.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw.strip()


def parse_sender(raw: str | None) -> str:
    """Extract a bare address from a ``Name <addr>`` header."""
    if not raw:
        return ""
    _, address = parseaddr(decode_mime_header(raw))
    return address.strip().lower()


def parse_date(raw: str | None, *, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed is not None else fallback
