# Discount code to offer (foundation fix 5, review finding D1)

This is a fix to the plain (v2) agent and the production path, not a v3 feature. v3 stays off.

## The bug

The agent names its offers with its own ids (`pct10`). Shopify reports the code the customer typed
(`COMEBACK10`), and the connector puts it in the purchase's `attrs["offer_id"]`
(`autonomy/connectors/shopify/mapping.py`). Three readers matched that value to the agent's id by plain
equality, so on a real shop no redemption ever matched:

- the `SpendLedger` (`autonomy/runtime/service.py`), which is the v2 money rule behind the spend-pace breaker and
  the budget left;
- the agent's own offer-spend tracking (`AgentPolicy._price_redemption`), which feeds the v3 budget price;
- the proof's `build_rewards` (`autonomy/proof/rewards.py`), which charges the offer's cost to a decision's reward.

Offer money was therefore never charged, and the budget did not limit discounts. The Gym hid it because it writes
the agent's own id onto its purchases (`autonomy/gym/world.py`).

## The fix

- `OfferOption` gains two additive fields: `codes` (exact discount codes) and `code_prefixes` (for per-send unique
  codes such as `WB10-A7K2`). Both default to empty and are left out of every dump when empty, so an offer that
  does not use them serialises and hashes exactly as before. A code or prefix that is empty, contains a comma (the
  connector comma-joins an order's codes, so it could never match) or is `none` (it means "no offer") is refused.
- An `Envelope` refuses codes that could be read two ways: one code (any case) or one prefix on two offers, or a
  code that is another offer's id. Before, the table silently took whichever offer came first. A short prefix is
  still allowed and still takes every shop code that starts with it (`W` takes `WELCOME15`); exact codes are read
  first. Choose prefixes as long as the shop's code scheme allows.
- `autonomy/contracts/offer_codes.py` holds the one mapping. `OfferCodeTable(offers).offer_for(token)` resolves a
  token in this order: the agent's own offer id (the Gym, and any connector that already reports it), an exact
  code, then the longest matching prefix. Codes and prefixes ignore case. `redemption_tokens(attrs)` reads a
  purchase's `offer_id` and every code of `discount_codes`, so an order with several codes is not read by its first
  code alone.
- The ledger, the agent and the proof (`Costs.from_brief`, and the API's own proof costs in `api/v2/deps.py`) all
  go through that table. The ledger and the agent both read a code with the table of the goal that sent the offer:
  when two goals both send `pct10` but name different codes, a code pays only the goal whose offer names it. The
  agent remembers its offer sends by (customer, goal, offer id) and charges the latest matching send (last touch, as
  the ledger does). A late send is still matched by the ledger.
- A code that no offer of any brief names is an **unattributed discount**: `SpendLedger.unattributed()` and
  `AgentPolicy.unattributed_discounts()` list each code with its order count and the discount money the shop
  reported. Every code of every order is checked, also on an order that used a known code too (COMEBACK10 plus
  FRIEND20 charges pct10 and lists FRIEND20). Shopify reports one discount total per order, so an order with several
  codes gives each code an equal share of that total (12.00 off over two codes lists 6.00 for each); the split is
  an approximation, the order count is exact. A known code with no matching send in the window is not
  "unattributed"; it is simply not charged (nobody offered it), as before.
- How long the lists cover: the ledger keeps every discounted purchase it has folded (by event id) and recomputes
  its list from them, so its list covers the whole log since the last rebuild, and a brief that names a code later
  (`refresh()`) takes the code off the list. The agent's list covers the purchases it still remembers
  (`learn_horizon_days` plus its usual margin): `_prune` forgets older orders, so per-send unique codes do not grow
  it without bound.
- Where a person reads the list: the runtime's nightly step logs a warning (`autonomy.runtime.service`, logger
  level WARNING) naming up to ten unattributed codes with their order count and money, e.g. `FRIEND20 (1 orders,
  12.00 off)`. Nothing is added to the briefing or the UI yet.

## Where the codes come from (configuration)

Setting the codes is an explicit configuration step: nothing guesses a code from an offer's label or from the
shop's history. Two real paths set them:

- Onboarding: `POST /v2/onboarding/accept` takes `offer_codes` and `offer_code_prefixes` next to `offers: true`.
  They go on the one offer onboarding builds (`autonomy.onboarding.offer_for`, `pct{N}`), which the signed mandate
  and the running plan's brief carry. A code with a comma is refused with 422.
- Any other offer: `codes` / `code_prefixes` are ordinary `OfferOption` fields. The Strategist copies the
  `OfferOption` objects it is given into the brief (`autonomy/agents/strategist.py`), and the mandate catalogue
  stores them whole, so an offer that names its codes keeps them; but no other production input sets them today.

## What moves

- A shop whose offers carry no codes (every shop today, until the owner names them): the money charged is the same
  as before. What is new is the record: its typed codes are listed as unattributed and logged nightly.
- A shop that names codes on an offer: each redemption now charges that offer's `cost_per_redemption` (or the
  shop-reported `offer_cost`) to the goal that sent it, where before it charged 0. Its budget and spend-pace breaker
  see offer money and can trip earlier; that is the intended effect.
