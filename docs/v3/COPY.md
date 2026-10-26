# Per-type copy: AI writes, a scoring model picks, live tests decide

Plan-v3 decision 6 ([`plan-v3.md`](../../plan-v3.md) section 4). Code: `autonomy/content/`. Tests: `tests/v3/copy/`.
It is **off by default** (`CopyConfig.enabled = False`): with it off the planner builds no types, asks no
model and returns an empty plan, so every v2 number is unchanged. Nothing in the loop calls it yet; the
integrator wires `CopyPlanner.run` into the weekly job (see "Wiring" below).

## The week, step by step

| Step | What happens | Code |
|---|---|---|
| 1. Types | Customers are grouped into 50-300 types from a short profile row; each type gets a plain label | `types.py` |
| 2. Write | The Creative writes 5-10 drafts per (type, goal), spread over the offer x theme cells the brief allows | `Creative.write_for_type`, `planner.py` |
| 3. Check | Every draft goes through the Critic, unchanged: rules first, then the judge twice. Only APPROVED drafts go on | `agents/critic.py` |
| 4. Score | A scoring model predicts each draft's purchase rate for the type. It is used only if it beat theme labels on held-out drafts; otherwise theme labels rank | `scoring.py` |
| 5. Test live | The top 3 drafts per type go out in equal shares; a clear loser is cut; results go back to step 4 next week | `live.py` |

### 1. Customer types

A customer is a row of numbers: nine base columns from `CustomerState` (days since last purchase, tenure,
purchases, revenue, order value, offer sensitivity, contacts in 30 days, reachable channels, has-bought)
plus up to 40 columns of `CustomerState.features` (the nightly profile) that at least half the customers have.
Skewed columns are log-scaled, all are standardised and clipped at +-5, and the profile columns are down-weighted
so 40 of them do not drown the nine base ones. Types come from k-means (k-means++ start, seeded, numpy only).

- **How many.** About 20 customers per type, never fewer than 50 types or more than 300 when the base is big
  enough, and never so many that the average type has under 5 customers (a 100-customer shop gets 20 types, not 50).
- **Cost of fitting.** Centres are fitted on a seeded sample of at most 50,000 customers; everyone else is assigned
  to the nearest centre in blocks.
- **Repeatable.** Same states and seed give the same types, ids (`ct000`, largest first) and labels.
- **Labels.** Plain words from where the type's typical member sits among all customers, in thirds:
  "lapsed, repeat buyer, low spend, deal seeker, mostly buys tea". Two types that would read the same get
  "last order about N days ago" added. The label is all the writer is told about the type; no customer data.
- Type ids are only meaningful within a week, because the types are refitted: next week's `ct000` is usually a
  different group. Past sends are therefore kept as per-send rows and re-placed in this week's types (`tally_sends`),
  never keyed by an old type id.
- **Point in time.** Each past send is a `PastSend(item, state, sent_at, bought)` with the customer's state **as of
  the send**, never today's: today's state already holds the purchase the send caused (one more order, recency near
  zero), which would move buyers into "recent, frequent buyer" types and leak the outcome into the type. A row whose
  `state.as_of` is after `sent_at` is skipped and counted in the plan's notes.
- **Bad values.** A NaN or infinite base field (a blank or garbled amount in an upload) is read as a neutral value
  (0, or 0.5 for offer sensitivity) instead of crashing the fit; non-finite profile values take the column median.

### 2. Drafting

`Creative.write_for_type(brief, type_id=..., type_note=<label>, channel, offer, theme, n, ...)` is a small additive
hook next to `build_library`. It reuses the same prompt, labels, slots, footer, length checks, brand kit and
template fallback; it only adds the line `Customer type: <label>`. The content id and the WhatsApp template name
carry the planner's key for the type (type id, ISO week, and a hash of the type's label and centre) plus a hash of
the final subject and body. Meta approves a template by name, so a name must never be reused for different wording,
and the scoring model pools past sends by content id, so one id must never cover two texts; a type id alone would
break both, because type ids are refitted weekly. `test_ids_and_template_names_are_never_shared_between_weeks_or_between_different_types`
checks it. `build_library` is unchanged: its prompts, and its content ids and template names (pinned in
`test_the_default_library_is_unchanged_by_the_hooks` to the values printed at base commit 672602ea), are as before.

