# Architecture

Decisions and the alternatives they beat live in [TRADEOFFS.md](TRADEOFFS.md);
what it costs and why in [COST_MODEL.md](COST_MODEL.md); how to run and extend it
in [HAND_OVER.md](HAND_OVER.md).

## The shape

An email with a contract attached arrives; a structured, validated record comes
out the other end — either into `contracts` if everything is clean and on
policy, or into a human review queue with the specific reason it was not.

```
                    ┌──────────────────────────────────────────────┐
   IMAP / webhook ──▶│ 01 receive   Source     →  RECEIVED         │  0 tokens
                    ├──────────────────────────────────────────────┤
                    │ 02 triage    RECEIVED   →  TRIAGED           │  0 tokens
                    │ 03 load      TRIAGED    →  LOADED            │  0 tokens  ← cost lever;
                    │                                                  masks personal data
                    │ 04 extract   LOADED     →  EXTRACTED         │  LLM, structured
                    │ 05 enrich    EXTRACTED  →  ENRICHED          │  checks free,
                    │                                                 agent only if
                    │                                                 they all pass
                    │ 06 decide    ENRICHED   →  DECIDED           │  0 tokens
                    │ 07 deliver   DECIDED    →  DELIVERED         │  0 tokens
                    └──────────────────────────────────────────────┘
                                        │
                        ┌───────────────┴───────────────┐
                   contracts                      review_items
```

Four of seven phases spend nothing. That is deliberate: the cheapest token is
the one never sent.

## One phase, one file

Every phase lives in `src/contract_intake/pipeline/stage_NN_name.py` and opens
with its own contract — what it does, what status it consumes and produces, what
it costs in tokens, and how it can fail. To understand a phase you open one file.

The whole flow is the `STAGES` tuple in [`pipeline/runner.py`](../src/contract_intake/pipeline/runner.py),
or:

```
make setup && .venv/bin/python -m contract_intake.cli stages
```

Supporting machinery sits one level down and is used *by* stages, never the
other way around: `adapters/` (01), `loaders/` (03), `extract/` (04),
`knowledge/` + `agent/` (05), `policy/` (06), `store/` + `web/` (07).

## The state machine

`attachments.status` is the work queue — there is no broker. The worker picks
the oldest attachment in a status some stage consumes, runs exactly that stage,
and persists the result.

```
RECEIVED → TRIAGED → LOADED → EXTRACTED → ENRICHED → DECIDED → DELIVERED
                                                                REJECTED   expected "no"
                                                                DEAD       retries exhausted
```

Three consequences worth naming:

- **Crash resumption is free.** A process killed mid-extraction leaves the row
  in `LOADED`; the next start picks it up there.
- **Any phase can be replayed in isolation** — `make stage N=04 ID=17` — which
  is most of the inner development loop, since re-tuning extraction does not pay
  to re-run loading.
- **The whole system state is one query away**, with no broker to inspect.

`REJECTED` and `DEAD` are different on purpose. A password-protected PDF or an
invoice sent to the contracts address is an expected outcome, not an incident:
no retry, no dead letter, no alert. `DEAD` means the pipeline could not finish
something it should have, and always leaves a replayable `dead_letters` row.

### Stage contract

```python
class Stage(Protocol):
    number:   ClassVar[int]
    name:     ClassVar[str]
    consumes: ClassVar[Status]
    produces: ClassVar[Status]
    uses_llm: ClassVar[bool]
    async def run(self, ctx: StageContext) -> StageOutcome: ...
```

`StageOutcome` is `Advanced | Rejected | Failed`. `Failed` carries `retryable`,
which is what separates a 429 from an encrypted file.

`validate_chain()` runs at import and asserts stage N's `produces` equals stage
N+1's `consumes`. This guards the one real hazard of splitting phases across
files: rename a status in one place, forget the other, and documents advance
into a status nobody consumes and stall silently. Now that is a startup crash.

Intake is the deliberate exception — it implements `Source`, not `Stage`,
because it creates attachments rather than advancing one. See TRADEOFFS.md.

## Data model

| Table | Written by | Holds |
|---|---|---|
| `emails` | 01 | one row per message, deduplicated on `Message-ID` |
| `attachments` | 01 | one row per file; **`status` is the queue** |
| `documents` | 03 | page count, the per-page text/image decision, masked-item counts |
| `extractions` | 04 | fields, each with confidence, source quote and page |
| `enrichments` | 05 | agent findings plus the full tool trace |
| `decisions` | 06 | route, reasons, blocking fields |
| `contracts` | 07 | the clean record downstream systems read |
| `review_items` | 07 | the human queue |
| `llm_calls` | `llm/client.py` | **the cost ledger** — every call, no exceptions |
| `dead_letters` | `runner` | anything unfinishable, with enough context to replay |

