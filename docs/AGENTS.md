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
  code. The AI service only adds the chat. With none connected the chat box says so
  (`chat.available = false`, `chat.reason = "AI_NOT_CONNECTED"`; a chat message is `409 AI_NOT_CONNECTED`)
  and everything else works the same. The chat uses **Product AI** (Connections → AI service), your
  team's own setting, separate from the customer-facing **Deliverable AI** (DEC-1140).

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
| `ui/modules/agent/` | The *Guided setup* tab (M75, DEC-1019 … DEC-1022), registered through `router.js`'s `registerSetupMode`; since Plan H it is the tab a use case opens on, with Manual setup one click away (DEC-1114). Under each chat reply that has `sent` items, `sent.js` draws a collapsed "What the AI looked at (N)" disclosure (the tool in plain words, its columns, the masked `preview` as escaped text in a height-capped block), and under the input a one-line status from `chat.data_access` / `chat.third_party` (nothing when no service is connected or when the API does not send them); when `chat.available` is false the chat box is replaced by a link to connect the Product AI (`#/connections/ai/product`). |

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
| `inspect_column` | `column` | Counts, up to 10 masked examples and 12 top values, and the outcome rate per bucket of the column. A personal-data column shows no values at all - not even its minimum or maximum. |
| `find_format_issues` | `columns` (optional) | Per text column, at most one issue: `number_as_text`, `mixed_dates`, `boolean_as_text`, `category_variants` or `untrimmed_text`, with counts, masked examples and the parameters a fix would use. The decimal mark is the one most values prove (a value counts only when it reads one way and not the other; `1,200` counts as grouping); when the values cannot tell (only `1.200`-style values, or a tie), no number fix is proposed. Category merge spellings are the stripped spellings as written, so `normalise_text` finds every one; the result lists them as `params.merge`: `[{from, to}, …]` (a dict keyed by spellings would put cells in a key the gate reads as a label), while the recipe step keeps its own dict. `example_cells` lists the cells the `examples` quote, piece by piece (§7.7). Personal-data columns are skipped (a column in `agent.always_hide_columns` is not: the rules still fix it). |
| `describe_outcome` | `column` | How many rows are "yes" by the engine's own label rule. |
| `describe_repeats` | `column` | How often each value of an ID column repeats: IDs, rows per ID, the most rows, IDs on several rows (the evidence behind "combine the rows?"). |
| `find_roles` | — | Which columns could be the ID, the outcome (exact, case-insensitive, synonym), the date, consent, opt-out and last contact. |
| `check_data` | `primary_key`, `target`, `overrides` | Every check the Run button would run (`check_plan`), with codes, severities, messages and fixes. |
| `sample_rows` | `columns` (1-12, default the first 8), `n` (1-20, default 10), `offset` (default 0), `where_column` + `equals` (optional exact match on one column) | Real rows in file order, aligned to `columns`; an empty cell is `null`, a personal-data column is the literal `"[personal data]"`, every other cell is masked and cut to 60 characters. Also `total_matching`, `returned`, `truncated` (and `next_offset`) and `withheld`. Deterministic: no random sample. A personal-data column cannot be used in `where_column` (`AGENT_TOOL_ARGS_INVALID`); a column in `agent.always_hide_columns` is not searched either, and answers no rows and `total_matching: null` whatever `equals` is. In `summaries_only` a value that fewer than 5 rows hold is answered the same way. Fewer rows come back when they would not fit the result budget (`truncated` and `next_offset` say where to go on). `equals` keeps its leading and trailing spaces, and on a column of true/false values it ignores case. |
| `value_counts` | `column`, `top` (1-50, default 20) | The most common values with their rows and share of all rows, `distinct`, `empty`, `other_rows`. A personal-data column gives counts only (`values_seen_once`, `most_rows_for_one_value`). |
| `find_values` | `column`, `contains` (1-40 characters), `n` (1-20, default 10) | The different values that contain the text (case ignored; plain text, never a pattern; a leading or trailing space in `contains` counts, so `' '` finds values with spaces) with their rows, and the total matching rows. **A personal-data column, and a column in `agent.always_hide_columns`, is not searched**: it answers `distinct`, no `matches` and no matching count, the same whatever `contains` is, because a count (even a yes / no) that depends on the text is an oracle that reads a value out one character at a time. In `summaries_only` a match held by fewer than 5 rows is not reported (`total_matching_rows` is `null` when none reaches 5). At most 5 `find_values` per column per turn and 15 per session (`AGENT_TOOL_LIMIT`). |
| `describe_numbers` | `column` | For a number column, or text that mostly (at least half) parses as numbers: minimum, maximum, mean, median, the 1/5/25/50/75/95/99 percent points, shares negative / zero / empty, outliers by the IQR rule, up to 10 histogram buckets, whether every value is whole. Every row is in exactly one of `numbers`, `empty`, `unparseable` and `infinite` (a column holding `inf`). The mean is exact (`math.fsum`), and a value too small for six decimals keeps six significant figures instead of showing as `0.0`. A personal-data column, or one in `agent.always_hide_columns`, is refused with counts and no statistic. |
| `describe_dates` | `column` | For a date column, or text that mostly parses as dates: earliest and latest (ISO), distinct days and months, longest gap in days, share with a time part, `day_first` and whether the order is ambiguous (from `formats.date_order`), how many are not dates, and the 5 most common written shapes (`99/99/9999`: digits 9, capitals A, lower-case a). A bare time of day (`09:30`) is not a date (pandas would read it as today). A personal-data column is refused. |
| `compare_columns` | `left`, `right` | Share of rows equal, share both empty, `kind` (`identical`, `one_to_one`, `left_determines_right`, `right_determines_left`, `constant_multiple`, `offset`, `linear`, `correlated`, `unrelated`, `not_derived`), a top-10 cross-tab for categories, Pearson and Spearman for numbers, and the slope and intercept when one is an exact linear function of the other (a constant column is never derived from another). The purpose is spotting duplicates and leaks. Either column personal: refused. |
| `describe_missing` | `columns` (optional, at most 30) | Empty share of the 30 most empty columns, groups of columns empty on the same rows (on at most 100,000 rows), and, when the outcome is known, the outcome rate where each of the 5 most empty columns is empty against filled. |
| `describe_duplicates` | `columns` (optional, 1-5) | Duplicate rows and share for the key (the whole row when no column is given), repeated keys, the largest group, the 5 most repeated keys (masked; personal-data columns `"[personal data]"`) and how many repeated keys differ in their other columns. `0.0` and `-0.0` are the same value. |

