# Marketing AI — Plan O: Data Doctor, the one data agent

**Companion to:** the technical design (`DATA_DOCTOR_AGENT_DESIGN.md`, cited as "the design"), Plan L
(`MARKETING_AI_PLAN_L_DATA_DOCTOR.md`), Plan G's agent rules (`docs/AGENTS.md`), Plan K
(`MARKETING_AI_PLAN_K_PROPOSAL.md`, `PLAN_K_TECHNICAL_DESIGN.md`), Plan M (`MARKETING_AI_PLAN_M_SIMPLE_UI.md`),
`docs/DATA_DOCTOR.md` and `PARALLEL_WORK_PROTOCOL.md`.
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1800 … 1899 (proposed, not claimed; M230 claims it once the plan is approved)
**Milestones:** M230 – M250 (Plan N proposes M200 – M225 and DEC-1700 … 1799)
**Status:** approved by the product owner on 12 October 2026: decisions O1–O19 as recommended. Build order: the foundation (M230–M235) starts now; the Data Doctor screen (M247) waits for the visual redesign (M185–M188).

**Added at approval (product owner, 12 October 2026): new patterns, with permission, then rules.** When the AI meets a value pattern no rule covers (for example `haan` / `nahi` / `haa` in a yes/no column), it may propose a fix, but only as one of the fixed step kinds, never code. The proposal cites a look at that column, is tried on 1,000 rows, and shows before and after, the rows it changes and the values it could not map; O10's change-share guard applies; it is never pre-ticked and is applied only after the person approves (recorded with the user id). An approved fix is remembered for that client (visible, revocable). When the same AI-proposed pattern is approved repeatedly, it is surfaced to the product team as a candidate rule (for example a lexicon entry), promoted into the deterministic rules by a reviewed change, so the AI is no longer needed for it. When no step kind can express the fix, the agent says it cannot do it safely yet and records a tool request. Scope: M245 (notice what I don't know) and M248 (evals, including the promotion report).

> **How to use this document.** The design says *what* Data Doctor is. This plan says *in what order* to build it,
> *how we know* each step is done, and *what it costs*. Read the design first. Where this plan is silent, follow
> the design, then Plan L, then the protocol. Where something is impossible or clearly wrong, stop, explain, and
> propose the smallest change. Evidence notes are cited as in the design: [REQ], [INV], [RDY], [RUN], [RES].

---

## Plan O at a glance

**The problem.** The product prepares data in many places that do not know about each other ([INV] "Overlaps"):

- six data-entry screens;
- three proposal and approval models;
- leak checks in five places;
- **seven readiness verdicts**, none of which promises the run will finish.

Validation can pass and the run still fail at split or training ([RDY] §1.7, nine gaps). A file in dollars is read
as rupees ([REQ] 7). Several files cannot be joined in general, and overlapping monthly exports are counted twice
([INV] gaps 2–3).

**The decision.** Build **one agent, Data Doctor**, whose tools are every data capability the product has. It has
an optional **goal brief**. It keeps one **case** per piece of work and writes one **prescription** that replays at
training and at scoring. It issues one **readiness report** whose `ready` means the run will not fail for a data
reason. It owns the time settings, so nobody guesses how long training takes or which dates to use. It asks, seeks
approval and states doubts by one protocol, and it remembers answers per client. This is the product owner's brief
([REQ] 1–10).

| Phase | Gate (week) | What the product can do at the end | Milestones | Effort (person-weeks) |
|---|---|---|---|---|
| **1 — Foundation** | 6 | One case per piece of work. One tool registry, with a policy for every proposal kind. One readiness report with X-probes. A visit that resumes. All behind a switch. | M230–M235 | 15.0 |
| **2 — The tools wave** | 12 | Every capability is a tool. Several files join and stack without double counting. The outcome can be built from values, events or thresholds. More formats read safely. | M236–M240 | 15.5 |
| **3 — Locale and currency** | 14 | Lakh, crore, `/-`, `haan/nahi`, GSTIN, PIN. Hinglish headers on. A workspace currency; dollars noticed, never mixed with rupees. | M241–M242 | 6.0 |
| **4 — Prescription, time and replay** | 18 | One prescription replays at training and scoring, for files and built datasets. Training time is a plain choice with an honest estimate; dates, cut points and windows are proposed, not guessed. Drift and new values are noticed, asked once, and remembered. | M243–M245 | 10.0 |
| **5 — Ask, approve, doubt** | 21 | One Data Doctor screen, in three places, with questions, approvals, doubts, the chat and the case timeline. | M246–M247 | 7.0 |
| **6 — Evals and benchmark** | 22 | pass^5 on AI proposals, harm metrics, 20 Indian files by another author, cost per case. | M248 | 3.5 |
| **7 — Merge and switch-over** | 23 | Guided setup and the Doctor page are Data Doctor. Old routes redirect. One verdict. | M249 | 3.0 |
| **8 — The pitch** | 24 | A demo and a proof report with measured figures only. | M250 | 2.0 |

**Team assumed:** 3 engineers and 1 data scientist, as in Plan K. **Total about 62 person-weeks** (55 engineering,
7 data science), about **24 calendar weeks**. With a fourth engineer, about 19 weeks. **All estimates are ours and
unmeasured.** Plans L and M were built faster than such estimates, by agents; that says nothing about review,
usability testing or pilot time, which dominate here.

**What the approvers must answer first:** decisions O1–O19 (§1.2).

---

## 1. The decision this plan implements

### 1.1 Why now

1. **The owner asked for one agent with every feature as a tool** ([REQ] 1), able to ask, seek approval and doubt
   ([REQ] 3), extensible ([REQ] 4) and pitchable as the headline feature ([REQ] 5).
