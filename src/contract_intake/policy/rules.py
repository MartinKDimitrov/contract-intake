"""The routing rules. Pure functions, no model, no I/O.

The model extracts and the agent reports; this decides. That split is the point
of the whole design: an LLM asked "should this be auto-approved?" gives an answer
that cannot be unit-tested, cannot be shown to a lawyer, and changes between
model versions. A rule that reads ``severity == high -> needs_review, citing
§4.1`` can be tested exhaustively and explained in one sentence.

Every rule returns a ``Reason`` carrying its own name and the evidence it acted
on, so a review item never says "low confidence" -- it says which field, which
clause, and what the threshold was.

The default is caution. Auto-approval requires every rule to stay silent; any
one of them firing sends the document to a human.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from contract_intake.config import Settings
from contract_intake.extract.schema import REQUIRED_FOR_AUTO_APPROVAL
from contract_intake.status import Route

RULES_VERSION = 1


@dataclass(frozen=True, slots=True)
class Reason:
    """One rule firing, with the evidence that made it fire."""

    rule: str
    message: str
    citation: str = ""
    fields: tuple[str, ...] = ()
    blocking: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "citation": self.citation,
            "fields": list(self.fields),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    route: Route
    reasons: tuple[Reason, ...]
    blocking_fields: tuple[str, ...]

    @property
    def auto_approved(self) -> bool:
        return self.route is Route.AUTO_APPROVED


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the rules are allowed to see."""

    extraction: dict[str, Any]
    findings: Sequence[dict[str, Any]] = ()
    counterparty_id: str | None = None
    counterparty_score: float | None = None
    counterparty_status: str = "unknown"

    def field(self, name: str) -> dict[str, Any]:
        value = self.extraction.get(name)
        return value if isinstance(value, dict) else {}

    def confidence(self, name: str) -> float:
        return float(self.field(name).get("confidence") or 0.0)

    def value(self, name: str) -> Any:
        return self.field(name).get("value")

    @property
    def provenance(self) -> dict[str, str]:
        return {
            p["field"]: p["status"] for p in self.extraction.get("_provenance", []) if "field" in p
        }


# -- the rules --------------------------------------------------------------
#
# Each takes the evidence and returns the reasons it found, or nothing. Order in
# ALL_RULES is presentation order in the review queue, not precedence: every
# rule runs, so a reviewer sees all the problems at once rather than the first.


def rule_high_severity_findings(ev: Evidence, _s: Settings) -> list[Reason]:
    return [
        Reason(
            rule="high_severity_finding",
            message=f"{f.get('field', 'document')}: {f.get('explanation', '')}".strip(),
            citation=str(f.get("citation", "")),
            fields=(str(f.get("field", "")),) if f.get("field") else (),
        )
        for f in ev.findings
        if f.get("severity") == "high"
    ]


def rule_medium_severity_findings(ev: Evidence, _s: Settings) -> list[Reason]:
    """Medium findings block too, but are marked so the queue can sort by weight."""
    return [
        Reason(
            rule="medium_severity_finding",
            message=f"{f.get('field', 'document')}: {f.get('explanation', '')}".strip(),
            citation=str(f.get("citation", "")),
            fields=(str(f.get("field", "")),) if f.get("field") else (),
        )
        for f in ev.findings
        if f.get("severity") == "medium"
    ]


def rule_unresolved_counterparty(ev: Evidence, s: Settings) -> list[Reason]:
    """An unknown counterparty is a deviation in itself -- playbook §7.2.

    It may be a genuine new supplier, a renamed entity, or an impersonation, and
    the contract alone cannot tell those apart.
    """
    if ev.counterparty_id:
        return []
    score = ev.counterparty_score or 0.0
    return [
        Reason(
            rule="unresolved_counterparty",
            message=(
                f"counterparty did not resolve to a registry entry "
                f"(best match {score:.2f}, threshold {s.min_vendor_match:.2f})"
            ),
            citation="§7.2",
            fields=("counterparty_name",),
        )
    ]


