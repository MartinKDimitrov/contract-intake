"""Masking personal data must remove identifiers without damaging the contract.

Two halves, and the second is the one that matters. Proving that an IBAN is
masked is easy. Proving that nothing a contracts team needs was masked with it
is the reason this can be switched on by default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_intake.loaders.document import Document, Page
from contract_intake.loaders.redact import MASK, is_egn, is_iban, is_luhn, is_nir, redact

DOCUMENTS = Path(__file__).resolve().parents[2] / "evals" / "documents"
EXPECTED = Path(__file__).resolve().parents[2] / "evals" / "expected"


# -- what must be masked ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Paid to BG80 BNBG 9661 1020 3456 78 monthly.", "iban"),
        ("Account DE89 3704 0044 0532 0130 00 at Commerzbank.", "iban"),
        ("Signed by I. Petrov, EGN 7523169263.", "national_id"),
        ("Numéro de sécurité sociale 1 84 12 76 451 089 46.", "national_id"),
        ("Firmado por J. Ruiz, DNI 12345678Z.", "national_id"),
        ("NIE X1234567L, residente en Barcelona.", "national_id"),
        ("Corporate card 4111 1111 1111 1111.", "card"),
        ("Notices to ops.contact@meridia.example.com.", "email"),
        ("Contact tel. +359 888 123 456 during business hours.", "phone"),
    ],
)
def test_identifier_is_masked(text: str, category: str) -> None:
    masked, counts = redact(text)
    assert counts.get(category) == 1, counts
    assert MASK[category] in masked


def test_counts_are_per_category() -> None:
    _, counts = redact("EGN 7523169263 and 8001010008, IBAN BG80 BNBG 9661 1020 3456 78")
    assert counts == {"national_id": 2, "iban": 1}


# -- what must survive ------------------------------------------------------
#
# Company identifiers are not personal data, and two of them are extracted
# fields: counterparty_registration_id and the amounts behind the thresholds.
# A redactor that eats these degrades extraction silently, which is worse than
# not redacting at all.


@pytest.mark.parametrize(
    "text",
    [
        "Registered under UIC 831915840, VAT BG831915840.",
        "Eingetragen im Handelsregister HRB 84421.",
        "CIF B-66214508, inscrita en el Registro Mercantil de Barcelona.",
        "Immatriculée au RCS de Lyon sous le numéro 851 402 336.",
        "SIRET 85140233600018.",
        "Payment terms: forty-five (45) days from receipt.",
        "Aggregate liability shall not exceed 500 000 EUR.",
        "The annual charge is 620 000 EUR excluding VAT.",
        "This Agreement is effective 14 March 2026 for 24 months.",
        "See clause 12.4.1 and Annex 3 of the Master Services Agreement.",
        "Reference 2026-00841 applies to purchase order 4500123789.",
    ],
)
def test_contract_content_survives(text: str) -> None:
    masked, counts = redact(text)
    assert masked == text
    assert counts == {}


@pytest.mark.parametrize(
    ("candidate", "check"),
    [("7523169264", is_egn), ("1 84 12 76 451 089 47", is_nir), ("4111111111111112", is_luhn)],
)
def test_a_failing_checksum_is_not_an_identifier(candidate, check) -> None:
    """One digit off is a reference number, not a person. Validation, not shape."""
    assert not check(candidate.replace(" ", ""))
    assert redact(f"Ref {candidate} applies.")[1] == {}


def test_iban_checksum_is_enforced() -> None:
    assert is_iban("BG80 BNBG 9661 1020 3456 78")
    assert not is_iban("BG81 BNBG 9661 1020 3456 78")


# -- the corpus invariant ---------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.stem for p in EXPECTED.glob("*.json")))
def test_every_expected_value_survives_redaction(name: str) -> None:
    """Redaction must not remove anything the eval expects to be extracted.

    This is the test that lets redaction default to on. It runs over the
    documents whose correct answers are written down, masks them, and asserts
    each answer is still findable in the text the model will be given.
    """
    source = next(DOCUMENTS.rglob(f"{name}*.txt"), None)
    if source is None:
        pytest.skip(f"no source document for {name}")

    original = source.read_text(encoding="utf-8")
    masked, _ = redact(original)
    expected = json.loads((EXPECTED / f"{name}.json").read_text(encoding="utf-8"))

    for field, value in expected["fields"].items():
        if isinstance(value, bool) or value is None:
            continue  # not a literal to find in the page
        needle = str(value)
        if needle not in original:
            continue  # worded rather than printed -- "45 (forty-five) days"
        assert needle in masked, f"{field}={value!r} was in the document and redaction ate it"


# -- the document boundary --------------------------------------------------


def test_image_pages_are_left_alone_and_that_is_the_known_gap() -> None:
    """A scan has no text layer, so nothing here can mask what is on it."""
    document = Document(
        pages=[
            Page(number=1, kind="text", text="EGN 7523169263"),
            Page(number=2, kind="image", image_path="/tmp/p002.png", width=800, height=1100),
        ]
    )
    result = document.mask_personal_data()

    assert result.pages[0].text == f"EGN {MASK['national_id']}"
    assert result.pages[1] == document.pages[1]
    assert result.redactions == {"national_id": 1}
    assert result.redacted


def test_a_clean_document_is_distinguishable_from_a_skipped_one() -> None:
    document = Document(pages=[Page(number=1, kind="text", text="Payment terms: 45 days.")])
    result = document.mask_personal_data()

    assert result.redactions == {}
    assert result.redacted is True
    assert document.redacted is False
