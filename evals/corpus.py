"""Fetch a real European corpus for the free stages of the pipeline.

The authored and generated documents test what was imagined; this tests what
exists. TED (Tenders Electronic Daily) publishes every EU public-procurement
notice above threshold, each one available in all twenty-four official
languages, and each one short -- a few pages. Three properties matter here:

* **Real.** Nobody wrote these with this system in mind.
* **Multilingual, genuinely.** The same notice in five languages is the honest
  test of a vocabulary that claims to work in five.
* **Negative.** A procurement notice is not a contract, and triage must say so
  without spending a token. Getting that wrong is expensive in a way getting it
  right is not: a false positive here pays for extraction on a document that was
  never going to produce a contract record.

Downloads are cached under `evals/documents/collected/`, which is gitignored --
the fetch is reproducible from this script rather than committed as a hundred
PDFs. Being entirely negative, this corpus cannot on its own tell a working
vocabulary from an empty one; see `evals/documents/README.md`.

    python evals/corpus.py --per-language 25
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CORPUS = Path(__file__).parent / "documents" / "collected"
SEARCH = "https://api.ted.europa.eu/v3/notices/search"
AGENT = "contract-intake/0.1 (eval corpus; https://github.com/MartinKDimitrov/contract-intake)"

#: The five the triage vocabulary claims, including one non-Latin script. Each
#: notice is published in all of them, so the same document is tested five ways.
LANGUAGES = {"ENG": "en", "BUL": "bg", "DEU": "de", "SPA": "es", "FRA": "fr"}

#: Transport and rail, to stay in the same domain as the rest of the corpus.
CPV = "60000000"


def search(limit: int, page: int = 1) -> list[dict]:
    body = json.dumps(
        {
            "query": f"classification-cpv={CPV} AND publication-date>=20260101",
            "fields": ["publication-number", "notice-title", "links"],
            "limit": limit,
            "page": page,
        }
    ).encode()
    request = urllib.request.Request(
        SEARCH,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["notices"]


def download(url: str, target: Path) -> bool:
    if target.exists() and target.stat().st_size > 0:
        return True
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"    skip {target.name}: {exc}")
        return False
    if not data.startswith(b"%PDF"):
        return False
    target.write_bytes(data)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-language", type=int, default=25)
    parser.add_argument("--pages", type=int, default=3, help="search result pages to walk")
    args = parser.parse_args()

    CORPUS.mkdir(parents=True, exist_ok=True)
    counts = dict.fromkeys(LANGUAGES, 0)

    for page in range(1, args.pages + 1):
        if all(n >= args.per_language for n in counts.values()):
            break
        try:
            notices = search(limit=args.per_language, page=page)
        except (urllib.error.URLError, TimeoutError, KeyError) as exc:
            print(f"search page {page} failed: {exc}")
            break

        for notice in notices:
            number = str(notice.get("publication-number", "")).replace("/", "-")
            pdfs = (notice.get("links") or {}).get("pdf") or {}
            for code, short in LANGUAGES.items():
                if counts[code] >= args.per_language or code not in pdfs:
                    continue
                target = CORPUS / f"ted-{short}-{number}.pdf"
                if download(pdfs[code], target):
                    counts[code] += 1
                    time.sleep(0.2)  # be a courteous client

    total = sum(counts.values())
    print(f"\n{total} document(s) in {CORPUS}")
    for code, short in LANGUAGES.items():
        print(f"  {short}: {counts[code]}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
