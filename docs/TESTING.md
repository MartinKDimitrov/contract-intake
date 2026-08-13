# Testing

What is checked, how, and — the part that matters — what a green run does not
prove.

```bash
make check     # everything a commit must pass -- and a commit will not go
               # without it. About 20 seconds.
make test      # the unit and integration suite, hermetic: no API key, no network
make lint      # ruff + ruff format --check + mypy strict
make audit     # the checks that do not answer to my judgement -- see below
make coverage  # the suite again, with a floor that fails the build
make triage    # classify every document with the real stage 02, free
make corpus    # fetch 100 real EU procurement notices (network)
make eval      # field accuracy and knowledge-base contribution (costs money)
```

`make check` is `lint` + `coverage` + `audit` + `triage`, and it is a *condition
of committing*, not a habit: `.githooks/pre-commit` runs it and refuses the
commit if anything fails. `make setup` enables it; `make hooks` enables it on an
existing clone.

This is not ceremony. In this repository every round of fixes introduced a
defect, and the one that broke the entire pipeline was caught by a check that
had been run -- just not before the commit that would have buried it. The hook
removes the step where a human decides whether this change is worth checking.

The same hook enforces a second condition: **a commit that changes
`src/contract_intake/` must document something.** Either prose in the diff
itself -- a docstring or comment beside the change -- or a file under `docs/`.

That is not a style rule either. Five separate audits of this repository found
the same class of defect repeatedly: a claim the code had stopped honouring.
"The worker picks the oldest attachment." "Every category is checksum-validated."
"Four of seven phases spend nothing." Each was true when it was written, and none
was corrected in the commit that falsified it. The hook does not judge whether
the documentation is good; it refuses to let it drift silently.

The escape hatch for both is `git commit --no-verify`, which is deliberately
something you have to type. CI runs the same targets, so skipping it locally only
moves the failure.

`corpus` and `eval` are not in either, for reasons given below.

Test counts are deliberately absent from this document. Three files carried a
hard-coded number and all three were stale within an hour of being written.

## Checks that do not depend on my judgement

Everything above this line is me reviewing my own work, including the tests.
These do not:

| tool            | what it measures                                                                   |
|-----------------|------------------------------------------------------------------------------------|
| `import-linter` | four layering contracts; they fail the build when ARCHITECTURE.md stops being true |
| `bandit`        | common security mistakes in the source                                             |
| `pip-audit`     | known vulnerabilities in every installed dependency                                |
| `vulture`       | code nothing references                                                            |
| `xenon`         | cyclomatic complexity ceilings                                                     |
| `deptry`        | dependencies declared and unused, or used and undeclared                           |
| `codespell`     | spelling, across five languages of deliberate vocabulary                           |
| `pytest-cov`    | a coverage floor, currently 75%                                                    |

`pip-audit` runs with one advisory named explicitly rather than silenced --
`PYSEC-2026-311` against `chromadb` -- because it is a vulnerability in Chroma's
HTTP server and this project uses the embedded client. The reasoning and the
conditions that would change it are in [HAND_OVER.md](HAND_OVER.md).

Coverage is thinnest in `cli.py` and `web/app.py` (argument parsing and routing),
`adapters/imap.py` (protocol I/O, exercised against a fake) and the two stages
whose bodies are a single model call.

## How this document came to say what it says

Every claim below has been attacked. The suite was read adversarially by five
independent passes -- architecture and layering, code style, documentation
against code, regressions in recent changes, and test quality by mutation -- and
what they found is recorded here rather than quietly fixed. Some of it was
severe: a change made to protect the cost ledger broke stage 04 on *every*
document, and only a full `drain()` on a real PDF revealed it. Two smaller
reproductions of the same scenario passed.

That is the standing lesson for verification here: **the useful reproduction is
the most realistic one, not the smallest.**

## Four layers, and what each one is for

| layer                            | what it runs against                                      | catches                                                   | cost   |
|----------------------------------|-----------------------------------------------------------|-----------------------------------------------------------|--------|
| unit and integration             | hand-built inputs, an in-memory database, a stubbed model | logic errors in a single unit                             | free   |
| corpus classification            | the real stage 02, over 135 documents in five languages   | vocabulary fitted to the author's own documents           | free   |
| provenance and policy invariants | pure functions, adversarial inputs                        | values reaching auto-approval without evidence            | free   |
| end-to-end evaluation            | the live API, real documents, expected values             | extraction accuracy, whether retrieval changes the answer | ~$0.50 |

The suite is hermetic by construction: `llm/client.py` is the only path to a
model, and nothing under `tests/` constructs one against a real key. That is why
CI needs no secrets, and why a green CI run means the same thing a green local
run means.

## The corpus, and why provenance is filed in folders

`evals/documents/` separates documents by where they came from, because that
decides what a result on them is worth — the full argument is in
[evals/documents/README.md](../evals/documents/README.md).

