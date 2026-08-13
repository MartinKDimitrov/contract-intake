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
from contract_intake.extract.schema import DECISION_BEARING, REQUIRED_FOR_AUTO_APPROVAL
from contract_intake.status import Route

RULES_VERSION = 1


@dataclass(frozen=True, slots=True)
class Reason:
    """One rule firing, with the evidence that made it fire."""

    # fmt: off
    rule     : str
    message  : str
    citation : str             = ""
    fields   : tuple[str, ...] = ()
    blocking : bool            = True
    # fmt: on

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
    # fmt: off
    route           : Route
    reasons         : tuple[Reason, ...]
    blocking_fields : tuple[str, ...]
    # fmt: on

    @property
    def auto_approved(self) -> bool:
        return self.route is Route.AUTO_APPROVED


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the rules are allowed to see."""

    # fmt: off
    extraction          : dict[str, Any]
    findings            : Sequence[dict[str, Any]] = ()
    counterparty_id     : str | None               = None
    counterparty_score  : float | None             = None
    counterparty_status : str                      = "unknown"
    # fmt: on

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
# Each takes the evidence and returns the reasons it found, or nothing. Every
# rule runs, so a reviewer sees all the problems at once rather than the first.
#
# Order here is execution order and nothing else. The queue sorts by its own
# table in web/review.py:_reason_weight, which a test keeps in step with the
# rule names emitted below -- two orderings of one list, in two packages, and
# this comment used to claim they were the same one.


#: The severities the agent is allowed to emit. Anything outside this set is
#: treated as blocking: the model picks the label, and a typo, a new word or a
#: capitalisation used to mean the finding was recorded and then ignored.
KNOWN_SEVERITIES = frozenset({"high", "medium", "low"})


def rule_high_severity_findings(ev: Evidence, _s: Settings) -> list[Reason]:
    """High findings, plus anything whose severity this code does not recognise."""
    return [
        Reason(
            rule="high_severity_finding",
            message=f"{f.get('field', 'document')}: {f.get('explanation', '')}".strip(),
            citation=str(f.get("citation", "")),
            fields=(str(f.get("field", "")),) if f.get("field") else (),
        )
        for f in ev.findings
        if str(f.get("severity", "")).strip().casefold() not in KNOWN_SEVERITIES - {"high"}
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
        if str(f.get("severity", "")).strip().casefold() == "medium"
    ]


def rule_low_severity_findings(ev: Evidence, _s: Settings) -> list[Reason]:
    """Low findings block as well; severity only orders the queue.

    The agent runs only on documents where every deterministic check already
    passed, so anything it reports is by construction a judgement the code could
    not make. Letting the model mark such a thing "low" and have it disappear
    hands the routing decision to the word it chose, which is exactly what the
    system prompt tells it that it is not doing.
    """
    return [
        Reason(
            rule="low_severity_finding",
            message=f"{f.get('field', 'document')}: {f.get('explanation', '')}".strip(),
            citation=str(f.get("citation", "")),
            fields=(str(f.get("field", "")),) if f.get("field") else (),
        )
        for f in ev.findings
        if str(f.get("severity", "")).strip().casefold() == "low"
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
    """§7.1 -- never auto-approve a supplier the registry does not call approved.

    Tested as an allow-list rather than against the literal string "suspended".
    The registry is free to grow a status -- "under_review", "terminated",
    "blocked" -- and every one of those used to auto-approve, as did a
    counterparty id that no longer resolves to any registry entry at all, which
    is what removing a vendor from the file looks like from here.
    """
    if ev.counterparty_status == "approved":
        return []
    if not ev.counterparty_id:
        # Nothing resolved, so there is no supplier to call unapproved.
        # `rule_unresolved_counterparty` owns that case; firing here as well
        # gave a reviewer two reasons for one fact, the second reading
        # "None is 'unknown' in the registry, not approved".
        return []
    return [
        Reason(
            rule="suspended_counterparty",
            message=(
                f"{ev.counterparty_id} is {ev.counterparty_status!r} in the registry, not approved"
            ),
            citation="§7.1",
            fields=("counterparty_name",),
        )
    ]


def rule_missing_required_fields(ev: Evidence, _s: Settings) -> list[Reason]:
    """A contract record with a hole in it is not a contract record.

    ARCHITECTURE.md has always said these five must be *present* before a
    document can pass without a human. Nothing enforced it. The confidence rule
    below looked like it did -- an absent value is extracted with confidence
    zero -- until a guard was added there to stop absent fields being reported
    twice, at which point an extraction with all five missing auto-approved in
    silence.

    Presence and confidence are separate questions and now have separate rules.
    """
    missing = [name for name in REQUIRED_FOR_AUTO_APPROVAL if ev.field(name).get("value") is None]
    if not missing:
        return []
    return [
        Reason(
            rule="missing_required_field",
            message=f"the document does not state: {', '.join(missing)}",
            fields=tuple(missing),
        )
    ]


def rule_low_confidence_required_fields(ev: Evidence, s: Settings) -> list[Reason]:
    """Present, but not confidently enough to decide on.

    Absent fields are skipped here and caught by the rule above, so that a
    document missing a term is reported once rather than twice.
    """
    weak = [
        name
        for name in DECISION_BEARING
        if ev.field(name).get("value") is not None and ev.confidence(name) < s.min_field_confidence
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


def rule_partially_unverifiable(ev: Evidence, _s: Settings) -> list[Reason]:
    """A decision-bearing value that nothing could check.

    The rule above only fires when *every* field is unverifiable, which one
    verified field disarms -- and the cheapest field to verify (our own name, in
    a header) is also the least load-bearing. So a mixed document could carry a
    liability cap read off a photograph, or attributed to one, straight to
    auto-approval.

    An unverifiable value is not a lie; a scan genuinely has no text layer to
    check against. It is simply not evidence, and a decision should not rest on
    it without a person seeing it.
    """
    unchecked = [name for name in DECISION_BEARING if ev.provenance.get(name) == "unverifiable"]
    if not unchecked:
        return []
    return [
        Reason(
            rule="partially_unverifiable",
            message=(
                f"{len(unchecked)} value(s) a decision rests on could not be checked "
                f"against a text layer: {', '.join(unchecked)}"
            ),
            fields=tuple(unchecked),
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
    rule_partially_unverifiable,
    rule_suspended_counterparty,
    rule_unresolved_counterparty,
    rule_high_severity_findings,
    rule_medium_severity_findings,
    rule_low_severity_findings,
    rule_unsupported_quotes,
    rule_missing_required_fields,
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
