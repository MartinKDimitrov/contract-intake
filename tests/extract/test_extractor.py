"""Provenance verification.

This is the mechanism that separates a system that knows from one that guessed,
so it gets the most tests in the project. Everything here is hermetic -- no API
key, no network.
"""

from __future__ import annotations

import pytest

from contract_intake.extract.extractor import _normalise, verify_provenance
from contract_intake.extract.schema import (
    BoolField,
    ContractExtraction,
    IntField,
    MoneyField,
    TextField,
)
from contract_intake.loaders.document import Document, Page

PAGE_ONE = (
    "MASTER SERVICES AGREEMENT\n"
    "This Agreement is entered into as of 14 March 2026 by and between\n"
    "Meridian Rail Holdings AD and Nordwind Logistik GmbH.\n"
    "The Customer shall pay each undisputed invoice within thirty (30) days\n"
    "of receipt. The aggregate liability shall not exceed EUR 500,000."
)


def text_document() -> Document:
    return Document(pages=[Page(number=1, kind="text", text=PAGE_ONE)])


def scan_document() -> Document:
    return Document(
        pages=[Page(number=1, kind="image", image_path="/tmp/p001.png", width=990, height=1400)]
    )


def build(**overrides: object) -> ContractExtraction:
    base: dict[str, object] = {
        "document_kind": "contract",
        "counterparty_name": TextField(value=None, confidence=0.0),
        "counterparty_registration_id": TextField(value=None, confidence=0.0),
        "customer_name": TextField(value=None, confidence=0.0),
        "effective_date": TextField(value=None, confidence=0.0),
        "term_months": IntField(value=None, confidence=0.0),
        "auto_renewal": BoolField(value=None, confidence=0.0),
        "termination_notice_days": IntField(value=None, confidence=0.0),
        "payment_terms_days": IntField(value=None, confidence=0.0),
        "liability_cap": MoneyField(value=None, confidence=0.0),
        "liability_uncapped": BoolField(value=None, confidence=0.0),
        "governing_law": TextField(value=None, confidence=0.0),
        "dpa_present": BoolField(value=None, confidence=0.0),
        "signatories": TextField(value=None, confidence=0.0),
    }
    return ContractExtraction(**(base | overrides))  # type: ignore[arg-type]


def verdict_for(extraction: ContractExtraction, document: Document, name: str):
    return next(v for v in verify_provenance(extraction, document) if v.name == name)


# -- the honest cases -------------------------------------------------------


def test_quote_present_in_the_document_is_verified() -> None:
    e = build(
        payment_terms_days=IntField(
            value=30, confidence=0.93, source_quote="within thirty (30) days", page=1
        )
    )
    v = verdict_for(e, text_document(), "payment_terms_days")
    assert v.status == "verified"
    assert v.confidence == pytest.approx(0.93)


def test_a_null_value_is_absent_not_a_failure() -> None:
    """A field the document does not state is the correct answer, not an error."""
    v = verdict_for(build(), text_document(), "liability_cap")
    assert v.status == "absent"
    assert v.confidence == 0.0


# -- the dishonest cases ----------------------------------------------------


def test_invented_quote_drives_confidence_to_zero() -> None:
    e = build(
        payment_terms_days=IntField(
            value=45,
            confidence=0.97,
            source_quote="payable within forty-five (45) days of invoice",
            page=1,
        )
    )
    document = text_document()
    v = verdict_for(e, document, "payment_terms_days")

    assert v.status == "not_found"
    assert v.confidence == 0.0
    assert "0.97" in v.note, "the note must record what the model claimed"
    assert e.payment_terms_days.confidence == 0.0, "the field itself is corrected, not just noted"


def test_value_without_a_quote_is_rejected() -> None:
    e = build(term_months=IntField(value=24, confidence=0.9, source_quote=None, page=1))
    v = verdict_for(e, text_document(), "term_months")
    assert v.status == "not_found"
    assert e.term_months.confidence == 0.0


def test_hallucinated_fields_are_collected() -> None:
    e = build(
        governing_law=TextField(
            value="Cayman Islands",
            confidence=0.9,
            source_quote="laws of the Cayman Islands",
            page=1,
        ),
        term_months=IntField(
            value=24, confidence=0.9, source_quote="initial term of twenty-four (24) months", page=1
        ),
    )
    names = {v.name for v in verify_provenance(e, text_document()) if v.status == "not_found"}
    assert names == {"governing_law", "term_months"}


# -- normalisation: quotes that differ only cosmetically --------------------


