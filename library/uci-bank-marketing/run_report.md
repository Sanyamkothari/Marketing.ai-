# UCI Bank Marketing — run report

Two runs are reported here, because one of them is a trap and the other is the answer. Both are
real; both used engine defaults except where stated.

```
use case      bank-term-deposit    (library/configs/use_cases/bank_term_deposit.yaml)
file          data/prepared.csv    (41,188 rows, 22 columns)
primary key   client_id
target        y, positive label "yes"

RUN A   r_20260922_9162baf6   ENGINE DEFAULTS, no overrides          1314.0 s, 112 models
RUN B   r_20260922_645fe3db   + prepare.exclude_columns: [duration]  1147.6 s, 144 models
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
| baseline | a 3-fold logistic over all columns clears 0.98 | it does not: the whole file gets 0.9371 |

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

### Leaderboard — top 3 of 112

Ranked on the **validation** score; `test` is reported, never ranked on. The default
`tuning_trials: 50` fits many variants of each family, so a twenty-two-minute search produced 112
candidates; a `/T20` suffix is a tuning trial.

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.9534 | 0.9516 | 18.6 s |
| 2 | `LightGBM_BAG_L1/T20` | LightGBM | 0.9502 | 0.9513 | 4.1 s |
| 3 | `LightGBM_BAG_L1/T33` | LightGBM | 0.9500 | 0.9515 | 3.9 s |

**Winner:** Ensemble (LightGBM + XGBoost + Random Forest), 20 features, 28,832 training rows.

### Test metrics — 6,178 rows, threshold 0.3524 (auto)

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.9509** |
| PR-AUC | 0.6589 |
| F1 | 0.6588 |
| Recall | 0.7227 |
| Precision | 0.6053 |
| Accuracy | 0.9157 |
| Specificity | 0.9402 |
| Brier score | 0.0545 |

### Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.9509 | 0.9371 | **+0.0138** |
| PR-AUC | 0.6589 | 0.6051 | +0.0538 |
| F1 | 0.6588 | 0.6254 | +0.0334 |
| Recall | 0.7227 | 0.7184 | +0.0043 |
| Precision | 0.6053 | 0.5537 | +0.0516 |

### Decile lift — base rate 11.27 %

| Decile | Rows | Subscribers | Rate | Lift |
|---|---|---|---|---|
| **D1** | 618 | 410 | **66.34 %** | **5.89×** |
| D2 | 618 | 206 | 33.33 % | 2.96× |
| D3 | 618 | 65 | 10.52 % | 0.93× |

D1→D10: 5.89, 2.96, 0.93, 0.16, 0.04, 0.01, 0.00, 0.00, 0.00, 0.00. D1 captures **58.9 %** of all
subscriptions and the top two capture **88.5 %**.

### Top 10 features

| # | Feature | Share |
|---|---|---|
| 1 | **`duration`** | **67.7 %** |
| 2 | `emp_var_rate` | 15.5 % |
| 3 | `euribor3m` | 6.6 % |
| 4 | `nr_employed` | 3.4 % |
| 5 | `month` | 2.2 % |
| 6 | `age` | 0.7 % |
| 7 | `pdays` | 0.7 % |
| 8 | `cons_price_idx` | 0.5 % |
| 9 | `cons_conf_idx` | 0.5 % |
| 10 | `contact` | 0.5 % |

**Read the top row and the run is over.** Two thirds of this model is one column that does not
exist until the call has already happened. A 0.95 ROC-AUC and a 5.9× lift look like a triumph and
would be worth nothing on Monday morning, because you cannot sort a call list by how long the call
is going to last.

---

## RUN B — the same run with `duration` excluded

One setting changed, in configuration: `prepare.exclude_columns: ["duration"]`. That is the exact
override `LEAKAGE_SUSPECTED`'s `details.override_path` offers when it *does* fire, used here on a
column the check could not have known about.

### Leaderboard — top 3 of 144

| # | Model | Family | Validation ROC-AUC | Test ROC-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.8067 | 0.7916 | 4.8 s |
| 2 | `LightGBM_BAG_L1/T18` | LightGBM | 0.8064 | 0.7933 | 2.6 s |
| 3 | `LightGBM_BAG_L1/T14` | LightGBM | 0.8062 | 0.7940 | 3.4 s |

**Winner:** Ensemble (LightGBM), 19 features. The weighted ensemble put all its weight on LightGBM
variants, so the "ensemble" is one family's tuning trials blended together.

### Test metrics — 6,178 rows, threshold 0.2809 (auto)

| Metric | Value |
|---|---|
| **ROC-AUC** | **0.7906** |
| PR-AUC | 0.4404 |
| F1 | 0.4860 |
| Recall | 0.4871 |
| Precision | 0.4850 |
| Accuracy | 0.8839 |
| Specificity | 0.9343 |
| Brier score | 0.0774 |

### Baseline comparison — **model beats baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.7906 | 0.7831 | **+0.0075** |
| PR-AUC | 0.4404 | 0.4153 | +0.0251 |
| F1 | 0.4860 | 0.4739 | +0.0121 |
| Recall | 0.4871 | 0.5216 | −0.0345 |
| Precision | 0.4850 | 0.4342 | +0.0508 |

### Decile lift — base rate 11.27 %

| Decile | Rows | Subscribers | Rate | Lift |
|---|---|---|---|---|
| **D1** | 618 | 315 | **50.97 %** | **4.52×** |
| D2 | 618 | 128 | 20.71 % | 1.84× |
| D3 | 618 | 50 | 8.09 % | 0.72× |

D1→D10: 4.52, 1.84, 0.72, 0.65, 0.39, 0.45, 0.29, 0.39, 0.36, 0.40. D1 captures **45.3 %** of all
subscriptions and the top two capture **63.6 %**.

### Top 10 features

| # | Feature | Share |
|---|---|---|
| 1 | `nr_employed` | 27.8 % |
| 2 | `month` | 24.6 % |
| 3 | `emp_var_rate` | 12.7 % |
| 4 | `euribor3m` | 12.1 % |
| 5 | `contact` | 9.4 % |
| 6 | `campaign` | 3.7 % |
| 7 | `cons_price_idx` | 2.4 % |
| 8 | `pdays` | 2.4 % |
| 9 | `cons_conf_idx` | 2.0 % |
| 10 | `day_of_week` | 1.9 % |

## What a business user should take from this

**A term-deposit call list built this way is four and a half times better than calling at random.**
The top decile subscribes at 51 % against a campaign average of 11 %, and calling that one-tenth of
the book reaches 45 % of everyone who was ever going to say yes. Call the top fifth and you reach
64 %. That is Run B — the model you can actually deploy.

**Most of what predicts a subscription is the economy and the calendar, not the client.** Four of
the top five features are macro-economic or seasonal: the employment level, the month, the
employment variation rate and Euribor together carry 77 % of the model. This campaign succeeded
when rates made deposits attractive. It is a genuine and slightly uncomfortable finding — the model
is partly forecasting the interest-rate cycle — and it means the ranking will need retraining when
rates move, which is what `monitoring.retraining: on_drift` is for.

**The difference between the two runs is the whole argument for having a person read the column
list.** Run A scores 0.95 and is worthless; Run B scores 0.79 and works. No validation check found
the difference and none could: `duration` does not contain the answer, it simply does not exist
yet. One line of configuration separates them.

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

Artefacts: `library/.runs/uci-bank-marketing/r_20260922_9162baf6.results.json` (A) and
`r_20260922_645fe3db.results.json` (B).
