# Contact less: fixing what the full-size Gym exposed

The full-size Gym (`GYM_CI_RESULTS.md`) showed the agent at about 1.8 contacts a customer a week
almost everywhere, with more unsubscribes and lost future value than the best fixed rule. It lost
to that rule wherever the right move was to contact less. This stream has two tasks: fatigue and
restraint (first section), then discounts and sleeping dogs (second section).

## Fatigue and restraint

**Files:** `autonomy/runtime/agent.py`, `autonomy/decide/models.py` (`HazardModel`),
`autonomy/decide/policy.py` (per-channel prices, budget and restraint hooks),
`autonomy/decide/pacing.py` (`step_aside`). **Tests:** `tests/v2/contact_less/test_restraint.py`.

**Short version.** Measured on 1,000 customers x 2 seeds, with only this task's files on top of
HEAD. The agent now beats the best fixed rule on both seeds of `whatsapp_fatigue`,
`baseline_shop`, `send_time_matters` and `sleeping_dogs_15`. All four of those reach Stage 3 except
`sleeping_dogs_15`, which reaches Stage 2. It stops on day 0 in `complaint_storm`, and in
`whatsapp_fatigue` it goes from 16% of the segment oracle to 105%.

Two scenarios still fail the bar. `null_effect` loses $0.28 a customer against an allowance of
$0.25 (it lost $0.27 before). `discount_cannibalisation` loses to the fixed rule on one seed, by
$144. Contacts a customer-week fell from 1.5-1.8 to 1.0-1.7, and unsubscribes fell in every
scenario.

### What was wrong (measured before changing anything)

Diagnostics read only what the agent sees, week by week. They lived in a scratch script, not in
the repo.

1. **The attention price was about two cents.** `FatigueModel` started at 0.2% per message and was
   worth 1,000 messages. It used one curve for all channels and learned only when a week-long
   reward window closed. The stake was 13 weeks x the agent's average expected net x its sends per
   customer. In `whatsapp_fatigue` 28% of customers unsubscribed, against 16% under the fixed rule.
2. **Every message was a second message.** In `whatsapp_fatigue` (seed 0, a typical week),
   1,373 of 1,492 sends went to someone already contacted in the week before. The price was
   myopic: it never saw that today's message tires next week's. Half the sends were emails, which
   in that scenario are worth a third of a WhatsApp message, and each one tired the WhatsApp
   message that followed.
3. **Two definitions of "this week".** The agent labelled each send by the sends in
   `[t - 7 days, t)` for learning, while the cap and the price use `(t - 7 days, t]`. Daily
   decisions at a fixed hour put many sends exactly seven days apart, so 41% of `baseline_shop`'s
   sends were learned as "third message of the week".
4. **In `null_effect` the holdout can look like proof.** On seed 0 the lift per click's posterior
   sat at 0.29-0.44 +- 0.15-0.22 for all 90 days, because by chance the holdout bought less.
   Nothing that reads only the agent's data can tell that from a real effect.

### What changed

**1. The contact hazard, learned from every send** (`HazardModel`). We estimate
P(unsubscribe or complaint | channel, messages in the last 7 days) as

    p(channel, n) = prior(n) x r_business x r_count[n] x r_channel[channel] x r_cell[channel, n]

* Each `r` is a Gamma(k, k) multiplier centred on 1, with strengths counted in expected events. It
  is updated by its events against what the levels above predicted for its sends. Email and
  WhatsApp are learned apart, but email borrows the count shape from WhatsApp's sends. The curve
  never falls with the count.
* The prior is conservative: 0.5% for a first message of the week, and x1.5 for each message
  already sent that week. Published email unsubscribe rates are 0.1-0.5% a campaign, and WhatsApp
  opt-outs run higher.
* It learns from each DELIVER as it arrives, and from the first UNSUBSCRIBE or COMPLAINT tied to
  that send, hours later, not after a week-long reward window. A complaint is also counted in its
  own `HazardModel`.