2. **The parts exist.** Plan G's loop works on data. Plan K's registry, policy and ledger govern. Plan L's Doctor
   functions diagnose several files ([RUN] "Bottom line"). Most of the catalogue is existing tools or wrappers:
   22 existing, 29 wrappers, 29 new (the design §5.5).
3. **"Ready" is not yet a promise.** Seven verdicts exist, and nine conditions pass validation and then fail
   ([RDY] §1.7). A readiness report computed on the prepared copy, with probes, closes that ([RDY] §5).
4. **The market has no step-and-replay agent for non-technical users that refuses to guess on drift** ([RES] TL;DR
   2, **[S, M]**). Our safety model is already stricter than the market's ([RES] §1.2). Plan O makes it one product.
5. **India first.** Lakh and crore, Hindi booleans, GSTIN and voter ID are not handled anywhere in the repository
   ([RES] §4.1, grep **[O]**). DPDP Rule 8(3) asks for a one-year processing log ([RES] §4.2, **[S, H]**).

### 1.2 Decisions to confirm

| # | Question | Recommended answer |
|---|---|---|
| O1 | One agent? | **Yes.** Guided setup becomes Data Doctor with a goal brief. The Doctor page becomes Data Doctor without a goal. One case, one memory, one prescription (the design §3, §14). |
| O2 | The name in the product? | **"Data Doctor" everywhere.** "Guided setup" is retired as a name. This settles Plan L's open decision §12. |
| O3 | Reshape level (`combine_rows`, `unpivot`, the new `keep_latest_per_id`) on by default? | **Yes, with guards.** Today it is on only for Win-back (`configs/use_cases/retail_win_back.yaml:81`; default `[clean, derive]`, `configs/engine.yaml:351`). Guards: a blocking question, the point-in-time leak probe, never ticked, the receipt. |
| O4 | Hinglish headers on by default? | **Yes.** Header reading leaves `agentic.enabled` (DEC-1409 (f)). Its suggestions stay "please check" and never ticked. The AI second opinion stays behind the switch. Changed golden digests are listed in the DEC. |
| O5 | A workspace currency setting? | **Yes, default INR.** It replaces the hard-coded rupee in `ui/format.js:72` `rupees()`. Every money column records its unit. |
| O6 | How are currencies converted? | **Only at a dated rate the person gives, as an approval.** The product never fetches a rate. Mixed currencies in one column stop. |
| O7 | MCP exposure of Data Doctor tools? | **None in Plan O.** Row-level tools are `session-only` and never listed. Aggregate verdicts may be proposed in a later plan. |
| O8 | Which AI service runs Data Doctor's AI parts? | **An India-hosted option is offered and recommended:** Amazon Bedrock in an India region, in the client's account. A third party only through the egress gate, with `summaries_only` as its default. |
| O9 | How are approvals recorded? | **Always with the user id in the case timeline. Also in Plan K's signed ledger when `agentic.enabled` is on.** The ledger stays additive. |
| O10 | How far may an AI-proposed step change a column? | **Warn above 30% (Plan L §7), refuse above 60%** unless the person re-proposes it as their own edit. |
| O11 | When does Data Doctor ask? | **Only when options change the data materially** (default 0.5% of rows or 0.5 points of label rate). Budget: 3 questions per file on average. |
| O12 | Are answers remembered? | **Yes, per client, as standing answers:** applied pre-ticked and visible, revocable in Settings, never for APPROVE-class items. |
| O13 | Processing-log retention? | **365 days by default**, configurable; data follows upload retention (DPDP Rule 8(3)). |
| O14 | How do we switch over? | **Behind `data_doctor.unified: false` until the parity checklist (§3.2) is met; then on.** Old routes stay as adapters for one major version. |
| O15 | More input formats? | **JSON, JSONL, and a `.gz` or `.zip` holding one table: yes.** `.xls`, `.xlsb` and password workbooks stay refused until a dependency review. |
| O16 | Multi-valued outcomes (Active / Churned / Suspended)? | **Yes, through an approved outcome definition**, never through a recipe step (Plan L promise 6; the design §9.2). |
| O17 | Separation of duties? | **Yes for APPROVE-class items** when `governance.approval_required` is on, through `engine/approvals.py:510`. |
| O18 | What may the pitch claim? | **Only the three proof points measured today (P1–P3) until M250's report measures P4–P6**, each with its caveat. The design §1.2 lists what we never claim. |
| O19 | Who sets training time and the data's time settings? | **Data Doctor, as part of the goal brief** ([REQ] 10). The budget is a choice of Quick, Balanced (recommended) and Thorough with "about N minutes", never a raw minutes field in Data Doctor. As-of date, cut points, maturity and cadence are proposed, asked when unsure, and remembered per client. |

### 1.3 What stays out of Plan O

- **Fuzzy person matching across files.** Exact keys after normalising only (Plan L §8).
- **Scanned PDFs, photos, free-form documents.**
- **Free-text features.** Text columns stay hidden or redacted ([INV] gap 13).
- **SaaS and CRM connectors, custom SQL, incremental loads** ([INV] gap 14). A connector plan may follow.
- **A vendor export fingerprint library** ([RES] D5). It is valuable; it needs real exports first. Listed in §7.
- **An OpenLineage export** ([RES] D11).
- **Any agent that writes into a client's systems** (Plan K's K1; Plan N's mandate is separate).

---

## 2. What the product can do at the end of each phase

