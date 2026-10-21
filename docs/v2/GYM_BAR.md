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

## Real-calibrated scenarios (`real_email_like`, `real_sms_like`)

Every scenario above plants a truth we chose. That tests whether the agent finds what we put there, and
it is how the Gym came to encode our own beliefs (a "price-sensitive" persona, a message worth 0.4% a
day, clicks that track money, send times that matter). These two scenarios plant numbers **fitted to
real randomised campaigns** instead, so the question is whether the agent does well in a world that
behaves the way a real one did. Code: `autonomy/gym/calibration.py` (generic estimators and the
fit-to-scenario builder, no dataset names), the fitted numbers as literals in `autonomy/gym/scenarios.py`
(`REAL_EMAIL_FIT`, `REAL_SMS_FIT`), and `scripts/v2_realbench/gym_fit.py` (the only place that knows which
column is which; `--check` refits and compares with the stored literals).

**Only the TRAIN halves are used.** The fit takes the bench's own customer-id-hash split (BLAKE2b,
`SPLIT_SALT`): the Gym never encodes anything about the test half the real scoreboard is judged on, and
`fit_campaign` ignores every row not marked train (a test poisons the other rows and checks the fit is
unchanged). The sealed datasets (Lenta, Starbucks) are not used.

What is fitted (from the two campaigns' TRAIN halves):

* **Base purchase rate**, per customer group, from the control group (turned into the Gym's daily rate
  over the campaign window).
* **Message lift**, per group and arm: the extra chance of buying in the window. Group estimates are
  shrunk toward the pooled value (DerSimonian-Laird empirical Bayes); raw and shrunk are both shown.
  *Relative lift is not small in the e-mail data* (a +54% to +160% rise on a base of about 0.5%, because
  the base is tiny; in absolute terms it is about half a percentage point); it is small in the SMS data
  (+2% to +14%).
* **Arm difference**: the two e-mail arms' lifts relative to their mean become `theme_uplift` multipliers
  on two no-offer e-mail themes (`ARM_THEMES`), so the agent can learn which message works.
* **Click behaviour**: `click_floor` = the control group's click-like rate; `click_per_extra_purchase` =
  extra clicks per extra purchase the message causes (the ratio of the two treatment effects, bootstrap
  interval). Two new `ScenarioSpec` knobs, with the Gym's old constants (0.02 and 3.0) as defaults, so
  every earlier scenario is byte-for-byte what it was (tested against fingerprints taken before the
  change). The e-mail data gives a floor of 0.106 and 12.2 extra clicks per extra purchase; only 36% of a
  treated customer's clicks are caused by the e-mail, and click and purchase correlate at +0.23 among
  treated customers. So clicks are a **weak, noisy proxy** for money: this is the proxy the agent used to
  learn from. The SMS campaign logged no clicks at all, so its floor and slope are 0 and the agent has
  to learn from purchases.
* **Money**: order value from the data's own spend (e-mail: $104.90, mean positive spend) or receipts
  (SMS: mean receipt, converted at 65.5 RUB per USD, a unit conversion only: $6.49), margin 0.4 and 0.25
  and the contact costs ($0.001 per e-mail, 2 RUB per SMS) are the bench's conventions, not data.

#### real_email_like (Hillstrom e-mail, TRAIN half)

public randomised e-mail campaign (men's e-mail, women's e-mail, no e-mail; 1/3 each; 64,000 customers; outcomes over 2 weeks); fitted on the TRAIN half by customer-id hash. TRAIN customers: 32,014. Window: 14 days.

| Group | Customers | Share | P(buy in window), no message (raw / shrunk) | Daily rate | Lift, mens_email (raw +/- se / shrunk) | Lift, womens_email (raw +/- se / shrunk) | Relative lift (shrunk) |
|---|---:|---:|---:|---:|---:|---:|---:|
| new | 16,076 | 50.2% | 0.0047 / 0.0047 | 0.00033 | +0.0052 +/- 0.0016 / +0.0061 | +0.0032 +/- 0.0015 / +0.0037 | +131.5%, +78.5% |
| recent | 5,264 | 16.4% | 0.0103 / 0.0068 | 0.00048 | +0.0078 +/- 0.0040 / +0.0061 | +0.0005 +/- 0.0035 / +0.0037 | +90.6%, +54.0% |
| lapsed | 10,674 | 33.3% | 0.0036 / 0.0038 | 0.00027 | +0.0070 +/- 0.0020 / +0.0061 | +0.0053 +/- 0.0019 / +0.0037 | +160.3%, +95.6% |

Order value $104.90 (gamma shape 2), margin 40%, contact cost $0.00100, pooled extra chance of buying per message +0.0049 on a base of 0.0047.
Clicks: control click-like rate 0.1060 (the Gym's `click_floor`), treated 0.1652; extra clicks per extra purchase 12.17 (bootstrap 95% 8.78 to 19.44; the Gym's `click_per_extra_purchase`); only 36% of a treated customer's clicks are caused by the message; click-purchase correlation among treated customers +0.227.

Not fitted (kept at library values or set neutral):
* contact fatigue: a single-send randomised campaign has no repeated contacts, so it is not identifiable; the Gym's library value is kept (fatigue_per_weekly_contact 0.25)
* unsubscribe and complaint rates: the data log none; the Gym's library values are kept
* send-time effect: no send-time variation in the data; hour sensitivity is set to 0 (no effect)
* offers: no offer variation in the data; an offer multiplies the lift by 1 and still costs its redemption cost
* sleeping dogs: not identifiable from a single send; none are planted
* customer-to-customer spread inside a group: the Gym's lognormal spread is kept

#### real_sms_like (X5 RetailHero SMS, TRAIN half)

public randomised SMS campaign of a grocery loyalty programme (SMS or none, 50/50; 100,000 clients of the bench's fixed sample; outcome: bought in the window); fitted on the TRAIN half by client-id hash. TRAIN customers: 49,956. Window: 14 days.

| Group | Customers | Share | P(buy in window), no message (raw / shrunk) | Daily rate | Lift, sms (raw +/- se / shrunk) | Relative lift (shrunk) |
|---|---:|---:|---:|---:|---:|---:|
| light | 15,297 | 30.6% | 0.3407 / 0.3409 | 0.02934 | +0.0509 +/- 0.0078 / +0.0482 | +14.1% |
| medium | 17,592 | 35.2% | 0.5994 / 0.5994 | 0.06325 | +0.0422 +/- 0.0073 / +0.0408 | +6.8% |
| heavy | 17,067 | 34.2% | 0.8378 / 0.8377 | 0.12180 | +0.0137 +/- 0.0055 / +0.0149 | +1.8% |

Order value $6.49 (gamma shape 2), margin 25%, contact cost $0.03053, pooled extra chance of buying per message +0.0342 on a base of 0.6016.
Clicks: the campaign logged none; `click_floor` 0 and `click_per_extra_purchase` 0.

Not fitted (kept at library values or set neutral):
* contact fatigue: a single-send randomised campaign has no repeated contacts, so it is not identifiable; the Gym's library value is kept (fatigue_per_weekly_contact 0.25)
* unsubscribe and complaint rates: the data log none; the Gym's library values are kept
* send-time effect: no send-time variation in the data; hour sensitivity is set to 0 (no effect)
* offers: no offer variation in the data; an offer multiplies the lift by 1 and still costs its redemption cost
* sleeping dogs: not identifiable from a single send; none are planted
* customer-to-customer spread inside a group: the Gym's lognormal spread is kept
* the window length: the data do not state it; 14 days (the bench's proof window) is assumed

**What is NOT fitted, and why** (listed in `CampaignFit.not_fitted`, never silent). The founder's brief
asked for contact fatigue to be fitted too. It cannot be: a single-send randomised campaign has no
repeated contacts, so fatigue is not identifiable from either dataset, and these scenarios keep the
library's 0.25 per weekly contact and say so. Likewise unsubscribe and complaint rates (the data log
none, library values kept), send-time effects (no variation: hour sensitivity 0), offers (an offer
multiplies the lift by 1 and still costs its redemption, so a discount is pure cost here), sleeping
dogs (none planted), and the customer-to-customer spread inside a group (the Gym's lognormal kept). The
SMS window length (14 days) is an assumption. The Gym keeps a closed set of persona names, so each fitted
group is planted under one of them (`REAL_EMAIL_PERSONAS`: new -> `new`, recent -> `loyal`, lapsed ->
`at_risk`; `REAL_SMS_PERSONAS`: light -> `price_sensitive`, medium -> `researcher`, heavy -> `loyal`).
The labels carry no meaning beyond two world side effects: `new` customers signed up within the last
month (the data's "new" customers were acquired within a year) and `at_risk` customers bought more
earlier in the history, falling to the fitted rate by day 0 (which suits lapsed customers); every
behaviour (rate, lift, themes, offers) is set explicitly from the fit, never from the persona's default. The Gym's WhatsApp slot stands in for SMS (the world has
no SMS content or history), with e-mail consent set to 0. Customer groups (e-mail: new / recent / lapsed;
SMS: light / medium / heavy buyers by receipts) were fixed in advance, not searched; for the e-mail data
the shrinkage pools both arms' lifts across groups completely (only the base rates differ), because the
groups' lifts differ no more than chance explains; for SMS the lift falls with buying frequency
(+0.048 light, +0.041 medium, +0.015 heavy), which is real heterogeneity.

### The current agent on them (1,000 customers x 2 seeds, 90 days)

Commands: `python -m autonomy.gym.run --scenario real_email_like --policy agent --seeds 2 --customers 1000
--report-json out/gym/real_email_like.json` (and `real_sms_like`), on the code of this step (branch
`v2/scientist`, base `cecbf4d8`, with the changes described here). 158 s and 202 s. True value in dollars
(the truth's `expected_net` of every action taken, summed over 1,000 customers and 90 days), mean of
seeds 0 and 1:

| | real_email_like | real_sms_like |
|---|---:|---:|
| Oracle | $3,720 | $328 |
| Segment oracle | $2,834 | $109 |
| Best fixed rule | $2,035 | $209 |
| Send-to-all | $1,369 | -$118,758 |
| Rule baseline | $40 | $0 |
| **Agent** | **$161** (4.4% of the oracle; $260 and $63 on the two seeds) | **$190** (58%) |
| Agent contacts a customer a week | 0.12 (0.19 and 0.04) | 0.94 |
| Agent unsubscribed | 0.6% | 2.5% |
| Rule breaks | 0 | 0 |
| Stage reached | **1 (Safe)** | **1 (Safe)** |
| Stage 2 (beats send-to-all, the rule baseline and the best fixed rule) | FAIL: beat all three in 0 of 2 seeds (send-to-all 0, rule 2, best fixed rule 0) | FAIL: 0 of 2 seeds (send-to-all 2, rule 2, best fixed rule 0): $20 below the best fixed rule |
| Stage 3 (80% of the segment oracle) | FAIL: 6% | PASS, but vacuous: 174%, the best fixed rule gets 192% |

What this says, honestly:

* **In the world fitted to the real e-mail campaign the agent does almost nothing.** It sends 0.12
  e-mails a customer a week while e-mailing everyone earns $1,369 to $2,035 (the oracle $3,720). A
  diagnostic run (20 days, seed 0, 1,000 customers; not part of the numbers above) shows why: on 85% of its decisions the
  agent says "no active goal has anything to offer this customer" (15,266 of 18,040), and where it does
  weigh a message none beats its cost (best -$0.03 against $0.00). With a base purchase rate of about 0.5% per fortnight, 1,000 customers hold too few
  purchases for its warm start to learn the +0.5-point lift (the true value of an e-mail is about
  $0.21), so it holds back. It is safe (no rule breaks, intervals cover) and useless. This is the
  cold-start failure the plain Gym scenarios do not show: they plant a lift about 6 times larger (0.028
  extra purchases per e-mail against 0.005 here). Why it does not learn the lift from the randomised
  history it is handed was not investigated further.
* **In the SMS world it is close to the best fixed rule** (91% of its value, 58% of the oracle) but not
  above it, and it contacts 0.94 customers a week where the fixed rule contacts 1.73. The oracle
  contacts 0.95 a week: the gap to it is in who is contacted, not how many (why was not investigated).
* **Stage 3's capture line is uninformative where the segment oracle is below the best fixed rule**, as
  here for SMS ($109 against $209; why a segment-level ceiling sits below a fixed rule that contacts almost
  everyone was not investigated). A pass there says little; the Stage 2 failure is the signal.
* **Two seeds is two seeds.** The intervals of realised profit are wide (for e-mail the two seeds' 95% ranges run from about -$2,800 to
  +$2,400), and the agent's e-mail true value moves from $260 to $63 between seeds, so only the
  truth-based true values carry the comparison; with two seeds the
  stage verdicts are indicative and CI's 3,000 x 3 report is the record.
* **These runs fit nothing to the agent.** No parameter of the agent, the world or the scenarios was
  changed after seeing these results.

### The same agent at the CI size (3,000 customers x 3 seeds, 90 days)

Commands: `python -m autonomy.gym.run --scenario S --policy agent --seeds 3 --customers 3000 --report-json
out/gym/S.json` for `real_email_like` (934 s) and `real_sms_like`, on the code of this step, 21 Oct 2026 (the runs behind
the pre-registered claims `gym.real_email_like.*` and `gym.real_sms_like.*` of [TARGETS.json](TARGETS.json)).

| | real_email_like | real_sms_like |
|---|---:|---:|
| Agent true value per seed (3 seeds) | $8,978, $7,618, $7,557 | $630, $495, $633 |
| Best fixed rule per seed | $5,980, $5,952, $5,772 | $665, $723, $698 |
| Segment oracle per seed | $8,179, $8,189, $7,811 | $358, $403, $382 |
| Agent contacts a customer a week | 1.68, 1.71, 1.46 | 1.25, 1.72, 1.11 |
| Rule breaks | 0 | 0 |
| Stage reached | **3 (Smart)** | **1 (Safe)** |
| Stage 2 | PASS: beat all three references in 3 of 3 seeds | FAIL: 0 of 3 seeds beat the best fixed rule (send-to-all 3, rule 3) |
| Stage 3 (80% of the segment oracle) | PASS: 100% ($24,153 of $24,179; best fixed rule 73%) | PASS but vacuous: 154% (the best fixed rule gets 183%) |

So the 1,000-customer reading above ("almost does nothing") was a **size effect**, not a property of the world: with 3,000
customers the same agent learns the lift from the randomised history, contacts 1.6 customers a week instead of 0.12 and
beats the best fixed rule by about $1,700 to $3,000 a seed. At 1,000 customers there are about a third of the purchases,
and the warm start does not see the lift. The SMS world is unchanged by size: the agent stays just below the best fixed
rule, which contacts 1.75 customers a week to its 1.1 to 1.7. The 1,000 x 2 spot-checks are therefore too small for these two
scenarios; the CI size is the record. Why the e-mail agent crosses over between 1,000 and 3,000 customers was not
investigated.

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
