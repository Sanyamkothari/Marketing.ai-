# Marketing AI — Plan G: Use-case agents (Guided setup)

**Companion to:** `plan.md` (Phase 1), Plan A (onboarding), `docs/GENERATIVE.md`, `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Branch:** a new branch from `main` when the build starts
**Decision range:** DEC-1000 … 1099 (the next free hundred; the first milestone claims it in the protocol table)
**Milestones:** M70 – M77 (M65–M69 left free for Plan F, whose numbering is not in the repository)
**Status:** proposed, waiting for approval

> **How to use this document.** Read it with `plan.md`. This plan adds one capability: an AI agent on every
> predictive use case that prepares the user's data and recommends the run's settings, and asks for approval
> before anything is applied. It adds **no new modelling**. Every data check, preparation step, setting and
> training path it uses already exists. Where this document is silent, follow `plan.md`, `docs/GENERATIVE.md`
> and the protocol. Where it is impossible or clearly wrong, stop, explain, and propose the smallest change.

---

## 1. Why

The platform works, but a new user must understand primary keys, targets, leakage, PII, split types, metrics,
imbalance handling and eight stages of advanced settings before a first run. The hardest part is data
preparation: a real client file has `"₹1,200"` in a number column, three date styles, `Y/yes/TRUE` in one flag,
personal data, an ID column and sometimes a column that contains the answer.

**Goal in one sentence:** a non-technical user picks a use case, opens *Guided setup*, uploads any reasonable
file, reads a short list of suggested fixes and recommended settings with a plain reason for each, answers at
most a few questions, clicks **Approve**, then **Run**, and gets a trained model. Next month the same fixes are
applied to the new file automatically.

## 2. In plain words (for the product owner)

- **The first screen does not change.** The lifecycle overview stays exactly as it is.
- **Every use case gets its own agent.** It knows that use case's goal, what data it needs and which settings
  suit it. Behind the scenes this is **one agent engine** configured per use case in its YAML file, so a new use
  case gets its agent by adding config, not code.
- **The second screen gets two tabs:** *Guided setup (recommended)* and *Manual setup* (today's screen,
  unchanged).
- **The agent suggests, the user approves.** Nothing changes until the user clicks Approve.
- **The agent never edits the file.** It writes a **recipe** (a list of steps). Our tested code runs the recipe
  on a copy, which becomes a new prepared file. The original is never changed.
- **The recipe is saved with the model**, so scoring next month's file runs the same steps. If a column is
  missing or a format changed, it stops and asks. It never guesses silently.
- **Settings default to Auto (recommended).** The agent picks every advanced setting for this data and explains
  each one. The user can change any of them, or type a request like "make training faster".
- **It works without an AI service.** The fixes and recommendations come from rules in our code. The AI adds the
  chat, the explanations and help with unclear cases. With no AI service connected, the checklist still works
  and the chat is switched off with a notice.

## 3. What exists today and what is missing

Findings from reading `main` @ `a49afed`. The implementing agent must re-check each before relying on it.

| Need | Exists | Gap this plan fills |
|---|---|---|
| Understand the data | `ingest.profile_dataset` → `DatasetProfile` / `ColumnProfile` (types, nulls, samples, PK and time candidates, `pii_kinds`, `looks_like_id`) | Target is never guessed from data; no detector for messy formats |
| Check the data | `validate.validate_for_training(frame, config, *, primary_key, target, acknowledged, upload_id, …)` runs as a library; findings carry `details.override_path` / `override_value` for fixes | No dry-run endpoint; validation only runs inside `POST /runs` |
| Change settings safely | `resolve_config(use_case, overrides)` refuses unknown, immutable and invalid paths with a code; `overridable_paths()`; `advanced_settings_schema()` (8 stages, `FieldSpec` with choices, help, `advisory`) | No recommender; no plain-language reason per setting |
| Clean values | `engine/onboarding/transforms.py`: `cast` (numeric/boolean/date/categorical/text, explicit `date_format`), `value_map`, `strip`, `lower`, `lstrip_zeros`, `scale`, `negate`, `derive` (AST whitelist) | Only reachable from the raw-tables flow; **no currency, percent or thousands-separator parsing anywhere** |
| Replay on scoring | `prepare.replay(df, prepare.json)` for Phase 1's fixed transforms; `schema.json` + `SCHEMA_MISMATCH` | **Any cleaning or derived column added before training is not re-applied at score time.** Scoring would fail `SCHEMA_MISMATCH`. Closing this is mandatory (§6.4). |
| Reshape many rows → one | Onboarding feature engine (`FeatureDef`, `AggFunction`, DuckDB, point-in-time guard, leak check) | Not offered for a single uploaded file |
| Talk to an LLM | `LLMClient.complete(prompt, *, system, …) -> LLMCompletion` (single prompt → text; **no tool use, by design, DEC-076**); `Meter` (budget, cache); `Guardrails`; versioned prompts in `configs/prompts/`; `FakeLLMClient` modes | No agent loop; no numeric-grounding check on generated text |
| Approve before apply | Win-back copy `CopyStatus` pending_review → approved (DEC-205); the 409 list's fix controls and `applyFix()` in `ui/usecase.js` | No proposal/approval model for data fixes and settings |
| Audit | `set_audit_context(...)` per request; closed `DETAIL_KEYS` list | New detail keys needed |

## 4. Rulings this plan assumes (record as DEC entries in M70)

| # | Decision | Ruling assumed | If overruled |
|---|---|---|---|
| G1 | How the agent calls tools | **A JSON-action loop over the existing `LLMClient.complete()`**: the model replies `{"action": "<tool>", "args": {…}}` or `{"action": "reply", …}`; the engine validates and runs the tool and feeds the result back. DEC-076 stands: no provider tool-use shape enters the engine. A native `converse(messages, tools)` may come later behind the same protocol. | Extend `LLMClient` with a tool-use method under a new DEC |
| G2 | Who decides | **Rules first, LLM second.** A deterministic advisor produces every proposal, reason and question from tool results. The LLM chooses among options the advisor offers, explains, and maps chat requests to allowed paths. Every LLM proposal is re-validated by code. | — |
| G3 | One agent or many | **One engine, one configuration per use case** (`agent:` block in the use-case YAML, defaults in `engine.yaml`). No branching on use-case id (plan §2.1 rule 1). | — |
| G4 | Recipe steps | **Stateless and row-wise only.** Parsing, mapping, trimming, deriving and dropping. Nothing that learns a statistic; filling, clipping and scaling stay in `prepare` (fitted on train only, DEC-046). A value map is frozen at approval time. | — |
| G5 | How the prepared data reaches training | The recipe runs on a copy and writes a **derived upload** (`uploads/<new_id>/`) with lineage to the original. `POST /runs` is called with the derived `upload_id`; the run pipeline is unchanged. | Apply the recipe inside the train pipeline instead |
| G6 | Replay at score time | The recipe is **stored with the model version** (`data_recipe.json` beside `schema.json`) and applied to a scoring upload **before** `validate_against_schema`. | — |
| G7 | Sessions | One agent session per upload, stored under `uploads/<id>/agent/`, deleted with the upload and covered by retention and erasure. No memory across sessions except the approved recipe (`docs/GENERATIVE.md` §10). | — |
| G8 | Levels of change allowed | v1: level 1 (clean columns), level 2 (derive columns), level 3 (one file, many rows per entity → one row, M76). Later: level 4 (several files; hand off to the raw-tables flow) and level 5 (create the target from events). **Never** level 6 (agent-written code). | — |
| G9 | Naming | UI: **Guided setup** tab; agent: "<use case> helper". Code: `engine/agent/`, `api/routes/agent.py`, `ui/modules/agent/`. Do not reuse "AI Assistant": the `ai-assistant` screen is the Phase 3a document Q&A. | — |
| G10 | Scope of use cases | Predictive and hybrid use cases (non-empty `advanced_settings.stages`). Generative use cases show no tab. | — |

## 5. The user experience

### 5.1 Use-case page (second screen)

```
[ Guided setup (recommended) ]  [ Manual setup ]

