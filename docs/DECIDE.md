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
| 13 | The offer and channel catalogue; channel-aware consent | M99 | not yet written |
| 14 | Choosing the offer (multi-treatment uplift) | M100 | not yet written |
| 15 | One action per customer across use cases | M101 | not yet written |
| 16 | Revenue outcomes and CUPED | M102 | not yet written |
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

`intended` is the population both arms are compared inside: an uplift run's `intended_treatment`, a
propensity run's treat bands, or every eligible customer (intent to treat). The **treatment start** is
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
to an execution tool (an ESP, SMS aggregator or dialler). Today `scores.csv` carries a band, an action label
and `reason_1..n` that read like raw feature names; buyers repeatedly say they distrust black-box AI and want
reasons they can explain to frontline staff or customers.

* **The treat list artefact.** `treat_list.csv`, `treat_list.parquet`, and `treat_list_summary.json` are written
  beside the scoring run in storage (`engine/decide/treat_list.py`). The builder is a pure function over finished
  scoring run artefacts (`scores.parquet`, `scores.csv`, `run.json`, `features.parquet`, `explanations.parquet`,
  `holdout_assignment.parquet`, `ranking_choice.json`); it is never imported by pipeline stage code and touches no
  frozen stage files.
* **Columns in `treat_list.csv` / `treat_list.parquet`:**
  - Customer key (all primary key columns for composite keys).
  - `use_case`: the use case identifier.
  - `model_version`: the model version or run ID.
  - `band`: the band name or uplift segment.
  - `treat`: integer 1/0 in CSV, boolean in parquet. Indicates whether the customer should be contacted.
  - `holdout`: integer 1/0 in CSV, boolean in parquet, or null when no holdout assignment exists (never defaulted to false).
  - `explore`: integer 1/0 in CSV, boolean in parquet, or null when explore assignment is absent.
  - `suppression_reason`: plain suppression reason when suppressed, else null.
  - `offer` and `channel`: nullable reserved columns (populated from the band's action or M99/M100 catalogue).
  - `net_value`: expected net value in INR from M97 when present, else null with a plain note in the summary (never defaulted to 0).
  - `reason_1`, `reason_2`, `reason_3`: business-language explanation phrases.
* **Invariants:**
  - `treat == 1` implies `holdout != True` and `suppression_reason is None`. Sleeping dogs are never treated.
  - Missing inputs are null (no fabricated booleans or numbers).
* **Business-language reasons.** `configs/decide/reasons.yaml` maps feature names and direction/value thresholds to
  human phrases ("Spent less each month for 3 months", "Raised two complaints in 30 days"). Unmapped features
  keep existing reason text (`Feature direction value`). All configured phrases contain zero jargon per `jargon_in`.
  `engine/decide/reasons.py` maps reasons vectorised without row-by-row python loops.
* **Guided setup.** `engine/agent/recommend.py::suggest_reason_phrases` proposes plain phrase mappings for unmapped
  features with confidence `CHECK`.
* **Row-level privacy and access.** Registered in `configs/privacy.yaml`, `engine/privacy/layout.py`, and
  `api/access_policy.py` (`ROW_LEVEL_ARTEFACTS`). Served via `GET /runs/{id}/treat_list.csv` and
  `GET /runs/{id}/artefacts/{name}`. Access requires Analyst role when sign-in is active and generates an audited
  read event. Customer IDs never appear in request URLs.
* **UI.** The Output page displays a Treat List summary card with customer counts and download button, along with
  the clear instruction: "include treat = 1, exclude holdout = 1". The button is role-gated in
  `ui/modules/production/gate.js` for Analysts.

