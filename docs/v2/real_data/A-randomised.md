# Family A: randomised campaigns, the agent scored off-policy

Written 20 Oct 2026 on branch `v2/realdata`. Every number below comes from a run in this session;
the command is given with each result, the JSON it wrote is in `/home/user/datasets/_results/`, and
the tables in the appendix are rendered from those JSONs by `scripts/v2_realdata/a_report.py`
(nothing was typed in by hand). No product code under `autonomy/` or `api/` was changed.

## The verdict in five lines

1. **On no randomised dataset did the agent beat the best simple policy.** Where messaging pays
   (Hillstrom, cheap e-mail) the agent ends up close to "send everyone the men's e-mail", and the
   paired comparison against that blanket rule is slightly negative (-0.014 profit per customer,
   95% CI -0.026 to -0.002): what it loses by leaving 2.5% of customers alone and sending 0.2% the
   women's e-mail while it explores.
2. **What the agent did learn from real data:** on Hillstrom it picked the men's e-mail over the
   women's for 99.8% of the customers it e-mailed, learnt only from the train customers' visits.
   That content choice is worth +0.095 profit per customer over a random pick of the two e-mails (paired DR, CI +0.004 to +0.186).
3. **Why it cannot do better: it is badly calibrated on real data.** The default (click) agent
   expected each Hillstrom e-mail to add **$4.08**; the RCT says **$0.38** (CI 0.21 to 0.54) per
   e-mailed customer, so about 10x. Two causes, both in product code: its "extra purchases per
   click" is a prior of 0.25 that nothing in a past campaign can correct (measured on the train
   split: 0.031 extra conversions per visit of an e-mailed customer, or 0.071 per visit the e-mail
   added), and the Hillstrom loader roughly doubles the order value (mean $242 vs $115.76 per real
   conversion). Part of the click-rate side also rests on this runner's choice to treat every
   visit after an e-mail as its click (10.5% of the unmailed control visited too). So it e-mails
   97.5% of people whether a message costs $0.001 or $0.25: on this data the price changed no
   decision. Criteo points the same way: the click agent expected $0.24 per contact before cost
   ($0.22 after the 0.023 cost); the RCT says about $0.023 before cost, i.e. about $0 after.
4. **Where there are no clicks, the default agent contacts nobody** (X5 SMS, MegaFon, Starbucks,
   Lenta: 0% contacted), because its only response signal is clicks and these campaigns logged
   none. The purchase-uplift configuration does contact people, but it sees every feature-only
   customer as the same prospect, so it cannot target: it sends to about half at random (MegaFon,
   Starbucks, where its estimate sits at the break-even cost by construction), to almost everyone
   (Lenta, 97.2%) or to no one (Criteo).
5. **Feature-based baselines show there was money to find.** At a contact cost where sending to
   everyone breaks even on train (a constructed scenario: the data has no costs), a plain T-learner on the anonymised features earns +0.49 per customer on
   MegaFon (CI +0.44 to +0.53) and +0.08 on Starbucks (CI +0.06 to +0.10), both over the best blanket arm. The
   agent cannot use those features at all: `CustomerState` has no place for them.

These are findings about the software, not about whether marketing works: the RCTs themselves show
real positive effects (Hillstrom, MegaFon, Starbucks, Lenta, X5).

## Skeptical review (20 Oct 2026, second pass)

Every headline run was repeated with the scripts unchanged, writing to a scratch folder, and each
JSON was compared value by value with the one in `/home/user/datasets/_results/`:
`a_hillstrom.py` (1,226 values), `a_x5.py --customers 100000 --seeds 2` (336),
`a_features_rct.py megafon` (547), `starbucks` (553), `lenta` (551) and `criteo --seeds 2` (817):
**all identical**, timings aside (Criteo again peaked at 7.26 GB, Hillstrom took 8 minutes).
The appendix matches a fresh `a_report.py` output line for line. Checked and found sound: the split
is by customer and every model, rule, break-even cost and best-blanket-arm choice uses train only;
agent states are folded from pre-campaign events only; propensities match the designs (1/3,
0.5, 0.85, Lenta's file share 0.751); DR terms are asserted equal to the harness's; the segment
policy reads only contexts. Corrected in this pass:

* Criteo calibration compared the agent's expected value **after** cost ($0.22) with the real
  value **before** cost ($0.023). Like for like: $0.24 vs $0.023 before cost, $0.22 vs about $0
  after. The 10x stands; the wording did not.
* The Hillstrom 10x is not all the lift prior: the loader's ~2x order value is part of it, and the
  0.031 "per click" is per visit of an e-mailed customer (0.071 per visit the e-mail added). The
  runner's visit-as-click mapping also inflates the learnt click rate.
* Scale: `map_frame` throughput is 24k to 39k rows/s (not 21k to 37k); memory is 1.6 to 1.9 KB
  of peak RSS per event above the receipts frame (the "1.2 to 1.4 KB per `Event` object" in the
  failure table was never measured).
* OOM narrative: the kernel log does not support "the third X5 run died at 13.4 GB" (that kill
  matches family C) nor that pid 20108 was another agent's process (its time matches the third X5
  run). All three X5 kills came at 4.8 to 9.5 GB of the process's own RSS. That the runner holding
  two agents caused the second kill is softened to "may have contributed".
* The quick Starbucks figure (-0.062) has no saved output; a rerun of the stated settings gives
  -0.059 [-0.136, +0.017], not significant.
* Lenta: the agent's 0.010 effect is its 100k train sample's own effect (0.0099), not "plus the
  prior". Open Bandit: ESS 66 / 154, and the coverage check is weak evidence.
* `a_features_rct.py criteo --nrows N` (documented as "a slice") silently took an all-treated
  head slice; it now stops with an error (adapter bug, same cause as failure 12).

## Method (the same for every dataset)

* **Split by customer, 50/50, seed 0** (`a_common.split_customers`). In every family-A file one row
  is one customer, so a row split is a customer split. Every model, rule and the agent's learning
  see TRAIN only; every number is computed on TEST.
