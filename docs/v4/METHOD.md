# The method change: an uplift threshold, and two agent ablations

Status: code and tests are in; **every result is pending CI** (nothing below was run on a full dataset here; the machine is shared and the bench data is not downloadable from it). This is tests T1 and T2 of [docs/v3/ROOT_CAUSE.md](../v3/ROOT_CAUSE.md), "How to test it cheaply". The default agent is unchanged.

## Why

The root-cause audit found that on the four real campaigns the agent's click-driven, per-message bidder adds about nothing over the uplift model it is blended with, and that its exploration costs money on a one-shot test. The recommended change is a plain method: a cross-fitted uplift model on purchases, act when expected extra profit is above the cost, and use it only where it provably beats a simple rule. T1 builds that as a bench policy. T2 asks, with two ablations of the agent, whether the click model and the exploration are what cost it.

## T1: `uplift_threshold`

Code: `autonomy/decide/uplift_threshold.py` (reusable by the runtime later), bench adapter `scripts/v2_realbench/uplift_policy.py`.

### In plain words

1. **Model.** One logistic model per fold, from the repo's own `autonomy.uplift` (`FeatureEncoder`, `SharedBaselineLogit`): the chance of buying with no message, and how each message arm changes it. Nothing is rewritten; the calibration reuses `grouped_slope` and the slope shrinkage of `fit_uplift`.
2. **Cross-fitting.** TRAIN customers are put into 5 folds by a hash of (seed, id). Fold f's model is fitted on the other four and scores fold f only, so a customer's out-of-fold score never comes from a model that saw them. Those out-of-fold scores give each arm's average effect (doubly robust) and how far the model's differences between customers are real (the slope, shrunk by its own noise). Where the slope is 0, every customer gets the arm's average effect and the policy is simply the best blanket arm. TEST customers are scored by the average of the five models.
3. **Decision.** For each customer and arm k: `net_k = uplift_k x value_k - cost_k`. `value_k` is the mean margin of a buyer of arm k on TRAIN, `cost_k` is the arm's channel plus offer cost (the bench's `arm_cost`). The customer gets the arm with the largest `net_k` if it is above 0, else nothing. Deterministic: no exploration, no draw, no multiplier, no hold. Ties go to the lowest arm.
4. **Gate (the Scientist's rule).** TRAIN is split by hash into 70% (fit) and 30% (held out). A model is fitted on the 70%. On the 30%, with the doubly robust estimator the bench uses (`autonomy.proof.replay.evaluate`, reward model `per_action_mean_model`), the model policy and every simple rule get a value with a 95% interval: send to none, send to all of each arm, and the bench's segment rule (every candidate column set, fitted on the 70%). **The model is deployed only if the lower end of its interval is above the best simple rule's value on the same 30%.** Otherwise the policy is that best simple rule (a segment rule is refitted on all of TRAIN). The model that is deployed is refitted on all of TRAIN.

It prints the scoreboard and DIAG block the agent prints (decisions by kind and reason, the figures line), and one more line:

```
UPLIFT_GATE chose=<model|rule:name> lb=... best_rule=<name> best_rule_value=... model_value=... gate_rows=... fit_rows=... reason=...
```

`lb` is the lower end of the model's own 95% interval on the held-out part; `best_rule_value` is the best rule's point value there. The same facts are in the JSON at `datasets.<d>.agent.uplift_gate`. In a DIAG block of this policy, "calibration" (expected against measured per contact) reads the model's belief even when the rule branch was taken, because the rule has no expectation of its own.

### Commands (the exact CI ones)

The `realbench` workflow (`.github/workflows/realbench.yml`) now runs each dataset on five policies: `agent`, `agent_v3` (both kept), `uplift_threshold`, `agent_greedy`, `agent_uplift_only`. It runs on a push to `version-2` or `claude/**` whose commit message contains `[real]`, or by hand (`workflow_dispatch`). Each cell is:

```
python -m scripts.v2_realbench.fetch --data-dir "$DATA_DIR" --dataset <hillstrom|x5|criteo|megafon>
python -m scripts.v2_realbench.run --dataset <d> --policy uplift_threshold --data-dir "$DATA_DIR" \
    --json realbench-report/<d>-uplift_threshold.json --quiet
```

and the summary job renders one table per policy (`realbench-<policy>.json` in the `realbench-report` artifact). Locally, with the data in `DIR`:

```
python -m scripts.v2_realbench.run --dataset all --policy uplift_threshold --data-dir DIR --json out.json
```

### Results: pending CI

| Dataset | Prediction (ROOT_CAUSE T1) | `uplift_threshold` DR | Agent DR | Gate branch |
|---|---|---|---|---|
| Hillstrom | beats the agent (no exploration cost) | pending CI | pending CI | pending CI |
| X5 | ties | pending CI | pending CI | pending CI |
| Criteo | ties | pending CI | pending CI | pending CI |
| MegaFon | ties | pending CI | pending CI | pending CI |

Read it with the `agent minus <policy>` rows of the report and the Holm-corrected table; a paired difference against the agent is not in the bench's report and has to be computed from the two JSON files (`datasets.<d>.agent.value.dr`, intervals as the CI summary prints them for `agent_v3`).

What was run here: unit tests on planted data, and one smoke run of each of the three new policies on the 6,401-row library sample of Hillstrom (`library/hillstrom-email/sample.csv`, with `--limit` for the ablations). That checked that the policies run through the bench path and print the lines above. It says nothing about the real results.

### Known limits (read before the numbers)

- **The gate is strict on purpose.** The model's lower bound is compared with the best rule's point value, so on a dataset with little usable heterogeneity the gate falls back to the rule. That is the intended "tie the best simple rule" outcome, not a failure.
- **Best of many rules on the held-out part.** The best simple rule is the best of the blanket arms and every segment candidate on the same 30% the model is judged on, so its value is slightly optimistic (a winner's curse), and a segment rule that won there can still be worse on TEST than send to all (it happened on the 6.4k smoke sample). The bench's own "best simple policy" is chosen differently (blanket arms on all of TRAIN), so the two names can differ.
- **The model is for the chance of buying.** The value of a buyer is the arm's mean margin on TRAIN, not a separate model of the margin per customer.
- **Additive features.** The learner is one-hot and additive in the features; it cannot learn an interaction (two features that matter only together). A depth-2 policy tree is test T3, not this stream.
- **Run time on MegaFon is not measured.** The model has up to about 350 one-hot columns there (50 features, up to 7 columns each) and is fitted ten times (five folds for the gate, five for the final model). The CI timeout is 90 minutes; read the step timings in the report (`datasets.megafon.timings`) once CI has run.

## T2: two agent ablations

Both are variants in `autonomy/runtime/ablation.py` (family `method`), so they are bench policies (`--policy agent_greedy`, `--policy agent_uplift_only`) and Gym policies, and they are in the diagnose workflow's two matrices as the existing test requires. The DIAG line "switches differing from the plain agent" names exactly the switches each one moves.

| Variant | What it changes | Why |
|---|---|---|
| `agent_greedy` | exploration floor 0, and the final choice takes the option with the best net after price, with no Thompson draw (`AutonomousPolicy(greedy=True)`) | on a one-shot test exploration is a cost and learns nothing the score uses; ROOT_CAUSE predicts the Hillstrom loss is mostly exploration |
| `agent_uplift_only` | the click model is detached from the price: for every option the uplift model scores, the bidder uses the score's mean and spread alone, with no blend (`autonomy.uplift.score.set_score_only`); an option with no score still uses the response model | shows whether the click model adds anything once an uplift score exists; ROOT_CAUSE predicts about nothing |

What `agent_greedy` leaves in place: the spread still feeds restraint, pruning and the plausibility check upstream; only the last draw is removed. What `agent_uplift_only` leaves in place: the contact-harm model, the offer logic, the attention price and every hard rule; only the response-model blend in `GoalBidder._raw_options` changes, and only for the response model that was switched.

### Results: pending CI

| Dataset | Prediction | `agent_greedy` minus `agent` | `agent_uplift_only` minus `agent` |
|---|---|---|---|
| Hillstrom | greedy recovers the exploration cost; uplift-only about 0 | pending CI | pending CI |
| X5, Criteo, MegaFon | both about 0 | pending CI | pending CI |

Command: the same `realbench` matrix as above (`--policy agent_greedy`, `--policy agent_uplift_only`). The diagnose workflow (`[diagnose]`) also runs them on the Gym scenarios; the Gym rewards contact volume (ROOT_CAUSE section 2), so those rows are a regression check, not evidence of value.

## Tests

`tests/v4/method/`:

- `test_uplift_threshold.py`: cross-fitting never scores a row with a model fitted on it (folds are disjoint, each fold model is fitted on the other folds and asked about its own fold only, a spy learner sees the sizes); the out-of-fold score equals its own fold model's; the gate falls back to the rule on planted null data (three seeds) and chooses the model on planted strong heterogeneity; the policy is deterministic for a seed and changes with another; the argmax and the cost threshold.
- `test_bench_policy.py`: the policy runs through `common.evaluate_dataset` on tiny frames shaped like each adapter's output (Hillstrom with three arms, X5 with a per-customer margin and a text column, Criteo 12 features at 85/15, MegaFon 50 features), prints the DIAG block and the `UPLIFT_GATE` line, fits on TRAIN customers only, and is repeatable; the CLI and the CI axis know the policy.
- `test_ablations.py`: greedy takes the best net every time where the plain policy draws, uplift-only prices a scored option from the score alone, and each is on its own bundle only.

Existing tests changed: `tests/v3/diagnose/test_variants.py` (the plain build's switches now list `greedy` and `click_model`, and one test for the two new variants). Existing tests run for the touched modules: `tests/v2/realfix/test_uplift_*.py`, `tests/v2/realfix/test_bench.py`, `tests/v3/diagnose/`, `tests/v2/decide/test_policy.py`, `tests/v2/decide/test_bidders.py`.