* Past campaigns warm-start both models before day 1. Each send gets its inverse-propensity weight,
  rescaled to average 1. An opt-out without a decision id is tied to the latest send on its channel
  within two days.
* In `whatsapp_fatigue`, by the end of the first week it put a second message of the week at
  1.0-1.4% against 0.2-0.3% for a first. By the end of the run it was 1.3% against 0.14-0.23%.

**2. The attention price, per channel** (`ContactPrice`):

    price(channel, n) = p(channel, n) x lost future value(customer, channel)
                        + p_complaint(channel, n) x COMPLAINT_COST ($2)

* **Lost future value** is `LOST_VALUE_WEEKS` (26) x the weekly value of the customer's best rhythm
  on that channel (see 4). It is computed from the customer's own options now, so it carries the
  per-person factors (shrunk to the segment) and the measured lift per click. A customer that
  messages do not move has little to lose, and a keen WhatsApp reader a lot.
* Half a year is a judgement, chosen before measuring. A contact permission is permanent, but plans
  further out are guesses, and CRM teams value a subscriber over 6-12 months.
* We use the channel's own value, not its value net of the other channel. That is the simpler and
  more cautious reading, and the one the Gym charges.
* The policy charges every bid its own channel's price (`channel_price_fn`). A sent decision logs
  that price as `attention_price`.

**3. Restraint under uncertainty: the lower-confidence-bound rule.**

* **When it applies.** The evidence that messages create lift is *weak* when the lift per click's
  posterior includes zero at `RESTRAINT_Z` = 1.64 (one-sided 95%): `k - 1.64 sd <= 0`. That happens
  when the effect is small or when the holdout comparison is too noisy to show it.
* **What it does.** In a weak segment an option may win the Thompson draw only if its expected net,
  after its price, beats doing nothing by 1.64 of its own standard deviations (the spread Thompson
  would have sampled it with). Sampling optimistically is replaced by "it pays even at the
  pessimistic end". The exploration floor still picks uniformly among all plausible options, held
  ones included, so learning goes on. The logged probability is exact: floor / n for every option,
  plus (1 - floor) x its share of Thompson wins among the options that pass
  (`thompson_with_floor(eligible=...)`).
* **When it is judged.** Only once the comparison has had its say: two learning windows (14 days)
  after the agent starts, and at least 20 matured clicks. A segment is held back on its own
  posterior only when the business-wide posterior is weak too.

Three versions were tried and dropped, because they stopped sending, and so measuring, in shops
where messages make money:

* **Lift doubt in the margin.** Counting the lift's own uncertainty in the margin holds back every
  option the moment the rule applies. The lift's lower bound at zero puts every option's lower
  bound below its costs. Restraint was then absorbing. `send_time_matters` seed 1 fell to $4,949
  (the fixed rule makes $6,861), with segments held for the rest of the run.
* **Judging from day 7.** `baseline_shop`'s posterior was 0.10 +- 0.18 at day 7, so it was held back
  in week 2.
* **Segments alone.** A tenth of a 100-customer holdout includes zero most weeks. Judged alone,
  segments of `send_time_matters` were held for weeks 4-9 ($7,676 against $8,657 before).

**4. A weekly budget spent on the best opportunities** (`Rhythm`, `ContactPrice.rhythm`). A message
with j others in its week is worth `fatigue(j)` of a fresh one, from the click model's "messages
this week" factor, and costs `price(j)`. From the customer's options now, the agent values every
rhythm the cap allows:

| Rhythm | Messages | Value a week |
|---|---|---|
| one a week | each fresh | `v(0)` |
| a burst of two | a message, another the next day, then rest until the week is clear | `(v(0) + v(1)) x 7/8` |
| two spread out | both tired | `2 v(1)` |

Here `v(j)` is the best option's net at count j after its price.

* **How it is spent.** The customer gets the rhythm worth most (fewer messages on a tie). The policy
  holds them, with `weekly_cap` in `blocked_by` (their own weekly limit), whenever one more message
  does not fit (`budget_fn`). The auction then gives each slot to the best option. A cheap email no
  longer takes the slot, and tires the reader, that a WhatsApp message would have used better.
