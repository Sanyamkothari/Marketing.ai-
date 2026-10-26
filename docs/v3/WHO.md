# Who to contact, and who to leave alone

Plan-v3 decision 1 ([`plan-v3.md`](../../plan-v3.md) section 4). Code:
[`autonomy/decide/slow_models.py`](../../autonomy/decide/slow_models.py) (leaving risk, 90-day value),
[`autonomy/decide/who.py`](../../autonomy/decide/who.py) (sleeping dogs by traits, the book the agent and the
bidders share), and a few lines in `bidders.py` and `runtime/agent.py`. Opt-in: the default agent is unchanged.
Tests: `tests/v3/who/`.

## What it is

| Piece | What it does | Where it shows |
|---|---|---|
| Leaving risk | Chance a customer who has been buying makes no order in the next 60 days | `CustomerState.scores["churn_risk"]` |
| 90-day value | Margin the customer brings in the next 90 days | `CustomerState.scores["clv_90d"]` |
| Urgency from risk | A bid is worth `1 + 0.5 x churn_risk` times as much: act before they lapse | `GoalBidder` urgency |
| Cost ceiling from value | An offer spending more than 25% of the larger of `clv_90d` and what it is expected to earn is not bid | `GoalBidder` options |
| Sleeping dogs by traits | A customer is also left alone when people like them credibly buy less after a message | `GoalBidder` leave-alone |

The models are numpy only (ridge logistic regressions and a ridge regression). The plan names v1's AutoGluon for
them; `autonomy/` cannot import it (the v2 CI job installs numpy, pandas and a few others), and a regularised linear
model on the 52-number profile is what the task asked for. The class boundaries (`LeavingRiskModel`,
`Clv90Model`, `SlowModels.fit` / `scores`) are where a stronger learner would go in.

## The slow models

**Rows.** Each row is one customer at one past month-end (UTC, the last microsecond of the month). Its features are
the customer profile at that instant (`autonomy.profile.training_rows`, the same 52 numbers the agent is fed, built
without the shop's price histogram so the agent's own events are enough: the two price features stay constant and
get weight 0). Its label is read from the window after the month-end.

- *Risk*: 1 if the customer made no order in the 60 days after, 0 if they did. Any order counts, whatever its value
  (a free or fully discounted order is an order, as it is in the profile). Only customers who ordered in the 180
  days before the month-end are asked ("about to stop" means they were buying).
- *Value*: margin in the 90 days after. The order's margin where reported; where not, the order's value read signed
  (as the profile reads it: a negative purchase takes money off) at the shop's margin rate as known at the end of
  that window (revenue while no order reports one); refunds subtracted; at least 0. Asked of every customer with an
  order. The rate is read at each window's end, so no training label reads a margin reported after the held-out
  month-end (tested).

**The value model has two parts**: the chance of any margin in the window (a ridge logistic regression) times the
margin of those with some (a ridge regression, at least the least such margin seen). One straight line through
everyone put customers likely to lapse below zero, clamped to exactly 0 (35 of 500 on `shop(500)`, measured in
review), a certainty no record gives. The attention price reads `clv_90d` (`ValueModel.future_value`), so an
unsubscribe from them was priced at nothing. With the two parts nobody is at 0 (below, tested).

**Point in time** (`test_slow_models.py`). Features come from `profile_for(events, month_end)`, which drops every
event after the month-end. Labels read only the window after it. The fit takes an `as_of` and drops every event after
it first: appending events from the future to the log does not change a single array or check (tested).

**Censoring.** A month-end whose window is not over at `as_of` (`month_end + window > as_of`) gets no label for that
model: it is left out, never counted as "stopped" or "worth nothing". The end of the data is `as_of`, not each
customer's own last event (a customer whose last event is old is exactly the one who left). The rows left out are
counted in `TrainingSet.censored_risk` and `censored_value`. Tested: a customer with one January order has no row on
25 March, a "stopped" row on 2 April (60-day window over), and a value of 0 only from 2 May (90-day window over).

