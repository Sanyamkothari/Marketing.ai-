# Live experiments: the agent tests itself

Plan-v3 foundation C ([`plan-v3.md`](../../plan-v3.md) section 3.C), with the limits fixed in decision V-3
of [`DECISIONS.md`](DECISIONS.md): at most 10% of the agent group in experiments at once, at most 2
experiments per goal. Code: [`autonomy/experiments/`](../../autonomy/experiments/), the question menu in
[`autonomy/scientist/questions.py`](../../autonomy/scientist/questions.py), the Gym scenario
`better_offer_planted` in [`autonomy/gym/scenarios.py`](../../autonomy/gym/scenarios.py). Tests:
[`tests/v3/experiments/`](../../tests/v3/experiments/).

Every number below comes from a command named next to it. What was not run is listed at the end.

## What it does, in plain words

The Scientist picks one question from a fixed menu, for example "which earns more profit per customer for
lapsed customers, 10% off or free shipping?". A small random group of the agent's customers (never the
holdout) is split between the answers. The agent works as usual for them, except that an assigned customer
can only get their arm's offer. Each week the test is read against the holdout. When one offer has clearly
beaten both the current default and doing nothing, it becomes the new default, inside the approved
envelope, with a reason in the log and a one-click undo. If an arm is clearly losing money the test stops.
The Analyst gets one sentence.

## The pieces