* **Why bursts matter.** The burst was added after the steady-only version. Spreading two messages
  out makes both tired. Two back to back then a clean week make one tired and one fresh. That
  version alone took `baseline_shop` from $9,706 to $10,828 and `send_time_matters` from $8,337 to
  $10,016 (means over the two seeds).

**5. The breaker reads the history** (`Breaker.check_history`). Before the first decision, each
brief's complaint-rate rule is judged on the past campaigns, channel by channel, with the weekly
check's own test: at least 5 complaints, and a count that would happen less than once in 10,000 at
the limit. A channel whose history already breaks the limit is paused before anything is sent on
it, until a person approves a new brief version. In `complaint_storm` the history shows a 1.1%
complaint rate on email and 1.3% on WhatsApp, against a 0.08% limit, so the agent sends nothing.

**6. Send hour.** For customers with clicks but no late click (so no known reading hour), the agent
now aims at the best allowed hour of `ReadingHourModel`'s posterior. Their quick clicks credit the
hours we sent at, shrunk to their segment's hours. A Thompson draw of that posterior was tried
first, on the steady-budget version. Summed over seeds 0-1 it was $538 better on
`send_time_matters` ($16,796 against $16,258) and $717 worse on `baseline_shop` ($18,799 against
$19,516). That is within noise, and the posterior mean moves people's hours about less.

**7. The pacer stays off by default** (`pacing=False`). It is built and tested, and
`pacing=True` turns it on with the fix below.

* **What the Gym showed.** It changed nothing in `whatsapp_fatigue` (identical runs). In
  `baseline_shop` it moved true value by +$80 on one seed and -$339 on the other: it binds now and
  then, near the complaint or unsubscribe rule, in a shop that is not in trouble.
* **The storm.** In `complaint_storm`, before the history check, it stopped the breaker from
  stopping the goal. The pacer holds a channel just before the breaker would trip, and lets it go
  again as the week rolls on. Sending trickled on for 18 batches, or to the end of the run, and
  Stage 1 failed on both seeds. Without the pacer the breaker stopped after 2 and 8 batches.
* **The fix.** `Pacer(step_aside=True)`, which the agent uses when pacing is on, releases the
  pressure once a goal is clearly over the limit, so the breaker can stop it. The default keeps the
  original behaviour, which other tests pin.

### Measured: 1,000 customers x 2 seeds, 90 days, true value

**Setup.**

* "Before" is HEAD (`4c165e0c`). "After" is HEAD plus this task's four source files. Other tasks'
  files in the worktree were in progress, so this isolates this task.
* Values are true value per seed. The best fixed rule and the segment oracle do not depend on the
  agent, so they are the same in both runs.
* Run: `python -m autonomy.gym.run --scenario S --policy agent --seeds 2 --customers 1000`.

| Scenario | Agent before | **Agent after** | Best fixed rule | Segment oracle | Contacts a week (before -> after) | Unsubscribed (before -> after) | Stage (before -> after) |
|---|---|---|---|---|---|---|---|
| whatsapp_fatigue | $1,957 / $2,123 | **$12,710 / $13,435** | $9,082 / $9,302 | $11,996 / $12,895 | 1.71 -> 0.99 | 28.4% -> 4.1% | 1 -> **3** (105% of segment oracle) |
| baseline_shop | $9,716 / $9,472 | **$11,612 / $10,302** | $10,028 / $9,893 | $12,147 / $12,215 | 1.82 -> 1.68 | 13.1% -> 8.7% | 1 -> **3** (90%) |
| send_time_matters | $8,657 / $8,635 | **$10,108 / $9,959** | $6,865 / $6,861 | $9,565 / $9,433 | 1.80 -> 1.56 | 14.1% -> 7.6% | 3 -> 3 (91% -> 106%) |
| null_effect | -$323 / -$110 | -$366 / -$70 | -$312 / -$313 | $0 | 1.53 -> 1.36 | 9.2% -> 7.2% | 1 -> 1 (-$0.27 -> -$0.28 a customer; allowed -$0.25) |
| complaint_storm | stopped after 1 batch (CI) | **paused before day 0**, true value $0 | -$621 (mean) | - | 0 | 0% | 2 -> 2 |
| sleeping_dogs_15 | $7,098 (mean, from the section below) | **$8,028 / $8,304** | $6,937 / $7,805 | $8,695 / $9,879 | -> 1.58 | -> 10.7% | -> 2 (88%; dogs contacted 95%, as send-to-all) |
| discount_cannibalisation | $7,434 (mean, from the section below) | $8,876 / $8,158 | $8,437 / $8,302 | $9,279 / $9,335 | -> 1.66 | -> 8.6% | -> 1 (beats the rule on seed 0 only; 92% of segment oracle, rule 90%) |

