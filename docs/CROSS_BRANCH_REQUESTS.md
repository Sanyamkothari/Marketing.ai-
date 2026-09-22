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

### 2026-09-22 — library-datasets → whoever owns `tests/unit/test_config_loading.py`: two assertions forbid a second industry

**What is needed.** Two assertions relaxed, so that a second industry and a seventh use case can be
added to `configs/` without turning the suite red. The library's whole purpose is to show that a
second, third and fourth industry need configuration and nothing else, and adding
`configs/industries/banking.yaml` fails `test_industries_list_and_telecom_loads`
(`assert list_industries() == ("telecom",)`, line 109); adding
`configs/use_cases/bank_term_deposit.yaml` fails
`test_industry_available_entries_have_files_and_matching_stage_names`
(`assert sorted(available) == sorted(list_use_case_ids())`, line 137), which pins the telecom
industry file to listing *every* shipped use case. A third would follow:
`tests/unit/test_generated_files.py::test_templates_are_up_to_date` needs a generated pair in
`templates/` for each new use case, and `templates/` is not this branch's either. Re-confirmed
against the merged tree on 2026-09-22, after `ai-onboarding-assistant` was promoted to available:
both assertions are unchanged and both still fail.

The smallest change is to make each assertion about the telecom industry rather than about the
whole config directory: `assert "telecom" in list_industries()`, and in the second test drop the
`== sorted(list_use_case_ids())` line (the loop above it already checks that every id telecom lists
has a file and a matching stage name) or narrow it to the ids telecom actually owns. Nothing in
`engine/` needs to change — `load_industry`, `list_industries` and every loader already take a
config root and already validate each file on its own (DEC-038). The engine is happy with four
industries; two test assertions are not.

**What I did meanwhile.** The library's four industry files, four use-case files and their
generated templates went into `library/configs/`, a second config root the engine already supports
(`MARKETING_AI_CONFIG_DIR`, or `root=` on every loader). Its `engine.yaml` is a **symlink** to
`configs/engine.yaml`, so there is exactly one copy of the defaults and nothing to drift, and
`library/run_engine.py` takes `--config-root`. Nothing under `configs/`, `templates/`, `engine/`,
`api/`, `ui/` or `tests/` was touched and no test broke. The five datasets ran end to end against
that root; the telco dataset runs against the repository's own `configs/`, because it reuses the
shipped `telco-churn` use case, and proves the same point inside the product. When the two
assertions are relaxed, moving the configs is `git mv library/configs/industries/*.yaml
configs/industries/`, the same for `use_cases/`, then `make generate`. Recorded as DEC-400.

### 2026-09-22 — library-datasets → whoever owns `engine/stages/prepare.py`: two PII detectors that disagree

**What is needed.** One PII detector, not two. `engine/stages/validate.py:390` says "PII detection
has exactly **one** definition in the engine - `engine.stages.ingest.detect_pii`". It does not:
`engine/stages/prepare.py:511` has a second, `_detect_pii`, with its own `_PII_VALUE_PATTERNS`, and
the two differ in three ways — ingest skips FLOAT, BOOLEAN, DATE and DATETIME columns
(`_PII_TYPES`, line 824) and prepare examines every column; ingest uses `fullmatch` and prepare
`search`; ingest applies a per-detector match rate *and* a distinct-value ratio, prepare a flat
50 %.

The phone-number pattern is `\+?\d[\d\s().-]{7,17}\d`, and an ISO date matches it:
`re.compile(r'\+?\d[\d\s().-]{7,17}\d').fullmatch('2011-09-10')` is a match. On the
`library/online-retail` win-back file, whose `snapshot_date` is `2011-09-10` in every row, one run
produced `validation.json` with a single `CONSTANT_COLUMN` finding and no `PII_DETECTED` — ingest's
type gate correctly skipped a DATE column — while `prepare.json` for the same run carries
`pii_columns: ["snapshot_date"]` and a `redact` transform replacing its values with `[REDACTED]`.
The engine told the user one thing and did another.

Here it changes nothing: the column was constant and headed for the bin anyway. On a file with
several snapshot dates — the shape `split.type: time_based` exists for — a live date column would
be silently redacted with nothing in the validation report to say so. The smallest change is to
delete `prepare._detect_pii` and `_PII_VALUE_PATTERNS` and call `ingest.detect_pii` per column,
which is what the validate docstring already claims happens; failing that, give prepare ingest's
type gate, the one line `if inferred not in _PII_TYPES: return ()`.

