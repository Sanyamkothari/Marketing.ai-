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
| Customers | **Real.** A fixed hash-sample (default 30,000; CI uses 10,000) of X5 RetailHero clients that have at least one receipt. |
| Features of a customer (recency, number of receipts, total spend, mean receipt, active days, age, gender, card age) | **Real**, computed only from receipts before the cutoff. |
| Baseline buying | **Real.** Each customer's own receipts after the cutoff are replayed day by day for 84 days, with their real times and amounts. With no message sent, the world is exactly those receipts (a test checks it). |
| Cutoff | The day that leaves exactly 84 days of receipts after it (the day after the last receipt, minus 84 days). If X5 ends on 18 March 2019 and starts in late November 2018, as I recall its description (not checked here), that is 2018-12-25 with about 34 days of history before it; the run prints the real figures on its `data:` line. |
| What a message does | **Synthetic**: the hidden effect below. |
| Clicks, unsubscribes, complaints | **Synthetic**, generated, with clicks decoupled from the effect. |
| Consent | SMS: every customer (as in the one-shot bench). E-mail: a fixed 60% by hash (X5 has no e-mail data). |
| Channels and prices | SMS (the X5 campaign channel) 2.0 RUB (`X5_SMS_COST`) and e-mail 0.3 RUB, set in `autonomy/gym/waves/config.py`. |
| Margin | 25% of a purchase (`X5_MARGIN`; the command asserts the two constants are equal). |
| Order value of an incremental purchase | The customer's own mean receipt before the cutoff (the population median if they had none), times the margin. |

Time is the shop's wall clock converted to UTC (receipts are Moscow time); a "day" is a UTC day starting 00:00 UTC
on the cutoff date.

## The hidden truth

A message sent on day D changes the customer's daily purchase probability on days D+1 to D+14 by an **additive**
amount:

    change on day D+j  =  rel x base_rate x w_j          (w_j = (15 - j) / 105, so 73% lands in the first 7 days)

- `base_rate` is the customer's purchases per day before the cutoff, shrunk toward the population's by 14 pseudo-days.
  Over the 14 days a message adds `rel x base_rate x 14` expected purchases.
- `rel` is the customer's relative lift (+0.07 is +7% of their 14-day purchase rate). It comes from a **random
  tree** over the eight real features: depth 3 (eight leaves), each split on a random feature at a random quantile
  (30% to 70%) of the customers in the node, one lift per leaf, scaled so the average `rel` is the world's
  `mean_lift` (3% to 10% territory: 4% in `homogeneous_small`, 7% elsewhere). A personal log-normal factor
  (sd 0.25) that no feature explains is multiplied on, so even the best rule on the features is not the oracle. The tree
  is fixed by the world's seed and never shown to a policy.
- **Sleeping dogs** (`sleeping_dogs`): whole leaves, chosen at random until about 15% of customers are covered, are
  made negative (a message lowers their buying: real receipts inside the window are removed), and they opt out twice
  as often. Leaves are blocks of customers, so the realised share is only roughly 15%.
- **Fatigue**: a positive effect is multiplied by `fatigue ** k`, `k` = messages the customer got in the previous 14
  days (0.8 normally, 0.5 in `fatigue_heavy`). A negative effect is not shrunk.
- **Opt-outs**: a message ends in an unsubscribe from that channel with chance `base x (1 + slope x messages in the
  previous 7 days)` (0.002 and 0.5; 0.006 and 1.5 in `fatigue_heavy`). An opt-out costs 50 RUB (the margin of about one
  purchase that its later messages would have made), a complaint (10% of opt-outs) another 150 RUB.
- **Pull-forward**: 40% of a positive effect's purchases are missing again from the 30 days after the window
  (realised by removing real receipts with the matching probability). So reading a message's value over 7 or 14 days
  overstates what it earned in the 44 days that count.
- An e-mail moves buying 70% as much as an SMS.
- **Clicks**: the chance is `floor + span x engagement^2`, where engagement is 35% the customer's rank in base buying
  rate and 65% a personal hash. The hidden effect is not an input; a test shows the click events are identical in a
  world where messages do nothing and one where they do. A click model therefore learns who clicks and buys, not who is
  moved, which is the failure mode ROOT_CAUSE.md describes for real data.

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

