# Hand-over

What someone taking this over needs to know: how to run it, what is real and
what is stubbed, what will break first, and what to build next.

---

## Running it

```bash
make setup            # venv, dependencies, embedding model, fixtures
cp .env.example .env  # then fill in the values below
make test             # 238 tests, hermetic -- no API key, no network
make triage           # classify 93 documents, free
make run              # review queue on :8000
make poll             # fetch mail and run the pipeline
```

Python 3.12. `make setup` also downloads Chroma's ONNX embedding model (~80MB,
once) and generates the fixture PDFs.

### Configuration

| variable | what breaks without it |
|---|---|
| `ANTHROPIC_API_KEY` | stages 04 and 05; everything else still runs |
| `CI_IMAP_USER`, `CI_IMAP_PASSWORD` | intake. Gmail needs an App Password, not the account password |
| `CI_IMAP_FOLDER` | **read the warning below** |

Everything else has a working default; see `.env.example`.

### The IMAP folder is not optional

Point `CI_IMAP_FOLDER` at a dedicated label fed by a provider-side filter, never
at `INBOX`. Intake extracts every attachment it finds and sends it to a model.
Against a personal mailbox that is a privacy incident and a cost incident at the
same time, and one wrong environment variable is all it takes.

The code refuses to poll `INBOX` above a hundred messages
(`adapters.imap.UnsafeMailboxError`), but that guard is a backstop, not the
design. Set up the filter.

For Gmail: a filter on `you+contracts@gmail.com` → *Skip the Inbox* → *Apply
label*, then point `CI_IMAP_FOLDER` at that label.

---

## What is real and what is not

**Real:** the pipeline, the state machine, retries and dead letters, extraction
with verified provenance, both retrieval methods, the deterministic checks, the
routing rules, the review queue, and the cost ledger. All of it runs against a
live mailbox and a live API.

**Synthetic, and must be replaced before this is useful to anyone:**

| file | what it is |
|---|---|
| `knowledge/data/vendors.json` | 20 invented suppliers. No real company appears. |
| `knowledge/data/playbook.md` | An invented contracting policy. |
| `knowledge/data/playbook_checks.json` | The machine-checkable half of the same. |
| `evals/fixtures/source/*.txt` | Generated contracts and lookalikes. |

The vendor registry is a JSON file because that is the smallest thing that
demonstrates entity resolution. In practice it is a view over a supplier master
in an ERP, and `knowledge/vendors.py:load_registry` is the seam.

The playbook is two files encoding one policy: prose the agent retrieves and a
human maintains, and JSON the machine evaluates. They can drift, and a test
asserts every section cited by a check still exists in the prose. Anything
expressible as a comparison belongs in the JSON; anything needing judgement
stays prose.

---

## What the numbers rest on

| claim | measured on |
|---|---|
| Triage: 93/93 correct, zero tokens | 33 fixtures + 60 real TED notices in bg/de/en, 1,034 pages |
| Extraction: 29/29 fields | 3 generated contracts, including a degraded scan |
| Knowledge base: 4/4 deviations found, 0/4 without it | 1 contract with four known deviations |
| Cost: $0.061–0.105 per document | the ledger, across the runs above |

The triage number is the strong one: sixty of those documents were written by
European contracting authorities with no knowledge of this system.

**The extraction and knowledge-base numbers are not.** They rest on three
documents that I wrote, with expected values that I also wrote. 100% on 29
fields is encouraging and is not evidence. Extending `evals/expected/` over the
ten real contracts already in `evals/fixtures/` is the first thing to do, and
costs about $0.70.

---

## What will break first

