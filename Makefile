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

# The two markers that spend money are excluded here, not in pyproject's `addopts`, and that is not
# a style choice: pytest keeps only the LAST `-m`, so a filter in `addopts` is silently replaced by
# the `-m "not slow"` below and every @bedrock test would be collected again. Each self-skips
# without credentials, but that is configuration rather than intent - the day a runner has both,
# the suite bills an account nobody asked it to. `pytest -m bedrock` (or `-m aws`) still opts in
# (DEC-358).
PAID_MARKERS := not bedrock and not aws

test: ## fast tests (excludes @slow and the two markers that bill a real account)
	$(BIN)/python -m pytest -m "not slow and $(PAID_MARKERS)"

test-all: ## every test that costs nothing; `pytest -m bedrock` or `-m aws` opts in to the paid ones
	$(BIN)/python -m pytest -m "$(PAID_MARKERS)"

lint: check-generated ## ruff + black --check + mypy --strict + generated-file drift check
	$(BIN)/ruff check engine api scripts tests
	$(BIN)/black --check engine api scripts tests
	$(BIN)/mypy

format: ## apply ruff --fix and black
	$(BIN)/ruff check --fix engine api scripts tests
	$(BIN)/black engine api scripts tests

generate: ## regenerate templates/, docs/API.md and infra/observability/ from configs and contracts
	$(BIN)/python -m scripts.gen_templates
	$(BIN)/python -m scripts.gen_api_docs
	$(BIN)/python -m scripts.gen_dashboard

check-generated: ## fail if templates/, docs/API.md or infra/observability/ are stale
	$(BIN)/python -m scripts.gen_templates --check
	$(BIN)/python -m scripts.gen_api_docs --check
	$(BIN)/python -m scripts.gen_dashboard --check

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
.PHONY: prototype-test prototype-screenshots

prototype-test: ## jsdom tests for marketing-ai-prototype.html (needs node + npm)
	cd tests/prototype && npm ci --no-audit --no-fund && node --test

prototype-screenshots: ## regenerate docs/prototype/*.png (needs node + playwright)
	node scripts/prototype_screenshots.mjs

# Plan A (Phase 2 completion, branch phase-2b-completion) has no block of its own; it finishes
# Phase 2, so its targets live here.
.PHONY: check-readme library-test

check-readme: ## fail if README.md calls a milestone pending whose tests pass (reads JUNIT=report.xml if given, else runs them)
	$(BIN)/python -m scripts.check_readme $(if $(JUNIT),--junit $(JUNIT),)

library-test: ## the public dataset library's tests: five small real trainings on committed samples, no network
	$(BIN)/python -m pytest library/tests -m "$(PAID_MARKERS)"
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
.PHONY: image image-test image-size compose-up compose-down postgres-up postgres-down \
        test-postgres migrate revision prices freeze infra-setup infra-lint infra-test \
        infra-synth infra-nag aws-deploy aws-bootstrap aws-test

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

infra-lint: ## ruff + black --check + mypy --strict over infra/ and tests/infra/ (needs only .venv-infra)
	$(INFRA_BIN)/ruff check infra tests/infra
	$(INFRA_BIN)/black --check infra tests/infra
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
		-c image_digest="$$(cat $(CURDIR)/.image-digest)" --require-approval broadening

aws-bootstrap: ## create the schema, seed the configs and verify the permissions of a deployment
	$(BIN)/python -m scripts.aws_bootstrap --env $(ENV)

# Two interpreters, because the infrastructure has its own venv: `make setup` installs `.[dev]`,
# which does not carry aws-cdk-lib, so collecting tests/infra with $(BIN) would fail on a checkout
# that has never run `make infra-setup` (DEC-364). It is `infra-test` rather than a second pytest
# invocation so there is one definition of how the infra suite runs.
aws-test: infra-test ## every Phase 4a suite: the AWS backends, the container files and the infrastructure
	$(BIN)/python -m pytest tests/unit/test_settings.py tests/unit/test_aws_secrets.py \
		tests/unit/test_storage_contract.py tests/unit/test_s3_storage.py tests/unit/test_s3_streaming.py \
		tests/unit/test_postgres_metadata.py tests/unit/test_alembic_migrations.py \
		tests/unit/test_run_index.py tests/unit/test_s3_registry.py tests/unit/test_sagemaker_mirror.py \
		tests/unit/test_runs_module.py tests/unit/test_sagemaker_jobs.py tests/unit/test_prices.py \
		tests/unit/test_job_entrypoint.py tests/unit/test_metrics.py tests/unit/test_container_files.py \
		tests/unit/test_docs_honesty.py tests/integration/test_jobs_as_sagemaker.py -q
