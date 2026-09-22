# Telco Customer Churn

**Industry** Telecom · **Use case** [`telco-churn`](../../configs/use_cases/telco_churn.yaml) ·
**Target** `Churn`

The dataset the engine's Telco use case was written against, and the only one in the library that
needs no preparation at all: `fetch.py` copies the download and stops.

| | |
|---|---|
| Source | <https://raw.githubusercontent.com/IBM/telco-customer-churn-on-icp4d/master/data/Telco-Customer-Churn.csv> |
| Also published as | <https://www.kaggle.com/datasets/blastchar/telco-customer-churn> |
| Licence | Apache-2.0 (the repository fetched); IBM Cognos Analytics sample data. See [LICENCE](LICENSE.txt) |
| Rows | 7,043, one per subscriber |
| Columns | 21 — 1 key, 19 features, 1 target |
| Primary key | `customerID` |
| Target | `Churn`, `Yes` / `No`; 1,869 `Yes` (26.5 %) |
| Time column | none |
| Config root | the repository's own `configs/` — no new use-case file was needed |

## What the target means

`Churn` is `Yes` when the subscriber left the company within the month after the snapshot the row
describes, and `No` otherwise. It is a state observed *after* the billing month the features
describe, which is what makes it a legitimate training target rather than a restatement of the
present.

## The columns

`customerID` identifies the subscriber and is never used as a feature. Nineteen features describe
the subscription: demographics (`gender`, `SeniorCitizen`, `Partner`, `Dependents`), how long they
have been a customer (`tenure`, in months), what they buy (`PhoneService`, `MultipleLines`,
`InternetService`, `OnlineSecurity`, `OnlineBackup`, `DeviceProtection`, `TechSupport`,
`StreamingTV`, `StreamingMovies`), and how they pay (`Contract`, `PaperlessBilling`,
`PaymentMethod`, `MonthlyCharges`, `TotalCharges`). Every one of them is described a line at a time
in [`templates/telco_churn_template_README.md`](../../templates/telco_churn_template_README.md),
which the engine generates from the use-case file.

## Known quirks

**`TotalCharges` is text, not a number.** Eleven rows hold a single space instead of a figure —
the subscribers whose `tenure` is 0 and who have therefore never been billed. `fetch.py` leaves
them exactly as published. Pandas reads the column as `object`, which means the engine sees a
column of numeric-looking strings with eleven blanks in it. It did not trip any validation check
(eleven blanks in 7,043 rows is 0.16 %, far below the 60 % `HIGH_NULL_COLUMN` threshold) and the
column survived to training, where it came fourth on the importance chart. See
[`run_report.md`](run_report.md).

**`SeniorCitizen` is 0/1 where every other yes/no column is `Yes`/`No`.** It is the one
inconsistency in the file's encoding. Nothing needs to be done about it: the engine treats a
two-valued integer column as a category.

**"No internet service" is a third value, not a missing value.** Six of the service columns carry
`Yes`, `No` and `No internet service` (and `MultipleLines` carries `No phone service`). Those are
real categories, and collapsing them into `No` would throw away the fact that the subscriber has no
internet at all.

**It is small.** 7,043 rows is above the engine's 1,000-row floor but well below what a telco would
actually have, so treat the numbers in `run_report.md` as a working demonstration rather than a
benchmark.

## Personal data

None. The file carries no names, addresses, emails, phone numbers or government identifiers;
`customerID` is a synthetic key of the form `7590-VHVEG`. Nothing is dropped for privacy, and the
engine's `PII_DETECTED` check found nothing.

## Rebuilding it

```bash
python library/telco-customer-churn/fetch.py            # download, prepare, rewrite sample.csv
python library/telco-customer-churn/fetch.py --no-download
```

`data/` is git-ignored. `sample.csv` (5,000 rows, seed 20260922) is committed and is what
[`library/tests/test_telco_customer_churn.py`](../tests/test_telco_customer_churn.py) trains on.
