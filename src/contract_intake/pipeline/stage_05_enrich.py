"""Stage 05 -- Enrich.

WHAT     Resolve the counterparty against the vendor registry and check the
         extracted terms against the internal contracting playbook.
IN       Status.EXTRACTED
OUT      Status.ENRICHED
TOKENS   LLM agent loop, effort=medium, bounded by max_iterations and the
         per-document USD ceiling. The only stage whose token use is not fixed
         before it starts.
FAILS    agent loops without converging, tool returns nothing, KB index missing,
         model refusal, budget exhausted mid-loop.
DEPENDS  agent/tools.py, agent/runner.py, knowledge/vendors.py, knowledge/policy.py

This is where the knowledge base has to earn its place, and it does two jobs that
need two different retrieval methods:

  1. *Entity resolution.* "NordWind Logistics Ltd." on a crooked scan against a
     registry holding "Nordwind Logistik GmbH". Names fail lexically, so this is
     trigram matching -- a dense retriever would rank "Nordwind Marine Services
     AS" alongside, because both are Nordic shipping firms.
  2. *Policy validation.* "payment terms: 90 days" against a playbook whose
     ceiling is 45. This is genuinely semantic, so it is dense retrieval, and a
     hit carries its section number so the finding can cite it.

The second is the job the model cannot do alone: no amount of reasoning tells it
what *this company's* liability ceiling is. That is the honest test of whether
retrieval improves the result or decorates it, and evals/ answers it by running
this stage with and without knowledge-base access.

The agent proposes findings; it does not decide. Routing is stage 06.

The full tool trace is persisted and rendered in the review UI, so a human can
see which clause drove which finding -- or that no clause was consulted at all.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from sqlalchemy import select

from contract_intake.agent.runner import review
from contract_intake.db.models import Attachment, Enrichment, Extraction
from contract_intake.db.models import Document as DocumentRow
from contract_intake.llm.client import BudgetExceededError, RefusalError
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.status import Status

log = logging.getLogger(__name__)


class EnrichStage:
    number: ClassVar[int] = 5
    name: ClassVar[str] = "enrich"
    consumes: ClassVar[Status] = Status.EXTRACTED
    produces: ClassVar[Status] = Status.ENRICHED
    uses_llm: ClassVar[bool] = True

    async def run(self, ctx: StageContext) -> StageOutcome:
        if ctx.llm is None:
            return Failed(error=RuntimeError("stage 05 needs an LLM client"), retryable=False)

        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        extraction = ctx.session.scalar(
            select(Extraction)
            .join(DocumentRow, Extraction.document_id == DocumentRow.id)
            .where(DocumentRow.attachment_id == attachment.id)
            .order_by(Extraction.id.desc())
        )
        if extraction is None:
            return Failed(
                error=RuntimeError("no extraction; stage 04 must run first"),
                retryable=False,
            )

        try:
            outcome = await review(
                extraction.fields,
                llm=ctx.llm,
                settings=ctx.settings,
                attachment_id=attachment.id,
            )
        except RefusalError as exc:
            return Rejected(reason=f"model declined to review this contract: {exc}")
        except BudgetExceededError as exc:
            return Failed(error=exc, retryable=False, note="per-document budget spent")
        except Exception as exc:
            return Failed(error=exc, retryable=True)

        if not outcome.used_knowledge_base:
            # Not fatal, but it means the findings rest on the model's priors
            # rather than on this company's policy -- exactly what stage 06 must
            # not treat as authoritative.
            log.warning(
                "attachment %d: agent recorded %d finding(s) without consulting the knowledge base",
                attachment.id,
                len(outcome.findings),
            )

        ctx.session.add(
            Enrichment(
                extraction_id=extraction.id,
                findings=[f.to_json() for f in outcome.findings],
                tool_trace=outcome.trace,
                counterparty_id=outcome.counterparty_id,
                counterparty_score=outcome.counterparty_score,
            )
        )
        ctx.session.flush()

        by_kind = {f.kind for f in outcome.findings}
        return Advanced(
            note=(
                f"{len(outcome.findings)} finding(s) {sorted(by_kind)}, "
                f"{len(outcome.trace)} tool call(s), ${outcome.usd:.4f}"
            ),
            metrics={
                "findings": float(len(outcome.findings)),
                "tool_calls": float(len(outcome.trace)),
                "iterations": float(outcome.iterations),
                "usd": outcome.usd,
            },
        )
