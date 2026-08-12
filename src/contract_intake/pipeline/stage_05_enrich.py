"""Stage 05 -- Enrich.

WHAT     Resolve the counterparty against the vendor registry and check the
         extracted terms against the internal contracting playbook.
IN       Status.EXTRACTED
OUT      Status.ENRICHED
TOKENS   LLM agent loop, effort=medium, bounded by max_iterations and a task
         budget. This is the only stage whose token use is not fixed up front.
FAILS    agent loops without converging, tool returns nothing, KB index missing,
         model refusal, budget exhausted mid-loop.
DEPENDS  agent/tools.py, agent/runner.py, knowledge/vendors.py, knowledge/policy.py

This is where the knowledge base has to earn its place, and it does two jobs
that need two different retrieval methods:

  1. *Entity resolution.* "NordWind Logistics Ltd." on a crooked scan against a
     registry holding "Nordwind Logistik GmbH". Fuzzy string matching beats
     embeddings for company names, so ``resolve_counterparty`` uses rapidfuzz
     with an embedding fallback.
  2. *Policy validation.* "payment terms: 90 days" against a playbook whose
     ceiling is 45. This is semantic, so ``search_policy`` uses dense retrieval
     and returns the clause with its section number.

The second job is the one the model cannot do alone: no amount of reasoning
tells it what *this company's* liability ceiling is. That is the honest test of
whether RAG improves the result or decorates it, and evals/ runs the agent with
and without KB access to put a number on it.

The agent proposes findings; it does not decide. Routing is stage 06.

The full tool trace is persisted and rendered in the review UI, so a human can
see which clause drove which finding.

Implemented in phase 4.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class EnrichStage:
    number: ClassVar[int] = 5
    name: ClassVar[str] = "enrich"
    consumes: ClassVar[Status] = Status.EXTRACTED
    produces: ClassVar[Status] = Status.ENRICHED
    uses_llm: ClassVar[bool] = True

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 4")
