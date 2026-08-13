"""The five ways a model could once defeat provenance verification.

Each of these passed on the day it was written. They are kept as one file, with
one shared document and one control, because the property under test is not
"quote checking works" but the stronger claim the README makes: that a value the
document does not support cannot reach auto-approval.

The document below contains a 45-day payment term. Every attack invents a 60-day
one and tries to get it past verification a different way.
"""

from __future__ import annotations

import pytest

from contract_intake.config import Settings
from contract_intake.extract.extractor import supports_value, verify_provenance
from contract_intake.extract.schema import ContractExtraction
from contract_intake.loaders.document import Document, Page
from contract_intake.policy.rules import Evidence, decide
from contract_intake.status import Route

BODY = (
    "MASTER SERVICES AGREEMENT dated 14 March 2026 between Meridian Rail Holdings AD "
    "and Nordwind Logistik GmbH, HRB 84421. Term twenty-four (24) months. "
    "Governed by the laws of Bulgaria. Liability capped at EUR 500,000. "
    "Payment of each undisputed invoice within forty-five (45) days. "
    "Termination on sixty (60) days notice. A Data Processing Agreement is annexed."
)


def document() -> Document:
    """One text page and one scanned page -- the mixed case, which is the common one."""
    return Document(
        pages=[
            Page(number=1, kind="text", text=BODY),
            Page(number=2, kind="image", image_path="/tmp/p2.png", width=800, height=1100),
        ]
    )


def honest() -> dict:
    return {
        "document_kind": "contract",
        "counterparty_name": {
            "value": "Nordwind Logistik GmbH",
            "confidence": 0.98,
            "source_quote": "Nordwind Logistik GmbH, HRB 84421",
            "page": 1,
        },
        "counterparty_registration_id": {
            "value": "HRB 84421",
            "confidence": 0.97,
            "source_quote": "Nordwind Logistik GmbH, HRB 84421",
            "page": 1,
        },
        "customer_name": {
            "value": "Meridian Rail Holdings AD",
            "confidence": 0.97,
            "source_quote": "between Meridian Rail Holdings AD",
            "page": 1,
        },
        "effective_date": {
            "value": "2026-03-14",
            "confidence": 0.9,
            "source_quote": "AGREEMENT dated 14 March 2026 between",
            "page": 1,
        },
        "term_months": {
            "value": 24,
            "confidence": 0.95,
            "source_quote": "Term twenty-four (24) months",
            "page": 1,
        },
        "auto_renewal": {
            "value": False,
            "confidence": 0.9,
            "source_quote": "Term twenty-four (24) months",
            "page": 1,
        },
        "termination_notice_days": {
            "value": 60,
            "confidence": 0.95,
            "source_quote": "Termination on sixty (60) days notice",
            "page": 1,
        },
        "payment_terms_days": {
            "value": 45,
            "confidence": 0.99,
            "source_quote": "undisputed invoice within forty-five (45) days",
            "page": 1,
        },
        "liability_cap": {
            "value": 500000,
            "currency": "EUR",
            "confidence": 0.95,
            "source_quote": "Liability capped at EUR 500,000",
            "page": 1,
        },
        "liability_uncapped": {
            "value": False,
            "confidence": 0.95,
            "source_quote": "Liability capped at EUR 500,000",
            "page": 1,
        },
        "governing_law": {
            "value": "Bulgaria",
            "confidence": 0.96,
            "source_quote": "Governed by the laws of Bulgaria",
            "page": 1,
        },
        "dpa_present": {
            "value": True,
            "confidence": 0.95,
            "source_quote": "A Data Processing Agreement is annexed",
            "page": 1,
        },
        "signatories": {
            "value": "Meridian Rail Holdings AD",
            "confidence": 0.8,
            "source_quote": "between Meridian Rail Holdings AD",
            "page": 1,
        },
    }


def route_for(raw: dict) -> tuple[str, float, Route]:
    extraction = ContractExtraction.model_validate(raw)
    verdicts = verify_provenance(extraction, document())
    payload = extraction.model_dump()
    payload["_provenance"] = [{"field": v.name, "status": v.status} for v in verdicts]

    decision = decide(
        Evidence(
            extraction=payload,
            counterparty_id="VEN-0142",
            counterparty_status="approved",
        ),
        Settings(),
    )
    verdict = next(v for v in verdicts if v.name == "payment_terms_days")
    return verdict.status, verdict.confidence, decision.route


def test_the_control_still_auto_approves() -> None:
    """Without this, every test below would pass on a broken system."""
    status, confidence, route = route_for(honest())

    assert status == "verified"
    assert confidence == pytest.approx(0.99)
    assert route is Route.AUTO_APPROVED


#: Each attack invents ``payment_terms_days = 60``. The document says 45.
ATTACKS = [
    pytest.param(
        {"page": 2, "source_quote": "PAYMENT IS DUE NET 60 DAYS AS AGREED"},
        "unverifiable",
        id="attributes the quote to a scanned page, where nothing can check it",
    ),
    pytest.param(
        {"page": 1, "source_quote": "net 60 days"},
        "not_found",
        id="keeps the quote under the minimum length",
    ),
    pytest.param(
        {"page": 1, "source_quote": "MASTER SERVICES AGREEMENT dated"},
        "not_found",
        id="cites real boilerplate that supports nothing",
    ),
    pytest.param(
        {"page": 1, "source_quote": "Term twenty-four (24) months"},
        "not_found",
        id="cites a real clause about a different number",
    ),
    pytest.param(
        {"page": 7, "source_quote": "PAYMENT IS DUE NET 60 DAYS AS AGREED"},
        "not_found",
        id="claims a page the document does not have",
    ),
]