Lost future value (the realised charge for unsubscribes and complaints) fell as follows, per seed:

* `whatsapp_fatigue`: $7,309 / $8,006 to $1,020 / $1,213.
* `baseline_shop`: $2,082 / $2,558 to $1,241 / $1,423. The fixed rule's is $1,373 / $1,267.
* `send_time_matters`: $2,381 / $2,294 to $1,154 / $1,202.

**What each part is worth.** Same setup, after version, one part off at a time, seeds 0 / 1:

| Part switched off | whatsapp_fatigue | baseline_shop | send_time_matters | complaint_storm |
|---|---|---|---|---|
| (none: the after column) | $12,710 / $13,435 | $11,612 / $10,302 | $10,108 / $9,959 | paused before day 0 |
| burst rhythm (steady budget only) | $12,920 / $13,708 | $9,658 / $9,753 | $8,068 / $8,605 | - |
| reading-hour model | - | $11,157 / $9,990 | $9,637 / $9,761 | - |
| pacer *on* (the default is off) | $12,710 / $13,435 | $11,692 / $9,963 | - | not stopped / 18 batches (before the history check) |

The ablations were run while the code was settling. The hour-model and pacer rows are on the
version just before the history pause and the pacer default, which change neither of their
scenarios. The burst row still had the pacer on.

**Where the agent still loses.**

* **`null_effect`**: -$436 over two seeds, $40 past the allowance; it was -$433 before.
  * Seed 1 improved, from -$110 to -$70, at 1.1 contacts a week. Its lift posterior includes zero
    (0.10-0.16 +- 0.23 in the diagnostics), so restraint applies.
  * Seed 0 is the holdout fluke in cause 4. With an apparent lift of 0.4 +- 0.15, sending is what a
    careful marketer would do. Its losses are WhatsApp's 3 cents a message and discounts redeemed
    by people who buy anyway, which the second section of this file addresses.
  * CI's 3,000 customers give a holdout three times the size. Before this work CI measured -$0.20
    a customer there.
* **`discount_cannibalisation`** loses to the fixed rule on seed 1 by $144, with this task alone.
  The second section's offer discipline is worth about +$250 there on its own.

**Both tasks together.** These runs used the worktree as it stood at the end of this task, with the
discounts-and-dogs files still in progress. Seeds 0 / 1:

* `discount_cannibalisation`: $9,184 / $8,527, beating the fixed rule on both seeds (Stage 3).
* `baseline_shop`: $11,221 / $10,778 (Stage 3).
* `sleeping_dogs_15`: $7,844 / $8,682 (Stage 2; dogs contacted 95%, as send-to-all).

### Honesty

* Nothing in the agent reads the Gym's truth. The hazard, the fatigue factor, the lift and the
  history pause are learned from events a real deployment sees, with `lost_value` stripped.
* The world's code was read only to understand what the scoreboard means: how it counts "contacts
  in the last 7 days" and what it charges for an unsubscribe. No planted parameter is copied. The
  conservative hazard prior (0.5%) is above the Gym's default (0.2%), and the half-year horizon
  was set before measuring.
* No constant was tuned to a seed. The 14-day judging window, the burst rhythm and the
  business-wide gate on restraint were each added to fix a mechanism seen in the diagnostics
  (named above), and then measured on both seeds.

