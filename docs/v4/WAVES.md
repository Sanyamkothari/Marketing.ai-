# Waves: a repeated-campaign benchmark on real customers

ROOT_CAUSE.md test T5. Code: `autonomy/gym/waves/`, command line `scripts/v2_realbench/waves.py`, CI
`.github/workflows/waves.yml`, tests `tests/v4/waves/`.

**Status: built and tested on a synthetic fixture. No X5 number exists yet.** The real X5 data cannot be
downloaded from the build environment (the proxy blocks it); it is fetched in CI. Every result on the real data is
"pending CI" below, with the exact command.

## Why it exists

The product claims things that only show over repeated campaigns: it learns from its own sends, it does not
over-message, it spaces messages, it protects customers who are put off. Two test beds cannot settle them.

- **The one-shot real bench** (`scripts/v2_realbench`) scores a single targeting decision per customer against a
  randomised campaign. It cannot see fatigue, learning from the agent's own logs, opt-outs that grow with volume, or
  purchases pulled forward from later weeks.
- **The Gym** can see all of those, but its truth is the agent's own model family: lift = channel x offer x theme x
  hour x fatigue, and a click chance of `0.02 + 3 x extra purchases` (ROOT_CAUSE.md, audit 2, "the Gym is
  circular"). It rewards contact volume and it makes the click model's central assumption true by construction.

This benchmark keeps the Gym's loop (so the agent, `agent_greedy`, every ablation variant and the Gym's fixed rules
run unchanged) and replaces its customers and its truth.

## What is real and what is synthetic

| Part | Source |
|---|---|
| Customers | **Real.** A fixed hash-sample (default 30,000; CI uses 20,000) of the X5 RetailHero clients who held a card at the cutoff: a card issued before it, or no issue date and a receipt before it (`eligible_clients`). Who buys after the cutoff plays no part in the choice; the run's `data:` line prints how many clients were dropped and why. |
| Features of a customer (recency, number of receipts, total spend, mean receipt, active days, age, gender, card age) | **Real**, computed only from receipts before the cutoff. |
| Baseline buying | **Real.** Each customer's own receipts after the cutoff are replayed day by day for 84 days, with their real times and amounts. With no message sent, the world is exactly those receipts (a test checks it). |
| Cutoff | The day that leaves exactly 84 days of receipts after it (the day after the last receipt, minus 84 days). If X5 ends on 18 March 2019 and starts in late November 2018, as I recall its description (not checked here), that is 2018-12-25 with about 34 days of history before it; the run prints the real figures on its `data:` line. |
| What a message does | **Synthetic**: the hidden effect below. |
| Clicks, unsubscribes, complaints | **Synthetic**, generated, with clicks decoupled from the effect. |
| Consent | SMS: every customer (as in the one-shot bench). E-mail: a fixed 60% by hash (X5 has no e-mail data). |
| Channels and prices | SMS (the X5 campaign channel) 2.0 RUB (`X5_SMS_COST`) and e-mail 0.3 RUB, set in `autonomy/gym/waves/config.py`. |
| Margin | 25% of a purchase (`X5_MARGIN`; the command asserts the two constants are equal). |
| Order value of an incremental purchase | In expectation the customer's own mean receipt before the cutoff (the population's mean receipt if they had none), times the margin. Each extra purchase copies the amount and time of day of one of the customer's own receipts before the cutoff, drawn at random, and its event id is a hash of the same form as a real receipt's, so a policy cannot tell it from a real one. |

Time is the shop's wall clock converted to UTC (receipts are Moscow time); a "day" is a UTC day starting 00:00 UTC
on the cutoff date.

## The hidden truth

A message sent on day D changes the customer's buying on days D+1 to D+14. With `rel` the customer's relative lift
after channel and fatigue (+0.07 is +7% of their 14-day purchase rate) and `w_j = (15 - j) / 105` the day weights (73%
lands in the first 7 days):

    positive rel:  extra daily purchase chance on day D+j      =  rel x base_rate x 14 x w_j     (additive)
    negative rel:  chance each real receipt on day D+j is lost  =  |rel| x 14 x w_j

