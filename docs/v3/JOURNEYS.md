# Channel journeys (plan-v3 decision 4)

**Files:** `autonomy/decide/journeys.py` (new), `configs/v3/journeys.yaml` (new),
`autonomy/contracts/decisions.py` (three additive fields), `autonomy/decide/policy.py` (hooks).
**Tests:** `tests/v3/journeys/test_journeys.py`.

A journey is a short plan of messages for one customer: an ordered list of steps, each a channel,
a wait since the step before and a condition. The agent chooses a journey the way it chooses any
option, by price, and logs the chance it had of choosing it.

It is opt-in. `AutonomousPolicy(journeys=JourneyRunner(load_journeys()))` switches it on. With the
default `journeys=None` the policy runs the same code path as before: a test builds two policies,
one without the argument and one with `journeys=None`, and compares 30 customers' decisions for
equality (`test_journeys_off_gives_identical_decisions`).

## What was there before

`Brief.follow_ups` (`FollowUpStep`: after N days, when `no_response` / `no_purchase` / `always`,
optionally a channel) is declared, validated by the Strategist and shown in briefings, but a grep for
`follow_up` / `follow` over `autonomy/` finds nothing that schedules a follow-up message: the only
other use is `Action.follow_up_step`, which the dispatcher and the explanation sentence read. So
journeys are the first thing that actually runs a second step. `Brief.follow_ups` is left as it is.

## The four journeys

Defined in `configs/v3/journeys.yaml`:

| Id | Steps | Offered when |
|---|---|---|
| A | email | always |
| B | email, then 3 days later WhatsApp if no purchase | the goal has a WhatsApp bid for this customer |
| C | WhatsApp now | the customer's value if they buy is at least `value_bar` (50 in the file, in the shop's currency; a starting value, replace it per shop with `JourneyConfig.with_value_bar`) |
| D | WhatsApp, then 2 days later an email reminder if no purchase | the goal has an email bid for this customer |

A step has `channel`, `wait_days` (since the step before) and `when`: `always`, `no_purchase` or
`not_opened` (no click or reply since the step before). The first step goes at once. The file also
holds the settings below. The loader refuses an empty journey, a first step with a wait or a
condition, an unknown channel or condition, a duplicate id and any misspelt field.

## Choosing: a journey is an option in the auction

`JourneyRunner.expand(offered, state)` runs inside `AutonomousPolicy.decide` after the bidders have
bid and the bids have their send times. Each bid on a channel that some journey starts on is
replaced by one bid per journey that starts there. Bids on other channels (SMS, push) go through
untouched. The rest of the auction (hard rules, attention price, pacing, weekly budget, restraint,
Thompson sampling with the floor) is unchanged and sees journeys as ordinary bids.

A journey's bid is priced over the whole plan:

    net = first step's net  +  sum over later steps of  reach x urgency x (gain x value - offer cost - channel cost)

- `reach` is the chance the customer has not bought after the steps before: the product of
  `1 - uplift` over them.
- `gain` is the later step's uplift times `follow_up_uplift_factor` (0.5 in the file: a second
  message is assumed to do half what a first does. This is a design default, not a measured number,
  and is the first thing to calibrate).
- A later step is priced as a plain reminder (the goal's best bid on that channel without an
  offer), so an offer is paid once, in the first step. A later step's channel cost is paid only
  with probability `reach`, which is how WhatsApp "goes where its extra uplift pays for it".
- The bid's `uplift`, `offer_cost`, `channel_cost` and `uncertainty` are combined the same way
  (uncertainty as independent spreads), so the budget rule sees the journey's expected cost.
- Organic purchases are not modelled in `reach`, so a later step's reach is slightly overstated.
  Its gain and its cost are overstated together, so the sign of the step's contribution is
  unaffected.

Worked example from the test fixtures (email: uplift 0.05, cost 0.001, value 100; WhatsApp: uplift
0.06, cost 0.03). `JourneyRunner(load_journeys()).expand` gives these expected nets, from running it:

| Journey | Expected net | Uplift | Channel cost |
|---|---|---|---|
| A | 4.999 | 0.0500 | 0.0010 |
| B | 7.8205 | 0.0785 | 0.0295 |
| C | 5.97 | 0.0600 | 0.0300 |
| D | 8.3191 | 0.0835 | 0.0309 |

Those are fixture numbers that pin the formula, not results on any dataset.

**Never-read channels.** `ChannelStats` has no count of opens (the folder counts delivers, clicks
and replies), so a channel counts as never read when the customer was sent `never_open_after_sends`
(5) messages on it with no click, no reply and no engagement time. A journey that uses such a
channel anywhere is not offered; if the state changes while a journey runs, the step on that
channel is skipped (`never_read` in the journey history). Set the setting to 0 to turn this off.

## Logging: journey-level probability

Three additive, optional fields (defaults `None`, so older logs and v2 code read unchanged):

- `Action.journey_id`: the journey this message belongs to.
- `Decision.journey_id`: the journey a decision starts, continues or holds.
- `Decision.journey_probability`: the chance the policy had of choosing that journey, in (0, 1].

For a first step, `journey_probability` is the sum of the auction's probabilities over every
option of that journey (every offer, content item and format), taken from the same distribution
that gives `Decision.probability` (`journeys.journey_probability`, over `Pick.probabilities`). So
`probability <= journey_probability`, and evaluating a new policy at journey level uses the
journey's probability instead of the option's. A test pins it exactly: with the exploration floor
at 1 and six options (two email contents x journeys A and B, plus one plain WhatsApp bid), each option has
chance 1/6 and a journey of two options logs 2/6.

