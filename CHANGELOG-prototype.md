# Prototype changelog

`marketing-ai-prototype.html` is the design source of truth. The build agents copy its
look, copy and structure rule for rule; this file says what changed and which plan
section each change implements.

The prototype is a single HTML file: hash routing, dark mode, mobile layout, no build
step and no dependencies. It is also the artefact behind the reference link in
`plan.md` — <https://claude.ai/artifact/8ARwky4HnEPQyEfRKa5Svi> — now checked into the
repository so it can be diffed and tested.

> **Note on section numbers.** `MARKETING_AI_PHASE2_PLAN.md` and
> `MARKETING_AI_PHASE3A_PLAN.md` are not in this repository (only the Phase 1 plan,
> `plan.md`, is). The section numbers below are the ones named in the brief for this
> change; the screens themselves were built from the brief's own specification, and the
> copy voice, validation-message style and Setup flow follow `plan.md` §6 and §9.

---

## New screens and states

| # | Screen / state | Plan section | Where it lives |
|---|---|---|---|
| A | **Client selector** in the header | Phase 2 §10 A | `clientPicker()`, every screen via `pageHead()` |
| B1 | **Setup → step 1 is a choice**: *Upload a prepared file* or *Build from raw tables* | Phase 2 §10 B | `predStep1()` |
| B2 | **Onboarding → 1. Sources** | Phase 2 §10 B | `obSources()` |
| B3 | **Onboarding → 2. Mapping** | Phase 2 §10 B | `obMapping()` |
| B4 | **Onboarding → 3. Features & label** | Phase 2 §10 B | `obFeatures()` |
| B5 | **Onboarding → 4. Build & review** | Phase 2 §10 B | `obBuild()`, `buildSteps()` |
| B6 | **Score mode with a saved recipe** | Phase 2 §10 B | `predStep1()`, `obSources()` |
| C1 | **Assistant Setup**: documents, optional reference Q&A | Phase 3a §9 C | `genStep1()`, `genStep2()` |
| C2 | **Assistant Advanced**: RAG settings incl. the refusal message | Phase 3a §9 C | `stagesHtml()` generative stage 3 |
| C3 | **Assistant Running**: index build stages | Phase 3a §9 C | `runSteps()` |
| C4 | **Assistant Results**: index, eval, worst ten, Try it, cost | Phase 3a §9 C | `assistantResults()` |
| D | **RCA root causes** per risk segment | Phase 3a §9 D | `rcaBlock()` |
| E | **Win-back campaign copy** | Phase 3a §9 E | `copyBlock()` |
| F | **Lineage block** on the Data page | Phase 2 §10 F | `lineageBlock()` |
| R | **Reconciled against the built screens** | — | see below |
| U1 | **Setup → Uplift problem type**, treatment-column picker, meta-learner step | Phase 3b §8 (plan B) | `setupHtml()`, `ptypesFor()`, `detectTreatment()` |
| U2 | **Setup → TREATMENT_NOT_RANDOM**: the six checks inline, acknowledge to run | Phase 3b §8, plan B §4 | `upChecksHtml()`, `sampleTargeted()` |
| U3 | **Advanced → uplift settings** inside stages 5 and 6 | Phase 3b §8, plan B §6 | `upStage5()`, `upStage6()` |
| U4 | **Running → uplift training and uplift scoring** | Phase 3b §8 | `runSteps()` |
| U5 | **Results → Model**: Qini curve with random line, AUUC with CI, uplift by decile | Phase 3b §8 | `upliftModelBlock()`, `qiniSvg()`, `decileBars()` |
| U6 | **Results → Output**: four segments, Recommended to contact, expected incremental conversions, treat list | Phase 3b §8 | `upliftOutputBlock()`, `segmentChart()`, `treatListCsv()` |
| U7 | **Campaign results** page: incrementality report, or "Results available on <date>" | Phase 3b §8, plan B §1.5 | `campaignPage()`, `campaignReport()` |
| U8 | **Not-causal banner** on every output of an acknowledged run | Phase 3b §8, plan B §4 | `ncBanner()` |

---

## Revision 2 — reconciled against `ui/modules/generative`

Phase 3a built the three generative screens from this prototype (its modules cite these
screenshots by name: "prototypes 09-11", "prototype 12-13", "prototype 14"). Building them
taught the product things the prototype did not yet show, so the prototype has been brought
back level with the code. Everything below is a change to a screen this file already added;
no Phase 1 screen moved.

