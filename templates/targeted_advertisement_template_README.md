# Targeted Advertisement — upload template

One row per customer. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | string | Unique customer identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the customer as of. Nothing after it may be used. |
| `visits_last_7d` | feature | integer | Site / app visits in last 7 days |
| `ad_ctr_90d` | feature | float | Click-through rate on past ads (0 to 1) |
| `plan_tier` | feature | string | Current subscription plan |
| `tenure_months` | feature | integer | Months as a customer |
| `region` | feature | string | Customer region |
| `marketing_opt_in` | consent | boolean | Customer agreed to marketing contact. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last marketing contact (blank if never) |
| `converted_30d` | target | integer | Target: 1 if the customer purchased within 30 days of ad exposure, else 0. Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `converted_30d` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
