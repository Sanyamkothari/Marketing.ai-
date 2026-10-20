# Autonomy on real data: what we learned

20 Oct 2026, branch `v2/realdata`. We ran Autonomy (v2) on 25 open datasets: real randomised
campaigns, shop transaction logs, click streams, and files from banks, telcos, insurers, hotels and
charities. The aim was to find out where it fails, not to show it off. Every number below comes from
a run we did. The command and the output JSON for each number are in the per-family notes:

- [A: randomised campaigns, agent scored off-policy](real_data/A-randomised.md)
- [B: transaction logs, catalogue, Shadow day](real_data/B-transactions.md)
- [C: event streams and scale](real_data/C-scale.md)
- [D: other domains and messy files](real_data/D-domains-messy.md)
- [Dataset catalogue and sources](real_data/CATALOGUE.md), [what Autonomy takes as input](real_data/INPUTS.md)

A second agent re-ran each family's headline numbers. All of them reproduced. That review also
withdrew or corrected several claims; each family note has a "Skeptical review" section that lists
them. This page uses the corrected figures.

## The short version

1. **On randomised data the agent never beat the best simple policy.** On Hillstrom it came close
   to "send everyone the men's e-mail" but scored slightly below it. On X5, a T-learner, a segment
   rule or plain send-to-all did as well or better.
2. **It overestimates what a message is worth by about 10x.** On Hillstrom it expected $4.08 per
   e-mail, against $0.38 measured. The result is that it contacts about 94-97% of the customers it
   may decide for, so in practice it behaves like send-to-all.
3. **It starts cold even when it has history.** The runtime never passes imported history to the
   agent's warm start. With 1.56M past mailings in the store, it used 0.
4. **Mapping a file to events often goes silently wrong.** 17 of 43 messy-file cases built from real
   rows gave wrong numbers with no warning: dates with day and month swapped, '2.550' read as 2.55,
   two currencies summed, rows lost on re-export.
5. **Scale is limited by memory.** The in-memory event path holds about 1.6-2.4 KB per event. About
   4.4M events (200k customers) need 9-10 GB. A daily sweep of 300k customers completed. A sweep of
   1M customers was OOM-killed twice, on a machine shared with other jobs. The SQLite store slows
   down quadratically as data grows.

## What we tested

| Family | Dataset | Size | Randomised? | Ran end to end? |
|---|---|---|---|---|
| A | Hillstrom e-mail (MineThatData) | 64,000 customers | yes, 3 arms | yes |
| A | X5 RetailHero (SMS + receipts) | 200,039 labelled clients, 45.8M purchase lines | yes, 50/50 | product path on all 200k clients; agent comparison on 100k (full agent runs OOM-killed) |
| A | Criteo uplift v2.1 | 13,979,592 rows | yes, 85/15 | baselines on all rows; agent on a 150k sample |
| A | MegaFon, Starbucks promo, Lenta | 600k / 126k / 687k | yes | yes, but customer features cannot reach the agent |
| A | Open Bandit (ZOZO) samples | 4 x 10,000 | logged propensities | OPE check only; the agent has no "item in a slot" action |
| A | Starbucks rewards | 17,000 people | unknown (simulated) | not used: no valid propensity |
| B | Online Retail I / II (UCI) | 4,371 / 5,939 customers, 0.5M / 1.1M lines | no | yes (II needed an adapter) |
| B | Olist, CDNOW, Ta-Feng, Superstore | 95k / 24k / 32k / 793 customers | no | yes, after adapters |
| B | Instacart, Dunnhumby (next-basket) | 20k / 36k customers | no | no: no dates or prices, so no events; recommender only |
| C | RetailRocket click stream | 2.76M events, 1.41M visitors | no | yes, after an adapter |
| C | Taobao sample, REES46 parquet | 1.95M rows / unreadable | no | no: no time column / no pyarrow |
| C | Synthetic customers resampled from Online Retail II | up to 1M customers | invented outcomes | scale only; up to 300k completed |
| D | Bank telemarketing, Telco churn, health cross-sell, hotel bookings, credit default | 7k to 381k customers | no | yes, but every one needed an adapter that invented something |
| D | KDD Cup 98 donations (full) | 95,412 donors, 1.84M events | no | yes |
| D | 43 messy variants of real rows | 2,000-row base | n/a | 17 silent errors, 8 refusals |

