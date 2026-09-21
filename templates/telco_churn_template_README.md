# Telco Customer Churn — upload template

One row per subscriber. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customerID` | primary key | string | Unique subscriber identifier. Never used as a feature. |
| `gender` | feature | string | Female or Male |
| `SeniorCitizen` | feature | integer | 1 if the subscriber is a senior citizen, else 0 |
| `Partner` | feature | string | Has a partner: Yes or No |
| `Dependents` | feature | string | Has dependents: Yes or No |
| `tenure` | feature | integer | Months the subscriber has stayed with the company |
| `PhoneService` | feature | string | Has phone service: Yes or No |
| `MultipleLines` | feature | string | Yes, No, or No phone service |
| `InternetService` | feature | string | DSL, Fiber optic, or No |
| `OnlineSecurity` | feature | string | Yes, No, or No internet service |
| `OnlineBackup` | feature | string | Yes, No, or No internet service |
| `DeviceProtection` | feature | string | Yes, No, or No internet service |
| `TechSupport` | feature | string | Yes, No, or No internet service |
| `StreamingTV` | feature | string | Yes, No, or No internet service |
| `StreamingMovies` | feature | string | Yes, No, or No internet service |
| `Contract` | feature | string | Month-to-month, One year, or Two year |
| `PaperlessBilling` | feature | string | Billed paperlessly: Yes or No |
| `PaymentMethod` | feature | string | Electronic check, Mailed check, Bank transfer (automatic), or Credit card (automatic) |
| `MonthlyCharges` | feature | float | Amount charged to the subscriber this month |
| `TotalCharges` | feature | float | Amount charged to the subscriber in total; blank for a subscriber whose tenure is 0 |
| `Churn` | target | string | Target: Yes if the subscriber left within the month, else No. Leave blank when scoring. |

## Required

- `customerID` — the primary key: it identifies each row and is never used as a feature.
- `Churn` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
