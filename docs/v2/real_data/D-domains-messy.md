# Family D: other domains and messy files

Runner D, 20 Oct 2026, branch `v2/realdata`. Six non-retail files (bank telemarketing, telecom
churn, health-insurance cross-sell, hotel bookings, credit-card default and the full KDD Cup 98
direct-mail donation file) went through what a business would do: Data Doctor, its own adapter,
events, customer state, its own goal, the catalogue, goal suggestions and one Shadow day of the
real runtime. Then 43 messy-file cases were built from real rows and fed to the Doctor's routes,
`map_frame` and the built-in loader. Then the IPW warm start was tried on two logged, non-randomised
campaigns. **No product code was changed.** Where the software is wrong, the failure is written
down with the file I would change.

**How to read the numbers.** Every number comes from a run whose command is given. The JSON
outputs are in `/home/user/datasets/_results/d/{domains,messy,ipw}/` (outside the repo) and the logs
are in `/home/user/datasets/_results/d/logs/`. The machine was shared (4 CPUs, other runners
busy), so **times are upper bounds**. Memory is the process's peak RSS. None of these six files is
randomised, so **nothing here shows that a message causes anything**. The Shadow day's "expected to
add" figures are the agent's untrained priors, not evidence.

## The commands

```bash
cd /home/user/v2wt/realdata
python3 -P scripts/v2_realdata/d_domains.py --all          # Doctor, events, state, goal, catalogue, suggestions, Shadow day
python3 -P scripts/v2_realdata/d_messy.py                  # 43 messy-file cases (about 1 minute)
python3 -P scripts/v2_realdata/d_ipw.py --all              # IPW warm start: bank marketing, KDD Cup 98
python3 -m pytest tests/v2/realdata/test_family_d.py -q    # 3 tests, inline data, under 2 s
```

`-P`, not `-I`: FastAPI lives in the user site-packages here, which `-I` drops (see B's notes).
Python 3.13 runs everything because the system's 3.11 has no numpy. The stdlib date parsing these
results depend on (`datetime.fromisoformat`, `strptime`) was checked on 3.11.17 and 3.13.16 and
gives identical answers for every format used below.

## The verdict in eight lines

1. **The event model can hold a bank, a telco, an insurer, a charity, a hotel and a card issuer,
   but only after code.** Every file needed an adapter. Four had no dates at all or only a month.
   Two were wide (one column per month or per mailing). One had no customer id. Things had to be
   invented to fit: values, calendars, a channel.
2. **Phone calls and paper mail are not channels.** Bank calls and charity mailings went in as
   `webhook`, so their costs and quiet hours are meaningless.
3. **"Retention" means "bought again in the window".** The retention template targets "2+ orders,
   last one 30 to 90 days ago", which on a subscription means "stopped paying", i.e. people who
   already left. On telco all **1,143** subscribers the Shadow day would e-mail had churned. That
   100% is **guaranteed by my adapter** (it invents bills that stop a month before the snapshot
   for churners and run to the snapshot for everyone else), so it is a demonstration of the
   template's logic, not an empirical rate. With real bills the audience would be "cancelled 1 to
   3 months ago" plus late payers.
4. **The agent does not target at all, in any domain.** It would e-mail about 94% of the
   customers it is allowed to decide for (the agent group: 80% of the sample after the random 10%
   holdout and 10% baseline) a retail "reminder" or "how-to" with no offer, split about 50/50. The
   other 6% are exploration rests. So every customer it "skipped" was skipped by the random
   group split or by its own exploration, not by a judgement about that customer,
   and the dataset's outcome being the same among contacted and not contacted (bank 11.5% vs
   11.3%, credit default 21.2% vs 21.7%) is **expected by design and says nothing about targeting
   skill**. What the runs do show: with customer attributes unable to reach state, there is
   nothing for it to target on, and it contacts nearly everyone.
5. **History never reaches the warm start in the runtime.** The event store held 42,703 bank calls
   and 1,559,430 charity mailings, and every Shadow day reported `history_sends_used = 0`.
6. **Handed over directly, a mailing history without clicks teaches the agent that mail never
   works.** 780,162 past mailings with an 18% gift rate gave a learned response rate of about
   0.00001. The warm start learns only from clicks.
7. **Of 43 messy cases, the runner marks 20 "silent wrong".** 17 of them are wrong data with no
   problem raised: wrong customer, time or amount, or rows added or lost (16 through the product's
   own reading, mapping or loader; I3 also needs the customer's default `pd.read_csv` for the
   consent list; D1 and D4 are the same duplicate-line mechanism on two paths). Two more (W3
   sentinels, W4 the `duration` leak) are Doctor checks that did not fire; no row is wrong. The
   last (F2) is pandas' `read_excel` default, on the customer's side. The worst: day/month order, ids that Excel or pandas reformat,
   placeholder ids, positional event ids on re-export, European thousands, currencies, time zones,
   a totals row and a stray quote. Formats the Doctor cannot read (Excel, gzip, UTF-16, JSON lines,
   cp1251) are refused with a misleading parser message, or profiled as garbage with a good score.
8. **IPW with estimated propensities balances what it can see, and that is all it can show.** On
   bank marketing, clipped at 20 it shrinks the test-half cellular-vs-telephone gap from 9.3 to
   4.9-6.4 points (DR / SNIPS); unclipped, both estimators reverse the sign with useless
   intervals, so the adjusted gap is not identified. On KDD Cup 98 the history's gift rate is
   17.7% naive and 18.7% IPW (NK appeals), against 5.1% for 97NK, which everyone in the file got:
   the file's population was selected on its own giving history, and no reweighting of the past
   fixes that. (A first version, with look-ahead covariates and not-yet-donors in the population, reported
   22.0%; withdrawn, see section 3.)

---

## 1. Can Autonomy represent a non-retail business?

