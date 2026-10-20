# Gym results: Autonomy's agent against the plan's bar (WF7)

The agent (`autonomy.runtime.agent.build_agent_policy`, registered in the Gym CLI as `agent`) was run in
every scenario of the library next to send-to-all, the rule baseline and the oracle, on the same
seeds, and judged with `autonomy.gym.score.pass_criteria` (plan-v2 section 8).

**Short version: the agent does not pass the bar, and parts of the bar cannot be passed by any
policy that does not read the Gym's truth.** It keeps every hard rule (zero consent, unsubscribe,
quiet-hour and cap breaks in all 18 runs), its breaker stops the planted harm after one batch, its
intervals cover the truth, and it beats send-to-all and the rule baseline in every scenario where
messages can make money. It captures about half of the oracle's value (32-56% by scenario), not
80%. The rest of this page gives the numbers and the reasons, including where we think the bar is
unreachable or ill-posed. Nothing was tuned to a seed, and the agent never reads the truth.

## How it was run

* Code: branch `v2/agent-gym` on top of `d5583537`, with this stream's changes.
* Every scenario at 1,000 customers (`small_business` at its own 1,500), 90 decision days, seeds 0
  and 1; four policies per seed. About 90-150 s per scenario on a shared 4-core machine; one agent
  run (1,000 customers, 90 days) takes 50-60 s.
* CI runs the full-size scenarios (5,000 customers) and more seeds. With five times the customers
  the holdout is five times larger, so the agent's purchase-based calibration is tighter there;
  expect the shares to move by a few points, not to 80%.

* After the scoreboard below was run, review fixed four things in the agent and the policy: a
  cached decision context that could hide a budget change within one moment (no effect in the Gym,
  where budgets are not tracked), events counted twice when a caller hands `nightly()` the whole
  log (the Gym hands it only what is new), a breaker crash when late complaints outnumber a week's
  deliveries, and a dropped send holding a customer as "too soon" for a week. Only the last can
  change Gym numbers, and only for customers whose scheduled send never went out; expect the table
  to move by at most a point or so until it is re-run.

Reproduce one line with:

    python -m autonomy.gym.run --scenario baseline_shop --policy agent --seeds 2 --customers 1000

## Scoreboard

"Share of oracle" is the sum over both seeds of a policy's true value (the Gym's planted truth
values every action it took) divided by the oracle's. "Beats both" counts seeds where the agent's
true value is above both send-to-all's and the rule baseline's.

| Scenario | Agent: share of oracle (true value) | Send-to-all | Rule baseline | Agent beats both | Sleeping dogs contacted | Rule breaks | Intervals cover truth | Breaker |
|---|---|---|---|---|---|---|---|---|
| baseline_shop | **53%** ($21,559 of $40,859) | 31% | 4% | 2 of 2 | - | 0 | 2 of 2 | - |
| sleeping_dogs_15 | **44%** ($15,068 of $34,150) | 24% | 2% | 2 of 2 | 241 of 250 (96%) | 0 | 2 of 2 | - |
| discount_cannibalisation | **56%** ($15,385 of $27,246) | -40% | 0% | 2 of 2 | - | 0 | 2 of 2 | - |
| whatsapp_fatigue | **32%** ($14,628 of $45,719) | 9% | 3% | 2 of 2 | - | 0 | 2 of 2 | - |
| send_time_matters | **46%** ($19,017 of $41,380) | 17% | 2% | 2 of 2 | - | 0 | 2 of 2 | - |
| drift_midway | **52%** ($21,709 of $41,375) | 37% | 4% | 2 of 2 | - | 0 | 2 of 2 | - |
| small_business (1,500) | **53%** ($33,383 of $63,179) | 30% | 3% | 2 of 2 | - | 0 | 2 of 2 | - |
| null_effect | oracle $0; agent **-$518** | -$11,534 | -$841 | 1 of 2 | - | 0 | 2 of 2 | - |
| complaint_storm | **3%** ($1,019 of $32,828) | 34% | 4% | 0 of 2 | - | 0 | 2 of 2 | stopped after 1 batch (both seeds) |

Against the pass bar, pooled over all 18 agent runs:

- [FAIL] Captures >= 80% of the oracle's value: 32% to 56% per scenario; `null_effect` lost $518
  (tolerance $16); `complaint_storm` 3%.
- [FAIL] Beats send-to-all and the rule baseline in >= 95% of seeds: won 15 of 18 (the misses are
  both `complaint_storm` seeds and `null_effect` seed 0).
- [FAIL] Contacts < 10% of planted sleeping dogs: contacted 241 of 250.
- [PASS] Zero consent, quiet-hour or cap violations: 0.
- [PASS] 95% intervals cover the truth 93%-97% of the time: 18 of 18 (Wilson range 82%-100%;
  with 18 runs this check cannot tell 93% from 100%).
