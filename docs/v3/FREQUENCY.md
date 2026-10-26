# Frequency: plan weeks, not messages

Plan-v3 decision 7 ([`plan-v3.md`](../../plan-v3.md) section 4). Know each person's tolerance and plan ahead.
Code: [`autonomy/decide/frequency.py`](../../autonomy/decide/frequency.py). Opt-in; the default agent is unchanged.

**Files.** `autonomy/decide/frequency.py` (new); `autonomy/decide/policy.py` (a `plan_fn` hook, a `plan=` flag on
`auction`); `autonomy/runtime/agent.py` (`build_agent_policy(frequency=...)`, `ContactPrice` reads the personal
rate); `autonomy/decide/rules.py` and `explain.py` (one new code, `frequency_plan`, and its sentence);
`api/v2/insights.py` (its words for the Live screen); `autonomy/gym/run.py` (the policy `agent_frequency`);
`autonomy/decide/bidders.py` (`GoalBidder.urgency_on`, a goal's urgency on a later day, for the plans).
**Tests:** `tests/v3/frequency/`.

## What it is

Two parts. Each can be switched off apart (`FrequencyConfig(tolerance=None)` or `FrequencyConfig(plans=None)`), to
measure it alone.

### 1. Tolerance from look-alikes (`LookalikeHazard`)

`HazardModel` learns one curve for everyone: the chance a message loses the contact (an unsubscribe or a complaint),
per channel and messages this week. The attention price (`ContactPrice`) multiplies it by what the customer would lose.
`LookalikeHazard` is a `HazardModel` (the curve is still learned and still fed the same way) that adds who is asked:

    rate(customer, channel, n) = curve(channel, n) x crowd(profile) x person(customer)

- **`crowd(profile)`**: a Poisson regression with a log link, fitted on every delivered send the agent decided and
  has seen the outcome of (lost the contact or not). The curve is the offset, so the regression learns only what the
  profile adds to it. Features are the look-alike profile (`lookalike.profile_of`, the nightly features included)
  without the weekly count, which the curve already carries. A ridge (`slope_penalty` 10, about 0.3 on the log scale
  per standard deviation) keeps a few dozen events from drawing a confident picture, and the multiplier stays within
  `[1/8, 8]`.
- **`person(customer)`**: a Gamma-Poisson factor on the customer's own record, their lost contacts over the ones the
  crowd expected for their sends, shrunk by `person_strength` (5 expected events). It starts at 1, so a new customer
  gets their neighbourhood's rate exactly, and it moves as their record grows. A lost contact stops that channel
  (the rules block it), so a customer has at most one loss per channel: in practice the factor mostly *lowers* the
  price for long-time survivors, and raises it only for a customer who lost one channel and is still reachable on
  another. On the fixture of `test_a_customers_own_record_moves_them_off_their_neighbourhood` (two groups both at
  2%, then one customer with 400 emails and no loss, one who lost email on the third and is still on WhatsApp) the
  first sits at 0.42 times a stranger's WhatsApp rate with the same profile and the second at 1.19 times; a customer
  with two sends is within 2% of a stranger. Both figures come from running that fixture in a scratch script (the
  test asserts the order and the 2%, not these two figures).
- **Too little data**: below `min_events` (20) lost contacts the multiplier is 1 and the answer is the plain curve's,
  exactly (`test_before_any_data_it_answers_exactly_like_the_plain_hazard`).

The agent keeps the profile each send was decided on until its delivery is seen (`AgentPolicy._tired_profiles`), hands
both to `LookalikeHazard.record_send`, marks a loss with `record_loss`, and refits in `nightly` (`fit`) when the
sends recorded since the last refit reach a fifth of the store (and at least 200). They are counted as they come, so
a store that is full (`max_rows`, 60,000, oldest dropped first) keeps refitting
(`test_a_full_store_keeps_refitting_as_the_crowd_changes`). The same pattern is used for the complaint model (its own base rate). `ContactPrice` reads
`rate_for(state, channel, n)` when the model is a `LookalikeHazard` and the plain `rate(channel, n)` otherwise, so the
interface the attention price used is the same.

### 2. Three-week plans (`FrequencyPlanner`)

For each customer, four fixed plans over the next 21 days are compared by total expected value minus the tiredness
each message causes:

| Plan | Messages on day |
|---|---|
| `nothing` | none |
| `now` | 0 |
| `now_and_10d` | 0 and 10 |
| `wait_14d` | 14 |

A message of a plan is the customer's best option at that moment, from the same bids the auction weighs:

    urgency(day) x (uplift x value x freshness - offer cost - channel cost) - attention price

times `weekly_discount ** (days / 7)` (0.98: a message a week later is worth a little less). This is how
`ContactPrice.rhythm` values a bid, from the bid's fields. It is not exactly the auction's `expected_net -
market.cost(bid)`: it leaves out pacing surcharges, the offer's shadow price (in `expected_net` only), the attention
price of a journey's later steps, and a negative uplift counts as 0.

**Urgency on a later day.** For a message today it is the bid's urgency. For a later message it is today's times the
goal's fade over those days, which the agent reads from the goal's bidder (`GoalBidder.urgency_on` at the later
moment, `AgentPolicy._urgency_fade`): a cart reminder halves every day (`DEFAULT_CART_HALF_LIFE_DAYS`), so 14 days on
it is worth about 2^-14 of today's, and a moment that has closed no longer raises it. A planner with no fade, or a
goal the fade does not know, keeps the bid's urgency up to 1 for a later message (a moment's boost taken as gone) and
values a bid whose urgency already fades (below 1) at nothing later. So a time-sensitive message is not held for a
day it would be worthless on (`test_a_fading_bid_is_never_held_for_a_later_day`,
`test_the_agent_fades_a_cart_reminder_by_its_bidder`). On top of that, only the daily sweep is planned: a decision
prompted by an event (a cart, a reply: `trigger` other than the sweep) is never held by the plan
(`test_a_decision_prompted_by_an_event_is_not_planned`).

