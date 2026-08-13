"""Deterministic playbook checks.

Anything expressible as a comparison is a comparison. "Is 90 within 45 to 90?"
is arithmetic, and the earlier design had a frontier model retrieve the clause,
read the numbers out of prose, and do that arithmetic -- at roughly seven cents
a document, non-deterministically, in a system whose stated principle is that
the model proposes and the code decides.

These checks close that gap. They produce the same findings the agent produced,
with the same citations, for nothing, and they can be tested exhaustively.

What stays with the agent is what a comparison cannot express: that a 90-day
non-renewal window is not a termination-for-convenience right, that an unusual
clause deserves attention, that the contract is silent about something the
registry implies it should cover. That is the judgement half, and it is the only
half worth paying for.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA = Path(__file__).parent.parent / "knowledge" / "data" / "playbook_checks.json"


class UnknownOperatorError(ValueError):
    """A check names an operator this module cannot evaluate."""


#: Leading noise that varies without changing which jurisdiction is meant.
#: Applied before comparing a governing law against the allow-list.
_JURISDICTION_NOISE = (
    "the laws of ",
    "laws of ",
    "the law of ",
    "law of ",
    "the republic of ",
    "republic of ",
    "the ",
)


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    section: str
    field: str
    op: str
    severity: str
    message: str
    params: dict[str, Any]
    applies_to_categories: tuple[str, ...] = ()

    def applies(self, vendor_category: str | None) -> bool:
        if not self.applies_to_categories:
            return True
        return (vendor_category or "") in self.applies_to_categories


@lru_cache(maxsize=1)
def load_checks(path: Path | None = None) -> tuple[Check, ...]:
    raw = json.loads((path or DATA).read_text(encoding="utf-8"))
    known = {"id", "section", "field", "op", "severity", "message", "applies_to_categories"}
    for c in raw["checks"]:
        if c["op"] not in OPERATORS:
            raise UnknownOperatorError(
                f"check {c['id']!r} uses operator {c['op']!r}, which is not implemented. "
                "An operator nobody evaluates is a check that silently passes."
            )
    return tuple(
        Check(
            id=c["id"],
            section=c["section"],
            field=c["field"],
            op=c["op"],
            severity=c.get("severity", "medium"),
            message=c["message"],
            params={k: v for k, v in c.items() if k not in known},
            applies_to_categories=tuple(c.get("applies_to_categories", ())),
        )
        for c in raw["checks"]
    )


def evaluate(
    extraction: dict[str, Any],
    *,
    vendor_category: str | None = None,
    checks: Sequence[Check] | None = None,
) -> list[dict[str, Any]]:
    """Run every applicable check. Returns findings in the agent's own shape.

    Same schema as ``agent.tools.Finding.to_json`` so stage 06 does not care
    which half of the system produced a given finding -- only that it carries a
    citation.
    """
    findings: list[dict[str, Any]] = []
    for check in checks if checks is not None else load_checks():
        if not check.applies(vendor_category):
            continue
        raw_entry = extraction.get(check.field)
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        value = entry.get("value")
        if _passes(check, entry):
            continue
        findings.append(
            {
                "kind": "policy_deviation",
                "severity": check.severity,
                "field": check.field,
                "citation": check.section,
                "explanation": _render(check, value),
                "source": "rules",
            }
        )
    return findings


#: Every operator this module implements. A check naming anything else is a
#: startup error rather than a warning, because the old behaviour -- log and
#: return True -- turned a typo in the playbook into a check that always passed.
OPERATORS = frozenset(
    {
        "required",
        "equals",
        "between",
        "lte",
        "gte",
        "matches_any",
        "matches_none",
        "currency_stated",
    }
)


def canonical_jurisdiction(value: str) -> str:
    """Reduce a stated governing law to something comparable to the allow-list.

    Substring matching used to do this job and made "New South Wales, Australia"
    an approved jurisdiction, because "wales" is in the list on account of
    England & Wales. Matching the whole canonical string instead fails closed:
    a rendering this does not recognise becomes a deviation and goes to a
    person, which is the right direction to be wrong in for a jurisdiction.
    """
    text = " ".join(str(value).casefold().split()).strip(" .,;")
    for prefix in _JURISDICTION_NOISE:
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.replace(" and ", " & ").strip()


def _passes(check: Check, entry: dict[str, Any]) -> bool:
    """Evaluate one check against one extracted field.

    Absence fails by default. It used to pass, on the reasoning that a `required`
    check would catch it -- but that reasoning was spread across two files and
    was wrong for §2.3, where a contract with no termination-for-convenience
    right at all sailed through a ceiling of 90 days. A check that cannot see a
    value has not checked it, so the two places where silence is genuinely
    acceptable now say so with `absent_ok`.
    """
    p = check.params
    value = entry.get("value")

    if value is None:
        return bool(p.get("absent_ok", False)) and not p.get("absent_fails", False)

    match check.op:
        case "required":
            return True  # a non-None value is all this asks
        case "equals":
            return bool(value == p["expected"])
        case "currency_stated":
            currency = str(entry.get("currency") or "").strip().upper()
            allowed = {str(a).upper() for a in p.get("allowed", ())}
            return bool(currency) and (not allowed or currency in allowed)
        case "between" | "lte" | "gte":
            number = _number(value)
            if number is None:
                # A value the checker cannot parse is a value it did not check.
                return False
            if check.op == "between":
                return bool(p["min"] <= number <= p["max"])
            if check.op == "lte":
                return bool(number <= p["limit"])
            return bool(number >= p["limit"])
        case "matches_any":
            allowed = {canonical_jurisdiction(a) for a in p["allowed"]}
            return canonical_jurisdiction(value) in allowed
        case "matches_none":
            text = str(value).casefold()
            return not any(str(f).casefold() in text for f in p["forbidden"])

    raise UnknownOperatorError(check.op)  # unreachable: load_checks validates


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _render(check: Check, value: Any) -> str:
    try:
        return check.message.format(value=value, **check.params)
    except (KeyError, IndexError, ValueError):
        return check.message


def cited_sections(checks: Sequence[Check] | None = None) -> set[str]:
    return {c.section for c in (checks if checks is not None else load_checks())}
