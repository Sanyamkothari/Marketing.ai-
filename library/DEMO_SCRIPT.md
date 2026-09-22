# Demo script — one page

Which dataset to show to whom, and what to click. Every number quoted below is in that dataset's
`run_report.md`; none of it is a forecast.

**Before you start.** `python library/<dataset>/fetch.py` for the dataset you are showing, so the
prepared file exists. Datasets 2–5 need `--config-root library/configs` (DEC-400).
**Never demo Criteo Uplift — it is CC BY-NC-SA, non-commercial.**

---

## Telecom → Telco Customer Churn

*The one to open with. It is the fastest, and it needs no configuration at all.*

1. **Overview** → Telecom → Churn → **Telco Customer Churn**. Say: this use case ships in the
   product; nothing was added for this demo.
2. **Upload** `library/telco-customer-churn/data/prepared.csv`. Point out it is the Kaggle file
   unchanged — 7,043 rows, 21 columns, blanks and all.
3. **Validation** — empty. *"The file was accepted as published."*
4. **Run training** with defaults. It finishes in about **20 seconds**.
5. **Model page** — ROC-AUC **0.857**, baseline 0.854. Say the honest thing: *"the ensemble barely
   beats a logistic regression here, and the point is that the whole path ran in twenty seconds."*
6. **Output page** — the decile chart. Top decile churns at **80 %** against a book average of
   27 %: **3.0× lift**, and it holds **30 %** of all churners.
7. **Explanations** — `tenure` and `Contract` are 76 % of the model. *"New subscribers on
   month-to-month contracts. The lever is the contract, not the network."*

**Close on:** one upload, no configuration, a call list you could work tomorrow.

---

## Banking → UCI Bank Marketing

*The best story in the library, and the only one that needs two runs. Allow ten minutes.*

1. **Upload** `library/uci-bank-marketing/data/prepared.csv` — 41,188 real campaign calls.
2. **Run training** with defaults. About **70 seconds**.
3. **Model page** — ROC-AUC **0.950**. Decile 1 subscribes at **66 %** against 11 %: **5.8× lift**.
   Let them enjoy it.
4. **Explanations** — then stop on the top row. **`duration` is 73.5 % of the model**: the length
   of the call, in seconds. *"We do not know that until after we have made the call. This model
   cannot sort a call list."*
5. **Advanced settings → Data preparation → Exclude columns → `duration`.** Re-run. One setting, no
   code.
6. **Model page again** — ROC-AUC **0.783**, decile 1 at **49 %**, **4.4× lift**, still capturing
   **44 %** of all subscriptions. *"That is the model you deploy."*
7. **Explanations again** — the employment variation rate is now half the model. *"This campaign
   succeeded when rates made deposits attractive. Your ranking will need retraining when rates
   move, which is what the drift monitor is for."*

**Close on:** the validator did not catch `duration` and could not have — it does not contain the
answer, it just does not exist yet. The engine gave you the ranking, the explanation that exposed
the problem, and the one-line fix.

---

## Insurance → Health Insurance Cross-Sell

*The scale story. 381,109 rows, and the most actionable finding in the library.*

1. **Upload** `library/health-insurance-cross-sell/data/prepared.csv` — **381,109 policyholders**.
   Let the row count land.
2. **Run training** with defaults. **Under three minutes.**
3. **Model page** — ROC-AUC **0.856** against a baseline of 0.837. Point at PR-AUC (0.360) and say
   why: at a 12 % base rate, ranking well and being precise are different achievements.
4. **Output page** — the decile chart is the moment. Top decile **39 %** interested against 12 %:
   **3.2× lift**. Then the bottom half: deciles 6–10 are at **0.09× and below — effectively zero**.
   *"Half your book is not worth a call. The top three deciles reach 79 % of the opportunity at
   30 % of the contact cost."*
5. **Explanations** — `Previously_Insured` is **57 %** of the model. *"People who already have the
   policy. Your best model starts as list hygiene — and it found that itself."*

**Caveat to say out loud:** `Response` is stated interest, not a signed policy. This ranks a call
list; it does not forecast revenue.

---

## Banking (second) → UCI Credit Default

*Show this when someone asks whether the AutoML is doing anything a logistic regression would not.*

1. **Upload** `library/uci-credit-default/data/prepared.csv` — 30,000 card accounts.
2. **Run training** with defaults. About **50 seconds**.
3. **Model page** — ROC-AUC **0.787** against a baseline of **0.725**: **+0.062**, the widest
   margin in the library. *"Ten times the gap we saw on the churn file. The reason is interaction —
   what a late payment means depends on the five months around it, and a linear model cannot say
   that."*
4. **Output page** — decile 1 defaults at **72 %** against 22 %: **3.3× lift**, capturing a third
   of next month's defaults. The curve falls monotonically with no inversions.
5. **Explanations** — `PAY_0`, last month's repayment status, is **49 %** on its own, and the
   remaining repayment columns rank in order of recency. *"It rediscovered that the last month
   matters most. That is a model reading the data, not the noise."*

**Close on:** this is where the model search earns its licence fee.

---

## E-commerce → UCI Online Retail (win-back)

*Only show this to a technical audience, and only to make a point about honesty.*

1. Explain first: this is a **transaction log**, 541,909 invoice lines, not one row per customer.
   `library/online-retail/fetch.py` aggregates it — that work becomes part of the product in
   Phase 2.
2. **Upload** `library/online-retail/data/prepared.csv` — 1,463 lapsed shoppers, 41 % of whom came
   back.
3. **Validation** — one warning: `CONSTANT_COLUMN` on `snapshot_date`. *"Single snapshot. It tells
   you, and drops it."*
4. **Run training** with defaults.
5. **Model page** — **the model loses to its own baseline.** PR-AUC 0.498 against 0.503;
   `model_beats_baseline: false`. Say it straight: *"Fourteen months of invoices, no campaign
   history, no contact log, no offer data. There is not enough here, and the system says so
   instead of showing you a number."*
6. **Output page** — the decile curve is flat and not even monotonic: D2 outranks D1.
7. **What would fix it:** campaign history, contact log, offer type and discount depth. Not a
   better algorithm — more columns.

**Close on:** a platform that only ever reports success is one you cannot trust when it reports
success.

---

## Ad-tech → Criteo Uplift — **do not demo**

CC BY-NC-SA 4.0: **non-commercial**. It may not appear in a sales deck, a customer pilot or a
shipped product.

If the conversation turns to incrementality, use it verbally: *"Ranking who will convert and
ranking who converts because of the ad are different questions. The second is Phase 3b, the
dataset and the mapping are already prepared in the repository, and we are not going to show you a
propensity model and call it uplift."* See [`criteo-uplift/README.md`](criteo-uplift/README.md).

---

## If you only have ten minutes

**Telco** (3 min) → **Bank, both runs** (7 min). One shows that the product works out of the box;
the other shows that it tells you the truth about its own output. Nothing else in the library beats
that pair.

## Questions you will get

| Question | Answer |
|---|---|
| "Did you tune these?" | No. Engine defaults, no overrides, except the one `exclude_columns` line in the bank's second run. |
| "How long does it take on our data?" | The largest file here is 381,109 rows and took under three minutes on four cores. |
| "What if our data is transactions, not customers?" | Today someone aggregates upstream — we show exactly how, in `library/online-retail/fetch.py`. In Phase 2 the product does it. |
| "Can it tell when data is bad?" | It catches columns that contain the answer, and it told us `snapshot_date` was constant. It did **not** catch `duration`, and we will show you why no automated check could. |
| "Will it always find a model?" | No — and the win-back dataset is in the library precisely because it did not. |
