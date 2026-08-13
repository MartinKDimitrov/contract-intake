"""Stage 04 -- Extract.

WHAT     Pull the commercial terms out of the document as structured data, with
         a confidence and a verbatim source quote attached to every field.
IN       Status.LOADED
OUT      Status.EXTRACTED
TOKENS   LLM. One call, structured output, no tools, effort=high.
FAILS    model refusal, truncation at max_tokens, schema validation failure,
         a quote that does not occur in the document, timeout, 429, budget.
DEPENDS  extract/schema.py, extract/extractor.py, extract/prompts.py

Two decisions shape this stage.

*Provenance is not optional.* Every field carries confidence, source_quote and
page, and each quote is then searched for in the document. A quote that is not
there drives that field's confidence to zero -- the difference between a system
that knows and one that guessed, and the only honest input to stage 06.

*No tools here.* Extraction is a single deterministic call so its accuracy can
be measured on its own in evals/, without agent non-determinism in the way.
Validation against the knowledge base is stage 05's job; fused, neither number
would mean anything.

Cost: the system prompt and JSON schema form a stable cached prefix, so from the
second document onwards that span bills at roughly a tenth of the input rate.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from sqlalchemy import select

from contract_intake.db.models import Attachment, Extraction
from contract_intake.db.models import Document as DocumentRow
from contract_intake.extract.extractor import extract
from contract_intake.llm.client import BudgetExceededError, RefusalError, TruncatedError
from contract_intake.loaders.document import Document
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.status import Status

log = logging.getLogger(__name__)


class ExtractStage:
    number: ClassVar[int] = 4
    name: ClassVar[str] = "extract"
    consumes: ClassVar[Status] = Status.LOADED
    produces: ClassVar[Status] = Status.EXTRACTED
    uses_llm: ClassVar[bool] = True

    async def run(self, ctx: StageContext) -> StageOutcome:
        if ctx.llm is None:
            return Failed(error=RuntimeError("stage 04 needs an LLM client"), retryable=False)

        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        row = ctx.session.scalar(
            select(DocumentRow)
            .where(DocumentRow.attachment_id == attachment.id)
            .order_by(DocumentRow.id.desc())
        )
        if row is None:
            return Failed(
                error=RuntimeError("no loaded document; stage 03 must run first"),
                retryable=False,
            )

        document = Document.from_json(row.pages)

        try:
            outcome = await extract(
                document,
                llm=ctx.llm,
                settings=ctx.settings,
                attachment_id=attachment.id,
            )
        except RefusalError as exc:
            # The same prompt will be refused again; a human decides instead.
            return Rejected(reason=f"model declined to read this document: {exc}")
        except BudgetExceededError as exc:
            return Failed(error=exc, retryable=False, note="per-document budget spent")
        except TruncatedError as exc:
            return Failed(error=exc, retryable=True, note="raise max_tokens and retry")
        except Exception as exc:
            return Failed(error=exc, retryable=True)

        if outcome.extraction.document_kind == "other":
            return Rejected(
                reason=f"model reads this as a non-contract: {outcome.extraction.notes[:200]}"
            )

        ctx.session.add(
            Extraction(
                document_id=row.id,
                fields=outcome.to_json(),
                model=ctx.settings.model,
                effort=ctx.settings.extract_effort,
            )
        )
        ctx.session.flush()

        hallucinated = outcome.hallucinated
        if hallucinated:
            log.warning(
                "attachment %d: %d field(s) quoted text not in the document: %s",
                attachment.id,
                len(hallucinated),
                ", ".join(hallucinated),
            )

        return Advanced(
            note=(
                f"{len(outcome.verified)} field(s) verified, "
                f"{len(hallucinated)} unsupported, ${outcome.usd:.4f}"
            ),
            metrics={
                "verified": float(len(outcome.verified)),
                "unsupported": float(len(hallucinated)),
                "usd": outcome.usd,
            },
        )
