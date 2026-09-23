# Demo script — one page

Which dataset to show to whom, and what to click. Every number quoted below is in that dataset's
`run_report.md`; none of it is a forecast.

**Before you start.** `python library/<dataset>/fetch.py` for the dataset you are showing, so the
prepared file exists. Every dataset's use case ships in the repository's own `configs/` (DEC-098),
so the overview's industry selector reaches all of them and no `--config-root` is needed.
**Never demo Criteo Uplift — it is CC BY-NC-SA, non-commercial.**

> ### Do not train live at the default budget
>
> The committed reports are engine defaults: `strategy: balanced`, `time_limit_minutes: 30`,
> `tuning_trials: 50`. On this hardware that is **10 to 22 minutes per run**, and you do not want
> to stand in front of a customer for twenty-two minutes watching a progress bar.
>
> For anything performed live, set **Model search → Strategy: `fast`** and **Time limit: 1 minute**
> in Advanced settings. That finishes in 20–60 seconds and produces every artefact — the same
> validation report, leaderboard, decile chart and explanations, on a smaller search. Say so out
> loud: *"this is the one-minute setting; the numbers in our library are the full thirty-minute
> budget, and they are in the repository."*
>
> Better still, for the numbers below: **run it beforehand and show the finished run.** The Output
> and Model pages are what sells; the progress bar is not.

---

## Telecom → Telco Customer Churn

*The one to open with. It needs no configuration at all.*

1. **Overview** → Telecom → Churn → **Telco Customer Churn**. Say: this use case ships in the
   product; nothing was added for this demo.
2. **Upload** `library/telco-customer-churn/data/prepared.csv`. Point out it is the Kaggle file
   unchanged — 7,043 rows, 21 columns, blanks and all.
3. **Validation** — empty. *"The file was accepted as published."*
4. **Run training.** At the default budget this is ten minutes and 111 candidate models; at the
   one-minute setting, under a minute.
5. **Model page** — ROC-AUC **0.845**, baseline 0.841. Say the honest thing: *"111 models and ten
   minutes bought four thousandths over a logistic regression. The point is not the cleverness —
   it is that the whole path ran unattended, and told us the honest size of its own advantage."*
6. **Output page** — the decile chart. Top decile churns at **79 %** against a book average of
   27 %: **3.0× lift**, holding **30 %** of all churners. The top three hold **68 %**.
7. **Explanations** — `tenure`, `Contract` and `InternetService` are **74 %** of the model. *"New
   subscribers, month-to-month, on fibre. The lever is the contract, not the network."*

**Close on:** one upload, no configuration, a call list you could work tomorrow.

---

## Banking → UCI Bank Marketing

*The best story in the library, and the only one that needs two runs. Pre-run both.*

**Open it:** Overview → industry selector **Banking** → Awareness → **Term Deposit Conversion**.

1. **Upload** `library/uci-bank-marketing/data/prepared.csv` — 41,188 real campaign calls.
2. **Show run A** (engine defaults). **Model page** — ROC-AUC **0.951**. Decile 1 subscribes at
   **66 %** against 11 %: **5.9× lift**, capturing **59 %** of all subscriptions in one decile.
   Let them enjoy it.
3. **Explanations** — then stop on the top row. **`duration` is 67.7 % of the model**: the length
   of the call, in seconds. *"We do not know that until after we have made the call. This model
   cannot sort a call list."*
4. **Advanced settings → Data preparation → Exclude columns → `duration`.** *"One setting. No
   code."* Show run B.
5. **Model page again** — ROC-AUC **0.791**, decile 1 at **51 %**, **4.5× lift**, still capturing
   **45 %** of all subscriptions and **64 %** in the top two deciles. *"That is the model you
   deploy."*
6. **Explanations again** — the employment level and the month are now the top two, and four of
   the top five features are macro-economic or seasonal. *"This campaign succeeded when rates made
   deposits attractive. Your ranking will need retraining when rates move, which is what the drift
   monitor is for."*

**Close on:** the validator did not catch `duration` and could not have — it does not contain the
answer, it just does not exist yet. The engine gave you the ranking, the explanation that exposed
the problem, and the one-line fix.

---

## Insurance → Health Insurance Cross-Sell

*The scale story. 381,109 rows, and the most actionable finding in the library.*

**Open it:** Overview → industry selector **Insurance** → Awareness → **Vehicle Policy Cross-sell**.

1. **Upload** `library/health-insurance-cross-sell/data/prepared.csv` — **381,109 policyholders**.
   Let the row count land.