**The held-out check.** The last month-end with a closed window is held out. Training uses the rows whose own window
closed by the held-out month-end (the purge, so no training label looks past the moment the held-out rows were taken).
Risk reports the AUC, value reports the root-mean-square error against the error of guessing the training mean
(`skill = 1 - model / guess`). A model is **used only when** it passes its bar (`SlowModelConfig`): AUC at least 0.60,
value skill at least 5%, at least 150 training and 40 held-out rows, at least 10 customers of each kind. A model that
passes is refit on every closed row. A model that fails writes no score, and its check says why
(`SlowModels.describe()`: "not used ... below the bar of 5% better than the mean", "too few rows (71 to train, 108
held out)", "fewer than two month-ends have a closed window").

**Measured, on the test fixtures** (synthetic: customers who order every 20 to 40 days, 40% of whom slow down and stop;
no claim to resemble a shop). Command `python -m tests.v3.who.probe` (`tests/v3/who/probe.py`), output:

> [slow models, shop(500)] Leaving risk is used: held-out AUC 0.726 on 489 customers of the month ending 2026-04-30,
> trained on 2631 earlier rows. 90-day value is used: held-out error 59.70 against 69.12 for guessing the mean (skill
> 13.6%) on 497 customers of the month ending 2026-03-31, trained on 1661 earlier rows. (fit 1.04 s)
>
> clv_90d: 500 scored, 0 at exactly 0, lowest 0.13; lapsed (no risk score): 34, lowest 0.13, highest 11.51

The fit time is one run on a shared, loaded machine. The single-line value model gave skill 12.2% on the same fit
(reproduced in review). **No real-data figure exists for these models yet**: the order-log fixture and the real bench
are not part of this stream (see "Not measured").

## Who is scored, and when they are refit

`WhoBook` (one per agent, shared by its bidders) keeps every event the agent is handed (`nightly` calls
`book.ingest`; a repeated `event_id` counts once) and, once a calendar day, on the first agent-group decision
(`book.refresh`):

- refits both slow models if a month-end's window has closed since the last fit (so at most monthly: the training rows
  only change then);
- refits the trait groups if the harm model has been refit and `trait_refit_days` (7) have passed, and drops them when
  the harm model reads nobody any more (too few customers).

For each agent-group decision `book.prepare(state, now)` returns the changes to the state the decision is made on:

- `scores`: the state's own plus `churn_risk` (customers with an order in the last 180 days) and `clv_90d` (customers
  with an order), each only if its model passed its check;
- `features` and `features_as_of`, only if the state carries no nightly profile, built from the events held
  (`WhoConfig.fill_profile`). This is what puts the profile in front of the look-alike model in the Gym, where the
  folder does not build one. Nightly features no older than 3 days, and not from after `now`, are used as they are;
  otherwise the profile is built from the events held, and with none held the customer gets no profile (tested).

This is cached per customer per day. `explain_bids` prepares the state the same way but stores nothing
(`prepare(..., remember=False)`) and refits nothing, so explaining does not change the next decision (tested).

## What the bidders do with the scores

- **Urgency** (`GoalBidder._quote`): the goal's urgency is multiplied by `1 + risk_urgency x churn_risk`
  (`risk_urgency` 0.5; no score, no change). The whole expected net scales with urgency, as the cart fade does, so a
  message that loses money for a customer likely to lapse loses more: "high risk + positive uplift = act now" holds,
  and high risk does not rescue a bad bid.
- **Cost ceiling**: an option **with an offer** whose expected spend (offer cost x chance of redeeming + channel and
  format cost) is more than `value_cost_share` (25%) of the larger of the customer's `clv_90d` and what the option is
  expected to earn (uplift x value if converted) is not bid (`WhoBook.cost_ceiling`). No `clv_90d`, no ceiling. An
  option without an offer is never stopped by it: its channel cost is already in its net. `clv_90d` is what the
  customer brings *without* a message, so capping by it alone would bar the very people a win-back is for. A customer
  valued at 0 can still be sent a plain message, and an offer for them must earn at least four times what it spends
  (tested: `test_a_customer_valued_at_nothing_can_still_be_sent_a_plain_message`).
- `clv_90d` also reaches code that already read it: `ValueModel.future_value` and `ShrunkValueModel.future_value`,
  which the attention price uses (what an unsubscribe costs). So with the switch on, the attention price of a customer
  with a value score is priced on that score instead of two orders' margin. That is the plan's "bidders already read
  it", and it is part of what the comparison below measures.

