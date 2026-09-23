# Data contract

The upload must be a **flat table with exactly one row per entity** (customer, order, asset).

This document is what a business user reads when the engine refuses their file. Plan §4 is the
normative statement of the *shape*; the validation table below is written from the **implemented
checks** in [`engine/stages/validate.py`](../engine/stages/validate.py), so it matches the behaviour
rather than the intention. Every code, message and suggestion here is the literal text the check
produces.

---

## 1. Required

- A **primary key** column: unique, non-null. Never used as a feature.
- For training: a **target** column. Binary (two distinct values, any labels such as 0/1, yes/no,
  true/false, churned/active) or numeric for regression.
- At least `min_rows` rows (default 1,000) and `min_positive` positive examples (default 200) for
  classification.
- CSV (UTF-8, comma separated, header row) or Parquet. Max 2 GB in Phase 1
  (`validation.max_file_size_mb: 2048` in `configs/engine.yaml`).

## 2. Strongly recommended

- A **snapshot/date column** (any column whose name matches `date|time|month|week|day|_ts$|_at$`, or
  picked by the user). Required when the use-case config sets `split.type: time_based`.
- Features that describe the entity **as of the snapshot date**, i.e. before the outcome happened.

---

## 3. Refusals that happen before validation

A file has to be readable before any check can look at it. These refusals come from the upload
route and the ingest stage, not from the validation table, and they are answered on
`POST /uploads` rather than on `POST /runs`.

| Code | HTTP | When | Message |
|---|---|---|---|
| `UPLOAD_TOO_LARGE` | 413 | File exceeds `validation.max_file_size_mb` | "The file is larger than the 2048 MB limit." |
| `UPLOAD_UNSUPPORTED_FORMAT` | 415 | Extension is not `.csv` or `.parquet` | "Only CSV and Parquet files can be uploaded." |
| `UPLOAD_EMPTY` | 422 | Zero bytes | "The file is empty." |
| `UPLOAD_NO_COLUMNS` | 422 | First row carries no column names | "The first row of the file does not contain column names." |
| `UPLOAD_DUPLICATE_COLUMNS` | 422 | The header repeats a name | "The header repeats the column name '{name}'. Every column needs a distinct name." |
| `UPLOAD_NO_ROWS` | 422 | Header row, no data rows | "The file has a header row but no data rows." |
| `UPLOAD_ENCODING_UNSUPPORTED` | 422 | Not decodable as text after the encoding retries | "The file is not text the engine can read. Save it as UTF-8 CSV and upload it again." |
| `UPLOAD_UNREADABLE` | 422 | The CSV/Parquet parser cannot read the body | "The file could not be read as {format}." |

---

## 4. How validation runs

`validate` is a **pure function of the frame**: every check takes the table and a flat, frozen
`CheckParams`, and returns findings. It never raises on bad data.

1. **Every check runs.** None may stop another. A check that raises an exception contributes no
   finding and is logged once as its exception *class name* only — never its message, which a
   pandas exception can quote a cell value into (plan §13.7).
2. **Two severities reach the user.** `error` blocks the run; `warning` is shown and the run
   continues.
3. **Findings are ordered** errors first, then warnings, then the validation-table order below,
   then the column's position in the file. The UI renders every item.
4. **`POST /runs` validates synchronously.** While any unacknowledged `error` remains, the run is
   refused with `409`. The body is the ordinary error envelope plus the whole report beside it:

   ```json
   {
     "detail": {"code": "VALIDATION_FAILED",
                "message": "3 problems must be fixed before this data can be used.",
                "path": null},
     "validation": {"upload_id": "...", "mode": "train", "checks": [...],
                    "error_count": 3, "warning_count": 4, "passed": false, "validated_at": "..."}
   }
   ```

   `passed` is `true` only when `error_count == 0`, and `error_count` excludes acknowledged errors.
5. **A check is skipped** when what it needs is absent (no primary key, no target, a non-binary
   problem type, a switch turned off). A skipped check produces no finding and is not a pass.

### 4.1 Which checks run when

A run is either **train** (`ingest → validate → …`) or **score**
(`ingest → validate_against_schema → …`), and the check set differs:

| Mode | Checks that run |
|---|---|
| both | `PK_MISSING`, `PK_NOT_UNIQUE`, `PK_NULLS`, `PII_DETECTED`, `CONSENT_COLUMN_MISSING`, `SUPPRESSION_COLUMN_MISSING` |
| train only | `TARGET_MISSING`, `TARGET_NOT_BINARY`, `TARGET_CONSTANT`, `TARGET_TOO_FEW_POSITIVES`, `TARGET_IMBALANCE_SEVERE`, `ROWS_TOO_FEW`, `LEAKAGE_SUSPECTED`, `TIME_COLUMN_MISSING`, `TIME_COLUMN_UNPARSEABLE`, `HIGH_NULL_COLUMN`, `CONSTANT_COLUMN`, `HIGH_CARDINALITY_ID_LIKE` |
| score only | `SCHEMA_MISMATCH` |

