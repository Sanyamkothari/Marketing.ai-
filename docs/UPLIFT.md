# Uplift modelling and measured impact (Phase 3b)

This page is for two readers: a **marketer** who wants to know what the uplift screens say and when
to trust them, and a **reviewer** who wants to check that every number on them is defined, measured
and honest. Each section starts with the plain version, then gives the exact rule and where it is
in the code.

Everything here describes the code on this branch: `engine/uplift/`, `api/routes/uplift.py` and
`ui/modules/uplift/`. Decisions are DEC-600 … DEC-699 in [`DECISIONS.md`](DECISIONS.md).

---

## 1. Uplift is not propensity

A **propensity** model, which is what Phase 1 builds, answers *who is likely to convert?* That is
the wrong question for spending a marketing budget. Many of the likeliest converters would have
converted anyway, and a few customers are put off by being contacted.

An **uplift** model answers *who converts **because** we contacted them?* For each customer it
estimates two probabilities, one if contacted (`p_treated`) and one if not (`p_control`), and
reports the difference:

    uplift = P(outcome | treated) − P(outcome | not treated)

No customer is ever both contacted and not contacted, so this can only be learnt from a past
campaign where the action was given to some customers and **held back from others at random**. That
randomness is the whole foundation. Without it, "treated customers converted more" may only mean
"we picked customers who were going to convert anyway" (see section 11).

Uplift is a problem type of its own (`problem_type: uplift`, metric `auuc`). It is configured by the
`uplift:` block of a use case and is not offered in Phase 1's Setup screen (DEC-608).

---

## 2. The data contract

One file, one row per customer - or one row per customer per snapshot date - recording one past
campaign (plan B §3):

| Column | Required | What it must be | Checked by |
|---|---|---|---|
| Primary key | yes | One column that identifies the customer, unique per row; or two - the customer and the snapshot date - unique together (M53) | Phase 1 validation |
| Treatment | yes | `1` = received the action, `0` = held out. Only 0/1 (also `true`/`false`, `"0"`/`"1"`); no blanks. **Randomly assigned.** | `TREATMENT_COLUMN_MISSING`, `TREATMENT_NOT_BINARY`, `TREATMENT_NOT_RANDOM` |
| Outcome | yes | Binary (converted or not), measured **after** the treatment | Phase 1 validation; the uplift engine refuses a non-binary outcome |
| Features | yes, at least one | Measured **before** the treatment date (the point-in-time rule) | `FEATURE_AFTER_TREATMENT` when dates are present |
| `treatment_date` | optional | When each customer was treated. Needed for the maturity and point-in-time checks. | `OUTCOME_WINDOW_IMMATURE`, `FEATURE_AFTER_TREATMENT` |
| `campaign_id` | optional | Which campaign a row belongs to. Never used as a feature. With a two-column key, a customer may change arm between campaigns but not within one. | `TREATMENT_VARIES_WITHIN_ENTITY` |

**Two-column keys (M53).** A file may hold each customer at several snapshot dates, keyed by the
customer and the snapshot date (`"primary_key": ["customer_id", "snapshot_date"]`, Plan A's
DEC-083). The **entity** is the key's first column, exactly as for a Phase 1 periodic dataset
(`engine.keys.entity_column`). Treatment and control stay **per customer**: a customer treated at
one snapshot and held out at another, within one campaign, is refused
(`TREATMENT_VARIES_WITHIN_ENTITY`). The arms are counted in customers, the randomness check keeps a
customer's snapshots in one cross-validation fold, the hold-out is drawn by customer (every snapshot
of a customer is in training or in the hold-out, never both; `split.json` names the `group_column`),
and neither key column nor the joined row key is ever a feature. Scoring and campaign results read
both columns: `scores.csv` writes each key column, and the outcomes file is joined on both.

The treatment column is either configured (`uplift.treatment_column`) or detected. Detection only
happens when nothing is configured: the first column whose name matches one of
`uplift.treatment_column_hints` (`treatment`, `treated`, `contacted`, `is_treated`), ignoring case. A
configured column is never swapped for a hint. If the user named the column that records the
experiment, measuring a different one would answer a different question. The uplift Setup screen
lists the upload's 0/1 columns with their treated share
(`GET /uploads/{id}/treatment-candidates`) so the user picks it explicitly.

**What is never a feature.** The key, the outcome, the treatment, the treatment date and campaign
id, the columns Phase 1 reserves (split, consent, fairness and suppression columns), anything in
`prepare.exclude_columns`, and **every date-like column**. In an uplift table a date is almost
always about the campaign (sent, opened, converted), so it would leak the treatment or the outcome.
Columns that Phase 1 validation flagged to drop (mostly empty, constant, ID-like, personal data) are
dropped for uplift too, so the Validate page and the uplift features never disagree. Text and
yes/no columns keep their 100 most frequent values, fixed at training time. At scoring time a value
the model never saw counts as missing. The model card (`model/uplift_model.json`) lists every
feature and every dropped column with its reason.

**Where the file comes from.** Today it is an uploaded CSV or Parquet file. Plan B also names a
Phase 1 scoring run's own control group joined with later outcomes, and Phase 2's `campaign_events`
role, which has a `treatment` standard column. Neither of those feeds `POST /uplift/runs` directly
yet: the request takes an `upload_id`, not a `dataset_id`.

---

## 3. The uplift checks

Phase 1's validation runs first, unchanged. Then the uplift checks - plan B §4's six, and since M53
a seventh for two-column keys - run on the same upload
(`engine/uplift/checks.py`, plan B §4). Both reports are written (`validation.json` and
`uplift_validation.json`), and a run needs both to pass. `POST /uplift/runs` runs them **before**
the run exists and answers `409` with both reports when something blocks. The run's own validate
stage runs them again.

| Code | Severity | What it means for you |
|---|---|---|
| `TREATMENT_COLUMN_MISSING` | error | No configured or hinted treatment column is in the file: there is no experiment to learn from. |
| `TREATMENT_NOT_BINARY` | error | Some treatment values are not 0/1, blanks included. Those rows are in neither arm, or in some third arm. The count is reported. |
| `TREATMENT_VARIES_WITHIN_ENTITY` | error | Two-column keys only (M53): a customer is treated at some snapshots and held out at others within one campaign (`uplift.campaign_id_column`, when configured and present). Treatment is assigned per customer. Cannot be acknowledged. |
| `TREATMENT_ARM_TOO_SMALL` | error | The treated or the control arm has fewer than `min_arm_rows` rows (default 1,000) or fewer than `min_arm_positives` conversions (default 50). Counted after immature rows are dropped. |
| `TREATMENT_NOT_RANDOM` | error, **can be acknowledged** | The features predict who was treated, so the campaign was targeted, not randomised. |
| `OUTCOME_WINDOW_IMMATURE` | warning | Some customers were treated too recently for their outcome to be final. Those rows are dropped and counted, never guessed. |
| `FEATURE_AFTER_TREATMENT` | error | A date-like column has values later than the row's treatment date, so the "before" snapshot already contains the campaign's effect. |

A check that cannot run because an earlier one failed is skipped, not failed twice. With no
treatment column there are no arms to count, and with customers in both arms
(`TREATMENT_VARIES_WITHIN_ENTITY`) there are no per-customer arms to count or to predict.

`uplift_validation.json` also records the arm sizes the checks counted (`treated_rows`,
`control_rows`, and for a two-column key `entity_column`, `treated_entities`, `control_entities`),
which Phase 1's Data page shows for an uplift run (M53).

**What each finding says.** This table is the normative list of the uplift codes (DEC-673); since
M53 `docs/DATA_CONTRACT.md` §11 repeats it for the business user, and a test keeps both in step with
`UPLIFT_VALIDATION_CODES`. Every finding carries a code, a message and a suggestion (and a `details`
object with the counts); the texts below are `engine/uplift/checks.py`'s, with `<…>` for the values
filled in.

| Code | Message | Suggestion |
|---|---|---|
| `TREATMENT_COLUMN_MISSING` (configured column absent) | The treatment column '`<column>`' is not in this file. | Choose the column that records who received the campaign in Setup, or upload the file that contains it. |
| `TREATMENT_COLUMN_MISSING` (nothing configured, no hint found) | No column in this file says which customers received the campaign. Looked for `<hints>`. | Add a column with 1 for customers who were contacted and 0 for the randomly held-out customers, or choose the column in Setup. |
| `TREATMENT_NOT_BINARY` | '`<column>`' should be 1 for treated customers and 0 for held-out customers, but `<n>` of `<rows>` rows (`<share>`) are blank or hold another value. | Record every customer as 1 (treated) or 0 (held out); true and false work too. Remove customers whose treatment is unknown from the file. |
| `TREATMENT_VARIES_WITHIN_ENTITY` | `<n>` of `<customers>` customers (`<share>`) are treated in some snapshots and held out in others[ within one campaign ('`<campaign column>`')], according to '`<column>`'. Treatment and control are assigned per customer ('`<entity column>`'), so such a customer would be compared with itself. | Give each customer the same treatment value in every snapshot of a campaign, or upload one campaign's snapshots at a time. |
| `TREATMENT_ARM_TOO_SMALL` | There are too few customers to measure what the campaign changed[ after leaving out `<n>` customers whose outcome is not final yet]: the `<treated/control>` group has `<n>` customers (at least `<min_arm_rows>` needed); only `<n>` customers in the `<arm>` had a positive '`<target>`' (at least `<min_arm_positives>` needed). | Use a longer period or a larger campaign, or hold out a bigger control group next time. |
| `TREATMENT_NOT_RANDOM` | Who was treated can be predicted from the customers' own data (AUC `<auc>`, where a random assignment scores about 0.50 and the limit is `<threshold>`). The strongest sign(s) was/were `<features>`. The campaign looks targeted, so comparing treated with untreated customers would mix what the campaign changed with how the chosen customers already differed. | Use data from a campaign with a randomly chosen hold-out group. If you go ahead anyway, every uplift result will be labelled not causal. |
| `OUTCOME_WINDOW_IMMATURE` | `<n>` customers were treated less than `<days>` days before `<date>`, so their outcome is not final yet. [`<n>` rows have no readable date in '`<column>`', so their outcome cannot be shown to be final.] They are left out of training and evaluation. | Nothing to fix now. Re-run on or after `<date>` to include every customer. (With only undated rows: Fill in '`<column>`' for every customer to include them.) |
| `OUTCOME_WINDOW_IMMATURE` (date column absent) | The treatment date column '`<column>`' is not in this file, so it cannot be checked whether every customer's outcome is final. | Add the treatment date to the file, or clear the treatment date setting if outcomes are already final. |
| `FEATURE_AFTER_TREATMENT` | '`<column>`' is later than the treatment date in `<n>` of `<rows>` rows, so this data was captured after the campaign reached customers. Other columns may already show what the campaign changed, which would make its effect look larger or smaller than it was. | Upload customer data as it was before the treatment date. If this column only records the outcome, exclude it in Data preparation. |

