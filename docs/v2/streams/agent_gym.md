# Stream: Agent in the Gym (agent_gym)

Autonomy's own policy, its fast learning loop, and the wiring that runs it in the Gym. Pure maths
per customer, no LLM, no clock reads: time and seeds are passed in, so a run repeats exactly.

## What was built

### `autonomy/learning/` (the fast learning loop)

| Module | What it does |
|---|---|
| `bundle.py` | `ModelBundle(response, value, fatigue)`: the three models the bidders share, plus the bookkeeping that makes learning idempotent. `build_bundle(priors)` seeds it from a flat `Prior.values`-style dict; `prior_values(priors)` merges `Prior` objects into that dict. `LearnedValueModel` is a `ValueModel` that learns order value and margin in place. |
| `observations.py` | Pure builders that turn decisions and events into observations. Treated: a delivered ACT decision, window `[send, send + horizon)`. Control: holdout do-nothing only (agent do-nothing only with opt-in, clipped IPW). Sends that failed, bounced or were blocked are not learned. Overlapping daily control windows are down-weighted. Fatigue counts the sends in the 7 days before each send. |
| `update.py` | `update_models(...)`: closes every window that has ended by `now` and updates the bundle in place, once per decision. `learn_from_rewards(...)`: the same loop from what `Policy.learn` receives. Both return a `LearnReport`. |

### `autonomy/runtime/agent.py` (the agent)

`build_agent_policy(briefs, content)` returns an `AgentPolicy`: one `GoalBidder` per brief, all
sharing one `ModelBundle`, inside `AutonomousPolicy`. Around `decide()` it adds:

* `ClickResponseModel`: uplift = lift per click (agent group against holdout) x click rate per
  behaviour segment and action x the customer's own click record x a "messages this week" factor.
* `Segmenter` (new, lapsed, light, medium, heavy buyer), `OrganicRate` (the chance of buying anyway,
  which prices discounts), `ShrunkValueModel` (the customer's order value shrunk to the business's),
  `AttentionPrice` (learned unsubscribe chance x future message value), `ReadingHours` (send hour).
* `nightly(decisions, events_by_customer, now)`: learns from clicks and purchases, then calls
  `update_models`, labelling each outcome with the segment at decision time.
* `Breaker`: the brief's stop rules, checked nightly on a rolling week with a one-sided 99.9%
  bound. A paused goal stays paused until a new brief version arrives. Newer brief versions rebuild
  that goal's bidder.

### Changes to `autonomy/decide/`

* `policy.py`: scheduled-but-unsent sends count towards the weekly cap; `decide(state, ctx, *,
  trigger=None)` gives one decision id per (customer, moment, trigger); opt-in `plausible_z` (skip
  options that cannot beat doing nothing) and `min_gap` (hold a message while one is scheduled or
  just went out).
* `rules.py`: new hard rules `snoozed` (an unreadable `snooze_until` blocks), `offer_approved`
  (offers marked `needs_approval` never go out), `no_discounts` and `too_soon`.
* `bidders.py`: opt-in `GoalBidder(own_spread=True)`: sample each option on its own treated spread,
  not the control spread every option shares.

### Changes to `autonomy/gym/`

* `run.py`: the `agent` policy is registered (a fresh agent per seed, built from the Gym's default
  brief and content).
* `score.py`: `run_policy` calls an optional `nightly()` hook once with the history and then every
  simulated morning, and asks the agent to decide for holdout customers so their do-nothing outcomes
  can be learned from (nothing decided for them is acted on).

Results against the plan's bar are in `docs/v2/GYM_RESULTS.md`.

## How other streams use it