**Assistant (Phase 3a §9 C)**

- **Ask box.** "Try it" is no longer three fixed exchanges. You can type a question: one the
  saved set covers comes back grounded with its citations, and anything else gets the refusal
  message — the same choice `min_similarity` makes in `engine/generative/retrieval.py`.
- **Minimum similarity** joins top-k and chunk size in Advanced → Retrieval, with its hint. It
  is a real field (`configs/engine.yaml` → `generative.retrieval.min_similarity`, default 0.25).
- **Citations carry their similarity** (`sim 0.91`), as `gdom.js`'s `citationCard` does.
- **Every answer says what it did**: chunks retrieved, latency, prompt version, and `Refused`
  where it refused, plus its guardrail checks — matching `assistant.js`'s `messageHtml`.
- **The cost line became a usage line.** The brief specified `LLM cost this run: —`, which was
  right while nothing computed a cost. `engine/generative` now writes `llm_usage.json`, so the
  prototype shows the shape the product shows: `2,204 calls · 1,284,600 tokens · $1.83`.

**RCA (Phase 3a §9 D)**

- **A confidence pill per cause** (`high` / `medium` / `low` confidence), as `RootCause.confidence`
  is rendered by `gdom.js`'s `confidencePill`.
- **Guardrail outcomes per segment** — counts and the checks behind them, so a warning is never
  silent. The Medium segment carries a real `warned`, because one of its causes rests on model
  drivers alone.
- **Each segment's share** of the customers at risk, beside its row count.
- **A Cost & guardrails card**, as `rca.js`'s `usageCard`.

**Win-back (Phase 3a §9 E)**

- **The judge names were wrong.** The prototype showed Brand / Clarity / Compliance. The engine
  runs two judges on copy — `_TEMPLATE_JUDGES = ("compliance", "toxicity")` in
  `engine/generative/win_back.py` — so the line now reads `Compliance 1.00 · Toxicity 0.01 · 86
  characters`.
- **Status, block reason and guardrails are now derived from the message**, not stored beside it.
  `tplGuards()` measures the copy against its channel's limit and returns the same four checks
  the engine does; a card is Blocked because a guardrail blocked it, and the reason is that
  guardrail's own wording (`42 characters over the 160-character limit`, matching
  `guardrails.py`). Regenerating into shorter copy clears the block by itself.
- **An approval record.** Approve is disabled until you name yourself, and an approved card says
  who accepted it and when — `DEC-055`'s unverified-claim pattern, as `copy.js` implements it.
  Seeded approvals carry their own name, not the one you type.
- **The control holdout is a number on screen**: 1,450 of 14,500 held back, 1,850 suppressed by
  consent or recent contact, 11,200 written to.

Two differences were left as they are, because the prototype is the design here and the build
should follow it rather than the other way round: the card header reads `Email · Variant A`
rather than the build's uppercased `EMAIL · Variant A`, and the block reason is repeated once
above the guardrail list so it is readable without parsing the list.

---

### A. Client selector — Phase 2 §10 A

A small dropdown in the header of every screen, default **Demo Telecom**, with two more
clients and **+ New client**. It is visual only: switching clients changes nothing else
on the page, and `+ New client` does not switch client. One hint line under it —
*"Tables, recipes and models are kept per client."*

### B. Setup step 1 — Phase 2 §10 B

Step 1 now opens with two cards. **Upload a prepared file** is the existing behaviour,
unchanged: the same file control, the same sample-dataset link, the same preview.
**Build from raw tables** reveals a four-step onboarding panel inline, below step 1.
Each step collapses, and shows a green tick when it is complete.

**1. Sources.** A drop zone that takes any number of real CSV or Parquet files (headers
and the first rows are parsed in the browser), or a link to six sample tables. Each file
becomes a row: name, rows, detected role (Customers / Bills / Payments / Complaints /
Usage / Campaign events / Orders / Activity / Other event), key candidate, time
candidate, and — on event tables — a coverage badge, *"96% of rows match a customer"*.
The Customers table is marked *spine table* instead. One hint line per role, for all
nine roles.

