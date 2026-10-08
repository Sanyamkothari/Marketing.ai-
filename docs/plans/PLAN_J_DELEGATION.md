# Plan J — Phase 2 delegation: who builds what, and how

**Companion to:** [`MARKETING_AI_PLAN_J_PRODUCT.md`](MARKETING_AI_PLAN_J_PRODUCT.md) (the plan; Phase 2 is §4 "Decide better"),
`PARALLEL_WORK_PROTOCOL.md`.
**Written:** 8 October 2026, after Phase 1 (M90–M95) merged on `main`.

Phase 2 is split between two builders:

- **Claude Code** (this repository's main session): builds the statistical milestones, reviews and
  merges everything, and owns `main`.
- **An external agent** (the "partner agent"): builds the well-specified product milestones on its own
  branches. It never pushes to `main`.

Every partner branch goes through the same gate as Claude Code's own work before it reaches `main`:
two adversarial reviews, fixes and polish, a merge, `make lint`, and the full test suite.

---

## 1. The split

| Milestone | Builder | Why |
|---|---|---|
| **M96** Uplift must earn its place (stability, calibration, beats-risk check) | Claude Code | Statistics: paired bootstrap, cross-fitting with no row scored by a model trained on it. A subtle error here gives confident wrong answers. |
| **M97** Rank by net value | Partner agent | A clear formula with exact acceptance tests. |
| **M98** The treat list and business-language reasons | Partner agent | A builder over existing artefacts, a config file and a download; well specified. |
| **M99** Offer and channel catalogue, channel-aware consent | Partner agent | Config, validation and suppression plumbing; well specified. |
| **M100** Choose the offer (multi-treatment uplift) | Claude Code | The hardest milestone: per-arm learners, per-arm evaluation, champion comparison. |
| **M101** One action per customer across use cases | Partner agent | Deterministic arbitration over existing outputs; well specified. |
| Reviews, fixes, merges, shared docs (DECISIONS, README, help texts, error-code registry, cross-branch records) | Claude Code | One owner for `main` and the shared files avoids conflicts. |
| `make test-all` and the failing slow Help-menu browser test | Claude Code | Housekeeping on `main`. |

**Trial first.** The partner agent starts with **M97 only**. If its review needs only light fixes,
it continues with M98, M99 and M101. If it needs heavy rework, Claude Code takes the remaining
milestones back.

## 2. What runs in parallel

| Wave | Partner agent | Claude Code | Starts when |
|---|---|---|---|
| **1** | **M97** (trial) | **M96**; `make test-all`; the Help-menu test | now |
| **1b** | **M98** (M97's review was not light, so it waits for M97 on `main`; it reads the review first) | fixes, reviews and merges M97 | M97 merged |
| **2** | **M99** (needs M97 on `main`) | reviews and merges M98 | M97 merged |
| **2** | **M101** (needs M97 and M98 on `main`) | reviews and merges M99, then M101 | M98 merged |
| **3** | — | **M100** (needs M96, M97, M99 on `main`) | M99 merged |

The partner agent works on **one milestone at a time** and always starts it from the latest `main`.
Two milestones in flight at once on its side would conflict with each other.

## 3. How a milestone moves

1. **Partner agent:** `git fetch origin && git checkout -b plan-j/<mXX>-<short-name> origin/main`.
2. **Partner agent:** builds the milestone following §4 and its prompt in §5. Commits on the branch. Writes
   the hand-off file `docs/handoff/<MXX>.md` (§4.6) on the same branch. Pushes the branch:
   `git push -u origin plan-j/<mXX>-<short-name>`. **Never pushes `main`, never merges, never rebases
   `main`.**
3. **The person** tells Claude Code: "M97 is ready on `plan-j/m97-net-value`".
4. **Claude Code:** fetches the branch, runs two adversarial reviews (correctness and statistics;
   compatibility and protocol), proves every new test fails on the code before the change, fixes
   and polishes, merges to `main`, writes the shared docs, runs `make lint` and the full suite, and
   pushes. The hand-off file is folded into the shared docs and deleted at merge.
5. **Claude Code** reports what it had to fix. The person passes that feedback to the partner agent
   before its next milestone, so the same mistakes are not repeated.

---

## 4. Rules for every partner build (read before every milestone)

### 4.1 Read first
- `PARALLEL_WORK_PROTOCOL.md` (shared files, ownership, decision numbers).
- `docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md` §3 (principles and protocol rules) and your milestone in §4.
- `docs/plans/MARKETING_AI_PLAN_J_LAYER.md`: the matching milestone there gives a more detailed
  earlier design; use it for detail, but where it talks about contracts, deliveries, destinations,
  send logs or activation it is **out of scope**.
- The Phase 1 decisions in `docs/DECISIONS.md` (DEC-1300 … DEC-1305) and `docs/DECIDE.md`.

### 4.2 Set-up
- Python 3.11. `make setup` creates `.venv` and installs everything (AutoGluon included; it is large).
- `make lint` = ruff, black --check, mypy --strict, generated-file checks. It must be clean.
- `make test` is the full fast suite (about 25 minutes on 4 cores). Run your **targeted** tests while
  developing, then run `make test` once before you push. Report the counts.
- `.venv/bin/python -m pytest -n 4 …` works if you install `pytest-xdist` locally (do not add it to
  `pyproject.toml`). Known parallel-only failures: `tests/unit/test_logging_audit.py` (three tests),
  `tests/unit/agent/test_scanner_hardening.py` (timing), and the order-dependent learn test in
  `tests/integration/uplift/test_measure_campaign.py`. These pass when run serially.

### 4.3 How to build
- **Test first.** Write each acceptance test from the plan, run it, and record that it **fails on the
  unchanged code** before you implement. Put that output in the hand-off file.
- **Additive and opt-in.** With default configuration every existing test passes unchanged and every
  existing artefact is byte-identical. New contract fields are optional or defaulted.
- **Never skip, delete or loosen a test.** If an existing test encodes old behaviour that the
  milestone deliberately changes, update it as a reviewed edit and say so in the hand-off file.
- **Nothing fabricated.** Every number shown traces to a computed artefact. A number that cannot be
  computed is `null` with a plain reason, never `0`. The UI renders server values only.
- **Config over code.** Nothing under `engine/` branches on a use-case, industry or region id.
- **Frozen files stay frozen:** `engine/stages/train.py`, `evaluate.py`, `explain.py`, and the champion
  rule in `engine/registry.py`. The only Phase 1 stage functions Plan J may edit are
  `engine/stages/actions.py` `_control_mask`, `_entity_control_mask`, `_holdout_size`,
  `suppression_rules`, and `engine/stages/export.py` `_suppression_counts` (DEC-1300 (c)).
- **Shared files** (`tests/unit/test_shared_file_markers.py` `SHARED_FILES`): add code only inside the
  `PLAN-J` block. A pydantic field that must live in a class body is declared in place, defaulted, typed
  in a Plan J module; list every such edit in the hand-off file.
- **Every new route** has a `RoutePolicy`; writes are Analyst; customer ids go in request bodies,
  never URLs; mutating routes are audited by the middleware.
- **Every new row-level artefact** (one row per customer) is registered in `configs/privacy.yaml`,
  `engine/privacy/layout.py`, and, if any route serves it, `ROW_LEVEL_ARTEFACTS` in
  `api/access_policy.py` (Analyst-only with sign-in on, audited — DEC-1301 (e)).
  `tests/integration/decide/test_row_level_downloads.py` enforces this.
- **Messages** a person sees are plain English; `engine/pilot/plain.py` `jargon_in` must find nothing.
- **Docs move with code.** Update `docs/UPLIFT.md`, `docs/DATA_CONTRACT.md` or `docs/DECIDE.md` in the
  same change as the behaviour they describe. Regenerate `docs/API.md` with `make generate` when a
  model changes.

### 4.4 Files you must NOT edit
Claude Code writes these for every milestone at merge time; put the text you want there in the
hand-off file instead:
`docs/DECISIONS.md`, `README.md`, `scripts/check_readme.py`, `configs/pilot/help.yaml`,
`engine/decide/codes.py`, `docs/CROSS_BRANCH_REQUESTS.md`, `PARALLEL_WORK_PROTOCOL.md`.

### 4.5 Commits
Clear messages, one logical change each, ending with your own attribution line. Do not squash
Claude Code's history; do not force-push a branch Claude Code has already fetched.

### 4.6 The hand-off file `docs/handoff/<MXX>.md`
Sections, all required:
1. **Summary** — what the milestone does, in plain words.
2. **Files changed** — each with one line on why. Mark in-place edits in shared files and edits in other
   workstreams' areas (see the ownership table in `PARALLEL_WORK_PROTOCOL.md` §3).
3. **Tests** — new test files; the output showing each new test failing on unchanged code; the final
   `make lint` result and `make test` counts.
4. **New error codes** — for each: code, a help title, and help text ("meaning: … fix: …", plain words).
5. **Decision text** — the DEC entry for this milestone as lettered sub-decisions (a), (b), (c)…, each
   with context, decision and consequence.
6. **README note** — one paragraph for the milestone row.
7. **Open questions and known gaps** — anything you were unsure of or left out, honestly.
8. **Self-check** — each item of §4.7 with one line of evidence (a test name, a timing, a command's
   output). "Done" without evidence counts as not done.

### 4.7 Checklist before pushing (lessons from the M97 review)
The M97 review (`docs/handoff/M97_REVIEW.md`) found eight blocking problems that lint and `make test`
did not catch. Each item below is one of them turned into a check. Answer every item in §8 of the hand-off file.
1. **Test the public entry point, with a real run's configuration.** Acceptance tests must go through
   the function or route a real run calls, not only the inner helper. Use the configuration a real run
   has: costs from `configs/pilot/value.yaml` as well as explicit settings, and the artefacts a real
   pipeline run writes (use the synthetic-run fixtures under `tests/integration/`), not only hand-made
   frames. (M97: `min_roi` held in `choose_contacts` but was lost in `recommend_policy`.)
2. **Compute once, pass everything down.** If a value is computed in one function and passed to
   another, pass every part of it (for example net value AND per-row cost). Do not recompute a
   different version further down.
3. **Linear time at 1M rows.** Nothing per row inside a loop over rows; use `cumsum`, vectorised pandas
   or numpy, or a join. Add a timing test on at least 200k rows (mark it `slow` if it takes more than
   ~3 s) and report the 1M-row estimate. (M97's profit curve was O(N²): about 13 minutes at 1M rows.)
4. **Nothing fabricated, nothing mislabelled.** A missing input gives `null` with a plain reason. Never
   use `fillna(1.0)` or `fillna(0)` to make a number appear, and never default an old file's missing
   column to a made-up value. A field named for one unit (conversions, rupees, a probability) never
   holds another.
5. **Defaults unchanged.** No default that existing runs see may change: config defaults, `RoiInputs`,
   column lists, file names, artefact bytes. Prove it with a test that runs the default configuration
   and compares with `main`. If you believe a default must change, do not change it: raise it as an
   open question.
6. **Stay in your lane.** Run `git diff origin/main --stat` before pushing; every file must be explained
   by the milestone in §2 of the hand-off file. No drive-by fixes in other workstreams' files: list
   them as open questions instead.
7. **Merge the latest `origin/main` before pushing** and resolve conflicts. Do not re-apply a fix that is
   already on `main`.
8. **Read your own diff line by line** (`git diff origin/main`) before pushing, looking for indentation
   changes, moved statements and leftover debug code. (M97 moved one `add_metrics` call into a loop by
   accident.)

---

## 5. Prompts

Paste one prompt per milestone, in a fresh session, with the repository checked out.

### 5.1 Partner agent — M97 Rank by net value (trial; start now)

```text
You are building milestone M97 of Plan J in the Marketing AI repository (Python 3.11, FastAPI,
vanilla JS). Read these first, in full: docs/plans/PLAN_J_DELEGATION.md (§3 and §4 are your rules),
PARALLEL_WORK_PROTOCOL.md, docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md §3 and the M97 section of §4,
and docs/plans/MARKETING_AI_PLAN_J_LAYER.md "M110 — Value weighting v1" for detail (ignore its
catalogue and contract parts: M99 adds the catalogue later).

Branch: git fetch origin && git checkout -b plan-j/m97-net-value origin/main

Goal: rank customers by expected net money, not by uplift alone.
- UpliftPolicyConfig (engine/uplift/config.py) gains optional value_column, horizon_months, margin_pct,
  min_roi (agent-editable where the existing fields are). A `value` block in configuration gives offer
  cost and per-channel contact cost; when M99's catalogue exists later it will take these over, so
  keep the lookup behind one function.
- Net value per customer = uplift x value x margin - offer_cost x p_treated - contact_cost.
  engine/uplift/policy.py ranking, choose_contacts, recommend_policy and profit_curve take this vector
  together; the persuadable-only and sleeping-dog guards stay exactly as they are.
- The training hold-out file carries the value column: HOLDOUT_COLUMNS in engine/uplift/flow.py and
  HoldoutUplift in engine/uplift/metrics.py, so expected value is a value-weighted observed uplift.
- Propensity runs get an optional expected_gross_value per row (p x value - cost), labelled
  "not incremental" everywhere it appears.
- One value block feeds both the policy and the RoiInputs defaults in engine/pilot/roi.py. Put India
  channel-cost defaults in configs/pilot/value.yaml, labelled as editable defaults.
- Guided setup proposes a value-like column (order value, premium, balance, ARPU) as a "check"
  suggestion in engine/agent/recommend.py; update tests/fixtures/agent_bench/expected.json with
  `python -m tests.fixtures.agent_bench.cases --update` and review its diff.
- ProfitCurve gains value_weighted and value_basis; PolicyRecommendation gains expected_net_value as a
  range; GET /runs/{id}/uplift/profit-curve gains optional min_roi and value parameters.
- ui/modules/uplift/views.js profitCard shows the value basis. docs/UPLIFT.md §14 updated.

Acceptance tests (write them first and show they fail on unchanged code):
- The identity in tests/unit/uplift/test_profit_curve.py (the profit-curve point at the budget equals
  the recommendation, field for field) holds with and without value_column. Extend that file; never
  loosen it.
- A constant value column reproduces today's scalar path exactly.
- On a fixture where high-value persuadables have lower raw uplift, they rank higher.
- min_roi is respected; no sleeping dog is ever treated.
- Money fields are null with a plain reason when value inputs are missing.

This edits Phase 3b's area (engine/uplift/*) as pre-approved, backward-compatible edits; list each in
the hand-off file. Do not edit the files in §4.4 of the delegation doc. Run make lint and make test
before pushing. Write docs/handoff/M97.md (§4.6), commit, and push the branch. Do not push main.
```

### 5.2 Partner agent — M98 The treat list and business-language reasons (after M97 is merged on `main`)

```text
You are building milestone M98 of Plan J in the Marketing AI repository. Read first, in full:
docs/plans/PLAN_J_DELEGATION.md (§3 and §4 are your rules; §4.7 is the checklist you must answer in
your hand-off file), docs/handoff/M97_REVIEW.md (the review of your M97 build: what went wrong and
why), PARALLEL_WORK_PROTOCOL.md, docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md §3 and the M98 section of §4.
Then read how M97 landed on main: engine/uplift/policy.py and docs/UPLIFT.md §14, where the net value
and its null reasons come from.

Branch: git fetch origin && git checkout -b plan-j/m98-treat-list origin/main

Goal: one downloadable file per scoring run that carries everything a campaign manager needs, with
reasons in business words. Until integration, this file is how lists reach a client's tool.
- A pure builder engine/decide/treat_list.py over a finished scoring run's artefacts (it reads files
  beside the run and writes treat_list.csv and treat_list.parquet beside them; it is never imported by
  the pipeline's stages and adds no method rebind in engine/pipeline.py). Do NOT change
  scores_csv_columns. Columns: customer key (all key columns for composite keys), use case, model
  version, band or uplift segment, treat flag, holdout flag (from holdout_assignment.parquet, M92),
  explore flag, suppression reason, offer and channel (the band's action for now; M99/M100 fill them
  later — reserve nullable columns), net value (from M97 when present, else null), and up to three
  business-language reasons.
- Business reasons: configs/decide/reasons.yaml maps feature names and value directions to phrases
  ("Spent less each month for 3 months", "Raised two complaints in 30 days"); engine/decide/reasons.py
  applies it; unmapped features keep today's reason text. engine/stages/explain.py is frozen: read its
  outputs, do not edit it. Guided setup proposes phrases for new features as "check" suggestions
  (engine/agent/recommend.py; update the agent benchmark golden file and review its diff).
- Register treat_list.csv and treat_list.parquet as row-level artefacts in configs/privacy.yaml,
  engine/privacy/layout.py and ROW_LEVEL_ARTEFACTS in api/access_policy.py; serve them through
  api/routes/runs.py read_artefact so the M91 rule (Analyst-only with sign-in on, audited) applies.
  tests/integration/decide/test_row_level_downloads.py must stay green and cover them.
- An existing browser test already saves a download under the name treat_list.csv
  (tests/integration/uplift/test_uplift_browser.py). Check what that download is and make sure the
  names do not collide or confuse users; explain your choice in the hand-off file.
- The Output page shows a treat-list summary and a "Download treat list" button (ui/modules/decide/),
  with one line: include treat = 1, exclude holdout = 1. Viewers with sign-in on see it disabled with the
  server's reason, as M92's integration did in ui/modules/production/gate.js.

Acceptance tests (write them first and show they fail on unchanged code):
- Golden treat lists for a propensity run and an uplift run, with and without M97 net value.
- Every row's holdout flag equals holdout_assignment.parquet.
- A mapped feature renders its phrase; an unmapped one keeps today's text; jargon_in finds nothing.
- The download is refused to a Viewer when sign-in is on and audited for an Analyst.

M98 checks (from the M97 review; each is a test, and each is answered in §8 of your hand-off file):
- Public path, real run: the golden tests build the treat list from the artefacts of a real synthetic
  pipeline run (a propensity run and an uplift run, through the same code the API calls), not from
  hand-made frames. The download test goes through GET on the real route.
- Nulls, never fake values: no holdout_assignment.parquet → holdout flag null (not False) and a plain
  note in the summary; no net value from M97 → net value null; fewer than three explainable features
  → the remaining reason columns are null, not empty strings or repeated text. Test each.
- One unit per column: net value is in rupees and named and documented that way. Flags are booleans,
  not "1"/"0" text in the parquet; the CSV writes 1/0 and the hand-off says so.
- Scale: building the treat list for 200k rows (reason mapping included) has a timing test; report
  the 1M-row estimate. No per-row Python loop or DataFrame.apply over rows for the reasons; map
  them vectorised or through a join.
- Defaults unchanged: scores.csv, scores_csv_columns, every existing artefact and download name, and
  the existing reason text for unmapped features are byte-identical to main (a test compares a
  default run's scores.csv with and without your change). The agent-benchmark golden diff contains
  only your new "check" suggestions; list each one in the hand-off file.
- Lane: the files you may touch are the ones named above plus their tests and docs/DECIDE.md.
  Anything else goes under open questions, not into the diff.

Do not edit the files in §4.4 of the delegation doc. Merge the latest origin/main, then run make lint
and make test before pushing. Write docs/handoff/M98.md (§4.6, including the §8 self-check), commit,
and push the branch. Do not push main.
```

### 5.3 Partner agent — M99 Offer and channel catalogue (after M97 is merged on `main`)

```text
You are building milestone M99 of Plan J in the Marketing AI repository. Read first, in full:
docs/plans/PLAN_J_DELEGATION.md (§3, §4, and the feedback on your earlier milestones),
PARALLEL_WORK_PROTOCOL.md, docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md §3 and the M99 section of §4,
and docs/plans/MARKETING_AI_PLAN_J_LAYER.md "M109 — Action catalogue and channel-aware
contactability" for detail (ignore its activation contract, manifest and delivery parts).

Branch: git fetch origin && git checkout -b plan-j/m99-catalogue origin/main  (M97 must be on main)

Goal: one catalogue of offers and channels with their costs, and consent per channel.
- configs/decide/catalogue.yaml validated by a frozen ActionCatalogue model in engine/decide/catalogue.py
  (follow the configs/privacy.yaml pattern): action id, label, channel, offer cost, contact cost,
  eligibility, required fields. catalogue_sha256 is stamped on every treat list (M98).
- M97's value lookup reads offer and contact costs from the catalogue when it exists.
- Use cases reference action ids additively: Band.action_id and uplift.policy.treat_action_id. The
  free-text Band.action stays as the label.
- SuppressionConfig.channels {channel: {consent_column, contactable_column}}. Per-channel counts go in an
  optional SuppressionCount.channel_counts: dict[str, int] | None. The 3-value reason Literal and the
  DEC-A2 precedence are unchanged. Edit only engine/stages/actions.py suppression_rules and
  engine/stages/export.py _suppression_counts (the functions DEC-1300 (c) allows).
- A row is treated only on a channel it is contactable on; the treat list carries contactable_channels
  and the channel used. A row with no contactable planned channel is not treated and is counted in
  channel_counts, but is not suppressed in scores.csv.
- Consent ledger: nullable consent_record.channel (null = all channels) with Alembic migration
  alembic/versions/0007_consent_channel.py chained to 0006_campaigns; ConsentLedger.classify gains
  channel=None. Existing records apply to all channels.
- Region rule, data-driven: configs/regions/in.yaml declares sms_requires: [dlt_template_id,
  message_category]; catalogue validation applies the configured region's list without naming any
  region in engine code.

Acceptance tests (write them first and show they fail on unchanged code):
- A customer opted out of SMS but in for email, with both planned, is treated only by email.
- A customer whose only planned channel is SMS and who opted out of SMS is not treated, is counted in
  channel_counts, and is not suppressed in scores.csv.
- Existing consent records apply to all channels; the platform migration tests pass.
- An unknown action id fails config load; editing the catalogue changes catalogue_sha256.
- Every existing suppression test and the SuppressionCount schema test pass unchanged.
- With region IN, an SMS action without a DLT template id is refused at config load.

Work through the §4.7 checklist as you build (public entry points with a real run's configuration,
linear time at 1M rows, nulls instead of made-up values, defaults unchanged, stay in your lane).
Do not edit the files in §4.4 of the delegation doc. Merge the latest origin/main, then run make lint
and make test before pushing. Write docs/handoff/M99.md (§4.6, including the §8 self-check), commit,
and push the branch. Do not push main.
```

### 5.4 Partner agent — M101 One action per customer across use cases (after M97 and M98 are merged)

```text
You are building milestone M101 of Plan J in the Marketing AI repository. Read first, in full:
docs/plans/PLAN_J_DELEGATION.md (§3, §4, and the feedback on your earlier milestones),
PARALLEL_WORK_PROTOCOL.md, docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md §3 and the M101 section of §4.

Branch: git fetch origin && git checkout -b plan-j/m101-arbitration origin/main  (M97 and M98 on main)

Goal: each customer gets at most one action per cycle across all use cases.
- POST /decide/arbitrate (Analyst) in a new api/routes/decide.py (RoutePolicy entries; register the
  router in api/main.py's PLAN-J block) takes the latest scoring run of each selected use case over
  the same customer key and writes arbitrated_treat_list.csv and .parquet.
- engine/decide/arbitrate.py (pure): one action per customer per cycle, chosen by priority weight x net
  value (M97). configs/decide/arbitration.yaml: per use case priority; per customer contact cap per
  period; per channel cap. Each row records the winning use case and the actions that lost.
- Holdout members stay untouched in every use case; explore rows keep their randomisation (M92).
- A conflicts summary on Results: how many customers qualified for more than one action and what was
  dropped (ui/modules/decide/).
- Measuring an arbitrated cycle creates one campaign per use case from its winning rows
  (engine/measurement/campaign.py, M94), so each use case's effect stays measurable.
- Register the new files as row-level artefacts (privacy.yaml, layout.py, ROW_LEVEL_ARTEFACTS if
  served) — tests/integration/decide/test_row_level_downloads.py must cover them.

Acceptance tests (write them first and show they fail on unchanged code):
- With three overlapping use cases, no customer has two actions; the winner has the highest
  priority x value; contact caps hold; holdout members are never treated.
- Each use case's campaign measures only its winning rows.
- With one use case selected, the output equals that use case's treat list (M98).

Work through the §4.7 checklist as you build (public entry points with a real run's configuration,
linear time at 1M rows, nulls instead of made-up values, defaults unchanged, stay in your lane).
Do not edit the files in §4.4 of the delegation doc. Merge the latest origin/main, then run make lint
and make test before pushing. Write docs/handoff/M101.md (§4.6, including the §8 self-check), commit,
and push the branch. Do not push main.
```

### 5.5 Claude Code — its own work

- **Now:** M96 (built here with Opus, two Opus reviewers, merged as in Phase 1); `make test-all` once and
  a fix for `tests/integration/pilot/test_pilot_acceptance.py::test_feedback_from_the_screen_is_recorded`
  (it fails on `main` before Phase 2, unrelated to Plan J).
- **For each partner branch:** fetch; two adversarial reviews; prove new tests fail before the change;
  fix and polish; merge; shared docs; `make lint`; full suite; push; report the fixes back as feedback.
- **After M96, M97 and M99 are on `main`:** M100, built here.
