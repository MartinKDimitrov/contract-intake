"""Stage 06 -- Decide.

WHAT     Turn extraction confidence plus agent findings into a route.
IN       Status.ENRICHED
OUT      Status.DECIDED
TOKENS   0. No model call.
FAILS    almost nothing at runtime -- pure functions over persisted data. The
         failure mode here is a wrong *rule*, which is what the truth table in
         tests/policy/test_rules.py exists to catch.
DEPENDS  policy/rules.py

The model extracts and the agent reports; this decides. An LLM asked "should
this be auto-approved?" produces an answer that cannot be unit-tested, cannot be
shown to a lawyer, and drifts between model versions. A rule reading
``severity == high -> needs_review, citing §4.1`` can be tested exhaustively and
explained in one sentence.

The default is caution: auto-approval requires every rule to stay silent.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from sqlalchemy import select

from contract_intake.db.models import Attachment, Enrichment, Extraction
from contract_intake.db.models import Decision as DecisionRow
from contract_intake.db.models import Document as DocumentRow
from contract_intake.knowledge.vendors import load_registry
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.policy.rules import RULES_VERSION, Evidence, decide
from contract_intake.status import Route, Status

log = logging.getLogger(__name__)


class DecideStage:
    # fmt: off
    number   : ClassVar[int]    = 6
    name     : ClassVar[str]    = "decide"
    consumes : ClassVar[Status] = Status.ENRICHED
    produces : ClassVar[Status] = Status.DECIDED
    uses_llm : ClassVar[bool]   = False
    # fmt: on

    async def run(self, ctx: StageContext) -> StageOutcome:
        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        enrichment = ctx.session.scalar(
            select(Enrichment)
            .join(Extraction, Enrichment.extraction_id == Extraction.id)
            .join(DocumentRow, Extraction.document_id == DocumentRow.id)
            .where(DocumentRow.attachment_id == attachment.id)
            .order_by(Enrichment.id.desc())
        )
        if enrichment is None:
            return Failed(
                error=RuntimeError("no enrichment; stage 05 must run first"),
                retryable=False,
            )

        extraction = ctx.session.get(Extraction, enrichment.extraction_id)
        if extraction is None:
            return Failed(error=RuntimeError("enrichment lost its extraction"), retryable=False)

        decision = decide(
            Evidence(
                extraction=extraction.fields,
                findings=enrichment.findings,
                counterparty_id=enrichment.counterparty_id,
                counterparty_score=enrichment.counterparty_score,
                counterparty_status=_registry_status(enrichment.counterparty_id),
            ),
            ctx.settings,
        )

        ctx.session.add(
            DecisionRow(
                enrichment_id=enrichment.id,
                route=decision.route,
                reasons=[r.to_json() for r in decision.reasons],
                blocking_fields=list(decision.blocking_fields),
                rules_version=RULES_VERSION,
            )
        )
        ctx.session.flush()

        headline = decision.reasons[0].rule if decision.reasons else "no rule fired"
        return Advanced(
            note=f"{decision.route}: {len(decision.reasons)} reason(s), first is {headline}",
            metrics={
                "reasons": float(len(decision.reasons)),
                "blocking_fields": float(len(decision.blocking_fields)),
                "auto_approved": float(decision.route is Route.AUTO_APPROVED),
            },
        )


def _registry_status(vendor_id: str | None) -> str:
    """Look the resolved vendor's status back up.

    Deliberately re-read from the registry rather than trusted from the agent's
    report: suspension is a fact about the supplier today, not about what the
    agent happened to see when it ran.
    """
    if not vendor_id:
        return "unknown"
    for vendor in load_registry():
        if vendor.id == vendor_id:
            return vendor.status
    return "unknown"