Step 1  Upload           ── any CSV/Parquet; "Download template" as today
Step 2  Data fixes       ── checklist of suggested changes, questions
Step 3  Settings         ── "Auto (recommended) – 8 settings chosen for your data"  [See details] [Change]
Step 4  Summary          ── Decisions · Assumptions · What will happen · Hidden columns
                            [ Approve ]   then the existing [ Run ]
Right side: chat with the helper ("why hide notes?", "make training faster")
```

- *Manual setup* is today's Setup screen, byte-identical.
- Approving fills the existing Setup state (primary key, target, `values`, `acknowledged`, `extraOverrides`) and
  the derived upload, so **Run is the existing `submit()`**. The Running and Results screens and the Data /
  Model / Output pages are unchanged. The Data page additionally shows the recipe receipt when one exists.
- **Score mode:** Guided setup replays the model's saved recipe on the new file and shows the receipt. It only
  asks something when the file changed (missing column, new format).

### 5.2 A proposal

Every item the user sees is a `Proposal`:

- **kind:** recipe step, setting, column role (key / target / date), exclusion, acknowledgement
- **what:** plain sentence, e.g. "Turn `monthly_bill` into numbers (`₹1,200` → 1200)"
- **why:** plain reason, e.g. "Stored as text, so the model cannot use it as a number"
- **evidence:** the tool result it came from, with its numbers (e.g. 48,110 of 48,210 values converted)
- **confidence:** `sure` or `check` (reuse onboarding's confidence pill)
- **default:** ticked or not ticked
- **state:** pending, accepted, rejected, or edited

### 5.3 When it asks instead of suggesting

| Suggest, ticked | Ask the user |
|---|---|
| Hide ID-like, constant or mostly-empty columns | `LEAKAGE_SUSPECTED`: "`refund_date` may contain the answer. Hide it?" |
| Hide PII columns (`PII_DETECTED`) | Two or more plausible date columns: "Which is the date of the data?" |
| Parse numbers, dates and yes/no when ≥ 95% convert | Target not found by name: "Which column says whether the customer converted?" |
| Merge case and space variants of a category | Target positive label unclear: "Which value means *converted*?" |
| Derive days-since from dates before the snapshot | Conversion rate below 95%: show the failing examples and ask |
| Recommended settings (§7) | `PK_NOT_UNIQUE`: "5 rows per customer. Combine them into one?" (M76) |

It **stops and explains** when the data cannot work: `TARGET_TOO_FEW_POSITIVES`, `ROWS_TOO_FEW`,
`TARGET_CONSTANT`. It says which data would fix the problem.

## 6. The recipe

### 6.1 Contract (`engine/agent/contracts.py`)

- `DataRecipe{recipe_id, use_case_id, source_fingerprint, steps: tuple[RecipeStep], created_at, approved_by, hash}`
- `RecipeStep{order, kind, column, new_column|None, params, reason, proposal_id, decided_by: agent|user}`

`hash` covers logic only (kinds, columns, params), like onboarding's `recipe_hash`.

### 6.2 Step kinds (v1)

| Kind | Does | Built on |
|---|---|---|
| `drop_column` | Hide a column from the model (kept in the file for the key or join-back when it is a role column) | new, trivial |
| `parse_number` | `"₹1,200"`, `"1,200.50"`, `"Rs. 1200"`, `"45%"`, `"(300)"` → number. Params: currency symbols, thousands and decimal separators, percent → fraction. | **new** |
| `parse_date` | Mixed styles → datetime with an explicit `date_format` or `dayfirst` decision; ambiguity becomes a question | `transforms.cast` + onboarding `DATE_FORMAT_AMBIGUOUS` logic |
| `map_boolean` | `Y/yes/TRUE/1` → 1, `N/no/FALSE/0` → 0 | `transforms.cast(boolean)` / `value_map` |
| `normalise_text` | Trim, fix case, merge listed variants (`"Delhi"`, `"delhi "`, `"DELHI"`) | `strip`, `lower`, `value_map` |
| `derive` | New column from existing ones, e.g. `days_between(last_order_date, snapshot_date)`, `total_spend / orders` | `transforms.derive` (AST whitelist) |

### 6.3 Execution rules (`engine/agent/recipe.py`)

- **Fixed order:** parse and normalise → derive → drop. `check_recipe()` refuses unknown kinds, unknown
  columns, a derive that reads a column dropped earlier, a derive that reads the target or a column after it
  (leakage), and duplicate new-column names.
- **Stateless:** a step's output for a row depends only on that row and the step's params.
- **Counted:** every step returns `{rows, converted, failed, examples_failed (masked)}`. Failures above
  `agent.max_conversion_failure_pct` (default 5%) stop the run and become a question.
- **Original untouched:** the source's `fingerprint.hash` is asserted unchanged after a run.
- **Output:** a derived upload `uploads/<new_id>/` holding `source.parquet`, `profile.json` and `upload.json`,
  with a new `derived_from{upload_id, recipe_hash}` field, plus `recipe.json` and `recipe_receipt.json`.
- **Preview:** the same code runs on a 1,000-row sample for before/after rows and the impact numbers.

### 6.4 Replay at score time (mandatory)

- `register` stores `data_recipe.json` with the model version when the training upload has one.
- In `POST /runs` score mode, when the chosen version has a recipe, the route applies it to the scoring upload,
  creates the derived upload, then runs `validate_against_schema` on the result.
- New validation codes in the plan §6.3 table and `docs/DATA_CONTRACT.md`:

| Code | When | Severity |
|---|---|---|
| `RECIPE_COLUMN_MISSING` | A column a step needs is absent. The message names it and suggests the nearest name. | error |
| `RECIPE_VALUES_UNCONVERTED` | Share of values a step could not convert is above the limit. Examples shown masked. | error above the limit; acknowledgeable warning below it |
| `RECIPE_STEP_INVALID` | A saved step can no longer run (bad parameters, or a file that cannot be written). A recipe replays under the levels and failure limit it was approved with (DEC-1043), so narrowing the use case's levels later does not stop a trained model. Replaces the sketched `RECIPE_NOT_SAVED`: a version finds its recipe in its training run's directory, so a missing file means "trained on the file as sent" (DEC-1013). | error |

- In Guided setup a `RECIPE_COLUMN_MISSING` becomes a question ("Is it now called `bill_amount`?"). The answer
  produces a new recipe version, recorded with lineage. The model's recipe is never edited in place.

## 7. Recommended settings (Auto)

`engine/agent/recommend.py`: `recommend_settings(profile, facts, config, schema) -> tuple[SettingProposal]`.

- **Pure rules over facts from tools.** Each rule yields `{path, value, reason, evidence}` for a path that
  exists in `advanced_settings_schema()` or `EXTRA_OVERRIDABLE_PATHS`.
- The full set is checked with `resolve_config()` before it is shown. A refused path is a bug, not a user
  error.
- **Never recommended:**
  - advisory paths (`ADVISORY_PATHS`)
  - immutable paths
  - `governance.approval_required: false`
  - lowering `validation.min_positive` or `validation.min_rows`
  - a threshold outside the field's `min`/`max`

Starting rules (tune in M73 against the benchmark in §10):

| Fact | Recommendation | Reason shown |
|---|---|---|
| A parseable date column exists and the use case allows it | `split.type: time_based`, `split.time_column` | "Test on the newest months, like real use." |
| Positive rate < 10% | `model_search.imbalance: class_weights` | "Only 4% said yes. The model must not ignore them." |
| Positive rate < 5% | `model_search.metric: pr_auc` | "Better measure for rare outcomes." |
| Rows < 5,000 | `model_search.strategy: fast`, a smaller `time_limit_minutes` | "Small file, so a quick search is enough." |
| Rows > 500,000 | a larger `time_limit_minutes` within the maximum | "Large file, so give the search more time." |
| Consent or opt-out column detected by name and values | `governance.consent_column`, `actions.suppress_opted_out` | "Respect customer choices." |
| Last-contacted date column detected | `actions.suppress_recently_contacted` | "Avoid contacting people twice in 14 days." |
| Otherwise | the use-case default, labelled "default for this use case" | — |

- Chat requests ("make training faster", "only target the top 20%") are mapped by the LLM to
  `{path, value}` **drawn from the schema's choices and ranges**, then validated by `resolve_config()`, then
  shown as a proposal to approve.
- `ui/settings.js` and `ui/usecase.js` must not hard-code any path (`test_the_setup_form_names_no_advanced_setting`).
  All paths come from the API.

## 8. The agent

### 8.1 Per-use-case configuration (`agent:` block)

Defaults live in `configs/engine.yaml`; a use-case YAML overrides them. The block is validated by pydantic
with unknown fields refused.

```yaml
agent:
  enabled: true
  display_name: "Targeted Advertisement helper"
  goal: "Find which customers are likely to buy within 30 days of seeing an ad."
  knowledge:                       # short facts in plain words, shown to the LLM and used by rules
    - "The outcome is a purchase within 30 days of ad exposure."
    - "Columns about the purchase itself (order_id, refund_*) contain the answer."
  column_hints:                    # optional extra hints beyond primary_key_hints / time_column_hints
    target_synonyms: [converted, purchased, bought]
    consent: [marketing_opt_in, opt_in, consent]
  levels: [clean, derive]          # reshape added in M76
  max_conversion_failure_pct: 5
  settings_rules: default          # or a named rule set in engine.yaml
  max_llm_calls_per_session: 40
  max_tool_steps_per_turn: 8