**How randomness is tested.** Under random assignment nothing about a customer predicts whether
they were treated. The check trains a small LightGBM classifier (100 trees, 15 leaves) to predict
the treatment from exactly the features the uplift model will use. It scores it with 3-fold
stratified cross-validation. At most 50,000 rows are used, sampled in proportion from each arm with
a seed taken from the upload id. The API and the run therefore reach the same verdict on the same
file. An out-of-fold ROC AUC around 0.5 means random. Above `uplift.randomness_auc_max` (default
0.60) the check fails, and the message names the features that carry at least 10% of the
classifier's split gain (at most three).

**Acknowledging it.** You can go ahead anyway by resending the request with
`overrides.validation.acknowledged: ["TREATMENT_NOT_RANDOM"]` (or `"TREATMENT_NOT_RANDOM:<treatment
column>"`). The Setup screen offers this as a button. The run then trains, but every artefact says
`causal: false` and carries the not-causal note (section 11). The threshold itself **cannot** be
raised for one run: `uplift.randomness_auc_max` is refused as a run override (`OVERRIDE_UNKNOWN_PATH`,
DEC-607). It can only be changed in the use-case file, where the change is reviewed.

**Maturity.** With `uplift.treatment_date_column` and `uplift.outcome_window_days` set, a row whose
treatment date plus the window is after "now" has an outcome that may still change. A row with a
blank or unreadable treatment date cannot be shown to be final. Both kinds are removed before
training and counted in `rows_immature`.

### Before the first uplift run: the treatment-history check and the sufficiency verdict (Plan J M93)

Uplift needs randomised history the client may not have. So before anyone starts an uplift run, the
readiness report (`GET /pilot/readiness/{dataset_id}?treatment_column=<column>`) looks at the column
the user names as "who past campaigns contacted" and answers two questions
(`engine/uplift/checks.py` `treatment_history`, `sufficiency_verdict`):

1. **How were customers chosen?** The same randomness test as `TREATMENT_NOT_RANDOM`
   (`treatment_predictability`: the small classifier, 3-fold, on the columns an uplift run would
   learn from), with the same limit, `uplift.randomness_auc_max`:
   - **random** - the out-of-fold score is at or below the limit;
   - **model-selected** - above it: past campaigns chose their customers by a model or a rule. The
     report carries `TREATMENT_HISTORY_NOT_RANDOM`, a **warning** (a Plan J code,
     `engine.measurement.codes`, not an uplift-run code: it blocks nothing, it changes the plan);
   - **unknown** - the column is not in the dataset, holds values other than 0/1 or blanks, a group
     has fewer than 30 customers, there is nothing to test the choice against, or (with a two-column
     key) a customer is contacted at some prediction dates and held back at others - the history an
     uplift run refuses with `TREATMENT_VARIES_WITHIN_ENTITY`, so the verdict is never "uplift now"
     there and no customer is counted in both groups.
2. **Is it enough to learn who a campaign changes now?** The **sufficiency verdict** uses the floors
   `TREATMENT_ARM_TOO_SMALL` uses, `uplift.min_arm_rows` (default 1,000) and
   `uplift.min_arm_positives` (default 50), counted in customers for a two-column key:
   - **"uplift now"** when the history is random and both groups reach both floors (`>=`, so the
     verdict switches exactly where an uplift run stops being refused);
   - otherwise **"propensity + random control (+ explore) first, uplift from the next cycle"**: rank
     customers with a propensity model, hold back a random control group in the first campaign
     (and, with M92, a small random explore slice outside the selection), and train uplift on that
     campaign's results. A count that is not known never counts as enough.

The report shows counts and the verdicts only, never a customer's value, and the verdict of the
readiness report itself does not move.

---

## 4. What a training run does

Training happens on a random **hold-out** split. By default 30% of the rows (`uplift.test_fraction`)
are kept aside, stratified on treatment and outcome together. Every number on the Model page is
measured on those rows, which the model never saw. With a two-column key the hold-out is drawn by
customer instead - whole customers, stratified on their arm and on whether they converted at any
snapshot - so no customer has snapshots on both sides (M53).

**Learners** (`engine/uplift/learners.py`), chosen by `uplift.learner`:

* **S-learner**: one model on the features plus the treatment flag. Uplift = its prediction with
  the flag set to 1 minus with it set to 0. Cheap, and it tends to shrink uplift towards zero. It is
  the baseline.
* **T-learner**: one model per arm; uplift = treated-arm prediction minus control-arm prediction.
* **X-learner** (the default; Künzel et al. 2019): the T-learner's two models, then a per-customer
  effect estimated for each arm with the other arm's model, then two regressors on those effects.
  They are blended with the treated share as the weight. It is the most accurate of the three when
  one arm is much smaller than the other, which is the usual shape of a campaign with a hold-out.

Each learner runs on `uplift.base_model`. `lightgbm` is fast and deterministic: the same data and
seed give identical predictions. `autogluon_fast` uses AutoGluon's `medium_quality` preset within
`uplift.time_limit_minutes`, and it is the configured default.

**Explanations** are SHAP values of the predicted uplift ("why this customer is persuadable"). For
the X-learner on LightGBM they are exact. For the other learners, and for AutoGluon, a LightGBM
surrogate is fitted to the predicted uplift and explained instead. Its R² is recorded as
`explanation_fidelity`, and the feature-importance caption says a surrogate was used. A customer
whose contributions are all zero gets no reasons. Inventing one would be fabrication.

---

## 5. Reading the Model page: Qini chart and AUUC

**The question the page answers:** if we contact customers in the order the model ranks them,
highest predicted uplift first, do we gain more conversions than contacting the same number at
random?

No single customer shows their own uplift. So every metric compares **groups**: rank the hold-out
by predicted uplift, walk down the ranking, and compare the treated and control customers seen so
far. The exact definitions, quoted from `engine/uplift/metrics.py`:

> **Ranking.** Rows are ranked by predicted uplift, highest first, with a *stable* sort on `-pred`
> (`kind="mergesort"`), so rows with equal predictions keep their input order.
>
> **Cumulative counts.** For the top `k` rows (`k = 0..n`): `N_t(k)`, `N_c(k)` are the treated and
> control counts and `Y_t(k)`, `Y_c(k)` their outcome sums. All curves are functions of these four.
>
> - Qini: `Qini(k) = Y_t(k) − Y_c(k)·N_t(k)/N_c(k)`, the scaled term 0 when `N_c(k) = 0`. The chart
>   point at `f = k/n` is `Qini(k)/n` (incremental conversions per hold-out customer) and the random
>   line is `f · Qini(n)/n`.
> - Uplift curve: `U(k) = (Y_t(k)/N_t(k) − Y_c(k)/N_c(k)) · k/n`, 0 while either arm is empty. Its
>   random line is `f · ATE`, where `ATE` is the overall treated rate minus the control rate.
> - `AUUC` is the trapezoid integral over `f ∈ [0, 1]` - every `k`, including `k = 0` - of
>   `U(k) − f·ATE`; `qini_coefficient` the same integral of `Qini(k)/n − f·Qini(n)/n`. Both are 0 for
>   a ranking no better than random, positive when the top of the ranking holds the customers the
>   action moves, negative when it holds the ones it puts off.
> - `uplift@fraction`: among the top `ceil(fraction·n)` rows, treated rate minus control rate; `None`
>   when either arm is empty there, because a rate of nobody is not zero.
> - Deciles: `np.array_split` of the ranked rows into ten groups, decile 1 the highest predicted.

**The Qini chart.** The horizontal axis is the share of customers contacted, best-ranked first. The
vertical axis is the extra conversions gained, per hold-out customer. The **dashed line** is random
targeting: a straight line from zero to what contacting everyone gains. The shaded area between the
curve and the line is what the model adds. A curve bowed well above the line means the top of the
ranking holds the persuadable customers. A curve on or below the line means the model is no better,
or worse, than picking at random. The curve usually rises and then flattens or falls. The fall is
the sleeping dogs at the bottom of the ranking, who convert *less* when contacted.

**AUUC** is that area as one number, and it is the metric the champion rule uses. Zero means no
better than random. The tile next to it gives its **95% interval**.

**Intervals** (DEC-605). Every interval on the page is a percentile bootstrap. `uplift.bootstrap_samples`
resamples (default 200) of the hold-out are drawn with replacement **within each arm**, so the
treated and control counts stay fixed. The model is held fixed, not refitted. The interval is the
2.5th to 97.5th percentile of the resamples. If any resample leaves a value undefined (for example a
top 10% with no control customer), the interval is shown as "—" rather than computed from the
remaining resamples.

**The verdict line** is `uplift_evaluation.json`'s `summary`:

* *"Targeting by predicted uplift beats random targeting: AUUC … (95% CI … to …)."* The whole
  interval is above zero. The pill reads **Measurable uplift**.
* *"No measurable uplift: the AUUC interval (… to …) includes zero, so this model cannot be shown to
  target better than random."* This is the honest answer on a campaign that changed nothing. Such a
  model is never promoted.
* *"… lies below zero, so this model targets worse than random."*

The page also shows the **ATE** (what contacting everyone gained, treated rate minus control rate,
with its interval). It also shows **uplift in the top 10/20/30%**, and the **decile chart and table**:
observed uplift per tenth of the ranking as bars (red below zero) with the predicted uplift as a
dot. A good model has observed bars that fall from decile 1 to decile 10 roughly in step with the
dots.

---

## 6. The four segments

Two numbers per customer, the predicted uplift and `p_control` (the chance of converting if not
contacted), place every customer in exactly one segment (`engine/uplift/segments.py`). The rules
apply in this order:

| Segment | Rule | Recommended action |
|---|---|---|
| **Persuadables** | uplift ≥ `persuadable_min_uplift` (default 0.02) | `Treat`, within the budget |
| **Sleeping dogs** | uplift ≤ `sleeping_dog_max_uplift` (default −0.01) | `Never treat (contact makes it worse)` |
| **Sure things** | otherwise, and `p_control` ≥ `sure_thing_min_probability` | `Don't treat (converts anyway)` |
| **Lost causes** | otherwise | `Don't treat (won't convert)` |

When `sure_thing_min_probability` is not set, it defaults to the **training base rate**. A customer
more likely than average to convert untreated is then a sure thing. The cuts actually applied are
recorded in `segments.json` and on the model card, and a scoring run reuses the training run's cuts.
A missing or non-finite prediction is refused, never put in a default segment.

**Sleeping dogs are never treated**, whatever the budget, cost or value. The targeting only ever
picks persuadables. The scoring stage checks again on the way out and fails the run rather than
export a treated sleeping dog.

---

## 7. Targeting within a budget

The Output page's **"Recommended to contact"** is `N`, the number of customers to treat
(`engine/uplift/policy.py`, `policy_recommendation.json`):

* Only persuadables can be chosen, and on a scoring run only eligible ones: not suppressed and not
  in the control group.
* They are taken highest predicted uplift first, up to `uplift.policy.budget_contacts` (no budget =
  every persuadable).
* When **both** `value_per_conversion` and `cost_per_contact` are set, selection stops before the
  first customer whose expected gain, `uplift × value`, is below the cost of contacting them.
* `stop_reason` says why `N` is not larger: `budget`, `value_below_cost` (also when both limits end
  at the same customer, since a bigger budget would not change `N`), `all_persuadables` or
  `no_persuadables`.

