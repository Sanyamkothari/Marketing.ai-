# Guided setup: the use-case helpers (Plan G)

**Owner:** Plan G (`engine/agent/`, `api/routes/agent.py`, `api/routes/agent_recipes.py`,
`configs/prompts/data_agent*`, `tests/**/agent/**`, `tests/fixtures/agent_bench/**`).
**Decisions:** DEC-1000 … DEC-1018 in `docs/DECISIONS.md`. **Plan:** `docs/plans/MARKETING_AI_PLAN_G_AGENTS.md`.

---

## 1. In plain words

Every predictive use case has a **helper** that gets a client's file ready to train on and picks the
run's settings. It works like a careful colleague:

1. You upload a file as usual.
2. The helper **looks at it** and makes a short list of **suggestions**, each with a plain reason and
   the numbers behind it: "Turn `monthly_spend` into numbers - 2,998 of 3,000 values convert",
   "Hide `refund_date`? It may contain the answer", "Use `signup_date` to test on the newest rows".
3. When it cannot decide alone it **asks** ("In `joined`, which comes first: the day or the
   month?"). When the data cannot work at all (too few rows, no outcome column, an ID that
   repeats) it **stops** and says what data would fix it.
4. You **tick or untick** each suggestion, answer the questions, and can **chat** with it ("why hide
   notes?", "make training faster").
5. You click **Approve**. Only then is anything prepared: the helper's fixes run on a **copy** of your
   file, never on the file itself. You get back exactly what the Run button needs.
6. The fixes are saved with the model as a **recipe**, so next month's file is prepared the same
   way before it is scored. If a column is missing or a format changed, scoring stops and names the
   problem instead of guessing.

Three promises hold throughout:

- **Nothing changes until you approve.** Suggestions are only suggestions.
- **The helper never edits a file and never writes code.** It proposes steps from a short fixed
  list; our tested code runs them.
- **It works without an AI service.** The suggestions, questions and settings come from rules in our
  code. The AI service only adds the chat. With none connected, the chat answers in practice mode
  (`chat.backend = "fake"`) and everything else works the same.

## 2. How it works, end to end

```
upload ──► POST /uploads/{id}/agent-session          advisor (rules only) ──► session file
           ├─ GET  …/agent-session                    read it back (Viewer)
           ├─ POST …/decisions   accept / reject / edit a setting's value
           ├─ POST …/answers     answer a question (a role answer re-runs the advisor)
           ├─ POST …/messages    one chat turn (JSON-action loop over the LLM, metered)
           ├─ POST …/preview     before/after on the first 1,000 rows
           └─ POST …/apply       recipe on every row ─► derived upload ─► Run's own checks
                                                      └─► {upload_id, primary_key, target, overrides, …}
POST /runs (unchanged) ──► run.json … model version; the recipe is copied to runs/<run_id>/data_recipe.json
scoring: POST /runs mode=score ──► replay the model's recipe ──► validate_against_schema ──► score
```

### 2.1 The pieces

| Module | Does |
|---|---|
| `engine/agent/config.py` | `AgentConfig`, the `agent:` block of a use case (§6). |
| `engine/agent/contracts.py` | `AgentSession`, `Proposal`, `Question`, `ToolResult`, `ChatMessage` + `TurnLog`, `DataRecipe`, `RecipeStep`, `RecipeReceipt`. All frozen, `extra="forbid"`. |
| `engine/agent/tools.py` | The read tools and `call_tool`, which validates arguments and records a `ToolResult` with an `evidence_id`. |
| `engine/agent/formats.py` | The messy-format detector and the parsers the recipe runs (they share code, so they cannot disagree). |
| `engine/agent/recipe.py` | `check_recipe` and `run_recipe`: the only code that changes data. |
| `engine/agent/reshape.py` | Level 3 (M76): plan and run a `combine_rows` step through the onboarding feature engine. |
| `engine/agent/advisor.py` | Every proposal, question and stop, by rules (§4). |
| `engine/agent/recommend.py` | The recommended settings (§5). |
| `engine/agent/session.py` | Deciding, answering, re-advising; the session status rule. |
| `engine/agent/loop.py`, `grounding.py`, `untrusted.py` | The chat turn and its safety checks (§7). |
| `engine/agent/checks.py` | `check_plan`: exactly what `POST /runs` validates, with nothing started. |
| `api/routes/agent.py` | The HTTP routes (§8). |
| `api/routes/agent_recipes.py` | Derived uploads, attaching a recipe to a run, replaying it for scoring. |
| `configs/prompts/data_agent.v1.md` | The chat prompt. |
| `ui/modules/agent/` | The *Guided setup* tab (M75, DEC-1019 … DEC-1022), registered through `router.js`'s `registerSetupMode`; Manual setup stays the default tab for now. |

### 2.2 A session

One session per upload, stored at `uploads/<upload_id>/agent/agent_session.json` (DEC-1007), so
upload retention and erasure remove it with the upload. It holds the evidence (tool results, with
sample values masked), the proposals and questions, the summary, the masked chat transcript and
counters (`rounds`, `llm_calls`). Its `status` is:

| Status | Means |
|---|---|
| `needs_review` | Something is pending: a proposal not yet accepted or rejected, or a blocking question. |
| `ready` | Everything is decided; Approve is allowed. |
| `stopped` | The data cannot work as it is; `stop_reason` says why and what would fix it. |
| `applied` | Approve ran; `applied_upload_id` names the prepared upload. Start again to change anything. |

Every `Proposal` cites at least one `ToolResult` of the same session (the contract refuses one that
does not), so any number on the screen traces back to the tool call that measured it (DEC-1010).
Only the person moves a proposal out of `pending`.

## 3. Tools

Every tool is **read-only**; nothing the helper (or the model) calls can write data. The only writer
is `apply`.

| Tool | Arguments | Returns |
|---|---|---|
| `get_profile` | `offset` (default 0) | Rows, and up to 50 columns per call (`columns_total`, `columns_offset` page through wider files): type, empty share, distinct count, unique/constant/ID-like/date-like, personal-data kinds; key and date candidates. |
| `inspect_column` | `column` | Counts, masked examples and top values, and the outcome rate per bucket of the column. A personal-data column shows no values at all - not even its minimum or maximum. |
| `find_format_issues` | `columns` (optional) | Per text column, at most one issue: `number_as_text`, `mixed_dates`, `boolean_as_text`, `category_variants` or `untrimmed_text`, with counts, masked examples and the parameters a fix would use. Personal-data columns are skipped. |
| `describe_outcome` | `column` | How many rows are "yes" by the engine's own label rule. |
| `describe_repeats` | `column` | How often each value of an ID column repeats: IDs, rows per ID, the most rows, IDs on several rows (the evidence behind "combine the rows?"). |
| `find_roles` | — | Which columns could be the ID, the outcome (exact, case-insensitive, synonym), the date, consent, opt-out and last contact. |
| `check_data` | `primary_key`, `target`, `overrides` | Every check the Run button would run (`check_plan`), with codes, severities, messages and fixes. |

A column name may be given exactly or as it was shown (cleaned and cut to 64 characters, §7.4);
anything else is `AGENT_COLUMN_UNKNOWN`. Bad arguments are `AGENT_TOOL_ARGS_INVALID`.

## 4. The advisor's rules

`engine/agent/advisor.advise` calls the tools above (so every number it uses is evidence) and
applies fixed rules (DEC-1015). The same file always gets the same advice;
`tests/fixtures/agent_bench/expected.json` pins it for nineteen files.

**Roles.**
- The ID: the first primary-key candidate, `sure` when its name is one of the use case's
  `primary_key_hints` or it is the only unique column. With no unique column, an ID-named column
  that repeats or has blanks **stops** with the Run button's own `PK_NOT_UNIQUE` / `PK_NULLS`
  message.
- The outcome (training only): `sure` when a column is named exactly `target.column`
  (case-insensitive); `check` for a single synonym from `agent.column_hints.target_synonyms`; a
  **question** over the synonyms, or over the two-valued columns, otherwise; a **stop** when there is
  no candidate at all.

**Fixes** (training only; one recipe proposal per format issue). The ID, the outcome and the use
case's consent, opt-out and last-contact columns are never given a fix.

| Issue | Proposal | Confidence / question |
|---|---|---|
| `number_as_text` | `parse_number` | `sure` when ≥ 95% convert, else `check`. More unreadable values than `max_conversion_failure_pct` → a non-blocking question: hide it, or leave it as text. |
| `mixed_dates` | `parse_date` | `sure` when the values prove the day/month order; otherwise a **blocking** question "day first or month first?". |
| `boolean_as_text` | `map_boolean` | `sure`. |
| `category_variants` / `untrimmed_text` | `normalise_text` | `sure`. |

**Checks on the prepared data.** The Run button's checks run on the file *with the ticked fixes
applied* (so numbers stored as text are not mistaken for an ID):
- `LEAKAGE_SUSPECTED` (error) → a blocking question: hide it (a `drop_column` step) or keep it (an
  acknowledgement).
- `PK_*`, `TARGET_*`, `ROWS_TOO_FEW`, `CONSENT_COLUMN_MISSING`, `SCHEMA_MISMATCH` (errors) → **stop**,
  with the check's own message and suggestion (`STOP_CODES`).
- Warnings the engine already acts on (`HIGH_NULL_COLUMN`, `CONSTANT_COLUMN`,
  `HIGH_CARDINALITY_ID_LIKE`, `PII_DETECTED`) → listed under *hidden columns*, one line per column,
  not proposed. `SUPPRESSION_COLUMN_MISSING` → an assumption.

**Many rows per entity** (level 3, M76, DEC-1026). With `reshape` in `agent.levels`, a training file
whose hinted ID column repeats and has a usable date column gets a blocking question "Each {entity}
appears on N rows on average. Combine them into one row per {entity}?" (Combine (recommended) /
Stop), with N from `describe_repeats`. It is asked once the outcome column is known; until then no
checks or settings run. Combine re-advises on the combined preview; Stop, no `reshape` level or no
date column keep the `PK_NOT_UNIQUE`-style stop.

