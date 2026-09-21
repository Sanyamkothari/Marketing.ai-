# RCA (Root Cause Analysis) — upload template

One row per customer. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | string | Unique customer identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the customer as of; used for the time-based split |
| `usage_drop_30d` | feature | float | Usage change vs previous 30 days, in percent (negative = drop) |
| `outages_90d` | feature | integer | Outages experienced, last 90 days |
| `billing_disputes_90d` | feature | integer | Billing disputes, last 90 days |
| `price_change_flag` | feature | boolean | Recent price increase |
| `complaint_text` | feature | text | Latest complaint notes, PII-redacted. Ignored by the Phase 1 model; used by the Phase 3 summary. |
| `marketing_opt_in` | consent | boolean | Customer agreed to retention contact. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last retention contact (blank if never) |
| `churn_next_60d` | target | integer | Target: 1 if the customer disconnected within 60 days, else 0. Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `churn_next_60d` — the target: required for training, leave blank when scoring.
- `snapshot_date` — the time column: this use case splits the data by time, so every row needs it.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
