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

## Step 7b - the owner's feature toolkit (2026-10-27)

Mapped from the owner's feature-engineering guide; 7b follows Step 7 on the same tool.

| Concept | Decision |
|---|---|
| Ratios | Presets: dormancy index = recency_days / tenure_days; return rate = returned items / items; spend velocity = spend / tenure months; basket depth = items / orders. Division by zero gives blank, reported. |
| RFM | Step 7 `rfm` preset (recency; orders in N days; mean / median / max order value). |
| Velocity / trend | activity in the last 30 days / (activity in the prior 90 days / 3), for spend and for orders; > 1 growing, < 1 decaying. |
| Encodings | Left to AutoML (one-hot, categories). Ordinal: the existing `map_values` step, proposed by the AI for known ordered labels. **Target encoding is not done in the Data Doctor**: it leaks the outcome unless computed out-of-fold inside training. |
| Interactions | Only business-meaningful products or flags (e.g. discount-sensitive x high historical spend), proposed with a reason, approved by the person. Trees learn the rest. |
| Binning | Business buckets from the use case (win-back: 0-30 days active, 31-90 at risk, 90+ lapsed), proposed by the AI, approved by the person; the raw number is kept beside the bucket. |
| Leakage | Enforced in code (Step 7): point-in-time to the snapshot, the leak probe in every scope, features never read the outcome. |

## The agent's knowledge (Step 7b)

The agent reads `configs/doctor/guides/features.md` before proposing features, through a read-only tool
`read_guide(name)` (guides live in `configs/doctor/guides/`, each loaded on demand so the system prompt stays under its
word limit). The prompt gains one line: "Before proposing features, read_guide('features')." Later guides (joins,
outcomes, time) and approved lessons (experience memory, AI-first design A5) use the same tool.

### Built (Step 7b)

* `read_guide(name, section?)` is an action of the registry (review and chat; `engine/doctor/guides.py`): the guide's
  `## Rules` plus the section, or all of it. An unknown name or section is refused with the valid ones. A guide is
  carried once in a turn's results (a second read of the same name and section answers "already read above") and is
  not sent again in a later turn. Both prompts render `configs/doctor/guides/index.md` (`{{ guide_index }}`) and one
  line telling the model to read a job's guide first. The stable parts (instructions, action contract, index) are the
  system text; the case state and the turn's results are the user text, so the system text is identical across calls.
* Preset `rfm_ratios` (tenure, dormancy index, return rate, spend per month, basket depth, `spend_velocity_30d`,
  `orders_velocity_30d`, `recency_bucket` beside the raw days) and the same names as custom aggregations
  (`agg: "dormancy_index"` ...), taking their columns from `amount_column` and `order_column`. A division by nothing is
  blank. Buckets: 0-30 days active, 31-90 at risk, 91 and more lapsed.
