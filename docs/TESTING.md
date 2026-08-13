# Testing

What is checked, how, and — the part that matters — what a green run does not
prove.

```bash
make test      # 323 tests, hermetic: no API key, no network
make lint      # ruff + ruff format --check + mypy strict
make triage    # classify every document with the real stage 02, free
make corpus    # fetch 100 real EU procurement notices (network)
make eval      # field accuracy and knowledge-base contribution (costs money)
```

The first three run in CI on every push. The last two do not, for reasons given
below.

## Four layers, and what each one is for

| layer | what it runs against | catches | cost |
|---|---|---|---|
| unit and integration | hand-built inputs, an in-memory database, a stubbed model | logic errors in a single unit | free |
| corpus classification | the real stage 02, over 136 documents in five languages | vocabulary fitted to the author's own documents | free |
| provenance and policy invariants | pure functions, adversarial inputs | values reaching auto-approval without evidence | free |
| end-to-end evaluation | the live API, real documents, expected values | extraction accuracy, whether retrieval changes the answer | ~$0.25 |

The suite is hermetic by construction: `llm/client.py` is the only path to a
model, and nothing under `tests/` constructs one against a real key. That is why
CI needs no secrets, and why a green CI run means the same thing a green local
run means.

## The corpus, and why provenance is filed in folders

`evals/documents/` separates documents by where they came from, because that
decides what a result on them is worth — the full argument is in
[evals/documents/README.md](../evals/documents/README.md).

| folder | n | what a result proves |
|---|---|---|
| `authored/` | 6 | that the intended path runs. I wrote the document *and* the expected answer, so agreement between them is not a measurement. |
| `generated/` | 30 | breadth of document type. Written by Gemini, ChatGPT and Grok, split by model, because three generators disagree in useful ways. |
| `collected/` | 100 | that it works on documents nobody wrote for it. Real TED procurement notices in five languages. |

`make triage` reports the folders separately rather than averaging them into one
number.

### Negatives alone cannot test a vocabulary

The collected corpus is entirely negative — a procurement notice is not a
contract — so a vocabulary that knows no Spanish scores 20/20 on Spanish
notices. That is not hypothetical: the Spanish and French terms were added, the
hundred notices were classified correctly, and the two Spanish and French
contracts added at the same time were *both rejected*. The terms had gone into
the wrong constant and were matching nothing.

Every language therefore carries a contract that must pass, in `authored/` and
in `tests/pipeline/test_stage_02_triage.py`.

## Tests that exist because something got through

Some test files guard a specific defect rather than a unit. They are worth
knowing about, because each one encodes a mistake that a green suite did not
catch at the time:

- **`tests/extract/test_verification_bypasses.py`** — four ways a model could
  once defeat provenance verification: attribute a quote to a scanned page,
  keep it under the minimum length, cite real boilerplate, or cite a real clause
  about a different number. Each ran against a document with no payment clause
  and produced an auto-approved 60-day payment term. The file opens with a
  control, so it cannot pass on a broken system.
- **`tests/loaders/test_redact.py`** — the "must survive" half is longer than
  the "must be masked" half, because over-masking is the worse failure: it
  destroys `counterparty_registration_id` silently. It carries the real French
  SIRET that the first version of the redactor masked as a payment card.
- **`tests/test_schema_guard.py`** — `create_all` adds missing tables and
  ignores missing columns, so a model that gains a field leaves an existing
  database one column short. That surfaced as an opaque "no such column" in the
  middle of a run.
- **`tests/policy/test_thresholds.py`** — absence, unparseable values and
  unknown operators, all of which used to pass silently.
- **`tests/pipeline/test_stage_02_triage.py`** — thirteen documents an
  adversarial read got past the vocabulary, each with the reason. Most were the
  same mistake: a term added because it made one of the author's own documents
  pass. There is also a structural test that no invoice term contains another,
  because "invoice no" and "invoice number" both contain "invoice" and one
  sentence in a supply agreement therefore scored twice and had the contract
  rejected.

Where a test asserts the opposite of what it once asserted, its docstring says
so and why. A test that changed direction is worth more than one that never did.

## What a green run does not prove

**Extraction accuracy.** No test in CI sends a document to a model. Field
accuracy comes from `make eval`, which costs money and is run by hand. The
numbers in the README are measured, not estimated, and their sample is small and
named.

**That the expected answers are right.** For `authored/` I wrote both the
document and the answer. That makes those numbers a demonstration, not a
measurement, and `evals/expected/` covers four documents.

**Anything about scanned pages beyond the routing rule.** A scan reaches the
model as an image; there is no text layer to verify a quote against or to mask
personal data out of. `rule_partially_unverifiable` covers the routing
consequence. Nothing tests the reading itself.

**Concurrency.** `runner.pick_next` has no locking, and no test runs two
workers.

**The IMAP path against a real server.** `tests/adapters/test_imap.py` runs
against a fake. The safety guard that refuses to poll a large `INBOX` is tested;
the protocol handling is not.

## Running the paid evaluation

```bash
make eval          # field accuracy, and the same run with the knowledge base removed
make eval-sweep    # the same documents at each effort level
```

`make eval` reports two numbers that answer different questions: how many fields
matched the expected values, and how many known policy deviations were found
with and without knowledge-base access. The second is the honest test of whether
retrieval changes the result or decorates it — same model, same document, same
extracted fields, and the only difference is access to the playbook and the
registry.

Every call it makes is written to `llm_calls` before and after, so
`make costs` shows exactly what a run cost.
