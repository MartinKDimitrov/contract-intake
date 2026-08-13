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

Two rules keep it from doing more harm than good, and both were learned by
measuring rather than by reasoning:

**Validated where a checksum exists.** IBAN goes through mod-97 with the
country's own length, the Bulgarian personal number through its weighted sum,
the French through its key, the Spanish through its check letter, cards through
Luhn. Email and phone have no checksum to run: an address is matched on shape
alone, and a phone number on shape plus a digit count -- which is why both of
those are the categories where a false positive would be cheapest.

**Personal identifiers are recognised only beside a label.** Checksums alone are
not enough to tell a person's number from a company's, because the schemes were
never designed to be mutually exclusive: a French SIRET carries a Luhn check *by
construction*, so treating "digits that pass Luhn" as a payment card masked every
French company number. A 13-digit Bulgarian BULSTAT clears Luhn one time in ten,
and any 15-digit run satisfies the French social-security key one time in 97.

That trade is deliberate and one-directional. An unlabelled identifier is missed,
which leaves us exactly where a system without redaction already is. A mislabelled
company number would silently destroy ``counterparty_registration_id`` -- a field
this pipeline extracts, decides on, and reports as verified.

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

# --- what may be recognised on its own, and what may not --------------------
#
# An IBAN and an email address carry their own structure: a country prefix and
# a mod-97 checksum, an @ and a domain. Nothing else in a contract looks like
# them, so they are matched wherever they appear.
#
# Everything else is a bare run of digits, and a contract is full of those --
# company registration numbers, order references, article numbers, amounts,
# schedules. Checksums do not separate them: a French SIRET carries a Luhn
# check *by construction*, so using Luhn as a card detector masked every French
# company number as a payment card. A Bulgarian 13-digit BULSTAT passes Luhn
# one time in ten, and any 15-digit run satisfies the French social-security
# key one time in 97.
#
# So the personal identifiers are recognised only beside a label. That trades
# recall for precision deliberately: an unlabelled identifier is missed, which
# leaves us where a system with no redaction already is, while a mislabelled
# company number would silently destroy `counterparty_registration_id` -- a
# field this pipeline extracts and decides on.

_EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Za-z]{2}\d{2}[ -]?(?:[A-Za-z0-9]{4}[ -]?){2,7}[A-Za-z0-9]{1,4}\b")

#: ``label , up to a little punctuation , number`` -- the number is group 1 so
#: the label survives into the masked text and the reviewer can still see what
#: kind of thing was removed.
#: The separator is optional and may be written several ways at once: "no.",
#: "No:", "№". Allowing only one token meant `n[o]` consumed the "no" and left
#: the "." unmatched, so "Card no. 4111 ..." was not masked at all.
_LABELLED = "(?i)(?:{labels})[\\s.:#-]*(?:n[o°º]|number|№)?[\\s.:#-]*({number})"

_ID_LABELS = (
    r"егн|бул\s*егн|eгн|egn"  # Bulgarian personal number
    r"|dni|nif|nie"  # Spanish
    r"|nir|s[ée]curit[ée]\s+sociale|num[ée]ro\s+de\s+s[ée]cu\w*"  # French
    r"|national\s+id|personal\s+(?:id|number)|id\s+number"
)
_CARD_LABELS = r"card|carte|karte|tarjeta|карта|pan|visa|mastercard|maestro|amex|american\s+express"
_PHONE_LABELS = (
    r"tel|t[ée]l|tlf|phone|telephone|t[ée]l[ée]phone|telefon|tel[ée]fono|тел(?:ефон)?"
    r"|mobile|mobil|m[óo]vil|gsm|fax|handy"
)

#: Exact shapes, not ranges. A range of 13-19 digits is what let a 14-digit
#: SIRET and a 13-digit BULSTAT be read as payment cards.
_EGN_SHAPE = r"\d{10}"
_NIR_SHAPE = r"\d(?:[ .]?\d){14}"
_CARD_SHAPE = r"\d{4}(?:[ -]?\d{4}){3}"
_DNI_SHAPE = r"\d{8}[- ]?[A-Za-z]"
_NIE_SHAPE = r"[XYZxyz][- ]?\d{7}[- ]?[A-Za-z]"
_PHONE_SHAPE = r"\+?\d[\d\-()/ ]{6,18}\d"

_LABELLED_ID = re.compile(
    _LABELLED.format(
        labels=_ID_LABELS,
        number=f"{_NIR_SHAPE}|{_NIE_SHAPE}|{_DNI_SHAPE}|{_EGN_SHAPE}",
    )
)
_LABELLED_CARD = re.compile(_LABELLED.format(labels=_CARD_LABELS, number=_CARD_SHAPE))
_LABELLED_PHONE = re.compile(_LABELLED.format(labels=_PHONE_LABELS, number=_PHONE_SHAPE))

