# Sleeping dogs and the complaint storm

## Complaint storm, unsubscribes and reasons

**Files:** `autonomy/decide/explain.py` (the reasons), `autonomy/decide/policy.py` (hands the
reason the auction's per-channel prices and the customer's channel records). **Tests:**
`tests/v2/dogs_storm/test_storm_reasons.py`. `breakers.py`, `models.py` and `pacing.py` are
unchanged: the measurements below found nothing in them to fix.

**Setup.** 1,000 customers x seeds 0 and 1, 90 days, true value. Runs used a clean copy of HEAD
(`e61725ec`) plus only this task's files, so other tasks' work in progress is not mixed in. To find
which guard holds each decision, a scratch script (not in the repo) wrapped `decide` and counted
`blocked_by` codes and whether the top bid beat its own price. It also switched the history check
and the weekly breaker off by patching them, in the scratch copy only.

### 1. complaint_storm: the history pause is what holds it, and it is right

**What holds every decision.** As shipped, on seed 0, all 73,620 of the agent group's decisions
are held by `Breaker.check_history`. It paused both channels before day 0:

* email: 64 complaints on 5,664 past messages (1.13%);
* WhatsApp: 15 complaints on 1,179 past messages (1.27%);
* the brief's limit is 0.08%.

With no channel left in the brief, the bidders offer nothing. The other suspects are not the
cause:

* **The hazard price.** With the pause off, on day 0 the agent sent to 173 of its 818 customers.
  279 had no option worth its price, and 232 were at the weekly cap from past campaigns.
* **Restraint.** It is not judged before day 14.
* **The weekly budget** never held a customer on day 0.

**What happens without it.** Same seeds. Stage 1 asks that sending stops within 2 daily batches of
day 0 and stays stopped.

| Agent variant | True value (seed 0 / 1) | Contacts a week | Unsubscribed | Batches before it stopped |
|---|---|---|---|---|
| As shipped: history pause | **$0 / $0** | 0.00 | 0% | **0 / 0** (Stage 2) |
| No history pause, weekly breaker on | $177 / $520 | 0.04 / 0.14 | 1.5% / 2.4% | 2 / **16** (seed 1 fails Stage 1) |
| Neither | $748 / $2,804 | 0.20 / 0.65 | 7.0% / 15.2% | never (fails Stage 1) |
| Best fixed rule | -$586 / -$656 | | | |
| Segment oracle | $3,460 / $3,657 | | | |

**Why no change.** The agent's own complaint model puts a first message of the week at 1.1-1.3% on
email and 0.8-1.3% on WhatsApp. It learns that from the history plus its own sends (the run with
nothing stopping it). That is 10-17 times the brief's 0.08% limit on both channels, and a second
message of the week costs more. So no customer the agent can pick out gets a message under the
limit: every message it could send breaks the client's own stop rule many times over. Mailbox
providers start filtering a bulk sender at 0.1-0.3% complaints.

The money the segment oracle finds is real in the Gym's accounting, which charges $5 a complaint.
But it is made by sending at more than 12 times the limit, which is why `GYM_BAR.md` judges
planted harm on Stage 1 and "loses less" only. "Sends to the people for whom it is worth it while
keeping complaints under the threshold" has no answer here: no one is under the threshold.

**The live breaker alone is too slow.** It already reacts to the agent's own recent rate with the
exact Poisson tail against the limit. On seed 0 it tripped on day 1, at 5 complaints on 445
messages (1.12%). But a policy that sends little gets a slow breaker. On seed 1 the price cut
sending after day 1 so hard that the rolling week did not reach the trip count until day 16.
Reading the past campaigns before day 0, with the same test (at least 5 complaints, a count that
would happen less than once in 10,000 at the limit), is what makes the stop fast. The agent's $0
beats the best fixed rule on both seeds (by $621 on average) and loses nothing to the storm.

