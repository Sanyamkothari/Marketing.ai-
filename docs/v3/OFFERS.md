# v3 offers: the cheapest offer that changes behaviour

**Stream:** plan-v3 decision 3 ([`plan-v3.md`](../../plan-v3.md) section 4). **Branch:** `v3/offers`.
**Status:** built and unit-tested; opt-in, default off. The Gym and sealed-data bars of the plan are
**not run here** (CI runs them after integration), so this page makes no claim about them.

## What it does

1. **A wider menu of offers, each with a cost model** (`autonomy/mandate/catalogue.py: offer_cost`).
2. **A planner** (`autonomy/decide/planner.py`) that spends the offer budget on all customers at once,
   by extra profit per unit of offer cost.
3. **A budget price** (`autonomy/decide/pacing.py: BudgetPrice`), a Lagrange multiplier that pacing
   moves day by day, so per-customer decisions approximate the plan without seeing everyone.
4. **Two hooks in the bidder** (`GoalBidder(offer_cost_fn=..., budget_price=...)`), both `None` by default.

Nothing here runs unless a caller passes the hooks or builds a plan. With the hooks off, the bids are
the v2 bids (`tests/v3/offers/test_bidder_hook.py::test_off_the_bids_are_identical`).

## The cost of one redemption

An explicit `cost_per_redemption` on the offer always wins (every v2 envelope sets one). Otherwise:

| Kind | Cost | Fields it reads |
|---|---|---|
| `none`, `content` | 0 | |
| `percent_off` | `amount`% of the customer's usual basket (0 if unknown) | `amount`; basket = `CustomerState.avg_order_value` |
| `amount_off` | `amount` | `amount` |
| `free_shipping` | shipping cost per order | `shipping_cost` (new) |
| `bundle` (new kind) | the money taken off the bundle | `bundle_discount`, `bundle_item_ids` (new) |
| `loyalty` | `points x point_value x redemption_rate` | `points`, `point_value`, `redemption_rate` (new; rate defaults to 1, the cautious end) |
| `gift`, `early_access` (new kind) | the configured cost | `cost_per_redemption` |

`bundle_offer(...)` builds a bundle from the co-purchase recommender (`CoPurchaseModel.related`): the
anchor item plus its best-co-bought items that have a price, discounted by `percent_off` of their
list prices. Items without a price are skipped, and fewer than two priced items gives no bundle.

An offer is paid for by everyone who buys while it is open, not only by those it persuades. So the
expected spend of an offer for one customer is `cost of one redemption x p_redeem`, where `p_redeem`
is the organic chance of buying plus the uplift. The bidder already did this; the planner reuses it.

Additive changes only: two `OfferKind` values (`bundle`, `early_access`) and six optional
`OfferOption` fields with neutral defaults. `catalogue_costs` (the mandate's budget accounting) is
untouched and still reads only the configured cost.

## The plan

For each customer-offer pair:

    extra profit = (uplift of the offer - uplift of the best plain message) x value - offer cost x p_redeem
    cost         = offer cost x p_redeem

`planner.offer_economics` computes this; `candidates_from_bids` reads it from the bids that
`GoalBidder.quote` returns (an offer's net against the best plain bid, or against doing nothing).

`plan_offers(candidates, budget, envelope=...)` then:

* drops every pair whose extra profit is not above zero. A customer left with no pair is a **sure
  thing** (`left_out[customer] == "sure_thing"`) and gets no offer, whatever budget is left;
* gives each customer one offer, climbing a ladder: the cheapest offer that pays, and a dearer one
  only as an upgrade whose extra profit per extra cost is worth the budget. (The ladder is the upper
  convex hull of cost against extra profit, so a dearer offer that earns less is never chosen);
* sorts all the steps by extra profit per unit of cost and fills the budget from the top. A step that
  does not fit is skipped, and so is every step above it for the same customer. The plan **never
  spends more than the budget**;
* with an `envelope`, drops offers the envelope does not list or that need approval, and caps the
  budget at the envelope's. The plan stays inside what the marketer approved.

The Anita example of the plan is a test: free shipping nets 100 for a cost of 40, 20% off nets 80 for
180, so she gets free shipping; and when 20% off nets slightly more (130), a budget of 100 still buys
her free shipping first while a budget of 1000 buys the upgrade.

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
* `BudgetPrice(budget, days, initial=...)` is the price. Each day end, `update(spent_today)` multiplies
  the price by `exp(0.5 x miss)` where `miss = spent / target - 1` (capped at 3) and the target is
  what is left of the budget divided by the days left. A day that overspends raises the price; one
  that underspends lowers it; a miss is made up over the days that remain. The price reads 0 when it
  falls to its floor (the budget is not binding) and a ceiling of 1000 once the budget is spent, which
  stops every offer. A good `initial` is `plan_offers(sample).price` on a sample of the audience.
* The object is callable, so `GoalBidder(budget_price=price)` works as is, and the runtime calls
  `price.update(spent_today)` once a day.

### What was measured

`tests/v3/offers/test_budget_price.py::test_the_price_converges_to_the_plans_and_spend_keeps_to_budget`
(synthetic: 300 fresh customers a day for 30 days, two offers each, a quarter of them sure things,
a daily budget of 2,000, the price started at 0.1 against a full-information price of 1.98). From a
run of that setup with `pytest`'s helper functions:

| Quantity | Result |
|---|---|
| Planner's price on a day's population, shown everyone | 1.98 (spend 1,999.85 of 2,000) |
| Price used by the per-customer loop, days 8 to 30 | 1.94 to 2.23 |
| Total spend over 30 days | 100.4% of the budget (60,000) |

The loop overspent in the first days (4,427 on day 1) because the price started low, and the daily
target fell afterwards. The test asserts the total is within 1% of the budget, the late prices are
within 25% of the planner's, and the first days overspent. This is a toy population, not evidence of
a lift in a real campaign.

## What was run, and what was not

Run on this branch: the four new test files in `tests/v3/offers/` (48 tests), `tests/v2/decide/test_bidders.py`,
`tests/v2/test_contracts.py`, `tests/v2/mandate/`, `tests/v2/decide/test_policy.py`, `ruff`, `black`
and `mypy --strict` on the changed files. All green.

**Not run (CI's job):** the Gym (including `discount_cannibalisation`, the plan's pass test), the real
bench, and the sealed Starbucks promo. No Gym number is claimed here. Nothing yet calls the planner or
passes the hooks in the live agent; wiring is below.

## Wiring it in (for the integrator)

    price = BudgetPrice(goal.budget, days, initial=plan.price)   # plan from a sample of the audience
    GoalBidder(brief, ..., offer_cost_fn=basket_offer_cost, budget_price=price)
    # once a day, after the day's sends:  price.update(spent_today)

## Known gaps in files this stream does not own

* `autonomy/decide/rules.py: DISCOUNT_KINDS` does not list `bundle`, so a customer who asked for no
  discounts could still be offered a bundle. `bundle` should be added there.
* `autonomy/agents/creative.py: OFFER_THEMES[offer.kind]` and `autonomy/gym/world.py: _HISTORY_THEME`
  have no entry for `bundle` or `early_access`; `OFFER_THEMES` indexes the dict directly, so a brief
  whose envelope holds one of the new kinds would raise `KeyError` when the Creative agent writes for
  it. Add themes there before putting a new kind in a live envelope.
* `autonomy/agents/critic.py` checks percent and money claims only for `percent_off` and `amount_off`;
  the claim checks for the new kinds are not written.