Every customer has a weekday turn (a hash). On their turn, with chance 10% (if both channels can reach them) they join a
**randomised slice**: no message, SMS or e-mail with equal chance, then nothing else for 14 days, so their outcome
window is clean. On the first day of each week from week 3 the method is refitted on all slice rows whose 14-day window
has closed: a cross-fitted uplift model, a cost threshold and the gate (`fit_uplift_threshold`). The model decides only if the
gate says its lower bound beats the best simple rule; otherwise that rule decides (send none, send all of an arm, or a
segment rule). Customers outside the slice get at most one message a week, on their turn. Before the first fit it
sends nothing outside the slice. Choices the method did not ask for: its label is the number of purchases in the 14 days,
capped at 4 and divided by 4 (the method's label is a 0/1 probability, which saturates for frequent buyers), priced at
4 x the customer's own mean margin per purchase; an arm's cost is its channel price only. The refits and what the gate
chose are in the JSON under `policy_diagnostics`.

## Score

For a world and a seed, per policy:

- **True value** = the incremental net margin of every contact the policy made against the same customers' no-message
  world, from the hidden truth, exactly (no sampling): margin of the expected extra purchases in the 14-day window,
  less the pull-forward payback in the next 30 days, less message costs, less the expected opt-out and complaint cost.
  It counts the tail past day 84. Contacts are the real ones the run made, so an opt-out that ends later contacts is
  already in it.
- **Share of the oracle** = mean true value over the oracle's, in the same world.
- **Measured at day 84** = what a business would book: contacted group's mean realised net minus the holdout's, times the
  group's size, with a 95% interval. It cannot see the tail, and it is noisy because it compares real receipts of
  different customers. It is printed next to the true value so over-claiming is visible.
- **Contacts per customer-week** and **unsubscribe rate** (share of customers who opted out of any channel).
- **Paired differences** against `agent` and against the best simple policy (the best of `send_none`, `send_all_sms`,
  `send_all_email`, `weekly_sms`, `best_fixed` by mean true value in the same run, which is chosen after the fact and so flatters
  it). Because policies share customers and luck, the difference is taken per customer; the interval combines the seeds
  as independent worlds (`autonomy/gym/waves/report.py`).

## Run it

Real data (CI, or a machine that can download X5):

    python -m scripts.v2_realbench.fetch --data-dir DIR --dataset x5
    python -m scripts.v2_realbench.waves --world heterogeneous --policy agent --seeds 3 --customers 30000 \
        --data-dir DIR --json out.json
    python -m scripts.v2_realbench.waves --summarize DIR_OF_JSON_FILES        # one table per world

Fixture (no download; its numbers say nothing about shoppers):

    python -m scripts.v2_realbench.waves --fixture --world null --policy send_all_sms --customers 600 --days 28

CI: a push to `version-2` or `claude/**` whose commit message contains `[waves]`, or workflow_dispatch, runs
`.github/workflows/waves.yml`: 5 worlds x 10 policies (`send_none`, `send_all_sms`, `send_all_email`, `weekly_sms`,
`best_fixed`, `oracle`, `uplift_waves`, `agent`, `agent_greedy`, `agent_uplift_only`), 3 seeds each, then one table per world
in the step summary.

### How the CI job is sized

Each job must finish inside 60 minutes, and the plain agent is the slow policy. Measured here on the synthetic
fixture, one CPU thread:

- the agent took 13.0 s for 600 customers x 28 days (16,800 customer-days, 0.77 ms each) and 360.5 to 393.1 s in five
  runs at 6,000 customers x 84 days (504,000 customer-days, about 0.73 to 0.78 ms each), so time is close to linear in
  customer-days;
- every other policy takes 8 to 40 s at 6,000 customers x 84 days.

At 10,000 customers the agent should take about 600 s a seed, 30 minutes for 3 seeds, plus loading and sampling X5. That
is an extrapolation, not a measurement, and CI machines may be slower; the margin to 60 minutes is meant to absorb that.
The default of 30,000 would take about 90 minutes of agent time for 3 seeds (extrapolated the same way), so CI uses 10,000 and says so in the job's
printed `data:` line. A bigger run is a `workflow_dispatch` input away.

## Results

### Real X5 (pending CI)

Nothing has run on X5. The command, per world and policy (CI runs all 50 combinations, 3 seeds, 10,000 customers):

    python -m scripts.v2_realbench.waves --world <world> --policy <policy> --seeds 3 --customers 10000 \
        --data-dir DIR --json waves-report/<world>-<policy>.json
    python -m scripts.v2_realbench.waves --summarize waves-report        # the tables below, from real data

Pending CI: true value, share of the oracle, difference to `agent` and to the best simple policy, contacts per
customer-week, unsubscribe rate and the measured holdout estimate, per policy in each of the five worlds. The first
thing to read on the real data is whether `agent` beats the best simple policy in `heterogeneous`, `sleeping_dogs`
and `fatigue_heavy` and ties it in `homogeneous_small` and `null`; whether `uplift_waves` gets anywhere near it; and
how wide the measured estimate is next to the true value.

### Synthetic fixture (a check of the machinery, not evidence)

6,000 synthetic customers (`--fixture`), seed 0 only, 84 days. Produced by:

    python -m scripts.v2_realbench.waves --fixture --customers 6000 --seeds 1 \
        --world null,homogeneous_small,heterogeneous,sleeping_dogs,fatigue_heavy \
        --policy send_none,send_all_sms,send_all_email,weekly_sms,best_fixed,oracle,uplift_waves --json simple.json
    python -m scripts.v2_realbench.waves --fixture --customers 6000 --seeds 1 \
        --world heterogeneous,fatigue_heavy --policy agent,agent_greedy --json agent.json
    python -m scripts.v2_realbench.waves --summarize <folder holding both>

The fixture's customers are drawn from gamma and log-normal distributions, not from shoppers, and there is one seed, so
the intervals below only reflect the sampling of customers, not of worlds. `agent` and `agent_greedy` ran only in
`heterogeneous` and `fatigue_heavy` (about 6.5 minutes each); a "-" in the `vs agent` column means no agent ran in that
world.

#### null

6,000 customers, seeds [0]. True value in RUB over 84 days plus the 30-day tail, mean of the seeds. Best simple policy here: `send_none`.

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at day 84 (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_none | 0 | - | - | - | 0.00 | 0.0% | 171,717 ± 471,311 |
| send_all_sms | -252,754 | - | - | -252,754 [-254,968, -250,540] | 1.79 | 5.0% | -80,097 ± 471,306 |
| send_all_email | -33,058 | - | - | -33,058 [-33,820, -32,295] | 1.10 | 3.1% | 139,173 ± 471,302 |
| weekly_sms | -126,341 | - | - | -126,341 [-127,398, -125,284] | 0.91 | 1.9% | 45,937 ± 471,311 |
| best_fixed | -253,449 | - | - | -253,449 [-255,636, -251,262] | 1.81 | 5.0% | -80,896 ± 471,306 |
| uplift_waves | -14,702 | - | - | -14,702 [-15,193, -14,210] | 0.18 | 0.5% | 157,060 ± 471,311 |
| oracle | 0 | - | - | +0 [+0, +0] | 0.00 | 0.0% | 171,717 ± 471,311 |

#### homogeneous_small

6,000 customers, seeds [0]. True value in RUB over 84 days plus the 30-day tail, mean of the seeds. Best simple policy here: `send_all_email`.

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at day 84 (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_none | 0 | 0% | - | -66,857 [-70,897, -62,818] | 0.00 | 0.0% | 264,257 ± 418,423 |
| send_all_sms | -16,729 | -15% | - | -83,587 [-88,817, -78,356] | 1.79 | 5.0% | 293,105 ± 420,768 |
| send_all_email | 66,857 | 61% | - | - | 1.10 | 3.1% | 354,819 ± 419,328 |
| weekly_sms | 22,995 | 21% | - | -43,862 [-47,214, -40,511] | 0.91 | 1.8% | 316,051 ± 419,852 |
| best_fixed | -15,334 | -14% | - | -82,191 [-87,460, -76,923] | 1.82 | 5.0% | 294,949 ± 420,766 |
| uplift_waves | 12,965 | 12% | - | -53,893 [-57,187, -50,598] | 0.15 | 0.5% | 283,627 ± 418,637 |
| oracle | 109,951 | 100% | - | +43,094 [+38,961, +47,227] | 1.09 | 2.9% | 406,608 ± 420,336 |

#### heterogeneous

6,000 customers, seeds [0]. True value in RUB over 84 days plus the 30-day tail, mean of the seeds. Best simple policy here: `best_fixed`.

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at day 84 (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_none | 0 | 0% | -211,557 [-233,409, -189,704] | -161,009 [-189,014, -133,003] | 0.00 | 0.0% | -72,361 ± 401,798 |
| send_all_sms | 158,457 | 54% | -53,100 [-60,942, -45,258] | -2,552 [-3,957, -1,146] | 1.79 | 4.9% | 131,116 ± 406,551 |
| send_all_email | 144,530 | 49% | -67,027 [-82,842, -51,211] | -16,479 [-35,459, +2,502] | 1.10 | 3.0% | 89,679 ± 403,468 |
| weekly_sms | 154,333 | 52% | -57,223 [-61,897, -52,549] | -6,675 [-15,836, +2,485] | 0.91 | 1.8% | 119,987 ± 404,837 |
| best_fixed | 161,009 | 55% | -50,548 [-58,337, -42,759] | - | 1.81 | 4.9% | 133,631 ± 406,559 |
| agent | 211,557 | 72% | - | +50,548 [+42,759, +58,337] | 0.73 | 2.3% | 157,512 ± 405,034 |
| agent_greedy | 212,585 | 72% | +1,029 [-2,050, +4,108] | +51,577 [+43,555, +59,598] | 0.75 | 2.4% | 166,444 ± 405,423 |
| uplift_waves | 41,335 | 14% | -170,222 [-188,755, -151,689] | -119,674 [-144,090, -95,258] | 0.19 | 0.5% | -21,242 ± 402,263 |
| oracle | 294,544 | 100% | +82,987 [+76,286, +89,689] | +133,535 [+131,037, +136,033] | 1.00 | 2.6% | 254,935 ± 405,962 |

#### sleeping_dogs

6,000 customers, seeds [0]. True value in RUB over 84 days plus the 30-day tail, mean of the seeds. Best simple policy here: `weekly_sms`.

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at day 84 (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_none | 0 | 0% | - | -83,609 [-111,877, -55,341] | 0.00 | 0.0% | 165,558 ± 396,855 |
| send_all_sms | -5,585 | -1% | - | -89,194 [-105,468, -72,919] | 1.79 | 5.9% | 223,687 ± 402,432 |
| send_all_email | 68,058 | 17% | - | -15,551 [-33,893, +2,791] | 1.09 | 3.5% | 263,130 ± 398,507 |
| weekly_sms | 83,609 | 21% | - | - | 0.91 | 2.2% | 298,529 ± 400,107 |
| best_fixed | -8,558 | -2% | - | -92,168 [-108,657, -75,678] | 1.81 | 5.9% | 221,276 ± 402,428 |
| uplift_waves | 29,613 | 7% | - | -53,996 [-75,397, -32,595] | 0.27 | 0.9% | 225,979 ± 397,769 |
| oracle | 396,797 | 100% | - | +313,188 [+298,477, +327,899] | 0.89 | 2.2% | 603,137 ± 402,799 |

#### fatigue_heavy

6,000 customers, seeds [0]. True value in RUB over 84 days plus the 30-day tail, mean of the seeds. Best simple policy here: `weekly_sms`.

| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | Contacts / cust-wk | Unsub | Measured at day 84 (± 95% half-width) |
|---|---:|---:|---|---|---:|---:|---:|
| send_none | 0 | 0% | -34,390 [-40,825, -27,954] | -45,319 [-57,822, -32,816] | 0.00 | 0.0% | -322,779 ± 447,396 |
| send_all_sms | -135,913 | -140% | -170,303 [-176,088, -164,518] | -181,232 [-184,475, -177,990] | 1.65 | 19.9% | -440,545 ± 448,783 |
| send_all_email | 1,219 | 1% | -33,171 [-36,180, -30,162] | -44,100 [-53,048, -35,152] | 1.02 | 11.9% | -310,507 ± 447,944 |
| weekly_sms | 45,319 | 47% | +10,929 [+3,774, +18,084] | - | 0.89 | 6.2% | -251,135 ± 449,126 |
| best_fixed | -137,428 | -142% | -171,818 [-177,590, -166,046] | -182,747 [-185,709, -179,786] | 1.74 | 19.9% | -442,664 ± 448,799 |
| agent | 34,390 | 35% | - | -10,929 [-18,084, -3,774] | 0.77 | 8.3% | -274,771 ± 448,294 |
| agent_greedy | 41,728 | 43% | +7,338 [+5,327, +9,348] | -3,591 [-11,486, +4,303] | 0.65 | 6.5% | -273,309 ± 448,023 |
| uplift_waves | 31,933 | 33% | -2,457 [-5,859, +946] | -13,386 [-22,893, -3,878] | 0.21 | 1.6% | -280,270 ± 447,781 |
| oracle | 96,980 | 100% | +62,590 [+58,308, +66,872] | +51,661 [+47,901, +55,421] | 0.53 | 5.2% | -204,806 ± 448,569 |

What the fixture does show, about the benchmark rather than the product:

- **It separates the policies and punishes the right things.** In `null` every policy that contacts anyone loses exactly its
  costs and the oracle sends nothing. In `fatigue_heavy` blasting loses (`send_all_sms` -135,913, 19.9% opted out) while one
  SMS a week gains 45,319. In `sleeping_dogs` blasting by SMS is about break-even (-5,585) and one a week gains 83,609.
- **The oracle is far above everything**, and in `heterogeneous` the best fixed rule gets 55% of it, so there is room for a
  policy that targets.
- **`agent`** reached 72% of the oracle in `heterogeneous`, +50,548 [+42,759, +58,337] over the best simple policy
  (`best_fixed`), sending 0.73 messages per customer-week against its 1.81. In `fatigue_heavy` it was 10,929 below one SMS a week
  [-18,084, -3,774]. `agent_greedy` (no exploration) was +1,029 [-2,050, +4,108] over `agent` in `heterogeneous` and +7,338
  [+5,327, +9,348] in `fatigue_heavy`. One seed of a synthetic population: read them as "the code runs and produces plausible
  numbers", not as a result.
- **`uplift_waves` is the weakest non-trivial policy here**: 41,335 in `heterogeneous` (14% of the oracle), 0.19 messages per
  customer-week. At every weekly refit the gate's lower bound was negative, so it ran a segment rule. The reason is
  power: a customer's 14-day profit in roubles is dominated by their own real receipts, the slice holds about 800
  customers per arm by the last refit, and the effect to find is small next to that noise (I did not measure the
  noise separately; the gate's lower bounds, -14 to -77 across the refits, are the evidence). To check that this is not a
  bug, the same run with the average lift forced to +60% (`--lift 0.6`, heterogeneous, 1 seed): `send_all_sms` 3,246,320,
  oracle 3,304,313, `weekly_sms` 2,266,683 and `uplift_waves` 1,400,322 at 0.53 messages per customer-week; the gate still
  never chose the model (lower bounds -35 to -87 at each refit). So on this population the method as shipped cannot
  act on a randomised slice of this size even with a large effect, and its weekly wave caps it at one message a week by
  construction (it cannot reach `send_all_sms` even when volume pays). Whether that survives real X5 data and 10,000
  customers is exactly what the CI run is for.
- **A holdout cannot read the value at this size.** The measured estimate's 95% half-width is about 400,000 RUB on every row,
  against true values between -253,000 and +397,000, so measured values flip sign and rank policies wrongly (in `null`,
  `send_none` "measures" 171,717 with true value 0). That is the over-claiming the benchmark was asked to make
  visible: with the fixture's heavy-tailed receipts a plain holdout difference at 6,000 customers tells a business almost
  nothing about 12 weeks of messaging. Real X5 will have different tails; the table will say how different.

## Limits and next steps

- Nothing here has been run on X5; the loader is tested on files of X5's layout written from the fixture, not on the real
  files. If the real `purchases.csv.gz` ends earlier than assumed, the cutoff moves and the pre-cutoff history shortens;
  the run prints the real cutoff and history length on its `data:` line.
- One builder wrote the world, the truth and the `uplift_waves` adapter. Someone else should read `autonomy/gym/waves/truth.py`
  and `uplift.py` with the question "does this favour either method?" before the numbers are used for a decision.
- 3 seeds are seeds of the world (tree, personal factors, luck) on the same customers; they say little about how the result
  would change with other customers.
- Timing, offers, WhatsApp and content choice are outside this benchmark; T6 (a live pilot) is still the real answer.

