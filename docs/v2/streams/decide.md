# Stream WF3: Decide & Proof

`decide()` and the proof engine. Pure maths per customer, no LLM, no clock reads: time and seeds
are always passed in, so a run repeats exactly.

## What was built

### `autonomy/decide/` (the inner loop)

| Module | What it does |
|---|---|
| `audience.py` | `matches(state, audience, now)`: evaluates a brief's audience filters on `CustomerState`, including dotted paths (`scores.churn_risk`, `channels.email.consent`). Missing or `None` fields fail, so "never bought" is not "lapsed". |
| `models.py` | Small learnable models. `BetaResponseModel`: uplift = treated rate minus control rate, Beta posteriors per (segment, `channel\|offer\|theme`), injectable priors with wildcards, partial pooling across segments. `ValueModel`: what one conversion is worth. `FatigueModel`: the attention price, p(unsubscribe given contacts this week) x future value. |
| `bidders.py` | `GoalBidder`: one goal's bids, one per reachable channel x allowed offer x approved content theme, priced `urgency x (uplift x value - offer cost x p_redeem - channel cost)`. Cart recovery urgency halves each day. `learn()` feeds closed rewards back into the response model. |
| `rules.py` | The hard rules as small pure functions (holdout, channel and offer in the envelope, unsubscribed, consent, address, open complaint, weekly cap, budget, quiet hours). `check_bid` runs them in order; the first block's code goes in `Decision.blocked_by`. |
| `timing.py` | `choose_send_time`: the customer's most active hour in the next 24 hours, never in quiet hours in their own time zone. Handles DST and half-hour zones. |
| `explain.py` | `reason(...)`: one plain sentence per decision, built from the numbers. |
| `policy.py` | `AutonomousPolicy` (implements `Policy`): split first (holdout gets nothing, baseline goes to the baseline rule), then bids, timing, rules, attention price, and Thompson sampling with an exploration floor against "do nothing" (worth 0). Logs the probability of the chosen option. `learn()` routes rewards to each bidder. |

### `autonomy/proof/` (proving value)

| Module | What it does |
|---|---|
| `split.py` | Salted-hash three-way split (agent / baseline / holdout). Growing one share never moves the other group. |
| `rewards.py` | Decisions + events into closed-window `Reward`s, including "virtual" rewards for doing nothing. `'window'` attribution for learning and OPE; `'last_touch'` (used by `proof_rewards`) credits each purchase once, over the same measured period for every group. |
| `estimate.py` | `proof_summary`: incremental revenue and profit vs holdout, lift vs baseline, spend and ROI, all with 95% intervals (Welch normal or seeded bootstrap). Returns no claim, with a plain note, when a group is too small. |
| `ope.py` | Off-policy evaluation: IPS, SNIPS, doubly robust, each with an interval, effective sample size and largest weight. `from_decisions` builds a log from agent decisions. |
| `replay/` | Public-data replay (plan-v2 section 8): readers for Open Bandit, Hillstrom and Criteo uplift into one `LoggedBandit` frame; a harness that scores a target policy against the logging policy and fits a simple segment policy; `fetch` for the full downloads (never used by tests). |

### `autonomy/bridges/uplift_bridge.py`

Cross-checks v2's estimators against v1 (`engine.uplift.ope.evaluate_policy`,
`engine.measurement.continuous.mean_difference`). v1 is imported inside the functions; tests carry
`@pytest.mark.v1bridge`.

## How other streams use it

```python
from autonomy.decide import AutonomousPolicy, GoalBidder, FatigueModel, BetaResponseModel

GoalBidder(brief, content=None, response_model=None, value_model=None, costs=None, *,
           kind=GoalKind.CUSTOM, urgency=None, per_theme=True,
           segment_of=None, control_groups=frozenset({Group.HOLDOUT}))
  .bids(state, ctx) -> list[Bid]
  .learn(rewards, decisions) -> None

AutonomousPolicy(bidders, attention_price_fn=None, exploration_floor=None, baseline_policy=None, *,
                 mc_draws=1000, max_options=10, segment_hours=None, default_hour=10,
                 currency="$", version="autonomy-v1")
  .decide(state, ctx) -> Decision
  .learn(rewards, decisions) -> None

FatigueModel().price            # plug in as attention_price_fn
check_bid(bid, state, ctx) -> RuleResult
choose_send_time(state, envelope, now, *, fallback_hours=None, default_hour=10) -> datetime
```

