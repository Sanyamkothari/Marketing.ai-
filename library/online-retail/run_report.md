# UCI Online Retail → win-back — run report

Every number below came out of one real training run. **This is the one that does not work**, and
that is reported as plainly as the four that do.

```
use case      retail-win-back      (library/configs/use_cases/retail_win_back.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv    (1,463 rows, 16 columns — aggregated by fetch.py)
primary key   customer_id
target        reactivated_90d, positive label 1
run           r_20260922_2242a2cc
wall clock    910.4 s   (of the 30-minute default budget), 106 models
```

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
  --dataset online-retail --use-case retail-win-back --config-root library/configs \
  --csv library/online-retail/data/prepared.csv \
  --primary-key customer_id --target reactivated_90d
```

Artefacts: `library/.runs/online-retail/r_20260922_2242a2cc.results.json`.