**What I did meanwhile.** Nothing was blocked. The run finished, and the discrepancy is reported in
full in `library/online-retail/run_report.md` under "A discrepancy worth recording", with the
artefact keys quoted, so the next reader meets it as a known issue rather than a surprise.

### 2026-09-22 — library-datasets → whoever owns `engine/config.py`: a template column name cannot contain a dot

**What is needed.** Either a widened pattern or one line in the data contract.
`TemplateColumn.name` is `Annotated[str, Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]`
(`engine/config.py:1144`, as of the merge of 2026-09-22). Two of the six public datasets publish dotted column names, and one of
them is a target: UCI Bank Marketing has `emp.var.rate`, `cons.price.idx`, `cons.conf.idx` and
`nr.employed`, and UCI Default of Credit Card Clients has `default.payment.next.month`. A dotted
name is legal in a CSV header, in pandas and in AutoGluon; the engine accepts it as an ordinary
feature. Only the template refuses it, and `_check_template` then rejects the whole use case with
`TEMPLATE_TARGET_MISMATCH` when the target is one of them. The only workaround inside the schema is
to leave `template.columns` empty, which costs the use case its generated template and its
`TARGET_MISSING` hint.

Either widen the pattern to allow interior dots (`^[A-Za-z_][A-Za-z0-9_.]*$` — the name is used as
a CSV header and a dictionary key), or add a line to `docs/DATA_CONTRACT.md` §7 saying template
column names are identifiers, so a file with dotted headers is expected to need a mapping step. The
second is cheaper and probably the better answer.

**What I did meanwhile.** Renamed the five columns to their underscored form in each dataset's
`fetch.py` and recorded every rename in its `mapping.yaml`, which is what a mapping layer is for
and what Phase 2's column-mapping UI will absorb. Both use cases carry full templates and the
source names are not lost. Recorded as DEC-404.

### 2026-09-22 — library-datasets → whoever owns `engine/stages/evaluate.py`: `threshold.mode: auto` can call every row positive

**What is needed.** A guard in the `auto` threshold search. `auto` maximises F1 on the validation
split, and on a weak model over a base rate near 50 % that maximum really can be at "predict
everything positive". On `library/online-retail` it was: threshold 0.3148, recall 1.0, specificity
0.0, precision 0.4110 — exactly the test-split positive rate — and accuracy 0.4110.

Nothing is mis-computed, and the score ranking, the bands and the decile chart are unaffected
because those read the score rather than the threshold. But the Model page would report 100 %
recall, which reads as a triumph and means the model made no decision at all. The smallest change
is to skip candidate thresholds whose confusion matrix has no predicted negatives (or no predicted
positives) and take the best remaining one; if none qualifies, fall back to 0.5 and say so in
`threshold_detail`, which already exists to carry exactly that sentence.

**What I did meanwhile.** Nothing was blocked. The numbers are reported as measured in
`library/online-retail/run_report.md`, with a paragraph explaining why 100 % recall is not good
news, so nobody quotes it as a result.


### 2026-09-22 — library-datasets → human reviewer: `README.md` has no block a non-phase branch may write in

**What is needed.** A ruling, and if it goes the obvious way, one line in `README.md`. The protocol
gives `README.md` three marked blocks — PHASE-2, PHASE-3A, PHASE-4A — and this branch is none of
them, so there is nowhere it may legally append. The result is that `docs/LIBRARY.md`, the six
public datasets, the four extra industries and the demo script are not reachable from the
repository's front door: `README.md` §"Where things are written down" lists `DECISIONS.md`,
`DATA_CONTRACT.md`, `API.md`, `AWS_DEPLOYMENT.md` and `CROSS_BRANCH_REQUESTS.md`, and a reader who
never opens `library/` will not know the library exists.

Either add a fourth marker block for work that is not one of the three phases, or paste this line
into the existing list yourself:

> [`docs/LIBRARY.md`](docs/LIBRARY.md) is the public dataset library: six public datasets across
> five industries, run through the engine on configuration alone, with what each run actually
> scored and what needed a code change.

**What I did meanwhile.** Did not touch `README.md`. Every library document cross-links to the
others — `docs/LIBRARY.md` ↔ `library/README.md` ↔ `library/DEMO_SCRIPT.md` ↔ each dataset's
`README.md` and `run_report.md` — so the set is navigable from any one of them, and
`docs/DECISIONS.md` (DEC-400 … DEC-411) names `docs/LIBRARY.md`. Only the entry point from
`README.md` is missing.


