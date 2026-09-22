# UCI Online Retail → win-back — run report

Every number below came out of one real training run. **This is the one that does not work**, and
that is reported as plainly as the four that do.

```
use case      retail-win-back      (library/configs/use_cases/retail_win_back.yaml)
config        ENGINE DEFAULTS, no overrides at all
file          data/prepared.csv    (1,463 rows, 16 columns — aggregated by fetch.py)
primary key   customer_id
target        reactivated_90d, positive label 1
run           r_20260922_15645399
wall clock    269.8 s
```

## Validation

**Passed: 0 errors, 1 warning.** Every finding, in full:

> **`CONSTANT_COLUMN`** — warning, acknowledgeable, column `snapshot_date`
> *"'snapshot_date' has the same value in every row, so it cannot help the model."*
> *Suggestion: "It will be dropped before training."*

Correct, and expected: this file is a single snapshot, so `2011-09-10` appears in all 1,463 rows.
The column did not reach training — the feature list below has 13 entries, not 14.

### A discrepancy worth recording

The warning above is the only thing `validation.json` said about `snapshot_date`. `prepare.json`
for the same run says something else:

```
pii_columns: ["snapshot_date"]
transforms[1]: {kind: "redact", columns: ["snapshot_date"], parameters: {replacement: "[REDACTED]"}}
```

The column was **redacted as personal data**, not dropped as constant — because
`prepare._detect_pii` has its own, looser copy of the PII patterns, and the phone-number regex
`\+?\d[\d\s().-]{7,17}\d` matches the string `2011-09-10`. `ingest.detect_pii`, which the
validation report uses, skips DATE columns entirely and so said nothing.

Here it changes nothing — the column was headed for the bin either way. On a file with several
snapshot dates it would silently redact a live date column with no validation finding to say so.
Filed in [`docs/CROSS_BRANCH_REQUESTS.md`](../../docs/CROSS_BRANCH_REQUESTS.md) as *two PII
detectors that disagree*; not fixed here, because
`engine/stages/prepare.py` is not this branch's to edit.

## Preparation and split

| | |
|---|---|
| Rows in → out | 1,463 → 1,463 (nothing dropped) |
| Columns in → out | 16 → 16 |
| Columns dropped | none reported (`snapshot_date` was redacted, see above) |
| Features handed to training | **13** |
| Split | `random_stratified` — train 1,025 / validation 219 / test 219 |
| Positive rate | 41.37 % train, 41.10 % validation, 41.10 % test |

Outlier clipping ran on every numeric feature, fitted on the 1,025 training rows only —
`recency_days` to [92, 282], `orders_total` to [1, 6], `items_total` to [4, 2389.24], and so on.

## Leaderboard — top 3 of 5

Ranked on the **validation** score, which for this use case is **PR-AUC**, not ROC-AUC
(`model_search.metric: pr_auc` — the question is who is worth an offer, so ranking quality on the
positives is what the search optimises).

| # | Model | Family | Validation PR-AUC | Test PR-AUC | Fit |
|---|---|---|---|---|---|
| 1 | `LinearModel_BAG_L1` | Logistic Regression | 0.5495 | 0.5423 | 0.5 s |
| 2 | `WeightedEnsemble_L2` | ensemble | 0.5495 | 0.5423 | 0.7 s |
| 3 | `XGBoost_BAG_L1` | XGBoost | 0.4910 | 0.5127 | 15.4 s |

**Winner:** "Ensemble (Logistic Regression)" — an ensemble of exactly one model, because the
weighted ensemble gave all its weight to the logistic regression. Neither gradient booster beat it.
On 1,025 training rows and thirteen correlated RFM aggregates, there is not enough signal for a
tree ensemble to find.

## Test metrics

Measured once, on the 219-row hold-out, at the auto-chosen threshold **0.3148**.

| Metric | Value |
|---|---|
| ROC-AUC | 0.6177 |
| **PR-AUC** (primary) | **0.4982** |
| F1 | 0.5825 |
| Recall | **1.0000** |
| Precision | 0.4110 |
| Accuracy | 0.4110 |
| Specificity | **0.0000** |
| Brier score | 0.2376 |

