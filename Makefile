# One-command everything. `make help` lists targets.
.DEFAULT_GOAL := help
PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help
help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip

.PHONY: install
install: $(BIN)/python ## create venv and install with dev+server extras
	$(BIN)/pip install --quiet -e '.[dev,server]'
	@echo "ready: source $(VENV)/bin/activate"

.PHONY: test
test: install ## run the test suite
	$(BIN)/pytest

.PHONY: cov
cov: install ## run tests with a coverage report
	$(BIN)/pytest --cov=syshealth --cov-report=term-missing

.PHONY: lint
lint: install ## check formatting and lint rules
	$(BIN)/ruff check src tests tools
	$(BIN)/ruff format --check src tests tools

.PHONY: fmt
fmt: install ## autoformat
	$(BIN)/ruff format src tests tools
	$(BIN)/ruff check --fix src tests tools

.PHONY: fixtures
fixtures: ## regenerate the recorded run fixtures
	$(PY) tools/make_fixtures.py

.PHONY: demo
demo: install ## show all three scenarios without needing a PSI kernel
	@for s in thrashing cache-heavy idle-oversized; do \
		echo "\n=============== $$s ==============="; \
		$(BIN)/syshealth report tests/fixtures/runs/$$s.jsonl --instance-type t3.large; \
	done

.PHONY: doctor
doctor: install ## check whether this machine can be measured
	$(BIN)/syshealth doctor

.PHONY: clean
clean: ## remove venv and build artefacts
	rm -rf $(VENV) dist build .pytest_cache .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
