# UCI Default of Credit Card Clients — run report

Every number below came out of one real training run.

```
use case      card-default-propensity   (library/configs/use_cases/card_default_propensity.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv         (30,000 rows, 25 columns)
primary key   ID
target        default_payment_next_month, positive label 1
run           r_20260922_efb5ecbe
wall clock    49.7 s   (the 30-minute default budget was never approached)
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

## Leaderboard — top 3 of 5

Ranked on the **validation** score; `test` is reported, never ranked on.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.7742 | 0.7872 | 12.5 s |
| 2 | `LightGBM_BAG_L1` | LightGBM | 0.7739 | 0.7866 | 3.7 s |
| 3 | `XGBoost_BAG_L1` | XGBoost | 0.7731 | 0.7857 | 4.0 s |

**Winner:** Ensemble (LightGBM + XGBoost + Random Forest), 23 features, 21,000 training rows.

## Test metrics

Measured once, on the 4,500-row hold-out, at the auto-chosen threshold **0.30**.

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.7869** |
| PR-AUC | 0.5483 |
| F1 | 0.5432 |
| Recall | 0.5618 |
| Precision | 0.5259 |
| Accuracy | 0.7911 |
| Specificity | 0.8562 |
| Brier score | 0.1337 |

## Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| **ROC-AUC** | 0.7869 | 0.7245 | **+0.0624** |
| PR-AUC | 0.5483 | 0.4970 | +0.0513 |
| F1 | 0.5432 | 0.5067 | +0.0365 |
| Recall | 0.5618 | 0.4764 | +0.0854 |
| Precision | 0.5259 | 0.5411 | −0.0152 |

**This is the widest gap in the library.** +0.0624 ROC-AUC over a logistic regression is a real
gradient-boosting win, and it is easy to see why: the repayment-status codes interact
non-linearly — being two months late on the most recent bill means something quite different
depending on the previous five months — and a linear model cannot express that.

## Decile lift

Base rate 22.11 %. Decile 1 holds the 450 highest-scoring accounts.

| Decile | Rows | Defaults | Rate | Lift |
|---|---|---|---|---|
| **D1** | 450 | 324 | **72.00 %** | **3.26×** |
| D2 | 450 | 175 | 38.89 % | 1.76× |
| D3 | 450 | 130 | 28.89 % | 1.31× |

D1→D10: 3.26, 1.76, 1.31, 0.94, 0.77, 0.59, 0.48, 0.45, 0.24, 0.19. D1 captures **32.6 %** of all
defaults, and the curve falls monotonically — a well-behaved ranking with no inversions.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | **`PAY_0`** | **49.0 %** |
| 2 | `LIMIT_BAL` | 12.3 % |
| 3 | `BILL_AMT1` | 6.9 % |
| 4 | `PAY_2` | 5.7 % |
| 5 | `PAY_AMT2` | 4.9 % |
| 6 | `PAY_3` | 3.4 % |
| 7 | `PAY_AMT3` | 3.3 % |
| 8 | `PAY_4` | 2.2 % |
| 9 | `PAY_6` | 1.8 % |
| 10 | `PAY_AMT1` | 1.6 % |

Eight of the ten are repayment-status or payment-amount columns, and the repayment ones are
ordered by recency:
`PAY_0` (most recent) beats `PAY_2` beats `PAY_3` beats `PAY_4`. The model rediscovered that the
last month matters most, which is a good sign that it is reading the data rather than the noise.

## What a business user should take from this

**Last month's payment behaviour is half the answer.** `PAY_0` — whether the account paid on time
in the most recent month — carries 49 % of the model. Everything else, including the credit limit,
the bill sizes and five months of earlier history, shares the other half. A collections team with
no model at all should still be sorting by that column.

**Work the top decile and you catch a third of next month's defaults.** 72 % of the 450
highest-scoring accounts default, against a portfolio rate of 22 %. That is where a payment plan
offered *before* the due date pays for itself.

**This is the dataset where the machine learning earns its keep.** The +0.062 ROC-AUC over a
logistic baseline is roughly ten times the gap seen on the Telco churn file. The reason is
interaction: the meaning of a late payment depends on the pattern around it, and only the tree
ensembles picked that up.

## Reproducing it

```bash
python library/uci-credit-default/fetch.py
python -m library.run_engine \
  --dataset uci-credit-default --use-case card-default-propensity --config-root library/configs \
  --csv library/uci-credit-default/data/prepared.csv \
  --primary-key ID --target default_payment_next_month
```

Artefacts: `library/.runs/uci-credit-default/r_20260922_efb5ecbe.results.json`.