## Sleeping dogs by traits

`ContactHarm` (v2) reads each customer's own buying around messages, shrunk towards neighbours along three clues
(click rate, direct visits, unsubscribes). A customer with a thin record is left alone only if those neighbours say so.
`TraitHarm` adds a second neighbourhood, customers who share a few **profile traits**:

| Trait | Cut |
|---|---|
| Answers messages | never clicked or replied / clicks less than the median engaged customer / more |
| Needs a deal | `price_offer_sensitivity` at or above 0.5 / below |
| Heavily contacted | 5 or more messages in the last 30 days / fewer |

(3 x 2 x 2 = up to 12 groups; the click-rate cut is the median of the engaged customers at each refit. The plan's
example is "discount-only buyers who got 5+ messages last month".)

1. Every customer's record is the one `ContactHarm` keeps (`ContactHarm.own_records()`: purchases in a message's week,
   purchases, the week's share of their time) and is read on the same ratio grid: buying in a message's week as a
   multiple of buying without one.
2. A group's reading is the sum of its members' log-likelihoods, with a Normal prior on the log ratio centred on the
   business's ratio, whose spread is how far groups really differ beyond their own noise
   (`_spread_between`, the DerSimonian-Laird estimate `ContactHarm` uses for its bands). A group that looks low by
   chance is shrunk back; many groups are read every week.
3. A member is read against their group **with themselves left out**, spread by the person-to-person `tau`, then
   against their own record. A thin record stays near the group; a long one is mostly the person. `tau` is read **per
   group**: a group can have a low average because a fifth of it are dogs while the rest are helped, and then its
   people differ widely and the helped members must not be left alone on the average. Every candidate in `HARM_TAUS`
   is weighed by how likely the group's records make it (flat prior), so a spread that thin records cannot rule out
   is not ruled out.
4. A customer is left alone when `LEAVE_ALONE_CREDIBLE` (0.9) of that posterior is below a ratio of 1 and something is
   at stake. A customer with **no record** is read as one more person of their group: the group's reading spread by
   its `tau` (not the group's average alone, which would be surer about a stranger than about a member). A group with
   fewer than 20 customers with a record is not read.
5. **A customer left alone keeps the group they were left alone in** (`WhoBook._frozen`) until a refit no longer
   leaves them alone under it. The load trait is the agent's own doing: once a heavily contacted dog is left alone
   their 30-day load falls, and keyed afresh they would join the lightly contacted, be let back in, turn heavy again,
   and so on. Tested (`test_a_dog_left_alone_stays_left_alone_when_its_own_messages_stop`): 40 days on, a dog's fresh
   key is `silent|deals|light`, and the planted dogs stay left alone.
6. When `ContactHarm` stops reading anyone (too few customers), the trait reading is dropped too (tested).
7. It is added to `leave_alone` with "or"; the old rule is untouched. The weekly retry (`leave_alone_retry`) lets a
   left-alone customer back in about one week a year, so a wrong group verdict produces new evidence.

**Tested** in `test_trait_harm.py` (synthetic records: 60 planted dogs with ratio 0.4 and 4 purchases on average,
60 neutral with ratio 1.0, 370 customers of three other groups with ratios 1.1 to 1.3) and in `test_book.py` (the
same kind of population, keyed by the book itself from the customers' own events, `refresh` and `prepare`, and read
through the bidder):

| Check | Result |
|---|---|
| Dogs left alone | 78% (47 of 60) |
| Neutral group left alone | 0% (0 of 60) |
| The dogs left alone on their own thin record alone (flat prior, same 0.9 bar) | 32% |
| Planted dogs' group, read | 60 customers, ratio 0.57 (shrunk from the planted 0.4 towards the business's), chance below 1: 1.00, tau 0.38 |
| A newcomer with the dogs' traits / with the neutral group's | chance below 1: 0.92 (left alone) / 0.52 (not) |
| A mixed group of 300: 60 dogs (ratio 0.3), 240 helped (1.1) | group ratio 0.84, chance below 1 0.9995, tau 0.37; 5 of 240 helped members left alone (2%), 8 of 60 dogs; a newcomer 0.72 (not left alone) |
| A dog-group member whose 120-purchase record shows no harm | not left alone |
| A neutral-group member whose 120-purchase record shows harm | left alone |
| A dog-group member as their record grows from 1 to 64 purchases (neutral person) | posterior moves from the group's to the person's |
| 12 groups of 60 with the same ratio (1.3), 4 seeds | nobody left alone, no group read as harmed |
| Group of 8, or fewer than 50 customers | not read |

