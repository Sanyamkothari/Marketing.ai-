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
- 12 message templates: 7 approved, 3 pending review, 2 blocked

Anything the product would have to invent is still `—`.

## Running it

```bash
open marketing-ai-prototype.html          # no build step, no server needed
make prototype-test                       # 37 jsdom tests
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
