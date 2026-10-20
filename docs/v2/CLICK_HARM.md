# Click harm: purchases weigh in on a gain read from clicks

**Files:** `autonomy/decide/bidders.py` (`ContactHarm.purchase_shift`, `ContactHarm.note_click`,
`ClickGap`, `GoalBidder._purchase_weighted_loss`, the chunked refit, `leave_alone`),
`autonomy/runtime/agent.py` (`ClickResponseModel.uplift_from_clicks`). **Tests:**
`tests/v2/click_harm/test_blend.py`, `tests/v2/click_harm/test_chunked_refit.py`.

## Short version

The agent's response model reads a message's gain from clicks. A sleeping dog still clicks, so the
model credited them with a message's full gain while their buying dropped. Now each customer's
click gain is weighed against their own purchases inside and outside message weeks, by how well
each is measured. A customer with little history keeps the click gain. A customer with a clear
record of buying less after messages ends with a gain near or below zero.

At 1,000 customers this changes almost nothing, and it is shipped anyway:

* **Profit.** No scenario moves by more than $116 on the two-seed mean, about one seed's noise.
  The largest single-seed drop is $358, in `baseline_shop` seed 1. Every Stage held before still
  holds.
* **Dogs.** `sleeping_dogs_15` still contacts 95.2% of its sleeping dogs, and they get the same
  number of messages (19.3 and 20.1 each, against 19.1 and 20.2). It stays at Stage 2.
* **Why.** Per person the records are too thin. A customer's recency-weighted record holds about
  one purchase (dogs 0.7-0.9, others 1.4-1.6). One purchase cannot show that one person differs
  from their click reading.
* **Where it helps.** `null_effect` seed 1 sends 0.15 messages a week instead of 0.82 and loses
  $15 instead of $41. Purchases show no lift there, so wasted sends stop sooner.

The step is principled and safe. It grows as records do: over a longer run, at CI's 3,000
customers, and with a client's longer history.

Three fixes to `ContactHarm` came with it:

* **Memory.** The refit reads its customers x 241 grid in blocks. At 50,000 customers its peak
  memory falls from 694 MB to 77 MB, with the same answers to 1e-12.
* **One number.** `leave_alone` now decides on the exact grid probability that `likely()` and
  `describe()` count, so the count of customers who "look clearly put off" is the count left alone.
* **Python 3.11.** A `MappingProxyType` dataclass default crashed the import; it is now a
  `default_factory`.

## Method in plain words

1. **The click reading.** For the message a customer would get (their best plain option), the click
   model credits `gain` +- `gain_sd`: extra chance of a purchase in its week. With `organic`
   their chance of buying without a message, that is a ratio r_c = 1 + gain / organic, of buying in
   a message's week to buying without. Its spread on the log scale is `gain_sd / (organic x r_c)`.
2. **The purchase record.** This is the record the harm model already reads (`ContactEvidence`,
   kept in `ContactHarm._own`). Of a customer's N purchases, how many fell in a message's week?
   Each day since the agent started counts at the holdout's organic level for their segment, so a
   season everyone shares is not read as harm. Given a ratio r, the count is binomial. No parallel
   model: the same grid and likelihood as the harm posterior.
3. **The gap between the two, learned from everyone** (`ClickGap`, empirical Bayes). Each night
   the refit reads the click readings the bidders noted (`note_click`) against the records. For a
   customer, u = log(record's ratio) - log(r_c). Across customers u ~ Normal(offset, spread^2 +
   click variance). The offset is fitted by Newton steps on the marginal likelihood. The spread is
   chosen from `CLICK_GAP_SPREADS` (0 to 1) as the one the records make most likely, as the harm
   model picks its `tau`. At most 5,000 customers are read, picked by a fixed hash.
   * **The offset is the bias every record shares.** The click model's scale is set against the
     holdout (extra purchases per click, agent group against holdout). So a gap everyone shares is
     the record's own bias: messages go out when people are likelier to buy anyway (a refill is
     due, they just visited). In `whatsapp_fatigue` records read 1.2-1.5 times the click ratio by
     mid-run (offset 0.16-0.41). The offset is taken off before any record is read, so no one's
     gain moves for it.
   * **The spread** is how far people truly differ from their click reading beyond noise. Where
     clicks tell the whole story it is 0.
