"""Stage 04's engine: one structured call, then provenance verification.

The verification pass is what separates this from a prompt that asks nicely for
citations. Every quote the model returns is looked for in the text the document
actually contains; a quote that is not there means the field was invented, and
the field's confidence is driven to zero regardless of what the model claimed.

Quotes on pages that reached the model as images cannot be checked this way --
there is no text to check against. Those are marked unverifiable rather than
silently trusted or silently failed, and the routing rules treat the two
differently.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from contract_intake.config import Effort, Settings
from contract_intake.extract.prompts import SYSTEM_PROMPT, user_instruction
from contract_intake.extract.schema import ContractExtraction, Evidence
from contract_intake.llm.client import LLMClient, LLMResult
from contract_intake.loaders.document import Document, page_content_blocks

log = logging.getLogger(__name__)

#: A quote shorter than this is too generic to locate meaningfully ("30 days").
MIN_VERIFIABLE_QUOTE_CHARS = 12


@dataclass(frozen=True, slots=True)
class FieldVerdict:
    name: str
    status: str  # verified | unverifiable | not_found | absent
    confidence: float
    note: str = ""


@dataclass(slots=True)
class ExtractionOutcome:
    extraction: ContractExtraction
    verdicts: list[FieldVerdict] = field(default_factory=list)
    usd: float = 0.0
    latency_ms: int = 0

    @property
    def hallucinated(self) -> list[str]:
        return [v.name for v in self.verdicts if v.status == "not_found"]

    @property
    def verified(self) -> list[str]:
        return [v.name for v in self.verdicts if v.status == "verified"]

    def to_json(self) -> dict[str, Any]:
        payload = self.extraction.model_dump()
        payload["_provenance"] = [
            {"field": v.name, "status": v.status, "confidence": v.confidence, "note": v.note}
            for v in self.verdicts
        ]
        return payload


async def extract(
    document: Document,
    *,
    llm: LLMClient,
    settings: Settings,
    attachment_id: int | None = None,
    effort: Effort | None = None,
) -> ExtractionOutcome:
    """One call, one schema, then verify what came back."""
    blocks = page_content_blocks(document)
    blocks.append({"type": "text", "text": user_instruction(document.page_count)})

    result: LLMResult[ContractExtraction] = await llm.parse(
        purpose="extract",
        schema=ContractExtraction,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": blocks}],
        effort=effort or settings.extract_effort,
        attachment_id=attachment_id,
    )

    verdicts = verify_provenance(result.value, document)
    return ExtractionOutcome(
        extraction=result.value,
        verdicts=verdicts,
        usd=result.usd,
        latency_ms=result.latency_ms,
    )


def verify_provenance(extraction: ContractExtraction, document: Document) -> list[FieldVerdict]:
    """Check every quote against the document, and zero the fields that fail."""
    haystack = _normalise(document.all_text)
    image_pages = {p.number for p in document.pages if p.kind == "image"}
    verdicts: list[FieldVerdict] = []

    for name, evidence in extraction.evidence_fields().items():
        verdicts.append(_verify_one(name, evidence, haystack, image_pages))
    return verdicts


def _verify_one(
    name: str,
    evidence: Evidence,
    haystack: str,
    image_pages: set[int],
) -> FieldVerdict:
    value = getattr(evidence, "value", None)

    if value is None:
        # Nothing claimed. Confidence should already be 0; make sure of it.
        evidence.confidence = 0.0
        return FieldVerdict(name, "absent", 0.0, "not stated in the document")

    quote = (evidence.source_quote or "").strip()
    if not quote:
        evidence.confidence = 0.0
        return FieldVerdict(name, "not_found", 0.0, "value given with no supporting quote")

    if evidence.page in image_pages or not haystack:
        return FieldVerdict(
            name,
            "unverifiable",
            evidence.confidence,
            "quote is on a scanned page; no text layer to check against",
        )

    if _normalise(quote) in haystack:
        return FieldVerdict(name, "verified", evidence.confidence)

    if len(quote) < MIN_VERIFIABLE_QUOTE_CHARS:
        return FieldVerdict(name, "unverifiable", evidence.confidence, "quote too short to locate")

    previous = evidence.confidence
    evidence.confidence = 0.0
    return FieldVerdict(
        name,
        "not_found",
        0.0,
        f"quote not present in the document (model claimed {previous:.2f})",
    )


def _normalise(text: str) -> str:
    """Collapse the differences that do not change what a quote says.

    PDF text extraction inserts line breaks mid-sentence, uses non-breaking
    spaces and typographic dashes, and varies in case. A quote failing over any
    of those would make verification useless noise.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = folded.replace("\u2019", "'").replace("\u2018", "'")
    folded = folded.replace("\u201c", '"').replace("\u201d", '"')
    folded = re.sub("[\u2010-\u2015]", "-", folded)
    return re.sub(r"\s+", " ", folded).strip()