- The agent's offer-spend tracking (offer pricing on) changes in one more case: when two goals sent the same offer
  id to one customer, a purchase used to be charged to whichever goal sent last; it is now charged to the latest
  send whose goal's offer names the code (or whose id the purchase carries). With the agent's own id on the
  purchase, as in the Gym, the result is the same goal as before.

## Evidence

- Tests through the real mapping (`map_orders` on a Shopify order) in
  `tests/v2/connectors/test_shopify_offer_codes.py`: a purchase with `COMEBACK10` charges the `pct10` cost to its
  goal in the ledger, in the agent's budget price and in the proof; a prefix code matches; a late send matches; an
  unknown code is counted as unattributed with the shop's discount money (the fixture now sets a non-zero
  `totalDiscountsSet`); an order with a known and an unknown code charges the known one and lists the unknown one;
  an order with two unknown codes lists both with equal shares; two goals sharing `pct10` with different codes
  charge only the goal whose code it is (ledger and agent); `refresh()` reads codes a new brief version names; the
  agent forgets unattributed orders past its memory; ambiguous codes are refused; the nightly warning is logged;
  `POST /v2/onboarding/accept` with `offer_codes` puts the code on the plan's offer (marked slow: 4.78 s in the
  local `pytest --durations` run).
- Numbers unchanged for the Gym's path: `test_a_gym_purchase_charges_the_same_with_or_without_codes` runs a
  Gym-style purchase (the agent's own id in `offer_id`, no `discount_codes`) through the `SpendLedger`, the agent
  with offer pricing on, and `build_rewards` with `Costs(...)` and with `Costs.from_brief`, once with offers that
  name codes and once without, and asserts the same charges (6.0 in each). That is the real check: the Gym smoke
  run never reaches the changed paths (it builds `Costs` directly in `autonomy/runtime/demo.py`, runs with offer
  pricing off, and tracks spend with `SpendTrackingEventStore`, not the `SpendLedger`), so the byte-identical Gym
  smoke report from the first commit (`python -m autonomy.gym.run --scenario discount_cannibalisation --policy agent
  --seeds 1 --customers 200 --report-json ...`, base 29382881 against the branch, equal except `seconds`) is
  expected and shows little about this change.
- The full Gym grid (including `agent_v3`, which turns offer pricing on) and the real bench are not rerun here:
  pending CI, with the same commands as the existing jobs.

## Not done

- The unattributed list is only logged; nothing reads it into the weekly briefing or the UI yet.
- No UI screen asks the owner for the code of each offer; the onboarding API fields above are the only input.
- Creating per-send unique codes in Shopify (the other option in the review) is not built; prefixes support them
  once something creates them.
- `docs/v2/openapi.json` is not regenerated here: `python -m api.v2.app --openapi` also picks up other streams'
  unrelated drift, so the integrator should regenerate it once after merging (the API test compares operations only,
  which did not change).
