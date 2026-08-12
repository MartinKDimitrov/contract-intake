"""Stage 02 -- Triage.

WHAT     Decide whether this file is worth spending a model call on.
IN       Status.RECEIVED
OUT      Status.TRIAGED, or Status.REJECTED for anything that is not a contract.
TOKENS   0 for the typical document. Heuristics only.
FAILS    zero-byte file, truncated/corrupt PDF, password-protected PDF,
         declared MIME lying about content, absurd page count, oversized file.
DEPENDS  loaders/pdf.py (metadata only -- no text extraction yet)

Why no LLM here: the cheapest token is the one never sent. Magic bytes, size,
encryption flag, page count and a keyword scan of page 1 settle the vast
majority of inputs for free. Only a genuinely ambiguous document -- a PDF that
parses, is document-shaped, but matches no contract vocabulary -- is escalated
to a single low-effort classification call. That escalation is a phase-6
addition; phase 2 ships the heuristics alone.

Implemented in phase 1.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class TriageStage:
    number: ClassVar[int] = 2
    name: ClassVar[str] = "triage"
    consumes: ClassVar[Status] = Status.RECEIVED
    produces: ClassVar[Status] = Status.TRIAGED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 1")
