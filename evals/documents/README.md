# The corpus, by provenance

Where a document came from decides what a result on it is worth, so the folders
say it rather than a footnote.

| folder               | n   | what it is                              | in git        |
|----------------------|-----|-----------------------------------------|---------------|
| `authored/`          | 6   | written by hand for this project        | yes           |
| `generated/gemini/`  | 10  | produced by Gemini                      | yes           |
| `generated/chatgpt/` | 10  | produced by ChatGPT                     | yes           |
| `generated/grok/`    | 10  | produced by Grok                        | yes           |
| `collected/`         | 100 | fetched unmodified from a public source | no, see below |
| `rendered/`          | —   | PDFs built from the text above          | no, derived   |

## The distinction that matters

A result on `collected/` is evidence. A result on `authored/` is a demonstration
that the intended path runs, and nothing more: I wrote both the document and the
expected answer, so agreement between them is not a measurement.

`generated/` sits in between, and is split by the model that wrote it because
three generators disagree in useful ways — the same instruction produced
different section orders, different clause vocabulary and different levels of
boilerplate. That variety found four defects `authored/` could not: a vocabulary
that weighted `supplier` as heavily as `whereas`, a triage step that read
`hereby` as a contract marker when certificates and declarations use it just as
freely, an English-only vocabulary, and a PDF writer that could not encode
Cyrillic. But they were still produced to be test documents, so they are not the
wild.

## Both directions, in every language

`collected/` is entirely negative — a procurement notice is not a contract — so
on its own it cannot tell a working vocabulary apart from an empty one. A
vocabulary that knows no Spanish scores 20/20 on Spanish notices.

That is not hypothetical. The Spanish and French terms were added, the hundred
notices were classified correctly, and the two hand-written contracts added at
the same time were both turned away: the terms had gone into the wrong constant
and were matching nothing. Every language therefore carries a positive case as
well, in `authored/` and in the unit tests.

## Formats

The text folders hold plain text so the wording is reviewable in a diff.
[`evals/render.py`](../render.py) renders them to PDF with an embedded Unicode
font. A file named `*.scan.txt` is rasterised, rotated, blurred and speckled
instead, producing a page with no text layer.

Page breaks are a form feed (`\f`).

## Why `collected/` is not committed

A hundred PDFs of real procurement notices are large, and the fetch is
reproducible:

```bash
make corpus
```

[`evals/corpus.py`](../corpus.py) is the source of truth, not the bytes. It
pulls from TED (Tenders Electronic Daily), the EU's public procurement journal,
which publishes every above-threshold notice in all official languages — which
is what makes a multilingual claim testable rather than asserted.

## Licensing

`collected/` documents are EU public-sector information, reusable under the
Commission's reuse decision (2011/833/EU). They are fetched at need and not
redistributed here.

Nothing in `authored/` or `generated/` describes a real company, contract or
counterparty. Every name in them is invented.