### Gaps

* **Bursts assume daily decisions.** A burst's second message is assumed to go out the next day
  (`BURST_GAP_DAYS`). An agent deciding several times a day would want a shorter gap.
* **Uplift fatigue is read from clicks.** The click model's "messages this week" factor stands in
  for how much a tired message moves people. Clicks include a floor from people a message does not
  move, so uplift fatigue is probably understated.
* **The lift per click is not split by channel.** WhatsApp's extra effect over email in
  `whatsapp_fatigue` is seen only through its higher click rate.
* **The learning state lives in memory**, as before: the hazard models, rhythms and restraint flags
  are rebuilt from the logs on restart.
* **mypy.** `mypy --strict` was run locally with a 3.13 target and is clean. CI's 3.11 run has the
  final say. The code uses no 3.12+ syntax.
* **Two seeds, 1,000 customers.** One seed's noise in true value is about $200-300. CI's 3,000 x 3
  run is the record.

## Discounts and sleeping dogs

**Files:** `autonomy/decide/bidders.py`, `autonomy/learning/observations.py`,
`autonomy/learning/update.py`. **Tests:** `tests/v2/contact_less/test_discounts_dogs.py`.

### Discount discipline

An offer is paid for by everyone who buys while it is open, including people who would have bought
anyway. So the bidder now prices every offer as

    offer cost = cost_per_redemption x (organic chance of buying + uplift)

whatever the response model is (`organic_chance`: the agent's own organic rate, or the treated rate
minus the uplift). It then believes an offer only as far as the evidence goes:

* **Against its plain version.** An offer option's uplift is its plain version's (the best no-offer
  option on the same channel and format) plus what the offer adds. That extra is shrunk towards
  nothing (`shrink_offer_extra`): a Normal prior centred on 0, give or take twice the plain
  message's uplift (never less than twice `OFFER_PRIOR_FLOOR`, half a point, so a well-measured
  discount can still be believed where the plain message does nothing), against the spread of the
  evidence for the difference. An offer in a rich format with no plain version in that format is
  held to the plain text one.
* **The evidence** is the response model's. For the agent that means the segment's clicks on past
  offers against past plain messages, including the logged campaigns of the warm start (40% of
  history sends carried an offer), and the customer's own offer factor from the click model.
* **The customer's own buying.** `offer_sensitivity` (the share of their purchases made with an
  offer) scales what is left. Someone who pays full price ten times gets a quarter of the credit. An
  unknown customer gets 1.
* **Default to no offer.** With pruning on, as the agent runs, an offer whose expected net on that
  evidence is below its plain version's is not bid at all. An offer is never tried just because
  its prior is wide. When sampled, an offer's spread is what it adds, not the plain message's spread
  drawn a second time. An offer the hard rules block anyway (waiting for approval, a customer who
  asked for no discounts, no budget left) is not gated: it cannot go out, and keeping it means the
  decision reports the rule that stops it. `AgentPolicy.explain_bids` lists what pruning left out
  (`GoalBidder.quote(prune=False)`), blocked by the hard rule that would stop it, else by
  `PRUNED` (`"pruned"`).

### Sleeping dogs: who contact puts off

`ContactHarm` learns how much one message makes a customer buy *less*. It is fitted nightly inside
`update_models`, from the warm-start history first and then from what each night adds.

* **The evidence** (`observations.ContactEvidence`) is kept per customer: days inside and outside the
  week after a delivered message, the purchases in each, clicks, unsubscribes and complaints on any
  channel, and direct visits (not within a day of a click). It is counted from the business's first
  purchase on file, not from signup: an import of six months of orders would otherwise read years of
  "open days" as days without buying. A record that opens with a purchase (an order import) does
  not count that purchase: it is why the customer is on file, and counting it would add an open-day
  purchase to everyone and make messages look harmful. It is weighted by recency, with a 90-day half-life, because
  annoyance starts at some point.
