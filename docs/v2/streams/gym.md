# Stream WF2: the Gym (`autonomy/gym/`)

The Gym is a simulated shop with a planted truth. It runs any `Policy` through the same
`Environment` seam as the real world, then scores it against the holdout, two baselines and an
oracle that can see the truth. It is how we prove `decide()` works before it touches a real customer.

## What was built

| Module | One line |
| --- | --- |
| `api.py` | The shared surface (Wave 0): `ScenarioSpec` (every knob of a business), the `GroundTruth` and `GymEnvironment` protocols, `PERSONAS`. |
| `truth.py` | `PlantedTruth`: each customer's hidden persona, sleeping-dog flag, consents, purchase rate, channel/offer/theme response, best hour, fatigue and drift; computes `expected_net` and `best_action`. |
| `world.py` | `GymWorld`: implements both `Environment` and `GymEnvironment`. Plants history before day 0, simulates hour by hour (numpy, seeded), acts on decisions, logs every contact and every rule break. |
| `scenarios.py` | The scenario library (10 named specs), the default brief, the default approved content, and the shared offers (`GYM_OFFERS`). |
| `baselines.py` | `ContactEveryone`, `RuleBaseline` and `OraclePolicy`, plus time and quiet-hour helpers. All respect consent, the weekly cap and quiet hours. |
| `score.py` | `run_policy`, `compare` and `pass_criteria`: the three-way split, realised profit with a 95% interval, true value, oracle share, rule checks and the plan's pass bar. |
| `run.py` | The CLI: `python -m autonomy.gym.run --scenario NAME --policy NAME --seeds N` (`--list` shows both). |

Scenarios: `baseline_shop`, `sleeping_dogs_15`, `discount_cannibalisation`, `whatsapp_fatigue`,
`send_time_matters`, `drift_midway`, `small_business`, `null_effect`, `complaint_storm` (planted harm,
for the breaker check) and `smoke` (tiny, for tests).

## How other streams use it

Everything below is exported from `autonomy.gym`.

```python
# Pick a business, the brief and the content library.
get_scenario(name: str, **overrides: object) -> ScenarioSpec
default_brief(goal_id: str = GYM_GOAL_ID) -> Brief
default_content(goal_id: str = GYM_GOAL_ID) -> list[ContentItem]

# The world (Environment + GymEnvironment). Read .truth / .planted after reset(seed).
GymWorld(spec, *, start=DEFAULT_START, offers=GYM_OFFERS, content=(),
         quiet_start_hour=21, quiet_end_hour=9, contact_cap=2)
  .reset(seed) -> Sequence[Event]          # history before day 0
  .act(decisions) / .advance(until) -> Sequence[Event] / .now()
  .violations() -> list[str]; .contacts; .over_cap_contacts(); .sleeping_dogs_contacted_share()
gym_world(spec, briefs, content) -> SimWorld   # a GymWorld set up from a brief's envelope

# The truth (GroundTruth). Batch extras used by the oracle: index(id), net(idx, action, now),
# expected_net_all(action, now).
PlantedTruth.expected_net(customer_id, action, now) -> float
PlantedTruth.best_action(customer_id, candidates, now) -> Action | None

# Scoring. `policy` is a Policy or a factory `(world) -> Policy` (prefer factories).
run_policy(spec, policy, seed=0, briefs=None, content=None, *, policy_name=None,
           world_factory=None, folder=None, learn=True, ...) -> ScoreCard
compare(spec, policies: Mapping[str, PolicyLike], seeds, briefs=None, content=None, ...) -> Comparison
pass_criteria(scorecards, *, policy=None, ...) -> PassReport   # .passed, .lines()
```

To put a new policy on the CLI, call `autonomy.gym.run.register_policy(name, factory)`. WF7 should
register `decide()` there and judge it with `compare(...)` then `pass_criteria(...)`.

A policy may define `prepare_day(states, ctx)`; the scoreboard calls it once a day before the
per-customer `decide` calls, so a policy can plan the whole day in one vectorised pass.

## Contract additions requested

- `GroundTruth`: an optional batch method, e.g. `expected_net_many(customer_ids, action, now)`, or
  formalise `PlantedTruth.index()` + `net()`. Today the oracle finds it by duck typing (`BatchTruth`
  in `baselines.py`); without it a 5,000 x 90-day oracle run takes minutes.
- `Policy`: an optional `prepare_day(states, ctx)` hook. Today it is Gym-only and called via `getattr`.
- `ScenarioSpec`: a harm onset day (e.g. `harm_day` plus a complaint multiplier), so harm can start
  mid-run. Today `complaint_storm` plants harm from day 0 and the breaker count starts from
  `drift_day` or day 0.

## Dependencies requested

None. numpy, pandas and pydantic only; nothing from v1.

## Known gaps

- mypy `--strict` for Python 3.11 is confirmed only in CI. Locally, mypy `--strict --python-version
  3.13` on `autonomy/gym` is clean apart from missing `pandas-stubs` (CI installs them, so pandas
  typing in `score.py` is untested locally).
- Speed: `SimpleStateFolder` and building pydantic `Event`s are the main cost. A full 5,000 x 90-day
  run takes about 7 s (do nothing) to 36 s (send to everyone). A faster WF1 folder speeds every run.
- The truth ignores two second-order effects on purpose: overlap with an offer already active, and
  future value lost when a contact causes an unsubscribe. So the oracle keeps sending in
  `complaint_storm`.
- `score.py` sums `expected_net` for a whole day's decisions before acting, so a second same-day
  contact to one customer is valued without the first's fatigue.
- Failed sends (no consent, unsubscribed) still count toward fatigue; they are already violations.
- Interval coverage of 93-97% needs hundreds of seeds to judge properly; with few seeds the Wilson
  overlap rule is lenient by design.
- The breaker check needs the agent's own breaker (WF3/WF5). The baselines never stop and so fail it,
  as expected.
- Nightly learning rewards are approximate: overlapping contacts can each claim the same purchase.
- A policy instance (not a factory) passed to `compare` keeps its state across seeds, including the
  oracle's best-hour cache. Pass factories.
- Offers must be known to the world (`offers=`); an unknown `offer_id` raises `KeyError`.