**Suggested, not made** (the change belongs in `agent.py`, outside this task's files). Judge the
history at the agent's own rhythm: use the complaint hazard for a first message of the week, not
the past campaigns' raw rate. Then a history whose complaints came only from heavy weeks would not
pause a lighter programme. It changes nothing in `complaint_storm`, where a first message already
draws 1.1%.

### 2. Unsubscribes in the plain shop: not understated

| baseline_shop | Agent | Best fixed rule |
|---|---|---|
| Unsubscribed | 8.3% / 7.8% (8.0%) | 8.0% |
| Lost future value (mean) | $1,364 | $1,320 |
| Contacts a week | 1.65 | 1.73 |
| True value (mean) | **$10,999** | $9,960 |

At this scale the gap is nil (CI's 3,000 x 3 showed 8.1% against 7.8%).

* **Shrinkage is not too strong.** By the end of seed 0 the hazard model's rate and the observed
  rate agree in every cell with data:
  * email, first message of the week: 0.22% against 0.23% (10,438 sends);
  * email, second: 0.57% against 0.57% (5,789);
  * WhatsApp, first: 0.15% against 0.14% (4,979);
  * WhatsApp, second: 0.57% against 0.59% (2,725).
* **The horizon is not too short.** With `LOST_VALUE_WEEKS` doubled to 52 (a sensitivity run
  only), fewer customers unsubscribe: 8.0% -> 7.4% (8.0% / 6.8%), and lost value falls from $1,364
  to $1,224. But true value falls from $10,999 to $10,622, and the edge over the fixed rule from
  $1,039 to $662. True value already charges lost future value, so the 26-week price is the better
  trade in money. The slightly higher unsubscribe rate is the cost of the extra $1k.

No change.

### 3. Reasons a marketer can read

Thompson sampling sends the option with the best *draw*, and the exploration floor now and then
picks any option. About 30% of sends are not the best estimate. Their reason used to be a shrug;
it now says what the send is testing, in the terms that differ from the favourite.

The point comes first, before any comma, so the Live screen's one-line cut shows it. That cut is
the words after "Sent ...:" up to the first comma, about 70 characters. Every test phrase fits it,
which the tests check. No reason names the customer: the screen shows who it is beside the reason.

| Before | After |
|---|---|
| `...: the top option's estimate is uncertain, so a close alternative won this draw.` | `Sent a reminder by WhatsApp with no discount: testing whether WhatsApp beats email for them; no clicks yet from 3 WhatsApp messages; $0.31 expected after all costs, about the same as the current favourite.` |
| (same) | `Sent a reminder by email with no discount: testing whether they buy without 10% off; $0.56 expected after all costs, against $0.60 for the current favourite.` |
| (same) | `Sent a how-to message by email with no discount: testing whether a how-to message beats a reminder for them; ...` |
| `...: trying a less-favoured option so every option keeps getting tested.` | `Sent a loyalty message by email with no discount: a routine test of a loyalty message instead of a how-to message, so every option keeps getting tested.` |
| `Left alone this time: the best message (about $0.08 after costs) is uncertain, so resting is still being tested.` | `Left alone this time: testing whether a rest beats a message for them; the best message would add about $0.08 after costs.` |

How the new reasons are built:

* **What differs.** The reason names the first way the send differs from the favourite: channel,
  then offer, then message, then format.
* **Offers.** An offer test reads "testing whether 10% off wins them over". The reverse reads
  "testing whether they buy without 10% off".
* **Channel tests** add the person's own record on that channel, from their channel stats: "no
  WhatsApp messages to them yet", "2 clicks from 5 WhatsApp messages so far".
* **Another goal.** A send for a different goal reads "not enough evidence yet on which message
  works best for them".
* **Best estimate.** Sends that are the best estimate keep "expected to add $X after costs".
* **Resting was the favourite.** When no message beat its own price on its estimate, a send that
  goes out anyway (a Thompson draw, or the floor) reads "testing whether a message beats a rest for
  them" (floor: "a routine test of a message instead of a rest"). It used to say "expected to add
  $0.20 after costs, more than the $0.30 cost of another message", which was false. Found in review.
* **Held by restraint.** When restraint (`margin_fn`) kept the best option out of the draw, the
  rest is a deliberate hold, not a test: "not sure enough yet that a message pays off for them".
  The policy passes `restrained` to `explain.reason`. Found in review; the measured shares above
  predate both of these, and neither changes a decision.

**One bug fixed on the way.** The reason found the favourite by expected net before each
channel's own attention price. The auction ranks after it. So a cheap email that had beaten a
dearer WhatsApp message was called "a close alternative": 67 sends in `baseline_shop` seed 0
(0.4%). The policy now passes the auction's per-channel prices.

**Measured on seed 0 (share of the agent's sends by reason):**

| Scenario | "Expected to add" before -> after | "Estimate is uncertain" before -> after | Named tests after | Routine tests after |
|---|---|---|---|---|
| baseline_shop | 69.5% -> 69.9% | 27.8% -> 0 | ~27% | ~2.7% |
| whatsapp_fatigue | 69.4% -> 69.5% | 28.1% -> 0 | ~28% | ~2.4% |
| discount_cannibalisation | 69.0% -> 69.0% | 28.1% -> 0 | ~28% | ~2.8% |

**Decisions are identical**, as they must be, since a reason is written after the choice. Seed 0
gives the same sends and the same true value before and after: $11,221 (`baseline_shop`), $12,713
(`whatsapp_fatigue`) and $9,184 (`discount_cannibalisation`). So Stage 3 holds wherever it held
before. Nothing in this task changes what the agent sends.

## Sleeping dogs per person

**Files:** `autonomy/decide/bidders.py` (`ContactHarm`, `leave_alone`, `leave_alone_retry`),
`autonomy/learning/observations.py` (`ContactEvidence`), `autonomy/learning/update.py` (the holdout,
segments and start go to the harm model), `autonomy/runtime/agent.py` (passes them). **Tests:**
`tests/v2/dogs_storm/test_per_person.py`.

**Short version.** The contact-harm model is now a posterior per customer. It reads their own
record, history and time since the start together. It is shrunk to the customers most like them
and to the business, by as much as groups really differ. Since the start, the holdout sets the
organic level. A customer is left alone when it is 90% credible that messages lower their buying,
and is let back in about one week a year. It ranks the put-off as well as the old model did. Unlike
the old one, it never flags anyone in a shop without dogs.

At 1,000 customers it almost never reaches 90% for anyone. So the Gym numbers barely move:
`sleeping_dogs_15` still contacts 95% of its sleeping dogs, the same as send-to-all, and stays at
Stage 2. Its dogs get 2% fewer messages on seed 0 (19.1 each instead of 19.4) and the same on
seed 1. Nothing is lost in the other scenarios: `sleeping_dogs_hidden`, `baseline_shop`,
`whatsapp_fatigue` and `discount_cannibalisation` give the same true value and the same Stage 3.

**Setup.** 1,000 customers x seeds 0 and 1, 90 days, true value. "Before" is HEAD (`e61725ec`).
"After" is HEAD plus only this task's four source files, so other tasks' work in progress is not
mixed in. Diagnostics that read the Gym's dog labels lived in scratch scripts, outside the repo.
They scored the model; they never fed it.

### What was measured first

1. **The Stage 3 dog check is settled in week one.** A dog counts as contacted after one message
   in 90 days. The agent sends 1.6 messages a customer a week, so by the end of week one it has
   reached 95% of the dogs, as send-to-all does. The share can fall 10 points only if about 18 of
   seed 0's 136 dogs are left alone from day 0 for the whole quarter. That decision rests on the
   history alone.
2. **How much the history can tell.** A logistic regression on the history's clues, fitted *with*
   the dog labels (an upper bound no honest model can beat), reaches AUC 0.73 on seed 0. It puts
   a chance of 0.5 or more on 6 of 178 dogs and 1 other customer, and 0.4 or more on 27 dogs and 22
   others. The strongest honest clues are direct visits (AUC 0.66 alone), click rate (0.64) and
   past unsubscribes.
3. **Two models of the same effect.** On day 0 the click model expects a message to add 67% of a
   dog's own buying in the window (the median), and 64% of everyone else's. So a rule "leave alone
   when the harm beats the click model's gain" never fires. The harm model's ratio is read on all
   of a customer's buying after a message, so it already nets off what messages add. The rule is
   therefore "ratio credibly below 1", and the click gain is not added on top (`leave_alone`).
4. **Four ways a harm model invents harm.** All four were found while building, each by a false
   flag in `baseline_shop`, a shop with no dogs:
   * *Message-attributed purchases as a clue.* Each one is itself a purchase in the week after a
     message, so "never bought through a message" puts everyone who has not bought after one at
     the harmed end. Dropping it raised day-0 AUC from 0.64 to 0.66.
   * *Today's segment.* It depends on what the customer bought since the start, which is the
     outcome. The model now keeps the segment a customer started in.
   * *Many groups read at 90%.* Ten bands, and a window per customer, are read every night, so one
     reads as credibly harmed by chance now and then. On `baseline_shop` seed 1, 51 customers were
     left alone for a few days in weeks 1-3. True value moved from $10,778 to $10,128, because the
     run diverged afterwards: contacts rose from 1.66 to 1.73 a week. Bands and segments are now
     shrunk towards the business by as much as they really differ beyond their noise.
   * *The segment counted twice.* Heavy buyers have a low ratio (messages move them least), and a
     segment shift on top of a neighbourhood that already held them made them "harmed".

### What changed

1. **The evidence** (`ContactEvidence`) adds two things:
   * `split_at` and the `post_*` counts: days, exposed days, purchases and exposed purchases since
     the agent's first decision;
   * `sends_since_click`: messages since the last click, not weighted by recency.
2. **The learner** gets the holdout (from the holdout decisions in the log, plus `harm_holdout`),
   each customer's first segment, and the start. `AgentPolicy.nightly` passes its holdout group and
   the time of its first decision.
3. **The organic baseline.** For each holdout customer, the model compares their purchases since the
   start with their own earlier rate. That gives the organic trend per segment, shrunk to the whole
   holdout's, and that to no change. The earlier rate is shrunk to the segment's by as much as
   people really differ (empirical Bayes for Poisson buying).
4. **Each customer's own record.** History and time since the start are read together. Given their
   N purchases, the share made in the week after a message is binomial, with odds = ratio x
   exposed / open days. Each day since the start counts at the holdout's organic level then, so a
   season everyone shares is not read as harm. Without a holdout, a slow season would read as
   harm; a test shows it.
5. **The hierarchy.**
   * *Person -> neighbours.* The prior for each person is their neighbours along the clue index:
     the band's worth of customers nearest them, themselves left out.
   * *Neighbours -> business.* That prior is shrunk to the business by the bands' real spread.
   * *Person.* The person's own likelihood moves them from there, with a person-to-person spread
     `tau` chosen by the records.
   * *The index.* As before: click rate, direct visits and "ever stopped", cross-fitted.
   * *Clues tried and left out.* The quiet streak ranked dogs worse: AUC 0.83 -> 0.81 at day 89 in
     `sleeping_dogs_15`, 0.75 -> 0.72 in `sleeping_dogs_hidden`. It is the click rate again. The
     segment as a clue sorted heavy buyers in with the dogs: 0.84 -> 0.74.
6. **The charge.** Every option still pays `dent x organic chance`. The dent is the larger of two
   credible dents: the customer's own, and their neighbours'. The neighbours' dent replaces the
   old band's, so someone who looks like the put-off but has bought too little to show it is
   charged what the people most like them make credible.
7. **Leaving alone.** With pruning on (the agent), a customer gets no bids when
   P(ratio < 1) >= `LEAVE_ALONE_CREDIBLE`. That is `HARM_CREDIBLE`, 0.9, set before measuring: a
   lower-confidence-bound rule like restraint, but on harm. The old hold stays too: credible harm
   above every option's optimistic gain. Below that bar the harm is only charged, as before.
8. **The exploration floor.** A customer left alone is bid for as usual in about 2% of calendar
   weeks (`LEAVE_ALONE_RETRY`: a fixed coin per customer and week, with time passed in). So a wrong
   verdict can be undone by a click or a purchase.

### Measured: 1,000 customers x 2 seeds, 90 days

Values are seed 0 / seed 1. BFR is the best fixed rule.

| Scenario | Agent before | **Agent after** | BFR | Send-to-all | Dogs contacted (agent / send-to-all) | Messages a dog (before -> after) | Stage |
|---|---|---|---|---|---|---|---|
| sleeping_dogs_15 | $7,844 / $8,682 | **$7,896 / $8,682** | $6,937 / $7,806 | $2,325 (mean) | 95.2% / 95.2% | 19.4 -> 19.1 / 20.1 -> 20.2 | 2 -> 2 |
| sleeping_dogs_hidden | $7,498 / $8,841 | **$7,502 / $8,841** | $6,740 / $7,591 | $1,942 (mean) | 94.5% / 94.5% (not judged) | - | 3 -> 3 |
| baseline_shop | $11,221 / $10,778 | **$11,221 / $10,778** | $10,028 / $9,892 | - | - | - | 3 -> 3 |
| whatsapp_fatigue | $12,713 / $12,986 | **$12,713 / $12,986** | $9,082 / $9,302 | - | - | - | 3 -> 3 |
| discount_cannibalisation | $9,184 / $8,527 | **$9,184 / $8,527** | $8,437 / $8,302 | - | - | - | 3 -> 3 |

Normal customers are not starved. In `baseline_shop` nobody is ever left alone or charged: seeds
0-3 give the same sends and the same true value as before.

**How well it ranks, and what it flags.** The same agent runs were replayed through the old model
(HEAD) and the new one, from the inputs saved on seven nights of seed 0 (`baseline_shop`: seed 1).
AUC is how well the chance of harm ranks the planted dogs above everyone else.

| Night | sleeping_dogs_15 AUC, old -> new | sleeping_dogs_hidden AUC, old -> new | baseline_shop: customers at P(harm) >= 0.5, old -> new |
|---|---|---|---|
| 0 | 0.65 -> 0.66 | 0.50 -> 0.50 | 111 -> 0 |
| 14 | 0.75 -> 0.73 | 0.62 -> 0.62 | 27 -> 0 |
| 28 | 0.76 -> 0.75 | 0.67 -> 0.64 | 91 -> 0 |
| 45 | 0.79 -> 0.80 | 0.71 -> 0.66 | 12 -> 0 |
| 60 | 0.81 -> 0.79 | 0.74 -> 0.72 | 6 -> 0 |
| 89 | 0.83 -> 0.83 | 0.79 -> 0.79 | 7 -> 0 |

* **Ranking.** Both models rank about equally, and much better as the agent's own messages pile
  up.
* **False charges.** On night 0 of `sleeping_dogs_15` the old model charged a credible dent to 65
  customers who were not dogs (and 35 dogs). The new one charges no one that night.
* **Clear cases.** In `sleeping_dogs_15` by night 89, 71 dogs and 51 others sit at P(harm) >= 0.75,
  but only 5 dogs (and no one else) reach the 0.9 bar.

**A looser bar, measured but not shipped.** With `LEAVE_ALONE_CREDIBLE` at 0.75:

* seed 0 of `sleeping_dogs_15` gives $7,947, with 17.6 messages a dog instead of 19.1;
* seed 1 does not change;
* dogs contacted stay at 95%.

The 0.9 bar was set before measuring, to match the model's own credible level, and is kept.

### Honesty

* The agent never reads the Gym's truth. Diagnostics read the dog labels only to score the model.
* No constant was tuned to a seed. The bar (0.9), the retry rate (one week in 50), and the
  spreads, read from the data, were fixed before the runs above.
* Every design change answered a mechanism a diagnostic showed:
  * a leaking clue;
  * a segment that depends on the outcome;
  * multiple comparisons;
  * double counting.
  Each was checked on `baseline_shop` for false harm.

### Gaps

* **Stage 3 still fails on `sleeping_dogs_15` at this scale.** The dog check is settled on day 0,
  and even a model fitted with the labels finds few dogs from the history alone. CI's 3,000
  customers give bands of 300 and a holdout of 300. That should make more cases clear later in the
  run, and cut messages to dogs, but probably not the share contacted at least once.
* **The click model still credits put-off customers with a full gain.** Precision-weighting the
  click model's uplift against the purchase ratio, where the ratio is well measured, is the next
  step. It changes every bid in every scenario, so it was not done here.
* **Days the agent held a customer since the start count as open days.** The agent chose them,
  mostly by the weekly rhythm rather than by purchase intent, so the bias should be small.
* **Memory.** Each refit holds a customers x 241 grid, about 190 MB at 100,000 customers, as
  before.
* **A test elsewhere now fails about one run in 15.** This needs a one-line fix in a file outside
  this task. `tests/v2/contact_less/test_discounts_dogs.py::test_harm_is_shrunk_by_how_much_record_there_is`
  takes "the harmed customer with the most messages" with `max(harmed, ...)`, where `harmed` is a
  set.
  * Fifteen customers tie, and which one `max` returns depends on the set's order, which changes
    with Python's hash seed.
  * One of the fifteen has one click and none of nine purchases after a message. Under the new
    model they are not credibly harmed (P = 0.8), so they get no dent.
  * The fix: `max(sorted(harmed), key=...)`.
* **mypy.** `mypy --strict` passes locally with a 3.13 target. CI's 3.11 run has the final say.
  The code uses no 3.12+ syntax.