**Recall 1.0 and specificity 0.0 mean the model called every shopper positive.** The auto threshold
maximises F1 on the validation split, and with a 41 % base rate and a weak model the maximum really
is at "say yes to everyone" — accuracy 0.411 is exactly the positive rate. Nothing is
mis-computed, and the ranking, the bands and the decile chart are unaffected because they read the
score rather than the threshold. But a Model page reporting 100 % recall reads as a triumph and
means no decision was made. Filed in
[`docs/CROSS_BRANCH_REQUESTS.md`](../../docs/CROSS_BRANCH_REQUESTS.md) as *`threshold.mode: auto`
can call every row positive*.

## Baseline comparison — **the model does NOT beat the baseline**

| Metric | Model | Baseline | Δ |
|---|---|---|---|
| ROC-AUC | 0.6177 | 0.6282 | **−0.0105** |
| PR-AUC | 0.4982 | 0.5027 | **−0.0045** |
| F1 | 0.5825 | 0.5855 | −0.0030 |
| Recall | 1.0000 | 0.9889 | +0.0111 |
| Precision | 0.4110 | 0.4159 | −0.0049 |

`model_beats_baseline: false`. The winning model is a logistic regression and so is the baseline;
they are the same class of model fitted twice, and the baseline came out marginally ahead on the
hold-out. The differences — 0.01 ROC-AUC on 219 rows — are inside the noise of a test split this
small, but the honest summary is: **the whole AutoML search bought nothing on this dataset.**

## Decile lift

Base rate 41.10 %. Decile 1 holds the 22 highest-scoring shoppers.

| Decile | Rows | Reactivated | Rate | Lift |
|---|---|---|---|---|
| **D1** | 22 | 13 | **59.09 %** | **1.44×** |
| D2 | 22 | 14 | 63.64 % | 1.55× |
| D3 | 22 | 8 | 36.36 % | 0.88× |

D1→D10: 1.44, 1.55, 0.88, 1.22, 0.77, 0.66, 0.66, 0.55, 1.22, 1.04.

**The curve is not monotonic and barely slopes.** D2 outranks D1, D9 outranks D3, and the whole
range sits between 0.55× and 1.55×. Compare the bank campaign's 5.83× top decile. On 22 rows a
decile is not a measurement, and this chart should not be shown to a customer as evidence of
anything.

## Top 10 features

Permutation importance on the test split.

| # | Feature | Share |
|---|---|---|
| 1 | `recency_days` | 44.7 % |
| 2 | `returned_orders` | 34.0 % |
| 3 | `orders_total` | 8.7 % |
| 4 | `order_value_max` | 5.9 % |
| 5 | `country` | 2.9 % |
| 6 | `order_value_mean` | 2.2 % |
| 7 | `days_between_orders_mean` | 1.0 % |
| 8 | `tenure_days` | 0.3 % |
| 9 | `active_months` | 0.3 % |
| 10 | `spend_total` | **−0.0 %** (importance −0.0016) |

A negative permutation importance means shuffling the column *improved* the score — noise, not
signal. On a 219-row test split, several of these numbers are noise.

## What a business user should take from this

**This dataset does not support a win-back model, and the engine said so correctly.** The
best model the search could build loses to a plain logistic regression on the hold-out, and the
decile curve is flat. The right conclusion is not "tune it harder" — it is that fourteen months of
invoices for 1,463 lapsed shoppers, with no campaign history, no contact log and no offer data,
does not contain enough to predict who comes back. The engine ran the whole path, produced every
artefact, and handed back an honest negative. That is the system working.

**What little signal there is, is recency and returns.** `recency_days` (45 %) and
`returned_orders` (34 %) carry four fifths of what the model found. Shoppers who lapsed recently
and who never sent anything back are the ones who come back. Both are things you can sort a list by
without a model at all.

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

Artefacts: `library/.runs/online-retail/r_20260922_15645399.results.json`.