**The raised hazard.** The attention price and the freshness (the click model's "messages this week" factor) are read
at a **fractional count**, so a later message is priced with the hazard the earlier ones raised. The count is built
from the customer's real past sends (`recent_contacts`, their send times in the last 30 days) and the plan's own
earlier messages, all fading the same way:

    count(day d) = sum over past sends i of exp(-(age_i + d) / 10)  +  sum over earlier messages j of the plan of exp(-(d - day_j) / 10)

So the plan and the plan made again on a later day agree: a message sent 10 days ago leaves today's message dearer by
`exp(-1)` of a message, the same amount the plan charged when it planned the second message for day 10
(`test_the_plan_made_ten_days_later_charges_the_raised_hazard_the_first_plan_assumed`). A message sent today counts 1,
as in the auction's weekly count. Sends the weekly count holds but the list does not explain (a pending send the
policy added) count at age 0; the month's unexplained ones at 7 days, the youngest they can be, as
`mandate.enforce.touches_in_window` counts them. The freshness of a later message is relative to the week's count the
bids' uplift was read at.

Pinned by `test_a_later_message_is_priced_with_the_hazard_the_earlier_one_raises`: with a round-number pricing (loss
100, hazard 1% x (1 + count), uplift 0.05, value 100, cost 0.01, no discount, no past sends) `now` is worth 3.99, the
message at day 10 of `now_and_10d` is worth 3.6221 (price 1.3679 against 1.0 for a fresh one), `now_and_10d` 7.6121 and
`wait_14d` 3.99. Those are fixture numbers that pin the formula, not results on any dataset.

**Today's decision follows the best plan's first step, and the plan is made again every day.** It is made from the
customer's state and bids of the day, so it bends when something happens: a purchase that opens a cross-sell moment
raises the bid's urgency and the plan goes from `wait_14d` to `now_and_10d` (`test_replanning_follows_a_purchase`).
As the last message fades the wait ends: on the fixture of `test_the_plan_is_made_again_every_day_from_the_state` (a
hazard of 0.5% for the first message of a week and 1.2% for the second, one message on day 0) the plan waits on days 0
to 3 and sends from day 4, when that message counts `exp(-0.4)` = 0.67. The hold therefore ends when the faded price
of a message today is low enough, not when the 7-day count empties, and a message 10 days after the last is still
charged the raised hazard. Nothing is stored between days except `last` and `chosen` (for the briefing and the tests).

**How the first step is applied: as a veto.**