**Expected incremental conversions** is the number the page leads with:

    N × (uplift observed on the training hold-out among the same share of the ranking)

It comes with that observed uplift's bootstrap interval scaled by `N`. The model's own sum of
predicted uplift over the chosen customers is also recorded (`predicted_incremental_conversions`),
but it is not the headline: a model is not evidence for itself. On a scoring run, control and
suppressed customers are ranked but never chosen. So the share asked about is the **ranking depth**
the selection reaches, meaning the position of the last chosen customer among all scored customers,
not `N / rows` (DEC-604). If the hold-out cannot measure that share, the field is "—".

**Money.** `expected_cost = N × cost_per_contact`; `expected_value = expected incremental conversions
× value_per_conversion`; `expected_net_value = value − cost`. Each is shown only when every input
exists.

### What if the budget changed? (the budget curve, DEC-1200…1204)

The Output page's **"What if the budget changed?"** card sweeps the budget instead of fixing it. It
reads `GET /runs/{run_id}/uplift/profit-curve` (`engine/uplift/policy.py` `profit_curve`), which
replays the run's own recommendation at every number of contacts, from 0 to the most the rules
allow, and answers `ProfitCurve` (`engine/uplift/contracts.py`):

* **Same rules at every point.** Only eligible persuadables, highest predicted uplift first, with the
  scoring run's own control group, suppression and tie-break; sleeping dogs never; and when both a
  cost and a value are set, nobody whose `uplift × value` is below the cost - so the curve ends at
  `max_contacts`, not at every row. Each point is what `policy_recommendation.json` would say with
  `budget_contacts` set to its `contacts`, so **the point at the configured budget equals the
  recommendation field for field** (pinned by `tests/unit/uplift/test_profit_curve.py`).
* **Each point:** `contacts`, `expected_incremental_conversions` (contacts × the uplift observed on
  the hold-out at the depth that budget reaches, with its bootstrap interval), `expected_cost`,
  `expected_value`, `expected_net_value`, `roi` (= net value ÷ cost; "—" at zero cost) and the
  model's own `predicted_incremental_conversions`.
* **Band.** `net_value_low`/`net_value_high` turn the same bootstrap interval the recommendation
  quotes into money. It is honest for each point on its own, not a band for the whole curve at once,
  and it only measures the evaluation's noise (predictions held fixed). No hold-out, or no cost and
  value, means no band, `bands_available: false` and `bands_note` says why. Nothing is estimated
  another way.
* **Optimum.** `optimum` is the contact count of highest expected net value, searched over **every**
  count with the hold-out's point estimate (not only the ~40 plotted ones), fewest contacts on a tie,
  and 0 when every budget loses money. It is null with `optimum_note` when cost, value or a hold-out
  is missing.
* **Overrides.** `?cost_per_contact=` and `?value_per_conversion=` replace the run's settings for
  that answer only (`overridden: true`); nothing is stored or retrained. `?points=` (2…201, default
  41) sets how many evenly spaced counts are plotted; the configured point and the optimum are
  always added.
* **Replay check.** Before plotting, the route recomputes the recommendation from the saved scores
  (a scoring run) or hold-out (a training run) and compares it with `policy_recommendation.json` (and
  with who the scores file marks `Treat`); if they differ it answers `409 PROFIT_CURVE_UNAVAILABLE`
  rather than a curve that is not the run's.

On the page the cost and value inputs are prefilled from the run; the slider steps through the
computed points only (no interpolated figures) and starts on the configured budget; the chart
marks the configured budget (dashed line), the optimum (ring) and the slider's point (dot).

---

## 8. Scoring: the treat list

A scoring run of an uplift model goes through Phase 1's own `POST /runs` with `mode: score`
(section 13). It replays the model card's features, predicts, explains every row and then applies
the actions (`engine/uplift/actions.py`):

1. **Suppression** (consent, opt-out, recent contact) and the **control group** come from Phase 1's
   `engine.stages.actions.apply_actions`, unchanged. The same run id holds out exactly the same
   customers as a Phase 1 run would. When a consent ledger applies to the run (the use case maps to
   a purpose in `configs/privacy.yaml` and the client has recorded consent for it), the ledger's
   verdict is written into the consent column first, through the same `engine.privacy.consent`
   seam a propensity run uses (DEC-732): a customer without valid consent is suppressed as
   `consent_false`, and the run writes `consent_report.json` (M91). Propensity and uplift runs of
   the same rows against the same ledger suppress the same customers.
2. Persuadables chosen by the policy get `Treat`.
3. Persuadables not chosen get `Don't treat (below cost)` or `Don't treat (over budget)`.
4. Everyone else gets their segment's action. The `band` column holds the segment label.
5. **Plan J M96 (J5):** when the model's training run found that it does not beat risk ranking, and
   the use case has an approved propensity model, the same number of `Treat` rows is chosen by that
   model's score instead, and `ranking_choice.json` says why (`UPLIFT_NOT_BETTER_THAN_RISK`). A model
   trained before M96 ranks exactly as before. See section 12.

`scores.csv` columns: the key (each column of a two-column key), `uplift`, `p_treated`, `p_control`, `segment`, `band`, `action`,
`reason_1…n`, `suppressed_reason`, `control_group`, `intended_treatment`. The Output page's
**Download treat list** is this file.

**`intended_treatment`** marks the customers the policy treats **plus** the control customers it
*would* have treated had they not been held out: persuadables ranked at or above the last chosen
customer. The campaign-results comparison (section 9) is made inside this set, so both sides pass
the same rule. Customers with exactly the same predicted uplift are ordered by a per-customer hash
seeded by the run (DEC-606). A budget that cuts through a block of ties therefore takes a random part
of it, not the first rows of the file.

With a two-column key the control group and suppression are decided **per customer**, as for a
Phase 1 periodic run: a customer is held out at every snapshot of the scoring file or at none.

**Drift (M53).** Every scoring run writes `uplift_drift.json`:

* **Feature drift** is Phase 1's PSI, computed by Phase 1's own code
  (`engine.stages.score.compute_drift`) against the `drift_baseline.json` the uplift training run
  stored over the raw columns its model uses, with `monitoring.drift_psi_threshold`. A model trained
  before M53 stored no baseline; its feature drift is `null` with the reason, never an invented zero.
* **Treated share.** When the scoring file carries the model's treatment column with 0/1 values (a
  re-scored campaign, the next wave of the same experiment), its treated share is compared with the
  training data's: within tolerance when `|current − training| ≤ uplift.drift_treated_share_tolerance`
  (default 0.05). The rule is an absolute difference, not a significance test, because a test's
  verdict depends on the file's size: on a million rows a z-test calls a 0.2-point difference
  significant, on two hundred it misses a 10-point one. The two-proportion p-value is recorded
  beside it, for information. A file without the column - the usual scoring file, whose campaign
  has not happened yet - is `not_applicable`, with the reason.

Phase 1's Data and Output pages show it for an uplift scoring run, and `scoring_summary.json`
carries the feature drift verdict as it does for a Phase 1 run.

---

## 9. The Campaign results page, and maturity

**The question it answers:** did the campaign actually work? It is measured from real outcomes, not
from the model.

After the campaign has run, upload an outcomes file with one row per customer: the primary key, the
outcome, and optionally a treatment date. `POST /runs/{run_id}/campaign-results` then compares the
**treated** customers with the **control** customers held back by that scoring run
(`engine/uplift/incrementality.py`):

* Suppressed customers are always left out.
* For an uplift run the comparison is made inside `intended_treatment` (section 8). For a Phase 1
  scoring run it is made over the requested `bands`, or over every eligible customer. Any finished
  scoring run with a control group can be measured, not only uplift ones.
* **Lift** = treated conversion rate − control conversion rate, with a 95% Newcombe (hybrid Wilson
  score) interval. The page also shows the **relative lift** (lift ÷ control rate), the **incremental
  conversions** (lift × treated customers, with the interval scaled the same way) and a two-sided
  **p-value** (pooled two-proportion z-test). The p-value is "—" when it is undefined, for example
  when both groups are all 0 or all 1.
* A customer with no row in the outcomes file, or a blank outcome, is counted in
  `rows_without_outcome`. They are never treated as a non-conversion, because that would pull the
  lift towards zero.

**Maturity.** An outcome such as "reactivated within 90 days" is only known once 90 days have
passed. Each customer's treatment date is the outcomes file's date column if given, else the time
the scoring run finished. A customer is **mature** when `treatment date + outcome_window_days ≤ as
of` (as of = now unless you set it).

* **No customer mature yet:** the page shows only **"Results available on \<date\>"**, the day every
  customer's window will have elapsed. No rate or lift is shown before then.
* **Some mature:** the report is computed on the mature customers only. The customers still inside
  their window are counted as excluded, with the date on which all of them will be mature.

The report is stored as `incrementality_report.json` and can be read again with
`GET /runs/{run_id}/campaign-results`. It is labelled causal when the scoring run held back a
non-empty control group among the eligible customers. The engine draws that group at random, per
customer, so the comparison is an experiment.

**One measurement path (Plan J M94, DEC-1304 (c)).** `POST /runs/{run_id}/campaign-results` no longer
calls `measure_incrementality` itself: it calls `engine.measurement.measure.measure_campaign`, the one
function every measured campaign result now comes from, with the run's scores and no test plan. With
no plan that function *is* `measure_incrementality` - same arguments, same report, byte for byte
apart from `computed_at` - so this page, step 4 below (which calls this route) and the existing
tests are unchanged. A **campaign** (`POST /campaigns {run_id, treatment_start?}`, `docs/DECIDE.md`
§8) measures the same scores through the same function, with three differences that matter once a
list goes out days after it was made: the treatment time is the day it really went out; a result is
refused (`CAMPAIGN_NOT_MATURED`, with the date) until *every* customer's window has elapsed, where
this page shows the mature part; and a registered test plan fixes the window, the outcome and the
population in advance (`TEST_PLAN_CHANGED` otherwise) and labels a read before its analysis date an
early look, with no verdict. The report gains two optional fields for that, `test_plan_hash` and
`early_look` (null and false here). M49's `POST /runs/{id}/outcomes` stays model monitoring: its
`incrementality_input.json` is never presented as a campaign result.

### Step 4 of a use case: "Measure the campaign" (Plan H M83)

The same measurement is also step 4 of every use case that acts on customers, on the scoring run's
own Results, so a marketer never has to find this page. Which use cases have it is configuration:
`actions.contacts_customers` (true unless the use-case YAML says false, as the operational
`order-fulfillment` and `fault-prediction` do) and `actions.control_group_fraction > 0`, and never an
AI-written-text use case (`engine.uplift.measure.measure_offered`).

* **One upload.** `POST /runs/{run_id}/measure` takes only the outcomes file. The outcome column is the
  use case's own target when the file has it, else the one column besides the customer id; the
  window is `uplift.outcome_window_days`, else the use case's label `horizon_days`. It then calls
  `POST /runs/{run_id}/campaign-results` with that body, so the report is the one this page shows -
  and, through it, the one measurement path (`measure_campaign`, above).
