# contract-intake

An email with a vendor contract attached arrives. An agent reads it, extracts the
commercial terms, checks them against the vendor registry and the internal
contracting playbook, and routes the result: clean and on-policy contracts are
stored, anything uncertain or off-policy goes to a human with the specific reason
attached.

> **Status: phase 4 of 8.** Mail arrives, is triaged for free, loaded per page,
> extracted with verified provenance, and reviewed by an agent that cites the
> playbook section behind every finding. Routing and the review queue land
> next. See [Development plan](#development-plan).

## Why vendor contracts

Extraction alone does not need a knowledge base. Contract intake does, and for
two different reasons:

- **Entity resolution.** "NordWind Logistics Ltd." on a phone photo of a signed
  page has to become `VEN-0142` in the vendor registry — or be flagged as an
  unknown counterparty.
- **Policy validation.** "Payment terms: 90 days" is not wrong on its face. It
  is wrong *against this company's playbook*, which caps them at 45. No amount
  of model reasoning supplies that fact.

The second is the honest test of whether retrieval improves the result or just
decorates it, and the eval harness measures it by running the agent with and
without knowledge-base access.

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
success, refusal and exception alike.

Detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); every decision that could
have gone the other way, with the condition that would flip it, in
[docs/TRADEOFFS.md](docs/TRADEOFFS.md).

## Cost

Four of the seven phases spend no tokens at all. The two that do are bounded by
a per-document USD ceiling checked before each call.

The largest single lever is stage 03, which spends nothing itself but decides
**per page** between text and vision. Measured on the fixtures:

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

## Development plan

| Phase | | Status |
|---|---|---|
| 0 | Skeleton: pipeline contract, state machine, cost ledger, DB | ✅ done |
| 1 | Email intake, triage, deduplication | ✅ done |
| 2 | Document loading, extraction with provenance | ✅ done |
| 3 | Knowledge base: vendor registry + policy playbook | ✅ done |
| 4 | Agent loop with knowledge-base tools | ✅ done |
| 5 | Deterministic routing, storage, review UI | next |
| 6 | Failure paths, retries, dead letters | |
| 7 | Eval harness: extraction accuracy, KB contribution, effort sweep | |
| 8 | Documentation and walkthrough recording | |

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 + SQLite (WAL) · Chroma · Anthropic
Claude Opus 5 · pytest, ruff, mypy `strict`.
