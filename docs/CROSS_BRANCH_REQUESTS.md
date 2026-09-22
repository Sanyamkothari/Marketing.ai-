# Cross-branch requests

One entry per thing a branch needs from outside its own ownership map
(`PARALLEL_WORK_PROTOCOL.md` §3). The human reviewer resolves open items daily (§6); an item older
than a day with no answer is a finding on that day's checklist.

Write an entry the moment you are blocked, not when you give up: the point of the log is that the
branch *keeps going* behind a stub while the request is outstanding, so **what I did meanwhile** is
the part that matters. Never leave it empty — an entry with no fallback is a branch that stopped.

**Format** (§5.10): one `###` heading carrying the date, the branch it is from and the branch or
person it is for, then a **What is needed** paragraph and a **What I did meanwhile** paragraph.
Move an entry to `## Resolved` with the answer and the date, rather than deleting it: the next
branch to hit the same wall should find the ruling, not the silence.

## Open

### 2026-09-22 — phase-2-onboarding → phase-3a, phase-4a: four fields added to `UseCaseConfig`

**What is needed.** Nothing from you; this is the announcement `PARALLEL_WORK_PROTOCOL.md` §3
requires the day a pre-approved shared-file change lands. `engine/config.py`'s `UseCaseConfig` now
declares `standard_schema`, `suggested_features`, `label` and `onboarding`, four lines above this
branch's §4 block. They had to go there: `_Base` sets `extra="forbid"`, so a use-case YAML cannot
carry the sections §3's ownership table assigns to Phase 2 until the model has fields for them, and
the contracts-first task could not add them without the Phase 2 plan. All four are optional with
defaults, so every existing config validates unchanged and no behaviour of yours can move. Their
types live in the PHASE-2 block at the foot of the same file and are resolved by a
`model_rebuild()` there.

**What I did meanwhile.** Nothing is stubbed — the change is in. Everything else that would have
needed room above the block went to `engine/onboarding/roles.py` instead (DEC-104), so this is the
only such edit this branch makes to `engine/config.py`. If either of you would rather these four
fields were moved into a reviewed change on `main`, say so and I will rebase onto it.

### 2026-09-22 — phase-2-onboarding → human reviewer: unify `OnboardingCheck` with `ValidationCheck`

**What is needed.** A reviewed change on `main` that lets `engine.contracts.ValidationCheck` accept
the onboarding code table as well as the Phase 1 one — `_known_code` consulting a registry the
phases add to, rather than `VALIDATION_CODES` alone. Phase 2 plan §7 gives the onboarding checks the
same contract as the Phase 1 checks and then re-runs the full Phase 1 validation on the assembled
dataset, appending its findings to the same list, so one type really does have to carry both.
`_known_code` sits above this branch's §4 block, which §4 forbids me to edit.

**What I did meanwhile.** `OnboardingCheck` in `engine/onboarding/specs.py`: same five fields plus
`source_id`, its own code table, and `from_validation_check()` to carry a Phase 1 finding across
unchanged (DEC-101). Nothing is lost while the request is open — the field contract is identical —
but it is a duplicated field list, so it should not stay open indefinitely.

## Resolved

### 2026-09-22 — the Phase 2 half of "the three phase plans" (asked 2026-09-22)

**Answer.** `MARKETING_AI_PHASE2_PLAN.md` is available to the `phase-2-onboarding` branch, and
`engine/onboarding/specs.py` is now §8 of it realised: `SourceProfile`, `SourceSpec`, `ClientRecord`,
`MappingSpec`, `FeatureSpec`, `LabelSpec`, `SnapshotSpec`, `OnboardingSpec`, `DatasetManifest`,
`BuildReport` and `BuildStatus`, with the fields §8 lists. The module is no longer empty and the
blocker it recorded is closed. The vocabulary those models are built from lives in the PHASE-2 block
of `engine/config.py` rather than in `specs.py`, for the cycle reason recorded in DEC-100; `specs.py`
re-exports all of it, so `from engine.onboarding.specs import ...` still gets the whole contract.