4. **The blend.** Bayes on the ratio grid. The prior is the click reading, a log-Normal centred
   on r_c (plus the offset) with spread sqrt(click spread^2 + gap spread^2). The likelihood is the
   record. This is an inverse-variance blend, done exactly:
   * no record: the click gain stands, exactly (shift 0);
   * a thin record moves it a little;
   * a long record of buying less after messages takes the gain to zero or below;
   * a vaguer click reading gives the record more weight.

   `purchase_shift` returns organic x (prior mean of r - posterior mean of r) x e^-offset. That is
   how much the purchases take off the click gain.
5. **Purchases only take off.** A record that reads *more* buying than the clicks predict leaves
   the click gain as it is. There are two reasons.
   * A high record is as likely the timing bias above as a keener customer. A low record shows
     up despite that bias.
   * A mean-zero spread of small raises and cuts is not neutral. It lifts the customers just
     below the attention price over it. Measured: the two-sided version sent 1.11 messages a week
     in `whatsapp_fatigue` instead of 1.03 and lost $546 on average.
6. **No double counting with the harm charge.** Two numbers read the same drop in buying:
   * the credible dent (`dent x organic`, from the person's posterior and their neighbours');
   * the purchase shift.

   Every option pays the **larger** of the two, never their sum:
   `loss = max(shift, dent x organic, 0)`. The blended gain already includes the purchase drop,
   so the dent is not taken off again. The dent still matters when the neighbours make a dent
   credible and the person's own record is thin. Every option moves by the same amount, so the
   customer's taste (which option, from clicks) is kept and only the level changes. If the
   blended gain is below zero even at its optimistic bound, the existing harm hold leaves them
   alone, as before.
7. **Where it applies.** Only for a response model whose uplift is read from clicks
   (`ClickResponseModel.uplift_from_clicks = True`; `GoalBidder(purchase_weighted=...)` can force
   it). A model whose uplift is read from purchases already nets the drop off; it pays the dent
   only, as before. Only `bids()` notes click readings. `quote()` (used to explain a decision)
   remembers nothing.

**Leave alone and the count agree.** `p_harm` is the exact grid probability that the ratio is
below 1, from the refit's posterior. `leave_alone` used the Normal summary (`p_below`), which
could put a skewed posterior on the other side of 0.9 from the count in `describe()`. Now all
three read `p_harm`. `likely(t)` counts `p_harm >= t`, so at `LEAVE_ALONE_CREDIBLE` it is exactly
the customers left alone. A hand-made posterior without a grid falls back to the Normal reading.

**The refit in blocks** (`HARM_BLOCK` = 2,048 customers, 4 MB of grid rows).
* Every pass recomputes a block's likelihood rows from the per-customer counts. The passes are
  band, segment and business sums; the neighbours' sliding window; the tau search; the posterior;
  and the click gap.
* The neighbours' window sums use running totals saved at each block boundary, so no step holds
  more than two blocks of rows. The test counts this.
* At 50,000 customers peak memory falls from 694 MB to 77 MB (tracemalloc). Time goes from 10.9 s
  to 14.0 s, which includes the click-gap fit; the machine was shared with Gym runs.
* Results match the unchunked refit to 1e-12 for blocks of 1, 7, 64 and 399.

## Measured: 1,000 customers x 2 seeds, 90 days

**Setup.**
* "Before" is a clean copy of the base commit (`4c5c26eb`, from `git archive`, outside the
  worktree).
* "After" is that copy plus this task's `bidders.py` and `agent.py`.
* Each run was `python -m autonomy.gym.run --scenario S --policy agent --seeds 2 --customers 1000`.

Values are seed 0 / seed 1 true value. "Change" is the change in the two-seed mean. BFR is the
best fixed rule, the same before and after.

