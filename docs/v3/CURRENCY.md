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
(`autonomy/mandate/catalogue.py`): for each channel it lists in the shop's currency, the cheapest provider's rate
(`CatalogueVersion.channel_rate(channel, currency=...)`) **replaces** the setting. A rate in another currency is
never used. With no `currency` set, the catalogue's currency is the shop's when it prices messages in exactly one.
The catalogue is read every tick, so a version published later is used from then on. Sends already charged keep the
price they were charged at; a full rebuild of the spend ledger from the log prices every send at the current table.
No production caller passes a catalogue yet (nothing in the wiring builds an `ActionCatalogue`); until one does, the
settings are the source.

### Refusing what cannot be priced

`ShopPrices.check_goal` (`autonomy/runtime/prices.py`) raises `MissingShopMoneyError` with a plain list of what is
missing. A goal cannot be priced when:

* the shop has no currency, or the goal's currency is not the shop's;
* the goal may use email, WhatsApp or SMS and there is no price for it in the shop's currency (a price of 0 is a
  price; an absent one is not, and is never defaulted);
* `typical_order_value` is not set.

It is enforced three times:

1. `build_runtime` raises at start while an active goal cannot be priced.
2. The tick does not decide for such a goal and raises a critical alert `prices.missing` once a day, naming what to set.
3. The API refuses `approve` and `resume` of such a goal (HTTP 409, same message).

Shadow goals are included: their decisions show the marketer expected profit, and that is wrong in dollars too.

### What stays dollar-sized, on purpose

`build_agent_policy(costs=None, complaint_cost=None, default_order_value=None)` and `SpendLedger()` keep the
dollar-sized defaults. The Gym, the bench and the demo never build a `ShopPrices`, so their numbers are the same by
construction. `JourneyConfig` called through `load_journeys()` with no order value uses the reference order 50
(`REFERENCE_ORDER_VALUE`), which gives the bars the file always had.

## What moves

* **A shop that sets its own prices.** WhatsApp's price rises to what it set. With the example in the tests (WhatsApp
  0.85, email 0.12, usual order 1,800 INR) WhatsApp costs 28.3 times what the old table said (0.85 / 0.03), the
  complaint cost is 72.0 (was 2.0) and journeys C and D are offered to customers worth 1,800 or more (was 50). The
  agent will therefore send less WhatsApp than before, and the spend ledger records the real WhatsApp bill. These
  example prices are test inputs, not measurements of any provider; a real shop uses its own.
* **A dollar shop that sets the old numbers** (email 0.001, WhatsApp 0.03, order 50) decides exactly as before.
* **Existing deployments** that set none of this stop at start with the error above. That is the intent (review
  section 8, "refuse to start without them").

## Evidence

All run in this worktree.

* `pytest tests/v3/currency` (17 tests). The end-to-end one builds the production runtime from environment
  settings (`INR`, WhatsApp 0.85), sends to three customers on a fake WhatsApp adapter, and asserts the ledger's spend
  is sends x 0.85 (and the old table would have recorded less).
* Gym smoke, `python -m autonomy.gym.run --scenario baseline_shop --policy agent --customers 200 --days 30 --seeds 1
  --no-compare --report-json ...`, on this branch and on the base commit 29382881 (extracted with `git archive`): the two
  JSON reports are identical except the `seconds` field (5.1 against 5.3). The line printed by both: `$928 more
  profit than the holdout ... 1.71 contacts per customer per week; 4.2% unsubscribed; 0 rule breaks`.
* The default fingerprint test, `tests/v3/preset/test_preset.py::test_the_default_agent_decides_and_learns_as_before_the_preset`,
  passes unchanged (`DEFAULT_FINGERPRINT` was not edited).
* Existing tests that build a production runtime on dollar goals now set the dollar prices explicitly
  (`tests/v2/runtime/test_service.py`, `tests/v2/zero_touch/wiring_support.py`,
  `scripts/v2_zero_touch_rehearsal.py`); no assertion changed.
* Pending CI: the real bench (`.github/workflows/realbench.yml`) and the full Gym (`gym.yml`). Both pass their own
  costs (`common.py`) and never build a `ShopPrices`, so no change is expected; the check is the same workflow runs
  compared with the numbers in [STATUS.md](STATUS.md).

## Not done here

* Offers' cost per redemption, the offer budget price and the other v3 money numbers (`DEFAULT_OFFER_PRICING`, the
  moments' lifts) are not rescaled; v3 is off. The onboarding opportunity sizing (`autonomy/onboarding/priors.py`
  `CHANNEL_COSTS`) still quotes the dollar table to the marketer before a goal exists.
* The usual order value is a setting, not yet learned from the shop's own orders at onboarding (a quantile of its order
  values would remove one thing to configure). The agent's value model already learns the shop's average order as
  purchases arrive (`LearnedValueModel`); the setting is only the starting point.
* Nothing yet builds an `ActionCatalogue` in production, so provider prices come from the settings.
