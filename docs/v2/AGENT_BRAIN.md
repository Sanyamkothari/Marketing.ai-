# Agent brain, wave 3b: choices per person

The agent (`autonomy.runtime.agent`) was scoring 32-56% of the oracle's value in the Gym, and below
a fixed rule: "WhatsApp if consented else email, no offer, one fixed local hour, everyone up to the
weekly cap". This wave measured the gap, found its causes, and made the agent's choices per
person. **Short version: the agent now beats the fixed rule (at either 14:00 or 18:00) in
`baseline_shop`, `send_time_matters` and `sleeping_dogs_15` on both seeds. In
`discount_cannibalisation` it beats the 14:00 rule on one seed out of two and stays below the 18:00
rule on both, though the gap to the 18:00 rule fell from 11.3 points to 2.5.** Its share of the
oracle rose by 2.0-8.8 points per scenario.

Nothing here reads the Gym's truth, tunes to a seed, or hard-codes a planted parameter. The truth
was read only by diagnostics kept out of the code (regret probes in a scratch directory), as in
`docs/v2/GYM_RESULTS.md`.

## How it was measured

* `tests/v2/agent_brain/diag.py` defines the fixed rule as a `Policy` (`FixedRule`, registered there
  as `fixed14` and `fixed18`; it remembers its own scheduled sends so it keeps the weekly cap) and a
  runner that scores policies against the oracle on the same seeds:

      python -m tests.v2.agent_brain.diag --scenarios baseline_shop --policies agent fixed14 fixed18 --seeds 2

* 1,000 customers, 90 decision days, seeds 0 and 1, the four scenarios where messages make money and
  the fixed rule was the bar: `baseline_shop`, `sleeping_dogs_15`, `discount_cannibalisation`,
  `send_time_matters`. "Share" is the sum of true value over both seeds divided by the oracle's.
* The fixed rule scored 52-54% on `baseline_shop` here, not the 57-59% that GYM_RESULTS quotes for
  one earlier run. The yardstick used below is this file's rule, run on the same seeds as the agent.

## Before and after

Share of the oracle's value, pooled over seeds 0 and 1 (per seed in brackets).

| Scenario | Agent before | **Agent after** | Fixed rule 14:00 | Fixed rule 18:00 | Oracle (2 seeds) |
|---|---|---|---|---|---|
| baseline_shop | 52.8% (53.3, 52.3) | **55.3% (54.8, 55.9)** | 51.8% (52.0, 51.5) | 53.9% (54.2, 53.6) | $40,859 |
| sleeping_dogs_15 | 44.1% (44.9, 43.4) | **50.2% (49.5, 50.8)** | 45.8% (44.7, 46.7) | 48.1% (47.1, 49.0) | $34,150 |
| discount_cannibalisation | 56.5% (57.6, 55.3) | **65.3% (67.1, 63.5)** | 65.2% (65.5, 64.8) | 67.8% (68.3, 67.3) | $27,246 |
| send_time_matters | 46.0% (45.8, 46.1) | **48.0% (49.2, 46.8)** | 40.6% (41.0, 40.2) | 46.9% (47.7, 46.0) | $41,380 |

Other numbers from the same runs:

* Contacts per customer-week: 1.65-1.72 before, 1.76-1.82 after (the fixed rule: 1.83-1.85).
* `null_effect` (nothing works): the agent lost $374 and $101 on seeds 0 and 1 ($475 in all,
  against $518 in GYM_RESULTS). The 18:00 fixed rule lost $293 on seed 1.
* `complaint_storm`: the breaker still stops sending after the first batch on both seeds.
* One agent run (1,000 customers, 90 days) takes 60-66 s with four runs sharing a 4-core machine
  (CI timing will differ). The extra work is small: refitting the personal priors once a week and
  the CUPED estimate each night.

## Why the agent was below the fixed rule

We measured each cause in the Gym before fixing it (one scenario and seed at a time). Below,
"regret" is the true value of the best option at that moment minus that of the option chosen.

1. **It sent 7% fewer messages than the cap allows** (1.72 a customer-week against 1.85). The weekly
   cap was judged on the 7 days before the 10:00 decision, not before the planned send. A customer
   last messaged at 20:00 a week ago is blocked at 10:00 but would be inside the cap at 20:00 today, so
   every such customer lost a day each week. The cap is now judged on the week before the planned send
   (`AgentPolicy._contacts_at_send`). The policy still adds sends that are scheduled but not yet
   folded into the state, so the cap is never broken. Effect alone: `baseline_shop` 53.3% to 54.5% and
   `discount_cannibalisation` 57.6% to 61.5% (seed 0).
