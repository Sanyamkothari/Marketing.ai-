# Term Deposit Conversion — upload template

One row per client. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `client_id` | primary key | integer | Row number in the published call order. Identifies the client; never used as a feature. |
| `age` | feature | integer | Age of the client in years |
| `job` | feature | string | Type of job: admin., blue-collar, entrepreneur, housemaid, management, retired, self-employed, services, student, technician, unemployed or unknown |
| `marital` | feature | string | Marital status: divorced, married, single or unknown |
| `education` | feature | string | Highest education: basic.4y, basic.6y, basic.9y, high.school, illiterate, professional.course, university.degree or unknown |
| `default` | feature | string | Has credit in default: yes, no or unknown |
| `housing` | feature | string | Has a housing loan: yes, no or unknown |
| `loan` | feature | string | Has a personal loan: yes, no or unknown |
| `contact` | feature | string | How the client was reached: cellular or telephone |
| `month` | feature | string | Month of the last contact, as a three-letter abbreviation. No year is published. |
| `day_of_week` | feature | string | Weekday of the last contact: mon, tue, wed, thu or fri |
| `duration` | feature | integer | Length of the last contact in seconds. Known only after the call ends, so a model that uses it cannot be used to plan the call. See README.md. |
| `campaign` | feature | integer | Contacts made to this client during this campaign, including the last one |
| `pdays` | feature | integer | Days since the client was last contacted in a previous campaign; 999 means never contacted before |
| `previous` | feature | integer | Contacts made to this client before this campaign |
| `poutcome` | feature | string | Outcome of the previous campaign: failure, nonexistent or success |
| `emp_var_rate` | feature | float | Employment variation rate, quarterly indicator |
| `cons_price_idx` | feature | float | Consumer price index, monthly indicator |
| `cons_conf_idx` | feature | float | Consumer confidence index, monthly indicator |
| `euribor3m` | feature | float | Three-month Euribor rate, daily indicator |
| `nr_employed` | feature | float | Number of employees, quarterly indicator, in thousands |
| `y` | target | string | Target: yes when the client subscribed to a term deposit, else no. Leave blank when scoring. |

## Required

- `client_id` — the primary key: it identifies each row and is never used as a feature.
- `y` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
