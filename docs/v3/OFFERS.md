# v3 offers: the cheapest offer that changes behaviour

**Stream:** plan-v3 decision 3 ([`plan-v3.md`](../../plan-v3.md) section 4). **Branch:** `v3/offers`.
**Status:** built and unit-tested; opt-in, default off. The Gym and sealed-data bars of the plan are
**not run here** (CI runs them after integration), so this page makes no claim about them.

## What it does

1. **A wider menu of offers, each with a cost model** (`autonomy/mandate/catalogue.py: offer_cost`,
   `OfferOption.model_cost`).
2. **A planner** (`autonomy/decide/planner.py`) that spends the offer budget on all customers at once,
   by extra profit per unit of offer cost.
3. **A budget price** (`autonomy/decide/pacing.py: BudgetPrice`), a Lagrange multiplier that pacing
   moves day by day, so per-customer decisions approximate the plan without seeing everyone.
4. **Two hooks in the bidder** (`GoalBidder(offer_cost_fn=..., budget_price=...)`), both `None` by default.
5. **A switch in the agent** (`build_agent_policy(..., offer_pricing=OfferPricing(...))`), `None` by
   default, that builds one budget price per goal and moves it from the nightly loop.

Nothing here runs unless a caller passes the hooks, turns on `offer_pricing` or builds a plan. With
them off, the bids are the v2 bids (`tests/v3/offers/test_bidder_hook.py`: one test on the sample
customer, one across four customers with the refill boost and contact harm on).

## The cost of one redemption

An explicit `cost_per_redemption` on the offer always wins (every v2 envelope sets one). Otherwise:

| Kind | Cost | Fields it reads |
|---|---|---|
| `none`, `content` | 0 | |
| `percent_off` | `amount`% of the customer's usual basket | `amount`; basket = `CustomerState.avg_order_value`, else the shop's `default_basket` |
| `amount_off` | `amount` | `amount` |
| `free_shipping` | shipping cost per order | `shipping_cost` (new) |
| `bundle` (new kind) | the money taken off the bundle | `bundle_discount`, `bundle_item_ids` (new) |
| `loyalty` | `points x point_value x redemption_rate` | `points`, `point_value`, `redemption_rate` (new; rate defaults to 1, the cautious end) |
| `gift`, `early_access` (new kind) | the configured cost | `cost_per_redemption` |

**Every reader sees the same cost.** An offer priced only through the new fields (free shipping,
bundle, loyalty) has `cost_per_redemption` filled in from them when it is built
(`OfferOption._price_from_cost_model`). So the budget ledger (`service._offer_price`), the mandate's
`catalogue_costs` and margin floor, the proof and the v2 bidder all charge it, not 0
(`test_costs.py::test_the_mandates_budget_accounting_sees_the_derived_cost`). A v2 offer leaves the
new fields at their defaults, so its cost is unchanged.

**An unknown basket is never free.** A percent-off offer with no configured cost has no price for a
customer with no orders. `planner.basket_offer_cost` uses the shop's `default_basket` for them, and
returns None when that is not known either. The bidder does not bid an offer whose cost function
returns None.

`bundle_offer(...)` builds a bundle from the co-purchase recommender (`CoPurchaseModel.related`): the
anchor item plus its best-co-bought items that have a price, discounted by `percent_off` of their
list prices (stated as both `bundle_discount` and `cost_per_redemption`). Items without a price are
skipped, and fewer than two priced items gives no bundle.

An offer is paid for by everyone who buys while it is open, not only by those it persuades. So the
expected spend of an offer for one customer is `cost of one redemption x p_redeem`, where `p_redeem`
is the organic chance of buying plus the uplift. The bidder already did this; the planner reuses it.

### Additive, and old data still reads the same

Two `OfferKind` values (`bundle`, `early_access`) and six optional `OfferOption` fields. A new field
that is still at its default is left out of every dump (`exclude_if`), so an offer that does not use
them serialises exactly as in v2. That keeps old hashes valid: a catalogue version written by v2
still passes `compute_hash` after the upgrade
(`test_costs.py::test_a_catalogue_version_saved_by_v2_still_verifies`).

**The Gym keeps its worlds.** `PlantedTruth` draws one multiplier column per offer kind from the
main random stream, so two more kinds would have shifted every later draw (themes, hours, signup
times, favourite categories) for every seed. The seven v2 kinds are now drawn in their old order,
and kinds added later are drawn from their own stream (`default_rng([seed, 8])`). A scratch script
compared the arrays against the base commit's `truth.py` (with the seven v2 kinds) for
`discount_cannibalisation`, 60 customers, seeds 1 and 7: all equal. Their sums are pinned in
`test_safety_and_wiring.py::test_the_gym_plants_the_same_customers_as_v2`.