* **One plain line.** The response adds a verdict read off the report: "The campaign added about N
  conversions" (or "prevented about N cases" for an outcome the use case exists to prevent, the
  value view's `configs/pilot/value.yaml` rule), "No clear effect yet" when the interval includes
  zero, "Outcome window not over yet" while it is immature. Rates, the interval and the p-value stay
  under **Details**.
* **Learn who to contact next time.** When both groups have at least `uplift.min_arm_rows` customers
  and `uplift.min_arm_positives` responders, `POST /runs/{run_id}/measure/learn` builds the experiment
  file on the server - the run's own input rows, `contacted` = 1 for every eligible customer not held
  back and 0 for the "Control (hold out)" rows, and the outcome as the use case's target - stores it
  as a training upload and starts `POST /uplift/runs` on it. Suppressed customers are in neither arm;
  for an uplift run only its `intended_treatment` customers are kept. The link then goes to the new
  model's contact list (`#/uplift/<use case>/output/<run>`).

`campaign_measure.json` in the run directory records the outcomes file and the uplift run learned
from it.

### Is the held-back group big enough? (control-group size)

A scoring run holds a random `actions.control_group_fraction` of the eligible customers back (default
10%, at most 50%). Whether that is enough to *see* a real lift is a question of statistical power, and
it can be answered before the campaign. The scoring run's Output page has a card for it,
"Is the held-back group big enough?": "With 10,000 eligible customers and a 10% control group, this
campaign can reliably detect a lift of 3 points (30% relative) or more." Two boxes try another control
percentage, or ask the reverse - the smallest control group that detects a lift you name (or "not
possible even holding back the maximum 50%").

* **What "reliably" means.** The same two-sided pooled two-proportion z-test that
  `engine/uplift/incrementality.py` runs after the campaign, at alpha 0.05, finds the lift with
  probability at least 0.80. The formula, written out in `engine/uplift/power.py`: with
  `p1 = p0 + lift`, pooled rate `p̄`, `se0 = sqrt(p̄(1-p̄)(1/n_t + 1/n_c))` and
  `se1 = sqrt(p0(1-p0)/n_c + p1(1-p1)/n_t)`, power is `Φ((lift - z·se0)/se1)` (plus the far tail), and the
  smallest lift with power 0.80 is found by bisection. Control rows are `round_half_up(N·f)`, as
  `actions` draws them. Approximations: normal approximation to the binomial (loose under about 5
  conversions per arm), the baseline taken as known, rows treated as independent customers (a
  periodic file with several snapshots per customer needs a slightly larger lift).
* **Where the baseline rate comes from.** The scoring run's `scoring_summary.json`: `score_mean`, the
  model's average predicted chance across everyone scored (a yes/no model only). Eligible customers
  are `rows_scored` minus the suppressed rows; held-back customers are `control_group_rows`. Training's
  positive rate lives in another run's `evaluation.json`, which a scoring run does not carry, so it is
  not used. The card says which baseline it assumed. A figure the run did not write, a score that is
  not a rate, or a run that held nobody back shows "—" with the reason, never a guess.
* **Where it is computed.** In the browser (`ui/power.js`, no new route), as a copy of
  `engine/uplift/power.py`; `tests/fixtures/power_cases.json` holds cases both sides are checked
  against (`tests/unit/uplift/test_power.py`, `power.test.mjs`).

---

## 10. Off-policy evaluation (OPE)

**The question it answers:** how would a *different* targeting rule have done, judged on the
randomised hold-out we already have, without running a new campaign? The Model page has a small
form for it ("Top share of customers, percent"), and the API takes a rule
(`POST /runs/{run_id}/uplift/ope` with `{"top_share": 0.2}` or `{"min_uplift": 0.01}`, or both) on a
finished uplift training run (`engine/uplift/ope.py`).

The rule is applied to the hold-out's predicted uplift, and up to three estimates of the rule's
conversion rate are reported, each with a 95% interval. IPS and DR are always there; SNIPS is left
out when it is undefined (DEC-661):

* **IPS** (inverse propensity scoring): unbiased when the treatment probabilities are right, which
  they are by construction in a random experiment.
* **SNIPS** (self-normalised IPS): slightly biased, usually steadier, and it stays within 0 to 1.
  It divides by the sum of the weights, and a customer's weight is zero unless the campaign gave them
  what the rule would give them. When that holds for nobody (possible on a tiny hold-out) it is 0/0,
  and it is then **left out** of the report, never shown as 0.
* **DR** (doubly robust): the model's prediction of the rule's value, corrected by the weighted
  errors. It is unbiased if either the propensities or the outcome model are right, and usually has
  the narrowest interval.

The report also gives DR estimates of **treat everyone** and **treat no one** as the yardsticks, and
the observed rate under the logged campaign. The logging propensity is the model card's treated
share, and the outcome predictions come from `uplift_holdout.parquet`, made by a model that never saw
those rows. This is what Phase 5's action-policy agent will be judged with.

---

## 11. What "not causal" means

"Causal" means the difference between treated and control customers was **caused by the action**.
That is only true when a coin decided who was treated. When `TREATMENT_NOT_RANDOM` was acknowledged,
the campaign was targeted. Treated customers then differ from untreated ones in ways the model may
not see, and "uplift" measures the targeting rule as much as the action.

Such a run still trains, but:

* every uplift artefact carries `causal: false`, and every screen shows a banner with the engine's
  note: *"Not causal: the treatment was not randomly assigned, so these numbers describe who was
  contacted, not what contacting them changed."*
* the model is **never promoted** to champion (section 12);
* it may be used for exploration, never as proof that a campaign works.

---

## 12. The champion rule for uplift

Same shape as Phase 1: a challenger must beat the champion **on the same hold-out** by
`champion_min_improvement_pct`, here on AUUC (`engine/uplift/champion.py`). Two gates come first:

1. **Causal.** A not-causal model is never promoted.
2. **Measurable.** The challenger's AUUC interval must lie entirely above zero (`ci_low > 0`). A
   missing interval counts as not measurable.

Then:

* **No uplift champion yet:** promoted, but only on a use case **configured** as uplift. On a use
  case configured for classification, an uplift run stays a candidate until a person promotes it on
  the Models page (DEC-609). Otherwise the one champion slot would switch that use case's Phase 1
  scoring to uplift.
* **A propensity champion holds the slot:** AUUC cannot be compared with ROC AUC, so the uplift model
  stays a candidate with the reason recorded. This mirrors Phase 1's `METRIC_MISMATCH`.
* **An uplift champion holds the slot:** it is reloaded, re-scored on this run's hold-out, and the
  challenger must beat its AUUC by at least `champion_min_improvement_pct` percent of |champion
  AUUC|. When the champion's AUUC is zero or below, any strictly greater AUUC wins, and no percentage
  is reported. Two evaluations with different row, treated or control counts are refused as not the
  same hold-out, and so are two whose `holdout_fingerprint` (a sha256 of the hold-out's sorted
  primary keys, in `uplift_evaluation.json`) differs: equal counts on different customers are caught
  too (DEC-670).
* `governance.approval_required` is honoured exactly as in Phase 1: a winning model waits as
  `pending_approval`.

Phase 1's champion rule in `engine/registry.py` is not changed.

### Does the model earn its place? Beats-risk, calibration and fold stability (Plan J M96)

An uplift model is worth using only where it ranks customers better than plain risk ranking does at
the same budget. The champion rule above does not ask that, and it stays frozen; M96 measures it and
shows it beside the decision.

**What every training run now measures** (in `uplift_evaluation.json`, fields that are null on an
evaluation written before M96):

* `baseline_comparison` - the hold-out ranked three plain ways and scored with the same AUUC: by the
  model's own `p_control` (plain risk), by its `p_treated`, and by the use case's **last approved
  propensity model** (its most recently promoted binary-classification version, scored through
  Phase 1's own `score.predict`; when there is none, or it cannot score the hold-out, the row says
  why). Each row carries the uplift model's AUUC minus that ranking's, with a **paired** bootstrap:
  the resample draws depend only on the seed and the arm sizes (section 5), so with the evaluation's
  own seed every resample holds the same customers for both rankings
  (`engine.uplift.metrics.paired_auuc_resamples`). The **beats-risk check** is decided against the
  propensity model when it was scored, else against `p_control`, and passes only when the paired
  difference's lower bound is above zero. Otherwise the sentence says "does not beat risk ranking"
  and whether the range includes zero or lies below it.
  *Ties.* Every ranking in the comparison (and in the calibration below) orders equal scores by a key
  drawn from the evaluation's seed alone, the same key for every ranking
  (`engine.uplift.metrics.tie_broken_ranks`), not by file order. A calibrated propensity score is often
  a step function, and a file that lists treated customers first would otherwise put every tie
  block's treated rows on top and bias the baseline's AUUC: a useless uplift model "beat" a five-level
  risk score that way. A ranking without ties is ranked exactly as the evaluation ranks it, so the
  uplift model's AUUC here is the evaluation's own; the champion rule's AUUC is unchanged.
* `calibration_by_decile` - each decile's mean predicted uplift against its observed uplift, with a
  bootstrap interval from the same draws, the row-weighted mean |observed − predicted|, and a verdict:
  calibrated when at least 8 in 10 of the deciles with an interval contain the prediction (not judged
  with fewer than five).
* `fold_auuc` - **off by default** (`uplift.evidence.fold_auuc`) and for the LightGBM base model only.
  When on, the meta-learner is refitted on all but one of `folds` folds (stratified on treatment and
  outcome, by customer under a two-column key) and its AUUC measured on the fold it did not see.
  Each fold's AUUC carries a bootstrap interval (resampled within arms on that fold, the run's
  `bootstrap_samples`). Stable means every fold was measured, no fold's interval lies wholly at or
  below zero, and the fold AUUCs differ no more than their bootstrap standard errors explain
  (Cochran's Q against the chi-square 95th percentile on `folds − 1` degrees of freedom). A bare
  "every fold above zero" rule called a third of sound models unstable on a 4,000-row file, because
  a fold of 800 rows often lands below zero by chance. The summary lists each fold's AUUC with its
  interval. When off, the field still says what turning it on would cost: `estimated_refit_seconds`,
  from this run's own fit time scaled to the fold sizes.

**The Approver's screen** shows these as advisory checks (`ApprovalItem.checks`, `{code, passed,
message}` from `engine/model_gates.py`): `UPLIFT_NOT_BETTER_THAN_RISK`, `UPLIFT_UNSTABLE_ACROSS_FOLDS`
and `UPLIFT_MISCALIBRATED`. `passed` is null ("Not measured") for a model trained before M96 or a
check that is off. They never disable Approve or Reject, and a propensity model has none.

**Which ranking a contact list uses (J5).** A scoring run reads the beats-risk verdict its model's
training run stored (`engine.decide.ranking`, called from the uplift actions stage):

| Training verdict | Approved propensity model | The contact list | `ranking_choice.json` |
|---|---|---|---|
| not computed (model trained before M96) | - | ranked by uplift, exactly as before | not written |
| beats risk | - | ranked by uplift | `ranking: uplift`, `code: null` |
| does not beat risk | exists and scores the file | the same number of `Treat` rows the uplift policy chose (equal budget), chosen by the propensity model's score among eligible, non-sleeping-dog customers; `intended_treatment` follows the same ranking; expected incremental conversions are null (the hold-out's top-uplift share does not describe this list) and `predicted_incremental_conversions` is the uplift model's prediction summed over the customers this list contacts | `ranking: propensity_model`, `code: UPLIFT_NOT_BETTER_THAN_RISK` |
| does not beat risk | none, or it cannot score the file | ranked by uplift, with the warning | `ranking: uplift`, `code: UPLIFT_NOT_BETTER_THAN_RISK` |

The check was decided at training time against the propensity model approved then (or `p_control`
when there was none); the fallback is the propensity model approved now. When they differ, the list
still falls back, and the reason names both: `ranking_choice.json` records `compared_baseline`,
`compared_model_id` and `fallback_matches_check: false`.

Suppression and the control group are Phase 1's, untouched, and a sleeping dog is never treated in
either ranking. The Output page shows the reason above the contact list, and the budget curve of a
list ranked by the propensity model is refused (`PROFIT_CURVE_UNAVAILABLE`), because it would describe
a list the run did not make.

**The equal-budget comparison** (`risk_comparison.json`, `GET /runs/{run_id}/risk-comparison`,
Viewer). Off by default (`uplift.evidence.risk_comparison`, LightGBM base model only). On the
training run's randomised rows, uplift top-N against risk top-N at the same budget
(`engine.measurement.compare`):

1. *Cross-fitted on a ring.* Every row is scored by an uplift learner and a plain LightGBM risk model
   fitted on other folds only - fold `k`'s by the next `(folds - 1) // 2` folds, `k+1 .. k+L` round
   the ring - so no row is ever scored by a model trained on it. Why not all other folds: then each
   fold's outcomes help choose who is contacted in the others and the fold results are correlated;
   the interval covered 92%, not 95%, on a null population. On the ring, of any two folds one never
   trained the other's models, and the interval holds its coverage. Each comparison model therefore
   learns from 40% of the rows (at 5 folds), which makes the comparison a little pessimistic for
   models that need many rows.