- `base_rate` is the customer's purchases per day before the cutoff, over their own time as a card holder (or since
  their first receipt, if earlier) within the history, shrunk toward the population's by 14 pseudo-days. Over the 14
  days a positive message adds `rel x base_rate x 14` expected purchases.
- `rel` comes from a **random tree** over the eight real features: depth 3 (eight leaves), each split on a random
  feature at a random quantile (30% to 70%) of the customers in the node, one lift per leaf, scaled so the average
  `rel` is the world's `mean_lift` (3% to 10% territory: 4% in `homogeneous_small`, 7% elsewhere). A personal
  log-normal factor (sd 0.25) that no feature explains is multiplied on, so even the best rule on the features is not
  the oracle. The tree is fixed by the world's seed and never shown to a policy.
- **Sleeping dogs** (`sleeping_dogs`): whole leaves, chosen at random until about 15% of customers are covered, are
  made negative: a message takes away some of the customer's real receipts in its window (so a dog who would not
  have bought then loses nothing), and they opt out twice as often. Leaves are blocks of customers, so the realised
  share is only roughly 15%.
- **Fatigue**: a positive effect is multiplied by `fatigue ** k`, `k` = messages the customer got in the previous 14
  days (0.8 normally, 0.5 in `fatigue_heavy`). A negative effect is not shrunk.
- **Opt-outs**: a message ends in an unsubscribe from that channel with chance `base x (1 + slope x messages in the
  previous 7 days)` (0.002 and 0.5; 0.006 and 1.5 in `fatigue_heavy`). An opt-out costs 50 RUB (the margin of about one
  purchase that its later messages would have made), a complaint (10% of opt-outs) another 150 RUB.
- **Pull-forward**: after a positive effect, each of the customer's real receipts on the 30 days after the window is
  removed with chance `0.4 x rel x 14 / 30`. For a customer who buys at their base rate that takes back 40% of the extra
  purchases; for one who stops buying it takes back less, for a heavy buyer more. So reading a message's value over 7
  or 14 days overstates what it earned in the 44 days that count.
- An e-mail moves buying 70% as much as an SMS.

**The truth is exactly what the world realises, customer by customer.** The truth keeps the two per-customer, per-day
chances above (an extra purchase, capped at 0.95; a real receipt removed, capped at 1), and the world draws from those
same arrays. A contact is valued at what it adds to them: the margin of its extra purchases at the customer's own mean
receipt, less the margin of the real receipts it is expected to remove, read from the customer's real receipts on
those days (past the end of the data, day 84 on X5, from `base_rate x mean receipt`). So a customer with no receipt
after the cutoff is never charged for a loss the world cannot show, and the expected realised loss equals the
charged loss for every customer (a test checks it, including customers with no receipt after the cutoff).

**Clicks**: the chance is `floor + span x engagement^2`, where engagement is 35% the customer's rank in base buying
rate and 65% a personal hash. The hidden effect is not an input; a test shows the click events are identical in a
world where messages do nothing and one where they do. But clicks are not decoupled from the *value* of a contact:
the effect in roubles is proportional to the base rate, and so is part of the click chance. On a 3,000-customer
fixture (`fixture_tables(3000, seed=0)`, seeds 0 to 4), the Spearman correlation of the SMS click chance with the
relative lift `rel` is -0.03 to 0.15, but with the true net value of one SMS on day 0 it is 0.25 to 0.31 in
`heterogeneous` and `fatigue_heavy`, 0.05 to 0.27 in `sleeping_dogs` and 0.31 in `homogeneous_small`. Within fifths of
the base rate it is 0.01 to 0.04, so clicks carry nothing about the value beyond the base rate (a test bounds both).
A click model therefore learns partly who is worth a message, through how often they buy, and not who is moved
beyond that. That is a lean toward click models, next to the frequency lean below.

