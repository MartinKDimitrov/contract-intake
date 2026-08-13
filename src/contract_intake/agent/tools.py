"""The agent's tools, and the trace they leave behind.

Three tools, and the split between them is the argument of the whole project:

* ``resolve_counterparty`` answers a lexical question against a closed registry.
* ``search_policy`` answers a semantic question against prose the model has
  never seen and could not have derived.
* ``record_finding`` is how the agent hands evidence back. It does not decide
  anything -- stage 06 does -- so a finding must carry a citation a human can
  check, not a verdict.

Every call is appended to a trace that is persisted and shown in the review UI.
That trace is the evidence that the knowledge base changed the outcome rather
than decorating it: if the agent never called a tool, the reviewer can see that
too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from anthropic import beta_async_tool

from contract_intake.config import Settings
from contract_intake.knowledge.policy import get_index
from contract_intake.knowledge.vendors import resolve

log = logging.getLogger(__name__)

FindingKind = Literal["policy_deviation", "counterparty", "data_quality"]
Severity = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    severity: Severity
    field_name: str
    citation: str
    explanation: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "field": self.field_name,
            "citation": self.citation,
            "explanation": self.explanation,
        }


@dataclass
class ToolBox:
    """Holds the agent's dependencies, its findings and its trace."""

    settings: Settings
    findings: list[Finding] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    counterparty_id: str | None = None
    counterparty_score: float | None = None

    def _record(self, tool: str, request: dict[str, Any], response: Any) -> None:
        self.trace.append({"tool": tool, "input": request, "output": response})

    # -- the tools ----------------------------------------------------------

    def build(self) -> list[Any]:
        """Bind the tools to this toolbox and hand them to the runner."""

        @beta_async_tool
        async def resolve_counterparty(name: str, registration_id: str = "") -> str:
            """Look a counterparty up in the approved vendor registry.

            Matches on the registration number when one is given and recognised,
            otherwise on the name -- tolerating a different legal form, a
            misspelling, or a reversed word order. Returns the registry entry, or
            the near misses when nothing matched well enough to be safe.

            Args:
                name: The counterparty's name exactly as it appears in the contract.
                registration_id: Company number, UIC, HRB or VAT id, if stated.
            """
            match = resolve(
                name,
                registration_id=registration_id or None,
                threshold=self.settings.min_vendor_match,
            )
            if match.vendor is None:
                payload = {
                    "resolved": False,
                    "score": round(match.score, 3),
                    "reason": match.reason,
                    "near_misses": [{"name": n, "score": round(s, 3)} for n, s in match.runners_up],
                }
            else:
                self.counterparty_id = match.vendor.id
                self.counterparty_score = match.score
                payload = {
                    "resolved": True,
                    "vendor_id": match.vendor.id,
                    "legal_name": match.vendor.legal_name,
                    "registration_id": match.vendor.registration_id,
                    "country": match.vendor.country,
                    "category": match.vendor.category,
                    "risk_class": match.vendor.risk_class,
                    "status": match.vendor.status,
                    "notes": match.vendor.notes,
                    "score": round(match.score, 3),
                    "matched_on": match.matched_on,
                }

            self._record(
                "resolve_counterparty", {"name": name, "registration_id": registration_id}, payload
            )
            return _dump(payload)

        @beta_async_tool
        async def search_policy(question: str) -> str:
            """Search the internal contracting playbook.

            Use this for anything the contract cannot tell you: whether a term is
            acceptable, what the threshold is, which jurisdictions are approved.
            Returns the governing clauses with their section numbers, which you
            must cite in any finding.

            Args:
                question: What you need to know, phrased as the contract phrases
                    it -- for example "payment terms are 90 days from invoice".
            """
            hits = get_index(self.settings.chroma_dir).search(question, k=3)
            # Every hit carries its rule, trimmed rather than dropped.
            #
            # An earlier version returned the body of the top hit only, to keep
            # the result small -- the runner resends every tool result on every
            # later turn, so verbosity is paid for repeatedly. It cost a real
            # deviation: for "renews automatically", S2.1 Initial term scored
            # 0.475 and S2.2 Automatic renewal scored 0.474, so the agent was
            # handed the wrong clause in full and the right one as a bare title.
            # A title names the topic; only the body carries the rule.
            #
            # Retrieval at these margins is not reliable enough to decide which
            # hit is worth reading. Trimming is the safe economy; dropping is not.
            payload = [
                {
                    "section": h.clause.section,
                    "title": h.clause.title,
                    "score": round(h.score, 3),
                    "text": _trim(h.clause.body),
                }
                for h in hits
            ]
            self._record("search_policy", {"question": question}, payload)
            return _dump(payload)

        @beta_async_tool
        async def record_finding(
            kind: FindingKind,
            severity: Severity,
            field_name: str,
            citation: str,
            explanation: str,
        ) -> str:
            """Record one thing a human should know. Call once per finding.

            You are not deciding the outcome -- deterministic rules do that. Your
            job is evidence: what deviates, from which clause, and why. A finding
            without a citation is not usable, so always search the playbook or
            resolve the counterparty first.

            Args:
                kind: policy_deviation, counterparty, or data_quality.
                severity: low, medium or high.
                field_name: The extracted field this concerns, or "document".
                citation: The playbook section (e.g. "S4.1") or vendor id
                    (e.g. "VEN-0142") this rests on.
                explanation: One or two sentences a reviewer can act on.
            """
            finding = Finding(
                kind=kind,
                severity=severity,
                field_name=field_name,
                citation=citation,
                explanation=explanation.strip(),
            )
            self.findings.append(finding)
            self._record("record_finding", finding.to_json(), {"recorded": True})
            return f"recorded finding {len(self.findings)}"

        return [resolve_counterparty, search_policy, record_finding]


#: Playbook clauses state their rule in the first sentences; the rest is
#: rationale a reviewer wants and the agent does not.
MAX_CLAUSE_CHARS = 420


def _trim(body: str) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= MAX_CLAUSE_CHARS:
        return collapsed
    return collapsed[:MAX_CLAUSE_CHARS].rsplit(" ", 1)[0] + " [...]"


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=None)
