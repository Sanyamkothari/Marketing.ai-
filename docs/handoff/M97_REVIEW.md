# M97 review (Claude Code, 2026-10-08)

Branch `plan-j/m97-net-value` at `19e5a71`. The partner agent's trial milestone. Read this before
starting M98: most of the findings are patterns to avoid, not one-off slips.

**Verdict: good shape, not mergeable as pushed.** The structure is right: one lookup function for costs,
additive contract fields, the identity test extended rather than loosened, and acceptance tests
written first and shown failing. Lint and `make test` were clean. The blocking findings below are
behaviour bugs that the tests did not reach. Claude Code fixed them on the way to `main` (DEC-1307);
the fix commit is the reference for how each one was handled. Every fix has a regression test, and
each was run against `19e5a71` (merged onto `main`) and fails there, except where this file says
otherwise.

## Blocking (fixed before merge)

1. **`min_roi` is ignored on the path real runs take.** `recommend_policy` computes the net values,
   then passes `net_value=` to `choose_contacts`, which calls `customer_net_values` again. With
   `net_value` given, it returns a constant `cost_per_contact` cost, or `None` when the cost came
   from `value.yaml`. So the cut uses a zero cost and `min_roi` has no effect. Reproduced: three customers, net values 9.14, 4.14
   and 1.14 at a contact cost of 0.86, `min_roi=5`. `choose_contacts` alone selects one;
   `recommend_policy` selects all three. **Fixed:** `customer_net_values` now returns one
   `CustomerMoney` (net value, per-row cost, the cut, the money unit), computed once and passed to
   `choose_contacts`, `below_cost`, the ranking, `_best_contacts` and the curve loop; the three entry
   points share one `_plan`. Tested through `recommend_policy` and `profit_curve` with the cost from
   `value.yaml` and with `offer_cost × p_treated` per-row costs. *Lesson: when a function computes
   something once and passes it down, pass everything it computed (net value AND per-row cost), and
   test the public entry point, not only the inner helper.*
2. **Quadratic time in `_best_contacts`, for existing configurations too.** The per-count means and
   sums (`values[ranked[:c]].mean()` for every `c`) are O(N²). `customer_net_values` returns a cost
   array whenever `value_per_conversion` and `cost_per_contact` are set, which is every money-configured
   run today. Measured: 0.08 s at 8,000 rows, 2.9 s at 60,000, about 13 minutes at 1M (`bench_1m`).
   **Fixed with one `np.cumsum`** of the ranked costs (`CustomerMoney.cost_totals`), shared by the
   recommendation and every curve point; the per-customer value no longer enters as a mean at all (the
   money comes from the value-weighted hold-out, finding 5). Re-measured: `profit_curve` on 200,000
   rows with a value column and per-row costs took 22 s on `19e5a71` and about 1 s after the fix; a
   brute-force reference checks `_best_contacts` on small random fixtures. *Use `np.cumsum`.*
3. **A dedent bug in `UpliftTrainFlow`.** `self._manifest.add_metrics(metrics)` moved inside the
   `for name, bound in ...` loop, at the loop's level, so it was called once per bound: twice per run.
   (This review first said "never when both bounds are `None`"; that is not so - the loop always runs
   over its two bounds, and `add_metrics` merges, so the manifest came out the same.) Unrelated to M97,
   which is exactly why no test caught it. **Fixed:** back after the loop. The regression test trains a
   run whose AUUC interval has no bounds and checks the manifest carries `auuc` (which passes on
   `19e5a71` too, for the reason above) and that the AUUC metrics are written exactly once (two writes
   on `19e5a71`). *Review your own diff line by line before pushing.*
4. **Fabricated values.** `read_holdout` gives old hold-outs `value = 1.0`, and missing customer
   values are `fillna(1.0)`. A scoring run whose training hold-out has no value column was then
   treated as value-weighted, and its "money" was conversions × margin. Plan J rule: nothing
   fabricated. **Fixed, one rule (DEC-1307 (d)):** the hold-out carries a `value` column only when the
   training run was configured with `value_column` and its file has it, missing values left as NaN;
   `read_holdout` adds nothing. A scoring run ranked by value quotes the hold-out only when the
   training run's `run_config.json` names the same column; otherwise expected conversions and money
   are null and `money_note` says why. A customer without a value counts as zero value (never chosen
   for it) and is counted in `values_missing`; above 10 % missing, the money is null with the reason.
   `apply_uplift_actions` no longer fails a scoring run over one missing value (it raised on
   `19e5a71`).