| Piece | File | What it is |
|---|---|---|
| Question menu | `scientist/questions.py` | `offer` or `channel`, two to four arms, every arm already inside the approved envelope, no offer that needs a person's approval |
| Experiment record | `experiments/model.py` | question, arms, reference arm (today's default), audience, share, **pick rule**, **stopping rule**, registration time and digest |
| Book | `experiments/book.py` | the register: limits, the append-only log, review, promotion, undo, `snapshot()` / `restore()` as plain data |
| Assignment | `experiments/assign.py` | a hash of (experiment id, customer id) |
| Policy wrapper | `experiments/policy.py` | `ExperimentPolicy(inner, book)` |
| Intervals | `experiments/sequential.py` | always-valid intervals (mixture SPRT) |
| Reading | `experiments/reading.py` | per-arm gain over the holdout and the reference; `judge()` |
| Briefing line | `experiments/summary.py` | one sentence plus a `BriefingSection` whose numbers it quotes |
| Gym harness | `experiments/gymrun.py` | the wrapped agent in `better_offer_planted`, read weekly |

```python
book = ExperimentBook()
book.start(spec, approved_brief, now)             # registers the rules, then starts; may raise ExperimentRefused
agent = ExperimentPolicy(build_agent_policy(...), book)   # use it wherever the agent policy is used
...
review = book.review(experiment_id, collect_outcomes(exp, decisions, last_touch_rewards, now), now)
if review.experiment.end_kind is EndKind.WINNER:
    book.promote(experiment_id, approved_brief, now)       # may raise PromotionRefused; book.revert(...) undoes it
line = summary_line(review.experiment, review.reading)    # for the Analyst's briefing
```

## Pre-registration

An experiment is an `ExperimentSpec` (frozen) plus what happened to it. `ExperimentBook.start` stamps it with
the registration time and the SHA-256 of the spec and of the rule text, and puts `registered` (with the
digest and the rules in words) as the first line of the log before `started`. `ExperimentPolicy` only
assigns customers to experiments that are running in the book, so no send can come before the record. The
policy also stores the time of the first assignment (`first_assignment_at`); a test checks
`registered_at < first_assignment_at`, that the digest still matches and that the log opens with
`registered`, `started`. `Experiment.digest_matches()` is False if the spec or its rule text is changed
afterwards, and `review` and `promote` refuse such an experiment.

The rule text for the default spec reads (from `rule_text`): a challenger wins when, with enough measured
customers in every arm and in the holdout, it earns more net profit per customer than the holdout and more
than the reference by over the minimum gain, each shown by an always-valid interval at overall error 0.05
split over the comparisons; if none does, the reference stays the default; the test also ends when an arm is
clearly losing, or after `max_days`.

## The limits (V-3)

`ExperimentBook` refuses, with a plain sentence that is also logged as `refused`:

- a third running experiment for one goal (`MAX_PER_GOAL = 2`);
- shares that sum past 10% (`MAX_TOTAL_SHARE = 0.10`). Each experiment's `share` is a share of its own
  audience, which is at most the whole agent group, so capping the sum caps the share of the agent group.
  A single spec cannot ask for more than 10% (it fails validation);
- a brief that is not approved or belongs to another goal, arms outside the envelope, an offer that needs
  approval, an id already used, and a spec whose reference is not the goal's promoted default.

A book may be made stricter than these limits, never looser, and `snapshot()` / `restore()` carry its limits
(tested: a book with 1 per goal and 5% stays that strict after a round trip). When an experiment ends it frees
its place.

## Assignment

`experiment_score(experiment_id, customer_id)` is `autonomy.proof.split.split_score` with the experiment id
as the salt, a number in [0, 1) that is the same on every run. A customer is in the test when the score is
below `share`; the arm is that band cut into equal parts, so given participation each arm has probability
exactly 1/k.

- Only the AGENT group is ever assigned. Holdout and baseline customers are never assigned, whatever their hash.
- The decision must fall inside the experiment's window: at or after `registered_at` and before
  `registered_at + max_days` (`assign.in_window`). A decision timestamped before registration (a replay, a
  backfill, a clock behind the book's) is not assigned, so `first_assignment_at` can never come before the
  registered rules; an experiment whose weekly review did not run stops restricting anyone at `max_days`.
- The customer must match the experiment's audience (`autonomy.decide.audience.matches`) at the decision,
  and the audience of the goal's brief in the decision context: a customer the goal never targets is never
  restricted for it. An experiment whose goal has no brief in the context assigns nobody.
- With several experiments running, a customer is in at most one: the earliest started whose hash admits
  them. Whether an earlier test admitted a customer depends only on that test's own hash, so the arms of a
  later test are still a fair random split of whoever is left.
- The matched holdout is read like for like (`assign.holdout_experiments`): a holdout customer is tagged for
  every eligible experiment up to and including the first whose hash band admits them. A holdout customer
  the first experiment's band would take is therefore not in a later experiment's holdout, exactly as an
  agent customer in that band is not in the later experiment's arms.

On 30,000 synthetic ids with a 10% share and three arms, participation and arm shares come out within the
tolerances asserted in `test_assign.py::test_share_and_arms_come_out_as_planned` (passes).

## `ExperimentPolicy`: what an arm changes, and what it cannot

The arm **restricts the options** and nothing else. The wrapper hands the inner policy a
`DecisionContext` whose content library keeps only the items of the arm's offer (or channel) for the
experiment's goal. Only that goal's view is cut: content shared by all goals (`goal_id` None) that falls
outside the arm is handed to each other goal of the brief list as that goal's own copy, so the customer's
other goals see the library they always see (tested: 6 shared items cut for the free-shipping arm leave the 2
shared free-shipping items plus 4 copies for the other goal, and the other goal's decision and probability are
exactly what the unwrapped policy gives). An offer arm forces its offer, "no offer" included in the cut
(unless "none" is itself an arm): the agent's evidence gate defaults to no offer until an offer has shown what
it adds, so an arm that could fall back to a plain message would measure that gate, not the offer. The inner
policy then does everything it always does with what is left: hard rules (consent,
unsubscribes, quiet hours, caps, the holdout, the envelope), pricing, exploration, send time. The wrapper
never sends and never lets through what the inner policy blocked. Tests show an unsubscribed or
non-consenting customer in an arm is still left alone, and the holdout still gets "do nothing" with probability 1.

Why the library and not the brief's envelope: `AgentPolicy` caches the live briefs by (brief id, version),
so a per-arm envelope with the same id would be served from the first arm's cache. The library is read
fresh on every decision. If a policy ignores the restricted library and returns an action outside the arm,
the wrapper turns it into "do nothing" (`experiment_arm` in `blocked_by`) and counts it in
`stats["outside_arm"]`; the send the inner policy had scheduled is withdrawn (`forget_pending`, on the
inner policy or on its `.policy`) so its weekly cap and minimum gap do not count it. Such a row keeps the
probability of the discarded draw, a lower bound on the chance of "do nothing", and should be left out of
off-policy reads. The Gym smoke run below had 0 of these with the real agent.

**The logged probability** for an assigned customer is the inner policy's own probability of its choice
among the options left in the arm, unchanged. The arm is a fixed function of (experiment id, customer id): for
this customer at this moment it is context, like the holdout split, not randomness; the only random step is
the inner policy's draw, which already counts its exploration floor. So the probability is right for every
decision, a "do nothing" and another goal's action included, and for both of its readers: `autonomy.proof.ope`
(weight = target / logged) and the agent's own learner, which weights agent "do nothing" rows by
1 / probability (`autonomy/learning/observations.py`, `autonomy/learning/update.py`). Tests: a customer nobody
may contact gets "do nothing" with probability 1 on either arm; with an exploration floor of 0.4 the logged
probability equals the inner policy's on the arm's library; another goal's decision inside an arm is exactly
the unwrapped one; and an off-policy read of a log mixing sends and "do nothing" rows has every probability in
(0, 1] and, for the logging policy itself, weights of 1 and the logged mean back.

**Consequence for off-policy reads (positivity).** On an experiment row an option outside the customer's arm
had probability 0. A target policy that would pick such an option is not supported on those rows, the
positivity assumption in `autonomy.proof.ope`. The arms are compared by the reading below, the randomised
comparison, not by off-policy evaluation. (An earlier version multiplied the probability by 1/k; that is the
mixture probability only for an option that exists in exactly one arm and was about k times too small for a
"do nothing" or another goal's action, so it was dropped.)

**The log reads itself.** The decision's `policy_version` gains `|exp:<id>:<arm>`. Holdout customers of the
matched holdout gain `|exp:<id>:holdout` (their decision is still "do nothing", probability 1). Baseline customers
are not tagged. `collect_outcomes` rebuilds arms and the matched holdout from the decision log and the closed
rewards alone.

**Promoted defaults.** With `apply_defaults` on (the default) and a promotion in the book, a customer outside
any test who matches the audience the experiment ran on (`Promotion.audience`) gets a library cut to the
default offer and "no offer" (an offer default), or to the default channel (a channel default). At every
decision the default is checked again against the goal's brief in the context: it is skipped, and counted in
`stats["default_skipped"]`, when the goal has no brief there, when the value is no longer inside that brief's
envelope (`check_arm`), or, for a channel, when the customer cannot be reached on it or the inner policy's
breaker has paused the goal or that channel of it. Tests: an offer default is skipped once the marketer
withdraws the offer (the other approved offers come back, not only "no offer"); a channel default is skipped
when email leaves the envelope or the breaker pauses it, and the goal falls back to WhatsApp; a default tested
on big spenders is not applied to a small one. Undoing the promotion puts the agent back as it was (tested).
With no running experiment and no promotion `decide` returns exactly what the inner policy returns (tested on
50 customers).

**Why the promoted default differs from the tested arm.** The arm forced its offer (no plain message) to
measure the offer; the default keeps "no offer" beside it, so the agent's evidence gate still chooses between
the default offer and a plain message per customer and never pushes an offer where a plain message is expected
to do better. The pick rule shows the winner beats the reference offer and doing nothing; it does not show it
beats a plain message unless "none" is one of the arms. To claim that, put "none" in the arms.

## Reading

The unit is the customer, as in `autonomy.proof.estimate`: each customer's reward values are summed (build the
rewards with `proof_rewards()`, last-touch, which counts every purchase once), and arms are compared per
customer. A customer belongs to the arm of their first decision in the experiment (intention to treat). The
reference group is the **matched holdout**: holdout customers who match the audience, a random sample of it,
never contacted.

For each arm the reading carries:

- the gain over the holdout with the plain 95% interval from `autonomy.proof.welch_difference` (what the proof
  code says at a single look; shown for reference), and
- the same gain with an always-valid interval, plus, for a challenger, the always-valid gain over the reference.

**Only the always-valid intervals make decisions.**

### Which conservative rule keeps the error honest under repeated looks

The normal-mixture sequential probability ratio test (mSPRT: Robbins 1970; Johari, Koomen, Pekelis and Walsh
2017). Full derivation in the docstring of `sequential.py`. In short, with `V` the variance of the estimated
difference and `tau` the registered mixture scale (`StoppingRule.prior_sd`), the interval is `d +/- h` with

    h = sqrt( (2 V (V + tau^2) / tau^2) x ( ln(1/alpha) + 0.5 ln((V + tau^2) / V) ) )

By Ville's inequality the chance that it ever excludes the true difference, at any number of looks, is at most
`alpha`. `alpha` is split by Bonferroni over the `2k - 1` comparisons of one look (each arm against the holdout,
each challenger against the reference), which is valid whatever their dependence.

Measured with the test `test_sequential.py::test_peeking_every_fifty_customers_keeps_the_false_alarm_rate_near_alpha`
(no true difference, 400 runs, a look after every 50 customers per arm up to 2,000, 40 looks, `tau = 0.3`):
the plain 95% interval excluded zero at some look in **30.5%** of runs; the always-valid interval in **2.75%**
(bar: at most 5%).

**The price is width.** On unit-variance data with 200 to 20,000 customers per arm, `half_width` is 1.55 to 1.9 times
the single-look half-width when `tau` is within a few times the final standard error, and up to 6.4 times at 200
customers with a `tau` of 0.02 (far too small). A wrong `tau` costs width, never the error rate.

**Honest limits.** The guarantee is exact for a Gaussian random walk with known variance. Here the variance is
estimated and customer profit is skewed (most customers buy nothing), so it holds asymptotically, by the central
limit theorem, as it does on every platform that uses this test. That is why no call is made before
`min_per_arm` / `min_holdout` customers are measured (default 200 each), and why alpha is shared out.
At small samples with rare, large purchases, treat the intervals as approximate.

### The calls (`judge`), in order

1. Fewer than `min_per_arm` measured customers in an arm or `min_holdout` in the holdout: continue (or, at
   `max_days`, end with no winner).
2. A challenger is above zero against the holdout **and** above `min_gain` against the reference: it wins; the
   best lower bound against the reference if two qualify. (Ends at once if `stop_for_win`, else at the end.)
3. An arm is **clearly losing** and `stop_for_loss` is on: the upper end of its interval is below zero against
   the holdout (it loses money compared with doing nothing), or, for a challenger, against the reference.
   The test stops and nothing is promoted. When the losing arm is the reference itself (today's default loses
   money against doing nothing), the reason and the briefing line say so and ask for a person's review
   instead of "the default stays".
4. `max_days` reached: no winner, the default stays. Otherwise continue.

Any clearly losing arm ends the whole test, a challenger included, even when another challenger might still
be winning (with three or four arms). Dropping only the losing challenger and carrying on would be fair under
the always-valid intervals, but it changes the registered stopping rule and the arm bands, so it is not done
here; it is a candidate for a later version of the rule.

Tests (synthetic data, seconds): a planted clear winner is found with both intervals above zero and the plain
proof interval agreeing; across **200 simulated nulls, each read weekly for four weeks, no winner is ever called**;
nothing is called at 40 customers per arm; time up without a winner keeps the default; a reference that loses
money against the holdout, and a challenger clearly worse than the default, each stop the test; an ended
experiment does not change on another look.

## Promotion and undo

`ExperimentBook.promote(experiment_id, brief, now, reason)` makes the winner the default of the goal:

- only an experiment that ended with a winner, not already promoted, whose digest still matches;
- only with the goal's approved brief **as it stands now**: the winner must still be inside its envelope (a
  winner whose offer the marketer has withdrawn meanwhile is refused);
- logged as `promoted` with the reason (the judgement's sentence unless one is given) and the default it replaces;
- recorded with the audience the experiment ran on (`Promotion.audience`): the default is applied to those
  customers only, since nothing was measured on anyone else.

`ExperimentBook.revert(promotion_id, now, reason)` undoes it: the goal returns to the default before it (or to none),
`reverted` is logged with the reason, and undoing twice is harmless. A promotion never edits the envelope: it is a
preference inside it that `ExperimentPolicy` applies, and the next experiment for the goal must use the promoted
default as its reference arm.

## For the Analyst's briefing

`summary_line(experiment, reading)` returns one sentence. The output for the planted winner of
`test_reading.py::test_the_summary_is_one_sentence_whose_numbers_are_in_the_briefing_section` (synthetic data,
seed 3, 600 customers per arm and 600 held out):

> Free shipping beat 10% off for lapsed customers: $1.31 more profit per customer (range $0.73 to $1.89) and
> $1.51 more than doing nothing, across 1,200 customers in 14 days. It is now the default offer.

`briefing_section(...)` returns the same sentence as a `BriefingSection` whose `numbers` contain every figure it
quotes (`gain_vs_default`, its range, `gain_vs_nothing`, `customers_measured`, `days`), so the Analyst quotes and
never computes. Variants: still running, no clear winner (the default stays), stopped because an arm was
clearly losing (the default stays, or, when the default itself was losing, it needs a person's review), stopped
by a person.

## The Gym: `better_offer_planted`

The scenario is the last entry of the registry. Free shipping multiplies a message's effect by 3.0 for every
persona; 10% off multiplies it by 1.0 (nothing beyond the message itself) and still costs its redemption.
Check of the planted truth, `python -m autonomy.experiments.gymrun --truth --customers 400` (`planted_truth`:
the world's `PlantedTruth.expected_net`, 400 customers, 14 days, 60 days of history, seed 0, email, mean over
customers of the true extra profit of one message with the offer; no policy is run, a few seconds): free
shipping **0.818**, no offer **0.363**, 10% off **-0.004**; free shipping is worth more than 10% off for
**87.5%** of customers. `test_gym_scenario.py` calls the same `planted_truth` on 300 customers and asserts the
ordering.

`python -m autonomy.experiments.gymrun --seeds 10 --customers 5000` runs the plan's pass test: the agent
wrapped in `ExperimentPolicy`, one offer test registered on day 0 (10% off is the reference, free shipping the
challenger, share 10%, at least 100 measured customers per arm), read every seven days, the winner promoted.
The bar: the better offer found within 4 weeks on at least 9 of 10 seeds and the worse one never promoted. The
same bar is `test_the_better_offer_is_found_within_four_weeks_on_nine_of_ten_seeds` (`slow`, nightly).
**This bar has not been run** (see below).

## What was run here

After the review fixes (probability, per-goal restriction, defaults re-checked at decide time, audience on
promotions, decision window, like-for-like holdout, limits kept on restore):

- `pytest tests/v3/experiments -m "not slow"` plus the two v2 tests the new scenario had broken
  (`test_testing_gym_calibration.py::test_existing_scenarios_are_unchanged`,
  `test_testing_targets.py::test_the_gym_is_covered_per_scenario_and_stage`): **67 passed, 1 deselected in
  4.56s** (the `slow` test is the 10-seed bar). The scenario is in `EXPERIMENT_SCENARIOS`
  (`autonomy/gym/scenarios.py`): it has no v2 per-stage Gym targets in docs/v2/TARGETS.json, since its bar is
  this harness's.
- `pytest tests/v2/scientist/test_menu_select.py tests/v2/scientist/test_testing_targets.py
  tests/v2/scientist/test_testing_gym_calibration.py::test_existing_scenarios_are_unchanged
  tests/v2/gym/test_score.py -m "not slow"`: **78 passed in 6.87s**.
- Smoke run of the harness (`run_gym_experiment(0, n_customers=200, days=14, share=0.10, min_per_arm=30)`,
  1.7s): the wrapped agent acts inside its arms (`outside_arm` 0; 20 sends chosen under 10% off, 17 under free
  shipping), 22 matched holdout customers and 10 and 8 customers in the arms were measured, nothing was
  concluded (far too few customers), and the registered rules were unchanged. It says nothing about power.
- `ruff check`, `black --check` and `mypy --strict --python-version 3.11` on every file added or changed.

## What was not run, and what to watch

- **The 9-of-10-seeds bar is unmeasured.** It needs minutes per seed (5,000 customers, 29 days, the real agent),
  which the compute budget of this step did not allow; CI's nightly `slow` job will run it. Whether the default
  settings have the power to find a 3x offer inside four weeks is therefore not known. If the nightly run fails
  on power, the knobs are `share` (up to 10%), `min_per_arm`, `prior_sd` (1.0 per customer in the harness) and
  the Bonferroni split; a bar missed on "never promotes the worse one" would be a bug and should be reported.
- **The profit measure in the Gym harness** is each customer's purchase margin minus offer cost minus message
  cost since day 0, counted from the world's events as the Gym's own ledger counts them. Unsubscribe losses are
  not included. The production path uses `collect_outcomes` on last-touch rewards instead (tested on synthetic
  rewards, not in the Gym).
- **Not wired into the runtime.** `autonomy/runtime/service.py` is not edited (not in this stream's files). To
  go live, whoever owns it wraps the agent in `ExperimentPolicy`, persists `ExperimentBook.snapshot()`, and
  runs the weekly `review` + `promote` + `summary_line` in the Scientist's job and the Analyst's briefing.
- **Cart and reply triggers**: `decide(..., trigger=...)` is passed through and assigned like a sweep decision;
  the same customer keeps the same arm across triggers.
- Question kinds are `offer` and `channel`. Themes, send hours and formats are not on the menu yet.
- A customer whose audience membership flips during a test is assigned only while they match; reading uses
  the arm of their first tagged decision.
