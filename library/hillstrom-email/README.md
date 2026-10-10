# MineThatData E-Mail Analytics (Hillstrom)

**Industry** E-commerce · **Use case** [`hillstrom-email`](../../configs/use_cases/hillstrom_email.yaml) ·
**Treatment** `segment` (randomised, three groups) · **Target** `conversion` · **Amount** `spend`

64,000 customers of a merchandise retailer who last bought within twelve months. In 2008 they were
split **at random** into three equal groups for two weeks: one was sent an e-mail featuring men's
merchandise, one an e-mail featuring women's merchandise, and one was sent nothing. The file records
what each customer had done before (recency, spend, which merchandise, area, channel) and what they did
in the two weeks after (a website visit, a purchase, the dollars spent).

It is the library's first dataset whose treatment was assigned at random, and so the first on which the
whole Plan J journey can be validated with real effects rather than planted ones: which e-mail, if any,
each customer should get, what that list is worth, and whether that can be proven (Plan J M110, DEC-1320).
The results, whichever way they fell, are in [`run_report.md`](run_report.md).

| | |
|---|---|
| Source | Kevin Hillstrom, MineThatData, "E-Mail Analytics And Data Mining Challenge", March 2008 |
| Canonical URL | <http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv> — did not resolve from this environment (2026-10-09, 2026-10-10) |
| Mirror used | <https://hillstorm1.s3.us-east-2.amazonaws.com/hillstorm_no_indices.csv.gz>, the file scikit-uplift's `fetch_hillstrom` reads |
| Catalogued | TensorFlow Datasets, [`hillstrom`](https://www.tensorflow.org/datasets/catalog/hillstrom) |
| SHA-256 | `00a6a868e05a9ffe7382da51629f6d6dce88c5acfc945e79d314ebc78fd3a2c0` (the decompressed CSV; `fetch.py` refuses any other) |
| Licence | **No explicit licence. Check before commercial redistribution.** See [LICENCE](LICENSE.txt) |
| Approval | the product owner approved obtaining the file on 2026-10-09 |
| Rows | 64,000, one per customer: 21,307 men's e-mail, 21,387 women's e-mail, 21,306 no e-mail |
| Columns | 12 published + `customer_id` added |
| Primary key | `customer_id` (added: the 1-based row number) |
| Outcomes | `visit`, `conversion` (578 in all, 0.9 %), `spend` (dollars) |
| Time column | none |
| Config root | `configs`, the repository's own (DEC-085) |

## Read the licence first

The file was released publicly by its author for his 2008 challenge and is redistributed by
TensorFlow Datasets and scikit-uplift, but no licence statement accompanies it anywhere it could be
read. Its terms of reuse are therefore not known. It is used here for **internal validation**, which
the product owner approved; ask the author before redistributing it or anything derived from it
commercially. [`LICENSE.txt`](LICENSE.txt) records the same.

## Keeping the data out of git

Exactly as the other datasets: `fetch.py` writes the verified raw file and `prepared.csv` into
`data/`, which `.gitignore` excludes, and journey runs write under `library/.runs/`, also ignored.
Only [`sample.csv`](sample.csv) is committed: a tenth of the rows (6,401), drawn at random within each
of the three groups by a fixed seed, for the library's own tests. The full file never is.

```bash
python library/hillstrom-email/fetch.py                       # canonical URL, then the mirror; verified
python library/hillstrom-email/fetch.py --from PATH/hillstrom.csv   # a copy you already have; verified
```

## What `fetch.py` changes

One thing: a `customer_id` column, the row number in the published order, because the file has no key.
Every published column keeps its name and values, including the `Surburban` spelling in `zip_code`.
[`mapping.yaml`](mapping.yaml) gives each column's role.

## What each column is for

| Column | Role | Why |
|---|---|---|
| `recency`, `history_segment`, `history`, `mens`, `womens`, `zip_code`, `newbie`, `channel` | features | all known before the e-mail |
| `segment` | **the treatment** | assigned at random; the campaign-effect run reads it as the lever (three levels, `No E-Mail` the control), and the risk model excludes it |
| `conversion` | target | a purchase in the two weeks after |
| `visit`, `spend` | outcomes, excluded | they happen after the e-mail, so a model that used them would be reading the answer |

Two columns are derived by the journey, not by `fetch.py`, and both carry an assumption that the run
report repeats: `value_inr` (each customer's value per conversion: their spend history scaled to the size
of one order, the scale measured on training rows only, in rupees at an assumed exchange rate) and
`history_date` (the day before the campaign, assigned by assumption as the date the pre-campaign spend
`history` is measured up to). See [`mapping.yaml`](mapping.yaml).

## Known quirks

**Conversions are rare.** 0.57 % of the no-e-mail group bought, 1.25 % of the men's e-mail group and 0.88 % of the women's. A
model that ranks customers by how much an e-mail changes a probability that small needs many rows per
group, and the training half of the file has about a hundred conversions per group. The run report says
what that did to the campaign-effect model.

**Spend is long-tailed.** Most customers spend nothing; a few spend hundreds of dollars. The engine
flags this (`OUTCOME_SKEWED`) rather than trimming it, and the pre-campaign amount `history` is the
registered adjustment (CUPED, M102).

**Money is in 2008 dollars.** The engine's money is rupees. The journey converts at an exchange rate
that is an input assumption, stated in the report, and every rupee figure scales with it.

**No dates.** The file says the outcomes cover the two weeks after the e-mail and the history covers the
year before; it carries no date column. The journey dates the history the day before the campaign it
records. That date is assigned by assumption, so the engine's point-in-time rule
(`COVARIATE_NOT_BEFORE_CAMPAIGN`) passes by construction: it is not a check on this file.