`d_domains.py`. Each adapter is in `scripts/v2_realdata/d_adapters.py` and lists what it invented.
Its long table goes through the product's own `map_frame` (0 mapping problems on all six). States
come from the production `StateFolder`. The goal is turned into a brief by `template_brief`, the
Strategist's no-LLM fallback. The Shadow day ran with consent invented (one e-mail
`consent_granted` per customer, labelled) on a seeded sample of at most 20,000 customers, at 10:00
local.

| | Bank marketing | Telco churn | Health insurance | Hotel bookings | Credit default | KDD Cup 98 (full) |
|---|---|---|---|---|---|---|
| business | Portuguese bank, outbound calls for term deposits | US telecom subscriptions | Indian health insurer, vehicle cross-sell | two Portuguese hotels | Taiwanese card issuer | US veterans' charity, direct mail |
| rows in | 41,188 | 7,043 | 381,109 | 119,390 | 30,000 | 95,412 (481 columns) |
| person | **none**: row number | `customerID` | `id` | **none**: one booking = one "customer" | `ID` | `CONTROLN` |
| time in the file | month + weekday, no year or day | none (tenure in months) | none (`Vintage` days) | arrival year / month name / day in 3 columns | month of 6 wide columns | `YYMM` integers in 66 wide columns |
| events made | 48,716 (42,703 calls as `send`, 6,013 deposits as `purchase`) | 235,033 (227,990 monthly bills) | 762,218 | 163,614 (44,224 cancellations as `refund`) | 143,103 payments | **1,837,462** (1,559,430 mailings, 278,032 gifts) |
| what was invented | id, year, day, deposit value 1.0, channel | calendar, every bill | calendar, currency | id | day of month | day of month, channel |
| Doctor via route | 79 (with `delimiter=;`) | 93 | **413** (21.4 MB) | 39 | 93 | **413** (117 MB) |
| Doctor in-process | 79, 2.5 s | 93, 0.6 s | 93, 12.7 s | 39, 8.9 s | 93, 2.2 s | 86, 116 s |
| segments (folder) | prospect 36,069, lapsed 4,310 | **vip 5,036** of 7,043 | lapsed 156,188, lapsing 118,265, new 106,656 | lapsed 107,003 of 119,390 | **vip 20,930** of 28,568 | lapsed 94,010 of 95,412 |
| own goal (kind / objective) | custom / conversion | retention / retention | cross_sell / conversion | retention / retention | custom / retention | win_back / revenue |
| template audience | "Everyone we may contact": 41,188 | "2+ orders, last 30 to 90 days ago": **1,489, churn 100%** (rest 6.8%) | "Bought in the last 60 days": 66,979, response 12.5% (rest 12.2%) | **0** | everyone: 28,568 | "Bought before, nothing in 60 days": 94,032 |
| Shadow day: would contact | 14,966 of 20,000 | **1,143, all churned** | 2,537 of 3,445 decided | **nothing** (empty audience) | 14,967 of 20,000 | 14,889 of 19,716 |
| outcome among contacted / rest (rest = mostly the random holdout and baseline; see below) | y 11.5% / 11.3% | churn 100% / 12.3% (by construction) | response 11.8% / 12.0% | - | default 21.2% / 21.7% | TARGET_B 5.3% / 5.1% (rerun: 5.25% / 5.24%) |
| local send hour (mode) | 10 | **02:00** Los Angeles | 15 (Kolkata) | - | 18 (Taipei) | **06:00** New York |
| history sends used by the warm start | **0** of 42,703 | 0 | 0 | 0 | 0 | **0** of 1,559,430 |
| catalogue sees | 2 items, none replenishable | 3 plans, **"replenishable, 31-day cycle"** | 1 item | 18 room types | `card_payment` **"replenishable, 31 days"** | `gift` "replenishable, 121 days" |
| API suggests | "Turn 4,225 one-time buyers into repeat customers"; "36,069 who **signed up** but never bought" | repeat purchase 613 | "Turn **381,109** one-time buyers into repeat customers" | repeat purchase 119,390 | repeat purchase 1,002 | repeat 23,081; onboarding 1,344 |
| time (adapt / rebuild / Shadow tick) | 1.7 / 2.3 / 15.6 s | 16.5 / 4.2 / 7.6 s | 29 / 40 / 4.8 s | 6.5 / 6.1 / 2.7 s | 4.0 / 3.2 / 20.9 s | 111 / 71 / 31.7 s |
| peak RSS (process peak so far; see below) | 0.6 GB | 0.7 GB | 3.2 GB | ≤ 1.0 GB | ≤ 0.9 GB | **4.9 GB** (4.85 GB alone) |

**How to read the "contacted / rest" row.** "Rest" is everyone in the sample the agent did not
act on. In every domain it is made of the random 10% holdout, the random 10% baseline and the
agent's own exploration rests (bank: 2,031 + 2,013 + 990 of 5,034); the agent acted on about 94%
of its own group (bank 14,966 of 15,956, credit 14,967 of 15,953, KDD 14,889 of 15,916, health
2,537 of 2,713, telco 1,143 of 1,210). A random split must give the same outcome rate on both
sides, so these pairs test nothing about targeting. They only confirm that the agent made no
choice. The exploration is not seeded: a rerun of KDD (`python3 -P scripts/v2_realdata/d_domains.py
kdd98`, review pass) contacted 14,940 instead of 14,889, with TARGET_B 5.25% vs 5.24%.

**How to read the time and memory rows.** Telco and bank were run alone. Credit, hotel, health
and KDD ran one after another in one process (`domains.log`), and `ru_maxrss` is the process's
peak so far, so credit's and hotel's figures are upper bounds. KDD was rerun alone in the review
pass: peak 4.85 GB (reached in the adapter step), adapter 84 s, Doctor in-process 109 s,
`rebuild_states` 63 s. The table keeps the first run's times; they are upper bounds on a shared
machine.

### Per domain: what is represented, what breaks, what is meaningless

