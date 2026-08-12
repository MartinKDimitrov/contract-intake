# Architecture

> Status: phase 0 (skeleton). Sections marked _pending_ are filled in as their
> phase lands. Decisions and their alternatives live in [TRADEOFFS.md](TRADEOFFS.md).

## The shape

An email with a contract attached arrives; a structured, validated record comes
out the other end — either into `contracts` if everything is clean and on
policy, or into a human review queue with the specific reason it was not.

```
                    ┌──────────────────────────────────────────────┐
   IMAP / webhook ──▶│ 01 receive   Source     →  RECEIVED         │  0 tokens
                    ├──────────────────────────────────────────────┤
                    │ 02 triage    RECEIVED   →  TRIAGED           │  0 tokens
                    │ 03 load      TRIAGED    →  LOADED            │  0 tokens  ← cost lever
                    │ 04 extract   LOADED     →  EXTRACTED         │  LLM, structured
                    │ 05 enrich    EXTRACTED  →  ENRICHED          │  LLM, agent + RAG
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
| `documents` | 03 | page count and the per-page text/image decision |
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

## Extraction with provenance

Every extracted field carries `value`, `confidence`, `source_quote` and `page`.
A quote that cannot be located in the loaded text fails validation and drives
that field's confidence to zero.

This is the difference between a system that knows and one that guessed, and it
is the only honest input to routing — stage 06 cannot make a defensible decision
from a bare value with no evidence behind it.

_Schema detail: pending (phase 2)._

## Knowledge base

Two jobs, two retrieval methods, because they are not the same problem:

| Collection | Job | Method |
|---|---|---|
| `vendors` | resolve "NordWind Logistics Ltd." on a scan to a registry entry | fuzzy string matching, embedding fallback |
| `policy` | check "payment terms: 90 days" against the playbook ceiling | dense retrieval, returns the clause and its section |
| `precedents` | give a reviewer approved wording for a deviation | dense retrieval |

Policy validation is the part the model cannot do alone — no amount of reasoning
tells it what *this company's* liability ceiling is.

_Detail: pending (phase 3)._

## Cost model

Four phases spend nothing. Of the two that do, stage 03 — which spends nothing
itself — determines most of stage 04's bill by deciding per page between text
(cheap) and vision (roughly 6× more for a full document).

`llm/client.py` is the only path to the API, and it writes to `llm_calls` on
success, refusal and exception alike. Nothing can spend without being recorded,
which is what makes the published numbers measured rather than estimated.

Full breakdown: [COST_MODEL.md](COST_MODEL.md) _(pending, phase 7)_.

## Running it

```
make setup     # venv + dependencies
make test      # hermetic; no API key needed
make lint      # ruff + mypy strict
make run       # review UI, /healthz, /metrics/costs
make poll      # IMAP poller + pipeline worker
make stage N=04 ID=17
```
