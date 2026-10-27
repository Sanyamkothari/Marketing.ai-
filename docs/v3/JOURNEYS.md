# Channel journeys (plan-v3 decision 4)

**Files:** `autonomy/decide/journeys.py` (new), `configs/v3/journeys.yaml` (new),
`autonomy/contracts/decisions.py` (four additive fields), `autonomy/decide/policy.py` (hooks),
`autonomy/channels/dispatcher.py` (a send-time purchase check for later steps),
`autonomy/runtime/agent.py` (`explain_bids` weighs journeys when they are on).
**Tests:** `tests/v3/journeys/test_journeys.py`.

A journey is a short plan of messages for one customer: an ordered list of steps, each a channel,
a wait since the step before and a condition. The agent chooses a journey the way it chooses any
option, by price, and logs the chance it had of choosing it.

It is opt-in. `AutonomousPolicy(journeys=JourneyRunner(load_journeys(order_value=...)))` switches it on
(the shop's usual order value sizes the value bars; `load_journeys()` alone leaves them unresolved and
`JourneyRunner` refuses it, see [CURRENCY.md](CURRENCY.md)). With the
default `journeys=None` every journey hook is skipped (`if self.journeys is not None`) and the new
decision fields stay `None`; the guarantee that v2 numbers are unchanged is the existing v2 test
suite, which runs that default path (CI runs it). One journey test adds a check of the hooks
themselves: a policy whose runner has only a journey on a channel no bidder uses (web) gives exactly
the decisions of a policy without journeys, for 30 customers
(`test_journeys_on_with_no_journey_for_the_channels_in_use_gives_identical_decisions`).

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
| C | WhatsApp now | the customer's value if they buy is at least the bar: `value_bar_order_multiple: 1.0` in the file, one usual order of the shop (`RuntimeSettings.typical_order_value`; the Gym and the bench use the reference order 50, so the bar the file always had). An exact bar for one shop, or None for no bar: `JourneyConfig.with_value_bar`. See [CURRENCY.md](CURRENCY.md) |
| D | WhatsApp, then 2 days later an email reminder if no purchase | the customer's value is at least the same bar as C (both send WhatsApp first) and the goal has an email bid |

A step has `channel`, `wait_days` (since the step before) and `when`: `always`, `no_purchase` or
`not_opened` (no click or reply since the step before). The first step goes at once. The file also
holds the settings below. The loader refuses an empty journey, a first step with a wait or a
condition, an unknown channel or condition, a duplicate id and any misspelt field.

## Choosing: a journey is an option in the auction

`JourneyRunner.expand(offered, state, price_of)` runs inside `AutonomousPolicy.decide` after the
bidders have bid and the bids have their send times. Each bid on a channel that some journey starts
on is replaced by one bid per journey that starts there. Bids on other channels (SMS, push) go
through untouched. A goal for which no journey fits the customer at all (WhatsApp only and below the
bar, say, or email never read and below the bar) keeps its single-message bids, so turning journeys
on never takes a channel away from such a customer; bids on a channel the customer never reads are
the exception and are dropped. For the first decision, the rest of the auction (hard rules, the
first step's attention price, pacing, weekly budget, restraint, Thompson sampling with the floor)
is unchanged and treats each journey as one option. The later steps go through the same market at
their own time (see "Running a journey").

A journey's bid is priced over the whole plan:

    net = first step's net
          + sum over later steps of  reach x (urgency x (gain x value - offer cost - channel cost) - attention price)

- `reach` is the chance the customer has not bought after the steps before: the product of
  `1 - uplift` over them.
- `gain` is the later step's uplift times `follow_up_uplift_factor` (0.5 in the file: a second
  message is assumed to do half what a first does. This is a design default, not a measured number,
  and is the first thing to calibrate).
- A later step is priced as a plain reminder (the goal's best bid on that channel without an
  offer), so an offer is paid once, in the first step. A later step's channel cost and its
  channel's attention price (`AutonomousPolicy.journey_prices`: the per-channel price the auction
  would set for the customer's single-message bids now, else the base attention price) are paid
  only with probability `reach`, which is how WhatsApp "goes where its extra uplift pays for it".
  The auction itself subtracts the first step's attention price, as for any bid.
