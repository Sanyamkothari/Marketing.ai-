# Diagnose: which mechanism costs the money?

On the real bench the plain agent ties the best simple rule and agent_v3 loses; in the Gym agent_v3 loses 12-36% to
the plain agent in all nine money-making scenarios and contacts 30-45% fewer customers a week (STATUS.md,
ARCHITECTURE_REVIEW.md section 2). Nobody yet knows why. This page is the measuring kit that finds out. It builds no
fix and changes no default: it only names variants, runs them, and prints how they decided.

Nothing on this page has been measured yet. The only runs made while building it were a few 200-customer, 14-day Gym
smokes and synthetic-bench smokes (to check that the DIAG block prints), whose numbers mean nothing. Every
comparison below is **pending CI**, with the command that runs it.

## The questions

"Too many rules or too strict a prompt" is one hypothesis among several. Note that the agent decides with maths, not an
LLM: there is no prompt at decision time (ARCHITECTURE_REVIEW.md section 1 point 4 and section 6), so the strictness that can matter is in
the guards, the price and the plans. The variants separate these:

| # | Hypothesis | Which variants test it |
|---|---|---|
| H1 | One v3 part is what makes the preset collapse | `v3_no_<part>` against `agent_v3`: the variant that recovers the plain agent's value names the part |
| H2 | No part hurts alone; the harm is in how they combine (each adds a hold or a price on top of the last) | every `agent_<part>` ties `agent`, yet `agent_v3` loses, and no single `v3_no_<part>` recovers |
| H3 | The plain agent is held back by one of its own v2 guards (too many rules) | `agent_no_<guard>` and `agent_loose_caps` against `agent` |
| H4 | The plain agent's ceiling is the information, not the rules: relaxing every guard gains nothing, because customers look alike in the data | `agent_no_guards` against `agent`, and the bench datasets where the agent already ties the best simple rule |
| H5 | The method is wrong: the agent's own expected value per contact is far from what is realised, so its choices are not better than chance | the DIAG `figures` line: expected against realised value per action |

## The variants

Each is a thin factory over `build_agent_policy` (`autonomy/runtime/ablation.py`). `switches(policy)` reads the built
flags back, and the tests check that each variant differs from `agent` in exactly the switches named here. Hard safety
rules (consent, quiet hours, unsubscribes, holdout) are not a switch of any variant.

