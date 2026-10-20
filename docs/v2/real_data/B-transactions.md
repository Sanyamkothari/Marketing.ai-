# Family B: transaction logs

Runner B, 20 Oct 2026, branch `v2/realdata`. Eight public order logs (and X5's receipts, used only
for the scale test) were put through what a business onboarding Autonomy would hit: Data Doctor,
mapping, customer-state rebuild, the catalogue engine, goal suggestions, the warm start, and one
Shadow-mode day of the real runtime. **No product code was changed.** Where the software failed,
the failure is written down with the file I would change.

**How to read the numbers.** Every number below comes from a run whose command is given, and the
JSON it wrote sits in `/home/user/datasets/_results/b/<stage>/<dataset>.json`, outside the repo.
Wall times were measured on a shared 4-CPU machine while other runners were busy (load average
6 to 10, recorded in each JSON where captured), so **times are upper bounds**. Memory is the
process's peak RSS. The numbers cited are from the first run, whose JSON is kept unchanged in
`/home/user/datasets/_results/b/_first_run/`; the review re-ran some stages (its JSON is in
`.../b/review_rerun/` and, for ingest of CDNOW, Olist and Superstore, in `.../b/ingest/`) and the
deterministic numbers were identical; only wall times differ.

**What these data can and cannot show.** None of these logs is randomised, and none holds a single
message, consent record or click. They show whether Autonomy reads the data, what state and
suggestions it builds, how good its purchase-timing and recommendation models are against simple
baselines (the `p14` and recommender checks are forward time splits, so fair forecast tests; the
refill holdout is not strictly forward, see section 2), and what it would do on day one.
They **cannot** show that any message would cause a purchase or make money. The Shadow day's
"expected to add $0.56" figures are the agent's untrained priors, not evidence. No off-policy
evaluation (IPS/SNIPS/DR) was run in family B: there is no logged treatment to evaluate, and none
should be read into these results.

**Skeptical review (same day).** A second pass re-ran the catalogue (Online Retail I, Ta-Feng,
CDNOW, Superstore), Shadow (Online Retail I, CDNOW, Superstore), ingest (CDNOW, Superstore, Olist)
and the 10,000-client scale step. Every deterministic number reproduced exactly (only wall times
moved). It also corrected the first draft in these places, all reflected below:

- The Olist orders file's **20/100 was mostly the runner's doing**, not the Doctor's. The runner
  passed `snapshot` = the day after the last *mapped* purchase, which is before Olist's own export
  (cancelled orders run later, and deliveries come after purchases). With no snapshot (the route's
  default) the file scores **100**; with the day after the file's latest realised timestamp it scores
  **80**, on 6 estimated-delivery dates. Superstore's future `ship_date` error (66 -> 86, with no
  snapshot and with the export snapshot) and Olist items' (73 -> 93, with no snapshot) disappear
  the same way. What is left is real but smaller: one `future_dates`
  error costs 20 points whether 6 rows or 1,522 are affected, and forecast columns count as
  "future".
- The refill-timing table showed one level per dataset, and on Olist and Superstore the level
  shown happened to be one where the model lost. With every level shown, the model also wins on Superstore at **order** level
  (140.3 vs 154.0 days) and on Olist at item and category level (small samples). The verdict is
  "mixed", not "wins only on the Online Retail shop".
