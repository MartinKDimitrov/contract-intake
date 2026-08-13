"""The four ways a model could once defeat provenance verification.

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
from contract_intake.extract.extractor import verify_provenance
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