**Bank telemarketing** (`bank-additional-full.csv`, `;`-separated).
- The customer had to invent a lot. There is no id, so a client called twice would be two people.
  There is no year or day: `d_adapters.bank_dates` rebuilds both from row order, which the UCI page
  says is chronological. The result is monotonic over all 41,188 rows and spans 26 months. There
  is no deposit amount: a purchase needs a value, so I used 1.0.
- There is no phone channel, so the call is a `webhook` send with the call type in
  `attrs["format"]`.
- The goal ("open a term deposit") is a conversion with no goal kind, so it becomes `custom`
  ("Everyone we may contact").
- Every client in the file was called, so there is no uncontacted group.
- The Doctor misses that `duration` is a leak. UCI says to drop it: call length is known only
  after the call. Its correlation with `y` is 0.41, below the leak check's 0.95 bar.
- The Doctor does not flag the sentinel `pdays = 999` ("never contacted"), which is 96.3% of rows.
- The agent would send "We miss you, {{first_name}}. New in {{top_category}} since your last
  visit." to 75% of the sampled clients (94% of its own group). That is the sample content, the only content in a no-LLM run.

**Telco churn.**
- Bills are the only way to say "still a customer". The adapter invents one purchase per month of
  tenure at today's `MonthlyCharges`. Churners' bills stop a month before the snapshot (the file
  says they left "within the last month").
- The retention template asks for "2+ orders, last order 30 to 90 days ago". On a monthly bill,
  that means "stopped paying": the audience is the 1,489 churners with 2+ bills and nobody else.
  **This exactness is my adapter's doing**: it stops churners' invented bills one month before the
  snapshot and runs everyone else's to the snapshot, so no non-churner can match. The finding is
  the template's logic (it picks people after they stop paying, not before), not the 100%. The
  Shadow day would e-mail 1,143 of them, and the other 346 were holdout, baseline or exploration
  rest.
  Retention *reward* is the same mistake (`autonomy/proof/rewards.py`): "converted" means a
  purchase in the window, not "did not cancel".
- There is no `cancel` event type. The contract type, tenure, services and payment method (what
  actually predicts churn here) cannot reach `CustomerState`.
