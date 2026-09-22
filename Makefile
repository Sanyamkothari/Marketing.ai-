PY ?= python3.11
VENV := .venv
BIN := $(VENV)/bin
EXTRAS ?=                                   # e.g. `make setup EXTRAS=nn`
UV := $(shell command -v uv 2>/dev/null)
PKG := .[dev$(if $(EXTRAS),$(shell printf ',%s' $(EXTRAS)),)]

# Phase 4a. IMAGE_TAG and POSTGRES_IMAGE are overridable because some build environments cannot
# reach Docker Hub's CDN and have to name a mirror; the defaults are what everyone else would type.
IMAGE          ?= marketing-ai
IMAGE_TAG      ?= local
PYTHON_BASE    ?= python:3.11-slim
POSTGRES_IMAGE ?= postgres:16-alpine
ENV            ?= dev
INFRA_VENV     := .venv-infra
INFRA_BIN      := $(INFRA_VENV)/bin
CDK            := npx --yes aws-cdk@2.1142.0

.DEFAULT_GOAL := help
.PHONY: help setup test test-all test-postgres lint format run generate check-generated clean \
        image image-test image-size compose-up compose-down postgres-up postgres-down \
        migrate revision prices freeze infra-setup infra-lint infra-test infra-synth infra-nag \
        aws-deploy aws-bootstrap

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

# --- Phase 4a: the container ------------------------------------------------
image: ## build the api image (override PYTHON_BASE/RUNTIME_APT_PACKAGES for an unusual base)
	docker build --target api --build-arg PYTHON_BASE=$(PYTHON_BASE) -t $(IMAGE):$(IMAGE_TAG) .

image-test: ## build the test stage and run the fast suite INSIDE the image
	docker build --target test --build-arg PYTHON_BASE=$(PYTHON_BASE) -t $(IMAGE):test .
	docker run --rm $(IMAGE):test

image-size: ## print the image size and fail if it exceeds MAX_IMAGE_BYTES (default 4 GiB)
	$(BIN)/python -m scripts.check_image_size --ref $(IMAGE):$(IMAGE_TAG)

# --- Phase 4a: the local stack ----------------------------------------------
compose-up: ## api + both postgres servers, production-like, on localhost
	POSTGRES_IMAGE=$(POSTGRES_IMAGE) PYTHON_BASE=$(PYTHON_BASE) docker compose up -d

compose-down: ## stop the local stack and drop its volumes
	docker compose down -v

postgres-up: ## just the two postgres servers the metadata tests use (55432 UTC, 55433 Asia/Kolkata)
	POSTGRES_IMAGE=$(POSTGRES_IMAGE) docker compose up -d postgres postgres-tz

postgres-down: ## stop them
	docker compose rm -sfv postgres postgres-tz

test-postgres: ## the metadata tests against a real server; FAILS rather than skips if there is none
	MARKETING_AI_REQUIRE_POSTGRES=1 $(BIN)/python -m pytest -m postgres

# --- Phase 4a: the database schema ------------------------------------------
migrate: ## alembic upgrade head against MARKETING_AI_DATABASE_URL
	$(BIN)/alembic upgrade head

revision: ## autogenerate a migration: make revision M="what changed"
	$(BIN)/alembic revision --autogenerate -m "$(M)"

# --- Phase 4a: generated data -----------------------------------------------
prices: ## refetch configs/aws_prices.yaml from the public AWS price list (NOT part of `make lint`)
	$(BIN)/python -m scripts.fetch_aws_prices

freeze: ## rewrite requirements-freeze.txt from this venv (review the diff; it is the pin test's input)
	VIRTUAL_ENV=$(VENV) uv pip freeze --exclude-editable | grep -vi '^marketing-ai' | sort -f > requirements-freeze.txt

# --- Phase 4a: the infrastructure -------------------------------------------
# infra/ lives in its own venv: aws-cdk-lib pulls jsii and a node bridge that the engine must never
# depend on, and `make lint` must keep working in a checkout that never installed it.
infra-setup: ## create .venv-infra and install the deploy extra
	$(PY) -m venv $(INFRA_VENV)
	$(INFRA_BIN)/python -m pip install -q --upgrade pip
	$(INFRA_BIN)/python -m pip install -q -e ".[deploy]" -r infra/requirements.txt

infra-lint: ## ruff + black --check + mypy --strict over infra/ and tests/infra/
	$(BIN)/ruff check infra tests/infra
	$(BIN)/black --check infra tests/infra
	$(INFRA_BIN)/mypy --strict infra tests/infra

infra-test: ## the offline CDK assertions and snapshots (no AWS account needed)
	$(INFRA_BIN)/python -m pytest tests/infra -q

infra-synth: ## really run `cdk synth` (needs node; still no AWS account)
	cd infra && PATH="$(CURDIR)/$(INFRA_BIN):$$PATH" $(CDK) synth --all -c env_name=$(ENV)

infra-nag: ## cdk synth with the AwsSolutions checks switched on
	cd infra && PATH="$(CURDIR)/$(INFRA_BIN):$$PATH" $(CDK) synth --all -c env_name=$(ENV) -c cdk_nag=true

# --- Phase 4a: deploying ----------------------------------------------------
aws-deploy: ## build, push and `cdk deploy` into ENV (needs credentials)
	./scripts/build_push_image.sh
	cd infra && PATH="$(CURDIR)/$(INFRA_BIN):$$PATH" $(CDK) deploy --all -c env_name=$(ENV) \
		-c image_digest="$$(cat .image-digest)" --require-approval broadening

aws-bootstrap: ## create the schema, seed the configs and verify the permissions of a deployment
	$(BIN)/python -m scripts.aws_bootstrap --env $(ENV)
