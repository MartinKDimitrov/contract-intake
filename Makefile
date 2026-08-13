# Overridable so CI can point at whatever interpreter the runner provides.
PYTHON ?= python3.12
PY := .venv/bin/python
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
	@$(PY) evals/fixtures/generate.py >/dev/null
	@echo "ready. copy .env.example to .env and fill in ANTHROPIC_API_KEY"

.PHONY: test
test:  ## Run the test suite (hermetic, no API key needed)
	$(PY) -m pytest -q

.PHONY: lint
lint:  ## ruff + mypy
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests
	$(PY) -m mypy

.PHONY: fmt
fmt:  ## Autoformat
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

.PHONY: run
run:  ## Serve the review UI + webhook on :8000
	$(PY) -m uvicorn contract_intake.web.app:app --reload --port 8000

.PHONY: poll
poll:  ## Run the IMAP poller + pipeline worker
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

.PHONY: eval
eval:  ## Run the extraction/routing eval harness
	$(PY) -m pytest evals -q -s

.PHONY: clean
clean:  ## Remove venv, db and caches
	rm -rf .venv .pytest_cache .mypy_cache var/
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
