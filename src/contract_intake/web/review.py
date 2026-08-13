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
    Decision,
    Document,
    Email,
    Enrichment,
    Extraction,
    LLMCall,
    ReviewItem,
)
from contract_intake.status import Route
from contract_intake.store.contracts import record as store_contract

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True, slots=True)
class QueueRow:
    # fmt: off
    item_id       : int
    attachment_id : int
    filename      : str
    sender        : str
    counterparty  : str
    reason_count  : int
    top_reason    : str
    state         : str
    created_at    : datetime
    # fmt: on


@dataclass(frozen=True, slots=True)
class FieldView:
    # fmt: off
    name       : str
    value      : Any
    currency   : str | None
    confidence : float
    quote      : str
    page       : int | None
    provenance : str
    # fmt: on

    @property
    def label(self) -> str:
        return label_for(self.name)

    @property
    def verified(self) -> bool:
        return self.provenance == "verified"

    @property
    def suspect(self) -> bool:
        return self.provenance == "not_found"


@dataclass(frozen=True, slots=True)
class ItemView:
    # fmt: off
    item               : ReviewItem
    decision           : Decision
    attachment         : Attachment
    email              : Email | None
    fields             : list[FieldView]
    reasons            : list[dict[str, Any]]
    findings           : list[dict[str, Any]]
    trace              : list[dict[str, Any]]
    counterparty_id    : str | None
    counterparty_score : float | None
    usd                : float
    notes              : str
    # fmt: on
    #: Personal-data items masked at load, by category. Shown so a reviewer
    #: reading a quote with [IBAN] in it knows why, and so that "clean
    #: document" and "redaction did not run" are distinguishable.
    redactions: dict[str, int]

    @property
    def route(self) -> Route:
        return Route(self.decision.route)

    @property
    def concerns(self) -> list[dict[str, Any]]:
        """One entry per thing that is wrong, not one per rule that noticed it.

        Rules overlap by design -- a governing law outside the allow-list trips
        the playbook check, the confidence floor and the quote verifier, and a
        reviewer got three cards saying the same thing in three vocabularies.
        Grouping by the field turns that into one heading with the specific
        problems under it, and leaves whole-document reasons on their own.
        """
        by_field: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        for reason in self.reasons:
            fields = [f for f in reason.get("fields", ()) if f] or ["_document"]
            for name in fields:
                if name not in by_field:
                    by_field[name] = {
                        "field": None if name == "_document" else name,
                        "label": "This document" if name == "_document" else label_for(name),
                        "citations": [],
                        "lines": [],
                    }
                    order.append(name)
                entry = by_field[name]
                citation = str(reason.get("citation") or "")
                if citation and citation not in entry["citations"]:
                    entry["citations"].append(citation)
                line = str(reason.get("message") or "")
                # Rule messages are prefixed with the field they concern, which
                # is now the heading; repeating it reads as a stutter.
                for prefix in (f"{name}: ", f"{label_for(name)}: "):
                    if line.startswith(prefix):
                        line = line[len(prefix) :]
                if line and line not in entry["lines"]:
                    entry["lines"].append(line)

        return [by_field[name] for name in order]

    @property
    def provenance_counts(self) -> dict[str, int]:
        """How the evidence divides: checked, unverifiable, invented, absent."""
        counts = {"verified": 0, "unverifiable": 0, "not_found": 0, "absent": 0}
        for f in self.fields:
            counts[f.provenance] = counts.get(f.provenance, 0) + 1
        return counts

    @property
    def agent_ran(self) -> bool:
        """Stage 05 skips the agent whenever a deterministic check already fired.

        That is the common path for a document in this queue, and it must not be
        confused with an agent that ran and looked nothing up.
        """
        return bool(self.trace)

    @property
    def consulted_knowledge_base(self) -> bool:
        """Did the agent, having run, actually look anything up?

        Meaningless when the agent did not run -- which is why `agent_ran` is
        asked first. Read on its own it accused the model of relying on its own
        priors precisely when the findings came from arithmetic in
        `policy/thresholds.py`.
        """
        return self.agent_ran and any(t.get("tool") != "record_finding" for t in self.trace)

    @property
    def findings_from_rules(self) -> int:
        return sum(1 for f in self.findings if f.get("source") == "rules")


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

    item, decision, enrichment, extraction, document, attachment = row
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
        redactions=dict(document.redactions or {}),
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


