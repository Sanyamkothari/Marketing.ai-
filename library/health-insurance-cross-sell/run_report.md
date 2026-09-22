# Health Insurance Cross-Sell — run report

Every number below came out of one real training run. This is the largest file in the library —
381,109 rows.

```
use case      insurance-cross-sell   (library/configs/use_cases/insurance_cross_sell.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv      (381,109 rows, 12 columns — a copy of the download)
primary key   id
target        Response, positive label 1
run           r_20260922_2345d3f1
wall clock    1245.6 s   (of the 30-minute default budget), 77 models
```

> **Licence:** reported as GNU GPL v2 and **not verified** — kaggle.com was unreachable from the
> environment this library was built in. Read [`LICENSE.txt`](LICENSE.txt) before this dataset
> appears in anything customer-facing.

## Validation

**Passed: 0 errors, 0 warnings.** The report is empty.

Four checks that could plausibly have fired and did not:

* **`TARGET_IMBALANCE_SEVERE`** needs below 1 % or above 99 %. This file is 12.26 % positive.
* **`CONSTANT_COLUMN`** needs exactly one distinct value. `Driving_License` is 1 in 99.8 % of rows
  — nearly constant, not constant — so it survives into training and contributes nothing
  measurable. It does not appear on the importance chart below at all.
* **`HIGH_CARDINALITY_ID_LIKE`** would flag a non-key column unique in every row. `id` is the key
  and is never a candidate; `Annual_Premium` has 41,561 distinct values across 381,109 rows, so it
  repeats and is not flagged.
* **`PII_DETECTED`** found nothing. `Gender` and `Age` are attributes; nothing matches the email,
  phone, PAN, Aadhaar or name detectors.

`Region_Code` and `Policy_Sales_Channel` are category codes stored as floats. No check exists for
that and none could — a float column of small integers is indistinguishable from a measurement
without knowing the domain. See [`README.md`](README.md).

## Preparation and split

| | |
|---|---|
| Rows in → out | 381,109 → 381,109 (nothing dropped) |
| Columns in → out | 12 → 12 |
| Columns dropped | none |
| Features handed to training | **10** (everything but `id` and `Response`) |
| Split | `random_stratified` — train 266,777 / validation 57,166 / test 57,166 |
| Positive rate | 12.26 % in every part |

## Leaderboard — top 3 of 77

Ranked on the **validation** score; `test` is reported, never ranked on. A `/T6` suffix is a
hyperparameter tuning trial.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `XGBoost_BAG_L1/T6` | XGBoost | 0.8575 | 0.8579 | 32.4 s |
| 2 | `XGBoost_BAG_L1/T10` | XGBoost | 0.8575 | 0.8579 | 23.2 s |
| 3 | `XGBoost_BAG_L1/T4` | XGBoost | 0.8574 | 0.8578 | 26.6 s |

**Winner:** Ensemble (XGBoost + LightGBM + Logistic Regression), 10 features, 266,777 training
rows. The top three are separated by 0.0001 on validation — on this data the choice of model
barely matters, which is itself worth knowing before anyone asks for a bigger budget.

## Test metrics

Measured once, on the 57,166-row hold-out, at the auto-chosen threshold **0.2353**.

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.8581** |
| PR-AUC | 0.3613 |
| F1 | 0.4623 |
| Recall | 0.7659 |
| Precision | 0.3311 |
| Accuracy | 0.7817 |
| Specificity | 0.7839 |
| Brier score | 0.0866 |

The gap between ROC-AUC (0.858) and PR-AUC (0.361) is the imbalance talking: at a 12 % base rate,
ranking well and being *precise* are different achievements. PR-AUC is the number to quote.

## Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.8581 | 0.8380 | **+0.0201** |
| PR-AUC | 0.3613 | 0.3164 | +0.0449 |
| F1 | 0.4623 | 0.4331 | +0.0292 |
| Recall | 0.7659 | 0.6890 | +0.0769 |
| Precision | 0.3311 | 0.3158 | +0.0153 |

## Decile lift

Base rate 12.26 %. Decile 1 holds the 5,717 highest-scoring policyholders.

| Decile | Rows | Interested | Rate | Lift |
|---|---|---|---|---|
| **D1** | 5,717 | 2,260 | **39.53 %** | **3.23×** |
| D2 | 5,717 | 1,826 | 31.94 % | 2.61× |
| D3 | 5,717 | 1,495 | 26.15 % | 2.13× |

D1→D10: 3.23, 2.61, 2.13, 1.30, 0.64, 0.08, 0.01, 0.00, 0.00, 0.00. D1 captures **32.3 %** of all
interested policyholders, and the top three deciles between them capture **79.7 %**.

**The bottom half is the striking part.** Deciles 6 to 10 contain almost no interested
policyholders at all — lift 0.08 and below, effectively zero. Half the book can be left out of the
campaign entirely at almost no cost in missed sales.

## Top features

Permutation importance on the test split. The chart has **nine** rows, not ten — see below.

| # | Feature | Share |
|---|---|---|
| 1 | **`Previously_Insured`** | **55.2 %** |
| 2 | `Vehicle_Damage` | 18.3 % |
| 3 | `Age` | 11.5 % |
| 4 | `Policy_Sales_Channel` | 8.9 % |
| 5 | `Vehicle_Age` | 3.4 % |
| 6 | `Region_Code` | 1.7 % |
| 7 | `Annual_Premium` | 0.9 % |
| 8 | `Gender` | 0.1 % |
| 9 | `Vintage` | 0.0 % (importance −0.0002) |

**There is no tenth row.** Ten features went into training and `feature_importance.json` came back
with nine: `Driving_License` — the column that is 1 in 99.8 % of rows — did not make the chart at
all. Permutation importance measures how much shuffling a column hurts the score, and shuffling a
column that is almost always the same value does nothing measurable. The engine reported what it
measured rather than padding the list to ten. `Vintage` is on the chart but with a negative
importance, which means the same thing: no signal.

## What a business user should take from this

**Stop calling people who already have the policy.** `Previously_Insured` carries 55 % of the
model on its own: a policyholder who already holds vehicle cover is essentially never interested in
being sold it again. That is not a machine-learning insight, it is a list-hygiene insight, and the
model found it first.

**Then ask whether their car has been damaged.** `Vehicle_Damage` is the second signal at 18 %.
Someone who has had a claim-worthy incident and no cover is the person who says yes. Those two
columns together are almost three quarters of the model, and both are already in every insurer's
CRM.

**Half the book is not worth a call.** Deciles 6 to 10 — 28,585 policyholders in this hold-out —
contain almost no interested customers. Working the top three deciles reaches 79.7 % of the
opportunity at 30 % of the contact cost.

**One caution to say out loud.** `Response` records *stated interest*, not a signed policy. The
model ranks people by willingness to hear more. Treating the decile chart as a revenue forecast
would overstate it; treating it as a call-list ordering is exactly right.

## Reproducing it

```bash
python library/health-insurance-cross-sell/fetch.py
python -m library.run_engine \
  --dataset health-insurance-cross-sell --use-case insurance-cross-sell --config-root library/configs \
  --csv library/health-insurance-cross-sell/data/prepared.csv \
  --primary-key id --target Response
```

Artefacts: `library/.runs/health-insurance-cross-sell/r_20260922_2345d3f1.results.json`.
Note that the explanation stage samples: `EXPLAIN_MAX_ROWS` is 5,000, so `row_explanations.parquet`
covers a seeded 5,000-row sample of the 57,166-row test split rather than all of it.