`enrichments.tool_trace` is persisted in full and rendered next to each finding
in the review UI: it is the evidence that the knowledge base changed the outcome
rather than decorating it.

## Personal data

Stage 04 discloses contract text to a third-party processor, so a closed set of
personal identifiers is masked in `loaders/redact.py` at the point page text is
produced — before the model sees it and before it is written to `documents`.

Two things make this safe to leave on by default. Every category is validated by
checksum rather than matched by shape, because a company UIC and a personal
number are both runs of digits and `counterparty_registration_id` is a field the
pipeline extracts. And the masked text *is* the stored text, so quote
verification searches exactly what the model was given.

An image page has no text layer, so a scanned contract reaches the model as
photographed. That gap is real; the reasoning and the alternatives are in
[TRADEOFFS.md](TRADEOFFS.md).

## Extraction with provenance

Every extracted field carries `value`, `confidence`, `source_quote` and `page`.
A quote that cannot be located in the loaded text fails validation and drives
that field's confidence to zero.

This is the difference between a system that knows and one that guessed, and it
is the only honest input to routing — stage 06 cannot make a defensible decision
from a bare value with no evidence behind it.

The schema is fifteen fields in `extract/schema.py`, each wrapped in `Evidence`.
`REQUIRED_FOR_AUTO_APPROVAL` names the five that must be present and confident
before a contract can pass without a human.

## Knowledge base

Two jobs, two retrieval methods, because they are not the same problem:

| Collection | Job | Method |
|---|---|---|
| `vendors` | resolve "NordWind Logistics Ltd." on a scan to a registry entry | fuzzy string matching, embedding fallback |
| `policy` | check "payment terms: 90 days" against the playbook ceiling | dense retrieval, returns the clause and its section |
| `precedents` | give a reviewer approved wording for a deviation | dense retrieval |

Policy validation is the part the model cannot do alone — no amount of reasoning
tells it what *this company's* liability ceiling is.

Both are consulted before the agent, not by it. Counterparty resolution is a
pure function over a closed registry; the threshold half of the playbook lives in
`playbook_checks.json` and is evaluated in Python. The agent is called only for
documents that pass every check, and its job is what a comparison cannot express
— an absent right, a conflict between sources, an unusual clause.

## Cost model

Four phases spend nothing. Of the two that do, stage 03 — which spends nothing
itself — determines most of stage 04's bill by deciding per page between text
(cheap) and vision (roughly 6× more for a full document).

`llm/client.py` is the only path to the API. It writes to `llm_calls` on
success, refusal and exception alike — nothing can spend without being recorded,
which is what makes the published numbers measured rather than estimated — and
it refuses to call at all once a single document has spent
`max_usd_per_document`, checked *before* each request rather than after.

A document that has been seen before never gets that far: stage 01 deduplicates
on the attachment `sha256`, so the same PDF arriving twice under two names
enters the pipeline once.

Deterministic checks run before the agent, so a document that already fails one
costs nothing to enrich. Full breakdown: [COST_MODEL.md](COST_MODEL.md).

## What it is tested against

The free stages run over every document on every check, because they cost
nothing to run and everything to get wrong.

`evals/documents/` is filed by provenance — `authored/`, `generated/` (split by
the model that wrote each one), `collected/` — because where a document came
from decides what a result on it is worth. A hundred real TED procurement
notices in five languages are evidence; six documents I wrote myself are a
demonstration that the intended path runs. `make triage` reports them
separately rather than averaging them into one number.

The collected corpus is entirely negative, so it is paired with contracts in
each of the five languages that must pass. Detail and the defect that pairing
caught: [evals/documents/README.md](../evals/documents/README.md).

## Running it

```
make setup     # venv + dependencies
make test      # hermetic; no API key needed
make lint      # ruff + mypy strict
make run       # review UI, /healthz, /metrics/costs
make poll      # IMAP poller + pipeline worker
make stage N=04 ID=17
make triage    # classify every document, by provenance, free
make eval      # field accuracy and knowledge-base contribution (costs money)
make dead      # what could not be finished, and why
```