2. *Equal budget.* In every fold both rankings contact `top_share` of the fold; equal scores are
   ordered by a key drawn from the run's seed, never by file order, so who is contacted depends on
   the score alone, not on whether the file listed treated customers first.
3. *Valued off-policy.* Each top-N goes through `engine.uplift.ope.evaluate_policy` with the rows'
   recorded treatment probabilities (`uplift.evidence.propensity_column`, M92's
   `treatment_probability`; rows outside (0, 1) are left out and counted; without the column, the
   treated share) and the cross-fitted `p_treated`/`p_control` as its outcome model. The extra
   conversions over contacting nobody are the sum of the per-row doubly robust effect scores of the
   rows contacted (`ope.dr_effect_terms`), which equals `rows × (DR(policy) − DR(treat none))`; the
   uplift-minus-risk difference is paired per row, with a 95% normal interval; per rupee divides by
   contacts × `uplift.policy.cost_per_contact` and is null without a cost.

The comparison's interval has a nightly coverage test on simulated heterogeneous-effect, null-effect
and risk-driven populations (`tests/statistical/test_risk_comparison_coverage.py`, the four Monte
Carlo standard error band of DEC-1305).

---

## 13. How to run it

### In the UI

1. Open **Settings → Advanced → Uplift workbench**. The screens live at `#/uplift` and
   `#/uplift/<use case>`. A use case's own screen does not link here: under Plan H uplift is step 4
   of a use case, **Measure the campaign**, whose "Learn who to contact next time" trains an uplift
   model and links to its contact list (docs/UI_AUDIT.md §8.4 item 8).
2. **Train uplift model**: upload the campaign file, pick the primary key, the **treatment column**
   (from the detected 0/1 columns) and the **outcome column**, and run. If a check blocks, the
   reasons appear in place. `TREATMENT_NOT_RANDOM` offers an acknowledge button.
3. Follow the run on the Running screen (the same stage rows as Phase 1). Then open **Model** (Qini,
   AUUC, deciles, OPE) and **Output** (segments, targeting).
4. **Score new data**: upload the customers to rank and choose a trained uplift model. The Output
   page of that scoring run has **Download treat list (CSV)** and a link to **Campaign results**.
5. After the campaign, open **Campaign results** (`#/campaign/<use case>/<run>`), upload the
   outcomes, choose the outcome column (and treatment date column and window if you have them), and
   **Measure the campaign**. A Phase 1 scoring run's screen also links to its campaign results.

### Through the API

```bash
# 1. upload the campaign file
curl -F file=@campaign.csv -F mode=train http://localhost:8000/uploads            # -> {"upload_id": ...}
# 2. which columns could be the treatment?
curl "http://localhost:8000/uploads/$UPLOAD/treatment-candidates?use_case=win-back-campaign"
# 3. start an uplift training run (409 with both reports when a check blocks)
curl -H 'content-type: application/json' -d '{"use_case": "win-back-campaign", "upload_id": "'$UPLOAD'",
      "primary_key": "customer_id", "target": "reactivated_90d", "treatment_column": "treated"}' \
     http://localhost:8000/uplift/runs                                             # -> 202 {"run_id": ...}
#    to go ahead on a targeted campaign, marked not causal:
#    add "overrides": {"validation": {"acknowledged": ["TREATMENT_NOT_RANDOM"]}}
# 4. poll, then read the uplift artefacts
curl http://localhost:8000/runs/$RUN
curl http://localhost:8000/runs/$RUN/uplift/uplift_evaluation.json   # also qini_curve.json, segments.json,
                                                                     # policy_recommendation.json, uplift_validation.json
# 5. off-policy estimate of "treat the top 20%"
curl -H 'content-type: application/json' -d '{"top_share": 0.2}' http://localhost:8000/runs/$RUN/uplift/ope
#    the budget curve, with a cost and a value for this answer only (any finished uplift run)
curl 'http://localhost:8000/runs/$RUN/uplift/profit-curve?cost_per_contact=1&value_per_conversion=40'
# 6. score new customers with an uplift model through Phase 1's own endpoint
curl -H 'content-type: application/json' -d '{"use_case": "win-back-campaign", "mode": "score",
      "upload_id": "'$SCORE_UPLOAD'", "primary_key": "customer_id", "model_version_id": "'$MODEL'"}' \
     http://localhost:8000/runs
curl -O http://localhost:8000/runs/$SCORE_RUN/scores.csv              # the treat list
# 7. after the campaign: measure it from an uploaded outcomes file
curl -H 'content-type: application/json' -d '{"upload_id": "'$OUTCOMES'", "outcome_column": "reactivated_90d",
      "outcome_window_days": 90}' http://localhost:8000/runs/$SCORE_RUN/campaign-results
```

Every route and body is listed in [`API.md`](API.md). Uplift artefacts are served only from
`GET /runs/{run_id}/uplift/{name}` (DEC-602). A request for any other name is `404 ARTEFACT_UNKNOWN`.

### Tests

```bash
make uplift-test     # every Phase 3b suite: engine, API, flows and UI module, slow ones included
make test            # the fast suite, which includes the fast uplift tests
```

The UI module's node tests run when `node` is installed. Otherwise they are skipped and the reason is
printed. So does `tests/unit/uplift/phase1_pages_uplift.test.mjs` (M53), which renders Phase 1's
Data, Model and Output pages (`ui/pages.js`) from uplift artefacts.

Two-column keys end to end - train, grouped hold-out, the mixed-arm refusal, composite scoring,
drift and campaign results joined on both columns - are `tests/integration/uplift/test_uplift_two_column_keys.py`;
the checks, the grouped split and the drift rules on their own are
`tests/unit/uplift/test_uplift_two_column_keys.py` and `tests/unit/uplift/test_uplift_drift.py`.

The real-browser journey (DEC-660) drives the product in Chromium: an uplift upload, the
not-random acknowledgement, training, the Model and Output pages checked number for number against
the artefacts, scoring, the Campaign results page before and after maturity, and a 390 px phone
width. It needs `playwright` and a Chromium build, is marked slow, and skips with its reason when
either is missing:

```bash
pytest tests/integration/uplift/test_uplift_browser.py -m slow
```

The metric cross-checks against scikit-uplift and causalml (section 16) run when those libraries are
installed and skip, saying so, when they are not.

---

## 14. Configuration

The `uplift:` block of `configs/engine.yaml:defaults`, overridable per use case. Every field is read
only when `problem_type` is `uplift` (DEC-601).