* **The outcome**: given a customer's N purchases, the share made in the week after a message is
  binomial, with odds = ratio x exposed / open days. How much someone buys in general cancels out.
* **The clues** are click rate, direct-visit rate and "ever stopped". Each is the customer's own,
  shrunk towards the business's. They order customers along an index from a logistic regression with
  that offset. The regression is cross-fitted: each half of the customers gets coefficients fitted
  on the other half, so no one is placed low because they happened to buy less.
* **Ten bands** along the index each read their own ratio, shrunk towards the business's. A band is
  charged only the dent its purchases make credible: 1 minus the upper 90% bound of its ratio.
  Each customer's own record then updates their band, with the band as the prior. Their charge is the
  larger of the two credible dents.
* **In the bids**, `dent x organic chance` comes off every option's uplift. With pruning on, a
  customer whose expected harm beats every option's optimistic gain gets no bids.

**Two designs that were tried and dropped, both because they invented harm in shops that have
none:**

1. **A two-class mixture.** Its classes are chosen by the purchase outcome itself, so noise alone
   made a "harmed" class. `baseline_shop` came out with 47% of customers harmed and an 18% dent.
2. **A plain log-linear regression.** It stretches "keen clickers buy more after messages" into
   "non-clickers are harmed". The bands, the cross-fitting and the credible bound are there to stop
   both.

As it stands, `baseline_shop` and `discount_cannibalisation` get no charge on either seed, and
synthetic shops with no harm are not charged.

### Measured: 1,000 customers x 2 seeds, 90 days, true value

The base is the committed agent (HEAD). The "with this task" column is HEAD plus only these three
files, so the comparison isolates this task: the other task's agent changes were still in progress
in the worktree. Each value is the mean over the two seeds.

| Scenario | Agent before | Agent with this task | Best fixed rule | Send-to-all | Dogs contacted (agent / send-to-all) |
|---|---|---|---|---|---|
| discount_cannibalisation | $7,434 (80% of segment oracle) | **$7,685** (83%) | $8,369 (90%) | -$7,194 | - |
| sleeping_dogs_15 | $7,098 | $7,048 | $7,371 | $2,325 | 95% / 95% |
| sleeping_dogs_hidden | $6,831 | $6,906 | $7,166 | $1,942 | 94.5% / 94.5% (not judged) |
| baseline_shop (guard) | $9,594 | $9,551 | - | - | - |

* **Offers in `discount_cannibalisation`** fell from about 1,100 sends to about 30 over the run,
  worth about +$250. The remaining gap to the best fixed rule (-$685) is lost future value:
  unsubscribes cost $2,083 against the rule's $1,068. That is the pacing and fatigue task's ground,
  not offers.
* **`baseline_shop`** (where offers do pay for price-sensitive buyers) and `sleeping_dogs_15` move by
  less than one seed's noise (about $200).
* **Sleeping dogs** are not avoided yet. On the history of `sleeping_dogs_15`, the most suspicious
  band holds about 35% dogs, against 15-18% overall. Its ratio, about 0.7, is charged an 11% dent.
  Dogs average a 3-7% dent against under 1% for others, but that is too little to outweigh a message
  for most of them, so the dog-contact share stays at send-to-all's.
* **Why the dogs are hard to find.** With the realistic Gym's faint clues (`sleeping_dog_signal`
  0.5), most dogs look like quiet loyal buyers who click little. Most of their history comes from
  before they were annoyed. A thousand customers do not separate them honestly. The charge is kept
  credible rather than forced, so shops without dogs lose nothing. CI's 3,000 customers give bands
  of 300, which should sharpen it.
* **The combined worktree** at the time of measuring, with the other task's restraint work part
  done, gave $7,765 on `discount_cannibalisation` (-$605 against the best fixed rule). It gave
  $5,707 and $6,221 on the two dog scenarios, at about 1.0-1.3 contacts a week. Those numbers move
  with that work.

**Stage 2 is not reached on these three scenarios at this scale.** What still separates the agent
from the best fixed rule is mostly the unsubscribe cost.
