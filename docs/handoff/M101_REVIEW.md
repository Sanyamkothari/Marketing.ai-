# M101 review (Claude Code, 2026-10-09)

Branch `plan-j/m101-arbitration` at `98b2959`. The partner agent's fourth milestone.

**Verdict: the clearest improvement so far, but not mergeable as pushed. The findings below are fixed in the
review fix commit on top of `origin/main` (`91cc0b4`); this file describes the code after that commit.**
- **Stayed in lane.** `scripts/bench_1m.py` was left alone and raised as an open question.
- **Integration tests start from real artefacts.** `tests/integration/decide/test_arbitration_flow.py` begins from
  runs written by the product's own code (`tests/fixtures/decide/treat_runs.py`), and the API is tested through
  `TestClient`. The partner's unit tests (`test_arbitrate.py`) built their treat lists by hand; the review's tests
  (`test_arbitrate_real_runs.py`, `test_arbitration_review.py`) do not.
- **Protocol followed.** The router is registered in `api/main.py`'s PLAN-J block, and the new files are
  registered as row-level.
- **Scale tested.** There is a 200k-row timing test (about 1 second here, budget 3 seconds, so it is not `slow`),
  and a second one with two kinds of value, explore rows, hold-outs and a channel cap.
- **The self-check was mostly true.** Where it was not, it is corrected in `docs/handoff/M101.md` §3 and §8.

What the review found: three findings would let arbitration make a choice that is either made up or
statistically harmful. Claude Code fixed them on the way to `main`; the fix commit is the reference.

## Blocking (fixed before merge)

1. **A made-up value decides the winner.** When a row has neither `net_value` nor `expected_gross_value`,
   `fillna(1.0)` gives it a value of 1. The winner is then "priority x 1" against another use case's rupees.
   Incremental net value (uplift runs) and non-incremental gross value (propensity runs) are also multiplied by
   priority and compared as if they were the same quantity. *Lesson (M97 item 4 again): a missing value is never
   1, and two units are never compared.*
   The rule is now: compare by priority x value only when every candidate of a customer has the same kind of
   value (all `net_value`, or all `expected_gross_value`). Otherwise rank by priority, then by the order of the use
   cases in the request, and say so: `arbitration_reason` on the row and `customers_decided_by_priority`,
   `customers_decided_by_request_order` and `customers_decided_by_value` in the summary. A row that was not ranked
   on a value has no `priority_score`.
2. **Explore rows lose their randomisation.** M92's explore rows are customers treated at random so that the
   model can learn. If one loses to another use case, the random sample is no longer random. The self-check cited
   `test_explore_rows_preserve_randomisation`, but that test only checks that the flag is copied.
   Explore-treated rows now keep their action (like hold-out protection); if two use cases both treat the same
   customer at random, both are kept only if the contact cap allows, otherwise the first in the request order is
   kept and the other is recorded (`losing_actions`, `explore_dropped_count`). The tests check the action, not the
   flag: `test_an_action_treated_at_random_survives_a_use_case_that_would_win` and
   `test_two_use_cases_that_both_treat_a_customer_at_random`.
3. **A shipped configuration with invented numbers.** `configs/decide/arbitration.yaml` gave priorities to four
   named use cases and invented channel caps (SMS 100,000, email 200,000, WhatsApp 50,000) that would apply to
   every deployment. *Lesson (M99 item 5 again): ship real configuration absent, and put examples in an `.example`
   file.* The file is deleted and `configs/decide/arbitration.example.yaml` says it is an example and is never
   read. With no arbitration file the defaults are: priority 1 for every use case, a cap of one contact per
   customer, and no channel caps. The tests use a temporary config root.

## Should fix (fixed)

- `holdout` was set to `True` on a customer's summary row even when the row came from a use case that did not hold
  them back. The row is now the use case that held them back, its `holdout` is its own flag, and a new column,
  `holdout_use_cases`, names every use case whose hold-out held the customer.
- Private names (`_RUPEE_DECIMALS`, `_SEGMENT_COLUMN`) were imported from `treat_list.py`. They are now public
  (`RUPEE_DECIMALS`, `SEGMENT_COLUMN`); the private aliases stay.
- Python-level per-row work (`map(lambda ...)`, a groupby `agg(lambda ...)`, list comprehensions over every key,
  and a row loop that wrote the CSV) was replaced with whole-array code: no loop over the rows remains. It was
  linear, but slow at 1M rows.
- Merged onto `main` with M99 (channels are filled, so the channel caps act on real data: the review's channel-cap
  test uses runs that plan a channel for each customer) and M100 part A. The treat list's other columns
  (`contactable_channels`, and M100's runner-up and offer-reason columns when the treat list has them) pass through
  to the arbitrated list.

## Found while fixing (also fixed)

- `POST /decide/arbitrate` crashed with a 500 for a composite key (a list is not hashable), and named the winners
  by the first key column only, so a campaign of a composite-key run would have measured nobody. It now uses all key
  columns, spelled as the treat list spells them.
- The summary's `treated_customers` counted winning rows; it now counts customers (the same unless the contact cap
  is above one).
- A run of an uplift use case and a run of a propensity use case in one arbitration dropped the propensity run's
  `band` column; both group columns are kept.
- With no use case named, the order of the use cases (the tie-break) followed the order the runs were found in; it is
  now the use case id order. Two runs of one use case are refused (`ARBITRATION_USE_CASE_REPEATED`).
- The documentation said a losing row carries `suppression_reason = "ARBITRATION_LOST"`. The code never wrote it.
  The text is corrected: a row that lost is not in the list; the winning row names it in `losing_actions`.
- A negative channel cap is refused.

## What was good

- Hold-out protection across use cases, with the reasoning recorded: a customer held back by A and treated by B
  would contaminate A's measurement.
- One campaign per use case from its winning rows, so each effect stays measurable.
- The single-use-case parity test with M98 (now on four real runs as well).
- The hand-off: clear, and nearly every claim is checkable.

## For the next milestones

- When two numbers are compared, check that they are the same kind of number.
- "Preserve randomisation" means the random decision stands. Test the decision, not the flag.
- Start the unit tests from the product's own artefacts too, not only the integration tests.
