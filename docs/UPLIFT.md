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

One file, one row per customer, recording one past campaign (plan B §3):

| Column | Required | What it must be | Checked by |
|---|---|---|---|
| Primary key | yes | One column that identifies the customer, unique per row | Phase 1 validation |
| Treatment | yes | `1` = received the action, `0` = held out. Only 0/1 (also `true`/`false`, `"0"`/`"1"`); no blanks. **Randomly assigned.** | `TREATMENT_COLUMN_MISSING`, `TREATMENT_NOT_BINARY`, `TREATMENT_NOT_RANDOM` |
| Outcome | yes | Binary (converted or not), measured **after** the treatment | Phase 1 validation; the uplift engine refuses a non-binary outcome |
| Features | yes, at least one | Measured **before** the treatment date (the point-in-time rule) | `FEATURE_AFTER_TREATMENT` when dates are present |
| `treatment_date` | optional | When each customer was treated. Needed for the maturity and point-in-time checks. | `OUTCOME_WINDOW_IMMATURE`, `FEATURE_AFTER_TREATMENT` |
| `campaign_id` | optional | Which campaign a row belongs to. Never used as a feature. | none |

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

## 3. The six checks

Phase 1's validation runs first, unchanged. Then six uplift checks run on the same upload
(`engine/uplift/checks.py`, plan B §4). Both reports are written (`validation.json` and
`uplift_validation.json`), and a run needs both to pass. `POST /uplift/runs` runs them **before**
the run exists and answers `409` with both reports when something blocks. The run's own validate
stage runs them again.

| Code | Severity | What it means for you |
|---|---|---|
| `TREATMENT_COLUMN_MISSING` | error | No configured or hinted treatment column is in the file: there is no experiment to learn from. |
| `TREATMENT_NOT_BINARY` | error | Some treatment values are not 0/1, blanks included. Those rows are in neither arm, or in some third arm. The count is reported. |
| `TREATMENT_ARM_TOO_SMALL` | error | The treated or the control arm has fewer than `min_arm_rows` rows (default 1,000) or fewer than `min_arm_positives` conversions (default 50). Counted after immature rows are dropped. |
| `TREATMENT_NOT_RANDOM` | error, **can be acknowledged** | The features predict who was treated, so the campaign was targeted, not randomised. |
| `OUTCOME_WINDOW_IMMATURE` | warning | Some customers were treated too recently for their outcome to be final. Those rows are dropped and counted, never guessed. |
| `FEATURE_AFTER_TREATMENT` | error | A date-like column has values later than the row's treatment date, so the "before" snapshot already contains the campaign's effect. |

A check that cannot run because an earlier one failed is skipped, not failed twice. With no
treatment column there are no arms to count.

**What each finding says.** This table is the normative list of the six codes (DEC-673): plan B asks
for them in Phase 1's check registry and `docs/DATA_CONTRACT.md`, which Phase 3b does not own; the
request to list them there is in `docs/CROSS_BRANCH_REQUESTS.md`. Every finding carries a code, a
message and a suggestion (and a `details` object with the counts); the texts below are
`engine/uplift/checks.py`'s, with `<…>` for the values filled in.

| Code | Message | Suggestion |
|---|---|---|
| `TREATMENT_COLUMN_MISSING` (configured column absent) | The treatment column '`<column>`' is not in this file. | Choose the column that records who received the campaign in Setup, or upload the file that contains it. |
| `TREATMENT_COLUMN_MISSING` (nothing configured, no hint found) | No column in this file says which customers received the campaign. Looked for `<hints>`. | Add a column with 1 for customers who were contacted and 0 for the randomly held-out customers, or choose the column in Setup. |
| `TREATMENT_NOT_BINARY` | '`<column>`' should be 1 for treated customers and 0 for held-out customers, but `<n>` of `<rows>` rows (`<share>`) are blank or hold another value. | Record every customer as 1 (treated) or 0 (held out); true and false work too. Remove customers whose treatment is unknown from the file. |
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

---

## 4. What a training run does

Training happens on a random **hold-out** split. By default 30% of the rows (`uplift.test_fraction`)
are kept aside, stratified on treatment and outcome together. Every number on the Model page is
measured on those rows, which the model never saw.

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

---

## 8. Scoring: the treat list

A scoring run of an uplift model goes through Phase 1's own `POST /runs` with `mode: score`
(section 13). It replays the model card's features, predicts, explains every row and then applies
the actions (`engine/uplift/actions.py`):

1. **Suppression** (consent, opt-out, recent contact) and the **control group** come from Phase 1's
   `engine.stages.actions.apply_actions`, unchanged. The same run id holds out exactly the same
   customers as a Phase 1 run would.
2. Persuadables chosen by the policy get `Treat`.
3. Persuadables not chosen get `Don't treat (below cost)` or `Don't treat (over budget)`.
4. Everyone else gets their segment's action. The `band` column holds the segment label.