The figures in the first six rows are from `python -m tests.v3.who.probe`; the tests assert at least 70% of the dogs,
at most 5% of the neutral group and at most 5% of the mixed group's helped members. Before the per-group `tau` (one
pooled `tau`, and a newcomer read by the group's average) the review measured, on the same populations, 100% of the
dogs, and in the mixed group 21% of the helped members and every newcomer left alone. The per-group spread costs
some planted dogs whose group is all alike: thin records cannot show that its people do not differ.

## Switching it on

| Where | Default | On |
|---|---|---|
| `build_agent_policy(..., who=...)` | `False`: no book, bidders ignore the scores, every number unchanged | `True`, a `WhoConfig`, or a shared `WhoBook` |
| Gym CLI `--policy` | `agent` | `agent_who` (look-alike + profile + these models) |
| Real bench | the agent | `python -m tests.v3.who.bench_run ...` |

`WhoConfig` has an off value for each piece: `models=False` (scores are not written), `risk_urgency=0`,
`value_cost_share=None`, `trait_harm=False`, `fill_profile=False`. With all of them off the bids are identical
(`test_a_book_with_everything_off_is_the_same_as_no_book`); without a book scores in the state change nothing
(`test_without_a_book_scores_in_the_state_change_nothing`); the default Gym run is equal to one with `who=False`
(`test_the_default_gym_run_is_the_same_with_the_switch_spelled_off`).

`agent_who` is `build_agent_policy(brief, content, lookalike=True, who=True)`. The Gym diagnostics script
(`tests/v3/who/gym_run.py`) runs the Gym with the same factory and prints what each agent learned.

### Limits of this stream

- **Not wired into the runtime.** `RuntimeSettings` and `build_runtime` are not edited; only `build_agent_policy` takes
  the switch. The book keeps the agent's events in memory (the events of every customer, for as long as the process
  runs), which suits the Gym and a pilot, not a million customers. In the runtime the nightly profile
  (`RuntimeConfig.nightly_profile`) already supplies `features`; the slow models would then be fitted from the
  Data Doctor's month-end rows and written to `scores` by the nightly job. That wiring is left.
- **No Data Doctor rows.** The plan trains on the order-log path's month-end rows with its approved outcome sentence,
  time-based split and leak probe. Those are not on this base, so the rows are built from the agent's events with
  `training_rows`; the time-based split, purge and censoring are in this module and tested, the approved outcome
  sentence and the leak probe are not.
- The trait cuts (above) are fixed, chosen before any data, not tuned on the Gym. Which profile traits mark sleeping
  dogs in a real shop is for the bench to say.
- The agent refits the models monthly, not weekly (the rows change at month-ends).

## Smoke run

One run, to see that it works end to end (not a comparison): 1 seed, 200 customers, 90 decision days, the scenario's
default 180 days of history, no comparison policies. It is the Gym CLI with the `agent_who` factory, plus the report
of what the agent learned:

```
python -m tests.v3.who.gym_run --scenario sleeping_dogs_15 --policy agent_who --seeds 1 --customers 200 --no-compare
```

