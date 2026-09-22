# UCI Default of Credit Card Clients — run report

Every number below came out of one real training run.

```
use case      card-default-propensity   (library/configs/use_cases/card_default_propensity.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv         (30,000 rows, 25 columns)
primary key   ID
target        default_payment_next_month, positive label 1
run           r_20260922_e8a6e3a1
wall clock    888.6 s   (of the 30-minute default budget)
```

## Validation

**Passed: 0 errors, 0 warnings.** The report is empty.

Three checks that could plausibly have fired and did not, with the reason:

* **`TARGET_IMBALANCE_SEVERE`** needs a positive rate below 1 % or above 99 %. This file is 22.1 %
  positive — well balanced for a default-risk dataset.
* **`HIGH_CARDINALITY_ID_LIKE`** would flag a column that is unique in every row. `ID` is, but the
  primary key is never a candidate; no feature column is unique.
* **`PII_DETECTED`** found nothing. `SEX`, `EDUCATION`, `MARRIAGE` and `AGE` are integer-coded
  attributes; none matches the email, phone, PAN, Aadhaar or name detectors.

The undocumented category codes (`EDUCATION` 0/5/6, `MARRIAGE` 0, `PAY_*` −2 and 0) produce no
finding, and there is no check that would: the engine has no way to know what the documentation
says. They are noted in [`README.md`](README.md) instead.

## Preparation and split

| | |
|---|---|
| Rows in → out | 30,000 → 30,000 (nothing dropped) |
| Columns in → out | 25 → 25 |
| Columns dropped | none |
| Features handed to training | **23** (everything but `ID` and the target) |
| Split | `random_stratified` — train 21,000 / validation 4,500 / test 4,500 |
| Positive rate | 22.12 % train, 22.11 % validation, 22.11 % test |

## Leaderboard — top 3 of 152

Ranked on the **validation** score; `test` is reported, never ranked on. The default
`tuning_trials: 50` fits many variants of each family — the `/T23` suffix is a tuning trial — so a
fifteen-minute search produced 152 candidates.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `LightGBM_BAG_L1/T23` | LightGBM | 0.7828 | 0.7947 | 3.2 s |
| 2 | `LightGBM_BAG_L1/T18` | LightGBM | 0.7828 | 0.7948 | 3.1 s |
| 3 | `LightGBM_BAG_L1/T43` | LightGBM | 0.7827 | 0.7948 | 3.4 s |

**Winner:** Ensemble (LightGBM + XGBoost + Random Forest), 23 features, 21,000 training rows.

## Test metrics

Measured once, on the 4,500-row hold-out, at the auto-chosen threshold **0.3077**.

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.7957** |
| PR-AUC | 0.5485 |
| F1 | 0.5562 |
| Recall | 0.5598 |
| Precision | 0.5526 |
| Accuracy | 0.8024 |
| Specificity | 0.8713 |
| Brier score | 0.1318 |

## Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| **ROC-AUC** | 0.7957 | 0.7279 | **+0.0678** |
| PR-AUC | 0.5485 | 0.4811 | +0.0674 |
| F1 | 0.5562 | 0.5172 | +0.0390 |
| Recall | 0.5598 | 0.4834 | +0.0764 |
| Precision | 0.5526 | 0.5561 | −0.0035 |

**This is the widest gap in the library.** +0.0678 ROC-AUC over a logistic regression is a real
gradient-boosting win, and it is easy to see why: the repayment-status codes interact
non-linearly — being two months late on the most recent bill means something quite different
depending on the previous five months — and a linear model cannot express that.

## Decile lift

Base rate 22.11 %. Decile 1 holds the 450 highest-scoring accounts.

| Decile | Rows | Defaults | Rate | Lift |
|---|---|---|---|---|
| **D1** | 450 | 323 | **71.78 %** | **3.25×** |
| D2 | 450 | 198 | 44.00 % | 1.99× |
| D3 | 450 | 125 | 27.78 % | 1.26× |

D1→D10: 3.25, 1.99, 1.26, 0.83, 0.78, 0.67, 0.49, 0.29, 0.25, 0.18. D1 captures **32.5 %** of all
defaults and the top three capture **64.9 %**. The curve falls monotonically — a well-behaved
ranking with no inversions, which is not true of every dataset in this library.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | **`PAY_0`** | **50.2 %** |
| 2 | `LIMIT_BAL` | 14.8 % |
| 3 | `PAY_2` | 6.7 % |
| 4 | `BILL_AMT1` | 4.3 % |
| 5 | `PAY_3` | 3.8 % |
| 6 | `PAY_AMT2` | 3.7 % |
| 7 | `PAY_AMT3` | 2.8 % |
| 8 | `PAY_4` | 2.3 % |
| 9 | `PAY_AMT1` | 1.7 % |
| 10 | `PAY_5` | 1.5 % |

Eight of the ten are repayment-status or payment-amount columns, and the repayment ones are ordered
by recency: `PAY_0` (most recent) beats `PAY_2` beats `PAY_3` beats `PAY_4` beats `PAY_5`. The
model rediscovered that the last month matters most, which is a good sign that it is reading the
data rather than the noise.

## What a business user should take from this

**Last month's payment behaviour is half the answer.** `PAY_0` — whether the account paid on time
in the most recent month — carries 50 % of the model. Everything else, including the credit limit,
the bill sizes and five months of earlier history, shares the other half. A collections team with
no model at all should still be sorting by that column.

**Work the top decile and you catch a third of next month's defaults.** 72 % of the 450
highest-scoring accounts default, against a portfolio rate of 22 %. Work the top three and you
reach two thirds of them. That is where a payment plan offered *before* the due date pays for
itself.

**This is the dataset where the machine learning earns its keep.** The +0.068 ROC-AUC over a
logistic baseline is more than fifteen times the gap seen on the Telco churn file (+0.004). The
reason is interaction: the meaning of a late payment depends on the pattern around it, and only the
tree ensembles picked that up.

## Reproducing it

```bash
python library/uci-credit-default/fetch.py
python -m library.run_engine \
  --dataset uci-credit-default --use-case card-default-propensity --config-root library/configs \
  --csv library/uci-credit-default/data/prepared.csv \
  --primary-key ID --target default_payment_next_month
```

Artefacts: `library/.runs/uci-credit-default/r_20260922_e8a6e3a1.results.json`.
