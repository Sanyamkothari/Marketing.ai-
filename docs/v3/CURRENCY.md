# Money in the shop's currency

Foundation fix 2 of [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) section 8 (review section 4 row 1; findings A3, E1, D8, F8).
It fixes the plain (v2) agent and the production path. v3 stays off.

## The bug

Production read dollar-sized constants as the shop's currency. `autonomy/runtime/settings.py` never passed channel
costs, so the agent, the spend ledger and the overspend breaker all used `DEFAULT_CHANNEL_COSTS`
(`autonomy/decide/bidders.py`: email 0.001, WhatsApp 0.03, SMS 0.02). In rupees that prices WhatsApp about 30 times
too cheap (a Meta/BSP bill of roughly 0.8-1.0 per message), so the agent chose WhatsApp where it does not pay, and
the budget the marketer signed was not enforced on WhatsApp. Three more dollar numbers rode along: the complaint
cost (`COMPLAINT_COST` 2.0), the order value assumed for a customer with no orders (`ValueModel.default_order_value`
50) and the journeys' "high value" bar (`value_bar: 50`). A goal's currency defaulted to "USD" everywhere.

## What changed

| Number | Before | Now |
|---|---|---|
| Channel costs (agent, spend ledger, overspend breaker, mandate check) | `DEFAULT_CHANNEL_COSTS`, never overridden in production | A required per-shop setting, or the mandate catalogue's provider prices. No default in production |
| Usual order value / order value assumed for new customers | 50 | `typical_order_value`, a required per-shop setting |
| Complaint cost | 2.0 | `complaint_cost`, else 4% of `typical_order_value` (2.0 on an order of 50, so a dollar shop that sets 50 keeps 2.0) |
| Journeys C and D value bar | `value_bar: 50` | `value_bar_order_multiple: 1.0`: one usual order. The file's bar is 50 only when the usual order is 50 |
| Currency symbol in a decision's reason | "$" | the shop's ("₹" for INR, the code for a currency with no symbol) |
| New goal's currency from the API | "USD" | the currency in the words, else the shop's, else USD |

### Settings (`RuntimeSettings`, environment `AUTONOMY_*`)

| Setting | Environment | Meaning |
|---|---|---|
| `currency` | `AUTONOMY_CURRENCY` | ISO code, e.g. `INR`. Every other money setting is in it |
| `channel_costs` | `AUTONOMY_CHANNEL_COSTS=email=0.12,whatsapp=0.85` | What one message costs per channel. Push, web and webhook are free unless listed |
| `typical_order_value` | `AUTONOMY_TYPICAL_ORDER_VALUE` | The shop's usual order, in `currency` |
| `complaint_cost` | `AUTONOMY_COMPLAINT_COST` | Optional; cost of one spam complaint beyond the lost contact |


`build_runtime(..., catalogue=ActionCatalogue)` also reads the mandate catalogue
(`autonomy/mandate/catalogue.py`): for each channel it lists in the shop's currency, the **dearest** provider's rate
**replaces** the setting. The dearest, not the cheapest, because the gateway fails over between providers on a channel
and a send that goes through a dearer one must not be charged at a cheaper one's price against the signed budget. A
rate in another currency is never used. With no `currency` set, the catalogue's currency is the shop's when it prices
messages in exactly one. `channel_costs` given to `build_runtime` with no currency in the settings and no catalogue
raise `ValueError` (as the settings do), never dropped. No production caller passes a catalogue yet (nothing in the
wiring builds an `ActionCatalogue`); until one does, the settings are the source.

**Re-read every tick.** The runtime reads the prices at the start of every tick. The spend ledger and the mandate
check use the new table from then on, and each goal's agent policy is built again when any of the shop's money figures
changes (`ShopPrices.money_key()` is part of the policy cache key; a rebuilt policy replays the log), so the agent's
bids and the ledger charge the same price for the same send.

