"""Stage 07 -- Deliver.

WHAT     Act on the decision: store the clean record, or open a review item.
IN       Status.DECIDED
OUT      Status.DELIVERED
TOKENS   0
FAILS    write conflict, notification target unreachable (non-fatal), attempting
         to deliver the same decision twice.
DEPENDS  store/, web/review.py

AUTO_APPROVED  -> a row in ``contracts``: the clean, high-confidence record a
                  downstream system would consume.
NEEDS_REVIEW   -> a row in ``review_items``: appears in the review queue with
                  the extracted fields, each field's source quote, the reasons
                  the router flagged it, and the agent's tool trace.
REJECTED       -> recorded with its reason; no downstream artefact.

Delivery is idempotent on ``decision_id`` -- replaying the stage cannot produce
a duplicate contract or a second review item.

Notification is deliberately last and deliberately non-fatal: a Slack webhook
that is down must not strand a document that was otherwise processed correctly.

Implemented in phase 5.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class DeliverStage:
    number: ClassVar[int] = 7
    name: ClassVar[str] = "deliver"
    consumes: ClassVar[Status] = Status.DECIDED
    produces: ClassVar[Status] = Status.DELIVERED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 5")
