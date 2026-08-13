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
    # fmt: off
    name       : str
    status     : str       # verified | unverifiable | not_found | absent
    confidence : float
    note       : str = ""
    # fmt: on


@dataclass(slots=True)
class ExtractionOutcome:
    # fmt: off
    extraction : ContractExtraction
    verdicts   : list[FieldVerdict] = field(default_factory=list)
    usd        : float              = 0.0
    latency_ms : int                = 0
    # fmt: on

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


#: A number as written: optional thousands groups, optional two-digit cents.
#: Deliberately not "digits and separators until they stop" -- that swallowed the
#: comma in "Section 3.2, 45 days" and the full stop in "1 April 2026. 30 days",
#: merging two unrelated numbers into one the field could never match. Six per
#: cent of the digit-bearing lines in this repository's corpus were affected.
_DIGIT_RUN = re.compile(r"\d{1,3}(?:[.,\u00a0 ]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?")


def _numbers_in(text: str) -> set[str]:
    """Every number the text states, as digits.

    Whole numbers, not a concatenation. Comparing against the concatenation was
    an open door: "This Agreement is dated 15 March 2024" flattens to "152024",
    which contains 24, 52, 202 and 15 -- so a date supported a two-year term, a
    fifty-two week period and a cap of fifteen.

    An amount written with cents yields both readings, because "EUR 500,000.00"
    and a stated cap of 500000 are the same figure.
    """
    found: set[str] = set()
    for run in _DIGIT_RUN.findall(text):
        digits = _digits(run)
        if not digits:
            continue
        found.add(digits)
        if len(run) > 3 and run[-3] in ".," and len(digits) > 2:
            found.add(digits[:-2])
    return found


#: Fields whose value is a normalised summary rather than a phrase lifted from
#: the page. "Signing names and titles, comma separated" is not a substring of
#: anything; neither is a jurisdiction reduced to a country noun. Requiring one
#: here rejects the honest answer the schema asks for.
_NOT_VERBATIM = frozenset({"signatories"})


#: Numerals as contracts write them out, in the five languages triage claims.
#: Only the stems that appear in a term, a notice period or an amount -- this is
#: not a parser, it answers one question: does this quote contain a number at
#: all? Without it, boilerplate with no digits ("MASTER SERVICES AGREEMENT
#: dated") counts as evidence for any figure the model likes.
#:
#: Anchored on word boundaries, and that is not a detail. Searched as bare
#: substrings these stems match inside ordinary words -- "MAINTENANCE" carries
#: "ten", "written" carries "ten", "percent" carries "cent", "component" carries
#: "one" -- and 14% of the digit-free clause fragments in this repository's own
#: corpus then counted as containing a number. That reopened the whole bypass.
_NUMBER_WORDS = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty"
    r"|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million"
    r"|един|едно|два|две|три|четири|пет|шест|седем|осем|девет|десет"
    r"|двадесет|тридесет|четиридесет|петдесет|шестдесет|сто|хиляда|милион"
    r"|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|veinte|treinta"
    r"|cuarenta|cincuenta|sesenta|noventa|ciento|mil"
    r"|deux|trois|quatre|cinq|huit|neuf|dix|vingt|trente|quarante"
    r"|cinquante|soixante|mille"
    r"|once|doce|trece|catorce|quince"
    r"|onze|douze|treize|quatorze|quinze)\b",
    re.IGNORECASE,
)

