# UCI Online Retail → win-back

**Industry** E-commerce · **Use case** [`retail-win-back`](../configs/use_cases/retail_win_back.yaml) ·
**Target** `reactivated_90d` (derived)

The one dataset in the library that does **not** arrive in the data contract's shape. Everything
else here is already one row per entity; this is a 541,909-line invoice log, and turning it into
one row per customer is the Phase 2 work the engine cannot do yet. `fetch.py` does it in the open,
and this file explains every column it produces.

| | |
|---|---|
| Upstream | <https://archive.ics.uci.edu/dataset/352/online+retail> |
| Copy fetched | <https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/data/retail-data/all/online-retail-dataset.csv> |
| Licence | CC BY 4.0. See [LICENCE](LICENSE.txt) |
| Source rows | 541,909 invoice lines, 2010-12-01 to 2011-12-09, 4,372 customers |
| Prepared rows | **1,463**, one per lapsed shopper |
| Columns | 16 — 1 key, 1 snapshot date, 13 features, 1 target |
| Primary key | `customer_id` |
| Target | `reactivated_90d`, 0/1; 604 positive (41.3 %) |
| Time column | `snapshot_date` (present, constant — a single snapshot) |
| Config root | `library/configs` |

## Which "Online Retail" this is

This is **Online Retail** — the 2010-2011 log, 541,909 lines — not **Online Retail II**, the
2009-2011 log of 1,067,371 lines that the brief names. Online Retail II is published on
`archive.ics.uci.edu` as an `.xlsx`, and that host is blocked by the egress policy of the
environment this library was built in. The brief allows "any e-commerce transactions set", and the
two files have the same schema, the same retailer and the same aggregation story; only the date
range differs. Swapping in Online Retail II is a change to `SOURCE_URL` and the three date
constants in `fetch.py`, nothing more.

## What the target means, and how the windows were chosen

A win-back campaign asks *which of the customers who have stopped buying will buy again?* That
needs three dates and one rule, and `fetch.py` fixes all four:

```
SNAPSHOT        2011-09-10     the day the campaign would be planned
LAPSE WINDOW    90 days        a shopper is in the audience when their last purchase was
                               on or before 2011-06-12
OUTCOME WINDOW  90 days        reactivated_90d is 1 when they bought again in
                               (2011-09-10, 2011-12-09]
```

The snapshot is **the last date in the log minus 90 days**, so the outcome window is complete for
every shopper in the file. Nobody is labelled "did not come back" merely because the data ran out —
which is the single most common way a derived churn or win-back label goes wrong.

The audience is the lapsed one, not everybody: 1,463 of the 4,338 identifiable customers. That is
the population a win-back budget is actually spent on, and it is why the positive rate is 41 %
rather than the few per cent a whole-base model would show.

## Leakage: the argument, and where it is checked

**Every feature is an aggregate over invoice lines dated on or before the snapshot.** The only
thing read from after the snapshot is the target. `fetch.py` splits the log into `before` and
`after` frames once, at the top of `build_rows`, and no feature expression touches `after`. The
last line of that function asserts `recency_days >= 90` for every row, which cannot be true if a
post-snapshot purchase had leaked into the recency calculation.

## The derived columns

Each one is a plain count, sum, mean or date difference over the shopper's own invoices — the
arithmetic the engine cannot do because it never sees more than one row per entity. No ratios, no
bins, no encodings, no scaling: the engine does those itself.

| Column | How it is computed (purchases on or before the snapshot) |
|---|---|
| `customer_id` | the log's `CustomerID`, as a string |
| `snapshot_date` | the constant `2011-09-10` |
| `country` | the shopper's most frequent `Country` |
| `recency_days` | snapshot − last purchase date, in days (≥ 90 by construction) |
| `tenure_days` | snapshot − first purchase date, in days |
| `orders_total` | distinct `InvoiceNo` |
| `items_total` | sum of `Quantity` |
| `distinct_products` | distinct `StockCode` |
| `active_months` | distinct calendar months containing a purchase |
| `spend_total` | sum of `Quantity × UnitPrice` |
| `order_value_mean` | mean, over invoices, of the invoice's total value |
| `order_value_max` | the largest single invoice total |
| `days_between_orders_mean` | (last − first) in days ÷ (`orders_total` − 1); **0 for a shopper who only ever ordered once** |
| `returned_orders` | distinct cancelled invoices (`InvoiceNo` beginning `C`) |
| `returned_items` | absolute sum of `Quantity` on cancelled invoices |
| `reactivated_90d` | **target**: 1 when a non-cancelled purchase falls in (snapshot, snapshot + 90 days] |

## Known quirks

**A quarter of the log has no customer.** 135,080 of 541,909 lines carry a blank `CustomerID` —
guest checkouts. They cannot be attributed to anyone, so they are dropped rather than imputed. Any
revenue figure computed from this file is therefore *identified-customer revenue*, not total
revenue.

**Cancellations are negative rows, not a flag.** An invoice number beginning with `C` is a
cancellation and its `Quantity` is negative. They are excluded from the purchase aggregates and
counted separately as `returned_orders` / `returned_items`, so a return never silently cancels out
a purchase inside `spend_total`.

**There are zero and negative unit prices.** 2,517 lines carry `UnitPrice <= 0` — postage
adjustments, manual corrections, samples. They are excluded from the monetary aggregates.

**`days_between_orders_mean` is 0 for one-order shoppers, and that is ambiguous.** A shopper with a
single order has no gap to measure. Zero is a placeholder, and it collides with the (rare) shopper
who placed several orders on one day. `orders_total` disambiguates the two, so the information is
not lost, but a model reading the column alone would be misled.

**`snapshot_date` is the same value in every row,** because this is a single snapshot. The engine
flags it as a constant column and drops it before training — see [`run_report.md`](run_report.md).
It is kept in the file anyway, because it is the one place the snapshot is recorded next to the
data it describes. A multi-snapshot version of this file would set `split.type: time_based` and
`split.time_column: snapshot_date`, and the column would stop being constant.

**1,463 rows is small.** It is above the engine's 1,000-row floor, but only just, and the test
split is about 220 shoppers. Treat the numbers in `run_report.md` accordingly.

## Personal data

None survives. `Description` — free-text product copy, the only column that could carry stray
personal data — is dropped in `read_transactions()` before any aggregation. `CustomerID` is a
pseudonymous retailer key and `Country` is a country name, not an address. No names, emails, phone
numbers or government identifiers exist in the source.

## Rebuilding it

```bash
python library/online-retail/fetch.py
python library/online-retail/fetch.py --no-download
```

`data/` is git-ignored. `sample.csv` is committed; it is all 1,463 rows, because the whole prepared
file is already under the 5,000-row limit. It is what
[`library/tests/test_online_retail.py`](../tests/test_online_retail.py) trains on.