- That holdout is **not strictly forward in time** (other customers' later gaps feed the
  population cycle; `backtest_last_interval`'s docstring says so). The p14 checks and the
  recommender are forward splits; the holdout is not.
- "Co-purchase ties on Dunnhumby" and "loses on Superstore" had no uncertainty behind them. The new
  `b_rec_ci.py` gives paired bootstrap intervals (section 2).
- Decisions per second: with consent the range was 520 to 1,060 a second (not 800 to 1,050), and
  every tick time includes the runtime's first-tick state rebuild, so these understate the decide
  step's own speed.
- SQLite append + rebuild was 5.7x to 10.5x the in-memory time (rebuild alone 2.0x to 4.7x), not
  "3x to 6x".
- Line-grain mapping ran at 12,000 to 19,000 events a second, not "about 20,000".
- The X5 reader counted a client whose lines straddle a 1M-line chunk boundary twice, so "10,000
  clients" built 9,999 states (39,996 and 99,989 at the larger sizes). Histories were complete and
  the tables always gave the states actually built; the reader is fixed (a re-run gives 10,000).
- Exact duplicate lines are not proof of double entry (a till can record the same item twice on
  one invoice); the notes now say "double counted if they are entry errors".

## The commands

```bash
cd /home/user/v2wt/realdata
python3 -P scripts/v2_realdata/b_ingest.py --all --http --sql   # Doctor, mapping, state, goals, warm-start inputs
python3 -P scripts/v2_realdata/b_catalogue.py --all             # refill cycles, p14, recommender
python3 -P scripts/v2_realdata/b_shadow.py --all                # one Shadow day, as-is and consent-invented
python3 -P scripts/v2_realdata/b_scale.py --curve 10000,40000,100000
python3 -P scripts/v2_realdata/b_scale.py --clients 20000 --shadow
python3 -m pytest tests/v2/realdata/test_b_transactions.py -q   # 3 tests, inline data, ~2 s
# review add-ons
python3 -P scripts/v2_realdata/b_doctor_snapshot.py             # Doctor with no / runner / export snapshot
python3 -P scripts/v2_realdata/b_rec_ci.py online_retail_ii ta_feng superstore olist instacart_nbr dunnhumby_nbr
```

`-P`, not `-I`: FastAPI lives in the user site-packages here (`/root/.local`), which `-I` drops,
so the Doctor import fails under `-I` (`ModuleNotFoundError: No module named 'fastapi'`). `-P`
still keeps the script and current directories off `sys.path`. Adapters are in
`scripts/v2_realdata/b_adapters.py`; each one is what a customer would have to write.

## The datasets side by side

From `b_ingest.py` (state at 10:00 local on the day after the last order) and `b_catalogue.py`.

| | Online Retail I | Online Retail II | Olist | CDNOW | Ta-Feng | Superstore | Instacart NBR | Dunnhumby NBR |
|---|---|---|---|---|---|---|---|---|
| kind | UK gift wholesaler | same shop, 2 years | Brazilian marketplace | US CD store | Taiwan grocery | fictional US retail | grocery, preprocessed | grocery, preprocessed |
| currency | GBP | GBP | BRL | USD | TWD | USD | none (no prices) | none (no prices) |
| span (days) | 373 | 738 | 729 | 545 | 119 | 1,457 | no dates | no dates |
| rows in | 541,909 lines | 1,067,371 lines | 99,441 orders | 69,659 rows | 817,741 lines | 9,994 lines | 2,759,107 lines | 2,188,352 lines |
| order events | 22,186 (18,532 buys, 3,654 refunds) | 44,870 (36,969 + 7,901) | 98,199 | 69,659 | 119,578 | 5,009 | **cannot be made** | **cannot be made** |
| customers in state | 4,371 | 5,939 | 94,983 | 23,570 | 32,266 | 793 | - | - |
| repeat rate (2+ orders) | 65.6% | 72.4% | **3.0%** | 49.5% | 63.2% | 98.5% | - | - |
| median order value | 303 GBP | 305 GBP | 105 BRL | 26 USD | 512 TWD | 152 USD | - | - |
| median days between orders | 21.9 | 24.7 | 29.0 | 31 | 9 | 128 | - | - |
| items per basket (median / mean) | 15 / 20.9 | 15 / 20.8 | 1 / 1.04 | no items | 5 / 6.8 | 1 / 2.0 | 7 / 9.0 | 5 / 7.4 |
| real margin | no | no | no | no | **yes** (price - cost) | **yes** (profit) | no | no |
| segments (folder) | active 1,211, vip 1,032, lapsed 862, new 646, lapsing 587, prospect 33 | lapsed 2,402, vip 1,616, active 908, lapsing 587, new 365, prospect 61 | lapsed 57,698, lapsing 19,657, new 17,042, active 581, vip 5 | lapsed 18,229, active 2,764, lapsing 2,059, vip 518 | active 12,178, new 8,717, **vip 7,556**, lapsing 3,815 | vip 346, lapsed 202, lapsing 162, active 81, new 2 | - | - |
| contactable customers | **0** | **0** | **0** | **0** | **0** | **0** | - | - |

What differs most: basket size (Olist and Superstore are one-item orders, so co-purchase has
almost nothing to learn), repeat rate (Olist 3%), time span (Ta-Feng 4 months, so nobody is
"lapsed" and the agent calls almost everyone "new"), and currency (a TWD order is 30x smaller in
money than a GBP one, and several fixed thresholds are not rescaled; see failures 6 and 7).

## 1. Ingestion and the Data Doctor

`profile_frame` and `read_csv_bytes` (the functions behind `POST /v2/engines/doctor/profile`) ran
on each file as a customer would upload it. Every file was also posted to the real route through
FastAPI's TestClient: the route's scores matched the in-process ones on every file it accepted, and
it returned **413 "the upload is over 20 MB"** for Online Retail I (45 MB), Online Retail II
(95 MB) and Ta-Feng (64 MB unzipped).

The scores below were taken with `snapshot` set to 10:00 local on the day after the last mapped
purchase. That is a choice of the runner, not something a customer would necessarily send (the
route's field is optional, and with no snapshot no future-date check runs). Where it matters the
no-snapshot score is given too (`b_doctor_snapshot.py`; `b_ingest.py` now records it as
`readiness_score_no_snapshot`).

| File | Route accepts as published? | Score (runner's snapshot / none) | What it flagged |
|---|---|---:|---|
| online-retail-dataset.csv (I) | no: 45 MB > 20 MB | 86 | duplicate_rows 5,268 (real); no_key (it did not see `CustomerID` as an id) |
| online_retail_II.csv | no: 95 MB | 86 | duplicate_rows 34,335 (real); duplicate_key on `Customer ID` 5,796 values, 243,007 blank (a log, so repeats are expected) |
| online_retail_II.xlsx | no: not CSV | - | `cannot read the file as CSV: Error tokenizing data. C error: Expected 2 fields in line 5, saw 3` |
| olist_orders (after gunzip) | no: .gz | **20 / 100** | under the runner's snapshot, 4 future_dates **errors**: order_estimated_delivery_date 1,522, delivered_customer 35, carrier 2, purchase 16. With the day after the file's latest realised timestamp (2018-10-17) as snapshot: 80, one error on 6 estimated delivery dates |
| olist_order_items (after gunzip) | no: .gz | 73 / 93 | duplicate_key on order_id (a line file); future shipping_limit_date 55 under the runner's snapshot only |
| olist_customers (after gunzip) | no: .gz | 93 | no_date_column |
| CDNOW_master.txt (`delimiter=\s+`) | yes, with that delimiter | 79 | duplicate_rows 255; duplicate_key on customer_id; **no_date_column** |
| ta_feng (unzipped) | no: zip, 64 MB | 93 | duplicate_key on CUSTOMER_ID |
| sample_superstore.csv | yes | 66 / 86 | personal_data customer_name (real); duplicate_key order_id; future_dates **error** on ship_date 42 under the runner's snapshot only (ship dates of the last orders fall after the last order; 86 with no snapshot or with the day after the last ship date) |
| Instacart_history.csv | yes | **86** | duplicate_key; no_date_column |
| Dunnhumby_history.csv | yes | **79** | duplicate_rows 21,656; duplicate_key; no_date_column |

**Useful flags:** exact duplicate rows (Online Retail II's 34,335 are 2.1% of its known-customer
sales value, and the built-in loader keeps them, so if they are entry errors they are double
counted; the data cannot say whether a repeated line is an error or a second identical line); the
customer name in Superstore.

**False alarms and misses.** The Doctor has one mental model, a table with one row per entity, and
transaction logs break it:

- *Duplicate key on every log*: a customer or order id repeats by design in a line file. It is a
  warning (-7) on most family-B files (Online Retail I gets `no_key` instead).
- *Future dates on forecasts, and a flat penalty*: an estimated delivery date after the snapshot
  is normal, yet it is a `future_dates` **error**, and one such error costs 20 points whether it
  covers 6 rows or 1,522. With a snapshot at Olist's own export moment, 6 estimated delivery dates
  (of 99,441 orders) take the orders file from 100 to 80. (The first draft's 20/100 came mostly
  from the runner's early snapshot, see the review note at the top; the other three "future"
  columns were realised dates that fall after the last purchase but before the export.)
- *YYYYMMDD not seen as a date*: CDNOW's `date` column (`19970101`) misses `_DATE_SHAPE`, so the
  file gets "no column holds dates".
- *Missed*: Online Retail I's 135,080 lines with no CustomerID (25%) raise no problem (the key
  heuristic `_KEY_HEADER` does not match `CustomerID`, and blanks are only counted for the key).
  Its 10,624 negative quantities and 2,515 zero prices are not mentioned anywhere
  (`b_doctor_snapshot.py`).
- *Not ready, scored ready*: the next-basket files have no time and no price, so no `Event` can be
  made from them at all, yet they score 86 and 79. "No date column" is only a warning.

**Mapping.** `map_frame` never crashed; bad rows came back as problems, as designed:
Online Retail I 135,120 `excluded` rows (guests, zero prices), Olist 1,234 `unknown_event_type`
(the canceled and unavailable orders I left unmapped on purpose) and 8 `invalid_event` (orders with
no item rows, so no value). Speed: 22,186 invoice events from 541,909 lines in 3.3 s; 406,789
line events in 21.6 s; Ta-Feng's 817,741 line events in 42 s with peak RSS 2.0 GB; Online Retail
II's 824,293 in 68 s; Olist's 112,101 in 8.8 s. That is 12,000 to 19,000 line events a second on
the loaded machine. An in-memory `Event` holds about 1.6 KB of Python objects (tracemalloc after
the input frame is freed: 1,593 B on CDNOW's order events and on Online Retail I's line events);
the process RSS per event is higher (about 2.1 to 2.4 KB retained on X5, section 6).

**State rebuild** (`rebuild_states`, production `StateFolder`). In-memory: 0.5 s (Online Retail I,
4,371 customers), 1.2 s (II), 7.7 s (Olist, 94,983 customers), 6.0 s (Ta-Feng). Through SQLite
(`autonomy.store`, in-memory engine): append + rebuild 3.2 + 2.1 s (I), 18.7 + 28.4 s (Olist),
23.4 + 17.8 s (Ta-Feng), 12.1 + 11.7 s (CDNOW), against 0.02 + 0.5, 0.5 + 7.7, 0.3 + 6.0 and
0.05 + 2.5 s in memory. Append + rebuild together is 5.7x to 10.5x the in-memory time across the
six datasets; the rebuild alone is 2.0x to 4.7x. (Measured in the same process right after the
in-memory rebuild, one run each.)

## 2. Catalogue engine

`b_catalogue.py`; line-level tables built from events through the product's own `purchases_frame`
and `basket_frame`.

**CATALOG.md reproduces.** On Online Retail I with the repo's own cleaning I get exactly the
published table: co-purchase hit-rate@5/10/20 34.7 / 46.2 / 60.6% vs popularity 22.0 / 33.6 /
45.0% (1,858 customers); refill timing MAE 54.1 vs 57.2 days on 58,774 pairs; p14 Brier 0.0320 vs
0.0342, AUC 0.750. Through the event path (which keeps postage and fee lines) the numbers barely
move (34.8 / 46.1 / 60.6%, 1,862 customers).

**Refill timing: hold out each repeat pair's last purchase** (MAE in days; naive = population mean
gap). Every level the runner computed is shown. This holdout is *not* strictly forward in time: the
held-out gaps are never seen, but other customers' gaps that fall later in the calendar do feed
the population cycle (see `backtest_last_interval`'s docstring). The `p14` table below is the
forward test.

| Dataset | level | pairs | model | naive | 80% interval coverage |
|---|---|---:|---:|---:|---:|
| Online Retail I | item | 58,994 | **54.1** | 57.2 | 77.9% |
| Online Retail I | order | 2,790 | **51.8** | 53.9 | 73.3% |
| Online Retail II | item | 120,068 | **93.1** | 97.1 | 75.0% |
| Online Retail II | order | 4,189 | **88.6** | 91.8 | 69.5% |
| Ta-Feng | item | 53,911 | 23.2 | 23.6 | 76.0% |
| Ta-Feng | category | 82,168 | 23.5 | 24.0 | 78.1% |
| Ta-Feng | order | 20,388 | 18.9 | 19.0 | 72.0% |
| CDNOW | order | 11,516 | 95.9 | **93.0** | 69.8% |
| Olist | item | 329 | 42.3 | 45.5 | 64.4% |
| Olist | category | 855 | 59.2 | 60.2 | 71.8% |
| Olist | order | 2,063 | 87.6 | **86.8** | 81.1% |
| Superstore | item | 45 | 367.1 | 367.1 | 15.6% |
| Superstore | category | 2,166 | 289.0 | **271.1** | 88.1% |
| Superstore | order | 781 | **140.3** | 154.0 | 77.2% |

Mixed. The model is 3.5% to 9% better than the naive mean gap on the Online Retail shop (both
levels, large samples) and on Superstore at order level (140 vs 154 days, 781 pairs); within about 2%
(a tie) on Ta-Feng at every level; worse on CDNOW (3%) and on Superstore at category level (7%).
On Olist it is a little better at item and category level and a little worse at order level, on
samples too small to read (329 to 2,063 pairs, almost all customers with no earlier gap of their
own). No interval or significance test was run on these MAEs. Its edge comes from customers with
3+ own gaps (Online Retail II: 54.3 vs 60.7; Superstore order level: 111.0 vs 134.1), and most
pairs elsewhere have 0 or 1.

**`p14` forward in time** (fit before the cutoff, bought in the next 14 days?)

| Dataset | level, cutoff | pairs | base rate | Brier model / naive | AUC | mean p14 | rate, window open / closed |
|---|---|---:|---:|---:|---:|---:|---:|
| Online Retail II | item, 2011-10-26 | 434,880 | 2.1% | 0.0195 / 0.0210 | 0.807 | 2.2% | 5.0% / 1.7% |
| CDNOW | order, 1998-05-17 | 23,570 | 3.3% | 0.0283 / 0.0323 | 0.851 | 4.4% | 11.3% / 2.3% |
| Ta-Feng | item, 2001-01-29 | 565,634 | 1.9% | 0.0206 / 0.0304 | **0.605** | **5.4%** | 2.3% / 1.8% |
| Ta-Feng | order, 2001-01-29 | 28,405 | 28.8% | 0.1871 / 0.2145 | 0.717 | 17.3% | 33.8% / 26.4% |
| Superstore | order, 2018-11-16 | 793 | 13.9% | 0.1250 / 0.1334 | 0.492 | 6.6% | 11.5% / 15.4% |

`p14` ranks well on the Online Retail and CDNOW logs (AUC 0.81 to 0.85, an open window lifts the
14-day rate 3x to 5x), though on CDNOW its mean (4.4%) is 1.3x the observed 3.3%. On Ta-Feng it is
**2.9x too high** at item level (mean 5.43% against 1.89% observed) and barely ranks (AUC 0.61);
its Brier score still beats the naive constant's, because that constant is even further off. At
Ta-Feng's order level it is the other way, too low (17.3% against 28.8%). On Superstore's order
level it ranks no better than chance (AUC 0.49, 793 customers) and is half the observed rate.
One cutoff per row; the second cutoff the runner also computed (Online Retail II 2011-09-11:
AUC 0.795; CDNOW 1998-04-02: 0.836) tells the same story.

**Catalogue inference and refill picks on the decision day**: Online Retail I 1,920 of 3,665
items replenishable, 2,918 of 4,338 customers with an open refill window; II 2,705 of 4,631 items;
Ta-Feng 1,494 of 23,812 items, 14,785 of 32,266 customers with a window open; **Olist 1 of 32,729
items** (12 customers with a refill item); **Superstore 0**. CDNOW has no item column.

**Co-purchase recommender vs best-sellers** (time split; a hit is a new product bought in the test window)

| Dataset | split | customers | cosine @5 / @10 / @20 | best-sellers @5 / @10 / @20 |
|---|---|---:|---|---|
| Online Retail I | 2011-09-11 + 90 d | 1,862 | **34.8 / 46.1 / 60.6%** | 21.9 / 33.5 / 44.6% |
| Online Retail II | 2011-09-11 + 90 d | 2,206 | **28.7 / 40.5 / 54.3%** | 19.9 / 30.2 / 42.0% |
| Ta-Feng | 2001-01-29 + 30 d | 13,259 | 17.2 / 22.8 / 29.1% | **18.4 / 25.8 / 33.2%** |
| Instacart NBR (invented time) | history -> next basket | 19,326 | **39.8 / 52.8 / 64.4%** | 35.2 / 48.6 / 59.1% |
| Dunnhumby NBR (invented time) | history -> next basket | 36,171 | 42.2 / 55.3 / 66.7% | 41.0 / 52.9 / 64.7% |
| Olist | 2018-06-06 + 90 d | 372 | 1.3 / 2.2 / 2.4% | 1.1 / 1.3 / 3.2% |
| Superstore | 2018-10-02 + 90 d | 429 | 1.2 / 1.6 / 3.0% | **3.3 / 4.4 / 5.4%** |

Hit rates are among customers who bought before the cutoff *and* bought at least one new product
in the test window (the same population for both methods).

**Paired difference, co-purchase minus best-sellers** (percentage points, 95% bootstrap interval
over customers, 2,000 resamples; `b_rec_ci.py`, which reproduces `evaluate`'s rates exactly). The
interval is sampling noise among this window's customers only, not a different period or shop.

| Dataset | customers | @5 | @10 | @20 |
|---|---:|---|---|---|
| Online Retail II | 2,206 | +8.8 [+6.5, +10.9] | +10.3 [+7.9, +12.5] | +12.3 [+10.1, +14.6] |
| Instacart NBR (invented time) | 19,326 | +4.6 [+4.1, +5.1] | +4.3 [+3.7, +4.8] | +5.3 [+4.7, +5.8] |
| Dunnhumby NBR (invented time) | 36,171 | +1.2 [+1.0, +1.4] | +2.3 [+2.0, +2.6] | +1.9 [+1.7, +2.2] |
| Ta-Feng | 13,259 | -1.3 [-1.8, -0.7] | -3.1 [-3.7, -2.4] | -4.1 [-4.9, -3.4] |
| Superstore | 429 | -2.1 [-3.7, -0.5] | -2.8 [-4.7, -0.9] | -2.3 [-4.9, +0.2] |
| Olist | 372 | +0.3 [-1.1, +1.6] | +0.8 [-0.8, +2.7] | -0.8 [-3.0, +1.1] |

Co-purchase beats best-sellers clearly on the wholesaler and on Instacart, by a small but real
margin on Dunnhumby (1 to 2 points; the first draft called this a tie), **loses on Ta-Feng
grocery** at every k, loses on Superstore at k = 5 and 10 (on 429 customers: 5 hits against 14 at
k = 5), and cannot be told apart from best-sellers on Olist. For the next-basket sets the times
are invented (weekly), and the default 30-day trip window then joins about four real baskets into
one trip; the same-basket-only variant, which does not depend on the spacing, gives 38.7% @5 on
Instacart (best-sellers 35.2%) and 40.7% on Dunnhumby (41.0%), so the Dunnhumby edge depends on
the invented spacing and the Instacart one does not. On Ta-Feng the default
30-day "trip" window joins a month of grocery runs into one trip; same-basket-only is even worse
(8.8% @5), so the problem is the signal, not only the window. Olist's single-item orders give 422
item links for 32,729 products. Speed: fit on 644,022 lines in 8.4 s; scoring 538 to 6,600
customers a second.

## 3. Goal suggestions

There are two suggestion paths in the code, and they disagree:

- **The API** (`api/v2/deps.py` `suggest_opportunities`, served at `/v2/opportunities`) only ever
  proposes *repeat purchase* (customers with exactly one order) and *onboarding* (no order), each
  with a fixed budget of 1,000 in any currency. It never proposes win-back, even on Olist, where
  83,486 of 94,983 customers match the win-back template.
- **The Analyst** (`autonomy/agents/analyst.py` `opportunities()`) sizes a budget from a segment
  summary. No product code builds that summary from `CustomerState`; I built it from the folder's
  segments. With reach taken from state it proposes **nothing anywhere**, because nobody is
  contactable. With reach treated as unknown it proposes one win-back goal per dataset:

| Dataset | API suggests | Analyst suggests (reach unknown) |
|---|---|---|
| Online Retail I | repeat purchase 1,493; onboarding 33 | win back 587 lapsing, about 794 GBP each, budget 2,800 GBP |
| Online Retail II | repeat purchase 1,623; onboarding 61 | win back 2,402 lapsed, budget 12,000 GBP |
| Olist | repeat purchase 92,096 | win back 57,698 lapsed, about 163 BRL each, budget 56,000 BRL |
| CDNOW | repeat purchase 11,908 | win back 18,229 lapsed, budget 6,400 USD |
| Ta-Feng | repeat purchase 11,878 | win back 3,815 lapsing, budget 11,000 TWD |
| Superstore | repeat purchase 12 | win back 202 lapsed, budget 1,100 USD |

The Analyst's "one_time" play never fires, because the folder calls one-order customers `new` and
the play listens for `one_time`. The API's audiences (`_AUDIENCES`) and the Strategist's templates
(`TEMPLATES`) also differ for the same goal kind; for Olist's cross-sell, 2,887 vs 11,497.

## 4. Warm start and priors

- **History the agent can learn from: none.** `logged_sends` finds 0 sends and 0 clicks in every
  family-B log. Order history alone gives the click model nothing, so the agent starts on its
  defaults wherever it is pointed at an order export.
- **Priors the runtime does not fit.** After the Shadow tick, every goal's `AgentPolicy` still had
  `segmenter_fitted=False`, cuts (0.15, 0.35) purchases per 30 days, organic rate 0.005 a day for
  everyone, and a fallback order value of 50 at margin 0.4. The runtime never calls
  `prepare_day`, which fits these (only the Gym's scoreboard does). Had it been called, the cuts
  would be (0.41, 0.72) on Online Retail I and (2.74, 5.23) on Ta-Feng, and the organic rate would
  be 0.024 and 0.053 a day: up to 10x what the agent used.
- **Ta-Feng's four months make everyone "new".** Even fitted, the agent's segmenter labels 32,073
  of 32,266 Ta-Feng customers `new` (`NEW_CUSTOMER_DAYS = 120` against a 119-day log).
- **The value prior is in no currency.** The median customer's order is 5.7x the 50 prior on
  Online Retail I, 14.2x on Ta-Feng (TWD) and 0.5x on CDNOW. A customer with one order is shrunk
  three orders' worth toward 50, whatever the currency.

## 5. One Shadow day of the real runtime

`b_shadow.py`. The runtime is built as `build_runtime` builds it (agent policy from the workspace,
10/10 holdout/baseline split by salted hash, the business's time zone), with the suggested goals
(both paths) turned into briefs by `template_brief` (the Strategist's no-LLM fallback), approved,
e-mail only, with the sample content library. One tick at 10:00 local on the day after the last
order. The win-back goal is the Analyst's suggestion *with reach treated as unknown*; with reach
taken from state the Analyst proposes nothing (section 3), so a real deployment would have had
only the API's goals. The tick seconds include the runtime's first-tick rebuild of every state
from the event store (`report.rebuilt` is true on every run), not only deciding.

| Dataset | goals | decisions (as is) | contacts, as is | consent invented: would contact | left alone (holdout / baseline / testing a rest) | tick seconds, incl. rebuild (as is / invented) |
|---|---|---:|---:|---:|---|---|
| Online Retail I | repeat, onboarding, win-back | 3,469 | **0** | 2,609 decisions (1,863 customers) | 349 / 349 / 162 | 2.7 / 6.6 |
| Online Retail II | repeat, onboarding, win-back | 5,135 | **0** | 3,837 (2,856 customers) | 526 / 514 / 258 | 3.4 / 6.2 |
| Olist | repeat, win-back | 174,734 | **0** | 130,996 (74,142 customers) | 17,491 / 17,418 / 8,829 | 52 / 166 |
| CDNOW | repeat, win-back | 33,009 | **0** | 24,791 (16,401) | 3,351 / 3,188 / 1,679 | 10 / 32 |
| Ta-Feng | repeat, win-back | 18,354 | **0** | 13,730 (9,668) | 1,860 / 1,820 / 944 | 8 / 23 |
| Superstore | repeat, win-back | 450 | **0** | 340 (330) | 48 / 41 / 21 | 0.24 / 0.48 |

- **As is, it contacts nobody**, which is right: there is no consent. But every agent-group
  customer's logged reason is *"Left alone: no active goal has anything to offer this customer."*
  and `blocked_by` is empty. The word "consent" appears nowhere in what a marketer would read.
- **With consent invented** (one e-mail `consent_granted` per customer, labelled), it would message
  about 94% of the agent group on day one (2,609 of 2,771 agent-group decisions on Online Retail I,
  130,996 of 139,825 on Olist; 93.6% to 94.2% everywhere), which is about three quarters of all
  decisions once the 20% holdout and baseline are counted. The rest (about 6%) are rests kept to
  measure what no message does. Every send is **e-mail with no discount** (the 10% and
  free-shipping offers were always pruned), split about evenly between the reminder and the
  how-to message. Expected net per message (its untrained prior, not evidence) is 0.21 to 0.62 in
  the business currency at the median.
- **Shadow counts one customer more than once.** Shadow goals do not mark a customer as contacted
  for the tick, so a customer in two audiences is a "would contact" for both: 2,609 decisions for
  1,863 customers on Online Retail I, and 130,996 for 74,142 on Olist. Live, one-contact-per-tick
  would allow one.
- **The baseline group gets nothing.** `build_runtime`'s policy has no baseline rule, so 10% of
  customers are "baseline group, and no baseline rule is set". The plan's "against the simple
  rule" comparison has no data in a production build.
- **Wrong send hour when the mapping's time zone is not a customer attr.** CDNOW and Superstore
  sends were timed for **06:00 and 05:00 New York time** (23,610 of 24,791 and 324 of 340), because
  the customers' state time zone stayed UTC. Olist and Ta-Feng, whose adapters also put a
  `timezone` attr on each event, got 10:00 local.
- **Probabilities are logged on every decision** (minimum 0.029 with consent invented, all in
  (0, 1]). That is necessary for a later off-policy evaluation, not sufficient: no offer was ever
  chosen, so a policy that uses the 10% or free-shipping offers has no support in such a log and
  could not be evaluated from it, and the rests are only about 6% of agent decisions.
- **Throughput** (decisions over the whole tick, so including the state rebuild): 1,280 to 3,350
  decisions a second with nobody contactable and 520 to 1,060 with consent (Olist, 174,734
  decisions in 166 s; Online Retail I, 3,469 in 6.6 s, which a quieter re-run did in 3.0 s, so
  these move 2x with machine load); 558 a second on X5 (19,998 customers in 36 s, of which about
  10 s is the rebuild, judging by the 10.6 s the same rebuild took just before it).

## 6. How much data it holds

`b_scale.py` on X5 RetailHero receipts (the first N clients of the file, complete histories; one
fresh process per size). In-memory stores, as above.

| clients (states) | lines read | receipt events | map_frame | rebuild_states | total | peak RSS |
|---:|---:|---:|---:|---:|---:|---:|
| 9,999 | 1,126,759 | 197,333 | 4.9 s | 5.3 s | 14 s | 552 MB |
| 39,996 | 4,548,931 | 798,136 | 20.7 s | 21.1 s | 53 s | 1,893 MB |
| 99,989 | 11,425,664 | 2,009,633 | 59.1 s | 63.9 s | 153 s | **4,692 MB** |

Time and memory grow roughly linearly: peak RSS is 2.8 / 2.4 / 2.3 KB per event and map +
rebuild 51 / 52 / 61 µs per event at the three sizes (one run each, loaded machine). The peak is
not mostly transient: a review re-run that read VmRSS after each step (`/proc/self/status`) found
529 MB still held after mapping and rebuild at 10,000 clients (peak 550) and 1,660 MB at 40,000
(peak 1,893), about 2.1 to 2.4 KB of RSS per event retained. A straight-line extrapolation (not
measured) puts X5's full 8.05M receipts at about 19 GB of peak RSS, more than this 15 GB
machine has, so the in-memory path tops out at a few million events on this machine. The SQL
stores avoid holding events, but they were 2x to 5x slower on rebuild alone here (section 1).
The client counts are 9,999 / 39,996 / 99,989, not 10k / 40k / 100k, because of a counting bug in
the runner's reader (fixed since; the histories were complete). Online Retail II's 1.07M lines went
through at line grain (824,293 events) in 68 s at a peak of 2.5 GB.

## 7. Failures, causes and the fix I would make

1. **No consent, no contact, wrong explanation.** Cause: `GoalBidder.quote` in
   `autonomy/decide/bidders.py` silently drops channels that are not contactable, so the decision
   says "no active goal has anything to offer" with an empty `blocked_by`. Fix:
   `autonomy/decide/policy.py`: when no bids come back, run `rules.consent` / `rules.contactable`
   on the briefs' channels and put `no_consent` / `no_address` in `blocked_by` and the reason (the
   decisions engine already does this for its own response, in `api/v2/engines/decisions.py`
   `_reach_code`).
2. **Priors never fitted in the runtime.** Cause: `AgentPolicy.prepare_day` (segment cuts, organic
   rates, warm start from history) is called only by the Gym. Fix: `autonomy/runtime/service.py`
   `_decide`: call `run.policy.prepare_day(everyone, ctx)` once a day before the sweep when the
   policy has it.
3. **Order history is not handed to a new policy.** Cause: `_replay` / `_learn` in
   `autonomy/runtime/service.py` pass events only for customers with decisions, and skip empty
   goals. A first deployment's history therefore never reaches the warm start. This one comes
   from reading the code, not from a run: family B has no sends, so a run could not show the loss,
   and the warm start itself only runs inside `prepare_day` (item 2), so the two go together.
   Fix: in `_replay`, hand a freshly built policy the event log of its audience before its first
   decision.
4. **Shadow counts a customer under every goal.** Cause: `_act` in `autonomy/runtime/service.py`
   returns for SHADOW before `contacted_this_tick.update(...)`. Fix: update `contacted_this_tick`
   for shadow ACT decisions too (without sending), so Shadow forecasts what live would do.
5. **The production baseline group is never contacted.** Cause: `agent_policy_factory` in
   `autonomy/runtime/settings.py` builds the agent with no `baseline_policy`. Fix: pass
   `RuleBaseline()` there, as `autonomy/runtime/demo.py` does.
6. **The VIP bar is in no currency.** 7,556 of 32,266 Ta-Feng customers (23%) are "vip", since
   500 TWD is about 16 USD; Olist has 5. Fix: `autonomy/data/folder.py` `SegmentRules`: derive
   `vip_min_revenue` from the business's own revenue distribution (say, its 80th percentile),
   unless it is set.
7. **The value prior and offer costs are in no currency.** `ValueModel.default_order_value = 50`
   (`autonomy/decide/models.py`) and the sample offers' costs (6 and 4) are used as-is in GBP, BRL
   and TWD. Fix: `autonomy/learning/bundle.py`: seed `LearnedValueModel.stats` from the business's
   purchase events at build time, so the fallback is the shop's own average order.
8. **A mapping's time zone does not reach the customer.** `EventMapping.timezone` only parses
   naive timestamps, and state keeps `timezone="UTC"`, so sends land at 05:00 to 06:00 local in the
   US. A customer can avoid it today with `fixed_attrs={"timezone": ...}` in the mapping (the
   Olist and Ta-Feng adapters did; the CDNOW and Superstore adapters did not, which is how it
   showed), but nothing tells them to, and the runtime's own `RuntimeConfig.timezone` is not used
   as the fallback. Fix: `autonomy/data/mapping.py` `map_frame`: add `attrs["timezone"]` from
   `mapping.timezone` on customer-action events unless a column supplies one.
9. **Goal suggestions.** `api/v2/deps.py` `suggest_opportunities` never proposes win-back and
   always budgets 1,000. Fix: build a `SegmentSummary` from state and call
   `Analyst.opportunities`. Also in `autonomy/agents/analyst.py` `PLAYS`, add `new` (the folder's
   label) to the repeat-purchase play's segments, or rename the folder's label.
10. **Data Doctor on transaction logs.** Fix, all in `api/v2/engines/doctor.py`: a `kind=events`
    option that checks the customer column for blanks instead of key uniqueness; treat date
    columns named `estimated_*` / `*limit*` as forecasts, not "future" errors, and scale the
    future-date penalty by the share of rows affected (6 of 99,441 rows cost 20 points); add
    `\d{8}` (YYYYMMDD) to `_DATE_SHAPE`; make a missing date column (and, for events, a missing
    amount) an error, not a warning.
11. **Online Retail II with the built-in loader.** The loader's date format is a constant
    (`autonomy/data/loaders.py` `ONLINE_RETAIL_DATE_FORMAT`), and the line mapping has no
    `row_key`, so exact duplicate lines are summed into invoices without a word. Fix: let
    `online_retail_log_events` take `date_format=` and column renames, and report exact duplicate
    lines (as `duplicate_row`) with an option to drop them; whether they are errors is the
    shop's call.
12. **Content ids collide across goals.** `autonomy/fakes/samples.sample_content` (a sample
    library, not what a real shop would load) gives every goal
    the ids `ci_0..ci_4`, and the workspace keys content by id, so a second goal's library replaces
    the first's. Its goal then decides "no active goal has anything to offer" (found in my first
    Shadow run; the runner now prefixes ids). Fix: in `sample_content`, include the goal id in each
    `content_id`, and have `InMemoryWorkspace.put_content` refuse an id that already belongs to
    another goal.
13. **Catalogue model limits on real data.** Item-level `p14` is 2.9x too high on Ta-Feng, and
    co-purchase loses to best-sellers there (bootstrap intervals exclude zero at every k) and on
    Superstore (at k = 5 and 10, on 429 customers). Fix: `autonomy/catalog/enrich.py`
    and the bidders should fall back to popularity, or skip the refill boost, when a nightly
    backtest (`calibrate_at_cutoff`, `evaluate`) shows the model losing on the shop's own data.
14. **Cannot ingest next-basket files.** No time, no price. Not a bug: the `Event` contract
    rightly refuses to invent them. The Doctor should say so (item 10).

## 8. What a customer would have to do, per dataset

- **Online Retail I**: nothing beyond the built-in loader. Cannot upload to the Doctor (45 MB).
- **Online Retail II**: rename three columns and rewrite the date column before the loader (code),
  and decide what to do about 34,335 duplicate lines. The .xlsx cannot be read; the CSV is 95 MB.
- **Olist**: join three tables (code), know that `customer_unique_id` is the person
  (`customer_id` is per order: mapping that would make the repeat rate 0%), and gunzip.
- **CDNOW**: set `delimiter=\s+` and `%Y%m%d`. Then it works, but there are no items for the
  catalogue.
- **Ta-Feng**: unzip, group lines into customer-day baskets (code), compute margin from cost
  (code); 64 MB is over the cap.
- **Superstore**: the person is a name; aggregate lines to orders and turn the discount column into
  `offer_id` (code).
- **Instacart / Dunnhumby next-basket**: cannot be used for state, goals or Shadow at all. Only
  the recommender can run, and only with invented times.
- **Everyone**: state consent as `consent_granted` events (no export here has it), or the agent
  contacts nobody; put a `timezone` attr on events; and write Python to store events and run the
  agent (there is no upload-and-run route).

## Files

- `scripts/v2_realdata/b_common.py`: timing, peak RSS, JSON output
- `scripts/v2_realdata/b_adapters.py`: one adapter per dataset (the customer's code)
- `scripts/v2_realdata/b_ingest.py`: Doctor, mapping, state, goals, warm-start inputs
- `scripts/v2_realdata/b_catalogue.py`: refill cycles, p14, recommender, CATALOG.md reproduction
- `scripts/v2_realdata/b_shadow.py`: one Shadow day of the real runtime
- `scripts/v2_realdata/b_scale.py`: X5 receipt scale curve and decision throughput
- `scripts/v2_realdata/b_doctor_snapshot.py` (review): the Doctor with no / runner / export snapshot
- `scripts/v2_realdata/b_rec_ci.py` (review): paired bootstrap intervals, co-purchase vs best-sellers
- `tests/v2/realdata/test_b_transactions.py`: three tests on inline data
- Results: `/home/user/datasets/_results/b/{ingest,catalogue,shadow,scale,rec_ci,doctor_snapshot}/*.json`
  (first run kept in `_first_run/`, review re-runs and the VmRSS probe `mem_check.py` in
  `review_rerun/`); logs in
  `/home/user/datasets/_results/b/logs/`