#: IBAN length by country, from the SWIFT registry. A country absent from this
#: table is not treated as an IBAN at all -- the failure is then a miss, not the
#: destruction of a company identifier.
IBAN_LENGTHS: Final = {
    "AD": 24,
    "AE": 23,
    "AL": 28,
    "AT": 20,
    "AZ": 28,
    "BA": 20,
    "BE": 16,
    "BG": 22,
    "BH": 22,
    "BR": 29,
    "BY": 28,
    "CH": 21,
    "CR": 22,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "DO": 28,
    "EE": 20,
    "EG": 29,
    "ES": 24,
    "FI": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GE": 22,
    "GI": 23,
    "GL": 18,
    "GR": 27,
    "GT": 28,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IL": 23,
    "IS": 26,
    "IT": 27,
    "JO": 30,
    "KW": 30,
    "KZ": 20,
    "LB": 28,
    "LC": 32,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MR": 27,
    "MT": 31,
    "MU": 30,
    "NL": 18,
    "NO": 15,
    "PK": 24,
    "PL": 28,
    "PS": 29,
    "PT": 25,
    "QA": 29,
    "RO": 24,
    "RS": 22,
    "SA": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "TN": 24,
    "TR": 26,
    "UA": 29,
    "VA": 22,
    "VG": 24,
    "XK": 20,
}

_DNI_LETTERS: Final = "TRWAGMYFPDXBNJZSQVHLCKE"
#: Bulgarian personal number weights, per the civil registration act.
_EGN_WEIGHTS: Final = (2, 4, 8, 5, 10, 9, 7, 3, 6)

#: PDF text extraction emits these instead of a plain space, and a pattern
#: written with a literal space silently stops matching. The verifier in
#: ``extract/extractor.py`` already normalises for the same reason; the
#: redactor did not, so a real IBAN broken by non-breaking spaces went to the
#: model untouched.
_INVISIBLE = str.maketrans({"\xa0": " ", "\u202f": " ", "\u2007": " ", "\u00ad": "", "\u200b": ""})


def _mod97(value: str) -> int:
    """IBAN/NIR checksum arithmetic, letters folded to digits (A=10 ... Z=35)."""
    digits = "".join(str(int(c, 36)) if c.isalpha() else c for c in value)
    return int(digits) % 97


def is_iban(candidate: str) -> bool:
    """Country prefix, the length that country actually uses, then mod-97.

    The length table is not decoration. Without it a Bulgarian VAT number --
    ``BG`` followed by digits, fifteen characters -- clears mod-97 one time in
    ninety-seven and is masked as a bank account, and VAT numbers are what
    ``counterparty_registration_id`` is often made of.
    """
    stripped = "".join(c for c in candidate.upper() if c.isalnum())
    if not (stripped[:2].isalpha() and stripped[2:4].isdigit()):
        return False
    if IBAN_LENGTHS.get(stripped[:2]) != len(stripped):
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

    # Do this first, or every pattern below silently stops matching on text that
    # came out of a PDF.
    text = text.translate(_INVISIBLE)
    found: Counter[str] = Counter()

    def mask_group(category: str, validator: Callable[[str], bool]) -> Callable[..., str]:
        def _replace(match: re.Match[str]) -> str:
            number = match.group(1)
            if not validator(number):
                return match.group(0)
            found[category] += 1
            return match.group(0).replace(number, MASK[category])

        return _replace

    def whole_match(category: str, validator: Callable[[str], bool]) -> Callable[..., str]:
        def _replace(match: re.Match[str]) -> str:
            if not validator(match.group(0)):
                return match.group(0)
            found[category] += 1
            return MASK[category]

        return _replace

    def any_national_id(candidate: str) -> bool:
        compact = candidate.replace(" ", "").replace(".", "").replace("-", "")
        if compact[:1].isalpha():
            return is_nie(candidate)
        if compact[-1:].isalpha():
            return is_dni(candidate)
        return is_nir(compact) or is_egn(compact)

    # Email first: an address can contain digit runs a later rule would eat.
    text = _EMAIL.sub(whole_match("email", lambda _c: True), text)
    text = _IBAN.sub(whole_match("iban", is_iban), text)
    text = _LABELLED_ID.sub(mask_group("national_id", any_national_id), text)
    text = _LABELLED_CARD.sub(mask_group("card", lambda c: is_luhn(_digits(c))), text)
    text = _LABELLED_PHONE.sub(mask_group("phone", lambda c: 7 <= len(_digits(c)) <= 15), text)

    return text, dict(found)


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def is_dni(candidate: str) -> bool:
    compact = candidate.replace(" ", "").replace("-", "")
    if len(compact) != 9 or not compact[:8].isdigit() or not compact[8].isalpha():
        return False
    return compact[8].upper() == _dni_letter(int(compact[:8]))


def is_nie(candidate: str) -> bool:
    compact = candidate.replace(" ", "").replace("-", "")
    if len(compact) != 9 or compact[0].upper() not in "XYZ":
        return False
    if not compact[1:8].isdigit() or not compact[8].isalpha():
        return False
    number = str("XYZ".index(compact[0].upper())) + compact[1:8]
    return compact[8].upper() == _dni_letter(int(number))


__all__ = ["MASK", "is_dni", "is_egn", "is_iban", "is_luhn", "is_nie", "is_nir", "redact"]
