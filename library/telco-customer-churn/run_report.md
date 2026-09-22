# Telco Customer Churn — run report

Every number below came out of one real training run. The command that produced it, and the
`results.json` it wrote, are named at the bottom.

```
use case      telco-churn          (configs/use_cases/telco_churn.yaml, the repo's own root)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv    (7,043 rows, 21 columns — a copy of the download)
primary key   customerID
target        Churn, positive label "Yes"
run           r_20260922_94afae7c
wall clock    624.3 s   (of the 30-minute default budget)
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

## Leaderboard — top 3 of 111

Ranked on the **validation** score; `test` is reported, never ranked on. The default
`tuning_trials: 50` means the search fits many variants of each family — hence 111 models in ten
minutes, where the names ending `/T42` are tuning trials of the same family.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `XGBoost_BAG_L1/T42` | XGBoost | 0.8511 | 0.8461 | 3.5 s |
| 2 | `XGBoost_BAG_L1/T14` | XGBoost | 0.8511 | 0.8455 | 2.4 s |
| 3 | `XGBoost_BAG_L1/T25` | XGBoost | 0.8510 | 0.8458 | 2.3 s |

**Winner:** Ensemble (XGBoost + Logistic Regression), 19 features, 4,931 training rows. The top
three validation scores are separated by 0.0001 — on this data the tuning bought almost nothing,
which is worth knowing before anyone asks for a bigger budget.

## Test metrics

Measured once, on the 1,056-row hold-out, at the auto-chosen threshold **0.3684**.

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.8448** |
| PR-AUC | 0.6691 |
| F1 | 0.6311 |
| Recall | 0.7393 |
| Precision | 0.5505 |
| Accuracy | 0.7708 |
| Specificity | 0.7822 |
| Brier score | 0.1321 |

## Baseline comparison

The engine fits a separate logistic-regression baseline and scores it on the same hold-out.
**The model beats the baseline.**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.8448 | 0.8409 | **+0.0039** |
| PR-AUC | 0.6691 | 0.6466 | +0.0225 |
| F1 | 0.6311 | 0.6188 | +0.0123 |
| Recall | 0.7393 | 0.7071 | +0.0322 |
| Precision | 0.5505 | 0.5500 | +0.0005 |

The gap is small, and that is the honest reading: churn on this file is close to a linear problem,
and a well-fitted logistic regression gets most of the way there. The winning ensemble's second
member *is* a logistic regression. Ten minutes of search and 111 models bought +0.004 ROC-AUC over
a model that fits in under a second.

## Decile lift

Base rate 26.52 %. Decile 1 holds the 106 highest-scoring subscribers.

| Decile | Rows | Churners | Rate | Lift |
|---|---|---|---|---|
| **D1** | 106 | 84 | **79.25 %** | **2.99×** |
| D2 | 106 | 66 | 62.26 % | 2.35× |
| D3 | 106 | 39 | 36.79 % | 1.39× |

Full lift curve, D1→D10: 2.99, 2.35, 1.39, 0.89, 1.07, 0.53, 0.43, 0.14, 0.14, 0.04.
D1 alone captures **30.0 %** of all churners; the top three capture **67.5 %**.

The curve is not quite monotonic — D5 (1.07×) sits above D4 (0.89×). On 106 rows a decile is a
small sample, and one inversion in the middle of the curve is noise rather than a defect.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | `tenure` | 37.3 % |
| 2 | `Contract` | 19.7 % |
| 3 | `InternetService` | 17.1 % |
| 4 | `TotalCharges` | 5.9 % |
| 5 | `StreamingMovies` | 3.2 % |
| 6 | `MonthlyCharges` | 2.6 % |
| 7 | `StreamingTV` | 2.5 % |
| 8 | `MultipleLines` | 2.3 % |
| 9 | `OnlineSecurity` | 2.3 % |
| 10 | `PaymentMethod` | 1.8 % |

## What a business user should take from this

**Call the top 10 % and four in five are leaving.** The highest-scoring decile churns at 79 %
against a book average of 27 %, and it contains 30 % of everyone who is going to leave. Work the
top three deciles and you reach two thirds of them. A retention team with the budget to call a
tenth of the base does not have to guess which tenth.

**Three things explain three quarters of the risk, and all of them are commercial.** Tenure,
contract type and internet service carry 74 % of the model's importance between them: new
subscribers, on month-to-month contracts, on fibre. Moving them onto a term is the lever. Nothing
about support tickets or payment history appears near the top — on this data, churn is a
pricing-and-commitment problem.

**The uplift over a simple model is modest, and worth saying so.** A plain logistic regression
scores 0.8409 against the ensemble's 0.8448, after ten minutes and 111 candidates. The value here
is not a cleverer model; it is that the whole path — upload, validate, train, rank, band, explain —
ran unattended on configuration alone, and told you the honest size of its own advantage.

## Reproducing it

```bash
python library/telco-customer-churn/fetch.py
python -m library.run_engine \
  --dataset telco-customer-churn --use-case telco-churn \
  --csv library/telco-customer-churn/data/prepared.csv \
  --primary-key customerID --target Churn
```

Artefacts: `library/.runs/telco-customer-churn/r_20260922_94afae7c.results.json` and the full run
directory beside it (git-ignored). Scores will move slightly between machines: the model search has
a wall-clock budget, so which candidates finish inside it is not something a seed controls
(`engine/stages/train.py`, "Honest determinism").
