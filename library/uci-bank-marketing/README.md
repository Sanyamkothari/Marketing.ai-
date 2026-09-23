# UCI Bank Marketing (bank-additional-full)

**Industry** Banking · **Use case** [`bank-term-deposit`](../configs/use_cases/bank_term_deposit.yaml) ·
**Target** `y`

41,188 direct-marketing calls made by a Portuguese retail bank between May 2008 and November 2010.
The question is the one every outbound campaign asks: *which client is worth calling?*

| | |
|---|---|
| Upstream | <https://archive.ics.uci.edu/dataset/222/bank+marketing> |
| Copy fetched | <https://raw.githubusercontent.com/llhthinker/MachineLearningLab/master/UCI%20Bank%20Marketing%20Data%20Set/data/bank-additional/bank-additional-full.csv> |
| Licence | CC BY 4.0. See [LICENCE](LICENSE.txt) |
| Rows | 41,188 calls |
| Columns | 22 after preparation — 1 derived key, 20 features, 1 target |
| Primary key | `client_id` (derived, see below) |
| Target | `y`, `yes` / `no`; 4,640 `yes` (11.3 %) |
| Time column | none usable |
| Config root | `configs`, the repository's own (DEC-098) |

## What the target means

`y` is `yes` when the client subscribed to a term deposit after the campaign call, and `no`
otherwise. It is the outcome of the call the row describes.

## What `fetch.py` changes, and why

Three format problems stand between the published file and an upload the engine accepts. None of
them is a modelling decision.

1. **Semicolons, not commas.** The published file is `;`-separated with every text field quoted.
   The data contract asks for comma-separated UTF-8 CSV, so `fetch.py` re-writes it as one.
2. **No primary key.** The contract needs a column that identifies each row, and the file ships
   none. The rows are published in the order the calls were made, so a 1-based row number in that
   order identifies a row stably; it is added as `client_id`. The engine never uses a key as a
   feature.
3. **Four dotted column names.** `emp.var.rate`, `cons.price.idx`, `cons.conf.idx` and
   `nr.employed` become `emp_var_rate`, `cons_price_idx`, `cons_conf_idx` and `nr_employed`,
   because a template column name has to match `^[A-Za-z_][A-Za-z0-9_]*$`. Only the punctuation
   changes; [`mapping.yaml`](mapping.yaml) records every rename.

Nothing is imputed, binned, encoded or dropped.

## Known quirks

**`duration` is the famous one, and it is deliberately left in.** It is the length of the call in
seconds — a number that does not exist until the call has ended. UCI's own notes say it "highly
affects the output target" and "should be discarded if the intention is to have a realistic
predictive model". Dropping it in `fetch.py` would have been the safe choice; leaving it in asks a
better question, which is whether *the engine* notices. What the engine actually said about it is
in [`run_report.md`](run_report.md), and the config-only fix — `prepare.exclude_columns: [duration]`
— is recorded there too.

**`pdays` uses 999 as a sentinel.** It is the number of days since the client was last contacted in
a previous campaign, and `999` means *never contacted before*, not "999 days ago". 39,673 of the
41,188 rows carry it. A model reads it as a very large number rather than as a category, which is a
real and slightly wrong thing for it to do; the honest fix is a separate `never_contacted` flag,
which is feature engineering and therefore not something this library does on the engine's behalf.

**`unknown` is a real category, not a null.** `job`, `marital`, `education`, `default`, `housing`
and `loan` all carry a literal `unknown` value. It is left as published: the engine's missing-value
handling never sees a null here, and "the bank does not know" is itself informative.

**The five macro-economic columns vary by date, not by client.** `emp_var_rate`, `cons_price_idx`,
`cons_conf_idx`, `euribor3m` and `nr_employed` are quarterly, monthly and daily indicators
describing the economy when the call was made. Two clients called the same week share them. They
are strong predictors precisely because the campaign's success moved with the interest-rate cycle —
which is worth saying out loud to a business audience, because a model leaning on them is partly
predicting the calendar.

**There is a month but no year.** `month` and `day_of_week` are `may` and `mon`; the file never
says which May. So nothing in it reads as a date, no time-based split is possible, and the split
stays random. That is a genuine limitation: the file is ordered in time, so a time-based split
would be the more honest evaluation, and it cannot be done from what is published.

## Personal data

None. Age, job, marital status and education are attributes, not contact details; there are no
names, addresses, emails, phone numbers or government identifiers. `client_id` is a row number.
Nothing is dropped for privacy.

## Rebuilding it

```bash
python library/uci-bank-marketing/fetch.py
python library/uci-bank-marketing/fetch.py --no-download
```

`data/` is git-ignored. `sample.csv` (5,000 rows, seed 20260922, 575 positives) is committed and is
what [`library/tests/test_uci_bank_marketing.py`](../tests/test_uci_bank_marketing.py) trains on.
