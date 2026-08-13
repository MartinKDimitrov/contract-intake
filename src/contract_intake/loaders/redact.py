"""Mask personal data before it leaves the building.

Stage 04 sends contract text to a third-party model. Contracts carry personal
data that has nothing to do with the commercial terms being extracted -- a
signatory's national identity number, the account an invoice is paid into, a
contact person's mobile. None of it is needed to answer "what are the payment
terms", and sending it is a disclosure to a processor that nobody asked for.

So it is removed at the point the text is produced, in ``loaders/document.py``,
which has three consequences worth stating:

* the model never receives it;
* **the database never stores it either** -- ``documents.pages`` holds the
  redacted text, so this is not only about the API boundary;
* ``source_quote`` verification stays consistent, because the model quotes the
  same text the verifier searches.

What is *not* redacted is as deliberate as what is. ``signatories`` and
``counterparty_registration_id`` are fields the pipeline extracts, so signing
names and company registration, VAT and UIC numbers must survive. A company
identifier is not personal data; a natural person's identifier is.

Every category is *validated*, not merely matched. A nine-digit Bulgarian UIC
and a ten-digit personal number are both runs of digits, and pattern-matching
alone would mask the field the extraction depends on. A checksum tells them
apart, and the cost of being wrong is asymmetric: masking a required field
silently degrades extraction, while missing a rare exotic identifier leaves us
where every system without redaction already is.

Scanned pages are the honest gap. They go to the model as images and nothing
here can see them -- see the limitation noted in the module's tests and in
HAND_OVER.md.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Final

#: What replaces each category. Bracketed and uppercase so a reader of the
#: review UI can tell a redaction from contract wording at a glance.
MASK: Final = {
    "email": "[EMAIL]",
    "iban": "[IBAN]",
    "national_id": "[NATIONAL-ID]",
    "card": "[CARD]",
    "phone": "[PHONE]",
}

_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_DIGIT_RUN = re.compile(r"\b\d[\d ]{8,21}\d\b")
_DNI = re.compile(r"\b(\d{8})[- ]?([A-Za-z])\b")
_NIE = re.compile(r"\b([XYZxyz])[- ]?(\d{7})[- ]?([A-Za-z])\b")
#: International format only. A local-format number is indistinguishable from a
#: clause reference or an amount, and a false positive here is expensive.
_PHONE = re.compile(r"\+\d[\d\-() ]{7,17}\d\b")

_DNI_LETTERS: Final = "TRWAGMYFPDXBNJZSQVHLCKE"
#: Bulgarian personal number weights, per the civil registration act.
_EGN_WEIGHTS: Final = (2, 4, 8, 5, 10, 9, 7, 3, 6)


def _mod97(value: str) -> int:
    """IBAN/NIR checksum arithmetic, letters folded to digits (A=10 ... Z=35)."""
    digits = "".join(str(int(c, 36)) if c.isalpha() else c for c in value)
    return int(digits) % 97


def is_iban(candidate: str) -> bool:
    stripped = candidate.replace(" ", "").upper()
    if not 15 <= len(stripped) <= 34 or not stripped[:2].isalpha():
        return False
    return _mod97(stripped[4:] + stripped[:4]) == 1


def is_egn(candidate: str) -> bool:
    """Bulgarian personal number: ten digits, weighted checksum, real date.

    The date check is what keeps a ten-digit invoice reference from being
    mistaken for a person: month is offset by 20 for the 1800s and 40 for the
    2000s, so 13-39 and 53-99 are not months at all.
    """
    if len(candidate) != 10 or not candidate.isdigit():
        return False

    month = int(candidate[2:4])
    if month > 40:
        month -= 40
    elif month > 20:
        month -= 20
    if not 1 <= month <= 12 or not 1 <= int(candidate[4:6]) <= 31:
        return False

    total = sum(int(d) * w for d, w in zip(candidate[:9], _EGN_WEIGHTS, strict=True))
    return int(candidate[9]) == (total % 11) % 10


def is_nir(candidate: str) -> bool:
    """French social security number: thirteen digits plus a two-digit key."""
    if len(candidate) != 15 or not candidate.isdigit():
        return False
    return int(candidate[13:]) == 97 - (int(candidate[:13]) % 97)


def is_luhn(candidate: str) -> bool:
    if not 13 <= len(candidate) <= 19 or not candidate.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(candidate)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _dni_letter(number: int) -> str:
    return _DNI_LETTERS[number % 23]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return the text with personal data masked, and a count per category.

    The counts are evidence rather than telemetry: they are persisted on the
    document and shown in the review UI, so "nothing was redacted" and "the
    redaction step did not run" are distinguishable after the fact.
    """
    if not text:
        return text, {}

    found: Counter[str] = Counter()

    def swap(category: str) -> Callable[[re.Match[str]], str]:
        def _replace(match: re.Match[str]) -> str:
            found[category] += 1
            return MASK[category]

        return _replace

    def iban(match: re.Match[str]) -> str:
        if not is_iban(match.group(0)):
            return match.group(0)
        found["iban"] += 1
        return MASK["iban"]

    def digit_run(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = raw.replace(" ", "")
        if is_nir(digits) or is_egn(digits):
            found["national_id"] += 1
            return MASK["national_id"]
        if is_luhn(digits):
            found["card"] += 1
            return MASK["card"]
        return raw

    def spanish_id(match: re.Match[str]) -> str:
        groups = match.groups()
        if len(groups) == 2:
            number, letter = groups
        else:
            prefix, digits, letter = groups
            number = str("XYZ".index(prefix.upper())) + digits
        if letter.upper() != _dni_letter(int(number)):
            return match.group(0)
        found["national_id"] += 1
        return MASK["national_id"]

    # Order matters. Email first, because an address can contain digit runs a
    # later rule would eat. Phone before the generic digit run, so that a number
    # which happens to satisfy Luhn is labelled a phone rather than a card --
    # both are masked either way, but the category is shown to a reviewer.
    text = _EMAIL.sub(swap("email"), text)
    text = _IBAN.sub(iban, text)
    text = _PHONE.sub(swap("phone"), text)
    text = _NIE.sub(spanish_id, text)
    text = _DNI.sub(spanish_id, text)
    text = _DIGIT_RUN.sub(digit_run, text)

    return text, dict(found)


__all__ = ["MASK", "is_egn", "is_iban", "is_luhn", "is_nir", "redact"]
