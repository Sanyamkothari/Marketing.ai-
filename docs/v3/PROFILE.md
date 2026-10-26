# The customer profile

Plan v3, foundation A ([`plan-v3.md`](../../plan-v3.md) section 3.A). Each night every customer's history is
turned into 52 numbers, written to `CustomerState.features` with the instant they were built for in
`CustomerState.features_as_of`. The same definitions build the training rows of the slow models, so what a
model learned from and what the agent is fed cannot drift apart.

Code: `autonomy/profile/`. Tests: `tests/v3/profile/`.

## What it is

| File | What it holds |
|---|---|
| `features.py` | The registry. Each feature is defined once: name, group, one-line description, default, computation. Nothing else names a feature. |
| `facts.py` | `gather(events, as_of)` reads one customer's events once, in time order, into the small lists the features need. This is the only place events are read, so the only place point-in-time has to be right. |
| `shop.py` | `ShopPrices`: a log-spaced histogram of the shop's order values, for `price_level`. A few hundred counters however many orders there are. |
| `build.py` | `profile_for`, `build_profiles`, `training_rows`, `training_rows_from_store`. |

## The features

The table is generated from the registry (`FEATURES`), so it is the list the code runs. "Default" is what the
feature returns when there is nothing to measure. Where a zero would be ambiguous, an indicator feature
(`buy_ordered`, `trend_gap_known`, `price_level_known`, `price_full_after_offer_known`, `time_engaged_known`,
`ch_engaged`, `mom_viewed`, `mom_carted`) says whether the number is a measurement.