**Scoring files** get no fixes of their own: the model's saved recipe prepares them (§6.4). If the
file does not fit that recipe, the session stops and names the problem.

## 5. Recommended settings ("Auto")

`engine/agent/recommend.recommend_settings` (DEC-1016). Each rule follows from one measured fact:

| Fact | Suggestion | Confidence |
|---|---|---|
| The use case splits by date but its date column is absent, and the file has a date column (≥ 3 values) | `split.time_column` = that column | `sure` |
| … and the file has no date column | `split.type: random_stratified` | `sure` |
| The use case splits at random and the file has a date column | `split.type: time_based` + `split.time_column` | `check` |
| Fewer than 5% of rows are "yes" and the metric is ROC-AUC | `model_search.metric: pr_auc` | `sure` |
| Fewer than 5,000 rows | `model_search.time_limit_minutes: 10` | `sure` |
| More than 500,000 rows | `model_search.time_limit_minutes: 60` | `sure` |
| A consent-looking column not used for suppression | `governance.consent_column` | `check` |

Imbalance is not proposed: `model_search.imbalance: auto` already weights rare outcomes; below 10%
"yes" this is stated as an assumption. Every suggestion must be a field of
`advanced_settings_schema()` (or an extra overridable path), inside its choices or range, never
advisory, never in `NEVER_RECOMMENDED` (`validation.min_positive`, `validation.min_rows`,
`validation.leakage_check`, `governance.approval_required`, `validation.acknowledged`), and the whole
set must resolve with `resolve_config`. A setting the rules say nothing about keeps the use case's
default. A person may edit a suggested setting only to another value the same check allows.

