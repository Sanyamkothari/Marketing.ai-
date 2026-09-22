PY ?= python3.11
VENV := .venv
BIN := $(VENV)/bin
EXTRAS ?=                                   # e.g. `make setup EXTRAS=nn`
UV := $(shell command -v uv 2>/dev/null)
PKG := .[dev$(if $(EXTRAS),$(shell printf ',%s' $(EXTRAS)),)]

.DEFAULT_GOAL := help
.PHONY: help setup test test-all lint format run generate check-generated clean

help:                                       ## list targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sort

$(VENV)/pyvenv.cfg:
ifdef UV
	uv venv --python $(PY) $(VENV)
else
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
endif

setup: $(VENV)/pyvenv.cfg                   ## create the venv and install the project (+dev, +EXTRAS); fails loudly if AutoGluon is missing
ifdef UV
	VIRTUAL_ENV=$(VENV) uv pip install -e "$(PKG)"
else
	$(BIN)/python -m pip install -e "$(PKG)"
endif
	$(BIN)/python -c "import autogluon.tabular, pydantic, fastapi, sqlmodel, yaml; print('setup ok: autogluon.tabular', autogluon.tabular.__version__)"

test: ## fast tests (excludes @slow)
	$(BIN)/python -m pytest -m "not slow"

test-all: ## every test
	$(BIN)/python -m pytest

lint: check-generated ## ruff + black --check + mypy --strict + generated-file drift check
	$(BIN)/ruff check engine api scripts tests
	$(BIN)/black --check engine api scripts tests
	$(BIN)/mypy

format: ## apply ruff --fix and black
	$(BIN)/ruff check --fix engine api scripts tests
	$(BIN)/black engine api scripts tests

generate: ## regenerate templates/ and docs/API.md from configs and contracts
	$(BIN)/python -m scripts.gen_templates
	$(BIN)/python -m scripts.gen_api_docs

check-generated: ## fail if templates/ or docs/API.md are stale
	$(BIN)/python -m scripts.gen_templates --check
	$(BIN)/python -m scripts.gen_api_docs --check

run: ## serve the API on :8000
	$(BIN)/uvicorn api.main:app --reload --port 8000

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache

# ==========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4). Add targets inside your own
# block: `onboarding-test`, `generative-test`, `aws-test`. Declare them in a
# `.PHONY:` line of your own and give each a `## help text` comment so it
# appears in `make help`. Do not change setup, test, test-all or lint.
# ==========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----
