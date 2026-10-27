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
  does not use them serialises and hashes exactly as before.
- `autonomy/contracts/offer_codes.py` holds the one mapping. `OfferCodeTable(offers).offer_for(token)` resolves a
  token in this order: the agent's own offer id (the Gym, and any connector that already reports it), an exact
  code, then the longest matching prefix. Codes and prefixes ignore case. `redemption_tokens(attrs)` reads a
  purchase's `offer_id` and every code of `discount_codes`, so an order with several codes is not read by its first
  code alone.
- The ledger, the agent and `Costs.from_brief` (the proof) all go through that table. The ledger checks a send
  against the table of the send's own goal; a late send is still matched.
- A code that no offer of any brief names is an **unattributed discount**: `SpendLedger.unattributed()` and
  `AgentPolicy.unattributed_discounts()` list each code with its order count and the discount money the shop
  reported. Nothing is dropped silently. A known code with no matching send in the window is not "unattributed";
  it is simply not charged (nobody offered it), as before.

## What moves

Nothing changes for a shop whose offers carry no codes: behaviour is the same, and the codes it uses now show up as
unattributed. For a shop that sets `codes` or `code_prefixes` on an offer, each redemption now charges that offer's
`cost_per_redemption` (or the shop-reported `offer_cost`) to the goal that sent it, where before it charged 0. Its
budget and spend-pace breaker will see offer money and can trip earlier; that is the intended effect. Setting the
codes is a configuration step: nothing guesses a code from an offer's label.

## Evidence

- New tests, through the real mapping (`map_orders` on a Shopify order), in
  `tests/v2/connectors/test_shopify_offer_codes.py`: a purchase with `COMEBACK10` charges the `pct10` cost to its
  goal in the ledger, in the agent's budget price and in the proof; a prefix code matches; a late send matches; an
  unknown code is counted as unattributed; the agent's own id still works.
- Gym unchanged: `python -m autonomy.gym.run --scenario discount_cannibalisation --policy agent --seeds 1
  --customers 200 --report-json ...` on the base commit (29382881) and on this branch gives byte-identical JSON
  except for the `seconds` timing field (diff of the two reports).
- The full Gym grid and the real bench are not rerun here: pending CI, with the same commands as the existing
  jobs. The change does not touch any code path they use unless a purchase carries a code the offers name.

## Not done

- Nothing yet reads the unattributed list into the weekly briefing or the UI.
- No UI or onboarding step asks the owner for the code of each offer; the field can be set through the brief.
- Creating per-send unique codes in Shopify (the other option in the review) is not built; prefixes support them
  once something creates them.
