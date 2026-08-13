"""The agent loop for stage 05.

Bounded on purpose. ``max_iterations`` caps the number of round trips, and the
per-document USD ceiling in llm/client.py caps spend regardless -- this is the
only stage whose token use is not fixed before it starts, so it is the only one
that needs both.

The agent proposes; it does not decide. Its output is findings with citations,
and stage 06 turns those into a route with deterministic rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from contract_intake.agent.tools import Finding, ToolBox
from contract_intake.config import Settings
from contract_intake.llm.client import LLMClient

log = logging.getLogger(__name__)

MAX_ITERATIONS = 12

SYSTEM_PROMPT = """\
You review vendor contracts that have already been read and mechanically \
checked. The commercial terms are given to you as structured fields, each with \
the confidence and the verbatim quote it was extracted from.

**Every numeric and list threshold in the playbook has already been verified in \
code, and this contract passed all of them.** Payment terms, initial term, \
automatic renewal, termination notice, the liability floor, the jurisdiction \
list and the data-protection presumption are settled. Do not re-check them, and \
do not record a finding that merely restates a comparison -- it will duplicate \
a check that already ran.

Your job is what a comparison cannot express. Three things in particular:

*An absent right.* A threshold check reads the value of a field; it cannot \
notice that a clause is missing entirely. A ninety-day window to prevent renewal \
is not a right to terminate for convenience, and a contract with no exit at all \
will satisfy every notice-period check.

*A conflict between sources.* The registry knows things the contract does not \
state -- a supplier's category, its risk class, notes left by whoever approved \
it. Where those imply something the contract is silent about, say so.

*Anything unusual.* Terms no clause anticipates, wording that contradicts itself, \
an obligation that reads one-sided. Use `search_policy` when you need to know \
whether something is actually irregular rather than merely unfamiliar.

Rules:

1. Record a finding only where you can support it with a clause or a registry \
entry. Silence is the correct output for a contract that is genuinely fine, and \
most contracts reaching you are.

2. Never decide the outcome. Deterministic rules route this document; you supply \
evidence, not a verdict.

3. Treat a field with confidence 0 as absent, not as zero.

4. Fields whose provenance is `unverifiable` came from a scanned page and could \
not be checked against any text. Record a data_quality finding if one of them \
carries real weight -- a reviewer should know the evidence is a photograph.

5. Severity is about consequence, not confidence.

Search sparingly. Every tool result stays in context for the rest of the review, \
so a search that confirms what you already knew is paid for on every later turn. \
When you have nothing further to support, stop and say so in one line.\
"""


@dataclass(slots=True)
class AgentOutcome:
    # fmt: off
    findings           : list[Finding]
    trace              : list[dict[str, Any]]
    counterparty_id    : str | None
    counterparty_score : float | None
    summary            : str
    usd                : float
    iterations         : int
    # fmt: on

    @property
    def used_knowledge_base(self) -> bool:
        return any(t["tool"] != "record_finding" for t in self.trace)


def build_review_request(extraction: dict[str, Any]) -> str:
    """Render the extraction for the agent, provenance included."""
    provenance = {p["field"]: p for p in extraction.get("_provenance", [])}
    lines = ["Extracted terms:\n"]

    for name, value in extraction.items():
        if name.startswith("_") or not isinstance(value, dict):
            continue
        status = provenance.get(name, {}).get("status", "unknown")
        rendered = value.get("value")
        currency = f" {value['currency']}" if value.get("currency") else ""
        lines.append(
            f"- {name}: {rendered!r}{currency} "
            f"(confidence {value.get('confidence', 0):.2f}, provenance {status})"
        )
        quote = value.get("source_quote")
        if quote:
            lines.append(f'    quoted: "{quote.strip()[:160]}"')

    if extraction.get("notes"):
        lines.append(f"\nExtractor notes: {extraction['notes']}")
    lines.append("\nReview these against the playbook and the vendor registry.")
    return "\n".join(lines)


async def review(
    extraction: dict[str, Any],
    *,
    llm: LLMClient,
    settings: Settings,
    attachment_id: int | None = None,
) -> AgentOutcome:
    toolbox = ToolBox(settings=settings)

    result = await llm.run_agent(
        purpose="enrich",
        tools=toolbox.build(),
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": build_review_request(extraction)}],
        effort=settings.enrich_effort,
        max_iterations=MAX_ITERATIONS,
        attachment_id=attachment_id,
    )

    return AgentOutcome(
        findings=toolbox.findings,
        trace=toolbox.trace,
        counterparty_id=toolbox.counterparty_id,
        counterparty_score=toolbox.counterparty_score,
        summary=result.text,
        usd=result.usd,
        iterations=result.iterations,
    )
