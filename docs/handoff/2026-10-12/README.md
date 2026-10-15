# Handoff — 12 Oct 2026 (stopped 16:35 UTC by the product owner: "we will do all the things tomorrow")

Session notes for resuming work. Not product documentation; nothing here is merged into `main`.

## Where things are

**On `main` (c1db36f, GitHub CI green except the known test-only fix in c1db36f):**
new look (M185–M188, M190), M189 progress bar, M192 AI service in one click, Plan O set-up (M230),
wave 1 (M231, M232, M234, M237, M238, M240, M241) and wave 2 batch A (M233, M235, M236, M242, M246, M248,
DEC-1821 ready-needs-roles fix, build-sentence fix). Data Doctor is all behind `data_doctor.unified` (off).

**Pushed, not on main:**

| Branch | What | State |
|---|---|---|
| `integrate-plan-o-3` (308b74e) | Batch B: fix-eval-determinism, M239, M244, M243, M247 merged onto main + codes, docs, API.md | Targeted tests passed; the **slow pass5 eval gate was NOT run yet** (`pytest tests/unit/doctor/test_eval_harness.py -k pass5`). Push to CI, run the gate, then merge to main. |
| `m245-wip` (712a248) | M245 "notice what I don't know" — partial build, stopped mid-way | Not reviewed, not for merge. Restart M245 from `integrate-plan-o-3` (or reuse this as a starting point). |
| `m239-outcomes`, `m243-prescription`, `m244-time`, `m247-screen`, `fix-eval-determinism` | The individual milestone branches | Already inside `integrate-plan-o-3`. |

## Remaining to switch Data Doctor on
1. CI + slow pass5 gate on `integrate-plan-o-3` → merge to main.
2. M245 (DEC-1815) — build, one review, fix (spec in the Plan O doc §M245 + `dd_design/00_requirements.md` items 8 and 11).
3. M249 (DEC-1819) — gate run `python -m scripts.plan_o_benchmark --train` (all parts, k=5), regenerate
   `reports/plan_o_benchmark.{md,json}`, fix what it finds, flip `data_doctor.unified` per the plan.
4. M250 (DEC-1820) — pitch, demo script, proof numbers.
Also open: `cases/` S3 prefix + conditional put before the switch is on in AWS; re-record the eval cassette from a real AI model once a key is configured.

## Decisions still waiting on the product owner
Day 1 report (M191, spec in `specs/m191_day_one_spec.md`, paused); the one-flow 6-step campaign picture;
a pilot client with real data; the 5-person usability test (then M181d default layout); test speed-ups
(build-script edit permission); later: feature discovery, Plan N.

## Working rules learned today
- Only targeted tests on the build machine; GitHub CI runs the fast suite (~15k tests, ~45 min) on every pushed branch.
- Never `git stash` (shared across worktrees). Never SendMessage an agent that runs inside a workflow (it forks a duplicate).
- Single-lens review per milestone; merge to main only when CI is green.

## Files here
- `plan_m_integration_notes.md` — the running integration log (every branch, code, DEC text pointer).
- `results/*.json` — full workflow results (DEC text, codes with help, README notes, cross-branch) for
  M236 (wgvf7mchh), M239 (wj4t5ds6c), M242 (wvk4y9lww), M243 (wk98jgb3q), M244 (wrypc0zxr), M246 (wbuxnhj2z), M247 (w0yhk20j1).
- `specs/`, `reviews/`, `dd_design/` — the binding specs, review findings and Data Doctor design notes.
