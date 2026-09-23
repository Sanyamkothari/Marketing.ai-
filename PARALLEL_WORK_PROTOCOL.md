# Marketing AI — Parallel Work Protocol

**Applies to:** every agent working on a phase branch (Phase 2, Phase 3a, Phase 4a)
**Owner:** Minfy — AI/ML team
**Read this before your phase plan.**

Three agents work at the same time on three branches. This document is the contract between them. Its purpose is to make the final merges boring.

---

## 1. Branches and merge order

| Branch | Plan | Merges into `main` | Rebases onto |
|---|---|---|---|
| `phase-2-onboarding` | `MARKETING_AI_PHASE2_PLAN.md` | **first** | `main` |
| `phase-3a-generative` | `MARKETING_AI_PHASE3A_PLAN.md` | second | `main`, then `phase-2-onboarding` once it is merged |
| `phase-4a-aws` | `MARKETING_AI_PHASE4A_PLAN.md` | third | `main`, then the merged result of 2 and 3a |
| `phase-3b-uplift` | Plan B (Phase 3b — uplift modelling and measured impact) | fourth, after 2, 3a and 4a had merged | `main`; its shared-file blocks come after Phase 4a's (DEC-600) |
| `plan-e-pilot` (developed on `main`) | Plan E (pilot readiness: data request kit, readiness, results and ROI reports, demo mode, playbook) | runs beside Plans D and F; reads artefacts only, changes no engine logic | `main`; its shared-file blocks come after Phase 3b's (DEC-900) |

- Rebase (or merge `main` into your branch) **at least once a day**. Resolve conflicts immediately; never let them accumulate.
- A branch merges only when: CI is green on the branch, the Phase 1 suites (`make test-all`) are green, the phase's own integration test is green, and `README.md` and `docs/DECISIONS.md` are current.
- Merge with a merge commit (not squash), so per-milestone history survives.

---

## 2. Contracts-first task (done on `main` before any branch starts feature work)

One small PR, reviewed by the human, adds the shared surface every branch needs. Branches **extend** these by adding fields/models; they never rename, remove or change the meaning of an existing field.

1. `engine/contracts.py`
   - `primary_key: str | list[str]` on `RunRequest`, `RunConfig`, `RunManifest` (Phase 2).
   - `RunRequest.dataset_id: str | None`, `RunRequest.client_id: str | None`, `RunManifest.dataset_id`, `RunManifest.client_id`, `RunManifest.dataset_fingerprint` (Phase 2).
   - `RunManifest.llm_usage: LLMUsage | None` and `LLMUsage {calls, input_tokens, output_tokens, cost_estimate_usd, model_ids[]}` (Phase 3a).
   - `RunManifest.compute: ComputeInfo | None` and `ComputeInfo {backend: local|sagemaker, job_arn, instance_type, duration_s, cost_estimate_usd}` (Phase 4a).
2. `engine/onboarding/specs.py` — empty module with the pydantic models named in Phase 2 §8 (fields as listed; Phase 2 fills in behaviour).
3. `engine/generative/contracts.py` — the models named in Phase 3a §7.
4. `engine/settings.py` — a single pydantic `Settings` (env-driven) with `storage_backend: local|s3`, `job_backend: thread|sagemaker`, `metadata_backend: sqlite|postgres`, `llm_backend: fake|bedrock`, `aws_region`, and the connection fields each needs. Phase 1 code reads its existing values through this object; defaults keep behaviour identical.
5. `engine/llm.py` — `LLMClient` protocol (`complete`, `embed`, `count_tokens`) and `FakeLLMClient` (deterministic, records calls). Phase 3a implements `BedrockLLMClient`.
6. Marker comments in shared files (section 4).
7. CI workflow: fast suite on every push to every branch; slow suite nightly on `main` and on demand.

---

## 3. File ownership

An agent may freely create and edit files in the directories it owns. It may edit **shared** files only inside its marked block (section 4). It must not touch files owned by another branch; if it needs something there, it writes the request in `docs/CROSS_BRANCH_REQUESTS.md` and continues with a stub behind the protocol.

| Path | Owner |
|---|---|
| `engine/onboarding/**`, `engine/clients.py`, `api/routes/{clients,sources,mappings,datasets}.py`, `ui/modules/onboarding/**`, `configs/roles.yaml`, `tests/**/onboarding/**`, `tests/fixtures/raw/**`, `docs/ONBOARDING.md` | Phase 2 |
| `configs/use_cases/*.yaml` sections `standard_schema`, `suggested_features`, `label` | Phase 2 |
| `engine/generative/**`, `engine/llm.py` (implementations), `api/routes/generative.py`, `ui/modules/generative/**`, `configs/prompts/**`, `configs/guardrails.yaml`, `tests/**/generative/**`, `tests/fixtures/docs/**`, `docs/GENERATIVE.md` | Phase 3a |
| `configs/use_cases/*.yaml` sections `generative`, `rca`, `copy` | Phase 3a |
| `engine/aws/**` (S3Storage, SageMakerJobRunner, PostgresMetadata, S3ModelRegistry), `infra/**` (CDK), `Dockerfile*`, `docker-compose.yml`, `.github/workflows/deploy*.yml`, `tests/**/aws/**`, `docs/AWS_DEPLOYMENT.md`, `docs/RUNBOOK.md` | Phase 4a |
| `engine/uplift/**`, `api/routes/uplift.py`, `ui/modules/uplift/**`, `tests/**/uplift/**`, `tests/fixtures/make_uplift_data.py`, `docs/UPLIFT.md` | Phase 3b |
| `configs/engine.yaml` section `defaults.uplift`, and `configs/use_cases/*.yaml` section `uplift` | Phase 3b |
| `engine/pilot/**`, `api/routes/pilot.py`, `ui/modules/pilot/**`, `configs/pilot/**`, `scripts/preflight.py`, `scripts/gen_data_request.py`, `scripts/seed_demo.py`, `tests/**/pilot/**`, `docs/pilot/**` | Plan E |
| `engine/stages/train.py`, `evaluate.py`, `explain.py`, `engine/registry.py` champion rule | **Frozen.** Nobody. |
| `engine/contracts.py`, `engine/config.py`, `engine/settings.py`, `engine/pipeline.py`, `api/main.py`, `api/schemas.py`, `ui/index.html`, `ui/modules/router.js`, `Makefile`, `pyproject.toml`, `README.md`, `docs/DECISIONS.md`, `docs/API.md` | **Shared** (section 4) |