## 6. The recipe

### 6.1 Step kinds

A `DataRecipe` is an ordered list of `RecipeStep`s. Every step is **stateless**: a row-wise step's
output for a row depends only on that row and the step's parameters (DEC-1004), and `combine_rows`'s
output for an entity only on that entity's rows (DEC-1023). Nothing is learnt from the whole file, so
running a recipe before the train/test split leaks nothing and next month's file is prepared
identically.

| Kind | Parameters | Does |
|---|---|---|
| `parse_number` | `decimal` (`.` or `,`), `percent_to_fraction` | `"₹1,200"`, `"Rs. 1,20,000"`, `"45%"` (→ 0.45), `"(300)"` (→ −300), `"1.200,50"` → number. |
| `parse_date` | `dayfirst` (must be decided) | Mixed styles → datetime, each value parsed on its own; a value with a UTC offset keeps its clock time. |
| `map_boolean` | `true_values`, `false_values` | Listed spellings (compared trimmed, case-folded) → 1 / 0; anything else fails. |
| `normalise_text` | `strip`, `merge` (frozen at approval) | Trim; replace listed spellings by their canonical one. |
| `combine_rows` | the step's `column` is the entity key; `time_column`, `snapshot_column`, `dayfirst`, `outcome`, `features` (a frozen `{name, function, column}` list) | Level 3: many rows per entity into one (DEC-1023 … DEC-1025), computed by the onboarding feature engine with its point-in-time guard. Only rows dated on or before the entity's snapshot count; the outcome is read from the latest row, not aggregated; numbers get sum/mean/max/latest, categories latest/nunique, dates days-since, plus a row count. Entity-wise rather than row-wise: an entity's output depends only on its own rows and the frozen parameters. A training Approve runs onboarding's full future-data leak probe, a scoring replay the narrow one, a preview none. Needs the `reshape` level. |
| `derive` | `expression` | A new column from `engine.onboarding.transforms.derive`'s whitelist: names, numbers, `+ - * /`, `days_between`, `months_between`, `year`, `month`, `coalesce`, `lower`, `abs`, and `snapshot_date`. At most 300 characters; no repeated text. Needs the `derive` level. Not proposed by the advisor today. |
| `drop_column` | — | Hide a column from the model. |