| Scenario | Agent before | **Agent after** | Change | BFR | Stage | Contacts a week | Dogs contacted (send-to-all) |
|---|---|---|---|---|---|---|---|
| baseline_shop | $11,221 / $10,778 | **$11,348 / $10,420** | -$116 | $10,028 / $9,893 | 3 -> 3 | 1.65 -> 1.67 | - |
| small_business | $11,074 / $11,390 | **$11,241 / $11,155** | -$34 | $9,940 / $9,924 | 3 -> 3 | 1.62 -> 1.63 | - |
| send_time_matters | $9,723 / $9,707 | **$9,629 / $9,748** | -$26 | $6,865 / $6,861 | 3 -> 3 | 1.57 -> 1.57 | - |
| drift_midway | $11,210 / $11,131 | **$11,159 / $11,055** | -$64 | $9,919 / $9,751 | 3 -> 3 | 1.62 -> 1.62 | - |
| sleeping_dogs_15 | $7,896 / $8,682 | **$7,864 / $8,736** | +$11 | $6,937 / $7,805 | 2 -> 2 | 1.60 -> 1.60 | 95.2% -> 95.2% (95.2%) |
| sleeping_dogs_hidden | $7,502 / $8,841 | **$7,478 / $8,765** | -$50 | $6,741 / $7,591 | 3 -> 3 | 1.61 -> 1.61 | 94.4% -> 94.4% (not judged) |
| null_effect | -$2 / -$41 | **-$2 / -$15** | +$12 | -$312 / -$313 | 2 -> 2 | 0.42 -> 0.09 | - |
| complaint_storm | $0 / $0 | **$0 / $0** | $0 | -$586 / -$656 | 2 -> 2 | 0.00 -> 0.00 | - |
| discount_cannibalisation | $9,184 / $8,527 | **$9,055 / $8,543** | -$57 | $8,437 / $8,302 | 3 -> 3 | 1.65 -> 1.64 | - |
| whatsapp_fatigue | $12,713 / $12,986 | **$12,740 / $12,952** | -$3 | $9,082 / $9,302 | 3 -> 3 | 1.03 -> 1.04 | - |

* **Stages.** Every scenario keeps its Stage, and the agent still beats the BFR on every seed.
* **Profit.** No mean moves by more than $116, under the bar of about $300 (one seed's noise at
  this scale).
* **Seed-level swings.** Single seeds move both ways by up to $358. A tiny change in one bid
  re-orders later Thompson draws, so a run diverges. Means over seeds are the honest read.
* **Small losses.** Most plain scenarios lose $26-$116. Purchases only ever take off, so the
  average gain is a little lower. Contacts barely move.
* **Dogs.** Each one gets 19.27 / 20.14 messages, against 19.09 / 20.22 before. Contacted others
  get 22.2 / 21.8 either way. This was scored with the Gym's dog labels in a scratch script,
  after the runs.
* **Unchanged code paths.** With purchase weighting switched off, the new code gives exactly the
  old run: `whatsapp_fatigue` seed 0 makes $12,713 at 1.09 contacts a week in both. The chunked
  refit and the new `leave_alone` reading change no decision here.

### Why the dogs do not move at this scale

Scratch diagnostics read the Gym's dog labels only to score the model. They looked at
`sleeping_dogs_15` seed 0 and `baseline_shop` seed 0, every night.

* **The signal is there when pooled.** Dogs' records read a ratio of 0.31-0.53, against 1.76-1.93
  for everyone else. The click model reads 1.13-1.37 for dogs and 1.25-1.42 for the rest, so
  clicks hardly tell them apart.
* **No one person shows it.**
  * A customer's recency-weighted record holds about one purchase.
  * One purchase is a single yes/no, and a set of yes/no records cannot show how far people
    spread around their click reading.
  * So the empirical-Bayes spread came out 0 on every night checked (every tenth), in
    `sleeping_dogs_15` and in `whatsapp_fatigue`.
  * The record then moves a gain only as far as the click model's own spread (about 0.15-0.3 on
    the log scale) lets it. That cuts a dog's gain by 3-20%, not enough to stop a message.
* **What finds dogs here is the pooled signal along the clue index.** The neighbours' dent
  already carries it into the charge, and the bidder keeps the larger of the two readings.
* **Records with several purchases each identify the spread**, as in the tests (15 purchases a
  customer: spread 0.3 with put-off customers, 0.05 without). There, a clear record takes a dog's
  gain below zero.

## What was tried and did not work

