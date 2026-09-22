# UCI Bank Marketing — run report

Two runs are reported here, because one of them is a trap and the other is the answer. Both are
real; both used engine defaults except where stated.

```
use case      bank-term-deposit    (library/configs/use_cases/bank_term_deposit.yaml)
file          data/prepared.csv    (41,188 rows, 22 columns)
primary key   client_id
target        y, positive label "yes"

RUN A   r_20260922_b9cd1fde   ENGINE DEFAULTS, no overrides        68.4 s
RUN B   r_20260922_bf5da58f   + prepare.exclude_columns: [duration] 57.5 s
```

Run A is the defaults run the brief asks for. Run B changes exactly one setting — through the
engine's own exclusion path, in configuration, with no code touched — and is the model anyone
should actually use. Read them together.

---

## Validation — both runs

**Passed: 0 errors, 0 warnings.** Both runs. The report is empty.

**That empty report is the most useful finding in this whole library**, so it is worth being
precise about what it does and does not mean.

`duration` — the length of the marketing call, in seconds — cannot be known before the call is
made. UCI's own documentation says it "should be discarded if the intention is to have a realistic
predictive model". The engine did not flag it, and it was right not to, because none of the three
branches of `LEAKAGE_SUSPECTED` describes this column:

| Branch | Fires when | Here |
|---|---|---|
| AUC | one column scores above 0.98 AUC alone | `duration` is strong but nowhere near 0.98 |
| name pattern | name matches `^(churn\|converted\|outcome)`, or `_date$` after the target | `duration` matches neither |
| baseline | a 3-fold logistic over all columns clears 0.98 | it does not: the whole file gets 0.9358 |

The check catches columns that **contain the answer**. `duration` does not contain the answer; it
is merely a fact that does not exist yet at the moment the prediction is needed. No statistical
test can tell those apart — only someone who knows when each column is written can. That is a
limitation of leakage detection in general, not a defect in this engine, and it is why the two runs
below are both reported.

Nothing else fired either: no `PII_DETECTED` (age, job and marital status are attributes, not
contact details), no `HIGH_NULL_COLUMN` (`unknown` is a literal category, not a null), no
`HIGH_CARDINALITY_ID_LIKE` on `client_id` (the primary key is never a candidate).

## Preparation and split

| | Run A | Run B |
|---|---|---|
| Rows in → out | 41,188 → 41,188 | 41,188 → 41,188 |
| Columns in → out | 22 → 22 | 22 → **21** |
| Columns dropped | none | `duration` — reason `user_excluded` |
| Features | **20** | **19** |
| Split | `random_stratified` — train 28,832 / validation 6,178 / test 6,178 | identical |
| Positive rate | 11.27 % in every part | identical |

---

## RUN A — engine defaults

### Leaderboard — top 3 of 5

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.9534 | 0.9509 | 11.1 s |
| 2 | `LightGBM_BAG_L1` | LightGBM | 0.9529 | 0.9509 | 3.4 s |
| 3 | `XGBoost_BAG_L1` | XGBoost | 0.9525 | 0.9496 | 5.4 s |

**Winner:** Ensemble (LightGBM + XGBoost + Random Forest), 20 features, 28,832 training rows.

### Test metrics — 6,178 rows, threshold 0.4286 (auto)

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.9503** |
| PR-AUC | 0.6450 |
| F1 | 0.6375 |
| Recall | 0.6279 |
| Precision | 0.6474 |
| Accuracy | 0.9196 |
| Specificity | 0.9566 |
| Brier score | 0.0551 |

### Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.9503 | 0.9358 | **+0.0145** |
| PR-AUC | 0.6450 | 0.5862 | +0.0588 |
| F1 | 0.6375 | 0.6228 | +0.0147 |
| Recall | 0.6279 | 0.6739 | −0.0460 |
| Precision | 0.6474 | 0.5790 | +0.0684 |

### Decile lift — base rate 11.27 %

| Decile | Rows | Subscribers | Rate | Lift |
|---|---|---|---|---|
| **D1** | 618 | 406 | **65.70 %** | **5.83×** |
| D2 | 618 | 206 | 33.33 % | 2.96× |
| D3 | 618 | 66 | 10.68 % | 0.95× |

D1→D10: 5.83, 2.96, 0.95, 0.22, 0.03, 0.00, 0.00, 0.00, 0.01, 0.00. D1 captures **58.3 %** of all
subscriptions.

### Top 10 features

| # | Feature | Share |
|---|---|---|
| 1 | **`duration`** | **73.5 %** |
| 2 | `emp_var_rate` | 9.1 % |
| 3 | `euribor3m` | 7.6 % |
| 4 | `nr_employed` | 4.3 % |
| 5 | `month` | 1.9 % |
| 6 | `cons_conf_idx` | 0.7 % |
| 7 | `age` | 0.6 % |
| 8 | `pdays` | 0.5 % |
| 9 | `contact` | 0.4 % |
| 10 | `day_of_week` | 0.4 % |

