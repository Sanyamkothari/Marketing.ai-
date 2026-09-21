# Order Fulfillment — upload template

One row per order. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `order_id` | primary key | string | Unique order identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the order was placed; used for the time-based split |
| `carrier_ontime_30d` | feature | float | Carrier on-time rate, last 30 days (0 to 1) |
| `stock_cover_days` | feature | float | Days of stock left for ordered items |
| `distance_km` | feature | float | Warehouse → customer distance in kilometres |
| `orders_same_day` | feature | integer | Order volume on the same day |
| `weather_alert` | feature | boolean | Severe weather on delivery route |
| `marketing_opt_in` | consent | boolean | Customer agreed to proactive notifications. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last proactive notification (blank if never) |
| `delayed_beyond_sla` | target | integer | Target: 1 if delivered after the promised SLA date, else 0. Leave blank when scoring. |

## Required

- `order_id` — the primary key: it identifies each row and is never used as a feature.
- `delayed_beyond_sla` — the target: required for training, leave blank when scoring.
- `snapshot_date` — the time column: this use case splits the data by time, so every row needs it.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
