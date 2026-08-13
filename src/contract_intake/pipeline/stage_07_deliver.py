"""Stage 07 -- Deliver.

WHAT     Act on the decision: store the clean record, or open a review item.
IN       Status.DECIDED
OUT      Status.DELIVERED
TOKENS   0
FAILS    write conflict, delivering the same decision twice.
DEPENDS  store/, web/review.py

AUTO_APPROVED  -> a row in ``contracts``: the clean, high-confidence record a
                  downstream system consumes.
NEEDS_REVIEW   -> a row in ``review_items``: the queue, carrying the extracted
                  fields with their quotes, the reasons the rules fired, and the
                  agent's tool trace.
REJECTED       -> recorded with its reason; no downstream artefact.

Idempotent on ``decision_id`` -- replaying cannot produce a duplicate contract
or a second review item.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from sqlalchemy import select

from contract_intake.db.models import Attachment, Contract, Enrichment, Extraction, ReviewItem
from contract_intake.db.models import Decision as DecisionRow
from contract_intake.db.models import Document as DocumentRow
from contract_intake.pipeline.base import Advanced, Failed, Rejected, StageContext, StageOutcome
from contract_intake.status import Route, Status

log = logging.getLogger(__name__)


class DeliverStage:
    number: ClassVar[int] = 7
    name: ClassVar[str] = "deliver"
    consumes: ClassVar[Status] = Status.DECIDED
    produces: ClassVar[Status] = Status.DELIVERED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        attachment = ctx.session.get(Attachment, ctx.attachment_id)
        if attachment is None:
            return Rejected(reason=f"attachment {ctx.attachment_id} disappeared")

        decision = ctx.session.scalar(
            select(DecisionRow)
            .join(Enrichment, DecisionRow.enrichment_id == Enrichment.id)
            .join(Extraction, Enrichment.extraction_id == Extraction.id)
            .join(DocumentRow, Extraction.document_id == DocumentRow.id)
            .where(DocumentRow.attachment_id == attachment.id)
            .order_by(DecisionRow.id.desc())
        )
        if decision is None:
            return Failed(
                error=RuntimeError("no decision; stage 06 must run first"),
                retryable=False,
            )

        enrichment = ctx.session.get(Enrichment, decision.enrichment_id)
        extraction = ctx.session.get(Extraction, enrichment.extraction_id) if enrichment else None
        if enrichment is None or extraction is None:
            return Failed(error=RuntimeError("decision lost its evidence"), retryable=False)

        route = Route(decision.route)

        if route is Route.AUTO_APPROVED:
            created = _store_contract(ctx, decision, extraction, enrichment)
            return Advanced(note=f"stored as contract {created}", metrics={"auto_approved": 1.0})

        if route is Route.REJECTED:
            reasons = "; ".join(r.get("message", "") for r in decision.reasons)
            return Rejected(reason=f"rules rejected this document: {reasons[:300]}")

        created = _open_review(ctx, decision)
        return Advanced(
            note=f"queued for review as item {created}, {len(decision.reasons)} reason(s)",
            metrics={"needs_review": 1.0, "reasons": float(len(decision.reasons))},
        )


def _store_contract(
    ctx: StageContext,
    decision: DecisionRow,
    extraction: Extraction,
    enrichment: Enrichment,
) -> int:
    existing = ctx.session.scalar(select(Contract).where(Contract.decision_id == decision.id))
    if existing is not None:
        return existing.id

    row = Contract(
        decision_id=decision.id,
        counterparty_id=enrichment.counterparty_id,
        counterparty_name=str(_value(extraction.fields, "counterparty_name") or ""),
        payload=_clean_payload(extraction.fields),
    )
    ctx.session.add(row)
    ctx.session.flush()
    return row.id


def _open_review(ctx: StageContext, decision: DecisionRow) -> int:
    existing = ctx.session.scalar(select(ReviewItem).where(ReviewItem.decision_id == decision.id))
    if existing is not None:
        return existing.id

    row = ReviewItem(decision_id=decision.id, state="open")
    ctx.session.add(row)
    ctx.session.flush()
    return row.id


def _value(fields: dict[str, Any], name: str) -> Any:
    entry = fields.get(name)
    return entry.get("value") if isinstance(entry, dict) else None


def _clean_payload(fields: dict[str, Any]) -> dict[str, Any]:
    """Flatten to plain values for downstream consumers.

    The provenance stays in ``extractions``; a system reading ``contracts`` wants
    the terms, and anything that reached this table already passed every rule.
    """
    return {
        name: (entry.get("value") if isinstance(entry, dict) else entry)
        for name, entry in fields.items()
        if not name.startswith("_")
    }
