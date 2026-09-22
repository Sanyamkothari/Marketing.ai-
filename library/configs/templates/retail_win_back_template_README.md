# Retail Win-back — upload template

One row per shopper. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | string | Unique shopper identifier from the invoice log. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the shopper as of. Nothing after it may be used. |
| `country` | feature | string | Country the shopper usually orders from |
| `recency_days` | feature | integer | Days between the shopper's last order and the snapshot. At least 90 by construction - this file is the lapsed audience. |
| `tenure_days` | feature | integer | Days between the shopper's first order and the snapshot |
| `orders_total` | feature | integer | Distinct orders placed on or before the snapshot |
| `items_total` | feature | integer | Items bought on or before the snapshot, summed over every order |
| `distinct_products` | feature | integer | Distinct product codes bought on or before the snapshot |
| `active_months` | feature | integer | Calendar months in which the shopper placed at least one order |
| `spend_total` | feature | float | Total order value on or before the snapshot |
| `order_value_mean` | feature | float | Mean value of the shopper's orders |
| `order_value_max` | feature | float | Value of the shopper's largest order |
| `days_between_orders_mean` | feature | float | Mean days between consecutive orders; 0 when the shopper ever placed only one |
| `returned_orders` | feature | integer | Orders the shopper cancelled or returned on or before the snapshot |
| `returned_items` | feature | integer | Items returned on or before the snapshot |
| `reactivated_90d` | target | integer | Target: 1 when the shopper ordered again within 90 days of the snapshot, else 0. Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `reactivated_90d` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