#: German, Bulgarian and Spanish write their numerals as single compounds --
#: "sechsunddreißig", "петнадесет", "veinticuatro" -- so a boundary on the right
#: rejects every one of them. Anchored on the left only, which is what kills the
#: substring family anyway: "MAINTENANCE" has no boundary before its "ten".
#:
#: German got this treatment first and the other two did not, so an honest
#: Bulgarian or Spanish contract was told its quote was a fabrication. That is
#: the worst possible error here: it corrupts the one signal a reviewer is
#: instructed to trust.
_COMPOUND_NUMBER_WORDS = re.compile(
    # German
    # "ein" and "eins" are omitted on purpose. "ein" is the indefinite article,
    # so it matches "eine", "einen" and every ordinary German sentence; "eins"
    # opens "einschließlich". Only "einund-", which is unambiguously a numeral,
    # survives from that stem.
    r"\b(?:einund|zwei|drei|vier|fünf|funf|sechs|sieben|acht|neun|zehn|zwölf"
    r"|zwanzig|dreißig|dreissig|vierzig|fünfzig|funfzig|sechzig|siebzig|achtzig"
    r"|neunzig|hundert|tausend|million"
    # Bulgarian: the teens are -надесет compounds, and tens join with "и"
    r"|единадесет|дванадесет|тринадесет|четиринадесет|петнадесет|шестнадесет"
    r"|седемнадесет|осемнадесет|деветнадесет|надесет|надесет"
    r"|двадесет|тридесет|четиридесет|петдесет|шестдесет|седемдесет|осемдесет"
    r"|деветдесет|двеста|триста|хиляд|милион"
    # Spanish: 16-29 are written as one word
    r"|dieciséis|dieciseis|diecisiete|dieciocho|diecinueve|veinti|veintiún"
    r"|veintiun|veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta"
    r"|noventa|ciento|cientos|mil)",
    re.IGNORECASE,
)


def _states_a_number(text: str) -> bool:
    return bool(_NUMBER_WORDS.search(text) or _COMPOUND_NUMBER_WORDS.search(text))


def _alphanumeric(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


def supports_value(name: str, value: Any, quote: str) -> bool:
    """Does this quote positively contradict the value beside it?

    Locating a quote proves it came from the document. It does not prove it is
    evidence *for the value* -- boilerplate present in every contract ("This
    Agreement is entered into") locates perfectly while supporting nothing.
    Without this test, "verified" means only that the model emitted twelve
    characters that occur somewhere in the file.

    The test only ever fires on positive disagreement, and that restraint is
    not politeness. A first version required a numeric value's digits to appear
    in its quote, which reads as strict and is wrong: contracts write amounts in
    words, and "Payment is due net thirty days", "five hundred thousand euros"
    and "an initial term of two (2) years" were all reported as the model having
    invented the value. A third of realistic quotes failed, and each failure
    told a reviewer the model had made something up.

    So: a quote with no digits cannot contradict a number, and a field whose
    value is a normalised summary is not asked to be verbatim. What remains --
    a quote whose numbers all disagree with the claim -- is the case worth
    catching, and it is the one the fabricated examples fall into.

    One false alarm survives on purpose: a unit conversion, where a term of 24
    months is quoted from "two (2) years". The quote's digits genuinely disagree
    with the value, and telling them apart from a fabrication needs unit
    arithmetic this does not do. It sends an honest contract to a person, which
    is the direction to be wrong in.
    """
    if isinstance(value, bool) or value is None or name in _NOT_VERBATIM:
        return True

    if name == "effective_date":
        # Normalised to ISO here, written as prose on the page. The year is the
        # one part that survives every rendering of a date.
        year = str(value)[:4]
        return not year.isdigit() or year in quote

    if name == "governing_law":
        # The schema asks for the jurisdiction as stated, and a contract states
        # it adjectivally. Accept the country noun, the adjective, or any of the
        # phrasings the playbook already knows how to canonicalise.
        return _states_a_jurisdiction(str(value), quote)

    if isinstance(value, int | float):
        wanted = _digits(f"{value:.0f}" if isinstance(value, float) else str(value))
        present = _numbers_in(quote)
        if present:
            return not wanted or wanted in present
        # No digits: acceptable only if the quote spells a number out. Prose
        # with no number in it cannot be evidence for one.
        return _states_a_number(quote)

    text = _alphanumeric(str(value))
    # Spacing and punctuation vary between the value and the page -- "HRB 84421"
    # against "HRB84421", "851 402 336" against "851402336".
    return not text or text in _alphanumeric(quote)


def _states_a_jurisdiction(value: str, quote: str) -> bool:
    """Does the quote name the jurisdiction the value claims, however it is written?

    Exempting this field entirely -- which is what an earlier version did -- let
    any locatable sentence support any jurisdiction, and jurisdiction is the
    input to a high-severity playbook check.
    """
    from contract_intake.policy.thresholds import phrasings_for

    # Fold accents on both sides. The alias table is stored accent-stripped and
    # `_normalise` keeps accents, so every non-English alias in it was unmatchable
    # against the accented text a contract actually contains.
    folded = _fold_accents(_normalise(quote))
    return any(_fold_accents(_normalise(p)) in folded for p in phrasings_for(value))


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


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