- The bid's `uplift`, `offer_cost`, `channel_cost` and `uncertainty` are combined the same way
  (uncertainty as independent spreads), so the budget rule sees the journey's expected cost.
- Organic purchases are not modelled in `reach`, so a later step's reach is slightly overstated.
  Its gain and its cost are overstated together, so the sign of the step's contribution is
  unaffected.

Worked example from the test fixtures (email: uplift 0.05, cost 0.001, value 100; WhatsApp: uplift
0.06, cost 0.03; no attention price). `JourneyRunner(load_journeys(order_value=50.0)).expand([EMAIL, WHATSAPP],
state())`, with `EMAIL`, `WHATSAPP` and `state` imported from `tests/v3/journeys/test_journeys.py`,
prints these (rounded to 4 places):

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
channel anywhere is not offered (a shorter journey without that channel usually is: A for B, C for
D, and failing those the goal's single messages), and a single message on that channel is not
offered either. If the state changes while a journey runs, the step on that channel is skipped
(`never_read` in the journey history). Set the setting to 0 to turn this off.

## Logging: journey-level probability

Four additive, optional fields (defaults `None`, so older logs and v2 code read unchanged):

- `Action.journey_id`: the journey this message belongs to.
- `Decision.journey_id`: the journey a decision starts, continues or holds.
- `Decision.journey_probability`: the chance the policy had of choosing that journey, in (0, 1].
- `Decision.journey_ended`: `'<journey id>:<code>'` when a running journey ended while this
  decision was made (for example `B:nothing_to_offer`), so a run rebuilt from the log stays ended.

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
message, by WhatsApp, on 24 Oct.", the day in the customer's own time zone) are NOTHING decisions
with the same fields.

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
5. Otherwise the goal's bidder quotes the step afresh (NaN or infinite quotes are dropped, as in
   the auction). No quote, or one whose net is not positive (`require_positive_net`), ends the
   journey. The quote is timed (the customer's best allowed hour, never in quiet hours in their own
   time zone) and passed through `rules.check_bid` and the gap between messages, the same as a first
   message: consent, address, the weekly cap, holdout, snooze, quiet hours and the goal's money
   left (`ctx.budget_left`) are checked again at each step's own time.
6. A step that passes the rules goes through the same market as a first message,
   `AutonomousPolicy.auction` with that one bid: the channel's attention price, pacing, the
   customer's weekly contact budget (`budget_fn`) and the restraint margin. A hold by pacing or the
   weekly budget waits like a cap (below). A step whose net after its price is not positive, or that
   restraint would keep out of the draw, ends the journey (`no_longer_pays`). The decision logs
   the price paid in `attention_price`.
7. A step more than `max_defer_days` past its due time (sweeps were missed, or the agent was
   restarted) is not sent late: the journey ends (`expired`, or `expired:<rule>` when a rule was
   holding it).

What a blocked step does:

| Rule | Effect |
|---|---|
| holdout, unsubscribed, open complaint, no brief | the journey ends |
| no consent, no address, channel or offer not allowed, offer needs approval, no discounts | that step is skipped, the next step still runs |
| weekly cap, weekly budget (`weekly_cap`), pacing, too soon, snooze, no money left, quiet hours | the step waits for the next sweep, for up to `max_defer_days` (2), then the journey ends (`expired:<rule>`) |

Because the bidders offer nothing on a channel the customer cannot be reached on, a consent
withdrawn before a step usually shows as "nothing to offer" and ends the journey; the rule path
above is the second line behind it, and is tested with a bidder that does not filter by reach.

Deciding the same moment again replaces that decision rather than moving the run twice (the same
rule `AutonomousPolicy` already follows for pending sends): `JourneyRunner.begin` takes back every
change the earlier decision with that id made to the run (a step sent, a step skipped by a rule or
for a never-read channel, a stop), so the same moment gives the same decision. `forget_pending`
also ends a journey whose first or latest decision was rejected or cancelled. A journey started by
a cart or reply trigger over a running one replaces it, logged as `stopped: replaced`.