### The safety rules and the agents

* `rules.DISCOUNT_KINDS` now includes `bundle`, so `no_discounts=true` blocks a bundle just as it
  blocks money off. Early access is not a discount.
* `creative.OFFER_THEMES` has themes for the new kinds (bundle: savings, convenience; early access:
  novelty, loyalty), and the Gym's history theme maps bundle to savings.
* The critic lets a bundle's copy state its own percent (`amount`), and no other figure.
* The strategist's templates propose only the seven v2 kinds. A new kind enters an envelope only when
  a marketer puts it there.

## The plan

For each customer-offer pair:

    extra profit = (uplift of the offer - uplift of the best plain message) x value - offer cost x p_redeem
    cost         = offer cost x p_redeem

`planner.offer_economics` computes this. `candidates_from_bids` reads it from the bids that
`GoalBidder.quote` returns: an offer's net against the best plain bid, or against doing nothing. The
bidder multiplies `expected_net` by the goal's urgency but not `offer_cost`, so the net is divided
by the urgency first, which keeps extra profit and cost in the same money. With the customer's
`state` and the `envelope`, bids the hard rules would block are dropped first: a discount to a
customer who asked for none, a channel they cannot be reached on, an offer waiting for approval.

`plan_offers(candidates, budget, envelope=..., audience=...)` then:

* drops every pair whose extra profit is not above zero. A customer left with no pair is a **sure
  thing** (`left_out[customer] == "sure_thing"`) and gets no offer, whatever budget is left. A
  customer whose offers pay but none reaches `min_ratio` is `below_min_ratio`. A customer of the
  `audience` with no candidate at all is `no_candidate`;
* gives each customer one offer, climbing a ladder: the cheapest offer that pays, and a dearer one
  only as an upgrade whose extra profit per extra cost is worth the budget. (The ladder is the upper
  convex hull of cost against extra profit, so a dearer offer that earns less is never chosen);
* sorts all the steps by extra profit per unit of cost and fills the budget from the top. A step that
  does not fit is skipped, and so is every step above it for the same customer. The plan **never
  spends more than the budget**;
* with an `envelope`, drops offers the envelope does not list or that need approval, and caps the
  budget at the envelope's. The plan stays inside what the marketer approved.

On 40 random instances of 5 customers with up to 3 offers each, the plan's extra profit is never
above the best plan found by trying every combination, and never below it by more than one pair's
extra profit (`test_planner.py::test_the_greedy_plan_is_within_one_pair_of_the_best_plan_on_small_cases`).

The Anita example of the plan is a test: free shipping nets 100 for a cost of 40, 20% off nets 80 for
180, so she gets free shipping. When 20% off nets slightly more (130), a budget of 100 still buys
her free shipping first, while a budget of 1000 buys the upgrade.

`OfferPlan.price` is the **budget price**: the ratio of the best pair left out for lack of budget, and
0 when everything that pays fits.

## The budget price in the daily loop

A customer who takes the offer with the most `extra profit - price x cost`, and none when that is not
above zero, makes the same choice the plan made for them (`planner.choose_at_price`). So instead of
planning every customer each day, the loop publishes one number.

* `GoalBidder(budget_price=callable)` charges `offer cost x (1 + price)` instead of `offer cost` in
  `expected_net`. `Bid.offer_cost` still shows the real cost. Plain messages are not affected. With
  the price on, `expected_net` is net of the shadow price; that is what the auction and the
  offer-versus-plain gate should compare.
* `BudgetPrice(budget, days, initial=..., channel_plan_per_day=...)` is the price. Each day end,
  `update(offer_spend, other_spend=channel_spend)` multiplies the price by `exp(0.5 x miss)`, where
  `miss = offer_spend / target - 1` (capped at 3). The target is what is left for offers divided by
  the days left. What is left for offers is the budget, less all spend so far, less the channel spend
  set aside for the days left (the average so far, or `channel_plan_per_day` before any day has run).
  So the price aims the offers at the budget net of the messages, not at the whole budget. A day that
  overspends raises the price, and one that underspends lowers it. A miss is made up over the days
  that remain. The price reads 0 when it falls to its floor (the budget is not binding), and reads
  a ceiling of 1000 once the budget is spent, which stops every offer.