@pytest.mark.parametrize(
    "quote",
    [
        "pay each undisputed invoice within thirty (30) days\nof receipt",  # PDF line break
        "PAY EACH UNDISPUTED INVOICE WITHIN THIRTY (30) DAYS OF RECEIPT",  # case
        "pay  each   undisputed    invoice within thirty (30) days of receipt",  # spacing
    ],
)
def test_cosmetic_differences_still_verify(quote: str) -> None:
    """Extraction inserts line breaks mid-sentence; failing on that is noise."""
    e = build(payment_terms_days=IntField(value=30, confidence=0.9, source_quote=quote, page=1))
    assert verdict_for(e, text_document(), "payment_terms_days").status == "verified"


def test_typographic_punctuation_is_folded() -> None:
    document = Document(
        pages=[
            Page(
                number=1,
                kind="text",
                text="the Supplier\u2019s liability is capped at EUR 500,000",
            )
        ]
    )
    e = build(
        liability_cap=MoneyField(
            value=500000.0,
            confidence=0.8,
            source_quote="the Supplier's liability is capped at EUR 500,000",
            page=1,
        )
    )
    assert verdict_for(e, document, "liability_cap").status == "verified"


def test_normalise_collapses_dashes_and_whitespace() -> None:
    assert _normalise("a\u2014b   c") == "a-b c"


# -- scanned pages: unverifiable, not trusted, not failed -------------------


def test_quote_from_a_scanned_page_is_unverifiable() -> None:
    e = build(
        counterparty_name=TextField(
            value="NordWind Logistics Ltd.",
            confidence=0.93,
            source_quote="NordWind Logistics Ltd.",
            page=1,
        )
    )
    v = verdict_for(e, scan_document(), "counterparty_name")

    assert v.status == "unverifiable"
    assert v.confidence == pytest.approx(0.93), "confidence survives; it just is not confirmed"
    assert "no text layer" in v.note


def test_unverifiable_is_not_counted_as_hallucination() -> None:
    e = build(
        term_months=IntField(value=18, confidence=0.9, source_quote="eighteen (18) months", page=1)
    )
    verdicts = verify_provenance(e, scan_document())
    assert [v.name for v in verdicts if v.status == "not_found"] == []


def test_mixed_document_verifies_text_pages_and_excuses_image_pages() -> None:
    """The realistic case: born-digital body with a scanned signature page."""
    document = Document(
        pages=[
            Page(number=1, kind="text", text=PAGE_ONE),
            Page(number=2, kind="image", image_path="/tmp/p002.png", width=990, height=1400),
        ]
    )
    e = build(
        payment_terms_days=IntField(
            value=30, confidence=0.9, source_quote="within thirty (30) days", page=1
        ),
        signatories=TextField(
            value="I. Petrova, CFO", confidence=0.8, source_quote="I. Petrova, CFO", page=2
        ),
    )
    assert verdict_for(e, document, "payment_terms_days").status == "verified"
    assert verdict_for(e, document, "signatories").status == "unverifiable"


def test_a_short_quote_cannot_verify_anything() -> None:
    """A quote too short to locate is not evidence, whether or not it occurs.

    This test used to assert the opposite. The length check sat *after* the
    search, so it was only ever reached by quotes that had already failed to
    match -- and it handed them back the model's own confidence. Any fabricated
    value could defeat verification by keeping its quote under twelve
    characters.
    """
    e = build(term_months=IntField(value=24, confidence=0.7, source_quote="24 mo", page=1))
    v = verdict_for(e, text_document(), "term_months")

    assert v.status == "not_found"
    assert v.confidence == 0.0
    assert "too short" in v.note


def test_a_claimed_scan_page_cannot_launder_an_invented_quote() -> None:
    """The model supplies ``page``, so it must not be able to skip checking.

    On a mixed document the value is still marked unverifiable rather than
    called a lie -- but ``rule_partially_unverifiable`` then blocks it, so the
    claim buys nothing.
    """
    from contract_intake.loaders.document import Document, Page

    mixed = Document(
        pages=[
            Page(number=1, kind="text", text="MASTER SERVICES AGREEMENT between the parties."),
            Page(number=2, kind="image", image_path="/tmp/p2.png", width=800, height=1100),
        ]
    )
    e = build(
        payment_terms_days=IntField(
            value=60,
            confidence=0.99,
            source_quote="PAYMENT IS DUE NET 60 DAYS AS AGREED",
            page=2,
        )
    )
    v = verdict_for(e, mixed, "payment_terms_days")

    assert v.status == "unverifiable"
    assert "is a scan" in v.note
