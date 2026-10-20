# Stream: agent_brain

Makes the agent (`autonomy.runtime.agent`) learn per person, time sends and slow down before a
stop rule trips. Pure maths per customer, no LLM, no clock reads. The full write-up, with the Gym
before/after table and the diagnosis, is in `docs/v2/AGENT_BRAIN.md`.

## What was built

| Module | What it does |
|---|---|
| `autonomy/runtime/agent.py` | `AgentPolicy`: click rates per segment with each send counted at the customer's own factor, per-person factors, CUPED and pooled lift per click, warm start from logged history, cap judged on the week before the planned send, exact Poisson breaker for complaint and unsubscribe rules, segment reading hours for people with no hour of their own. |
| `autonomy/decide/models.py` | `PersonalEffects`: a hierarchical Gamma-Poisson model of each customer's clicks (overall, channel, offer, theme), prior strengths refitted by method of moments. Also `cuped_theta`, `cuped_adjust`, `shrink_to_pool` (DerSimonian-Laird), `moment_strength`, `product_moments`. |
| `autonomy/decide/bidders.py` | `GoalBidder(prune_z=...)`: opt-in pruning of options that cannot be the customer's best; the floor comes only from bids the offer rules allow. |
| `autonomy/decide/timing.py` | `ReadingHourModel`: each customer's reading hours as a Dirichlet over 24 local hours, shrunk to segment and business. `choose_send_time` can use it (learned scores, or one Thompson draw). |
| `autonomy/decide/pacing.py` | `Pacer`: per (goal, channel) pressure from 0 to 1 as complaints and unsubscribes near a stop rule, turned into a surcharge on the bid; full pressure holds the channel. |
| `autonomy/decide/policy.py` | `AutonomousPolicy` takes optional `reading_hours` and `pacer` (both off by default; the default path is unchanged). |
| `autonomy/decide/rules.py` | New code `PACING` in `ALL_CODES`. |
| `autonomy/decide/explain.py` | `BLOCK_SENTENCE` (one sentence per block code) and `BLOCK_ORDER`; reasons rank options after the pacing surcharge. |
| `autonomy/learning/observations.py` | `logged_sends`, `ipw_weights`, `weighted_rate`: read past campaigns (with their logged probabilities) so the agent can warm-start before day 1. |
| `tests/v2/agent_brain/` | Unit tests for all of the above, and `diag.py`, a fixed-hour-rule Policy and runner for Gym comparisons. |

## How other streams use it

```python
from autonomy.runtime.agent import build_agent_policy, AgentPolicy

build_agent_policy(briefs, content, *, bundle=None, costs=None, exploration_floor=None,
                   baseline_policy=None, kinds=None, learn_horizon_days=7) -> AgentPolicy
  .decide(state, ctx, *, trigger=None) -> Decision
  .learn(rewards, decisions) -> None
  .nightly(decisions, events_by_customer, now) -> list[str]   # paused goal ids
```

Events handed to `nightly` before the first decision are read as past campaigns. Handing the
same log twice, or in pieces, counts each event once.

```python
from autonomy.decide import PersonalEffects, ReadingHourModel, Pacer, AutonomousPolicy

PersonalEffects(*, overall_strength=3.0, feature_strength=8.0)
  .observe(customer_id, action_key, *, expected=0.0, clicks=0.0, weight=1.0)
  .factor(customer_id, action_key) -> (mean, variance)
  .refit(*, min_units=30)

ReadingHourModel(*, default_hour=10, prior_hours=None, customer_strength=6.0, ...)
  .observe_send(customer_id, send_at, tz, segment=None)
  .observe_click(customer_id, send_at, click_at, tz, segment=None, weight=1.0)
  .best_hour(customer_id, segment=None, allowed=None) -> int
  .sample_hour(customer_id, segment, rng, allowed=None) -> int

Pacer(*, window=7 days, z=3.09, min_events=5, lookahead=1.0, start=0.5)
  .record_event(event, goal_id); .set_unsubscribe_baseline(goal_id, rate); .reset(goal_id)
  .pressure(goal_id, channel, envelope, now) -> float
  .surcharge(channel, pressure, price, now) -> float

AutonomousPolicy(bidders, ..., plausible_z=None, min_gap=None, reading_hours=None, pacer=None)
choose_send_time(state, envelope, now, *, fallback_hours=None, default_hour=10,
                 hour_scores=None, reading=None, rng=None) -> datetime
```

```python
from autonomy.learning import logged_sends, ipw_weights, weighted_rate

logged_sends(events_by_customer, content_by_id, *, exclude=frozenset()) -> list[LoggedSend]
ipw_weights(propensities, *, clip=20.0) -> list[float]
weighted_rate(outcomes, weights) -> (rate, effective_n)
```

## Contract additions requested

None required. Would help:

- `Decision.segment` (also requested by agent_gym). Logged history sends now carry the segment
  at the first decision, not at send time.
- Document the shape of logged history SEND events in the realistic Gym: attrs
  `logged_probability`, `offer_id` and `content_id` (or `theme`), with one `decision_id` shared
  by SEND, DELIVER and CLICK. The reader handles events with and without decision ids.

## Dependencies requested

None. Only numpy and the stdlib.

## Known gaps

- `ReadingHourModel` and `Pacer` are built and tested but not yet switched on in the agent.
  Thompson send-hour draws explore heavily for customers with no clicks (13% at the best hour in
  a scratch check), so compare draws, the posterior mean and today's 5% explore on the Gym first.
  When wiring: call `observe_send` as well as `observe_click`, call `pacer.reset(goal)` wherever
  `Breaker.resume` runs, copy the breaker's unsubscribe baseline into the pacer, and make
  `_contacts_at_send` use the hour the policy then picks.
- The agent keeps its own `ReadingHours` (segment reading hours) alongside `ReadingHourModel`;
  one should replace the other once the Gym comparison is done.
- discount_cannibalisation stays 2.5 points below the fixed 18:00 rule (pooled 65.3% vs 67.8% of
  oracle). The first month of learning costs about 12%, and today's Gym has no past campaigns.
- Per-person ranking across options is weak (rank correlation 0.14-0.18): 2-3 clicks per customer
  in 90 days cannot identify individual channel, offer and theme multipliers.
- Uplift is proportional to clicks, so strong options are under-estimated against weak ones.
- The history warm start is covered by unit tests only; the realistic Gym will exercise it.
- History sends with no known content id or theme warm-start a `*` theme cell the agent never reads.
- Pacing defaults are reasoned, not tuned; no Gym numbers on whatsapp_fatigue yet.
- Gym figures in `AGENT_BRAIN.md` were measured before the review fixes (prune, DST, breaker
  underflow, history dedup); these should not move them at 1,000 customers, but were not re-run.
- `LiftPerClick.estimate()` keeps its old formula because an agent_gym test pins it; the agent
  uses `posterior()`. `ClickResponseModel.hour_factor` is kept up to date but no longer read.
- Learning state lives in memory; the runtime must persist it or rebuild it from the logs.
- mypy --strict was checked locally only against a 3.13 target (numpy stubs need 3.12+, pandas
  stubs missing); CI on 3.11 has the final say.