**2. Mapping.** One card per source: *your column → our column* (a dropdown that always
offers **not used**), the inferred type, three sample values and a confidence pill —
green at 0.85 and above, amber below it, grey where nothing is suggested. A
required-but-unmapped column is a highlighted row carrying the plain-language message,
e.g. *"Every event table needs the date the event happened. Without it we cannot tell
which rows came before a snapshot."* — and it stays until a column is chosen, so
**Accept all suggestions** cannot paper over it. `DND_FLAG → marketing_opt_in` opens a
value-map popover showing the flip (`Y → false`, `N → true`) and why. A column that
looks like personal data (`AADHAAR_NO`) is left out by default and says so.

**3. Features & label.** Sixteen suggested features as a checklist grouped by role —
name, one-line description, window — all ticked. An **Add feature** form (table,
function, column, window, optional filter) appends one. The label is an editable
sentence: *"A customer counts as **churned** if there is **no activity** in the **60**
days after the snapshot"*, and the target column name follows what you type.
Snapshot settings are Single or Periodic (monthly), capped at 12, default 6.
**Preview** shows five rows, a null-rate bar per feature and the positive rate per
snapshot.

**4. Build & review.** **Build dataset** runs a progress list — apply mappings, build
snapshots, one line per role, labels, assemble, validate, write — and then a build
report: sources with coverage, snapshots with inline-SVG positive-rate mini bars,
dropped features with their reasons, and the checks list. **Use this dataset** fills
step 2 (primary key `customer_id + snapshot_date`, target `churned_60d`, problem type,
time column `snapshot_date`) and collapses the panel.

**Score mode.** The second card reads **Upload this month's tables** and names the saved
recipe; the source rows come pre-labelled by role and the mapping is already accepted.

### C. AI Onboarding Assistant — Phase 3a §9 C

**Setup** replaces the dataset step with **Documents** (name, pages, chunks per file) and
an optional **Reference questions** step — without it the assistant is built but never
scored, and the run is not blocked. **Advanced** keeps chunk size and top-k where they
were and adds the **refusal message**. **Running** is now an index build: read, split,
redact, embed, index, then answer and grade only if reference questions were uploaded.
**Results** adds the index summary, the eval pass rate against its threshold with a
small bar, the ten weakest answers with what went wrong, and a **Try it** panel with
three saved exchanges whose citation chips expand to the quoted chunk — including one
that finds nothing and returns the refusal message. A cost line reads
*"LLM cost this run: —"*.

### D. RCA root causes — Phase 3a §9 D

The Output page opens with a **Generate root causes** button and a line saying none have
been written yet. Generating gives one block per segment (High risk, Medium risk):
a headline, three causes, each with a reason chip (`outages_90d ↑ 3x`) and a
complaint-snippet chip, then the recommended actions and a caveat naming how many
complaint notes the causes rest on and what share of the segment has none.

### E. Win-back campaign copy — Phase 3a §9 E

A banner — *"Nothing is sent from here. Approved messages are exported for your campaign
tool."* — over twelve template cards, two bands × three channels (Email / SMS /
WhatsApp) × two variants. Each carries a status pill (Pending review / Approved /
Blocked), the judge scores, its character count, and a block reason where blocked
(*"Exceeds 160 characters (202)."*, read off the message itself so it cannot drift).
**Approve** and **Regenerate** act on one card; **Download messages** exports the
approved ones.

### F. Lineage — Phase 2 §10 F

A run built from raw tables adds **Where this dataset came from** to its Data page: a
horizontal flow *Sources → Mapping → Recipe → Dataset → Run* with truncated ids and a
truncated sha256. It does not appear for a run from a prepared file.

---

## Revision 3 — the reference set the API accepts, and the threshold it grades at

The Phase 3a API review made the assistant's reference set strict (`DATA_CONTRACT.md` §9.2): four
columns, and a 422 naming the first one missing, checked on upload. The prototype still showed three
columns keyed on `question_id`, so it described a flow the API now refuses.

- **The sample reference set is the product's template**, column for column —
  `question, expect_refusal, source_doc, reference_answer` — and its first column is the key, as the
  built Setup screen assumes. `question_id` no longer appears anywhere in the prototype.
- **Step 2 names the four columns before anything is uploaded**, each with what it holds. The built
  screen shows the error only after an upload fails; here it is avoidable. *The build should adopt
  this.*