- The best plan waits (nothing today, a message later: `wait_14d`): the customer is left alone today, with
  probability 1 and `frequency_plan` in `blocked_by` ("Left alone: waiting is worth more for them than a message
  today, by their 3-week plan.").
- The best plan sends now: the auction goes on as before, with its price, restraint, Thompson draw and exploration
  floor.
- The best plan is `nothing` (no plan with a message beats nothing): the auction goes on too, so exploration is not
  cut where no plan is worth anything.

The plan can only hold, never release (`test_the_plan_only_ever_holds_it_never_releases`).

**Order in `AutonomousPolicy.auction`**: attention price, pacing, weekly budget (`ContactPrice.budget`, the weekly
rhythm), **the plan**, the best `max_options`, plausibility, restraint, the draw. The weekly rhythm is kept: the plan
adds a three-week view to it, and both must allow a message.

**Hard caps stay a backstop.** The hard rules (`rules.check_bid`) run before the auction and remove bids; the plan is
only asked about the bids that survive (`test_the_weekly_cap_still_holds_whatever_the_plan_says`).

**Coexistence with journeys.** A customer inside a journey follows the journey: `_journey_turn` runs before the
auction, and the later steps go through the market with `plan=False`, so a step is not vetoed by a new plan
(`test_a_customer_in_a_journey_follows_it_even_when_the_plan_would_wait`). A journey's first step is chosen in the
auction, so the plan can hold it back for a day like any first message; the journey's bid is then the "message" the
planner sees.

## Switching it on

| Where | Default | On |
|---|---|---|
| `build_agent_policy(..., frequency=...)` | `False`: plain `HazardModel`s, no plan hook, every existing number unchanged | `True`, or a `FrequencyConfig(tolerance=ToleranceConfig(...), plans=PlanConfig(...))` |
| Gym CLI `--policy` | `agent` | `agent_frequency` (look-alike response model on, plus both parts) |

There is no `RuntimeSettings` switch yet: `api/v2/engines/decisions.py` and `build_runtime` build the agent without
it. Say if the runtime should carry one.

The switch is pinned by `tests/v3/frequency/test_agent.py`: the default agent has plain `HazardModel`s and no hook;
`frequency=False` gives decisions identical to no argument for 40 customers; tolerance alone, before any data, gives
the default decisions exactly; with plans on, 40 customers with a free week are decided exactly as the default. The
first of these goes through the same code both ways, so it pins the switch, not the default's numbers: that the
default is unchanged by this stream is guarded by the v2 tests of the code it touches (`tests/v2/contact_less`,
`tests/v2/decide/test_policy.py`, `tests/v2/decide/test_bidders.py`), which pass.

## Design defaults, not measurements

`memory_days` (10), `weekly_discount` (0.98), the four plans, `slope_penalty`, `person_strength` and `min_events` are
design defaults. None was tuned to a dataset or a Gym seed. The first to calibrate is `memory_days`: the hazard curve is
learned on the 7-day count, so how long a message keeps a customer tired beyond a week is assumed, not measured.

## Limits

- The tolerance learns only from sends this process decided (it needs the profile at decision time). The warm start
  from history feeds the plain curve (`_warm_hazard`) but has no profiles, so the crowd starts empty after a restart
  or a rebuild, as the look-alike response model's does (docs/v3/LOOKALIKE.md).
- Unsubscribes and complaints are rare. The crowd needs `min_events` of them before it speaks, and a small shop may
  never get there; the plain curve answers then.
- The plan is a veto on the first step. It does not yet choose a stronger offer for the message after a long wait
  ("nothing for 2 weeks then a stronger offer" in plan-v3 is the same plan priced with the fresher message).
- `last` and `chosen` live in memory.

## Tests (`tests/v3/frequency`)

- `test_tolerance.py`: exactly the plain answer before data and below `min_events`; a planted tired group (5%) is
  priced above 4x a calm one (0.2%) for a customer nobody has sent anything to (on that fixture the rates come out
  at 5.1% and 0.33% against the plain curve's 2.7% for everyone; the test asserts the ratio and a loose band);
  non-decreasing in the weekly count; the weekly count in the profile is not counted twice; a customer's own record
  moves them (at most one loss per channel), two messages barely do; identical groups get no invented difference;
  the multiplier is bounded; a full store keeps refitting when the planted rate changes.
- `test_plans.py`: later messages are priced with the raised hazard (exact numbers); earlier messages fade into
  the planned ones; unexplained sends count at age 0 (week) and 7 days (month); the plan made 10 days later charges
  the hazard the first plan assumed; a longer memory makes the second message dearer; the discount; who waits; a
  fading bid (a cart) is never held, with or without the goal's fade; a decision prompted by an event is not planned;
  a waiting plan leaves the customer alone and says why; re-planning follows a purchase and the days;
  `observe=False` records nothing; the plan never releases a hold; the weekly cap and consent still hold and the
  plan is not asked; a customer in a journey follows it.
- `test_agent.py`: the default; the switch; each part apart; the Gym lists `agent_frequency`; the agent learns
  tolerance from its own sends and unsubscribes; **a planted tired group gets fewer messages**, with and without
  plans (60 calm customers all get a message, fewer than half of the 60 tired ones do, and with the plain curve all
  120 do); **the plan itself holds a tired group** after a message yesterday (planted 1.5% against 0.2%: the price
  alone lets more than 50 of 60 tired customers through, the plan holds more than 50 of them and none of the 60 calm
  ones, and with the plain curve both groups are planned alike); the agent fades a cart reminder by its bidder; the
  module imports numpy only.
- `test_gym.py` (slow, CI): one 200-customer Gym run.

## Smoke run

One run, to see that it works end to end (not a comparison): 1 seed, 200 customers, 90 days, history 30 days, scenario
`baseline_shop`, with the slow test:

```
python -m pytest tests/v3/frequency/test_gym.py -m slow -s
```

Run on the commit with the review fixes:

- It finished in 11.48 s (pytest's timer) with 0 rule breaks.
- True value 2208.0. No `agent` or `agent_lookalike` run was made on this commit and the numbers of
  docs/v3/LOOKALIKE.md come from another history length, so this says nothing about whether the frequency agent beats
  them. (The run before the review fixes printed 2057.9; one seed of 200 customers says nothing about which is better.)
- The plans won `now_and_10d` 3,491 times and `wait_14d` 379 times over the run (`chosen`, one per planned decision).
  157 customers had a plan recorded when the run ended; none of them was in `wait_14d` at that moment.
- The look-alike tiredness had 3,364 delivered sends with their profiles but was not in use (`ready=False`): the run
  had fewer than `min_events` (20) lost contacts at the last nightly fit, so every answer was the plain curve's.

## The comparison: pending CI

`agent_lookalike` and `agent_frequency` on the same seeds (the frequency agent has the look-alike response model on,
so the pair isolates this stream; add `agent` for the full picture). Pending CI: nothing below has been run.

```
for s in baseline_shop sleeping_dogs_15 discount_cannibalisation send_time_matters whatsapp_fatigue null_effect; do
  python -m autonomy.gym.run --scenario $s --policy agent_lookalike --seeds 5 --customers 2000 --report-json lookalike_$s.json
  python -m autonomy.gym.run --scenario $s --policy agent_frequency --seeds 5 --customers 2000 --report-json frequency_$s.json
done
```

Read, against the pass test of plan-v3 decision 7:

- `null_effect`: loss within the staged bar's allowance ([`docs/v2/GYM_BAR.md`](../v2/GYM_BAR.md)).
- `whatsapp_fatigue`: `unsubscribe_rate` and `contacts_per_customer_week` no higher than the best fixed rule's, with
  true value not lower than `agent_lookalike`'s.
- Every scenario: true value, share of the segment oracle, `sleeping_dog_contact_share`.
- Live (after launch): unsubscribe and complaint rates in the agent group no higher than in the holdout plus a small
  margin.

The tests of this stream and the v2 files that cover the changed code ran with the commands below; the
Gym comparison above is the only part that waits for CI.

```
python -m pytest tests/v3/frequency -m "not slow"
python -m pytest tests/v2/contact_less tests/v2/agent_brain/test_timing_pacing.py tests/v3/journeys tests/v3/lookalike/test_agent.py tests/v2/decide/test_policy.py -m "not slow"
python -m pytest tests/v2/decide/test_bidders.py tests/v3/moments/test_hook.py -m "not slow"
```
