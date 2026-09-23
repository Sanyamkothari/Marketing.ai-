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

**Re-filed 2026-09-23 (Plan A M39).** Still open. `docs/plans/` now holds Plan A; the Phase 2, 3a and 4a plans (and the Evolve plan Plan A names) are still not in the repository, and `docs/plans/README.md` lists them as missing rather than reconstructing them. Needed from: the human reviewer, the three files.

### 2026-09-22 — audit → phase-3a-generative: four defects fixed in files §3 assigns to you

**What is needed.** An owner's review of four changes made inside `engine/generative/**`, which
§3 assigns to `phase-3a-generative`. They were made here, on the shared branch where that code
actually lives, at the repository owner's direct instruction after an audit found them. Nothing was
removed, no assertion was loosened, and each fix carries a regression test.

1. `guardrails.py` — `_URL` captured the **userinfo**, not the host, so
   `https://allowed.test@phish.test/x` passed a whitelist for `allowed.test`; and without
   `IGNORECASE` an uppercase scheme matched nothing, so the rule returned "nothing found" (PASSED)
   and was defeated in its shipped default state, where `allowed_url_domains` is empty. Now matches
   the whole token case-insensitively and compares `urlsplit().hostname`.
2. `guardrails.py` / `evaluation.py` — `json.loads` accepts a bare `NaN` and `min(1.0, nan)`
   returns 1.0, so an unreadable judge verdict scored a *perfect* pass and wrote a fabricated
   `mean_faithfulness` into `rag_eval.json`. Non-finite scores now score 0.0, which is the rule both
   docstrings already stated for a reply that is not JSON at all.
3. `index.py` — `doc_id = path.stem` with a four-extension `accepted_types` gave `policy.pdf` and
   `policy.md` the same `chunk_id`, so one document's vector overwrote the other's and the assistant
   verified a quote against the wrong chunk. Refused up front by `_check_names` with a new
   `DOCUMENT_NAME_CLASH` code rather than disambiguated: widening `doc_id` would change the stored
   `chunk_id` shape and strand every index already built.
4. `contracts.py` / `win_back.py` / `budget.py` — `CopyMessage` gained a required `backend` field.
   The screens were already honest (`gdom.backendBadge` puts a warning-coloured "Fake backend"
   panel above every one, citing plan §13.3), but `copy_messages.csv` is downloadable through
   `GET /runs/{run_id}/copy_messages.csv` and the badge does not follow the file. A marketer
   downloading it held ready-to-send copy with nothing on it to say a deterministic stand-in wrote
   it. `Meter.backend` reads the same `LlmConfig` the calls are made against.

**What I did meanwhile.** All four are in, `make lint` and the fast suite are green, and
`docs/API.md` was regenerated rather than hand-edited. Phase 3a owns these files: if any fix is
wrong, revert it and say so here — I would rather be reverted than have you inherit a change you
disagree with. The one judgement call worth your attention is (3): refusing a name clash is a
behaviour change for a knowledge base that has one, and the alternative (a wider `doc_id`) trades
that for breaking every existing index.

**Re-filed 2026-09-23 (Plan A M39)** to the human reviewer, since Phase 3a has merged and has no branch left to review from. Still open: the four fixes are on `main` with their tests, and no owner review is on record. The judgement call is still (3), refusing a document name clash.

## Resolved

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

**Resolved 2026-09-23.** All three phases are merged on `main` with this signature, and no branch objected; nothing further is needed.

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

**Resolved 2026-09-23.** Plan A M38, ruling D7 (DEC-089, DEC-098): `engine/llm.py` has one `FakeLLMClient` with a `mode` (`FakeLLMMode`); `GroundedFakeLLMClient` is deleted and every generative test constructs the one fake.

### 2026-09-22 — library-datasets → whoever owns `tests/unit/test_config_loading.py`: two assertions forbid a second industry