* The object is callable, so `GoalBidder(budget_price=price)` works as is.

### In the agent

`build_agent_policy(..., offer_pricing=OfferPricing(days=30, initial=0.5, default_basket=...))` turns
it on (default None: off, and the bidders are built exactly as before,
`test_safety_and_wiring.py::test_off_the_agent_builds_bidders_without_the_hooks`). Then:

* each goal gets a `BudgetPrice` over its envelope's budget. A new brief version keeps the goal's
  price and its spend so far, and takes the new budget;
* each goal's bidder is built with `budget_price=` that price, and with `default_basket` set,
  `offer_cost_fn=basket_cost_fn(default_basket)`;
* `nightly` counts each new agent send's channel cost against its goal and remembers which goal sent
  which offer to whom. It charges each purchase that used one of those offers to that goal: the cost
  the shop reported (`attrs["offer_cost"]`), else the offer's `cost_per_redemption`. That is the same
  rule as the service's ledger. Once per calendar day (UTC), after the first decision, it closes the
  day for every goal's price with that day's offer and channel spend
  (`test_safety_and_wiring.py::test_on_the_agent_charges_a_price_and_moves_it_each_day`).

**After the plan's days (`OfferPricing.renew`, off by default).** A `BudgetPrice` is exhausted once its `days` have
run, and an exhausted price reads the ceiling (1000), which stops every offer. A goal with no end date outlives its
first 30 days, so offers would have stopped for good on day 31 with budget left. With `renew=True` the agent plans
another `days` for what is left when the plan runs out and the budget has not (`BudgetPrice.extend`). The v3
preset turns it on, and starts the price at 0 (`autonomy.runtime.v3.DEFAULT_OFFER_PRICING`; see
[`PRESET.md`](PRESET.md)).

### What was measured

All numbers below are from a scratch script that reuses the test helpers. It was run on this branch
after the fixes.

**The price alone** (`test_budget_price.py::test_the_price_converges_to_the_plans_and_spend_keeps_to_budget`;
synthetic: 300 fresh customers a day for 30 days, two offers each, a quarter of them sure things, a
daily budget of 2,000, the price started at 0.1):

| Quantity | Result |
|---|---|
| Planner's price on a day's population, shown everyone | 1.981 (spend 1,999.85 of 2,000) |
| Price used by the per-customer loop, days 8 to 30 | 1.94 to 2.234 |
| Spend on day 1 (price started too low) | 4,426.6 |
| Total spend over 30 days | 100.36% of the budget (60,000) |

**The bidder with the price** (`test_safety_and_wiring.py::test_the_bidder_and_the_budget_price_keep_offer_spend_to_the_budget`;
`GoalBidder` on the sample brief, 10 customers a day for 10 days whose past revenue runs from 40 to
400, each taking its best bid, the price starting at 1.0):

| Budget | Offer spend over 10 days | Price, day 1 to day 10 | Offers sent, day 1 to day 10 |
|---|---|---|---|
| 80 | 79.2 | 1.0 to 4.506 | 7 to 2 |
| 1,000,000 (not binding) | 154.8 | 1.0 to 0.011 | 7 to 9 |

These are toy populations. They show that the mechanism holds to a budget. They are not evidence of a
lift in a real campaign.

## What was run, and what was not

Run on this branch after the fixes: `tests/v3/offers/` (all files), `tests/v2/test_contracts.py`,
`tests/v2/decide/test_bidders.py`, `tests/v2/decide/test_policy.py`,
`tests/v2/mandate/test_catalogue_api.py`, `tests/v2/mandate/test_mandate.py`,
`tests/v2/agents/test_creative_critic.py`, `tests/v2/agents/test_strategist.py` and
`tests/v2/gym/test_world.py`, all with `-m "not slow"`. `ruff`, `black` and `mypy --strict` were run
on the changed files. All green.

**Not run (CI's job):** the Gym runs (including `discount_cannibalisation`, the plan's pass test),
the real bench, the sealed Starbucks promo and the agent's own test suites. No Gym number is claimed
here.

## Known gaps

* `brand.kit.DISCOUNT_KINDS` (the shop's discount *forms*, read from its copy) does not list
  `bundle`, so the brand kit never skips a bundle for being in a form the shop does not use.
* The Gym's `PlantedTruth.offer_cost` derives a cost only for percent and amount off. A Gym offer of
  a new kind must carry a cost; one built with the new fields gets it filled in automatically.
* The critic has no claim check for early access beyond the default (no figures at all).