`recipe_hash` covers kinds, columns and parameters only (not reasons or ids). A recipe also records
what it was approved under - `primary_key`, `target`, `snapshot_column`, and the use case's `levels`
and `max_conversion_failure_pct` at that moment (`levels`, `max_failure_pct`; null on a recipe saved
before they were recorded, which then runs under the current config). Every replay runs under the
recorded levels and limit, so a use case that later drops a level does not refuse the scoring files of
a model already trained with it: narrowing `agent.levels` or the limit affects new approvals only,
not the models already trained. Two recipes prepare a file alike (`DataRecipe.prepares_like`) only
when the hash *and* all of these match.

### 6.2 Rules `check_recipe` enforces

Steps are numbered 1…n and run in a fixed order: parse and tidy → combine → derive → drop. Refused with
`RECIPE_STEP_INVALID`: an unknown parameter, an undecided date order, a spelling that means both yes
and no, a step above the levels it runs under (the use case's at Approve, the recipe's recorded
ones on a replay), a step that changes the ID or the outcome, a derived
column that reads the outcome or `snapshot_date` without a snapshot column, or a name clash. A
column the file lacks is `RECIPE_COLUMN_MISSING`. `run_recipe` works on a copy and counts, per step,
the values it changed and could not convert (examples masked); more failures than
`max_conversion_failure_pct` of a column's non-empty values stop the run with
`RECIPE_VALUES_UNCONVERTED`.

### 6.3 Where a recipe runs

- **Preview** (`…/preview`): on the first 1,000 rows - or, when the recipe combines rows, on every
  row of the first 1,000 entities, so no entity is combined from part of its rows - returning 5 rows before and after (personal
  data shown as `[personal data]`, other cells masked) and the receipt of those rows.
- **Approve** (`…/apply`): on every row, into a **derived upload** `uploads/<new_id>/` with
  `source.parquet` (types kept), `profile.json`, `upload.json` (`"<file> (prepared)"`),
  `data_recipe.json` and `recipe_receipt.json`, whose `upload_id` names the file it was prepared from
  (DEC-1014). The original is untouched. A column whose type changes between the 100,000-row read
  chunks (numeric IDs, then `C-123`) is written as text so the file can be saved: the text of what
  the chunked read gave, so a chunk read as numbers has already lost any leading zeros (`0000123` is
  `123`), as the same file read without a recipe has - not the cells as written. A prepared file that
  still cannot be saved is `RECIPE_STEP_INVALID`, never a 500. Guided setup is refused on a
  prepared *training* upload (409 `AGENT_UPLOAD_PREPARED`): a session saves only its own steps, so the
  model would carry part of the preparation.
- **Training** (`POST /runs` from a derived upload): the recipe is copied to
  `runs/<run_id>/data_recipe.json`; the model version finds it through its `run_id` (DEC-1013). The
  run must name the recipe's ID column and outcome (409 `RECIPE_ROLES_MISMATCH` otherwise): every
  safety rule of the recipe was checked against those. A scoring run must name the model recipe's ID
  column, when it named one. `agent_recipes.refuse_other_roles` is the one rule: `POST /runs`, the
  dry run and a scoring session's Approve all apply it before anything is prepared, so they give the
  same answer and a refused scoring run leaves no prepared upload behind.
