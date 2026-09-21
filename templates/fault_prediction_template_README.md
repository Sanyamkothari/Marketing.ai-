# Fault Prediction — upload template

One row per asset. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `asset_id` | primary key | string | Unique network asset identifier. Never used as a feature. |
| `snapshot_date` | time | date | Date the telemetry window ends; used for the time-based split |
| `error_rate_24h` | feature | float | Errors per hour, 24h window |
| `latency_p95_1h` | feature | float | 95th percentile latency in milliseconds, 1h window |
| `firmware_age_days` | feature | integer | Days since last firmware update |
| `tickets_90d` | feature | integer | Service tickets in last 90 days |
| `device_model` | feature | string | Hardware model |
| `marketing_opt_in` | consent | boolean | Account agreed to proactive service contact. Rows that are false are suppressed. |
| `last_contacted_at` | contact | date | Date of the last proactive service contact for this asset (blank if never) |
| `fault_next_72h` | target | integer | Target: 1 if a fault occurred within 72 hours, else 0. Leave blank when scoring. |

## Required

- `asset_id` — the primary key: it identifies each row and is never used as a feature.
- `fault_next_72h` — the target: required for training, leave blank when scoring.
- `snapshot_date` — the time column: this use case splits the data by time, so every row needs it.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