| Setting | Default | Per-run override | Phase 5 agent may edit |
|---|---|---|---|
| `learner` | `x_learner` (`s_learner`, `t_learner`; plan B's `s`, `t`, `x` are accepted, DEC-671) | yes | no |
| `base_model` | `autogluon_fast` | yes | no |
| `treatment_column`, `treatment_column_hints` | none; `[treatment, treated, contacted, is_treated]` | yes | no |
| `treatment_date_column`, `campaign_id_column`, `outcome_window_days` | none | yes | no |
| `min_arm_rows`, `min_arm_positives` | 1,000; 50 | yes, recorded in `run.json`'s `overrides` (a small random arm cannot fake causality, but on a very small arm the percentile bootstrap intervals are less reliable than their 95% says; DEC-680) | no |
| `randomness_auc_max` | 0.60 | **no** (DEC-607) | no |
| `bootstrap_samples`, `test_fraction`, `time_limit_minutes` | 200; 0.30; 10 | yes | no |
| `drift_treated_share_tolerance` | 0.05 (absolute difference in treated share; section 8, M53) | yes | no |
| `segments.*` (the three cuts) | 0.02; −0.01; base rate | yes | **yes** |
| `policy.*` (budget, cost, value, margin, ROI) | none | yes | **yes** |
| `evidence.fold_auuc`, `evidence.risk_comparison` (Plan J M96, section 12) | off; off | yes | no |
| `evidence.folds`, `evidence.top_share`, `evidence.propensity_column` | 5 (3 to 10); 0.2; none (never a feature) | yes | no |

### Ranking by net money (Plan J M97, DEC-1307)

`uplift.policy` takes, besides `budget_contacts`, `cost_per_contact` and `value_per_conversion`:

| Setting | Meaning | When unset |
|---|---|---|
| `value_column` | The column holding each customer's value in rupees (order value, premium, balance, ARPU). Setting it ranks customers by expected net money. | ranked by uplift, as before |
| `margin_pct` | The share of that value that is margin, 0 to 100. | 100 % |
| `horizon_months` | Months of value counted, 1 to 120. | 1 |
| `min_roi` | The least return each contact must make, as net value ÷ cost (0.15 is 15 %). | 0: a contact must pay for itself |

**Net value per customer** = uplift × value × margin × horizon − offer cost × p_treated − contact cost.
The contact cost is `cost_per_contact`, else the `value:` block of `configs/pilot/value.yaml` (editable
India defaults: WhatsApp marketing ₹0.86, WhatsApp utility ₹0.13, SMS ₹0.15, e-mail ₹0.05, voice ₹0.70;
offer cost ₹0), read through one function, `engine.pilot.roi.lookup_value_costs`, which M99's
catalogue will take over. The block is read only by a use case that sets `value_column`; the ROI
form's defaults (`RoiInputs`) are not changed by it. A run reads it **once**, uses those costs for
every step, and records them on `policy_recommendation.json` (`contact_cost`, `offer_cost`); the
budget curve replays the recorded costs, never the file as it reads later, and answers 409 when the
replay does not reproduce the recorded cost of the list.

**Who is chosen.** Eligible persuadables whose net value reaches `min_roi ×` their own cost, best net
value first, up to the budget. A customer below that is left out *wherever it ranks*: with an offer
cost each customer has its own cost (`offer cost × p_treated`), so a customer with a high net value
and a high cost can miss `min_roi` while a customer below it in the ranking, with a lower cost,
reaches it - and is chosen. With one cost for every customer (and on every run without
`value_column`) the customers left out are exactly the tail of the ranking, as before M97.

* **The money is computed once and passed down.** `engine.uplift.policy.customer_net_values` returns
  every row's net value, cost and cut; the ranking, the cut, `choose_contacts`, `recommend_policy` and
  `profit_curve` all take that one result, so `min_roi` and the per-row costs hold through every entry
  point, and the curve's point at the budget is the recommendation, field for field, in every money
  configuration. The cost of every contact count comes from one cumulative sum: the curve is linear in
  the rows (about a second at 200,000 rows with per-row costs).
* **Conversions stay conversions.** `expected_incremental_conversions` is `N ×` the hold-out's observed
  uplift; `expected_value` is `N ×` the hold-out's **value-weighted** observed uplift × margin × horizon.
  Both are read from the hold-out ranked the same way as the list (by each hold-out customer's net
  value), so "the top share" is the same kind of customer the list chose.
* **Margin and horizon apply everywhere.** On a run without `value_column`, one conversion is worth
  `value_per_conversion × margin × horizon` in the ranking cut, the reported value and the curve alike.
  With neither set it is exactly `value_per_conversion`: a run configured before M97 computes the same
  numbers, to the last digit.
* **Nothing is invented.** The training hold-out (`uplift_holdout.parquet`) carries a `value` column
  only when the training run was configured with `value_column` and its file has it; missing values
  stay missing. A scoring run ranked by value quotes the hold-out only when the training run stored
  values of the **same** column (its `run_config.json` says which); otherwise the expected conversions
  and money are null and `money_note` says why. A customer without a value counts as zero value (so
  is never chosen for its value) and is counted in `values_missing`; when more than 10 % of the
  customers (or of the hold-out) have none, the money is null with the reason. One missing value never
  fails a scoring run.
* **What a value-ranked scoring run writes.** `scores.csv` gains two columns at the end,
  `customer_value` and `net_value`; `policy_recommendation.json` and the budget curve gain
  `money_note`, `values_missing`, `contact_cost` and `offer_cost`, and the curve `value_weighted` and
  `value_basis`. A run without `value_column` writes exactly the scores columns it did, and those four
  fields are null on it - except `money_note`, never set on such a run either.
* **What every run with money gains.** `policy_recommendation.json` gains `net_value_low` and
  `net_value_high` on **every** run, value-ranked or not, that has a cost, a value and a measured
  interval: the band of the curve's point at the budget, so the identity "point at the budget = the
  recommendation" covers it too. Every other field of a run configured before M97 is what it was; the
  new fields have defaults, so older files still read.
* **The value basis.** The curve's `value_basis` says, in words, what the money is based on: "each
  customer's monthly_spend × 30% margin", or "₹1,500 per conversion × 30% margin × 3 months" in the
  INR format, the paise kept when the value is not whole ("₹7.70 per conversion × 1 month"). It is null
  - and the budget card shows no caption - for a run with neither `value_column`, `margin_pct` nor
  `horizon_months`, whose money is the value of one conversion as configured.
* **The budget curve** (`GET /runs/{id}/uplift/profit-curve`) replays a value-ranked run the same way,
  and takes `min_roi` for that answer only. `value` is another name for `value_per_conversion`; giving
  both answers `422 PROFIT_CURVE_QUERY_INVALID`.
* **Propensity runs** of a use case that sets `value_column` write `expected_gross_value.json`
  (`engine.decide.value`): p × value × margin × horizon − cost, per band, labelled **not incremental**
  everywhere, with two run-manifest metrics (`expected_gross_value_not_incremental`,
  `expected_gross_value_rows_missing`). Phase 1's `apply_actions` and `scores.csv` are not changed.

The persuadable-only and sleeping-dog guards are unchanged: a sleeping dog is never treated at any
value.

The control-group fraction and suppression rules live in `actions:`, are shared with Phase 1, and are
not agent-editable. `engine.uplift.config.uplift_agent_editable_paths()` returns this map for Phase 5.

### The holdout and the explore slice (Plan J M92, DEC-1302)

Who is held out, and who is explored, is decided in `actions:` for propensity and uplift runs alike:
an uplift run inherits it through Phase 1's `apply_actions`. Both settings are config-only (a use-case
file or `configs/engine.yaml`); neither can be changed per run, and neither is agent-editable.

| Setting | Default | Meaning |
|---|---|---|
| `actions.holdout.scope` | `run` | `run`: today's rule, unchanged - a new control group every run, the `control_group_fraction` of the eligible rows with the smallest run-seeded digests. `use_case`: the same customers held out of every run of this use case. `universal`: the same customers held out of every use case that uses this scope. |
| `actions.holdout.fraction` | none | Required under `use_case` and `universal` (0 < f ≤ 0.50) and then **wins** over `control_group_fraction`; refused under `run`, where it would not be read. |
| `actions.control_group_fraction` | 0.10 | Keeps its meaning under `scope: run`. |
| `actions.explore_fraction` | 0 | 0 to 0.10. The share of explore candidates treated anyway (below). |

**One fraction for every reader.** `engine.holdout.spec.effective_holdout_fraction(actions)` is the
holdout share: `holdout.fraction` under a persistent scope, `control_group_fraction` under `run`.
`measure_offered` (step 4), `incrementality_input.json`'s `control_group_fraction`
(`engine/scheduling/outcomes.py`) and the browser's copy of the step-4 rule read it.

**Membership.** Under a persistent scope a customer is a member when
`int(sha256(f"{salt}:{scope_key}:{entity}")[:16], 16) < fraction × 2^64`, with `scope_key` the use-case
id or `universal` and `entity` the key as text (the customer column of a two-column key). It does not
depend on the run, on who is eligible or on row order, and smaller fractions nest inside larger ones.
The control group is `eligible ∧ member`: a suppressed member stays a member, it was never going to be
contacted. The realised share is binomial around the fraction (on 10,000 customers at 10%, within
about ±1.0 point 99.9% of the time: 3.29 × √(0.1 × 0.9 / 10,000)), not exact as under `run`. A use
case whose id is `universal` or `explore` cannot take `scope: use_case` (`HOLDOUT_SCOPE_RESERVED`): its
membership text would be the universal holdout's, or share the explore draw's prefix.

**The salt** is `MARKETING_AI_HOLDOUT_SALT` (a secret setting, at least 16 characters, no default).
Its fingerprint - never the salt - is kept in `platform_setting`. A persistent scope without the salt
fails the run at its first stage with `HOLDOUT_SALT_MISSING`; a different salt from the recorded one
with `HOLDOUT_SALT_CHANGED`.

**Epochs.** Each persistent holdout has a ledger entry: its epoch and the largest fraction used in it.
Raising a `use_case` holdout's fraction keeps every member and stays in the epoch. Lowering it would
contact customers who were held out, so scoring is refused (`HOLDOUT_FRACTION_LOWERED`) until an Admin
starts a new epoch at the lower share with `PUT /holdout` (audited). The **universal** holdout has one
share for every use case on it - the ledger's: a use case asking for more is refused too
(`HOLDOUT_FRACTION_MISMATCH`), because within one epoch it would hold out customers another use case
contacts. To change the universal share, set every universal use case to the new share, then have an
Admin start a new epoch at it; `PUT /holdout` refuses a universal epoch at a share some universal use
case does not declare (`HOLDOUT_FRACTION_MISMATCH`, 409). Rotating the salt (`PUT /holdout` with
`rotate_salt: true`) starts a new epoch of every persistent holdout; rotating to the salt already
recorded is refused (`HOLDOUT_SALT_UNCHANGED`, 409), so the audit trail never records a reshuffle that
did not happen. `GET /holdout` (Viewer) shows the salt's state by fingerprint, every ledger entry and
each use case's scope, fraction, explore share, epoch and the share its epoch has reached
(`epoch_fraction`).

**The explore slice.** "Selected" is decided before the holdout: on an uplift run the policy's
`intended_treatment` rows (its `Treat` rows plus the held-out rows it would have treated), on a
propensity run every band but the lowest. Candidates are rows that are eligible, not holdout members,
not selected and not a predicted sleeping dog. Each candidate customer is explored when
`sha256(f"{salt}:explore:{seed}:{entity}")` falls under `explore_fraction × 2^64` - a second label,
re-drawn every run. A sleeping dog is never explored. The scored action in `scores.*` is unchanged;
the flag is for the hand-off file.

**Probabilities for off-policy evaluation.** Two columns answer two questions:

* `explore_probability` is `P(explored | candidate)`: the explore share for a candidate, 0 otherwise.
  It describes the explore draw alone and is **not** a logging propensity.
* `treatment_probability` is the logging propensity `P(treated | x)` that `ope.evaluate_policy`
  needs, and `treated` is the logged action (selected and not held out, or explored). With `h` the
  effective holdout fraction and `e` the explore share it is, before either draw: `1 − h` for an
  eligible selected row; `(1 − h) × e` for an eligible, non-selected row that is not a predicted
  sleeping dog; 0 for a suppressed row, a sleeping dog, and a non-selected row when there is no
  explore slice. Held-out rows keep their ex-ante value and are logged untreated.

Only rows strictly inside `(0, 1)` say anything about both actions: `engine.holdout.assign.ope_rows`
keeps exactly those, and `evaluate_policy(t=treated, propensity=treatment_probability, ...)` accepts
them as they are. The whole table, unfiltered, is refused - by design.

**What a run writes.** When the service is engaged (a persistent scope or an explore slice), the
scoring run writes `holdout_assignment.parquet` - the key column(s), `holdout_member`, `explore`,
`explore_probability`, `treated` and `treatment_probability` for **every** row, suppressed rows
included - and `holdout_assignment.json` (the run's `HoldoutSpec`: scope, fraction, salt fingerprint,
epoch, and the counts). The parquet file is a row-level artefact (`configs/privacy.yaml`,
`engine/privacy/layout.py`): retention deletes it and erasure rewrites it like `scores.parquet`.
Under the default configuration (`scope: run`, no explore slice) nothing is engaged: the run is byte
for byte what it was and writes neither file, because a default scoring run's files are pinned to
`SCORE_ARTEFACTS` (DEC-1302 (e)). A reader that needs the table for a default run - the hand-off
builder - derives it from `scores.*` with `engine.holdout.assign.assignment_frame(..., active=None,
explore_fraction=0)`: `holdout_member` is the run's `control_group`, nobody is explored, and
`treatment_probability` is `1 − control_group_fraction` on eligible selected rows.