- **Scoring**: `replay_for_scoring` runs the model's recipe on the scoring upload before
  `validate_against_schema`. `scoring_source` decides which file that is, for `POST /runs`, the dry
  run and Guided setup alike: an upload already prepared by a recipe that `prepares_like` the model's
  is read as it is; an upload prepared by any *other* recipe - or scored by a model that has no recipe
  - goes back to the file it came from (named by its receipt), never prepared on top of another
  recipe's output nor scored prepared by a model trained on files as sent. Guided setup's Approve on
  a scoring file does the same replay, so the upload it returns is the one Run reuses.
- **Dry run** (`POST /uploads/{id}/checks`, scoring file): the same source and the same recipe on
  every row, in memory - nothing is written - so it agrees with `POST /runs` (DEC-1011); other roles
  than the recipe's are the same 409 `RECIPE_ROLES_MISMATCH`.
- **Built datasets**: a dataset cannot be prepared by a recipe yet, so scoring one with a model that
  has a recipe is refused - `POST /runs` with `dataset_id` answers 409 `RECIPE_DATASET_UNSUPPORTED`,
  and a scheduled score firing fails with the same code - rather than scored unprepared.

## 7. Chat safety

The chat is the only place a language model is involved (DEC-1001, DEC-1017).

### 7.1 The JSON-action loop

`engine/agent/loop.chat_turn` renders `configs/prompts/data_agent.v1.md` and calls
`Meter.complete(…, GenerativePurpose.DATA_AGENT)`. The model must answer with one JSON object:

- `{"action": "<read tool>", "args": {…}}` - the engine runs the tool and feeds the result back;
- `{"action": "propose_setting", "args": {"path", "value", "reason"}}` - a **pending**, `check`
  setting proposal, allowed only if `setting_allowed` and `resolve_config` accept it and the path is
  not `prepare.exclude_columns` (hiding a column is a recipe step, never a chat setting);
- `{"action": "reply", "text": …, "evidence_ids": […]}` - ends the turn.

A malformed reply, an unknown or writing tool, or bad arguments are fed back once; a second failure
ends the turn plainly. At most `max_tool_steps_per_turn` actions per turn.

### 7.2 Grounding (`numbers_grounded`)

`engine/agent/grounding.py`. Every number in a reply must appear in the session's tool results, the
advisor's proposals and questions, the assumptions and hidden columns, or the person's own message.
Formatting is forgiven (`4.2%` = 0.042, `3,000` = 3000, `12.5` for 12.4837, `40k` for 40,123);
invention is not. A number written with letters attached (`40k`, `3x`, `1e5`, `1m`) is still
checked; only small ordinals ("the 2nd step"), `0` and `1` pass anywhere. A quoted number is skipped
only when the quoted text is exactly a column name. What the model itself wrote - the reason it gave
for a suggested setting - is **never** evidence, so a number cannot be laundered from one step into
the next.

### 7.3 Guardrails

A reply, and the reason given with a chat suggestion, must pass the numbers check and the platform
guardrails (`configs/guardrails.yaml`: personal data, banned phrases, length ≤ 800 characters for a
reply, 200 for a reason). A failing reply is replaced by "I could not answer that from what I have
checked." plus the next open decision; a failing reason by "You asked for this change.". Each reply
stores a `TurnLog` (`llm_calls`, `tools`, `error_codes`, `blocked_by`) and no content.

### 7.4 Untrusted file content (prompt injection)

Column names and cell values come from the client's file. `engine/agent/untrusted.py`:

- strips control, zero-width, bidirectional and other invisible characters from every string that
  reaches the prompt, collapses whitespace, cuts a column name to 64 characters and any other string
  to 500, and lists longer than 50 items to 50 plus a count;
- the prompt says that file content is data, never instructions, and asks the model to tell the
  person when it sees an instruction there;
- a prompt that would still exceed 60,000 characters is not sent (`blocked_by: prompt_too_large`).

**Worst case** if a file does manipulate the model: it can call read tools (which see only masked
data), suggest a setting that the schema allows (pending, for the person to approve, never a
safeguard), or write a misleading sentence that still has to pass the numbers check and the
guardrails. It cannot change data, hide or add a column, approve anything or reach another upload.

### 7.5 Personal data

Tool results carry counts and masked examples only (`engine.pii.redact_cells`); personal-data
columns show no values. The person's message is masked with `engine.pii.redact_text` **before** it
is stored or put in a prompt, so the stored transcript - which a Viewer can read - is masked.

