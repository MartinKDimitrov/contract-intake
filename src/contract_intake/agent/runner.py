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
You review vendor contracts that have already been read. The commercial terms \
are given to you as structured fields, each with the confidence and the verbatim \
quote it was extracted from. Your job is to check them against what this company \
actually accepts, and to leave a human enough evidence to act.

You have three tools. Use them; do not answer from memory.

`resolve_counterparty` tells you whether the other party is a known, approved \
supplier. Always call it. A contract with an unresolved counterparty is not \
automatically wrong, but nobody can tell a genuine new supplier from a renamed \
entity or an impersonation by reading the contract, so it must be recorded.

`search_policy` tells you what is acceptable. Nothing in a contract states the \
company's own thresholds, so any judgement about whether a term is acceptable \
must rest on a clause you retrieved. Search for each commercial term \
separately -- payment terms, renewal, termination notice, liability cap, \
governing law, data protection -- phrased the way the contract phrases it. One \
search per term is enough; if a result did not answer your question, a reworded \
search of the same clause will not either. Every tool result stays in context for \
the rest of the review, so a redundant search is paid for on every later turn.

`record_finding` is how you hand evidence back. Call it once per issue, with the \
section or vendor id it rests on. A finding with no citation is unusable.

Rules:

1. Record a finding for every deviation you can support with a clause. Do not \
record one for a term that complies -- silence means compliant.

2. Never decide the outcome. Do not write that a contract should be approved or \
rejected; deterministic rules do that from your findings. Report what deviates \
and from what.

3. Treat a field with confidence 0 as absent, not as zero. A missing liability \
cap and a cap of zero are different facts, and the playbook may cover the \
absence explicitly.

4. Fields whose provenance is `unverifiable` came from a scanned page and could \
not be checked against any text. Use them, but record a data_quality finding if \
one of them drives a deviation -- a reviewer should know the evidence is a \
photograph.

5. Severity is about consequence, not confidence. An offshore governing law is \
high whatever the confidence; a missing signatory name is low.

When you have recorded everything you can support, stop and write one short \
paragraph summarising what you found.\
"""


@dataclass(slots=True)
class AgentOutcome:
    findings: list[Finding]
    trace: list[dict[str, Any]]
    counterparty_id: str | None
    counterparty_score: float | None
    summary: str
    usd: float
    iterations: int

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