### 4.2 Acknowledging a finding

Some findings can be **acknowledged**: the user says "I know, go ahead" and the run starts. An
acknowledged `error` stops counting towards `error_count`; a warning is displayed either way.

Acknowledgements are sent as run overrides under `validation.acknowledged`, each entry either a
bare code (`TARGET_IMBALANCE_SEVERE`) or a code and a column (`LEAKAGE_SUSPECTED:churn_flag`). A
finding about a column is satisfied by either form; the exact key the UI should send is in the
finding's `details.acknowledge` where one is offered.

Acknowledgeable findings: `LEAKAGE_SUSPECTED`, `TARGET_IMBALANCE_SEVERE`, `HIGH_NULL_COLUMN`,
`CONSTANT_COLUMN`, `HIGH_CARDINALITY_ID_LIKE`, `PII_DETECTED`, `SUPPRESSION_COLUMN_MISSING`, and
the extra-columns form of `SCHEMA_MISMATCH`. Nothing else can be waved through: a file with no
usable key, no target or too few rows is refused however the user answers.

### 4.3 What a finding carries

```
code          the machine code, one of the 19 below
severity      error | warning
message       business language, numbers already filled in
suggestion    what to do about it
column        the column it is about, when it is about one
details       the machine-readable numbers behind the message
acknowledgeable / acknowledged
```

`details` often carries an `override_path` and an `override_value`: the exact setting that fixes
the finding, so the UI can offer the change rather than describe it.

---

## 5. The validation table

Nineteen codes: the eighteen of plan §6.3 plus `SUPPRESSION_COLUMN_MISSING` (DEC-030, DEC-031).
`engine.contracts.VALIDATION_CODES` is the closed set — a `ValidationCheck` with any other code is
rejected before it can reach a user.

| Code | Severity | Mode | Triggered when |
|---|---|---|---|
| `PK_MISSING` | error | both | No primary key chosen, or the chosen name is not a column in the file |
| `PK_NOT_UNIQUE` | error | both | The key repeats, so the file holds more than one row per entity |
| `PK_NULLS` | error | both | The key is null in at least one row |
| `TARGET_MISSING` | error | train | No target chosen, or the chosen name is not a column in the file |
| `TARGET_NOT_BINARY` | error | train | A binary problem whose target has more than two distinct values |
| `TARGET_CONSTANT` | error | train | The target has one distinct value, or none at all |
| `TARGET_TOO_FEW_POSITIVES` | error | train | Positive rows < `min_positive` (default 200) |
| `TARGET_IMBALANCE_SEVERE` | warning | train | Positive rate < 1 % or > 99 % |
| `ROWS_TOO_FEW` | error | train | Rows < `min_rows` (default 1,000) |
| `LEAKAGE_SUSPECTED` | error / warning | train | A column predicts the target far too well, or is named like an outcome |
| `TIME_COLUMN_MISSING` | error | train | `split.type: time_based` and no usable time column |
| `TIME_COLUMN_UNPARSEABLE` | error | train | The time column does not read as a date |
| `HIGH_NULL_COLUMN` | warning | train | A non-key column is null in more than 60 % of rows |
| `CONSTANT_COLUMN` | warning | train | A non-key, non-target column has one distinct value |
| `HIGH_CARDINALITY_ID_LIKE` | warning | train | A non-key, non-target column is unique and complete in every row and is text- or integer-typed |
| `PII_DETECTED` | warning | both | A column looks like personal data |
| `SCHEMA_MISMATCH` | error / warning | score | The scoring file does not match the schema the model was fitted with |
| `CONSENT_COLUMN_MISSING` | error | both | A consent column is configured and the file does not carry it |
| `SUPPRESSION_COLUMN_MISSING` | warning | both | A suppression switch is on and the column it needs is absent |

`{entity}` below is the use case's own word for a row — "customer", "order", "asset" — from
`entity` in the use-case config. Numbers are rendered with thousands separators.

---

### `PK_MISSING` — error, train and score

**Triggered when** no primary key was chosen, or the chosen name is not a column in the file.

> Choose the column that identifies each {entity}.

**Suggestion**, whichever applies:

- nothing chosen, and the file has unique non-null columns: *"Pick one of: `a`, `b`, `c`."*
- nothing chosen, and no column is unique and complete: *"No column in this file is unique and
  complete, so no column can identify a {entity}."*