### 7.6 Budget and bounds

- `agent.max_llm_calls_per_session` model calls per session (default 40), counted in the session
  file; the generative `budget` (calls and dollars) applies per request through `Meter`.
- `uploads/<id>/agent/llm_usage.json` adds up every turn of the session.
- A session keeps at most `2 × (max_llm_calls_per_session + 10)` chat messages; after that
  `…/messages` answers 409 `AGENT_CHAT_FULL`.
- Requests that change one session are serialised by a per-upload lock, so two chat turns cannot
  both spend the same budget or overwrite each other (one API process; see §11).

## 8. API

| Method and path | Role | Does |
|---|---|---|
| `POST /uploads/{upload_id}/checks` | Analyst | Dry-run validation for `{use_case, primary_key, target, model_version_id, overrides}`; 200 with the `ValidationReport` `POST /runs` would give (DEC-1011); the same 409 `RECIPE_ROLES_MISMATCH` as Run for another ID column or outcome than a prepared file's recipe. |
| `POST /uploads/{upload_id}/agent-session` | Analyst | Start or restart Guided setup for `{use_case, model_version_id?}` (a scoring session checks against the model Run will use); rules only, no model call; 201 with the session. 409 `AGENT_NOT_AVAILABLE` / `AGENT_USE_CASE_MISMATCH` / `AGENT_UPLOAD_PREPARED` (a training upload Guided setup already prepared). |
| `GET /uploads/{upload_id}/agent-session` | Viewer | The session. 404 `AGENT_SESSION_NOT_FOUND`. |
| `POST …/agent-session/decisions` | Analyst | `{decisions: [{proposal_id, state, value?}], accept_recommended}`. 422 `AGENT_EDIT_NOT_ALLOWED` / `AGENT_VALUE_NOT_ALLOWED`, 404 unknown id, 409 `AGENT_SESSION_APPLIED`. |
| `POST …/agent-session/answers` | Analyst | `{question_id, option_id}`; a role answer re-runs the advisor, keeping decisions already made. |
| `POST …/agent-session/messages` | Analyst | `{text}` (≤ 1,000 characters): one chat turn. 409 `AGENT_SESSION_APPLIED` / `AGENT_CHAT_FULL`. |
| `POST …/agent-session/preview` | Analyst | Before/after rows and the receipt on the first 1,000 rows (or entities). 409 with a `RECIPE_*` code when a step cannot run. |
| `POST …/agent-session/apply` | Analyst | Approve: 409 `AGENT_SESSION_STOPPED` / `AGENT_SESSION_APPLIED` / `AGENT_UNDECIDED`; 409 `VALIDATION_FAILED` with the checks when Run would fail; else `{upload_id, mode, primary_key, target, overrides, summary, receipt}` for `POST /runs`. Audited with the prepared upload and the recipe hash. |

Every route has a `RoutePolicy`; a Viewer can read a session but cannot start one, decide, answer,
chat, preview or approve.

## 9. How to change the helper

### 9.1 Tune a use case (`agent:` block)

Defaults are `defaults.agent` in `configs/engine.yaml`; a use-case YAML overrides only what differs.
Unknown keys are refused; the block is not overridable per run and is outside `recipe_hash`.

```yaml
agent:
  enabled: true                       # false hides Guided setup for this use case
  display_name: null                  # null = "<use case name> helper"
  goal: "Find which customers are likely to buy within 30 days of seeing an ad."
  knowledge:                          # <= 20 lines, <= 300 characters each; shown to the model
    - "The outcome is a purchase within 30 days of ad exposure."
  column_hints:
    target_synonyms: [converted, purchased, bought]
    consent: [marketing_opt_in, opt_in, consent, consented]
    opt_out: [opt_out, opted_out, unsubscribed, do_not_contact]
    recently_contacted: [last_contacted_at, last_contacted, last_contact_date]
  levels: [clean, derive]             # must include clean; add reshape for level 3 (combine_rows)
  max_conversion_failure_pct: 5.0     # 0..50
  tick_uncertain: false               # whether "check" suggestions start ticked
  max_llm_calls_per_session: 40       # 1..500
  max_tool_steps_per_turn: 8          # 1..20
```

After a change run `make test` (config validation) and the benchmark (§9.4) if it changes advice.

### 9.2 Add or change a rule

