# The v3 preset: every piece together

Plan-v3 section 8 asks whether "v3" is smarter than the plain v2 agent. Each v3 piece is opt-in and was built and
checked alone; this page is the switch that turns them on together, so CI can score v3 as a whole, and the check that
they compose. Code: [`autonomy/runtime/v3.py`](../../autonomy/runtime/v3.py) (`V3Preset`), the `v3=` argument of
`build_agent_policy` in [`autonomy/runtime/agent.py`](../../autonomy/runtime/agent.py), `RuntimeSettings.v3`
(`AUTONOMY_V3`), the Gym policy `agent_v3`, the real bench's `--policy agent_v3`. Tests: `tests/v3/preset/`.

Every number below comes from a command named next to it. What only CI can run is marked **pending CI**, with the
exact command.

## What it turns on

`build_agent_policy(briefs, content, v3=True)`, or `v3=V3Preset(...)` to switch a part off. With `v3=False`, the
default, nothing here runs and every number is unchanged (`test_v3_false_changes_no_decision`).

| Part | `V3Preset` field | On means | Off inside the preset |
|---|---|---|---|
| Look-alike learning | `lookalike` | `LookalikeClickModel` on top of the click model ([LOOKALIKE.md](LOOKALIKE.md)) | `V3Preset(lookalike=False)` |
| Who | `who` | leaving risk, 90-day value, sleeping dogs by traits; also puts the profile in front of the look-alike and tiredness models ([WHO.md](WHO.md)) | `who=False` |
| Frequency | `frequency` | look-alike tiredness and 3-week plans ([FREQUENCY.md](FREQUENCY.md)) | `frequency=False` |
| Moments | `moments`, `moments_country` | the shipped `MomentBook` (`configs/v3`), the shop's country as the calendar for customers who name none ([MOMENTS.md](MOMENTS.md)) | `moments=False` (the bidders get `False`, which beats `use_moments`' process-wide book) |
| Journeys | `journeys` | the shipped `configs/v3/journeys.yaml` (A to D) ([JOURNEYS.md](JOURNEYS.md)) | `journeys=False` |
| Offer pricing | `offers` | the budget price for offers, starting at 0 and renewing its plan (`DEFAULT_OFFER_PRICING`; [OFFERS.md](OFFERS.md)) | `offers=False` |
| Experiments | `experiments` | `ExperimentPolicy` around `decide`, with an `ExperimentBook` that has no experiment running, so it changes nothing until one is started ([EXPERIMENTS.md](EXPERIMENTS.md)) | `experiments=False` |

Each field is True, False or the piece's own config (`LookalikeConfig`, `WhoConfig`, `FrequencyConfig`,
`JourneyConfig`, `OfferPricing`, a shared `ExperimentBook`). A piece also passed by name to `build_agent_policy`
(`moments=False`, `journeys=runner`, `offer_pricing=...`) wins over the preset's. The wrapper sits inside the agent,
not around it: `build_agent_policy` still returns an `AgentPolicy`, whose `decide` goes through
`agent.experiment_policy` (`agent.experiment_policy.book` is the book).

**The runtime.** `RuntimeSettings.v3` (`AUTONOMY_V3`, default off) builds every goal's agent with the preset (the
shop's calendar is its one country when `AUTONOMY_COUNTRIES` names exactly one) and sets
`RuntimeConfig.nightly_profile`, so every state carries the profile that the look-alike model, who and the tiredness
model read. The scale fixes stay as they were: sweep batches of 5,000 and a tick report of 1,000 decisions
(`test_the_runtime_setting_turns_on_the_preset_and_the_profile_and_keeps_the_scale_fixes`).

Not switched on by `AUTONOMY_V3`, because nothing in the runtime calls them on this base:

- **Early signals and auto-budget** (`autonomy/learning/early_signal.py`, `autonomy/budget/`). They reach the product
  only as arguments of the briefing (`Analyst.briefing(..., early=)`, `inbox.briefing.budget_week`), and the weekly
  job that would call `rebalance` and fit the early-signal model is not written ([EARLY_SIGNALS.md](EARLY_SIGNALS.md)).
  When it is, a budget move reaches the agent as a new brief version, which the offer price already follows (below).
- **Copy and images** ([COPY.md](COPY.md), [IMAGES.md](IMAGES.md)). They write and check content ahead of time, behind
  their own `enabled` flags and approval steps; they do not change how a decision is made.
- `api/v2/engines/decisions.py` builds its agent directly and ignores the setting, as it ignores `lookalike_model`.

## The order a decision is made in

One order, fixed by where each piece hooks in. Read it top to bottom for one customer on one daily sweep.