Worlds (`WORLDS`): `null` (nothing works), `homogeneous_small`, `heterogeneous`, `sleeping_dogs`, `fatigue_heavy`.
Several seeds change the tree, the personal factors and all random draws; the customers do not change.

### Why this does not favour the agent or the uplift method

- The agent's model is a click rate times a lift per click, per segment, with personal and fatigue multipliers. Here
  no click enters the truth and the effect is additive on the purchase probability, from a tree of features the agent
  only sees through its own state. Its click model has to earn its place.
- The uplift method's model is a logistic on binned features with a per-arm effect. A depth-3 tree with random splits
  is neither a logistic nor a bin. The method also has no knowledge of fatigue or pull-forward.
- Neither policy ever sees `rel`, the tree, the personal factor, `base_rate` or the future receipts.

### Where it still leans, so it can be questioned

I built this and also the `uplift_waves` adapter, so it needs a second pair of eyes (ROOT_CAUSE.md asked for someone
other than the builder).

- The effect is proportional to `base_rate`, so rules built on purchase frequency look good. That is realistic (a lift
  is usually a share of what people already do) but it is a lean toward frequency rules, which both policies can learn.
- Clicks rank the value of a contact at a Spearman correlation of about 0.25 to 0.31 through the base rate (above).
  The spec asks for clicks that depend on base buying, so this stays; it leans toward the agent's click model.
- The 14-day window and the front-loaded shape are my choice. Policies that learn with a 7-day horizon (the agent's
  default learning window) see 73% of an effect and none of the payback.
- Not tested: send hour, offers and discounts, WhatsApp, content themes (none carries an effect), several goals,
  drift. A "timing" claim cannot be settled here.
- The oracle is greedy per contact (best action today given what has been sent). It is a ceiling in practice, not a proof
  of optimality; it ignores that an opt-out ends later contacts.
