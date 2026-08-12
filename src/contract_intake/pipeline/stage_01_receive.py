"""Stage 01 -- Receive.

WHAT     Pull new mail, persist each attachment to disk, create one row per file.
IN       Nothing (this is a Source, not a Stage -- see base.py).
OUT      Status.RECEIVED
TOKENS   0
FAILS    IMAP auth failure, connection drop mid-fetch, malformed MIME,
         duplicate delivery of the same Message-ID, attachment write failure.
DEPENDS  adapters/imap.py, adapters/webhook.py, adapters/models.py

Deduplication happens twice on purpose: on ``Message-ID`` (the same mail
delivered twice) and on the attachment ``sha256`` (the same PDF arriving under
two different mails). The second one is also a cost lever -- an already-seen
document never reaches a model again.

Implemented in phase 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from sqlalchemy.orm import Session

from contract_intake.config import Settings
from contract_intake.status import Status


class ImapSource:
    """Polls an IMAP folder. The demo path."""

    name: ClassVar[str] = "receive.imap"
    produces: ClassVar[Status] = Status.RECEIVED

    async def poll(self, session: Session, settings: Settings) -> Sequence[int]:
        raise NotImplementedError("phase 1")


class WebhookSource:
    """Accepts provider-pushed mail (Mailgun-compatible multipart).

    Same output shape as ImapSource. Present so the intake path is not welded to
    polling; not on the demo path, since it needs a public URL.
    """

    name: ClassVar[str] = "receive.webhook"
    produces: ClassVar[Status] = Status.RECEIVED

    async def poll(self, session: Session, settings: Settings) -> Sequence[int]:
        raise NotImplementedError("phase 1")