| # | Feature | Group | Description | Default |
|---|---|---|---|---|
| 1 | `buy_orders_30d` | buying | Orders placed in the last 30 days | 0 |
| 2 | `buy_orders_90d` | buying | Orders placed in the last 90 days | 0 |
| 3 | `buy_orders_365d` | buying | Orders placed in the last 365 days | 0 |
| 4 | `buy_orders_total` | buying | Orders placed in the customer's whole history | 0 |
| 5 | `buy_net_revenue` | buying | Money spent in the whole history, minus refunds | 0 |
| 6 | `buy_avg_order_value` | buying | Average value of one order (0 if no order yet) | 0 |
| 7 | `buy_margin_rate` | buying | Share of order value kept as margin, over orders with a value that report margin (0 if none do) | 0 |
| 8 | `buy_active_months` | buying | Calendar months in which the customer ordered | 0 |
| 9 | `buy_ordered` | buying | 1 if the customer has ever ordered, else 0 | 0 |
| 10 | `buy_days_since_last_order` | buying | Days since the last order (0 if no order yet; see buy_ordered) | 0 |
| 11 | `buy_tenure_days` | buying | Days since the first thing we saw the customer do (0 if nothing yet) | 0 |
| 12 | `trend_gap_known` | trend | 1 if the customer has ordered at least twice on different days, else 0 | 0 |
| 13 | `trend_avg_gap_days` | trend | Usual number of days between orders (0 unless trend_gap_known) | 0 |
| 14 | `trend_last_gap_ratio` | trend | Latest gap between orders against the earlier gaps: below 1 buying faster, above 1 slower (1 if under 3 orders) | 1 |
| 15 | `trend_overdue_ratio` | trend | Days since the last order against the usual gap: above 1 means later than usual (0 unless trend_gap_known) | 0 |
| 16 | `trend_order_momentum` | trend | Orders in the last 90 days against the 90 before, from -1 (slowing) to 1 (speeding up) | 0 |
| 17 | `taste_distinct_categories` | taste | Different product categories bought | 0 |
| 18 | `taste_top_category_share` | taste | Share of category-tagged orders that are in the favourite category (0 if none tagged) | 0 |
| 19 | `taste_repeat_item_share` | taste | Share of item-tagged orders that re-buy an item bought in an earlier order (0 if none tagged) | 0 |
| 20 | `taste_new_category_share_90d` | taste | Share of the last 90 days' tagged orders with a category not bought before then | 0 |
| 21 | `price_level_known` | price | 1 if the shop has enough orders to read a price level against, and the customer has spent money | 0 |
| 22 | `price_level` | price | Where the customer's average order sits among the shop's orders: 0 cheapest, 1 priciest (0.5 unless price_level_known) | 0.5 |
| 23 | `price_offer_sensitivity` | price | How much the customer needs an offer to buy, 0 to 1: offer share pulled toward 0.3 until there are enough orders | 0.3 |
| 24 | `price_full_after_offer_known` | price | 1 if an order with an offer was followed by another order, else 0 | 0 |
| 25 | `price_full_after_offer_share` | price | Share of orders right after an offer order that were at full price (0 unless known) | 0 |
| 26 | `time_engaged_known` | time | 1 if the customer has ever clicked or replied, so the hours below mean something, else 0 | 0 |
| 27 | `time_peak_hour` | time | Local hour of day (0 to 23) with the most clicks and replies (0 unless time_engaged_known) | 0 |
| 28 | `time_evening_share` | time | Share of clicks and replies between 18:00 and 23:59 local time (0 unless time_engaged_known) | 0 |
| 29 | `time_weekend_order_share` | time | Share of orders placed on a Saturday or Sunday, local time (0 if no order yet) | 0 |
| 30 | `time_payday_order_share` | time | Share of orders placed on the 1st or 2nd, or the last two days, of a month, local time (0 if no order yet) | 0 |
| 31 | `ch_open_rate` | channels | Share of messages sent that were opened; weak, as mail apps open messages by themselves (0 if none sent) | 0 |
| 32 | `ch_click_rate` | channels | Share of messages sent that were clicked (0 if none sent) | 0 |
| 33 | `ch_reply_rate` | channels | Share of messages sent that were answered (0 if none sent) | 0 |
| 34 | `ch_email_engage_rate` | channels | Share of emails sent that were clicked or answered (0 if none sent) | 0 |
| 35 | `ch_whatsapp_engage_rate` | channels | Share of WhatsApp messages sent that were clicked or answered (0 if none sent) | 0 |
| 36 | `ch_engaged` | channels | 1 if the customer has ever clicked or replied to a message, else 0 | 0 |
| 37 | `ch_days_since_engaged` | channels | Days since the customer last clicked or replied (0 if never; see ch_engaged) | 0 |
| 38 | `ch_contactable_channels` | channels | Channels we may message: address known, consent given, not unsubscribed | 0 |
| 39 | `resp_stop_replies` | responses | Replies that asked us to stop | 0 |
| 40 | `resp_not_now_replies` | responses | Replies that said 'not now' | 0 |
| 41 | `resp_snoozed` | responses | 1 if a 'not now' reply still holds, so we should wait, else 0 | 0 |
| 42 | `resp_complaints` | responses | Complaints the customer has made about our messages | 0 |
| 43 | `resp_returns` | responses | Refunds issued to the customer | 0 |
| 44 | `resp_refund_value_share` | responses | Refunded money as a share of money spent, at most 1 (0 if no order yet) | 0 |
| 45 | `resp_no_discounts` | responses | 1 if the customer said they do not want discounts, else 0 | 0 |
| 46 | `mom_viewed` | moments | 1 if the customer has ever viewed a product, else 0 | 0 |
| 47 | `mom_days_since_view` | moments | Days since the customer last viewed a product (0 if never; see mom_viewed) | 0 |
| 48 | `mom_viewed_not_bought` | moments | Different products viewed in the last 30 days and never bought | 0 |
| 49 | `mom_carted` | moments | 1 if the customer has ever added to a cart, else 0 | 0 |
| 50 | `mom_days_since_cart` | moments | Days since the customer last added to a cart (0 if never; see mom_carted) | 0 |
| 51 | `mom_cart_open` | moments | 1 if the last cart came after the last order (something may be waiting to buy), else 0 | 0 |
| 52 | `mom_refill_due_days` | moments | Days until the next order is due by the customer's usual gap; negative if overdue (0 unless trend_gap_known) | 0 |

Group sizes: buying 11, trend 5, taste 4, price 5, time 5, channels 8, responses 7, moments 7. The plan said
"about 40"; eight of the 52 are indicator features, which keep a missing value from looking like a real zero.

### What the plan listed that is not here, and why

The profile can only use what `autonomy/contracts/events.py` carries.

- *Average discount used.* A purchase says whether an offer was applied (`attrs["offer_id"]`), not how much it
  took off. `price_offer_sensitivity` and `price_full_after_offer_share` are what the events support.
