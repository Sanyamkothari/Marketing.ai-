# UCI Default of Credit Card Clients

**Industry** Banking · **Use case** [`card-default-propensity`](../configs/use_cases/card_default_propensity.yaml) ·
**Target** `default_payment_next_month`

30,000 Taiwanese credit-card accounts observed from April to September 2005, with whether each one
defaulted the month after. The payment-propensity question, on real repayment history.

| | |
|---|---|
| Upstream | <https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients> |
| Copy fetched | <https://raw.githubusercontent.com/YuChenAmberLu/Data-Science--Credit-Card-Default/master/UCI_Credit_Card.csv> |
| Licence | CC BY 4.0. See [LICENCE](LICENSE.txt) |
| Rows | 30,000, one per account |
| Columns | 25 — 1 key, 23 features, 1 target |
| Primary key | `ID` |
| Target | `default_payment_next_month`, 0/1; 6,636 positive (22.1 %) |
| Time column | none |
| Config root | `library/configs` |

## What the target means

The published name is `default.payment.next.month`: 1 when the account failed to make the minimum
payment in the month **after** the six-month observation window the features describe, 0 otherwise.
The features stop at September 2005; the outcome is October 2005. That gap is what makes the label
a prediction target rather than a description.

## What `fetch.py` changes, and why

One thing: `default.payment.next.month` becomes `default_payment_next_month`, because a template
column name has to match `^[A-Za-z_][A-Za-z0-9_]*$` and the published name has dots in it. Nothing
else is touched — not the undocumented category codes, not the negative bill amounts, not the
column that is named `PAY_0` where it should be `PAY_1`. [`mapping.yaml`](mapping.yaml) records the
rename.

## The columns

`ID` identifies the account. `LIMIT_BAL` is the credit limit. `SEX`, `EDUCATION`, `MARRIAGE` and
`AGE` describe the holder. Then three six-month series, most recent month first:

* `PAY_0`, `PAY_2` … `PAY_6` — repayment status
* `BILL_AMT1` … `BILL_AMT6` — bill statement amount
* `PAY_AMT1` … `PAY_AMT6` — amount actually paid

## Known quirks

**The category codes do not match the documentation.** `EDUCATION` is documented as 1 graduate
school, 2 university, 3 high school, 4 others — but 0, 5 and 6 all occur in the data and none of
them is explained anywhere upstream. `MARRIAGE` is documented as 1, 2, 3 and carries a 0 as well.
They are **left as published**. Collapsing 0, 5 and 6 into "other" is the usual tidy-up, and it is
a modelling decision this library does not make on the engine's behalf: a tree model treats them as
their own categories, which is a defensible reading of "we do not know what this code means".

**`PAY_0` should be `PAY_1`.** The series runs `PAY_0, PAY_2, PAY_3, PAY_4, PAY_5, PAY_6`, so the
most recent month is the one with the odd name. Renaming it would make the file read better and
would break every notebook and paper written against it, so it stays. It is also, unsurprisingly,
the single most important feature the engine found — see [`run_report.md`](run_report.md).

**The repayment codes carry undocumented values too.** The documentation defines −1 (paid duly), 1
(one month late), 2 (two months late) and so on up to 9. The data also contains −2 and 0, in very
large numbers, and neither is defined. The usual reading is that −2 means "no balance to pay" and 0
means "paid the minimum, revolving credit", but that is inference, not documentation.

**Bill amounts go negative.** `BILL_AMT*` reaches −339,603. A negative statement balance is an
overpayment or a credit, which is real; it is not an error to clean.

**It is a single cohort from 2005.** Every account is observed over the same six months in the same
market. There is no time dimension to split on and nothing to say about how the model would travel
to another period — which is exactly what makes `split.type: time_based` unavailable here.

## Personal data

None. `SEX`, `EDUCATION`, `MARRIAGE` and `AGE` are attributes and could be used for fairness
reporting (`evaluation.fairness_column` takes one column name if you want that); there are no
names, addresses, emails, phone numbers or government identifiers. `ID` is a sequential integer.
Nothing is dropped for privacy, and the engine's `PII_DETECTED` check found nothing.

## Rebuilding it

```bash
python library/uci-credit-default/fetch.py
python library/uci-credit-default/fetch.py --no-download
```

`data/` is git-ignored. `sample.csv` (5,000 rows, seed 20260922, 1,101 positives) is committed and
is what [`library/tests/test_uci_credit_default.py`](../tests/test_uci_credit_default.py) trains
on.
