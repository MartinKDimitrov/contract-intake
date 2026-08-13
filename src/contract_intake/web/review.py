"""The review queue.

What a person needs in order to act on a flagged contract, in one screen: the
value, the confidence, the quote it came from and the page it is on; the rules
that fired with the clause each cites; and the agent's tool trace, so it is
visible which lookup drove which finding -- or that none did.

The view assembles; it decides nothing. Approving here records a human's
judgement alongside the machine's, rather than overwriting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from contract_intake.db.models import (
    Attachment,
    Contract,
    Decision,
    Document,
    Email,
    Enrichment,
    Extraction,
    LLMCall,
    ReviewItem,
)
from contract_intake.status import Route

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True, slots=True)
class QueueRow:
    item_id: int
    attachment_id: int
    filename: str
    sender: str
    counterparty: str
    reason_count: int
    top_reason: str
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FieldView:
    name: str
    value: Any
    currency: str | None
    confidence: float
    quote: str
    page: int | None
    provenance: str

    @property
    def verified(self) -> bool:
        return self.provenance == "verified"

    @property
    def suspect(self) -> bool:
        return self.provenance == "not_found"


@dataclass(frozen=True, slots=True)
class ItemView:
    item: ReviewItem
    decision: Decision
    attachment: Attachment
    email: Email | None
    fields: list[FieldView]
    reasons: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    counterparty_id: str | None
    counterparty_score: float | None
    usd: float
    notes: str

    @property
    def route(self) -> Route:
        return Route(self.decision.route)

    @property
    def consulted_knowledge_base(self) -> bool:
        return any(t.get("tool") != "record_finding" for t in self.trace)


def queue(session: Session, *, state: str = "open", limit: int = 100) -> list[QueueRow]:
    stmt = (
        select(ReviewItem, Decision, Attachment, Email, Enrichment)
        .join(Decision, ReviewItem.decision_id == Decision.id)
        .join(Enrichment, Decision.enrichment_id == Enrichment.id)
        .join(Extraction, Enrichment.extraction_id == Extraction.id)
        .join(Document, Extraction.document_id == Document.id)
        .join(Attachment, Document.attachment_id == Attachment.id)
        .join(Email, Attachment.email_id == Email.id)
        .order_by(ReviewItem.created_at.desc())
        .limit(limit)
    )
    if state != "all":
        stmt = stmt.where(ReviewItem.state == state)

    rows: list[QueueRow] = []
    for item, decision, attachment, email, enrichment in session.execute(stmt):
        reasons = sorted(decision.reasons, key=_reason_weight)
        rows.append(
            QueueRow(
                item_id=item.id,
                attachment_id=attachment.id,
                filename=attachment.filename,
                sender=email.sender if email else "",
                counterparty=enrichment.counterparty_id or "unresolved",
                reason_count=len(decision.reasons),
                top_reason=reasons[0]["message"] if reasons else "",
                state=item.state,
                created_at=item.created_at,
            )
        )
    return rows


def load_item(session: Session, item_id: int) -> ItemView | None:
    row = session.execute(
        select(ReviewItem, Decision, Enrichment, Extraction, Document, Attachment)
        .join(Decision, ReviewItem.decision_id == Decision.id)
        .join(Enrichment, Decision.enrichment_id == Enrichment.id)
        .join(Extraction, Enrichment.extraction_id == Extraction.id)
        .join(Document, Extraction.document_id == Document.id)
        .join(Attachment, Document.attachment_id == Attachment.id)
        .where(ReviewItem.id == item_id)
    ).first()
    if row is None:
        return None

    item, decision, enrichment, extraction, _document, attachment = row
    email = session.get(Email, attachment.email_id)
    spent = session.scalar(
        select(func.coalesce(func.sum(LLMCall.usd), 0.0)).where(
            LLMCall.attachment_id == attachment.id
        )
    )

    return ItemView(
        item=item,
        decision=decision,
        attachment=attachment,
        email=email,
        fields=build_fields(extraction.fields),
        reasons=sorted(decision.reasons, key=_reason_weight),
        findings=sorted(
            enrichment.findings,
            key=lambda f: SEVERITY_ORDER.get(str(f.get("severity", "")), 9),
        ),
        trace=enrichment.tool_trace,
        counterparty_id=enrichment.counterparty_id,
        counterparty_score=enrichment.counterparty_score,
        usd=float(spent or 0.0),
        notes=str(extraction.fields.get("notes") or ""),
    )


def build_fields(extraction: dict[str, Any]) -> list[FieldView]:
    """Pair every extracted value with the provenance verdict for it."""
    provenance = {
        p["field"]: p.get("status", "unknown")
        for p in extraction.get("_provenance", [])
        if "field" in p
    }
    out: list[FieldView] = []
    for name, entry in extraction.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        out.append(
            FieldView(
                name=name,
                value=entry.get("value"),
                currency=entry.get("currency"),
                confidence=float(entry.get("confidence") or 0.0),
                quote=str(entry.get("source_quote") or ""),
                page=entry.get("page"),
                provenance=provenance.get(name, "unknown"),
            )
        )
    return out


def resolve_item(
    session: Session,
    item_id: int,
    *,
    action: str,
    corrections: dict[str, Any] | None = None,
    resolved_by: str = "reviewer",
) -> ReviewItem | None:
    """Close a review item. Approving also writes the contract the rules withheld.

    The machine's decision is left intact: what a human concluded is recorded
    beside it, not over it, so a disagreement stays visible afterwards.
    """
    item = session.get(ReviewItem, item_id)
    if item is None or item.state != "open":
        return item

    item.state = "approved" if action == "approve" else "rejected"
    item.human_corrections = corrections or {}
    item.resolved_by = resolved_by
    item.resolved_at = datetime.now(UTC)

    if item.state == "approved":
        _promote(session, item)

    session.flush()
    return item


def _promote(session: Session, item: ReviewItem) -> None:
    if session.scalar(select(Contract).where(Contract.decision_id == item.decision_id)):
        return

    row = session.execute(
        select(Enrichment, Extraction)
        .join(Decision, Decision.enrichment_id == Enrichment.id)
        .join(Extraction, Enrichment.extraction_id == Extraction.id)
        .where(Decision.id == item.decision_id)
    ).first()
    if row is None:
        return

    enrichment, extraction = row
    payload = {
        name: (entry.get("value") if isinstance(entry, dict) else entry)
        for name, entry in extraction.fields.items()
        if not name.startswith("_")
    }
    payload |= item.human_corrections
    payload["_approved_by_human"] = True

    session.add(
        Contract(
            decision_id=item.decision_id,
            counterparty_id=enrichment.counterparty_id,
            counterparty_name=str(payload.get("counterparty_name") or ""),
            payload=payload,
        )
    )


def _reason_weight(reason: dict[str, Any]) -> tuple[int, str]:
    rank = {
        "not_a_contract": 0,
        "suspended_counterparty": 1,
        "high_severity_finding": 2,
        "unsupported_quote": 3,
        "unresolved_counterparty": 4,
        "medium_severity_finding": 5,
        "wholly_unverifiable": 6,
        "low_confidence_required_field": 7,
    }
    return rank.get(str(reason.get("rule")), 9), str(reason.get("message", ""))
