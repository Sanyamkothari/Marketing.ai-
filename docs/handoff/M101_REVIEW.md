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
  The text is corrected: a lost action is named in the winning row's `losing_actions`. A customer left with no
  action at all (held back, not selected, or whose only action a channel cap removed) has one row with
  `treat = 0` and `arbitration_reason` = `held_out`, `not_selected` or `channel_cap`, and no offer or channel.
- A negative channel cap is refused (in the engine's model; the second review found the route still ran on the defaults, see below).

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

# Second review (two reviewers, 2026-10-09)

The branch was merged with `main` at `ef4bd2a` (M100 part B) and the findings below were fixed; this file and
`docs/handoff/M101.md` describe the code after those fixes. The reviewers confirmed that the first review's three
blocking findings were truly fixed and verified; what they found next:

## Blocking (fixed)

1. **The arbitrated campaign did not compare like with like.** It cut only the treated arm to the winners; the
   held-back arm kept every eligible customer. On a propensity run the treated arm shrank to the treat list's
   winners (top bands plus explore) while the held-back arm was every eligible customer, even with a single use
   case (band shares 26/31/42% against 20/52/27%); on an uplift run, the customers A lost to B left the treated arm
   only. The effect measured came from who was in each arm, not from the contact.
   Both arms are now cut by one rule that does not look at the hold-out (`comparable_keys`): the arbitration is run
   again as if no one were held back, a held-back customer the use case's policy intended to contact
   (`policy_intended`) competing like a treated one. A use case compares the customers it wins in that run; a
   customer a rival wins (or a rival took at random) is in neither arm. The population is the policy's own (an uplift
   run's `intended_treatment`, a propensity run's treat bands, found with M92's `selection_masks`), so explore rows
   stay out of `intended`. Customers kept from being contacted by another use case's hold-out or by a channel cap
   stay in both arms (intent to treat); the dilution is named in the hand-off.
   *Lesson: when you restrict one arm of a comparison, ask what the same restriction does to the other arm.*

## Major (fixed)

2. **The arbitrated campaign skipped the rules of `POST /campaigns`.** Start = the run's creation (labelled
   "run finished"); no outcome window; a LIVE campaign for a use case with no hold-out or no winner; a new set of
   LIVE campaigns on every POST. Now: the start is the run's finish time, the window is the use case's, a use case
   with no hold-out (`measure_offered`) or no winner is skipped with the reason in the response, and posting the same
   arbitration again returns the campaigns already made (`Campaign.arbitration_id`).
3. **A customer-level copy sat at `decide/`, where retention never looked** (`store_of` maps it to `Store.OTHER`).
   It is gone: the arbitrated list is kept in the participating runs only (registered and retained as row-level),
   the aggregate summary is at `decide/` and in each run, and the downloads are served from a run's copy.
4. **"Latest scoring run" was picked by run-id text order**, which is random on one day. Now by finish time, then
   creation time, then run id.
5. **A malformed `arbitration.yaml` was silently replaced by the defaults** (the valid caps in it lost too, and the
   review's own line above said a negative cap "is refused"). Now 422 `ARBITRATION_CONFIG_INVALID`, naming the
   setting; only an absent file gives the defaults.
6. **The branch had not been merged with `main`** (M100 part B), and `runner_up_offer`, `runner_up_net_value` and
   `offer_reason` come before `net_value` on the treat list; the arbitrated list's column order is now the treat
   list's, and the parity test compares by position.
7. **The 200k timing tests failed under load** (4.89 s against 3 s with `-n 4`). They are `slow`; a fast test checks
   linear growth against 50,000 rows in the same process.

## Minor (fixed)

- With a binding channel cap, which explore rows survived was decided by value: the explore sample is now kept in a
  fixed order of (use case, customer) that ignores value. Not done: moving a customer whose action was capped to
  their next uncapped candidate (the reviewer's optional suggestion): the plan names one channel per row.
- `customers_decided_by_*` counted customers whose chosen action a channel cap then removed. They count only
  customers who end with an action and match the reasons on the rows; `contested_customers_channel_capped` counts
  the others. The card shows it.
- `arbitration_summary.json` was a known run artefact that nothing wrote under `runs/`: it is mirrored into each
  participating run.
- A failure to load a treat list was always `RUN_NOT_SCORED` with the exception text: the builder's own code and
  plain message are used, and anything else surfaces as a server error.
- Two statements in the hand-off documents were untrue (the lost-row bullet above; `conflicts_card.test.mjs`
  listed as unchanged): corrected.
