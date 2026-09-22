# Card Default Propensity — upload template

One row per account. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `ID` | primary key | integer | Unique account identifier. Never used as a feature. |
| `LIMIT_BAL` | feature | float | Credit limit of the account, in New Taiwan dollars |
| `SEX` | feature | integer | 1 male, 2 female. A category code stored as a number. |
| `EDUCATION` | feature | integer | 1 graduate school, 2 university, 3 high school, 4 other. Codes 0, 5 and 6 also occur and are not documented upstream. |
| `MARRIAGE` | feature | integer | 1 married, 2 single, 3 other. Code 0 also occurs and is not documented upstream. |
| `AGE` | feature | integer | Age of the account holder in years |
| `PAY_0` | feature | integer | Repayment status in the most recent month: -1 paid duly, 1 one month late, 2 two months late, and so on. Codes -2 and 0 also occur and are not documented upstream. |
| `PAY_2` | feature | integer | Repayment status one month earlier, coded as PAY_0 |
| `PAY_3` | feature | integer | Repayment status two months earlier, coded as PAY_0 |
| `PAY_4` | feature | integer | Repayment status three months earlier, coded as PAY_0 |
| `PAY_5` | feature | integer | Repayment status four months earlier, coded as PAY_0 |
| `PAY_6` | feature | integer | Repayment status five months earlier, coded as PAY_0 |
| `BILL_AMT1` | feature | float | Bill statement amount in the most recent month |
| `BILL_AMT2` | feature | float | Bill statement amount one month earlier |
| `BILL_AMT3` | feature | float | Bill statement amount two months earlier |
| `BILL_AMT4` | feature | float | Bill statement amount three months earlier |
| `BILL_AMT5` | feature | float | Bill statement amount four months earlier |
| `BILL_AMT6` | feature | float | Bill statement amount five months earlier |
| `PAY_AMT1` | feature | float | Amount paid in the most recent month |
| `PAY_AMT2` | feature | float | Amount paid one month earlier |
| `PAY_AMT3` | feature | float | Amount paid two months earlier |
| `PAY_AMT4` | feature | float | Amount paid three months earlier |
| `PAY_AMT5` | feature | float | Amount paid four months earlier |
| `PAY_AMT6` | feature | float | Amount paid five months earlier |
| `default_payment_next_month` | target | integer | Target: 1 when the account defaulted on next month's payment, else 0. Leave blank when scoring. |

## Required

- `ID` — the primary key: it identifies each row and is never used as a feature.
- `default_payment_next_month` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
