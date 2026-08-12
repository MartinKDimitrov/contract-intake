"""Stage 03 -- Load.

WHAT     Turn a file into pages, deciding *per page* between cheap text and
         expensive vision.
IN       Status.TRIAGED
OUT      Status.LOADED
TOKENS   0 -- but this stage determines most of stage 04's bill.
FAILS    page that renders but yields no text and no image, pathological page
         count, embedded fonts that defeat text extraction, rasterisation OOM.
DEPENDS  loaders/pdf.py, loaders/image.py

This is the single biggest cost lever in the system, so it gets its own stage
rather than hiding inside extraction:

  * A 20-page contract sent as text is roughly 15k tokens. The same contract
    sent as page images is roughly 20 x 4.8k = 95k tokens -- about 6x more.
  * The decision is per page, not per document. A born-digital contract with
    one scanned signature page sends 19 pages of text and exactly one image,
    instead of 20 images.
  * Image pages are downsampled to the smallest size at which the model still
    reads them reliably, which is measured in evals/ rather than assumed.

No OCR engine. Pages without a text layer go to the model as images and Claude
reads them directly. That removes a system-level dependency (tesseract) from
the reviewer's setup and, on noisy scans, reads better than OCR-then-text.
Recorded in docs/TRADEOFFS.md.

Implemented in phase 2.
"""

from __future__ import annotations

from typing import ClassVar

from contract_intake.pipeline.base import StageContext, StageOutcome
from contract_intake.status import Status


class LoadStage:
    number: ClassVar[int] = 3
    name: ClassVar[str] = "load"
    consumes: ClassVar[Status] = Status.TRIAGED
    produces: ClassVar[Status] = Status.LOADED
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: StageContext) -> StageOutcome:
        raise NotImplementedError("phase 2")