Phase 2's six listed changes to Phase 1 code (Phase 2 plan §6.5: composite key in `prepare.py`, `score.py`, `actions.py`, `ingest.py` accepting `dataset_id`) are the only exception to "don't touch stages"; they are pre-approved, must be backward compatible, and Phase 2 announces them in `CROSS_BRANCH_REQUESTS.md` the day they land.

---

## 4. Shared-file rules

Each shared file gets three marked blocks, added by the contracts-first task:

```python
# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----
# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----
# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----
```

- Add code **only inside your block**, at its end. Never edit above your block, never reorder, never reformat the file (run `black` on your block's content before pasting rather than on the whole file if the formatter would reflow other lines).
- `pyproject.toml`: add dependencies in your block; pin versions; never bump a shared pin (pandas, pydantic, FastAPI, AutoGluon). If you need a bump, request it.
- `README.md` and `docs/DECISIONS.md`: append a section per milestone under your phase heading. Decision numbers are allocated per workstream so that parallel branches cannot claim the same one:

  | Range | Workstream | State at integration (2026-09-23) |
  |---|---|---|
  | DEC-001…099 | trunk (Phase 1, the contracts-first change and Plan A) | **exhausted** at DEC-099 (Plan A used DEC-083…099); the trunk continues at DEC-800 |
  | DEC-100…199 | Phase 2 — onboarding | used to DEC-110 |
  | DEC-200…299 | Phase 3a — generative | used to DEC-232 |
  | DEC-300…399 | Phase 4a — AWS | **exhausted** at DEC-399 |
  | DEC-400…499 | public dataset library (`library/`) | used to DEC-411; allocated after the fact |
  | DEC-500…599 | Phase 5 — Evolve (reserved by plan B) | reserved; unused |
  | DEC-600…699 | Phase 3b — uplift (plan B assigns it this range and reserves the DEC-500s for Phase 5) | claimed 2026-09-23; used to DEC-680 |
  | DEC-700…799 | Phase 4b — production: access, audit, privacy, scheduling (`docs/PHASE4B_PLAN.md`) | claimed 2026-09-23 |
  | DEC-800…899 | trunk, continued (the integration of the parallel branches onwards) | claimed 2026-09-23; used to DEC-801 |
  | DEC-900…949 | Plan E — pilot readiness (M59–M64) | claimed 2026-09-23 |
  | DEC-950 up | unallocated | — |

  A new phase, or a phase that exhausts its hundred, takes the **next free hundred** and adds its row to this table *before* its first entry — the row is the claim. Never borrow a number inside another workstream's hundred, even an unused one.
- `docs/API.md` is generated; never hand-edit. Run `gen_api_docs` after adding models.
- `ui/index.html` / `router.js`: register your module in your block only; all screens live in your `ui/modules/<phase>/`.
- `Makefile`: add targets in your block (`make onboarding-test`, `make generative-test`, `make aws-test`); do not change `setup`, `test`, `test-all`, `lint`.

---

## 5. Behaviour rules for all agents

1. Run `make lint test` before every commit; `make test-all` before every rebase and before requesting merge.
2. If a Phase 1 test fails on your branch and the failure is not caused by your change, **stop and report**; do not "fix" the test.
3. Never disable, skip or loosen an existing test. Never widen a `try/except` to make a test pass.
4. Nothing fabricated reaches the UI (Phase 1 rule 3). For LLM outputs, the same rule: no hardcoded example summaries or copy in code paths that reach the UI.
5. Secrets only via `engine/settings.py` (env vars). Never commit keys, account IDs, bucket names or ARNs; use placeholders in docs.
6. Every external call (Bedrock, SageMaker, S3) sits behind a protocol with a local/fake implementation, and the fast test suite runs entirely against fakes. Real-AWS tests are marked `aws` and are opt-in.
7. Log timings, counts, token counts and costs at INFO; never log data values, prompts containing customer data, or generated copy that includes personal fields.
8. When your plan is silent, choose the option that keeps the system config-driven and record it in `DECISIONS.md` with your phase's number range.
9. When the plan is impossible or clearly wrong: stop, explain, propose the smallest change. Do not silently deviate.
10. Write `docs/CROSS_BRANCH_REQUESTS.md` entries as: date, from-branch, to-branch, what is needed, what you did meanwhile (stub/fake). The human resolves these daily.

---

## 6. Daily checklist for the human reviewer

- Each branch: CI green? last rebase within 24 h? `CROSS_BRANCH_REQUESTS.md` has no open items older than a day?
- Any "stop and report" messages waiting?
- Any agent touching a frozen or another branch's file (`git diff --stat main..branch` outside its ownership map)?
- Any dependency added or pinned version bumped in `pyproject.toml`?
- Any test skipped, deleted or loosened (`git diff` on `tests/` for removed assertions)?

Five minutes per branch per day prevents the multi-day merge.
