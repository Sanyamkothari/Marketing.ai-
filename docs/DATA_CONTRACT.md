# Data contract

The upload must be a **flat table with exactly one row per entity** (customer, order, asset). This
document restates plan §4; the plan is the normative version.

This is the M1 stub: it fixes the shape of the upload and points at the templates. The validation
table — every machine code, its message and its suggestion, including the new `SUPPRESSION_COLUMN_MISSING`
warning of DEC-030 — is written here by M2, when the checks that raise those codes exist.

---

## Required

- A **primary key** column: unique, non-null. Never used as a feature.
- For training: a **target** column. Binary (two distinct values, any labels such as 0/1, yes/no,
  true/false, churned/active) or numeric for regression.
- At least `min_rows` rows (default 1,000) and `min_positive` positive examples (default 200) for
  classification.
- CSV (UTF-8, comma separated, header row) or Parquet. Max 2 GB in Phase 1.

## Strongly recommended

- A **snapshot/date column** (any column whose name matches `date|time|month|week|day|_ts$|_at$`, or
  picked by the user). Required when the use-case config sets `split.type: time_based`.
- Features that describe the entity **as of the snapshot date**, i.e. before the outcome happened.

## Templates

For every use case, `templates/<use_case>_template.csv` contains the header row, five example rows,
and a second file `<use_case>_template_README.md` describing each column in one line. The UI exposes
these as "Download template" next to the upload control. Generate them from the use-case config, do
not hand-write them.

The templates live in [`templates/`](../templates) at the repository root, one pair per use case:

| File | What it is |
|---|---|
| `templates/<use_case>_template.csv` | the header row plus five example rows |
| `templates/<use_case>_template_README.md` | one line per column: its role, its type and what it means |

Both files are generated from the use-case YAML (`make generate`, `python -m scripts.gen_templates`)
and committed, so a clean checkout can hand a client a template without running anything (DEC-014).
The example rows in the CSV are illustrative column values so the shape is obvious; they are not
measurements, and nothing derived from them is ever shown as a result.

The API serves the same two files at `GET /use-cases/{id}/template.csv` and
`GET /use-cases/{id}/template_README.md` (DEC-024), byte-for-byte identical to the committed copies.

Each template column carries a **role** — `primary_key | time | feature | target | consent | contact`
(DEC-025) — which is what lets the README name the required columns and what makes a disagreement
between a template and its use-case config (a target column under another name, two primary keys) a
config-load error rather than a surprise at upload time.

## Schema memory

When a model is trained, the exact feature schema (column names, inferred types, category levels) is
saved with the model (`schema.json`). Scoring validates the new file against it and reports every
missing, extra or type-mismatched column by name.