5. **Rupees in a conversions field.** With a value-weighted hold-out, `_expected_conversions`
   returns contacts × value-weighted uplift, which is money. It was stored in
   `expected_incremental_conversions`. Conversions and value are now computed separately: conversions
   from the unweighted hold-out lookup, the value from the value-weighted one (`× margin × horizon`),
   each passed as its own explicit parameter.
6. **Margin and horizon applied inconsistently.** The cut and the ranking used
   `value × margin × horizon`, but the reported money for the `value_per_conversion` path used the bare
   value. With `margin_pct=30`, the list was chosen on 30 % margin and reported at 100 %. **Fixed:** one
   multiplier (`value_per_conversion × margin × horizon` on that path, `margin × horizon` on the value
   path) for the ranking, the cut, the reported value and the curve. Without margin or horizon it is
   exactly `value_per_conversion`, and a configuration without M97's settings is `main`'s arithmetic to
   the last digit (`contacts × cost`, not the sum of a cost array, which on `19e5a71` already differed
   in the last digits for 2 of 10 random fixtures).
7. **The default behaviour changed.**
   - `RoiInputs.contact_cost` went from 0.0 to 0.86. Every pilot that never entered a contact cost now
     shows a lower ROI, from a number nobody typed. Reverted. The value block's costs are read only by a
     use case that opts in with `uplift.policy.value_column` (and sets no `cost_per_contact`); the ROI
     form keeps its own defaults and is not prefilled from the block (offering them there is left to
     Plan E).
   - `apply_actions` (`engine/stages/actions.py`) is outside the functions Plan J may edit there
     (protocol §4: `_control_mask`, `_entity_control_mask`, `_holdout_size`, `suppression_rules` only).
     It added `expected_gross_value` to every run with `value_per_conversion` set, uplift runs included,
     where the score is an uplift, not a probability. Reverted, and moved to a Plan J module,
     `engine/decide/value.py`, installed on the propensity score flow only (from the Plan J block of
     `engine/pipeline.py`, the holdout service's seam), and only when `value_column` is set and the
     upload has it. It writes `expected_gross_value.json` (per band, labelled "not incremental") and two
     run-manifest metrics; `scores.csv` is unchanged (its columns are Phase 1's contract, and the
     partner's column never reached it anyway).
8. **Unrelated edits.** `scripts/bench_1m.py` (`sys.platform` → `platform.system()`) is a different
   workstream's file and has nothing to do with M97; reverted. `tests/unit/measurement/test_simulate.py`
   duplicated a fix already on `main` (196e017); main's version kept.
9. **Found while fixing: the value path never ran end to end.** Training with `value_column` set
   failed at the evaluate stage on `19e5a71` (`NameError: name 'pd' is not defined`): the tests drove
   the policy functions only, never a run. **Fixed** (the code was rewritten), and an integration test
   now trains and scores a model ranked by `monthly_spend` through the API, with three customers
   missing their value.

## Should fix (fixed)

- The DEC number: Plan J uses one DEC per milestone, so M97 is **DEC-1307**. The hand-off said 1310, the code 1307. `M97.md` is kept as the partner's record with a "superseded" banner and inline corrections.
- `value_basis` hard-coded `₹`. It now uses the repository's INR formatting helper (`format_inr`:
  "₹1,50,000 (1.50 lakh) per conversion"), and names the margin and horizon when set.
- The `value` query parameter duplicated `value_per_conversion`. It is kept as the spec asks, documented
  as an alias, and both given → 422 `PROFIT_CURVE_QUERY_INVALID` (no generic code for a bad query
  parameter exists, so the route has its own, like `OPE_INVALID`).
- `is_value_weighted` was detected with `getattr` on a function attribute (`observed.is_value_weighted = ...`
  with a `type: ignore`). It is now explicit: the value-weighted lookup is its own parameter
  (`observed_top_value` on `recommend_policy`, `observed_value` on `profit_curve`), and
  `CustomerMoney.value_weighted` says which path a list is on.
- Hand-off claim "zero shared files modified": `docs/API.md` is generated, which is fine, but say so.
  `M97.md` now says so, with the other reverted claims (`RoiInputs`, `actions.py`, `HOLDOUT_COLUMNS`,
  `bench_1m.py`, `test_actions.py`) corrected inline under its banner.

## Also changed in the fix (not in the findings)

- A list ranked by value now asks the hold-out ranked the same way - by each hold-out customer's net
  value - for both its conversions and its money (`holdout_lookups`), so "the top share" is the same kind
  of customer the list chose; ranking the hold-out by uplift would describe a different group.
- A value-ranked scoring run's `scores.csv` ends with `customer_value` and `net_value`, so the budget
  curve can replay it (on `19e5a71` it looked for the value column in a file that never had it) and
  M98's treat list can read the net value.
- `policy_recommendation.json` and the budget curve carry `money_note` and `values_missing`; the
  budget card shows the note.
- `policy_recommendation.json` gains `net_value_low` and `net_value_high` on **every** run with a cost,
  a value and a measured interval - default configurations included - as the band of the curve's point
  at the budget, so the identity covers it. Every field a pre-M97 configuration wrote is bit-identical to
  `main`; the new fields have defaults, so older files still read.
- `min_roi` must be ≥ 0 and `value_column` non-empty (config validation).

## Second review (fixed)

Two reviewers read the fix commit (`4456806`). Default configurations matched `main` exactly in both
(36 and 40 fixtures); the findings below were fixed with regression tests that fail on `4456806`.

1. **`min_roi` with per-row costs threw away customers who clear it** (major). The cut stopped at the
   first row below `min_roi × its cost`, which assumes those rows are a tail of the ranking by net
   value. With an offer cost (`offer_cost × p_treated` per row) and `min_roi` they need not be:
   net 13, 9, 10 at costs 37, 3, 5 and `min_roi` 1 chose nobody, although two customers earn 3× and 2×
   their cost, and the actions stage called them "over budget" with no budget. **Fixed:** a row below
   `min_roi × its cost` is left out wherever it ranks, and `N` is a prefix of the rest. With one
   threshold for every row (every run without an offer cost, and every run without `value_column`) this
   is the same set as before. The identity test gains a configuration where a skipped row ranks above a
   chosen one.
2. **The costs read from `value.yaml` were not recorded** (major), so the budget curve re-read the
   file at request time: after an edit that does not change who is chosen, the curve's configured
   point showed different money from the recommendation, with `overridden` false. **Fixed:** a run reads
   the costs once and records them (`contact_cost`, `offer_cost` on `policy_recommendation.json`, echoed
   on the curve); the curve replays the recorded costs, and the replay must also reproduce the recorded
   `expected_cost`, else 409.
3. **`value_basis` rounded to whole rupees** (₹0.40 read "₹0") and appeared as a new caption on every
   default run. **Fixed:** `format_inr` takes `decimals` (default 0, unchanged), the basis keeps the
   paise when the value is not whole, and it is null for a configuration with neither `value_column`,
   `margin_pct` nor `horizon_months`, so a pre-M97 budget card reads as on `main`.
4. **An unreadable training hold-out left `money_note` null** on a list ranked by value. It now says
   "The model's training hold-out could not be read, so no expected conversions or money are shown.",
   on the recommendation and on the curve.

## What was good

- The single `lookup_value_costs` seam for M99.
- Extending the identity test instead of loosening it.
- Acceptance tests written first, with the failing output pasted.
- Clear hand-off: the files table with workstream areas, and DEC text ready to paste.

## For M98 and later

- Run the public entry points in tests, with the configuration a real run has (cost from `value.yaml`, not
  only `cost_per_contact`) - and run the flow once end to end (finding 9).
- Add a performance check for anything per-row inside a loop over rows (1M rows must stay linear).
- Never change a default that existing runs see. If a default must change, it needs its own DEC and
  must be raised as an open question in the hand-off.
- `git diff origin/main --stat` before pushing: every file must be explainable by the milestone.
