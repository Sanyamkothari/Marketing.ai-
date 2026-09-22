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

### 2026-09-22 — phase-3a-generative → phase-2-onboarding and phase-4a-aws: `LLMClient.complete` takes an optional `system`

**What is needed.** Nothing from anybody; this is the announcement §3 asks for when a branch
touches a surface another branch reads. `LLMClient.complete` has gained a keyword-only
`system: str = ""`, and `LLMCall` a matching `system` field.

It is an extension in §2's sense rather than a change: the parameter has a default, every existing
call site keeps working untouched, `FakeLLMClient` folds it into its digest and records it, and no
field was renamed, removed or given a new meaning. The reason is that the Converse API takes a
system prompt as its own block, and a generative flow's whole grounding rule — answer only from the
extracts, cite what you used, never invent a number — belongs in that block rather than concatenated
onto the front of the user turn where a model weighs it as ordinary input. Folding the two into one
string would have been the alternative, and it would have made every Phase 3a prompt measurably
weaker at the one thing the guardrails then check.

**What I did meanwhile.** Nothing was blocked. `BedrockLLMClient` sends it as the Converse system
block; a caller that passes nothing gets exactly the behaviour that was there before.

### 2026-09-22 — phase-3a-generative → human reviewer: a second fake, beside `FakeLLMClient`

**What is needed.** A ruling, at review time, on whether two fakes are one too many.
`FakeLLMClient` stays exactly as the contracts-first task wrote it and `tests/unit/test_llm.py`
still pins it. Phase 3a has added `GroundedFakeLLMClient` beside it.

The reason is that the two are asked for different things. DEC-076 chose a digest deliberately —
visibly machine-made, reaching nothing, assertable byte for byte — and that is right for the seam.
It also means a hash embedding has no semantics, so the chunk that answers a question sits no
closer to it than any other chunk in the corpus, and `[fake completion 3f2a…]` quotes no extract
and cites no chunk. A retrieval test against the first would assert that one random number beat
another, and every grounding test would pass for the wrong reason. The second fake hashes words
into buckets so shared vocabulary means proximity, and builds its answer out of the extracts it was
handed. `GroundedFakeMode` then breaks exactly one rule at a time, which is how the guardrails are
tested at all.

**What I did meanwhile.** Both ship, `build_client` hands the grounded one to a generative flow,
and neither touches the other. If the ruling is that there should be one, the merge is to move the
grounded behaviour into `FakeLLMClient` behind a mode argument whose default is today's digest —
which keeps `tests/unit/test_llm.py` green as written. See DEC-214.

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

### 2026-09-22 — reviewer → human reviewer: `main` is missing four of the seven contracts-first items, and the protocol itself

**What is needed.** A decision on the cut point for the three phase branches, before they are cut.
The contracts-first task of §2 was done twice, 64 seconds apart, by two agents unaware of each
other: `5f93051` on `claude/gracious-lovelace-c344tl` (07:47:54) and `04c76da` on `main`
(07:48:58). §2 says it is one PR on `main`, and §1 has all three branches rebase onto `main`, so
what is on `main` is what the branches inherit. Measured against §2's own checklist, `main` is
missing item 1's `RunManifest.primary_key`, all of item 3 (`engine/generative/contracts.py`,
`LLMUsage`, `RunManifest.llm_usage`), all of item 4 (`engine/settings.py`, `ComputeInfo`,
`ComputeBackend`, `RunManifest.compute`) and all of item 5 (`engine/llm.py`). It also does not
carry `PARALLEL_WORK_PROTOCOL.md` or this file, so a branch cut from `main` today would have no
ownership map, no merge order, no frozen-file list and nowhere to write a request.

The consequence is the collision §2 exists to prevent: Phase 3a would add `llm_usage` and Phase 4a
would add `compute`, on the same day, to the same `RunManifest`, in `engine/contracts.py`, which §3
marks Shared. §2's "never rename, remove or change the meaning of an existing field" cannot prevent
it, because on `main` those fields do not yet exist to be preserved.

Suggested order: merge `5f93051` into `main`; reconcile `engine/onboarding/specs.py` (21-line stub
on the branch, 706 lines on `main`) once the open request above for the three phase plans is
answered, since the plan is the only definition of record for those models and is in neither
branch; then cut the three branches from the merged `main`.

**What I did meanwhile.** Verified the rest of the shared surface rather than assuming it: the
branch's items 1, 4, 5, 6 and 7 are present and the marker blocks are in every shared file; `main`
and the branch diverge at `46ab665`; the divergence in `engine/contracts.py` is additive on the
branch (`ComputeBackend`, `LLMUsage`, `ComputeInfo` added, nothing renamed, removed or
type-changed), so merging the branch into `main` should not break `main`'s Phase 2 work. Recorded
as A-1 and A-2 in `reports/2026-09-22.md`. I hold no code and have pushed none; this entry is the
whole of my action.

## Resolved

### 2026-09-22 — contracts-first → human reviewer: the Phase 3a half of "the three phase plans"

**What is needed.** `engine/generative/contracts.py` was left empty because
`MARKETING_AI_PHASE3A_PLAN.md` §7 was the only definition of the models it should hold.

**Resolved for Phase 3a, 2026-09-22.** The plan reached the `phase-3a-generative` branch and the
module is filled in from its §7 rather than guessed: `DocIndexManifest`, `RagEval`,
`RootCauseSummary`, `CopyBatch`, `CopyMessage`, `GuardrailReport` and `LlmUsageReport`, with the
chunk and evidence models they carry. Nothing the contracts-first task wrote was renamed or
re-meant: `engine.contracts.LLMUsage` stays the per-run total a `RunManifest` carries, and
`LlmUsageReport.totals` *is* that object — the artefact explains the total rather than restating it
in different words. `GENERATIVE_ARTEFACTS` is a map parallel to `ARTEFACT_REGISTRY` rather than an
addition to it, because that registry is pinned name-for-name by a test and a generative artefact
is not a predictive one (DEC-210).

The Phase 2 and Phase 4a halves of the original request are still open: `engine/onboarding/specs.py`
is untouched and this branch has no business filling it in.

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