- *Replies that are questions.* The Conversation agent leaves events behind for stop (an UNSUBSCRIBE with
  reason `reply_stop`), "not now" (a `snooze_until` PREFERENCE) and complaint, and none for a question or an
  "interested". Those three are counted; the others are only in `ch_reply_rate`.
- *Top categories as names.* A name is not a float. `taste_*` carries the shape of the customer's taste
  (how many categories, how concentrated, how new); `CustomerState.top_categories` still holds the names.
  The learned taste profile the plan lists as "later" is not part of this stream.
- *Per-channel numbers for every channel.* Email and WhatsApp have their own engagement rate; the rest
  are in the all-channel rates.
- *The Data Doctor's line -> order -> shopper roll-up.* The plan says the builder reuses it. That roll-up
  works on the Doctor's dataframes, before events exist; the profile reads `Event` objects, so it rolls
  lines up itself with one rule (below, "Orders"). A log loaded one PURCHASE per invoice line
  (`ONLINE_RETAIL_LINE_MAPPING`) therefore gives one order per invoice. What it does not do: lines of one
  order stamped at different instants stay separate orders, and nothing reads the Doctor's confirmed
  column choices. Wiring the profile to the Doctor's order-log path is left to the stream that builds it.

## How a value is read

- **Point in time.** `gather` drops every event with `ts > as_of` before reading anything. An event stamped
  exactly at `as_of` is kept, as everywhere in the runtime (`e.ts <= now`). The shop's price histogram
  (`ShopPrices.until`, `ShopPrices.at`) obeys the same rule. v1's feature builder
  (`engine/onboarding/features.py`) enforces the same guarantee in SQL, with one guard clause and an assertion
  that re-reads the generated query. It compiles feature specs to DuckDB over dataframes, which does not fit
  this stream of `Event` objects, so the profile does not import it; it takes over the idea (one place enforces
  the guard, and a test plants the future) and the inclusive snapshot instant.
- **Windows.** "30 days" is after `as_of - 30 days` and up to `as_of`: an order exactly 30 days ago is out.
- **Orders.** Purchases of one customer at the same instant that name the same order (`attrs["order_id"]`,
  else `attrs["invoice_no"]`, else none) are one order: values and margins summed (margin only if every line
  reports one), categories and items the union of the lines', offer if any line had one. Two invoices at the
  same instant stay two orders; unnamed purchases at the same instant are one. Every order count, value, gap
  and share is per order. `ShopPrices` applies the same rule to the shop's orders; it holds only the
  purchases of the current instant, so it needs the events in time order (`EventStore.scan` gives them so;
  `training_rows` sorts before it builds the histograms). `price_offer_sensitivity` therefore equals the
  folder's estimate on a log with one PURCHASE per order, and differs from it on a line-level log, where
  the folder counts lines.
- **Time zones, amounts, currency.** As in `autonomy/data/folder.py`: the customer's zone is the last valid
  `timezone` attr on an event they made themselves (signup, visit, view, cart, purchase), else UTC; hours,
  weekdays and month days are local. Amounts are `Event.value` in the business currency and are never
  converted; a rebuild already refuses mixed currencies (`MixedCurrencyError`). A purchase adds; a refund
  subtracts its size (`abs(value)`, as the event mapper writes refunds), so a refund stored as a negative
  number still takes money off. Here the profile differs from the folder, which subtracts the value as
  stored. A value that is not a finite number counts as 0.
- **Offers.** A purchase with `attrs["offer_id"]` set. The offer `none` is not an offer (the spend ledger
  reads it the same way). `price_offer_sensitivity` is the folder's own estimate (prior 0.3, three pseudo
  orders), and a test holds the two equal.
- **Engagement** is a click or a reply. Opens are counted in `ch_open_rate` and nowhere else, because mail
  privacy proxies open messages on the reader's behalf (the folder makes the same choice).
- **Price level** is the share of the shop's orders worth no more than the customer's average order, from a
  histogram with bins about 13% wide. It is 0.5 and `price_level_known` is 0 until the shop has 50 orders.