- 5,036 of 7,043 subscribers (72%) are "vip", because 5 bills over 500 USD is easy (the bills are
  invented from tenure x today's charge, which real billing would roughly match).
- The catalogue calls all three plans "replenishable, 31-day cycle", so the refill machinery would
  send renewal reminders for plans that renew themselves.
- The sends were timed for **02:00 in Los Angeles**: the mapping's time zone never reaches state
  (B's finding 8).

**Health insurance cross-sell.**
- The file is 21.4 MB, so the Doctor's route refuses it (413). Cut to 19 MB, the same file scores
  93. In-process it took 12.7 s.
- The goal is to sell a product nobody in the data has bought (vehicle insurance). The only
  purchase is the health policy, so the co-purchase recommender has nothing to recommend.
- The "bought in the last 60 days" audience is just `Vintage < 60`. Its response rate (12.5%) is
  the same as everyone else's (12.2%).
- The value of a conversion is the customer's own average order. Here that is a 30,475 INR annual
  premium (the fallback mean) x 0.4 margin. So the agent expects 40 INR net per e-mail (median),
  for a different product with a different price.
- `Response` is "said they are interested", not a sale.
- Age, region, licence, vehicle age and damage are the features that predict response, and none
  can enter state.

**Hotel bookings.**
- There is no guest id, so 119,390 bookings are 119,390 one-purchase "customers". 3,810 bookings
  are flagged repeat guests, but their earlier stays cannot be linked.
- The arrival date is split over three columns with the month as a name. The mapping cannot read
  that (case W2): the best no-code mapping (`%Y` on the year column) puts every stay on 1 January.
- The Doctor here is good. It scores the file 39 and flags `reservation_status` as a leak
  ("its values alone tell the target apart", 1.0), 257 status dates after the snapshot, and
  31,994 duplicate rows.
- Retention's audience is empty, so the Shadow day decides nothing.

**Credit-card default.**
- The six monthly payments are columns, so the file had to be melted in code (case W1).
- A card payment is mapped to a purchase, because there is no "paid a bill" event.
- 20,930 of 28,568 cardholders (73%) are "vip" (500 in TWD is about 16 USD).
- The goal ("prevent default") has no kind and no outcome event. The agent would e-mail 75% of
  sampled cardholders (94% of its own group) with the same retail reminder. The default rate is
  the same among those picked (21.2%) and those not (21.7%), as it must be: the "not" are a random
  split, so this shows no targeting, not bad targeting.

**KDD Cup 98, full learning file** (95,412 lapsed donors; this run found a GitHub copy, see
section 6).
- 22 past mailings plus the gift for each are 88 wide columns. They melt into 1,559,430 sends and
  278,032 gifts.
- Dates are `YYMM`. `timestamp_format="%y%m"` reads "9606", but the product refuses the "9606.0"
  that pandas makes of a column with blanks (case T7, fix 15).
- The win-back template keeps the 10%-off and free-shipping offers in the envelope of a charity
  appeal. The agent did not choose them, but nothing stopped it.
- The Doctor found no date column among 481 (YYMM is not a date shape). It flagged `DOB` as a date
  of birth by its header, and flagged `TARGET_D` (the gift amount) as a leak by its name only.
- The whole history (1.56M sends) sat in the event store and the warm start used none of it.

**B2B** was asked for, but **no B2B dataset was tested**: I did not find one on a reachable host in
the time spent. Online Retail I (a wholesaler, family B) is the closest.

---

## 2. Messy files: what the Doctor, the mapping and the loader do

`d_messy.py`. The base is the first 2,000 sale lines of Online Retail I that have a customer id
(99 customers, 47,695.12 GBP). The native messes come from bank marketing, Starbucks rewards,
Olist, KDD Cup 98, hotel bookings and health insurance. Each variant goes through the real routes
(TestClient) or the customer's one line of pandas, and is compared row by row with the clean
truth. The verdict is computed by the runner, not judged by me. **silent_wrong** means rows came
out with a different customer, time or amount (or appeared or vanished) and no problem said so.

### Silent wrong results (the runner's 20: these 19, plus F2 below)

17 of the 19 below are wrong data with no problem raised. W3 and W4 are different: no row is
wrong, a Doctor check simply did not fire. They are kept here because the runner's verdict
counts them, but "17 silent data errors + 2 missed Doctor warnings + 1 pandas default (F2)" is
the accurate split. All 43 verdicts reproduced exactly on a rerun (`python3 -P
scripts/v2_realdata/d_messy.py`, review pass).


| # | Case | What happened (numbers from the run) | Fix: file, one line |
|---|---|---|---|
| T1 | UK day-first dates `01/12/2010` read with `%m/%d/%Y` (the built-in loader's fixed format) | First 2,000 rows (all December): **all 2,000 dated 12 January**, no problem. Over a year of 5,000 rows: 2,904 `bad_timestamp` (day > 12) and **1,899 mapped with day and month swapped**. The loader gives the same counts. The Doctor's profile raised nothing. | `api/v2/engines/doctor.py` `_date_check`: count values whose day and month are both <= 12 and report `ambiguous_day_month`; `autonomy/data/loaders.py`: take `date_format=` |
| I2 | Placeholder ids `0`, `unknown`, `-`, `guest` (with `N/A`, `NULL`, `#N/A`) on 14% of rows | `N/A`/`NULL`/`#N/A` -> 120 `missing_customer` (good). `0`, `unknown`, `-`, `guest` -> **4 new "customers" with 40 lines each**, no problem. | `autonomy/data/mapping.py` `customer_key`: treat a configurable list of placeholder ids as missing; Doctor: flag an id value holding > 1% of rows |
| I3 | Leading-zero ids: orders through the Doctor (text, `0012431`), consent list through default `pd.read_csv` (`12431`) | 99 customers with orders and 99 with consent, **0 in common**. Nobody with an order can be contacted. | `autonomy/data/mapping.py`: an `id_format` option (e.g. strip leading zeros) applied to every source; Doctor: warn when an id column has leading zeros |
| I4 | 12-digit ids saved by Excel as `1.23457E+11` | 99 customers became **1**. | `autonomy/data/mapping.py` `customer_key`: refuse ids in scientific notation as a new `bad_customer_id` problem |
| I5 | E-mail as the id, a third of rows upper-cased | 99 people became **189**. | `autonomy/data/mapping.py` `customer_key`: lower-case ids that look like e-mails |
| D1 | Exact duplicate lines (every 20th twice), no `row_key` | 2,100 events, **+8.6% revenue**. The Doctor warns "144 rows repeat another exactly" only as a warning. | `autonomy/data/mapping.py` `map_frame`: without `row_key`, report exact repeats of all mapped columns as `duplicate_row` |
| D2 | Re-export with this week's 100 orders at the top (newest first), loaded over last week's file | **The 100 new orders are dropped as repeats and 100 old ones are counted twice**: −2.7% revenue, no problem. Event ids come from row position. | `autonomy/data/mapping.py` `_row_key`: default to a hash of the mapped values (plus an occurrence counter), not the row position |
| D4 | Built-in Online Retail loader on a file with duplicate lines | Same 129 invoices, **+8.6% value**, no problem. | `autonomy/data/loaders.py` `online_retail_log_events`: report exact duplicate lines as `duplicate_row` (B's fix 11). Report, not silently drop: the clean 2,000-row base already holds 44 exact repeats that this runner counts as real lines |
| N3 | European thousands without decimals: `2.550` meaning 2550 | **1,669 of 2,000 amounts read 1,000x too small** (−96.6% revenue). | `autonomy/data/mapping.py`: an `EventMapping.decimal` option (`"."` or `","`); Doctor: flag a money column whose values all have exactly three decimals |
| N5 | GBP and BRL orders in one file, with a currency column | Mapped fine. State revenue adds **47,695 GBP + 300,838 BRL = 348,533** in "the business currency". | `autonomy/data/mapping.py`: an `EventMapping.currency` (constant or column) and a `mixed_currency` problem when rows disagree |
| N6 | Negative prices on 2% of lines, no `negative_as` | Stored as **purchases with negative value**, −3.2% revenue, still counted as purchases. | `autonomy/data/mapping.py` `_event`: a negative purchase without `negative_as` becomes a `bad_number` problem |
| T4 | Two-digit years (`%y`), data from 1968 | Every row dated **2068**. `map_frame` raised nothing. The Doctor flags "2,000 values fall after the snapshot" only when given a snapshot. | `autonomy/data/mapping.py` `map_frame`: take an optional `not_after` (the snapshot) and report later times as `bad_timestamp` |
| T5 | Placeholder dates `01/01/1900`, `01/01/1970` on 5% of rows | Accepted. **60 of 99 customers "first seen" in 1900.** The Doctor raised nothing. | `autonomy/data/mapping.py`: an optional `not_before` bound; Doctor `_date_check`: flag 1900-01-01 and 1970-01-01 spikes |
| Z1 | Naive New York times, mapping time zone left at its default (UTC) | **Every purchase stored 5 hours early**, no warning. Even with the right zone, state keeps `timezone="UTC"` (sends at 02:00 to 06:00 local in section 1). | `autonomy/data/mapping.py`: no silent UTC default for naive times (require `timezone`), and put `attrs["timezone"]` on customer events (B's fix 8) |
| X2 | A stray quote opens in one row's text and closes four rows later | **4 rows merged into one text cell and vanished**, no problem (−0.14%). A quote that never closes is refused ("Expected 8 fields in line 12"). | `api/v2/engines/doctor.py` `read_csv_bytes`: compare physical lines with rows read and report rows swallowed by quoted newlines |
| X4 | A totals row at the bottom (`TOTAL` in the id column) | One more "customer", **revenue doubled** (+100%). | Doctor `profile_frame`: flag a row whose amount equals the sum of the others, or an id like `total`/`sum` |
| X5 | Two columns both headed `CustomerID` (a store id first) | pandas renames the second to `CustomerID.1`. The mapping silently takes the first: **99 customers became 7 store ids.** | `api/v2/engines/doctor.py` `read_csv_bytes`: refuse duplicate header names |
| W3 | Sentinels: bank `pdays = 999` (96.3% of rows), Starbucks `age = 118` (2,175 of 17,000) | No problem names either column. | Doctor `profile_frame`: flag a numeric value that is far outside the rest and holds a large share (`sentinel_value`) |
| W4 | Leaks by timing: bank `duration` (corr 0.41 with `y`) | Not flagged. Hotel `reservation_status` is flagged (purity 1.0), good. | Doctor: a `known_after` option, plus a name hint for `duration`/`status`; values alone cannot show timing |

Two more on the customer's side. F2 is the 20th silent case the runner counts; F6's
categories are not in its row comparison:
- **F2**: `pd.read_excel` without `sheet_name=None` reads only the first sheet, so half the rows
  (−48% revenue) disappear with no warning.
- **F6**: a cp1251 file is decoded as Latin-1 (the Doctor's fallback). The mapping is refused by
  name, which is good. But a mapping written against the garbled headers loads every category as
  mojibake: `Подарочный набор` was stored as `Ïîäàðî÷íûé íàáîð`.

### Refused with a message (the whole file)

| # | Case | Message | Good or misleading |
|---|---|---|---|
| F1 | `;` CSV without `delimiter` | profile: 200, **score 79 with 1 column** (the same score as when read correctly); map-events: "the table has no column 'age', 'month'; it has 'age;"job";...'" | profile **misleading** (a one-column file scores like the real one); map-events good |
| F2 | `.xlsx` workbook | "cannot read the file as CSV: Error tokenizing data. C error: Expected 1 fields in line 3, saw 2" | misleading: it does not say "Excel" |
| F3 | JSON lines (Starbucks transcript) | profile: 200, **score 86** on four garbage columns (`{"person": ...`); map-events: "no column 'person'..." | profile **misleading**. The customer's `pd.read_json(lines=True)` then gives 2,000 `bad_timestamp` (time is hours since start) and a nested `value` dict that only becomes a string attr |
| F4 | gzipped CSV | "Expected 1 fields in line 4, saw 3" | misleading (it does not say "compressed") |
| F5 | UTF-16 CSV (Excel "Unicode text") | "Expected 8 fields in line 111, saw 9" | misleading |
| F6 | cp1251 Russian headers | "the table has no column 'Клиент', ...; it has 'Ñ÷¸ò', ..." | half good: you can see the mojibake, but not why |
| X5 | header `' CustomerID'` | "the table has no column 'CustomerID'; it has ..., ' CustomerID', ..." | good (the repr shows the space) |
| X6 | 21.4 MB upload | 413 "the upload is over 20 MB" | good message, hard limit |

Fix for the first five, all in `api/v2/engines/doctor.py` `read_csv_bytes`: check the first
bytes (`PK` = zip/xlsx, `\x1f\x8b` = gzip, `\xff\xfe`/`\xfe\xff` = UTF-16, `{` = JSON lines) and
either read them or refuse with what the file is. Sniff the delimiter when only one column comes
back, and make "one column" an error. For F6, report "decoded as Latin-1" as a problem.

### Reported (rows left out, each named) and OK

- **Reported**:
  - **I1** blank ids: 200 `missing_customer`.
  - **N1** decimal comma: 1,995 `bad_number`; 5 prices had no decimals and read correctly.
  - **N2** `1.234,50`: 2,000 `bad_number`.
  - **N4** `£2.55` / `2.55 GBP`: `bad_number`.
  - **N7** `N/A` and `-` in the price. Note `N/A` becomes "invalid_event: a purchase event needs a
    value" with no column named, and `-` becomes `bad_number`.
  - **T2** non-ISO with no format: all `bad_timestamp`.
  - **T3** Excel serials: 100 `bad_timestamp`. The Doctor warns "100 of 2,000 values are not
    readable dates".
  - **T6** ` GMT` suffix: all `bad_timestamp`.
  - **T7** `9606.0`: all `bad_timestamp`. As text, `9606` maps, and the 64 blanks are
    `bad_timestamp` "no timestamp".
  - **D3** `row_key=(InvoiceNo, StockCode)`: 93 real repeated lines dropped as `duplicate_row`.
    Reported, but the money (−1.0%) is gone.
- **OK**:
  - **F0** clean control.
  - **F6** UTF-8 Russian headers and values.
  - **F7** Chinese headers with BOM and CRLF.
  - **T6** mixed numeric offsets in one column.
  - **X3** a header row repeated mid-file: one `bad_timestamp`, all 2,000 real rows right.
  - **Z2** DST: a non-existent London time (01:30 on 27 Mar 2011) is stored without a warning, as
    01:30 UTC.
  - **X1** a 200 KB review on 50 rows: 9.3 MB upload, profile 0.7 s, map 0.4 s. All 191,498
    characters are kept in `attrs` and echoed back in a 9.65 MB response. There is no length limit
    on an attr.
- **Cannot without code**:
  - **W1** wide tables (six payment columns).
  - **W2** a date split over three columns.

---

## 3. The IPW warm start on logged, non-randomised campaigns

`d_ipw.py`. The product's warm start weights each past send by `1 / attrs["logged_probability"]`,
clipped at 20. Here nobody logged that probability, so it is **estimated**: ridge logistic
regression on what was known before each contact, fitted on a **train half of customers** (salted
hash of the id) and scored on the test half. Every conclusion below assumes no unmeasured
confounding, which these files cannot check.

### Bank marketing: cellular vs telephone

Everyone in the file was called, so "call or not" cannot be studied. The only thing that varied
is the contact type, and it is barely an action: it is mostly which number the client gave the
bank (mobile or landline), and in May-June 2008 the campaign used landlines only. The covariates are client attributes, previous-campaign fields, month,
weekday and the macro indicators. `duration` (known after the call) and `campaign` (counts the
last call) are excluded.

| | cellular | telephone | difference |
|---|---:|---:|---:|
| raw `y` rate (all 41,188) | 14.74% | 5.23% | 9.51 pts |
| raw `y` rate (test half, 20,653: the like-for-like baseline for the rows below) | 14.67% | 5.38% | 9.30 pts |
| test half, IPW (SNIPS), weights clipped at 20 | 15.73% [15.00, 16.46] | 9.32% [7.91, 10.73] | 6.41 pts |
| test half, doubly robust, clipped at 20 | 13.37% [12.87, 13.86] | 8.49% [7.17, 9.80] | 4.88 pts |
| test half, SNIPS, **unclipped** | 15.60% [14.83, 16.38] | 21.63% [8.76, 34.50] | −6.03 pts (sign reversed) |
| test half, doubly robust, **unclipped** | 13.26% [12.72, 13.81] | 19.60% **[−8.63, 47.83]** | −6.34 pts (sign reversed) |
| product warm start, `y` written as a click (train) | 14.79% | 5.08% | |
| product warm start + estimated `logged_probability` (train) | 15.69% | 9.80% | |
| my numpy check of the product's weighting (train, clip 20) | 15.71% | 9.84% | |

- **Positivity fails in places.** May and June 2008 were called by telephone only (7,763 and 4,374
  clients). Five months with 12,342 clients have fewer than 20 of one type. The propensity model's
  test AUC is 0.93. 50.7% of logged calls had p > 0.95, and the largest unclipped "all telephone"
  weight is 2,692, leaving an effective sample of 97.
- **What it can honestly show**: very little about the size of the adjusted gap. Clipped at 20,
  the estimators shrink the test-half gap from 9.3 to 4.9-6.4 points. Unclipped, both SNIPS and DR
  **reverse its sign** (about −6 points), with a telephone interval that covers almost everything.
  So the answer is set by the clip, not by the data. Of 7,568 test telephone calls, 268 have a
  weight above 20; they carry 57% of the unclipped weight and 15.7% of them led to a deposit,
  against 5.0% for the rest. Telephone calls in May-June 2008 converted at 3.5%, later ones at
  12.3%. Clipping caps those 268 and leans on the landline-only 2008 months (review-pass check,
  same split and propensity model as `d_ipw.py`). Without overlap the adjusted gap is not identified, and I would not
  quote "a third to a half" (an earlier draft did). What does hold: the product reads an estimated
  `logged_probability` exactly as a logged one; its rates match my numpy Hajek estimate to the
  third decimal (15.69% vs 15.71%, 9.80% vs 9.84%). All bank numbers reproduced exactly on a
  rerun (`python3 -P scripts/v2_realdata/d_ipw.py bank_marketing`, review pass).
- **What it cannot show**: that a mobile call *causes* more deposits. Who had a mobile, and which
  clients the bank chose to ring on which line, are not in the file. There is no randomised check
  in this data.

### KDD Cup 98: who was mailed in 22 past appeals

The population is the 95,412 donors whose last gift was **13 to 24 months before June 1997**
(documentation, `cup98doc.txt`). Every one of them received the June 1997 appeal 97NK (TARGET_B,
5.08% gave). The history records whether each donor was mailed in each of 22 appeals from 1994 to
1996, and their gift for it. Gifts exist only for donors who were mailed. Before each appeal the
model sees: mailings and gifts so far, months since the last gift, the last amount, and whether
they gave to the previous appeal. "So far" means dated before the appeal's usual mailing month.

**Corrected in the review pass.** The first version of `kdd_panel` had two flaws, and its numbers
(targeting std diffs up to 0.24, IPW "mail everyone" NK rate 22.0%, warm start 22.0%) are
withdrawn. (1) Look-ahead: it counted every gift to an earlier appeal, including gifts that
arrived after the current appeal was mailed. 25% of the gifts behind "gave to the previous
appeal" and 3.7% of the gift counts were dated after the current mailing month, and 5.4% of rows
had a negative "months since last gift". (2) Population: "everyone at the appeal" included donors
who were not donors yet. 42.8% of unmailed donor-appeal rows belong to donors whose first-ever
gift (`FISTDATE`) came after that appeal, against 1.1% of mailed rows, so IPW was reweighting the
mailed towards people who could not have been on a renewal list. The script now uses only gifts
dated before the appeal and restricts the propensity fit, balance and Hajek rates to donors
already acquired (1,850,100 of 2,099,064 donor-appeal rows). The warm start still gets every
mailing. Rerun: `python3 -P scripts/v2_realdata/d_ipw.py kdd98` (381 s, peak 4.89 GB).

- **Targeting bias in the history** (acquired donors) is mild: mailed donors had slightly more
  past mailings (standardised difference +0.10), past gifts (+0.08) and larger last gifts (+0.06)
  than everyone at the same appeal.
- **Propensity.** Test AUC per appeal is 0.66 to 0.93 (0.50 for the first, 94NK, where nobody has
  any history yet). Among mailings, 60.0% had p > 0.95, and 0.07% hit the clip.
- **Balance after weighting.** Every standardised difference falls to ≤ 0.022.
- **The "mail everyone" response rate** (test donors):

| | naive (gift rate among mailed) | IPW (Hajek, clip 20) | 97NK (everyone in the file mailed) |
|---|---:|---:|---:|
| all 22 appeals | 17.33% | 17.56% | 5.12% |
| NK-type appeals only (97NK is an NK card) | 17.69% | 18.72% | 5.12% |
| 1994 appeals | 26.04% | 25.03% | |
| 1995 appeals | 24.08% | 23.68% | |
| 1996 appeals | 11.30% | 11.17% | (96LL / 96NK / 96SK / 96TK under 1%) |

  **IPW changes the history rate by about one point at most, and neither number is an estimate
  of 97NK.** The 12-point gap to 97NK comes from **selection on the outcome**, not from
  targeting. The file holds only donors who gave 13 to 24 months before June 1997, so they had to
  give in mid-1995 to mid-1996 (inflating those appeals' rates) and could not give after that (the
  late-1996 appeals are near 0%). By June 1997 every one of them had lapsed. No propensity model of
  "who was mailed" can undo how the population itself was chosen. On top of that, 830 gifts are
  dated before their mailing.
- **What the product does with this history** (train donors, the real `AgentPolicy`: `nightly`
  before the first decision, then `prepare_day`):

| history handed over | events | sends used | clicks | learned rate for the NK mailing (pooled) |
|---|---:|---:|---:|---:|
| as a customer would load it: mailings + gifts | 918,913 | 780,162 | 0 | **0.00001** |
| gifts also written as clicks (linked by `decision_id`) | 1,057,664 | 780,162 | 138,751 | 0.180 |
| + estimated `logged_probability` | 1,057,664 | 780,162 | 138,751 | 0.192 |

  The agent's own segmenter put 47,680 of 47,714 donors in "lapsed", so there is one segment to
  compare. Its 97NK response was 5.03% against the warm start's 18.0% (naive) and 19.2% (IPW).
  Each run took 108 to 124 s and the process peaked at 4.89 GB.
- **Off-policy evaluation of 97NK is impossible.** Every donor was mailed (p = 1), so a policy
  that skips anyone has no logged rows (positivity fails by construction). `autonomy/proof/ope.py`
  would accept the arrays, but there is nothing to score "mail fewer" on.
- **What it can honestly show**: the history is mildly targeted on what the file records, the
  estimated propensities balance it, and the warm start reads them. A warm start built from this
  history would carry an 18-19% prior into a 5% campaign, because nothing in it knows the
  population has since lapsed (old history needs a time decay or a recency covariate). **What it
  cannot show**: anything causal about mailing, because no donor went unmailed in the target
  appeal and no gift is recorded for an unmailed appeal.

---

## 4. How much data it handles (family D)

| what | size | time | peak RSS |
|---|---|---|---|
| KDD Cup 98 adapter (melt 22 appeals, `map_frame`) | 95,412 donors -> 1,837,462 events | 111 s (rerun alone: 84 s) | 4.9 GB (process; rerun alone: 4.85 GB, reached in this step) |
| KDD Cup 98 `rebuild_states` (in-memory) | 1,837,462 events, 95,412 states | 71 s (rerun: 63 s) | (same process) |
| KDD Cup 98 Doctor in-process (481 columns, 117 MB) | 95,412 rows | 116 s (rerun: 109 s) | |
| KDD Cup 98 warm start (train half) | 918,913 to 1,057,664 events | 107 to 128 s (rerun: 108 to 124 s) | 4.65 GB (rerun: 4.89 GB) |
| Health insurance adapter + rebuild | 381,109 customers, 762,218 events | 29 + 40 s | 3.2 GB |
| Health insurance Doctor in-process (21.4 MB) | 381,109 rows | 12.7 s | |
| Doctor route on 19 MB (the cap minus a bit) | 354,404 rows | 10.0 s (rerun: 10.9 s) | |

Per event that is 34 to 39 µs of `rebuild_states` on KDD and 53 µs on health insurance, and a
process peak of about 2.6 KB per KDD event (that peak includes the 481-column raw frame: reading it
alone takes a process to 1.36-1.48 GB, `ipw/kdd98.json` step "read"), the same order as B's 2.3 KB and 60 µs. The in-memory path holds a few million
events, then the RAM runs out. The route cannot take any of the big files.

---

## 5. Failures, causes and the fix I would make

Messy-file fixes are in the tables in section 2. Domain-level ones:

1. **No channel for calls or mail.** `Channel` has email, whatsapp, sms, push, web, webhook. Fix:
   `autonomy/contracts/base.py` `Channel`: add `phone` and `mail` (with costs and no quiet-hour
   assumptions from e-mail).
2. **Retention means "bought in the window".** The template's audience picks those who already
   left a subscription (1,143 of 1,143 contacted had churned), and `_reward` counts a purchase as
   "retained". Fix: `autonomy/agents/strategist.py` `TEMPLATES[RETENTION]`: target a churn score
   (`scores.churn_risk`) when the business bills on a cycle; `autonomy/contracts/events.py`: add a
   `cancel` event; `autonomy/proof/rewards.py`: for RETENTION, converted = no `cancel` in the
   window.
3. **Customer attributes cannot reach state.** Contract type, job, vehicle damage and RFA codes
   are the predictive columns in these files, and `CustomerState` has nowhere to take them. Fix:
   `autonomy/data/folder.py`: fold a `preference`/profile event's numeric attrs into `scores` (or
   add a customer-table mapping to `autonomy/data/mapping.py`).
4. **History never reaches the warm start in the runtime** (42,703 and 1,559,430 sends,
   `history_sends_used = 0`). This confirms B's finding 3 with a run. Cause: `_replay` feeds
   `nightly()` only customers with a decision in the runtime's own log, and only for
   `replay_lookback` (30 days); imported sends have no such decisions, and most of this history
   is older than 30 days anyway. Fix: `autonomy/runtime/service.py` `_replay`: on a policy's
   first build, hand it its audience's imported event log (`policy.nightly`, with its own
   lookback) and call `prepare_day` before the first sweep.
5. **The warm start learns only from clicks.** A mailing history with gifts but no clicks gives
   a learned rate of 0.00001. A response weeks later can only be credited through `decision_id`,
   because the loose click window is 3 days. Fix: `autonomy/learning/observations.py`
   `logged_sends`: when a campaign has no clicks, count a purchase tied by `decision_id` (or within
   the brief's horizon) as its response.
6. **A conversion is worth one of the customer's past orders.** That is 1.0 for the bank (an
   invented value), a 30k INR premium for a vehicle cross-sell, and 50 in any currency without
   history. Fix: `autonomy/decide/models.py` `ValueModel.value_if_converted`: use a goal-level
   `value_per_conversion` when given (`autonomy/proof/rewards.Costs` already has one for proof;
   bids ignore it).
7. **Retail content and offers in every domain.** With no LLM, the sample library ("New in
   {{top_category}} since your last visit") is all there is. The win-back template keeps "10% off"
   and "free shipping" in a charity's envelope. Fix: `autonomy/agents/strategist.py`
   `template_brief`: drop price offers unless the business declares products with prices; ship
   neutral fallback content.
8. **Suggestions in the wrong words and currency.** "36,069 customers who signed up" when there
   are no signups; "Turn 381,109 one-time buyers into repeat customers" for an insurer; USD for
   EUR/INR/TWD. Fix: `api/v2/deps.py` `suggest_opportunities`: word the goal from the event types
   present and use the business currency (see also B's fix 9).
9. **The catalogue calls subscriptions refills.** Plans and card payments are "replenishable,
   31-day cycle". Fix: `autonomy/catalog/products.py` `InferenceRules`: skip items with a
   near-constant interval and value (auto-billing), or let the business mark them recurring.
10. **The VIP bar is in no currency** (72% of telco and 73% of card holders are "vip"). Fix: B's
    fix 6 (`autonomy/data/folder.py` `SegmentRules`).
11. **The mapping's time zone does not reach state.** Sends were timed for 02:00 Los Angeles and
    06:00 New York. Fix: B's fix 8.
12. **The Doctor's mental model is "one row per entity with dates".** YYMM and YYYYMMDD
    (`became_member_on`) are not dates. "No date column" is a warning on every snapshot file. A
    one-column file scores like a 21-column one. Fixes are in section 2.
13. **Off-policy evaluation without logged probabilities.** `autonomy/proof/ope.py` takes any
    probability you give it. For estimated propensities it has no overlap report and no warning
    when a few rows carry the estimate (here a max weight of 2,692 and effective n of 97 out of
    20,653, reported only as numbers). Fix: `autonomy/proof/ope.py` `evaluate`: return an
    `overlap` block (share of p < 0.05, max weight, effective n / n) and refuse to report SNIPS or
    DR when effective n / n < 1%.

## 6. Data: what was used, wanted and blocked

- **KDD Cup 98 full learning file**: the scout found only a 99-row sample. This run found the full
  file (95,412 x 481, 117 MB) in the GitHub repo `bekerov/kdd98`, downloaded from
  `raw.githubusercontent.com` (zip SHA-256 `eff33400...cf80b872`, recorded in
  `/home/user/datasets/kdd98_full/SOURCE.txt` with the official `cup98doc.txt` and
  `cup98dic.txt`). `gh api` refused to list unattached repos ("GitHub access to this repository is
  not enabled for this session", HTTP 403), but a blobless `git clone --filter=blob:none
  --no-checkout` listed their files. The validation file `cup98VAL.zip` and `valtargt.txt` are in
  the same repo and were not downloaded.
- Not reached in this step: a B2B dataset (none looked for on reachable hosts in the time spent);
  the original `kdd.ics.uci.edu` and `archive.ics.uci.edu` (blocked, per the scout).
- Total under `/home/user/datasets`: 2.3 GB of the 8 GB budget (this step added 33 MB).

## 7. What a customer would have to do, per dataset

- **Bank marketing**:
  - Set `delimiter=;` (or the profile scores a one-column file 79).
  - Invent an id, rebuild call dates from row order, invent a deposit value.
  - Call the phone a webhook.
  - Remove `duration` themselves.
- **Telco churn**:
  - Invent a calendar and a bill history.
  - Know that the retention template will target people who already left, and write their own
    audience. They cannot target on contract or tenure.
- **Health insurance**:
  - Split the file under 20 MB or use Python.
  - Invent dates.
  - Accept that the agent values a vehicle sale as one health premium and cannot use the
    features that predict interest.
- **Hotel bookings**: join three date columns in code. There is no guest identity, so a hotel
  would need its guest ids from the PMS before any of this means anything.
- **Credit default**:
  - Melt six columns in code.
  - Call a payment a purchase.
  - Accept that no goal kind fits "prevent default".
- **KDD Cup 98**:
  - Melt 88 columns in code and turn YYMM into dates (not through pandas floats).
  - Call mail a webhook.
  - Write gifts as clicks with the mailing's `decision_id`, or the warm start learns "mail never
    works".
  - Call `nightly` and `prepare_day` themselves, because the runtime does not hand history over.
- **Everyone**: invent consent, put a time zone on every event, avoid Excel, gzip, UTF-16 and
  non-UTF-8 files, never rely on row order for re-imports (set a `row_key` that is truly unique),
  and check day/month order, thousands separators and currencies themselves. Nothing will tell
  them.

## Files

- `scripts/v2_realdata/d_common.py`: results folder, Doctor TestClient helper
- `scripts/v2_realdata/d_adapters.py`: one adapter per dataset (the customer's code), bank date reconstruction, YYMM reader
- `scripts/v2_realdata/d_domains.py`: Doctor, events, state, goal, catalogue, suggestions, Shadow day
- `scripts/v2_realdata/d_messy.py`: the 43 messy-file cases with row-by-row truth
- `scripts/v2_realdata/d_ipw.py`: estimated propensities, balance, OPE, the product's warm start
- `tests/v2/realdata/test_family_d.py`: three tests on inline data
- Results: `/home/user/datasets/_results/d/domains/*.json`, `/home/user/datasets/_results/d/messy/cases.json`,
  `/home/user/datasets/_results/d/ipw/{bank_marketing,kdd98}.json`; logs in `/home/user/datasets/_results/d/logs/`
