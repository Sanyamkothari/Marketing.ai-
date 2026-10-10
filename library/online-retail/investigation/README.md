# Why the win-back case does not beat its baseline (Plan J M109, DEC-1319)

Four scripts, run in this order from the repository root. They need `data/online-retail-dataset.csv`
(`python library/online-retail/fetch.py`) and write only to `data/`, which is not committed.

| Step | Script | What it reads | What it answers |
|---|---|---|---|
| 1 | `seal.py` | `sample.csv` | Puts 20% of the 1,463 shoppers aside (stratified, seed 1319). Nothing before step 4 reads them. |
| 2 | `dev_cv.py` | the other 80% | Repeated 5-fold cross-validation (5 repeats): does any model family beat the logistic-regression baseline? |
| 3 | `feat_cv.py` | the 80% and the raw log | Would five more columns (December 2010 orders, orders and spend in the last 180 days, invoice days, how overdue the shopper is) help? |
| 4 | `sealed_eval.py` | both | The one look at the sealed 20%: every model fitted on the 80%, scored once; nothing is chosen from it. |

The engine's own splits are seeded from the run id, so every run draws a new test set from the same 1,463
shoppers; picking a setting by looking at several runs' test scores would be tuning on the test set. The sealed
20% is the guard against that. The numbers are in `../run_report.md`.