- a name was chosen that is not there: *"The file has no column called '{name}'."*

**What to do.** Name the column that identifies each row. A candidate must be unique and non-null
over the whole file; the engine suggests the ones whose names also look like identifiers. If no
column qualifies, the file is not one row per entity yet — aggregate it upstream first.

`details`: `selected`, `candidates` (up to five), `present`.

---

### `PK_NOT_UNIQUE` — error, train and score

**Triggered when** the chosen key repeats.

> This file has 3.4 rows per customer on average. The model needs one row per customer.

**Suggestion.** *"Combine the rows so each {entity} appears once, or upload a file that already has
one row per {entity}."*

**What to do.** One row per entity is the contract. Aggregate the repeated rows — sum, mean or
latest per key — before uploading. Automated aggregation is Phase 2 (plan §12); in Phase 1 it
happens upstream.

`details`: `rows`, `distinct_keys`, `duplicate_rows`, `rows_per_entity`, `sampled` (true when the
count came from a sampled head rather than the whole file).

---

### `PK_NULLS` — error, train and score

**Triggered when** the key is null in one or more rows.

> 412 rows have no customer identifier, so their predictions could not be joined back to your
> systems.

**Suggestion.** *"Fill in the missing '{key}' values, or remove those rows, and upload again."*

**What to do.** Either is fine. A scored row with no key cannot be matched to anything in the
customer's own systems, which is why this blocks rather than warns.

`details`: `null_count`, `null_rate`, `rows`.

---

### `TARGET_MISSING` — error, train only

**Triggered when** no target was chosen, or the chosen name is not a column in the file.

> This file has no column called 'churned'. Choose the column that records the outcome to learn.

or, when nothing was chosen and the use case defines its outcome in words:

> Choose the column that records the outcome to learn: whether the customer left within 90 days.

otherwise:

> Choose the column that records the outcome to learn.

**Suggestion** names the template's column and any column in the file that looks like the target:
*"The template for this use case calls it 'churned'. Download the template to see the expected
columns. This file has a column called 'is_churn' — is that it?"*

**What to do.** Point the run at the column holding the historical outcome. It has to be the
outcome as it was **known after** the snapshot date; a column describing the present state is
usually leakage, not a target.