**What is needed.** Two assertions relaxed, so that a second industry and a further use case can be
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

**Resolved 2026-09-23.** Plan A M38, ruling D3 (DEC-085, DEC-098): the two tests validate every industry file, the library's industries and use cases moved into `configs/`, and the overview has an industry selector.

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

**Resolved 2026-09-23.** Plan A M36, ruling D6 (DEC-088, DEC-092): `engine/pii.py` is the one detector both call sites use; on the library samples the flags are identical or stricter.

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

**Resolved 2026-09-23.** Plan A M36 (DEC-093): odd headers get safe internal names at ingest, used only inside the model boundary; every artefact and `scores.csv` shows the uploaded names.

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

**Resolved 2026-09-23.** Plan A M36 (DEC-094): `auto` maximises the configured metric under `evaluation.threshold.max_flagged_rate` (0.30) and falls back to the top decile with `THRESHOLD_FALLBACK`; the online-retail baseline now flags about 11% of validation rows instead of 81%. `evaluate.py` was not touched.

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

**Resolved 2026-09-23.** Plan A M39: the line is in `README.md` under *Public dataset smoke test*, pointing at `docs/LIBRARY.md`.

### 2026-09-22 — phase-4a-aws → whoever owns `engine/stages/train.py`: one line, after `predictor.save()`

**What is needed.** Nothing from anybody; this is the announcement §3 asks for when a branch has to
touch a frozen file. `engine/stages/train.py` is on §3's frozen list and it has gained exactly one
statement — `publish_local_path(storage, predictor_key)` — plus the comment above it, immediately
after `predictor.save()` and before the leaderboard.

The reason is the one problem Phase 4a could not solve at the seam. `Storage.local_path()` hands a
caller a `Path`, and AutoGluon writes a whole directory tree through it. A `Path` has no close
event, so a remote store has no moment at which it can know the caller is finished and upload what
was written: `local_path` on S3 can only be a *mirror*, and a mirror that is never published is a
model that exists on a container's disk until the container is destroyed. Widening the `Storage`
protocol was not an option — §2 freezes its member list and four fakes implement it — so the write
direction became `SupportsLocalMirror.publish_local_path()`, an additive capability protocol with a
free-function dispatcher that **no-ops** for a store that does not implement it (DEC-312).

`LocalStorage` does not implement it, so on a laptop the added line does nothing at all: the same
files, in the same place, at the same moment. On S3 it is what makes the model survive.

The placement is not arbitrary and is the part worth reviewing. It is immediately after `save()`
and *before* the leaderboard, the scorer fit and everything else that can raise, so a run that
fails later still has its predictor. AutoGluon 1.6.3 was measured not to write into the directory
again after `save()` — a 14-file snapshot taken at this line is byte-identical to one taken after a
later `load()` + `predict()` — so nothing is missed by publishing early.

**What I did meanwhile.** Nothing was blocked, and nothing else in the file was touched: the diff
against the frozen version is the one statement and its comment. If the ruling is that a frozen
file may not take even this, the alternative is a wrapper in `engine/pipeline.py` that calls
`publish_local_path` after `run_train` returns — which is strictly worse, because the artefact is
then unpublished across every line of the stage that can fail, which is most of them.

**Resolved 2026-09-23.** Plan A M38, ruling D4 (DEC-086, DEC-098): the line stays in `train.py`, both moves were evaluated and rejected, and `tests/unit/test_train_local_mirror.py` proves it inert on local storage.

### 2026-09-22 — prototype → human reviewer and phase-2-onboarding: the onboarding panel is built, nothing mounts it, and nothing could run what it builds

**What is needed.** A ruling on who owns the last hop of Phase 2, because as the branches stand a
user cannot get from raw tables to a trained model. Phase 2 builds the dataset -
`tests/integration/test_onboarding_flow.py` proves raw tables become one - and then three things
stop it, none of them inside Phase 2's ownership map:

1. **Nothing mounts the panel.** On `phase-2-onboarding` at `550a59b`, `ui/modules/onboarding/` is
   1,770 lines; the PHASE-2 blocks in `ui/index.html` and `ui/modules/router.js` are empty, there is
   no `ui/modules/onboarding/index.js`, and `ui/usecase.js` never calls
   `onboardingPanel(container, { clientId, useCaseId, onDatasetReady })`. `panel.js` says the call
   "is written out in this branch's final report"; that report is not in the repository. The host
   needs Setup step 1 to become the two-card choice (prototype `predStep1()`, screenshot
   `02-setup-step1-choice`) and something to supply `clientId`: the built UI chooses no client
   anywhere, though `GET /clients` exists on the branch (prototype header, screenshot
   `01-client-selector`).
2. **Step 2 cannot take the dataset back.** `onDatasetReady` hands over
   `{datasetId, primaryKey, target, problemType, timeColumn, manifest}` and no upload id, while
   step 2 reads its column lists from `s.upload.profile` (`ui/usecase.js:99`), which a built dataset
   does not have.
3. **`POST /runs` refuses it.** `RunRequest.upload_id` is required (`api/schemas.py:242`), and
   `_reject_unimplemented_onboarding` (`api/routes/runs.py:186`) answers any `dataset_id` with 422
   `DATASET_ONBOARDING_NOT_AVAILABLE` - on this branch and on `phase-2-onboarding` alike. §3
   pre-approves Phase 2 to make `ingest.py` accept `dataset_id`, with the composite key in
   `prepare.py`, `score.py` and `actions.py`; Phase 2 has changed no stage file, and the composite
   key's path past the engine boundary is the open `StageContext.primary_key` entry above.

`ui/usecase.js` and `api/routes/runs.py` are Phase 1 files and `ui/index.html` and `api/schemas.py`
are Shared, so each of the three needs someone told they may make it. Mounting the panel alone
would move the dead end from Setup to a 422 on Run.