- A data-fix, question or stop rule lives in `engine/agent/advisor.py` (`_format_proposals`,
  `_leak_questions`, `STOP_CODES`, `_ENGINE_HIDES`). A settings rule is a function in
  `engine/agent/recommend.py` added to the list in `recommend_settings`.
- Base it on a tool result (call it through `_Builder.call` so it is evidence) and cite that
  result's `evidence_id`. Write the title and reason in plain words; show column names with
  `display_name`.
- Never branch on a use-case id: read the use case's config instead (DEC-1003).
- A settings rule must pass `setting_allowed`; `test_no_use_case_is_ever_offered_an_unsafe_setting`
  walks every trainable use case.
- Update the golden file (§9.4) and review its diff.

### 9.3 Add a recipe step kind

1. Add the member to `RecipeStepKind` (`engine/agent/contracts.py`); regenerate `docs/API.md`
   (`python -m scripts.gen_api_docs`).
2. In `engine/agent/recipe.py`: its phase in `STEP_PHASE` (parse 1, combine 2, derive 3, drop 4), its level in `_LEVEL_OF` if it is not
   `clean`, its parameters in `_check_params`, and its runner in `_apply`.
3. Write the parser in `engine/agent/formats.py` (row-wise and stateless, computed once per distinct
   value, returning a `ParseOutcome` that counts failures), and a detector if the advisor should
   propose it.
4. Tests: a row-wise test and an equivalence test in `tests/unit/agent/test_formats.py`, recipe
   tests in `test_recipe.py`, and the replay test in `tests/integration/agent/test_recipe_runs.py`.
5. Anything that learns a statistic from the file does not belong in a recipe; it belongs in
   `prepare`, fitted on training rows only (DEC-046).

### 9.4 Update the benchmark golden file

`tests/fixtures/agent_bench/cases.py` builds nineteen files (the messy generator
`make_messy.py`, the broken fixtures, ambiguous dates, unreadable numbers, the multi-row order logs
of `make_multirow.py` …) and `expected.json`
holds a digest of the advice for each. After a deliberate rule change:

```
python -m tests.fixtures.agent_bench.cases --update
git diff tests/fixtures/agent_bench/expected.json   # the diff is the review
```

`tests/unit/agent/test_advisor.py::test_the_advisor_matches_the_benchmark` fails on any other change.

### 9.5 Change the prompt

Edit `configs/prompts/data_agent.v1.md` (or add `data_agent.v2.md`). Keep "one JSON object and
nothing else", "never invent a number" and the file-content rule; `FakeLLMClient` recognises the
prompt by `HELPER TURN` and parses its state and tool-result sections, so keep those headings.

## 10. Performance

See `reports/plan_g_performance.md`. On a 1,000,000-row messy file: the format detector 25 s, the
whole advisor 110 s (mostly the engine's own profiling and checks), the six fixes on every row 4 s;
on a 1,000,000-row order log, combining with the full leak check 6 s. The parsers run once per
distinct value; the preview runs on 1,000 rows (or entities). Reproduce with
`python -m tests.fixtures.agent_bench.perf --rows 1000000`.

## 11. Known limitations

- **One API process.** The session lock is a thread lock; several API processes writing one session
  need a store-level lock. The per-session call budget is counted in the session file; the dollar
  budget is per request (`generative.budget.max_cost_usd_per_run`), not per session.
- **Every session request re-reads the upload** (up to the profile cap), so on a very large file each
  decision takes as long as reading it; answering a role question re-runs the advisor.
- **Numbers in words** ("seventy percent") are not checked by `numbers_grounded`; the guardrails and
  the prompt are the only defence there.
- **Column names are shown as they are** after cleaning and cutting; a header that is itself
  personal data (an email address as a column name) is masked only where the guardrails catch it.
- **`derive` steps** are supported by the recipe engine but not proposed by the advisor or the chat.
- **The combine leak probe** is onboarding's `_run_leak_probe`, which opens its own DuckDB connection;
  Plan G makes it single-threaded by swapping `duckdb.connect` for the duration of the probe
  (`reshape._single_threaded_duckdb`). The lasting fix is for the probe to accept a connection.
- **A combine preview** is computed on the first 1,000 entities and without the leak probe; the numbers
  it shows are for those entities only.
- `make agent-eval` (the benchmark on Bedrock) is not built yet; Guided setup is not yet the default
  tab (DEC-1019).
- Chat is English only.
