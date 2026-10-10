# UCI Online Retail → win-back — run report

Every number in the run sections below came out of one real training run (2026-09-22); the M109 section
below came out of the scripts in `investigation/`. **This is the one that does not work**, and that is
reported as plainly as the four that do.

```
use case      retail-win-back      (configs/use_cases/retail_win_back.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv    (1,463 rows, 16 columns — aggregated by fetch.py)
primary key   customer_id
target        reactivated_90d, positive label 1
run           r_20260922_2242a2cc
wall clock    910.4 s   (of the 30-minute default budget), 106 models
```

## Plan J M109 follow-up: could a different setting beat the baseline? No, and here is why

The question was whether the use-case configuration (label, window, features, model search) could be
changed to beat the logistic-regression baseline. **It cannot, on this data, and no setting was changed.**
Nothing below was chosen by looking at a test set: 20% of the 1,463 shoppers (293) were put aside before
any experiment (`investigation/seal.py`, stratified, seed 1319) and read once, at the end. The engine's own
splits are seeded from the run id, so each run draws a different test set from the same shoppers; picking a
setting by comparing several runs' test scores would have been tuning on the test set.

**1. No model family beats the baseline in cross-validation** (`investigation/dev_cv.py`; the other 1,170
shoppers, 5-fold, repeated 5 times, so 25 held-out folds of about 234 shoppers each):

| Model | PR-AUC | ROC-AUC | PR-AUC minus baseline | Folds ahead of the baseline |
|---|---|---|---|---|
| Always the base rate (no model) | 0.413 | 0.500 | -0.149 | 0 of 25 |
| **Baseline (logistic regression)** | **0.562** | **0.626** | | |
| Logistic regression, stronger shrinkage | 0.563 | 0.631 | +0.002 | 16 of 25 |
| Logistic regression on logged columns | 0.559 | 0.629 | -0.003 | 11 of 25 |
| Extra trees | 0.562 | 0.624 | +0.001 | 11 of 25 |
| Gradient boosting | 0.558 | 0.617 | -0.004 | 10 of 25 |
| Random forest | 0.556 | 0.612 | -0.005 | 11 of 25 |

The columns do carry signal: every model is 0.15 PR-AUC above the base rate. But that signal is almost entirely
linear and a plain logistic regression already takes it. The engine's search tries the same four families
plus an ensemble of them, so there is nothing more for a different `candidates`, `strategy`, `ensemble` or
`time_limit_minutes` setting to find.

