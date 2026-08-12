"""Stage 06 -- Decide.

WHAT     Turn extraction confidence plus agent findings into a route.
IN       Status.ENRICHED
OUT      Status.DECIDED
TOKENS   0. No model call, by design.
FAILS    essentially nothing at runtime -- pure functions over persisted data.
         The failure mode here is a wrong *rule*, which is what the truth-table
         tests in tests/pipeline/test_stage_06_decide.py exist to catch.
DEPENDS  policy/rules.py

The model never decides. It extracts, and it reports findings with evidence;
this stage applies deterministic rules to that evidence and produces one of
three routes with an explicit list of reasons.

That split is the point. An LLM asked to "decide whether to auto-approve" gives
an answer that cannot be unit-tested, cannot be audited by a lawyer, and drifts
between model versions. A rule that reads "payment_terms_days > playbook ceiling
=> needs_review, citing S3.2" can be tested exhaustively and explained to a
human in one sentence.

Routes:
  AUTO_APPROVED  every required field above the confidence floor, counterparty
                 resolved above the match floor, no policy deviation.
  NEEDS_REVIEW   anything uncertain or off-policy, with the specific reason and
                 the blocking fields attached.
  REJECTED       positively established as not processable.

Implemented in phase 5.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class DecideStage:
    number: ClassVar[int] = 6
    name: ClassVar[str] = "decide"
    consumes: ClassVar[Status] = Status.ENRICHED
    produces: ClassVar[Status] = Status.DECIDED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 5")
