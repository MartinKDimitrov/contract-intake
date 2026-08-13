"""The routing rules, exhaustively.

These are the tests that could not exist if an LLM made the decision, which is
most of the argument for stage 06 being pure Python. Nothing here touches a
model, a network or a database.
"""

from __future__ import annotations

import pytest

from contract_intake.extract.schema import REQUIRED_FOR_AUTO_APPROVAL
from contract_intake.policy.rules import ALL_RULES, Evidence, decide
from contract_intake.status import Route


def extraction(**overrides) -> dict:
    """A clean, fully-confident, fully-verified extraction."""
    base = {
        "document_kind": "contract",
        "_provenance": [
            {"field": name, "status": "verified"} for name in REQUIRED_FOR_AUTO_APPROVAL
        ],
    }
    for name in REQUIRED_FOR_AUTO_APPROVAL:
        base[name] = {"value": "x", "confidence": 0.95, "source_quote": "q", "page": 1}
    return base | overrides


def clean(**overrides) -> Evidence:
    kwargs = {
        "extraction": extraction(),
        "findings": (),
        "counterparty_id": "VEN-0142",
        "counterparty_score": 1.0,
        "counterparty_status": "approved",
    }
    return Evidence(**(kwargs | overrides))


def finding(severity: str = "high", **overrides) -> dict:
    return {
        "kind": "policy_deviation",
        "severity": severity,
        "field": "governing_law",
        "citation": "§4.1",
        "explanation": "Cayman Islands is outside the approved list",
    } | overrides


# -- the only path to auto-approval -----------------------------------------


def test_a_clean_contract_is_auto_approved(settings) -> None:
    decision = decide(clean(), settings)
    assert decision.route is Route.AUTO_APPROVED
    assert decision.reasons == ()
    assert decision.auto_approved


def test_silence_from_every_rule_is_required(settings) -> None:
    """Auto-approval is the absence of reasons, not a positive score."""
    for rule in ALL_RULES:
        assert rule(clean(), settings) == [], f"{rule.__name__} fires on a clean contract"


# -- each rule, one at a time -----------------------------------------------


@pytest.mark.parametrize("severity", ["high", "medium"])
def test_any_finding_blocks_auto_approval(settings, severity: str) -> None:
    decision = decide(clean(findings=(finding(severity),)), settings)
    assert decision.route is Route.NEEDS_REVIEW
    assert "governing_law" in decision.blocking_fields


def test_a_low_severity_finding_still_blocks(settings) -> None:
    """The agent does not get to route by choosing an adjective.

    This asserted the opposite. But the agent runs only where every
    deterministic check already passed, so any finding it returns is a
    judgement the code could not make -- and "low" made it vanish. Severity
    now orders the queue and nothing else.
    """
    decision = decide(clean(findings=[finding("low", citation="§3.3")]), settings)

    assert decision.route is Route.NEEDS_REVIEW
    assert [r.rule for r in decision.reasons] == ["low_severity_finding"]


def test_an_unrecognised_severity_blocks_rather_than_vanishing(settings) -> None:
    """A label outside the vocabulary used to mean the finding was ignored."""
    for severity in ("critical", "HIGH", "", None):
        decision = decide(clean(findings=[finding(severity)]), settings)
        assert decision.route is Route.NEEDS_REVIEW, severity


def test_the_finding_citation_survives_into_the_reason(settings) -> None:
    decision = decide(clean(findings=(finding(),)), settings)
    assert decision.reasons[0].citation == "§4.1"
    assert "Cayman" in decision.reasons[0].message


def test_an_unresolved_counterparty_blocks_on_its_own(settings) -> None:
    decision = decide(
        clean(counterparty_id=None, counterparty_score=0.41, counterparty_status="unknown"),
        settings,
    )
    assert decision.route is Route.NEEDS_REVIEW
    reason = next(r for r in decision.reasons if r.rule == "unresolved_counterparty")
    assert reason.citation == "§7.2"
    assert "0.41" in reason.message, "the reviewer needs the score, not just a refusal"


def test_a_suspended_supplier_blocks_whatever_the_terms(settings) -> None:
    """§7.1 -- commercially perfect and still not automatic."""
    decision = decide(clean(counterparty_id="VEN-0291", counterparty_status="suspended"), settings)
    assert decision.route is Route.NEEDS_REVIEW
    assert any(r.citation == "§7.1" for r in decision.reasons)


@pytest.mark.parametrize("name", REQUIRED_FOR_AUTO_APPROVAL)
def test_each_required_field_blocks_when_weak(settings, name: str) -> None:
    weak = extraction()
    weak[name] = {"value": "x", "confidence": 0.4, "source_quote": "q", "page": 1}
    decision = decide(clean(extraction=weak), settings)

    assert decision.route is Route.NEEDS_REVIEW
    assert name in decision.blocking_fields