- **A real upload is checked on arrival, the way the API checks it**: same columns, same order as
  `api/routes/generative.py`, same wording — "The reference set is missing the expect_refusal
  column." A rejected file fills in nothing, and because the set is optional the build stays
  runnable, unscored. Evaluate mode checks its test questions the same way (it previously went
  through the predictive file handler, which never cleared its blocker for a real upload).
- **The pass threshold is a setting** in Advanced → Evaluation, default **75%** — the product's
  `generative.reference_set.pass_threshold: 0.75`, not the 85% the prototype had invented.
- **The result says which side of the threshold the run landed**, and a failing run is drawn red.
  The built bar is always green, so a 60% run against a 75% threshold reads as a pass. *The build
  should adopt this too.*

`tests/prototype/consistency.test.mjs` now reads the product's own files — `configs/engine.yaml`,
`api/routes/generative.py`, the template CSV and `DATA_CONTRACT.md` — so the prototype fails its
tests the day the product changes and it does not.

**Phase 2's panel against the prototype.** `ui/modules/onboarding/` on `phase-2-onboarding` covers
nearly every element here, down to the value map, "Accept all suggestions" and "Use this dataset".
Two differences, left as they are pending a decision:

- *Label sentence.* The panel reads "An **entity** counts as…" with only the day count editable. The
  prototype reads "A **customer** counts as…" with the name, condition and days editable. The
  product already substitutes the entity's name elsewhere (DEC-022), so "customer" should win; how
  much of the sentence is editable is a design call.
- *Snapshot cap.* The product allows 1–120 with a default of 12 (`configs/engine.yaml`). The
  prototype says "max 12", as the original brief did; without the Phase 2 plan it is not clear
  which is intended.

The panel is not reachable from the app yet, and a built dataset cannot start a run; both are
logged in `docs/CROSS_BRANCH_REQUESTS.md`.

---

## Revision 4 — Phase 3b uplift (plan B §8) · 2026-09-23

Plan B milestone M44: "UI — Setup, Model, Output and Campaign results screens; prototype updated to
match". Uplift lives on **Win-back Campaign**, where `plan.md` always put it: the use case's model
steps already said "Pick best offer · Uplift model", and its output already held back a 10% control
group. No use case was added, no setup step and no Advanced stage; uplift is a problem type, and
everything below appears only in uplift state, which is an explicit opt-in. Nothing on the Phase 1
path moved: *use the sample dataset* on Win-back loads the same columns as before (no treatment
column), trains *LightGBM + Claude copy · ROC-AUC 0.78 · Set as champion*, and renders the same Setup,
Results, Data, Model and Output markup as 8f0d358 — checked byte for byte in jsdom for Win-back and
for every other use case, and pinned by a test. The one thing a Phase 1 user gains is the plan B §8
page itself: a win-back **scoring** run keeps a control group, so its summary line links to
**Campaign results** and its pages carry a fourth tab. The screenshots 01–18 are unchanged, except
the run stamp in 11 and 16 (review round 2: *23 Sep 2026, 10:33*, the seeded stamps' form, no longer
*22 Sept 2026, 05:54 pm*).

**Setup**

- **Uplift is an opt-in.** Win-back's step 1 offers *use the sample campaign file (with a control
  group)* (`#f-samplecampaign`, `win_back_campaign_with_control.csv`: the Phase 1 columns plus
  `treatment` and `treatment_date`) next to the unchanged *use the sample dataset*, and *use a sample
  where offers were targeted* for the non-random case.
- **Only Win-back has uplift** (round 1 decision). The prototype has uplift sample numbers for
  Win-back alone, so uplift is offered only where `SETUP[id].treatment` is set (`hasUplift()`).
  Elsewhere a column named like a treatment (`contacted` on Payment Propensity, say) is an ordinary
  column and the Phase 1 path runs; no other use case can show win-back's uplift numbers. The engine
  itself detects a treatment column on any use case; the prototype simply has nothing honest to show
  there.
- **Uplift is a problem type**, `Uplift (who changes because of your action)` — the
  `catalog.problem_types.uplift.label` of `configs/engine.yaml`, verbatim — detected when a treatment
  column is chosen, with the one-line explanation *"Predicts who changes behaviour because of your
  action."* in place of the generic "Metrics and models will switch…" warning. `change…` offers it
  only when there is a treatment column; `PTYPES` itself is untouched (Classification stays first).
