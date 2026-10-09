# Plan J — deciding better and proving better

**Owner:** Plan J (`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`, milestones M90–M111)
**Decisions:** DEC-1300 … DEC-1399 in [`DECISIONS.md`](DECISIONS.md)
**Status:** created by M90 as a skeleton; each milestone fills in its own section in the same change as the code
it describes, and marks it written in §3's table. A section §3 lists as "not yet written" describes nothing yet.

## 1. Purpose

Plan J makes the product good at the two things a marketer pays for before any integration: **deciding** who to
contact with which offer on which channel, and **proving** what the campaign earned. This page is where a reader
learns how, in plain words and with the numbers each result comes from. Until integration, the hand-off is a file
the user downloads (the treat list).

## 2. Where Plan J's code lives

`engine/holdout/`, `engine/measurement/`, `engine/decide/` (error codes in `codes.py`), `api/routes/{campaigns,holdout,measurement}.py`,
`ui/modules/decide/`, `configs/decide/`, `tests/**/{holdout,measurement,decide}/`, `tests/statistical/`. See `PARALLEL_WORK_PROTOCOL.md` §3.

## 3. Sections to be filled by later milestones

| § | Section | Milestone | State |
|---|---|---|---|
| 5 | Defects fixed and the regression tests that pin them | M91 | not yet written |
| 6 | The persistent holdout and the explore slice | M92 | not yet written |
| 7 | Planning a test and defining the outcome | M93 | not yet written |
| 8 | The campaign record, the measurement path and the test plan | M94 | written (below) |
| 9 | How we know our intervals are honest; the synthetic quarantine | M95 | written (below) |
| 10 | Uplift stability, calibration and the beats-risk check | M96 | written (below) |
| 11 | Ranking by net value | M97 | not yet written |
| 12 | The treat list and its reasons | M98 | written (below) |
| 13 | The offer and channel catalogue; channel-aware consent | M99 | written (below) |
| 14 | Choosing the offer (multi-treatment uplift) | M100 | written (below) |
| 15 | One action per customer across use cases | M101 | written (below) |
| 16 | Revenue outcomes and CUPED | M102 | written (below) |
| 17 | Auditing a campaign another tool ran; the programme readout | M103 | not yet written |
| 18 | The Value Proof Pack | M104 | not yet written |
| 19 | Warnings and proven value to date | M105 | not yet written |
| 20 | Learning from the last cycle | M106 | not yet written |
| 21 | The monthly loop (read-only) | M107 | not yet written |
| 22 | Cost before each run | M108 | not yet written |
| 23 | Validation on real public randomised data | M110 | not yet written |
| 24 | The manager demo | M111 | not yet written |

## 4. Running the statistical suite

`make test-statistical` runs `tests/statistical/` (marker `statistical`). `make test` and `make test-all` never
collect it: `tests/statistical/conftest.py` ignores the directory's test files unless `MARKETING_AI_STATISTICAL=1`
(DEC-1300 (e)). The nightly workflow runs it. The tests use fixed seeds and bands of four Monte Carlo standard errors.
This paragraph is the only section M90 writes beyond the skeleton.

## 8. The campaign record, the measurement path and the test plan (M94, DEC-1304)

**A campaign is the list going out.** A scoring run makes a list; `POST /campaigns {run_id,
treatment_start?, bands?, outcome_window_days?, name?}` (Analyst) records that it was sent, and when.
The record (`engine.measurement.campaign.Campaign`, kind `scored`; `external` is reserved for M103)
lives in the platform database (`campaign`, alembic `0006`) and its files under `campaigns/<id>/`:

| File | What it holds | Privacy |
|---|---|---|
| `campaign.json` | a copy of the record: ids, counts, dates, column names | none (no customer id) |
| `assignment.parquet` | per scored customer: the key, `arm` (treated, holdout, suppressed), `intended`, `band`, `segment` and `explore` when the run has them | row-level: erasure, retention |
| `outcomes.parquet` | the key, the outcome and the optional treatment date, copied from the upload | row-level: erasure, retention |
| `incrementality_report.json` | the measured report | aggregate |
| `test_plan.json`, `test_plan_v<n>.json` | the plan in force, and every version | aggregate |

`intended` is the population both arms are compared inside: an uplift run's `intended_treatment` (for a run
that chose the offer per customer, the customers its policy would give some offer to if nobody were held back,
`Campaign.intended_source = "offer_choice"`, section 14), a propensity run's treat bands, or every eligible
customer (intent to treat). The **treatment start** is
the day the campaign went out, entered by the person (never before the run finished), else the run's
finish time; maturity dates count from it.

**One measurement path.** `engine.measurement.measure.measure_campaign` is the only function that
turns an assignment and an outcomes file into an `IncrementalityReport`: a wrapper over
`engine.uplift.incrementality.measure_incrementality` (the numbers) and `engine.uplift.measure
.campaign_verdict` (the plain verdict, through `campaign_verdict_for`). `POST
/runs/{id}/campaign-results` calls it with the run's scores and no plan, so its reports are what they
were; `POST /campaigns/{id}/measure` calls it with the assignment and the plan in force. `as_of`
always comes from the caller; the route defaults it to now and refuses a later one (`422
CAMPAIGN_INVALID`, path `as_of`), since a moment that has not happened would call open outcome windows
closed and an early look final. A campaign is never measured on part of its customers: while any
outcome window is open the answer is `409 CAMPAIGN_NOT_MATURED` with `results_available_on`, and
nothing is stored.

**The registered test plan.** `POST /campaigns/{id}/plan` (Analyst) freezes a `TestPlan` - metric,
outcome column and kind, optional covariate, outcome window, analysis date and secondary dates, the
detectable effect and expected rate, and from the assignment the holdout share and the customers in
each arm - with `plan_hash`, the SHA-256 of its content. The audit event's `after_hash` is
`content_hash(plan)` and its details carry `plan_hash`. The same plan again returns the stored one; a
different one is `409 TEST_PLAN_EXISTS`; `POST /campaigns/{id}/plan/amendments {reason, ...}` writes
version n+1 with `amends`, every version kept (`GET /campaigns/{id}/plan`); once a final result has
been read the plan can no longer be amended (`409 TEST_PLAN_INVALID`). When an effect and a rate
are given, the power of the planned arms is computed with M93's `engine.measurement.planner.achieved_power`; below
the plan's power the plan carries `PLAN_UNDERPOWERED`, a warning that never blocks. At measurement any
difference from the plan - holdout share, population, window, outcome column, value or kind,
covariate - is `409 TEST_PLAN_CHANGED`. The covariate defaults to the plan's, so only a different one
named in the body is a change. The population allows for erasure: the planned population less a few
customers (neither arm larger, at most 1 % of it and at least one customer gone) is the planned one,
and beyond that the holdout share is compared within half a point. A read before `analysis_date` is
an **early look**: the report says so (`early_look`), its summary gives only the rates and group sizes
so far and the date the result is read - never the conclusion - and no verdict is given.