- **Finite floats.** Every value is a Python `float` and finite; a computation that comes out NaN or infinite
  becomes the feature's default. Ratios against the customer's own usual are capped at 10, and every share
  is kept in [0, 1]. `buy_margin_rate` reads only orders with a positive value; `price_level_known` needs a
  positive average order.
- **Buying cycle.** `trend_gap_known` is 1 only when the customer ordered on at least two different local
  days. `trend_avg_gap_days`, `trend_overdue_ratio` and `mom_refill_due_days` are 0 otherwise, so two orders
  a minute apart do not make a cycle.
- **Replies.** `resp_stop_replies` counts distinct replies (`attrs["reply_event_id"]`, else the event's own
  id) behind `reply_stop` unsubscribes: the Conversation agent unsubscribes every channel the customer has
  for one "stop", which is one reply. A `snooze_until` preference is read as `autonomy/decide/rules.py`
  reads it: an empty value clears the snooze (and is not a "not now" reply), a value that cannot be read is
  a snooze with no end, a time without a zone is on the customer's clock.
- **Order and repeats.** Events may come in any order and repeat an `event_id`; the first copy is used, in
  `profile_for` and in `training_rows`, as the stores drop repeats on append. Events of more than one
  customer, or a naive `as_of`, raise `ValueError`.

## How to run it

```python
from autonomy.profile import build_profiles, profile_for, training_rows, training_rows_from_store

profile_for(events_of_one_customer, as_of)             # dict[str, float]; reads nothing after as_of
build_profiles(event_store, state_store, as_of)         # fills features and features_as_of; returns the count
training_rows(events, [month_end_1, month_end_2])       # ProfileRow(customer_id, as_of, features), customers in
                                                        # id order, dates ascending, in memory
training_rows_from_store(event_store, customer_ids, as_ofs)   # the same, one customer's events at a time
```

`training_rows` gives a customer a row at a date only if they have an event at or before it. Each row equals
`profile_for(that customer's events, that date)`, with the shop histogram of that date (a test checks this),
and equals what `build_profiles` writes for the same date (a second test).

`build_profiles` pages the state store with `scan()`, profiles `batch_size` customers (default 2,000), writes
the batch back with `put`, then reads the next page. Events come per customer from
`EventStore.for_customer`, as `AutonomyRuntime._feed_history` reads them. Unless it is given a `ShopPrices` or
`price_context=False`, it first streams the log once (`EventStore.scan(until=...)`) to fill the price
histogram.

### In the runtime

`RuntimeConfig.nightly_profile` (default `False`, in `autonomy/runtime/service.py`) makes `tick` call
`build_profiles(self.events, self.states, now)` after the fold on the nightly tick and on any tick that did a
full rebuild. A rebuild builds states from the log and resets `features`, so refilling on every rebuild keeps a
state from losing its profile; doing it before `_decide` means the tick that rebuilds also decides on fresh
profiles. A failure becomes a `profile.error` warning alert and does not stop the tick. After a rebuild the
states had already lost their profile (a rebuild keeps only `group` and `scores`, `KEPT_ON_REBUILD`), so the
alert says some states have none until the next night; on a night without a rebuild, states not yet refilled
keep the last one. An ordinary tick leaves `features` as they were; `features_as_of` says how old they are.
The setting is a field of `RuntimeConfig`; `autonomy/runtime/settings.py` was not edited, so
`RuntimeSettings` does not expose it yet. The demo runtime (`autonomy/runtime/demo.py`) sets it `False`
explicitly.

It is off by default because nothing reads `features` yet, so turning it on changes no decision and only
adds cost:

- each nightly run (and each rebuild, including a process's first tick) streams the log once for the shop's
  prices and reads every customer's events once;
- every state carries 52 more floats. The daily sweep holds every refreshed state in one list
  (`everyone = [self.folder.refresh(s, now) for s in self.states.scan()]` in `service.py`), so its peak
  grows by the size of the profile per customer. `python -m tests.v3.profile.state_size` (2,000 synthetic
  states read back from JSON under `tracemalloc`) gave 3,576 bytes per state without the profile and 5,168
  with it: 1,592 bytes more, about 1.59 GB more at 1M customers. The bench below measures
  `build_profiles` only, not this.

Turn it on when the first consumer of `features` lands, and measure the sweep's peak with it at that point.

## Measured

All on this machine: 4 CPUs (`nproc` = 4), 15 GB, Linux, Python 3.11.17, one process. Synthetic data from
`tests/v3/profile/synthetic.py` (about 15 events per customer, 52 features each); it is not any real shop.

**Scale bench.**

    /home/user/Marketing.ai-/.venv/bin/python -m tests.v3.profile.bench --customers 100000

It writes the events to a SQLite file with `SqlEventStore`, folds them into states with the production
`StateFolder` into `SqlStateStore`, then runs `build_profiles` once in a fresh process while sampling the
process's resident memory (RSS) every 10 ms. Result:

| Measure | 100,000 customers | 5,000 customers |
|---|---|---|
| Events in the log | 1,511,798 | 75,050 |
| `build_profiles` wall time (batch 2,000, shop price pass included) | 80.8 s | 4.4 s |
| Customers per second | 1,238 | 1,142 |
| `profile_for` alone, per customer (events already in memory) | 72 microseconds | 67 microseconds |
| Process RSS before the build | 143 MB | 143 MB |
| Process RSS peak during the build | 198 MB | 196 MB |
| Growth during the build | 55 MB | 53 MB |

A second run, through the slow test (`pytest tests/v3/profile/test_scale.py -m slow`), gave 80.5 s, 1,242
customers per second and +54 MB.

These bench figures were measured at commit `08f37a60`, before the review fixes (orders rolled up from
lines, the shop histogram's roll-up, the gap and snooze rules). The fixes add a little work per purchase and
were not re-benched here (the 100k bench is too heavy for this shared machine): pending CI, with
`/home/user/Marketing.ai-/.venv/bin/python -m pytest tests/v3/profile/test_scale.py -m slow -s`.

Memory did not grow with the shop: twenty times the customers cost 2 MB more at the peak. A second check
isolates the Python heap with `tracemalloc` (`test_memory_does_not_grow_with_the_number_of_customers`, marked
slow because it takes about 7 s: batch and store page size 100, `build_profiles` over SQLite stores). After
the review fixes, `pytest tests/v3/profile/test_scale.py::test_memory_does_not_grow_with_the_number_of_customers
-s` printed a peak of 3,222,511 bytes at 200 customers and 3,317,766 bytes at 1,000.

**Against the plan's pass test** (1M customers in under 15 minutes on 4 CPUs, under 4 GB): 1M was **not run**.
Filling a 1M-customer SQLite database (about 15 million events) would take roughly 40 minutes on this
shared 4-CPU machine (the 100k load and fold took 145 s and 79 s), so it was not attempted. Extrapolating the 100k rate linearly, 1,000,000 customers would take about 808
seconds (13.5 minutes), and `build_profiles`' own memory is flat. That covers the build only: with the
profile on, the daily sweep's list of every state grows by about 1.59 GB at 1M customers (above), so the 4 GB
bar has to be checked on the sweep too, not only on the build. That is a projection from
one SQLite run, with little headroom against 15 minutes, and Postgres could be faster or slower. Run the same
command with `--customers 1000000` (it needs about 40 minutes to fill the database first) to settle the build,
and measure the sweep's peak at 1M with `nightly_profile` on to settle the memory.

**Where the time goes** (cProfile of a 5,000-customer build, so absolute times are inflated, shares are
indicative): reading each customer's events with `SqlEventStore.for_customer` 47%, the shop price pass 16%,
computing the 52 features 15%, writing states back 11%. The compute itself is about 70 microseconds a customer,
which is 70 seconds per million; the rest is the store. A store method that returns many customers' events
in one query would be the next step if the time matters; the profile code does not need to change for it.

**Tests.**

    /home/user/Marketing.ai-/.venv/bin/python -m pytest tests/v3/profile -p no:cacheprovider -m "not slow"
    /home/user/Marketing.ai-/.venv/bin/python -m pytest tests/v3/profile -m slow     # the tracemalloc check and the 100,000-customer bench, a few minutes

## Not measured here

- **The signal test** (a simple model on the profile beats one on v2's state on held-out AUC for MegaFon, X5
  and Criteo). Those datasets are not in this environment and their hosts are blocked. The profile exists to
  be tested that way; nothing here claims it passes.
- **Postgres.** The stores measured are SQLite.
- **1M customers**, as above.
