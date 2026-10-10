# Marketing AI — Plan K: Data Doctor, one agent for messy data

**Companion to:** Plan G (`MARKETING_AI_PLAN_G_AGENTS.md`, Guided setup), `docs/AGENTS.md`, `docs/ONBOARDING.md`, `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Branch:** a new branch from `main` when the build starts
**Decision range:** DEC-1400 … 1499 (the next free hundred; the first milestone claims it in the protocol table)
**Milestones:** M120 – M131 (M112–M119 left free for Plan J)
**Status:** proposed, waiting for approval

> **How to use this document.** Read it with Plan G and `docs/AGENTS.md`. Plan G built a careful helper
> inside each use case. This plan turns that helper, the onboarding builder and the AI service into one
> agent, **Data Doctor**, for people who have no data team, several files of uneven quality and no idea
> what format the product wants. It keeps every promise Plan G made (§5.2). Where this document is
> silent, follow Plan G and the protocol. Where it is impossible or clearly wrong, stop, explain, and
> propose the smallest change.

---

## 1. Why

The people we now sell to do not have good data and do not have a team to fix it. A typical first
hour looks like this:

- They have **several files**: an Excel export from the CRM with a title row and a "Grand Total" row
  at the bottom, a billing CSV per month, a sheet someone keeps by hand.
- Some files are **usable**, some are **not**, and some hold **one piece** of what we need (the
  outcome in one file, the customer details in another).
- They **do not know** what "one row per customer", "outcome column" or "ID" mean, and they should
  not have to.

Today every one of these meets a wall before they see any value: an upload error, a stop with a
correct but technical message, or a *Manual setup* screen. Each wall is a reason to give up. The
measures we have taken so far (Guided setup, the placeholder-code check, the raw-tables builder) treat
symptoms one at a time, each inside its own screen.

**Goal in one sentence:** a person with no data skills drops whatever files they have into one
place, Data Doctor tells them in plain words what they have and what it can power, fixes what can be
fixed with their approval, lets them correct anything by hand, builds the one file a use case needs
from the files they have, and tells them honestly what is missing and how to get it. Next month the
same treatment runs by itself.

## 2. In plain words (for the product owner)

- **Data Doctor is one agent with three hands.** Our **rules** find problems and measure them. The
  **AI** reads the data like a colleague, understands what columns and files mean, and proposes fixes
  the rules cannot see. The **person** decides and can correct anything by hand.
- **It is a place, not just a step.** Home gets a "Bring your data" entry: drop any files, before
  choosing a use case. Inside every use case it is still the Guided setup they know.
- **It writes prescriptions, not code.** Every fix, by rule, by AI or by hand, is a step from a fixed
  list that our tested code runs. The list grows (§6), but the AI never writes code and never edits a
  file.
- **It can make new files from old ones.** Join files, stack monthly files, turn a wide sheet into rows,
  build the outcome from events, cut a file into "learn from" and "score". The originals never change.
- **It tells the truth about what is missing.** "Your files can power Churn today. Win-back needs the
  date of the last order, which none of your files has. Ask your billing team for X."
- **Without an AI service it still works,** as Guided setup does today: the rules, the checklist and the
  manual edits work; only the understanding and the chat need the AI.

## 3. What we handle today

An honest inventory, so the plan builds on what exists instead of redoing it.

| Problem a client's data has | Handled by | Where |
|---|---|---|
| Unknown encoding, unknown CSV delimiter | Ingest tries encodings and sniffs the delimiter | `engine/stages/ingest.py` (`detect_encoding`, `sniff_delimiter`) |
| Numbers written as text (`₹1,200`, `Rs. 1,20,000`, `45%`, `(300)`, `1.200,50`) | `parse_number` step, proposed by the advisor | `engine/agent/formats.py`, `recipe.py` |
| Dates in several styles; day/month order | `parse_date`; asks when the order is ambiguous | `formats.py`, `advisor.py` |
| `Y/yes/TRUE/0` flags | `map_boolean` | `formats.py` |
| `Delhi` / `delhi ` / `DELHI`; stray spaces | `normalise_text` | `formats.py` |
| Placeholder codes (`99` months, `-1` days) | `set_missing` (DEC-1220) | `engine/agent/placeholders.py` |
| Which column is the ID and which the outcome | Role rules, questions when unsure | `advisor.py`, `find_roles` |
| A column that contains the answer (leak) | Blocking question: hide or keep | `advisor.py`, `LEAKAGE_SUSPECTED` |
| Personal data | Detected, masked before any prompt, hidden from the model | `engine/pii.py`, `engine/agent/egress.py` |
| Many rows per customer in one file | `combine_rows` (level 3), point-in-time safe | `engine/agent/reshape.py` |
| Several tables (customers, bills, tickets, usage) | Raw-tables builder: roles, mapping, features, snapshots | `engine/onboarding/` |
| Several files of the same table (monthly exports) | Stacked by role in the build | `engine/onboarding/build.py` |
| Keys that nearly match across tables (leading zeros, prefixes) | `KEY_FORMAT_MISMATCH` with the one transform that fixes it | `engine/onboarding/validate.py` |
| No outcome column ("churn" is a sentence, not a column) | `LabelSpec` builds it from events | `engine/onboarding/labels.py` |
| Next month's file | The approved recipe replays; stops and names the problem when the file changed | `api/routes/agent_recipes.py` |
| Questions about the data | Chat with read-only look tools, grounded numbers | `engine/agent/loop.py`, `tools.py` |
| Too few rows, no outcome, repeated ID | Stops with a reason and what would fix it | `advisor.py` (`STOP_CODES`) |

This is a strong base. The safety model (recipe-only changes, approval, replay, grounding, egress
gate) is better than most products in this market and must not be weakened.

## 4. What we do not handle

Grouped by where the person gets stuck. "Today" says what actually happens.

### 4.1 Getting the file in

| Problem | How common for our users | Today |
|---|---|---|
| Excel files (`.xlsx`, `.xls`), several sheets | Very common: most non-technical exports | Refused: `UPLOAD_UNSUPPORTED_FORMAT`, "Only CSV and Parquet" (`ingest.py` `file_format_for`) |
| Header not on the first row (a title, a date line, blank rows on top) | Very common in report exports | The title becomes the header; columns are named `Unnamed: 1` … or the upload fails `UPLOAD_NO_COLUMNS` |
| Two header rows (merged "Q1 / Revenue") | Common in hand-kept sheets | Read as data |
| Repeated column names | Common in Excel | Refused: `UPLOAD_DUPLICATE_COLUMNS` |
| "Grand Total" / "Subtotal" / notes rows at the bottom | Common in report exports | Read as customers; can break the ID check or skew numbers |
| Missing values written as text (`N/A`, `-`, `NULL`, `none`, `not known`) | Very common | Only an empty cell is missing (`na_values=[""]`); a number column with `N/A` stays text, and `N/A` becomes a category |

### 4.2 Shape

| Problem | Today |
|---|---|
| Several files, one use case | Possible only through *Manual setup → Build from raw tables*; Guided setup never offers it (Plan G level 4, "later") and Home only describes the path in plain text (`ui/modules/simple/pages.js`) |
| A wide sheet (`Jan_spend`, `Feb_spend`, … as columns) | No unpivot; each month is a separate, unrelated column |
| One column holding two things (`"Mumbai, MH"`, `"Gold - Annual"`) | No split |
| The outcome in one file, the customers in another | Builder only (Manual setup) |
| A built dataset scored by a model with a recipe | Refused: `RECIPE_DATASET_UNSUPPORTED` |
| A file that mixes the training rows and the rows to score | No way to cut it in Guided setup |

### 4.3 Values

| Problem | Today |
|---|---|
| Same category, different words (`Prepaid` / `Pre-paid` / `PP`, `Bombay` / `Mumbai`) | `normalise_text` merges case and spaces only |
| Impossible values (age 212, negative tenure, a date in 2099) | Not flagged unless it is one of the six placeholder codes |
| Mixed units (`2 GB` and `500 MB`; amounts in rupees and lakhs) | Not detected |
| A value the person knows is wrong | No way to correct it: the helper "never edits the file", and there is no editing screen |

### 4.4 Meaning

| Problem | Today |
|---|---|
| "What is this file?" | Roles are proposed in the builder only; Guided setup sees one file |
| "What can my data do?" | The person must pick a use case first and learn by failing its checks |
| "What do these columns mean?" | Chat can look and explain, but cannot act on what it understands |
| The AI understands a fix the rules cannot see | The chat may only `propose_setting` (`loop.py`); it cannot propose a data fix |

### 4.5 Enough data

| Problem | Today |
|---|---|
| Too few rows, too few "yes" outcomes, too short a history | A stop with a reason, then nothing: no "what you can do instead" and no "collect this, then come back" |

### 4.6 Things that stay out of scope (§8)

Matching people across systems with no shared key, reading scanned PDFs or photos, inventing an
outcome history the client never recorded.

## 5. The Data Doctor agent

### 5.1 The visit

```
 Intake ─► Diagnosis ─► Treatment plan ─► Preview ─► Approve ─► Prescription saved ─► Follow-up
 (any files)  (what you have,   (fixes by rules,  (before/after,  (copy made,   (recipe + intake +     (next month's
              what it can power) AI and by hand)   rows changed)   originals     build spec, per        files replay;
                                                                   untouched)    client)                stops on change)
