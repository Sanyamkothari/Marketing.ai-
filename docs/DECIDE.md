# Plan J — deciding better and proving better

**Owner:** Plan J (`docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md`, milestones M90–M111)
**Decisions:** DEC-1300 … DEC-1399 in [`DECISIONS.md`](DECISIONS.md)
**Status:** skeleton, created by M90. Nothing below describes behaviour that exists yet; each milestone fills in
its own section in the same change as the code it describes, and removes the "not yet written" line.

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
| 8 | The campaign record, the measurement path and the test plan | M94 | not yet written |
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
