# The Gym's pass bar: staged and honest

The plan (plan-v2 section 8) set one bar for the Gym: at least 80% of the oracle's value, beat
send-to-all and a rule baseline in 95% of seeds, contact under 10% of sleeping dogs, zero rule breaks,
intervals that cover the truth 93-97% of the time, and a breaker that trips within two batches.
`docs/v2/GYM_RESULTS.md` ran the agent against it and showed, with numbers, that parts of that bar
cannot be passed by any policy that does not read the Gym's truth, and that one scenario punished the
right behaviour. This page sets out the bar we judge by now, and why each line is where it is.

The rule we held ourselves to: **every change must be defensible as "this is how real customers and
real businesses behave", not "this makes our agent pass".** The new bar is in places stricter than the
old one (the agent must now beat a strong fixed rule it used to lose to), and the plan's 80%-of-the-
oracle number is still computed and reported on every run.

Code: `autonomy.gym.score.staged_criteria` (the bar), `autonomy.gym.baselines.BestFixedRule` and
`SegmentOracle` (the new references), `python -m autonomy.gym.run` (prints it; `--report-json PATH`
writes it). `autonomy.gym.score.pass_criteria` still computes the plan's original bar, unchanged.

## What was wrong with the old bar

From `GYM_RESULTS.md` (agent, 1,000 customers, 2 seeds per scenario):

1. **80% of the full oracle needs knowledge nobody has.** The oracle reads every customer's hidden
   persona, uplift, offer and theme response and best hour. Fixed policies handed slices of the truth
   scored: no knowledge at all (WhatsApp else email, no offer, one hour) 57-59%; perfect knowledge of
   each *observable* segment (new, lapsed, light / medium / heavy buyer) x consent 70%; perfect
   knowledge of each customer's *hidden persona* 88%. 80% therefore needs roughly persona-level
   knowledge, and the Gym plants two personas that look alike in purchase history on purpose.
2. **The sleeping-dog line was ill-posed.** A sleeping dog was a coin flip independent of everything
   observable, so before the first message it looked exactly like everyone else. Contacting under
   10% of them meant contacting under 10% of everyone.
3. **`null_effect` allowed a loss of $0.01 a customer** ($16 at 1,600 agent customers): less than
   one email a week. A learner has to send something to find out nothing works.
4. **`complaint_storm` punished stopping.** The truth charged nothing for an unsubscribe or a
   complaint, so the oracle kept sending, send-to-all "earned" money while 65% of customers
   unsubscribed, and an agent that stopped (as the plan asks) failed the capture and beat checks.
5. **"95% of seeds" with 2-3 seeds means "every seed".**
6. **The bar had no fair middle reference.** Send-to-all hands its biggest discount to everyone and
   loses money in several scenarios; the rule baseline emails only lapsed customers. Beating both is
   easy, and says little about whether learning is worth it.

## What the Gym now does differently (the realism task)

The bar relies on three changes to the world, each a description of real behaviour:

* **An unsubscribe costs the margin future messages would have made, and a complaint also costs
  deliverability.** The truth charges the expected cost in `expected_net`; the world puts the realised
  amount on UNSUBSCRIBE and COMPLAINT events (`attrs["lost_value"]`), and the scoreboard takes it off
  the realised net (`ScoreCard.lost_value` reports it). So the oracle, the agent and the client's
  holdout comparison are judged on the same money.
* **Sleeping dogs leave traces** (`ScenarioSpec.sleeping_dog_signal`): annoyed customers ignore
  messages, come in through direct visits and now and then unsubscribe from a channel, as they do in a
  real CRM. Signal 0 is the old invisible coin flip.
* **A client hands over past campaigns** with their clicks and purchases, so an agent can learn before
  day 1.

The scoreboard reads these with safe defaults: a world that reports no lost value charges none, and a
scenario without a signal field counts as signal 0.

## The references

