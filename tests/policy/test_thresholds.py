"""The deterministic half of the playbook.

These are the comparisons a frontier model used to perform in prose, one clause
at a time, at roughly seven cents a document. Now they are data plus twenty lines
of Python, and they can be tested at their boundaries -- which is the part that
was never possible before.
"""

from __future__ import annotations

import pytest

from contract_intake.knowledge.policy import parse_playbook
from contract_intake.policy.thresholds import Check, cited_sections, evaluate, load_checks

COMPLIANT = {
    "payment_terms_days": 45,
    "term_months": 24,
    "auto_renewal": False,
    "termination_notice_days": 60,
    "liability_cap": 500_000,
    "governing_law": "Republic of Bulgaria",
    "dpa_present": True,
    "effective_date": "2026-03-14",
}


def fields(**overrides) -> dict:
    """A compliant extraction with the named fields overridden.

    Built on a passing baseline so a test that changes one field sees only that
    field's check fire.
    """
    return {
        name: {"value": value, "confidence": 0.95, "source_quote": "q", "page": 1}
        for name, value in (COMPLIANT | overrides).items()
    }


def sections(findings) -> set[str]:
    return {f["citation"] for f in findings}


# -- the two files must not drift apart -------------------------------------


def test_every_cited_section_exists_in_the_playbook() -> None:
    """The guard on encoding one policy in two files.

    `playbook.md` is what a human maintains and the agent retrieves;
    `playbook_checks.json` is what the machine evaluates. A check citing a
    section that no longer exists would produce a finding a reviewer cannot
    look up.
    """
    prose = {clause.section for clause in parse_playbook()}
    missing = cited_sections() - prose
    assert not missing, f"checks cite sections absent from playbook.md: {sorted(missing)}"


def test_check_ids_are_unique() -> None:
    ids = [c.id for c in load_checks()]
    assert len(ids) == len(set(ids))


def test_every_check_carries_a_message_and_severity() -> None:
    for check in load_checks():
        assert check.message
        assert check.severity in {"low", "medium", "high"}


# -- boundaries, which is the whole point -----------------------------------


@pytest.mark.parametrize(
    ("days", "fires"),
    [(44, True), (45, False), (60, False), (90, False), (91, True)],
)
def test_payment_terms_boundaries(days: int, fires: bool) -> None:
    """The range is inclusive at both ends. 90 days complies; 91 does not."""
    found = evaluate(fields(payment_terms_days=days))
    assert ("§1.1" in sections(found)) is fires


@pytest.mark.parametrize(("months", "fires"), [(11, True), (12, False), (36, False), (37, True)])
def test_initial_term_boundaries(months: int, fires: bool) -> None:
    found = evaluate(fields(term_months=months))
    assert ("§2.1" in sections(found)) is fires


@pytest.mark.parametrize(("days", "fires"), [(30, False), (90, False), (91, True)])
def test_termination_notice_ceiling(days: int, fires: bool) -> None:
    found = evaluate(fields(termination_notice_days=days))
    assert ("§2.3" in sections(found)) is fires


@pytest.mark.parametrize(("cap", "fires"), [(249_999, True), (250_000, False), (500_000, False)])
def test_liability_cap_floor(cap: int, fires: bool) -> None:
    found = evaluate(fields(liability_cap=cap))
    assert any(f["citation"] == "§3.1" for f in found) is fires


# -- the individual rules ---------------------------------------------------


def test_automatic_renewal_is_a_deviation() -> None:
    assert "§2.2" in sections(evaluate(fields(auto_renewal=True)))
    assert "§2.2" not in sections(evaluate(fields(auto_renewal=False)))


def test_a_missing_liability_cap_is_a_deviation() -> None:
    """A contract that excludes liability states no cap at all."""
    found = evaluate(fields(liability_cap=None))
    assert any(f["citation"] == "§3.2" for f in found)


@pytest.mark.parametrize(
    ("law", "fires"),
    [
        ("Republic of Bulgaria", False),
        ("Germany", False),
        ("England & Wales", False),
        ("the Netherlands", False),
        ("Cayman Islands", True),
        ("Delaware", True),
    ],
)
def test_governing_law_allow_list(law: str, fires: bool) -> None:
    found = [f for f in evaluate(fields(governing_law=law)) if f["citation"] == "§4.1"]
    assert bool(found) is fires


def test_offshore_jurisdictions_are_flagged_separately() -> None:
    """Outside the list and offshore are two facts, and the second escalates."""
    found = evaluate(fields(governing_law="Cayman Islands"))
    assert len([f for f in found if f["citation"] == "§4.1"]) == 2
    assert all(f["severity"] == "high" for f in found if f["citation"] == "§4.1")


def test_an_unknown_jurisdiction_is_flagged_once() -> None:
    found = evaluate(fields(governing_law="Japan"))
    assert len([f for f in found if f["citation"] == "§4.1"]) == 1


# -- the check that depends on the registry ---------------------------------


def test_dpa_is_required_only_for_processing_categories() -> None:
    absent = fields(dpa_present=None)

    assert "§5.1" in sections(evaluate(absent, vendor_category="data_analytics"))
    assert "§5.1" in sections(evaluate(absent, vendor_category="it_services"))
    assert "§5.1" not in sections(evaluate(absent, vendor_category="freight_forwarding"))
    assert "§5.1" not in sections(evaluate(absent, vendor_category=None))


def test_a_processor_with_a_dpa_passes() -> None:
    found = evaluate(fields(dpa_present=True), vendor_category="data_analytics")
    assert "§5.1" not in sections(found)


# -- absence is not zero ----------------------------------------------------


def test_a_field_the_document_omits_does_not_fail_a_range_check() -> None:
    """Absence is caught by `required` where it matters, not silently by `between`."""
    found = evaluate({"payment_terms_days": {"value": None, "confidence": 0.0}})
    assert "§1.1" not in {f["citation"] for f in found if "payment" in f["field"]}


def test_a_missing_effective_date_is_its_own_finding() -> None:
    found = evaluate(fields(effective_date=None))
    assert [f["field"] for f in found] == ["effective_date"]


def test_an_empty_extraction_fires_only_the_required_checks() -> None:
    found = evaluate({})
    assert {f["field"] for f in found} == {"liability_cap", "effective_date"}


# -- the output shape stage 06 consumes -------------------------------------


def test_findings_match_the_agent_shape_and_name_their_source() -> None:
    finding = evaluate(fields(auto_renewal=True))[0]
    assert set(finding) == {"kind", "severity", "field", "citation", "explanation", "source"}
    assert finding["source"] == "rules"
    assert finding["citation"].startswith("§")


def test_the_message_carries_the_offending_value() -> None:
    finding = next(f for f in evaluate(fields(payment_terms_days=120)) if f["citation"] == "§1.1")
    assert "120" in finding["explanation"]
    assert "45" in finding["explanation"] and "90" in finding["explanation"]


# -- robustness -------------------------------------------------------------


def test_a_compliant_contract_produces_nothing() -> None:
    assert evaluate(fields(), vendor_category="freight_forwarding") == []


def test_an_unparseable_number_does_not_fire_a_range_check() -> None:
    found = evaluate(fields(payment_terms_days="thirty days"))
    assert not [f for f in found if f["field"] == "payment_terms_days"]


def test_an_unknown_operator_is_skipped_rather_than_crashing() -> None:
    odd = Check(
        id="odd",
        section="§1.1",
        field="payment_terms_days",
        op="teleports",
        severity="low",
        message="?",
        params={},
    )
    assert evaluate(fields(payment_terms_days=1), checks=[odd]) == []
