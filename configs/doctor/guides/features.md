# Guide: building features (read before proposing features)

## Rules
- Every feature uses only rows dated on or before `$as_of`; the tool enforces it; no typed dates or hand-written SQL workarounds.
- Never use the outcome column, or anything recorded because of it (cancellation survey, refund after churn, "reactivated" flag), as a feature.
- Training snapshot is at least the outcome window before the data ends; scoring can be later.
- Count orders, not lines (sum lines per order first). Division by zero gives blank, never 0.
- Never target-encode: it leaks.
- A feature that almost perfectly predicts the outcome is a leak until proven otherwise: say so and ask.
- Give the person the feature list in plain words with the snapshot date; wait for yes.

## What to build
1. Recency: days since last order, the strongest churn signal. Keep the number; buckets only if asked.
2. Frequency: distinct orders in the last 30 / 90 / 365 days.
3. Monetary: spend in the same windows; mean, median, max order value.
4. Trend: activity in the last 30 days / (prior 90 days / 3), for orders and spend. Above 1 growing, below 1 decaying; blank when the prior window is empty.
5. Ratios: dormancy index = recency_days / tenure_days; return rate = returned items / items; spend velocity = spend / tenure months; basket depth = items / orders.
6. Tenure and breadth: days since first order; distinct products; active months.
Start with 1-3, then 4-6; remove what the data cannot support, explaining why.

## Returns and guests
- Net returns against spend unless told otherwise; return count and amount are own features.
- No customer ID: leave out, with count. Exact duplicate lines: ask; drop double scans before counting.

## Buckets and interactions
- Buckets follow the use case's cliffs (win-back: 0-30 active, 31-90 at risk, 90+ lapsed), raw number kept beside; no invented cut points.
- Interactions (A x B) only when the goal names them; say why each is worth adding.

## Leave to training (AutoML)
- One-hot, scaling, filling blanks: training does them.
- Ordered labels (Low / Medium / High): `map_values` to 1 / 2 / 3 only when the order is certain.

## Receipt
- Fill rate per feature; drop constant or almost-blank ones; customers kept/lost.
