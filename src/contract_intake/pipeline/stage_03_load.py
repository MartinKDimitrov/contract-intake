"""Stage 03 -- Load.

WHAT     Turn a file into pages, deciding *per page* between cheap text and
         expensive vision.
IN       Status.TRIAGED
OUT      Status.LOADED
TOKENS   0 -- but this stage determines most of stage 04's bill.
FAILS    page that renders but yields no text and no image, pathological page
         count, embedded fonts that defeat text extraction, rasterisation OOM.
DEPENDS  loaders/document.py, which owns rendering and masking in turn

This is the single biggest cost lever in the system, so it gets its own stage
rather than hiding inside extraction:

  * A 20-page contract sent as text is roughly 5k tokens. The same contract
    sent as page images is roughly 20 x 1,850 = 37k tokens at the default
    resolution -- about 7.4x more.
  * The decision is per page. A born-digital contract with one scanned
    signature page sends 19 pages of text and exactly one image.
  * Image pages are downsampled to the smallest size at which the model still
    reads them reliably, which is measured in evals/ rather than assumed.

No OCR engine. Pages without a text layer go to the model as images and Claude
reads them directly. That drops a system-level dependency (tesseract) and, on
noisy scans, reads better than OCR-then-text.

This stage is also where personal data leaves the pipeline. Page text is masked
as it is produced, so neither the model nor the database ever holds a national
identity number or a bank account -- see loaders/redact.py, including what is
deliberately kept and what a scanned page means for the guarantee.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from contract_intake.db.models import Attachment
from contract_intake.db.models import Document as DocumentRow
from contract_intake.loaders.document import load
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.status import Status

log = logging.getLogger(__name__)


class LoadStage:
    # fmt: off
    number   : ClassVar[int]    = 3
    name     : ClassVar[str]    = "load"
    consumes : ClassVar[Status] = Status.TRIAGED
    produces : ClassVar[Status] = Status.LOADED
    uses_llm : ClassVar[bool]   = False
    # fmt: on

    async def run(self, ctx: StageContext) -> StageOutcome:
        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        source = Path(attachment.stored_path)
        if not source.exists():
            return Rejected(reason=f"stored file missing: {source}")

        pages_dir = ctx.settings.data_dir / "pages" / attachment.sha256[:16]

        try:
            document = load(source, settings=ctx.settings, into=pages_dir)
        except ValueError as exc:
            return Rejected(reason=str(exc))
        except Exception as exc:
            return Failed(error=exc, retryable=True, note="rendering failed")

        if document.page_count == 0:
            return Rejected(reason="document produced no pages")

        usable = [p for p in document.pages if p.text or p.image_path]
        if not usable:
            return Rejected(reason="no page yielded either text or an image")

        ctx.session.add(
            DocumentRow(
                attachment_id=attachment.id,
                page_count=document.page_count,
                text_pages=document.text_pages,
                image_pages=document.image_pages,
                pages=document.to_json(),
                redactions=document.redactions if document.redacted else None,
            )
        )
        ctx.session.flush()

        masked = sum(document.redactions.values())  # empty when nothing matched
        return Advanced(
            note=(
                f"{document.page_count} page(s): {document.text_pages} as text, "
                f"{document.image_pages} as image (~{document.estimated_tokens} tokens)"
                + (f"; masked {masked} item(s) of personal data" if masked else "")
            ),
            metrics={
                "pages": float(document.page_count),
                "text_pages": float(document.text_pages),
                "image_pages": float(document.image_pages),
                "estimated_tokens": float(document.estimated_tokens),
                "redacted_items": float(masked),
            },
        )