**Hidden columns.** A column in `agent.always_hide_columns` is treated by every look tool exactly like one the
profile marks as personal data: no value, no minimum, no match and no count that depends on what was asked
(`_personal` in `tools.py` includes it). Only `find_format_issues` still reads it, because the advisor's
rules fix such a column and the model never receives what they quote (§7.7).

**Look first.** The chat helper may look at every column and every value of the file except
personal data, and its prompt says it must: before it makes a claim about values, formats, dates,
duplicates or relationships it calls a look tool, and if it has not looked it says so. All eight
compute on the frame in memory (vectorised; about a second on a million rows; text dates, text
numbers and cross-tabs read an evenly spread sample past 100,000 rows or 20,000 distinct values, and say
`sampled`), stay under 5,500 characters of JSON per result, and only read. The size is the real `json.dumps` length (a cell full of quotes or control characters is several times longer once escaped): a list that does not fit loses items from its end and the result says `truncated` (and `shown`); totals such as `other_rows` still add up to the rows. An empty cell is a null or,
in a text column, an empty string. Numbers in the results are evidence for `numbers_grounded` like any
other. The advisor never calls them, so its evidence and the benchmark are unchanged.

**Where text may sit in a result.** Names of columns and fixed words are under `column`, `columns`,
`type`, `kind`, `code`, `name`, `function`, `dtype`, `label` and `message`. Everything derived from a
cell is under other keys (`value`, `values`, `rows`, `cells`, `matches`, `left_value`, `right_value`,
`earliest`, `latest`, `shape`, `positive_label`) and has been masked **whole** with the gate's complete scanner
set (`egress.mask_value`: e-mail, phone, PAN, Aadhaar, card, IBAN, IP, URL, API-key-like token, nine-digit run)
and only then cut to 60 characters (40 in `describe_duplicates`, 80 for an example), so a default-deny egress
gate can treat every other string as a cell and never depends on seeing a cell before it was cut. **A dict is
never keyed by a cell value**: its keys are field names of ours (`egress.RESULT_KEYS`) or column names; the
canary walks every result of every tool and fails on any other key.

A column name may be given exactly or as it was shown (cleaned and cut to 64 characters, §7.4);
anything else is `AGENT_COLUMN_UNKNOWN`. Bad arguments are `AGENT_TOOL_ARGS_INVALID`. Results name
columns the same way (the shown name), so one very long header cannot fill a result. Arguments are
trimmed of surrounding spaces, except `find_values.contains` and `sample_rows.equals`, which are values
to look for. Text columns treat `""` as an empty cell in every text type (`object`, `string`, category).

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
  **question** over the synonyms, or over the two-valued columns, when there are two or more; `check`
  again (a pending proposal to accept, with a reason that says it is the only two-valued column) when
  there is exactly **one** two-valued column and no synonym, because a question needs at least two
  choices; a **stop** when there is no candidate at all.

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
- Any other error (`TIME_COLUMN_MISSING`, `TIME_COLUMN_UNPARSEABLE`, a code added later) → the checks
  run again with the suggested settings that start ticked (§5); an error still there → **stop**, with
  its message and suggestion. A session therefore never reaches `ready` on its own suggestions with
  an error Approve's checks would refuse.
- A column a leak question may hide is never named by the helper's own suggested settings (the date
  to split by, the consent column). A split by date the person asked for in chat can still name it;
  answering Hide on that column is then refused with `TIME_COLUMN_MISSING` (422), as a decision
  that would split by a hidden date is, and the message says which suggestions to reject first.
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
| The use case splits by date but its date column is absent, and the file has a date column (≥ 3 values) the use case can split by | `split.time_column` = that column | `sure` |
| … and the file has no such date column | `split.type: random_stratified` | `sure` |
| … and its date column is in the file but cannot be used (fewer than 3 dates, a column a leak question may hide, …) | `split.type: random_stratified`; the reason names what is wrong with the column | `sure` |
| … and its date column is in the file but the Run button's check cannot read it as dates (`TIME_COLUMN_UNPARSEABLE`) | `split.type: random_stratified` + `split.time_column: null` (that check runs whatever the split), citing the check | `sure` |
| The use case splits at random and the file has a date column (≥ 3 values) the use case can split by | `split.type: time_based` + `split.time_column` | `check` |
| Fewer than 5% of rows are "yes" and the metric is ROC-AUC | `model_search.metric: pr_auc` | `sure` |
| Fewer than 5,000 rows | `model_search.time_limit_minutes: 10` | `sure` |
| More than 500,000 rows | `model_search.time_limit_minutes: 60` | `sure` |
| A consent-looking column not used for suppression | `governance.consent_column` | `check` |

