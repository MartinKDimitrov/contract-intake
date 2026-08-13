"""The human queue, which had no tests at all.

Human review is not a bolt-on here -- it is where every document the rules would
not pass ends up, and it is the only place a person's judgement enters the
record. It was also the last module in the package at zero percent coverage,
which is how a second, divergent writer to the `contracts` table lived in it
unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from contract_intake.db.models import (
    Contract,
    Decision,
    Document,
    Enrichment,
    Extraction,
    ReviewItem,
)
from contract_intake.status import Route
from contract_intake.web import review


def field(value, confidence=0.95, quote="q", page=1):
    return {"value": value, "confidence": confidence, "source_quote": quote, "page": page}


FIELDS = {
    "counterparty_name": field("Nordwind Logistik GmbH"),
    "payment_terms_days": field(90),
    "governing_law": field("Bulgaria"),
    "_provenance": [
        {"field": "counterparty_name", "status": "verified"},
        {"field": "payment_terms_days", "status": "not_found"},
        {"field": "governing_law", "status": "unverifiable"},
    ],
}


@pytest.fixture
def item(session, attachment):
    """One document sitting in the queue, off policy on its payment terms."""
    document = Document(attachment_id=attachment.id, page_count=2, text_pages=2)
    session.add(document)
    session.flush()

    extraction = Extraction(
        document_id=document.id, fields=dict(FIELDS), model="claude-opus-5", effort="medium"
    )
    session.add(extraction)
    session.flush()

    enrichment = Enrichment(
        extraction_id=extraction.id,
        findings=[{"severity": "medium", "citation": "§1.1", "field": "payment_terms_days"}],
        tool_trace=[{"tool": "search_playbook"}, {"tool": "record_finding"}],
        counterparty_id="VEN-0142",
        counterparty_score=1.0,
    )
    session.add(enrichment)
    session.flush()

    decision = Decision(
        enrichment_id=enrichment.id,
        route=Route.NEEDS_REVIEW,
        reasons=[
            {"rule": "medium_severity_finding", "citation": "§1.1", "message": "90 days"},
            {"rule": "partially_unverifiable", "message": "governing_law"},
        ],
        blocking_fields=["payment_terms_days"],
    )
    session.add(decision)
    session.flush()

    row = ReviewItem(decision_id=decision.id, state="open", created_at=datetime.now(UTC))
    session.add(row)
    session.commit()
    return row


# -- reading the queue -------------------------------------------------------


def test_the_queue_lists_what_is_open(session, item) -> None:
    rows = review.queue(session)

    assert len(rows) == 1
    assert rows[0].item_id == item.id


def test_a_resolved_item_leaves_the_open_queue(session, item) -> None:
    review.resolve_item(session, item.id, action="approve")
    session.commit()

    assert review.queue(session) == []
    assert len(review.queue(session, state="approved")) == 1


def test_the_detail_view_pairs_each_value_with_its_provenance(session, item) -> None:
    view = review.load_item(session, item.id)

    assert view is not None
    by_name = {f.name: f for f in view.fields}
    assert by_name["counterparty_name"].verified
    assert by_name["payment_terms_days"].suspect, "a quote that is not in the document"
    assert not by_name["governing_law"].verified, "unverifiable is not verified"
    assert view.route is Route.NEEDS_REVIEW
    assert view.agent_ran
    assert view.consulted_knowledge_base


def test_a_value_that_could_not_be_checked_sorts_above_a_policy_deviation(session, item) -> None:
    """You cannot judge a deviation in a value you have no evidence for.

    So provenance problems rank above policy ones. Both block; the order is what
    a reviewer reads first.
    """
    view = review.load_item(session, item.id)

    assert view is not None
    assert [r["rule"] for r in view.reasons] == [
        "partially_unverifiable",
        "medium_severity_finding",
    ]


def test_load_item_is_none_for_an_unknown_id(session) -> None:
    assert review.load_item(session, 9_999) is None


# -- resolving ---------------------------------------------------------------


def test_approving_files_the_contract_the_rules_withheld(session, item) -> None:
    review.resolve_item(session, item.id, action="approve", resolved_by="mdimitrov")
    session.commit()

    stored = session.scalars(select(Contract)).all()
    assert len(stored) == 1
    assert stored[0].decision_id == item.decision_id
    assert stored[0].counterparty_id == "VEN-0142"
    assert stored[0].payload["_approved_by_human"] is True
    assert "_provenance" not in stored[0].payload, "provenance stays in extractions"


def test_a_correction_reaches_the_contract_and_is_kept_beside_the_machine_value(
    session, item
) -> None:
    """A human's value and a model's value are not the same kind of fact."""
    review.resolve_item(session, item.id, action="approve", corrections={"payment_terms_days": 45})
    session.commit()

    stored = session.scalars(select(Contract)).one()
    assert stored.payload["payment_terms_days"] == 45

    resolved = session.get(ReviewItem, item.id)
    assert resolved is not None
    assert resolved.human_corrections == {"payment_terms_days": 45}
    extraction = session.scalars(select(Extraction)).one()
    assert extraction.fields["payment_terms_days"]["value"] == 90, "the machine's answer stands"


def test_rejecting_files_nothing(session, item) -> None:
    review.resolve_item(session, item.id, action="reject")
    session.commit()

    assert session.scalar(select(func.count()).select_from(Contract)) == 0
    resolved = session.get(ReviewItem, item.id)
    assert resolved is not None
    assert resolved.state == "rejected"


def test_approving_twice_files_one_contract(session, item) -> None:
    """A double-clicked approve button must not produce two records."""
    review.resolve_item(session, item.id, action="approve")
    review.resolve_item(session, item.id, action="approve")
    session.commit()

    assert session.scalar(select(func.count()).select_from(Contract)) == 1


def test_resolving_an_unknown_item_is_not_an_error(session) -> None:
    assert review.resolve_item(session, 9_999, action="approve") is None


def test_a_skipped_agent_is_not_reported_as_an_unfounded_one(session, item) -> None:
    """The common path, and the review UI used to accuse the model on it.

    Stage 05 skips the agent whenever a deterministic check already fired, which
    is what puts most documents in this queue. With an empty trace the card read
    "the agent recorded findings without consulting the registry ... they rest on
    its own priors" -- about findings produced by arithmetic in code.
    """
    from sqlalchemy import select

    from contract_intake.db.models import Enrichment

    enrichment = session.scalars(select(Enrichment)).one()
    enrichment.tool_trace = []
    enrichment.findings = [
        {"severity": "medium", "citation": "§1.1", "source": "rules", "field": "payment_terms_days"}
    ]
    session.commit()

    view = review.load_item(session, item.id)

    assert view is not None
    assert not view.agent_ran
    assert not view.consulted_knowledge_base
    assert view.findings_from_rules == 1


def test_a_correction_typed_into_the_form_reaches_the_record(session, item) -> None:
    """The route used to accept only `action`, so corrections never arrived.

    `resolve_item(corrections=...)` was tested by calling it directly, and the
    HTTP path that a reviewer actually uses passed nothing -- so
    `human_corrections` was always empty while every approval was stamped
    "approved by human".
    """
    form = {
        "action": "approve",
        "field:payment_terms_days": "45",
        "field:counterparty_name": "Nordwind Logistik GmbH",  # unchanged
    }
    corrections = review.corrections_from_form(session, item.id, form)

    assert corrections == {"payment_terms_days": "45"}, "only what changed is a correction"

    review.resolve_item(session, item.id, action="approve", corrections=corrections)
    session.commit()

    stored = session.scalars(select(Contract)).one()
    assert stored.payload["payment_terms_days"] == "45"
    resolved = session.get(ReviewItem, item.id)
    assert resolved is not None
    assert resolved.human_corrections == {"payment_terms_days": "45"}


def test_approving_without_editing_anything_records_no_corrections(session, item) -> None:
    """Otherwise every approval reads as a disagreement with the model."""
    form = {"action": "approve", "field:payment_terms_days": "90"}

    assert review.corrections_from_form(session, item.id, form) == {}