| Variant | Switch | Tests |
|---|---|---|
| `agent` | none | the baseline of every reading |
| `agent_v3` | `v3=True` | the preset that loses |
| `v3_no_lookalike` | `V3Preset(lookalike=False)` | the look-alike response model (and the profile in front of it) |
| `v3_no_who` | `V3Preset(who=False)` | leaving risk, 90-day value, trait-pooled leave-alone |
| `v3_no_frequency` | `V3Preset(frequency=False)` | 3-week contact plans (`frequency_plan` holds) and look-alike tiredness |
| `v3_no_moments` | `V3Preset(moments=False)` | moments |
| `v3_no_journeys` | `V3Preset(journeys=False)` | journeys (holds between steps, `never_read` endings) |
| `v3_no_offers` | `V3Preset(offers=False)` | the budget price on offers |
| `agent_lookalike` | `lookalike=True` | look-alike alone (existing Gym policy, now also in the bench) |
| `agent_who` | `lookalike=True, who=True` | who on top of look-alike (existing) |
| `agent_frequency` | `lookalike=True, frequency=True` | frequency on top of look-alike (existing) |
| `agent_moments` | `moments=MomentBook.load()` | moments alone |
| `agent_journeys` | `journeys=True` | journeys alone |
| `agent_offers` | `offer_pricing=DEFAULT_OFFER_PRICING` | offer pricing alone |
| `agent_no_restraint` | `restraint=False` | the lower-confidence-bound rule where lift is unproven |
| `agent_no_weekly_budget` | `weekly_budget=False` | each customer's own weekly rhythm (`ContactPrice.budget`) |
| `agent_no_explore` | `exploration_floor=0.0` | the random-rest and random-send floor (0 is legal for the policy; a brief's own floor is 0 to 0.5) |
| `agent_no_attention_price` | `attention_price=False` (new, opt-in) | the unsubscribe-and-complaint price in the auction: both `attention_price_fn` and `channel_price_fn` of `AutonomousPolicy` are left out, so a bid only has to beat zero |
| `agent_loose_caps` | brief's `max_contacts_per_week` x3 (`weekly_cap` in the switches) | the cap and everything sized by it: the hard rule, the weekly-budget rhythm menu, the attention price's horizon, the journeys' cap filter. Read it next to `agent_no_weekly_budget` |
| `agent_no_guards` | the four guards above together, cap unchanged | the ceiling of "fewer rules" (added beyond the brief: one extra job per matrix cell) |

Read these caveats before reading numbers:

- `agent_no_attention_price` takes the price out of the **auction** only. The weekly budget's rhythm
  (`ContactPrice.rhythm`) still prices messages inside, so it can still hold a customer. Compare with `agent_no_guards`
  for both gone.
- `agent_loose_caps` is not the hard cap alone. The agent reads the briefs' cap into `ContactPrice.cap()`, which sets
  the rhythms the weekly budget may choose (up to `cap` messages a week: the Gym brief's cap is 2, the variant's 6, as
  `test_loose_caps_raises_the_cap_and_what_the_agent_sizes_by_it` checks) and how far the attention price's lost value looks ahead; the
  journeys are filtered by the same cap (`rules.strictest_weekly_cap`). A gain here can come from any of these, so read
  it next to `agent_no_weekly_budget` (rhythm gone, cap kept) before crediting the hard rule.
- `agent_loose_caps` changes the **run's brief**, so in the Gym every policy of that run (the references too) sees the
  x3 cap, and its rule-break count is judged against it. In the bench the agent decides once per customer, so the cap
  rarely binds there; a null result on the bench is expected.
- `agent_moments` does nothing in a Gym run without `--moments` (the simple folder keeps no carts or views), so it ties
  `agent` there by construction. The diagnose workflow does not pass `--moments`, matching the STATUS.md runs.
- A "leave-one-out" variant is read against `agent_v3` in the same scenario and the same seeds, not against `agent`.

## Reading the DIAG block

Every Gym run (the policy under test, all seeds summed) and every bench run prints one block, each line starting
`DIAG `. From a local smoke (`python -m autonomy.gym.run --scenario baseline_shop --policy agent_journeys --seeds 1 --customers 200 --days 14 --no-compare`;
200 customers, so a format sample, not a result):

```
DIAG run gym scenario=baseline_shop policy=agent_journeys seeds=1 customers=200 days=14
DIAG switches differing from the plain agent: journeys=True
DIAG decisions total=2338 act=477 (20.4%) nothing=1861
DIAG nothing by reason blocked:weekly_cap=1023 no_bids:journey_never_read=515 journey_hold=133 blocked:no_consent=112 blocked:unsubscribed=43 journey_hold:weekly_cap=20 rest_draw=12 price_beats_value=2 explore_rest=1
DIAG blocked_by (any decision) weekly_cap=1043 no_consent=155 unsubscribed=43
DIAG holds and runs by part weekly_cap_hold_at_cap=908 journey_never_read_filter=515 journey_first_step=340 journey_hold=153 weekly_cap_hold_below_cap=135 journey_later_step=53 journey_ended:expired=6 journey_ended:purchased=2
DIAG part counters last_seed=0; journey_events={'started': 340, 'completed:completed': 323, 'sent': 53, 'deferred:weekly_cap': 20, 'stopped:expired': 6, 'stopped:purchased': 2}
DIAG figures mean_expected_net_chosen=0.8978 mean_attention_price_chosen=0.3345 contacts_per_customer_week=1.428 unsubscribe_rate=0.0240 true_value_per_action=0.6920 realised_incremental_per_action=0.6232 true_value_sum=330 scorecard_actions=477
```

Read it against the same command with `--policy agent` (the plain agent has no journeys, so it has no such hold);
so the 515 customers held by journeys' never-read filter are a lead to check at CI scale for the v3 collapse, not a
finding.

- **switches**: which flags differ from the plain agent (a leave-one-out lists the parts still on). `weekly_cap` is the
  cap the agent sized its budget and price by (`ContactPrice.cap()`); `weekly_contact_cap=x3` says the run's briefs were
  loosened too. Every line lists every code: none is cut.
- **decisions**: every decision for the agent group, `act` against `nothing`. The holdout and the baseline group are not
  counted. In the bench, one decision per test customer per seed.
- **nothing by reason**: why each "do nothing" happened, one code each. `blocked:<code>` is a hard rule or a policy hold
  (`autonomy/decide/rules.py`); `journey_hold` a customer waiting between journey steps; `journey_hold:<code>` a
  customer held mid-journey by that rule (`weekly_cap`, `quiet_hours`...), so a rule hold is not read as journeys';
  `experiment_arm` the experiments'
  wrapper; `rest_draw` the auction drew rest although a message was worth sending; `explore_rest` the exploration floor
  chose rest; `restraint_hold` restraint kept the best message out of the draw; `price_beats_value` the best message
  earns something but less than the attention price; `net_not_positive` no message earns more than its own cost;
  `no_bids` nothing reached the auction: the bidders offered nothing (no goal, or a customer left alone before the
  auction: who, harm, pruning). It is **not** who only. With journeys on, `JourneyRunner.expand` removes every bid on a
  channel where the customer has had `never_open_after_sends` (5) messages and no click or reply, and the decision then
  reads like a plain `no_bids`; the tally checks the customer's contactable channels and names it
  `no_bids:journey_never_read` when all of them are never read (no bid can have survived), or
  `no_bids:journey_never_read_some` when some are (the bids may have been on those). This is the "never read" hold for
  customers not yet in a journey; `journey_ended:never_read` is the same rule ending a journey already running.
- **blocked_by**: every code on any decision, so an ACT that lost an option to a rule counts too.
- **holds and runs by part**: where a decision records the part. `frequency_plan_hold`, `journey_hold` (every
  mid-journey hold, by a rule or not), `journey_first_step` / `journey_later_step`, `journey_ended:<code>`
  (`never_read`, `purchased`...), `journey_never_read_filter` / `journey_never_read_some_filter` (the `no_bids` above).
  `weekly_cap` is one code for two things: the briefs' hard cap and the agent's own weekly budget. They are split by
  whether the customer's contacts in 7 days had reached the cap (`weekly_cap_hold_at_cap`) or not
  (`weekly_cap_hold_below_cap`: their rhythm, or a send already scheduled but not yet on record).
- **part counters**: the parts' own counters, read after the run (the last seed in the Gym): customers the who book
  leaves alone, the frequency plans chosen, the journey event log, the offers' budget price. who, moments and offers
  leave nothing on the decision itself (a customer the who book leaves alone is a plain `no_bids`), which is why these
  exist.
  Moments have no counter: read them as `agent_moments` against `agent` in a `--moments` run.
- **figures**: `mean_expected_net_chosen` is the agent's own estimate for the actions it took, before the attention
  price. Gym: `true_value_per_action` (the world's truth, summed over seeds) and `realised_incremental_per_action`
  (agent group against holdout, noisy). Bench: `expected_net_per_contact` against `measured_net_per_contact` (the DR gain
  per contacted customer, with its interval) and their ratio; `contacts_per_customer_week` is the contact rate, as the
  bench decides once per customer.

## How to read the results

Read each variant's scoreboard (true value, with the 3 seeds in the table) against `agent` or `agent_v3` first; the DIAG
block says why. A gap inside the seeds' spread is not a finding.

- H1 holds if one `v3_no_X` recovers most of `agent`'s value and `agent_X` alone loses. Look at the DIAG of `agent_v3`
  for the part's own line: `journey_hold`, `no_bids:journey_never_read*` and `journey_ended:never_read` for journeys,
  `frequency_plan_hold` for frequency, plain `no_bids` rising together with `who_left_alone` for who (a plain `no_bids`
  can also be no goal or pruning), `offer_budget_price` above 0 for offers.
- H2 holds if all `agent_<part>` tie `agent`, all `v3_no_<part>` still lose, and `agent_v3`'s `act` share is far below
  `agent`'s. The holds then stack: compare the sums of the part lines.
- H3 holds if a guard variant beats `agent` outside the seeds' spread. The DIAG says which guard was binding: `blocked:weekly_cap`
  split at/below cap (budget against hard cap), `restraint_hold`, `rest_draw` plus `explore_rest`, `price_beats_value`.
  A gain from `agent_loose_caps` alone does not name the hard cap: it also loosens the rhythm and the price (caveats).
  A guard variant that contacts more but earns the same or less says the guard was doing its job.
- H4 holds if `agent_no_guards` ties `agent`, the contact rate rises and `true_value_per_action` falls. The rules were
  not the ceiling; look at the bench datasets where features carry no signal (Criteo, MegaFon).
- H5 holds if `mean_expected_net_chosen` is far above `true_value_per_action` (Gym) or the ratio is far from 1 (bench,
  the existing "within 2x" check): the agent mis-prices its own actions, which no switch here repairs.

## How to run

```
python -m autonomy.gym.run --scenario baseline_shop --policy v3_no_journeys --seeds 3 --customers 3000
python -m scripts.v2_realbench.run --dataset x5 --policy v3_no_journeys --data-dir DIR --json x5.json --quiet
python -m autonomy.gym.run --list          # every policy name, the variants included
```

CI: `.github/workflows/diagnose.yml` runs on a push to a `claude/**` branch whose head commit message contains
`[diagnose]`, and by hand (`workflow_dispatch`). Two matrices, `fail-fast: false`, `continue-on-error`, 120 minutes, one
concurrency group per commit:

- `gym`: baseline_shop, whatsapp_fatigue, discount_cannibalisation, sleeping_dogs_15, each with the 20 policies above,
  `--seeds 3 --customers 3000`. The log has the normal scoreboard, then the DIAG block (a short step prints it again alone).
- `bench`: hillstrom and x5, each with the same 20 policies. It uses the cached, checksummed download of `realbench.yml`
  (same cache key and folder). The log has the summary row and the DIAG block per dataset.

That is 80 Gym jobs and 40 bench jobs, so trigger it deliberately. Artefacts are uploaded too, but the logs are the
reading surface.

## Pending CI

Everything above. The first reading to make: for each scenario and dataset, `agent_v3` against `agent`, then the six
`v3_no_<part>` and the six `agent_<part>`, then the guards; fill the answers into STATUS.md only from the logs of one
`[diagnose]` run, with its commit.