**2. More columns do not help either** (`investigation/feat_cv.py`; same 1,170 shoppers, 4 repeats). Five
columns computed from the raw invoice log up to the snapshot were tried: orders in December 2010 (the log
starts on 1 December 2010, so this is the only look at the gift retailer's holiday peak), orders and spend in
the last 180 days, days with an invoice, and how overdue the shopper is for an order (recency divided by the
usual gap). Logistic regression went from PR-AUC 0.560 to 0.557 and gradient boosting from 0.558 to 0.559,
inside the noise. The December 2010 count has a correlation of 0.01 with coming back. The honest reading is
that a shopper's seasonal habits cannot be seen from nine months of history that begin in December.
Because the committed sample and the library test pin 13 features and 16 columns, none of these columns was
added to `fetch.py`.

**3. The one look at the sealed shoppers** (`investigation/sealed_eval.py`; every model fitted on the 1,170,
scored once on the 293; nothing was chosen from it). The interval is a paired bootstrap of the difference
from the baseline's PR-AUC.

| Model | PR-AUC | ROC-AUC | PR-AUC minus baseline | 95% interval |
|---|---|---|---|---|
| **Baseline (logistic regression)** | **0.551** | **0.646** | | |
| Random forest | 0.560 | 0.669 | +0.009 | -0.046 to +0.060 |
| Extra trees | 0.556 | 0.656 | +0.005 | -0.035 to +0.040 |
| Gradient boosting | 0.538 | 0.657 | -0.013 | -0.062 to +0.041 |

Every interval includes zero. None of these is a model that "beats the baseline", and none loses to it
reliably.

**4. Why the earlier verdict flips from run to run.** On a test set of 219 shoppers the standard deviation of
the model-minus-baseline PR-AUC difference is 0.022 to 0.031 (resampling the sealed shoppers 219 at a time).
The earlier report's -0.0010, the margins of -0.048 to +0.037 in `library/tests/test_online_retail.py` and the
differences above are all well inside that. A verdict of "beats" or "does not beat" read off one test set of
this size is a coin toss that the data does not decide.

**What this means for the use case.** `configs/use_cases/retail_win_back.yaml` is left as it is: a setting
that happened to win on one run would be a number tuned until it stopped failing (DEC-410). The use case
works as a ranking (ROC-AUC about 0.63, 0.15 PR-AUC above the base rate) and the engine's baseline is as good
as any model it can build here. Whoever uses it should expect the logistic regression's answer and should
not expect more from a larger search. What would change that is what the "What would make this work" section
below already says: campaign history, contact log and offer data, and more than nine months of history.

## Validation

**Passed: 0 errors, 1 warning.** Every finding, in full:

> **`CONSTANT_COLUMN`** — warning, acknowledgeable, column `snapshot_date`
> *"'snapshot_date' has the same value in every row, so it cannot help the model."*
> *Suggestion: "It will be dropped before training."*

Correct, and expected: this file is a single snapshot, so `2011-09-10` appears in all 1,463 rows.
The column did not reach training — the feature list has 13 entries, not 14.

### A discrepancy worth recording

The warning above is the only thing `validation.json` said about `snapshot_date`. `prepare.json`
for the same run says something else:

```
pii_columns: ["snapshot_date"]
transforms: {kind: "redact", columns: ["snapshot_date"], parameters: {replacement: "[REDACTED]"}}
```

The column was **redacted as personal data**, not dropped as constant — because
`prepare._detect_pii` has its own, looser copy of the PII patterns, and the phone-number regex
`\+?\d[\d\s().-]{7,17}\d` matches the string `2011-09-10`. `ingest.detect_pii`, which the
validation report uses, skips DATE columns entirely and so said nothing.

Here it changes nothing — the column was headed for the bin either way. On a file with several
snapshot dates it would silently redact a live date column with no validation finding to say so.
Filed in [`docs/CROSS_BRANCH_REQUESTS.md`](../../docs/CROSS_BRANCH_REQUESTS.md) as *two PII
detectors that disagree*; not fixed here, because `engine/stages/prepare.py` is not this branch's
to edit.

## Preparation and split

| | |
|---|---|
| Rows in → out | 1,463 → 1,463 (nothing dropped) |
| Columns in → out | 16 → 16 |
| Columns dropped | none reported (`snapshot_date` was redacted, see above) |
| Features handed to training | **13** |
| Split | `random_stratified` — train 1,025 / validation 219 / test 219 |
| Positive rate | 41.37 % train, 41.10 % validation, 41.10 % test |

## Leaderboard — top 3 of 106

Ranked on the **validation** score, which for this use case is **PR-AUC**, not ROC-AUC
(`model_search.metric: pr_auc` — the question is who is worth an offer, so ranking quality on the
positives is what the search optimises).

| # | Model | Family | Validation PR-AUC | Test PR-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `WeightedEnsemble_L2` | ensemble | 0.6033 | 0.5368 | 12.3 s |
| 2 | `LinearModel_BAG_L1/T1` | Logistic Regression | 0.5560 | 0.5509 | 1.3 s |
| 3 | `LinearModel_BAG_L1/T10` | Logistic Regression | 0.5481 | 0.5242 | 0.9 s |

**Winner:** Ensemble (XGBoost + Random Forest), 13 features, 1,025 training rows.

**Look at rows 1 and 2 together.** The ensemble wins on validation by 0.047 and *loses* on test by
0.014. On a 219-row hold-out that is what overfitting the validation split looks like, and it is
the clearest signal in this report that there is not enough data here to choose a model with.

## Test metrics

Measured once, on the 219-row hold-out, at the auto-chosen threshold **0.35**.

| Metric | Value |
|---|---|
| ROC-AUC | 0.6237 |
| **PR-AUC** (primary) | **0.4975** |
| F1 | 0.5749 |
| Recall | 0.7889 |
| Precision | 0.4522 |
| Accuracy | 0.5205 |
| Specificity | 0.3333 |
| Brier score | 0.2469 |

Recall 0.79 against specificity 0.33 means the model says "yes" to most of the audience. The auto
threshold maximises F1 on validation, and with a 41 % base rate and a weak model that optimum sits
close to "say yes to everyone" — an earlier run of this same configuration reached it exactly,
with recall 1.0 and specificity 0.0. Filed in
[`docs/CROSS_BRANCH_REQUESTS.md`](../../docs/CROSS_BRANCH_REQUESTS.md) as *`threshold.mode: auto`
can call every row positive*.

## Baseline comparison — **the model does NOT beat the baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| **PR-AUC** (primary) | 0.4975 | 0.4985 | **−0.0010** |
| ROC-AUC | 0.6237 | 0.5910 | +0.0327 |
| F1 | 0.5749 | 0.5886 | −0.0137 |
| Recall | 0.7889 | 0.9778 | −0.1889 |
| Precision | 0.4522 | 0.4211 | +0.0311 |

`model_beats_baseline: false`. The verdict is taken on the **primary metric**, PR-AUC, which this
use case sets deliberately — and there the ensemble loses by a thousandth. It is ahead on ROC-AUC
and behind on F1 and recall. Fifteen minutes and 106 candidates bought a result that cannot be
distinguished from a logistic regression fitted in a second, on a 219-row hold-out where a
thousandth of PR-AUC is a fraction of one row.

## Decile lift

Base rate 41.10 %. Each decile holds 22 shoppers, except D10 which holds 21.

| Decile | Rows | Reactivated | Rate | Lift |
|---|---|---|---|---|
| **D1** | 22 | 12 | **54.55 %** | **1.33×** |
| D2 | 22 | 13 | 59.09 % | 1.44× |
| D3 | 22 | 13 | 59.09 % | 1.44× |

D1→D10: 1.33, 1.44, 1.44, 1.11, 1.44, 0.55, 0.55, 0.88, 0.66, 0.58.

**The curve is not monotonic and barely slopes.** D2, D3 and D5 all outrank D1, and the whole top
half sits between 1.11× and 1.44×. Compare the bank campaign's 4.52× top decile. On 22 rows a
decile is not a measurement, and this chart should not be shown to a customer as evidence of
anything.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | `distinct_products` | 48.7 % |
| 2 | `days_between_orders_mean` | 21.5 % |
| 3 | `order_value_mean` | 11.9 % |
| 4 | `orders_total` | 11.7 % |
| 5 | `active_months` | 4.0 % |
| 6 | `tenure_days` | 2.0 % |
| 7 | `returned_orders` | 0.2 % |
| 8 | `order_value_max` | 0.0 % (importance −0.0025) |
| 9 | `country` | 0.0 % (importance −0.0031) |
| 10 | `returned_items` | 0.0 % (importance −0.0049) |

Three of the ten have **negative** importance: shuffling them improved the score, which is noise,
not signal. And `recency_days` — which an earlier run of this same configuration put first, at
45 % — does not appear in the top ten at all. A feature ranking that reorders itself between runs
is not a finding about shoppers; it is a measurement of how little signal there is to find.

## What a business user should take from this

**This dataset does not support a win-back model, and the engine said so correctly.** The best
model a fifteen-minute, 106-candidate search could build ties with a plain logistic regression on
the primary metric, the decile curve is flat and out of order, and the feature ranking is unstable
between runs. The right conclusion is not "tune it harder" — it is that twelve months of invoices
for 1,463 lapsed shoppers, with no campaign history, no contact log and no offer data, does not
contain enough to predict who comes back. The engine ran the whole path, produced every artefact,
and handed back an honest negative. That is the system working.

**What would make this work.** Not a better algorithm — more columns. Campaign history (who was
offered what, and when), a contact log, offer type and discount depth, and email engagement are the
variables a win-back model needs, and none of them exists in a bare invoice log. The 41 %
reactivation rate also says something cheerful on its own: four in ten lapsed shoppers came back
within 90 days *with no intervention at all*, which sets the bar any campaign has to beat.

## Reproducing it

```bash
python library/online-retail/fetch.py
python -m library.run_engine \
  --dataset online-retail --use-case retail-win-back \
  --csv library/online-retail/data/prepared.csv \
  --primary-key customer_id --target reactivated_90d
```

Artefacts: `library/.runs/online-retail/r_20260922_2242a2cc.results.json`.