1. **The experiments' wrapper** (`ExperimentPolicy.decide`). The holdout and baseline are only tagged. For an agent
   customer: if a journey is running for them, the wrapper steps aside (below). Otherwise a hash puts them in at most
   one running test, and the arm cuts the goal's content library to its offer or channel *before anyone quotes*.
2. **The agent** (`AgentPolicy._decide`). Paused goals and channels leave the context. `WhoBook` writes the slow
   models' scores and, where the state has no nightly profile (the Gym, the bench), a profile built from the events it
   holds, onto the state this decision is made on. From here the look-alike model, the tiredness model and the trait
   groups all read that one profile.
3. **A running journey goes first** (`AutonomousPolicy._journey_turn`). It stops on a purchase, an unsubscribe or a
   complaint; holds the customer until its next step is due; quotes a due step afresh, times it, passes it through
   every hard rule and the auction's attention price (not the weekly rhythm, not a new plan) and sends it with
   probability 1. If it ends, the same decision goes on to 4.
4. **The bidders quote.** A bid's urgency is the goal's own (a cart reminder fades), with the live moments inside it,
   times who's `1 + 0.5 x leaving risk`. Every offer is costed (the basket-based cost where the config gives none) and
   charged `cost x (1 + the goal's budget price)`; who's cost ceiling compares the real spend. Then the offer gate
   (an offer must beat its plain version on the evidence) and the leave-alone checks (own record, neighbours, traits).
5. **Journeys are priced** (`JourneyRunner.expand`) from those bids: each journey is one option, worth its first
   step's net plus the later steps' net weighted by the chance they still run, each at the urgency left on its day.
   A journey that puts more messages in 7 days than the strictest weekly cap allows is not offered.
6. **Hard rules** (consent, quiet hours, caps, holdout, the envelope) remove bids. They are never learned.
7. **The auction**: the attention price (look-alike tiredness), pacing, the weekly rhythm, **the 3-week plan** (it can
   only hold the customer for a day, never release one), the best options, plausibility, restraint, and the draw with
   the exploration floor. The logged probability is the draw's; for a journey also the chance of the journey.
8. **After the fact** (`nightly`): the response models and the look-alike outcomes learn from the first message of a
   journey and from single messages, not from a journey's later steps; the breaker, the tiredness curves and the pacer
   count every message; the offer price moves once a day by the offer money spent.

Where two pieces change the same thing:

| Pair | Rule | Status |
|---|---|---|
| Moments and who, on urgency | Product. The goal's urgency (moments inside it, at most 2.5) times who's factor (at most 1.5), so at most 3.75. Both scale the whole net, so a high risk or a live cart never turns a losing message into a winner. The plan's fade reads the goal's own part only: risk does not fade, a moment does | Documented, pinned by `test_urgency_is_the_goals_fade_times_moments_times_leaving_risk` |
| Moments and journeys | A later step is priced at the urgency left on its day: a cart left today raises today's message, and when step 2 runs three days on the moment has closed. Before, a journey priced its later steps at today's urgency | **Fixed** (`JourneyRunner.fade`, set by the agent) |
| Journeys and the weekly rhythm | The rhythm is one message a week for most customers, so it held every step inside the week of the one before it until the step expired (`max_defer_days`): in a 200-customer Gym run, 124 journeys B started and none of their WhatsApp steps went out, while those customers were held out of the sweep for days. The journey is the plan, so the agent's journeys skip the rhythm (`JourneyRunner.follow_rhythm = False`); the hard weekly cap and the attention price still apply to every step, and a journey the cap cannot hold is not offered | **Fixed** |
| Journeys and frequency plans | A customer in a journey follows it (the plan is not asked for a step). A journey's first step is one message to the plan like any other, and the plan can hold it for a day. The planner reads a journey bid as one message with the whole plan's gain and cost, and charges the first channel's attention price only; later steps pay theirs when they run | Documented; an approximation |
| Journeys and learning | A later step goes to someone who did not buy after the step before, so it is not a fresh draw. It no longer teaches the response models or the look-alike outcomes as a treatment; the breaker, the tiredness curves and the pacer still count it | **Fixed** (`AgentPolicy._keep`, `_remember`) |
| Journeys and a rebuilt policy | A policy built after a restart or a brief change had no runs, so a customer mid-journey would be sent a second first message. It now replays its runs from the decision log it is handed before its first decision | **Fixed** (`AgentPolicy.nightly`) |
| Experiments and journeys | A journey in progress is finished as planned. A channel arm leaves a later step on the other channel nothing to offer, which ended the journey and started a new one in its place, so a test moved a customer in the middle of a plan. A customer with a journey running is not assigned on the sweep (decisions prompted by a cart or a reply are); they join, by the same hash, after the journey ends. A first step *is* cut by the arm before it is quoted, so a journey with a step on the other channel is not offered to that arm at all | **Fixed** (`ExperimentPolicy(busy=...)`) |
| Experiments and offer pricing | An offer arm forces its offer, and the budget price is charged on that offer's cost like any other; arm redemptions count against the goal's budget. Arms are still a random split compared with each other and the holdout; the price changes how many customers of an arm are worth a send, which is part of what that arm earns | Documented |
| Offer pricing and the budget planner | A budget move (auto-budget) arrives as a new brief version; the goal's price keeps its spend and takes the new budget (`test_the_offer_price_follows_a_budget_that_moves...`). A plan of `days` (30) that has run pinned every offer at the ceiling from day 31 with budget left; with `renew` the agent plans another period for what is left. The preset starts the price at 0, where the module default of 0.5 held offers back for a week even when the budget did not bind | **Fixed** (`OfferPricing.renew`, `BudgetPrice.extend`, off by default) |
| Offer pricing and the 3-week plan | The planner values an offer at its cost, without the budget price the auction charges. The plan only holds, never releases, so this can make a hold slightly rosier than the auction's net, not send what the auction would not | Documented; an approximation |
| Who, look-alike and tiredness, on the profile | One profile per decision: the nightly one when the state has it (no older than 3 days), else built from the events the who book holds | Documented |

