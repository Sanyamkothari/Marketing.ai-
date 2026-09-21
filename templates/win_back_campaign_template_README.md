# Win-back Campaign — upload template

One row per customer. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | string | Unique identifier of the churned customer. Never used as a feature. |
| `snapshot_date` | time | date | Date the features describe the customer as of |
| `churn_reason` | feature | string | Stated reason for leaving |
| `months_since_churn` | feature | integer | Months since disconnection |
| `avg_monthly_spend` | feature | float | Average past monthly bill |
| `offers_accepted` | feature | integer | Past offers accepted |
| `preferred_channel` | feature | string | Email / SMS / call |
| `marketing_opt_in` | consent | boolean | Customer agreed to marketing contact. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last win-back contact (blank if never) |
| `reactivated_90d` | target | integer | Target: 1 if the customer rejoined within 90 days of an offer, else 0. Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `reactivated_90d` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