2. **Run training.** 77 models at the default budget.
3. **Model page** — ROC-AUC **0.858** against a baseline of 0.838. Point at PR-AUC (0.361) and say
   why: at a 12 % base rate, ranking well and being precise are different achievements.
4. **Output page** — the decile chart is the moment. Top decile **40 %** interested against 12 %:
   **3.2× lift**. Then the bottom half: deciles 6–10 are at **0.08× and below — effectively
   zero**. *"Half your book is not worth a call. The top three deciles reach 80 % of the
   opportunity at 30 % of the contact cost."*
5. **Explanations** — `Previously_Insured` is **55 %** of the model. *"People who already have the
   policy. Your best model starts as list hygiene — and it found that itself."* If someone asks
   why the chart has nine rows and not ten: `Driving_License` is 1 in 99.8 % of rows, and
   permutation importance measured nothing to report. The engine did not pad the list.

**Caveat to say out loud:** `Response` is stated interest, not a signed policy. This ranks a call
list; it does not forecast revenue.

---

## Banking (second) → UCI Credit Default

*Show this when someone asks whether the AutoML is doing anything a logistic regression would not.*

**Open it:** Overview → industry selector **Banking** → Service / Payments → **Card Default
Propensity**.

1. **Upload** `library/uci-credit-default/data/prepared.csv` — 30,000 card accounts.
2. **Run training.** 152 models at the default budget.
3. **Model page** — ROC-AUC **0.796** against a baseline of **0.728**: **+0.068**, the widest
   margin in the library. *"Fifteen times the gap we saw on the churn file. The reason is
   interaction — what a late payment means depends on the five months around it, and a linear model
   cannot say that."*
4. **Output page** — decile 1 defaults at **72 %** against 22 %: **3.25× lift**, capturing a third
   of next month's defaults and two thirds in the top three. The curve falls monotonically with no
   inversions.
5. **Explanations** — `PAY_0`, last month's repayment status, is **50 %** on its own, and the
   remaining repayment columns rank in order of recency. *"It rediscovered that the last month
   matters most. That is a model reading the data, not the noise."*

**Close on:** this is where the model search earns its licence fee.

---

## E-commerce → UCI Online Retail (win-back)

*Only show this to a technical audience, and only to make a point about honesty.*

**Open it:** Overview → industry selector **E-commerce** → Win-back → **Retail Win-back**.

1. Explain first: this is a **transaction log**, 541,909 invoice lines, not one row per customer.
   `library/online-retail/fetch.py` aggregates it — that work becomes part of the product in
   Phase 2.
2. **Upload** `library/online-retail/data/prepared.csv` — 1,463 lapsed shoppers, 41 % of whom came
   back.
3. **Validation** — one warning: `CONSTANT_COLUMN` on `snapshot_date`. *"Single snapshot. It tells
   you, and drops it."*
4. **Run training.** 106 models.
5. **Model page** — **the model ties with its own baseline and is marked as not beating it.**
   PR-AUC 0.4975 against 0.4985; `model_beats_baseline: false`. Then show the leaderboard: the
   winning ensemble beats a logistic regression by 0.047 on validation and *loses* to it by 0.014
   on test. Say it straight: *"Fourteen months of invoices, no campaign history, no contact log, no
   offer data. There is not enough here, and the system says so instead of showing you a number."*
6. **Output page** — the decile curve is flat and out of order: D2, D3 and D5 all outrank D1.
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

**Telco** (3 min) → **Bank, both runs** (7 min), from pre-run results. One shows that the product
works out of the box; the other shows that it tells you the truth about its own output. Nothing
else in the library beats that pair.

## Questions you will get

| Question | Answer |
|---|---|
| "Did you tune these?" | No. Engine defaults, no overrides, except the one `exclude_columns` line in the bank's second run. |
| "How long does it take on our data?" | At the default thirty-minute budget, 10–22 minutes on four cores; the largest file here is 381,109 rows and took 21 minutes. At `fast` / 1 minute, under a minute. The budget is a setting. |
| "What if our data is transactions, not customers?" | Today someone aggregates upstream — we show exactly how, in `library/online-retail/fetch.py`. In Phase 2 the product does it. |
| "Can it tell when data is bad?" | It catches columns that contain the answer, and it told us `snapshot_date` was constant. It did **not** catch `duration`, and we will show you why no automated check could. |
| "Will it always find a model?" | No — and the win-back dataset is in the library precisely because it did not. |