- Probably only about 34 days of history before the cutoff (X5's receipts are about four months long): features and base rates are noisy.
- The 30-day tail cannot be observed in the data (it ends at day 84), so the true value counts it from the truth
  and the measured estimate cannot.

## Protocol

- The world is driven by `autonomy.gym.score.run_policy`: events are folded into customer state, every non-holdout
  customer gets one decision a day for 84 days, decisions are acted on, time moves on. Nobody is on the Gym's baseline
  rule (`baseline_share = 0`); the other 90% are the policy's.
- A fixed 10% holdout (a salted hash of the customer id) is never contacted by any policy. A test checks it, and the
  scoreboard's own rule-break count includes a contacted holdout customer.
- Weekly cap of 2 contacts per customer in 7 days, quiet hours 21:00 to 09:00 shop time, SMS or e-mail, no offers.
- A policy sees only observed events: history before the cutoff, then sends, clicks, unsubscribes and purchases
  (real receipts and incremental purchases alike) as they happen. The Gym strips the scoreboard-only `lost_value`
  from what policies see.
- Common random numbers: every purchase, removal, click and opt-out draw is a function of `(seed, customer, day)`
  alone, so two policies in the same world and seed face the same luck. A test shows that customers nobody contacted
  live identically in every run, and that a second contact only adds incremental purchases.

## Policies

| Name | What it is |
|---|---|
| `agent`, `agent_greedy`, `agent_uplift_only`, any name in `autonomy.runtime.ablation` | The Gym's agent, built on this brief and library with `costs={SMS: 2.0, EMAIL: 0.3}` and nothing else changed. |
| `send_none` | Never contacts anyone. |
| `send_all_sms`, `send_all_email` | Every customer who can receive it, every day the cap allows, 14:00 shop time. |
| `weekly_sms` | One SMS a week to everyone. |
| `best_fixed` | The Gym's `BestFixedRule` (one channel, no offer, fixed hour, as often as the cap allows), with the channel order SMS then e-mail. This is the one change to existing code: an optional `channels` argument, defaulting to the old WhatsApp then e-mail. |
| `oracle` | The Gym's `OraclePolicy` on the hidden truth. A ceiling. |
| `uplift_waves` | The `uplift_threshold` method (`autonomy/decide/uplift_threshold.py`) refitted every week, below. |

### `uplift_waves`

Every customer has a weekday turn (a hash). On their turn, with chance 10%, a customer a message can reach joins a
**randomised slice**: no message or one of the messages that can reach them, with equal chance (propensity 1/3 with
SMS and e-mail, 1/2 with SMS only; the design probability is kept per row), then nothing else for 14 days, so their
outcome window is clean. On the first day of each week from week 3 the method is refitted on all slice rows whose
14-day window has closed: a cross-fitted uplift model, a cost threshold and the gate (`fit_uplift_threshold`). The
model decides only if the gate says its lower bound beats the best simple rule; otherwise that rule decides (send
none, send all of an arm, or a segment rule). Outside the slice the deployed model or rule decides **every day**, for
every customer not in an outcome window and under the weekly cap, so a "send all SMS" rule sends as often as
`send_all_sms`. Before the first successful fit the declared default is `weekly_sms` (one SMS a week to everyone SMS
reaches).

Adapter decisions the method did not ask for, so they can be questioned:

- the label is the number of purchases in the 14 days, divided by a cap and capped at 1 (the method's label is a
  probability, and a 0/1 label saturates for frequent buyers). The cap is set at each refit to the 99th percentile of
  those counts in the rows fitted on, at least 4, so heavy buyers, who carry the largest effect in roubles, are not cut
  off; a purchase is priced at the cap x the customer's own mean margin per purchase;
- an arm's cost is its channel price only;
- it is fitted on the slice only, not on everything observed: its own deployed sends have propensity 0 or 1 and cannot
  be reweighted into unconfounded data;
- e-mail is only randomised to customers e-mail reaches, so when the gate judges a rule or model that would e-mail an
  SMS-only customer, that customer's e-mail value comes from the model, not from data. When deployed, the model gives
  such a customer their best reachable arm and a rule gives them nothing.

The refits (day, rows, outcome cap, what the gate chose, its lower bound and the share the model would contact) are in
the JSON under `policy_diagnostics`, printed after each run, and listed under each world's table.

## Score

For a world and a seed, per policy:

- **True value** = the incremental net margin of every contact the policy made against the same customers' no-message
  world, from the hidden truth, exactly (no sampling): margin of the expected extra purchases in the 14-day window,
  less the pull-forward payback in the next 30 days, less message costs, less the expected opt-out and complaint cost.
  It counts the tail past day 84. Contacts are the real ones the run made, so an opt-out that ends later contacts is
  already in it.
- **Share of the oracle** = mean true value over the oracle's, in the same world.
- **Measured at the last day** = what a business would book: the intention-to-treat difference between the policy's group
  (every non-holdout customer, contacted or not) and the holdout in mean realised net per customer, raw (no pre-period
  adjustment), times the group's size, with a 95% interval. It cannot see the tail, and it is noisy because it compares
  real receipts of different customers. It is printed next to the true value so over-claiming is visible.
- **Contacts per customer-week** and **unsubscribe rate** (share of customers who opted out of any channel).
- **Paired differences** against `agent` and against the best simple policy (the best of `send_none`, `send_all_sms`,
  `send_all_email`, `weekly_sms`, `best_fixed` by mean true value in the same run, which is chosen after the fact and so flatters
  it). Because policies share customers and luck, the difference is taken per customer. Its standard error has two
  parts: customer sampling, from each customer's difference averaged over the seeds (the seeds share their customers, so
  they are not independent samples of them), and the spread of the per-seed totals between worlds, divided by the
  number of seeds (`paired_difference` in `autonomy/gym/waves/report.py`). The true value cell also shows the sd of the
  per-seed totals.

## Run it

Real data (CI, or a machine that can download X5):

    python -m scripts.v2_realbench.fetch --data-dir DIR --dataset x5
    python -m scripts.v2_realbench.waves --world heterogeneous --policy agent --seeds 3 --customers 30000 \
        --data-dir DIR --json out.json
    python -m scripts.v2_realbench.waves --summarize DIR_OF_JSON_FILES        # one table per world

