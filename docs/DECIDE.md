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
| 9 | How we know our intervals are honest; the synthetic quarantine | M95 | written (below) |
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

## 9. How we know our intervals are honest

Every campaign result shows a 95% range for the lift. "95%" is a promise: if the same kind of campaign were
run again and again, the range should hold the true effect 95 times in 100. We do not assume that; we test it,
every night, on campaigns whose true effect we know because we made them.

**The simulator.** `engine/measurement/simulate.py` builds a campaign with a chosen conversion rate and a
chosen effect: who is held back, who converts, and when each customer was treated. It returns the same two
tables the product reads (a run's scores and an outcomes file with dates), so the test measures through the
real `measure_incrementality`, not a copy of it. It can make some customers *immature* (their outcome window is
still open), have only some of the contacted customers receive the offer, and let some held-back customers
receive it anyway. One seed is one campaign, exactly, on any machine.

**What is checked**, by `make test-statistical` (`tests/statistical/`, nightly; never part of `make test`):

| Check | Simulations | Must be |
|---|---|---|
| The 95% range holds the true effect, at base rates 2%, 5% and 20% | 2,000 each | 95%, within the band |
| The same when only some contacted customers receive the offer (the effect measured is then the effect of being *assigned*, intent to treat) | 2,000 | 95%, within the band |
| A campaign with no effect is reported as having one (range excludes zero; p < 0.05) | 10,000 | 5%, within the band |
| Leaving immature customers out does not bias the lift; counting them as non-converters does (towards zero, by the amount the model predicts) | 1,000 each | within four standard errors of the mean |
| Achieved power at the planner's number of customers (when the planner, M93, is in) | 2,000 | 80%, within the band |

**The band rule.** With `n` simulations and a true rate `p`, the observed share has a Monte Carlo standard error
of `sqrt(p(1-p)/n)`. A check passes when the observed share is within **four** of them: 95% +/- 1.95 points at
2,000 simulations, 5% +/- 0.87 points at 10,000. The band comes from the number of simulations, never from
choosing a width that happens to pass; a check that needs a tighter band runs more simulations. Four, not two,
because a correct measurement then fails by chance about one night in sixteen thousand, so a red night means
something and nobody is tempted to loosen the test. Seeds are fixed, so a result is reproducible.

**Runtime.** The suite takes about seven minutes on one busy core (the 10,000-simulation false-positive check is
about three of them), and the power checks add a few more once the planner is in: each simulated population is kept small (1,200 to 4,000
customers) because the number of simulations, not the population size, sets the band.

**What it does not show.** It shows that the *method* is honest on randomised data with the assumptions
above. It does not show that a particular client's holdout was randomised, that their outcome data is complete,
or that their effect is what the demo's is: those are checked per client by the product's own checks.

### The synthetic quarantine

The demo's churn effect is *planted* in generated data. It must never be read as a result or a
forecast. So a run that read generated data is recorded with `synthetic: true` in its `run.json` (the seeded demo
marks all its runs; an upload can be marked synthetic when it is made, and runs that read it inherit the mark).
Every results and value report drawn from such a run, on screen and as PDF, opens with the block "Synthetic data:
planted effect, not a forecast". A run recorded before the field existed, and any run that did not read generated
data, carries nothing. `tests/unit/test_docs_honesty.py` keeps the planted figure out of the documentation: it
may appear only in demo documents, or on a line that says it is planted.