def rule_suspended_counterparty(ev: Evidence, _s: Settings) -> list[Reason]:
    """§7.1 -- never auto-approve a suspended supplier, whatever the terms."""
    if ev.counterparty_status != "suspended":
        return []
    return [
        Reason(
            rule="suspended_counterparty",
            message=f"{ev.counterparty_id} is suspended in the registry",
            citation="§7.1",
            fields=("counterparty_name",),
        )
    ]


def rule_low_confidence_required_fields(ev: Evidence, s: Settings) -> list[Reason]:
    weak = [
        name for name in REQUIRED_FOR_AUTO_APPROVAL if ev.confidence(name) < s.min_field_confidence
    ]
    if not weak:
        return []
    return [
        Reason(
            rule="low_confidence_required_field",
            message=(
                f"{len(weak)} required field(s) below the {s.min_field_confidence:.2f} "
                f"confidence floor: {', '.join(weak)}"
            ),
            fields=tuple(weak),
        )
    ]


def rule_unsupported_quotes(ev: Evidence, _s: Settings) -> list[Reason]:
    """A quote that is not in the document means the field was invented.

    Extraction already drove those to zero confidence, so the rule above would
    catch a required one. This exists to name the failure explicitly, because
    "the model made this up" and "the model was unsure" are different problems
    and a reviewer should not have to guess which happened.
    """
    invented = [name for name, status in ev.provenance.items() if status == "not_found"]
    if not invented:
        return []
    return [
        Reason(
            rule="unsupported_quote",
            message=(
                f"{len(invented)} field(s) quoted text that is not in the document: "
                f"{', '.join(invented)}"
            ),
            fields=tuple(invented),
        )
    ]


def rule_wholly_unverifiable(ev: Evidence, _s: Settings) -> list[Reason]:
    """A document whose every claim rests on a photograph.

    This is the rule that would be easiest to leave out and worst to leave out.
    A clean scan can satisfy every commercial threshold and still deserve a human,
    because nothing in it was checked against anything -- the text layer that
    verification needs does not exist. Auto-approving it would be exactly the
    kind of quiet pass this system is meant to prevent.
    """
    statuses = set(ev.provenance.values()) - {"absent"}
    if not statuses or statuses != {"unverifiable"}:
        return []
    return [
        Reason(
            rule="wholly_unverifiable",
            message=(
                "every extracted value came from a scanned page with no text layer, "
                "so no quote could be checked against the document"
            ),
        )
    ]


def rule_not_a_contract(ev: Evidence, _s: Settings) -> list[Reason]:
    kind = ev.extraction.get("document_kind")
    if kind in (None, "contract", "amendment", "order_form"):
        return []
    return [
        Reason(
            rule="not_a_contract",
            message=f"the model reads this document as {kind!r}",
        )
    ]


ALL_RULES: tuple[Callable[[Evidence, Settings], list[Reason]], ...] = (
    rule_not_a_contract,
    rule_suspended_counterparty,
    rule_unresolved_counterparty,
    rule_high_severity_findings,
    rule_medium_severity_findings,
    rule_unsupported_quotes,
    rule_low_confidence_required_fields,
    rule_wholly_unverifiable,
)


def decide(evidence: Evidence, settings: Settings) -> Decision:
    """Run every rule. Silence means auto-approval; anything else means a human."""
    reasons: list[Reason] = []
    for rule in ALL_RULES:
        reasons.extend(rule(evidence, settings))

    if not reasons:
        return Decision(route=Route.AUTO_APPROVED, reasons=(), blocking_fields=())

    if any(r.rule == "not_a_contract" for r in reasons):
        route = Route.REJECTED
    else:
        route = Route.NEEDS_REVIEW

    blocking = tuple(dict.fromkeys(f for r in reasons if r.blocking for f in r.fields if f))
    return Decision(route=route, reasons=tuple(reasons), blocking_fields=blocking)