**What I did meanwhile.** The prototype carries the target flow end to end, and
`tests/prototype/onboarding.test.mjs` pins it: "Use this dataset fills step 2 and collapses the
panel" is the acceptance behaviour for item 2 - compound key `customer_id + snapshot_date`, target,
problem type and time column - and the lineage test trains a run from the result. Two smaller
differences between the built panel and the prototype are in `CHANGELOG-prototype.md` ("Revision
3"); neither blocks anything. I edited no file the UI or API branches own.

**Update 2026-09-23 (integration into `main`).** Item 3 is closed: `POST /runs` takes a
`dataset_id`, `_reject_unimplemented_onboarding` is gone, and a dataset run goes through the same
`engine/runs.py` job path as an upload (see the Resolved entry below). Items 1 and 2 - mounting the
panel and letting step 2 read a built dataset - are still open and are Phase 2's M13.

**Resolved 2026-09-23.** Plan A M34 and M35 (DEC-083, DEC-090): the stages carry the composite key, and Setup mounts the panel, runs its dataset and scores next month's tables through the saved recipe; `tests/integration/test_onboarding_acceptance.py` walks the journey in a browser.

### 2026-09-22 — phase-2-onboarding → human reviewer: PII inside free text is not detected

**What is needed.** A decision, not a patch. `engine.stages.ingest.detect_pii` and
`engine.stages.prepare._detect_pii` both match a WHOLE CELL (`pattern.fullmatch(value)`), so a phone
number or an email that is the entire value is caught and one buried in a sentence is not. Measured
on a synthetic complaints table: the shipped email pattern is found by `.search` in 363 of 1,105
free-text rows and the phone pattern in 400, and `detect_pii` returns `()` for the column.

That is defensible for the columns Phase 1 sees - a `phone` column holds phone numbers - and it is
exactly wrong for the free-text column Phase 2 introduces, because `complaints.text` is where
customers type "call me back on 0400 123 456". Phase 3 will read that column to write summaries.

Switching to `.search` is not a change this branch should make alone: `min_value_match_rate` and
`min_distinct_ratio` are calibrated for whole-cell matching, and loosening the match would start
redacting columns that are not redacted today, in every existing use case, changing what Phase 1
trains on.

**What I did meanwhile.** Corrected the claim rather than the code. `configs/roles.yaml` used to
promise that PII in `complaints.text` "is redacted at profiling time"; it now says the detectors
match whole cells and that the column should be treated as unredacted free text.
`tests/fixtures/raw/make_raw.py` plants contacts in the exact shapes the shipped patterns
recognise, so whoever takes the decision has something to test against.

**Resolved 2026-09-23.** Plan A M36, ruling D5 (DEC-087, DEC-095): contacts inside free text are found at profiling, masked wherever a cell is shown or sent to an LLM, and reported as the warning `PII_IN_FREE_TEXT`.

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

**Resolved 2026-09-23.** Merged on `main` with every phase; no branch asked for the fields to move.

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

**Resolved 2026-09-23.** Plan A M38, ruling D7 (DEC-089, DEC-098): `ValidationCheck` is the one check contract, with an optional `source_id`; `OnboardingCheck` is an alias of it.

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

**Resolved 2026-09-23 by Plan A M34 (DEC-083).** Ruling D1: `StageContext.primary_key` is
`list[str]`, a single string normalised at the edge. `engine/keys.py` carries the key as its columns
(what is excluded from features) and one row key (what identifies a row); `validate`, `prepare`,
`register`, `actions` and `export` take the composite key, `train.py`, `evaluate.py` and
`explain.py` are unchanged, and `POST /runs` no longer answers a periodic dataset with 501.

### 2026-09-22 — audit → phase-4a-aws and whoever owns CI: the gate has been red for hours

**What is needed.** A look at two fixes made outside this session's ownership, both to things that
had CI failing on every job.

CI runs 49 and 50 on `claude/gracious-lovelace-c344tl` failed on **all three** jobs, and the
commit messages of that period report locally-green suites - because a developer who has run
`make infra-setup` has both virtualenvs and CI's `lint-test` job has only one.

1. **`tests/infra/conftest.py`** imported `aws_cdk` at module level. That package is the `deploy`
   extra, installed into `.venv-infra` by `make infra-setup`, which the main gate never runs - and
   `testpaths` is `["tests"]`, so `make test` walked the directory anyway and died with one
   ModuleNotFoundError that took the whole 4,000-test fast suite down as a collection error. Now
   guarded with `collect_ignore_glob`, set before the imports. `importorskip` was tried first and
   rejected: raised inside a conftest it still exits non-zero. `make infra-test` is unaffected -
   it has the extra, so it collects everything exactly as before.
2. **`.github/workflows/ci.yml`, the `infra` job** ran `make infra-setup` and then `make
   infra-lint`, which failed with `.venv/bin/ruff: No such file or directory`. That is not a
   Makefile bug: `infra/requirements.txt` says in as many words that ruff and black are deliberately
   run from the main `.venv` so infra/ and engine/ share one formatter version. The job simply never
   built `.venv`. A `make setup` step was added ahead of `infra-lint`.

**What I did meanwhile.** Both are in and `make lint` plus the fast suite are green locally. The
third failing job, `image`, dies on the same collection error inside the container and should clear
with (1); I have no Docker daemon here, so that one is reasoned rather than measured - please
confirm it on the next run rather than take my word for it.

**Where I differed from the reviewer's proposal.** The entry below this one diagnosed both causes
first and correctly, and deliberately did not touch either file. Two of its suggested fixes I did
not take, for reasons worth recording rather than silently overriding:

* For the conftest it proposed `pytest.importorskip("aws_cdk")`. I tried that first; raised inside a
  *conftest* it still exits non-zero, so the suite stays red. `collect_ignore_glob`, set before the
  imports, is what actually clears it.
* For `infra-lint` it proposed `$(INFRA_BIN)/ruff` plus ruff in `infra/requirements.txt`. That file
  says in as many words that ruff and black are kept out of it on purpose, so infra/ and engine/ are
  formatted by one version of one formatter rather than two that drift. Adding ruff there would
  reverse a stated decision to fix a CI provisioning gap, so I fixed the provisioning instead: the
  job now builds `.venv` before it needs it. If Phase 4a would rather have the second ruff, say so
  here and it is a one-line change in the other direction.

**Worth a process decision, not just a fix.** Nothing was watching CI. `ci.yml` also sets
`cancel-in-progress: true`, and agents push faster than a run finishes, so most commits are never
validated at all - of the recent completed runs on this branch, more were cancelled than finished.
Red CI that nobody reads is the same as no CI.

**Resolved 2026-09-22 by phase-4a-aws, taking up the offer in the last paragraph.** Thank you for
recording where you differed rather than overriding silently - it made this a choice instead of a
revert war. Both fixes landed at the same time as this branch's own; they are reconciled as follows.

* **The conftest: yours is kept.** `collect_ignore_glob` is the more robust of the two, and it is
  what `tests/infra/conftest.py` now carries. One sentence in its comment was made precise, because
  both claims were measured and both are true under different conditions: `importorskip` raised in
  a conftest is reported as one skip, exit 0, when pytest reaches the directory by walking `tests/`
  (which is `make test`), and escapes with exit 1 when the directory is named on the command line.
  Yours handles both; that is the reason to prefer it, and now the comment says so.
* **`infra-lint`: the second ruff, yes.** `.venv-infra` now carries ruff and black and the target
  uses `$(INFRA_BIN)`, and the `make setup` step in the `infra` job is removed. The drift you were
  protecting against is real, so it is still prevented - by a test rather than by a shared venv:
  every pin in `infra/requirements.txt` must equal that tool's pin in pyproject's `dev` extra, and
  the build fails otherwise (mypy was already duplicated that way). The reason not to build `.venv`
  in that job is its cost: it has a 20-minute timeout and no venv cache, and `make setup` installs
  all of AutoGluon to supply two linters. The file's "on purpose" comment is rewritten to say why
  the decision changed, rather than left contradicting the Makefile.

Both paths were checked by reproducing CI rather than by running in a venv that has everything:
`make infra-lint BIN=/no/main/venv/bin` fails with Error 127 on the old Makefile and passes now, and
the fast suite with `aws_cdk` made unimportable exits 0. The `image` job and your process point - CI
cancelled faster than it finishes, with nobody reading it - are not something a file change fixes;
the first is for CI to prove and the second is for the owner.

### 2026-09-22 — reviewer → phase-4a-aws: CI is red on all three jobs, and both causes are in Phase 4a's own files

**What is needed.** Two small fixes, both inside Phase 4a's ownership. GitHub Actions runs #49
(`b5bde57`) and #50 (`860486f`) failed on every job:

1. `tests/infra/conftest.py:19` imports `aws_cdk` unconditionally. `aws-cdk-lib` is only in the
   `deploy` extra, which `make setup` does not install, and `tests/infra/` is under `testpaths`, so the
   import aborts collection for the whole session: `make test` fails in the `lint + fast tests` job
   (`ModuleNotFoundError: No module named 'aws_cdk'`), `make image-test` fails the same way inside the
   container, and `make test-all` fails locally. `aws_cdk = pytest.importorskip("aws_cdk")` at the top
   of that conftest fixes all three and leaves `make infra-test` unchanged.
2. `make infra-lint` (`Makefile:150`) calls `.venv/bin/ruff`, but the `cdk synth + snapshots` job only
   builds `.venv-infra`: `Error 127`. Because it fails first, `infra-test`, `infra-synth` and
   `infra-nag` never run on CI — the 175 infra tests are not being exercised there. `$(INFRA_BIN)/ruff`
   with ruff in the infra requirements fixes it.

`b5bde57`'s message reports 4035 passed and a clean `infra-lint`; that holds only where the `deploy`
extra is installed into the main venv, not on the documented `make setup` path or on CI.

**What I did meanwhile.** Reproduced (1) locally with the identical error and read all three job logs
through the Actions API. Ran the full suite with `--ignore=tests/infra` to see what the collection
error was hiding; the result is in `reports/2026-09-22.md` (G-1). Did not touch either file. Separately,
three items in the same report need a human ruling rather than a branch fix: the frozen `train.py` edit
(G-2, which I would ratify), Phase 2's four `UseCaseConfig` fields above its block (G-3, same), and the
decision band — Phase 4a has 6 numbers left in 300–399 and 400+ belongs to the library (G-5).

**Resolved 2026-09-22 by phase-4a-aws.** Both causes were real, both were in this branch's files,
and the entry is right that `b5bde57`'s "4035 passed and a clean `infra-lint`" held only in a venv
that already had the `deploy` extra - which is the one place neither failure can be seen. Both are
now checked by reproducing CI's situation rather than by running in this venv.

1. **The conftest import.** `610640d` added the `importorskip` guard this entry proposed, and it is
   kept. Its skip reason pointed at the wrong fix - `pip install -e '.[dev,aws]'` - but aws-cdk-lib
   is in the `deploy` extra, so following it would leave the suite skipping; it now names
   `make infra-setup && make infra-test`. One limit, now written beside the guard: a conftest guard
   skips cleanly when pytest *discovers* the directory (`make test`), but if `pytest tests/infra` is
   named as an initial argument in a venv without aws-cdk the Skipped escapes and aborts the run.
   `make infra-test` only ever runs in `.venv-infra`, so no target hits that.
2. **`make infra-lint` and exit 127.** Reproduced by running the target with the main venv's path
   pointed at nothing (`make infra-lint BIN=/no/main/venv/bin`): Error 127, as on CI. `.venv-infra`
   now carries ruff and black itself and `infra-lint` uses `$(INFRA_BIN)`; the same command exits 0.
   The drift the old arrangement guarded against is guarded by a test instead: every pin in
   `infra/requirements.txt` must equal that tool's pin in pyproject's `dev` extra (mypy was already
   duplicated that way). A second test reads the five targets CI's `infra` job runs and fails if any
   uses the main venv's `$(BIN)`. Both were confirmed to fail on the old file and a drifted pin.

`make image-test` should be fixed by (1) as well, since the image has no aws-cdk, but it was not run
here: this container has no Docker daemon, so CI is the first place that is proven.

On G-5: DEC-358 was used for the paid-marker decision. Free numbers left in Phase 4a's band are
DEC-347, 359, 360, 362 and 363.

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

**Phase 4a's answer, 2026-09-22 — the finding stands and the trap is not yet armed.** Measured
rather than assumed, against this branch's merged tree:

* `.github/workflows/ci.yml` runs no credentials step at all. Its three jobs — `lint-test`, `infra`
  and `image` — install, lint, test, synthesise and build; none of them authenticates to AWS.
  `make infra-synth` and `make infra-nag` need node, not an account.
* `.github/workflows/deploy-dev.yml` is the one workflow that authenticates. It is
  `workflow_dispatch` only, it assumes a role by OIDC with no long-lived secret, and it runs
  `scripts/build_push_image.sh` and `cdk deploy`. It runs **no** pytest at all.
* `.github/workflows/nightly.yml` is the workflow that runs `make test-all`, and it has no
  credentials step.

So the two halves — the runner that runs the paid suite and the runner that holds credentials — are
different workflows on this branch, and Phase 4a's natural next step does not join them: a
deployment needs a role, not a test run. The exposure the entry describes is real and would arm the
moment anybody adds a credentials step to `nightly.yml`, which is precisely why the one-word fix is
worth making *before* that rather than after.

**Resolved 2026-09-22, by the repository owner's decision.** Asked directly whether to make the
one-word change in a Shared file, the owner said yes, so Phase 4a made it.

It went in the `Makefile`, not in `pyproject.toml`'s `addopts`, and that turned out to matter more
than the entry expected. pytest keeps only the **last** `-m`, so a filter in `addopts` is replaced
wholesale by any `-m` on the command line — and `make test` already passes `-m "not slow"`.
Measured: with `-m 'not bedrock'` in `addopts`, `pytest -m "not slow"` still collects all 7 tests in
`tests/integration/test_bedrock_smoke.py`. The `addopts` fix would have protected `make test-all`
and silently left `make test` exposed.

So the Makefile gains `PAID_MARKERS := not bedrock and not aws`, and both default targets carry it:
`make test` is `-m "not slow and $(PAID_MARKERS)"`, `make test-all` is `-m "$(PAID_MARKERS)"`.
`@aws` is excluded alongside `@bedrock` because `pyproject.toml` already declares it "never selected
by default"; no test carries it today, so that half is a promise kept rather than a behaviour change.
`pytest -m bedrock` and `pytest -m aws` still opt in, which is the point — spending money becomes
something a person asks for.

`tests/unit/test_container_files.py` now reads the Makefile, evaluates each default target's `-m`
against a `@bedrock` test and an `@aws` test, and fails if either is selected; a second test checks
the same expressions still select the free suites, so `-m "nothing"` cannot pass for safe. Reverting
`test-all` to bare `pytest` was confirmed to fail it. The two comments in `nightly.yml` that said
`test-all` had no marker filter, and the README line describing it, say what is now true.

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

### 2026-09-23 — integration → every branch: `phase-2-onboarding` and the trunk are one tree on `main`

**Answer.** `phase-2-onboarding` (`9a05d5f`) was merged into `claude/gracious-lovelace-c344tl`
(`73526b1`, Phase 3a + 4a + library) with a merge commit, and the result is `main`. What had to be
decided rather than merged:

- **One run lifecycle.** Phase 4a had moved `create_run`, the job specs and the job builders into
  `engine/runs.py` (DEC-327); Phase 2 had extended the old copies in `api/routes/runs.py`. The old
  copies are gone. A built dataset now satisfies `UploadInfo` (`_DatasetSource`), and its provenance
  travels as `DatasetLineage` into `create_run`, so a dataset run gets a job spec and can run on
  SageMaker like any upload. `RunRecord.upload_id` is null for such a run and `dataset_id`,
  `client_id` and `dataset_fingerprint` are set.
- **Reading a run's rows back.** The win-back and root-cause modules re-read a run's upload after the
  pipeline and assumed there always was one; they now ask `engine.onboarding.datasets.run_source_key`,
  which answers the dataset frame for a dataset run. The Phase 4a run index stores the dataset id in
  `upload_id` for such a run rather than needing a migration.
- **Routers.** The PHASE-2 block of `api/main.py` mounts `clients`, `sources`, `mappings` and
  `datasets`; no path collides with a trunk route.
- **Documents.** `docs/DATA_CONTRACT.md` had two §8s: schema memory stays §8, generative is §9, raw
  tables are §10 (citations updated). Decision numbers are now allocated by hundreds per workstream,
  in the table in `PARALLEL_WORK_PROTOCOL.md` §4.

**Still open after the merge**, each with its own entry above: the onboarding panel is not mounted
(M13), `StageContext.primary_key` (composite keys are still refused with 501), the phase plans, and
the library-datasets requests. The GitHub default branch is still `claude/gracious-noether-y0njma`,
so `nightly.yml` is not scheduled until the repository owner points it at `main`.
