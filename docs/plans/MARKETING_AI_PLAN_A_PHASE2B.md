# Marketing AI — Plan A: Phase 2 Completion and Integration Hardening

**Companion to:** `plan.md` (Phase 1), `MARKETING_AI_PHASE2_PLAN.md`, `PARALLEL_WORK_PROTOCOL.md`, status report of 23 Sep 2026 (`main @ 8f0d358`)
**Owner:** Minfy — AI/ML team
**Branch:** `phase-2b-completion` — starts now; its first milestone (M34) must merge before Plan B (Phase 3b) starts
**Decision range:** trunk continues at DEC-083…099

> **How to use this document.** Everything from Phases 1, 2, 3a, 4a and the library is merged on `main` and CI is green. This plan finishes the open items from the status report so that the product works end to end *through the UI* on raw tables, and closes the engine issues the library found. It adds no new capability beyond what the Phase 2 plan already specified. Where this document is silent, follow the Phase 2 plan and the protocol.

---

## 1. Goal in one sentence

A non-technical user uploads raw tables (customers, bills, complaints) in the browser, builds a dataset with one row per customer per snapshot date, trains, and a month later scores new tables, without anyone from the ML team, and the build runs within the planned time.

## 2. Decisions this plan assumes (record each as a DEC entry in M34)

| # | Decision | Ruling assumed here | If overruled |
|---|---|---|---|
| D1 | `StageContext.primary_key` for two-column keys | Becomes `list[str]` internally; a single string is normalised to a one-item list at the edge. Behaviour for single keys is byte-identical to today. | Stop M34 and ask |
| D2 | Six inactive "features" settings (encoding, scaling, text, selection, auto FE, max features) | Stay in the schema, rendered disabled with "Coming later", excluded from the recipe hash. Not built. | Remove from schema instead |
| D3 | Second industry in `configs/` | Allowed. `configs/industries/*.yaml`, one file per industry; tests validate every industry file instead of asserting exactly one. | Keep library configs where they are |
| D4 | Phase 4a's one-line change to frozen `engine/stages/train.py` | Approved, on condition that a test proves local behaviour is unchanged when the storage backend is local. Prefer moving the upload into `ModelRegistry.register()` if that is a clean change; record which option was taken. | Move the call out of `train.py` |
| D5 | Free-text PII (complaint text, notes) | Detected at profiling and redacted in any sample shown in the UI or logs; the check `PII_IN_FREE_TEXT` is a warning, not an error. | — |
| D6 | Two PII detectors that disagree (library finding) | One detector module, one pattern set, used by both call sites. | — |
| D7 | Second fake LLM client (3a) and merging two check types (2) | Keep one `FakeLLMClient` with modes; merge the check-result types into the Phase 1 `CheckResult` contract. | — |

## 3. Milestones

### M34 — Two-column keys through every stage (do first; unblocks Plan B)

- `StageContext.primary_key: list[str]`. Build an internal row key `"|".join(values)` used only for `row_explanations.parquet` and joins; `scores.csv` keeps the original key columns as separate columns.
- `validate`: `PK_NOT_UNIQUE` checks the tuple; `PK_HAS_NULLS` if any component is null.
- `prepare`/split: for periodic datasets (manifest `snapshot_mode: periodic`), grouped split by `entity_key`, or time-based on `snapshot_date` if the use case says so. `snapshot_date` and key columns are auto-excluded from features.
- `explain`, `score`, `actions`: carry the tuple; suppression and control-group assignment are per entity, not per row (a customer is either control or not across snapshots in a run).
- Remove the 501 refusal for two-column datasets.
- Tests: the full Phase 1 fast and slow suites pass unchanged; a periodic dataset trains; no entity appears in two splits; control assignment stable per entity; `scores.csv` round-trips original IDs (including leading zeros).

### M35 — Onboarding screen wired into Setup (Phase 2 M13)