### Several offers against one shared control (Plan J M100, DEC-1310)

A campaign that tried two offers and held a random group back can be learned from in one run. Name
the values of the treatment column, the held-back group's first:

```yaml
uplift:
  treatment_column: offer
  treatment_levels: [none, offer_a, offer_b]   # control first; empty (the default) is one 0/1 treatment
```

It is not agent-editable and may be set per run (`overrides.uplift.treatment_levels`), like the
treatment column. A cell matches a level by one spelling (`1`, `1.0`, `"1.0"` and `" 1 "` are one
level, and YAML levels `[0.0, 1.0, 2.0]` read as `0`, `1`, `2`; text is compared without case). With the setting empty nothing changes: every artefact of a default run is
byte for byte what it was (`tests/integration/decide/test_m100_binary_identity.py`), and the dump of
the configuration in `run_config.json` does not even mention it.

**The rule DEC-668 recorded.** Every existing field keeps its meaning as **the first treatment against
the control** (`offer_a` above): `treated_rows`, `control_rows`, the rates, `p_treated`, `uplift`, the
model card's `propensity`, `base_rate` and `training_rows`, the hold-out file, the contact list. Every report that has arms gains `arms`, one `ArmSummary` per
treatment against the same control, in the configured order; `arms[0]` repeats the report's own
fields. A binary run's reports carry no `arms` key at all.

| Step | What changes with several levels |
|---|---|
| Checks | `TREATMENT_NOT_BINARY` means "a value outside the configured levels" and lists them. `TREATMENT_ARM_TOO_SMALL` is checked for the control and every offer (by name). The randomness check runs once per offer, on its customers and the control's; one offer that was targeted makes the run not causal, and its `TREATMENT_NOT_RANDOM` names the offer. `uplift_validation.json` lists each offer's counts and randomness score in `arms`. |
| Split | One hold-out of every row, stratified on (level, outcome), so the shared control is on one side for every offer. |
| Learning | One model per offer against the shared control: a T- or X-learner per offer, or one S-learner with the offer as a feature (one 0/1 column per offer). With the T- or X-learner on LightGBM, the first offer's model is exactly the binary learner on its rows; the S-learner is one model of every arm, and AutoGluon's time budget is shared across the offers (`engine/uplift/learners.py` `MultiArmUpliftModel`). |
| Evaluation | `uplift_evaluation.json` `arms`: each offer's effect (the bootstrap within each arm), AUUC and Qini, measured on its own hold-out customers and the control's. `segments.json` and `policy_recommendation.json` `arms`: the segments by each offer's uplift, and what the run's policy would do with each offer on its own. |
| Value of choosing | `arm_policy_value.json` (served by `GET /runs/{id}/uplift/arm_policy_value.json`): what giving each customer the offer with the highest predicted uplift × value (or no offer) is worth per customer on the hold-out, against the first offer alone, by inverse probability weighting and a paired bootstrap within each arm. The comparison is before costs (`costs_included: false`); `offer_costs` lists each offer's offer and contact cost as a scoring run's choice prices it (its catalogue action's, else the value settings or the run's cost per contact). |
| Champion | Never promoted (`MULTI_ARM_PROMOTION_REFUSED`), by the run or on the Models page (`POST /models/{id}/promote` answers 409): the champion rule compares one treatment's AUUC and is frozen. The guard reads the model card, else the run's `run_config.json`; an uplift version whose card and run configuration both cannot be read is refused too, never let through. Score with the model by naming its version. |
| Scoring | `scores.csv` is unchanged: the first offer's contact list. `scores.parquet` adds `uplift_arm_<k>` and `p_treated_arm_<k>` for every offer `k` (1 = the first), after the same columns. |
| Measurement | `measure_campaign(..., arm_column=..., arms=..., control_level=...)` measures each offer against the shared control with the unchanged Newcombe interval; `arms` (the configured offers) and `control_level` are required, so the report's own fields are the first configured offer's whatever the file's row order, and `arms` lists every offer. |

**Choosing the offer per customer.** `engine/decide/offer_choice.py` is the choice, as a pure function:
the eligible offer, not one the customer is a sleeping dog for, with the highest M97 net value
(`arm_net_values`: uplift × value × margin × horizon − offer cost × p_treated − contact cost, with each
offer's own costs, priced the same way with or without `value_column`; an offer cost needs `p_treated`),
or no offer when none pays for itself; the runner-up offer's net value is kept. Under a total budget,
customers keep their best offer and are taken by net value per rupee until the budget is spent.

**In the scoring run (M100 part B, DEC-1310 (q) on).** A scoring run of a model of several offers makes
that choice after the actions stage (`engine/decide/offer_run.py`, installed in the Plan J block of
`engine/pipeline.py` after M99's contactability), once suppression, the control group and per-channel
contactability are known:

```yaml
uplift:
  treatment_levels: [none, offer_a, offer_b]
  policy:
    value_per_conversion: 1000          # or value_column; with neither, no offer is chosen
    arm_action_ids: {offer_a: offer_a_sms, offer_b: offer_b_email}   # catalogue actions; optional
    total_budget: 50000                 # rupees, contact and offer costs together; optional
```

* **Costs per offer.** An offer mapped to a catalogue action (`arm_action_ids`, checked at config load
  like `treat_action_id`: `CATALOGUE_ACTION_UNKNOWN`; a key must be an offer of `treatment_levels`, never
  the control) is priced with that action's offer cost (× the customer's `p_treated_arm_k`) and contact
  cost, read from the run's `catalogue_stamp.json` - the catalogue the use case was checked against. The
  catalogue's contact cost wins over `uplift.policy.cost_per_contact` for that offer. An offer with no
  action is priced as M97 prices the first offer's list (`configs/pilot/value.yaml` on the value path,
  else `cost_per_contact`), with the costs the run's actions stage read from that file - read once per
  run, so an edit during the run cannot price the choice differently from the first offer's list.
* **Eligibility per offer.** Not suppressed, not held back as control (the persistent holdout included),
  and contactable on at least one of the offer's planned channels (the action's, else the configured
  channels), from `channel_contactability.parquet` joined on every key column. A customer the file does
  not cover, and every customer of a run with no channels configured, is eligible as before. A planned
  channel the use case does not configure is open only to a customer whose consent covers every channel
  (`all_channel_consent`, `docs/DECIDE.md` section 14): one who consented on SMS alone never gets an offer
  on push.
* **Sleeping dogs per offer.** An offer whose predicted effect is at or below the model's sleeping-dog cut
  is never given to that customer; a customer who is a sleeping dog for every offer gets none.
* **Budget.** `total_budget` (rupees) and `budget_contacts` (a count) both hold; customers are taken by net
  value per rupee, and an offer is never switched to a cheaper one to fit.
* **What it writes.** `offer_choice.parquet` (row-level, Analyst-only: the offer given, its channel - the
  first planned channel of that offer the customer is contactable on - its net value and its total
  expected cost `offer_total_cost` (contact + offer cost x `p_treated`, rupees), the runner-up and its net
  value, the reason, and the offer an explored customer would be given, `explore_*`) and `offer_choice.json` (counts and rupees per offer, the
  budget and the spend; served with the uplift reports and shown on the Output page). `scores.csv` and
  `scores.parquet` are unchanged; a run of one offer writes neither file.
* **The treat list** reads the choice: `offer` is the offer's catalogue label (else its level), `channel`
  its channel, `net_value` its net value; `runner_up_offer` and `runner_up_net_value` the next-best offer
  the customer could be given; a customer who could be contacted but got no offer has `treat = 0` and a
  plain `offer_reason`. An explored customer (M92) left without an offer is given the best offer they
  could be given - the one the choice preferred when the budget dropped them, else the best eligible
  offer that is not a sleeping dog for them - outside the budget, as the explore slice is outside the
  policy; the treat list summary counts and prices those offers apart (`explore_offer_counts`,
  `explore_cost`, `explore_note`). With no value set the run says so
  (`offer_choice.json` `chosen: false`, `OFFER_CHOICE_NOT_MADE`) and the treat list stays the first
  offer's.

The Model page shows a card per offer (its effect with the likely range, both response rates) and
whether choosing per customer does better than the first offer alone; every number is the server's.

---

## 15. Limits

* **Several offers are learned and measured, not yet acted on.** A model of several offers (section
  14, Plan J M100) reports every offer and writes each offer's predicted uplift, but the contact list
  is still the first offer's, the offer chosen per customer reaches the treat list only with M100's
  second part, and such a model is never champion. Offer costs per arm wait for M99's catalogue.
* **Binary outcome only for the model.** Converted or not. Revenue or other continuous outcomes are not
  modelled: an uplift model learns from a yes/no outcome, and step 4 does not offer "Learn who to contact
  next time" on a campaign measured on an amount.