Fixture (no download; its numbers say nothing about shoppers):

    python -m scripts.v2_realbench.waves --fixture --world null --policy send_all_sms --customers 600 --days 28

Fixture as dense as X5 (about 0.17 receipts per customer-day, 34 days of history):

    python -m scripts.v2_realbench.waves --fixture --fixture-rate 0.17 --fixture-history-days 34 --customers 600 --days 28

CI: a push to `version-2` or `claude/**` whose commit message contains `[waves]`, or workflow_dispatch, runs
`.github/workflows/waves.yml`: one job per world x policy x seed, 5 worlds x 10 policies (`send_none`, `send_all_sms`,
`send_all_email`, `weekly_sms`, `best_fixed`, `oracle`, `uplift_waves`, `agent`, `agent_greedy`, `agent_uplift_only`) x
3 seeds = 150 jobs, then one table per world in the step summary. Each job rewrites its JSON after the run, so a job
that times out loses only its own seed.

### How the CI job is sized

Each job must finish inside 60 minutes, and the plain agent is the slow policy. Measured on the 4-CPU dev box, one
thread:

- At X5's density (the fixture with `--fixture-rate 0.17 --fixture-history-days 34`: 0.161 receipts per customer-day
  replayed, 33.8 days of history), 600 customers x 28 days (16,800 customer-days): `agent` 13.1 s and
  `agent_uplift_only` 12.9 s (0.78 and 0.77 ms per customer-day); `uplift_waves`, `send_all_sms` and `oracle` 1.0 s each.
  Command in "Results" below.
- Earlier, at the fixture's default density (0.07 receipts per customer-day, 60 days of history), the agent took 13.0 s
  for 600 x 28 and 360.5 to 393.1 s in five runs at 6,000 customers x 84 days (504,000 customer-days, 0.73 to 0.78 ms
  each), so time is close to linear in customer-days and the purchase density barely moves it at this size.

One seed of the agent at 20,000 customers x 84 days (1.68M customer-days) should take about 1.68M x 0.78 ms = 22
minutes, plus loading and sampling X5 (not measured here). That is an extrapolation, not a measurement, and GitHub's
runners may be slower; the margin to 60 minutes (about 2.7x) is meant to absorb that, and a job that still runs out of
time loses one seed, not three. `agent_greedy` was not timed at X5's density; it runs the same code without exploration.
The spec's 30,000 would take about 33 minutes per seed by the same extrapolation, which leaves too little margin for an
unmeasured runner, so CI uses 20,000 and says so on each job's `data:` line. A bigger run is a `workflow_dispatch` input
away.

## Results

### Real X5 (pending CI)

Nothing has run on X5. The command, per world, policy and seed (CI runs all 150, 20,000 customers):

    python -m scripts.v2_realbench.waves --world <world> --policy <policy> --seeds 1 --first-seed <seed> \
        --customers 20000 --data-dir DIR --json waves-report/<world>-<policy>-<seed>.json
    python -m scripts.v2_realbench.waves --summarize waves-report        # the tables below, from real data

Pending CI: true value, share of the oracle, difference to `agent` and to the best simple policy, contacts per
customer-week, unsubscribe rate and the measured holdout estimate, per policy in each of the five worlds. The first
thing to read on the real data is whether `agent` beats the best simple policy in `heterogeneous`, `sleeping_dogs`
and `fatigue_heavy` and ties it in `homogeneous_small` and `null`; whether `uplift_waves` gets anywhere near it; and
how wide the measured estimate is next to the true value.

### Synthetic fixture (a check of the machinery, not evidence)