Offers the brand kit says the shop never states are left out before anything is written (and noted); a goal with
no offer and theme left is skipped with a note. `channel` must be one the goal's envelope allows (`ValueError`
otherwise).

The planner asks about `drafts_per_type / drafts_per_call` cells (8 drafts in calls of 2 means 4 calls), starting at a
different cell for each type, one call per cell. Personal slots are the ones the dispatcher already fills:
`first_name`, `top_category`, `recommended_product` (the nearest thing to "last product"), `store_name`, `offer_label`.
**Not done:** refill date and points balance. They are not in `ALLOWED_SLOTS`, so the Critic rejects them, and adding
them needs dispatcher and Critic changes that belong to other streams.

### 3. Scoring model

One row is a (draft, type) cell with its sends and purchases. Draft features (42): theme, tone, offer kind and
channel one-hots, plus 14 text features (length, words, average word length, sentences, subject length, question and
exclamation marks, slot count and which slots, share of "you", greeting). Type features: five traits (the type's
centre on recency, purchases, spend, order value, offer need). The model is a ridge-regularised logistic regression
on binomial counts, fitted by Newton steps in numpy:

`logit(rate) = b0 + draft . b1 + traits . b2 + (draft x traits) . b3 + type intercept`

**The trust gate.** Past drafts are split by a hash of the draft id (25% held out, so the test is on copy the model has
never seen). The model is fitted on the rest. The score is the AUC **within a type** (pairs of different drafts of the
same type, one with a purchase and one with a non-purchase), because a draft picker only ever compares drafts for the
same type. The comparison is **theme labels alone**: the (type, theme) purchase rate, shrunk towards the theme's rate
and then the shop's rate. The model is used only if

1. its AUC is more than 2 standard errors above 0.5, and
2. its AUC lead over theme labels is at least 0.01 and more than 2 standard errors above 0.

Standard errors come from 200 bootstrap resamples of whole held-out drafts (the draft, not the customer, is what
carries the luck). Also needed: at least 200 purchases in the training part and 50 held out. If any test fails,
`predict` returns the theme-label rate and `ScoringReport.sentence()` says why. The report always carries the
held-out numbers. When the model is used it is refitted on all the cells.

### 4. Live elimination

Per type, the top 3 drafts (`select_top`: best score first; among equal scores, a theme not yet picked, so a
theme-label ranking does not put three drafts of one theme in the test). A customer's draft is a fixed hash of
the type and customer, so the split is equal and stable, and `LiveTest.probability` gives the exact chance to log.

A draft is cut only if all of these hold:

- it and the leader each have at least 300 sends;
- its Wilson band (3 standard errors wide) lies entirely below the leader's band (3 wide);
- it is not the leader (so at least one draft always stays).

This is deliberately slow: a draft only a little worse stays live.

A customer's slot is a hash over all the test's drafts, cut or not, so a cut never moves the customers of a
surviving draft; only the cut draft's customers are re-split, by a second hash, over the drafts still live
(`test_a_cut_moves_only_the_customers_of_the_cut_draft`). Results feed back only through per-send rows: store each
send as a `PastSend` (state as of the send) and pass them as `past_sends` next week. `LiveBoard.feedback` is a
same-week summary keyed by this week's type ids, not training data.

### 5. Cost guard