#: What a field is called to a person. The schema names are the column names a
#: downstream system reads; a reviewer should not have to translate
#: `counterparty_registration_id` in their head twenty times a day.
LABELS: dict[str, str] = {
    "counterparty_name": "Counterparty",
    "counterparty_registration_id": "Registration number",
    "customer_name": "Our side",
    "effective_date": "Effective date",
    "term_months": "Initial term",
    "auto_renewal": "Automatic renewal",
    "termination_notice_days": "Notice to terminate",
    "payment_terms_days": "Payment terms",
    "liability_cap": "Liability cap",
    "liability_uncapped": "Liability excluded",
    "governing_law": "Governing law",
    "dpa_present": "Data processing agreement",
    "signatories": "Signatories",
}


def label_for(name: str) -> str:
    return LABELS.get(name, name.replace("_", " ").capitalize())


#: Prefix a form field carries when it is an editable extracted value.
FIELD_PREFIX = "field:"


def corrections_from_form(session: Session, item_id: int, form: dict[str, str]) -> dict[str, Any]:
    """Whatever the reviewer changed, and nothing they did not.

    Only fields that differ from the extracted value count. Recording an
    unchanged value as a correction would make every approval look like a
    disagreement, and the whole point of keeping the two side by side is that
    the disagreements are visible.
    """
    view = load_item(session, item_id)
    if view is None:
        return {}

    extracted = {f.name: f.value for f in view.fields}
    corrections: dict[str, Any] = {}
    for key, raw in form.items():
        if not key.startswith(FIELD_PREFIX):
            continue
        name = key[len(FIELD_PREFIX) :]
        if name not in extracted:
            continue
        if str(raw).strip() != str(extracted[name] if extracted[name] is not None else "").strip():
            corrections[name] = raw.strip()
    return corrections


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
    """File the contract a reviewer approved. Idempotent, like stage 07's path.

    Both write through store/contracts.py. They used to write it separately, in
    their own words, and had already drifted.
    """
    row = session.execute(
        select(Enrichment, Extraction)
        .join(Decision, Decision.enrichment_id == Enrichment.id)
        .join(Extraction, Enrichment.extraction_id == Extraction.id)
        .where(Decision.id == item.decision_id)
    ).first()
    if row is None:
        return

    enrichment, extraction = row
    store_contract(
        session,
        decision_id=item.decision_id,
        fields=extraction.fields,
        counterparty_id=enrichment.counterparty_id,
        corrections=item.human_corrections,
    )


def _reason_weight(reason: dict[str, Any]) -> tuple[int, str]:
    rank = {
        "not_a_contract": 0,
        "suspended_counterparty": 1,
        "high_severity_finding": 2,
        "unsupported_quote": 3,
        "partially_unverifiable": 4,
        "unresolved_counterparty": 5,
        "medium_severity_finding": 6,
        "wholly_unverifiable": 7,
        "missing_required_field": 8,
        "low_confidence_required_field": 9,
        "low_severity_finding": 10,
    }
    return rank.get(str(reason.get("rule")), 9), str(reason.get("message", ""))


def dashboard(session: Session) -> dict[str, Any]:
    """The four numbers a reviewer wants before the list itself.

    Deliberately four, and deliberately these: how much is waiting, how much
    went through without anyone, how much this cost, and how much of it nothing
    could check. The last is the one a queue view usually hides.
    """
    from contract_intake.db.models import Attachment, Contract, LLMCall

    spent = session.scalar(select(func.coalesce(func.sum(LLMCall.usd), 0.0))) or 0.0
    delivered = (
        session.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.status == "delivered")
        )
        or 0
    )
    scans = sum(
        1
        for row in session.scalars(select(Extraction)).all()
        if any(p.get("status") == "unverifiable" for p in row.fields.get("_provenance", []))
    )
    return {
        "open": session.scalar(
            select(func.count()).select_from(ReviewItem).where(ReviewItem.state == "open")
        )
        or 0,
        "filed": session.scalar(select(func.count()).select_from(Contract)) or 0,
        "usd": spent,
        "usd_each": spent / delivered if delivered else 0.0,
        "unverifiable": scans,
    }