- Setup Step 1 offers **Upload a prepared file** (unchanged) or **Build from raw tables**, which loads `ui/modules/onboarding/` inline: Sources → Mapping → Features and label → Build and review, matching the reference prototype.
- Client picker in the header (default "Demo").
- "Use this dataset" fills Step 2: primary key (both columns), target, problem type, time column; Run then posts `dataset_id`.
- Score mode with a saved onboarding spec: "Upload this month's tables", mapping replayed, mapping step reopens only for a source with missing columns.
- Data page shows the lineage block (sources → mapping → spec → dataset → run).
- Browser acceptance test (slow suite): raw fixture tables → build → train → second month's tables → score → `scores.csv` keyed by the client's own IDs, with a reason on every row.

### M36 — Engine issues found by the library

1. **PII detectors (D6):** one `engine/pii.py`; both callers use it; tests cover every pattern from both old detectors; the library datasets re-run with identical or stricter flags.
2. **Dotted column names:** columns like `default.payment.next.month` break some internal paths. Sanitise to safe internal names at ingest with a stored, reversible mapping; every output (scores, reasons, reports, UI) shows the original names. Property test: arbitrary Unicode/dotted/spaced names round-trip.
3. **"auto" threshold flags every row:** define `auto` precisely: choose the threshold that maximises the configured metric on validation, subject to a flagged-rate ceiling (default 30%, configurable). If the optimum exceeds the ceiling or flags all rows, fall back to the top-decile threshold and emit `THRESHOLD_FALLBACK` with the reason. Test on the library dataset that triggered it.
4. **Free-text PII (D5):** detect in text columns at profiling; redact in samples, previews, logs and LLM inputs.

### M37 — Dataset build speed (Phase 2 M14)

- Target from the Phase 2 plan: 200k customers, 5M usage events, 12 snapshots, 60 features in ≤ 300 s on a laptop (current: 829 s, correct but slow).
- Profile first and record the breakdown in `docs/PERFORMANCE.md`. The report says most time is Parquet writing and column renaming, so expected fixes: write the final dataset with DuckDB `COPY … TO … (FORMAT PARQUET)` once instead of via pandas; do renames in SQL projection; avoid materialising intermediate DataFrames per role; join per-role results inside DuckDB; use Arrow zero-copy where pandas is unavoidable.
- The golden aggregation test and the point-in-time test must stay green; add a benchmark script with the result and machine spec in the README. Also run the Phase 1 1M-row test on a laptop and record it (open item from Phase 1).

### M38 — Configuration and settings cleanup

- Multiple industries (D3): relax the two tests; move library configs into `configs/industries/` and `configs/use_cases/`; the overview screen gets an industry selector (default Telecom).
- Inactive settings (D2): disabled with "Coming later", excluded from recipe hash; test that two runs differing only in those settings share a recipe hash.
- Frozen-file change (D4): implement the chosen option with its test.
- Merge check-result types and fake LLM clients (D7).

### M39 — Docs, CI and housekeeping

- README: every phase table current (no "pending" for built milestones); a short "What works today" section at the top mirroring the status report.
- Add the Phase 2, 3a, 4a and Evolve plan files under `docs/plans/`.
- CI: nightly slow suite on `main` (requires `main` as default branch, a manual step for the owner); library tests nightly as an opt-in job.
- Close or re-file every entry in `docs/CROSS_BRANCH_REQUESTS.md`; delete merged branches.
- `scripts/check_readme.py` added to CI (fails if README says "pending" for a milestone with passing tests).

## 4. Order and parallelism

M34 first (1–2 days; merge immediately so Plan B can start). Then M35, M36 and M37 can run in parallel inside this branch or as sub-branches (different files: `ui/`, `engine/pii.py` + ingest, `engine/onboarding/build.py`). M38 and M39 last.

## 5. Rules

- Phase 1 suites are the regression gate; no edits to existing assertions except where D1–D7 explicitly change behaviour, each with a DEC entry.
- `evaluate.py`, `explain.py` scoring logic and the champion rule stay frozen; M34 only changes how keys are carried.
- Every fix from M36 gets a regression test built from the library dataset that exposed it.

## 6. Acceptance test (overall)

In the browser, on the synthetic raw telecom fixtures: create client → upload three raw tables → accept suggested roles, mappings and features → keep the default churn definition → build (≤ 300 s at full size, measured separately) → train → next month's tables → score → download `scores.csv` with the client's own customer IDs, a band, an action and a reason for every row. All five library datasets still train with no engine code change, and the one that used to flag every row no longer does.
