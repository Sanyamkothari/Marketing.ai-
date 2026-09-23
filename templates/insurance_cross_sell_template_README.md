# Vehicle Policy Cross-sell — upload template

One row per policyholder. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `id` | primary key | integer | Unique policyholder identifier. Never used as a feature. |
| `Gender` | feature | string | Male or Female |
| `Age` | feature | integer | Age of the policyholder in years |
| `Driving_License` | feature | integer | 1 when the policyholder holds a driving licence, else 0 |
| `Region_Code` | feature | float | Code of the policyholder's region. A category code stored as a number, not a quantity. |
| `Previously_Insured` | feature | integer | 1 when the policyholder already has a vehicle policy, else 0 |
| `Vehicle_Age` | feature | string | Age of the vehicle: < 1 Year, 1-2 Year or > 2 Years |
| `Vehicle_Damage` | feature | string | Yes when the vehicle has been damaged in the past, else No |
| `Annual_Premium` | feature | float | Premium the policyholder pays for the year, in rupees |
| `Policy_Sales_Channel` | feature | float | Code of the channel that reached the policyholder. A category code stored as a number, not a quantity. |
| `Vintage` | feature | integer | Days the policyholder has been with the company |
| `Response` | target | integer | Target: 1 when the policyholder was interested in the vehicle policy, else 0. Leave blank when scoring. |

## Required

- `id` — the primary key: it identifies each row and is never used as a feature.
- `Response` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