| folder       | n   | what a result proves                                                                            |
|--------------|-----|-------------------------------------------------------------------------------------------------|
| `authored/`  | 6   | that the intended path runs -- I wrote the document *and* the answer                            |
| `generated/` | 30  | breadth of document type; three generators disagree in useful ways                              |
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

- **`tests/extract/test_verification_bypasses.py`** — five ways a model could
  once defeat provenance verification: attribute a quote to a scanned page, keep
  it under the minimum length, cite real boilerplate, cite a real clause about a
  different number, or name a page the document does not have. Each ran against a document with no payment clause
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
- **`tests/policy/test_thresholds.py`** — absence, unparsable values and
  unknown operators, all of which used to pass silently.
- **`tests/pipeline/test_transaction_boundaries.py`** — what a failing stage is
  allowed to leave behind. A stage that wrote its output row and then raised
  used to have that row committed anyway, so three attempts meant three rows and
  three paid agent runs. And when the stage's failure *was* a database failure,
  the book-keeping written into the same poisoned session raised on its way out:
  `attempts` stayed at zero, `MAX_ATTEMPTS` could never engage, and the row was
  handed back forever.
- **`tests/pipeline/test_stage_01_receive.py`** — a failure on the second of
  three attachments used to commit the email row and the attachments written so
  far, after which the Message-ID dedup swallowed the redelivery.
- **`tests/pipeline/test_stage_02_triage.py`** — thirteen documents an
  adversarial read got past the vocabulary, each with the reason. Most were the
  same mistake: a term added because it made one of the author's own documents
  pass. There is also a structural test that no invoice term contains another,
  because "invoice no" and "invoice number" both contain "invoice" and one
  sentence in a supply agreement therefore scored twice and had the contract
  rejected.

- **`tests/test_spending_guards.py`** — the three guards between an agent loop
  and an unbounded bill. All three were written in one afternoon and none had a
  test, which is the worst combination for code whose only job is to be there
  when something else goes wrong.
- **`tests/web/test_review.py`** — the human queue, which was the last module at
  zero coverage. A second, divergent writer to the `contracts` table lived in it
  unnoticed.
- **`tests/test_provider_boundary.py`** — an AST check that no module but
  `llm/client.py` constructs a provider client. An import graph cannot express
  this, because `agent/tools.py` legitimately imports a decorator from the same
  package.

Where a test asserts the opposite of what it once asserted, its docstring says
so and why. A test that changed direction is worth more than one that never did.

## Known false alarms

Two checks are deliberately biased toward sending a document to a person, and
both will sometimes be wrong in that direction:

- `supports_value` cannot follow a unit conversion. A term of 24 months quoted
  from "two (2) years" reads as a disagreement, because telling that apart from
  a fabrication needs arithmetic this does not do.
- `canonical_jurisdiction` fails closed on a rendering it does not recognise.
  The alias table covers how the five supported languages state a governing law;
  a sixth phrasing becomes a deviation and goes to review.

Both cost a reviewer a few minutes. The alternative -- being wrong in the other
direction -- is a contract nobody read.

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
workers. `attachments.sha256` is unique, so the intake half is constrained; the
claim on a queued row is not.

**Idempotent replay.** `make stage N=06 ID=17` run three times produces three
decisions and three review items. The unique constraints pin one artefact per
*decision*, and every replay mints a fresh decision. Replaying a settled
document is now refused without `--force`; replaying an in-flight one still
duplicates.

**The IMAP path against a real server.** `tests/adapters/test_imap.py` runs
against a fake. The safety guard that refuses to poll a large `INBOX` is tested;
the protocol handling is not.

## Mutation testing: what a green suite is worth

Passing tests measure the tests, not the code. The objective measure is whether
the suite notices when the code is broken on purpose, so 103 semantic mutations
were applied to the load-bearing functions -- inverted comparisons, dropped
branches, constants moved by one, guards removed.

**65 caught, 38 survived: a mutation score of 63%.** The shape of the failures
was consistent and more useful than the number:

> The pure functions are exhaustively tested and the stages that call them are
> barely tested at all. `policy/rules.py` is at 99% with a full truth table;
> `stage_06_decide.py`, which assembles the evidence those rules read, is at 38%.
> A bug in the wiring is invisible to a suite that only tests the wire's ends.

Three survivors were closed immediately because each was a safety property the
code's own docstring claimed was already covered:

- `rule_suspended_counterparty` reads as an allow-list, but the registry held
  only `approved` and `suspended`, so reverting it to a deny-list passed. A
  vendor with status `under_review` now exists in `vendors.json` for this.
- The fixture in `tests/policy/test_rules.py` built only the five required
  fields, so both rules that iterate the wider `DECISION_BEARING` set were
  untestable in the half that matters -- the liability cap and the
  data-processing agreement.
- `_verify_one`'s final branch was unreached: a quote attributed to a page the
  document does not have. That was a fifth provenance bypass, in a file whose
  docstring claimed to enumerate them all.

To repeat the measurement:

```bash
.venv/bin/mutmut run          # slow; the dev extra is declared for this
```

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
