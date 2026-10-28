# Customer Spend — upload template

One row per customer. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | string | Unique customer identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the customer as of. Nothing after it may be used. |
| `orders_365d` | feature | integer | Orders in the 365 days up to the snapshot |
| `spend_365d` | feature | float | Amount spent in the 365 days up to the snapshot |
| `days_since_last_order` | feature | integer | Days between the last order and the snapshot |
| `spend_next_90d` | target | float | Target: the amount spent in the 90 days after the snapshot (0 when nothing). Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `spend_next_90d` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