* **The agent, two product configurations** (`a_common.run_agent_variants`):
  * `click` = `autonomy.runtime.agent.build_agent_policy(briefs, content)` as the runtime builds it
    (`ClickResponseModel`). The train customers' history is handed over through `policy.nightly([],
    history, now)` before the first decision, which is the product's warm start: SEND events with
    `content_id` and `logged_probability` (the design probability), their CLICKs where the data has
    a click-like signal (Hillstrom `visit`, Criteo `visit`), and the purchase history.
  * `purchase` = the same agent on `autonomy.learning.build_bundle()` (a `BetaResponseModel`: uplift
    = treated minus control conversion rate per agent segment and action). After the warm start
    the RCT's own assignments are fed as `Decision`s (treated = ACT in the agent group, control =
    NOTHING in the holdout group) with their window outcomes as `Reward`s to
    `autonomy.learning.learn_from_rewards`, with each train customer's segment as the agent labels it.
  * Customer states are folded from **pre-campaign events only** (no send, no outcome) with the
    production `StateFolder` through `rebuild_states`, as of 15 days after the campaign (so the
    train outcomes are in the agent's past). The agent then calls `prepare_day` and `decide` for
    every test customer.
  * `Decision` logs only the probability of the option it chose, not its full distribution, so the
    agent's policy is estimated by deciding for each test customer under several seeds (its
    Thompson draw is seeded by (seed, customer)); the share of seeds choosing each arm is the
    agent's probability of that arm, exploration floor included. Strictly, what is scored is the
    agent randomised over those 2 to 4 seeds: a valid policy, and an approximation of the agent's
    full action distribution, not that distribution itself.
* **Baselines:** send to none; send to all (each arm); uniform random; the best segment rule
  (repo `fit_segment_policy`, columns chosen by DR on a 30% validation slice of train, then refit
  on all of train); a T-learner uplift model (one ridge regression of the reward per arm on
  one-hot binned features, fitted on train, argmax on test; `a_common.uplift_t_learner`); on
  Hillstrom also "send the e-mail matching the customer's past category".
* **Scoring:** the repo's `autonomy.proof.replay.harness.evaluate`, which calls
  `autonomy.proof.ope.ips / snips / doubly_robust` with the design propensities. The reward model
  for DR is the repo default (`per_action_mean_model`, cross-fitted). Gains are paired DR
  differences, row by row: against send-to-none, and against the best blanket arm (the single arm
  with the best mean reward **on train**), with 95% normal intervals (`ope.mean_interval`).
* **Profit** needs assumptions the data does not carry; each is stated with its table. Where a
  dataset has no amounts (MegaFon, Starbucks, Lenta, Criteo) the value of a conversion is the
  agent's own default (order value 50 x margin 0.4 = 20) and the contact cost is the
  **break-even cost measured on train** (20 x the train average effect): at that cost sending to
  everyone earns nothing on average, so only targeting can make money. The agent is told the same
  cost through `build_agent_policy(costs=...)`.

Tiny tests of the evaluation code: `python3 -m pytest tests/v2/realdata -q` (3 tests, inline
fixtures, about 3 s).

## Hillstrom (64,000 customers, men's / women's / no e-mail, 1/3 each)

Command: `python3 -I scripts/v2_realdata/a_hillstrom.py` (8 to 11 minutes; run twice by the
runner and once more in review, identical numbers each time). Train 32,000, test 32,000. The agent decided 4 times per test customer (128,000
decisions per variant per cost).

**What worked.** The repo loader `hillstrom_events` read all 64,000 rows (251,114 events, 0
problems) once a `customer_id` column was added (the full public file has none). States folded in
8 s. The agent ran at 0.9 to 1.5 ms per decision.

**Results on TEST, profit = 40% of spend minus $0.001 per e-mail** (DR, per customer, 95% CI):

| Policy | Contacted | DR gain vs send-to-none | DR gain vs send all men's e-mail |
|---|---:|---:|---:|
| send all men's e-mail | 100% | +0.381 [+0.216, +0.547] | 0 |
| rule: e-mail matching past category | 100% | +0.374 [+0.213, +0.536] | -0.007 [-0.117, +0.102] |
| best segment rule (`channel`) | 100% | +0.351 [+0.186, +0.516] | -0.031 [-0.102, +0.041] |
| uplift model (T-learner) | 89.7% | +0.300 [+0.150, +0.450] | -0.081 [-0.226, +0.063] |
| **AGENT (click model)** | 97.5% | +0.368 [+0.206, +0.530] | **-0.014 [-0.026, -0.002]** |
| **AGENT (purchase model)** | 90.1% | +0.294 [+0.154, +0.433] | -0.088 [-0.142, -0.033] |
| random (uniform over 3 arms) | 66.7% | +0.187 [+0.104, +0.269] | -0.195 [-0.302, -0.088] |

Conversion and spend tell the same story (appendix): send-all-men's is the best policy on test,
the click agent is a whisker behind it (-0.0004 conversions per customer, CI -0.0006 to -0.0001),
nothing targeted beats it.

**Offer / content choice.** Holding "e-mail everyone" fixed and letting each policy only pick men's
vs women's: the click agent's pick is worth +0.375 per customer [+0.211, +0.538], against +0.280
[+0.156, +0.404] for a random pick (agent vs random pick, paired: +0.095 [+0.004, +0.186], from
`a_report.py`) and +0.381 for always-men's (agent vs always-men's -0.007
[-0.015, +0.002]). The agent learnt the right content from 21,384 logged sends and their visits;
it did not learn any per-person content preference that pays (nor did the T-learner or the
segment rule).

**With a costly contact ($0.25 per e-mail)** nothing reliably makes money (send-all-men's +0.132,
CI -0.033 to +0.298). The click agent made **exactly the same decisions** as at $0.001 (124,807
sends of 128,000), because it expects $3.83 per e-mail: a $0.25 cost cannot move a decision that
is wrong by 10x. The purchase agent did cut back (73.4% contacted) and lands at +0.005 [-0.114, +0.123].

**Calibration (the main finding).**

| | Agent's own expected net per e-mail it sent | What the e-mail made, per e-mailed customer (DR gain / contact share) |
|---|---:|---:|
| click model, $0.001 | $4.08 | $0.377 [0.211, 0.543] |
| purchase model, $0.001 | $0.50 | $0.326 [0.171, 0.481] |
| click model, $0.25 | $3.83 | $0.128 [-0.038, 0.294] |
| purchase model, $0.25 | $0.35 | $0.006 [-0.155, 0.168] |

Why the click model is 10x off: `uplift = lift_per_click x click_rate x personal`. Its click rate
(here, a visit after the e-mail) is learnt from the history, but `lift_per_click` stays at its prior
`LIFT_PER_CLICK_PRIOR = (0.25, 0.25)`: it is only ever updated from the agent's own decisions
against its own holdout (`LiftPerClick`, fed by `decide()` and `nightly()`), never from a past
campaign's control group, even when that control group is randomised and in the history. On the
train split the real figure is 0.031 extra conversions per visit for the men's e-mail and 0.023 for
the women's (conversion 1.15% with the men's e-mail vs 0.57% without; visit rate 18.5% with the
men's e-mail). Those are per visit of an e-mailed customer; the unmailed control visited at
10.5%, so per visit the e-mail *added* the figure is 0.071 (men's) and 0.078 (women's), still a
third of the prior. The click rate the agent learnt is itself inflated by this runner's adapter,
which counts every visit after an e-mail as its click (Hillstrom has no click data). The value side
is inflated too: the loader turns `history` (a year's spend, mean $242) into one or two purchases,
so the agent's "order value" is about twice the real value of a campaign conversion (mean $115.76
on train). Roughly: the lift prior accounts for 3x to 8x, the order value for 2x; the split
between them is not measured separately.

**Failures and fixes (Hillstrom).**

| What | Evidence (run) | Cause | Fix (file: one line) |
|---|---|---|---|
| The loader's SEND carries no propensity the warm start can read | 2,012 of 2,012 loader SENDs read back by `logged_sends` with `propensity=None` (test `test_hillstrom_loader_send_carries_no_logged_probability_for_the_warm_start`) | `autonomy/data/loaders.py` writes `attrs["probability"]`; `autonomy/learning/observations.py` reads `attrs["logged_probability"]` | `autonomy/data/loaders.py`: write the attr as `LOGGED_PROBABILITY` (`logged_probability`). The runner adds it itself. Harmless here (equal arms), wrong for any unequal design |
| Expected value 10x too high; price-insensitive | table above | lift per click never learnt from a past RCT's control group | `autonomy/runtime/agent.py` `_warm_start`: when the history holds a randomised no-send group (customers with a logged design probability and no send), set `self.lift`'s prior from treated-minus-control purchases per click in the history (the same CUPED ratio `LiftPerClick` computes) |
| The two e-mails could only be told apart by mislabelling them | men's = theme `quality`, women's = theme `novelty` (arbitrary) | `ContentItem` and `action_key` have theme / offer / format / channel but no product line or category | `autonomy/contracts/content.py`: an optional `category` on `ContentItem`, included in `autonomy/decide/models.action_key` |
| Approximate order values | mean history $242 vs conversion $115.76 | `hillstrom_events` books a year's spend as one or two orders | `autonomy/data/loaders.py`: book `history` as several orders of a typical size, or mark it so `ShrunkValueModel` ignores it |

## X5 RetailHero (SMS RCT on 200,039 clients; 45.8M real pre-campaign purchase lines)

This is the best real case: a randomised SMS campaign (`uplift_train.treatment_flg`, 99,981 of
200,039 treated, design 50/50 per the contest) with the real receipts of the same clients before
it (per the contest description: the data carries no campaign date; the last receipt is
2019-03-18 23:40 and this runner dates the SMS 2019-03-19 10:00 Moscow). `target` = bought in the
campaign window (no amount is given, so profit below is assumed).

**Scale, product path, all 200,039 labelled clients** (run 2, log `a_x5_oom_killed_2.log`, command
`python3 -I scripts/v2_realdata/a_x5.py --seeds 2`):

| Step | Rows | Time | Peak RSS |
|---|---:|---:|---:|
| Adapter (pandas, chunked): purchase lines -> receipts | 45,786,568 lines -> 8,045,201 receipts | 86.6 s | 3.05 GB |
| `map_frame` receipts (`EventMapping`, type purchase, Europe/Moscow) | 4,024,935 rows | 109.2 s | 8.35 GB |
| `map_frame` signup + SMS consent (invented consent) | 400,078 rows | 14.0 s | 8.38 GB |
| `rebuild_states` (InMemoryEventStore, production `StateFolder`) | 4,425,013 events, 0 problems -> 200,039 states | 163.6 s | 8.97 GB |
| Click agent: warm start on 49,968 logged train sends | | 77.8 s | 10.44 GB |
| Click agent: decide, 100,020 test clients x 2 seeds | 200,040 decisions | 92.6 s (0.46 ms each) | 10.44 GB |

Growth with size (command `python3 -I scripts/v2_realdata/a_x5.py --scale-only --scale-sizes
10000,50000,100000`, random clients of all 400,162):

| Clients | Events | `map_frame` | `rebuild_states` | Peak RSS (includes a 1.8 GB receipts frame) |
|---:|---:|---:|---:|---:|
| 10,000 | 220,196 | 5.7 s | 6.9 s | 2.0 GB |
| 50,000 | 1,105,947 | 46.7 s | 46.4 s | 3.7 GB |
| 100,000 | 2,211,706 | 88.1 s | 122.9 s | 5.9 GB |

So about 24k to 39k rows a second through `map_frame` (rows = receipts + profile rows) and 18k to
32k events a second through the fold, and about 1.6 to 1.9 KB of peak RSS per event above the
1.8 GB receipts frame (1.0 KB at 10k clients). Peak RSS is `ru_maxrss`, the process's lifetime
peak, so each figure covers everything held at that point (event list, store, states, frame
copies), not the size of one `Event`. The full 200,039-client run did not finish: it was killed
three times in the 14.3 GB memory cgroup it runs in (`memory.limit_in_bytes` = 14,343,688,192).
That limit is **shared with other agents' processes in this environment**. The kernel log
(`dmesg`) has three python kills whose times match the three X5 logs: at 4.8 GB (19:50, during
`map_frame`), 9.4 GB (20:05, building the purchase agent) and 9.5 GB (20:24, pid 20108, building
the click agent) of the killed process's own RSS, each below the 10.4 GB the same workload reached and survived in run 2; so the
kills were caused by the cgroup's total, not by this process alone passing 14.3 GB. (An earlier
version of this note said the third X5 run died at 13.4 GB and that pid 20108 was another agent's
process; the timings say pid 20108 was most likely this X5 run, and the 13.4 GB kill at 20:36,
pid 24672, matches family C's 1M-record run in `C-scale.md`.) What is measured is the 10.4 GB peak
of run 2 for 200,039 clients with one agent. All 400,162 clients (about 8.8M events) would need
roughly twice that: an extrapolation, not a run. **The agent comparison below is therefore on a random 100,000 of
the labelled clients** (`python3 -I scripts/v2_realdata/a_x5.py --customers 100000 --seeds 2`,
about 6 minutes, peak 5.3 GB): train 50,000, test 50,000.

**What the agent saw.** Real recency, frequency and spend per client. But 88,580 of the 100,000
states are labelled `vip` (177,105 of 200,039 in the full run) because the folder's VIP bar is 500
in the business's currency and these are roubles. The agent's own segmenter (new / lapsed / light /
medium / heavy) does use the real purchase rhythm.

**Results on TEST** (`target`, per client; profit assumed = 25% margin x the client's own
pre-period mean receipt if they bought, minus 2 RUB per SMS):

| Policy | Contacted | `target` gain vs send-to-none | `target` vs send to all | profit (RUB) vs send-to-none | profit vs send to all |
|---|---:|---:|---:|---:|---:|
| send to all | 100% | +0.0351 [+0.0266, +0.0436] | 0 | +2.15 [+0.60, +3.70] | 0 |
| best segment rule (`recency_days`; profit: `recency_days+n_receipts`) | 100% / 77.9% | +0.0351 | 0 | +2.16 [+0.77, +3.55] | +0.01 [-0.69, +0.70] |
| uplift model (T-learner) | 75.6% / 56.6% | +0.0274 [+0.0199, +0.0349] | -0.0077 [-0.0118, -0.0037] | +1.06 [-0.32, +2.44] | -1.09 [-1.80, -0.37] |
| **AGENT (click model)** | **0%** | 0 | -0.0351 [-0.0436, -0.0266] | 0 | -2.15 [-3.70, -0.60] |
| **AGENT (purchase model)** | 70.3% | +0.0260 [+0.0188, +0.0333] | -0.0091 [-0.0131, -0.0051] | +2.28 [+0.81, +3.76] | +0.13 [-0.26, +0.52] |
| random 50/50 | 50% | +0.0176 [+0.0133, +0.0218] | -0.0176 [-0.0218, -0.0133] | +1.07 [+0.30, +1.85] | -1.07 [-1.85, -0.30] |

Reading: the SMS worked (+3.5 points of buyers). The default agent sent nothing: the campaign has
SENDs but no clicks, so its warm start learnt a click rate of about zero for SMS from 24,866 sends
and every bid came out at minus the SMS cost ("Left alone: no message is worth more than the cost
of contacting (best -$2.00 vs $0.00)", where "$" is roubles). The purchase-uplift agent learnt from
the same 50,000 train clients and contacted 70.3%; it gets fewer buyers than sending to all
(-0.0091) and, on the assumed profit, cannot be told apart from sending to all (+0.13 RUB, CI
-0.26 to +0.52). Its targeting signal is the agent segment x action conversion uplift; it did not
find clients worth skipping with confidence. Neither did the T-learner (it loses to send-to-all
on both rewards).

**What a customer would need to do here:** aggregate purchase lines to receipts (or map lines and
accept 10x the events), invent SMS consent, write the campaign as SEND events with
`logged_probability`, know to use the purchase bundle because SMS has no clicks, override the SMS
cost in roubles (default 0.02) and live with `vip` meaning nothing. The Data Doctor API cannot take
these files at all (CSV only, 20 MB cap; the purchases file is 670 MB gzipped).

## Feature-only RCTs: MegaFon, Starbucks promotion, Lenta

Command per dataset: `python3 -I scripts/v2_realdata/a_features_rct.py <megafon|starbucks|lenta>`.
Baselines fitted on the whole train split and scored on the whole test split; the agent learnt from
100,000 train customers (all 63,092 for Starbucks) and decided twice for 50,000 test customers;
every policy is also scored on that agent sample (appendix).

The adapter can only give the agent a signup and an (invented) e-mail consent per customer. The
files' features (`X_1..X_50`, `V1..V7`, Lenta's aggregates) have nowhere to go: `CustomerState`
has no attribute map, and the agent's segmenter (`new / lapsed / light / medium / heavy`) reads only
purchases and dates. So every customer is the same prospect to the agent.

| Dataset (rows; treated share) | Outcome effect of sending to all (test) | Profit at break-even cost: T-learner vs best blanket arm | Agent (click) | Agent (purchase) |
|---|---|---|---|---|
| MegaFon (600,000; 0.501) | +0.0479 conversions [+0.0450, +0.0507] | **+0.486 [+0.443, +0.528]** | contacts 0%: +0 | contacts 44.5% at random: +0.022 conversions [+0.018, +0.026], profit -0.020 [-0.101, +0.060] |
| Starbucks (126,184; 0.500) | +0.0098 purchases [+0.0081, +0.0115] | **+0.083 [+0.065, +0.102]** (segment rule V4+V5: +0.090 [+0.070, +0.111]) | 0% | 49.6% at random: profit -0.001 [-0.024, +0.022] |
| Lenta (687,029; 0.751) | +0.0083 responses [+0.0060, +0.0107] | +0.037 [+0.006, +0.068] | 0% | 97.2%: profit -0.015 [-0.136, +0.107] |

Break-even costs (20 x train effect): MegaFon 1.023, Starbucks 0.184, Lenta 0.136.

Reading: the click agent never contacts anyone because these campaigns logged sends but no clicks
(next section). The purchase agent learnt the average effect correctly and, since its estimate
sits at the break-even cost by construction, it explores about half the time (MegaFon, Starbucks):
that is the Thompson sampling working as designed on an option worth about zero, not targeting.
On Lenta it contacts almost everyone: its own expected net was +$0.06 per send after the 0.136
cost, i.e. it learnt an effect of about 0.010, which is what its own 100,000-customer train
sample shows (0.0099; sampling noise against 0.0068 on the whole train split), so the send looked
worth it. A plain feature model would have made money on MegaFon and Starbucks; the agent has no
way to use features.

A caution about small samples: a first quick Starbucks run on 5,000 test customers and one seed put
the purchase agent at -0.062 [-0.116, -0.008]; that run's exact settings and output were not kept,
so the figure cannot be checked. Re-running `a_features_rct.py starbucks --agent-test 5000 --seeds 1`
in review gave -0.059 [-0.136, +0.017] vs send-to-none (contacting 49.1%). The full run (50,000
customers, two seeds) gives -0.001 [-0.024, +0.022]. Small agent samples are noisy, which is why
every number in the tables is from the full runs.

## Criteo uplift v2.1 (13,979,592 rows, 85/15): scale

Command: `python3 -I scripts/v2_realdata/a_features_rct.py criteo --seeds 2` (about 10 minutes,
peak 7.3 GB). All 13,979,592 rows read (43 s, float32), treated share 0.8500 (design 0.85); train
6,989,796, test 6,989,796. The treatment is eligibility for ads, not a message, and `visit` stands
in for the click. The agent learnt from 100,000 train rows and decided twice for 50,000 test rows.

**Scale of the evaluation code:** the repo's segment-rule fit plus the T-learner on 7.0M train rows
took 79 to 87 s per reward, and IPS / SNIPS / DR of five policies on 7.0M test rows 30 to 39 s.
The estimators themselves are vectorised and cheap; the cost is in building string segment
labels for millions of rows.

**Results on TEST** (profit = 20 x conversion - 0.0231 per contact, the train break-even cost):

| Policy (fitted for profit) | Contacted | profit gain vs best blanket arm (send to all, chosen on train) |
|---|---:|---:|
| send to none (full test, 7.0M) | 0% | +0.0002 [-0.0017, +0.0021] (break-even by design) |
| best segment rule (`f2+f8`, full test) | 15.0% | **+0.0172 [+0.0166, +0.0178]** |
| uplift model (T-learner, full test) | 22.4% | **+0.0162 [+0.0156, +0.0168]** |
| AGENT (click model, 50k sample) | 97.6% | -0.0014 [-0.0027, -0.0001] |
| AGENT (purchase model, 50k sample) | 0% | -0.0002 [-0.0209, +0.0204] |

On conversions alone, sending to all adds +0.00115 [+0.00105, +0.00124] per customer, and the
T-learner fitted for conversion keeps +0.00109 [+0.00100, +0.00119] of that while contacting
35.0%.

The same pattern as Hillstrom: the click agent expects **$0.24 per contact before cost** (about its
click rate of 4.9% x the lift-per-click prior 0.25 x a value of 20; it reports $0.22 after the
0.023 cost), while the real value of a contact is about 20 x 0.00115 = $0.023 before cost, i.e.
about $0 after it (by construction of the cost). So it contacts everyone and loses a little to the
blanket rule. Caveats: `visit` stands in for a click (the control visits at 3.8% without any ad),
and the treatment is ad eligibility, not a message. The purchase agent, on 100,000 train rows with a 0.3% conversion rate, finds
the effect too small to pay the cost and contacts no one. With 7M rows, a two-feature segment rule
(contact 15%) earns 0.017 per customer more than any blanket policy: there is targeting value in
Criteo that the agent cannot reach, because `f0..f11` cannot enter its state.

## Open Bandit (ZOZOTOWN), 4 x 10,000-row samples

Command: `python3 -I scripts/v2_realdata/a_open_bandit.py`.

**The agent cannot be run on this data.** Its actions are channel x offer x content (x item only
through a refill or recommender lookup), not "which of 80 items in which of 3 slots", and it has no
way to report a probability per item. What was checked is the OPE code on real logged propensities:
the uniform-random policy's value estimated from the Thompson-sampling (`bts`) log, against the CTR
the uniform policy actually got in the `random` log of the same campaign and week.

| Campaign | Random log CTR (truth) [95% CI] | IPS from the bts log | DR from the bts log | ESS of 10,000 | Max weight |
|---|---|---|---|---:|---:|
| all (80 items) | 0.00380 [0.00259, 0.00501] | 0.00236 [0.00065, 0.00407] | 0.00331 [0.00155, 0.00507] | 340 | 277.8 |
| men (34 items) | 0.00460 [0.00327, 0.00593] | 0.00301 [0.00149, 0.00453] | 0.00314 [0.00161, 0.00466] | 656 | 178.3 |

DR's interval covers the truth on both; IPS covers it on `all` and misses it on `men` by a hair
(0.00453 vs 0.00460). Both point estimates are below the truth, which is expected noise with an
ESS of a few hundred (the logs are different traffic, not the same rows). The repo's `replay` on
5,000 test rows of the random log cannot tell its segment policy from the logging policy (ESS 66
on `all`, 154 on `men`). This is a weak check: the "truth" is itself a 10,000-row estimate (CI
about +/-0.0013), all four point estimates sit 13% to 38% below it, and intervals this wide would
also cover a fairly biased estimator. It shows the code runs on real logged propensities and is
not wildly off; it does not validate unbiasedness.
The full 26M-row dataset (`research.zozo.com`) is blocked here.

## Starbucks rewards app (306,534 events, 17,000 people): not used

Simulated data whose offer assignment rule is not documented, so there is no known propensity and
no valid off-policy estimate. It belongs with families B/D for state-building tests.

## What a customer would have to do (and could not do without code)

1. **Add a customer id** where the export has none (Hillstrom, MegaFon, Lenta, Criteo, Starbucks
   promo all key by row).
2. **Invent consent.** No dataset carries consent; without a `consent_granted` event per customer
   the agent contacts nobody (every adapter here writes one, and says so).
3. **Write the past campaign as events by hand**: a SEND per treated customer with `content_id`
   and `logged_probability` (the loader's `probability` is ignored), CLICKs tied by `decision_id`,
   and, for the purchase configuration, `Decision` + `Reward` records through
   `learn_from_rewards`. There is no "here is my A/B test file" input anywhere.
4. **Choose the configuration in code.** The runtime builds the click agent; a business whose
   channel has no click tracking (SMS, direct mail, call centre, ads) must know to pass
   `bundle=build_bundle()` or the agent will never send.
5. **Label content creatively.** Two e-mails that differ by product line must carry different
   themes for the agent to tell them apart.
6. **Override money in its own currency.** Channel costs (`DEFAULT_CHANNEL_COSTS`: SMS 0.02),
   the folder's VIP bar (500 revenue) and the explanation text ("$") assume dollars.
7. **Bring features some other way.** Customer attributes and model scores cannot reach the bids
   (the only `scores` the bidders read are the refill ones and `days_since_cart`).
8. **Estimate the agent's policy by re-deciding.** To score the agent off-policy you need its
   probability of every action; the decision only logs the chosen one, so this runner decides
   several times per customer.

## Failures, causes and fixes (all datasets)

| # | Failure | Evidence | Cause | Fix (file: one line) |
|---|---|---|---|---|
| 1 | Agent overestimates a message's value about 10x | Hillstrom: $4.08 expected vs $0.38 realised per e-mail (net of cost); Criteo: $0.24 expected before cost vs about $0.023 real (net: $0.22 vs about $0) | `LIFT_PER_CLICK_PRIOR` 0.25 never corrected by a past RCT (measured 0.031 per treated visit, 0.071 per added visit), plus the Hillstrom loader's ~2x order value (row "Approximate order values"); the visit-as-click adapter choice also inflates the click rate | `autonomy/runtime/agent.py` `_warm_start`: calibrate `self.lift` from the history's randomised no-send customers |
| 2 | Contact cost changed no decision | Hillstrom: identical policy matrices at $0.001 and $0.25 (the agent does subtract the cost: $4.08 -> $3.83 expected; no bid crossed zero) | consequence of 1 | as 1 |
| 3 | Default agent never contacts when the history has sends but no clicks | X5 SMS, MegaFon, Starbucks, Lenta (the last three mapped as e-mail with no click signal): 0% contacted; reason "no message is worth more than the cost of contacting" | `ClickResponseModel` learns click rate ~0 from tens of thousands of sends with no click | `autonomy/runtime/agent.py` `ClickResponseModel.warm_start`: skip (treat as unknown) a channel with zero clicks in the whole history, and fall back to the purchase `BetaResponseModel` for it |
| 4 | Loader propensity lost | 2,012/2,012 sends `propensity=None` | attr name `probability` vs `logged_probability` | `autonomy/data/loaders.py`: write `logged_probability` |
| 5 | Features cannot reach the agent | every feature-only RCT: agent sends at random or not at all; a T-learner earns +0.49 per customer on MegaFon at the constructed break-even cost | `CustomerState` has no attribute map; bidders read no general score | `autonomy/decide/bidders.py`: read an optional per-action uplift score (`scores["uplift:<action_key>"]`) written by a slow model, as the refill scores are |
| 6 | No product-line label on content | Hillstrom men's vs women's needed fake themes | `action_key` = channel, offer, theme, format | `autonomy/contracts/content.py`: optional `category` on `ContentItem`, used in `action_key` |
| 7 | Currency assumed to be dollars | X5: 177,105 of 200,039 clients (88.5%) labelled `vip`; reasons print "$" for roubles | `SegmentRules.vip_min_revenue = 500`; `build_agent_policy` does not pass a currency to `AutonomousPolicy` (default "$"); `DEFAULT_CHANNEL_COSTS` unitless | `autonomy/data/folder.py`: set the VIP bar from the business's own revenue distribution; `autonomy/runtime/agent.py`: take `currency` and pass it on |
| 8 | Full action distribution not logged | runner had to decide 2-4x per customer | `Decision` carries only `probability` of the chosen option; `Pick.probabilities` is dropped | `autonomy/decide/policy.py`: keep the per-option probabilities (and their actions) on the `Decision` |
| 9 | In-memory event path is heavy: a few million events take most of a 14 GB box | X5: 4,425,013 events reached 8.97 GB in `rebuild_states` and 10.4 GB with the click agent; three full runs were OOM-killed in the shared 14.3 GB cgroup, each at 4.8 to 9.5 GB of its own RSS (kernel log), i.e. with other processes holding the rest | about 1.6 to 1.9 KB of peak RSS per event above the 1.8 GB receipts frame (event list, store, states and frame copies together; not a per-`Event` measurement); `map_frame` returns a list | `autonomy/data/mapping.py`: a streaming `iter_frame` that yields events in chunks, folded as they come; or the Postgres store (not tested here) |
| 10 | Open Bandit action space not expressible | no agent run possible | no item-in-slot action | none proposed; out of scope for v2.0 |
| 11 | Criteo official URL blocked | `go.criteo.net` unreachable | `autonomy/proof/replay/fetch.py` lists only that URL | `autonomy/proof/replay/fetch.py`: add the GitHub release mirror `jszymon/uplift_sklearn_data` |
| 12 | A head slice of Criteo has no control rows | the file is in 13 blocks; rows 1 to 5,571,799 are all treated, so the first 1M or 5M rows are 100% treated (checked with pandas on the `treatment` column) | `read_criteo_uplift(path, nrows=N)` reads the first N rows and, with the default `treatment_rate=0.85`, gives every row propensity 0.85 with no warning | `autonomy/proof/replay/datasets.py` `read_criteo_uplift`: raise when the slice has only one arm, and offer a seeded random row sample instead of `nrows` |

Runner problems I hit (not product bugs): the environment's 14.3 GB memory cgroup is shared with
other agents' processes, and four of my runs were OOM-killed in it (three full X5 runs at 4.8, 9.4
and 9.5 GB of their own RSS, and the first full Criteo run at 7.2 GB, per `dmesg`). The runner
used to keep the first agent alive while building the second; that may have added to the second
X5 kill (at 9.4 GB, below the 10.4 GB the run had already survived, so other processes were
holding memory too) and was fixed with `del` + `gc.collect()` in `a_common.run_agent_variants`.
Logs: `a_x5_oom_killed*.log`. My full Criteo run coincided with the OOM kill of another agent's
process in the same cgroup (the family-C `c_sweep.py`, pid 27807 at 11.5 GB, at 21:00; I started
the Criteo run when the cgroup held 2.4 GB). Anyone re-running these scripts here should run them
alone, or with `--customers` / `--agent-test` set lower.

## What can and cannot be concluded

* **Can:** these are randomised trials with known assignment probabilities, so IPS / SNIPS / DR on
  a held-out customer split are unbiased estimates of what each policy would have earned on
  customers like the test ones, in that campaign. The intervals are normal 95% intervals on tens
  or hundreds of thousands of customers, one per comparison, with no correction for the dozens of
  comparisons in these tables; an interval that only just excludes zero (e.g. Hillstrom click agent
  vs send-all-men's, -0.014 [-0.026, -0.002]) should be read as suggestive, not settled.
* **Cannot:** the agent is scored on one decision per customer at one moment. Its learning over
  weeks (its own holdout, the lift-per-click update, fatigue, timing) is not exercised by a
  one-shot trial, and the real campaigns had no unsubscribe or complaint data, so the attention
  price is untested. Profit figures depend on stated margins and costs. Hillstrom is 2008 data.
  MegaFon's data is generated. Starbucks is an exercise dataset. A loss to send-to-all here does
  not mean the agent would lose in a live programme with its own holdout, but nothing here shows
  it would win either.

## Files

* Runners: `scripts/v2_realdata/a_common.py` (split, baselines, agent driver, OPE),
  `a_hillstrom.py`, `a_x5.py`, `a_features_rct.py`, `a_open_bandit.py`, `a_report.py` (tables).
* Tests: `tests/v2/realdata/test_family_a.py`.
* Results: `/home/user/datasets/_results/a_*.json`, `*.log`, `*_pi.npz` (policy matrices).

## Appendix: full tables

Rendered by `python3 -I scripts/v2_realdata/a_report.py`. Values per customer; DR gains are paired
row-by-row differences with 95% normal intervals.

- Hillstrom, cost 0.001: the click agent's content pick vs a random pick (both e-mail everyone): +0.0950 [+0.0036, +0.1864]

## Hillstrom

Command: `python3 -I scripts/v2_realdata/a_hillstrom.py`

### conversion (conversion)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0057 | 0.0057 | 0.0057 | +0.0000 [+0.0000, +0.0000] | -0.0079 [-0.0105, -0.0053] |
| send all men's e-mail | 100.0% | 0.0136 | 0.0136 | 0.0136 | +0.0079 [+0.0053, +0.0105] | +0.0000 [+0.0000, +0.0000] |
| send all women's e-mail | 100.0% | 0.0085 | 0.0085 | 0.0085 | +0.0028 [+0.0006, +0.0051] | -0.0050 [-0.0079, -0.0022] |
| random (uniform over 3 arms) | 66.7% | 0.0093 | 0.0093 | 0.0093 | +0.0036 [+0.0022, +0.0049] | -0.0043 [-0.0060, -0.0027] |
| rule: e-mail matching past category | 100.0% | 0.0136 | 0.0136 | 0.0136 | +0.0078 [+0.0052, +0.0105] | -0.0000 [-0.0020, +0.0019] |
| best segment rule (channel) | 100.0% | 0.0126 | 0.0126 | 0.0126 | +0.0069 [+0.0043, +0.0095] | -0.0010 [-0.0021, +0.0002] |
| uplift model (T-learner, ridge) | 89.7% | 0.0108 | 0.0108 | 0.0108 | +0.0051 [+0.0028, +0.0073] | -0.0028 [-0.0049, -0.0007] |
| AGENT (click model) | 97.5% | 0.0132 | 0.0132 | 0.0132 | +0.0075 [+0.0050, +0.0101] | -0.0004 [-0.0006, -0.0001] |
| AGENT (purchase model) | 90.1% | 0.0116 | 0.0117 | 0.0116 | +0.0059 [+0.0038, +0.0081] | -0.0020 [-0.0030, -0.0010] |

### spend (spend)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.6192 | 0.6178 | 0.6180 | +0.0000 [+0.0000, +0.0000] | -0.9560 [-1.3688, -0.5433] |
| send all men's e-mail | 100.0% | 1.5724 | 1.5732 | 1.5741 | +0.9560 [+0.5433, +1.3688] | +0.0000 [+0.0000, +0.0000] |
| send all women's e-mail | 100.0% | 1.0644 | 1.0662 | 1.0654 | +0.4474 [+0.0882, +0.8066] | -0.5086 [-0.9711, -0.0462] |
| random (uniform over 3 arms) | 66.7% | 1.0853 | 1.0853 | 1.0859 | +0.4678 [+0.2610, +0.6746] | -0.4882 [-0.7548, -0.2217] |
| rule: e-mail matching past category | 100.0% | 1.5563 | 1.5544 | 1.5556 | +0.9376 [+0.5340, +1.3412] | -0.0184 [-0.2928, +0.2560] |
| best segment rule (channel) | 100.0% | 1.4913 | 1.4991 | 1.4979 | +0.8798 [+0.4665, +1.2932] | -0.0762 [-0.2539, +0.1015] |
| uplift model (T-learner, ridge) | 89.7% | 1.3669 | 1.3711 | 1.3703 | +0.7522 [+0.3761, +1.1283] | -0.2038 [-0.5646, +0.1569] |
| AGENT (click model) | 97.5% | 1.5392 | 1.5380 | 1.5395 | +0.9214 [+0.5161, +1.3268] | -0.0346 [-0.0641, -0.0050] |
| AGENT (purchase model) | 90.1% | 1.3541 | 1.3547 | 1.3550 | +0.7369 [+0.3880, +1.0859] | -0.2191 [-0.3546, -0.0836] |

### profit (profit = 0.4 x spend - 0.001 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.2477 | 0.2471 | 0.2472 | +0.0000 [+0.0000, +0.0000] | -0.3814 [-0.5465, -0.2163] |
| send all men's e-mail | 100.0% | 0.6280 | 0.6283 | 0.6286 | +0.3814 [+0.2163, +0.5465] | +0.0000 [+0.0000, +0.0000] |
| send all women's e-mail | 100.0% | 0.4248 | 0.4255 | 0.4252 | +0.1780 [+0.0343, +0.3217] | -0.2034 [-0.3884, -0.0185] |
| random (uniform over 3 arms) | 66.7% | 0.4335 | 0.4335 | 0.4337 | +0.1865 [+0.1037, +0.2692] | -0.1950 [-0.3016, -0.0883] |
| rule: e-mail matching past category | 100.0% | 0.6215 | 0.6207 | 0.6213 | +0.3740 [+0.2126, +0.5355] | -0.0074 [-0.1171, +0.1024] |
| best segment rule (channel) | 100.0% | 0.5955 | 0.5986 | 0.5981 | +0.3509 [+0.1856, +0.5163] | -0.0305 [-0.1016, +0.0406] |
| uplift model (T-learner, ridge) | 89.7% | 0.5458 | 0.5475 | 0.5472 | +0.3000 [+0.1495, +0.4504] | -0.0814 [-0.2257, +0.0629] |
| AGENT (click model) | 97.5% | 0.6147 | 0.6142 | 0.6148 | +0.3676 [+0.2055, +0.5297] | -0.0138 [-0.0256, -0.0020] |
| AGENT (purchase model) | 90.1% | 0.5407 | 0.5410 | 0.5411 | +0.2939 [+0.1543, +0.4334] | -0.0875 [-0.1417, -0.0333] |

### content_choice_profit (profit = 0.4 x spend - 0.001 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| random content (50/50) | 100.0% | 0.5264 | 0.5269 | 0.5269 | +0.2797 [+0.1556, +0.4038] | -0.1017 [-0.1942, -0.0092] |
| send all men's e-mail: its content pick | 100.0% | 0.6280 | 0.6283 | 0.6286 | +0.3814 [+0.2163, +0.5465] | +0.0000 [+0.0000, +0.0000] |
| send all women's e-mail: its content pick | 100.0% | 0.4248 | 0.4255 | 0.4252 | +0.1780 [+0.0343, +0.3217] | -0.2034 [-0.3884, -0.0185] |
| rule: e-mail matching past category: its content pick | 100.0% | 0.6215 | 0.6207 | 0.6213 | +0.3740 [+0.2126, +0.5355] | -0.0074 [-0.1171, +0.1024] |
| best segment rule (channel): its content pick | 100.0% | 0.5955 | 0.5986 | 0.5981 | +0.3509 [+0.1856, +0.5163] | -0.0305 [-0.1016, +0.0406] |
| uplift model (T-learner, ridge): its content pick | 100.0% | 0.5407 | 0.5410 | 0.5410 | +0.2938 [+0.1356, +0.4520] | -0.0876 [-0.2208, +0.0456] |
| AGENT (click model): its content pick | 100.0% | 0.6211 | 0.6217 | 0.6219 | +0.3747 [+0.2110, +0.5384] | -0.0067 [-0.0149, +0.0015] |
| AGENT (purchase model): its content pick | 100.0% | 0.5787 | 0.5803 | 0.5804 | +0.3332 [+0.1849, +0.4815] | -0.0482 [-0.0962, -0.0001] |

- agent click: {'act': 124807, 'nothing': 3193}, arms {'1': 124543, '0': 3193, '2': 264}, mean expected net of its sends 4.082, 1.003 ms/decision
- agent purchase: {'act': 115360, 'nothing': 12640}, arms {'2': 23505, '1': 91855, '0': 12640}, mean expected net of its sends 0.495, 1.54 ms/decision
- segment rule columns chosen on validation: ['channel']

### profit (profit = 0.4 x spend - 0.25 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.2477 | 0.2471 | 0.2472 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send all men's e-mail | 100.0% | 0.3791 | 0.3793 | 0.3796 | +0.1324 [-0.0327, +0.2975] | +0.1324 [-0.0327, +0.2975] |
| send all women's e-mail | 100.0% | 0.1762 | 0.1765 | 0.1762 | -0.0710 [-0.2147, +0.0727] | -0.0710 [-0.2147, +0.0727] |
| random (uniform over 3 arms) | 66.7% | 0.2676 | 0.2676 | 0.2677 | +0.0205 [-0.0623, +0.1032] | +0.0205 [-0.0623, +0.1032] |
| rule: e-mail matching past category | 100.0% | 0.3722 | 0.3717 | 0.3723 | +0.1250 [-0.0364, +0.2865] | +0.1250 [-0.0364, +0.2865] |
| best segment rule (newbie+mens+womens) | 55.0% | 0.3003 | 0.2974 | 0.2977 | +0.0505 [-0.0684, +0.1695] | +0.0505 [-0.0684, +0.1695] |
| uplift model (T-learner, ridge) | 63.6% | 0.2613 | 0.2598 | 0.2595 | +0.0123 [-0.1062, +0.1308] | +0.0123 [-0.1062, +0.1308] |
| AGENT (click model) | 97.5% | 0.3720 | 0.3717 | 0.3720 | +0.1248 [-0.0373, +0.2870] | +0.1248 [-0.0373, +0.2870] |
| AGENT (purchase model) | 73.4% | 0.2524 | 0.2522 | 0.2519 | +0.0047 [-0.1136, +0.1230] | +0.0047 [-0.1136, +0.1230] |

### content_choice_profit (profit = 0.4 x spend - 0.25 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| random content (50/50) | 100.0% | 0.2776 | 0.2779 | 0.2779 | +0.0307 [-0.0934, +0.1548] | -0.1017 [-0.1942, -0.0092] |
| send all men's e-mail: its content pick | 100.0% | 0.3791 | 0.3793 | 0.3796 | +0.1324 [-0.0327, +0.2975] | +0.0000 [+0.0000, +0.0000] |
| send all women's e-mail: its content pick | 100.0% | 0.1762 | 0.1765 | 0.1762 | -0.0710 [-0.2147, +0.0727] | -0.2034 [-0.3884, -0.0185] |
| rule: e-mail matching past category: its content pick | 100.0% | 0.3722 | 0.3717 | 0.3723 | +0.1250 [-0.0364, +0.2865] | -0.0074 [-0.1171, +0.1024] |
| best segment rule (newbie+mens+womens): its content pick | 100.0% | 0.3142 | 0.3139 | 0.3138 | +0.0666 [-0.0778, +0.2110] | -0.0658 [-0.1643, +0.0327] |
| uplift model (T-learner, ridge): its content pick | 100.0% | 0.2506 | 0.2502 | 0.2502 | +0.0030 [-0.1406, +0.1465] | -0.1295 [-0.2499, -0.0090] |
| AGENT (click model): its content pick | 100.0% | 0.3723 | 0.3727 | 0.3729 | +0.1257 [-0.0380, +0.2894] | -0.0067 [-0.0149, +0.0015] |
| AGENT (purchase model): its content pick | 100.0% | 0.3126 | 0.3117 | 0.3120 | +0.0648 [-0.0776, +0.2071] | -0.0676 [-0.1297, -0.0055] |

- agent click: {'act': 124807, 'nothing': 3193}, arms {'1': 124543, '0': 3193, '2': 264}, mean expected net of its sends 3.833, 1.152 ms/decision
- agent purchase: {'act': 93992, 'nothing': 34008}, arms {'2': 19422, '1': 74570, '0': 34008}, mean expected net of its sends 0.352, 0.917 ms/decision
- segment rule columns chosen on validation: ['newbie', 'mens', 'womens']

## X5

Command: `python3 -I scripts/v2_realdata/a_x5.py --customers 100000 --seeds 2`

### target

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.6002 | 0.6011 | 0.6011 | +0.0000 [+0.0000, +0.0000] | -0.0351 [-0.0436, -0.0266] |
| send to all | 100.0% | 0.6373 | 0.6363 | 0.6363 | +0.0351 [+0.0266, +0.0436] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.6187 | 0.6187 | 0.6187 | +0.0176 [+0.0133, +0.0218] | -0.0176 [-0.0218, -0.0133] |
| best segment rule (recency_days) | 100.0% | 0.6373 | 0.6363 | 0.6363 | +0.0351 [+0.0266, +0.0436] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 75.6% | 0.6241 | 0.6287 | 0.6285 | +0.0274 [+0.0199, +0.0349] | -0.0077 [-0.0118, -0.0037] |
| AGENT (click model) | 0.0% | 0.6002 | 0.6011 | 0.6011 | +0.0000 [+0.0000, +0.0000] | -0.0351 [-0.0436, -0.0266] |
| AGENT (purchase model) | 70.3% | 0.6247 | 0.6272 | 0.6272 | +0.0260 [+0.0188, +0.0333] | -0.0091 [-0.0131, -0.0051] |

### profit

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 70.343 | 70.456 | 70.459 | +0.000 [+0.000, +0.000] | -2.149 [-3.703, -0.595] |
| send to all | 100.0% | 72.723 | 72.607 | 72.608 | +2.149 [+0.595, +3.703] | +0.000 [+0.000, +0.000] |
| random 50/50 | 50.0% | 71.533 | 71.533 | 71.534 | +1.075 [+0.298, +1.852] | -1.075 [-1.852, -0.298] |
| best segment rule (recency_days+n_receipts) | 77.9% | 72.696 | 72.615 | 72.616 | +2.157 [+0.767, +3.547] | +0.008 [-0.687, +0.703] |
| uplift model (T-learner, ridge) | 56.6% | 71.387 | 71.522 | 71.522 | +1.063 [-0.318, +2.444] | -1.086 [-1.799, -0.373] |
| AGENT (click model) | 0.0% | 70.343 | 70.456 | 70.459 | +0.000 [+0.000, +0.000] | -2.149 [-3.703, -0.595] |
| AGENT (purchase model) | 70.3% | 72.457 | 72.747 | 72.741 | +2.282 [+0.807, +3.757] | +0.133 [-0.258, +0.523] |

- agent click: {'nothing': 100000}, mean expected net of its sends None, 0.455 ms/decision
- agent purchase: {'act': 70295, 'nothing': 29705}, mean expected net of its sends 2.7008324128531616, 0.666 ms/decision

## megafon

Command: `python3 -I scripts/v2_realdata/a_features_rct.py megafon`; rows 600000, treated share 0.5006, break-even cost 1.0233

### conversion, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1793 | 0.1794 | 0.1794 | +0.0000 [+0.0000, +0.0000] | -0.0479 [-0.0507, -0.0450] |
| send to all | 100.0% | 0.2273 | 0.2272 | 0.2272 | +0.0479 [+0.0450, +0.0507] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.2033 | 0.2033 | 0.2033 | +0.0239 [+0.0225, +0.0254] | -0.0239 [-0.0254, -0.0225] |
| best segment rule (X_1) | 100.0% | 0.2273 | 0.2272 | 0.2272 | +0.0479 [+0.0450, +0.0507] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 77.8% | 0.2409 | 0.2409 | 0.2409 | +0.0615 [+0.0589, +0.0641] | +0.0136 [+0.0123, +0.0149] |

### conversion, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1816 | 0.1817 | 0.1817 | +0.0000 [+0.0000, +0.0000] | -0.0467 [-0.0538, -0.0396] |
| send to all | 100.0% | 0.2285 | 0.2284 | 0.2284 | +0.0467 [+0.0396, +0.0538] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.2051 | 0.2051 | 0.2051 | +0.0233 [+0.0198, +0.0269] | -0.0233 [-0.0269, -0.0198] |
| best segment rule (X_1) | 100.0% | 0.2285 | 0.2284 | 0.2284 | +0.0467 [+0.0396, +0.0538] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 77.7% | 0.2442 | 0.2429 | 0.2431 | +0.0614 [+0.0550, +0.0677] | +0.0147 [+0.0116, +0.0178] |
| AGENT (click model) | 0.0% | 0.1816 | 0.1817 | 0.1817 | +0.0000 [+0.0000, +0.0000] | -0.0467 [-0.0538, -0.0396] |
| AGENT (purchase model) | 44.5% | 0.2035 | 0.2035 | 0.2035 | +0.0218 [+0.0177, +0.0258] | -0.0249 [-0.0296, -0.0203] |

### profit, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 3.5860 | 3.5875 | 3.5874 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send to all | 100.0% | 3.5231 | 3.5217 | 3.5216 | -0.0658 [-0.1233, -0.0083] | -0.0658 [-0.1233, -0.0083] |
| random 50/50 | 50.0% | 3.5546 | 3.5546 | 3.5545 | -0.0329 [-0.0616, -0.0041] | -0.0329 [-0.0616, -0.0041] |
| best segment rule (X_4) | 50.1% | 3.7244 | 3.7192 | 3.7194 | +0.1320 [+0.0907, +0.1733] | +0.1320 [+0.0907, +0.1733] |
| uplift model (T-learner, ridge) | 53.0% | 4.0665 | 4.0740 | 4.0731 | +0.4856 [+0.4431, +0.5281] | +0.4856 [+0.4431, +0.5281] |

### profit, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 3.6320 | 3.6340 | 3.6341 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send to all | 100.0% | 3.5466 | 3.5446 | 3.5448 | -0.0893 [-0.2306, +0.0520] | -0.0893 [-0.2306, +0.0520] |
| random 50/50 | 50.0% | 3.5893 | 3.5893 | 3.5895 | -0.0446 [-0.1153, +0.0260] | -0.0446 [-0.1153, +0.0260] |
| best segment rule (X_4) | 50.1% | 3.6952 | 3.6741 | 3.6747 | +0.0406 [-0.0610, +0.1422] | +0.0406 [-0.0610, +0.1422] |
| uplift model (T-learner, ridge) | 53.0% | 4.1159 | 4.1334 | 4.1313 | +0.4972 [+0.3928, +0.6015] | +0.4972 [+0.3928, +0.6015] |
| AGENT (click model) | 0.0% | 3.6320 | 3.6340 | 3.6341 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| AGENT (purchase model) | 44.5% | 3.6146 | 3.6135 | 3.6139 | -0.0202 [-0.1008, +0.0603] | -0.0202 [-0.1008, +0.0603] |

- agent click: {'nothing': 100000}, 0.699 ms/decision
- agent purchase: {'nothing': 55499, 'act': 44501}, 0.993 ms/decision

## starbucks

Command: `python3 -I scripts/v2_realdata/a_features_rct.py starbucks`; rows 126184, treated share 0.5002, break-even cost 0.1845

### purchase, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0074 | 0.0074 | 0.0074 | +0.0000 [+0.0000, +0.0000] | -0.0098 [-0.0115, -0.0081] |
| send to all | 100.0% | 0.0172 | 0.0171 | 0.0171 | +0.0098 [+0.0081, +0.0115] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0123 | 0.0123 | 0.0123 | +0.0049 [+0.0040, +0.0057] | -0.0049 [-0.0057, -0.0040] |
| best segment rule (V1) | 100.0% | 0.0172 | 0.0171 | 0.0171 | +0.0098 [+0.0081, +0.0115] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 82.4% | 0.0173 | 0.0172 | 0.0172 | +0.0098 [+0.0082, +0.0114] | +0.0000 [-0.0005, +0.0006] |

### purchase, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0073 | 0.0073 | 0.0073 | +0.0000 [+0.0000, +0.0000] | -0.0092 [-0.0112, -0.0073] |
| send to all | 100.0% | 0.0166 | 0.0166 | 0.0166 | +0.0092 [+0.0073, +0.0112] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0119 | 0.0119 | 0.0119 | +0.0046 [+0.0037, +0.0056] | -0.0046 [-0.0056, -0.0037] |
| best segment rule (V1) | 100.0% | 0.0166 | 0.0166 | 0.0166 | +0.0092 [+0.0073, +0.0112] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 82.4% | 0.0167 | 0.0166 | 0.0166 | +0.0093 [+0.0075, +0.0111] | +0.0000 [-0.0006, +0.0007] |
| AGENT (click model) | 0.0% | 0.0073 | 0.0073 | 0.0073 | +0.0000 [+0.0000, +0.0000] | -0.0092 [-0.0112, -0.0073] |
| AGENT (purchase model) | 49.6% | 0.0118 | 0.0118 | 0.0118 | +0.0045 [+0.0034, +0.0057] | -0.0047 [-0.0059, -0.0036] |

### profit, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1471 | 0.1474 | 0.1474 | +0.0000 [+0.0000, +0.0000] | -0.0111 [-0.0454, +0.0232] |
| send to all | 100.0% | 0.1588 | 0.1585 | 0.1585 | +0.0111 [-0.0232, +0.0454] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.1529 | 0.1529 | 0.1529 | +0.0056 [-0.0116, +0.0227] | -0.0056 [-0.0227, +0.0116] |
| best segment rule (V4+V5) | 43.0% | 0.2489 | 0.2486 | 0.2487 | +0.1013 [+0.0738, +0.1288] | +0.0902 [+0.0697, +0.1107] |
| uplift model (T-learner, ridge) | 52.5% | 0.2423 | 0.2413 | 0.2416 | +0.0943 [+0.0655, +0.1231] | +0.0832 [+0.0645, +0.1018] |

### profit, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1464 | 0.1464 | 0.1463 | +0.0000 [+0.0000, +0.0000] | -0.0005 [-0.0386, +0.0375] |
| send to all | 100.0% | 0.1468 | 0.1468 | 0.1469 | +0.0005 [-0.0375, +0.0386] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.1466 | 0.1466 | 0.1466 | +0.0003 [-0.0188, +0.0193] | -0.0003 [-0.0193, +0.0188] |
| best segment rule (V4+V5) | 42.9% | 0.2415 | 0.2412 | 0.2414 | +0.0950 [+0.0645, +0.1255] | +0.0945 [+0.0718, +0.1172] |
| uplift model (T-learner, ridge) | 52.5% | 0.2380 | 0.2370 | 0.2374 | +0.0910 [+0.0592, +0.1229] | +0.0905 [+0.0697, +0.1113] |
| AGENT (click model) | 0.0% | 0.1464 | 0.1464 | 0.1463 | +0.0000 [+0.0000, +0.0000] | -0.0005 [-0.0386, +0.0375] |
| AGENT (purchase model) | 49.6% | 0.1452 | 0.1451 | 0.1451 | -0.0013 [-0.0241, +0.0216] | -0.0018 [-0.0252, +0.0216] |

- agent click: {'nothing': 100000}, 0.973 ms/decision
- agent purchase: {'act': 49632, 'nothing': 50368}, 1.116 ms/decision

## lenta

Command: `python3 -I scripts/v2_realdata/a_features_rct.py lenta`; rows 687029, treated share 0.7509, break-even cost 0.1357

### response_att, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1016 | 0.1015 | 0.1015 | +0.0000 [+0.0000, +0.0000] | -0.0083 [-0.0107, -0.0060] |
| send to all | 100.0% | 0.1098 | 0.1098 | 0.1098 | +0.0083 [+0.0060, +0.0107] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.1057 | 0.1057 | 0.1057 | +0.0042 [+0.0030, +0.0053] | -0.0042 [-0.0053, -0.0030] |
| best segment rule (gender) | 99.8% | 0.1099 | 0.1099 | 0.1099 | +0.0083 [+0.0060, +0.0107] | +0.0000 [-0.0001, +0.0001] |
| uplift model (T-learner, ridge) | 72.7% | 0.1085 | 0.1088 | 0.1088 | +0.0072 [+0.0052, +0.0092] | -0.0011 [-0.0023, +0.0002] |

### response_att, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.1032 | 0.1025 | 0.1025 | +0.0000 [+0.0000, +0.0000] | -0.0063 [-0.0125, -0.0001] |
| send to all | 100.0% | 0.1086 | 0.1088 | 0.1088 | +0.0063 [+0.0001, +0.0125] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.1059 | 0.1057 | 0.1057 | +0.0032 [+0.0001, +0.0063] | -0.0032 [-0.0063, -0.0001] |
| best segment rule (gender) | 99.9% | 0.1086 | 0.1088 | 0.1088 | +0.0063 [+0.0001, +0.0125] | -0.0000 [-0.0002, +0.0001] |
| uplift model (T-learner, ridge) | 72.5% | 0.1074 | 0.1084 | 0.1084 | +0.0058 [+0.0006, +0.0111] | -0.0005 [-0.0038, +0.0028] |
| AGENT (click model) | 0.0% | 0.1032 | 0.1025 | 0.1025 | +0.0000 [+0.0000, +0.0000] | -0.0063 [-0.0125, -0.0001] |
| AGENT (purchase model) | 97.2% | 0.1081 | 0.1084 | 0.1084 | +0.0059 [-0.0002, +0.0119] | -0.0005 [-0.0012, +0.0003] |

### profit, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 2.0314 | 2.0306 | 2.0306 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send to all | 100.0% | 2.0609 | 2.0611 | 2.0611 | +0.0305 [-0.0166, +0.0776] | +0.0305 [-0.0166, +0.0776] |
| random 50/50 | 50.0% | 2.0461 | 2.0458 | 2.0459 | +0.0153 [-0.0083, +0.0388] | +0.0153 [-0.0083, +0.0388] |
| best segment rule (main_format+promo_share_15d) | 24.9% | 2.0660 | 2.0541 | 2.0543 | +0.0236 [-0.0054, +0.0527] | +0.0236 [-0.0054, +0.0527] |
| uplift model (T-learner, ridge) | 36.5% | 2.0708 | 2.0677 | 2.0678 | +0.0372 [+0.0059, +0.0684] | +0.0372 [+0.0059, +0.0684] |

### profit, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 2.0635 | 2.0504 | 2.0506 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send to all | 100.0% | 2.0369 | 2.0412 | 2.0412 | -0.0094 [-0.1334, +0.1147] | -0.0094 [-0.1334, +0.1147] |
| random 50/50 | 50.0% | 2.0502 | 2.0459 | 2.0459 | -0.0047 [-0.0667, +0.0573] | -0.0047 [-0.0667, +0.0573] |
| best segment rule (main_format+promo_share_15d) | 25.0% | 2.0777 | 2.0523 | 2.0525 | +0.0019 [-0.0739, +0.0778] | +0.0019 [-0.0739, +0.0778] |
| uplift model (T-learner, ridge) | 36.4% | 2.0718 | 2.0723 | 2.0723 | +0.0218 [-0.0596, +0.1031] | +0.0218 [-0.0596, +0.1031] |
| AGENT (click model) | 0.0% | 2.0635 | 2.0504 | 2.0506 | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| AGENT (purchase model) | 97.2% | 2.0312 | 2.0360 | 2.0359 | -0.0146 [-0.1360, +0.1068] | -0.0146 [-0.1360, +0.1068] |

- agent click: {'nothing': 100000}, 0.874 ms/decision
- agent purchase: {'act': 97194, 'nothing': 2806}, 0.979 ms/decision

## criteo

Command: `python3 -I scripts/v2_realdata/a_features_rct.py criteo --seeds 2`; rows 13979592, treated share 0.8500, break-even cost 0.0231

### visit, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0382 | 0.0382 | 0.0382 | +0.0000 [+0.0000, +0.0000] | -0.0103 [-0.0107, -0.0099] |
| send to all | 100.0% | 0.0485 | 0.0485 | 0.0485 | +0.0103 [+0.0099, +0.0107] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0433 | 0.0433 | 0.0433 | +0.0051 [+0.0049, +0.0053] | -0.0051 [-0.0053, -0.0049] |
| best segment rule (f0) | 100.0% | 0.0485 | 0.0485 | 0.0485 | +0.0103 [+0.0099, +0.0107] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 40.0% | 0.0483 | 0.0479 | 0.0480 | +0.0098 [+0.0094, +0.0101] | -0.0005 [-0.0006, -0.0004] |

### visit, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0400 | 0.0401 | 0.0401 | +0.0000 [+0.0000, +0.0000] | -0.0078 [-0.0127, -0.0029] |
| send to all | 100.0% | 0.0480 | 0.0479 | 0.0479 | +0.0078 [+0.0029, +0.0127] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0440 | 0.0440 | 0.0440 | +0.0039 [+0.0015, +0.0063] | -0.0039 [-0.0063, -0.0015] |
| best segment rule (f0) | 100.0% | 0.0480 | 0.0479 | 0.0479 | +0.0078 [+0.0029, +0.0127] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 40.2% | 0.0483 | 0.0478 | 0.0478 | +0.0077 [+0.0030, +0.0124] | -0.0001 [-0.0015, +0.0014] |
| AGENT (click model) | 97.6% | 0.0470 | 0.0469 | 0.0469 | +0.0068 [+0.0019, +0.0116] | -0.0010 [-0.0014, -0.0007] |
| AGENT (purchase model) | 0.0% | 0.0400 | 0.0401 | 0.0401 | +0.0000 [+0.0000, +0.0000] | -0.0078 [-0.0127, -0.0029] |

### conversion, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0019 | 0.0019 | 0.0019 | +0.0000 [+0.0000, +0.0000] | -0.0011 [-0.0012, -0.0011] |
| send to all | 100.0% | 0.0031 | 0.0031 | 0.0031 | +0.0011 [+0.0011, +0.0012] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0025 | 0.0025 | 0.0025 | +0.0006 [+0.0005, +0.0006] | -0.0006 [-0.0006, -0.0005] |
| best segment rule (f0) | 100.0% | 0.0031 | 0.0031 | 0.0031 | +0.0011 [+0.0011, +0.0012] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 35.0% | 0.0031 | 0.0030 | 0.0030 | +0.0011 [+0.0010, +0.0012] | -0.0001 [-0.0001, -0.0000] |

### conversion, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0016 | 0.0016 | 0.0016 | +0.0000 [+0.0000, +0.0000] | -0.0012 [-0.0022, -0.0001] |
| send to all | 100.0% | 0.0028 | 0.0028 | 0.0028 | +0.0012 [+0.0001, +0.0022] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0022 | 0.0022 | 0.0022 | +0.0006 [+0.0001, +0.0011] | -0.0006 [-0.0011, -0.0001] |
| best segment rule (f0) | 100.0% | 0.0028 | 0.0028 | 0.0028 | +0.0012 [+0.0001, +0.0022] | +0.0000 [+0.0000, +0.0000] |
| uplift model (T-learner, ridge) | 35.2% | 0.0028 | 0.0028 | 0.0028 | +0.0012 [+0.0002, +0.0022] | +0.0000 [-0.0003, +0.0003] |
| AGENT (click model) | 97.6% | 0.0027 | 0.0027 | 0.0027 | +0.0011 [+0.0000, +0.0021] | -0.0001 [-0.0002, -0.0000] |
| AGENT (purchase model) | 0.0% | 0.0016 | 0.0016 | 0.0016 | +0.0000 [+0.0000, +0.0000] | -0.0012 [-0.0022, -0.0001] |

### profit, full test split

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0389 | 0.0389 | 0.0389 | +0.0000 [+0.0000, +0.0000] | +0.0002 [-0.0017, +0.0021] |
| send to all | 100.0% | 0.0387 | 0.0387 | 0.0387 | -0.0002 [-0.0021, +0.0017] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0388 | 0.0388 | 0.0388 | -0.0001 [-0.0011, +0.0009] | +0.0001 [-0.0009, +0.0011] |
| best segment rule (f2+f8) | 15.0% | 0.0562 | 0.0558 | 0.0559 | +0.0170 [+0.0152, +0.0188] | +0.0172 [+0.0166, +0.0178] |
| uplift model (T-learner, ridge) | 22.4% | 0.0554 | 0.0547 | 0.0549 | +0.0160 [+0.0142, +0.0178] | +0.0162 [+0.0156, +0.0168] |

### profit, agent sample

| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] | DR gain vs best blanket arm (chosen on train) [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0320 | 0.0321 | 0.0321 | +0.0000 [+0.0000, +0.0000] | -0.0002 [-0.0209, +0.0204] |
| send to all | 100.0% | 0.0324 | 0.0324 | 0.0324 | +0.0002 [-0.0204, +0.0209] | +0.0000 [+0.0000, +0.0000] |
| random 50/50 | 50.0% | 0.0322 | 0.0322 | 0.0322 | +0.0001 [-0.0102, +0.0105] | -0.0001 [-0.0105, +0.0102] |
| best segment rule (f2+f8) | 15.0% | 0.0545 | 0.0540 | 0.0542 | +0.0221 [+0.0030, +0.0413] | +0.0219 [+0.0141, +0.0296] |
| uplift model (T-learner, ridge) | 22.4% | 0.0487 | 0.0482 | 0.0484 | +0.0163 [-0.0035, +0.0361] | +0.0161 [+0.0101, +0.0220] |
| AGENT (click model) | 97.6% | 0.0311 | 0.0310 | 0.0310 | -0.0011 [-0.0217, +0.0194] | -0.0014 [-0.0027, -0.0001] |
| AGENT (purchase model) | 0.0% | 0.0320 | 0.0321 | 0.0321 | +0.0000 [+0.0000, +0.0000] | -0.0002 [-0.0209, +0.0204] |

- agent click: {'act': 97567, 'nothing': 2433}, 0.667 ms/decision
- agent purchase: {'nothing': 100000}, 0.5 ms/decision
