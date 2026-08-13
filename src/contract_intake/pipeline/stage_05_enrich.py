"""Stage 05 -- Enrich.

WHAT     Resolve the counterparty, check the extracted terms against the
         playbook, and call the agent only for what a check cannot express.
IN       Status.EXTRACTED
OUT      Status.ENRICHED
TOKENS   0 for a document that already fails a deterministic check. Otherwise a
         bounded agent loop, capped by max_iterations and the per-document USD
         ceiling.
FAILS    agent loops without converging, KB index missing, model refusal,
         budget exhausted mid-loop.
DEPENDS  policy/thresholds.py, knowledge/vendors.py, agent/

The stage runs in three passes, cheapest first.

**Counterparty resolution** is a pure function over a closed registry. It was a
model tool once; it never needed to be.

**Threshold checks** are comparisons. "Is 90 within 45 to 90?" is arithmetic,
and an earlier version had a frontier model retrieve the clause, read the
numbers out of prose and do that arithmetic -- non-deterministically, at roughly
seven cents a document, in a system whose stated principle is that the model
proposes and the code decides. That principle was only half true until these
checks existed.

**The agent** runs last, and only when the first two passes found nothing. If a
document already fails three checks it is going to a human whatever the model
says, so paying for an opinion is paying for nothing. Spending is reserved for
the documents where the answer is not already known -- which is also where the
agent's actual strength lies: judgement a comparison cannot carry, such as a
90-day non-renewal window not being a termination-for-convenience right, or a
contract being silent about something the registry implies it should cover.

Findings from both halves share one shape and both carry a citation, so stage 06
does not care which produced them.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from sqlalchemy import select

from contract_intake.agent.runner import review
from contract_intake.db.models import Attachment, Enrichment, Extraction
from contract_intake.db.models import Document as DocumentRow
from contract_intake.knowledge.vendors import Match, resolve
from contract_intake.llm.client import BudgetExceededError, RefusalError
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.policy.thresholds import evaluate
from contract_intake.status import Status

log = logging.getLogger(__name__)


class EnrichStage:
    number: ClassVar[int] = 5
    name: ClassVar[str] = "enrich"
    consumes: ClassVar[Status] = Status.EXTRACTED
    produces: ClassVar[Status] = Status.ENRICHED
    uses_llm: ClassVar[bool] = True

    async def run(self, ctx: StageContext) -> StageOutcome:
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
                error=RuntimeError("no extraction; stage 04 must run first"), retryable=False
            )

        fields = extraction.fields

        # Pass 1 -- resolution. Pure function, no tokens.
        match = _resolve_counterparty(fields, ctx.settings.min_vendor_match)
        category = match.vendor.category if match.vendor else None

        # Pass 2 -- comparisons. Pure functions, no tokens.
        findings: list[dict[str, Any]] = evaluate(fields, vendor_category=category)
        trace: list[dict[str, Any]] = []
        usd = 0.0
        consulted_agent = False

        # Pass 3 -- judgement, only where the answer is not already settled.
        if findings:
            log.info(
                "attachment %d: %d deterministic deviation(s); the agent adds nothing to a "
                "document already bound for review",
                attachment.id,
                len(findings),
            )
        else:
            if ctx.llm is None:
                return Failed(error=RuntimeError("stage 05 needs an LLM client"), retryable=False)
            try:
                outcome = await review(
                    fields, llm=ctx.llm, settings=ctx.settings, attachment_id=attachment.id
                )
            except RefusalError as exc:
                return Rejected(reason=f"model declined to review this contract: {exc}")
            except BudgetExceededError as exc:
                return Failed(error=exc, retryable=False, note="per-document budget spent")
            except Exception as exc:
                return Failed(error=exc, retryable=True)

            consulted_agent = True
            findings.extend(f.to_json() | {"source": "agent"} for f in outcome.findings)
            trace = outcome.trace
            usd = outcome.usd

        ctx.session.add(
            Enrichment(
                extraction_id=extraction.id,
                findings=findings,
                tool_trace=trace,
                counterparty_id=match.vendor.id if match.vendor else None,
                counterparty_score=match.score,
            )
        )
        ctx.session.flush()

        from_rules = sum(1 for f in findings if f.get("source") == "rules")
        return Advanced(
            note=(
                f"{len(findings)} finding(s): {from_rules} from checks, "
                f"{len(findings) - from_rules} from the agent"
                f"{'' if consulted_agent else ' (agent not needed)'}, ${usd:.4f}"
            ),
            metrics={
                "findings": float(len(findings)),
                "from_rules": float(from_rules),
                "agent_called": float(consulted_agent),
                "usd": usd,
            },
        )


def _resolve_counterparty(fields: dict[str, Any], threshold: float) -> Match:
    def value(name: str) -> str | None:
        entry = fields.get(name)
        raw = entry.get("value") if isinstance(entry, dict) else None
        return str(raw) if raw is not None else None

    return resolve(
        value("counterparty_name"),
        registration_id=value("counterparty_registration_id"),
        threshold=threshold,
    )
