# Telco Customer Churn — run report

Every number below came out of one real training run. The command that produced it, and the
`results.json` it wrote, are named at the bottom.

```
use case      telco-churn          (configs/use_cases/telco_churn.yaml, the repo's own root)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv    (7,043 rows, 21 columns — a copy of the download)
primary key   customerID
target        Churn, positive label "Yes"
run           r_20260922_49c5bed1
wall clock    21.0 s   (the 30-minute default budget was never approached)
```

## Validation

**Passed: 0 errors, 0 warnings.** The report is empty, which on this dataset is the finding: the
published file satisfies the data contract as it stands.

Worth naming the two things that could have fired and did not:

* **`TotalCharges`** is a text column with eleven blanks. `HIGH_NULL_COLUMN` needs 60 % null and
  this is 0.16 %, so no warning — correctly. The column reached training and finished fourth on the
  importance chart.
* **No `PII_DETECTED`.** `customerID` is a string of the form `7590-VHVEG`; nothing in the file
  matches the email, phone, PAN, Aadhaar or name detectors.

## Preparation and split

| | |
|---|---|
| Rows in → out | 7,043 → 7,043 (nothing dropped) |
| Columns in → out | 21 → 21 |
| Columns dropped | none |
| Features handed to training | **19** (everything but `customerID` and `Churn`) |
| Split | `random_stratified` — train 4,931 / validation 1,056 / test 1,056 |
| Positive rate | 26.55 % train, 26.52 % validation, 26.52 % test |

## Leaderboard — top 3 of 5

Ranked on the **validation** score; `test` is reported, never ranked on.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.8496 | 0.8597 | 6.3 s |
| 2 | `LinearModel_BAG_L1` | Logistic Regression | 0.8475 | 0.8569 | 0.7 s |
| 3 | `LightGBM_BAG_L1` | LightGBM | 0.8415 | 0.8551 | 3.4 s |

**Winner:** Ensemble (Logistic Regression + LightGBM + XGBoost), 19 features, 4,931 training rows.

## Test metrics

Measured once, on the 1,056-row hold-out, at the auto-chosen threshold **0.3667**.

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.8573** |
| PR-AUC | 0.6761 |
| F1 | 0.6598 |
| Recall | 0.6821 |
| Precision | 0.6388 |
| Accuracy | 0.8134 |
| Specificity | 0.8608 |
| Brier score | 0.1272 |

## Baseline comparison

The engine fits a separate logistic-regression baseline and scores it on the same hold-out.
**The model beats the baseline.**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.8573 | 0.8535 | **+0.0038** |
| PR-AUC | 0.6761 | 0.6645 | +0.0116 |
| F1 | 0.6598 | 0.6490 | +0.0108 |
| Recall | 0.6821 | 0.7000 | −0.0179 |
| Precision | 0.6388 | 0.6049 | +0.0339 |

The gap is small, and that is the honest reading: churn on this file is close to a linear problem,
and a well-fitted logistic regression gets most of the way there. The ensemble's own second member
*is* a logistic regression.

## Decile lift

Base rate 26.52 %. Decile 1 holds the 106 highest-scoring subscribers.

| Decile | Rows | Churners | Rate | Lift |
|---|---|---|---|---|
| **D1** | 106 | 85 | **80.19 %** | **3.02×** |
| D2 | 106 | 65 | 61.32 % | 2.31× |
| D3 | 106 | 46 | 43.40 % | 1.64× |

Full lift curve, D1→D10: 3.02, 2.31, 1.64, 1.10, 0.75, 0.57, 0.25, 0.07, 0.11, 0.14.
D1 alone captures **30.4 %** of all churners.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | `tenure` | 56.7 % |
| 2 | `Contract` | 19.3 % |
| 3 | `InternetService` | 11.0 % |
| 4 | `TotalCharges` | 3.3 % |
| 5 | `MonthlyCharges` | 2.4 % |
| 6 | `PaymentMethod` | 1.6 % |
| 7 | `OnlineSecurity` | 1.3 % |
| 8 | `TechSupport` | 1.2 % |
| 9 | `PaperlessBilling` | 1.0 % |
| 10 | `StreamingMovies` | 0.6 % |

## What a business user should take from this

**Call the top 10 % and you reach three churners in every four.** The highest-scoring decile churns
at 80 % against a book average of 27 %, and it contains 30 % of everyone who is going to leave. A
retention team with the budget to call a tenth of the base does not have to guess which tenth.

**Two things explain most of the risk, and both are commercial, not technical.** Tenure and
contract type together carry 76 % of the model's importance: new subscribers on month-to-month
contracts are the risk, and moving them onto a term is the lever. Nothing about network quality or
support tickets appears near the top — on this data, churn is a pricing-and-commitment problem.

**The uplift over a simple model is modest, and worth saying so.** A plain logistic regression gets
0.8535 against the ensemble's 0.8573. The value here is not a cleverer model; it is that the whole
path — upload, validate, train, rank, band, explain — ran in 21 seconds on configuration alone.

## Reproducing it

```bash
python library/telco-customer-churn/fetch.py
python -m library.run_engine \
  --dataset telco-customer-churn --use-case telco-churn \
  --csv library/telco-customer-churn/data/prepared.csv \
  --primary-key customerID --target Churn
```

Artefacts: `library/.runs/telco-customer-churn/r_20260922_49c5bed1.results.json` and the full run
directory beside it (git-ignored). Scores will move slightly between machines: the model search has
a wall-clock budget, so which candidates finish inside it is not something a seed controls
(`engine/stages/train.py`, "Honest determinism").
