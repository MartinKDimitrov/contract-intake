# Cost model

Every figure here is read from the `llm_calls` ledger, which every model call
writes to on success, refusal and exception alike. Nothing is estimated.

Measured on `claude-opus-5`, first-party API list pricing ($5 / $25 per MTok),
against the authored documents that carry expected values.

---

## Where the money goes

Five of the seven pipeline phases spend nothing at all. Intake, triage, loading,
routing and delivery are heuristics, comparisons and writes.

| phase      | tokens             | note                               |
|------------|--------------------|------------------------------------|
| 01 receive | 0                  | IMAP, dedupe                       |
| 02 triage  | 0                  | magic bytes, vocabulary scan       |
| 03 load    | 0                  | but sets most of stage 04's bill   |
| 04 extract | LLM                | one structured call                |
| 05 enrich  | LLM, conditionally | skipped when a check already fired |
| 06 decide  | 0                  | deterministic rules                |
| 07 deliver | 0                  | writes                             |

Within a paid call, output tokens dominate:

|             | extract | enrich  |
|-------------|---------|---------|
| input       | 12%     | 11%     |
| cache read  | 2%      | 4%      |
| cache write | 22%     | 30%     |
| **output**  | **64%** | **55%** |

So the bill is what the model *thinks and writes*, not what it reads. Input is
nearly free once the prefix is cached. Every lever below follows from that.

---

## The levers, and what each was measured to be worth

### 1. Per-page text versus vision — stage 03

The largest single decision, and it costs nothing to make.

|                                  | tokens |
|----------------------------------|--------|
| a page with a text layer         | ~250   |
| the same page rendered at 1400px | ~1850  |

**7.4x.** Because the decision is per page rather than per document, a
born-digital contract with one scanned signature page sends nineteen pages of
text and exactly one image.

### 2. Prompt caching

The system prompt and JSON schema are a stable prefix, cached and re-read at a
tenth of the input rate from the second document onward.

The agent loop matters more here: the tool runner resends the whole conversation
every iteration, so without caching its input cost grows quadratically in turns.

|                                 | input tokens |
|---------------------------------|--------------|
| 13-tool-call review, uncached   | 16,462       |
| the same review, history cached | 6            |

Everything else is served from cache. Note this only pays off across documents
inside the cache TTL — processing one document in isolation writes a cache
nobody reads, which is why single-run figures are the pessimistic case.

### 3. Deterministic thresholds — stage 05

Every playbook rule expressible as a comparison is evaluated in Python. The
agent runs only for documents that pass all of them, because a document that
already fails three checks is going to a human whatever the model says.

|                                              | per document |
|----------------------------------------------|--------------|
| everything through the model (first version) | $0.147       |
| thresholds in code, agent for the rest       | $0.105       |
| at a 50% deviation rate                      | $0.074       |
| at a 70% deviation rate                      | $0.061       |

The worse the incoming stream, the cheaper it runs. The agent's own work also
shrank from 8–13 tool calls to 5–6 once its prompt said the numbers were settled.

### 4. Content-addressed deduplication — stage 01

A document already seen never reaches a model again, whether it arrives twice or
is forwarded under a different message. Zero tokens, not fewer.

### 5. Effort

Extraction accuracy against cost, measured over 29 fields on three documents:

| effort | correct | accuracy | USD (3 docs) | sec/doc |
|--------|---------|----------|--------------|---------|
| low    | 29/29   | 100%     | $0.106       | 8.6     |
| medium | 29/29   | 100%     | $0.112       | 8.4     |
| high   | 29/29   | 100%     | $0.142       | 13.3    |

`high` costs 27% more than `medium` for no measured gain, so the default is
`medium`. `low` is cheaper again, but by six tenths of a cent — not enough to
justify the risk on a sample this size, and the one time an apparently-clean
optimisation was taken on this project it silently cost a real finding.

---

## What was measured and rejected

### A cheaper model for enrichment

`claude-haiku-4-5` is 5x cheaper per output token, and enrichment was 71% of the
bill, so this looked like the obvious move.

|           | enrichment | findings on the deviations fixture     |
|-----------|------------|----------------------------------------|
| Opus 5    | $0.105     | 6, including the subtlest              |
| Haiku 4.5 | $0.016     | 6, but one false positive and one miss |

**6.7x cheaper, and rejected.** Haiku flagged a compliant payment term (reading
the 45–90 day range as exclusive at the top) and missed that the contract had no
termination-for-convenience right at all.

The two errors are not symmetric. A spurious finding costs a reviewer thirty
seconds. A missed one, if it were the only deviation, would let a contract with
no exit right auto-approve in silence. The per-stage model setting stayed, so the
decision is a config change once there is a larger sample to decide it on.

### Dropping the runners-up from policy retrieval

Returning the body of only the top hit cut 31%. It also lost a real deviation:
for "renews automatically", §2.1 *Initial term* scored 0.475 and §2.2 *Automatic
renewal* scored 0.474, so the agent received the wrong clause in full and the
right one as a bare title. Trimming every hit instead came out cheaper still.

---

## What the whole system costs

Per document, at `effort=medium`, depending on how many contracts deviate:

| incoming stream                            | per document |
|--------------------------------------------|--------------|
| everything compliant (worst case for cost) | ~$0.105      |
| half deviating                             | ~$0.074      |
| mostly deviating                           | ~$0.061      |

At a thousand contracts a month that is roughly **$60–105**, against the
alternative of a person reading each one.

Guardrails: a per-document USD ceiling checked before every call, so a runaway
agent loop costs one stage rather than an invoice; `max_iterations` on the loop;
and a ledger that makes any of this checkable rather than asserted.

```bash
make costs
curl :8000/metrics/costs
```

---

## Not yet measured

**A local model for extraction.** Only text-layer documents are a plausible
candidate — scanned pages are where local vision models are weakest, and the
agent loop is where they are weakest again. The method is written up in
[HAND_OVER.md](HAND_OVER.md); the eval harness is the instrument that would
decide it, and provenance verification gives a free partial signal, since a model
that invents quotes shows up as `not_found` without any labelling.

**Batch processing.** The Batches API is 50% cheaper for up to 24 hours of
latency, which contract intake by email can absorb. It needs an async submission
and polling path, which is more change than the saving justified so far.