The Phase 3a and Phase 4a halves of the original request are **still open** — those plans are not in
the repository and nothing here supplies them.


### 2026-09-22 — contracts-first → human reviewer: the three phase plans

**What is needed.** `MARKETING_AI_PHASE2_PLAN.md`, `MARKETING_AI_PHASE3A_PLAN.md` and
`MARKETING_AI_PHASE4A_PLAN.md` are not in the repository. `PARALLEL_WORK_PROTOCOL.md` §2 items 2
and 3 ask the contracts-first task to add "the pydantic models named in Phase 2 §8" and "the models
named in Phase 3a §7", and those sections are the only definition of what those models are called
and which fields they carry.

**What I did meanwhile.** Items 1, 4, 5, 6 and 7 of §2 are complete and do not depend on the
plans: the composite `primary_key`, `dataset_id`, `client_id`, `LLMUsage` and `ComputeInfo` are on
the shared contracts; `engine/settings.py` and `engine/llm.py` exist; the marker blocks are in
every shared file; CI already runs the fast suite on every push and the slow suite nightly.
`engine/onboarding/specs.py` and `engine/generative/contracts.py` exist as empty modules, so the
import paths and the ownership are settled, and each one's docstring says exactly what is missing.
The models themselves are **not** guessed: a branch that starts against an invented `SourceSpec`
has to rename its fields when the real plan lands, and §2 forbids renaming a field of a shared
contract. Sending the three plans unblocks both modules with no change to anything already built.

### 2026-09-22 — contracts-first → human reviewer: `StageContext.primary_key` and the ownership map

**What is needed.** A ruling on who may widen `StageContext.primary_key` in `engine/pipeline.py`.

The composite key now reaches the engine's boundary: `RunRequest`, `Recipe`, `RunRecord`,
`FeatureSchema` and `RunManifest` all take `str | list[str]`. It stops there. `StageContext`, the
object the stages actually read, still carries a single `str`, and `POST /runs` narrows to it
through `sole_key()`, which refuses a composite key with `COMPOSITE_KEY_NOT_SUPPORTED`.

Phase 2 §6.5 is pre-approved to thread the composite key through `prepare.py`, `score.py`,
`actions.py` and `ingest.py`. To do that it must also widen `StageContext.primary_key` and change
the `sole_key()` call sites that feed those four stages — and `engine/pipeline.py` is a **shared,
append-only** file (§4), so on a literal reading Phase 2 cannot make the change its own task
requires.

Widening `StageContext` here instead was rejected: it is behaviour, not surface, and it would push
`sole_key()` into ~15 call sites across stages that Phase 2 does not own and that §3 freezes
(`train.py`, `evaluate.py`, `explain.py`). Better one explicit exception than a shared file
rewritten by the branch that needs the least of it.

**What I did meanwhile.** The narrowing is in one place per flow and is honest: a composite key
gets a named error saying what to do, never a silent join on the first column. Phase 2 can start
on everything up to the pipeline boundary with no ruling at all. The smallest change that unblocks
it is one line in §3: add `engine/pipeline.py`'s `StageContext` and its `sole_key()` call sites to
Phase 2's pre-approved exception list.

## Resolved

### 2026-09-22 — contracts-first: where §2's three model names live in this repository

**What is needed.** §2 item 1 puts `RunRequest`, `RunConfig` and `RunManifest` in
`engine/contracts.py`. Two of the three are elsewhere and the third does not exist under that name.

**Resolved, no ruling required.** `RunRequest` is in `api/schemas.py`, `RunManifest` is in
`engine/contracts.py`, and `RunConfig` is `engine.config.Recipe` — the object `train(recipe)` takes
and `RunManifest.recipe` carries. `run_config.json` is `ResolvedConfig`, the merged YAML document,
whose leaves are configuration settings rather than per-run choices, so the key does not belong
there. `Recipe` already carried `primary_key` and was widened in place. `RunRecord` and
`FeatureSchema` were widened too, though §2 lists neither: the same column name is serialised into
both, and both live in an append-only file, so leaving them narrow would have stopped the key at the
first artefact it reaches with no way for Phase 2 to fix it. See DEC-077.
