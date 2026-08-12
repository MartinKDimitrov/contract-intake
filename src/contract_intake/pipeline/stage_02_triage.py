"""Stage 02 -- Triage.

WHAT     Decide whether this file is worth spending a model call on.
IN       Status.RECEIVED
OUT      Status.TRIAGED, or Status.REJECTED for anything that is not a contract.
TOKENS   0. Heuristics only.
FAILS    zero-byte file, truncated/corrupt PDF, password-protected PDF,
         declared MIME lying about content, absurd page count, oversized file.
DEPENDS  loaders/detect.py, loaders/pdf.py

Why no model here: the cheapest token is the one never sent. Magic bytes, size,
encryption, page count and a vocabulary scan of page one settle the vast
majority of inputs for free. An invoice mailed to the contracts address costs
nothing to turn away.

Rejection is an expected outcome, not an incident -- no retry, no dead letter.
The reason is recorded so the sender can be told why.

Images are the deliberate soft spot: a photo of a signed page has no text to
scan, so it passes on size alone and stage 04 decides. Rejecting them here would
throw away exactly the messy real-world case worth handling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from contract_intake.db.models import Attachment
from contract_intake.loaders.detect import IMAGE_KINDS, MIME_BY_KIND, FileKind, sniff_path
from contract_intake.loaders.pdf import MAX_REASONABLE_PAGES, PdfProbe, probe
from contract_intake.pipeline.base import Advanced, Rejected, StageContext, StageOutcome
from contract_intake.status import Status

log = logging.getLogger(__name__)

ACCEPTED_KINDS = frozenset({FileKind.PDF}) | IMAGE_KINDS

#: Below this an image is a logo or an email signature, not a document page.
MIN_IMAGE_BYTES = 50_000

CONTRACT_TERMS: tuple[str, ...] = (
    "agreement",
    "contract",
    "party",
    "parties",
    "hereby",
    "whereas",
    "effective date",
    "termination",
    "terminate",
    "liability",
    "indemnif",
    "governing law",
    "confidential",
    "warrant",
    "obligations",
    "clause",
    "in witness whereof",
    "counterparts",
    "supplier",
    "vendor",
    "services",
)

INVOICE_TERMS: tuple[str, ...] = (
    "invoice",
    "invoice no",
    "invoice number",
    "amount due",
    "subtotal",
    "bill to",
    "vat",
    "tax invoice",
    "payment due",
    "remit to",
    "order total",
)

#: Two hits is enough that a lone stray word does not carry a document through.
MIN_CONTRACT_HITS = 2


class TriageStage:
    number: ClassVar[int] = 2
    name: ClassVar[str] = "triage"
    consumes: ClassVar[Status] = Status.RECEIVED
    produces: ClassVar[Status] = Status.TRIAGED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        path = Path(attachment.stored_path)
        if not path.exists():
            return Rejected(reason=f"stored file missing: {path}")

        if attachment.size_bytes == 0:
            return Rejected(reason="empty file")

        ceiling = ctx.settings.max_attachment_mb * 1024 * 1024
        if attachment.size_bytes > ceiling:
            return Rejected(
                reason=f"file is {attachment.size_bytes / 1e6:.1f} MB, "
                f"ceiling is {ctx.settings.max_attachment_mb} MB"
            )

        kind = sniff_path(path)
        attachment.detected_mime = MIME_BY_KIND[kind]
        if kind.value not in {k.value for k in ACCEPTED_KINDS}:
            return Rejected(reason=f"unsupported file type: {kind}")

        if attachment.declared_mime and attachment.declared_mime != attachment.detected_mime:
            log.info(
                "attachment %d declared %s but is %s; trusting the content",
                attachment.id,
                attachment.declared_mime,
                attachment.detected_mime,
            )

        if kind in IMAGE_KINDS:
            return _triage_image(attachment)
        return _triage_pdf(path)


def _triage_image(attachment: Attachment) -> StageOutcome:
    if attachment.size_bytes < MIN_IMAGE_BYTES:
        return Rejected(
            reason=f"image is only {attachment.size_bytes / 1024:.0f} KB; "
            "too small to be a document page"
        )
    return Advanced(
        note="image accepted without content check; stage 04 reads it",
        metrics={"pages": 1.0},
    )


def _triage_pdf(path: Path) -> StageOutcome:
    result = probe(path)

    if result.encrypted:
        return Rejected(reason="PDF is password-protected")
    if not result.readable:
        return Rejected(reason=f"PDF is unreadable: {result.error}")
    if result.page_count == 0:
        return Rejected(reason="PDF has no pages")
    if result.page_count > MAX_REASONABLE_PAGES:
        return Rejected(
            reason=f"{result.page_count} pages exceeds the {MAX_REASONABLE_PAGES}-page "
            "ceiling; likely a bundle rather than one contract"
        )

    verdict = classify_text(result.first_page_text)
    if verdict.kind == "invoice":
        return Rejected(reason=f"looks like an invoice, not a contract ({verdict.evidence})")
    if not result.has_text_layer:
        return Advanced(
            note="no text layer on page 1; stage 04 will read it as an image",
            metrics={"pages": float(result.page_count)},
        )
    if verdict.kind != "contract":
        return Rejected(reason=f"no contract vocabulary on page 1 ({verdict.evidence})")

    return Advanced(
        note=f"contract vocabulary found ({verdict.evidence})",
        metrics={"pages": float(result.page_count)},
    )


class TextVerdict:
    __slots__ = ("evidence", "kind")

    def __init__(self, kind: str, evidence: str) -> None:
        self.kind = kind
        self.evidence = evidence


def classify_text(text: str) -> TextVerdict:
    """Cheap vocabulary check on the first page.

    Deliberately blunt. Its job is to turn away obvious non-contracts for free,
    not to be right about hard cases -- those go on to the model, which is what
    it is for.
    """
    lowered = text.lower()
    contract_hits = [term for term in CONTRACT_TERMS if term in lowered]
    invoice_hits = [term for term in INVOICE_TERMS if term in lowered]

    if len(invoice_hits) >= 2 and len(invoice_hits) > len(contract_hits):
        return TextVerdict("invoice", f"invoice terms: {', '.join(invoice_hits[:3])}")
    if len(contract_hits) >= MIN_CONTRACT_HITS:
        return TextVerdict("contract", f"terms: {', '.join(contract_hits[:3])}")
    return TextVerdict(
        "unknown",
        f"{len(contract_hits)} contract term(s), {len(invoice_hits)} invoice term(s)",
    )


__all__ = ["PdfProbe", "TriageStage", "classify_text"]