`CopyConfig.write_cap` = `max_writes_per_week` if set, else `max_types x drafts_per_type` (300 x 8 = 2,400 by default;
300 x 10 = 3,000, the plan's figure, at the most). Drafts asked for count as writes, even if the model returns fewer.
A type is written only if all its drafts fit under the cap; types are taken largest first, so when the cap is hit the
smallest types are skipped (listed in `WeeklyPlan.skipped`) and keep the v2 library. Judge calls are on top of this:
the Critic judges each draft twice, so 3,000 drafts mean 6,000 judge calls a week. `WeeklyPlan.llm_calls` counts
requests answered by the model; cells written from template copy while the model was down are not counted, and a
JSON retry inside one request is not counted separately.

## Settings (`CopyConfig`)

`enabled` (False), `seed`, `min_types` 50, `max_types` 300, `members_per_type` 20, `min_members_floor` 5,
`max_profile_features` 40, `fit_sample` 50,000, `drafts_per_type` 8 (5-10), `drafts_per_call` 2, `max_writes_per_week`
(None), `ridge` 5, `holdout_share` 0.25, `min_train_purchases` 200, `min_test_purchases` 50, `min_auc_gain` 0.01,
`auc_confidence_z` 2, `bootstrap_draws` 200, `top_k` 3, `min_sends_per_arm` 300, `cut_z` 3. None of these are tuned
on data; they are starting values.

## What was run, and what it showed

All numbers below come from commands run in this worktree on synthetic data. **Nothing was run on real data**, so
none of it says how much money better copy makes.

| Claim | Command | Result |
|---|---|---|
| Targeted tests, and the existing Creative/Critic tests, pass | `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 pytest tests/v3/copy tests/v2/agents/test_creative_critic.py` | `76 passed` (after the review fixes) |
| Planted signal (copy length matters, in opposite directions for opposite types; 6 types x 40 drafts x 2,000 sends): the model is used and beats theme labels | `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m tests.v3.copy.scoring_seeds 40` (seeds 100..139, `ScoringModel(CopyConfig(seed=1)).fit`) | `signal=True: used 40 of 40; mean held-out AUC 0.721 vs 0.491 for theme labels` |
| No signal (every draft in a type has the same true rate): the model falls back | same command | `signal=False: used 0 of 40; mean held-out AUC 0.499 vs 0.500 for theme labels` |
| With enough planted history the planner uses the model and its top drafts go live | `test_with_enough_history_the_scoring_model_is_used_and_picks_the_live_drafts` | passes |
| Planted loser (rates 5%, 5%, 3%, 500 sends per arm per look, 20 looks) is cut and the equals stay | `test_a_planted_loser_is_cut_and_the_others_stay` | cut in all 12 seeds, equals never cut |
| Planted equals (3 x 5%, 30 looks) are never cut | `test_a_planted_equal_is_never_cut_over_many_looks_and_seeds` | 0 cuts in 150 seeds |
| Sizing sanity | 6,000 synthetic customers, `CopyConfig()` defaults, `CustomerTypes.fit` | 300 types, 0.3 s fit, 0.02 s to assign all (1 BLAS thread) |

The synthetic signal is built to need the interaction (length x type), so theme labels cannot find it by construction;
it shows the model can find a signal that exists and refuses to claim one that does not, not that real copy has one.

**Speed caveat.** With the default multi-threaded BLAS on the shared 4-CPU box, the two model-fit tests took about 7 s
each (the linear solves were being starved); with `OMP_NUM_THREADS=1` they take 0.08 s and 0.27 s. The fit is 14 small
solves, so this is contention and not the code; they are not marked `slow`.

**Not run:** the Gym, the real bench, the scale tests, the full v2 suite (left to CI). The plan's live pass tests
("picks beat the v2 library on purchases per send, read against the holdout") and "Critic still catches 95% of planted
violations" need live sends and the Critic's own eval; this stream does not change the Critic, and the live test
has no data until the planner is wired in. The held-out-AUC bar for the scoring model is built in as the trust gate.

## Wiring (for the integrator)

```python
planner = CopyPlanner(CopyConfig(enabled=True), creative, critic)
# rows: PastSend(item, state_as_of_the_send, sent_at, bought), one per earlier send of ours
plan = planner.run(brief, states, rules=rules, brand_voice=voice, now=now, past_sends=rows, kit=kit)
# at send time, for an agent-group customer only:
#   type_id = plan.customer_types.assign(state)
#   content_id = plan.board.tests[type_id].assign(customer_id)  -> the draft to send
# plan.board.record(type_id, content_id, sends, purchases); plan.board.review_all() -> cuts
```

Only agent-group customers (`Group.AGENT`) may be passed to `LiveTest.assign`; the holdout never enters a live test.
`states` given to `run` may include holdout and baseline customers (they shape the types and the write order, which
is harmless), but no draft is ever sent to them from here.

Open: persisting the fitted types and the board between the weekly run and the sends; WhatsApp templates need Meta's approval, so they must be submitted as a weekly batch before the live test starts (only
the email path is ready to send as soon as it is approved); the planner uses one channel per run (default the
envelope's first), because a draft count per channel would break the types x drafts cost line.