**Read the top row and the run is over.** Three quarters of this model is one column that does not
exist until the call has already happened. A 0.95 ROC-AUC and a 5.8× lift look like a triumph and
would be worth nothing on Monday morning, because you cannot sort a call list by how long the call
is going to last.

---

## RUN B — the same run with `duration` excluded

One setting changed, in configuration: `prepare.exclude_columns: ["duration"]`. That is the exact
override `LEAKAGE_SUSPECTED`'s `details.override_path` offers when it *does* fire, used here on a
column the check could not have known about.

### Leaderboard — top 3 of 5

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.8055 | 0.7847 | 17.2 s |
| 2 | `LightGBM_BAG_L1` | LightGBM | 0.8050 | 0.7851 | 2.7 s |
| 3 | `XGBoost_BAG_L1` | XGBoost | 0.8045 | 0.7847 | 4.3 s |

**Winner:** Ensemble (LightGBM + XGBoost + Logistic Regression), 19 features.

### Test metrics — 6,178 rows, threshold 0.29 (auto)

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.7825** |
| PR-AUC | 0.4258 |
| F1 | 0.4849 |
| Recall | 0.5532 |
| Precision | 0.4316 |
| Accuracy | 0.8676 |
| Specificity | 0.9075 |
| Brier score | 0.0788 |

### Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.7825 | 0.7724 | **+0.0101** |
| PR-AUC | 0.4258 | 0.4098 | +0.0160 |
| F1 | 0.4849 | 0.4606 | +0.0243 |
| Recall | 0.5532 | 0.5086 | +0.0446 |
| Precision | 0.4316 | 0.4209 | +0.0107 |

### Decile lift — base rate 11.27 %

| Decile | Rows | Subscribers | Rate | Lift |
|---|---|---|---|---|
| **D1** | 618 | 303 | **49.03 %** | **4.35×** |
| D2 | 618 | 115 | 18.61 % | 1.65× |
| D3 | 618 | 60 | 9.71 % | 0.86× |

D1→D10: 4.35, 1.65, 0.86, 0.62, 0.52, 0.53, 0.50, 0.37, 0.33, 0.26. D1 captures **43.5 %** of all
subscriptions.

### Top 10 features

| # | Feature | Share |
|---|---|---|
| 1 | `emp_var_rate` | 49.9 % |
| 2 | `nr_employed` | 13.7 % |
| 3 | `contact` | 9.8 % |
| 4 | `month` | 7.6 % |
| 5 | `cons_price_idx` | 5.4 % |
| 6 | `euribor3m` | 4.9 % |
| 7 | `cons_conf_idx` | 1.8 % |
| 8 | `day_of_week` | 1.7 % |
| 9 | `pdays` | 1.4 % |
| 10 | `poutcome` | 1.3 % |

## What a business user should take from this

**A term-deposit call list built this way is four times better than calling at random.** The top
decile subscribes at 49 % against a campaign average of 11 %, and calling that one-tenth of the
book reaches 44 % of everyone who was ever going to say yes. That is Run B — the model you can
actually deploy.

**Most of what predicts a subscription is the economy, not the client.** Four of the top six
features are macro-economic: the employment variation rate alone carries half the model, and the
employment level, the consumer price index and Euribor add another quarter between them. The other
two are the channel and the calendar month. This campaign succeeded when
rates made deposits attractive. It is a genuine and slightly uncomfortable finding — the model is
partly forecasting the interest-rate cycle — and it means the ranking will need retraining when
rates move, which is what `monitoring.retraining: on_drift` is for.

**The difference between the two runs is the whole argument for having a person read the column
list.** Run A scores 0.95 and is worthless; Run B scores 0.78 and works. No validation check found
the difference and none could have: `duration` does not contain the answer, it simply does not
exist yet. One line of configuration separates them.

## Reproducing them

```bash
python library/uci-bank-marketing/fetch.py

# Run A — defaults
python -m library.run_engine \
  --dataset uci-bank-marketing --use-case bank-term-deposit --config-root library/configs \
  --csv library/uci-bank-marketing/data/prepared.csv --primary-key client_id --target y

# Run B — one config override
python -m library.run_engine \
  --dataset uci-bank-marketing --use-case bank-term-deposit --config-root library/configs \
  --csv library/uci-bank-marketing/data/prepared.csv --primary-key client_id --target y \
  --override 'prepare.exclude_columns=["duration"]'
```

Artefacts: `library/.runs/uci-bank-marketing/r_20260922_b9cd1fde.results.json` (A) and
`r_20260922_bf5da58f.results.json` (B).
