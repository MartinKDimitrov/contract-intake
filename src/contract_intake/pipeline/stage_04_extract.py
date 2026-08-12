"""Stage 04 -- Extract.

WHAT     Pull the commercial terms out of the document as structured data, with
         a confidence and a verbatim source quote attached to every field.
IN       Status.LOADED
OUT      Status.EXTRACTED
TOKENS   LLM. One call, structured output, no tools, effort=high.
FAILS    model refusal, truncation at max_tokens, schema validation failure,
         a quote that does not actually occur in the document, timeout, 429.
DEPENDS  extract/schema.py, extract/prompts/, llm/client.py

Two decisions shape this stage.

*Provenance is not optional.* Every field carries ``confidence``, ``source_quote``
and ``page``. A quote that cannot be found in the loaded text fails validation
and drags the field's confidence to zero -- which is the difference between a
system that knows and a system that guessed. It is also the only honest input
to the routing rules in stage 06.

*No tools here.* Extraction is a single deterministic call so that its accuracy
can be measured on its own in evals/, without agent non-determinism in the way.
Validation and enrichment are stage 05's job. Fusing the two would make neither
measurable -- and two of the five review criteria are exactly those measurements.

Cost: the system prompt and the JSON schema form a stable cached prefix, so from
the second document onwards that portion bills at roughly a tenth of the rate.

Implemented in phase 2.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class ExtractStage:
    number: ClassVar[int] = 4
    name: ClassVar[str] = "extract"
    consumes: ClassVar[Status] = Status.LOADED
    produces: ClassVar[Status] = Status.EXTRACTED
    uses_llm: ClassVar[bool] = True

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 2")
