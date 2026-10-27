# Guide: building features (read before proposing features)

A feature is a business clue computed from raw rows that shows intent or behaviour. Good features beat clever models.

## The golden rule: nothing from the future
- Every feature uses only rows dated **on or before the snapshot** (`$as_of`). The tool enforces this; never work around it
  with typed dates or hand-written SQL.
- Never use the outcome column, or anything recorded because of the outcome (a cancellation survey, a refund issued after
  churn, a "reactivated" flag), as a feature.
- Training snapshot: at least the outcome window before the data ends (90-day outcome, data ends 9 Dec -> snapshot on or
  before 10 Sep). The scoring date can be later.

## What to build, in order of value (event or order logs)
1. **Recency** - days since the last order. The strongest single churn signal. Keep the number; add buckets only if asked (below).
2. **Frequency** - distinct orders in the last 30 / 90 / 365 days (count orders, not lines).
3. **Monetary** - spend in the same windows; mean, median and max order value (sum lines per order first, then per customer).
4. **Trend (velocity)** - activity in the last 30 days / (activity in the prior 90 days / 3), for orders and for spend.
   Above 1 growing, below 1 decaying. Blank when the prior window is empty, and say so.
5. **Ratios (compared to what?)** - dormancy index = recency_days / tenure_days; return rate = returned items / items;
   spend velocity = spend / tenure months; basket depth = items / orders. Division by zero gives blank, never 0.
6. **Tenure and breadth** - days since the first order; distinct products; active months.
Start from the preset `rfm` (1-3) or `rfm_plus` (1-6) and remove what the data cannot support; explain each removal.

## Returns, cancellations, guests
- Net returns against spend unless the person says otherwise; keep return count and amount as their own features.
- Rows with no customer ID belong to no customer: leave them out, with their count.
- Exact duplicate lines: ask; when they are double scans, drop them before counting.

## Buckets and interactions (only with a business reason)
- **Buckets** follow the use case's own cliffs (win-back: 0-30 days active, 31-90 at risk, 90+ lapsed). Keep the raw
  number beside the bucket. Do not invent cut points.
- **Interactions** (A x B) only when the person's goal names them (e.g. discount-sensitive x high spender). Tree models
  find most others; say why each one is worth adding.

## Leave to training (AutoML)
- One-hot and category handling, scaling, filling blanks for the model: training does these. Do not repeat them.
- Ordered labels (Low / Medium / High): propose a `map_values` step to 1 / 2 / 3 only when the order is certain.
- **Never target-encode** (replacing a category by its outcome rate): it leaks the outcome.

## Checking your features
- Read the receipt: fill rate per feature, constant or almost-blank features (drop them), customers kept and lost.
- A feature that almost perfectly predicts the outcome is a leak until proven otherwise: say so and ask.
- Tell the person the feature list in plain words with the snapshot date, then wait for their yes.