- It finished in 27.6 s (the Gym's own timer, on a shared machine) with 0 rule breaks.
- True value $1,517 (the oracle's $3,378); 1.39 contacts per customer per week; 6.3% unsubscribed.
- It contacted 19 of 20 planted sleeping dogs (95%). That is no better than v2's 95.2% (`docs/v2/CLICK_HARM.md`), as
  expected at this size (below).
- This says nothing about whether `agent_who` beats `agent`: no `agent` run was made here.

What the agent had learned by the end of that run (the same command's report):

- Leaving risk: used. Held-out AUC 0.610 on 129 customers of the month ending 2026-01-31, trained on 347 rows.
- 90-day value: not used. Held-out error 39.26 against 34.04 for guessing the mean (skill -15.3%) on 122 customers,
  trained on 150 rows, below the bar of 5%.
- The own-record harm model: "No sign that messages put anyone off as a group: across 200 customers, buying after a
  message runs at 1.77 times their own." The trait groups: 8 read from 149 customers with a record, the largest of 51,
  all around a ratio of 1.7, none credibly below 1 (a newcomer's chance below 1 at most 0.05); nobody left alone on
  them.
- On the last decision day the book prepared 159 customers, all with a profile, 119 with `churn_risk`, none with
  `clv_90d`, all with a trait group.
- The look-alike model had 4,230 rows, was in use and refused none, with the filled profile as its features.

So in a 200-customer shop the value model does not clear its bar and trait pooling finds nothing to pool; leaving
risk passes just above its bar. The held-out gate doing its job is the expected outcome here.

## The comparison: pending CI

`agent`, `agent_lookalike` and `agent_who` on the same seeds. **Pending CI: nothing below has been run.**

```
for s in baseline_shop sleeping_dogs_15 discount_cannibalisation send_time_matters whatsapp_fatigue null_effect; do
  python -m autonomy.gym.run --scenario $s --policy agent           --seeds 5 --customers 2000 --report-json agent_$s.json
  python -m autonomy.gym.run --scenario $s --policy agent_lookalike --seeds 5 --customers 2000 --report-json lookalike_$s.json
  python -m autonomy.gym.run --scenario $s --policy agent_who       --seeds 5 --customers 2000 --report-json who_$s.json
done
python -m tests.v3.who.gym_run --scenario sleeping_dogs_15 --policy agent_who --seeds 2 --customers 2000   # what it learned
```

Read: true value, share of the segment oracle, contacts to sleeping dogs, rule breaks, and the pass bar of
[`docs/v2/GYM_BAR.md`](../v2/GYM_BAR.md). The plan's pass test for this decision: `sleeping_dogs_15` contacts under 50%
of the planted sleeping dogs (v2: 95.2%), in scenarios where `sleeping_dog_signal` makes them observable
(`sleeping_dogs_hidden` has signal 0 and cannot meet it). `null_effect` must still lose at most the stated amount.
Also read from `gym_run`: whether the two models cleared their bars at 2,000 customers, and the trait groups.

The real bench (MegaFon and Criteo: beat the best simple policy with the corrected lower bound above 0; Hillstrom and
X5: at least tie), with the same wrapper pattern as the look-alike bench:

```
python -m tests.v3.lookalike.bench_run --dataset megafon --data-dir DIR --json lookalike_megafon.json
python -m tests.v3.who.bench_run       --dataset megafon --data-dir DIR --json who_megafon.json
```

(and `criteo`, `hillstrom`, `x5`). The datasets are not in this environment.

**What the bench can measure of decision 1.** The bench hands `nightly` only the TRAIN customers' history, and its
TEST states carry no nightly profile, so on its own the book would hold no event of the customers it decides for: no
profile, no score, no trait group, and the "who" scoreboard would repeat the look-alike one. The wrapper therefore
also hands the book every customer's pre-campaign events (the `pre` the bench folds the TEST states from: before the
campaign, so point in time); TEST customers' campaign events are never handed over. It prints the book's coverage
(customers prepared, with a profile, with each score, with a trait group) after the run: read it before reading the
scoreboard. The wrapper has not been run here (no datasets), and the book then holds the pre-campaign events of every
customer in memory, which on Criteo is large.

## Not measured

- Any real-data figure for leaving risk or 90-day value, and the signal test of the profile against v2's state.
- Whether the trait groups find the Gym's planted dogs. The planted-group tests show the pooling can; the Gym at 200
  customers has too few records to show anything.
- The cost of the book's profile building at scale. Per decision it builds one profile from the customer's events
  (about 70 microseconds on the profile bench's synthetic events, `docs/v3/PROFILE.md`), once a day per customer.
- The tests: `tests/v3/who` (the slow one, `test_the_who_agent_runs_in_the_gym_reads_the_history_and_repeats`, is
  marked `slow`):

```
python -m pytest tests/v3/who -m "not slow"
python -m pytest tests/v3/who -m slow
python -m pytest tests/v2/dogs_storm tests/v2/click_harm tests/v2/decide/test_bidders.py tests/v2/contact_less/test_discounts_dogs.py tests/v3/lookalike/test_agent.py -m "not slow"
```