### 2026-09-22 — reviewer → phase-4a-aws and human reviewer: `make test-all` will bill Bedrock once AWS credentials exist

**What is needed.** A one-line change before Phase 4a puts AWS credentials anywhere CI can see them.
`tests/integration/test_bedrock_smoke.py` calls Bedrock for real. `pyproject.toml` declares the
marker as *"costs money, needs credentials, opt-in with `-m bedrock`"* and the module carries
`pytestmark = [..., pytest.mark.bedrock]` — but nothing implements the opt-in: `addopts` is
`-q --strict-markers` with no marker filter, `make test-all` is plain `pytest`, and
`.github/workflows/nightly.yml` runs `make test-all`.

The suite skips today only because the three Bedrock env vars and AWS credentials are absent. Both
gates are configuration, not intent, so the protection inverts exactly where it matters: the machine
most likely to carry both is the Phase 4a agent's, and the nightly runner as soon as Phase 4a adds
credentials to CI — which is that branch's natural next step. From then on every nightly makes paid
calls nobody asked for, and the first signal is the invoice.

`make test` already deselects with `-m "not slow"`, so the pattern exists. Either `make test-all`
becomes `pytest -m "not bedrock"` or `addopts` carries it; `-m bedrock` then genuinely opts in.
`Makefile` and `pyproject.toml` are both Shared files (§3), so this is a human call rather than a
branch's to take unilaterally.

**What I did meanwhile.** Did not run the suite — the standing instruction is never to run `-m aws`
or `-m bedrock`. Verified the rest by reading: the double gate is correct and both skips are loud and
well written, so nothing is wrong with the suite itself; the exposure is only in how `test-all`
selects. Confirmed `make test` is unaffected, and that the 7 skips in tonight's full run are exactly
this module. Recorded as E-1 in `reports/2026-09-22.md`, with E-2 (the library sits outside §3's
ownership map and uses an unreserved `DEC-400…499` band) and E-3 (no input-side prompt-injection
boundary and no test for it) in the same pass.

## Resolved

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
and the branch diverge at `46ab665`.

**Correction, same day — read this before acting on the paragraph above.** I first wrote here that
the `engine/contracts.py` divergence "is additive on the branch … so merging the branch into
`main` should not break `main`'s Phase 2 work." That was read off the diff, not measured, and it
is **wrong**. The §8 merge simulation has now been run: merging the branch into `main` conflicts in
**8 files, 17 hunks** — `engine/contracts.py` (4), `api/routes/runs.py` (3), `docs/API.md` (3),
`api/schemas.py` (2), `engine/onboarding/specs.py` (2, add/add), `README.md` (1),
`docs/DECISIONS.md` (1), `engine/onboarding/__init__.py` (1, add/add). The merged tree cannot be
tested because the merge never reaches a commit.

The conflicts are semantic. §2.1 item 1 specifies `primary_key: str | list[str]`. The branch has
`PrimaryKey = str | list[str]` and matches it; `main` has `str | tuple[str, ...]` and does not.
`tuple` and `list` differ in JSON serialisation and in every downstream type check, and
`sole_key()`, the composite-key narrowing path and `FeatureSchema` are all typed against the
branch's alias. Suggested resolution: take the protocol's type, since §2.1 is the written
specification and `main` departed from it.

This does not change the request — it makes it more urgent, since the reconciliation is a real
merge rather than the near-clean one I implied, and it is far cheaper before any phase branch is
cut than after three have built on two incompatible surfaces. Recorded as A-1, A-2 and A-4 in
`reports/2026-09-22.md`. I hold no code and have pushed none; this entry is the whole of my
action.

**Resolved 2026-09-22.** `main` was force-updated from `04c76da` to `5f93051`, the
contracts-first commit this branch already carried, so the divergent surface is gone from every
branch rather than reconciled hunk by hunk. `main` now has all seven §2 items, the
protocol-conforming `primary_key: str | list[str]`, `PARALLEL_WORK_PROTOCOL.md` and this file.
`merge-base(main, branch) == main`: the branch is 11 commits ahead, 0 behind, and merges clean, so
the 17-hunk conflict measured against `04c76da` no longer exists anywhere. The phase branches can
be cut from `main` safely. The three phase plans (still open above) continue to gate
`engine/onboarding/specs.py`, which is the one §2 item `main` carries only as a stub.

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