## What the preset does not fix

- **Memory in the runtime.** The who book keeps every event the agent is handed, in memory, per goal ([WHO.md](WHO.md)):
  fine for the Gym, the bench and a pilot, not for a million customers. The look-alike rows and outcomes and the
  tiredness crowd are in memory too and start empty after a restart ([LOOKALIKE.md](LOOKALIKE.md)). `AUTONOMY_V3` is for
  pilots and small to mid-size shops until those stream. The sweep itself stays in batches.
- **The experiment book is not persisted.** A rebuilt policy gets an empty one. Writing `ExperimentBook.snapshot()`
  and running the weekly review is the Scientist's job, not wired ([EXPERIMENTS.md](EXPERIMENTS.md)).
- **Moments are mostly dormant in the plain Gym matrix.** The Gym's default folder keeps no carts, views or buying
  rhythms, and its customers have no country. `agent_v3` on the ten matrix scenarios therefore measures the other six
  parts; moments are measured by `moments_planted` with `--moments` (below). Likewise journeys have little to do in the
  real bench, which makes one decision per test customer.
- **Price drops have no producer** ([MOMENTS.md](MOMENTS.md)); journey-level credit is not in the proof
  ([JOURNEYS.md](JOURNEYS.md)).
- Every threshold in the pieces is still a design default, not a measurement.

## What was run here

All from this worktree, on a shared 4-CPU machine, nothing heavy.

- `python -m pytest tests/v3 -n 2 -m "not slow" -o addopts="" -q`: 701 passed in 20.0 s (all of v3, the pieces' own
  tests included, on the final code).
- Our own tests, `tests/v3/preset/` (32 fast tests, 1 slow): the default is unchanged; the preset builds every part;
  each part switches off apart (7 cases, each also checked as the only part left on); a part given by name wins; the
  experiments' wrapper changes nothing until a test runs (30 customers, decisions equal); one decision with every part
  active (a cart left 6 hours ago and a leaving risk of 0.8: urgency 2.106 = 1.505 x 1.4, journeys among the options,
  the plan made, the send's profile kept, the offer price in the bidder, and with a test started the same wrapper
  puts a customer on an arm); each row of the table above marked Fixed; `RuntimeSettings.v3` and the scale fixes;
  the Gym policy and its CI matrix; the bench handing the who book the TEST customers' pre-campaign events (on the
  synthetic trial of `tests/v2/realfix/test_bench.py`, 303 customers prepared with a profile = the whole TEST half,
  and none of their campaign events handed over).
- `tests/v2/agent_brain`, `tests/v2/runtime/test_service.py`, `tests/v2/gym`, `tests/v2/realfix/test_bench.py`,
  `tests/v2/failure/test_runtime_wiring.py`, `tests/v2/polish/test_runtime_gaps.py`,
  `tests/v2/zero_touch/test_wiring_production.py`, `tests/v2/decide/test_policy.py` and the four
  `tests/v2/scientist` files that read the bench and the targets, together with `tests/v3/preset` including its slow
  test (a 200-customer, 40-day Gym run twice, same card both times, 0 rule breaks): 314 passed in 17.9 s, on the
  final code.