**Repricing of past sends.** A SEND event carries no unit price. The ledger only adds while the process runs, so a
send keeps the price it was charged at until the next full rebuild. A full rebuild (every night by default,
`nightly_rebuild`, and at every process start) replays the period's log at the current table: after a price change,
the whole period's spend moves to the new price overnight. A rise can then trip the budget or the overspend breaker
earlier than the old price would have; a drop under-counts. Recording the unit price on each SEND would remove this;
it is not done here.

### Refusing what cannot be priced

`ShopPrices.check_goal` (`autonomy/runtime/prices.py`) raises `MissingShopMoneyError` with a plain list of what is
missing (`problems_for(currency, channels)` gives the same list for a goal not created yet). A goal cannot be priced
when:

* the shop has no currency, or the goal's currency is not the shop's;
* the goal may use email, WhatsApp or SMS and there is no price for it in the shop's currency (a price of 0 is a
  price; an absent one is not, and is never defaulted);
* `typical_order_value` is not set.

It is enforced per goal, never for the whole process:

1. The tick does not decide for such a goal and raises a critical alert `prices.missing` once a day, naming what to set.
2. The API refuses `approve` and `resume` of such a goal (HTTP 409, same message).
3. The one-click onboarding (`POST /v2/onboarding/accept`) checks the shop's prices for the goal's currency and the
   mandate's channels **before** it stores a goal, signs a mandate or writes a plan (HTTP 409, nothing written).

The runtime itself always starts, in the API and in the worker: a goal that cannot be priced must not stop the API
(the marketer could not pause or fix it, and STOP replies, privacy requests and ingest would be lost). An earlier
version of this branch refused to start; that was dropped after review.

A signed mandate's money limits are in `Mandate.currency`. A mandate in another currency than the shop's does not
authorise its goals: the goal does not act and the `mandate.not_active` alert says which currencies differ.

Shadow goals are included: their decisions show the marketer expected profit, and that is wrong in dollars too.

### Onboarding

`GET /v2/onboarding/proposals`, `/limits` and `POST /v2/onboarding/accept` size each proposal's budget (reached x
`MESSAGES_COSTED` messages x the price of the channel each customer is reached on) with the runtime's `ShopPrices`
and in the shop's currency. That budget becomes the goal's budget and the signed mandate's `budget_ceiling`. A
customer reached on a paid channel with no price gives the proposal no money (budget 0, with a note naming the
missing price) rather than a dollar figure read as the shop's currency. The goal suggestions served at
`/v2/opportunities` are budgeted the same way. Without a runtime that has prices (the demo, the fakes) the documented
dollar-sized defaults (`autonomy/onboarding/priors.py` `CHANNEL_COSTS`) are used, as before.

### What stays dollar-sized, on purpose

`build_agent_policy(costs=None, complaint_cost=None, default_order_value=None)` and `SpendLedger()` keep the
dollar-sized defaults. The Gym, the bench and the demo never build a `ShopPrices`. The Gym and the demo rely on those
defaults (they do not pass the costs explicitly); the bench passes its own (`scripts/v2_realbench/common.py`,
`build_agent_policy(..., costs=dict(inp.costs))`) and leaves the complaint cost and the order value at the
defaults. Their numbers are therefore the same by construction.

`load_journeys()` with no `order_value` leaves the bars of C and D as multiples of the order value, and
`JourneyRunner` refuses such a config until `JourneyConfig.for_shop(order_value)` is applied, so no fixed bar is ever
applied to a shop by default. `build_agent_policy` resolves it with `default_order_value` (the shop's), or with the
reference order 50 (`REFERENCE_ORDER_VALUE`) when none is given, which is the Gym's and the bench's path and gives the
bars the file always had. `JourneyConfig.with_value_bar(id, bar)` sets an exact bar for one shop, or no bar with
None; either wins over the multiple.

## What moves

* **A shop that sets its own prices.** WhatsApp's price rises to what it set. With the example in the tests (WhatsApp
  0.85, email 0.12, usual order 1,800 INR) WhatsApp costs 28.3 times what the old table said (0.85 / 0.03), the
  complaint cost is 72.0 (was 2.0) and journeys C and D are offered to customers worth 1,800 or more (was 50). The
  agent will therefore send less WhatsApp than before, and the spend ledger records the real WhatsApp bill. An
  onboarding proposal in that shop is budgeted at 0.12 an e-mail instead of 0.001. These example prices are test
  inputs, not measurements of any provider; a real shop uses its own.