| After | A person can |
|---|---|
| Phase 1 | (with the switch on) open a case and add several files. See one readiness verdict that names every probe. Close the browser and come back to the same state. |
| Phase 2 | join a customer list to a churned-IDs list. Stack three overlapping monthly exports without double counting. Turn Active/Churned/Suspended into an outcome they agree to. Read a JSON export. |
| Phase 3 | upload `₹2.5 L` and `haan/nahi` and get numbers and yes/no. See GSTIN treated as personal data. Upload a dollar file in a rupee workspace and be asked, not misled. |
| Phase 4 | choose Quick, Balanced or Thorough, and see the same "about N minutes" on the progress bar. Be told the newest month's outcomes are not final, and leave it out of testing. Train from a built dataset with its after-build fixes. Score next month's files with everything replayed. Be told "this column's values are about a lakh times larger than last month". |
| Phase 5 | use one screen with questions, approvals, doubts and a chat, from the Data page, the wizard or a goal |
| Phase 6 | (the team) see pass^5, harm metrics and cost per case on a benchmark that includes files written by someone else |
| Phase 7 | find no separate Guided setup or Doctor page; old links still work |
| Phase 8 | (sales) run the demo and quote only measured figures |

---

## 3. Principles and rules of work

### 3.1 How Data Doctor is built

- **One registry, three kinds:** READ, PROPOSE, ASK. APPLY is never a tool (the design §5.1).
- **Rules first.** The AI proposes, words, reranks and converses. It never decides AUTO, ticks, approves or runs in
  replay (the design §6.2).
- **Every PROPOSE kind has a default-deny policy rule set**, mutation-tested (the design §5.4).
- **Every step is stateless**, from a fixed list, with a receipt. No code, no SQL, no free expression from the AI.
- **Every number shown is evidence** from a tool, by content-hash evidence id.
- **Nothing is silent.** Rows in and out are counted at every step, join, stack and de-dup.
- **It works with no AI service.** Only CONVERSE, the second opinion and `explain` need one.

### 3.2 Rules of work

1. **Additive.** New modules live under `engine/doctor/` and `api/routes/cases.py`. Existing modules gain wrappers,
   not rewrites. With `data_doctor.unified: false`, every existing test and golden digest is byte-identical.
2. **One switch, then switch-over.** `data_doctor.unified` stays off until the **parity checklist** is met:
   - Plan G's 22 and the 27 Hinglish golden digests are byte-identical through the new runtime, except changes a DEC
     lists (O3, O4);
   - every Guided setup and Doctor integration test passes through the adapters;
   - the Plan L M160 benchmark is at least as good (30 of 30 to Approve, at most 0.33 questions per file);
   - the readiness invariant holds on every benchmark file (the design §10.3);
   - the M247 journeys pass in jsdom and Playwright.
3. **Never loosen a test.** A failing existing test is fixed in the code or recorded as a deliberate change in a DEC
   with the reason. Thresholds are never lowered to pass.
4. **Keep Plan L's promises** (Plan L §5.2; the design §15). A milestone that would break one stops and asks.
5. **Protocol.** Claim DEC-1800 … 1899 in `PARALLEL_WORK_PROTOCOL.md` §4 at M230. The `PLAN-O (data doctor)` block
   follows the last plan block present at M230 in every shared file (Plan M's today; Plan N's if it claims first).
   Each milestone appends its DEC entry and its README row.
6. **Docs move with the code.** `docs/DATA_DOCTOR.md` (plain words) and `docs/AGENTS.md` (developer rules) are
   updated in the milestone that changes behaviour. `docs/API.md` is regenerated.
7. **The redesign.** UI work uses the M185–M188 components (`theme.css`, `components.js`) when they have landed,
   otherwise `ui/modules/agent/styles.js`. No new palette (DEC-1122 light only).

---

## 4. Phases

**Effort basis.** As Plan K: each engineer keeps back 0.5 person-weeks a week for reviews and fixes, leaving 2.5
engineer-weeks of milestone work per week across three engineers. The data scientist has 0.8 a week. Figures include
tests, docs, the DEC entry and shared-file paperwork. "DS n" is the data-science share inside the figure. All
estimates are ours and unmeasured.

### Phase 1 — Foundation (gate at week 6)

**Goal.** The base every tool stands on: the case, the registry, the policy, the readiness report and the visit.

#### M230 — Plan O set-up

- **Scope.** Claim the DEC range. Add the `PLAN-O` shared-file blocks, `PLAN_O_CODES`, README rows (pending) for
  M230–M250, and a "Plan O" section skeleton in `docs/DATA_DOCTOR.md`. Add the switch `data_doctor.unified: false`
  to `configs/engine.yaml` defaults.
- **Acceptance.** `tests/unit/test_shared_file_markers.py` and `test_help.py` green; nothing else changes.
- **Effort:** 0.5. **Depends on:** approval.

#### M231 — The case

- **Scope.** Contracts `Case`, `CaseFile`, `TimelineEvent` (the design §4.1). Store under
  `cases/<client_id>/<case_id>/` with a version check. Per-client scope. A `cases` store in
  `engine/privacy/layout.py`; erasure and retention, with the processing log kept `privacy.processing_log_days`
  (default 365). Routes `POST /cases`, `GET /cases/{id}`, `POST /cases/{id}/files`, `GET /cases/{id}/timeline`.
  Adapters open an existing Guided session or a Doctor diagnosis as a case, read-only.
- **Where.** `engine/doctor/case.py` (new), `api/routes/cases.py` (new), `engine/privacy/layout.py`,
  `engine/privacy/erasure.py`.
- **Acceptance.**
  - Round-trip property test on `Case`.
  - Two concurrent writers: the second gets `409 CASE_VERSION_CONFLICT`.
  - Another client's case is not readable (`404 CASE_NOT_FOUND`).
  - Erasing an upload removes its file entry and evidence; erasing a person removes `correct_cells` rows and
    standing answers holding their id.
  - The retention job keeps the timeline for the configured days and deletes data with its upload.
  - With the switch off, the full suite is unchanged.
