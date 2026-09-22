# Health Insurance Cross-Sell

**Industry** Insurance · **Use case** [`insurance-cross-sell`](../configs/use_cases/insurance_cross_sell.yaml) ·
**Target** `Response`

381,109 health-insurance policyholders and whether each said they were interested in a vehicle
policy. The largest file in the library, and the second of the two that need no preparation at all.

| | |
|---|---|
| Upstream | <https://www.kaggle.com/datasets/anmolkumar/health-insurance-cross-sell-prediction> |
| Copy fetched | <https://raw.githubusercontent.com/asyntychaki/Health-Insurance-Cross-Sell/main/data/train.csv> |
| Licence | **Check before you demo it** — reported as GNU GPL v2, unverified. See [LICENCE](LICENSE.txt) |
| Rows | 381,109, one per policyholder |
| Columns | 12 — 1 key, 10 features, 1 target |
| Primary key | `id` |
| Target | `Response`, 0/1; 46,710 positive (12.3 %) |
| Time column | none |
| Config root | `library/configs` |

## Read the licence first

This is the one dataset in the library whose licence could not be read from its own source, because
kaggle.com is blocked by the egress policy of the environment this library was built in. Secondary
sources report the Kaggle licence as **GNU GPL v2**, which is a copyleft software licence rather
than one of the Creative Commons data licences the other datasets carry. Use it for internal
evaluation; confirm the licence on the Kaggle page before it appears in anything you ship or show
to a customer. [`LICENSE.txt`](LICENSE.txt) says the same thing at greater length.

## What the target means

`Response` is 1 when the policyholder said they were interested in the vehicle policy the company
offered them, and 0 otherwise. It is a **stated interest**, not a sale: a model trained on it ranks
people by willingness to hear more, which is what a cross-sell call list is for, and not by
likelihood of paying a premium. Worth saying to a business audience before they read the decile
chart as revenue.

## What `fetch.py` changes

Nothing. The published `train.csv` is already one row per policyholder with a unique `id` and an
outcome column, so `prepared.csv` is a copy. [`mapping.yaml`](mapping.yaml) has `source` and
`standard` identical in every row.

## Known quirks

**Two columns are category codes wearing float clothes.** `Region_Code` (53 distinct values, 0.0 to
52.0) and `Policy_Sales_Channel` (155 distinct values, 1.0 to 163.0) are identifiers, not
quantities: region 52 is not twice region 26. They are **left as published**, because re-typing
them to strings is a modelling decision. A gradient-boosted tree can split on a numeric code and
recover most of the signal; what it cannot do is treat two codes as similar because they are
adjacent, which is the right behaviour here anyway.

**`Driving_License` is almost constant.** 99.8 % of rows are 1. It does not trip the engine's
`CONSTANT_COLUMN` check, which needs exactly one distinct value, so it survives into training and
contributes nothing. That is fine, and it is the kind of column a business user should expect to
see at the bottom of the importance chart.

**`Previously_Insured` almost defines the answer.** A policyholder who already has a vehicle policy
is essentially never interested in another one. It is legitimate — it is known before the offer is
made, not after — but it means a large part of the model's apparent skill is one obvious rule, and
the run report says how much.

**`Annual_Premium` has a long tail.** The median is 31,669 and the maximum is 540,165. The engine's
default outlier handling clips to the 1st–99th percentile, fitted on the training split only.

**`Vintage` is a day count, not a date.** It is how long the policyholder has been on the books (10
to 299 days). Nothing in the file reads as a date, so a time-based split is not available.

**The published train/test split is not used.** Kaggle ships a `test.csv` with no `Response`
column, for leaderboard scoring. The library uses `train.csv` only and lets the engine make its own
train/validation/test split, because a hold-out with no labels cannot be evaluated.

## Personal data

None. `Gender` and `Age` are attributes rather than contact details, and there are no names,
addresses, emails, phone numbers or government identifiers. `id` is a sequential integer. Nothing
is dropped for privacy, and the engine's `PII_DETECTED` check found nothing.

## Rebuilding it

```bash
python library/health-insurance-cross-sell/fetch.py
python library/health-insurance-cross-sell/fetch.py --no-download
```

`data/` is git-ignored (the download is 21 MB). `sample.csv` (5,000 rows, seed 20260922, 619
positives) is committed and is what
[`library/tests/test_health_insurance_cross_sell.py`](../tests/test_health_insurance_cross_sell.py)
trains on.