Imbalance is not proposed: `model_search.imbalance: auto` already weights rare outcomes; below 10%
"yes" this is stated as an assumption. Every suggestion must be a field of
`advanced_settings_schema()` (or an extra overridable path), inside its choices or range, never
advisory, never in `NEVER_RECOMMENDED` (`validation.min_positive`, `validation.min_rows`,
`validation.leakage_check`, `governance.approval_required`, `validation.acknowledged`), and the whole
set must resolve with `resolve_config`. "Can split by" means the pair resolves: `resolve_config`
accepts only a template column with role `time` (`TEMPLATE_TIME_MISSING`), so a split by date and its
column are resolved, kept and dropped as one group. A setting the rules say nothing about keeps the use
case's default. A person may edit a suggested setting only to another value the same check allows, and
the settings accepted after any decision must resolve together. One setting has at most one accepted
value: accepting a second suggestion for it rejects the first, and when one decision accepts several
(Preview sends the helper's ticked suggestion and the person's chat request together), the latest
suggestion in the session wins - the chat request over the helper's - and the others are rejected.
A split by date is decided with its date column: once no split suggestion is pending, a decision that
would split by date with no date column in the file (accepting "Test on the newest rows" but rejecting
its column, or rejecting the random split a file without the use case's date column needs) is refused
with `TIME_COLUMN_MISSING` and a message naming the suggestions to accept, instead of reaching
`ready` and failing at Approve. "Accept recommended" (`accept_recommended`) never touches a setting the
person has already accepted a value for: the helper's suggestion for it stays pending for them.

## 6. The recipe

### 6.1 Step kinds

A `DataRecipe` is an ordered list of `RecipeStep`s. Every step is **stateless**: a row-wise step's
output for a row depends only on that row and the step's parameters (DEC-1004), and `combine_rows`'s
output for an entity only on that entity's rows (DEC-1023). Nothing is learnt from the whole file, so
running a recipe before the train/test split leaks nothing and next month's file is prepared
identically.

| Kind | Parameters | Does |
|---|---|---|
| `parse_number` | `decimal` (`.` or `,`), `percent_to_fraction` | `"₹1,200"`, `"Rs. 1,20,000"`, `"45%"` (→ 0.45), `"(300)"` (→ −300), `"1.200,50"` → number. A grouping mark counts only where grouping puts it (`1,200.50`, `1,20,000`; `1.200,50` under `,`), so a value in the other convention (`"1,5"` under `.`, `"1.5"` under `,`) fails and is counted instead of being read as 15. A real number inside a text column (a chunked CSV read) is read by its value, not its text. |
| `parse_date` | `dayfirst` (must be decided) | Mixed styles → datetime, each value parsed on its own; a value with a UTC offset is converted to UTC before the offset is dropped, a value without one is kept as written. A value with no digit (`now`, `today`) is never a date: it fails and is counted. |
| `map_boolean` | `true_values`, `false_values` | Listed spellings (compared trimmed, case-folded) → 1 / 0; a real number matches a numeric spelling by value (1.0 is `"1"`); anything else fails. |
| `normalise_text` | `strip`, `merge` (frozen at approval) | Trim; replace listed spellings by their canonical one. |
| `combine_rows` | the step's `column` is the entity key; `time_column`, `snapshot_column`, `dayfirst` (per date column: a map of column to true/false/null, frozen at planning; a single value applies to every date column), `outcome`, `features` (a frozen `{name, function, column}` list) | Level 3: many rows per entity into one (DEC-1023 … DEC-1025), computed by the onboarding feature engine with its point-in-time guard. Only rows dated on or before the entity's snapshot count; the outcome is read from the latest row, not aggregated; `latest` settles a same-date tie by the later snapshot, then the later row in the file (the row the outcome would read among those rows; across dates `latest` follows the row date and the outcome the snapshot); numbers get sum/mean/max/latest, categories latest/nunique, dates days-since (over dates on or before the snapshot; a date column already holding later dates on earlier rows is left out of the plan, and on a training file stops with `RECIPE_STEP_INVALID` (`FUTURE_EVENTS_LEAKED: ...`) if a later file shows it), plus a row count. A row with no key or date, or whose entity has no readable snapshot on any row, is a counted failure; a blank snapshot on one row is fine when the entity has one elsewhere. Dates with an offset or zone are compared in UTC (a zoned value unreadable in UTC is empty); out-of-range dates are empty. Each date column's day/month order is the one frozen for it at planning (its own values' order, else the file's when its date columns agree, else `null`); the combine never re-decides it, and a `null` column holding an ambiguous date stops with `RECIPE_VALUES_UNCONVERTED`. An untyped key column is read as text on every row, so 7 and `"7"` are one entity. Carried consent/opt-out/last-contact columns keep their header whatever its spelling. Entity-wise rather than row-wise: an entity's output depends only on its own rows and the frozen parameters, never on other entities or a statistic of the whole file. A training Approve runs onboarding's full future-data leak probe, a scoring replay the narrow one, a preview none. Needs the `reshape` level. |
| `derive` | `expression` | A new column from `engine.onboarding.transforms.derive`'s whitelist: names, numbers, `+ - * /`, `days_between`, `months_between`, `year`, `month`, `coalesce`, `lower`, `abs`, and `snapshot_date`. At most 300 characters; no repeated text. Needs the `derive` level. Not proposed by the advisor today. |
| `drop_column` | — | Hide a column from the model. A file without the column has nothing to hide: the step is skipped and its receipt says `skipped: true`, so a hidden column (a leak, often unknown when scoring) need not be in a scoring file. |

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
column the file lacks is `RECIPE_COLUMN_MISSING`, except for a `drop_column` step, which is skipped
(a column the file has but an earlier step used up is still refused). A parser that cannot run on a
column's values (never expected; a coded guard) is `RECIPE_STEP_INVALID`, not a 500. `run_recipe` works on a copy and counts, per step,
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

A malformed reply (including one nested too deep to decode), an unknown or writing tool, or bad
arguments are fed back once; a second failure ends the turn plainly. `evidence_ids` that is not a
list cites nothing. Anything else a step raises ends the turn plainly with `blocked_by:
AGENT_TURN_FAILED`, so the turn still returns and its model calls are still counted and metered.
At most `max_tool_steps_per_turn` actions per turn (default 12, so the helper can take several looks).

### 7.2 Grounding (`numbers_grounded`)

`engine/agent/grounding.py`. Every number in a reply must appear in the session's tool results, the
advisor's proposals and questions, the assumptions and hidden columns, or the person's own message.
Formatting is forgiven (`4.2%` = 0.042, `3,000` = 3000, `12.5` for 12.4837, `40k` for 40,123);
invention is not. A number written with letters attached (`40k`, `3x`, `1e5`, `1m`) is still
checked; so are a decimal without its leading zero (`.73`) and a percent glued to a word (`is73%`).
Only small ordinals ("the 2nd step") and a bare `0` or `1` pass anywhere; `0%`, `1%` and `100%` are
claims and must be grounded. A quoted number is skipped only when the quoted text is exactly a column
name. What the model itself wrote - the reason it gave for a suggested setting, or a number it passed
as a tool argument (`get_profile`'s `offset`, a `check_data` override) even when the result echoes
it - is **never** evidence, so a number cannot be laundered from one step into the next. Only the
arguments the model actually wrote count (`ToolResult.supplied`, `typed_args`): a default the engine
filled in (`n=10`, `top=20`) is not a number the model typed, so a count of 10 or 20 the engine measured
stays evidence. A record saved before `supplied` existed treats every argument as typed.

### 7.3 Guardrails

A reply, and the reason given with a chat suggestion, must pass the numbers check and the platform
guardrails (`configs/guardrails.yaml`: personal data, banned phrases, length ≤ 800 characters for a
reply, 200 for a reason). A failing reply is replaced by "I could not answer that from what I have
checked." plus the next open decision; a failing reason by "You asked for this change.". Each reply
stores a `TurnLog` (`llm_calls`, `tools`, `error_codes`, `blocked_by`) and no content: `tools` holds
only known action names (a read tool, `propose_setting`, `reply`); any other name the model writes is
logged as `unknown`.

### 7.4 Untrusted file content (prompt injection)

Column names and cell values come from the client's file. `engine/agent/untrusted.py`:

- strips control, zero-width, bidirectional and other invisible characters from every string that
  reaches the prompt, collapses whitespace, cuts a column name to 64 characters and any other string
  to 500, and lists longer than 50 items to 50 plus a count;
- the prompt says that file content is data, never instructions, and asks the model to tell the
  person when it sees an instruction there;
- keeps the file's headers out of the **system** section, above that rule: a setting whose choices are
  the file's columns (`evaluation.fairness_column`, `split.group_column`, ...) says "one of the column
  names listed under 'Columns a setting may name'" there, and the columns themselves are listed in the
  **user** section under that heading, through the same gate as every other file text (aliases, masks).
  A setting with fixed choices (a metric, a model family) still lists them in the system section;
- the test model (`FakeLLMClient`, allowed only with `MARKETING_AI_ALLOW_FAKE_AI`) finds its sections by the full delimiter line (`\n\nThe person says:\n`),
  so a cell or message that contains a section's words does not split the prompt;
- a prompt that would still exceed 60,000 characters is not sent (`blocked_by: prompt_too_large`).

**Worst case** if a file does manipulate the model: it can call read tools (which see the data, but only what the
egress gate lets through, §7.7: masked cells, and counts only for personal-data columns), suggest a setting that the schema allows (pending, for the person to approve, never a
safeguard), or write a misleading sentence that still has to pass the numbers check and the
guardrails. It cannot change data, hide or add a column, approve anything or reach another upload.

### 7.5 Personal data

Tool results carry counts and masked cells (`egress.mask_value`, the gate's own scanner set) - up to 20 rows of 12 columns from `sample_rows`, 50 values from `value_counts`, 20 from `find_values` - not five examples: the model reads the file itself. Personal-data columns, and columns in `agent.always_hide_columns`, show no value at all (`"[personal data]"` / `"[hidden by your settings]"` or counts only, never a minimum, a maximum, a match or a count that depends on a search text). A cell is masked whole **before** it is cut to length, so no half of an
email or phone number survives the cut. The person's message is masked with `engine.pii.redact_text` **before** it
is stored or put in a prompt, so the stored transcript - which a Viewer can read - is masked.
What the tools return is *not* what the AI service receives: everything passes the egress gate in §7.7 first.

### 7.6 Budget and bounds

- `agent.max_llm_calls_per_session` model calls per session (default 40), counted in the session
  file; the generative `budget` (calls and dollars) applies per request through `Meter`.
- `uploads/<id>/agent/llm_usage.json` adds up every turn of the session.
- A session keeps at most `2 × (max_llm_calls_per_session + 10)` chat messages; after that
  `…/messages` answers 409 `AGENT_CHAT_FULL`.
- Requests that change one session are serialised by a per-upload lock, so two chat turns cannot
  both spend the same budget or overwrite each other (one API process; see §11).

### 7.7 What the AI service sees: default-deny egress, two modes, what is recorded

The product rule: the chat sees the **actual data** - every column, every value it asks for - except
masked personal data, so it does not have to guess. Masking by pattern can never be complete (a name
inside a sentence, an address, another country's ID number look like ordinary words), so the boundary
is **default-deny** and lives in one module, `engine/agent/egress.py`, wired in one place
(`loop.chat_turn`): nothing reaches `Meter.complete` except what has passed it.

**Default-deny.** `egress.prepare` walks every tool result. A string is a *label* only when it sits under
one of `SAFE_KEYS` - `column columns type kind code name function dtype label message`, plus `error`,
`severity`, `suggestion`, `override_path` (what the loop and `check_data` already return) - or is a known
column name or a fixed literal (a detector name such as `email`, a marker). **Every other string is a cell
value**, whatever key it sits under and whichever tool wrote it, and goes through `mask_value`. A tool
added tomorrow is therefore masked without anyone remembering to; a tool must put non-cell text under a
`SAFE_KEYS` key and cell-derived text under any other key. A label is still checked: an identifier key
must hold an identifier-shaped value, and every label is scanned. A dict keyed by column names (a row) holds
cells even under a key like `name`.

**`mask_value`.** The text is first read the way a person reads it: compatibility-folded (full-width digits,
`＠`), every Unicode dash (hyphen, en and em dash, minus sign...) written as `-`, and every character that
draws nothing removed - the `C*` categories, but also the combining grapheme joiner, variation selectors,
Hangul and Khmer fillers, the braille blank, and a combining mark on an ASCII character (a phone number in
keycap emoji is digits). Then the structured scanners in this order: URL, e-mail (`engine.pii`'s pattern,
which needs a dotted ASCII domain), **address-shaped identifiers** without one (`jane.roe@okhdfcbank` - a UPI
id -, `x@intranet`, `ravi@example.भारत`; the host must start with a letter and have three characters, so an
`@` in prose is not one), payment card (13 to 19 digits, Luhn-valid, joined by a space, `-`, `_`, `/`, `,` or a
spaced hyphen; when the card is followed by another number the card is found among the groups of digits),
IBAN, IPv4/IPv6, API-key-like tokens (a feature name such as `total_spend_last_90d` is not one). Then the
platform's own `engine.pii.redact_text` (phone, PAN, Aadhaar, e-mail); then a **PAN typed with spaces or
hyphens** (`ABCDE 1234 F`), a **phone with a bracketed block** (`+91 (98765) 43210`, `+7 (495) 123-45-67`, at
least nine digits), any other run of nine or more digits, and groups of three or more digits joined by a
spaced hyphen. Dates (`2024-01-31`), UUIDs and decimals with a short whole part (`12345.678`, `-122.4194155`)
are kept on purpose - but not when the decimal is a piece of a longer number (`9876.543210`, the
`43210.0` of `+91 98765 43210.0`); a date of birth is not guessed. A cell is masked whole and then cut to
80 characters. Every pattern is linear in the length of the text (a 20,000-character run of any shape is
scanned in under 0.1 s; `test_scanner_hardening.py` fuzzes each scanner), and the tools mask at most 2,000
characters of a cell: a longer cell keeps its masked head (cut at a delimiter) and a length, and an unbroken
run of 1,000 or more characters is shown as `[REDACTED:token]`.
A word with a marker inside it (`zk_live_51Hx...[REDACTED:phone]45`, the rest of a value that a tool's own
masker already cut) is masked whole.

**Two modes** (`agent.ai_data_access`, per use case, not overridable per run):

| Mode | The model sees |
|---|---|
| `masked_data` (default) | cell values after masking, up to what the tools return |
| `summaries_only` | no cell at all: every cell-derived string is its **shape** (digits 9, letters `A`/`a`, runs of at most four, at most 32 characters: `Jane Roe, 12 Baker St` is `Aaaa Aaa, 99 Aaaa Aa`); numbers that are cells (`minimum`, `median`, list items) are shapes too. Counts, averages (`mean`), types and check messages stay. A dict key that is not a field name of ours is a shape too. In the advisor's sentences every cell it quoted becomes its shape (below). |

A column the profile marks as personal data never yields a value in **either** mode, and neither does a
column named in `agent.always_hide_columns` (case-insensitive; default none): its strings become
`[personal data]` / `[hidden by your settings]` and only counts remain (`rows`, `empty`, `distinct`, `*_rate`,
`*_count`...). This applies to every tool, including tools added later, because it is done on the result, not
inside the tool; a suggestion about such a column shows none of its quoted examples (below).

**The advisor's sentences (structured examples).** A suggestion's `reason` quotes real cells (`'Basic' → 'BASIC'`),
and a person must see them, so the sentence for the *screen* is never changed. The advisor also records the cells it
quoted, as written in the sentence, in `Proposal.examples` / `Question.examples` (optional; a session saved before
they existed loads with none). The copy for the model is built in `loop._state` by `Egress.sentences`, which
replaces each `quoted(cell)` **by value** (one pass, longest first, so an apostrophe inside a cell -
`O'Brien`, `Men's Wear` - cannot hide it from a parser) by the cell's shape (`summaries_only`), its masked self
(`masked_data`) or the hidden marker. A suggestion is hidden **as a whole**: when its step's column, or any column
its title, reason or question names in quotes, is hidden by the profile or by `agent.always_hide_columns`, every
quoted example in all its sentences becomes `[hidden by your settings]` - the reason that quotes the cells no
longer has to name the column. Quotes that are not covered by `examples` (an old session) are still read out of the
prose as before (`_QUOTED`, which now accepts an apostrophe between two letters), so the structured path removes
the reliance on that parse and does not add a new one. `untrusted.quoted` is unchanged: the person's text keeps
`'O'Brien'`.

**Dict keys.** The gate reads a key as a label only when it is a column, one of `SAFE_KEYS`, or a field name in
`egress.RESULT_KEYS` (the names our tools, checks and the loop write). Any other key may be a cell (`{cell: count}`):
under a hidden column it becomes the hidden marker and in `summaries_only` its shape; in `masked_data` a
label-shaped key still passes and the rest is masked like a value. No tool may return such a dict:
`find_format_issues` lists `params.merge` as `[{from, to}]`, and the canary fails on a key that is neither a column nor
in `RESULT_KEYS`, so a new field name is added on purpose.

**Numbers.** A number is a cell unless its key is a count (`rows`, `distinct`, `*_rate`, ...): in `summaries_only` it
becomes a shape, and in every mode a number with nine or more digits is masked. `mean`, `std`, `var`, `sum` and
`average` stay exact in `summaries_only` - but not `median`, which with an odd number of rows is one person's value
(the same number as `p50`) - and, unlike a count, they are subject to the nine-digit rule (the mean of ten-digit
phone numbers stored as floats is masked).

**Searches are not oracles.** `find_values` (and `sample_rows` with `where_column`) answer nothing that depends on the
searched text for a personal-data or hidden column, and in `summaries_only` nothing about fewer than 5 rows
(§3); `find_values` is capped per column (5 per turn, 15 per session). `agent.always_hide_columns` entries that the
file does not have are listed in the session's assumptions as a warning (and logged): a typo hides nothing.

**Column names.** A name that reads as ordinary words (letters, digits, spaces, `_`, `-`; at most 64
characters; no `@`, no run of six digits, no URL or path, nothing a scanner would change) is shown as it is.
Any other name - an e-mail address as a header, a 12-digit number, a year (`2020`), a name of 65 characters - is
shown as a stable alias `column_<n>` (n = its position in the file, so it is the same on every request and
needs no storage). A name is replaced by its alias inside a sentence only as a whole word, and a name that is
only a number (`2020`, `0.05`, `500.0`) never: in a sentence it cannot be told from the number, so the
ranges and dates in the settings block stay as they are. The model may use an alias as a tool argument; the engine translates it back. In the reply the
person reads, an alias is replaced by the real name again **only when that name is itself clean**: a header
that is an e-mail address stays `column_5` in the stored transcript.

**The last check.** After the prompt is rendered, `egress.assert_clean` re-scans it with the same scanners, after
the same folding (dashes, invisible characters, full-width look-alikes; the layout is kept, and a prompt
that matches nothing is returned byte for byte). A bare number in the JSON of a tool result (`"rows": 1000000`,
`"maximum": 100000000`) is not scanned - the gate already masked every cell number of nine digits or more -
while a number inside a string is. What still matches is masked and the turn log records `EGRESS_LATE_MASK` in
`error_codes`; it never fires on the canary (below), so a non-zero count means a bug or an injection upstream. The person's own message is masked
with every scanner, not only `redact_text`.

**What is recorded (transparency).** Each agent `ChatMessage` has `sent`: one `SentItem` per tool result put
in that turn's prompts - `tool`, `args` (masked), `preview` (the exact masked payload **after the last check**, at most 1,500
characters), `chars` (its whole length) and `mode`. Up to 25 items per message (a turn can take 21 looks; the screen shows 12 and counts the rest) and 20 KB of previews per
session file; when the session is over the limit the oldest previews are replaced by a short note and keep
their `chars`. A session saved before this field existed loads with `sent` empty. `GET/POST …/agent-session…`
also answer `chat.available` / `chat.reason` (false with `AI_NOT_CONNECTED` when Product AI has nothing
connected), `chat.backend` (the provider's id, `none` when not connected), `chat.provider_label`,
`chat.data_access` and `chat.third_party` (true unless the effective Product AI is Amazon Bedrock or the
test model; `egress.is_third_party(llm, provider)` is fed `engine.ai_service.effective_service(...,
slot="product")`, so a saved OpenAI, Claude, OpenRouter, Hugging Face or compatible server counts as a
third party and the data gate above is chosen for it).

**The canary.** `tests/unit/agent/test_egress_canary.py` and `tests/integration/agent/test_egress_sessions.py`
plant fake personal data (an e-mail, a phone, PAN, card, Aadhaar, IBAN, IPv4 and IPv6, a URL with a token, an
API key, a name and an address in a sentence) in an obviously personal column, in innocent-looking columns
(`notes`, `feedback`, `region_contact`), in the ID column and in **headers**, run whole sessions through the
real `chat_turn` with a client that calls **every tool** on every column, and search every prompt, every stored
message and every `sent` preview. No pattern-recognisable value appears in either mode; in `summaries_only`
and with the text columns in `always_hide_columns` neither do the names and addresses; a fuzz test builds
secrets from each scanner's alphabet and checks none survives `mask_value`; a temporary new tool that returns a
raw cell under a new key is masked by default, and one that returns a dict keyed by cells is caught (every dict key of
every tool result is a column or a field name in `RESULT_KEYS`).

**What masking still cannot guarantee.** In `masked_data` mode a person's **name inside a sentence**, or a
**street address**, in a column that neither the profile nor the settings marked, is shown to the model: the
canary asserts this on purpose (`test_masked_data_cannot_hide_a_name_in_a_sentence`). So are other
countries' ID formats no scanner knows (a MAC address, a driving-licence or national-ID number, a UK NI
number), a name written as a column header in ordinary words (`Jane Roe`), and obfuscated values
(`jane dot roe at example dot test`). UPI ids (`name@bank`), dotless and internationalised addresses, a PAN
typed with separators and phones with a bracketed block are masked; a phone or card written with words
(`nine eight seven six...`), a sentence of digits with letters between them (`9x8x7x6...`) or with a
homoglyph letter for a digit is not. That is why `always_hide_columns` and `summaries_only` exist:
list the free-text columns, or switch the mode, for data where that is not acceptable.

**What changed after the leak review** (findings 1-7 and 12; the scanner findings are in the scanners' own notes):

| Was | Now |
|---|---|
| Hiding looked for the hidden column's name *inside one string*, so a `reason` that quoted its cells but did not name the column went out | A suggestion is hidden as a whole, from its step column and every sentence (`Egress.sentences`, `Proposal.examples`) |
| `summaries_only` found examples by parsing quotes, and `'Men's Wear'` did not parse | The advisor records the quoted cells and the gate replaces them by value; the parse fallback accepts an inner apostrophe |
| `find_format_issues.params.merge` was a dict keyed by cells; `_key` let label-shaped keys through | A list of `{from, to}`; any key that is not a column or a field name is hidden / shaped in a hidden column and in `summaries_only`; the canary asserts it |
| `median` exempt like `mean` | `median` is a shape in `summaries_only`; every non-count number of nine or more digits is masked |
| `sent[].preview` taken before the last check | Taken after it (`egress.sent_item` runs `assert_clean`) |
| Tools cut a cell to 60 (40, 80) characters before the gate's card / IBAN / IP / URL / token scanners saw it | `tools._masked` and `formats.masked_cut` mask the whole cell with the complete scanner set, then cut |
| `find_values` counted matches on any column; `sample_rows` filtered by a hidden column | No search on a personal or hidden column; nothing under 5 rows in `summaries_only`; a per-column cap; hidden columns are "personal" for the tools |

## 8. API

| Method and path | Role | Does |
|---|---|---|
| `POST /uploads/{upload_id}/checks` | Analyst | Dry-run validation for `{use_case, primary_key, target, model_version_id, overrides}`; 200 with the `ValidationReport` `POST /runs` would give (DEC-1011); the same 409 `RECIPE_ROLES_MISMATCH` as Run for another ID column or outcome than a prepared file's recipe. |
| `POST /uploads/{upload_id}/agent-session` | Analyst | Start or restart Guided setup for `{use_case, model_version_id?}` (a scoring session checks against the model Run will use); rules only, no model call; 201 with the session. 409 `AGENT_NOT_AVAILABLE` / `AGENT_USE_CASE_MISMATCH` / `AGENT_UPLOAD_PREPARED` (a training upload Guided setup already prepared). |
| `GET /uploads/{upload_id}/agent-session` | Viewer | The session. 404 `AGENT_SESSION_NOT_FOUND`. |
| `POST …/agent-session/decisions` | Analyst | `{decisions: [{proposal_id, state, value?}], accept_recommended}`. 422 `AGENT_EDIT_NOT_ALLOWED` / `AGENT_VALUE_NOT_ALLOWED`, the `resolve_config` code (e.g. `TEMPLATE_TIME_MISSING`) when the accepted settings do not resolve together, or `TIME_COLUMN_MISSING` when they would split by date with no date column; `accept_recommended` skips a setting the person already accepted a value for; 404 unknown id, 409 `AGENT_SESSION_APPLIED`. |
| `POST …/agent-session/answers` | Analyst | `{question_id, option_id}`; answering again replaces the previous answer and what it added. A role answer re-runs the advisor, keeping decisions already made (an accepted suggestion wins over a rejected duplicate); a step the person accepted on a column that is now the ID or the outcome is dropped, since the recipe may never change those. |
| `POST …/agent-session/messages` | Analyst | `{text}` (≤ 1,000 characters): one chat turn. 409 `AGENT_SESSION_APPLIED` / `AGENT_CHAT_FULL`. The reply carries `sent` (§7.7); the response's `chat` says `data_access` and `third_party`. |
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
  max_tool_steps_per_turn: 12         # 1..20
  ai_data_access: masked_data         # masked_data | summaries_only (§7.7)
  always_hide_columns: []             # columns whose values the chat model never sees, any case (§7.7)
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

- **A float column of phone-like numbers is masked for the model but not in the stored evidence.** The gate
  masks any number of nine or more digits that is not a count (`minimum`, `maximum`, `mean`, `median`, list items),
  so neither the prompt nor `ChatMessage.sent[].preview` holds it. `session.tool_results` (the tool's own output,
  below) still does, and so does the preview of the derived file. The personal-data detector does not look at float
  columns; teaching `engine/pii` to look at float columns whose values are all whole numbers is follow-up work.

- **One API process.** The session lock is a thread lock; several API processes writing one session
  need a store-level lock. The per-session call budget is counted in the session file; the dollar
  budget is per request (`generative.budget.max_cost_usd_per_run`), not per session.
- **Every session request re-reads the upload** (up to the profile cap), so on a very large file each
  decision takes as long as reading it; answering a role question re-runs the advisor.
- **Numbers in words** ("seventy percent") are not checked by `numbers_grounded`; the guardrails and
  the prompt are the only defence there.
- **Column names** that are not ordinary words (an e-mail as a header, a long number, a year or another
  number, a URL, more than 64 characters) are shown to the model as an alias `column_<n>` (§7.7); a header that *is* ordinary words -
  including a person's name - is shown as it is, because nothing can tell it from a real header.
- **A name in a sentence, an address, or an ID format no scanner knows** in a column nobody marked reaches the
  AI service in `masked_data` mode (§7.7). Use `agent.always_hide_columns` for the free-text columns or
  `agent.ai_data_access: summaries_only`. Obfuscated values (`jane dot roe at example dot test`), MAC
  addresses and other national ID formats are not recognised either. UPI ids and other `name@host` identifiers
  are masked (they are shaped like an address); an eight-digit identifier (`REF-72304995`) is read as a phone
  number by `engine.pii` and masked, by design.
- **Stored evidence is not the gated copy.** `session.tool_results` keeps each tool's own output (masked by
  the tool with `egress.mask_value`, not by the egress gate); what the AI service received is the gated
  payload in `ChatMessage.sent`. The person's stored message is masked with `redact_text`; the extra
  scanners apply to what is sent.
- **Sequences made only of `A`, `a` and `9`** (a shape) are never masked, by design: they carry no value.
  A real value of only nines in groups of 2-4 (`9999 9999`) is therefore not masked.
- **A dict keyed by cell values** in a tool result is never sent as written in `summaries_only` or under a hidden
  column (its key is a shape or the hidden marker), but in `masked_data` a label-shaped key (`{"jane": 3}`) still
  passes. No tool returns one: every result is walked by the canary, which fails on a key that is neither a column
  nor a field name in `egress.RESULT_KEYS`. A tool with a new field name adds it there (and in `summaries_only` it
  is shaped until it does).
- **Match counts on a normal column** (`find_values`, `sample_rows` with `where_column`) are exact in `masked_data`,
  where the model may read the column anyway; in `summaries_only` they say nothing about fewer than 5 rows. A model
  that reads a value that many rows share out of the counts learns an aggregate `value_counts` lists too.
- **A suggestion saved before `examples` existed** has none, so the model's copy of its sentences falls back on
  reading quotes (`'Jane Roe'`; an apostrophe between two letters is accepted, a value that ends in an apostrophe
  next to a space is not). Starting the session again records them.
- **`derive` steps** are supported by the recipe engine but not proposed by the advisor or the chat.
- **The combine leak probe** is onboarding's `_run_leak_probe`, which opens its own DuckDB connection;
  Plan G makes it single-threaded by swapping `duckdb.connect` for the duration of the probe
  (`reshape._single_threaded_duckdb`). The lasting fix is for the probe to accept a connection.
- **A combine preview** is computed on the first 1,000 entities and without the leak probe; the numbers
  it shows are for those entities only.
- **A combine with an unproven day/month order waits for the day/month question.** When nothing in
  the file proves the order, the plan freezes that column's order as `null` and the format detector
  asks which comes first. The answer's `parse_date` runs before the combine, so the combine reads
  real dates. Answered Combine first, the helper waits for the day/month answer (no checks yet) and
  advises again once it is given. A `null` column with no question (a value the detector does not
  ask about) still stops with `RECIPE_VALUES_UNCONVERTED`.
- **Overwritten date columns.** The leak probe re-dates only the row date, so a date column written
  after the snapshot (an overwritten "last order date") is left out by the plan (made on the file the
  helper reads, up to its profile row cap), and on a training file caught by a direct check in the
  combine (`RECIPE_STEP_INVALID`, message starting `FUTURE_EVENTS_LEAKED:`), not by the probe. The
  plan's rule is conservative: it also leaves out an honest per-event date (a refund or delivery
  date) that fell after the snapshot on the preview, although the combine's masking would make it
  safe, so that signal is lost.
- `make agent-eval` (the benchmark on Bedrock) is not built yet.
- Chat is English only.
