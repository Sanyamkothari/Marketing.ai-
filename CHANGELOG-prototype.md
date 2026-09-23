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
  against an 85% threshold
- 12 message templates: 7 approved, 3 pending review, 2 blocked; the holdout adds up
  (1,450 control + 1,850 suppressed + 11,200 written to = 14,500 eligible)
- RCA segments: 2,180 (32.1%) high, 3,410 (50.1%) medium, of the 6,800 at risk

Anything the product would have to invent is still `—`.

## Running it

```bash
open marketing-ai-prototype.html          # no build step, no server needed
make prototype-test                       # 42 jsdom tests
make prototype-screenshots                # docs/prototype/*.png, desktop and mobile
```

`Makefile` and `README.md` are shared files under `PARALLEL_WORK_PROTOCOL.md` §4, so the
targets and the notes above live inside the Phase 2 and Phase 3a marker blocks.
`tests/prototype/` and `docs/prototype/` are new directories and belong to no other branch.

Screenshots of every new state, desktop (1440px) and mobile (390px), are in
`docs/prototype/`, plus two dark-mode shots. They were captured in a sandbox with no
outbound access to Google Fonts, so they render in the stylesheet's fallback stack
rather than Inter — the layout is exact, the typeface is not. The font link itself is
unchanged.
