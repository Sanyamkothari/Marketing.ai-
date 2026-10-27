# Stronger baselines, and why "who" collapses the bench

Stream `v4/m-baselines`, from commit 53fc9028. It closes two gaps left by [ROOT_CAUSE.md](../v3/ROOT_CAUSE.md) (test T3) and
[DIAGNOSE_RESULTS.md](../v3/DIAGNOSE_RESULTS.md) (H1: which of who's two code paths empties the bidders on the bench).

**Results are pending CI.** Nothing below that looks like a bench or Gym result was measured here. The only numbers in this
page are test counts and timings from commands that were run in this worktree, named where they appear.

## 1. Two learned baselines in the bench's policy table

The bench's "simple rules" table had send-to-none, send-to-all (each arm), the best segment rule and a logistic T-learner.
A T-learner is a weak yardstick: it fits one model per arm and subtracts, so its noise adds up. Two stronger ones now sit
next to it (`scripts/v2_realbench/learners.py`, numpy only, fitted on TRAIN, scored on TEST by the same DR estimator):

| Row | What it is |
|---|---|
| `DR-learner (ridge)` | Doubly-robust pseudo-outcomes of the margin per arm, `phi_a = q_a(x) + 1[A=a] (Y - q_a(x)) / e`. `q_a` is a ridge regression on the one-hot binned features (the T-learner's `Design`), cross-fitted over 2 folds (fold = hash of the customer id). The effect of each arm over no contact is regressed on the same features. A customer gets the arm with the largest predicted effect **only if it is above that arm's contact cost** (the cost threshold); otherwise no contact. |
| `Policy tree (depth 2)` | A depth-2 tree whose leaves name an arm, fitted by exhaustive search on the same pseudo-outcomes **net of arm cost**. Candidate splits: the quantile cuts of each numeric feature and the top 8 levels of each categorical (one-vs-rest). Every root split and every child split is tried; a leaf takes the arm with the largest summed score; at least 1% of rows (floor 50) per leaf. Fitted on at most 100,000 train rows (smallest hashes). |

Rules they follow:

- **TRAIN only.** Outcome models, folds, bin edges and split candidates come from train rows. `tests/v4/baselines/test_learners.py`
  replaces every TEST row's outcome, arm and propensity with junk and checks that both policies come out identical; a
  second test replaces every TEST row's features with out-of-range values and checks that the bin edges, the split
  candidates, the fitted tree and both policies on TRAIN rows are unchanged.
- **Deterministic.** No random numbers; the folds are a BLAKE2b hash of the id.
- **"Best simple" keeps its meaning.** It is still none / send to all / the segment rule, chosen on train, so earlier numbers
  stay comparable. The two learned rows are not candidates for it.
- **A separate "best learned baseline" line.** Each dataset's block prints, under its policy table, the better of the two (by
  DR on TEST) with its DR and "agent minus it" with 95% intervals. It is chosen on TEST, so it flatters the baseline: it is
  a yardstick, and the report says so. The JSON keys are `policies[...]` (the two rows, with the usual IPS / SNIPS / DR
  and `agent_minus_this`), `learned_baselines` (fit details, the tree as text) and `best_learned_baseline`.
- **Outside the Holm family.** The two rows are left out of the multiple-comparison family (`agent_comparisons`), so `m`, the
  corrected intervals and the pre-registered verdicts are the same numbers as before. Their `agent_minus_this` intervals are
  the raw 95% ones, like every other row of the table.

The tree search is exact: for a root split `s` and a child split `t`, the four leaf sums come from one product of the split
indicators with the scores, and `test_the_tree_search_is_exact_on_a_small_case` checks the reported value against a brute
force over every root and child split.

Measured here (these commands only):

- `python -m pytest tests/v4 -q`: 15 passed (all tests of this stream, about 3 s).

How long the two fits take on the real datasets is not measured here; the CI jobs' logs will show it.

### What to read in the CI report

For each of hillstrom, x5, criteo, megafon, in the job's `.md` and the summary: if the agent's DR is not above both learned
rows (and the interval of "agent minus best learned baseline" is not above 0), then the agent is not smarter than a depth-2
tree or a ridge DR-learner on that data, which is the ROOT_CAUSE.md T3 claim in a stronger form. If the learned rows are
themselves not above send-to-all or the segment rule, the data has little to exploit (DIAGNOSE_RESULTS H4). Either
reading is pending CI.

## 2. Why "who" collapses the bench: two variants

From DIAGNOSE_RESULTS H1: `agent + who only` contacts 1.9% of Hillstrom customers (1.5% on X5), 99.5-99.97% of its
decisions are `no_bids`, and removing who from v3 restores everything. Turning who on changes the bidder in four places;
two of them can be isolated by a switch, and now are:

| Policy | What differs from `agent_who` | Switch |
|---|---|---|
| `agent_who_notrait` | the trait-pooled leave-alone is off: `WhoBook.leave_alone` is always False, and the trait groups are never fitted | `WhoConfig(trait_harm=False)` |
| `agent_who_noprune` | the bidder's leave-alone/harm step (`autonomy/decide/bidders.py`, "Leave alone someone a message clearly harms") runs only when a harm model exists, as before who existed | `WhoConfig(prune_without_harm=False)` |

Both are `agent_who` plus the look-alike model (who sits on it, as in the existing `agent_who` variant), registered in
`autonomy/runtime/ablation.py` (family `who_probe`) so the Gym and the bench take them by name. The defaults
(`trait_harm=True`, `prune_without_harm=True`) are what every earlier run used. `ablation.switches` lists the two flags only
when they are off, so the DIAG block's "changed switches" shows them and the plain agent's switch table is unchanged.

### What I expect to fire (from reading `who.py` and `bidders.py`; the CI run decides)

Everything who adds to a bid, and whether it can remove all bids:

1. **Urgency** x `(1 + 0.5 x churn_risk)` (bidders.py, `_quote`): multiplies a net by a positive number. It cannot turn a
   bid into no bid.
2. **Offer cost ceiling** (`WhoBook.cost_ceiling`): applies only to options with an offer (`offer_cost > 0`). The bench's
   campaigns are plain messages, and a plain option is never stopped by it.
3. **Slow-model scores** only write `churn_risk` / `clv_90d` into the state's scores.
4. **The leave-alone step.** This is the only who-dependent `return []` in the path (`_quote` has three others, for
   audience, channels and no raw options, which do not depend on who). Its test is
   `clear or alone`, and `alone` includes `self.who.leave_alone(customer, stake)`.

So only item 4 can produce 99.5-99.97% `no_bids`. Inside it there are two candidate causes:

- **The trait path (expected to fire).** `WhoBook.leave_alone` is true when the customer's trait group credibly (>= 0.9) buys less
  after a message. A customer with no record of their own is read as one more person of their group
  (`TraitHarm.p_harm` -> `_stranger_p[key]`, for groups of at least 20 with a record). On the bench the pre-campaign
  history has no sends (ROOT_CAUSE.md), so I expect most customers to land in the `silent` group (`ch_engaged < 0.5`) and
  most to have no record of their own; the CI report's who coverage (`diag`, `trait_group`, `left_alone_kept`) will say. If the pooled reading for that one group lands below a ratio of 1 with
  90% credibility, every customer in it is left alone, and `_frozen` keeps them there on later refits. One group verdict
  would then explain a 98% stop, which a per-customer cause would not. The pooled reading is the ratio of buying in a
  message's week to buying without one, on records built from a single past campaign; whether that ratio sits below 1 on
  these datasets I cannot tell from the code, only that this is the one path where a single group number silences nearly
  everyone. **Prediction: `agent_who_notrait` goes back to the plain agent's numbers** (Hillstrom about 0.65 and about
  98% contacted, X5 about 72 and about 76%, the figures in DIAGNOSE_RESULTS).
