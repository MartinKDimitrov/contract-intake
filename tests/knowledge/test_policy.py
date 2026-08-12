"""Playbook retrieval.

The knowledge the model cannot derive from the contract: what *this company*
accepts. A hit has to carry its section number, because "deviates from §4.1" is
checkable by a human and "unusual jurisdiction" is not.
"""

from __future__ import annotations

import pytest

from contract_intake.knowledge.policy import PolicyIndex, parse_playbook


@pytest.fixture(scope="module")
def clauses():
    return parse_playbook()


@pytest.fixture(scope="module")
def index(tmp_path_factory, clauses):
    idx = PolicyIndex(tmp_path_factory.mktemp("chroma"))
    idx.build(clauses)
    return idx


# -- chunking ---------------------------------------------------------------


def test_playbook_splits_into_numbered_sections(clauses) -> None:
    assert len(clauses) >= 12
    assert all(c.section.startswith("§") for c in clauses)


def test_every_clause_carries_a_citation_and_a_body(clauses) -> None:
    for clause in clauses:
        assert clause.title, f"{clause.section} has no title"
        assert clause.body.strip(), f"{clause.section} has no body"
        assert clause.section in clause.citation


def test_sections_are_unique(clauses) -> None:
    sections = [c.section for c in clauses]
    assert len(sections) == len(set(sections))


def test_the_thresholds_the_rules_depend_on_are_present(clauses) -> None:
    """Stage 06 cites these by number; a renamed section would break it silently."""
    sections = {c.section for c in clauses}
    assert {"§1.1", "§2.2", "§3.1", "§3.2", "§4.1", "§5.1", "§7.1", "§7.2"} <= sections


# -- retrieval --------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("payment terms are 90 days from the invoice date", "§1.1"),
        ("the agreement renews automatically for successive periods", "§2.2"),
        ("this agreement is governed by the laws of the Cayman Islands", "§4.1"),
        ("neither party shall be liable for any loss whatsoever", "§3.2"),
        ("no data processing agreement is attached", "§5.1"),
        ("the supplier is suspended pending screening", "§7.1"),
    ],
)
def test_a_contract_phrase_finds_its_governing_clause(index, query: str, expected: str) -> None:
    """None of these are answerable by string matching -- this is the semantic half."""
    hits = index.search(query, k=3)
    assert expected in {h.clause.section for h in hits}, (
        f"{query!r} returned {[h.clause.section for h in hits]}"
    )


def test_the_top_hit_for_an_unambiguous_phrase_is_exact(index) -> None:
    top = index.search("undisputed invoices payable within ninety days", k=1)[0]
    assert top.clause.section == "§1.1"


def test_hits_are_ordered_by_score(index) -> None:
    hits = index.search("automatic renewal of the initial term", k=3)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_k_bounds_the_result_count(index) -> None:
    assert len(index.search("liability", k=2)) <= 2


def test_an_unrelated_query_still_returns_something_low(index) -> None:
    """Retrieval never refuses; the caller decides what a weak hit is worth."""
    hits = index.search("the migratory patterns of arctic terns", k=1)
    assert hits, "the index should answer, and let the agent judge relevance"


# -- rebuilding -------------------------------------------------------------


def test_building_twice_does_not_duplicate(index, clauses) -> None:
    index.build(clauses)
    index.build(clauses)
    hits = index.search("payment terms", k=5)
    sections = [h.clause.section for h in hits]
    assert len(sections) == len(set(sections))