def test_confidence_exactly_at_the_floor_passes(settings) -> None:
    at_floor = extraction()
    at_floor["term_months"] = {
        "value": 24,
        "confidence": settings.min_field_confidence,
        "source_quote": "q",
        "page": 1,
    }
    assert decide(clean(extraction=at_floor), settings).route is Route.AUTO_APPROVED


def test_an_invented_quote_is_named_as_such(settings) -> None:
    """ "Made up" and "unsure" are different problems; the reason says which."""
    invented = extraction()
    invented["_provenance"] = [
        {"field": name, "status": "verified"} for name in REQUIRED_FOR_AUTO_APPROVAL
    ] + [{"field": "liability_cap", "status": "not_found"}]

    decision = decide(clean(extraction=invented), settings)
    reason = next(r for r in decision.reasons if r.rule == "unsupported_quote")
    assert "liability_cap" in reason.message
    assert "liability_cap" in decision.blocking_fields


def test_a_document_the_model_calls_something_else_is_rejected(settings) -> None:
    decision = decide(clean(extraction=extraction(document_kind="other")), settings)
    assert decision.route is Route.REJECTED


# -- the rule that is easiest to leave out ----------------------------------


def test_a_wholly_scanned_contract_is_never_auto_approved(settings) -> None:
    """Commercially compliant, resolved counterparty, high confidence -- and a
    photograph. Nothing in it was checked against anything."""
    scan = extraction()
    scan["_provenance"] = [
        {"field": name, "status": "unverifiable"} for name in REQUIRED_FOR_AUTO_APPROVAL
    ]

    decision = decide(clean(extraction=scan), settings)
    assert decision.route is Route.NEEDS_REVIEW
    assert any(r.rule == "wholly_unverifiable" for r in decision.reasons)


def test_a_mixed_document_is_not_treated_as_a_scan(settings) -> None:
    """One scanned signature page does not make the whole contract unverified."""
    mixed = extraction()
    mixed["_provenance"] = [
        {"field": name, "status": "verified"} for name in REQUIRED_FOR_AUTO_APPROVAL
    ] + [{"field": "signatories", "status": "unverifiable"}]

    assert decide(clean(extraction=mixed), settings).route is Route.AUTO_APPROVED


def test_absent_fields_do_not_look_like_a_scan(settings) -> None:
    """A verified contract that simply omits a term is still verified."""
    partial = extraction()
    partial["_provenance"] = [
        {"field": name, "status": "verified"} for name in REQUIRED_FOR_AUTO_APPROVAL
    ] + [{"field": "liability_cap", "status": "absent"}]

    assert decide(clean(extraction=partial), settings).route is Route.AUTO_APPROVED


# -- several problems at once -----------------------------------------------


def test_every_rule_reports_rather_than_the_first(settings) -> None:
    """A reviewer should see all the problems, not discover them one per round."""
    bad = extraction(document_kind="contract")
    bad["governing_law"] = {"value": "Cayman", "confidence": 0.2, "source_quote": "q", "page": 1}

    decision = decide(
        clean(
            extraction=bad,
            findings=(finding("high"), finding("medium", field="payment_terms_days")),
            counterparty_id=None,
            counterparty_score=0.3,
        ),
        settings,
    )

    fired = {r.rule for r in decision.reasons}
    assert fired >= {
        "high_severity_finding",
        "medium_severity_finding",
        "unresolved_counterparty",
        "low_confidence_required_field",
    }


def test_blocking_fields_are_deduplicated_and_ordered(settings) -> None:
    decision = decide(
        clean(findings=(finding("high"), finding("medium", field="governing_law"))),
        settings,
    )
    assert decision.blocking_fields.count("governing_law") == 1


def test_reasons_serialise_for_storage(settings) -> None:
    decision = decide(clean(findings=(finding(),)), settings)
    payload = [r.to_json() for r in decision.reasons]
    assert payload[0]["rule"] == "high_severity_finding"
    assert payload[0]["citation"] == "§4.1"
    assert isinstance(payload[0]["fields"], list)


# -- thresholds come from settings, not from the code -----------------------


def test_the_confidence_floor_is_configurable(settings) -> None:
    weak = extraction()
    weak["term_months"] = {"value": 24, "confidence": 0.5, "source_quote": "q", "page": 1}

    strict = settings.model_copy(update={"min_field_confidence": 0.8})
    lenient = settings.model_copy(update={"min_field_confidence": 0.4})

    assert decide(clean(extraction=weak), strict).route is Route.NEEDS_REVIEW
    assert decide(clean(extraction=weak), lenient).route is Route.AUTO_APPROVED


def test_an_empty_extraction_does_not_crash(settings) -> None:
    decision = decide(Evidence(extraction={}), settings)
    assert decision.route is Route.NEEDS_REVIEW