- **One smoke run**, so that every part runs together:
  `python -m autonomy.gym.run --scenario baseline_shop --policy agent_v3 --seeds 1 --customers 200 --days 35 --no-compare`
  (`--days 35` and `--no-compare` to keep it small). On the commit before the weekly-rhythm fix it finished in 7.1 s with
  0 rule breaks, a true value of $608, 0.86 contacts per customer per week and 2.4% unsubscribed.
  The same scenario, seed and size through a small harness that counts what each part did (it wraps the same
  `run_policy`), after the fix: true value $665, 0 rule breaks, 844 sends; journey A started 447 times, journey B 124
  times and its WhatsApp step went out 92 times (before the fix: 0); the 3-week plan chose `now_and_10d` 811 times and
  `wait_14d` 405 times, and held 405 decisions; 178 sends carried an offer; the offer price ended at 0.0 after 34
  days; who's leaving-risk model was **not** in use (held-out AUC 0.566, below its 0.60 bar), its 90-day value model
  was not in use (24 training rows), trait groups were read (8) and none left anyone alone, and all 167 customers
  prepared on the last day had a profile; the look-alike model had 824 rows and was still gathering (`ready` false);
  no experiment ran. This says the wiring works. At 200 customers and one seed it says nothing about whether
  `agent_v3` beats `agent`, and the two true values above (different code, same seed) show how far one seed moves.
- `ruff check`, `black --check` and `mypy --strict --python-version 3.11 --ignore-missing-imports
  --follow-imports=silent` on every changed file: clean.

## The comparison: pending CI

Nothing here has been run. v3's bars are plan-v3 section 8: the Gym, a higher share of the oracle than v2's agent in
every scenario and, where messages make money, a win over the best fixed rule on every seed; the real bench, a win
over the best simple policy where features carry signal (MegaFon, Criteo) and at least a tie everywhere else
(Hillstrom included), with expected value per message within 1.5x of measured.

**Gym: `agent` against `agent_v3` on every scenario.** On a push to `version-2` or `claude/**` whose commit message
contains `[gym]`, `.github/workflows/gym.yml` runs each of the ten scenarios for `agent`, `agent_lookalike`,
`agent_who`, `agent_frequency` and `agent_v3`. By hand, the pair:

```
for s in baseline_shop sleeping_dogs_15 sleeping_dogs_hidden discount_cannibalisation whatsapp_fatigue \
         send_time_matters drift_midway small_business null_effect complaint_storm; do
  for p in agent agent_v3; do
    python -m autonomy.gym.run --scenario $s --policy $p --seeds 3 --customers 3000 --report-json gym-report/$s-$p.json
  done
done
# moments, which the matrix above cannot show (carts and refills are planted; --moments folds with the full StateFolder)
python -m autonomy.gym.run --scenario moments_planted --policy agent    --moments --seeds 3 --customers 3000 --report-json gym-report/moments_planted.json
python -m autonomy.gym.run --scenario moments_planted --policy agent_v3 --moments --seeds 3 --customers 3000 --report-json gym-report/moments_planted_v3.json
```

Read, per scenario: true value, share of the segment oracle, contacts to sleeping dogs, unsubscribe rate, rule breaks
and the staged bar of [GYM_BAR.md](../v2/GYM_BAR.md) (`verdict` in the JSON). `agent_v3` against `agent` is the whole
preset against v2; `agent_v3` against `agent_lookalike`, `agent_who` and `agent_frequency` says what the pieces add on
top of one another. `moments_planted_v3.json` carries `moments.mean_gain` for the preset's moments against the preset
with only them off. `better_offer_planted` is the experiments' scenario (`python -m autonomy.experiments.gymrun`); the
preset's wrapper is a no-op there until a test is started.

**Real bench: `agent` against `agent_v3` on Hillstrom, X5, Criteo and MegaFon.** On `workflow_dispatch` or a commit
message containing `[real]`, `.github/workflows/realbench.yml` runs each dataset for both policies and renders both
reports and a table of the doubly-robust profit per customer, `agent_v3` minus `agent`. By hand:

```
for d in hillstrom x5 criteo megafon; do
  for p in agent agent_v3; do
    python -m scripts.v2_realbench.run --dataset $d --policy $p --data-dir DIR --json bench/$d-$p.json --quiet
  done
done
python -m scripts.v2_realbench.run --render bench/megafon-agent_v3.json
```

`--policy agent_v3` builds the agent with `build_agent_policy(v3=True)` and hands the who book every customer's
pre-campaign events (the `pre` the bench folds the TEST states from), the same step `tests/v3/who/bench_run.py` takes.
Without it the TEST customers would have no profile and no score, and decision 1 would not be measured at all. The
campaign events of TEST customers are never handed over. Read the `learned` lines of the agent block first: "who:
customers read at the last decision day" should show `prepared` and `profile` at the TEST size. Then the verdicts and
calibration (`agent.expected_net_per_contact` against the measured gain) per dataset. Memory on Criteo is the thing to
watch: the book holds the pre-campaign events of every customer (the job limit is 90 minutes for that reason; it has
not been timed).

When the numbers are in, they go in this file under the commit that produced them.