- **The new gate (expected not to matter).** In the plain agent the step runs only if `harm is not None`; with who on it
  also runs when `harm is None`. But when there is no harm model, `loss` is 0, so `clear` is false, and `alone` can only be
  true through `who.leave_alone`, which needs trait groups, which are fitted only from a harm model's records (and are
  reset when it disappears, `WhoBook.refresh`). On the bench the agent's `update_models` call (`learn_harm` is on by default) fills and fits a harm model in the
  warm start, before any decision. So the gate is a no-op either way. **Prediction: `agent_who_noprune` is identical to
  `agent_who`** (Hillstrom 0.2847 / 1.9%, X5 70.76 / 1.5% in DIAGNOSE_RESULTS, measured at an older commit in
  diagnose.yml). `agent_who` itself is now on realbench.yml's policy axis too, so the CI run compares the probes with an
  `agent_who` of the same commit and the same workflow.

If the CI run bears this out, it would correct a phrase in DIAGNOSE_RESULTS ("that step now runs for every customer, not
only those with a harm record"): true of the gate, but the gate would not be what removed the bids. This is a prediction
from reading the code, pending CI; `agent_who_noprune` is the control that decides it. A unit test
(`test_the_noprune_gate_skips_the_leave_alone_step_only_without_a_harm_model`) checks the switch itself: with who on and
no harm model, the default skips a trait-left-alone customer and `prune_without_harm=False` bids for them.

### How to read the CI outcome

| `notrait` | `noprune` | Reading |
|---|---|---|
| back to the plain agent | same as `agent_who` | as predicted: the trait-pooled leave-alone is the collapse. Next fix: the group verdict must not apply to customers with no record (or needs a floor on how many it may silence), and trait groups must be validated against the holdout before they are believed. |
| back to the plain agent | back to the plain agent | the gate matters after all (a harm model is missing in some bidder), and my reading of `refresh` is wrong |
| still collapsed | same as `agent_who` | item 4's other half (`clear`, from the harm model) or something outside the step fires with who on; then rerun `v3_no_who`-style cuts per piece (`risk_urgency=0`, `value_cost_share=None`, `models=False`) |

## 3. CI

Everything below runs in `.github/workflows/realbench.yml` (trigger: manual `workflow_dispatch`, or `[real]` in the head commit
message). The `policy` axis is now `[agent, agent_v3, agent_who, agent_who_notrait, agent_who_noprune]`, so the matrix is
4 datasets x 5 policies = 20 jobs. Every job prints the two new baseline rows and the "best learned baseline" line. The
summary job adds one table, "Which part of who collapses the bench" (DR profit per customer and contact rate for the five
policies per dataset).

Exact commands, to run one cell by hand where the data is downloaded (`$DATA_DIR`):

```
python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_who         --data-dir "$DATA_DIR" --json out-who.json
python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_who_notrait --data-dir "$DATA_DIR" --json out-notrait.json
python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_who_noprune --data-dir "$DATA_DIR" --json out-noprune.json
python -m scripts.v2_realbench.run --dataset x5       --policy agent_who_notrait --data-dir "$DATA_DIR" --json out-notrait-x5.json
python -m scripts.v2_realbench.run --dataset x5       --policy agent_who_noprune --data-dir "$DATA_DIR" --json out-noprune-x5.json
python -m scripts.v2_realbench.run --render out-notrait.json     # tables, with the learned rows and their line
```

The Gym takes the same names: `python -m autonomy.gym.run --policy agent_who_notrait` (and `agent_who_noprune`), for a check that
the probes do not change the Gym.

Tests of this stream (cheap, run on every push): `python -m pytest tests/v4/baselines -q`. The existing tests that cover the
touched code: `tests/v2/realfix/test_bench.py`, `tests/v3/diagnose`, `tests/v3/who`, `tests/v3/preset/test_wiring.py`.

## Results (pending CI)

| Dataset | agent | agent_who | agent_who_notrait | agent_who_noprune | DR-learner (ridge) | Policy tree (depth 2) | Agent minus best learned baseline |
|---|---|---|---|---|---|---|---|
| Hillstrom | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI |
| X5 | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI |
| Criteo | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI |
| MegaFon | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI | pending CI |