The fixture tables of commit 132bbc65 (6,000 customers, one seed, all five worlds) were made before the fixes in this
version: the truth now charges a sleeping dog's loss and the pull-forward payback on the customer's real receipts, the
base rate uses each customer's own time as a card holder, and `uplift_waves` randomises every reachable customer, acts
every day and starts from `weekly_sms`. Those tables are superseded and are not repeated here. To make them again:

    python -m scripts.v2_realbench.waves --fixture --customers 6000 --seeds 1 \
        --world null,homogeneous_small,heterogeneous,sleeping_dogs,fatigue_heavy \
        --policy send_none,send_all_sms,send_all_email,weekly_sms,best_fixed,oracle,uplift_waves --json simple.json
    python -m scripts.v2_realbench.waves --fixture --customers 6000 --seeds 1 \
        --world heterogeneous,fatigue_heavy --policy agent,agent_greedy --json agent.json
    python -m scripts.v2_realbench.waves --summarize <folder holding both>

One smoke run of this version, at X5's density, 600 synthetic customers, 28 decision days, one seed (it is the timing
run above; at this size nothing in it is evidence about any policy):

    python -m scripts.v2_realbench.waves --fixture --fixture-rate 0.17 --fixture-history-days 34 --customers 600 \
        --days 28 --seeds 1 --world heterogeneous --policy agent,agent_uplift_only,uplift_waves,send_all_sms,oracle

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at the last day (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_all_sms | 23,155 | 89% | +9,482 [+6,372, +12,592] | - | 1.99 | 1.7% | 47,914 ± 92,375 |
| agent | 13,673 | 53% | - | -9,482 [-12,592, -6,372] | 1.21 | 1.1% | 34,146 ± 91,936 |
| agent_uplift_only | 13,673 | 53% | +0 [+0, +0] | -9,482 [-12,592, -6,372] | 1.21 | 1.1% | 34,146 ± 91,936 |
| uplift_waves | 15,824 | 61% | +2,151 [+392, +3,910] | -7,331 [-9,547, -5,114] | 0.94 | 0.9% | 37,835 ± 91,969 |
| oracle | 25,916 | 100% | +12,243 [+9,241, +15,244] | +2,761 [+2,333, +3,190] | 1.58 | 1.9% | 50,928 ± 92,446 |

`send_all_sms` is the only simple policy in this run, so it is "best simple" by default. `uplift_waves` made 182 slice
assignments and no refit (fewer than 400 closed slice rows by day 21), so it ran its default, `weekly_sms`, outside the
slice. `agent_uplift_only` tied `agent` exactly over these 28 days.

**What a review of commit 132bbc65 found about `uplift_waves`** (no numbers here, since they came from that version): the
earlier text blamed power for its weak fixture result. That was wrong. The gate's lower bound is the bound on *model
minus the best simple rule*, not on the model's value, and at every weekly refit the model would have contacted
everyone, which was close to right in that world. What held the policy back was the fallback: the simple rule the gate
kept was chosen by DR on a few hundred noisy slice rows (send none at the first refit, then segment rules on columns
such as tenure or age), the adapter acted only on each customer's weekly turn, and it sent nothing outside the slice
before the first fit. This version acts every day, starts from `weekly_sms`, randomises every reachable customer and
lists every refit under the tables, so the CI run will show which rule the gate keeps.

## Limits and next steps

- Nothing here has been run on X5; the loader is tested on files of X5's layout written from the fixture, not on the real
  files. If the real `purchases.csv.gz` ends earlier than assumed, the cutoff moves and the pre-cutoff history shortens;
  the run prints the real cutoff and history length on its `data:` line.
- One builder wrote the world, the truth and the `uplift_waves` adapter. Someone else should read `autonomy/gym/waves/truth.py`
  and `uplift.py` with the question "does this favour either method?" before the numbers are used for a decision.
- 3 seeds are seeds of the world (tree, personal factors, luck) on the same customers; they say little about how the result
  would change with other customers. The paired interval counts customer sampling once and adds the spread between
  the seeds; with 3 seeds that spread is itself poorly known.
- With pull-forward and sleeping dogs priced on real receipts, a world's true value depends on what the sampled
  customers really bought after the cutoff. The holiday weeks after 25 December are in that window on X5.
- Timing, offers, WhatsApp and content choice are outside this benchmark; T6 (a live pilot) is still the real answer.