@pytest.mark.parametrize(("mutation", "expected_status"), ATTACKS)
def test_an_invented_payment_term_cannot_reach_auto_approval(
    mutation: dict, expected_status: str
) -> None:
    raw = honest()
    raw["payment_terms_days"] |= {"value": 60} | mutation

    status, _, route = route_for(raw)

    assert status == expected_status
    assert route is Route.NEEDS_REVIEW


#: Ordinary words that carry a numeral as a substring. Searched unanchored --
#: which is how the first version did it -- 14% of the digit-free clause
#: fragments in this repository's own corpus counted as "containing a number",
#: and the boilerplate bypass reopened for every numeric field.
NOT_NUMBERS = [
    "SOFTWARE MAINTENANCE AND SLA AGREEMENT",
    "written notice to the other party hereto",
    "each component of the service described",
    "under similar terms and conditions",
    "the tenant shall maintain the premises",
    "executed in September of that year",
    "the central office of the Supplier",
]


@pytest.mark.parametrize("quote", NOT_NUMBERS)
def test_a_word_that_merely_contains_a_numeral_is_not_a_number(quote: str) -> None:
    assert not supports_value("payment_terms_days", 60, quote), quote


#: A quote's digits are the numbers it states, not a concatenation of them.
#: Flattened, "dated 15 March 2024" reads as "152024", which contains 24, 52,
#: 202 and 15 -- so a date supported a two-year term and a fifty-two week period.
DIGITS_THAT_ARE_NOT_THE_VALUE = [
    ("term_months", 24, "This Agreement is dated 15 March 2024 and is made between"),
    ("term_months", 52, "This Agreement is dated 15 March 2024"),
    ("payment_terms_days", 202, "dated 15 March 2024"),
]


@pytest.mark.parametrize(("name", "value", "quote"), DIGITS_THAT_ARE_NOT_THE_VALUE)
def test_a_number_must_be_stated_whole(name: str, value: int, quote: str) -> None:
    assert not supports_value(name, value, quote)


#: Quotes taken from how contracts are actually drafted, in the five languages
#: triage claims. A first version of `supports_value` required a numeric value's
#: digits to appear in its quote, and rejected a third of these -- telling the
#: reviewer, in each case, that the model had invented the value.
HONEST_PHRASING = [
    ("liability_cap", 500_000.0, "shall not exceed five hundred thousand euros"),
    ("payment_terms_days", 30, "Payment is due net thirty days from receipt"),
    ("termination_notice_days", 90, "upon ninety days prior written notice"),
    ("payment_terms_days", 45, "в срок от четиридесет и пет дни"),
    ("term_months", 36, "für eine Laufzeit von sechsunddreißig Monaten"),
    ("payment_terms_days", 60, "en un plazo de sesenta días naturales"),
    ("payment_terms_days", 45, "dans un délai de quarante-cinq jours"),
    ("term_months", 12, "Die Laufzeit beträgt zwölf Monate"),
    ("term_months", 24, "eine Laufzeit von vierundzwanzig Monaten"),
    ("term_months", 12, "El plazo inicial será de doce meses"),
    ("payment_terms_days", 15, "Le paiement intervient sous quinze jours"),
    ("liability_cap", 500_000.0, "shall not exceed EUR 500,000"),
    ("counterparty_registration_id", "HRB84421", "eingetragen HRB 84421 in Hamburg"),
    ("counterparty_registration_id", "851402336", "sous le numéro 851 402 336"),
    ("governing_law", "England & Wales", "governed by the laws of England and Wales"),
    ("signatories", "I. Petrova, CFO; K. Brandt", "I. Petrova, CFO"),
]


@pytest.mark.parametrize(("name", "value", "quote"), HONEST_PHRASING)
def test_ordinary_drafting_is_not_called_a_fabrication(
    name: str, value: object, quote: str
) -> None:
    assert supports_value(name, value, quote), f"{name}={value!r} rejected on an honest quote"


#: The other half of the same test. A rule that never fires is not a rule.
FABRICATIONS = [
    ("payment_terms_days", 60, "MASTER SERVICES AGREEMENT dated"),
    ("payment_terms_days", 60, "This Agreement is entered into by and between"),
    ("payment_terms_days", 60, "Term twenty-four (24) months"),
    ("liability_cap", 9_000_000.0, "Liability capped at EUR 500,000"),
    ("counterparty_name", "Acme Holdings Ltd", "Nordwind Logistik GmbH, HRB 84421"),
    # governing_law and effective_date were briefly exempt from this check
    # altogether, which let any locatable sentence support any jurisdiction --
    # and jurisdiction is the input to a high-severity playbook check.
    ("governing_law", "Cayman Islands", "This Agreement is entered into by and between"),
    ("effective_date", "1999-01-01", "AGREEMENT dated 14 March 2026 between"),
]


@pytest.mark.parametrize(("name", "value", "quote"), FABRICATIONS)
def test_a_quote_that_supports_nothing_is_still_caught(
    name: str, value: object, quote: str
) -> None:
    assert not supports_value(name, value, quote)
