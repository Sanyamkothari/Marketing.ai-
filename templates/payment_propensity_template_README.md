# Payment Propensity — upload template

One row per account. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `account_id` | primary key | string | Unique billing account identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the account as of (before the due date) |
| `late_payments_6m` | feature | integer | Late payments in last 6 months |
| `autopay_enabled` | feature | boolean | Autopay is active |
| `bill_change_pct` | feature | float | Bill change vs 3-month average, in percent |
| `tenure_months` | feature | integer | Months as a customer |
| `plan_type` | feature | string | Prepaid / postpaid plan |
| `marketing_opt_in` | consent | boolean | Customer agreed to marketing contact. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last reminder sent (blank if never) |
| `late_or_missed_payment` | target | integer | Target: 1 if the invoice was paid late or not paid, else 0. Leave blank when scoring. |

## Required

- `account_id` — the primary key: it identifies each row and is never used as a feature.
- `late_or_missed_payment` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