```

`agent` is not in `IMMUTABLE_PATHS` but is **not overridable per run**, and it is excluded from
`Recipe.recipe_hash`.

### 8.2 Knowledge pack

The prompt gets a pack generated from config and never hand-written:

- the use-case `target` block and `agent.goal` / `knowledge`
- the data contract rules
- each validation code's plain title, message and suggestion (from the glossary)
- each advanced-setting field's label, help, choices and range

### 8.3 Tools (`engine/agent/tools.py`)

A registry of `Tool{name, description, args_model, kind: read|propose|ask, run(session, args) -> ToolResult}`.
Every `ToolResult` gets an `evidence_id` and is stored in the session.

| Tool | Kind | Wraps |
|---|---|---|
| `get_profile` | read | `load_upload_profile` |
| `inspect_column(name)` | read | `ColumnProfile` plus masked samples, and the relation to the target (positive rate per bucket) |
| `find_format_issues(columns?)` | read | **new** detector: currency, percent, thousands separators, mixed dates, yes/no variants, case and space variants; returns counts and masked examples |
| `check_data(plan)` | read | `validate_for_training` / `validate_against_schema` on the plan's derived sample; no run started |
| `preview_recipe(recipe)` | read | §6.3 on a sample: before/after rows and per-step counts |
| `get_settings(…)` | read | `advanced_settings_schema()` plus `resolve_config()` of the current plan |
| `recommend_settings()` | read | §7 |
| `propose(proposal)` | propose | adds or edits a `Proposal` after `check_recipe` / `resolve_config` validation |
| `ask_user(question, options)` | ask | adds a `Question`; options are buttons |
| `finish_summary()` | read | assembles Decisions, Assumptions, Intended actions and Hidden columns from accepted proposals |

**No tool writes data.** Only `POST /agent-sessions/{id}/apply` does, and it refuses unless every proposal is
decided and every blocking question answered.

### 8.4 Loop (`engine/agent/loop.py`)

- **Start.** Without the LLM, the advisor runs: profile → format issues → roles → `check_data` → proposals and
  questions → recommend settings. The session is useful with no AI service.
- **Chat turn** (LLM available). The loop renders the prompt `configs/prompts/data_agent.v1.md` (system:
  knowledge pack and rules; user: session state, the user's message, the last tool results). It calls
  `Meter.complete(rendered, GenerativePurpose.agent_turn)`, parses one JSON action, runs the tool, and repeats
  up to `max_tool_steps_per_turn`. It ends with a `reply`.
- **Malformed JSON, an unknown tool, or invalid args** are fed back once, then the turn ends with a plain
  "I could not do that" plus the advisor's state. The session is never corrupted.

### 8.5 Guardrails on everything the agent writes

These are the existing `Guardrails` plus two new deterministic rules:

- **`numbers_grounded` (new):** every number in the agent's text must appear in this session's tool results,
  within formatting tolerance. Otherwise the reply is blocked and replaced by the advisor's template text.
  This enforces plan §2.1 rule 5 for generated text.
- **`allowed_actions_only` (new):** an action must name a registered tool, and a proposal a legal path or step
  kind.
- `pii_in_output`, `banned_phrases` and `max_length` as configured.
- Tool results pass the PII masker (`engine/pii.py`) **before** they enter a prompt. Raw rows never reach the
  LLM; only profiles, counts and masked examples do.

### 8.6 Fake LLM

- Add a prompt-shape branch for `data_agent` in `FakeLLMClient._prompt_shape`. In `GROUNDED` mode it returns a
  deterministic, valid action sequence: inspect, then reply with the advisor's summary.
- The existing modes (`UNGROUNDED`, `PII`, `MALFORMED`, `REFUSING`, …) each break one rule and must be caught by
  §8.5.
- The UI keeps the `backendBadge` watermark on fake-backend replies. In demo mode, chat shows the "Needs AI
  service connection" notice; the checklist still works.

## 9. API (`api/routes/agent.py`)

| Method and path | Does |
|---|---|
| `POST /uploads/{upload_id}/agent-session` | Start (or restart) Guided setup for `{use_case}`; runs the advisor; returns the session. One session per upload (DEC-1007), so every path is scoped to the upload (DEC-1018) |
| `GET /uploads/{upload_id}/agent-session` | Current proposals, questions, summary and transcript (masked) |
| `POST /uploads/{upload_id}/agent-session/decisions` | Accept or reject proposals (a setting may be edited to another allowed value); `accept_recommended` accepts every `sure` one |
| `POST /uploads/{upload_id}/agent-session/answers` | Answer a question; answering a role re-runs the advisor with the role fixed |
| `POST /uploads/{upload_id}/agent-session/messages` | One chat turn; returns the session with the reply and any new pending setting proposal |
| `POST /uploads/{upload_id}/agent-session/preview` | Before/after rows and impact numbers on the preview rows |
| `POST /uploads/{upload_id}/agent-session/apply` | Run the recipe, create the derived upload, and return the **run request pre-fill**: `{upload_id, primary_key, target, overrides, summary, receipt}`. 409 `AGENT_UNDECIDED` while anything is pending; 409 with the checks when Run would fail. |
| `POST /uploads/{upload_id}/checks` | Dry-run validation for `{use_case, primary_key, target, overrides}`; returns `ValidationReport` without starting a run. Also useful to Manual setup. |

**Access, audit and budget:**
- Access: Analyst, like `indexes.ask` (DEC-716). Apply follows the same rule as `POST /runs`.
- Audit: each call sets `set_audit_context`. Add `DETAIL_KEYS` `agent_session_id`, `recipe_hash`,
  `proposals_accepted`, `proposals_rejected`, `questions_answered`.
- Budget: session LLM usage is written to `uploads/<id>/agent/llm_usage.json` through the existing `Meter`.

## 10. How we know it works

**Benchmark.** `tests/fixtures/agent_bench/` holds each existing broken fixture plus a new **messy-file
generator** (currency, percent, thousands separators, mixed date styles, yes/no variants, case and space
variants, an ID column, a PII column, a leaky column, a consent column). Each case has a golden
`expected.json` with the proposals, questions and settings the agent must produce. It runs on the fake LLM in
CI. A manual `make agent-eval` runs the same cases on Bedrock and reports agreement.

**Must-pass tests:**
- **Approval.** Nothing is written to storage except session files until `apply`; `apply` refuses while
  anything is undecided.
- **Original untouched.** The original upload's fingerprint is unchanged after apply.
- **Deterministic.** The same file and the same recipe give byte-identical derived data.
- **Safe settings.** No advisory, immutable or approval-weakening path is ever proposed (property test over
  every use-case config).
- **Grounded.** `numbers_grounded` blocks the `UNGROUNDED` fake mode, and PII never reaches a prompt (the `PII`
  fake mode plus a prompt capture on `FakeLLMClient.calls`).
- **Score replay.** Train on a messy file, score next month's messy file, and every row gets a score. Renaming
  a column gives `RECIPE_COLUMN_MISSING` naming it.
- **Golden quality.** On the messy Targeted Advertisement fixture, test ROC-AUC after Guided setup is at least
  the ROC-AUC of Manual setup with defaults on the clean fixture, minus 0.01.
- **Browser journey** (Playwright, `@slow`): overview → use case → Guided setup → upload messy file → Approve →
  Run → results → score mode → recipe receipt → `scores.csv`.

**UI invariants** that must stay green:
- `test_ui.py`: the module list, no sample values, no placeholders, no hard-coded setting paths
- `test_shared_file_markers.py`: add a `PLAN-G` block to `ui/index.html` and `router.js` and to `PHASES`
- `test_ui_journey.py`: back links
- the escaping tests

## 11. Milestones

### M70 — Decisions, contracts, config

- Claim DEC-1000…1099 in `PARALLEL_WORK_PROTOCOL.md`, then record G1–G10 as DEC entries.
- Add pydantic contracts: `AgentSession`, `Proposal`, `Question`, `ToolResult`, `DataRecipe`, `RecipeStep`,
  `RecipeReceipt`, and `derived_from` on `UploadRecord`. Regenerate `docs/API.md`.
- Add the `agent:` config block with defaults in `engine.yaml`, and add it to every predictive and hybrid
  use case. Every config validates.
- **Done when:** `make lint test` is green, and every use case has an `agent` block that loads.

### M71 — Read and check tools, dry-run validation

- Build the tool registry and read tools, including the new `find_format_issues` detector.
- Add `POST /uploads/{id}/checks`.
- **Done when:** a unit test covers every tool on the broken fixtures, and the dry run returns the same checks
  that `POST /runs` would 409 with.

### M72 — Recipe engine and score replay

- Implement the step kinds, including the new `parse_number`, plus `check_recipe`, preview, derived upload and
  receipt.
- Store the recipe with the model version; replay it in `POST /runs` score mode; add the three new check codes.
- **Done when:** the recipe, original-untouched, deterministic and score-replay tests pass.

### M73 — Advisor: data-fix proposals, questions, settings

- Implement the rules in §5.3 and §7, which need no LLM, and the benchmark with golden files.
- **Done when:** every benchmark case matches its golden file, and the safe-settings property test passes.

### M74 — Agent loop, chat, guardrails, API

- Add the prompt, the JSON-action loop, the fake-LLM branch, `numbers_grounded` and `allowed_actions_only`.
- Add the session store and the endpoints in §9, with access and audit.
- **Done when:** every fake mode is caught by its rule, the API tests pass, and the budget is enforced per
  session.

### M75 — Guided setup in the UI

- Add a new seam in `router.js` (`registerSetupMode`) and scope the `.seg` Train/Score selector in
  `usecase.js` so a new tab strip cannot trigger it.
- Build `ui/modules/agent/`: upload step, checklist, questions, the Auto settings card, summary, chat (reuse
  the chat pattern from `modules/generative/assistant.js`), Approve, and pre-filling the Setup state through
  `applyFix` / `writePath` with API-supplied paths.
- Score mode shows the recipe receipt. Add a recipe section to the Data page.
- Add a new availability check for chat, based on the LLM backend rather than `generative.kind`.
- Add screenshots in `scripts/ui_screens.mjs` (`guided-setup--{empty,full,loading,error}`).
- **Done when:** the jsdom tests pass, the Playwright journey passes, and the UI invariants stay green.

### M76 — Level 3: many rows per entity

- On `PK_NOT_UNIQUE` with a date column, propose "combine into one row per entity". The proposal compiles
  suggested `FeatureDef`s (count, sum, mean, latest, days_since_last) through the onboarding feature engine,
  with the point-in-time guard and the full leak check on a first build.
- The result is a derived dataset used through the existing `dataset_id` path.
- **Done when:** a multi-row fixture trains through Guided setup, the leak check runs, and next month's file
  replays.

### M77 — Hardening and docs

- Write `docs/AGENTS.md` (how it works, how to add a rule, how to tune a use case's agent). Update the README
  and `docs/DATA_CONTRACT.md`.
- Report the benchmark results in `reports/`.
- Measure performance: a 1M-row file prepared within the plan's laptop budget, with preview on a sample.
- Run a security review of the new endpoints.
- **Done when:** `make test-all` is green twice, and a non-technical walk-through (plan §11 acceptance, run
  through Guided setup) passes without the ML team.

## 12. Later (design for, do not build now)

- **Level 4:** several files. Guided setup hands off to *Build from raw tables* with suggested roles, mappings
  and features pre-filled, and the agent explains each step.
- **Level 5:** create the target from events, e.g. "churned = no activity in 60 days" (onboarding `LabelSpec`).
  This always needs the user's explicit agreement on the definition.
- **Per-client memory:** a client's approved recipes are offered first for the next file of the same shape.
- **Native tool use** through Bedrock `converse` `toolConfig` behind the `LLMClient` protocol (supersedes G1's
  JSON loop only if measurably better).
- An agent on the Results pages that explains the model, e.g. "why is this customer High?", using the existing
  artefacts.

## 13. Risks

| Risk | Mitigation |
|---|---|
| The LLM invents a number or a column | `numbers_grounded`, `allowed_actions_only`, code re-validation of every proposal, advisor template fallback |
| A wrong automatic fix degrades the model | Nothing is applied without approval, preview shows before/after, golden-quality test, Manual setup always available |
| A recipe breaks on next month's file | Stops with `RECIPE_COLUMN_MISSING` / `RECIPE_VALUES_UNCONVERTED` and asks; the model's recipe is never edited in place |
| Client data reaches the LLM | Masking before prompts, profiles and counts only, Bedrock in the client's account, PII fake-mode test |
| Cost and latency | The advisor needs no LLM; `Meter` budget per session; LLM used only for chat and ambiguity |
| Two ways to set up confuse users | Guided is the default tab; Manual is labelled for experts; both end at the same Run |

## 14. Open decisions

| Decision | Assumed default | Revisit when |
|---|---|---|
| Tab name | *Guided setup* | Product review |
| Guided setup as the default tab | Yes, for Analysts; Manual remembered per user in the browser | After the first pilot |
| Default ticked state of `check`-confidence proposals | Not ticked | Benchmark results in M73 |
| `max_conversion_failure_pct` | 5% | Benchmark results in M73 |
| Level 3 in v1 | Yes (M76) | If M72–M75 slip |
| Chat language | English only | When a client needs another |