- [PASS] The breaker trips within 2 batches in planted-harm scenarios: 2 of 2, after 1 batch.

## What passes

* **Zero rule breaks.** Two fixes in `decide` were needed. (a) Scheduled-but-unsent messages now
  count towards the weekly cap: the policy remembers each send until the customer's state shows it
  (or a week passes), so a message timed for tomorrow evening can no longer be joined by a third.
  (b) No new message within six hours of the last one, or while one is still scheduled
  (`rules.too_soon`). Before this, a customer could unsubscribe from this morning's message after
  the 10:00 decision and before an evening send, and the evening send then broke the unsubscribe.
  We saw one such break in `send_time_matters` (send-to-all still has them there).
* **The breaker.** The brief's stop rules are judged every night on the agent's own sends of the
  last week, and a rule trips only when the lower end of a one-sided 99.9% interval is past it
  (with at least 5 complaints). In `complaint_storm` the goal paused after the first batch in both
  seeds and stayed paused. An earlier version with a 99% interval and a 3-complaint minimum paused
  a healthy `baseline_shop` run (3 complaints on 925 messages in the first week); nightly checks of a
  rolling week need the stricter bar. The "unsubscribes 3 times the usual" rule measures "usual" on
  the goal's first two weeks: measured on the first days only, when everyone gets their first message
  of the week, it called the normal twice-a-week rhythm a storm.
* **Intervals cover the truth** in every run.
* **Beats the baselines** wherever messages make money (15 of 15 such seeds). Send-to-all hands its
  biggest discount to everyone and loses money outright in `discount_cannibalisation` and
  `null_effect`; the rule baseline only emails lapsed customers.

## What does not pass, and why

### Oracle capture (needs >= 80%; the agent gets 32-56%)

The oracle reads every customer's planted uplift, offer and theme multipliers, best hour and
sleeping-dog flag. Some of that is learnable from behaviour, much of it is not, at least not within
90 days and about 25 messages per customer. To see how far a policy could get with partial
knowledge, we scored a few fixed policies that are each handed a slice of the truth (diagnostics
only, kept out of the code), on `baseline_shop`, 1,000 customers, seed 0:

| Policy (diagnostic only) | What it is given | Share of oracle |
|---|---|---|
| WhatsApp if consented else email, no offer, one fixed local hour, everyone up to the cap | nothing | 57% (14:00) to 59% (18:00) |
| Best fixed action and hour per observable segment (new, lapsed, light/medium/heavy) x WhatsApp consent | the true average value of each action per segment | 70% |
| Best fixed action and hour per hidden persona x WhatsApp consent | each customer's persona | 88% |
| The agent | what it learns | 53% |

Perfect knowledge at the level of segments a business can see (tenure, recency, frequency) reaches
70%. 80% needs roughly persona-level knowledge, and the Gym plants personas so that two of them
(researchers and price-sensitive shoppers) look alike in purchase history and differ mainly in how
they answer offers and in their best hour.

The agent sits below even the "fixed action, fixed hour" rows. What we measured on an earlier build
(before the final send-hour and pacing changes, at 50% capture), by scoring the agent's own options
against the truth on days 40-90:

* **Learning speed is not the main gap.** Run a second 90 days on the same customers with
  everything learned kept (a warm start): 53% instead of 50%.
* **Choosing between actions for one person is.** Among the options it bid on, the truly best one
  was worth $0.82 a message on average, its top-ranked one $0.59 and the one sent $0.55
  (exploration). Its estimates are right on average per segment and action (correlation 0.55 with
  the truth), but people in one segment want different things. Per-customer click records help
  (+1.5 points) but are thin at 25 messages per customer.
* **Send time.** Once a customer's own reading hour is known (a click that comes hours after a
  message) the agent sends within 1% of their best hour, but that covered only about one decision in
  six by day 40. For everyone else it starts at the business default (10:00) and moves as clicks by
  send hour show a better hour, a weak signal at the default hour sensitivity. A fixed 14:00 instead
  of 10:00 scored about 9 points more in one `baseline_shop` run; we did not hard-code it, because
  that is fitting this Gym's planted best hours, not learning. Learning the hour from clicks by send
  hour is what lifted `send_time_matters` from 30-35% (earlier builds) to 46%.
* **Calibration on 1,000 customers.** "Extra purchases per click" is measured against a holdout of
  about 100 customers; on seed 0 it sat about a third above the ratio the Gym's click model implies
  for weeks, which makes discounts look better than they are.
