# Marketing AI — Plan D: Leftovers and Hardening

**Companion to:** status report of 23 Sep 2026 (`main @ 2248cd0`), `PARALLEL_WORK_PROTOCOL.md`, Plan A, Phase 3b and Phase 4b plans
**Owner:** Minfy — AI/ML team
**Branch:** `plan-d-hardening` — starts now; runs in parallel with Plans E and F
**Decision range:** DEC-850…899
**Milestones:** M53–M58

> **Purpose.** Close every open item from the latest status report that does not need an AWS account, so that `main` is clean before a real client ever sees it. No new capability; every item below is already named in an earlier plan or in `docs/CROSS_BRANCH_REQUESTS.md`.

---

## 1. Rulings this plan assumes (record as DEC entries in M53)

| # | Item | Ruling |
|---|---|---|
| R1 | DEC-096, narrowed future-data-leak check | **Accepted with conditions:** the narrowed check stays the default for builds; the **full** check runs in the nightly suite and in the golden tests; a `full_leak_check: true` option exists and is forced on for the first build of any new onboarding recipe. The SQL guard assertion on every compiled query is unchanged. |
| R2 | Criteo licence (non-commercial) | Criteo may be used for internal validation only. It must never appear in demos, screenshots, sales material or the demo dataset (Plan E). |
| R3 | Consent salt | Must be a secret read through `engine/settings.py`, never a default in code; startup refuses production mode without it. |

## 2. Milestones

### M53 — Uplift on two-column keys, and uplift across the product

- Uplift reads keys through Plan A's key helper and accepts two-column keys (customer + snapshot date). Treatment and control assignment stay **per entity**; a customer cannot be treated in one snapshot and control in another within one campaign. Grouped split by entity.
- Phase 1's Data and Model pages recognise uplift runs: Data shows treatment/control counts and the randomness check; Model shows the Qini curve and AUUC instead of ROC/lift; Previous runs label the problem type.
- Drift check for uplift models: PSI on features (same as Phase 1) plus a check that the treatment/control ratio of new data is close to training.
- Add the six uplift check codes to `docs/DATA_CONTRACT.md`.
- Tests: two-column uplift dataset trains; no entity in two arms; Phase 1 pages render uplift artefacts; all existing uplift tests unchanged.

### M54 — Phase 4b leftovers

- **Login rate limiting:** per account and per IP, with lock-out and an audit entry; configurable; tested with a controlled clock.
- **Secret consent salt (R3).**
- **Approve / promote screens:** an Approver sees pending challengers with the head-to-head comparison (same held-out data, both metrics, differences highlighted) and approves or rejects with a reason; separation of duties is enforced (the trainer cannot approve), and it is visible on screen.
- **Background erasure:** erasure requests run as a background job with progress, a completion report, and retry of any failed store; the request is audited at start and end.
- Tests for each, including a denied-role test.

### M55 — Leak-check conditions (R1)

- Nightly job runs the full future-data-leak check on the full-size fixture.
- `full_leak_check` option in the onboarding recipe; forced on the first build of a new recipe (and shown in the build report as "full check" vs "narrow check").
- A test that deliberately introduces a leak affecting a customer *outside* the narrowed set (for example, a join-key bug) and proves the full check catches it.

### M56 — Test reliability and CI coverage

- Fix the two timing-sensitive uplift prototype tests: the helper waits for the page to settle (a "ready" signal), not a fixed delay. Run them 50 times in a loop under load to confirm.
- Run the jsdom UI tests in CI: add `npm ci` to the workflow with a cached `node_modules`; the jsdom suites must now run, not skip.
- Postgres tests in CI confirmed to run (not skip) against the CI database.

### M57 — Measurements that need a laptop or another network (manual, documented)

- **1-million-row test on a laptop** (Phase 1 plan §11): run `scripts/bench_1m.py` on a stated laptop spec; record time and peak memory in `docs/PERFORMANCE.md`.
- **Criteo uplift run** (internal only, R2): run on a machine with open internet; commit the run report to `docs/LIBRARY.md`, clearly marked "internal validation, non-commercial licence". If the network stays blocked, record that and skip.

### M58 — Documentation and repository hygiene

- Commit the missing plan documents under `docs/plans/`: Phase 2, 3a, 4a, Plan B (uplift), Phase 4b, Phase 5 design, and Plans D, E, F.
- Close or re-file all 25 entries in `docs/CROSS_BRANCH_REQUESTS.md`.
- README "What works today" section updated to the post-merge state; the README honesty check covers M53–M58.
- Delete merged branches (owner action; list them in the PR description).

## 3. Rules

- Frozen files remain frozen (`train.py` except the approved upload line, `evaluate.py`, `explain.py`, champion rule).
- No test assertion may be loosened to make a flaky test pass; fix the timing, not the expectation.
- Every change in M53–M55 carries a denied-role test if it adds or changes a route.

## 4. Acceptance

`make test-all` green twice in a row on a busy machine; jsdom and Postgres tests run (not skip) in CI; an uplift run on a two-column dataset is visible and correct on the Phase 1 pages; an Approver promotes a challenger through the new screen while the trainer cannot; an erasure request completes in the background with an audit trail; the 1M-row laptop measurement is recorded; `CROSS_BRANCH_REQUESTS.md` has no open entries older than this plan.