- **Effort:** 3.0. **Depends on:** M230.

#### M232 — One tool registry

- **Scope.** `engine/doctor/registry.py` with Plan K's `AgentTool` shape and kinds READ, PROPOSE, ASK.
  `DataToolContext` (the design §5.2). Content-hash evidence ids. Exposure `session-only` or `aggregate`. A
  `RoutePolicy` per tool. Register the 22 existing tools and the first wrappers (#10, #29, #32–34, #36–38, #40, #71
  in the design §5.5). Plan G's `chat_turn` calls tools through the registry.
- **Acceptance.**
  - Every registered tool is in the egress canary and passes in both modes.
  - The same input gives the same evidence id across runs and processes.
  - A test lists the MCP surface and finds no `session-only` tool.
  - The 22 + 27 golden digests are byte-identical through the registry.
  - A Viewer calling a PROPOSE tool is refused per call.
- **Effort:** 3.0. **Depends on:** M230.

#### M233 — A policy for every proposal kind

- **Scope.** Data rule sets in `configs/agentic/policy.yaml` for every PROPOSE kind: allowed step kinds, never the ID
  or outcome, PII drop-only, levels, never-ticked kinds, the setting allow-list, APPROVE-class kinds. The rules
  hard-coded today (`CHAT_STEP_KINDS` `loop.py:114-130`, `NEVER_TICKED_STEPS`, `setting_allowed`, the PII refusal,
  `check_recipe`'s ID and outcome rule) are read from the rule sets, and the code keeps them as a second check.
- **Acceptance.**
  - A PROPOSE tool with no rule set is refused with `POLICY_NO_RULES`.
  - The mutation test (`engine/agentic/evals.py:345`) kills every mutant of every data rule.
  - On a fixture set, every refusal today is still a refusal, with the same code.
- **Effort:** 2.0. **Depends on:** M232.

#### M234 — The readiness report

- **Scope.** `DataReadinessReport` ([RDY] §5; the design §10) and `check_readiness`. The 15 X-probes. The verdict.
  Stored in the case and copied into the run directory beside `data_recipe.json`. Every `use_case_type`.
- **Acceptance.**
  - For each of the nine gaps of [RDY] §1.7, a fixture passes `check_plan` today and fails the run. The report on it
    is `not_ready` and names the probe.
  - Property test: `verdict == ready` implies the dry run passed and no probe failed.
  - On the 30 Plan L benchmark files, every `ready` file's split and training finish.
  - The report agrees with `POST /runs`'s own `ValidationReport` on every code they share.
- **Effort:** 3.5 (DS 1). **Depends on:** M232.

#### M235 — The visit and the goal brief

- **Scope.** The visit state machine (the design §3.4) after `engine/agentic/planner.py`: persisted per transition,
  resumable, idempotent. CONVERSE is Plan G's `chat_turn`. `load_goal_brief` from the use-case configuration. Mode
  without a goal ("what can this power", "what is missing"). Setting or switching a goal on a live case.
- **Acceptance.**
  - Kill the process at every state; the visit resumes with nothing lost or repeated.
  - With no AI service, every state completes.
  - Setting a goal mid-case keeps every decided item and asks nothing again.
  - Two goals on one case keep separate goal sections; switching back restores the first.
  - `brief_hash` is stable for an unchanged configuration.
- **Effort:** 3.0. **Depends on:** M231, M232.

### Phase 2 — The tools wave (gate at week 12)

**Goal.** Every capability is a tool; the big gaps (joins, stacks, outcomes, formats) are closed.

#### M236 — Wrapper tools and `explain`

- **Scope.** The remaining wrappers: `propose_role`, `propose_key`, `propose_mapping`, `check_leaks` (one leak
  verdict over five sources), `find_personal_data`, `preview_connection`, `propose_import`, `check_refresh`,
  `preflight`, `check_uplift_data`, `read_run_checks`, `request_approval`, `second_opinion`, `propose_intake`.
  New: `explain`, through the claims verifier with data claim types (count, share, rate by group, "changed N of M").
- **Acceptance.**
  - Each wrapper's output equals the wrapped function's on fixtures.
  - `propose_role` and `propose_mapping` land pending, `check`, never ticked.
  - `explain` refuses a claim not backed by evidence and falls back to the rules' narrative.
  - The injection canary passes for every new tool.
- **Effort:** 3.0. **Depends on:** M233.

#### M237 — Several files

- **Scope.** `join_files` (per-customer file to per-customer file, and outcome lists), `stack_files` (de-duplicates
  overlapping exports on key, date and all columns; conflicting duplicates become one question), the `normalise_key`
  step (prefix strip, leading zeros, case, spaces, phone to `+91` E.164), and `build_dataset`, which drives the
  builder (closes DEC-1550 and DEC-1578).
- **Acceptance.**
  - A customer list plus a churned-IDs list plus two CRM exports join, with exact counts on the receipt.
  - Three monthly exports that overlap by one week each are stacked; each overlapping row is counted once, and the
    receipt lists the duplicates removed.
  - `CUST-00123` and `123` join after an approved `normalise_key`; nothing joins on a fuzzy match.
  - A join or de-dup that drops more than 2% of rows is an APPROVE-class item.
  - Plan L M155's journey (customer list, three bills, tickets) trains with no Manual setup.
- **Effort:** 4.0. **Depends on:** M236.

#### M238 — Row and value tools

- **Scope.** `keep_latest_per_id` (reshape level, point-in-time), `impossible_values` (future dates, negative tenure or
  amount, 2099, age over 120), `synonym_clusters` (rules, emits `map_values`), `detect_units` (emits `convert_units`),
  `split_file`.
- **Acceptance.**
  - Each new step: stateless property test, receipt, replay test, refusal on ID and outcome.
  - `keep_latest_per_id` never keeps a row dated after the snapshot.
  - `synonym_clusters` merges `Pre-paid`, `PP` and `Prepaid` only as a pending proposal.
  - `split_file` gives two prepared uploads with one recipe; a split by date keeps the point-in-time guard.
- **Effort:** 3.0 (DS 0.5). **Depends on:** M233.

#### M239 — Outcome tools

- **Scope.** `define_outcome` with presence, absence, value threshold, grace, exclude and censoring, from the
  builder's `build_labels`. `map_outcome_values` as an outcome definition of kind `column_values`.
  `check_target_type` for regression targets. Exact-text agreement for all three.
- **Acceptance.**
  - A file with Active / Churned / Suspended trains after an approved map; Suspended rows are excluded and counted.
  - "A bill paid more than 7 days late" builds as a value threshold.
  - A changed sentence records nothing (`APPROVAL_SENTENCE_CHANGED`).
  - The recipe still refuses any step on the outcome; existing tests unchanged.
  - A text regression target is `not_ready` with `X_REGRESSION_TARGET_NUMERIC`.
- **Effort:** 3.0 (DS 1). **Depends on:** M234, M236.

#### M240 — Input formats and parser safety

- **Scope.** `read_other_format` (JSON, JSONL, one-table `.gz` or `.zip`). `check_parser_loss`. Key-like columns read
  as text so leading zeros survive. Each sheet of a workbook may become its own file in the case (Plan L wanted
  this; [INV] 1.2).
- **Acceptance.**
  - A CSV with three malformed lines is `not_ready` with `X_PARSER_ROW_LOSS`, naming the lines.
  - `0000123` stays `0000123` in a key column through intake, recipe and replay.
  - A workbook with a customer sheet and a bills sheet becomes two files in one case.
  - A zip holding two tables is refused with a plain reason.
- **Effort:** 2.5. **Depends on:** M232.

### Phase 3 — Locale and currency (gate at week 14)

#### M241 — The locale profile and Indian formats

- **Scope.** `detect_locale` and the per-client profile, confirmed once (the design §8.1). `parse_number` with
  `indian_scale_words` and a trailing `/-`. Hindi and Hinglish booleans in `map_boolean`. `detect_india_ids`:
  GSTIN with its checksum (our own code; python-stdnum is LGPL, [RES] D3), EPIC, PIN as 6-digit text, phone
  normalising. Egress masks GSTIN and EPIC. Hinglish headers on by default (O4).
- **Acceptance.**
  - `₹2.5 L`, `1.2 Cr`, `Rs 3,50,000/-`, `12K` parse exactly; a column where some letters are not a listed unit is
    `check`, not `sure`.
  - `haan/nahi` and `हाँ/नहीं` map only when both sides are seen.
  - Valid and invalid GSTIN checksums are told apart on fixtures; the egress canary includes GSTIN and EPIC.
  - The profile is asked once per client and then applied, visibly.
  - Golden digests change only where a DEC lists the header switch's effect.
- **Effort:** 3.0 (DS 0.5). **Depends on:** M232.

#### M242 — Currency

- **Scope.** A `unit` on money columns. A workspace currency setting; `ui/format.js` `money(value, currency)`.
  `CURRENCY_FOREIGN` asks; `convert_currency` with an approved dated rate; `CURRENCY_MIXED` stops. Reports, net
  value and budgets show the unit, and the rate when one was used.
- **Acceptance.**
  - A dollar file in a rupee workspace asks; "report in dollars" shows dollar amounts, labelled.
  - An approved conversion records rate, date and source, and every report that uses it shows them.
  - A column holding `₹` and `$` stops and is never summed.
  - An INR workspace's screens are byte-identical to today (the format test's no-currency-sign rule still holds).