```

- **Intake.** Drop one or many files: CSV, Parquet, Excel (every sheet), from an upload or a connection.
  For each file the intake rules find the header row, the footer rows, the missing-value words and
  repeated column names (§6.1). What they decide is recorded, so next month's file is read the same way.
- **Diagnosis.** For each file: what it is (a customer list, bills, tickets, a campaign log; the
  onboarding role scores, explained by the AI), its health (the existing format, placeholder, leak and
  personal-data findings), and how its keys meet the other files' keys (`join_coverage`,
  `KEY_FORMAT_MISMATCH`). Across files: **what your data can power**, per use case, as one of
  *Ready*, *Ready after fixes*, *Missing something* (named, with who usually has it), from the use
  case's own required roles and checks. No use case has to be picked first.
- **Treatment plan.** One checklist, as today, with three sources of items, each labelled:
  *Found by our checks* (rules, can start ticked when `sure`), *Suggested by the AI* (always `check`,
  never ticked for the person, like `set_missing` today), *Your edit* (manual).
- **Preview.** Before/after on a sample with the receipt (rows changed, values that failed), as today,
  plus for a build: rows per customer, join coverage, and the outcome rate.
- **Approve.** Runs on a copy. A single file becomes a prepared upload; several files become a built
  dataset. Lineage names every original.
- **Prescription.** The recipe, the intake decisions and, for several files, the build spec, saved with
  the model and remembered per client (§5.6).
- **Follow-up.** Next month's files replay. A change (new column, new spelling, a sheet renamed) stops
  and asks, naming the change, as `RECIPE_COLUMN_MISSING` does today.

### 5.2 What does not change (Plan G's promises)

1. **Nothing changes until the person approves.** AI suggestions are pending, `check`, never ticked.
2. **The AI never writes code and never edits a file.** It proposes steps from the fixed list; our code
   validates them (`check_recipe`) and runs them on a copy. "Level 6: agent-written code" stays **never**.
3. **Every step is stateless** (DEC-1004): its output for a row depends on that row and the step's
   frozen parameters only, so running it before the train/test split leaks nothing and replay is exact.
   A step that would need a whole-file statistic (fill with the mean, scale by the maximum) is refused;
   the model's own preprocessing does that inside the split.
4. **Every number the AI says is grounded** in a tool result (`numbers_grounded`).
5. **The egress gate decides what the AI may see**; personal-data columns give counts only.
6. **No step may read the outcome to change other rows.** `filter_rows` on the outcome, a derived column
   from the outcome, a value map on the outcome are refused, as today.
7. **The rules part works without an AI service.**

### 5.3 What the AI newly does

Today the AI can look and talk; it can only act on settings. Data Doctor gives it one new action and
several new things to understand. The safety comes from where the action lands, not from keeping the
AI quiet.

| New AI action | Lands as | Checked by |
|---|---|---|
| `propose_step` `{kind, column, params, reason, evidence_ids}` | A pending `check` proposal on the checklist | `check_recipe` on the session's recipe plus the new step; a preview receipt on the sample; the reason through `numbers_grounded` and the guardrails; `evidence_ids` must cite a look-tool result of the same session |
| `propose_role` (what a file is) | A pending choice in the diagnosis | The onboarding role catalogue; the role's required columns must exist |
| `propose_mapping` (which column is which) | A pending mapping row | `engine/onboarding/mapping.py` rules; one column per standard name |
| `propose_label` (the outcome from events, in the person's words) | A pending outcome definition **shown back as a sentence** ("A customer has churned when they have no recharge for 60 days after the snapshot") | `LabelSpec` validation; always needs the person's explicit agreement (Plan G level 5) |
| `explain` (what a column or file means) | Text in the diagnosis, labelled as the AI's reading | Grounding and guardrails |

What the AI is good at and the rules are not: synonyms (`Pre-paid` = `PP` = `Prepaid`), what a column
means from its name and values, which file is the customer list, what a business sentence means as an
outcome, unit words in values, which rows are notes. What the rules are good at and the AI is not:
counting, proving, replaying. Data Doctor uses each for what it is good at.

A turn's budget, the per-session call limit and the per-upload lock work as today
(`agent.max_llm_calls_per_session`, `Meter`).

### 5.4 Editing by hand

The person can open a **table view** of any file (personal data masked as in `sample_rows`) and:

- change one value everywhere it appears ("`Bombay` → `Mumbai`") → a `map_values` step;
- correct one cell for one customer → a `correct_cells` step keyed by the ID and the old value
  (it replays: next month the same customer with the same wrong value is corrected; a customer whose
  value changed is left alone and counted);
- remove rows by a rule ("rows where `status` is `TEST`") → a `filter_rows` step;
- rename, hide or split a column → `rename_column`, `drop_column`, `split_column`;
- undo any of it: an edit is a pending step like any other, so unticking it undoes it.

Edits are never written into the file; they are steps, so they appear in the checklist, the preview,
the receipt, the audit log and next month's replay like every other fix.

### 5.5 Making new files from old ones

| The person says | Data Doctor does | Built on |
|---|---|---|
| "Use all these files" | Proposes roles, mappings and the key; shows join coverage; builds one row per customer | Onboarding builder (Plan G level 4); `join_coverage`, `KEY_FORMAT_MISMATCH` |
| "These are the same report for different months" | Stacks them (`append`), checks the columns agree, records the month | `build.py` stacking by role; a new intake check for column drift |
| "My sheet has a column per month" | `unpivot` into rows (customer, month, value), then `combine_rows` if needed | New step (§6.2) + level 3 |
| "I don't have a churn column" | Proposes the outcome from events, as a sentence to agree | `LabelSpec` (level 5) |
| "Score the customers who joined after March" | Cuts one file into a training file and a scoring file by a rule | New `split_file` action producing two prepared uploads with one recipe |

The result of several files is a built dataset. Plan K closes `RECIPE_DATASET_UNSUPPORTED`: a built
dataset carries its build spec and the recipe applied after the build, and scoring replays both.

### 5.6 Memory per client

A client's approved intake decisions, recipes and build specs are offered first for the next file of
the same shape ("This looks like last month's billing export. Use the same treatment?"). Shape means
the column fingerprint the ingest already computes, plus the sheet name for Excel. Memory is per
client (`docs/CLIENT_ISOLATION.md`), never shared across clients.

### 5.7 Enough data, and what to do when there is not

The diagnosis says, per use case, whether the data is enough, using the checks that already decide it
(`ROWS_TOO_FEW`, the minimum positives, history length for snapshots, and the test planner's sample size
for use cases that contact customers). When it is not enough it never just stops. It says:

- **what is short and by how much** ("312 customers left in the last year; the model needs about 500");
- **what you can do now** (a longer history window, a broader outcome, another use case the data
  does power, a simple ranked list by a rule the person agrees, clearly labelled as not a model);
- **what to collect, and from whom** (a short data request in plain words, reusing the pilot's data
  request kit).

## 6. New intake rules and step kinds

### 6.1 Intake rules (before a file has columns)

Recorded in an `IntakeSpec` saved with the recipe; replayed exactly; a later file that does not fit
stops and names what changed.

| Rule | Decides | Confidence |
|---|---|---|
| Excel reader | Which sheet(s); each sheet is a file in the diagnosis | Asks when several sheets look like data |
| Header finder | The header row (the first row whose cells are mostly distinct, non-empty text followed by rows of the same shape); two header rows joined (`Q1 Revenue`) | `sure` when one row clearly wins; else a question showing the top rows |
| Footer finder | Trailing rows that are totals or notes (`Total`, `Grand Total`, a row with an empty ID and a sum equal to the column above, free text in the first cell only) | `check`; shown with the rows it would drop |
| Repeated column names | Renamed `name`, `name_2`, … (the safe-name rule in `engine/column_names.py`) | `sure` |
| Missing-value words | `N/A`, `NA`, `n/a`, `-`, `--`, `NULL`, `null`, `None`, `none`, `nil`, `#N/A`, `?`, `not known`, `unknown` (configurable per client) read as empty, only in columns where they sit beside other values | `sure` for the fixed list; `check` for a word the AI proposes |