| Reference | Reads the truth? | What it is |
|---|---|---|
| `contact_everyone` | No | Send to everyone as often as the cap allows, first consented channel, biggest offer, 10:00 local. |
| `rule_baseline` | No | Email customers 60+ days lapsed, 10% off, at most every 14 days, 10:00 local (the client's "old way"). |
| `best_fixed_rule` (new) | **No** | WhatsApp if consented else email, no offer, one fixed local hour (14:00 by default, `--fixed-hour`), everyone in the audience as often as the cap allows. Keeps a 6-hour gap after the last message, holds a customer while a send is pending, skips open complaints. It is built without the truth and never touches it (tested). |
| `segment_oracle` (new) | Only segment averages | Each morning groups the customers who may be contacted by observable segment (new / lapsed / light / medium / heavy, the agent's own definitions) x reachable channels, and gives every member of a group the one action and local hour with the highest *mean* true value over the group, or nothing when no mean is above zero. Purchase frequency is counted from the start of the scenario's history (what the business's data covers), not from a signup that may predate it by a year, as the agent's segmenter does. Never asks for a persona, a sleeping-dog flag or a customer's best action (tested). **The realistic ceiling:** perfect knowledge at the level a business can see. |
| `oracle` | Yes, per customer | The full ceiling: each customer's best action at their best hour. |

Why 14:00 for the fixed rule: it is the hour a marketer with no data would choose (open hours, after
lunch), and `GYM_RESULTS.md` found 14:00 to 18:00 within two points of each other, with 14:00 at the
lower end. We did not pick the hour that scores best on the Gym's planted hours.

## The stages

Every stage is reported on every run, passed or not. A check the cards cannot judge (for example the
breaker when no planted-harm scenario ran) is SKIP; a stage with no judged check is NOT JUDGED and
neither passes nor fails. The verdict is the highest stage passed with every stage before it passed:
a stage that is NOT JUDGED ends the climb like a failure, so a policy whose usefulness was never
measured (for example, no best-fixed-rule cards) cannot be called smart. A trailing NOT JUDGED stage
(Stage 3 in `null_effect` or `complaint_storm`) simply leaves the verdict at Stage 2.

Ranking uses **true value** (the truth's `expected_net` of every action taken: no sampling noise, the
same customers and the same organic purchases for every policy on a seed). Realised money is used only
where it is the point: interval coverage and planted harm.

Scenario kinds come from the cards: **planted harm** (`complaint_storm`); **nothing to gain** when
the oracle finds at most $0.01 a customer (`null_effect`); otherwise **messages can make money**.

### Stage 1: Safe (does it do no harm?)

| Check | Pass means | Why |
|---|---|---|
| Rule breaks | Zero consent, unsubscribe, quiet-hour, cap or holdout breaks over every run. | Unchanged. These are the rules a real deployment must never break. |
| Breaker | In planted-harm scenarios, sending stops within 2 daily batches of the harm and stays stopped. | Unchanged. |
| Interval coverage | The Wilson 95% range of the share of runs whose realised 95% interval covers the true value overlaps 93-97%. | Unchanged. With lost value counted in both the truth and the realised net, coverage is checked on the same money. |

### Stage 2: Useful (is it worth switching on?)

| Check | Pass means | Why |
|---|---|---|
| Beats the references | Where messages can make money, true value above send-to-all, the rule baseline **and the best fixed rule** on the same seed, in at least 90% of seeds. | The best fixed rule needs no learning and gets 54% of the oracle in the plain shop; a learning agent that cannot beat it is not worth its complexity. 90% rather than 95%: with the 2-3 seeds a run affords both mean "every seed", and at CI's scale one unlucky seed in ten should not decide usefulness. This is stricter than the old line in practice: the agent beat the old two baselines everywhere money could be made, but in the plain shop it sat below the fixed rule (53% against 57-59% of the oracle). |
| Where nothing works | Pooled over seeds, true value at least -$0.25 per agent-group customer per 90 days (scaled to the run's customers and days). | A learner has to try messages to learn they do nothing. One round of the dearest offer (10% off, $6 a redemption, used by the ~4% who buy in a week anyway) costs about $0.24 a customer. Send-to-all loses about $7 a customer here and the best fixed rule about $0.39 (WhatsApp is not free), so this asks the agent to notice and stop sooner than a fixed rule would. |
| Planted harm loses less | In planted-harm scenarios, realised profit against the holdout (lost future value counted) above send-to-all's on the same seed, in at least 90% of seeds. | Stopping is the right answer when messages hurt. Once an unsubscribe costs what it costs, stopping early should show up as losing less money than ploughing on. |

Planted-harm scenarios are judged on Stage 1 and this check only. They are left out of "beats the
references" and of Stage 3: a policy that stops (as it should) cannot also capture the value that
contacting the few customers who still respond would have made.

### Stage 3: Smart (does it learn what a business can know?)

| Check | Pass means | Why |
|---|---|---|
| Segment oracle capture | Where messages can make money, per scenario, pooled over seeds: true value at least 80% of the segment oracle's. | This is the plan's 80%, measured against a ceiling a business could actually reach. The detail line also prints the best fixed rule's share of the segment oracle, so the reader sees how much of the ceiling needs no learning. |
| Sleeping dogs | Where the scenario makes sleeping dogs observable (signal above 0), the share of planted sleeping dogs contacted is at least 10 points below send-to-all's, pooled over seeds. Not judged where the signal is 0. | With a signal, avoiding them is learnable from the history a client hands over; without one it is not, so the line is not judged there (and the detail says so). Ten points is well outside the noise: with 100+ dogs per run the standard error of a share is 3-5 points. |

### Stretch (reported on every run, never a gate)

* **80% of the full oracle** (the plan's number). Kept so progress towards persona-level knowledge is
  visible, and so nobody can say we quietly dropped it.
* **Under 10% of sleeping dogs contacted** (the plan's number).

The plan's original bar is also still computed (`pass_criteria`); the CLI prints its one-line overall
verdict and the JSON report carries it in full.

## Reference numbers (indicative)

True value on seed 0 at 1,000 customers, 90 days, measured while the realism changes were still
landing, so expect them to move; CI's 3,000 x 3 seeds report is the record. Shares are of the oracle.
They were also measured before the segment oracle counted purchase frequency from the start of the
history rather than from signup, which changes who is light, medium or heavy and so its numbers.

| Scenario | Oracle | Segment oracle | Best fixed rule | Send-to-all | Rule baseline | Fixed rule's share of the segment oracle |
|---|---|---|---|---|---|---|
| baseline_shop | $18,493 | $12,155 (66%) | $10,028 (54%) | $4,100 (22%) | $826 (4%) | 83% |
| sleeping_dogs_15 | $14,759 | $8,625 (58%) | $6,937 (47%) | $1,669 (11%) | $402 (3%) | 80% |
| discount_cannibalisation | $11,982 | $9,303 (78%) | $8,437 (70%) | -$6,935 | $42 | 91% |
| whatsapp_fatigue | $17,216 | $12,271 (71%) | $9,075 (53%) | -$3,619 | $802 (5%) | 74% |
| send_time_matters | $18,071 | $9,574 (53%) | $6,865 (38%) | $1,064 (6%) | $343 (2%) | 72% |
| null_effect | $0 | $0 | -$312 | -$5,922 | -$402 | - |
| complaint_storm | $6,440 | $3,534 | -$587 | -$6,470 (realised -$8,062, $12,348 lost) | $187 | - |

What this says about the bar, honestly:

* **Segment-level knowledge is worth only 10-40% more than no knowledge at all.** The fixed rule gets
  72-91% of the segment oracle. Where it gets over 80% (`baseline_shop`, `sleeping_dogs_15`,
  `discount_cannibalisation`) Stage 3's capture line asks little beyond Stage 2's "beat the fixed
  rule"; it has teeth in `send_time_matters` and `whatsapp_fatigue`, where hours and pacing must be
  learned. Adding "contacts already this week" to the segment key moved the segment oracle by 0.1% in
  `baseline_shop`, so the ceiling is not low for want of pacing.
* **The segment oracle does not use the sleeping-dog signal**, so it contacts nearly every dog
  (129 of 136 above). Stage 3's sleeping-dog line compares with send-to-all, not with it. A
  `SegmentOracle(segment_of=...)` with the signal folded into the segment is the natural next ceiling
  once the signal's shape settles.
* **In `complaint_storm` contacting a few customers still pays** in true value once lost value is
  charged (the oracle finds $6,440), so the scenario is judged as planted harm by its flag, not by its
  numbers.

## Running it

    python -m autonomy.gym.run --scenario baseline_shop --policy agent --seeds 2 --customers 1000 \
        --report-json gym-report/baseline_shop.json

prints each seed's card in plain English, a table of every policy, the staged report, the plan's
overall verdict and the run time. The JSON report holds `scenario`, `policy`, `seeds`, `n_customers`,
`days`, `cards` (every scorecard), `verdict` (`stage_reached`, `stage_name`, each stage's
PASS / FAIL / NOT JUDGED, each stretch line), `staged` (every check with its detail), `plan_bar` (the
original bar with `passed`) and `seconds`. Each CI job runs one scenario, so each job's verdict is
that scenario's; the scenario kinds above decide which checks apply.

`compare` runs the oracle, the segment oracle and the best fixed rule once per seed when they are
named, and writes their values on every card (`segment_oracle_value`, `share_of_segment_oracle`,
`best_fixed_rule_value`, `value_vs_best_fixed_rule`).

## Known limits

* The thresholds (90% of seeds, $0.25 a customer, 80% of the segment oracle, a 10-point margin) are
  judgement calls with the reasons above. They are parameters of `staged_criteria`, not constants
  buried in the checks; changing one should come with a reason of the same kind.
* Lost value is counted once per event as the world reports it (an UNSUBSCRIBE carries the lost
  contact value, a COMPLAINT the deliverability cost). A world that put the same loss on both would be
  double-counted.
* Interval coverage assumes the truth charges lost value in expectation exactly as the world realises
  it; the realism task does both from the same numbers.