- **Effort:** 3.0. **Depends on:** M241.

### Phase 4 — Prescription, time and replay (gate at week 18)

#### M243 — The prescription

- **Scope.** The prescription envelope (the design §9): intake decisions, per-file recipes, build spec, outcome
  definition, locale, post-build recipe, column contract, goal sections; one hash. Replay at **training** and
  scoring, for files and built datasets, including scheduled firings. An existing `DataRecipe` reads as a
  prescription with one part, so models trained today keep scoring unchanged.
- **Acceptance.**
  - A built dataset trains with its post-build recipe (the gap in [INV] §8 is closed).
  - Next month's training file replays every part.
  - Scoring with a single-file recipe is byte-identical to today.
  - A contract breach that breaks a step is `PRESCRIPTION_DRIFT`; one that breaks no step is a warning and a question.
  - The hash is stable across processes and changes when any replayed part changes.
- **Effort:** 4.0. **Depends on:** M237, M239, M241.

#### M244 — Time settings

- **Why.** "We do not know what to expect" ([REQ] 10). Today the budget rule only shortens or lengthens the search by
  file size (`engine/agent/recommend.py:317-341`). Time-split failures and immature labels are among the gaps where
  validation passes and the run fails ([RDY] §1.7, gaps 1, 2 and 6).