Later steps are not drawn again. They carry `probability = 1.0` (given the journey) and the first
step's `journey_probability`, and `Action.follow_up_step` is the step's index, so
`explain.reason` already says "Sent follow-up 1". Holds ("Waiting: Journey B sends its next
message, by WhatsApp, on 24 Oct.") are NOTHING decisions with the same fields.

## Running a journey

`JourneyRunner` keeps one run per customer, made from the journey's decisions (`record`, called
from `AutonomousPolicy._remember`; `rebuild(decisions)` rebuilds all runs from a decision log).
Each daily sweep, `AutonomousPolicy.decide` asks `JourneyRunner.advance` first, before the auction:

1. **Stop at once** on a purchase since the first decision, an unsubscribe on any channel of the
   journey (a "stop" reply becomes an UNSUBSCRIBE event, `autonomy/agents/conversation.py`), or an
   open complaint. This is checked on every sweep, not only when a step is due. A customer who
   leaves the agent group (holdout, baseline) loses the journey too.
2. Skip steps on never-read channels.
3. If the next step is not due (its wait since the step before, looking ahead `lookahead_hours`,
   24, so a daily sweep does not make every step a day late), the customer is held: no new auction
   in the sweep (`hold_during_journey`). Cart and reply triggers are decided on their own and still
   reach the auction.
4. If the step is due and its condition no longer holds (`not_opened` and the customer clicked or
   replied), the journey ends.
5. Otherwise the goal's bidder quotes the step afresh. No quote, or one whose net no longer beats
   its cost (`require_positive_net`), ends the journey. The quote is timed (the customer's best
   allowed hour, never in quiet hours in their own time zone) and passed through `rules.check_bid`
   and the gap between messages, the same as a first message. The hard rules, consent, caps,
   holdout, snooze, quiet hours and budget, are therefore checked again at each step's own time.

What a blocked step does:

| Rule | Effect |
|---|---|
| holdout, unsubscribed, open complaint, no brief | the journey ends |
| no consent, no address, channel or offer not allowed, offer needs approval, no discounts | that step is skipped, the next step still runs |
| weekly cap, too soon, snooze, no budget, quiet hours | the step waits for the next sweep, for up to `max_defer_days` (2), then the journey ends (`expired:<rule>`) |

Because the bidders offer nothing on a channel the customer cannot be reached on, a consent
withdrawn before a step usually shows as "nothing to offer" and ends the journey; the rule path
above is the second line behind it, and is tested with a bidder that does not filter by reach.

Deciding the same moment again replaces that decision rather than moving the run twice (the same
rule `AutonomousPolicy` already follows for pending sends). `forget_pending` also ends a journey
whose first or latest decision was rejected or cancelled.

`JourneyRunner.history` is a bounded log of `started`, `sent`, `skipped`, `deferred`, `stopped` and
`completed` events with a code, for the briefing and the Analyst.

## Turning it on in the agent

The factory has no flag yet (kept out to leave `autonomy/runtime/agent.py` alone while other
streams edit it). After building the agent: `agent.policy.journeys = JourneyRunner(load_journeys())`.
`AgentPolicy._install` replaces `agent.policy.bidders` but not `journeys`, so it survives brief
changes. I ran this by hand on the real agent (`build_agent_policy` with the sample brief and
content, 60 synthetic customers who are reachable on email and WhatsApp): 59 chose a journey (A 16,
B 29, D 14, C was not chosen), one chose nothing;
and one B customer's second step, decided 3 days and 2 hours after the first send, came out as a
WhatsApp "follow-up 1" with `probability` 1.0 and the first step's `journey_probability`. That is a
wiring check on made-up customers, not a result.

## What was run, and what was not

Run, from the worktree root:

- `pytest tests/v3/journeys tests/v2/decide/test_policy.py tests/v2/test_contracts.py`: 89 passed
  in 2.47 s (the journey tests are 50 of them: parsing, pricing, value bar, never-read, the
  probability, journeys off, running, stopping on purchase / stop reply / unsubscribe / complaint,
  each hard rule at a step, rebuilding from the log).
- `ruff check` and `black --check` on the changed files, and `mypy --strict --python-version 3.11
  --ignore-missing-imports --follow-imports=silent` on `journeys.py`, `policy.py`, `decisions.py` and
  the test file: clean.

Not run (compute is shared and these run in CI): the Gym, including the plan's pass tests
(`whatsapp_fatigue` beating v2's agent and the best fixed rule, and a lower cost per extra order
than single messages in `baseline_shop`), the real bench and the scale tests. No claim is made
about them. The Gym needs a switch for `journeys` on its agent before it can be measured.

## Open points for the integrator

1. **Gym / factory switch** for `journeys` (above), then measure the two pass tests.
2. **Dispatcher.** A step decided in a sweep may be timed up to a day ahead, and the dispatcher
   re-checks consent but not "bought in the meantime". `JourneyRunner.stop_reason(run, journey,
   state)` is what a last-line check at send time can call.
3. **Proof and learning.** The proof harness should credit a journey's outcome to its first
   decision with `journey_probability`, over `JourneyRun`'s window (`Journey.window_days(horizon)`:
   total waits plus the brief's horizon). Later steps' `probability` is 1.0 given the journey and
   must not be used as an exploration probability. The response model learns step decisions as
   ordinary sends of their channel.
4. **State is in memory** (like the policy's pending sends). `rebuild` restores it from the log, but
   a purchase or unsubscribe is re-detected from the customer's state at the next sweep, not from
   the log.
5. `follow_up_uplift_factor`, `never_open_after_sends` and C's `value_bar` are design defaults, not
   measured; calibrate them on the Gym and a real shop.
