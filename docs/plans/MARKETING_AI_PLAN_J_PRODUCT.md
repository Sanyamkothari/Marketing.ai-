# Marketing AI — Plan J: Product value first

**Companion to:** Plan H (`MARKETING_AI_PLAN_H_SIMPLE.md`), the market research
([`docs/research/MARKET_RESEARCH_2026_10.md`](../research/MARKET_RESEARCH_2026_10.md)), the positioning decision memo
([`docs/research/POSITIONING_DECISION_2026_10.md`](../research/POSITIONING_DECISION_2026_10.md)), the hand-off reference design
([`MARKETING_AI_PLAN_J_LAYER.md`](MARKETING_AI_PLAN_J_LAYER.md)), `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1300 … 1399 (claimed by M90)
**Milestones:** M90 – M111
**Status:** proposal for review, written on 7 October 2026 after the founders decided **product value first, integration later** (§1).

---

## Plan J at a glance

**The decision.** We make the product itself good before we connect it to anyone's tools. The research
found a list of things the product does not yet do, or does not do well enough. Plan J adds them,
fixes the weak spots found along the way, and proves the result on real public data. Then we show it to
the managers. Integration with clients' CRMs and engagement tools comes after that review, using the
hand-off design already written in `MARKETING_AI_PLAN_J_LAYER.md`.

Until then the hand-off is **a file the user downloads**: a richer treat list with the offer, channel,
reason, net value and holdout flag on every row. Anyone can load it into any tool by hand.

| Phase | Weeks | What the product can do at the end | Milestones | Effort (person-weeks) |
|---|---|---|---|---|
| **1 — Results you can trust** | gate at week 6 | The control group stays fixed per customer. A test is planned and registered before it runs. Every campaign is one record with one measurement. Our 95% ranges are tested to really cover 95%. Known defects are fixed. | M90–M95 | 18.0 |
| **2 — Decide better** | gate at week 13 | Uplift is approved only when it is stable and beats plain risk ranking. Customers are ranked by net value. The product chooses **which offer** and **which channel**, and gives **one action per customer** across use cases. Reasons read as business language. | M96–M101 | 23.0 |
| **3 — Prove better** | gate at week 15 | Revenue outcomes with smaller holdouts (CUPED). Audit any campaign another tool ran. A finance-ready **Value Proof Pack**. Warnings that tell the user what needs attention. | M102–M105 | 12.5 |
| **4 — Learn, validate, demonstrate** | gate at week 19 | Each cycle learns from the last. The monthly loop runs on schedule from saved connections (read-only). Cost is shown before each run. The full journey is validated on **real public randomised data**, and a manager demo is ready. | M106–M111 | 15.0 |
| **Later — Integration** *(after the manager review)* | — | Hand-off into the client's tools, reading their send logs back, AWS Marketplace, industry packs, DPDP evidence pack, starter-sender decision. | re-planned then | see the layer design |

**Team assumed:** 4 engineers and 1 data scientist, about **19 weeks** for about 68.5 person-weeks
(50 engineering, 18.5 data science). Phases overlap: each starts as soon as its dependencies are
merged, and each ends at a gate. The data scientist is the bottleneck (§6.2). With 3 engineers it is
about 24 weeks.

**What the founders must answer first:** the decisions J1–J10 (§1.2).

---

## 1. The decision this plan implements

### 1.1 Product first

The research (October 2026) produced two kinds of findings:

1. **What a good product in this space must do**, with evidence:
   - target by response, not risk (uplift, sleeping dogs) — **we have it**;
   - rank by net money, not by uplift alone;
   - choose between offers and channels, not only "contact or not";
   - one action per customer across use cases;
   - plan tests so effects of 1–3 points can be detected;
   - keep the control group fixed so results add up over months;
   - report net value with honest ranges, gross against incremental, naive credit against measured;
   - prove it on real randomised data before claiming it.
2. **How to get it into clients' tools** (the decide-and-prove layer).

The founders chose to do (1) first. A product that is excellent at deciding and proving is worth
integrating; integrating a product that is not yet excellent only spreads its weaknesses. The positioning
in the decision memo still stands: when we integrate, we integrate as a layer on the client's existing
tools, never as a replacement CRM.

### 1.2 Decisions to confirm with the founders

| # | Question | Recommended answer |
|---|---|---|
| J1 | Do we build the product improvements first and defer every write into client systems? | **Yes.** No milestone in Plan J writes outside our own storage. DEC-1100 ("the platform never writes to a client's systems") stays in force. |
| J2 | What is the hand-off until integration? | **A downloaded file:** the treat list (M98), with offer, channel, reason, net value and holdout flag per row. Row-level downloads are Analyst-only and audited when sign-in is on (M91). |
| J3 | Should the control group become persistent? | **Yes, opt-in per deployment.** Default stays today's per-run draw, so nothing changes until someone switches it on. The recommended setting for any real use is **universal** (one holdout across all use cases). |
| J4 | Do we run a small "explore" slice (a random share of eligible customers outside the target who are treated anyway)? | **Yes, opt-in, 0–10% (default 0).** Without it the next model cannot learn uplift for customers we never contacted. |
| J5 | Should "uplift beats risk ranking" decide which ranking the treat list uses? | **Yes.** When the uplift model does not beat plain risk ranking at equal budget, the treat list uses the propensity ranking and says why. The approval rule itself stays unchanged (M96). |
| J6 | Do we add multi-offer choice (several offers plus "no offer") in this plan? | **Yes (M100).** Every incumbent decides between offers, and the research names it as the next thing buyers expect. It follows the extension path already recorded in DEC-668. |
| J7 | Do we add one-action-per-customer arbitration across use cases? | **Yes (M101).** Without it, the same customer can get a churn offer, a payment reminder and a win-back message in the same week. |
| J8 | Which public datasets do we use for validation? | **Hillstrom** (randomised email test: two offer arms and a no-email control, visit, conversion and spend), **Criteo uplift** (if an owner downloads it through an approved channel) and **X5 RetailHero or Lenta** if licences allow. Each needs an owner to obtain it, because this environment cannot download them (M110). |
| J9 | Is sign-in on for any shared or client use? | **Yes.** It is needed for row-level download control and the Approver role. Local single-user demos may keep it off (Plan H H5 still holds there). |
| J10 | Team and horizon? | **4 engineers + 1 data scientist for about 19 weeks**, then the manager review, then a separate integration plan. |

### 1.3 What stays out of Plan J

- **Any write into client systems**: activation contract delivery, connectors that push, webhooks, send-log importers from engagement platforms. Their design is ready in `MARKETING_AI_PLAN_J_LAYER.md` and is re-planned after the manager review.
- **Message sending** of any kind, journeys, a CRM, a CDP or reverse-ETL engine.
- **Real-time decisioning**, a frontline lookup API, per-user reinforcement learning.
- **Multi-tenant SaaS.**
- **AWS Marketplace listing, FTR, DPDP evidence pack, industry packs.** These belong with going to market and come after the review.

---

## 2. What the product can do at the end of each phase

| Phase | A user can | We demonstrate | Exit gate |
|---|---|---|---|
| **1 — Results you can trust** | Fix the control group per customer across months and use cases. See what effect a campaign can detect at each holdout size, and what the holdout costs, before running it. Register the test plan. Record a campaign once and measure it through one path. | A 12-month synthetic replay where holdout membership never changes. The planner matching the closed-form formula. Nightly tests showing our intervals cover 95%. | M90–M95 merged; `make test-all` and the nightly statistical suite green; every defect in M91 has a regression test that fails on today's `main`. |
| **2 — Decide better** | Approve an uplift model only with a stability and beats-risk check. Download a treat list ranked by net value, with one offer and one channel per customer, one action per customer across use cases, and reasons in plain business words. | On planted data: high-value persuadables outrank cheap ones; the right offer is chosen per segment; an uplift model that does not beat risk ranking falls back to it; no customer gets two actions in one cycle. | M96–M101 merged; the multi-offer and arbitration journeys pass end to end on fixtures. |
| **3 — Prove better** | Measure revenue, not only yes/no, with CUPED shrinking the needed holdout. Upload any past randomised campaign and get an audit readout. Generate a Value Proof Pack for finance. See warnings for campaigns with no control, early looks, backfiring segments or fading effects. | A planted revenue effect recovered inside its interval with the expected variance reduction. A Proof Pack in which every number traces to a measured artefact. | M102–M105 merged; the Proof Pack provenance test green. |
| **4 — Learn, validate, demonstrate** | Run the monthly cycle on schedule from saved connections, with a person only approving. Learn the next model from the last cycle's holdout and explore slice. See the cost of a run before it starts. | The full journey on Hillstrom (and Criteo if obtained): plan, train, approve, value-weighted multi-offer list, measured result against the dataset's own randomised control, Proof Pack. A manager demo script that uses only these real results. | M106–M111 merged; validation results recorded in `docs/LIBRARY.md`; the demo rehearsed once end to end by someone outside the team. |

---

## 3. Principles and rules

### 3.1 Product principles

1. **Nothing fabricated, everywhere.** Every number shown traces to a computed artefact. A number that cannot be computed is null with a plain reason, never 0. The planted demo effect never appears outside demo docs (M95 enforces it).
2. **Opt-in, additive change.** New behaviour defaults to today's: `holdout.scope: run`, `explore_fraction: 0`, single treatment, no arbitration. Contracts only gain optional fields. Frozen files (`engine/stages/train.py`, `evaluate.py`, `explain.py`, the champion rule in `engine/registry.py`) stay frozen; new gates go beside them in `engine/model_gates.py` (new) and `engine/approvals.py`.
3. **Measure what happened, and say what was measured.** Intent-to-treat is the primary result. Every interval shown to a user has a nightly coverage test. "Causal" is claimed only for random assignment the engine made or verified.
4. **Money, not model scores, is the headline.** Rankings, recommendations and reports are in net INR ranges when value inputs exist, and say "not measured" with the reason when they do not.
5. **Config over code, for any industry.** Nothing under `engine/` branches on a use-case id (`tests/unit/test_no_use_case_branching.py`). Industry knowledge lives in configuration.
6. **Keep Plan H's simplicity.** The top bar stays Home, Connections, Results and Settings, and Home shows one generic journey. New screens live inside the existing flow (Setup, Run, Results) or under Settings.
7. **Prove it before showing it.** Phase 4 validates the whole journey on real randomised public data before any manager demo; no slide uses a synthetic number.

### 3.2 Parallel-work protocol rules that apply

- **Claim first.** M90 adds the row `DEC-1300…1399 | Plan J — product value first (M90–M111)` to `PARALLEL_WORK_PROTOCOL.md` §4 before the first DECISIONS entry. Each milestone records **one** DEC entry with lettered sub-decisions (a), (b), (c)…, so the plan fits the range.
- **Shared files.** M90 adds a `---- PLAN-J — append only below this line ----` / `---- END PLAN-J ----` block to the 12 shared files and appends `PLAN-J` to `PHASES` in `tests/unit/test_shared_file_markers.py`.
- **Stage modules.** Protocol §3 allows only Phase 2's six changes to Phase 1 stage code. M90's DEC amends §3 to name the stage functions Plan J may edit, and nothing else: `engine/stages/actions.py` `_control_mask`, `_entity_control_mask`, `_holdout_size`, `suppression_rules`; `engine/stages/export.py` `_suppression_counts`. Under default configuration, behaviour stays byte-identical, pinned by the existing tests. This needs the protocol owner's sign-off before M92 starts.
- **In-place declarations.** A pydantic field cannot be added from a block at the foot of a file, so Plan J follows the Plan G / Phase 3b pattern: one defaulted declaration in place, typed in a Plan J module, listed in the milestone and announced in `docs/CROSS_BRANCH_REQUESTS.md` the day it lands.
- **Ownership.** Plan J owns `engine/holdout/**`, `engine/measurement/**`, `engine/decide/**`, `api/routes/{campaigns,holdout,measurement}.py`, `ui/modules/decide/**`, `configs/decide/**`, `tests/**/{holdout,measurement,decide}/**`, `tests/statistical/**`, `docs/DECIDE.md`. Edits elsewhere are listed per milestone as pre-approved, backward-compatible edits in the owning workstream's area.
- **Error codes.** `engine/decide/codes.py` exports `PLAN_J_CODES`. M90 makes one pre-approved edit in Plan E's `engine/pilot/help.py` so `known_codes()` also returns them; each milestone adds its codes there and to `configs/pilot/help.yaml` in the same change, so `tests/unit/pilot/test_help.py` stays green unchanged.
- **Every external call sits behind a protocol with a fake;** the fast suite needs no network.
- **Every new route** has a `RoutePolicy`; row-level reads are audited; customer ids go in request bodies, never URLs (DEC-746).
- **Every new row-level artefact** is registered in `configs/privacy.yaml` and `engine/privacy/layout.py` in the same change.
- **New platform tables** are SQLModel tables appended to `PLATFORM_TABLES` with an Alembic migration, in order: 0006 (M94), 0007 (M99).
- **`make lint test` before every commit, `make test-all` before every merge.** Never skip, delete or loosen a test.
- **Docs move with code.** `docs/API.md` is regenerated whenever a model changes; `docs/UPLIFT.md` and `docs/DATA_CONTRACT.md` change in the same commit as the behaviour they describe. M90 adds README rows (pending) for M90–M111; each milestone adds its `MILESTONE_TESTS` entry with its tests.

---

## 4. Phases

**Effort basis.** 4 engineers and 1 data scientist. Engineering keeps back 0.75 person-weeks a week for
reviews, support and fixes, leaving **3.25 engineer-weeks of milestone work per week**; the data scientist
keeps back 0.2, leaving **0.8**. Each figure includes tests, docs, the DEC entry and shared-file paperwork.
Effort figures are person-weeks; "DS n" is the data-science share inside the figure. All estimates are ours and unmeasured.

### Phase 1 — Results you can trust (gate at week 6)

**Goal.** Make every result the product reports defensible, before adding anything that decides more.

#### M90 — Plan J set-up

- **Scope.** Claim DEC-1300…1399 in protocol §4; add the PLAN-J shared-file blocks and `PHASES` entry; the amendment of protocol §3 (DEC-1300 (c)) naming the stage functions above; `engine/decide/codes.py` with `PLAN_J_CODES` and the `known_codes()` hook; the `statistical` pytest marker, kept out of `make test` and `make test-all` without editing either target (protocol §4 forbids it): `tests/statistical/conftest.py` sets `collect_ignore_glob = ["test_*.py"]` unless `MARKETING_AI_STATISTICAL=1`, so those runs never collect the suite and nothing is reported as skipped; a `make test-statistical` target in the Makefile's PLAN-J block sets the variable and runs `-m statistical`; one job of `.github/workflows/nightly.yml` runs it; a trivial `tests/statistical/test_harness_smoke.py` and a unit test prove the mechanism (DEC-1300 (e)); README rows for M90–M111; `docs/DECIDE.md` skeleton; record the missing PLAN-I shared-file markers in `docs/CROSS_BRANCH_REQUESTS.md`.
- **Acceptance.** `tests/unit/test_shared_file_markers.py` green with PLAN-J; `check_readme` green; the new marker excluded from the fast suite.
- **Effort:** 1.5 engineer-weeks. **Depends on:** —.

#### M91 — Fix what the research and the code review found

- **Why.** Each of these was confirmed against the code during the research:
  - **Uplift runs skip the consent ledger.** `UpliftScoreFlow._actions` (`engine/uplift/flow.py`) overrides the method that `engine/pipeline.py` rebinds to `_consent_gated_actions`, so uplift runs never consult the ledger and write no `consent_report.json`.
  - **Four contacting use cases have no consent purpose.** `configs/privacy.yaml` `use_case_purposes` omits `retail-win-back`, `bank-term-deposit`, `insurance-cross-sell` and `card-default-propensity`.
  - **The SMS opt-out line does not work in India.** "Reply STOP to opt out" (`engine/config.py:995`, `configs/engine.yaml:255`) is impossible with one-way sender IDs.
  - **Copy approval records the wrong person:** it stores `body.approved_by` instead of the signed-in principal.
  - **Row-level contact lists are downloadable by Viewers without an audit trail:** `GET /runs/{run_id}/artefacts/{name}` serves `scores.csv`, `scores.parquet` and `copy_messages.csv` to Viewers (`api/access_policy.py`), and `read_scores` and `read_copy_messages` go through it.
- **Scope.**
  - `UpliftScoreFlow._actions` calls the existing `consent_gate_for_run` and `apply_consent_gate` (`engine/privacy/consent.py`) before `apply_uplift_actions` (pre-approved, Phase 3b area).
  - Add the four purposes (`marketing_communication`; `card-default-propensity` → `account_servicing`) and a test that fails when any use case with `actions.contacts_customers: true` lacks one (`CONSENT_PURPOSE_MISSING` at config load).
  - Add `generative.campaign_copy.sms_sender: one_way | two_way` (default `two_way`). With `one_way`, the SMS required line is an `{{opt_out_link}}` field (`OPT_OUT_LINK_FIELD` beside `UNSUBSCRIBE_FIELD` in `engine/generative/win_back.py`, in `_RESERVED_FIELDS`, returned by `allowed_placeholder_fields`), and a guardrail rule `sms_reply_stop_one_way` in `configs/guardrails.yaml` blocks "Reply STOP".
  - Copy approval takes the identity from the principal.
  - Row-level downloads (`scores.csv`, `scores.parquet`, `copy_messages.csv`, and M98's `treat_list.csv`) require Analyst when sign-in is on, and are audited (`audit_reads=True`). The check sits in `api/routes/runs.py::read_artefact`, which both download routes already call.
- **Where in the code.** `engine/uplift/flow.py`, `engine/privacy/{consent,config}.py`, `configs/privacy.yaml`, `engine/config.py` (`CampaignCopyConfig.sms_sender`), `configs/guardrails.yaml`, `engine/generative/win_back.py`, `api/routes/{generative,runs}.py`, `api/access_policy.py`.
- **Acceptance.**
  - An uplift scoring run with a `withdrawn` ledger record suppresses that customer as `consent_false` and writes `consent_report.json`; this test **fails on today's `main`**. Propensity and uplift runs suppress the same rows on the same input.
  - Removing a contacting use case's purpose fails the coverage test.
  - With `one_way`, copy containing "Reply STOP" is blocked and a template ending in `{{opt_out_link}}` renders; with the default, every existing generative test passes unchanged.
  - A Viewer gets 403 on every row-level download route when sign-in is on; each Analyst download writes one audit event; with sign-in off, behaviour is unchanged.
- **Effort:** 2 engineer-weeks. **Depends on:** M90.

#### M92 — Persistent holdout and explore slice

- **Why.** Today the control group is salted with `seed_from(run_id)` and drawn among eligible rows (`engine/stages/actions.py` `_control_mask`, `_entity_control_mask`). Monthly re-scoring reshuffles who is held out, the holdout differs between use cases, and membership moves when eligibility changes. Results therefore cannot be added up over months or across use cases. And without some randomised treatment outside the target, the next model cannot learn uplift there.
- **Scope.**
  - New package `engine/holdout/`. Membership is a threshold rule, decided per customer over the whole population, independent of run, eligibility and row order: `int(sha256(f"{salt}:{scope_key}:{entity}")[:16], 16) < fraction × 2^64`. Smaller fractions nest inside larger ones (5% ⊂ 10%).
  - Scopes: `run` (default, today's rule), `use_case`, `universal`.
  - **Fraction.** The existing `actions.control_group_fraction` keeps its meaning under `scope: run`. Under a persistent scope, `actions.holdout.fraction` is required and wins. `measure_offered`, `engine/scheduling/outcomes.py` and the help entry read the effective fraction through one helper, `effective_holdout_fraction(actions)`.
  - **Salt.** `MARKETING_AI_HOLDOUT_SALT` (a secret setting, hidden by `redacted()`), fingerprinted in `platform_setting` like `privacy_salt`. A missing salt with a persistent scope raises `HOLDOUT_SALT_MISSING`; a changed fingerprint refuses scoring with `HOLDOUT_SALT_CHANGED`.
  - **Epochs.** Raising the fraction keeps every member; lowering it or rotating the salt starts a new epoch (Admin only, audited).
  - **Explore slice.** `actions.explore_fraction` (0–0.10, default 0) marks, with a second salt label, rows that are eligible, not held out, not selected and not a predicted sleeping dog. The per-row treatment probability is recorded for off-policy evaluation (`engine/uplift/ope.py`).
  - Each scoring run writes `holdout_assignment.parquet` (key, holdout member, explore, explore probability) for every row, suppressed rows included.
  - `GET /holdout` (Viewer) and `PUT /holdout` (Admin, audited); a Settings entry, "Holdout".
- **Where in the code.** `engine/holdout/{spec,assign,salt}.py` (new); `engine/stages/actions.py` `_control_mask`, `_entity_control_mask`, `_holdout_size` (protocol amendment, M90); `engine/config.py` (`ActionsConfig.holdout`, `explore_fraction`); `engine/settings.py` (`holdout_salt`, `SECRET_FIELDS`); `infra/naming.py` (settings mirror); `engine/uplift/measure.py` `measure_offered`; `api/routes/holdout.py` (new); `docs/UPLIFT.md` §14.
- **Acceptance.**
  - **Stability:** on 10,000 keys with universal scope, membership is identical across 12 synthetic monthly runs, 2 use cases, 20% of rows becoming suppressed and shuffled row order.
  - **Share and nesting:** on 1,000,000 keys the realised share is within binomial 99.9% bounds; 5% members are a strict subset of 10% members.
  - **Default unchanged:** with `scope: run`, every test in `tests/unit/test_actions.py` (including `test_a_different_run_id_draws_a_different_holdout`) and `tests/unit/uplift/test_profit_curve.py` passes unchanged.
  - No sleeping dog is ever treated, explore rows included.
- **Effort:** 3 engineer-weeks (DS 0.5). **Depends on:** M90.

#### M93 — Plan the test, define the outcome well

- **Why.** Realistic effects are 1–3 points of churn or conversion. An underpowered test gives a range that crosses zero even when the campaign worked, and reads as failure. A wrong outcome definition is the most likely silent failure; labels have no grace window today, and "lapse" outcomes (no purchase, renewal or recharge within N days) exist in every industry. Uplift also needs randomised history the user may not have.
- **Scope.**
  - **Planner** (`engine/measurement/planner.py`, pure, `statistics.NormalDist` as `engine/uplift/incrementality.py` uses): `mde_two_proportions`, `n_for_mde`, `holdout_for_mde`, `achieved_power`, `cost_of_holdout`, `cost_of_explore` (Money ranges, or null with a reason). `POST /measurement/power-preview` (Viewer; counts only, no customer data).
  - **"Can we measure it?"** on the readiness report (`engine/pilot/readiness.py`): the detectable effect at 3/5/10/15% holdouts from the dataset's counts and base rate.
  - **Lapse outcomes:** optional `LabelDefinition.grace_days` and `exclude_roles`, compiled in `engine/onboarding/labels.py`, keeping `future_window_clause` and `LABEL_WINDOW_NOT_ENFORCED`. Label stability per month (`LABEL_RATE_UNSTABLE`).
  - **Treatment-history check:** on a treatment column the user names, `engine/uplift/checks.treatment_predictability` reports random, model-selected or unknown (`TREATMENT_HISTORY_NOT_RANDOM`), with a sufficiency verdict against `uplift.min_arm_rows` and `min_arm_positives`: "uplift now", or "propensity + random control (+ explore) first, uplift from the next cycle".
  - **Neutral defaults:** the data request's default use case and the demo name ("Demo Company") stop assuming telecom.
- **Where in the code.** `engine/measurement/{__init__,planner}.py` (new), `api/routes/measurement.py` (new), `engine/pilot/{readiness,demo}.py`, `engine/config.py` (`LabelDefinition`), `engine/onboarding/labels.py`, `engine/uplift/checks.py`, `engine/contracts.py` (`CHECK_CODE_TABLES`), `configs/pilot/data_request.yaml`, `scripts/seed_demo.py`.
- **Acceptance.**
  - The planner matches the closed-form two-proportion formula within 1%: at 80% power and two-sided 95%, 4% → 3% needs about 5,300 per arm; 10% → 8% about 3,200.
  - A 30-day lapse label with a 7-day grace period gives the hand-computed positive count on a golden file; a feature inside the grace window is caught as `FUTURE_EVENTS_LEAKED`.
  - Model-selected treatment is reported "not random"; a randomised fixture "random"; the sufficiency verdict switches exactly at the thresholds.
  - `make pilot-check` is green with no telecom default; `jargon_in` finds nothing in any planner or readiness message.
- **Effort:** 4 engineer-weeks (DS 1.5). **Depends on:** M90.

#### M94 — One campaign record, one measurement path, a registered test plan

- **Why.** Measurement is tied to our own scoring run today (`RUN_NOT_SCORED`), and three routes compute it (`POST /runs/{id}/campaign-results`, `POST /runs/{id}/measure`, M49's `/outcomes`). Audits, the Proof Pack, revenue outcomes and learning all need one object to attach to. Peeking and moving goalposts turn a real effect into a disputed one, so the plan must be fixed before the campaign runs.
- **Scope.**
  - **Campaign store** (`engine/measurement/campaign.py`): `Campaign` with kinds `scored` (from one of our scoring runs) and `external` (M103), a `CampaignStore` protocol with SQL and in-memory implementations (alembic `0006_campaigns.py`). Artefacts under `campaigns/<id>/`: `assignment.parquet` (key, arm, intended flag, band or segment, explore), outcomes, `incrementality_report.json`, `test_plan.json`.
  - **One measure function:** `engine/measurement/measure.py::measure_campaign(assignment, outcomes, *, run_id, primary_key, outcome_column, positive_label=None, intended_column=None, bands=None, treatment_time, treatment_date_column=None, outcome_window_days=None, as_of, campaign_id=None, plan=None) -> IncrementalityReport`, a pure wrapper over `measure_incrementality` and `campaign_verdict`. `as_of` always comes from the caller. `create_campaign_results` (`api/routes/uplift.py`) delegates to it, and `POST /runs/{id}/measure` keeps its behaviour; `/outcomes` stays model monitoring only.
  - **Treatment time** is entered when the campaign is created (the date it actually went out), defaulting to the scoring run's `finished_at`, so maturity dates are right when a campaign launches days later.
  - **Test plan:** `GET /campaigns/{id}/plan-preview` (computed points from M93) and `POST /campaigns/{id}/plan` (Analyst) freeze a `TestPlan` (metric, outcome kind, optional covariate, holdout, analysis date, detectable effect) with a `plan_hash` recorded as the audit event's `after_hash`. Identical re-POST returns the stored plan; a different body is `409 TEST_PLAN_EXISTS`; changes go through `POST /campaigns/{id}/plan/amendments` (a new version, both kept). At measurement, differences from the plan give `TEST_PLAN_CHANGED`; a read before `analysis_date` is labelled "early look" with no final verdict; `PLAN_UNDERPOWERED` is a warning, never a block.
  - **Results** lists campaigns beside runs (`ui/modules/simple/pages.js`, pre-approved); the "Plan the test" card is on the campaign page with a slider over computed points only (DEC-1204).
- **Where in the code.** `engine/measurement/{campaign,measure,plan}.py` (new), `api/routes/campaigns.py` (new), `api/routes/{uplift,measure}.py` (delegate), `engine/pilot/roi.py` (reads the campaign report first), `engine/uplift/contracts.py` (`IncrementalityReport` + `test_plan_hash`, `early_look`, additive), `engine/audit/events.py` `DETAIL_KEYS` (+ `plan_hash`, reviewed), `engine/platform_db.py`, `ui/modules/decide/plan.js` (new).
- **Acceptance.**
  - `tests/integration/uplift/test_measure_campaign.py` and `test_campaign_results_phase1.py` pass unchanged.
  - On an uplift run with every row mature and a fixed `as_of`, the campaign route and `/runs/{id}/campaign-results` give identical reports except `computed_at` and `campaign_id`; on a propensity run, the campaign route equals `/campaign-results` called with the campaign's treat bands.
  - Outcomes before maturity give `CAMPAIGN_NOT_MATURED` with a date, never a partial number.
  - The audit event's `after_hash` equals `content_hash(plan)`; measuring before the analysis date shows "early look" and no verdict.
  - Erasure removes a principal from `campaigns/`.
- **Effort:** 4.5 engineer-weeks (DS 1). **Depends on:** M90, M92, M93.

#### M95 — Validity harness and synthetic quarantine

- **Why.** All validation so far is synthetic, the demo's 12.7-point effect is planted, and the Criteo run could not be done from our environment (`docs/LIBRARY.md`). Before anyone sees a result, we must show that our 95% intervals really cover 95%, and that a planted number can never be presented as a result.
- **Scope.**
  - `engine/measurement/simulate.py`: populations with a known effect, base rate and share of immature outcomes (M102 adds continuous outcomes, M100 several arms).
  - `tests/statistical/` (marker `statistical`, nightly, fixed seeds): interval coverage, false-positive rate at zero effect, bias when immature rows are excluded, achieved power at the planner's n. Bands at ±4 Monte Carlo standard errors.
  - **Quarantine:** `RunRecord.synthetic` (set by `scripts/seed_demo.py`, `engine/pilot/demo.py` and synthetic uploads); `ReportDocument.synthetic` draws a "Synthetic data: planted effect, not a forecast" block in `engine/pilot/document.py`; `test_docs_honesty` allows "12.7 points" only in demo docs.
- **Where in the code.** `engine/measurement/simulate.py` (new), `tests/statistical/` (new), `engine/contracts.py` (`RunRecord.synthetic`), `engine/pilot/{demo,document,results,roi}.py`, `scripts/seed_demo.py`, `tests/unit/test_docs_honesty.py`, `.github/workflows/nightly.yml` (the `statistical` job, added by M90), `Makefile` (the `test-statistical` target in the PLAN-J block, added by M90; `test` and `test-all` are not edited).
- **Acceptance.** Coverage within ±4 standard errors of 95% (2,000 simulations) at base rates 2%, 5% and 20%; false-positive rate within ±4 standard errors of 5% (10,000 simulations); achieved power within band of 0.80 or the planner is corrected; reports from a seeded demo run show the synthetic block in HTML and PDF.
- **Effort:** 3 person-weeks (DS 2). **Depends on:** M93.

**Phase 1 exit gate.** M90–M95 merged; `make lint test`, `make test-all` and the nightly statistical suite green; each M91 defect has a regression test that fails on the commit before its fix; a 12-month replay on synthetic data shows a stable universal holdout.

---

### Phase 2 — Decide better (gate at week 13)

**Goal.** Make the list itself better: approved only when it beats the simple alternative, ranked by
money, with the right offer, channel and reason, and one action per customer.

#### M96 — Uplift must earn its place: stability, calibration and the beats-risk check

- **Why.** Approval rests on one hold-out split today, with no fold variance, no comparison with plain risk ranking and no calibration check, and uplift models are known to swing across folds. The research's strongest warning is that uplift is worth using only where it beats risk targeting at the same budget; we must compute that, out of sample, not assume it.
- **Scope.**
  - `UpliftEvaluation` gains optional fields: `baseline_comparison` (AUUC when ranking by `p_control`, by `p_treated` and by the use case's propensity model on the same hold-out, with a paired bootstrap of the difference reusing `engine/uplift/metrics.py`'s bootstrap); `calibration_by_decile` (predicted against observed uplift); `fold_auuc` (LightGBM base learner, off by default, cost shown before it runs).
  - `engine/model_gates.py` (new) turns these into advisory checks on the Approver screen (`ApprovalItem.checks`). `should_promote` and the champion tests are unchanged.
  - **Ranking choice (J5):** a scoring run whose uplift champion does not beat risk ranking (`ci_low ≤ 0` on the paired difference) ranks its treat list by the use case's last approved propensity model, and says so on the Output page and in the treat list (`UPLIFT_NOT_BETTER_THAN_RISK`).
  - **Equal-budget comparison:** uplift top-N against risk top-N at equal budget, by incremental conversions per rupee with intervals, through `engine/uplift/ope.py` `evaluate_policy`, cross-fitted on randomised rows (holdout plus explore) so no row is scored by a model trained on it.
- **Where in the code.** `engine/uplift/{metrics,flow,contracts,ope}.py` (pre-approved, Phase 3b area), `engine/model_gates.py` (new), `engine/approvals.py` (`ApprovalItem.checks`), `engine/measurement/compare.py` (new), `ui/modules/production/approvals.js`, `docs/UPLIFT.md`.
- **Acceptance.** On a fixture where uplift equals minus risk, the treat list falls back to the propensity ranking with the reason; on a heterogeneous-effect fixture it uses uplift; on an equal-effect fixture the interval covers 0 and the screen says "does not beat risk ranking"; the cross-fit never scores a row with a model trained on it; every existing champion and approval test passes unchanged; the comparison interval meets its nightly coverage band.
- **Effort:** 4 engineer-weeks (DS 2). **Depends on:** M92, M95.

#### M97 — Rank by net value

- **Why.** The best-evidenced gap in the research: ranking by expected net money, not by uplift alone, gave much higher profit (Lemmens & Gupta 2020, offline evaluation; Gubela & Lessmann 2021). Today the list optimises heads, and the uplift policy and the ROI report use two value models that can disagree. Channel costs differ by several times (WhatsApp marketing vs utility messages in India, about 6.6×).
- **Scope.**
  - `UpliftPolicyConfig` gains optional `value_column` (order value, premium, balance, ARPU), `horizon_months`, `margin_pct`, `min_roi`; a `value` block in configuration gives offer cost and per-channel contact cost (M99's catalogue takes these over when present).
  - **Net value per customer** = uplift × value × margin − offer cost × p_treated − contact cost. `ranking`, `choose_contacts`, `recommend_policy` and `profit_curve` (`engine/uplift/policy.py`) take the vector; the persuadable-only and sleeping-dog guards stay.
  - The hold-out file (`HOLDOUT_COLUMNS` in `engine/uplift/flow.py`, `HoldoutUplift` in `engine/uplift/metrics.py`) carries the value column, so expected value is a value-weighted observed uplift.
  - Propensity runs get an optional `expected_gross_value` (p × value − cost), labelled "not incremental" everywhere.
  - One value block feeds both the policy and the `RoiInputs` defaults (`engine/pilot/roi.py`); India channel-cost defaults in `configs/pilot/value.yaml`, labelled as editable defaults.
  - Guided setup proposes a value-like column as a "check" suggestion (`engine/agent/recommend.py`).
- **Where in the code.** `engine/uplift/{config,policy,flow,metrics}.py` (pre-approved), `engine/pilot/roi.py`, `configs/pilot/value.yaml` (new), `engine/agent/recommend.py`, `tests/fixtures/agent_bench/expected.json`, `ui/modules/uplift/views.js` `profitCard`, `docs/UPLIFT.md` §14.
- **Acceptance.** The profit-curve identity in `tests/unit/uplift/test_profit_curve.py` holds with and without a value column (the file is extended, never loosened); a constant value column reproduces the current path exactly; on a fixture where high-value persuadables have lower raw uplift, they rank higher; `min_roi` is respected; money fields are null with a reason when value inputs are missing.
- **Effort:** 3 engineer-weeks (DS 1). **Depends on:** M90.

#### M98 — The treat list: one file with everything, reasons in business words

- **Why.** Until integration, the downloaded file **is** the hand-off, so it must carry everything a campaign manager needs. Today `scores.csv` carries a band, an action label and `reason_1..n` that read like feature names; buyers repeatedly say they distrust black-box AI and want reasons they can act on.
- **Scope.**
  - A new run artefact `treat_list.csv` (and `.parquet`), written beside `scores.csv` by a pure builder over a finished scoring run's artefacts (no change to `scores_csv_columns`): customer key, use case, model version, band or segment, `treat` flag, `holdout` flag, `explore` flag, suppression reason, offer and channel (from M99/M100 when present, else the band's action), net value (M97, when present), and up to three **business-language reasons**.
  - **Business reasons:** `configs/decide/reasons.yaml` maps feature names and value directions to phrases ("Spent less each month for 3 months", "Raised two complaints in 30 days"); unmapped features keep today's text. Guided setup proposes phrases for new features as "check" suggestions.
  - The Output page shows the treat list summary and a **Download treat list** button (Analyst, audited per M91), with a one-line "how to use it in your tool" note: include `treat = 1`, exclude `holdout = 1`.
- **Where in the code.** `engine/decide/{treat_list,reasons}.py` (new), `configs/decide/reasons.yaml` (new), `engine/stages/explain.py` (read only; frozen), `engine/agent/recommend.py`, `api/routes/runs.py` (artefact registration), `ui/modules/decide/` (new), `configs/privacy.yaml`, `engine/privacy/layout.py`.
- **Acceptance.** A golden treat list per fixture (propensity, uplift, with and without value and offers); every row's holdout flag equals `holdout_assignment.parquet`; a mapped feature renders its phrase, an unmapped one today's text; `jargon_in` finds nothing in the phrases; the download is refused to a Viewer when sign-in is on.
- **Effort:** 3 engineer-weeks. **Depends on:** M91, M92.

#### M99 — Offer and channel catalogue, channel-aware consent

- **Why.** Today one consent flag covers every channel and the action is free text. Users need consent per channel (opted out of SMS, in for email), and each offer needs its channel, cost and, for SMS in India, its registered template. Value ranking (M97) and offer choice (M100) need offer and channel costs in one place.
- **Scope.**
  - `configs/decide/catalogue.yaml`, validated by a frozen `ActionCatalogue` model (the `privacy.yaml` pattern): action id, label, channel, offer cost, contact cost, eligibility, `requires` fields. `catalogue_sha256` is stamped on every treat list.
  - Use cases reference action ids additively (`Band.action_id`, `uplift.policy.treat_action_id`); the free-text `Band.action` stays as the label.
  - `SuppressionConfig.channels {channel: {consent_column, contactable_column}}`; per-channel counts in an optional `SuppressionCount.channel_counts`; the 3-value `reason` and DEC-A2 precedence unchanged. A row is treated only on a channel it is contactable on; the treat list carries `contactable_channels` and the channel used.
  - Consent ledger: nullable `consent_record.channel` (null = all channels; alembic `0007_consent_channel.py`), `ConsentLedger.classify(..., channel=None)`.
  - **Region rule, data-driven:** `configs/regions/in.yaml` declares `sms_requires: [dlt_template_id, message_category]`; catalogue validation applies the configured region's list without naming any region in code.
- **Where in the code.** `engine/decide/catalogue.py` (new), `configs/decide/catalogue.yaml` (new), `configs/regions/in.yaml` (new), `engine/config.py` (`Band.action_id`, `SuppressionConfig.channels`), `engine/stages/actions.py` `suppression_rules` and `engine/stages/export.py` `_suppression_counts` (protocol amendment), `engine/contracts.py` (`SuppressionCount.channel_counts`), `engine/privacy/{consent,contracts,tables}.py`, `engine/platform_db.py`.
- **Acceptance.** A customer opted out of SMS but in for email, with both planned, is treated only by email; a customer whose only planned channel is SMS and who opted out of it is not treated and is counted in `channel_counts`; existing consent records apply to all channels and `test_platform_migration.py` is green; an unknown action id fails config load; every existing suppression test passes unchanged.
- **Effort:** 3.5 engineer-weeks. **Depends on:** M91, M97.

#### M100 — Choose the offer: multi-treatment uplift

- **Why.** Every incumbent decides **which** offer, channel or message, not just whether to contact (Pega's next-best-action, OfferFit/BrazeAI's offer selection). Our uplift is binary only (`_validated` in `engine/uplift/learners.py` enforces 0/1), and DEC-668 already recorded how to extend it without breaking any reader.
- **Scope** (the DEC-668 path):
  - `UpliftConfig.treatment_levels` (control value first). With one level, today's rules hold; with several, `TREATMENT_NOT_BINARY` becomes "a value outside the configured levels".
  - **Learning:** one uplift model per arm against the shared control (T- and X-learners per arm; S-learner with the arm as a feature), reusing `engine/uplift/learners.py`; per-arm checks (`TREATMENT_ARM_TOO_SMALL`, the randomness test per arm).
  - **Evaluation:** per-arm Qini/AUUC with intervals in an `arms: tuple[ArmSummary, ...]` field on the evaluation, segments, policy and incrementality reports; every existing field keeps its meaning as the first treatment against control.
  - **Choice:** per customer, the arm with the highest net value (M97) among the arms they are eligible and contactable for (M99), or **no offer** when every arm's net value is below zero or the customer is a sleeping dog for every arm; under a total budget, a greedy choice by net value per rupee. The treat list carries the chosen offer and the runner-up's value.
  - **Measurement:** `measure_campaign` reports each arm against the shared control.
  - **The champion rule** compares multi-arm models by the value-weighted policy's out-of-sample value with a paired bootstrap (sub-decision of the DEC); until that DEC is recorded, a multi-arm run is refused, as DEC-668 requires.
- **Where in the code.** `engine/uplift/{config,data,checks,learners,metrics,segments,policy,actions,flow,contracts,incrementality}.py` (pre-approved, Phase 3b area), `engine/decide/offer_choice.py` (new), `engine/measurement/measure.py`, `ui/modules/uplift/views.js` (per-arm cards), `docs/UPLIFT.md`.
- **Acceptance.**
  - With one treatment level, every existing uplift test passes unchanged and every artefact is byte-identical.
  - On a planted three-arm fixture (two offers and control, different segments responding to different offers), each segment gets its responding offer, sleeping dogs get no offer, and per-arm effects are recovered inside their intervals.
  - Budget is respected; a customer never gets an offer on a channel they are not contactable on.
  - Nightly per-arm interval coverage meets the M95 band.
- **Effort:** 6 engineer-weeks (DS 2). **Depends on:** M96, M97, M99.

#### M101 — One action per customer across use cases

- **Why.** Each use case decides alone today. The same customer can be on the churn list, the payment-reminder list and the win-back list in the same week, and the only safeguard is a "recently contacted" rule. The research found that "did another campaign reach them first?" is one of the questions that decides whether AI marketing makes money, and Pega's core idea is to arbitrate between everything a customer qualifies for.
- **Scope.**
  - `POST /decide/arbitrate` (Analyst) takes the latest scoring run of each selected use case over the same customer key and writes `arbitrated_treat_list.csv`: one action per customer per cycle, chosen by **priority weight × net value** (`configs/decide/arbitration.yaml`: per use case priority, per customer contact cap per period, per channel cap). Each row says which action won and which lost.
  - Holdout members stay untouched in every use case (universal scope recommended); explore rows keep their randomisation.
  - A conflicts summary on Results: how many customers qualified for more than one action, and what was dropped.
  - Measurement of an arbitrated cycle creates one campaign per use case from the winning rows, so each use case's effect stays measurable.
- **Where in the code.** `engine/decide/arbitrate.py` (new), `configs/decide/arbitration.yaml` (new), `api/routes/campaigns.py` (or `decide.py`), `ui/modules/decide/`, `engine/measurement/campaign.py`.
- **Acceptance.** On a fixture with three overlapping use cases, no customer has two actions; the winner has the highest priority × value; contact caps hold; holdout members are never treated; each use case's campaign measures only its winning rows; with one use case selected, the output equals that use case's treat list.
- **Effort:** 3.5 engineer-weeks (DS 0.5). **Depends on:** M94, M97, M98.

**Phase 2 exit gate.** M96–M101 merged and `make test-all` green; end-to-end on fixtures: approve with
checks → value-ranked, offer-chosen, channel-aware treat list → arbitration across three use cases →
campaigns created and measured.

---

### Phase 3 — Prove better (gate at week 15)

**Goal.** Make the proof stronger, cheaper and readable by finance.

#### M102 — Revenue outcomes and CUPED

- **Why.** Binary effects of 1–3 points are hard to detect, and campaign owners are judged on revenue. Today a continuous outcome reads "No outcomes have been recorded" in `engine/pilot/roi.py`. CUPED (adjusting for each customer's pre-period behaviour) narrows ranges on continuous metrics, so a smaller holdout detects the same effect; it is standard at Microsoft, Booking and Netflix.
- **Scope.** `measure_incrementality` takes `outcome_kind: binary | continuous` (Welch interval on the difference in means); CUPED through an optional `covariate_column`, read point-in-time, with `adjusted_lift`, `adjusted_interval` and `variance_reduction` (additive), run only when the test plan pre-registered the covariate (`TEST_PLAN_CHANGED` otherwise); the planner gains a continuous detectable effect with an expected ρ²; `compute_roi` gets a continuous branch.
- **Where in the code.** `engine/uplift/{incrementality,contracts}.py` (pre-approved, additive), `engine/measurement/{planner,measure,simulate}.py`, `engine/pilot/roi.py`, `api/routes/{measure,campaigns}.py`, `tests/statistical/`, `docs/UPLIFT.md` §15.
- **Acceptance.** Nightly coverage meets the M95 band; with ρ = 0.6, `variance_reduction` is within ±0.03 of 0.36; an uncorrelated covariate gives about the unadjusted result; on a binary outcome every existing field is identical and every new field null.
- **Effort:** 3.5 engineer-weeks (DS 1.5). **Depends on:** M94, M95.

#### M103 — Audit any campaign, and the programme readout

- **Why.** The fastest route to "net value proven against a control" is measuring a campaign that already happened: a prospect's past randomised campaign gets a readout within days. It needs no integration: the user uploads who was in which group and what happened. It also lets us measure the whole programme against the universal holdout.
- **Scope.**
  - `POST /campaigns/audit` (Analyst): an assignment upload (key, arm 0/1 or offer level, optional sent date, optional intended flag), an outcomes upload, column mappings. It creates an `external` campaign and calls `measure_campaign` unchanged; `run_id` carries the campaign id and no run record is written.
  - **Causal labels:** `causal=true` only when the engine made the assignment or verified it random (`checks.treatment_predictability` when features are present); "random by the user's statement, not verified" otherwise; non-random assignment gets "descriptive only".
  - **Programme readout:** `POST /campaigns/programme {period, outcome}` measures all non-holdout customers against universal-holdout members (ITT, CUPED once M102 lands).
  - **Who was actually contacted (optional):** an uploaded contact file (key, contacted yes/no) gives contact rate and holdout contamination for any campaign, with a complier-adjusted effect labelled as secondary. This is the manual version of the send-log reconciliation the integration phase will automate.
- **Where in the code.** `engine/measurement/{audit,reconcile}.py` (new), `api/routes/campaigns.py`, `engine/uplift/checks.py` (reused), `engine/pilot/roi.py`, `ui/modules/decide/audit.js`, `ui/modules/simple/pages.js`.
- **Acceptance.** An assignment and outcomes built from an existing propensity scoring run reproduce its report field for field (except `run_id`, `campaign_id`, `computed_at`); a planted randomised file recovers its effect inside the interval; a propensity-assigned file is labelled not causal; a programme readout recovers a simulated programme effect; injected contamination and contact rates are reported exactly.
- **Effort:** 3.5 engineer-weeks (DS 0.5). **Depends on:** M94, M92.

#### M104 — The Value Proof Pack

- **Why.** CFOs are the most sceptical buyers, and "naive against incremental" is the most persuasive single view. Every number in the pack must trace to a measured artefact, because it is what converts a pilot and what any outcome fee would be settled on.
- **Scope.** `engine/pilot/proof.py::build_proof(campaign_id)`, served at `GET /pilot/proof/{campaign_id}?format=html|pdf|json` (Viewer), composing the registered plan, the incrementality report, the contact file (if any), segments and value inputs, plus a new `campaigns/<id>/segment_effects.json` (measured effect and interval per band, segment and offer, written by `measure_campaign`). Sections:
  1. The plan as registered against what ran.
  2. Whether the list ran (contact rate and contamination, when a contact file exists; otherwise "not measured").
  3. Incremental outcomes with 95% ranges (per offer when several).
  4. Gross against incremental.
  5. Naive credit against measured credit.
  6. Offer money spent on sure things and sleeping dogs (uplift runs).
  7. The cost of the holdout and the explore slice.
  8. Backfire: any segment whose interval lies entirely below zero, with a "suppress next cycle" suggestion an Analyst approves.
  9. Net INR as a 95% range.
  10. Method and limits, including the causal basis.

  Synthetic runs are refused (`PROOF_SYNTHETIC_DATA`); immature ones too (`PROOF_NOT_MATURE`).
- **Where in the code.** `engine/pilot/proof.py` (new; pre-approved in Plan E's area), `engine/measurement/measure.py`, `engine/pilot/{document,roi,plain}.py`, `api/routes/pilot.py`, `configs/privacy.yaml`, `engine/privacy/layout.py`, `ui/modules/pilot/screen.js`, `ui/modules/simple/pages.js`.
- **Acceptance.** A provenance test resolves every number in `ProofView` to an artefact field, and the build fails on any it cannot; a missing artefact renders "not measured" with its reason; a planted negative segment is flagged as backfire and a neutral one is not; naive value ≥ measured value on the planted fixture; HTML and PDF render, `jargon_in` finds nothing, and the pack holds no row-level data.
- **Effort:** 4 engineer-weeks (DS 1.5). **Depends on:** M94, M95, M102, M103.

#### M105 — Warnings and proven value to date

- **Why.** Users need the product to tell them what needs attention, the way Optimove's insight cards do, and a running total of value proven.
- **Scope.** Cards computed from existing artefacts only: campaign with no control; early look; underpowered plan; contamination; drift; challenger ready; backfire segment; uplift did not beat risk; uplift fading (measured effect falling across cycles). A Results summary of value proven to date, as the sum of measured **lower bounds**, labelled as such.
- **Where in the code.** `engine/measurement/summary.py` (new), `api/routes/campaigns.py` (`/campaigns/summary` declared before `/campaigns/{id}`), `ui/modules/simple/pages.js`.
- **Acceptance.** The summary equals the sum of lower bounds on a fixture; each card appears only when its condition holds; nothing renders without a server value.
- **Effort:** 1.5 engineer-weeks. **Depends on:** M94, M96, M104.

**Phase 3 exit gate** (week 15, with M105 following in week 16). M102–M104 merged; nightly coverage (binary, continuous, per arm) within bands;
the Proof Pack provenance test green; an audit readout produced on at least one real past campaign if a
prospect or Minfy team can supply one (otherwise on Hillstrom in M110).

---

### Phase 4 — Learn, validate, demonstrate (gate at week 19)

**Goal.** Close the loop, make it cheap to run every month, and prove the whole journey on real
randomised data before the manager review.

#### M106 — Learn from the last cycle

- **Why.** The next model should learn from what the last campaign actually did. Today `build_experiment_frame` (`engine/uplift/measure.py`) uses "non-control" intent-to-treat, and uplift retraining cannot read built datasets because `UpliftRunRequest` takes only `upload_id`.
- **Scope.** The experiment frame uses the intended flag, holdout and explore rows (and the contact file when present); learning from a cycle with `explore_fraction = 0` is refused with a plain reason (`LEARN_NO_OVERLAP`) unless the data is already randomised; `UpliftRunRequest` takes exactly one of `upload_id` or `dataset_id` (the `POST /uplift/runs` handler reads the dataset manifest, and seed and lineage come from the dataset id); the learned model is a challenger waiting for the Approver, who also sees "predicted against measured uplift by decile" from the last campaign.
- **Where in the code.** `engine/uplift/{measure,flow}.py`, `api/routes/{measure,uplift}.py`, `api/schemas.py` (`UpliftRunRequest`), `engine/approvals.py`, `ui/modules/production/approvals.js`.
- **Acceptance.** With explore of at least 5% the frame passes `TREATMENT_NOT_RANDOM`; with 0 it is refused with the reason; a dataset-backed uplift run trains, and a request with both ids or neither gets 422; the calibration block shows a planted miscalibration; `test_measure_campaign.py` passes unchanged.
- **Effort:** 2.5 engineer-weeks (DS 0.5). **Depends on:** M94, M96.

#### M107 — The monthly loop, read-only

- **Why.** The value has to arrive every month without someone downloading and uploading files. This needs **reading** from the client's systems, which Connections already allows (read-only), not writing to them.
- **Scope.**
  - Sources bound to saved connections (`POST /clients/{id}/sources {connection_id, selection}`; "newest object under a prefix" or a fixed table), fetched fresh before each scheduled build (`engine/scheduling/firing.py` `latest_recipe_inputs`).
  - Outcomes and consent pulled from saved connections (`POST /campaigns/{id}/outcomes {connection_id, selection, date_from, date_to}`, `POST /privacy/consent/imports {connection_id, selection}`). The query rule amends DEC-1105: one quoted date-window `WHERE` on a declared column for SQL kinds, no free SQL.
  - Schedule kinds for SCORE → TREAT LIST → MEASURE (when the window matures) → LEARN (challenger only), with an alert at each step. The treat list stays a download; nothing is written outside our storage.
- **Where in the code.** `api/routes/{sources,campaigns,privacy}.py`, `engine/onboarding/{specs,sources,replay}.py`, `engine/scheduling/{firing,schedules,scheduler}.py` (`ScheduleKind` gains the new kinds), `engine/connections/{sql,s3,azure_blob,bigquery}.py`, `engine/privacy/consent.py`, `docs/{CONNECTIONS,ONBOARDING}.md`.
- **Acceptance.** A spec bound to a fake S3 prefix picks up the newest object with no upload; a date-window outcome pull on the SQLite fake returns only rows in the window, on a read-only session; a full scheduled cycle on fakes under a controlled clock runs score → treat list → measure → learn with only the approval left to a person; existing multipart sources behave exactly as today.
- **Effort:** 4 engineer-weeks. **Depends on:** M94, M98, M106.

#### M108 — Cost before each run, with a cap

- **Why.** We run on the client's cloud bill; a surprise bill destroys trust. Today cost is recorded only after a run.
- **Scope.** A pre-run estimate (`engine/aws/prices.py`: list price × time limit × instances, plus scoring, plus the LLM ceiling), in USD and labelled as a list-price estimate, INR only with an Admin-configured FX rate and source; `GET /use-cases/{use_case_id}/cost-estimate` beside the Run button; optional `governance.max_run_cost_usd`, above which a run needs confirmation and is stopped with `cancel_run` when its running cost passes the cap; a monthly spend view in Settings.
- **Where in the code.** `engine/aws/{prices,cost_capture}.py`, `configs/aws_prices.yaml`, `engine/config.py` (`GovernanceConfig`), `api/routes/use_cases.py`, `api/access_policy.py`, `engine/runs.py`.
- **Acceptance.** The estimate is null without prices; no INR without a configured rate; a capped run is refused unless confirmed, and a confirmed run past the cap is stopped.
- **Effort:** 2 engineer-weeks. **Depends on:** M90.

#### M109 — Small fixes that make existing things work better

- **Why.** The research and the code map found existing behaviour that undersells the product. None needs a new feature.
- **Scope.**
  - **The retail win-back case loses to its baseline** (`library/online-retail/run_report.md`): find out why (label, window, features) and either fix the use-case configuration or document the limit.
  - **Drift explanations** name market events the user annotates (a price change, a competitor launch) on the drift and root-cause views, so a drift alarm after a known event is explained, not alarming.
  - **Measure-then-learn wording:** `campaign_verdict` and the Output page use the planner's language ("detectable effect", "early look") consistently.
  - **Guided setup proposes a holdout and explore setting** from the planner when a use case contacts customers.
- **Where in the code.** `configs/use_cases/retail_win_back.yaml`, `library/online-retail/`, the drift views fed by `engine/stages/score.py`'s drift report (annotations are a new artefact; the stage stays unchanged), `engine/uplift/measure.py`, `engine/agent/recommend.py`, `tests/fixtures/agent_bench/expected.json`.
- **Acceptance.** The retail case either beats its baseline after a documented change or its report says why it cannot; an annotated event appears on the drift view; the benchmark diff for the new suggestions is reviewed and committed.
- **Effort:** 2 engineer-weeks (DS 1). **Depends on:** M93.

#### M110 — Validate on real public randomised data

- **Why.** Before anyone outside the team sees a number, the full journey must work on **real randomised data with real effects**, not on planted ones. Only Criteo in our library is randomised, and it could not be downloaded here.
- **Scope.**
  - **Hillstrom** (MineThatData e-mail test: about 64,000 customers, randomised into men's e-mail, women's e-mail and no e-mail, with visit, conversion and spend outcomes and pre-period history): added to `library/` with `fetch.py`, `README.md` (source, licence), `mapping.yaml` and a multi-offer uplift use case. It exercises M100 (two offers and control), M102 (spend with a history covariate) and M104. An owner obtains the file through an approved channel; its licence is recorded before use.
  - **Criteo uplift** if an owner downloads it through an approved channel (internal validation only, per its licence and ruling R2); **X5 RetailHero or Lenta** if their licences allow.
  - **The journey on each dataset:** readiness and power sheet → train uplift and propensity → approval checks (stability, beats risk) → value-weighted, offer-chosen treat list → measurement on the dataset's **own held-out randomised rows** (policy value by off-policy evaluation, with intervals) → Value Proof Pack built on those measured numbers, labelled "public dataset, retrospective".
  - Results recorded in `docs/LIBRARY.md` and each dataset's `run_report.md`, whichever way they fall, including where uplift does **not** beat risk.
- **Where in the code.** `library/hillstrom-email/` (new), `library/criteo-uplift/`, `library/run_engine.py`, `library/tests/`, `configs/use_cases/` (the dataset's use case), `docs/LIBRARY.md`.
- **Acceptance.** Each obtained dataset has a reproducible `run_report.md` produced only by `library/run_engine.py`; the Proof Pack renders from its artefacts and passes the provenance test; no synthetic number appears; any dataset not obtained has its skip recorded with the reason.
- **Effort:** 3 engineer-weeks (DS 2.5). **Depends on:** M96, M97, M100, M102, M104.

#### M111 — The manager demo

- **Why.** The managers should see the product deciding and proving on real data, in its own screens, with nothing fabricated.
- **Scope.** `library/DEMO_SCRIPT.md` rewritten around the M110 results: the story (who to contact, which offer, who to leave alone, what it is worth, how we know), the screens to click, and the honest limits (public data, retrospective; a client's own campaign is the next proof). A one-page results summary generated from the artefacts. A rehearsal by someone outside the team using only the script.
- **Where in the code.** `library/DEMO_SCRIPT.md`, `docs/START_HERE.md` (pointer), `scripts/` (a seed command that loads the validated datasets instead of planted data).
- **Acceptance.** The rehearsal completes without help; every number on the summary resolves to an artefact; `test_docs_honesty` passes.
- **Effort:** 1.5 engineer-weeks (DS 0.5). **Depends on:** M110.

**Phase 4 exit gate.** M106–M111 merged; the scheduled cycle green on fakes; validation results
recorded; the demo rehearsed. **The manager review follows**, and its outcome decides the integration
plan.

---

## 5. Shared contracts introduced

| Contract | Introduced | Shape (all fields optional or defaulted unless stated) |
|---|---|---|
| `HoldoutSpec` | M92 | `scope: run \| use_case \| universal`, `fraction`, `salt_id`, `epoch`; `actions.explore_fraction` |
| `holdout_assignment.parquet` | M92 | key, `holdout_member`, `explore`, `explore_probability` |
| `Campaign` | M94 | `campaign_id`, `kind: scored \| external \| programme`, `use_case_id`, `run_ids`, `treatment_start`, `outcome_window_days`, `causal_basis`, `holdout_epoch` |
| `TestPlan` | M94 | metric, `outcome_kind`, `covariate_column`, holdout share, explore share, `analysis_date`, secondary dates, detectable effect, `plan_hash`, `amends` |
| `measure_campaign` | M94 | the one measurement function (signature in M94) |
| `IncrementalityReport` additions | M94, M100, M102 | `test_plan_hash`, `early_look`; `arms`; `outcome_kind`, `mean_difference`, `mean_difference_ci`, `adjusted_lift`, `adjusted_interval`, `variance_reduction` |
| `UpliftEvaluation` additions | M96, M100 | `baseline_comparison`, `calibration_by_decile`, `fold_auuc`; `arms` |
| `ApprovalItem.checks` | M96 | `[{code, passed, message}]` |
| `UpliftPolicyConfig` additions | M97 | `value_column`, `horizon_months`, `margin_pct`, `min_roi` |
| `treat_list.csv` | M98 | key, use case, model version, band or segment, treat, holdout, explore, suppression reason, offer, channel, contactable channels, net value, reason phrases 1–3, `catalogue_sha256` |
| `ActionCatalogue` | M99 | actions with id, label, channel, offer cost, contact cost, eligibility, required fields; region `requires` lists |
| `UpliftConfig.treatment_levels` | M100 | control value first; one level = today |
| `arbitrated_treat_list.csv` | M101 | the treat-list columns plus winning use case, losing actions, priority score |
| `segment_effects.json` | M104 | per band, segment and offer: rows, conversions, effect and interval |

---

## 6. Sequencing and timeline

### 6.1 Dependencies

| Milestone | Depends on |
|---|---|
| M90 Set-up | — |
| M91 Fixes | M90 |
| M92 Holdout and explore | M90 |
| M93 Planner and outcome definitions | M90 |
| M94 Campaign, measurement, test plan | M90, M92, M93 |
| M95 Validity harness | M93 |
| M96 Uplift checks and beats risk | M92, M95 |
| M97 Net value | M90 |
| M98 Treat list and business reasons | M91, M92 |
| M99 Catalogue and channel consent | M91, M97 |
| M100 Multi-offer uplift | M96, M97, M99 |
| M101 Arbitration | M94, M97, M98 |
| M102 Revenue and CUPED | M94, M95 |
| M103 Audit and programme | M92, M94 |
| M104 Value Proof Pack | M94, M95, M102, M103 |
| M105 Warnings and value to date | M94, M96, M104 |
| M106 Learn from the last cycle | M94, M96 |
| M107 Monthly loop, read-only | M94, M98, M106 |
| M108 Cost before each run | M90 |
| M109 Small fixes | M93 |
| M110 Public-data validation | M96, M97, M100, M102, M104 |
| M111 Manager demo | M110 |

**Critical path:** M90 → M93 → M95 → M96 → M100 → M110 → M111 (about 19 weeks). Multi-offer (M100) is the longest single
item; if it slips, M110 runs on Hillstrom with the two offers evaluated as two binary use cases and the
multi-offer result follows.

### 6.2 Effort and capacity

| Phase | Gate | Milestones | Total (DS inside) | Engineering | DS |
|---|---|---|---|---|---|
| 1 Results you can trust | week 6 | M90–M95 | 18.0 (DS 5.0) | 13.0 | 5.0 |
| 2 Decide better | week 13 | M96–M101 | 23.0 (DS 5.5) | 17.5 | 5.5 |
| 3 Prove better | week 15 | M102–M105 | 12.5 (DS 3.5) | 9.0 | 3.5 |
| 4 Learn, validate, demonstrate | week 19 | M106–M111 | 15.0 (DS 4.5) | 10.5 | 4.5 |
| **Total** | **19 weeks** | **22 milestones** | **68.5 (DS 18.5)** | **50.0** | **18.5** |

Capacity over 19 weeks: engineering 3.25 × 19 ≈ 62 person-weeks against 50 needed; data science
0.8 × 19 ≈ 15 against 18.5 needed.

**The data scientist is the bottleneck** (18.5 DS-weeks against about 15 available). Three ways out, in
order of preference: (1) a second data scientist part-time for Phases 1–2 (about 4 weeks of work);
(2) engineers take the simulator and test plumbing of M95 and M102, leaving the DS the statistics;
(3) the plan stretches by about 3 weeks. Engineering has spare capacity in every phase, which absorbs
fixes, reviews and the client conversations that will start once the demo is ready.

### 6.3 Week-by-week (4 engineers A–D, 1 DS)

| Weeks | Engineer A | Engineer B | Engineer C | Engineer D | Data scientist |
|---|---|---|---|---|---|
| 0–1 | M90 (all) | M90 | M90 | M90 | M93 power formulas |
| 1–3 | M91 fixes | M92 holdout | M93 planner, outcome definitions | M97 net value (no dependency, so it starts early) | M93; M92 review |
| 3–6 | M99 catalogue | M94 campaign, test plan | M95 harness plumbing, then M108 cost (starts early) | M98 treat list, reasons | M95 statistics; M94 check |
| 6–9 | M96 checks (engineering) | M101 arbitration | M103 audit, programme | Screens for M94, M96, M98; M109 fixes | M96 bootstrap, cross-fit |
| 9–13 | M100 multi-offer | M106 learn | M100 multi-offer (with A) | M102 revenue, CUPED | M100 per-arm learning, champion DEC; M102 CUPED |
| 13–15 | M104 Proof Pack | M107 monthly loop | M104 Proof Pack (with A) | Screens for M100, M102, M104 | M104 review; M109 retail case |
| 15–19 | M110 library plumbing | M107 finish | Hardening, fixes from review | M105 warnings, then M111 demo kit | M110 validation runs; M111 |

Gates: Phase 1 at week 6 (M95 is last), Phase 2 at week 13 (M100 is last), Phase 3 at week 15, Phase 4
at week 19. Milestones with no open dependency (M97, M108) start early to use spare engineering time;
they still count in their own phase.

### 6.4 Scaling the plan

- **3 engineers + 1 DS:** about 24 weeks. Cut first, in this order: M109, M108, M105, M101 (arbitration can follow the demo). Never cut: M91, M92, M94, M95, M96, M110.
- **5 engineers + 1 DS:** about 17 weeks; the DS bottleneck sets the floor unless a second DS joins.
- **Integration afterwards:** the layer design (`MARKETING_AI_PLAN_J_LAYER.md`) gets shorter, because this plan builds its holdout service, campaign record, test plan, validity harness, Proof Pack, revenue outcomes, catalogue, value weighting, beats-risk check and cost estimate. What remains there is the contract and destinations, send-log import and reconciliation from engagement tools, deployment in client accounts, Marketplace, the DPDP evidence pack and industry packs.

---

## 7. Do not build in Plan J (and why)

- **Any write into a client's systems** (destinations, push connectors, webhooks) — the founders' decision (J1); designed, not built.
- **Send-log importers from engagement platforms** — integration; the manual contact file (M103) covers measurement until then.
- **Message sending, journeys, a CRM, a CDP, reverse ETL** — the positioning memo's boundary.
- **Real-time decisioning, a lookup API, per-user reinforcement learning** — need volume and machine credentials; later.
- **AWS Marketplace, FTR, industry packs, the DPDP evidence pack** — go-to-market work, after the review.
- **Money-precise lifetime value** — practitioners trust it as a ranking, not as rupees; we use a value column and ranges.
- **Any result figure not measured** — including the planted demo effect and Criteo results outside internal validation.
- **Changing the per-run holdout default, or loosening any test.**

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Public randomised data cannot be obtained** (downloads blocked here; licences). | An owner obtains Hillstrom and Criteo through an approved channel in Phase 1, so M110 is not waiting. If neither arrives, M110 runs an audit readout on a real past campaign from a Minfy client or prospect, and the demo says so. |
| **Uplift does not beat risk on the public data.** | That is a valid result and is reported (M96 makes the product fall back to risk ranking with the reason). The demo then shows value ranking, offer choice and honest measurement, which still beat a plain churn score. |
| **Multi-offer is harder than estimated.** | It follows DEC-668's additive path; one treatment level stays byte-identical. If it slips, the validation evaluates each offer as its own binary use case first. |
| **The data scientist is overloaded.** | §6.2: part-time second DS, engineers take test plumbing, or a 3-week stretch. |
| **Shared-rule regressions** (holdout touches DEC-A4, consent gate, stage code). | Opt-in config with existing tests pinned unchanged; the protocol amendment names the exact functions; M91's consent test covers both flows. |
| **Building without a client's feedback.** | Engineering slack is kept for client conversations; an audit readout (M103) and the demo give prospects something real to react to before integration starts. |
| **Nothing reaches clients for about 19 weeks.** | From Phase 2 the treat list download is usable with any tool by hand, so a willing client can run a campaign on it at any time; the plan does not have to finish first. |

---

## 9. Measures of success

| Phase | Product (tested) | Evidence for the manager review |
|---|---|---|
| 1 | Holdout stability property tests, planner accuracy, nightly coverage and false-positive bands, M91 regression tests | Intervals proven to cover 95%; no planted number reachable in reports |
| 2 | Beats-risk fallback, value-ranking identity, offer-choice and arbitration fixtures | On planted data: the right offer per segment, no double contacts, value-ranked lists |
| 3 | CUPED variance reduction within band, Proof Pack provenance | A Proof Pack every number of which traces to an artefact |
| 4 | Scheduled cycle on fakes, validation runs reproducible | Results on real public randomised data, reported whichever way they fall, and a rehearsed demo |

**Plan-level success:** the managers see the product decide (who, which offer, who to leave alone) and
prove (net value with a 95% range) on real randomised data, with nothing fabricated.

---

## 10. Open questions for the founders

1. Do you accept J1–J10 as recommended?
2. Who obtains the public datasets (Hillstrom, Criteo, X5 or Lenta), through which channel, and by which week? M110 depends on it.
3. Is a part-time second data scientist available for Phases 1–2, or do we accept the 3-week stretch?
4. Is there a past randomised campaign, from Minfy or a prospect, that we may audit (M103) for the demo?
5. Who are "the managers" for the review, and what would they need to see to approve the integration phase?
6. Does any client want to try the downloaded treat list on a real campaign before the plan finishes?
