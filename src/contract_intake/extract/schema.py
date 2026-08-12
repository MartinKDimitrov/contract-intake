"""What gets extracted, and the evidence that has to come with it.

Every field carries four things, not one:

    value          what the model read
    confidence     how sure it is, 0..1
    source_quote   the verbatim words it read it from
    page           where on the document those words are

The quote is the load-bearing part. Without it there is no way to tell a
correct answer from a plausible one, and no honest input to the routing rules
in stage 06 -- "payment terms are 90 days" is not actionable, but "payment
terms are 90 days, from 'settle all invoices within ninety (90) days' on page
1" is. A quote that cannot be found in the document is treated as a failure of
that field rather than a cosmetic flaw; see extractor.verify_provenance.

A field the document does not contain must come back with ``value: null`` and
``confidence: 0``. Guessing a plausible liability cap is worse than admitting
there isn't one, because a guess routes to auto-approval and a null routes to a
human.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class Evidence(BaseModel):
    """Provenance shared by every extracted field."""

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "0 if the document does not state this. Do not raise confidence for a "
            "value you inferred rather than read."
        ),
    )
    source_quote: str | None = Field(
        default=None,
        description=(
            "The exact words from the document this value was read from, copied "
            "verbatim. Null only when the value is null."
        ),
    )
    page: int | None = Field(default=None, description="1-based page the quote appears on.")


class TextField(Evidence):
    value: str | None = None


class IntField(Evidence):
    value: int | None = None


class BoolField(Evidence):
    value: bool | None = None


class MoneyField(Evidence):
    value: float | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code, e.g. EUR.")


class ContractExtraction(BaseModel):
    """The commercial terms a contracts team actually needs off a new agreement."""

    document_kind: Literal["contract", "amendment", "order_form", "other"] = Field(
        description="What this document is. 'other' if it is not a contract at all."
    )

    counterparty_name: TextField = Field(
        description="The other party's full legal name, including its legal form."
    )
    counterparty_registration_id: TextField = Field(
        description="Company registration number, UIC, HRB, VAT id -- whatever is stated."
    )
    customer_name: TextField = Field(description="Our side of the agreement.")

    effective_date: TextField = Field(description="ISO 8601 (YYYY-MM-DD) if determinable.")
    term_months: IntField = Field(description="Initial term length in months.")
    auto_renewal: BoolField = Field(
        description="True only if the document states the term renews without action."
    )
    termination_notice_days: IntField = Field(
        description="Notice period for termination for convenience, in days."
    )

    payment_terms_days: IntField = Field(description="Days to pay an undisputed invoice.")
    liability_cap: MoneyField = Field(description="Aggregate cap on liability.")
    liability_uncapped: BoolField = Field(
        description="True if the document excludes liability entirely or states no cap."
    )

    governing_law: TextField = Field(description="Governing jurisdiction as stated.")
    dpa_present: BoolField = Field(
        description="True if a data processing agreement is referenced or attached."
    )
    signatories: TextField = Field(description="Signing names and titles, comma separated.")

    notes: str = Field(
        default="",
        description=(
            "Anything a reviewer should know that the fields above cannot carry: "
            "contradictions, illegible passages, unusual clauses. Empty if none."
        ),
    )

    def evidence_fields(self) -> dict[str, Evidence]:
        """Every field that carries provenance, by name."""
        return {
            name: value
            for name, value in ((n, getattr(self, n)) for n in type(self).model_fields)
            if isinstance(value, Evidence)
        }


#: Fields a contract must have before it can be approved without a human. A null
#: here is not a model failure -- it is exactly the signal review exists for.
REQUIRED_FOR_AUTO_APPROVAL: tuple[str, ...] = (
    "counterparty_name",
    "effective_date",
    "term_months",
    "payment_terms_days",
    "governing_law",
)