- **Scope.**
  - `estimate_training_time`: minutes from rows × columns × allowed model families × this deployment's past runs,
    read from the stored stage durations M189's progress estimate uses (`engine/pipeline.py:353-370`, `:705`). With
    no history, the public library's runs are the prior, and the estimate says so.
  - `propose_training_budget`: the point of diminishing returns from past runs' score against time; three choices,
    Quick, Balanced (recommended) and Thorough, each with "about N minutes" and what it buys. It replaces
    `_time_rules`.
  - The chosen budget and its expectation feed M189's progress bar and ETA. FOLLOW-UP records actual against
    expected per stage.
  - `propose_time_settings`: the as-of date, feasible time-split cut points, the newest slice's maturity, the outcome
    window (through `define_outcome`), the lapse definition, the snapshot cadence (the design §3.5). Questions when
    unsure; standing answers per client for the first four.
- **Where.** `engine/doctor/time_settings.py` (new), `engine/agent/recommend.py`, the run's progress document,
  `configs/engine.yaml` (the diminishing-returns step and the three choices' ratios).
- **Acceptance.**
  - Data Doctor never shows a raw minutes field; the three choices each show "about N minutes".
  - The progress bar's first ETA equals the promised expectation for the chosen budget.
  - After each run, actual and expected are recorded per stage; on a replayed history of ten runs, the median
    actual-over-expected ratio is within 0.8–1.25.
  - A file whose newest month is immature asks "Leave it out of testing?"; leaving it out makes `X_LABEL_MATURITY` pass.
  - A time split whose cuts would leave a part empty is never proposed (`X_TIME_CUTS_FEASIBLE`).
  - With no run history, the estimate is labelled "from similar public datasets", and Balanced is `check`, not `sure`.
- **Effort:** 3.0 (DS 1). **Depends on:** M234, M235, M239; M189 (progress estimate, another branch) for the ETA
  link, otherwise the expectation is shown before the run only.

#### M245 — Notice what I don't know

- **Scope.** The detectors of the design §7.1, `drift_check` on the prescription's contract, standing answers per
  client, and a Settings page to revoke them.
- **Acceptance.**
  - Each detector's fixture produces exactly one question.
  - Next month, the answer is applied as a pre-ticked, visible item; revoking it makes the question come back.
  - An APPROVE-class answer is never stored as standing.
  - A lakh switch (values about 10⁵ times larger) and a doubled yes-rate are both flagged on replay.
- **Effort:** 3.0 (DS 0.5). **Depends on:** M243.

### Phase 5 — Ask, approve, doubt (gate at week 21)

#### M246 — The protocol

- **Scope.** Questions with the impact rule and "I don't know"; the APPROVE-class list; doubts in three parts;
  exact-text consent for meaning-bearing items; user id on every decision; ledger kinds and body keys when
  `agentic.enabled`; receipts; separation of duties; the question budget; the AI change-share guard.
- **Acceptance.**
  - An ambiguity below the impact threshold is decided, with a stated, revocable assumption.
  - A person cannot approve their own APPROVE-class item when separation is on.
  - Ledger entries verify; with the switch off, the timeline holds the same record.
  - An AI step changing 70% of a column is refused (`AI_STEP_CHANGE_TOO_LARGE`); at 40% it carries a warning.
  - Every number in a doubt is grounded.
- **Effort:** 3.0. **Depends on:** M233, M235.

#### M247 — One Data Doctor screen, three places

- **Scope.** The component of the design §13: checklist, chat, timeline, questions, approvals, doubts, "What the AI
  looked at", the verdict. Opened from the Data page, the wizard's Customers step
  (`ui/modules/campaigns/wizard_views.js:104`) and a goal's setup.
- **Acceptance.**
  - Playwright journey: Data page with no goal → pick a goal → continue in the wizard on the same case, nothing asked
    twice.
  - jsdom tests for each question, approval and doubt shape.
  - Accessibility checks as Plan M M181; the click count from upload to Approve is pinned.
  - With no AI service, the chat area links to Connections and everything else works.
- **Effort:** 4.0. **Depends on:** M246; the M185–M188 components when available.

### Phase 6 — Evals and benchmark (gate at week 22)

#### M248 — Evals

- **Scope.** The design §12. `make agent-eval` with a real or recorded model. pass^5 on proposal digests. Injection
  canaries for every tool. Harm metrics. Cost per case. The extended messy benchmark, and **20 Indian files written by
  someone other than the lexicon's author**, reported separately and never used for tuning.
- **Acceptance.**
  - A report `reports/plan_o_benchmark.md` is generated by a script, as Plan L's was.
  - Harm metrics are all zero.
  - pass^5 is 100% acceptable with no harmful proposal, or the kill criterion (§9) applies to that tool.
  - The readiness invariant holds on every benchmark file.
- **Effort:** 3.5 (DS 1.5). **Depends on:** M234; the harness starts after M232, the gate run after M247.

### Phase 7 — Merge and switch-over (gate at week 23)

#### M249 — One Data Doctor

- **Scope.** The design §14. Guided setup and the Doctor page become Data Doctor. Old routes become adapters or
  redirects. The seven verdicts become views of the report. Builder screens move to Settings → Advanced. Turn
  `data_doctor.unified` on once the parity checklist (§3.2) is met.
- **Acceptance.**
  - The parity checklist is met and recorded in the DEC.
  - Every old route's tests pass through its adapter; no API is deleted.
  - `#/doctor` and the Guided setup tab land on the right case.
- **Effort:** 3.0. **Depends on:** M247, M248.

### Phase 8 — The pitch (gate at week 24)

#### M250 — Demo and proof

- **Scope.** A demo script on generated data: an Excel CRM export, three overlapping bills, a dollar file, a
  Hinglish export. Timed sessions with five or more non-technical testers (P4). A proof report with P1–P6, each
  measured or marked "not measured".
- **Acceptance.**
  - `reports/plan_o_proof.md` exists; every figure in the pitch text appears in it with its source.
  - The demo also runs with no AI service connected.
- **Effort:** 2.0 (DS 1). **Depends on:** M249.

---

## 5. Shared contracts introduced

| Contract | Introduced | Shape (all fields optional or defaulted unless stated) |
|---|---|---|
| `Case`, `CaseFile`, `TimelineEvent` | M231 | the design §4.1 |
| `DataTool` registry entry | M232 | `name`, `kind: read \| propose \| ask`, input and output models, `route_policy`, `exposure: session-only \| aggregate` |
| Data policy rule sets | M233 | one per PROPOSE kind in `configs/agentic/policy.yaml` |
| `DataReadinessReport` | M234 | [RDY] §5, verdict `ready \| ready_with_warnings \| needs_answers \| needs_approval \| not_ready` |
| `GoalBrief` | M235 | the design §3.2, with `brief_hash` |
| `OutcomeDefinition` | M239 | `kind: column_values \| event_presence \| event_absence \| value_threshold`, `map`, `grace_days`, `exclude`, `censoring` |
| Step kinds | M237, M238, M242 | `normalise_key`, `keep_latest_per_id`, `convert_currency`; `parse_number.indian_scale_words`, `parse_number.unit` |
| `LocaleProfile` | M241 | the design §8.1, each field with `source` |
| `Prescription`, `ColumnContract` | M243 | the design §9.1, `prescription_hash` |
| `TimeSettings`, `TrainingTimeEstimate` | M244 | the design §3.5: budget choice, expected and actual minutes per stage, as-of column, cut points, maturity, cadence |
| `StandingAnswer` | M245 | the design §7.2 |
| `Question` v2, `Doubt`, `ApprovalRecord` | M246 | the design §6.5–§6.7 |
| Ledger body keys and kinds | M246 | `case_id`, `upload_id`, `prescription_hash`, `recipe_hash`, `payload_digest`; `data.prescription.approved`, `data.decision` |

New codes (in `PLAN_O_CODES`, with help entries): `CASE_NOT_FOUND`, `CASE_VERSION_CONFLICT`, `CURRENCY_FOREIGN`,
`CURRENCY_MIXED`, `CURRENCY_RATE_REQUIRED`, `PRESCRIPTION_DRIFT`, `APPROVAL_SENTENCE_CHANGED`,
`AI_STEP_CHANGE_TOO_LARGE`, and the fifteen `X_*` probe codes.

---

## 6. Sequencing

### 6.1 Dependencies

| Milestone | Depends on |
|---|---|
| M230 Set-up | approval |
| M231 The case | M230 |
| M232 Registry | M230 |
| M233 Policy | M232 |
| M234 Readiness report | M232 |
| M235 Visit and goal brief | M231, M232 |
| M236 Wrappers and `explain` | M233 |
| M237 Several files | M236 |
| M238 Row and value tools | M233 |
| M239 Outcome tools | M234, M236 |
| M240 Input formats | M232 |
| M241 Locale and Indian formats | M232 |
| M242 Currency | M241 |
| M243 Prescription | M237, M239, M241 |
| M244 Time settings | M234, M235, M239; M189 for the ETA link |
| M245 Notice what I don't know | M243 |
| M246 Protocol | M233, M235 |
| M247 One screen | M246; M185–M188 when available |
| M248 Evals | M234 (harness after M232; gate after M247) |
| M249 Merge and switch-over | M247, M248 |
| M250 Demo and proof | M249 |

**Critical path:** M230 → M232 → M233 → M236 → M237 → M243 → M245, then M247 → M248 → M249 → M250. The
prescription (M243) waits on joins, outcomes and locale, so those three run in parallel.

### 6.2 Effort and capacity

| Phase | Gate (week) | Milestones | Total (DS inside) | Engineering | DS |
|---|---|---|---|---|---|
| 1 Foundation | 6 | M230–M235 | 15.0 (DS 1) | 14.0 | 1.0 |
| 2 Tools wave | 12 | M236–M240 | 15.5 (DS 1.5) | 14.0 | 1.5 |
| 3 Locale and currency | 14 | M241–M242 | 6.0 (DS 0.5) | 5.5 | 0.5 |
| 4 Prescription, time and replay | 18 | M243–M245 | 10.0 (DS 1.5) | 8.5 | 1.5 |
| 5 Ask, approve, doubt | 21 | M246–M247 | 7.0 (DS 0) | 7.0 | 0 |
| 6 Evals | 22 | M248 | 3.5 (DS 1.5) | 2.0 | 1.5 |
| 7 Merge | 23 | M249 | 3.0 (DS 0) | 3.0 | 0 |
| 8 Pitch | 24 | M250 | 2.0 (DS 1) | 1.0 | 1.0 |
| **Total** | | **21 milestones** | **62.0 (DS 7)** | **55.0** | **7.0** |

Engineering capacity is 2.5 a week, so 55 engineer-weeks take about 22 weeks of work; the gates allow about 24 for
hand-offs. The data scientist is not the constraint (7 DS-weeks against about 19 available). A fourth engineer would
shorten the plan to about 19 weeks, mostly in Phase 2.

### 6.3 Week by week (3 engineers A–C, 1 DS)

| Weeks | Engineer A | Engineer B | Engineer C | Data scientist |
|---|---|---|---|---|
| 0–1 | M230; M232 registry | M231 case | M232 wrappers | M234 probe design |
| 1–6 | M233 policy; M235 visit | M231 finish; M240 formats | M234 report and probes | M234 invariant; M248 harness |
| 6–12 | M236 wrappers; M237 several files | M238 row and value tools | M241 locale; M239 outcomes | M239 outcome counts; M238 thresholds; M241 Indian fixtures |
| 12–18 | M243 prescription | M242 currency; M246 protocol | M244 time settings; M245 detectors | M244 time estimate and diminishing-returns rule; M245 drift tolerances; commission the 20 Indian files |
| 18–22 | M247 screen | M247 screen; M246 finish | M248 harness, canaries, cost | M248 pass^5 and harm metrics |
| 22–24 | M249 switch-over | M249 adapters | M250 demo | M250 tester sessions and proof report |

---

## 7. Do not build in Plan O (and why)

- **A `script` step, SQL from the AI, or any code path.** The research's strongest finding is that constrained
  vocabularies work and free code fails ([RES] §2.2, D8).
- **Auto-applied AI steps.** The market does it (Fabric Copilot, **[S]**); our promise is stricter (Plan L §5.2.1).
- **Whole-file statistics in a recipe** (mean fill, scale by maximum). They leak across the split (DEC-1004).
- **Fuzzy person matching**, including Indian transliterated names ([RES] §4.1).
- **Fetched exchange rates.**
- **MCP exposure of row-level tools** (O7).
- **A vendor export library before real exports exist** ([RES] D5); revisit after the first pilot.
- **Jellyfish or other non-commercial model weights** ([RES] D9, CC BY-NC).
- **Loosening any existing test or threshold.**

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Scope: 80 tools is a lot.** | 51 are existing or wrappers. New tools come in waves with their own acceptance. Each milestone ships on its own behind the switch. |
| **Existing users break during the merge.** | The switch stays off until the parity checklist is met. Old routes become adapters, never deleted, for one major version. |
| **Golden digests move** (O3, O4). | Every changed digest is listed in its DEC with the reason; any other change fails the test. |
| **AI proposals are not repeatable.** | Cached by evidence hash; pass^5 is a gate; AI never decides ([RES] §2.3, up to 15 points of variance at temperature 0, **[S]**). |
| **Multi-file work widens the privacy surface** (join keys are often PII). | Join tools return counts, never keys; the egress canary walks every new tool; third parties default to `summaries_only`. |
| **Readiness probes miss a failure.** | The invariant is checked on every benchmark run; a miss is a bug with a DEC; P6 is not pitched until it holds. |
| **Old models stop scoring after the prescription change.** | A `DataRecipe` reads as a one-part prescription; byte-identical scoring is an M243 acceptance. |
| **The impact thresholds are wrong**, asking too much or too little. | Each answer logs whether it changed the data; thresholds are configuration; the benchmark reports questions per file. |
| **Time estimates are wrong on a new deployment** (no history). | The prior is the public library's runs, labelled as such; Balanced is `check` until history exists; actual against expected is reported per run. |
| **The redesign (M185–M188) slips.** | M247 falls back to existing styles; restyling later is a small change. |
| **The 20 Indian files are written by someone too close to the team.** | The author is named in the report; the files are frozen before any tuning; the split is reported separately. |
| **Over-claiming in sales.** | O18: only figures in the proof report; the design §1.2 "do not claim" list. |
| **Process-level locks** (`docs/AGENTS.md` §11). | The case store's version check replaces the thread lock. |

---

## 9. How success is measured

### 9.1 The pitch's proof points

| # | Proof point | Target | Measured in |
|---|---|---|---|
| P1 | Messy export shapes reaching Approve with no manual setup; questions per file | at least 30 of 30; at most 0.33 (today's figure, kept) | M248 (re-run through Data Doctor) |
| P2 | Every change a named step with a receipt; replay stops on change | demonstrable | M250 demo |
| P3 | Indian IDs masked before any prompt; Hinglish header accuracy | canary green with GSTIN and EPIC; held-out top-3 reported with its caveat | M241, M248 |
| P4 | Time from first file to first trained model, non-technical tester | under 20 minutes (Plan L §11) | M250 |
| P5 | Messy files made ready with no person beyond answering, including the 20 Indian files by another author; questions per file | at least 80%; at most 3 per file | M248 |
| P6 | Silent data loss; runs failing for a data reason after `ready` | zero; zero | M248 |
| — | Training time: actual over expected (a product measure, not a pitch claim until measured) | median within 0.8–1.25 after ten runs | M244, M248 |

### 9.2 Product measures (tested)

- pass^5 on AI proposals; injection canaries; harm metrics; cost per case.
- The readiness invariant as a property test.
- Byte-identical behaviour with the switch off; parity at switch-over.

### 9.3 Kill criteria

Each pauses the item named and is reported to the approvers:

- **pass^5 below 100%** for a tool after one fix round: that tool's AI path is switched off; its rules stay.
- **A run fails for a data reason after `ready`:** P6 is not pitched until the probe is fixed.
- **Any silent row loss on the benchmark:** the release is blocked.
- **Testers take over 20 minutes** in M250: M247's screen is re-planned before the pitch uses P4.
- **Questions per file above 3** on the Indian files: the impact thresholds are reviewed before switch-over.

---

## 10. Open questions for the approvers

1. Do you accept O1–O19 as recommended?
2. Who writes the 20 Indian files (a pilot partner's analyst, or a contractor), and by week 12?
3. Is an India-region Bedrock model available in the first pilot client's account (O8)?
4. Should a US region profile (`configs/regions/us.yaml`, labels only) be in scope, given the dollar case in [REQ] 7?
5. Who recruits the five non-technical testers for M250?
6. If Plan N is approved, should its mandate cover any Data Doctor approvals, or are they always a person's (our
   recommendation: always a person's)?
7. When do M185–M188 land, so M247 can use their components?
8. May we review a dependency for `.xls` files (O15), or do they stay refused?
9. Where does M189 (the progress estimate) stand, so M244 can share its stored stage durations and its ETA?