* **A dollar shop that sets the old numbers** (email 0.001, WhatsApp 0.03, order 50) gets the same channel costs,
  complaint cost (2.0), order value and journey bars as the defaults, so it should decide as before. No command here
  compares the two decision by decision: this equivalence is untested.
* **Existing deployments** that set none of this keep starting, but none of their goals runs until the money is set:
  each raises `prices.missing` and approve and resume answer 409. That is the intent (review section 8, "refuse to
  start without them"), applied per goal.
* **A catalogue with several providers on a channel** is charged at the dearest one's rate (before this fix: the
  cheapest).

## Evidence

All run in this worktree.

* `pytest tests/v3/currency` (24 tests, 24 passed). Among them:
  * the end-to-end one builds the production runtime from environment settings (`INR`, WhatsApp 0.85), sends to
    three customers on a fake WhatsApp adapter, and asserts the ledger's spend is sends x 0.85;
  * an INR mandate with a daily cap of 4 rupees lets exactly one 0.85 send go and blocks the others (at 0.03 all of
    them would fit), and a USD mandate over the INR goal halts it;
  * the real agent policy factory, ticked twice around a newly published catalogue rate, logs WhatsApp bids at 0.78
    and then at 0.95;
  * a runtime with an unpriced live goal (and an ended USD goal still marked active) starts, the goal does not decide,
    it raises `prices.missing`, and the API can still pause it but not resume it;
  * an INR shop's accepted onboarding proposal has budget = ceil(reached x 2 x 0.12) on goal and mandate, and an
    unpriced shop's accept answers 409 and writes no goal, no mandate and no ledger entry.
* Gym smoke, `python -m autonomy.gym.run --scenario baseline_shop --policy agent --customers 200 --days 30 --seeds 1
  --no-compare --report-json ...`, on this branch after the review fixes. It prints `$928 more profit than the
  holdout ... 1.71 contacts per customer per week; 4.2% unsubscribed; 0 rule breaks`, the same line the builder
  recorded for the base commit 29382881 (not re-run here).
* The default fingerprint test, `tests/v3/preset/test_preset.py::test_the_default_agent_decides_and_learns_as_before_the_preset`,
  passes unchanged (`DEFAULT_FINGERPRINT` was not edited).
* Existing tests that build a production runtime on dollar goals now set the dollar prices explicitly
  (`tests/v2/runtime/test_service.py`, `tests/v2/zero_touch/wiring_support.py`,
  `scripts/v2_zero_touch_rehearsal.py`); no assertion changed. Tests that load the shipped journeys and run them
  directly now pass the reference order (`load_journeys(order_value=REFERENCE_ORDER_VALUE)`) in
  `tests/v3/journeys/test_journeys.py` and `tests/v3/preset/test_preset.py`; no assertion changed.
* Pending CI: the real bench (`.github/workflows/realbench.yml`) and the full Gym (`gym.yml`). Neither builds a
  `ShopPrices` (above), so no change is expected; the check is the same workflow runs compared with the numbers in
  [STATUS.md](STATUS.md).

## Not done here

* Offers' cost per redemption, the offer budget price and the other v3 money numbers (`DEFAULT_OFFER_PRICING`, the
  moments' lifts) are not rescaled; v3 is off.
* (Done on v4, docs/v4/ONBOARDING_MARGIN.md.) The API's proof and measurement read their channel costs from `api/v2/deps.py` `SEND_COST` (dollar-sized), not from
  the shop's prices.
* A SEND does not record its unit price or provider, so a full rebuild reprices the period (above), and the budget is
  held at the dearest listed provider rather than the one actually used.
* The usual order value is a setting, not yet learned from the shop's own orders at onboarding (a quantile of its order
  values would remove one thing to configure). The agent's value model already learns the shop's average order as
  purchases arrive (`LearnedValueModel`); the setting is only the starting point.
* Nothing yet builds an `ActionCatalogue` in production, so provider prices come from the settings.
