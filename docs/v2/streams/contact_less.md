# Stream contact_less: contact less, and only where it pays

The full-size Gym showed the agent sending about 1.8 messages a customer a week and losing to the
best fixed rule wherever the right move was to send less. This stream makes the cost of a message
real (learned per channel and per weekly count), gives each customer a weekly budget, keeps
uncertain options out of the draw, stops paying discounts to people who would buy anyway, and
starts to charge for contact that puts some customers off. No LLM in the per-customer loop; time
and seeds are passed in. The full write-up, with Gym numbers and ablations, is
`docs/v2/CONTACT_LESS.md`.

## What was built

| Module | What changed |
|---|---|
| `autonomy/decide/models.py` | `HazardModel`: P(unsubscribe or complaint \| channel, messages in the last 7 days), four-level empirical Bayes from a conservative 0.5% prior, learned from every send as its outcome arrives. Deterministic across processes (cells summed in a fixed order). |
| `autonomy/decide/policy.py` | `AutonomousPolicy` hooks `channel_price_fn` (a price per channel), `budget_fn` (holds a customer with `weekly_cap` once their weekly budget is spent) and `margin_fn` (an uncertain option must beat doing nothing by a margin to win the draw). `Market` is one customer's priced options. `thompson_with_floor(..., eligible=)` keeps the exploration floor and exact logged probabilities, and never writes into the caller's mask. |
| `autonomy/decide/pacing.py` | `Pacer(step_aside=True)` lets a goal clearly over its stop limit reach the breaker. The pacer stays off by default in the agent. |
| `autonomy/decide/bidders.py` | Offers are priced as cost x (organic buying + uplift), believed only as far as the evidence beats the plain version (`shrink_offer_extra`, `offer_sensitivity_factor`), and, with pruning on, not bid unless expected to beat the plain version. Offers the hard rules block anyway are not gated, so the rule is reported. `ContactHarm` charges the dent contact makes in some customers' own buying. `quote(..., prune=False)` lists what pruning leaves out. |
| `autonomy/learning/observations.py` | `ContactEvidence` / `contact_evidence(_all)`: per customer, days inside and outside a message's window and purchases in each, clicks, opt-outs and direct visits, recency weighted. |
| `autonomy/learning/update.py` | `learn_contact_harm` refits `ContactHarm` each night; `update_models(..., learn_harm=True)` calls it. |
| `autonomy/runtime/agent.py` | `ContactPrice`: hazard x the customer's lost future value per channel, plus complaint cost; weekly rhythms (`Rhythm`, `rhythms`, `best_rhythm`: one a week, a burst of two, two spread out). Restraint (`RESTRAINT_Z`) once lift is judged weak. The breaker judges past campaigns before day 1. Reading-hour model on. `explain_bids` lists pruned options with code `PRUNED`. |
| `autonomy/decide/__init__.py`, `autonomy/learning/__init__.py` | Re-export the new public names. |

## How other streams use it

```python
from autonomy.runtime.agent import build_agent_policy, ContactPrice, PRUNED

build_agent_policy(briefs, content, *, ...,             # wire_agent keywords unchanged
                   pacing=False, reading_hour_model=True,
                   restraint=True, weekly_budget=True) -> AgentPolicy
AgentPolicy.contact_price: ContactPrice
ContactPrice.prices(state, bids) -> dict[Channel, float]    # attention price per channel
ContactPrice.rhythm(state, bids) -> Rhythm                  # the customer's weekly rhythm
ContactPrice.budget(state, bids, cost=None) -> int          # the policy's budget_fn hook
AgentPolicy.explain_bids(state, ctx, *, trigger=None)       # pruned options: blocked_by == PRUNED

from autonomy.decide import HazardModel, ContactHarm, contact_harm_of, shrink_offer_extra, Market
HazardModel(*, base_rate=0.005, growth=0.5, max_contacts=3, ...)
  .observe_send(channel, contacts_7d, weight=1.0); .observe_event(channel, contacts_7d, weight=1.0)
  .rate(channel, contacts_7d) -> float
AutonomousPolicy(bidders, ..., channel_price_fn=None, budget_fn=None, margin_fn=None)
  # ChannelPrices    = (state, bids) -> Mapping[Channel, float]
  # ContactBudget    = (state, bids, price_of_bid) -> int | None
  # RestraintMargin  = (state, bid) -> float
GoalBidder.quote(state, ctx, *, prune=True) -> list[Bid]
contact_harm_of(response_model, *, create=False) -> ContactHarm | None
ContactHarm.dent(customer_id) -> float; .expected_loss(customer_id, organic) -> float
shrink_offer_extra(extra, extra_sd, reference) -> tuple[float, float]

from autonomy.learning import contact_evidence, contact_evidence_all, learn_contact_harm
learn_contact_harm(bundle, events_by_customer, content_by_id, now, horizon_days) -> ContactHarm | None
```

## Contract additions requested

- `CustomerState` counts of direct visits and of message-attributed purchases, so the harm clues
  are visible without the event log.
- A holdout lookup handed to the learner (`update_models(..., holdout_customers=...)` from
  `AgentPolicy`), so the harm model can compare contacted customers with holdout customers that
  look the same, instead of a comparison confounded by targeting.
- A way to pass the trigger to the budget hook, so an urgent one-off trigger (a cart reminder) is
  not held by a weekly rhythm built for sweeps.

## Dependencies requested

None.

## Known gaps

- `null_effect` still misses its Stage 2 allowance locally (-$436 against -$396 over 2 seeds).
- Stage 2 is not reached on `discount_cannibalisation`, `sleeping_dogs_15` or
  `sleeping_dogs_hidden` at 1,000 x 2; sleeping dogs are still contacted at send-to-all's rate.
- The Pacer is off by default (it never helped in the Gym and kept the breaker from tripping in
  `complaint_storm`); `pacing=True` turns it on with `step_aside`.
- The hazard depends on channel and weekly count only, not the customer; only lost value is
  per customer. Lift per click is not split by channel. Bursts assume one decision a day.
- Triggered decisions can be held by the weekly budget (no triggers in the Gym, so unmeasured).
- Hazard models, rhythms, restraint flags and `ContactHarm` live in memory (`ContactHarm` in a
  weak map keyed by the response model, not in `ModelBundle`).
- `mypy --strict` was run with a 3.13 target locally; CI's 3.11 run has the final say.
- All numbers are 1,000 customers x 2 seeds; the full-size CI Gym after merge is the record.
