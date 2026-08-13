# Trade-offs

Every row is a decision that could reasonably have gone the other way. The last
column is the one that matters: the condition under which this choice stops
being right.

Written as decisions are made, not reconstructed afterwards.

---

## Architecture

### One file per pipeline phase

**Chose:** seven files under `pipeline/`, one per phase, plus a `Stage` protocol.

**Instead of:** a single `pipeline.py` with seven functions.

**Why:** the phase boundaries have to be real anyway — the resumable state
machine (below) needs them — so making them physical costs little and buys a lot
of navigability: open `stage_04_extract.py` and you have the whole phase, its
contract, its failure modes and its token cost in one screen. It also makes
`make stage N=04 ID=17` natural, which matters more than it sounds: re-tuning
extraction without paying to re-run loading is most of the inner development
loop.

**Cost, honestly:** for ~2000 lines this risks reading as ceremony; a single
module gets perhaps 80% of the benefit. It also weakens type safety, since data
now travels between phases through the database rather than through function
arguments — mitigated by re-validating into the Pydantic model at each stage
boundary so a malformed hand-off fails loudly.

**Stops being right when:** phases start needing to share intermediate state
that does not belong in the database, or when the file count grows past what one
`ls` shows. Guardrail: exactly seven stages, and a stage that carries no logic of
its own is not created.

### `Source` is separate from `Stage`

**Chose:** intake implements `Source`, not `Stage`.

**Why:** stage 01 consumes no attachment — it *creates* them, fanning one email
into N rows. Forcing it into `consumes: Status` would have made that field a lie
in one of seven files.

**Cost:** the pipeline is not uniform end to end; `runner.py` drives six stages
and the worker drives intake separately.

### Status column as the work queue

**Chose:** `attachments.status` drives everything; the worker polls for the
oldest row in a non-terminal status.

**Instead of:** Celery/RQ with Redis, or an in-process `asyncio.Queue`.

**Why:** a broker is a second piece of infrastructure that buys nothing here.
Durable status in the database gives crash resumption for free
— a document interrupted mid-extraction is picked up at exactly that phase — and
the entire system state is one `sqlite3` query away. An in-memory queue would
lose work on restart, which is strictly worse than both.

**Stops being right when:** more than one worker process runs concurrently. Two
pollers will both claim the same row. The fix is `SELECT ... FOR UPDATE SKIP
LOCKED` on Postgres, which is also the point at which SQLite has to go.

### `validate_chain()` runs at import

**Chose:** the stage chain is verified contiguous when `runner` is imported.

**Why:** this is the specific failure mode that one-file-per-phase introduces —
rename a status in one file, forget its neighbour, and documents advance into a
status nobody consumes, then stall silently forever. Import-time validation
turns a silent stall into a startup crash.

---

## Storage

### SQLite, not Postgres

**Chose:** SQLite in WAL mode, one file under `var/`.

**Why:** nothing to provision, and one file to inspect or delete. WAL gives the
review UI a concurrent reader while the worker
writes, which is the only concurrency this design actually needs.

**Cost:** single-writer. No `SKIP LOCKED`. Type affinity rather than real types.

**Stops being right when:** a second worker process, or a reviewer team large
enough that read contention matters. `DATABASE_URL` is the only change in code;
the concurrency strategy in `runner.pick_next` is the real work.

### No Alembic

**Chose:** `Base.metadata.create_all()` plus a `schema_version` column.

**Why:** there is no deployed instance to migrate. Migrations solve a problem
this project does not have yet.

**Stops being right when:** anything runs against data you cannot recreate.
Tracked in `HAND_OVER.md`.

### Chroma for ~65 vectors

**Chose:** Chroma persistent client, file-based, using its bundled ONNX
embedding model.

**Instead of:** `sqlite-vec` (one datastore instead of two), or a plain NumPy
array (honestly sufficient at this size).

**Why:** a real vector store with no server to run and no operational burden.
Its built-in embedder means no extra dependency at all — importantly no PyTorch,
which would have added ~2 GB to an install.

**Cost:** heavier dependency than the data justifies; retrieval quality is
marginally below a dedicated e5-class model, which is immaterial across 15
policy clauses.

**Stops being right when:** the knowledge base outgrows a single machine's
memory, or needs to be shared between processes. That is pgvector's territory.

---

## Cost and models

### A wrapper is the only way to call the model

**Chose:** every model call goes through `llm/client.py`, which writes a row to
`llm_calls` on success, refusal *and* exception, and refuses to start a call
that would push a document past its USD ceiling.

**Why:** "we thought about cost" is a claim; a ledger is evidence. Making the
wrapper the sole entry point means there is no code path that spends money
without recording it, so the numbers in `COST_MODEL.md` are measured rather than
estimated. The budget ceiling bounds the one stage whose token use is not fixed
in advance — the agent loop.

**Cost:** every stage takes an `LLMClient` rather than reaching for the SDK, and
the wrapper has to be kept in step with SDK changes.

### An unpriced model is a hard error

**Chose:** `rates_for()` raises rather than defaulting to zero.

**Why:** a silently unpriced call makes the ledger under-report, and an
under-reporting ledger is worse than none. Better to fail on an unknown model.

### Per-page text-vs-vision decision

**Chose:** stage 03 decides per page, not per document.

**Why:** the single largest cost lever in the system. A 20-page contract as text
is roughly 15k tokens; as page images, roughly 95k. A born-digital contract with
one scanned signature page sends 19 text pages and exactly one image.

### No OCR engine

**Chose:** pages without a text layer go to the model as images; Claude reads
them.

**Instead of:** Tesseract via `pytesseract`.

**Why:** drops a system-level dependency, and on noisy scans direct vision reads
better than OCR-then-text. We are already paying
for a vision-capable model.