**At send time.** A step is decided up to `lookahead_hours` (24) before it is due and may be timed
up to a day after, so the dispatcher checks once more: a decision whose action has a `journey_id`
and `follow_up_step > 0` is skipped (`bought_since_decision`) when the customer's
`last_purchase_at` is at or after the decision's `made_at`. A queued send runs this check when the
job comes due; without a queue the check runs when `dispatch` hands the message (with its
`send_at`) to the provider.

`JourneyRunner.history` is a bounded log of `started`, `sent`, `skipped`, `deferred`, `stopped` and
`completed` events with a code, for the briefing and the Analyst.

## Turning it on in the agent

`build_agent_policy(..., journeys=True)` (or a `JourneyConfig`, or a `JourneyRunner`) switches it on, and so does
the v3 preset ([`PRESET.md`](PRESET.md)). `AgentPolicy._install` replaces `agent.policy.bidders` but not
`journeys`, so it survives brief changes. `explain_bids` ("why this, and why not that") then lists the journey
options `decide` weighs; it does not show the mid-journey hold or a due step, which `decide` handles before the
auction. The agent sets `JourneyRunner.fade` so that a later step is priced at the urgency left on its day (a cart
reminder halves daily, a moment closes), and a policy that is built again replays its runs from the decision log it
is handed before its first decision (`nightly`). No run of the real agent with journeys on, alone, is reported
here (an earlier hand run's numbers were dropped because its script was not kept).

## What was run, and what was not

Run, from the worktree root:

- `pytest tests/v3/journeys -q -o addopts=""`: 65 passed (parsing, pricing, the value bar, the
  single-message fallback, never-read channels, the probability, journeys on with no journey for
  the channels in use, running, stopping on purchase / stop reply / unsubscribe / complaint, each
  hard rule at a step, the weekly budget and attention price at a step, NaN quotes, re-deciding a
  skip, rebuilding from the log including ended runs and skips, late steps, replacement, the hold's
  date in the customer's zone, and the dispatcher's send-time purchase check).
- `pytest tests/v2/decide/test_policy.py tests/v2/test_contracts.py tests/v2/channels/test_dispatcher.py`:
  74 passed; `pytest tests/v2/wire_agent/test_items_formats.py -k explain`: 3 passed.
- `ruff check`, `black` and `mypy --strict --python-version 3.11 --ignore-missing-imports
  --follow-imports=silent` on every changed file: clean.

Not run (compute is shared and these run in CI): the rest of the v2 suite, the Gym, including the
plan's pass tests (`whatsapp_fatigue` beating v2's agent and the best fixed rule, and a lower cost
per extra order than single messages in `baseline_shop`), the real bench and the scale tests. No
claim is made about them. The Gym needs a switch for `journeys` on its agent before it can be
measured.

## Open points for the integrator

1. **Gym / factory switch** for `journeys` (above), then measure the two pass tests.
2. **Dispatcher.** It now skips a later step after a purchase (above). It does not know the
   journey's other channels, so a "stop" reply on email before a WhatsApp step is caught at the next
   sweep (and by the dispatcher only if it unsubscribed WhatsApp too).
3. **Proof and learning.** The proof harness should credit a journey's outcome to its first
   decision with `journey_probability`, over `JourneyRun`'s window (`Journey.window_days(horizon)`:
   total waits plus the brief's horizon). Later steps' `probability` is 1.0 given the journey and
   must not be used as an exploration probability. The agent no longer learns a later step as a
   fresh treatment (the customer did not buy after the step before it, so their purchase rate is a
   survivor's): the breaker, the tiredness curves and the pacer still count it as a message, the
   response models and the look-alike outcomes do not (`AgentPolicy._keep`, PRESET.md). Journey-level
   reward and credit (plan-v3: the purchase within the journey's window minus all its costs) is not
   wired into proof or learning yet; that is a follow-up for the proof stream.
4. **State is in memory** (like the policy's pending sends). `rebuild` restores it from the log,
   including runs that ended (`journey_ended`) and steps skipped by a rule (the hold's
   `blocked_by`). A purchase or unsubscribe is re-detected from the customer's state at the next
   sweep, and a never-read skip from the channel's counts. A customer who left the agent group is
   dropped again at their next decision.
5. `follow_up_uplift_factor`, `never_open_after_sends` and C's `value_bar` are design defaults, not
   measured; calibrate them on the Gym and a real shop.
