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
    """Check every quote against the document, and zero the fields that fail.

    Two properties this has to hold, both learned the hard way:

    * **The model does not get to choose whether it is checked.** ``page`` is a
      value the model emits, so any test that lets a claimed page number skip
      verification is a bypass the model controls. A page claim can only ever
      make checking *impossible* (an image page), never *unnecessary*, and the
      impossible case is escalated by ``rule_partially_unverifiable`` rather
      than passed over here.
    * **Every failure zeroes.** There is no branch that leaves a failed quote
      holding the model's own confidence.
    """
    text_pages = {p.number: _normalise(p.text) for p in document.pages if p.kind == "text"}
    whole = _normalise(document.all_text)
    image_pages = {p.number for p in document.pages if p.kind == "image"}
    verdicts: list[FieldVerdict] = []

    for name, evidence in extraction.evidence_fields().items():
        verdicts.append(_verify_one(name, evidence, text_pages, whole, image_pages))
    return verdicts


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def supports_value(name: str, value: Any, quote: str) -> bool:
    """Does this quote actually say what the field claims?

    Locating a quote proves it came from the document. It does not prove it is
    evidence *for the value beside it* -- and boilerplate present in every
    contract ("This Agreement is entered into") will locate perfectly while
    supporting nothing. Without this test, "verified" means only that the model
    emitted twelve characters that occur somewhere in the file.

    Deliberately one-sided. It fires only when the quote positively disagrees
    with the value or carries nothing that could support it, so its errors are
    false alarms that cost a reviewer five minutes -- never quiet approvals.

    Booleans are exempt: no wording of "the term shall not renew automatically"
    contains ``False``.
    """
    if isinstance(value, bool) or value is None:
        return True

    if isinstance(value, int | float):
        # "sixty (60) days" carries 60; a numeric claim quoting prose with no
        # number in it is not evidence, whatever else the prose says.
        wanted = _digits(f"{value:.0f}" if isinstance(value, float) else str(value))
        return bool(wanted) and wanted in _digits(quote)

    text = str(value).strip()
    if not text:
        return True

    if name == "effective_date":
        # The field is normalised to ISO; the document says "14 March 2026".
        # The year is the part that survives every rendering.
        year = text[:4]
        return not year.isdigit() or year in quote

    return _normalise(text) in _normalise(quote)


def _fail(name: str, evidence: Evidence, note: str) -> FieldVerdict:
    previous = evidence.confidence
    evidence.confidence = 0.0
    return FieldVerdict(name, "not_found", 0.0, f"{note} (model claimed {previous:.2f})")


def _verify_one(
    name: str,
    evidence: Evidence,
    text_pages: dict[int, str],
    whole: str,
    image_pages: set[int],
) -> FieldVerdict:
    value = getattr(evidence, "value", None)

    if value is None:
        # Nothing claimed. Confidence should already be 0; make sure of it.
        evidence.confidence = 0.0
        return FieldVerdict(name, "absent", 0.0, "not stated in the document")

    quote = (evidence.source_quote or "").strip()
    if not quote:
        return _fail(name, evidence, "value given with no supporting quote")

    # Before the search, not after it. Reached the other way round, this test
    # rescues exactly the quotes that could not be found -- the fabricated ones.
    if len(quote) < MIN_VERIFIABLE_QUOTE_CHARS:
        return _fail(name, evidence, f"quote of {len(quote)} chars is too short to locate")

    if not supports_value(name, value, quote):
        return _fail(name, evidence, f"quote does not support the value {value!r}")

    needle = _normalise(quote)
    claimed = evidence.page

    if claimed in text_pages:
        if needle in text_pages[claimed]:
            return FieldVerdict(name, "verified", evidence.confidence)
        if needle in whole:
            found_on = next((n for n, text in text_pages.items() if needle in text), None)
            return FieldVerdict(
                name,
                "verified",
                evidence.confidence,
                f"quote is on page {found_on}, not the claimed page {claimed}",
            )
        return _fail(name, evidence, "quote not present in the document")

    if not text_pages:
        return FieldVerdict(
            name,
            "unverifiable",
            evidence.confidence,
            "the document has no text layer; no quote could be checked",
        )

    if claimed in image_pages:
        # Checking really is impossible, so the value is not called a lie -- but
        # it is not called verified either, and the rule layer blocks on it.
        return FieldVerdict(
            name,
            "unverifiable",
            evidence.confidence,
            f"quote is attributed to page {claimed}, which is a scan",
        )

    if needle in whole:
        return FieldVerdict(
            name,
            "verified",
            evidence.confidence,
            f"quote located, but page {claimed} is not a page of this document",
        )

    return _fail(name, evidence, "quote not present in the document")


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
