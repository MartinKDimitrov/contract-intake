"""The deterministic half of the playbook.

These are the comparisons a frontier model used to perform in prose, one clause
at a time, at roughly seven cents a document. Now they are data plus twenty lines
of Python, and they can be tested at their boundaries -- which is the part that
was never possible before.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from contract_intake.knowledge.policy import parse_playbook
from contract_intake.policy.thresholds import (
    UnknownOperatorError,
    cited_sections,
    evaluate,
    load_checks,
)

#: Shape of one entry in playbook_checks.json, for tests that build a broken one.
RAW_CHECK = {
    "id": "odd",
    "section": "§1.1",
    "field": "payment_terms_days",
    "op": "lte",
    "limit": 90,
    "severity": "low",
    "message": "?",
}

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
    built = {
        name: {"value": value, "confidence": 0.95, "source_quote": "q", "page": 1}
        for name, value in (COMPLIANT | overrides).items()
    }
    if "liability_cap" in built and built["liability_cap"]["value"] is not None:
        built["liability_cap"].setdefault("currency", "EUR")
    return built


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


def test_a_field_the_document_omits_fails_its_range_check() -> None:
    """A check that cannot see a value has not checked it.

    This asserted the opposite, on the reasoning that a `required` check would
    catch absence where it mattered. That reasoning lived in a different file
    and was wrong for §2.3: `termination_notice_ceiling` is `lte 90`, nothing
    marks the field required, and a contract with no termination-for-convenience
    right at all therefore passed. Silence is now a deviation unless a check
    declares `absent_ok`.
    """
    found = evaluate({"payment_terms_days": {"value": None, "confidence": 0.0}})
    assert "§1.1" in {f["citation"] for f in found if f["field"] == "payment_terms_days"}


def test_the_two_checks_where_silence_is_acceptable_say_so() -> None:
    """Auto-renewal and the offshore denylist are the documented exceptions."""
    found = {f["field"] for f in evaluate(fields(auto_renewal=None, governing_law=None))}

    assert "auto_renewal" not in found, "a contract silent on renewal does not renew"
    assert "§4.1" in {f["citation"] for f in evaluate(fields(governing_law=None))}, (
        "an unstated governing law still fails the allow-list"
    )


def test_a_missing_effective_date_is_its_own_finding() -> None:
    found = evaluate(fields(effective_date=None))
    assert [f["field"] for f in found] == ["effective_date"]


def test_a_liability_cap_with_no_currency_is_a_deviation() -> None:
    """Section 1.2: a figure with no currency is a deviation regardless of size."""
    entry = {"value": 500_000, "confidence": 0.95, "source_quote": "q", "page": 1}
    found = evaluate(fields() | {"liability_cap": entry})

    assert [f["citation"] for f in found] == ["§1.2"]


def test_a_cap_in_an_unlisted_currency_does_not_clear_the_floor() -> None:
    """250,000 JPY is about EUR 1,500. The floor comparison cannot see that."""
    entry = {"value": 250_000, "currency": "JPY", "confidence": 0.95, "source_quote": "q"}
    found = evaluate(fields() | {"liability_cap": entry})

    assert "§1.2" in {f["citation"] for f in found}


def test_excluding_liability_is_a_deviation_even_beside_a_stated_cap() -> None:
    """Section 3.2's first half had no check at all; only "states no cap" did."""
    found = evaluate(fields(liability_uncapped=True))

    assert [f["citation"] for f in found] == ["§3.2"]


def test_new_south_wales_is_not_england_and_wales() -> None:
    """The allow-list carries "wales" for England & Wales; substring matching
    therefore approved a jurisdiction we hold no counsel for."""
    found = evaluate(fields(governing_law="New South Wales, Australia"))

    assert [f["citation"] for f in found] == ["§4.1"]


@pytest.mark.parametrize(
    "stated",
    [
        "Bulgaria",
        "Republic of Bulgaria",
        "the laws of the Republic of Bulgaria",
        "England & Wales",
        "England and Wales",
        "  germany  ",
    ],
)
def test_ordinary_renderings_of_an_approved_jurisdiction_still_pass(stated: str) -> None:
    assert evaluate(fields(governing_law=stated)) == []


def test_an_empty_extraction_fires_almost_everything() -> None:
    """Nothing extracted means nothing checked, and that is not a pass."""
    fired = {f["field"] for f in evaluate({})}

    assert "auto_renewal" not in fired, "the documented absent_ok exception"
    assert {
        "liability_cap",
        "effective_date",
        "payment_terms_days",
        "term_months",
        "termination_notice_days",
        "governing_law",
    } <= fired


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


def test_an_unparsable_number_fires_its_range_check() -> None:
    """A value the checker cannot parse is a value it did not check."""
    found = evaluate(fields(payment_terms_days="thirty days"))
    assert [f["field"] for f in found] == ["payment_terms_days"]


def test_an_unknown_operator_is_a_loading_error_not_a_silent_pass() -> None:
    """An operator nobody evaluates is a check that always passes.

    It used to log a warning and return True, so a typo in the playbook JSON
    disabled a rule and said so only in a log line nobody reads.
    """
    broken = json.dumps({"checks": [dict(RAW_CHECK, op="teleports")]})
    path = Path(tempfile.mkdtemp()) / "checks.json"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(UnknownOperatorError, match="teleports"):
        load_checks(path)


#: The schema asks the model to record the governing law *as stated*, and
#: contracts state it adjectivally. Before the alias table the model was in a
#: double bind: quote the page faithfully and fail the allow-list, or normalise
#: to a country noun and fail quote verification.
AS_STATED = [
    ("English law", []),
    ("the laws of England and Wales", []),
    ("German law", []),
    ("deutschem Recht", []),
    ("Austrian law", []),
    ("Bulgarian law", []),
    ("the laws of the Republic of Bulgaria", []),
    ("la legislación española", ["§4.1"]),
    ("le droit français", ["§4.1"]),
    ("New South Wales, Australia", ["§4.1"]),
    ("Cayman Islands law", ["§4.1", "§4.1"]),
]


@pytest.mark.parametrize(("stated", "citations"), AS_STATED)
def test_a_jurisdiction_is_judged_however_it_is_written(stated: str, citations: list) -> None:
    found = [f["citation"] for f in evaluate(fields(governing_law=stated))]
    assert found == citations, stated


def test_a_missing_liability_cap_is_one_finding_not_three() -> None:
    """Three high findings for one absent figure is noise -- and worse.

    Stage 05 calls the agent only when the deterministic checks are silent, so
    duplicate findings for a single missing value suppressed the paid review
    that might have explained it. Section 3.2 owns absence; the floor and the
    currency check declare `absent_ok`.
    """
    found = [f for f in evaluate(fields(liability_cap=None)) if f["field"] == "liability_cap"]

    assert [f["citation"] for f in found] == ["§3.2"]
