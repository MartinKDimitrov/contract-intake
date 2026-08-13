# contract-intake

[![CI](https://github.com/MartinKDimitrov/contract-intake/actions/workflows/ci.yml/badge.svg)](https://github.com/MartinKDimitrov/contract-intake/actions/workflows/ci.yml)

An email with a vendor contract attached arrives. An agent reads it, extracts the
commercial terms, checks them against the vendor registry and the internal
contracting playbook, and routes the result: clean and on-policy contracts are
stored, anything uncertain or off-policy goes to a human with the specific reason
attached.

## Why vendor contracts

Extraction alone does not need a knowledge base. Contract intake does, and for
two different reasons:

- **Entity resolution.** "NordWind Logistics Ltd." on a phone photo of a signed
  page has to become `VEN-0142` in the vendor registry — or be flagged as an
  unknown counterparty.
- **Policy validation.** "Payment terms: 90 days" is not wrong on its face. It
  is wrong *against this company's playbook*, which caps them at 45. No amount
  of model reasoning supplies that fact.

The second is the one retrieval is actually load-bearing for, so the eval
harness measures it directly, by running the agent with and without
knowledge-base access.

## Quickstart

```bash
make setup                      # venv + dependencies (Python 3.12)
cp .env.example .env            # then fill in ANTHROPIC_API_KEY
make test                       # hermetic — no API key needed
make run                        # http://localhost:8000/healthz
```

See the whole pipeline at a glance:

```bash
.venv/bin/python -m contract_intake.cli stages
```

```
01 receive        (Source)  ->  received
02 triage         [ 0 ]  received   ->  triaged
03 load           [ 0 ]  triaged    ->  loaded
04 extract        [LLM]  loaded     ->  extracted
05 enrich         [LLM]  extracted  ->  enriched
06 decide         [ 0 ]  enriched   ->  decided
07 deliver        [ 0 ]  decided    ->  delivered
```

## How it is put together

**One phase, one file.** Every stage lives in
`src/contract_intake/pipeline/stage_NN_name.py` and opens with its own contract:
what it does, what it consumes and produces, what it costs in tokens, and how it
can fail. The whole flow is the `STAGES` tuple in `pipeline/runner.py`.

**The status column is the queue.** No broker. The worker picks the oldest
attachment in a status some stage consumes and runs exactly that stage. A crash
mid-document resumes at that phase; any phase can be replayed on its own with
`make stage N=04 ID=17`.

**The model extracts; code decides.** Stage 04 produces fields with confidence
and a verbatim source quote. Stage 05 validates them against the knowledge base
and reports findings with evidence. Stage 06 is pure Python — deterministic
rules that can be unit-tested exhaustively and explained to a lawyer in one
sentence.

**Nothing spends money unrecorded.** `llm/client.py` is the only path to the
API, and it writes tokens, cache split, USD and latency to `llm_calls` on
success, refusal and exception alike. It also refuses to call once one document
has spent `max_usd_per_document`, checked before the request, not after.

**Personal data does not leave the building.** A signatory's national identity
number and the account an invoice is paid into are nothing the extracted fields
need, so they are masked in stage 03 — before the model sees the page and before
it reaches the database. Each category is checksum-validated rather than
pattern-matched, because a company registration number is an extracted field and
looks much like a personal one.

Detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); every decision that could
have gone the other way, with the condition that would flip it, in
[docs/TRADEOFFS.md](docs/TRADEOFFS.md). Running it in anger:
[docs/HAND_OVER.md](docs/HAND_OVER.md).

## Cost

Four of the seven phases spend no tokens at all. The two that do are bounded by
a per-document USD ceiling checked before each call.

The largest single lever is stage 03, which spends nothing itself but decides
**per page** between text and vision. Measured across the corpus:

| | tokens |
|---|---|
| a page with a text layer | ~250 |
| the same page rendered as an image | ~1850 |

So a born-digital contract with one scanned signature page sends 19 pages of
text and exactly one image, instead of twenty images.

Extraction, measured end to end at `effort=medium`:

| document | in | cache read | out | USD |
|---|---|---|---|---|
| 2-page born-digital contract | 765 | 3507 | 941 | $0.049 |
| 1-page contract, text layer | 432 | 3507 | 1225 | $0.035 |
| 1-page scan, no text layer | 1868 | 3507 | 914 | $0.034 |

The cached 3507 tokens are the system prompt and JSON schema, written once and
read at a tenth of the rate from the second document onwards. Note that output
tokens dominate: on documents this size, how much the model *thinks* costs more
than what it reads.

`effort=medium` returned values identical to `high` on all three documents, for
11% less and 31% faster — an indication at n=3, not yet an eval. Full breakdown
in [docs/COST_MODEL.md](docs/COST_MODEL.md) _(phase 7)_.

```bash
make costs          # the ledger, aggregated
curl :8000/metrics/costs
```

## Tested against

136 documents, sorted into folders by where they came from, because that decides
what a result on them is worth — see [evals/documents/](evals/documents/).

| provenance | documents | |
|---|---|---|
| written by hand | 6 | the paths the design intends, including one degraded scan |
| generated by Gemini, ChatGPT and Grok | 30 | breadth of document type — certificates, board resolutions, leases, addenda, an NDA, an SLA |
| collected from TED, unmodified | 100 | real EU procurement notices in five languages, 1,719 pages, none of them contracts |

```bash
make corpus && make triage
```

```
authored and generated -- contracts that must pass, lookalikes that must not
  authored:                5/5   correct
  generated/chatgpt:      10/10  correct
  generated/gemini:       10/10  correct
  generated/grok:         10/10  correct
  total:                  35/35  correct
  (1 scanned, no text layer -- classified by stage 04 instead)

collected -- real EU procurement notices, none of them contracts
  bg:  20 documents,  356 pages  ->  turned away 20/20
  de:  20 documents,  340 pages  ->  turned away 20/20
  en:  20 documents,  338 pages  ->  turned away 20/20
  es:  20 documents,  340 pages  ->  turned away 20/20
  fr:  20 documents,  345 pages  ->  turned away 20/20
  100/100 correct
```

**135 documents classified correctly, without a token spent.** The hundred from
TED matter most: they were written by European contracting authorities with no
knowledge of this system, and they are all negatives — which is where a mistake
is expensive, since a false positive buys an extraction on a document that could
never produce a contract record.

Negatives alone would not be enough, though. A vocabulary that knows no Spanish
scores 20/20 on Spanish notices, so every language carries a contract that must
*pass* as well. That is not a hypothetical: it is how the Spanish and French
terms were caught matching nothing on the day they were added.

On the paid stages, measured over three contracts including a degraded scan:

| | |
|---|---|
| field accuracy | 29/29 |
| deviations found with the knowledge base | 4/4 |
| deviations found without it | 0/4, while producing 8 findings citing nothing |

That last pair is the honest test of whether retrieval improves the result. Same
model, same contract, same extracted fields — the only difference is access to
this company's playbook and registry.

Those numbers rest on documents written for this project, and are marked as such
in [HAND_OVER.md](docs/HAND_OVER.md) — which is the reason the corpus is filed by
provenance in the first place.

## Roadmap

Working today: intake, triage, per-page loading, extraction with verified
provenance, deterministic playbook checks, agent review for what a check cannot
express, rule-based routing and a human review queue.

Still open:

- expected values for the generated contracts, which would move extraction
  accuracy off documents written for this project
- batch processing, for 50% off in exchange for latency the domain can absorb
- a sixth triage language, and ground truth in more than English

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 + SQLite (WAL) · Chroma · Anthropic
Claude Opus 5 · pytest, ruff, mypy `strict`.