```python
from autonomy.proof import group_assigner, proof_rewards, build_rewards, proof_summary, evaluate

group_assigner(salt, *, envelope=None, holdout_share=None, baseline_share=None)
    -> Callable[[CustomerState], CustomerState]          # pass to run_days(assign_group=...)
build_rewards(decisions, events_by_customer, horizon_days, objective, costs, *,
              now=None, attribution="window") -> list[Reward]     # for learning and OPE
proof_rewards(decisions, events_by_customer, horizon_days, objective, costs, *, now=None)
    -> list[Reward]                                        # for proof_summary only
proof_summary(rewards, decisions, now, goal_id=None, *, min_group=30,
              method="normal", n_boot=2000, seed=0) -> ProofSummary
evaluate(rewards, logged_prob, target_prob, *, q_logged=None, q_target=None, clip=None) -> OpeReport
from_decisions(decisions, rewards).evaluate(target, *, q=None, clip=None) -> OpeReport
```

```python
from autonomy.proof.replay import read_hillstrom, read_open_bandit, read_criteo_uplift, replay
replay(logged, *, segment_columns, test_share=0.5, seed=0, ...) -> ReplayReport   # .summary is plain English
```

Wiring for WF7/WF8: build one `GoalBidder` per approved brief (pass `kind=` from the goal), wrap
them in `AutonomousPolicy` with `FatigueModel().price`, assign groups with `group_assigner`, and
call `policy.learn(build_rewards(...), decisions_by_id)` nightly. Pass `segment_of` to the bidders
when the state store can look up a customer's segment; otherwise they use the segment seen at bid
time. Use `proof_rewards` (not `build_rewards`) with `proof_summary`, with the same `now`.

## Contract additions requested

- `Brief.kind: GoalKind | None = None`. Today the bidder needs `kind=` passed in to set
  cart-recovery urgency.
- `CustomerState.last_cart_at: AwareDatetime | None = None`, set by the folder on cart events.
  Today cart urgency falls back to the `days_since_cart` score, then `last_seen_at`, which any visit
  moves forward.

## Dependencies requested

None. Uses numpy, pandas and the stdlib. Replay type checks need `pandas-stubs`, which CI already has.

## Known gaps

- mypy --strict passes locally on a 3.13 target (only missing pandas stubs reported); CI on 3.11 has
  the final say.
- Sends scheduled for later do not count towards `contacts_7d`, so if `send_at` falls after the next
  decision the weekly cap can be exceeded. The runtime should count pending sends in state.
- `decision_id` repeats for two decisions on the same customer at the same `now`, and the random
  draws are seeded only by `(seed, customer_id)`.
- Neither the bidder nor the rules handle `OfferOption.needs_approval=True` or stated preferences
  such as `preferences.no_discounts`. The hard rules should add codes for both.
- A `GoalBidder` keeps the brief it was built with. Rebuild bidders when a brief changes.
- The fatigue model is not learned yet: `Reward`/`Decision` do not carry the customer's
  `contacts_7d` at decision time, so `FatigueObservation`s must be built by whoever has state history.
  The fatigue curve is one global curve, not per channel.
- `GoalBidder.learn` keys treated outcomes by the content's theme, so content must be seen in a bid
  (or passed to the constructor) before its outcomes can be learned. Its segment memo grows with
  the number of customers bid for; pass `segment_of` for large live runs.
- Control outcomes come from the holdout only by default, because the agent's own "do nothing"
  choices are not random. Pass `control_groups` to change that.
- Content is chosen per (channel, offer, theme); tone and format within a theme are not explored.
- `ValueModel` defaults (AOV 50, margin 0.4, 2 future orders) and `DEFAULT_CHANNEL_COSTS` are
  placeholders until per-business settings or priors replace them.
- Proof: refunds are not netted out; per-goal proof refuses when goals share customers (needs
  per-goal holdouts); the normal-path ROI interval treats spend as fixed.
- Replay: `autonomy.proof.ope.LoggedBandit` (from decisions) and `autonomy.proof.replay.LoggedBandit`
  (dataset frame) share a name in different packages. Open Bandit is treated as one action per slot,
  not slate OPE, so its intervals are somewhat optimistic. Checksums for the Open Bandit and Criteo
  downloads are placeholders and the URLs are untested. No replay report over the full datasets has
  been produced yet; it needs the downloads.
- The replay tests and fixtures live in `tests/v2/decide/` (moved from `tests/v2/proof/` to stay
  inside the stream's test folder).