**Which way round.** The campaign page judges the outcome exactly as step 4 does: a column found in
the outcomes file is the use case's own outcome (for a churn use case, one to prevent), a column the
person named (`CampaignOutcomes.outcome_named`) is judged by its own name, and the verdict uses the use
case's outcome words.

**The value view** (`engine.pilot.roi.compute_roi`) reads a campaign's final report first - the one
named, else the run's latest - and never prices an early look.

**On screen.** Results lists the campaigns beside the runs (a results list registered through the
router's PLAN-J seam); a campaign's page (`#/campaigns/<id>`) shows its result and the "Plan the test"
card with the server's values only.

**Wired at M94's integration, once M92 and M93 had merged (DEC-1304 (l), (m)).**

* *Plan preview (M93).* `GET /campaigns/{campaign_id}/plan-preview?holdout=&base_rate=` (Viewer) in
  `api/routes/campaigns.py` passes the assignment's `realised_population(...)` (its measured
  population, and its explore share) to M93's `engine.measurement.planner.power_preview` at each
  control-group share asked for, or at a fixed ladder (2 % to 50 %) plus the campaign's own realised
  share when none is asked. The rate is the request's, else the registered plan's, else unknown (every
  smallest change is then null with the planner's sentence); the direction is the use case's aim (`down`
  for an outcome to prevent); value and costs are optional query parameters. The answer is
  `{eligible, n_treat, n_holdout, holdout_fraction, base_rate, base_rate_source, direction, points,
  current_index, basis}`: computed points only, nothing stored. `freeze_plan` takes `achieved_power`
  from `planner.achieved_power` (the same test as `engine.uplift.power.power_of_lift`; they differ only
  by the far tail in very small groups). The card's slider (`ui/modules/decide/plan.js`) is a range input
  over the indices of `points`, starting on `current_index`; moving it repaints the readout only, and a
  rate typed into the form asks the route again (DEC-1204). The stop does not change the plan: the
  campaign's split is already drawn. Tests: `tests/integration/measurement/test_plan_preview.py` and the
  jsdom slider tests in `campaigns.test.mjs`.
* *Holdout epochs (M92).* `POST /campaigns` records the run's holdout - `holdout_scope`,
  `holdout_scope_key`, `holdout_epoch` - from its `holdout_assignment.json`
  (`engine.holdout.assign.run_holdout_spec`; a run that wrote none drew per run, scope `run`). `POST
  /campaigns/{id}/measure` refuses `409 CAMPAIGN_EPOCH_MISMATCH` (audited, nothing stored) when
  `engine.measurement.campaign.epoch_mismatch` finds the campaign's runs in two epochs, the run's epoch
  no longer the recorded one, or the persistent holdout redrawn (a later epoch in the ledger `GET
  /holdout` reads) before the outcomes were all in - the end of the outcome window, or the measurement's
  own moment when there is no window or the dates are per row. A redraw after that is fine. The ledger
  keeps only the current epoch's start, so two or more epochs since are refused: when the first began is
  not known. Tests: `tests/unit/measurement/test_campaign_epochs.py`,
  `tests/integration/measurement/test_campaign_epochs.py`.
* *Explore slice (M92, closed at M95's integration, DEC-1305 (k)).* A run's `scores.*` carries no explore
  flag, so `POST /campaigns` reads the run's `holdout_assignment.parquet` when it wrote one and
  `build_assignment` joins `explore` and `explore_probability` onto `assignment.parquet` by the key (a
  scored customer the file does not list was not explored). The slice is drawn outside the selection, so
  `n_explore` counts it over every eligible customer, not only the measured population (an uplift run's
  intended set and the treat bands never contain it), and the plan preview prices it with
  `planner.cost_of_explore` on that count. Tests: `tests/integration/measurement/test_campaign_explore.py`.

## 9. How we know our intervals are honest

Every campaign result shows a 95% range for the lift. "95%" is a promise: if the same kind of campaign were
run again and again, the range should hold the true effect 95 times in 100. We do not assume that; we test it,
every night, on campaigns whose true effect we know because we made them.

**The simulator.** `engine/measurement/simulate.py` builds a campaign with a chosen conversion rate and a
chosen effect: who is held back, who converts, and when each customer was treated. It returns the same two
tables the product reads (a run's scores and an outcomes file with dates), so the test measures through the
real `measure_incrementality`, not a copy of it. It can make some customers *immature* (their outcome window is
still open), have only some of the contacted customers receive the offer, and let some held-back customers
receive it anyway. One seed is one campaign, exactly, on any machine.

**What is checked**, by `make test-statistical` (`tests/statistical/`, nightly; never part of `make test`):

| Check | Simulations | Must be |
|---|---|---|
| The 95% range holds the true effect, at base rates 2%, 5% and 20% | 2,000 each | 95%, within the band |
| The same when only some contacted customers receive the offer (the effect measured is then the effect of being *assigned*, intent to treat) | 2,000 | 95%, within the band |
| A campaign with no effect is reported as having one (range excludes zero; p < 0.05) | 10,000 | 5%, within the band |
| Leaving immature customers out does not bias the lift; counting them as non-converters does (towards zero, by the amount the model predicts) | 1,000 each | within four standard errors of the mean |
| Achieved power at the planner's number of customers (`n_for_mde`, two cases: 4% to 3% and 10% to 8%), with the planner's own `achieved_power` and the plan preview's `power_preview` agreeing with it | 2,000 | 80%, within the band |

**The band rule.** With `n` simulations and a true rate `p`, the observed share has a Monte Carlo standard error
of `sqrt(p(1-p)/n)`. A check passes when the observed share is within **four** of them: 95% +/- 1.95 points at
2,000 simulations, 5% +/- 0.87 points at 10,000. The band comes from the number of simulations, never from
choosing a width that happens to pass; a check that needs a tighter band runs more simulations. Four, not two,
so that a check that goes red means something and nobody is tempted to loosen the test. Seeds are fixed, so the
suite gives the same answer every night: a correct measurement would fall outside a four-standard-error band for
about 1 seed set in 16,000 per check (about 1 in 2,000 across the suite), so a failure after a code change points
at the change. **What a band detects.** Each 2,000-simulation coverage case sees coverage off by more than about
1.95 points; the false-positive check, at +/- 0.87 points, is the most sensitive guard against an interval that
is too narrow, and a narrowing of less than about a point is below what the suite can see.

**Runtime.** The suite takes about fourteen minutes on one core (measured at M95's integration: 15 tests in 837
seconds). The 10,000-simulation false-positive check takes about three and a half of them and the two power checks
about five and a half, because their populations are the planner's own n (10,602 and 6,426 customers). The other
simulated populations are kept small (1,200 to 4,000 customers) because the number of simulations, not the
population size, sets the band.

**What it does not show.** It shows that the *method* is honest on randomised data with the assumptions
above. It does not show that a particular client's holdout was randomised, that their outcome data is complete,
or that their effect is what the demo's is: those are checked per client by the product's own checks.

### The synthetic quarantine

The demo's churn effect is *planted* in generated data. It must never be read as a result or a
forecast. So a run that read generated data is recorded with `synthetic: true` in its `run.json` (the seeded demo
marks all its runs; an upload can be marked synthetic when it is made, and runs that read it inherit the mark).
Every results and value report drawn from such a run, on screen and as PDF, opens with the block "Synthetic data:
planted effect, not a forecast". A run recorded before the field existed, and any run that did not read generated
data, carries nothing. `tests/unit/test_docs_honesty.py` keeps the planted figure out of the documentation: it
may appear only in demo documents, or on a line that says it is planted.

## 10. Uplift stability, calibration and the beats-risk check (M96)

An uplift model is worth using only where it ranks customers better than plain risk ranking at the same
budget. M96 measures that, out of sample, and acts on it; the frozen champion rule is unchanged. The full
account, with the formulas, is `docs/UPLIFT.md` section 12 ("Does the model earn its place?").

* **The beats-risk check.** Every uplift training run scores plain rankings of its own hold-out (the
  model's `p_control`, its `p_treated`, and the use case's last approved propensity model) with the same
  AUUC, and the model's AUUC minus each with a paired bootstrap that reuses the evaluation's own resamples
  (`UpliftEvaluation.baseline_comparison`). It passes only when the lower bound of the difference against
  risk (the propensity model, else `p_control`) is above zero.
* **Calibration and fold stability.** `calibration_by_decile` (predicted against measured uplift per
  decile, with intervals) is computed on every run; `fold_auuc` is off by default, LightGBM only, and says
  what it would cost before it is turned on.
* **The Approver's checks.** `engine/model_gates.py` turns these into advisory checks
  (`UPLIFT_NOT_BETTER_THAN_RISK`, `UPLIFT_UNSTABLE_ACROSS_FOLDS`, `UPLIFT_MISCALIBRATED`) on the Approvals
  screen. Not measured is shown as not measured, never as a pass. They do not block a decision.
* **The ranking a list uses (J5).** A scoring run of a model whose stored check failed ranks its contact list
  by the approved propensity model, at the same number of contacts, and writes `ranking_choice.json` with
  the reason; with no approved propensity model it keeps the uplift ranking and says so. When the fallback
  is not the model the training check compared with (a newer approval, or none at training), the reason
  names both and `fallback_matches_check` is false. A model trained before M96 ranks exactly as before
  (`engine/decide/ranking.py`).
* **The equal-budget comparison.** Opt-in (`uplift.evidence.risk_comparison`): uplift top-N against risk
  top-N in each fold of a ring cross-fit of the randomised rows, valued with `evaluate_policy` and the rows'
  recorded treatment probabilities, by extra conversions and per rupee with 95% intervals
  (`engine/measurement/compare.py`, `GET /runs/{run_id}/risk-comparison`, a Viewer's). Its nightly coverage
  test is `tests/statistical/test_risk_comparison_coverage.py`.

## 12. The treat list and its reasons (M98, DEC-1308)

Until integration, the downloaded file is the campaign hand-off. A campaign manager downloads it to hand
to an execution tool (an ESP, SMS aggregator or dialler). `scores.csv` (the "contact list") stays as it was:
a band, an action label and `reason_1..n` that read like feature names. The **treat list** is the file made
for the hand-off, and the Output page labels them differently: **Download contact list (CSV)** is
`scores.csv`, **Download treat list (CSV)** is `treat_list.csv`.

* **The artefact.** `treat_list.csv`, `treat_list.parquet` and `treat_list_summary.json` are written beside
  the scoring run (`engine/decide/treat_list.py`, `build_treat_list`) the first time one is asked for
  (`GET /runs/{id}/treat_list.csv` or `/artefacts/<name>`), from the run's own files: `scores.parquet` (else
  `scores.csv`), `run.json`, `run_config.json`, `holdout_assignment.parquet`, `row_explanations.parquet`,
  `expected_gross_value.json` and the uploaded rows. The builder is never imported by the pipeline's stages,
  adds no rebind, and changes no existing file (`scores.csv` is byte-identical with or without it). A run that
  cannot have one (not finished, trained a model, no scores, no saved `run_config.json`) answers `409
  RUN_NOT_SCORED` with the reason in plain words. Retention removes `treat_list.*` with `scores.*` but keeps the summary, so a
  missing row-level file is rebuilt from the scores, or refused the same way once they are gone.
* **The run's own settings.** The builder reads the settings the run was scored with (`run_config.json`),
  never today's use case file, so editing a use case does not change the treat list of a finished run.
* **Columns** (CSV and parquet, in this order): the customer key (every column of a composite key), `use_case`,
  `model_version`, `band` (propensity) or `segment` (uplift), `treat`, `holdout`, `explore`,
  `suppression_reason`, `offer`, `channel`, `contactable_channels`, `runner_up_offer`,
  `runner_up_net_value`, `offer_reason` (Plan J M100 part B, §14; empty on a run of one offer), `control_group`
  (below), `net_value`, `expected_gross_value`, `reason_1`, `reason_2`, `reason_3`. The M100 columns and
  `control_group` sit before `net_value`, so every column before them keeps its position from the start and the
  last five keep theirs from the end
  (the reasons are always the last three columns). In the parquet the flags are booleans; in the CSV they are `1` and `0`, and **empty when
  unknown**. Rupee columns are written to the paisa.
* **`control_group` is the run's own control group (DEC-1311 (al)).** `1` for a customer the run kept back as its
  control (Phase 1's actions stage: the per-run draw, or the eligible members of a persistent hold-out), `0` for
  every other; a treat list written before the column existed has no such column; arbitration reads it from the
  run's scores in memory and leaves the stored files as they were. It never changes `holdout`, which stays M92's flag and stays empty on a run that wrote no
  assignment file. A customer in the control group is never treated.
* **Joined on the key, never by position.** `holdout_assignment.parquet` and `row_explanations.parquet` are
  joined to the scores on **every** key column (`customer_id` and `snapshot_date` for a periodic dataset),
  whatever order they list the customers in. A customer a file does not cover gets a null, never `false`:
  the holdout and explore flags are null for that customer, and the summary says how many (`holdout_note`);
  their reasons are the text `scores.csv` already holds for them. A file that repeats a key is not guessed at:
  the flags are null for every customer, with a note.
* **`treat`** is M92's own column. With `holdout_assignment.parquet` it is its `treated` column (selected, not
  held out, not suppressed, or explored). Without one (a default run) or for a customer it does not cover it
  is derived with M92's own functions (`engine.holdout.assign.selection_masks` and `treated_flags`), not a copy
  of them. Whatever the source, `treat = 1` implies not suppressed, not in the holdout or control group, and
  not a sleeping dog. M99 can then set `treat = 0` for a customer contactable on **none** of their planned
  channels (§13), without suppressing them: `suppression_reason` stays empty and `scores.csv` is untouched.
* **`holdout`** is `holdout_member` of `holdout_assignment.parquet`, null when the file is missing (a plain
  note in the summary) or does not cover the customer. **`explore`** likewise.
* **`offer`** is the row's action (the band's, or the uplift policy's), null for a suppressed or control row.
  A customer explored although the policy left them out gets the uplift policy's treat action, and none on a
  propensity run (a band the list does not contact names no offer): the band's own action would contradict
  `treat = 1`. **`channel`** is the first planned channel the customer is contactable on, null when the
  customer is not treated or no channel is planned; **`contactable_channels`** lists the configured channels
  the customer is contactable on, null when the run configured none (§13).
* **Money, one unit per column, in rupees.** `net_value` is M97's `net_value` column of an uplift run's scores
  (incremental: `uplift x value x margin x horizon - costs`), null when the run was not ranked by value.
  `expected_gross_value` is M97's expected gross value of a **propensity** run
  (`p x value x margin x horizon - costs`, `engine.decide.value.expected_gross_values` over the costs, value
  column and score field in the run's `expected_gross_value.json` and the uploaded values), null when the run
  did not opt in. It is **not incremental**: it counts customers who would have responded without a contact.
  So a propensity run's `net_value` is always null, and the summary labels the gross figure. A missing or
  non-numeric value stays null, never zero. If the gross value cannot be worked out (for example the upload was
  deleted), the column is null, the summary says why and a warning is logged.
* **Business-language reasons.** `configs/decide/reasons.yaml` (found under the configuration root) maps a
  **feature** and a **direction** to a phrase. A reason's direction is whether the feature pushed the **score**
  up or down, not whether the customer's value went up or down, so a phrase may say two things only: the
  customer's own value (`{value}`, as the explain stage stored it) and which way it moved the score ("Monthly
  spend of 1499 pushes the score down"). It never asserts a trend or a number the row does not carry: the file
  is refused when it loads if a phrase holds a digit outside `{value}`. `none` is the phrase of a general reason
  (no direction was measured). A feature or direction with no phrase, and a reason whose value is missing, keep
  today's text. Fewer than three reasons leave the remaining columns null. Phrases pass
  `engine.pilot.plain.jargon_in`. The mapping reads the Arrow list column once and runs in linear time
  (`engine/decide/reasons.py`).
* **Guided setup.** For the columns `reasons.yaml` does not cover, `engine/agent/recommend.py::suggest_reason_phrases`
  proposes wording as a **check** suggestion. No run setting can hold a phrase, so Guided setup lists it among
  the helper's assumptions, in plain words, and applies nothing (DEC-1308 (n)).
* **Row-level privacy and access.** Registered in `configs/privacy.yaml`, `engine/privacy/layout.py` and
  `api/access_policy.py` (`ROW_LEVEL_ARTEFACTS`). Served by `GET /runs/{id}/treat_list.csv` and
  `GET /runs/{id}/artefacts/{name}`. Analyst when sign-in is on, audited on read (`runs.treat_list_download`).
  The summary is not row-level and a Viewer may read it.
* **UI.** The Output page of every scoring run, propensity or uplift, shows the treat list card (`ui/modules/decide/`): the counts of the
  summary, "Include treat = 1, exclude holdout = 1", the money lines and the server's notes, and **Download
  treat list (CSV)**, gated by role in `ui/modules/production/gate.js`. If the summary cannot be loaded the card
  shows the server's message. The Output page's own **Download contact list (CSV)** is `scores.csv`.
* **Scale.** The builder works on whole columns (Arrow compute and hash joins on the key), with no loop over
  customers. 200,000 customers with every input take about 3 seconds on a shared 4-CPU machine, so one million
  take about 13 to 16 seconds (`tests/unit/decide/test_treat_list_scale.py`).

## 13. The offer and channel catalogue; channel-aware consent (M99, DEC-1309)

* **The action catalogue (`configs/decide/catalogue.yaml`), absent by default.**
  Validated by a frozen pydantic model `ActionCatalogue` (`engine/decide/catalogue.py`), following the
  `configs/privacy.yaml` pattern. Each action has an `action_id`, a `label`, `channels` (a list, in order of
  preference; a single `channel: sms` is accepted), `offer_cost` and `contact_cost` in rupees, an optional
  `eligibility`, and channel requirements such as `dlt_template_id` and `message_category`. The repository
  ships **no** catalogue: `configs/decide/catalogue.example.yaml` is a labelled example, never read. With no
  catalogue nothing changes: no action id is checked, no cost is overridden, and the treat list's
  `catalogue_sha256` is null. When one exists in the run's config root, the run writes `catalogue_stamp.json`
  after the actions stage (the file's SHA-256 and the channels of each action the use case names). The treat
  list, built later on demand, plans channels from that stamp and puts its SHA-256 on the summary; when the
  file has since been edited, added or removed it logs a warning and says so in `catalogue_note`, and still
  plans from the run's record.
* **Costs (`engine/pilot/roi.py::lookup_value_costs`).** Still the one place costs are looked up. With a
  catalogue, its channels' contact costs replace `configs/pilot/value.yaml`'s for those channels (the first
  action listing a channel decides). An **offer** cost comes only from an action id (`action_id=`); a channel
  gives a contact cost only. An unknown action id is `CATALOGUE_ACTION_UNKNOWN`.
* **Use cases point at actions additively:** `actions.bands[].action_id` and `uplift.policy.treat_action_id`.
  `Band.action` stays the label. An id the catalogue does not declare fails config load with
  `CATALOGUE_ACTION_UNKNOWN` (`engine.decide.catalogue.validate_action_ids`). The channel types are declared
  in `engine/decide/spec.py`, which imports nothing from `engine` (M92's `engine.holdout.spec` pattern). Unset, neither field is serialised, so a default config dumps as before.
* **Per-channel consent and contactability (`actions.suppression.channels`).**
  `{channel: {consent_column, contactable_column}}`, in order of preference. A channel opt-out is **not** a
  suppression reason: the three reasons and their precedence (DEC-A2) are unchanged, and the customer keeps
  their score, band and action in `scores.csv`. During the run, right after the actions stage
  (`engine/decide/contactability.py`, installed in the Plan J block of `engine/pipeline.py`), each customer's
  contactability per channel is worked out from the columns **as uploaded** (Phase 1's truthiness: a null is
  not a consent) and, when the consent ledger gates the run, from the ledger's records for that channel
  (`ConsentLedger.classify(..., channel=ch)`). It is written as `channel_contactability.parquet` (the key
  columns and one `contactable_<channel>` flag per channel; row-level, Analyst-only) and
  `channel_contactability.json` (counts). A configured column the file lacks is skipped with a warning. With
  no channels configured, nothing runs and nothing new is written.
* **`channel_counts`.** Per channel, the customers **eligible to be treated** (neither suppressed nor held out
  as control) who are not contactable on it. They are in `channel_contactability.json`, in the treat list
  summary, and on the `opted_out` entry (else the `consent_false` entry) of `scoring_summary.json`'s
  `suppressed` list. No entry is added for them: when neither rule ran, the summary has none.
* **The treat list.** It joins `channel_contactability.parquet` on every key column. The planned channels are
  the catalogue action's as the run recorded them (`catalogue_stamp.json`; the band's `action_id`, or the
  uplift run's `treat_action_id`); with no recorded catalogue action they are the configured channels. A treated customer is sent on the first planned channel they are
  contactable on (`channel`); one contactable on none of them is **not treated** (`treat = 0`), is counted
  in `uncontactable_rows`, and is not suppressed. `contactable_channels` lists every configured channel the
  customer is contactable on; it is null for a customer the file does not cover (and for every customer of a
  run with no channels configured), who is treated as before, on the first planned channel. It is labelled
  from the flag patterns present, so the cost is linear in rows whatever the number of channels.
* **The consent ledger per channel.** `consent_record.channel` is nullable (`alembic/versions/0007_consent_channel.py`;
  on a laptop's SQLite `platform.db`, the column is added in place, also when the scoring seam opens the
  ledger). Null means every channel, so every record stored before M99 keeps applying to all of them. The
  scoring gate's all-channel question (`channel=None`) reads only all-channel records: an SMS-only
  withdrawal closes SMS and does not suppress the customer. Since M100 part B, when the use case configures
  channels, a valid grant on at least one configured channel (with no newer all-channel withdrawal) also
  passes that question, so consent recorded per channel only is not read as no consent (section 14). A channel must be a channel name (lower case
  letters, digits and `_`, after stripping and lower-casing; the same rule as config and catalogue): an
  imported row with `e-mail` is refused with `CONSENT_CHANNEL_INVALID`, so no opt-out is stored that no
  configured channel could match.
* **Region rules, data-driven.** `configs/regions/<region>.yaml` (such as `in.yaml`:
  `sms_requires: [dlt_template_id, message_category]`) is applied to the catalogue's `region` without naming
  any region in Python. A missing DLT template id is `ACTION_DLT_TEMPLATE_MISSING`; another missing required
  field is `CATALOGUE_INVALID`. A value left as a placeholder in angle brackets counts as missing.

## 14. Choosing the offer (M100, DEC-1310)

Part A (learning, checking, evaluating and measuring several offers against one shared control) is
described in `docs/UPLIFT.md` section 14, "Several offers against one shared control". Part B makes the
choice inside the scoring run and puts it on the treat list:

* **Where.** `engine/decide/offer_run.py`, installed on the score flow's stage table after M99's
  contactability seam: after the actions stage of a scoring run of a model of several offers. A run of one
  offer is untouched (`tests/integration/decide/test_m100_binary_identity.py`,
  `tests/integration/decide/test_offer_choice_binary.py`).
* **The rule.** Per customer, the offer with the highest net value (M97, each offer priced with its own
  catalogue action's costs, `uplift.policy.arm_action_ids`) among the offers they are eligible for (not
  suppressed, not held back, contactable on one of the offer's planned channels, M99) and not a sleeping dog
  for; no offer when none pays for itself; a total budget in rupees (`uplift.policy.total_budget`) and in
  contacts (`budget_contacts`), greedy by net value per rupee. `docs/UPLIFT.md` section 14 has the details.
* **Files.** `offer_choice.parquet` (row-level: registered in `configs/privacy.yaml`,
  `engine/privacy/layout.py`, `ROW_LEVEL_ARTEFACTS`; its `offer_total_cost` is the offer's total expected
  cost, contact + offer cost x `p_treated`, in rupees, not the catalogue's offer cost alone) and
  `offer_choice.json`. The treat list gains three columns on every run, between `contactable_channels`
  and `net_value` (section 12): `runner_up_offer`, `runner_up_net_value` and `offer_reason` (empty on a run
  of one offer), and its summary
  `offer_counts` (a run that chose offers: the choice's own offers, `offered_rows` of `offer_choice.json`)
  and `channel_rows` (any run whose treated rows have a channel).
* **The policy's offer without the hold-out (DEC-1311 (af)-(ak)).** The choice is made after the hold-out has set
  customers aside, so a held-back customer has no offer in the list, and a campaign cut by the first offer's
  `intended_treatment` leaves out a customer whose best offer is another one (about one in five of the contacted
  customers in the Phase 2 journey). `offer_choice.parquet` therefore also says, for **every** customer,
  `policy_offer_arm` (with `policy_offer_label` and `policy_offer_net_value`) and `policy_intended`: the offer the
  same rule would choose if nobody were held back. Only the hold-out is ignored: suppression, channel
  contactability, sleeping dogs and `min_roi` still count. **The budget** (`total_budget`, `budget_contacts`) was
  spent in the run on customers who were not held back, in the order of the greedy walk (net value per rupee, then
  net value, then row order), skipping an offer that did not fit. The policy replays that same walk over **every**
  customer with a preferred offer, in the same order: what has been spent (and how many offers given) moves only when
  a customer the run actually contacted is reached, and at each customer's place the walk's own fit test decides
  whether the walk, as run, had room for their offer. A customer is intended when it did. For a customer who was not
  held back that is exactly "the run contacted them", so the treated arm of a campaign is the contacted list and a
  customer the walk skipped is in neither arm; a held-back customer is intended when the walk had room for their
  offer at their place, whatever their own draw (as `intended_treatment` is, DEC-606). A budget that does not bind
  behaves like no budget. The hold-out is on top of the budget. The new
  columns sit between `offer_reason` and `explore_arm`: every earlier column keeps its place from the start and the explore columns stay the last five. A file written before this has none of them, and its campaign keeps the first offer's population.
  On the treat list a held-back row the policy meant to contact carries that offer's net value in `net_value`,
  as a held-back row of a one-offer run carries its own, so arbitration can compare it by value; its `offer` and
  `channel` stay empty, `treat_list_summary.json` counts such rows (`held_back_value_rows`) and says the value is
  the policy's intended offer and not an action taken (`held_back_value_note`). The list's totals count treated
  rows only. `policy_intended()` (what `comparable_keys` is given) reads the same column.
* **The explore slice (M92).** An explored customer the choice left without an offer is given the best
  offer they could be given (`explore_arm` of `offer_choice.parquet`: the offer the choice preferred for
  them when the budget dropped them, else the best eligible offer that is not a sleeping dog for them),
  outside the budget. The treat list summary counts those offers apart (`explore_offer_counts`), gives
  their expected cost (`explore_cost`, rupees) and says they are outside `uplift.policy.total_budget`
  (`explore_note`), so the list's cost is the choice's `spent` plus `explore_cost`.
* **The catalogue the run is checked against is the one it is priced with.** Whatever creates a scoring
  run (and an uplift training run that maps offers to actions) writes `catalogue_stamp.json` from the
  config root it resolved the use case from: `POST /runs` and `POST /uplift/runs` (`create_app(config_root=...)`,
  else `MARKETING_AI_CONFIG_DIR`, else `configs/`), and a scheduled firing
  (`engine.scheduling.firing.start_dataset_run`, from `FiringServices.config_root`); the run keeps that
  stamp rather than stamping again from its own root (`engine.decide.catalogue.stamp_checked_catalogue`,
  `stamped_at_creation`).
* **Channel consent columns are never model inputs.** A training run whose use case configures
  `actions.suppression.channels` leaves those channels' consent and contactable columns out of the
  features, through `prepare.exclude_columns` (`engine/decide/channel_columns.py`), without an edit to a
  Phase 1 stage file. A model trained before that with one of them among its inputs (its `schema.json`)
  does not score for a use case that names them: the run stops before predicting with
  `CHANNEL_COLUMN_MODEL_INPUT` and says to retrain.
* **Channel-only consent.** With channels configured, the scoring gate passes a customer with a valid grant
  on at least one configured channel (section 13); without channels it is unchanged. A planned channel the
  use case does not configure (a catalogue action's push, say) has no flag of its own, so it is open only
  to a customer whose consent covers every channel: when the gate lets anyone through on a channel grant
  alone, `channel_contactability.parquet` gains `all_channel_consent` (and its summary
  `channel_only_consent_rows`), and the choice of offer and the treat list read it. A customer who consented
  to SMS alone is never sent anything on push. Without such a customer the file is M99's, and such a channel
  restricts nobody, as before.
* **On screen.** The uplift Output page shows "Which offer each customer gets" (per offer: customers,
  channels, net value, costs; no offer and why), and the treat list card counts the treated rows per
  offer and per channel. Every number is the server's.

## 15. One action per customer across use cases (M101, DEC-1311)

**Arbitration resolves competing actions across use cases.** When a client runs several campaigns at once
(win-back, cross-sell, churn prevention), a customer can be on more than one use case's treat list in the same
cycle. Without arbitration they get several messages, and no use case can tell what its own contact did.
`POST /decide/arbitrate` (Analyst) takes the latest finished scoring run of each selected use case (or explicit
`run_ids`), over the same customer key, builds or reads each run's treat list (M98) and writes
`arbitrated_treat_list.csv` and `.parquet` into every participating run: at most one action per customer (or the
configured contact cap), with the winning use case, the actions that lost and why. "Latest" is the run that
finished last (then created last, then the larger run id): run ids end in random characters, so their text order
says nothing about which is later.

* **Who wins (`engine/decide/arbitrate.py`).** A candidate is a treat-list row with `treat = 1`, in a customer no
  hold-out or control group keeps back. For one customer, in this order:
  1. **Hold-out members are never treated** by any use case, even one that does not know the hold-out. **So is a
     customer in the control group of any selected use case's run** (`control_group` of its treat list,
     DEC-1311 (al)), which another use case's list may still want: the run kept them back to see what happens
     without a contact, so no other use case contacts them either.
  2. **A row M92 treated at random keeps its action** (`explore = 1` and `treat = 1`): it is kept before any
     comparison, so the random sample stays random. If two use cases both treat the same customer at random,
     both are kept only if the contact cap allows; otherwise the first in the request's use case order is kept,
     and the other is listed in `losing_actions` and counted in `explore_dropped_count`.
  3. **The rest are ranked by priority x value only when every candidate carries the same kind of value**: all a
     `net_value` (incremental, from an uplift run), or all an `expected_gross_value` (not incremental, from a
     propensity run). The two are never compared with each other. A candidate with no value is never given one
     (no value of 1, no zero). When the kinds differ, or one candidate has no value, the customer is ranked by
     priority alone.
  4. **A tie is broken by the order of the use cases in the request** (with no use case named, by use case id).
* **Why each row is what it is.** `arbitration_reason` on every row: `only_action`, `within_contact_cap`,
  `net_value`, `expected_gross_value`, `priority`, `request_order`, `explore_treated`, `explore_request_order`
  for a winner; `held_out`, `channel_cap`, `not_selected` for a customer nobody treats. `priority_weight` is the
  weight used; `priority_score` is priority x value, and is blank where the choice was not made on a value.
  The summary counts the customers who end with an action by how it was chosen when several use cases wanted
  them: `customers_decided_by_value`, `_by_priority`, `_by_request_order`, `_by_explore` (each matches the
  `arbitration_reason` of the treated rows), and apart from them `contested_customers_channel_capped`, the contested
  customers whose chosen action a channel cap then removed (they end with no action); and `holdout_blocked_actions`,
  `control_blocked_actions` (actions kept back by another use case's control group where no hold-out held the
  customer), `explore_kept_count`, `explore_dropped_count`. The summary also names the `run_ids` arbitrated.
* **No configuration ships.** `configs/decide/arbitration.example.yaml` is a labelled example that is never read.
  With no `decide/arbitration.yaml` in the config root, every use case has priority 1, a customer gets at most one
  action per cycle, and there are no channel caps. A client's file sets `use_cases.<id>.priority`,
  `contact_cap_per_customer` and `channel_caps` (a cap of 0 or more per channel). A file that is there but cannot be
  read (not YAML, an unknown key, a contact cap below 1, a negative channel cap) is refused: **422
  `ARBITRATION_CONFIG_INVALID`**, naming the setting. It is never replaced by the defaults, which would drop the
  caps and priorities the client wrote without saying so.
* **Contact and channel caps.** `contact_cap_per_customer` limits the winning actions per customer. `channel_caps`
  limit the actions per channel over the cycle (the `channel` column is M99's planned channel). Capacity goes to
  explore rows first, then to rows with an incremental value, then to rows with a gross value, then to rows with
  no value; inside each group by priority x value (priority alone for a row with no value), then by request order.
  **The explore rows are the exception inside their group: they are kept in a fixed pseudo-random order of
  (use case, customer) that ignores their value**, so a binding cap keeps a random sample of the explore rows, not
  the most valuable ones (the explore sample stays random).
  A customer whose only action is capped out is not treated (`arbitration_reason = channel_cap`, the dropped action
  is in `losing_actions`); no action moves to another channel.
* **What a non-winning row says.** A customer nobody treats has one row, `treat = 0`. If a hold-out kept them back,
  the row is a use case that held them back, `holdout` is that row's own flag (true) and `holdout_use_cases` names
  every use case whose hold-out held them. A customer in a use case's control group is
  named the same way in `control_use_cases`; when no hold-out held them the row is that use case's, `control_group` is true, and
  `arbitration_reason` is `held_out` when the control group kept an action from them; a control customer nobody
  wanted reads `not_selected`, as before (the one use case's own control group included). A customer in both a hold-out and another use case's control group keeps the hold-out's row, whichever is listed first (DEC-1311 (aq)). A customer capped out of a channel keeps no offer, channel or offer
  detail on the row: the action not taken is in `losing_actions`. Customers keep the order they first appear in
  (one use case selected: the treat list's own row order).
* **One use case selected equals its treat list.** Every column of the treat list (including M99's
  `contactable_channels` and M100's runner-up and offer-reason columns when it has them) comes through with its
  values, and the arbitration columns are added; both `segment` (uplift) and `band` (propensity) are kept when the
  use cases differ in kind.
* **One campaign per use case, comparing like with like.** `create_arbitrated_campaign`
  (`engine/measurement/campaign.py`) builds a campaign from a run's scores. The population is the use case's own
  policy (an uplift run's `intended_treatment`, a propensity run's treat bands), never every eligible customer, and
  **both arms are cut by one rule that does not look at the hold-out**: for a use case that chose the offer per
  customer the population is the customers whose policy offer exists (`policy_intended` of
  `offer_choice.parquet`, DEC-1311 (ai)), not the first offer's `intended_treatment`; `comparable_keys` runs the arbitration again
  as if no one were held back (no hold-out and no control group), a held-back customer the policy intended to contact (`policy_intended`, M92's
  one definition of "selected") competing like a treated one, and a customer is compared in a use case when it wins
  him in that run. A customer another use case wins, or one a rival's random (explore) action took, is in neither
  arm (`suppressed`, not intended), the held-back ones as the treated ones. A customer kept from being contacted by
  another use case's hold-out or control group, or by a channel cap, stays in both arms (intent to treat): nothing in that depends
  on the customer's own random draw. (Cutting only the treated arm to the winners, as the first version did, left
  the held-back arm with every eligible customer, and the measured effect came from who was in each arm.) Keys are
  matched in the treat list's spelling (`engine.keys.key_text`, so a composite key and a whole-number id match).
  The campaign has the terms `POST /campaigns` gives: its start is the run's finish time
  (`treatment_start_source = "run_finished"`), its outcome window the use case's own, and a use case that holds
  nobody back (`measure_offered`) or won no customer gets no campaign; the response lists each use case's campaign
  (`created`, `reused`, or `skipped` with the reason). Posting the same arbitration again (the same runs in the
  same order under the same settings, `Campaign.arbitration_id`) returns the campaigns already made.
  `POST /decide/arbitrate` refuses two runs of one use case (`ARBITRATION_USE_CASE_REPEATED`).
* **Row-level privacy and access.** `arbitrated_treat_list.csv` and `.parquet` are row-level run artefacts
  (`configs/privacy.yaml`, `engine/privacy/layout.py` `Store.SCORES`, `api/access_policy.py` `ROW_LEVEL_ARTEFACTS`),
  kept **only in the runs that took part** (`runs/<id>/`), where retention and erasure find them: no customer-level
  copy is kept at the store root. `GET /decide/arbitrated-treat-list.csv` and `.parquet` (Analyst, audited:
  `decide.arbitrated_treat_list_download`) serve the first participating run's copy that is still kept, and answer
  404 `ARBITRATION_NOT_FOUND` once every copy has aged out. The aggregate `arbitration_summary.json` (no customer id)
  is kept at `decide/` and in each participating run; `GET /decide/arbitrate` and `GET /decide/conflicts` return it
  to a Viewer.
* **Results.** The `Arbitration & Conflicts` card (`conflictsCardHtml`, `ui/modules/decide/views.js`) shows customers
  evaluated, with conflicts, treated, actions dropped, how conflicts were settled (and the contested customers a
  channel cap left with no action), and a table by use case.
* **Scale.** Whole-array sorts and Arrow string kernels, no Python loop over the rows: 200,000 customers across
  two or three use cases take about 1 to 1.5 seconds on a quiet 4-CPU machine, so a million take about 5 to 8.
  `tests/unit/decide/test_arbitrate_scale.py::test_arbitration_time_grows_linearly_with_the_rows` (fast) compares
  200,000 rows with 50,000 in one process, so machine load slows both; the wall-clock budget tests
  (`test_arbitrate_200k_rows_with_mixed_values_explore_and_caps`, `test_arbitrate.py::test_arbitrate_scale_200k_rows`)
  are marked `slow`, because a 3 second budget fails on a shared machine under load.

## 16. Revenue outcomes and CUPED (M102, DEC-1312)

**A campaign can be judged on an amount.** Campaign owners are judged on revenue, and a binary effect of one to
three points is hard to see. A campaign (or a run's own measurement) can now be measured on an amount per customer:
`outcome_kind: continuous` on the registered test plan, on `POST /campaigns/{id}/measure` (the plan's kind when the
body names none; a different one is `409 TEST_PLAN_CHANGED`), on `POST /runs/{id}/measure` and on
`POST /runs/{id}/campaign-results`. Without it everything is measured as a yes/no outcome, exactly as before: a
binary report's JSON is byte for byte what it was (`tests/unit/measurement/test_m102_binary_identity.py`, against
reports recorded on the commit before M102).

* **What is reported (`engine/measurement/amounts.py`, `engine/measurement/continuous.py`).** The population, the
  join, the maturity rule and the rows left out are `measure_incrementality`'s, unchanged. On the usable amounts:
  `treated_mean`, `control_mean`, `mean_difference` and `mean_difference_ci`, the **Welch** interval (unequal
  variances, Welch-Satterthwaite degrees of freedom, Student's t computed in the module through the regularised
  incomplete beta function, so no statistics library is needed); `p_value` is Welch's; `relative_lift` is the
  difference over the held-back mean. Rates mean nothing on an amount, so `treated_rate`, `absolute_lift` and
  `incremental_conversions` are null and `treated_conversions` / `control_conversions` count the customers whose
  amount is above zero. An amount is read as a number; a value written otherwise ("1,200") is refused with a count,
  never guessed, and a missing one is left out and counted, as a missing yes/no outcome is.
* **The adjusted estimate (CUPED).** With a covariate - an amount each customer had *before* the campaign, such as
  last quarter's revenue - the report adds `adjusted_lift`, `adjusted_interval` (Welch's on `y - theta (x - x̄)`,
  `theta` the within-arm slope) and `variance_reduction` (`1 - adjusted variance / unadjusted variance` of the
  difference, about `rho²`: 0.36 at `rho = 0.6`). A customer with no earlier amount keeps their row with the mean of
  the known ones (`rows_covariate_missing`); a covariate that does not vary gives `adjustment_note`, never a number.
  A yes/no outcome never uses a covariate.
* **Only a covariate registered in advance.** `measure_campaign` uses a covariate only when the test plan in force
  names it: a different one, or a covariate on an amount with no plan registered, is `409 TEST_PLAN_CHANGED`,
  because choosing the adjustment after seeing the outcomes is one more way to move the goalposts. On a yes/no
  outcome a covariate is never used, so one named with no plan is ignored, as before M102. When it was registered, the
  verdict (`engine.measurement.measure.amount_verdict`) and the value view read the adjusted estimate: it is the
  planned analysis. The unadjusted one is always reported beside it.
* **Point in time, no leakage.** The covariate comes with the outcomes file (`POST /campaigns/{id}/outcomes
  {covariate_column, covariate_date_column}`), with the date each value was measured up to. The dates are compared
  by day, since a value dated a day includes that whole day: a value dated on or after the day of the customer's
  treatment (their own date, or the campaign's start, even when that start is later in the day), or a covariate with
  no date column, is refused with `422 COVARIATE_NOT_BEFORE_CAMPAIGN`, and nothing is stored: such a value could
  contain the campaign's own effect. A value with no date on its row is treated as unknown.
* **Skewed revenue.** Most customers spend nothing and a few spend a hundred times the median. The interval rests on
  the average being close to normal, which a long tail delays; Kohavi, Deng, Longbotham and Xu (2014, rule 7) give
  the size that suffices: more than `355 g²` customers per arm, `g` the arm's skewness. Below it the report keeps its
  numbers and carries `outcome_warnings: [OUTCOME_SKEWED]` with a plain sentence that the range may be too narrow.
  Amounts are never capped or trimmed: that would change what is measured.
* **Planning (`engine/measurement/planner.py`).** `mde_continuous(n_t, n_c, sd, rho2=)`, `n_for_mde_continuous` and
  `achieved_power_continuous` use `se = sd · sqrt(1 - rho2) · sqrt(1/n_t + 1/n_c)`: a covariate expected to explain
  36% of the spread needs 36% fewer customers for the same change. A test plan of an amount takes `mde_value` (in the
  amount's unit), `outcome_sd` and `expected_rho2` (which needs the covariate), and its `achieved_power` and
  `PLAN_UNDERPOWERED` come from them; it never mixes them with `mde_pp` / `base_rate`. A plan that does not use them
  stores and hashes as before. On a plan of an amount the "Plan the test" card (`GET /campaigns/{id}/plan-preview`)
  gives each point's `mde_amount` (`mde_continuous` with the plan's `outcome_sd` and `expected_rho2`; `mde_pp` is
  null), or, with no `outcome_sd`, no points and a plain reason; a yes/no plan's points are unchanged.
* **Money (`engine/pilot/roi.py`).** A report on an amount is priced from its per-customer difference (adjusted when
  registered) times the contacted customers; `value_per_outcome` is what one unit is worth in rupees (1 when it is
  revenue in rupees), and the offer cost is counted for every contacted customer whose amount is above zero. Without
  values nothing is put in rupees and `value_note` says what to enter. Outcomes ingested as amounts
  (`incrementality_input.json`) keep only the two averages, so the view says no range can be given and prices
  nothing, instead of "No outcomes have been recorded".
* **Several offers.** `measure_campaign(arm_column=...)` (M100) stays yes/no only: an amount there is refused with a
  plain `ValueError`, because no nightly check covers per-offer amounts or their adjustment yet; a covariate named
  on a yes/no several-offer campaign is ignored, as before M102.
* **Learning.** An uplift model learns from a yes/no outcome, so step 4 does not offer "Learn who to contact next
  time" on a campaign measured on an amount, and says why.

**How we know it is honest.** `tests/statistical/test_continuous_coverage.py` (nightly, 2,000 simulations a case,
the M95 band of four Monte Carlo standard errors) checks the coverage of both intervals on roughly normal revenue with
a covariate correlated 0.6 (and that the mean `variance_reduction` is within 0.03 of 0.36) and with an uncorrelated
one (the adjustment then changes nothing), on zero-inflated lognormal revenue (80% spend nothing) at the size the
skew rule asks for, that smaller long-tailed campaigns carry `OUTCOME_SKEWED`, and that the planner's n with
`rho2 = 0.36` has its 80% power. `engine.measurement.simulate.revenue_campaign` draws the campaigns with a known
difference in means. The fast tests (`tests/unit/measurement/test_continuous.py`, `test_planner_continuous.py`,
`test_roi_amount.py`, `test_amounts_scale.py`, `tests/integration/measurement/test_campaign_amounts.py`) check
Welch's interval and Student's t against `scipy`, the 0.36 at `rho = 0.6` on 40,000 customers, the leakage refusal,
the plan gate through the API, the value view, and that 200,000 customers take about ten times as long as 20,000
(1,000,000 in about 9 s, marked `slow`).