1. **The first attempt** (the interrupted run's code). It used the click reading as the prior and
   the record as the likelihood, with a prior spread from the harm model's band spread plus its
   tau, and no shared offset.
   * Results: `sleeping_dogs_15` $7,697 / $8,567 (-$157), `sleeping_dogs_hidden` $7,525 / $8,681
     (-$69), `drift_midway` $11,105 / $11,100 (-$68). The other scenarios were not finished:
     their runs were cut off.
   * Diagnosis: records read a higher ratio than clicks for nearly everyone, the timing bias in
     step 3. So the blend raised most customers' gains by 5-30% and cut dogs' gains only 10-15%.
   * The band spread and tau measure how purchase ratios differ between people. They do not
     measure how far a record strays from a click reading.
2. **Two-sided, with the empirical-Bayes gap.** This was this method without step 5.
   * The plain scenarios stayed within noise: `baseline_shop` +$30, `small_business` -$75,
     `send_time_matters` +$72, `drift_midway` +$76, `discount_cannibalisation` +$39.
   * But `whatsapp_fatigue` fell to $11,950 / $12,655 (-$546 mean), at 1.11 contacts a week
     instead of 1.03. `sleeping_dogs_15` fell $105 and `sleeping_dogs_hidden` $37.
   * The raises and cuts were only +-3-5% of a gain (10th to 90th percentile), but they pushed
     customers just under the attention price over it.
   * Making it one-sided restored `whatsapp_fatigue` ($12,740 / $12,952).
3. **One-sided, with the spread floored at the bands' purchase spread.** The idea was to give
   records more weight while the gap's own spread cannot be read.
   * `sleeping_dogs_15` fell to $7,767 / $8,553 (-$129, at 1.53 contacts a week).
   * `whatsapp_fatigue` fell to $12,475 / $12,474 (-$375, at 1.15 a week).
   * `baseline_shop` was -$16.
   * More weight on thin records is more noise, not more dogs found.
4. **The full harm posterior as the purchase estimate.** This uses the person's record plus their
   neighbours' prior. It was analysed on saved nights and never run in the Gym.
   * The harm model's tau always sits at its smallest candidate (0.05), for the same reason the
     spread above is 0: thin records cannot show it.
   * So the posterior looks as sure as the click reading (sd 0.15-0.24).
   * The blend would then pull every customer with no purchases (click ratios 2.4-3.7, since
     their organic chance is tiny) halfway to the business ratio. That would erase the click
     model's personal targeting in every shop.
   * The neighbours' information still enters, through the dent.

Nothing was tuned to a seed. The only choices are the candidate spreads (the harm model's own
`HARM_TAUS` plus 0), the sample size and the one-sided rule. Each was fixed by the reasoning above
and checked on all ten scenarios.

## Honesty

* The agent never reads the Gym's truth. Dog labels were read only by scratch scripts, after a
  run, to score it.
* No planted parameter is hard-coded. The offset and spread are read from the customers each
  night.
* "Before" was measured on a clean copy of the base commit, not on the shared worktree.

## Gaps

* **The dogs are not found at 1,000 customers**, and probably not at CI's 3,000 either. Per-person
  records are as thin there, though the pooled readings are sharper. Stage 3 on
  `sleeping_dogs_15` still fails; its check is settled in week one (`DOGS_STORM.md`).
* **The click gap is read from yesterday's readings.** The first decision day has none, so it
  keeps the click gains. Its effect starts on day 1.
* **A record that reads more buying is never used.** Once the record's timing bias is modelled
  (for example by comparing message weeks with the holdout's weeks for the same people), a
  customer who truly buys more could be credited too.
* **Only the best plain option's gain is weighed.** Every option moves by the same amount, so a
  dog's discount and plain message are cut alike. Per-option purchase records would need far more
  data than a customer has.
* **mypy.** `mypy --strict` passes locally with a 3.13 target. CI's 3.11 run has the final say;
  the code uses no 3.12+ syntax, and `import autonomy.decide` works on Python 3.11.
* **Tests elsewhere.** Only `tests/v2/click_harm/` was run locally, as asked. The harm tests in
  `tests/v2/dogs_storm/` and `tests/v2/contact_less/` read `p_harm`, `likely` and `quote`, which
  keep their meaning. CI runs them.