**A second worker process.** `runner.pick_next` has no locking, so two pollers
will claim the same row. This is the point at which SQLite has to go: the fix is
`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres, and `DATABASE_URL` is the only
change in application code.

**Schema changes against data you cannot recreate.** There are no migrations —
`Base.metadata.create_all()` plus a `schema_version` column. Add Alembic before
the first deployment that holds real contracts.

**A contract longer than 120 pages** is rejected by triage as a probable bundle
(`loaders/pdf.MAX_REASONABLE_PAGES`). That threshold is a guess.

**A document in a fourth language.** The triage vocabulary covers English,
Bulgarian and German. A French or Romanian contract will be turned away with
"0 instrument markers" and never reach a model. Extending it is a data change in
`stage_02_triage.py`, and the corpus check is how you would know it worked.

**Prompt cache economics on a sparse queue.** Cache writes are 22–30% of a call.
Processing one document in isolation writes a cache nobody reads. The published
per-document figures are therefore the pessimistic case; a real queue amortises
them.

---

## Runbook

**Documents stop moving.** `make dead` lists what could not be finished, with
the stage and error class. `contract-intake dead --replay N` rewinds a document
to the phase it died in. If nothing is dead, check `attachments.retry_after` —
a backed-off row is waiting, not stuck.

**Costs jump.** `make costs`, then group by purpose. A rise in `enrich` means
more documents are passing the deterministic checks and reaching the agent. A
rise in `extract` usually means more scanned pages: check `documents.image_pages`.

**The model refuses.** Recorded as a dead letter with `RefusalError` and a
category. Not retryable — the same prompt will be refused again. A human decides.

**Everything routes to review.** Check that the vendor registry loaded
(`make knowledge`) and that the counterparty resolves. An empty or unreachable
registry makes §7.2 fire on every document.

**A document was auto-approved that should not have been.** The evidence is all
persisted: `extractions.fields` has every value with its quote and confidence,
`enrichments.tool_trace` has every lookup, `decisions.reasons` has what the rules
saw. Start with the provenance status of the field that was wrong.

---

## Next, in order

**1. Ground truth on the real contracts.** `evals/fixtures/` already holds ten
real contracts — addenda, bilingual leases, an NDA, an SLA — that have passed
triage but never been extracted. Writing `evals/expected/*.json` for them turns
the extraction number from three documents to thirteen. About $0.70 and an hour.

**2. Batch processing.** The Batches API is 50% cheaper for up to 24 hours of
latency, which contract intake by email can absorb comfortably. It needs an
async submit-and-poll path in stage 04, which is why it has not been done.

**3. A local model for extraction, measured rather than assumed.** Only
text-layer documents are a plausible candidate; scanned pages are where local
vision models are weakest and the agent loop is where they are weakest again.
The method:

- Run the local model through `LLMClient` behind an OpenAI-compatible adapter
  (`llm/client.py` is the only place that talks to a provider).
- Score with `evals/run.py` against the expected files from step 1.
- Use provenance verification as a free first filter: a model that invents
  quotes shows up as `not_found` with no labelling at all.
- Adopt only if field accuracy holds on the scanned fixture too, and route
  image pages to the API regardless.

**4. Cheaper enrichment, once there is a sample to decide on.** `claude-haiku-4-5`
was 6.7x cheaper and missed one finding while adding a false positive
([COST_MODEL.md](COST_MODEL.md)). The per-stage model setting already exists;
the decision needs more than three documents.

**5. A fourth language.** French or Romanian, following the pattern in
`stage_02_triage.py` and verified with `make corpus && make triage`.

---

## Things that look like bugs and are not

**Rejection leaves no dead letter.** An invoice mailed to the contracts address
is an expected outcome, not an incident: no retry, no alert, and the reason is
recorded on the attachment.

**A scanned contract never auto-approves**, however compliant it looks. Every
value in it rests on a photograph that nothing verified — see
`rule_wholly_unverifiable`.

**A quote on a scanned page reads `unverifiable`, not `verified`.** There is no
text layer to check it against. That is different from a quote that was checked
and not found, which zeroes the field.

**The agent is sometimes not called at all.** If the deterministic checks already
found a deviation, the document is going to a human regardless, so the agent adds
cost and nothing else. The stage note says `(agent not needed)`.