# ---- END PHASE-4A ----

# ---- PHASE-4B (production) — append only below this line ----
.PHONY: production-test production-test-fast

# Every Phase 4b suite: access, audit, privacy, scheduling and their screens (M46-M49). Slow tests
# are included (two train a real model through a schedule), the paid markers are not. The jsdom
# screen tests run through pytest, which writes their fixtures from the real app; without node and
# jsdom they skip and say so (`cd tests/integration/production/ui && npm install` once).
production-test: ## every Phase 4b suite (M46-M49), slow ones included; no AWS account needed
	$(BIN)/python -m pytest -m "$(PAID_MARKERS)" tests/unit/production tests/integration/production \
		tests/unit/test_alembic_migrations.py tests/unit/test_postgres_metadata.py

production-test-fast: ## the Phase 4b suites without the two that train a model
	$(BIN)/python -m pytest -m "not slow and $(PAID_MARKERS)" tests/unit/production tests/integration/production \
		tests/unit/test_alembic_migrations.py tests/unit/test_postgres_metadata.py
# ---- END PHASE-4B ----
# ---- PHASE-3B (uplift) — append only below this line ----
.PHONY: uplift-test

uplift-test: ## every Phase 3b suite: the uplift engine, its API, the flows and the UI module (slow ones too)
	$(BIN)/python -m pytest tests/unit/uplift tests/integration/uplift -m "$(PAID_MARKERS)" -q
# ---- END PHASE-3B ----
# ---- PLAN-E (pilot) — append only below this line ----
.PHONY: pilot-test pilot-generate pilot-check pilot-kit demo-seed demo demo-signin

pilot-test: ## every Plan E suite (M59-M64): kit, pre-flight, reports, value view, demo, feedback, screens
	$(BIN)/python -m pytest tests/unit/pilot tests/integration/pilot -m "$(PAID_MARKERS)" -q

pilot-generate: ## regenerate docs/pilot/DATA_REQUEST.md and docs/pilot/templates/ from the configs
	$(BIN)/python -m scripts.gen_data_request

pilot-check: ## fail if docs/pilot/DATA_REQUEST.md or its templates are stale
	$(BIN)/python -m scripts.gen_data_request --check

pilot-kit: ## dist/pilot-kit.zip: what a client needs to run the pre-flight check (no AutoGluon)
	$(BIN)/python -m scripts.build_pilot_kit

demo-seed: ## seed Demo Telecom into MARKETING_AI_DATA_DIR (or data/): trains once, takes a few minutes
	$(BIN)/python -m scripts.seed_demo

demo: demo-seed ## seed the demo if needed, then serve it on :8000 with demo mode on
	@echo ""
	@echo "  Marketing AI demo: open http://localhost:8000 in your browser. Press Ctrl+C here to stop it."
	@echo ""
	MARKETING_AI_DEMO_MODE=true $(BIN)/uvicorn api.main:app --port 8000

demo-signin: demo-seed ## the demo with sign-in on and one demo user per role (local demo only; passwords printed)
	MARKETING_AI_AUTH_MODE=local $(BIN)/python -m scripts.demo_users
	@echo ""
	@echo "  Marketing AI demo with sign-in: open http://localhost:8000 and sign in as one of the users above. Ctrl+C stops it."
	@echo ""
	MARKETING_AI_DEMO_MODE=true MARKETING_AI_AUTH_MODE=local $(BIN)/uvicorn api.main:app --port 8000
# ---- END PLAN-E ----