### 6.2 New recipe step kinds

All stateless, all with a receipt (`changed`, `failed`), all refused on the ID and the outcome unless
the table says otherwise.

| Kind | Parameters | Level | Proposed by |
|---|---|---|---|
| `map_values` | `mapping` (spelling → canonical, frozen), `unmapped: keep` | clean | rules (case/space, as `normalise_text`), AI (synonyms), person |
| `set_missing_text` | `values` (1–20 words) | clean | intake rule, AI |
| `valid_range` | `minimum`, `maximum` (either may be null), `action: set_missing` | clean | rules (impossible for the column's meaning: age, a percentage, a future date), AI, person |
| `convert_units` | `units` (a frozen map, e.g. `MB: 1, GB: 1024`), `target_unit` | clean | AI (rules verify every value carries a listed unit) |
| `split_column` | `separator` or `pattern` from a fixed set (`first word`, `before/after the separator`), `new_columns` | derive | AI, person |
| `rename_column` | `new_name` | clean | person, AI |
| `correct_cells` | `cells`: `[{key, old, new}]` (≤ 1,000) | clean | person only |
| `filter_rows` | `column`, `operator` (`equals`, `in`, `is_empty`, `before`, `after`), `values`; never the outcome | derive | rules (test rows, footers), person, AI |
| `unpivot` | `id_columns`, `value_columns` (frozen list), `name_pattern` (e.g. `{metric}_{month}`) | reshape | rules (repeating column-name pattern), AI |

`fill_missing` with a constant is allowed (`clean`); with a mean or median it is refused (a whole-file
statistic, §5.2.3).

## 7. Safety

- **Same gate for every source.** A step from the rules, the AI or the person goes through
  `check_recipe`, the preview and Approve the same way. The AI has no faster path.
- **The AI's step is never trusted for its effect.** The preview receipt measures what it does; a step
  that changes more than `agent.max_ai_step_change_pct` of a column (default 30%) is shown with a
  warning, and a step that fails on more values than `max_conversion_failure_pct` is refused, as today.
- **Prompt injection.** File content stays data (`untrusted.py`). The new actions add pending items only;
  the worst case remains a misleading suggestion a person must still approve, now with a before/after
  in front of them.
- **Leaks.** No new step may read the outcome; `filter_rows` and `unpivot` re-run the leak and
  future-data checks on a training Approve; `split_file` by a date keeps the point-in-time guard.
- **Audit.** Every proposal records its source (`rules`, `ai`, `person`), and every approval records
  who approved it (`engine/audit/`).

## 8. What Data Doctor will not do

Said plainly in the product, so the person is not surprised:

- **Match people across files with no shared key.** It normalises keys (leading zeros, prefixes, phone
  formats) and says how many match; it does not guess that "R. Sharma, Pune" is "Rahul Sharma". That is
  identity resolution, a CDP's job.
- **Read scanned PDFs, photos or free-form documents** as tables.
- **Invent history.** If a client never recorded who left, Data Doctor can build an outcome from events
  they did record; it cannot create one from nothing.
- **Promise a good model from data without the signal.** It says when the data is enough to try, and the
  training report says how good the result is.
- **Change an original file** or write code.

## 9. Milestones

Ordered by how much resistance each removes, not by how interesting it is to build.

| Milestone | What | Done when |
|---|---|---|
| **M120** Intake widening | Excel (every sheet), header and footer finders, repeated names, missing-value words; `IntakeSpec` saved and replayed. Rules only. | 30 real-shape messy files (§10) all reach a profile with correct headers; next month's file replays; no change to the 22 pinned Plan G benchmark digests except where an intake rule now applies (each listed in the DEC) |
| **M121** Data Doctor, the place | Home "Bring your data"; drop many files; diagnosis per file (role, health, keys); *what your data can power* per use case; hand-off into a use case's Guided setup with everything pre-filled | A non-technical tester with 3 mixed files reaches a use case's checklist without opening Manual setup |
| **M122** The AI proposes steps | `propose_step` for the existing step kinds; `explain`; sources labelled on the checklist | Fake-model tests: a grounded step lands pending; an ungrounded, leaking or invalid one is refused; the injection canary passes |
| **M123** New value steps | `map_values`, `set_missing_text`, `valid_range`, `convert_units`, `split_column`, `rename_column`, `fill_missing` (constant) | Each step: stateless property test, receipt, replay test, refusal on ID/outcome |
| **M124** Editing by hand | Table view; edits become `map_values`, `correct_cells`, `filter_rows`, `rename_column`, `drop_column`; undo by unticking | Jsdom + Playwright journey: edit, preview, approve, replay next month |
| **M125** Several files (level 4) | Data Doctor drives the onboarding builder: `propose_role`, `propose_mapping`, join coverage, stacking monthly files with a column-drift check; built datasets carry a recipe, closing `RECIPE_DATASET_UNSUPPORTED` | Customer list + 3 monthly bills + tickets train through Data Doctor with no Manual setup; scoring next month replays |
| **M126** Wide sheets | `unpivot` with name-pattern detection | A month-per-column sheet trains and replays |
| **M127** Outcome from events (level 5) | `propose_label` from the person's sentence, shown back as a sentence, explicit agreement | "No recharge for 60 days" becomes a `LabelSpec` that builds, with censoring explained |
| **M128** Enough data | Readiness per use case; *what you can do now*; the data request | Every `STOP_CODES` stop in Data Doctor shows what to do instead and what to collect |
| **M129** Memory per client | Same-shape files offered the saved treatment | Second month's upload is one click to Approve |
| **M130** The messy-file benchmark | 30 files shaped like real client exports (§10); time-to-first-run measured with non-technical testers | Targets in §11 met or the gap recorded |
| **M131** Hardening and docs | `docs/DATA_DOCTOR.md`; security review of the new actions; 1M-row and 20-sheet performance | `make test-all` green twice |

M120 and M121 alone remove most of the walls a first-hour user hits, and need no AI. They are the
right first build.

## 10. The messy-file benchmark

Plan G's benchmark (`tests/fixtures/agent_bench/`) pins 22 files, each with one clean problem. Real
files have several at once. Add 30 synthetic files shaped like what clients send (no client data):

- an Excel CRM export with a logo row, a date line, two header rows and a "Grand Total";
- a workbook with a customer sheet, a pivot sheet and a notes sheet;
- three monthly billing CSVs where the third renamed a column;
- a sheet with a column per month;
- a hand-kept list with `N/A`, `-`, `Bombay`/`Mumbai`, ages of 212 and `TEST` rows;
- a customer list keyed `CUST-00123` and a ticket log keyed `123`;
- a file with no outcome but a recharge log that can define one;
- a file too small for any use case, to test the "what to do instead" path.

Each file has an expected diagnosis, treatment and readiness, pinned like `expected.json`.

## 11. How we know it worked

| Measure | Today (measure in M130 before building) | Target |
|---|---|---|
| Files that fail at upload | — | < 2% of the benchmark |
| First files to an approved run without Manual setup | — | ≥ 80% of the benchmark |
| Time from first file to first approved run, non-technical tester | — | < 20 minutes |
| Questions asked per file | — | ≤ 3 on average |
| Second month: clicks to Approve | — | 1 |

## 12. Open decisions

| Decision | Assumed default | Revisit when |
|---|---|---|
| Name in the product | *Data Doctor* (the place), *Guided setup* stays the tab inside a use case | Product review |
| Excel library | `openpyxl` for `.xlsx` (pinned in Plan K's `pyproject.toml` block); `.xls` through `xlrd` or refused with "save as .xlsx" | M120 |
| AI-suggested steps ever start ticked | Never (as `set_missing`) | After M130's benchmark |
| `correct_cells` limit | 1,000 cells per recipe | First pilot |
| Rule-based "simple ranked list" when data is short | Offered, labelled "not a model" | Product review: it must not be mistaken for a prediction |
| Data Doctor without a use case's config | Diagnosis uses every enabled use case's required roles | M121 |