* `whatsapp_fatigue` (32%) is low partly because the brief's own complaint rule (0.08%) is tighter
  than the complaints the scenario plants for a second WhatsApp message in a week: on seed 1 the
  breaker paused the goal mid-run (6 complaints on 1,320 messages in a week, 0.45%), as the brief
  tells it to. The truth does not charge for complaints, so pausing costs value on the scoreboard.

### Sleeping dogs (needs < 10% contacted; the agent contacts 96%)

This criterion is ill-posed for any policy that does not read the truth. The Gym marks a sleeping
dog with a coin flip that is independent of persona, purchase history, consent and every other
observable, so before the first message a sleeping dog looks exactly like everyone else. Contacting
fewer than 10% of them means contacting fewer than about 10% of all customers, which gives up most
of the value. After contact they click far less
(0.5% against about 10%), and the agent's personal click factor shrinks their bids from then on, but
they already count as contacted. Send-to-all contacts the same share; the rule baseline about half.

### `null_effect` (needs a loss of at most $0.01 per customer, $16 in total)

Nothing works here, so the right answer is to stop. A learner must send to find that out: the
evidence is the agent group's purchases against the holdout's, divided by its clicks. On seed 1 that
estimate reached zero within the first weeks and the agent stopped (loss $37, $0.05 a customer); on
seed 0 noise from a 100-customer holdout kept it positive and it lost $481 ($0.60 a customer), still
a twentieth of send-to-all. The tolerance is smaller than one email a week per customer over the
90 days; meeting it reliably needs either far more customers or a prior that messages do nothing,
which would cost value in every other scenario.

### `complaint_storm` (capture and beating the baselines fail by construction)

The breaker passes. But the Gym's truth ignores the future value lost to unsubscribes and
complaints, so the oracle keeps sending ($32,828 over the two seeds), send-to-all earns $11,285 and
an agent that stops earns $1,019. The realised money says the same (agent -$1,773 and -$185 against
the holdout; send-to-all +$4,772 and +$6,876): the Gym charges nothing for a complaint or for 65% of
customers unsubscribing. Stopping is what the plan asks for here, and it necessarily fails the
capture and beat checks for this scenario. We kept the pass bar as written rather than exclude
the scenario. A fair version would judge planted-harm scenarios on the breaker only, or charge the
lost future value of an unsubscribe in the truth.

## What the agent is

`autonomy/runtime/agent.py`; the per-customer maths is in `autonomy/decide`. One `GoalBidder` per
brief shares one `ModelBundle`; `AutonomousPolicy` runs the hard rules, the auction against "do
nothing" and Thompson sampling with the envelope's exploration floor, and logs every probability.

* **Uplift** = extra purchases per click (agent group against the holdout) x click rate per
  behaviour segment and action x the customer's own click record x a "messages this week" factor.
  Clicks arrive within hours and tell actions apart far sooner than week-long purchase windows.
* **Offers** are paid by everyone who buys while they are open: cost = cost per redemption x (the
  customer's own organic chance of buying + uplift).
* **Value** of a purchase: the customer's average order, shrunk towards the business's.
* **Attention price**: learned unsubscribe chance x the value of the messages we could no longer send.
* **Send hour**: own reading hour from late clicks, else the segment's best hour by clicks per send
  hour, starting from 10:00, with 10% of messages at random allowed hours.
* **Pacing**: pending sends count towards the cap; no message within six hours of the last; options
  that could not beat doing nothing even two standard deviations up are not explored.
* **Nightly**: clicks and purchases feed the models, `autonomy.learning.update_models` closes outcome
  windows (labelled with the segment each decision was made in, never today's segment), and the
  breaker checks the stop rules. The scoreboard now calls a policy's `nightly()` each simulated
  morning, and asks it to decide (and log "do nothing") for its holdout, as the runtime would.
* **New hard rules** in `decide/rules.py`: snoozes (`preferences["snooze_until"]`), offers marked
  `needs_approval`, `preferences["no_discounts"]` (money off and free shipping), and the minimum gap.
  Decision ids are unique per (customer, moment, trigger).

## What would move the numbers

* Per-customer learning of offer response with a proper hierarchical model (for example a logistic
  model of clicks on customer and action features with customer random effects), so a
  price-sensitive customer is found after a few offers rather than a few dozen messages.
* A send-time model that uses every click with its timing (a click soon after a send says the hour
  was close; a late one says when they read), not late clicks only.
* Variance reduction for "extra purchases per click" (pre-period purchases as a covariate) and
  calibration per segment; both matter most at 1,000 customers.
* Pacing against the brief's stop rules, not only against the unsubscribe price: stay under the
  complaint limit instead of tripping it (`whatsapp_fatigue`).
* In the Gym: charge the lost future value of an unsubscribe or complaint in the truth, so the oracle
  and the agent are judged on the same money, and plant some observable correlate of sleeping dogs
  if the sleeping-dog criterion is to be reachable.