* **Amounts are measured, not modelled (Plan J M102, DEC-1312).** A campaign's result can be measured on an
  amount such as revenue (`outcome_kind: continuous`): the difference in the two groups' averages with a
  **Welch** interval (unequal variances, Student's t), and - when the registered test plan named an amount
  from before the campaign - the **CUPED** adjusted difference, `adjusted_lift` / `adjusted_interval`, with the
  `variance_reduction` it bought (about rho²). The covariate must be dated a day before each customer's treatment
  day or earlier (`COVARIATE_NOT_BEFORE_CAMPAIGN` otherwise) and registered in advance (`TEST_PLAN_CHANGED` otherwise).
  Long-tailed revenue is never capped; below Kohavi et al.'s size (355 g² customers per arm) the report
  carries `OUTCOME_SKEWED`: the range may be too narrow. Both intervals' coverage is tested nightly, on
  normal and on zero-inflated lognormal revenue. On a yes/no outcome nothing changes: every M102 field is
  absent. Several offers are still measured on a yes/no outcome only. Details: [`docs/DECIDE.md`](DECIDE.md)
  section 16.
* **Two-column keys: uploads train, datasets score.** Since M53 `POST /uplift/runs` takes a
  two-column key (customer + snapshot date) on an uploaded file, and uplift scoring and campaign
  results read both columns. Phase 1's `POST /runs` still takes a composite key only with a built
  dataset (DEC-083), so a scoring file with several snapshots per customer is scored through a
  dataset; a scoring file with one row per customer is scored with the customer column alone.
  `POST /uplift/runs` itself still takes an `upload_id`, not a `dataset_id`.
* **Randomised data only for causal claims.** Nothing here corrects a targeted campaign. It is
  labelled not causal instead.
* **Criteo was not run here.** The Criteo Uplift use case is configured for this problem type
  ([`library/criteo-uplift/`](../library/criteo-uplift/)), but the dataset could not be downloaded
  from this environment (re-tested 2026-09-23). Plan B's acceptance run on it has therefore **not**
  been done, and no Criteo number exists anywhere in the repository. The dataset is also
  non-commercial (CC BY-NC-SA 4.0).
* **The 95% range is an approximation, and it is tested.** The lift's interval is the Newcombe hybrid
  score interval (section 9), an approximation that is close to, not exactly, 95%. Plan J M95 measures how
  close: `make test-statistical` simulates campaigns with a known effect and checks that the range holds it
  within four Monte Carlo standard errors of 95%, at 2%, 5% and 20% base rates, that a campaign with no effect
  is called effective about 5% of the time, and that excluding immature rows leaves the lift unbiased
  ([`docs/DECIDE.md`](DECIDE.md) section 9). The test covers the randomised comparison of two arms; it says
  nothing about a campaign that had no random control group, which is labelled not causal.
* **Expected incremental conversions on a scoring run** borrow the training hold-out's observed
  uplift at the same ranking depth. That is an approximation when suppression is related to uplift.
* **Scoring cost.** Every scored row is explained with TreeSHAP, as in Phase 1. On a very large file
  this takes minutes.
* **Drift** is measured on scoring runs (`uplift_drift.json`, section 8), not by Phase 4b's
  scheduled drift check: that check replays Phase 1's `prepare.json`, which an uplift model does not
  have, so for an uplift champion it reports drift as not measured.
* **Training runs write no `prepare.json`.** The feature spec is on the model card instead. Phase 1's
  Data page therefore shows no prepare report for an uplift run.

---

## 16. Cross-checks against scikit-uplift and causalml

The metrics were checked against both libraries (scikit-uplift 0.5.1, causalml 0.17.0) as well as
against an independent loop-based implementation. Neither library is a dependency. The brute-force
cross-check always runs; `tests/unit/uplift/test_metrics.py` also has
`test_point_metrics_match_scikit_uplift_after_conversion` and
`test_point_metrics_match_causalml_after_conversion`, which run when the libraries are installed and
skip otherwise. The libraries report the same quantities in other units and with other edge rules,
so a comparison needs these conversions (`n` is the number of hold-out rows, `ATE` the treated rate
minus the control rate):

| Engine | scikit-uplift | causalml |
|---|---|---|
| `qini_coefficient` | `(auc(x, y) − auc([0, n], [0, y[-1]])) / n²` with `x, y = qini_curve(y_true, uplift, treatment)` and scikit-learn's `auc` | `qini_score(df, normalize=False) × (n + 1) / n²` |
| `auuc` | the same expression on `uplift_curve(...)` | `(A·(n + 1)/n − ATE/2)/n − ATE/2` with `A = auuc_score(df, normalize=False)` |
| `uplift_at` for share `f` | `uplift_at_k(..., strategy="overall", k=ceil(f·n))`, passing the count: a float `k` is rounded **down** | none |
| deciles | `uplift_by_percentile(..., strategy="overall", bins=10)` (needs `n > 10`) | none |

Where they differ by design, not by error:

* **Ties.** The engine keeps tied rows in input order (a stable sort on `−pred`, DEC-613).
  scikit-uplift reverses them and draws a straight chord across each tie block; causalml's default
  sort does not guarantee any order. On tied predictions the numbers therefore differ slightly.
* **A top share with one arm empty.** The engine's uplift curve is 0 there (a rate of nobody is not
  zero, and not the other arm's rate). scikit-uplift uses the non-empty arm's rate, and causalml
  produces a NaN it then interpolates. Any prefix of the ranking with only treated or only control
  rows that holds a conversion makes the AUUCs differ.
* **Normalisation.** scikit-uplift's `qini_auc_score` and `uplift_auc_score`, and causalml's default
  `normalize=True`, divide by a perfect or final value; the engine's areas are not normalised.

With distinct predictions, and both arms present in the top two rows with no conversion before them,
all four conversions are exact to 1e-12; that is the data the two tests use.

---

## 17. API errors

The uplift routes answer errors in Phase 1's envelope, `{"detail": {"code", "message", "path"}}`
(DEC-678). The message says what to do; this table is the place to look a code up.

| Code | Status | Route | Meaning | What to do |
|---|---|---|---|---|
| `VALIDATION_FAILED` | 409 | `POST /uplift/runs` | Phase 1's own checks block the file. The body also carries `validation` and `uplift_validation`. | Fix the findings in `validation`, or acknowledge the ones that allow it, and send again. |
| `UPLIFT_VALIDATION_FAILED` | 409 | `POST /uplift/runs` | Phase 1's checks pass, but an uplift check blocks (the message names the codes; see section 3). | Fix the file, or acknowledge `TREATMENT_NOT_RANDOM` to train a model labelled not causal. |
| `UPLOAD_MODE_MISMATCH` | 409 | `POST /uplift/runs` | The file was uploaded for scoring. | Upload it again with `mode=train`. |
| `OVERRIDE_UNKNOWN_PATH` | 422 | `POST /uplift/runs` | An override names a setting a run may not change, such as `uplift.randomness_auc_max` (DEC-607). | Change it in the use-case file, or leave it out. |
| `ARTEFACT_UNKNOWN` | 404 | `GET /runs/{id}/uplift/{name}` (and, since M53, `GET /runs/{id}/artefacts/{name}`, which whitelists the same names) | `name` is not an uplift artefact. | Use one of the names in section 5 to 10 (`uplift_evaluation.json`, `qini_curve.json`, …). |
| `ARTEFACT_NOT_FOUND` | 404 | `GET /runs/{id}/uplift/{name}` | The run has not written that file (a scoring run writes no `uplift_evaluation.json`, and campaign results or OPE exist only once asked for). | Ask the run that writes it, or create it first. |
| `RUN_NOT_SCORED` | 409 | `POST /runs/{id}/campaign-results` | The run is not a finished scoring run, or has no scores file. | Measure a campaign against the scoring run that chose its customers. |
| `CAMPAIGN_RESULTS_INVALID` | 422 | `POST /runs/{id}/campaign-results` | The outcomes file or the request cannot be measured: `bands` on an uplift run, an outcome column that is missing or not 0/1, duplicate keys, and so on (the message names it). | Correct the file or the request as the message says. |
| `UPLIFT_REQUIRES_UPLIFT_ROUTE` | 422 | `POST /runs` | A training run whose problem type is uplift - by override or by the use case's own configuration - was sent to Phase 1's route, which cannot run the uplift checks first (M53). | Start it with `POST /uplift/runs`. Scoring an uplift model stays on `POST /runs`. |
| `CAMPAIGN_RESULTS_NOT_FOUND` | 404 | `GET /runs/{id}/campaign-results` | No campaign has been measured for this run yet. | `POST` the outcomes file first. |
| `RUN_NOT_UPLIFT` | 409 | `POST /runs/{id}/uplift/ope` | The run is not a finished uplift training run, so it has no hold-out to replay. | Use an uplift training run. |
| `RUN_NOT_UPLIFT` | 409 | `GET /runs/{id}/uplift/profit-curve` | The run is not a finished uplift run, or made no targeting recommendation, or (a training run) has no hold-out. | Use a finished uplift training or scoring run. |
| `RUN_NOT_SCORED` | 409 | `GET /runs/{id}/uplift/profit-curve` | A scoring run without its scores file. | Score the customers again. |
| `PROFIT_CURVE_UNAVAILABLE` | 409 | `GET /runs/{id}/uplift/profit-curve` | The saved scores no longer reproduce the run's recommendation, or lack the columns the curve needs; or (Plan J M96) the list was ranked by the approved propensity model because the uplift model does not beat risk ranking. | Score the customers again; a list ranked by the propensity model has no uplift budget curve. |
| `ARTEFACT_NOT_FOUND` | 404 | `GET /runs/{id}/risk-comparison` | The run computed no equal-budget comparison (section 12, M96). | Train with `uplift.evidence.risk_comparison` on and the LightGBM base model. |
| `PROFIT_CURVE_QUERY_INVALID` | 422 | `GET /runs/{id}/uplift/profit-curve` | The query names the value of one conversion twice: `value` (Plan J M97) is another name for `value_per_conversion`. | Give one of the two. |
| `MULTI_ARM_PROMOTION_REFUSED` | 409 | `POST /models/{id}/promote` (and the reason a training run keeps the model a candidate) | The model chooses between several offers (Plan J M100), and the champion rule compares models of one offer only. | Keep it a candidate and score with it by naming its version. |
| `OPE_INVALID` | 422 | `POST /runs/{id}/uplift/ope` | The rule or the logged data cannot be evaluated (the message says why). | Correct the rule as the message says. |
| `MEASURE_NOT_OFFERED` | 409 | `POST /runs/{id}/measure`, `.../measure/learn` | The use case does not contact customers, or holds nobody back (section 9, step 4). | Nothing to measure; the Campaign results route still answers for any scoring run. |
| `MEASURE_INVALID` | 422 | `POST /runs/{id}/measure`, `.../measure/learn` | The outcomes file has no customer id column, only the id, or several columns and none is the use case's outcome. | Keep the customer id and one outcome column, or name it with `outcome_column`. |
| `MEASURE_NOT_READY` | 409 | `POST /runs/{id}/measure/learn` | The campaign was not measured yet, its window is still open, or a group is below the uplift floors (the message gives the counts). | Measure it, wait for the window, or run a larger campaign. |
| `HOLDOUT_SALT_MISSING` | the run fails at ingest; 503 on `PUT /holdout` | any scoring run of a use case with a persistent holdout; `PUT /holdout` | `MARKETING_AI_HOLDOUT_SALT` is not set (section 14). | Set the salt (at least 16 characters, kept for as long as the holdout runs), or set `actions.holdout.scope` back to `run`. |
| `HOLDOUT_SALT_CHANGED` | the run fails at ingest; 409 on `PUT /holdout` | any scoring run with a persistent holdout; `PUT /holdout` without `rotate_salt` | The configured salt is not the one the holdout was drawn with. | Set the original salt again, or have an Admin adopt the new one with `PUT /holdout` and `rotate_salt: true` (a new epoch of every holdout). |
| `HOLDOUT_FRACTION_LOWERED` | the run fails at ingest | any scoring run with a persistent holdout | The use case asks for a smaller holdout than its current epoch has used. | Set the share back, or have an Admin start a new epoch at the lower share (`PUT /holdout`). |
| `HOLDOUT_FRACTION_MISMATCH` | the run fails at ingest; 409 on `PUT /holdout` | any scoring run on the universal holdout; `PUT /holdout` with scope `universal` | The universal holdout is one share for every use case on it, and this use case (or the requested epoch) asks for a different one. | Set `actions.holdout.fraction` of every universal use case to the same share; to change it, set them all and then have an Admin start a new epoch at it. |
| `HOLDOUT_SALT_UNCHANGED` | 409 | `PUT /holdout` with `rotate_salt: true` | The configured salt is the one already recorded (or the first one), so a rotation would reshuffle nobody. | Set the new `MARKETING_AI_HOLDOUT_SALT` first, or start the epoch without `rotate_salt`. |
| `HOLDOUT_SCOPE_RESERVED` | the run fails at ingest; 422 on `PUT /holdout` | a use case named `universal` or `explore` with `scope: use_case` | The id is reserved by the holdout itself. | Rename the use case, or use scope `universal` or `run`. |

Phase 1's shared codes (`RUN_NOT_FOUND`, `UPLOAD_NOT_FOUND`, the ingest codes and the configuration
codes) keep their Phase 1 meaning on these routes.