Datasets on disk: 2.3 GB, under the 8 GB budget; all of it lives outside the repo, in
`/home/user/datasets/`.

## Headline numbers

### Incremental value on randomised data

Paired doubly-robust estimates (the repo's `autonomy.proof` IPS/SNIPS/DR) on a test half split by
customer, with 95% intervals. Everything was fitted on the train half only. These intervals are not
corrected for the dozens of comparisons we ran, so a result that only just excludes zero is
suggestive, not proven.

| Dataset (reward) | Best simple policy vs nothing | Agent vs best simple policy |
|---|---|---|
| Hillstrom (profit per customer, 40% margin) | send-all men's e-mail: **+$0.381** [+0.216, +0.547] | click agent (contacts 97.5%): **-$0.014** [-0.026, -0.002]; purchase agent: **-$0.088** [-0.142, -0.033] |
| X5, 100k clients (buyers per client) | send-to-all: **+0.0351** [+0.0266, +0.0436] | click agent contacts **0%** (SMS logs no clicks); purchase agent: **-0.0091** [-0.0131, -0.0051] |
| Criteo (profit at break-even cost) | segment rule f2+f8: +0.0172 [+0.0166, +0.0178] vs best blanket arm | click agent vs blanket: -0.0014 [-0.0027, -0.0001]; purchase agent contacts 0% |
| MegaFon (profit at break-even cost) | T-learner: +0.486 [+0.443, +0.528] vs best blanket arm | the agent sees every customer as the same prospect and has nothing to target with |
| Starbucks promo / Lenta | segment rule +0.090 [+0.070, +0.111] / T-learner +0.037 [+0.006, +0.068] | the agent sent at random or to nearly everyone; profit about 0 |

On the feature-only sets, the "break-even cost" is a cost we constructed so that blanket sending
breaks even on train. Those gains only show that customer features carry signal and the agent cannot
use them.

**Calibration:** on Hillstrom the agent expected $4.08 per e-mail after cost. The experiment
measured $0.377 [0.211, 0.543] per e-mailed customer. Criteo shows the same gap independently:
about $0.24 expected against about $0.023 measured, both before cost. Raising the e-mail cost from
$0.001 to $0.25 changed none of its decisions.

**Content choice works modestly.** Learning from 21,384 Hillstrom sends, the agent picked the
men's e-mail 99.8% of the time. That beats a random men's/women's pick by +$0.095 [+0.004, +0.186],
and it ties "always men's".

### Recommender lift (co-purchase vs best-sellers, hit rate @5, forward time split)

| Dataset | Co-purchase vs best-sellers |
|---|---|
| Online Retail I | 34.7% vs 22.0% (matches CATALOG.md exactly) |
| Online Retail II | **+8.8 points** [+6.5, +10.9] |
| Instacart | +4.6 [+4.1, +5.1] (with invented weekly dates) |
| Dunnhumby | +1.2 [+1.0, +1.4] (depends on the invented dates; same-basket-only it ties) |
| Ta-Feng | **-1.3** [-1.8, -0.7] (loses at every k) |
| Superstore | loses at @5/@10, e.g. -2.1 [-3.7, -0.5] (5 vs 14 hits, n=429) |
| Olist | cannot be told apart (96.7% single-item baskets; 1 replenishable item of 32,729) |

The refill-timing model gives mixed results. It beats the naive mean gap on Online Retail II
(93.1 vs 97.1 days) and on Superstore at order level, and loses or ties elsewhere. Its 14-day
purchase probability ranks well on Online Retail II (AUC 0.807) and CDNOW (0.851), but on Ta-Feng
it is 2.9x too high.

### Scale limits (4-CPU, 16 GB machine shared with other runs; times are upper bounds)

| What | Largest that completed | Time and peak memory | Where it broke |
|---|---|---|---|
| Ingest + state rebuild, in memory | RetailRocket, all 2.76M rows, 1.41M visitors | 239 s, 10.3 GB | memory: about 2 KB per event plus 3 KB per customer state |
| Same, X5 receipts | 200,039 clients, 4.4M events | rebuild 164 s, 9.0 GB (10.4 GB with the agent) | full agent runs at 200k were OOM-killed 3 times (shared cgroup) |
| Ingest + rebuild, SQLite | all 2.76M RetailRocket rows | 2,454 s | the keyset scan is quadratic: 2.9 s at 100k events, 709 s at 2.76M |
| Daily sweep (Shadow, 3 goals) | 300k customers | 313 s tick, 890-1,220 decisions/s | 1M customers OOM-killed twice (at 11.5 and 9.7 GB, with other jobs in the cgroup) |
| Nightly learning | 300k customers | 135 s | `ContactHarm.refit` needs about 13 KB per customer at peak; 1M records OOM-killed twice |
| Criteo with pandas baselines (not the event path) | 13,979,592 rows | about 10 min, 7.3 GB | n/a |

## What data it handles

**Well:** order-level purchase logs with a customer id, a timestamp and an amount (Online Retail,
CDNOW, X5 receipts). It also handles past e-mail campaigns that logged sends, clicks and the send
probability (Hillstrom, once a `customer_id` and `logged_probability` were added). The mapper never
crashed: rows it could not read came back as listed problems.

**Badly:**
- **Click streams.** Views and carts only update "last seen". Item, price and session are dropped,
  and 99.2% of RetailRocket visitors become empty prospects.
- **Campaigns without click tracking** (SMS, direct mail, telemarketing). The default agent learns a
  click rate of about 0 and then never contacts anyone.
- **Subscriptions and retention.** "Retained" means "bought in the window", so the retention goal
  targets people who have already cancelled.
- **Non-retail domains** (bank, insurer, charity, hotel, card issuer) need code that invents ids,
  calendars or values. There is no phone or mail channel.
- **Non-dollar currencies.** The VIP bar of 500 makes 88.5% of X5's rouble clients VIP and 73% of
  the Taiwanese card holders.
- **Wide files with customer features.** Columns such as age, contract or past response cannot reach
  the agent's state.

**Not at all:**
- files with no timestamp (Taobao sample; Instacart and Dunnhumby next-basket);
- purchases with no amount (RetailRocket transactions, until a fake 0 is added);
- parquet without pyarrow;
- xlsx, gzip or UTF-16 uploads, which are refused with a misleading "CSV tokenizing" message;
- uploads over 20 MB through the Data Doctor route (413);
- item-in-slot recommendation logs (Open Bandit).

## How much data it can handle

In memory, one process fit **4.4M events (X5, 200k customers) in 9 GB, and 2.76M events with 1.41M
customer states (RetailRocket) in 10.3 GB**. That is roughly 1.6-2.4 KB of peak memory per event. The daily decision sweep completed at 300k customers
and failed at 1M in a shared 14 GB cgroup. The **first bottleneck is memory, not CPU.** Three
things cause it: the in-memory event path, the sweep keeping every Decision and a copy of every
state for the whole tick (about 7.4 KB per decision), and `ContactHarm.refit`'s grids. On SQLite
the first bottleneck is the quadratic scan in `autonomy/store/sql.py`. A one-line change to the SQL
cut a raw paging test from 13.8 s to 0.8 s on 400k events. The product code is unchanged. Postgres
was not tested.

Throughput (X5): 24k-39k rows/s through `map_frame` and 18k-32k events/s folded into state; 0.45-1.5
ms per decision.

## Where it fails, ranked by harm to a real customer

| # | Failure | Evidence | Fix (file) |
|---|---|---|---|
| 1 | Expected value of a message is about 10x too high, so it contacts nearly everyone | Hillstrom: $4.08 expected vs $0.38 measured; it contacts 94-97% of its group in every domain | `autonomy/runtime/agent.py` `_warm_start`: calibrate the lift per click from a past campaign's randomised control group. `autonomy/data/loaders.py`: book Hillstrom spend as real orders (it doubles order value today) |
| 2 | History never reaches the agent, and daily fitting never runs | `history_sends_used=0` with 1.56M mailings stored; segment cuts and organic rate stay at defaults | `autonomy/runtime/service.py`: `_replay` should feed a new policy its audience's full history (not only customers it has already decided on, and not only 30 days); `_decide` should call `policy.prepare_day` |
| 3 | Channels without click tracking are never used | 0% contacted on X5 SMS, MegaFon, Starbucks, Lenta | `autonomy/runtime/agent.py` `ClickResponseModel.warm_start`: treat a channel with zero clicks as unknown and fall back to purchases. `autonomy/learning/observations.py`: count a tied purchase as the response |
| 4 | Silent wrong data from mapping | event ids are row positions (a re-export drops 100 new rows and double-counts 100 old ones); day/month swaps; '2.550' read as 2.55 (-96.6% revenue); currencies summed; placeholder or `1.2E+11` ids merge 99 customers into 1 | `autonomy/data/mapping.py`: content-hash row keys; `decimal`, `currency`, `id_format` and date-bounds options; treat placeholder ids as missing. `api/v2/engines/doctor.py`: flag ambiguous day/month |
| 5 | Customer features cannot be used for targeting | MegaFon: a T-learner earns +0.486, while the agent sees one kind of customer | `autonomy/decide/bidders.py`: read an optional per-action uplift score from a slow model |
| 6 | Retention goal targets people who already left | telco: retention audience = churners, built into the definition | `autonomy/agents/strategist.py`: target `churn_risk`; add a `cancel` event in `autonomy/contracts/events.py`; `autonomy/proof/rewards.py` |
| 7 | Currency-blind thresholds and order values | VIP for 88.5% of X5 and 73% of card holders; reasons print "$" for roubles | `autonomy/data/folder.py`: VIP bar from the shop's own distribution; `autonomy/runtime/agent.py`: pass currency on; `autonomy/learning/bundle.py`: seed order value from history |
| 8 | Learning bug: early sends dropped from the harm model | win-back model counted 45 of 851 sends and read "100% put off" on outcomes with no effect | `autonomy/learning/update.py` `learn_contact_harm`: take `data_start` from full history, never after `split_at` |
| 9 | Missing consent is never explained; production baseline group is never contacted | every decision says "no active goal has anything to offer", with an empty `blocked_by`; 10% get "no baseline rule is set" | `autonomy/decide/policy.py`: put `no_consent` in `blocked_by`; `autonomy/runtime/settings.py`: pass `RuleBaseline()` |
| 10 | Sends timed for the wrong time zone | CDNOW at 06:00 New York, telco at 02:00 Los Angeles | `autonomy/data/mapping.py`: put the mapping's timezone on customer events |
| 11 | Scale ceiling (see above) | 1M-customer sweep OOM; SQLite quadratic | `autonomy/runtime/service.py`: stream the sweep in batches; `autonomy/store/sql.py`: add a leading `ts >= ?`; `autonomy/decide/bidders.py`: chunk `ContactHarm.refit` |
| 12 | Cart recovery messages people who only viewed | 13,129 of 13,657 targeted had never carted | add `last_cart_at` (`autonomy/contracts/customer.py`, `autonomy/data/folder.py`); point the template at it |
| 13 | Data Doctor misses leaks and sentinels, false alarms on logs | bank `duration` leak and `pdays=999` not flagged; one future date costs 20 points whether it covers 6 rows or 1,522; YYYYMMDD not seen as a date | `api/v2/engines/doctor.py`: events mode, sentinel, leak-by-timing and totals checks, more date shapes, a larger cap or streaming |
| 14 | Catalogue recommends where popularity is better | Ta-Feng and Superstore losses | `autonomy/catalog/enrich.py`: gate on a nightly self-backtest |
| 15 | Smaller gaps | loader writes `probability` where `logged_probability` is read; Decision drops per-option probabilities; no phone/mail channel; Shadow counts a customer once per goal; `ope.py` gives no overlap warning (bank: effective n 97 of 20,653) | `autonomy/data/loaders.py`; `autonomy/decide/policy.py`; `autonomy/contracts/base.py`; `autonomy/runtime/service.py`; `autonomy/proof/ope.py` |

## Silent wrong-result risks (no error, wrong answer)

These are the ones to fear, because a customer would not notice them:

- **Dates:** day and month swapped (all 2,000 December rows dated 12 January); 2-digit years
  becoming 2068; 1900 placeholder dates; naive times stored 5 hours off.
- **Money:** European decimals (-96.6% revenue); two currencies summed; a totals row doubling
  revenue; a negative purchase counted as a purchase; exact duplicate lines summed (+8.6% revenue).
- **Identity:** placeholder ids ('0', 'unknown', '-', 'guest') each becoming one big customer;
  Excel scientific notation turning 99 customers into 1; leading zeros giving 0 overlap between
  orders and consent; mixed-case e-mails splitting one person in two.
- **File shape:** a newest-first re-export losing new rows (event ids are row positions); a stray
  quote silently merging 4 rows; a duplicated header using the wrong column (99 customers became 7);
  a `;` file read as one column and still scoring 79/100; cp1251 text stored as mojibake.
- **Model:** the 10x value overestimate, the "100% put off" harm reading, VIP labels in the wrong
  currency, a Criteo head slice with no control rows getting propensity 0.85, and a refill model
  2.9x too confident on Ta-Feng. None of these raise an error.

## Datasets blocked by the network (hosts to allow)

| Host | What we wanted | What we used instead |
|---|---|---|
| `kaggle.com` | REES46 full months (about 42M events per month), Instacart raw, Olist/RetailRocket/Telco originals | GitHub mirrors and small samples |
| `huggingface.co`, `go.criteo.net` | Criteo uplift | GitHub release mirror `jszymon/uplift_sklearn_data` (checksum matches) |
| `archive.ics.uci.edu`, `kdd.ics.uci.edu` | UCI originals, KDD Cup 98 | GitHub mirrors (`bekerov/kdd98` for the full KDD file) |
| `research.zozo.com` | Open Bandit full (about 26M rows) | 4 x 10k samples from `st-tech/zr-obp` |
| `tianchi.aliyun.com` | Taobao UserBehavior with timestamps (about 100M rows) | subset without timestamps (unusable) |
| `dunnhumby.com` | Complete Journey with campaign and coupon tables | next-basket version (no campaigns) |
| `www.minethatdata.com` | Hillstrom original | scikit-uplift S3 copy (checksum matches) |
| `zenodo.org`, `www.openml.org`, `figshare.com`, `data.world`, `www.brucehardie.com` | various | CDNOW from the `Lifetimes` wheel on PyPI |
| (not the network) | Google Merchandise Store on BigQuery | none: no GCP credentials in the session |

The most valuable hosts to open are `kaggle.com` (REES46 at real scale) and `research.zozo.com`
(full Open Bandit). After those come `tianchi.aliyun.com` (Taobao with timestamps) and
`dunnhumby.com` (a real coupon campaign).

## Next steps

1. **Fix calibration first** (failures 1-3). Until the agent's value estimate is about right and it
   learns from history and from channels without clicks, it is a costlier send-to-all. Then re-run
   `scripts/v2_realdata/a_hillstrom.py` and `a_x5.py`: the bar is to beat send-all men's on
   Hillstrom and send-to-all on X5 with intervals that exclude zero.
2. **Make mapping refuse rather than guess** (failure 4 and the silent list). Most of these are
   small options in `autonomy/data/mapping.py` plus Doctor checks. `scripts/v2_realdata/d_messy.py`
   is a ready regression suite of 43 cases.
3. **Let features in** through a slow-model uplift score (failure 5). MegaFon, Lenta and Criteo then
   become fair tests.
4. **Stream the sweep, fix the SQLite scan, chunk `ContactHarm.refit`**. Then repeat the 1M-customer
   run on an idle machine, and test Postgres, which nothing here touched.
5. **Fix the subscription and non-retail model**: cancel events, phone and mail channels, a
   goal-level conversion value, currency.
6. Open the hosts above and re-run family C on full REES46 months.

What these tests cannot show: every randomised dataset is a one-shot campaign. The agent's
weeks-long learning (its own holdout, fatigue, timing) was never exercised, and no dataset records
unsubscribes or complaints. A loss here does not prove the agent would lose live. Nothing here
shows that it would win.

## How to re-run

Scripts are in `scripts/v2_realdata/`, tests in `tests/v2/realdata/`. Run them with `python3 -P`
(FastAPI is in user site-packages). Results JSON goes to `/home/user/datasets/_results/`. Each
family note lists its exact commands.
