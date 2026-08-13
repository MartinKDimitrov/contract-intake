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
        entry = extraction.get(check.field)
        value = entry.get("value") if isinstance(entry, dict) else None
        if _passes(check, value):
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


def _passes(check: Check, value: Any) -> bool:
    p = check.params

    match check.op:
        case "required":
            return value is not None
        case "equals":
            # A field the document does not state cannot equal anything, so an
            # absent value passes by default -- absence is caught by a `required`
            # check where it matters. `absent_fails` inverts that for the cases
            # where silence *is* the deviation: a contract with no data-protection
            # clause has not satisfied the requirement, it has ignored it.
            if value is None:
                return not p.get("absent_fails", False)
            return bool(value == p["expected"])
        case "between":
            return value is None or _number(value) is None or p["min"] <= _number(value) <= p["max"]
        case "lte":
            return value is None or _number(value) is None or _number(value) <= p["limit"]
        case "gte":
            return value is None or _number(value) is None or _number(value) >= p["limit"]
        case "matches_any":
            if value is None:
                return True
            text = str(value).casefold()
            return any(a in text for a in p["allowed"])
        case "matches_none":
            if value is None:
                return True
            text = str(value).casefold()
            return not any(f in text for f in p["forbidden"])
        case _:
            log.warning("unknown check operator %r in %s; skipping", check.op, check.id)
            return True


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
