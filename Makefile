# Overridable so CI can point at whatever interpreter the runner provides.
PYTHON ?= python3.12
PY := .venv/bin/python
BIN := .venv/bin
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.venv:
	$(PYTHON) -m venv .venv

.PHONY: setup
setup: .venv  ## Create venv and install the project with dev extras
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@echo "fetching the embedding model (~80MB, once)..."
	@$(PY) -m contract_intake.cli knowledge --build >/dev/null
	@$(PY) evals/render.py >/dev/null
	@git config core.hooksPath .githooks 2>/dev/null || true
	@echo "ready. copy .env.example to .env and fill in ANTHROPIC_API_KEY"

.PHONY: test
test:  ## Run the test suite (hermetic, no API key needed)
	$(PY) -m pytest -q

.PHONY: lint
lint:  ## ruff + mypy
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests
	$(PY) -m mypy

.PHONY: check
check: lint docs fields coverage audit triage verify  ## Everything a commit must pass. Runs in CI too.
	@echo
	@echo "check: passed -- lint, tests with a coverage floor, the external"
	@echo "       audit, and classification of every document."

.PHONY: hooks
hooks:  ## Make `check` a condition of committing
	@git config core.hooksPath .githooks
	@echo "pre-commit hook enabled; 'git commit --no-verify' is the escape hatch"

.PHONY: audit
audit:  ## Checks that answer to something other than my own judgement
	@echo "-- architecture contracts"
	$(BIN)/lint-imports
	@echo "-- dead code"
	$(BIN)/vulture
	@echo "-- complexity"
	$(BIN)/xenon --max-absolute C --max-modules C --max-average B src
	@echo "-- security"
	$(BIN)/bandit -q -r src
	@echo "-- dependency hygiene"
	$(BIN)/deptry .
	@echo "-- spelling"
	$(BIN)/codespell
	@echo "-- known vulnerabilities"
	@# PYSEC-2026-311 is Chroma's HTTP server; we use PersistentClient and start
	@# no server. Named rather than silenced, and justified in docs/HAND_OVER.md.
	$(BIN)/pip-audit --progress-spinner off --ignore-vuln PYSEC-2026-311

.PHONY: coverage
coverage:  ## Tests with a floor that fails the build
	$(PY) -m pytest -q --cov=contract_intake --cov-report=term-missing

.PHONY: docs
docs:  ## Align the tables in the documentation so they read without a renderer
	@$(PY) scripts/align_tables.py --check || ($(PY) scripts/align_tables.py && exit 1)

.PHONY: fields
fields:  ## Align field declarations into columns, and keep the formatter off them
	@$(PY) scripts/align_fields.py --check || ($(PY) scripts/align_fields.py && exit 1)

.PHONY: verify
verify:  ## Quote verification over the real corpus (free)
	$(PY) evals/verify.py

.PHONY: watch
watch:  ## Keep polling and draining until interrupted -- the worker
	$(PY) -m contract_intake.cli poll --watch

.PHONY: dead
dead:  ## What could not be finished, and why
	$(PY) -m contract_intake.cli dead

.PHONY: eval-sweep
eval-sweep:  ## Field accuracy at each effort level (costs money)
	$(PY) evals/run.py --sweep

.PHONY: fmt
fmt:  ## Autoformat
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

.PHONY: run
run:  ## Serve the review UI on :8000
	$(PY) -m uvicorn contract_intake.web.app:app --reload --port 8000

.PHONY: poll
poll:  ## Fetch mail and run the pipeline once
	$(PY) -m contract_intake.cli poll

.PHONY: stage
stage:  ## Re-run one stage: make stage N=04 ID=17
	$(PY) -m contract_intake.cli stage --number $(N) --attachment-id $(ID)

.PHONY: knowledge
knowledge:  ## Rebuild the policy index and print registry stats
	$(PY) -m contract_intake.cli knowledge --build

.PHONY: costs
costs:  ## Print the cost ledger summary
	$(PY) -m contract_intake.cli costs

.PHONY: triage
triage:  ## Classify every document, by provenance (free)
	$(PY) evals/run.py --triage

.PHONY: corpus
corpus:  ## Download the real EU document corpus
	$(PY) evals/corpus.py

.PHONY: eval
eval:  ## Measure extraction accuracy and the knowledge base contribution (costs money)
	$(PY) evals/run.py

.PHONY: clean
clean:  ## Remove venv, db and caches
	rm -rf .venv .pytest_cache .mypy_cache var/
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