```python
from autonomy.runtime.agent import AgentPolicy, build_agent_policy
from autonomy.learning import ModelBundle, build_bundle, update_models, learn_from_rewards

build_agent_policy(briefs, content, *, bundle=None, costs=None, exploration_floor=None,
                   baseline_policy=None, kinds=None, learn_horizon_days=7) -> AgentPolicy
AgentPolicy
  .decide(state, ctx, *, trigger=None) -> Decision
  .learn(rewards, decisions) -> None                       # the Policy hook
  .nightly(decisions, events_by_customer, now) -> list[str] # breaker alerts tonight
  .prepare_day(states, ctx) -> None
  .alerts: list[str]

build_bundle(priors=None, *, strength=50, value_prior_orders=20) -> ModelBundle
update_models(bundle, decisions, events_by_customer, content_by_id, now, horizon_days, *,
              segment_of=None, holdout_customers=(), treated_groups={AGENT, BASELINE},
              agent_nothing_ipw=False, ipw_clip=20.0, overlap_weighting=True,
              require_send=True, learn_value=True) -> LearnReport
learn_from_rewards(bundle, rewards, decisions, content_by_id, *, horizon_days,
                   segment_of=None, contacts_7d_at=None, ...) -> LearnReport
```

`update_models` and `learn_from_rewards` share the bundle's record of learned decisions, so each
decision is learned once whichever runs first. Do not also call `AutonomousPolicy.learn` or
`GoalBidder.learn` on the same bundle: those feed the same response model without that record.

`autonomy/runtime/` belongs to the runtime stream and has no `__init__.py`, so import from
`autonomy.runtime.agent` directly. In the Gym: `python -m autonomy.gym.run --scenario baseline_shop
--policy agent --seeds 2 --customers 1000`.

## Contract additions requested

* `Decision.segment` (or the features at decision time): learners cannot label an outcome without
  it. Today the agent remembers decision id -> segment in memory; `update_models` takes
  `segment_of`, which gives today's segment.
* `Decision.contacts_7d` (or on `Reward`): fatigue learning from rewards needs it
  (`contacts_7d_at` is the workaround).
* `DecisionContext.trigger` (or `Decision.trigger`): ids unique per trigger. Today a keyword-only
  argument outside the `Policy` protocol.
* `Policy`: formal optional `nightly(decisions, events_by_customer, now)` and
  `prepare_day(states, ctx)` hooks (the scoreboard finds them with `getattr`).
* Gym `run_policy` / `Policy.learn`: give the policy holdout control outcomes (done here through
  `nightly`; through `learn` alone the control rate never moves off its prior).
* `ValueModel.update(observations)` or a non-frozen `ValueModel` (workaround: `LearnedValueModel`).
* `Envelope.min_gap_hours` (today the agent's `MIN_GAP` = 6 h).
* `OfferOption.valid_days` (today the agent assumes its 7-day learning horizon).
* `CustomerState.first_purchase_at` or a data-start date on the business (today inferred from the
  earliest purchase seen).
* Gym truth: charge the future value lost to an unsubscribe or complaint, so planted-harm scenarios
  can pass both the breaker check and the capture check.

## Dependencies requested

None. Only stdlib, numpy and pydantic.

## Known gaps

* **The pass bar is not met** (1,000 customers, 2 seeds): 32-56% of the oracle's value, not 80%;
  beats both baselines in 15 of 18 seeds; contacts 96% of sleeping dogs; loses $518 in
  `null_effect` against a $16 tolerance; `complaint_storm` fails capture and beat checks because
  the breaker (correctly) stops sending. The sleeping-dog and complaint_storm checks look ill-posed
  for a policy that cannot read the truth. The scoreboard was not re-run after review fixes.
* Treated observations are not propensity-weighted, and overlapping treated windows can each claim
  the same purchase.
* Learning bookkeeping sets (`learned_*`) are never pruned; each night re-scans the whole log
  (0.1-0.3 s for 2,000 customers).
* `AgentPolicy` keeps its learning state (segments, click records, reading hours, breaker) in
  memory only. The runtime has to persist it or rebuild it from the logs.
* Speed: one agent run (1,000 customers, 90 days) takes 50-60 s, so a 5,000-customer CI run is
  about 5 minutes per seed. Most time goes on pydantic `Bid`/`Action` construction.
* The spend-pace and WhatsApp-quality stop rules are not implemented; the unsubscribe baseline is
  the goal's own first two weeks. The agent does not pace itself to stay under the stop rules.
* `explain.py` has no plain sentences for the new codes (`snoozed`, `too_soon`, `no_discounts`,
  `offer_needs_approval`), so those decisions get the generic reason.
* mypy --strict ran locally only with `--python-version 3.13` (numpy's stubs do not parse for 3.11
  here); it was clean for this stream's files. CI on 3.11 has the final say.