`details`: `selected`, `expected` (the template's name), `candidate`, `label_source`.

---

### `TARGET_NOT_BINARY` — error, train only

**Triggered when** the problem type is binary classification and the target has more than two
distinct values. Skipped for regression.

> 'segment' has 7 different values. A yes/no model needs exactly two.

**Suggestion**, when the column holds numbers: *"This column holds numbers, so you can switch the
problem type to Regression (a number) instead."* Otherwise: *"Pick the column that records the
outcome as two values, or derive one (for example, 1 when the customer left and 0 otherwise)."*

**What to do.** Either switch the problem type (`details.override_path: problem_type`), or derive a
two-value column upstream.

`details`: `distinct_count`, `numeric`, `switch_to`, `override_path`, `override_value`,
`sample_values`.

---

### `TARGET_CONSTANT` — error, train only

**Triggered when** the target has exactly one distinct value, or is empty in every row.

> Every row has the same value in 'churned', so there is nothing for the model to tell apart.

or, when the column is empty throughout:

> 'churned' is empty in every row, so there is no outcome to learn.

**Suggestion.** *"Upload data that contains both outcomes - rows where the event happened and rows
where it did not."*

**What to do.** Export a period long enough to contain both outcomes. A file filtered to churners
only, or to active customers only, cannot train a model.

`details`: `distinct_count`, `value`, `null_count`.

---

### `TARGET_TOO_FEW_POSITIVES` — error, train only

**Triggered when** the count of positive rows is below `min_positive` (default 200). Skipped when
the target is not two-valued — `TARGET_NOT_BINARY` or `TARGET_CONSTANT` already covers that.

> Only 41 positive examples. At least 200 are needed for a reliable model.

**Suggestion.** *"Add more history, or widen the outcome window in the definition of '{target}'."*

**What to do.** Add history, or widen the window the outcome is defined over (90 days instead of
30, say). The threshold is a setting: `details.override_path` is `validation.min_positive`, and
lowering it is a deliberate choice about how much the resulting model can be trusted.

`details`: `positive_label`, `positive_count`, `negative_count`, `min_positive`, `positive_rate`,
`override_path`.

---

### `TARGET_IMBALANCE_SEVERE` — warning, train only, acknowledgeable

**Triggered when** the positive rate is below `imbalance_warn_min_rate` (1 %) or above
`imbalance_warn_max_rate` (99 %).

> Only 0.40% of rows are positive. The model can look accurate while finding almost none of them.

or, at the other end:

> 99.30% of rows are positive. The model can look accurate while finding almost none of the
> negatives.

**Suggestion.** *"Training continues. Judge it on PR-AUC and recall rather than accuracy, and
consider setting Class imbalance to 'Class weights' in Model search."*

**What to do.** Nothing is required — the run proceeds. Read PR-AUC and recall rather than
accuracy, and consider `model_search.imbalance: class_weights`, which is what
`details.override_value` offers.

`details`: `positive_rate`, `positive_count`, `negative_count`, `min_rate`, `max_rate`,
`override_path`, `override_value`.

---

### `ROWS_TOO_FEW` — error, train only

**Triggered when** the row count is below `min_rows` (default 1,000).

> This file has 240 rows. At least 1,000 are needed to train a model that generalises.

**Suggestion.** *"Upload a longer history - more {entity}s, or more snapshot dates."*

**What to do.** Upload more history. Either more entities, or the same entities at several snapshot
dates. `details.min_rows` is the threshold in force for this use case.

`details`: `rows`, `min_rows`.

---

### `LEAKAGE_SUSPECTED` — error or warning, train only, acknowledgeable

The one check with three ways to fire. It is skipped entirely when `validation.leakage_check` is
off, when the problem is not binary classification, or when the target is not two-valued. The
primary key, the target, the time column and the configured exclusions are never candidates.

**Per-column message**, for both the pattern and the AUC branch:

> Column 'cancellation_date' almost perfectly predicts the target. It may contain the answer.
> Exclude it?

| Branch | Severity | Triggered when |
|---|---|---|
| AUC | **error** | A single column scores above `leakage_auc_threshold` (default 0.98 AUC) against the target, measured on a sample of up to 50,000 rows |
| name pattern | **warning** | The column name matches `^(churn\|converted\|outcome)`, or it sits after the target in the file and matches `_date$` |
| baseline | **warning** | No single column crosses the threshold, but a 3-fold logistic-regression baseline over all candidates does |

**Suggestion**, for the AUC branch: *"If '{column}' is only known after the outcome, exclude it. If
it is genuinely available before, confirm and the run continues."* For a name match: *"'{column}'
is named like an outcome. If it is known before {the outcome definition}, confirm and the run
continues."*

The baseline branch carries its own words:

> The data predicts 'churned' almost perfectly (99.4% AUC) with a simple model, even though no
> single column does. Some combination of columns may contain the answer.

*"Check the columns listed below for anything recorded after the outcome, and exclude them in Data
preparation. Training continues either way."*

**What to do.** Ask one question about the flagged column: *was this value known before the outcome
happened?* If it was recorded afterwards — a cancellation date, a refund flag, a case-closed
timestamp — exclude it (`details.override_path: prepare.exclude_columns`, with
`override_value` already holding the new exclusion list). If it genuinely is available at scoring
time, acknowledge it with `LEAKAGE_SUSPECTED:{column}` and the run continues. A model trained on
leakage scores beautifully in evaluation and is worthless in production, which is why the AUC
branch blocks rather than warns.

`details`: `reason` (`auc` / `name_pattern` / `after_target` / `baseline`), `auc`, `threshold`,
`pattern`, `sampled_rows`, `target_position`, `column_position`, `override_path`,
`override_value`, `acknowledge`. The baseline finding adds `model`, `folds`, `features_used` and
`top_features`.

---

### `TIME_COLUMN_MISSING` — error, train only

**Triggered when** `split.type` is `time_based` and no time column is configured, or the configured
name is not a column in the file. Skipped for every other split type.

> This use case splits the data by date, so it needs the column that says when each row was taken.

or, when a name was configured and is not there:

> This file has no column called 'snapshot_date', and the data is split by date.

**Suggestion.** *"Choose one of: `a`, `b`, `c`."* — the columns whose names look like dates — or,
when there are none: *"Add a snapshot date column, or change Split type to Random (stratified) in
Data split."*

**What to do.** A time-based split trains on the past and tests on the future, which needs a column
saying when each row was taken. Either supply it, or change `split.type`, accepting that a random
split will flatter the model if the data has a time trend.

`details`: `selected`, `candidates`, `split_type`, `override_path`.

---

### `TIME_COLUMN_UNPARSEABLE` — error, train only

**Triggered when** the configured time column is not already a date or datetime and fewer than 95 %
of its non-null values parse as dates.

> 'snapshot_date' does not read as a date, so the data cannot be split by time.

**Suggestion.** *"Use a standard date format such as 2026-08-01, or choose a different time column.
1,204 of 50,000 values could not be read as a date."*

**What to do.** Use one unambiguous format throughout, ISO (`2026-08-01`) for preference. Mixed
formats, free text and placeholder strings such as `N/A` in a date column are the usual causes.

`details`: `inferred_type`, `parse_rate`, `checked_rows`, `sample_values`.

---

### `HIGH_NULL_COLUMN` — warning, train only, acknowledgeable

**Triggered when** a column other than the primary key is null in more than
`high_null_column_rate` (60 %) of rows. One finding per column.

> 'last_complaint_reason' is empty in 87% of rows.

**Suggestion.** *"It will be dropped before training. Keep it by clearing it from the exclusions in
Data preparation."*

**What to do.** Usually nothing. If the emptiness is meaningful — a column that is only filled for
customers who did something — keep it, and the missingness itself becomes a signal.

`details`: `null_count`, `null_rate`, `threshold`, `will_be_dropped`.

---

### `CONSTANT_COLUMN` — warning, train only, acknowledgeable

**Triggered when** a column other than the key and the target has exactly one distinct value.

> 'country' has the same value in every row, so it cannot help the model.

**Suggestion.** *"It will be dropped before training."*

**What to do.** Nothing. A column that never varies carries no information. If it was meant to vary,
the file is probably filtered more narrowly than intended.

`details`: `value`, `will_be_dropped`.

---

### `HIGH_CARDINALITY_ID_LIKE` — warning, train only, acknowledgeable

**Triggered when** a column other than the key and the target is **unique and non-null in every
row**, is string-, text- or integer-typed, and the file has at least 20 rows. The test is on the
values, not on the name: a column called `account_reference` that repeats is not flagged, and a
column called `x` that never repeats is. (The name pattern `(^id$|_id$|^id_|_key$|customer|cust)`
exists, but it ranks the *candidates `PK_MISSING` suggests*, not this check.)

> 'account_reference' has a different value in almost every row, so it looks like an identifier
> rather than a feature.

**Suggestion.** *"It will be dropped before training. Keep it by clearing it from the exclusions in
Data preparation."*

**What to do.** Usually nothing. An identifier memorised by a model is the classic way to get a
perfect score on the training data and nothing useful afterwards. Keep it only if it is genuinely a
feature — a postcode, say, which is high-cardinality but real.

`details`: `distinct_count`, `rows`, `distinct_ratio`, `threshold`, `will_be_dropped`.

---

### `PII_DETECTED` — warning, train and score, acknowledgeable

**Triggered when** a column's values or name match the detectors for email addresses, phone
numbers, PAN numbers, Aadhaar numbers or personal names. One finding per column.

> 'contact_email' looks like it contains email addresses.

**Suggestion** follows `prepare.pii_handling`:

| `pii_handling` | Suggestion |
|---|---|
| `redact` (default) | "Its values will be replaced with [REDACTED] before training." |
| `drop_columns` | "The column will be dropped before training." |
| `keep` | "It will be used as it is. Change PII handling in Data preparation to redact or drop it." |

**What to do.** Decide whether the column should be in the file at all. Contact details are needed
for the action list, not for the model, and the default handling reflects that.

`details`: `pii_kinds`, `handling`. **The finding never records a matched value** — not the address,
not the number — only the kind and the column name (plan §13.7).

---

### `SCHEMA_MISMATCH` — error or warning, score only

**Triggered when** the scoring file disagrees with the `schema.json` saved with the model. Skipped
when no schema is supplied.

**Error**, when a required column is missing or a type has changed incompatibly:

> This file does not match the data the model was trained on. Missing: tenure_months, plan_type.
> Changed type: monthly_charges (was float, now string).

*"Upload a file with the same columns as the training data. The template for this use case lists
them."*

**Warning** (acknowledgeable), when the only difference is extra columns:

> This file has 3 column(s) the model was not trained on: campaign_id, region_v2, notes.

*"They will be ignored while scoring."*

Types are compared by group, not by name: integer, float and boolean are interchangeable, as are
date and datetime, and string and text. Only a move between groups counts as a change. The target
column is not required in a scoring file, and the primary key is never reported as extra.

**What to do.** Produce the scoring file the same way the training file was produced. The template
lists the expected columns, and `details.expected_columns` is the exact fitted list in the exact
fitted order.

`details`: `missing`, `extra`, `type_changed` (each with `name`, `expected`, `actual`),
`model_version_id`, `expected_columns`.

---

### `CONSENT_COLUMN_MISSING` — error, train and score

**Triggered when** `governance.consent_column` is configured and the file has no such column.
Skipped when no consent column is configured.

> This use case only contacts customers who have given consent, and the file has no 'marketing_ok'
> column.

**Suggestion.** *"Add a 'marketing_ok' column holding true or false, or clear Consent column in
Governance & privacy."*

**What to do.** Supply the column, or change the configuration deliberately. This is an error and
not a warning because a use case that contacts people has to be able to tell who agreed to be
contacted; the engine will not guess consent, and it will not proceed without it.

`details`: `consent_column`, `override_path`.

---

### `SUPPRESSION_COLUMN_MISSING` — warning, train and score, acknowledgeable

**Triggered when** a suppression switch is on (`actions.suppression.suppress_opted_out` or
`suppress_recently_contacted`) and the column it names is not in the file (DEC-030). One finding
per missing column.

> 'opted_out' is not in this file, so opted-out customers cannot be suppressed.

> 'last_contacted_at' is not in this file, so recently contacted customers cannot be suppressed.

**Suggestion.** *"Add the column, or turn the matching switch off in Actions & output."*

**What to do.** Read this one carefully before acknowledging it. The run continues and the action
list is produced — but nobody is filtered out of it, so a customer who opted out can appear in a
campaign. Either add the column, or turn the switch off (`details.override_value: false`) so that
the configuration says what is actually happening.

`details`: `suppression_column`, `role`, `override_path`, `override_value`.

---

## 6. What the report quotes, and what the log never does

The validation report is written for the person who owns the data, and it can quote from it: a
sample of the offending values appears in `details.sample_values` for `TARGET_NOT_BINARY` and
`TIME_COLUMN_UNPARSEABLE`, and the single repeated value in `details.value` for `TARGET_CONSTANT`
and `CONSTANT_COLUMN`. That is deliberate — being told "these three values could not be read as a
date" is what makes the message actionable. `PII_DETECTED` is the exception: it reports the kind
and never the value.

**Nothing from the file reaches the engine's log.** Stage timings, row counts and column names are
logged at INFO; cell values never are, on any path, including failure paths (plan §13.7, enforced by
`tests/unit/test_logging_audit.py`).

---

## 7. Templates

For every use case, `templates/<use_case>_template.csv` contains the header row and five example
rows, and `templates/<use_case>_template_README.md` describes each column in one line. The UI
exposes these as "Download template" next to the upload control.

| File | What it is |
|---|---|
| `templates/<use_case>_template.csv` | the header row plus five example rows |
| `templates/<use_case>_template_README.md` | one line per column: its role, its type and what it means |

Both files are **generated from the use-case YAML** (`make generate`, `python -m
scripts.gen_templates`) and committed, so a clean checkout can hand a client a template without
running anything (DEC-014). `make lint` re-runs the generator with `--check`, so a template that
has drifted from its config fails the build rather than reaching a customer.

The API serves the same two files at `GET /use-cases/{id}/template.csv` and
`GET /use-cases/{id}/template_README.md` (DEC-024), byte-for-byte identical to the committed
copies.

Each template column carries a **role** — `primary_key | time | feature | target | consent |
contact` (DEC-025) — which is what lets the README name the required columns, and what makes a
disagreement between a template and its use-case config (a target column under another name, two
primary keys) a config-load error rather than a surprise at upload time. It is also what
`TARGET_MISSING` reads when it says "the template for this use case calls it 'churned'".

**The example rows are illustrative column values so that the shape is obvious.** They are not
measurements, they are not real customers, and nothing derived from them is ever shown as a result
(plan §13.3).

---

## 8. Schema memory

When a model is trained, the exact feature schema is saved with it as `schema.json`:

```
use_case_id, model_version_id       which model this schema belongs to
primary_key, target, problem_type   the roles as they were at fit time
columns[]                           name, inferred_type, required, nullable,
                                    categories (when few enough), minimum, maximum
                                    — in the exact order used at fit time
row_count_at_fit, created_at
```

Scoring validates the new file against it and reports every missing, extra or type-mismatched
column **by name** (`SCHEMA_MISMATCH` above). The schema belongs to the model version, not to the
use case: rescoring against an older model checks against the columns *that* model was fitted with,
which is why `details.model_version_id` is in every mismatch finding.

The stored `categories`, `minimum` and `maximum` are the fitted levels and ranges. They are what
lets the engine tell a genuinely new category from a typo later, and they are the baseline the
drift report compares a scoring file against.

---

## 9. What a generative use case uploads instead (Phase 3a)

Everything above describes a **flat table with one row per entity**, which is what a predictive use
case is trained and scored on. A generative use case is not trained on a table at all, so it
uploads two different things and neither goes through the validation table above. Both are read by
`engine/generative/`, and the codes below are the literal ones that module raises.

### 9.1 The knowledge base

The documents the assistant answers **from**, and the only thing it is allowed to answer from.

- Accepted types: **PDF, DOCX, Markdown, plain text** (`generative.knowledge_base.accepted_types`).
  Anything else is refused per file, not per upload.
- At most **200 documents** and **200 MB in total** (`max_docs`, `max_mb`). The megabyte limit is
  the whole knowledge base rather than one file, and it is checked before a byte is parsed.
- **Headings matter more than formatting.** Chunking never crosses a heading, so a heading is what
  a citation names when it says *where* in a document an answer came from, and it is embedded in
  front of the passage because the words a question uses are very often in the heading and nowhere
  in the prose beneath it (DEC-217). A document with no headings still indexes; it is recorded with
  `DOCUMENT_NO_HEADINGS` and every citation into it can only name the file.

| Code | When | Message |
|---|---|---|
| `DOCUMENT_TYPE_UNSUPPORTED` | Extension is not in `accepted_types` | "{name} is a {extension} file, which this knowledge base does not accept." |
| `DOCUMENT_EMPTY` | The parser found no text | "{name} has no text in it, so there is nothing to index." |
| `DOCUMENT_UNREADABLE` | Corrupt, or password-protected | "{name} could not be read; the file may be corrupt or password-protected." |
| `KNOWLEDGE_BASE_TOO_LARGE` | Past `max_docs` or `max_mb` | "This knowledge base would hold {documents} documents and {megabytes} MB." |
| `INDEX_EMPTY` | **Every** document failed | "The {index_id} index holds no chunks, so no question can be answered from it." |

**One bad document does not fail the build.** A corrupt PDF among two hundred costs that PDF and
nothing else: the failure is recorded against its own entry in the index manifest with the code
that explains it, and the build carries on. Only a build where *every* document failed raises, and
it raises `INDEX_EMPTY`, because an index with no chunks can answer nothing.

**PII in a knowledge document is warned about, never redacted.** This is the opposite of the rule
for the uploaded table, where `PII_DETECTED` is a finding about a customer's data. These are the
client's **own published documents**, and the support address or escalation number printed in one is
frequently the very thing a question is about - redacting it would damage the answer that address is
the point of. So the manifest carries `PII_IN_DOCS` naming the kinds found, never a value, and the
text is indexed exactly as written. The difference is whose data it is (DEC-216). A document that
genuinely should not have been published is the operator's to withdraw; the engine tells them, and
does not decide it for them.

### 9.2 The reference set

An optional Q&A file the index is **graded** against, and the only reason a faithfulness or
correctness number exists. Without one an index still answers questions; it just has no score.

| Column | Default name | Required | What it holds |
|---|---|---|---|
| Question | `question` | yes | What a customer would ask. Configurable via `generative.reference_set.question_column`. |
| Reference answer | `reference_answer` | yes | The answer a correct reply is graded against. Configurable. |
| Refusal flag | `expect_refusal` | yes | True where the documents genuinely do not answer the question. Configurable. |
| Source document | `source_doc` | yes | Which document should have been retrieved. **Not configurable.** |

`source_doc` is fixed where the other three are renameable, because it is the evaluation's own
bookkeeping rather than a client-facing field: the other three are things a client might reasonably
call something else, and this one exists only so retrieval can be graded at all.

Two things about it are worth stating plainly, because both have caused real bugs:

- It is matched on the document's **stem**, so `faq_billing.md`, `faq_billing.pdf` and `faq_billing`
  all name the same document. A reference set written against one export of a corpus still grades a
  differently-formatted export of it.
- A row marked `expect_refusal` is **not** graded on retrieval, whether or not its `source_doc` cell
  happens to be filled in. It has no document it ought to have found, and folding it into the
  retrieval hit rate would measure retrieval against rows nobody wanted retrieval for.

| Code | HTTP | When | Message |
|---|---|---|---|
| `REFERENCE_SET_INVALID` | 422 | A required column is absent | "The reference set is missing the {column} column." |

The header is checked **on the request**, before the build is queued, so a reference set the grader
would reject is refused immediately rather than twenty seconds later inside a status document the
caller has to go and poll for.

### 9.3 What is never uploaded

The hybrid capabilities - root-cause summaries and campaign copy - upload **nothing at all**. They
read a finished predictive run's own artefacts, which already passed everything above. That is what
"hybrid" means here, and the dependency runs one way (DEC-210): the predictive engine produces the
numbers, the generative engine explains or acts on them, and no generative module is imported by
`engine.pipeline` or anything under `engine.stages`.

Complaint text is the one customer-written field either of them reads, and it is **always redacted**
before it reaches a prompt - the mirror of the knowledge-base rule above, and for the same reason
stated the other way round: those are a customer's own words, never published, and they reach a
model only as evidence.

## 10. Raw tables

Everything above describes the **prepared** file: one flat table, one row per entity, our column
names, the target already in it. That is what the engine trains and scores on, and it is what a
client with a data team usually sends.

A client without one sends what their systems actually hold — a customer master, a billing table
with one row per invoice, a complaints table with one row per ticket — and no target column at all,
because churn is a definition their business makes rather than a field their billing system stores.
Those are **raw tables**, and onboarding (plan §4–7, [`engine/onboarding/`](../engine/onboarding/))
turns a set of them into exactly the prepared file this document specifies. The contract above is
not relaxed for them; it is what they are built *up to*, and the assembled dataset is put through
the same validation table before anything is trained on it.

What a raw table may look like is therefore much weaker than section 1, and worth stating.

### What a raw table must have

Each uploaded table is given a **role** from [`configs/roles.yaml`](../configs/roles.yaml). There
are two kinds, and the requirement is different for each.

| Kind | Roles | Required columns |
|---|---|---|
| `entity` | `entity` | `entity_key` |
| `event` | `bills`, `payments`, `complaints`, `usage`, `campaign_events`, `orders`, `plan_changes`, `activity`, `other_event` | `entity_key`, `event_time` |

- **Exactly one** table may be the `entity` table: one row per entity, with its attributes. Two is
  `MULTIPLE_ENTITY_SOURCES`, none is `NO_ENTITY_SOURCE`, and duplicated ids in it are
  `ENTITY_DUPLICATE_KEYS` — all three errors.
- **Every event table needs a key and a date and nothing else.** Every other column a role mentions
  — `amount` on `bills`, `severity` on `complaints`, `data_mb` on `usage` — is role-typical and
  optional, used as an alias hint when mapping and as something to aggregate when it is present.
- The required names above are *standard* names, not the client's. `entity_key` and `event_time` are
  settled from the shape of the data (which column is unique and rarely null, which column parses as
  a date), never from an alias list, because no alias list covers `CUST_ID`, `ACCT_NO` and `MSISDN`.
- No target column, on any table. The outcome is derived (plan §5.2): from an existing column, from
  the presence or absence of events in a window after each snapshot date, or from a threshold on
  them.
- Same formats and the same size limits as section 1: CSV (UTF-8, header row) or Parquet, and the
  ingest refusals in section 3 apply per file. Two further limits are onboarding's own, both from
  `onboarding.limits` in [`configs/engine.yaml`](../configs/engine.yaml): `SOURCE_TOO_LARGE` above
  `max_source_rows` (default 50,000,000) and `TOO_MANY_SOURCES` above `max_sources` (default 10).

### What a raw table may have that a prepared file may not

- **Any column names, in any case, with any punctuation.** A mapping records what each one means.
- **Any date format**, including one that reads two ways. `03/04/2024` is `DATE_FORMAT_AMBIGUOUS`,
  an error, answered by pinning an explicit format on the mapping rather than by the engine guessing.
- **Any value vocabulary.** `PRE`/`PPD`/`POST`, `Y`/`N`, `1`/`0`, `allowed`/`blocked` are all read
  through a value map. A value the map does not cover is `VALUE_UNMAPPED`, a warning listing it.
- **Flags that mean the opposite of ours.** `DND_FLAG` and `marketing_opt_in` are one fact stated in
  two directions; the mapping carries a `negate` transform and the values are flipped on the way in.
- **Keys written differently from table to table** — leading zeros, stray spaces, a difference of
  case. Below `onboarding` thresholds this is `JOIN_KEY_COVERAGE_LOW`, and when one transform would
  fix it, `KEY_FORMAT_MISMATCH` names that transform.
- **Columns nobody needs.** An unmapped column is dropped: the mapped table is built from the
  mapping rather than by renaming the input, so there is no path by which it reaches the dataset.
- **Many rows per entity** — in an event table, where it is the point, and where every one of those
  rows carries its own date. In the `entity` table it is `ENTITY_DUPLICATE_KEYS` until the mapping
  collapses it (the `dedupe` transform keeps the latest row per entity by a column you name) or the
  client sends one row each.

### What comes out

One row per entity per snapshot date: `entity_key`, `snapshot_date`, the mapped attributes, the
features computed strictly as of that date, and the derived target. That is a section 1 file — the
primary key is `(entity_key, snapshot_date)` for a periodic build and `entity_key` for a single one
— and it is validated as one.

[`ONBOARDING.md`](ONBOARDING.md) is the guide a business user reads for how the roles, the mapping,
the features, the outcome and the snapshot dates are decided, and what each build-report check means.