2. **The breaker paused healthy goals by chance.** Two of eight runs were paused on 5 complaints in
   about 1,150-1,370 messages. The true complaint rate was about 0.06% against the brief's 0.08% limit,
   and at that rate such a count shows up once in a few hundred weeks. But the rule is checked every
   night on a rolling week (about 90 looks a quarter), and a Wilson bound is too eager at a handful of
   events. The complaint and unsubscribe rules now trip only on a count whose exact Poisson tail at the
   limit is below 1 in 10,000 (`BREAKER_ALPHA`). A real storm (complaints at 10x the limit) still
   trips on its first batch. The tail is summed in log space, so it stays exact at a big sender's
   volume (a running product from exp(-expected) underflows past about 745 expected events).
3. **Offers went to the wrong people, and that was most of the regret.** On `baseline_shop` (seed 0,
   days 30-90) the best option at the chosen hour was worth $0.85-0.87 a message on average and the
   one sent $0.58. Of that gap, 79% was the offer: price-sensitive and at-risk customers are best
   served with 10% off, and the agent gave it to fewer than a third of them. It also gave
   discounts to loyal customers who buy anyway.
4. **Calibration noise.** "Extra purchases per click" came from a ~100-customer holdout. It swung
   from 0.06 to 0.36 in the first weeks and was weighted by click count, however noisy the purchase
   comparison behind it still was. A low value makes every message look worthless; a high one makes
   discounts look cheap.
5. **Selection bias in click rates.** An action sent mostly to keen clickers was credited with their
   keenness. Discount content, for example, then looked better than it is.
6. **Send hour.** Customers without a known reading hour started at 10:00 and moved only as clicks
   by send hour slowly showed a better hour.
7. **Exploring personal taste does not pay.** A first version sampled each option on the
   uncertainty of the customer's own taste as well. That shuffled options for everyone, all run long,
   because a person gets about 25 messages and clicks on 2-3 of them, too few to learn their taste
   from what we try on them.

## What changed

**Per-person response** (`PersonalEffects` in `autonomy/decide/models.py`). A hierarchical
(empirical-Bayes) model of each customer's clicks relative to their segment:

    clicks ~ Poisson(segment rate(action) x rho_c x psi_c[channel] x psi_c[offer] x psi_c[theme])

* Every factor has a Gamma(k, k) prior centred on 1, so a customer with no record is exactly their
  segment, and one with many messages is close to their own rate. Every delivered message adds its
  segment-predicted clicks to the customer's evidence and every first click adds a click. The
  posterior comes from two rounds of backfitting.
* Features, not whole actions: learning that someone answers 10% off by email informs 10% off on
  WhatsApp. "No offer" is the offer's reference level (fixed at 1), so `rho` is how a customer answers
  a plain message and the offer factor is how much more or less an offer moves them.
* **The prior strengths are fitted, not guessed** (`refit()`, weekly). The method-of-moments
  estimate (`moment_strength`) subtracts yes/no click noise, because a message gets at most one
  click. A Poisson likelihood (`fit_gamma_strength`, kept for Poisson-like data) mistook that smaller
  noise for "people do not differ". Each feature is measured against the customer's level on their
  other messages (on their no-offer messages, for the offer), so their general keenness does not hide
  their taste. On synthetic data with known spread both strengths come back within about 30%
  (tests).
* The bids use the personal factor in the means. The sampling spread between a customer's options
  stays the segment click rate's (see cause 7).
* Segment click rates count each send with the customer's own factor as its exposure (a Poisson
  offset), which removes the selection bias in cause 5 (`ClickResponseModel.observe_send`).

**Calibration** (`LiftPerClick` in `autonomy/runtime/agent.py`; helpers `cuped_adjust`,
`cuped_theta`, `shrink_to_pool` in `autonomy/decide/models.py`).

* **CUPED.** Each customer's purchase rate before their first decision is a covariate. The
  agent-vs-holdout difference in purchases since the start is adjusted by theta x (pre-period rate -
  its mean), with theta fitted on both groups together, so the mean is unchanged and the variance of
  "some people buy more than others" is gone. On synthetic data where habit drives two thirds of the
  variance, the reported standard error falls by more than 30% with no bias (test).
* **Precision weighting** (`LiftPerClick.posterior`). The measured ratio counts by 1 / its variance
  against the prior's, not by its click count. `estimate()` is kept for its existing callers.
* **Per segment, shrunk** (`by_segment`). The same ratio inside each segment (the one a customer had
  when they joined), pulled towards the business-wide posterior by the DerSimonian-Laird spread. With
  ~20 holdout customers per segment it mostly stays at the pool, as it should.

**Warm start from history** (`logged_sends`, `ipw_weights`, `weighted_rate` in
`autonomy/learning/observations.py`; `ClickResponseModel.warm_start` and `AgentPolicy._warm_start`).
Everything handed to `nightly()` before the first decision is history. It is kept by event id and
read only at the first decision, so the whole log handed over twice counts once and a click
handed over after its send is still matched. Past campaigns' sends (with or
without decision ids), deliveries and clicks are read back, along with `attrs["logged_probability"]`
when a randomised campaign logged it. At the first decision:

* segment click rates take each action's inverse-propensity-weighted (Hajek) click rate, worth its
  effective sample size, so a campaign that targeted keen customers does not inflate the rate (on
  synthetic data: true rate 0.30, naive rate 0.46, learned 0.30 +- 0.05);
* each customer's own record takes their past sends (unweighted: their response to what they got is
  what it is);
* late past clicks give reading hours.

Today's Gym has no past campaigns, so this is exercised by tests only. The realistic Gym (another
stream) adds them.

**Send hour** (`ReadingHours.closest_hour`). A customer with a late click is sent at their own
reading hour, as before. Everyone else is sent at the allowed hour closest on average (circular
median) to when people in their segment read. Each known customer counts once, and the segment is
shrunk towards the business. If closeness to the reading hour is what makes a message land, as any
falling curve says, that hour loses least whatever the curve's steepness, so no planted hour or
sensitivity is assumed. 5% of messages to customers without a known hour go at a random allowed
hour (was 10%).

**Pruning** (`GoalBidder(prune_z=...)`, opt-in; the agent uses 2). Options that could not be this
customer's best even two standard deviations up are not offered. Before, the uniform exploration
floor kept handing discounts to people who plainly buy at full price. The bar is set only by
options the offer rules let through for that customer (approved, within budget, no discount for
someone who asked for none), so a blocked discount never prunes away the plain message.

## What still separates the agent from the oracle

Per-person choice among options is still weak. Over a customer's options at one moment, the rank
correlation between the agent's estimates and the truth is 0.14-0.18 (days 30-90). The Gym gives
each customer independent lognormal multipliers per channel (sigma 0.6) and noisy offer and theme
multipliers (sigma 0.3) on top of their persona. At about 25 messages and 2-3 clicks per customer in
90 days, these cannot be told apart from noise, and the empirical-Bayes fit rightly keeps most
people close to their segment. The rest of the gap:

* **`discount_cannibalisation`** is the one scenario where the fixed rule still wins. There, offers
  barely work and the fixed rule's "WhatsApp if consented" is a good prior. Days 30-90, the agent's
  choices are worth about what the fixed rule's are ($0.49 a message), but its first month costs
  12% while the click rates and the purchase calibration are learned. A history warm start (the
  realistic Gym) is the cure for that month. A stronger prior against offers would be the other, but
  it would cost value where offers work (`baseline_shop`), so we did not add one.
* **Clicks are not uplift.** Some clicks come from people a message does not move. Uplift
  proportional to clicks compresses the differences between options: a strong option's uplift is
  under-estimated relative to a weak one's. A per-action click floor cannot be identified from a
  100-customer holdout.
* **Hours:** about 7% of value is still lost to the send hour (best hour against chosen hour, same
  action). Only customers with a late click have a known hour (15-20% of them by day 90).

## Tests

`tests/v2/agent_brain/test_per_person.py` (all pure maths or tiny runs, about 2 s in total):

* shrinkage: two messages leave a customer near their segment, 300 bring them to their own rate,
  and most of it carries over to actions they have not had yet; learning on one offer informs the
  same offer on another channel;
* the fitted strength is high for alike customers and low for different ones (Poisson case), and the
  moment fit recovers a known spread with a quarter of non-responders;
* CUPED keeps the mean and cuts the variance; `LiftPerClick` with CUPED is unbiased and tighter;
  `shrink_to_pool` pools noisy groups and keeps precise ones;
* `logged_sends` reads propensities and matches clicks with and without decision ids; the warm
  start uses logged propensities (the learned rate is the segment's, not the targeted few's), gives
  keen and cold customers their own records, and gives reading hours;
* the segment's closest hour, pruning, and the cap judged on the week before the planned send;
* a tiny Gym run repeats exactly, with no rule breaks.

The existing `tests/v2/agent_gym` tests still pass.

## Gaps and requests

* **Contract additions requested:** none are needed for this work. It would be cleaner with
  `Decision.segment` (already requested by agent_gym) and a documented shape for logged history
  sends, `attrs["logged_probability"]` with `offer_id` and `content_id` on SEND events. The reader
  accepts both with and without decision ids and falls back to `attrs["theme"]` when a content id
  is unknown.
* History segments are the customer's segment at the first decision, not at each past send
  (decisions in the history were not ours, so their segment is unknown).
* The per-person model, reading hours and calibration live in memory, like the rest of
  `AgentPolicy`'s learning state; the runtime has to persist them or rebuild them from the logs.
* mypy --strict was run locally against a 3.13 target only (numpy's stubs do not parse for 3.11
  here), and was clean apart from pandas stubs missing locally. CI on 3.11 has the final say.
* The agent picks the send hour itself and hands the policy a one-hot `active_hours`, and
  `_contacts_at_send` judges the cap at that hour. If the policy's optional
  `AutonomousPolicy(reading_hours=...)` (added by the send-time work in this stream) is switched on
  for the agent, it would choose the hour instead, and the cap check must then use that hour.
* Numbers are 1,000 customers and two seeds; CI's larger runs will move them by a few points.
