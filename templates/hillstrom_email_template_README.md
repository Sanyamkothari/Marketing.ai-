# E-mail Offer Choice — upload template

One row per customer. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `customer_id` | primary key | integer | Row number in the published order. Identifies the customer; never used as a feature. |
| `recency` | feature | integer | Months since the customer's last purchase |
| `history_segment` | feature | string | Band of last year's spend, as published |
| `history` | feature | float | Dollars spent in the past year, before the e-mail |
| `mens` | feature | integer | 1 when the customer bought men's merchandise in the past year |
| `womens` | feature | integer | 1 when the customer bought women's merchandise in the past year |
| `zip_code` | feature | string | Area type: Urban, Surburban (spelled so in the file) or Rural |
| `newbie` | feature | integer | 1 when the customer is new in the past twelve months |
| `channel` | feature | string | How the customer bought in the past year: Phone, Web or Multichannel |
| `segment` | feature | string | The e-mail the customer was sent, assigned at random: Mens E-Mail, Womens E-Mail or No E-Mail. THE LEVER: the campaign-effect run reads it as the treatment and the risk model excludes it. The template has no treatment role, so it is listed as a feature. Leave out when scoring. |
| `visit` | feature | integer | Outcome: 1 when the customer visited the website in the two weeks after. Excluded from the features. Leave out when scoring. |
| `spend` | feature | float | Outcome: dollars spent in the two weeks after. Excluded from the features. Leave out when scoring. |
| `conversion` | target | integer | Target: 1 when the customer bought in the two weeks after, else 0. Leave blank when scoring. |

## Required

- `customer_id` — the primary key: it identifies each row and is never used as a feature.
- `conversion` — the target: required for training, leave blank when scoring.

## Limits

At least 1,000 rows and 200 positive examples; maximum file size 2048 MB.
