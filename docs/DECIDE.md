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
| 9 | The validity harness and the synthetic quarantine | M95 | not yet written |
| 10 | Uplift stability, calibration and the beats-risk check | M96 | not yet written |
| 11 | Ranking by net value | M97 | not yet written |
| 12 | The treat list and its reasons | M98 | not yet written |
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
always comes from the caller. A campaign is never measured on part of its customers: while any
outcome window is open the answer is `409 CAMPAIGN_NOT_MATURED` with `results_available_on`, and
nothing is stored.

**The registered test plan.** `POST /campaigns/{id}/plan` (Analyst) freezes a `TestPlan` - metric,
outcome column and kind, optional covariate, outcome window, analysis date and secondary dates, the
detectable effect and expected rate, and from the assignment the holdout share and the customers in
each arm - with `plan_hash`, the SHA-256 of its content. The audit event's `after_hash` is
`content_hash(plan)` and its details carry `plan_hash`. The same plan again returns the stored one; a
different one is `409 TEST_PLAN_EXISTS`; `POST /campaigns/{id}/plan/amendments {reason, ...}` writes
version n+1 with `amends`, every version kept (`GET /campaigns/{id}/plan`). When an effect and a rate
are given, the power of the planned arms is computed with `engine.uplift.power.power_of_lift`; below
the plan's power the plan carries `PLAN_UNDERPOWERED`, a warning that never blocks. At measurement any
difference from the plan - holdout share, population, window, outcome column, value or kind,
covariate - is `409 TEST_PLAN_CHANGED`; a read before `analysis_date` is an **early look**: the report
says so (`early_look`, and its summary), and no verdict is given.

**The value view** (`engine.pilot.roi.compute_roi`) reads a campaign's final report first - the one
named, else the run's latest - and never prices an early look.

**On screen.** Results lists the campaigns beside the runs (a results list registered through the
router's PLAN-J seam); a campaign's page (`#/campaigns/<id>`) shows its result and the "Plan the test"
card with the server's values only.

**Waiting for M92 and M93 (built in parallel).** Two pieces are wired once they merge:

* *Plan preview (M93).* `GET /campaigns/{campaign_id}/plan-preview?holdout=` (Viewer) in
  `api/routes/campaigns.py`: the assignment's `realised_population(...)` counts and the plan's
  expected rate passed to M93's `engine.measurement.planner` (`holdout_for_mde`, `achieved_power`,
  `cost_of_holdout`, `cost_of_explore`) at the shares the planner names; answer `{points: [...]}`,
  computed points only. `freeze_plan` then takes `achieved_power` from `planner.achieved_power` instead
  of `engine.uplift.power.power_of_lift` (the same test, so the numbers agree). The card's slider
  (`ui/modules/decide/plan.js`, the TODO there) steps over those points only. Tests: the preview
  matches the planner on the assignment's counts; the slider draws exactly the server's points.
* *Holdout epochs (M92).* `POST /campaigns` records `Campaign.holdout_epoch` from the scoring run's
  `holdout_assignment.parquet` / `HoldoutSpec.epoch`, and refuses `CAMPAIGN_EPOCH_MISMATCH` when the
  run's rows span two epochs (`engine/holdout/` helper over that file); `assignment.parquet` takes
  its `explore` flag and probability from the same file. Tests: a run spanning two epochs is refused;
  an explore row is carried and counted.