`scores.csv` columns: the key, `uplift`, `p_treated`, `p_control`, `segment`, `band`, `action`,
`reason_1…n`, `suppressed_reason`, `control_group`, `intended_treatment`. The Output page's
**Download treat list** is this file.

**`intended_treatment`** marks the customers the policy treats **plus** the control customers it
*would* have treated had they not been held out: persuadables ranked at or above the last chosen
customer. The campaign-results comparison (section 9) is made inside this set, so both sides pass
the same rule. Customers with exactly the same predicted uplift are ordered by a per-customer hash
seeded by the run (DEC-606). A budget that cuts through a block of ties therefore takes a random part
of it, not the first rows of the file.

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

---

## 13. How to run it

### In the UI

1. From the Overview, follow **Uplift modelling ›** (or **Uplift for this use case ›** on any
   use-case screen). Both links are added by the uplift module. The screens live at `#/uplift` and
   `#/uplift/<use case>`.
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
printed.

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
| `segments.*` (the three cuts) | 0.02; −0.01; base rate | yes | **yes** |
| `policy.*` (budget, cost, value) | none | yes | **yes** |

The control-group fraction and suppression rules live in `actions:`, are shared with Phase 1, and are
not agent-editable. `engine.uplift.config.uplift_agent_editable_paths()` returns this map for Phase 5.

---

## 15. Limits

* **Binary treatment only.** One action versus no action. Several offers (multi-treatment) are not
  supported. The artefacts are designed so they can be added without renaming anything: DEC-668
  describes the extension path.
* **Binary outcome only.** Converted or not. Revenue or other continuous outcomes are not modelled.
* **Single-column primary key.** Plan B's contract allows one row per customer per snapshot using
  Plan A M34's two-column keys, which are not on `main`. Uplift runs and campaign results use a
  one-column key until they land.
* **Randomised data only for causal claims.** Nothing here corrects a targeted campaign. It is
  labelled not causal instead.
* **Criteo was not run here.** The Criteo Uplift use case is configured for this problem type
  ([`library/criteo-uplift/`](../library/criteo-uplift/)), but the dataset could not be downloaded
  from this environment (re-tested 2026-09-23). Plan B's acceptance run on it has therefore **not**
  been done, and no Criteo number exists anywhere in the repository. The dataset is also
  non-commercial (CC BY-NC-SA 4.0).
* **Expected incremental conversions on a scoring run** borrow the training hold-out's observed
  uplift at the same ranking depth. That is an approximation when suppression is related to uplift.
* **Scoring cost.** Every scored row is explained with TreeSHAP, as in Phase 1. On a very large file
  this takes minutes.
* **No drift monitoring** for uplift models yet: no drift baseline is stored and scoring runs write
  no `drift.json`.
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
| `ARTEFACT_UNKNOWN` | 404 | `GET /runs/{id}/uplift/{name}` | `name` is not an uplift artefact. | Use one of the names in section 5 to 10 (`uplift_evaluation.json`, `qini_curve.json`, …). |
| `ARTEFACT_NOT_FOUND` | 404 | `GET /runs/{id}/uplift/{name}` | The run has not written that file (a scoring run writes no `uplift_evaluation.json`, and campaign results or OPE exist only once asked for). | Ask the run that writes it, or create it first. |
| `RUN_NOT_SCORED` | 409 | `POST /runs/{id}/campaign-results` | The run is not a finished scoring run, or has no scores file. | Measure a campaign against the scoring run that chose its customers. |
| `CAMPAIGN_RESULTS_INVALID` | 422 | `POST /runs/{id}/campaign-results` | The outcomes file or the request cannot be measured: `bands` on an uplift run, an outcome column that is missing or not 0/1, duplicate keys, and so on (the message names it). | Correct the file or the request as the message says. |
| `COMPOSITE_KEY_NOT_SUPPORTED` | 422 | `POST /runs/{id}/campaign-results` | The run's primary key has several columns (section 15). | Combine them into one column. |
| `CAMPAIGN_RESULTS_NOT_FOUND` | 404 | `GET /runs/{id}/campaign-results` | No campaign has been measured for this run yet. | `POST` the outcomes file first. |
| `RUN_NOT_UPLIFT` | 409 | `POST /runs/{id}/uplift/ope` | The run is not a finished uplift training run, so it has no hold-out to replay. | Use an uplift training run. |
| `OPE_INVALID` | 422 | `POST /runs/{id}/uplift/ope` | The rule or the logged data cannot be evaluated (the message says why). | Correct the rule as the message says. |

Phase 1's shared codes (`RUN_NOT_FOUND`, `UPLOAD_NOT_FOUND`, the ingest codes and the configuration
codes) keep their Phase 1 meaning on these routes.
