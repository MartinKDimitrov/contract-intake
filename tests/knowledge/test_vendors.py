"""Counterparty resolution.

The cases below are the argument for using trigram matching rather than
embeddings here, so they are pinned as regressions: a scan that mangles the
legal form must resolve, and a company that merely *sounds* similar must not.
"""

from __future__ import annotations

import pytest

from contract_intake.knowledge.vendors import Vendor, normalise_company, resolve

NORDWIND = "Nordwind Logistik GmbH"


# -- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nordwind Logistik GmbH", "nordwind logistik"),
        ("Nordwind Logistik G.m.b.H.", "nordwind logistik"),
        ("NORDWIND LOGISTIK GMBH", "nordwind logistik"),
        ("Vistula Cargo Sp. z o.o.", "vistula cargo"),
        ("Balkan Steel Trading EOOD", "balkan steel trading"),
        ("Kestrel Analytics Limited", "kestrel analytics"),
        ("Nordwind  Logistik   GmbH", "nordwind logistik"),
    ],
)
def test_legal_forms_and_punctuation_are_stripped(raw: str, expected: str) -> None:
    """A legal form identifies a jurisdiction, not a company."""
    assert normalise_company(raw) == expected


def test_a_name_that_is_only_a_legal_form_survives() -> None:
    assert normalise_company("GmbH") != ""


# -- the cases that justify the choice of matcher ---------------------------


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("Nordwind Logistik GmbH", "exact"),
        ("NordWind Logistics Ltd.", "the scan: different legal form and spelling"),
        ("Nordwind Logistik G.m.b.H.", "punctuated legal form"),
        ("NORDWIND LOGISTIK GMBH", "shouting"),
        ("Nordwlnd Logistik GmbH", "OCR read i as l"),
        ("Logistik Nordwind GmbH", "word order reversed"),
        ("  Nordwind   Logistik  GmbH  ", "whitespace damage"),
    ],
)
def test_damaged_names_still_resolve(name: str, why: str) -> None:
    match = resolve(name)
    assert match.resolved, f"{why}: {name!r} scored {match.score:.2f}"
    assert match.vendor is not None
    assert match.vendor.legal_name == NORDWIND


def test_a_similar_sounding_company_is_not_confused_with_it() -> None:
    """The case a dense retriever gets wrong: same first word, same industry."""
    match = resolve("Nordwind Marine Services AS")
    assert match.resolved
    assert match.vendor is not None
    assert match.vendor.legal_name == "Nordwind Marine Services AS"
    assert match.vendor.legal_name != NORDWIND


def test_an_added_word_is_rejected_rather_than_assumed() -> None:
    """A subsidiary is a different legal entity; guessing is worse than asking."""
    match = resolve("Nordwind Logistics International GmbH")
    assert not match.resolved
    assert match.score < 0.85


@pytest.mark.parametrize(
    "name",
    ["Totally Unknown Vendor SRL", "Балкан Стийл", "Acme Corp", "x"],
)
def test_unknown_counterparties_are_not_forced_into_a_match(name: str) -> None:
    assert not resolve(name).resolved


def test_no_name_is_handled_without_raising() -> None:
    for value in (None, "", "   "):
        match = resolve(value)
        assert not match.resolved
        assert "no counterparty name" in match.reason


# -- registration number is decisive ----------------------------------------


def test_registration_number_beats_the_name() -> None:
    match = resolve("Nordwind Logistik", registration_id="HRB 84421")
    assert match.matched_on == "registration_id"
    assert match.score == 1.0


@pytest.mark.parametrize("written", ["HRB 84421", "hrb84421", "HRB-84421", "HRB  84421"])
def test_registration_number_formatting_does_not_matter(written: str) -> None:
    """How the number is punctuated must not change what it resolves to.

    The name here used to read "something unrecognisable", which made this a
    test of the very hole it was hiding: a registration number resolved on its
    own, so any name at all -- including a *suspended* supplier's -- landed on
    whichever vendor the number belonged to. The name now has to agree; the
    disagreement case is its own test below.
    """
    match = resolve("NordWind Logistics Ltd.", registration_id=written)
    assert match.resolved
    assert match.vendor is not None
    assert match.vendor.legal_name == NORDWIND


def test_a_name_that_disagrees_with_the_registration_resolves_to_nobody() -> None:
    """One borrowed number was a path from a suspended supplier to approved."""
    match = resolve("Levant Shipping Agency SAL", registration_id="HRB 84421")

    assert not match.resolved
    assert match.matched_on == "conflict"
    assert "Levant" in match.reason and NORDWIND in match.reason


def test_a_scan_mangled_name_still_resolves_on_its_registration() -> None:
    """The reason the number is consulted at all must keep working."""
    match = resolve("NordWlnd Logisttk GmbH", registration_id="HRB 84421")

    assert match.resolved
    assert match.vendor is not None
    assert match.vendor.legal_name == NORDWIND


def test_an_unknown_registration_number_falls_back_to_the_name() -> None:
    match = resolve(NORDWIND, registration_id="HRB 000000")
    assert match.resolved
    assert match.matched_on == "name"


# -- what the match carries forward -----------------------------------------


def test_a_rejected_match_offers_the_near_misses() -> None:
    """A reviewer needs candidates, not just a refusal."""
    match = resolve("Nordwind Logistics International GmbH")
    assert match.runners_up, "review needs something to choose between"
    assert match.reason


def test_suspended_vendors_resolve_but_are_flagged() -> None:
    """Resolution and approval are different questions."""
    match = resolve("Levant Shipping Agency SAL")
    assert match.resolved
    assert match.vendor is not None
    assert match.vendor.is_suspended


def test_threshold_is_configurable() -> None:
    name = "Nordwind Logistics International GmbH"
    assert not resolve(name, threshold=0.85).resolved
    assert resolve(name, threshold=0.60).resolved


def test_resolution_runs_against_an_injected_registry() -> None:
    registry = (
        Vendor(
            id="VEN-TEST",
            legal_name="Solitary Test Ltd",
            aliases=(),
            registration_id="1",
            country="GB",
            category="test",
            risk_class="standard",
            status="approved",
        ),
    )
    assert resolve("Solitary Test Limited", registry=registry).resolved
    assert not resolve(NORDWIND, registry=registry).resolved
