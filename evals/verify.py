"""Run quote verification over the real corpus, for nothing.

`make triage` proves stage 02 on 135 real documents and costs no tokens. Stage
04's verification had no such check: it was tested only against sentences
written by hand, in a test file, by the person who wrote the rule. Every
verification defect this project has had was a false accusation against wording
that a human would call ordinary -- amounts in words, German compounds, an
amount with cents, a clause reference before a number -- and none of them was
visible to a suite built from invented examples.

This closes that. For every real document, it takes the sentences that state a
number, and asserts that a model quoting *that sentence* for *that number* is
believed. No API call: `supports_value` is a pure function, and the documents
are already on disk.

    python evals/verify.py

The reverse direction -- that a fabrication is caught -- stays in
`tests/extract/test_verification_bypasses.py`, because a fabrication has to be
invented and cannot be harvested from a corpus.

What this does *not* cover, stated plainly: it can only test forms the corpus
actually contains. These documents state amounts as "EUR 500,000" and never with
cents, and never put a clause reference immediately before a term -- both of
which were real defects, and both of which this harness passes either way. It
discriminates only as far as the corpus is varied, which is an argument for
widening the corpus rather than for trusting the number.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contract_intake.extract.extractor import supports_value  # noqa: E402

DOCUMENTS = Path(__file__).parent / "documents"

#: A *term*, not an identifier: a number followed by a unit, or preceded by a
#: currency. Without that, the first version of this harness sliced IBANs and
#: invoice numbers into fragments and reported each one as wording the verifier
#: had wrongly rejected -- which it is not, because nothing would ever quote
#: "3704" out of a bank account as evidence for a payment term.
UNITS = (
    r"days?|months?|years?|weeks?"
    r"|дни|дена|месеца?|години"
    r"|tage?n?|monate?n?|jahre?n?"
    r"|d[íi]as?|mes(?:es)?|a[ñn]os?"
    r"|jours?|mois|ans?|ann[ée]es?"
)
CURRENCIES = r"EUR|USD|GBP|CHF|BGN|RON|PLN"
AMOUNT = r"\d{1,3}(?:[.,\u00a0 ]\d{3})+|\d{1,7}"
NUMBER = re.compile(
    rf"(?:{CURRENCIES})\s*({AMOUNT})|({AMOUNT})\s*(?:{UNITS})\b",
    re.IGNORECASE,
)

#: Sentences, roughly. Contracts are full of "No." and "Art. 5", so splitting on
#: a full stop alone produces fragments; a following capital or newline is the
#: cheap heuristic that survives five languages.
SENTENCE = re.compile(r"[^.\n]{20,400}[.\n]")

#: Numbers that are references rather than terms: clause numbers, years, and the
#: page furniture a contract carries. Quoting them proves nothing either way.
IGNORED = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"}


def clauses(text: str) -> list[tuple[int, str]]:
    """Every (number, sentence) pair the document states."""
    found: list[tuple[int, str]] = []
    for sentence in SENTENCE.findall(text):
        clause = " ".join(sentence.split())
        for groups in NUMBER.findall(clause):
            raw = next((g for g in groups if g), "")
            digits = "".join(c for c in raw if c.isdigit())
            if digits in IGNORED or not digits:
                continue
            if 1900 <= int(digits) <= 2100 and len(digits) == 4:
                continue  # a year, not a term
            found.append((int(digits), clause))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    sources = sorted(
        p for folder in ("authored", "generated") for p in (DOCUMENTS / folder).rglob("*.txt")
    )
    if not sources:
        print("no documents found", file=sys.stderr)
        return 1

    checked = 0
    rejected: list[tuple[str, int, str]] = []

    for path in sources:
        for value, clause in clauses(path.read_text(encoding="utf-8")):
            checked += 1
            # The field name only selects the rule; any numeric field does.
            if not supports_value("payment_terms_days", value, clause):
                rejected.append((path.name, value, clause))

    print("=" * 78)
    print("QUOTE VERIFICATION AGAINST THE REAL CORPUS  (zero tokens)")
    print("=" * 78)
    print(f"\n{len(sources)} documents, {checked} clauses that state a number")
    print(f"{checked - len(rejected)}/{checked} believed")

    if rejected:
        print("\nhonest wording called a fabrication:")
        for name, value, clause in rejected[:12]:
            print(f"  {name}: {value} <- {clause[:88]}")
        if len(rejected) > 12:
            print(f"  ... and {len(rejected) - 12} more")
        return 1

    print("\nEvery number a real document states is believed when quoted from its own clause.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