**Cost:** image pages are the expensive path, so this trades tokens for
simplicity and accuracy.

**Stops being right when:** volume makes per-page vision tokens dominate the
bill. At that point OCR-first with vision as fallback is the cheaper shape.

### Routing is deterministic

**Chose:** stage 06 is pure Python. The model proposes findings with evidence;
rules decide.

**Why:** an LLM asked to "decide whether to auto-approve" produces an answer
that cannot be unit-tested, cannot be audited by a lawyer, and drifts between
model versions. `payment_terms_days > ceiling -> needs_review, citing S3.2` can
be tested exhaustively and explained in one sentence.

### Deterministic thresholds, and calling the agent only when they pass

**Chose:** every playbook threshold that can be written as a comparison lives in
`playbook_checks.json` and is evaluated in Python. The agent runs only for
documents that pass all of them.

**Instead of:** the agent retrieving each clause, reading the numbers out of
prose, and doing the comparison itself.

**How we got here.** The first working version put everything through the model.
It produced good findings, each citing a section, and cost about $0.105 per
document for enrichment alone — 71% of a $0.147 total, with output tokens making
up 55% of that. Two attempts to reduce it went nowhere useful. Trimming tool
results saved 31% and cost a real deviation (recorded above). Moving enrichment
to a cheaper model was measured properly: 6.7x cheaper, but it missed the
subtlest finding and added a false positive, and the two errors are not
symmetric — a spurious finding costs a reviewer thirty seconds, while a missed
one can let a contract with no exit right auto-approve silently.

Looking at what the agent actually did on a typical document made the answer
obvious. For §1.1 it retrieved the clause, read "45 to 90 days", compared it to
90, and decided. That is arithmetic, performed by a frontier model,
non-deterministically, in a system whose stated principle is that the model
proposes and the code decides. The principle was only half true.

**Why this is not only cheaper.** Anything expressible as a comparison is now
exhaustively testable and produces identical output every run. What remains with
the agent is what a comparison cannot carry: that a 90-day non-renewal window is
not a termination-for-convenience right; that the registry implies an obligation
the contract is silent about; that a clause is simply unusual.

**Measured:** enrichment falls to $0 for a document that already fails a check,
and the agent's own work shrank from 8–13 tool calls to 5–6 once its prompt said
the numeric thresholds were already settled. Per document: $0.147 → $0.105 when
everything passes, → $0.074 at a 50% deviation rate, → $0.061 at 70%. The worse
the incoming stream, the cheaper it runs, because bad contracts are caught for
nothing.

**Cost, honestly:** skipping the agent on a document that already failed a check
forfeits its judgement findings for that document. The §2.3 observation — that
the deviations fixture has no termination-for-convenience right at all — no
longer appears, because four other checks stopped it first. The defence is that
the document is going to a human with four citations anyway, and a lawyer reading
it will see the missing clause. That is a defence, not a free lunch.

There are now two files encoding the same policy: `playbook.md` for the agent to
retrieve and a human to maintain, and `playbook_checks.json` for the machine.
They can drift. A test asserts every section cited by a check exists in the prose.

**Stops being right when:** the checkable rules grow complex enough that the JSON
becomes a small programming language. At that point they should be Python
functions in `policy/`, tested directly, rather than data.

### Trimming tool results, not dropping them

**Chose:** every policy hit comes back with its clause body, capped at ~420
characters.

**Instead of:** returning the body of the top hit only, and just a section and
title for the runners-up.

**Why:** the agent loop resends every tool result on every later turn, so a
verbose result is paid for repeatedly -- the first version of this stage spent
$0.167 on one contract, with 16k input tokens. Returning only the top hit's text
cut that to $0.115.

It also lost a real deviation. For "renews automatically", §2.1 *Initial term*
scored 0.475 and §2.2 *Automatic renewal* scored 0.474 -- a margin of 0.001. The
agent was handed the wrong clause in full and the right one as a bare title, and
did not record the finding. A title names the topic; only the body carries the
rule.

Trimming instead of dropping came to $0.105 -- cheaper than both -- with the
deviation back. The lesson is specific enough to be worth writing down:
**retrieval at these margins is not reliable enough to decide which hit is worth
reading.** Trimming is the safe economy; dropping is not.

**Cost:** ~1.2k tokens per search instead of ~500.

**Stops being right when:** the playbook grows to clauses long enough that 420
characters truncates the rule itself rather than its rationale.

### Caching the agent's conversation

**Chose:** top-level `cache_control` on the tool-runner call, not just on the
system prompt.

**Why:** the runner resends the whole conversation each iteration, so input cost
grows quadratically in turns. With the history cached, input tokens on a
13-tool-call review fell from 16,462 to 6 -- everything else is a cache read.

**Consequence:** output tokens now dominate the agent's bill at roughly 65%.
Further savings have to come from thinking depth, not from context size.

### Extraction and enrichment are separate calls

**Chose:** one deterministic structured-output call, then a separate agent loop.

**Why:** fused, neither is measurable. Split, extraction accuracy can be
evaluated without agent non-determinism in the way, and the knowledge base's
contribution can be isolated by running enrichment with and without KB access —
which is the only honest answer to "does the KB improve the result, or is it
decoration?"

**Cost:** two calls instead of one, and the extraction result has to be
round-tripped through the database.

---

## Setup

### `venv` + `pip`, not `uv`

**Chose:** stdlib `venv` and `pip`, driven by `make setup`.

**Why:** `uv` is faster and nicer, and is one more thing to install first.
Nothing should stand between `git clone` and a running system.

### Python 3.12, not 3.14

**Chose:** pinned `>=3.12,<3.14`.

**Why:** 3.14 is new enough that several dependencies have no wheels yet, which
turns `make setup` into a compiler run.
