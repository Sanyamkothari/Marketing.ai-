# Data Doctor: feature engineering as an AI tool (Step 7)

Status: approved by the owner to follow root-cause steps 2-6 (`DATA_DOCTOR_ROOT_CAUSES.md`). Builds on one
materialisation (`engine/doctor/materialize.py`), one decisions model and current tables.

## Why

Most of a model's accuracy on order and event histories comes from behaviour features (recency, frequency, spend,
trends). Today the Data Doctor gets them only when the AI hand-writes a SQL query - flexible, but the "only data before
the snapshot" rule then depends on the model getting it right. A point-in-time feature library already exists
(`engine/onboarding/features.py`: count, sum, mean, min, max, std, nunique, latest, first, days since first/last,
exists, ratio, each with an optional window and a filter; snapshots in `snapshots.py`; the leak probe in `build.py`), but
the Data Doctor's AI cannot call it.

## What to build

1. **Tool `propose_features`** (registry, change kind; argument model generated into the prompts per F7):
   `{table, entity_key, time_column, snapshot: decisions.as_of | explicit, features: [{name, agg, column?, window_days?,
   where?}] | preset: "rfm" | "rfm_plus"}`. It compiles to the onboarding feature specs and runs through
   `materialize`, so the trial equals the run.
2. **Presets** (the AI may start from one and edit): `rfm` = days since last order, orders in 90/365 days, spend in
   90/365 days, average order value; `rfm_plus` adds return rate, distinct products, days between orders (mean),
   tenure, spend trend (last 90 days vs the 90 before), active months.
3. **Guarantees in code, not in the prompt:** every feature is computed from rows dated on or before the snapshot (the
   compiled SQL carries the point-in-time clause; the leak probe runs in every scope); features never read the outcome
   column; a feature that is constant or almost always blank is reported, not silently kept.
4. **What the person sees:** one plain list to approve - "Spend in the last 90 days, orders in the last year, days since
   the last order, ..." - with each feature's fill rate and a sample, and the snapshot it is measured at.
5. **Not in scope:** model-specific transforms (scaling, one-hot, imputation for the model) - AutoML does those.

## Tests

Point-in-time (a feature never changes when rows after the snapshot are added or removed); preset compile = SQL result
on a fixture; leak probe fires on a hand-written feature that reads the future; the Online Retail replay builds the
training table with `rfm_plus` and the outcome, and reaches a readiness verdict.