- **Treatment column picker**, a field in step 2 shown only when a column matches
  `uplift.treatment_column_hints` (`treatment, treated, contacted, is_treated`), with a **None**
  option and the detected column pre-selected, as `GET /uploads/{id}/treatment-candidates` returns
  `detected`. Hint: 1 means received the action, 0 means held out, and it must be randomly assigned.
  The treated share reads *90% treated* for the sample and **—** for a real upload.
- **Checks on a real upload.** A CSV the page read whole (up to 256 KB) is counted row by row, as
  `engine/uplift/checks.py` counts it: a treatment value that is not 0/1/true/false is
  TREATMENT_NOT_BINARY, and an arm under `min_arm_rows` or `min_arm_positives` is
  TREATMENT_ARM_TOO_SMALL — so a two-row file is stopped with *"There are too few customers to measure
  what the campaign changed: the treated group has 1 customers (at least 1,000 needed); …"*, Run is
  disabled and nothing can be acknowledged. Checks that cannot run say so (*Not run: an arm has fewer
  than 30 customers.*) instead of showing a tick; a treatment column that is all 0/1 ticks
  TREATMENT_NOT_BINARY, so the panel always lists the six checks. A bigger upload cannot be checked in
  the page and shows no panel.
- **Blocked reasons.** Uplift with no treatment column: *"Choose the treatment column"*. Clearing the
  column keeps the problem type on Uplift (the choice was the user's) rather than silently switching
  to Classification. Loading another file — an upload or any sample link — detects the problem type
  afresh, so the plain sample after that is Classification again and runs.
- **Step 3 for uplift** offers the meta-learners (X-learner recommended, T-learner, S-learner as the
  baseline) and the base model (AutoGluon fast, LightGBM), each with a hint.
- **Advanced**: stage 5 carries bootstrap resamples and the hold-out share; stage 6 swaps the
  High / Medium cuts for the segment cuts (persuadable, sleeping dog and
  `segments.sure_thing_min_probability`, empty meaning the training base rate, 0.099 on the sample)
  and the policy (budget, cost per contact, value per conversion — empty is "—"), and shows the
  control group as read-only text: plan B §5 makes it non-editable. Every field has a one-line hint.
  Only the settings the config lets be null can be empty: emptying bootstrap resamples, hold-out
  share or either segment cut puts its default back (the placeholder shows it), and a value outside
  the config's range (`bootstrap_samples` 10..5000, `test_fraction` 0.1..0.5, cuts −1..1) is brought
  inside it. The **hold-out share** sizes the hold-out the Model and Output pages report: at 50% it is
  160,000 rows (144,000 treated, 16,000 control), each decile keeps its rates, and the stored
  bootstrap intervals scale by √(30 / share). A run keeps the share it was trained with.
  `uplift.time_limit_minutes` (AutoGluon's budget) is deliberately not exposed: it tunes the build's
  compute, not a result the user reads. The eight stage titles are unchanged.
- **Non-random treatment (the 409).** *use a sample where offers were targeted* loads a file whose
  checks fail inline: the six plan B §4 checks, TREATMENT_NOT_RANDOM failing at *AUC 0.74 against the
  0.60 limit*, the message and suggestion, and an **acknowledge** checkbox (`#f-ack`). Run stays
  disabled until it is ticked. **The wording is the engine's**: each failing check shows the
  `message` of `engine/uplift/checks.py` (`_not_random`, `_arm_too_small`, `_treatment_not_binary`) and
  its `suggestion`, word for word — *"Who was treated can be predicted from the customers' own data
  (AUC 0.74, where a random assignment scores about 0.50 and the limit is 0.60). The strongest signs
  were 'months_since_churn' and 'avg_monthly_spend'. …"*. The prototype carries the engine's f-string
  templates (`UP_CHECK_TEXT`) and fills them with `pyfmt()`; the ticked rows for checks that passed are
  the prototype's own (the engine writes no text for a pass).

**Running and results**

- The uplift run's steps: checking the treatment (six checks, *random assignment: AUC 0.52 ≤ 0.60* on
  the sample; an upload reports what the page ran — *3 of 6 checks passed · not run in the prototype:
  TREATMENT_NOT_RANDOM, …* — and a file too big to read claims no check at all),
  preparing features, training the X-learner, measuring on the hold-out (200 resamples), segmenting
  and saving. Scoring with an uplift model segments the list and chooses who to contact.
- The run's score is *AUUC 0.0125*. A scoring run copies its model's problem type (it previously got
  `""`, because nothing detects a type without a target), which is what makes the uplift screens
  follow a scored list.
- **Champion.** An uplift run is labelled **Uplift champion** and the propensity champion keeps its
  badge. *Open question:* whether an uplift champion should replace the propensity champion for the
  use case is Stage A's registry decision; the prototype shows both. A not-causal run is never
  promoted.
- The flow keeps three blocks; the Output block reads *Recommended to contact: 4,000*. A win-back
  scoring run adds a **Campaign results** link to the summary line instead of a fourth block.

**Results → Model** (`upliftModelBlock`): four KPIs (learner, AUUC, Qini coefficient, uplift in the
top 10%); `uplift_at` in full — the top 10%, 20% and 30%, each with its interval — with a one-line
caption; an inline-SVG **Qini curve** drawn in `var(--c)` against a dashed `var(--faint)` random
line, with axis captions; **AUUC with its 95% interval** as an interval bar, the verdict pill, and
the sentence from `engine/uplift/metrics.py::_summary`, template for template (4 decimals, "95% CI a
to b"); a link shows what a **null result** reads like (*No measurable uplift… includes zero…*); the example
belongs to the page it was opened on, and any navigation brings back the real run.
**Uplift by decile** is a diverging bar chart around a zero line, observed next to predicted — not
`.vchart`, which cannot draw the negative sleeping-dog deciles — above the decile table (n, treated
rate, control rate, observed and predicted uplift).

**Results → Output** (`upliftOutputBlock`): the **four segments** with `SEGMENT_LABELS` and
`SEGMENT_ACTIONS` from `engine/uplift/contracts.py`; **Recommended to contact** within the budget
with its stop reason; **expected incremental conversions with a CI**; a caption naming all three
segment cuts (the sure-thing cut too); the model's own estimate;
cost, value and net value, or "—"; and a caption saying whether the numbers were computed on the
training run's hold-out or on every scored row. **Download treat list** (`#up-dl`) builds
`winback_treat_list.csv` in the page with the contract's `scores.csv` columns — persuadables within
budget only, never a sleeping dog, never a control-group row. Before a scoring run it is disabled:
*"Score a list with this model to download who to contact."* The campaign copy block follows,
unchanged.

**Campaign results** (`#/uc/win-back-campaign/campaign`, a fourth tab once a scoring run is current):
the scoring run, the outcomes file (or *use sample outcomes*), outcome column, window and treatment
date column, each with a hint. A list scored today shows **"Results available on <send date +
window>"** and measures nothing. The seeded 1 May 2026 campaign, once measured, shows treated and
control n and rate, the absolute lift with its Newcombe interval, relative lift, incremental
conversions with their interval, the p-value, the row accounting (still inside the window, without
an outcome, suppressed or not treated) and a summary sentence — all computed in the page from the
counts. Every date and run stamp uses one fixed month table (`MONTHS`): en-IN ICU spells September
"Sept" in Node and in the bundled Chromium alike, which put *23 Sept 2026* in the run select beside
*Sent 23 Sep 2026* until review round 2. The treated and control counts are the run's own: a Phase 1
scoring run of 12,480 rows is 1,248 control, 1,592 suppressed and 9,640 sent (10,888 in the IMMATURE
sentence), not the 14,500-row seed list's 12,650; a row count the page only estimated gives no count. **The summary sentence is the engine's**: `engine/uplift/incrementality.py::
_summary` branch for branch (`incSummary()`, templates in `INC_TEXT`) — *"Treated customers converted
at 11.0% against 7.5% for the control group: a lift of +3.5 points (95% CI +1.9 points to +4.9 points;
p < 0.001), about 390 extra conversions caused by the campaign."* — and, while the window is open,
its IMMATURE sentence, ISO date and bare count included. Only *use sample outcomes* has numbers behind
it: an uploaded outcomes file is named, but the page says it is not read and shows "—" everywhere,
rather than the sample's figures under the file's name.

**Not causal.** After an acknowledged TREATMENT_NOT_RANDOM, Model and Output carry a `.ncbanner`
with `NOT_CAUSAL_NOTE` verbatim, and the AUUC sentence is prefixed with it, as
`UpliftEvaluation.summary` is. **One deliberate difference from the brief:** on Campaign results the
banner does not repeat the note. The report compares treated customers with a control group the
engine held out at random, so the measured lift *is* causal (`IncrementalityReport.causal` is true
by contract); what is not causal is the model that chose the list. The banner says exactly that. The
class is `.ncbanner`, not `.banner`, so the copy block's banner tests are unaffected.

**Decisions and assumptions to confirm**

- *N after control and suppression.* `N` is chosen among persuadables that are neither held out nor
  suppressed, as `engine/uplift/actions.py` does (eligible = not suppressed and not control). On the
  sample: 5,220 persuadables × 11,200 / 14,500 eligible = 4,032 candidates, budget 4,000.
- *Expected incremental conversions* are `N × observed uplift in the same top share of the hold-out`
  (`policy.py`). The engine's interval is a bootstrap; the prototype cannot resample, so its interval
  is Newcombe's over the pro-rated top-share counts. Shape, not method.
- *Qini and AUUC at decile resolution.* The engine integrates over every row (101 curve points); the
  prototype has ten deciles, so its curve has 11 points. The definitions are the same.
- *Segment counts move with the cuts* in Advanced by the share of the predicted-uplift curve (linear
  through the decile means) above each cut; at the default cuts they are the sample's.
- *Campaign results is shown on Win-back only.* The page applies to any scoring run with a control
  group; the prototype shows it where plan B's acceptance test puts it.

`tests/prototype/uplift.test.mjs` (26 tests) drives all of this. `consistency.test.mjs` adds eight:
the label, thresholds and defaults (the sure-thing cut too) against the yaml `uplift:` block; the
control share against `control_group_fraction`; `SEG_LABELS`, `SEG_ACTIONS` and `NOT_CAUSAL_NOTE`
against `engine/uplift/contracts.py`; the four AUUC sentences against `metrics.py`; the check
findings against `checks.py` and the Campaign results summary against `incrementality.py`, both
ways (every template the page carries is the engine's, and every sentence those engine functions
write is carried), so a wording change on either side fails a test; and the internal arithmetic. The
five that read Phase 3b files **skip**, with the reason, on a branch that does not have them yet, and
`PRODUCT_ROOT=<checkout>` points them at another checkout (all 76 pass against the Phase 3b tree).

**Review round 1** fixed: the plain win-back sample had become an uplift run (uplift is now the
opt-in above); uplift reached other use cases with win-back's numbers (now Win-back only, and a
too-small upload is stopped by TREATMENT_ARM_TOO_SMALL); the check and summary wording differed
from the engine (now the engine's, pinned by the two tests above); the null-uplift example survived
navigation; an uploaded outcomes file showed the sample's figures; `sure_thing_min_probability` and
`uplift_at` 20% / 30% were missing. It also records a correction Revision 4 made without saying so:
the "consistent numbers" list below read *91% pass rate against an 85% threshold*; the threshold has
been **75%** (`generative.reference_set.pass_threshold: 0.75`) since Revision 3, and the list now says
so.

**Review round 2** fixed, each with a test that fails without the fix: Campaign results for a Phase 1
scoring run counted the seed list's 12,650 treated and control customers for a 12,480-row run (now the
run's own counts); after the campaign file with treatment *None*, *use the sample dataset* stayed on a
manual Uplift with no treatment picker and could not run (a sample file now re-detects the type); an
uploaded file's run step claimed *6 checks passed · AUC 0.52*, the sample's (now the checks it got);
the hold-out share was ignored and Model and Output always said 96,000 (now sized by it); emptying a
non-nullable uplift number stored `null` and showed it (now its default); the upload checks panel
dropped TREATMENT_NOT_BINARY when it passed (now six rows); run stamps spelled *Sept* beside *Sep*
(now one month table).

---

## What did not change

The lifecycle overview, the seven use-case definitions, the eight advanced-settings
stages for predictive use cases, the three-state Setup → Running → Results flow, the
Data / Model / Output pages, the colour tokens (`#2F6FDB`, `#7A55D3`, `#12957F`), dark
mode and the mobile layout. `tests/prototype/existing.test.mjs` pins each of these.

## Illustrative numbers

Prototype values are sample values; the built product shows real ones. The new screens
use one consistent set, checked by `tests/prototype/consistency.test.mjs`:

- 20,000 customers, 6 monthly snapshots → **120,000 rows**
- **96%** of event rows match a customer, on every event table
- 16 suggested features − 2 dropped → **14 features**
- **11.8%** positive overall (10.9 / 11.4 / 11.6 / 12.0 / 12.3 / 12.6 per snapshot) →
  **14,160 positive examples**
- 5 documents, **122 pages**, **1,600 chunks**, 300 reference questions, 91% pass rate
  against a 75% threshold
- 12 message templates: 7 approved, 3 pending review, 2 blocked; the holdout adds up
  (1,450 control + 1,850 suppressed + 11,200 written to = 14,500 eligible)
- RCA segments: 2,180 (32.1%) high, 3,410 (50.1%) medium, of the 6,800 at risk
- **Uplift hold-out** (Revision 4): 320K rows × 0.30 = **96,000**, ten deciles of 9,600 (8,640
  treated, 960 control — the 10% control share). Treated conversions per decile 1508, 1218, 1060,
  949, 861, 798, 726, 670, 615, 512; control 38, 46, 53, 58, 61, 65, 67, 69, 72, 77; predicted uplift
  0.142 … −0.022. They give 10.32% treated, 6.31% control, an average effect of **+4.01 pts**,
  qini(1) = 0.03607, **AUUC 0.0125** (stored 95% CI 0.0098 to 0.0151) and **Qini coefficient 0.0113**
  (0.0088 to 0.0137) by the trapezoid rule, and **+13.50 pts** in the top 10% (11.87 to 14.83),
  +11.40 in the top 20% (10.26 to 12.39), +9.85 in the top 30% (8.91 to 10.69). Training base rate
  9,523 / 96,000 = **0.099**, the default sure-thing cut.
  At another hold-out share the deciles scale (50%: 160,000 rows, deciles of 16,000; AUUC still 0.0125,
  interval 0.0104 to 0.0145). Null example: AUUC 0.0006 (−0.0021 to 0.0034). TREATMENT_NOT_RANDOM: AUC 0.52 random, 0.74 targeted.
- **Segments** of the 14,500 scored: 5,220 persuadables, 2,610 sure things, 5,510 lost causes, 1,160
  sleeping dogs; of the hold-out: 57,600 / 9,600 / 17,600 / 11,200. Budget **4,000** → N = 4,000
  (stop: budget) of 4,032 eligible persuadables, about **405** expected incremental conversions.
- **Campaign of 1 May 2026** (the same holdout as the copy block): 1,232 of 11,200 treated (11.0%)
  against 109 of 1,450 control (7.52%) → **+3.48 pts** (95% CI 1.91 to 4.86), +46.3% relative,
  **390** incremental conversions (213 to 545), p = 5.0e-5, 1,850 suppressed. Mature on 30 Jul 2026;
  a list scored on 23 Sep 2026 is available on 22 Dec 2026. Checked against an independent
  Wilson/Newcombe oracle.

Anything the product would have to invent is still `—`.

## Running it

```bash
open marketing-ai-prototype.html          # no build step, no server needed
make prototype-test                       # 76 jsdom tests (5 skip until Phase 3b's engine/uplift is present)
make prototype-screenshots                # docs/prototype/*.png, desktop and mobile
```

`Makefile` and `README.md` are shared files under `PARALLEL_WORK_PROTOCOL.md` §4, so the
targets and the notes above live inside the Phase 2 and Phase 3a marker blocks.
`tests/prototype/` and `docs/prototype/` are new directories and belong to no other branch.

Screenshots of every new state, desktop (1440px) and mobile (390px), are in
`docs/prototype/`, plus three dark-mode shots. Revision 4 added `19-uplift-setup-treatment` to
`24-campaign-results-mature` (and `22-uplift-output-desktop-dark`); review round 1 reshot 19–22 and
24, whose screens changed (23 did not); review round 2 reshot 11, 16 and 23, whose run stamps
changed (19–22 were reshot too and came out byte-identical); `ONLY=19,20 node
scripts/prototype_screenshots.mjs` reshoots just those. The script is an ES module, so `NODE_PATH`
does not reach it: `playwright` must resolve from a `node_modules` above the checkout. They were captured in a sandbox with no
outbound access to Google Fonts, so they render in the stylesheet's fallback stack
rather than Inter — the layout is exact, the typeface is not. The font link itself is
unchanged.
