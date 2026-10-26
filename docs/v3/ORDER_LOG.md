# The order-log path: status of phase 0 (B1 to B7; B8 skipped)

Branches `v3/dd-order-log` (B1 to B4) and `v3/dd-tool` (B5 to B7). Design: [`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`](../plans/DATA_DOCTOR_EVENT_LOG_PATH.md).
The owner's answers to its open questions are rows B-1 to B-8 of [`DECISIONS.md`](DECISIONS.md); the choices the proposal
left open are in [`docs/DECISIONS.md`](../DECISIONS.md) as DEC-1822 (B1 to B4) and DEC-1823 (the re-scope and B5 to B7).

**Re-scoped.** The owner's newer design, [`docs/plans/DATA_DOCTOR_AI_FIRST.md`](../plans/DATA_DOCTOR_AI_FIRST.md), makes the
order-log path one of the AI's workflows (milestone A3: "order-log workflow as a tool: customer table, returns netted,
guests out, outcome ordered again within 90 days"), not a rule path inside the visit's state machine, and another session
is changing the visit (`engine/doctor/visit.py`, `agent_chat.py`, `questions.py`, `ui/modules/datadoctor/`) right now. So
B5 to B7 build what A3 will call and nothing that touches the visit: the audience filter (B5), the in-process builder port
and one pure entry point (the reusable part of B6), and the real-data fixture with its tests (B7). **B8 (the no-goal
wording, the legacy offer's next step, the Guided-setup pointer, the new questions in the UI) is skipped**: it is
superseded by A3 (the AI's tool asks and words the questions) and A6 (plan, assumptions and approval on one screen). The
`order_log_build` plan item, PRESCRIBE running `drive_builder`, `current_upload` following the built table, VERIFY on the
dataset and `_goal_rules` calling `order_log.rules` of B6 are not built for the same reason: they are visit wiring.

The path turns a raw order log (UCI Online Retail: one row per line of an order, no customer file, no outcome column) into
a dataset of one row per lapsed shopper per month-end, with the outcome "an order within 90 days after the snapshot". It is behind
`data_doctor.order_log` (default off, needs `data_doctor.unified`). With the switch off nothing a Data Doctor, onboarding,
pilot or Guided-setup path produces for an existing input changes. The one thing that moves is `retail-win-back`'s
configuration dump (its `run_config.json`), which gains the four declarations below; nothing reads them there, and they stay
in the dump because a `run_config.json` must read back as the same document (DEC-1822 (o)).

## What B1 to B4 built

| Milestone | What | Where |
|---|---|---|
| B1 | The switch `data_doctor.order_log`; the `retail-win-back` outcome template (`order_log_only`) and `order_log: true` on it and on `win-back-campaign`; `retail_win_back.yaml` gains `order_log_label`, `order_log_features`, `audience` and `built_split` (its `label` and `suggested_features` stay unset); two internal roles, `order_lines` and `returns`, refused by the source routes while the path is off | `configs/engine.yaml`, `configs/outcomes.yaml`, `configs/roles.yaml`, `configs/use_cases/retail_win_back.yaml`, `engine/doctor/config.py`, `engine/config.py`, `engine/doctor/outcomes.py` |
| B2 | The detector `order_log_reading` (names split at capitals, ID-like name first and blank share second), Q1 (which column names the shopper), the test that it never fires on a parity file | `engine/doctor/order_log.py` |
| B3 | The derived customer list (exact `key_text` rules, first order day as `signup_date`), Q2 through the existing loss guard, the mixed-order question, `derived_entity=` on `offer_outcome` and `propose_event_outcome`, no `parse_number` on the chosen shopper or an order number | `engine/doctor/order_log.py`, `engine/doctor/outcomes.py`, `engine/doctor/outcome_definition.py`, `engine/doctor/cleaning.py` |
| B4 | `plan_build(order_log=...)` with the line to order to shopper roll-up (`orders`, `order_lines`, `returns`), the list as a real entity source, `drive_builder` and `replay_build` on a log, the check, read independently of the list, that the list is exactly the log's distinct IDs | `engine/doctor/order_roll_up.py`, `engine/doctor/build_driver.py` |

How the pieces meet, in the order a caller uses them (`engine/doctor/order_log_workflow.py` below does exactly this; no visit calls them):

1. `order_log_reading(file, config)` says the file reads as an order log, with the shopper candidates.
2. `customer_question(reading)` is Q1. A person answers it.
3. `derive_customer_list(...)` and `blank_customer_question(...)` (Q2) and `mixed_orders_question(...)`. A person answers them.
4. `derived_entity(...)` gives `offer_outcome` and `propose_event_outcome` the list, and the outcome is measured on shoppers who had
   started by each month-end. A person approves the sentence.
5. `plan_build(..., label_spec=config.order_log_label, order_log=OrderLogInput(...), suggested=config.order_log_features)` plans the dataset; a person approves
   the plan's exact sentence; `drive_builder(...)` builds it through the builder's routes.

## Run its tests

From the repository root, with the shared virtual environment. The two integration tests that build a dataset are marked
`slow` (about 12 s for the synthetic log, about 55 s for the real sample), so `make test` skips them and CI runs them.

```
python -m pytest tests/unit/doctor/test_order_log.py tests/unit/doctor/test_order_log_parity.py \
    tests/unit/doctor/test_order_log_config.py tests/unit/doctor/test_order_roll_up.py \
    tests/unit/doctor/test_order_log_outcome.py tests/unit/doctor/test_order_log_cleaning.py \
    tests/unit/doctor/test_order_log_build.py tests/unit/doctor/test_order_log_audience.py \
    tests/unit/doctor/test_order_log_workflow.py tests/unit/onboarding/test_audience.py \
    tests/integration/test_order_log_build.py tests/integration/test_order_log_workflow.py \
    tests/integration/test_order_log_api.py -n 2 -p no:cacheprovider
make check-generated VENV=<the shared venv>
```

The wider check, pending CI (the whole folders were run once by the previous builder, below, and are not re-run here):

```
python -m pytest tests/unit/doctor tests/unit/onboarding tests/unit/agent tests/integration/test_order_log_build.py \
    -n 4 -p no:cacheprovider -m "not frozen and not bedrock and not aws"
```

The synthetic log is `tests/fixtures/order_log.py` (`order_log_frame`): UCI-shaped, with `C` cancellations, negative quantities,
prices of 0, a float `CustomerID` with about a quarter of the lines blank (whole guest orders), two orders with two timestamps.
It is not the real file; the real one is `tests/fixtures/online_retail/` (B7, below).

## What was measured (B1 to B4)

Every figure is from a command run on `v3/dd-order-log`.

| Command | Result | Run by |
|---|---|---|
| The nine test files in "Run its tests" above, `-n 2 -W ignore::FutureWarning` | 184 passed in 28.45 s (the integration test alone: 12.96 s) | review-fix pass |
| `tests/unit/doctor/test_build_driver.py test_cleaning.py test_outcomes.py test_outcome_tools.py test_outcomes_key_rules.py test_goal_brief.py test_plan_o_setup.py`, `tests/unit/onboarding/test_sources.py test_roles.py test_validate.py test_features.py`, `tests/unit/agent/test_advisor_order_log_names.py`, `tests/unit/test_config_loading.py`, `tests/integration/test_config_roundtrip.py test_api_clients.py test_api_doctor_outcome.py`, `-n 2 -m "not slow and not frozen and not bedrock and not aws"` | 359 passed in 26.22 s | review-fix pass |
| `tests/unit/pilot/test_data_request.py test_data_request_messages.py -m "not slow"` | 34 passed in 0.46 s | review-fix pass |
| `ruff check`, `black --check` on the 16 Python files the review fix changed; `mypy --strict` on its 8 changed `engine/` and `api/` files; `make check-generated VENV=...` | clean; no issues found in 8 source files; clean | review-fix pass |
| The eight test files of the completion pass, `-n 2` | 166 passed in 26 s (the integration test alone: 11.7 s) | this completion pass |
| `tests/unit/doctor/test_build_driver.py test_cleaning.py test_goal_brief.py test_outcome_tools.py test_outcomes.py test_outcomes_key_rules.py test_plan_o_setup.py`, `tests/unit/onboarding/test_sources.py test_roles.py test_validate.py`, `tests/unit/pilot/test_data_request.py test_data_request_messages.py`, `tests/unit/test_config_loading.py test_config_extra_fields.py test_config_merge.py test_config_validators.py`, `tests/unit/agent/test_advisor_order_log_names.py`, `-n 2 -m "not slow and not frozen and not bedrock and not aws"` (the existing files that cover the changed modules) | 288 passed in 11.5 s | this completion pass |
| `ruff check` and `black --check` on every Python file the branch changed | clean | this completion pass |
| `mypy --strict` on the 11 changed `engine/` files | no issues found in 11 source files | this completion pass |
| `make check-generated VENV=...` | clean (templates, `docs/API.md`, dashboard) | this completion pass |
| `pytest tests/unit/doctor tests/unit/onboarding tests/unit/agent tests/integration/test_order_log_build.py -n 6 -m "not slow and not frozen and not bedrock and not aws"` | 4167 passed, 1 failed in 712 s (load average about 15 on 4 CPUs) | previous builder, before the move to CI; not re-run |
| `pytest tests/unit/agent/test_scanner_hardening.py` (the one failure above, on its own) | 191 passed in 9.15 s | previous builder |
| Agent unpivot and reshape API, the decide phase-2 journey, `test_threshold_fallback_retail.py` and `library/tests/test_online_retail.py` (every test that uses `retail-win-back`, including the shipped `sample.csv` path) | 42 passed in 554 s | previous builder; pending CI to repeat |

The one failure is `test_no_scanner_is_slow_on_any_repeated_unit[mask_value]`: a regex-speed budget (0.3 s per scan) that a loaded machine
misses; it passes on its own, and `engine/agent` is not changed by this branch. A whole-repository `mypy` run by the previous builder
reported 181 errors in `scripts/`, `autonomy/` and `api/`, none in a file this branch changed; it was not repeated here.

On the synthetic log (`order_log_frame()`, 150 shoppers): 2,096 lines, 532 of them (25.4%) with no `CustomerID` (163 order numbers),
150 shoppers. Of the 1,564 lines with a shopper (counted by a short script just now): 438 order numbers have a line with a quantity above
zero, and 34 lines have a quantity below zero, on 19 order numbers (16 start with `C`, 3 do not).
The same log has 3 lines that are exact copies of another line in every column the roll-up reads (`duplicate_lines`, from the
plan's sentence in `tests/unit/doctor/test_order_log_build.py`); they are kept and counted (DEC-1822 (q)).
Through the builder's routes (`tests/integration/test_order_log_build.py`, 1,500 shoppers, with the path switched on in a copy of
`configs/`) the dataset has 5,725 rows, 2,888 yes and 2,837 no, over six month-ends, 31 Mar to 31 Aug 2011, and passes its own checks:
the review-fix pass re-read these by printing them once from that test (a temporary `print`, removed), and the test now asserts that
the rows are exactly the shoppers whose first order is on or before each month-end (`snapshot_rows`). The roll-up of a tiled log of 586,480 lines
took 16.4 s (previous builder, a one-off script on a loaded machine; not re-run).

## What B5 to B7 built

| Milestone | What | Where |
|---|---|---|
| B5 | The lapsed audience on the spine: a shopper has a row at a snapshot only with an order on or before it and none in the `no_event_days` days up to and including it (`MAX(event_time) <= snapshot - N days`, through `point_in_time_join_clause` and `assert_point_in_time`, so a later event can never keep or drop a row; "has an earlier order" is the same clause, because an event at or before `snapshot - N` is before the snapshot). Applied after `build_snapshot_frame` at train and score time. It is `OnboardingSpec.audience` (so in `recipe_hash`), `BuildPlan.audience` (so in `plan_hash` and the build sentence), `OutcomeDefinition.audience` (so in `definition_hash`, the outcome sentence and the counts, which are those of the rows the build will have); replay carries it; all are left out of a dump while unset. A recipe whose audience reads a table that is not mapped stops with `LABEL_ROLE_MISSING` (no new code) | `engine/onboarding/audience.py`, `specs.py`, `build.py`, `replay.py`, `api/routes/datasets.py`, `engine/doctor/build_driver.py`, `outcome_definition.py`, `order_roll_up.py` (`audience_words`) |
| B6 (reusable pieces) | `LocalBuilderPort`: the `BuilderPort` that calls the onboarding routes' own functions (`create_source`, `suggest_mapping`, `save_mapping`, `create_onboarding_spec`, `create_dataset`) in process, the dataset job run on the calling thread, and reads the finished build back (`read_dataset`). `propose_order_log` (the outcome with its exact sentence and the plan with its exact sentence, nothing built) and `build_from_order_log(file, customer_column, guests_answer, use_case, approvals, port)`: re-derives both, refuses an approval that is not for the hash and sentence read now, runs `drive_builder`, reads the table back and runs `check_readiness` on it with the use case's `built_split` | `engine/doctor/builder_local.py`, `engine/doctor/order_log_workflow.py` |
| B7 | A seeded by-customer sample of the real UCI log (1,000 of its 4,372 customers, guest checkouts in proportion), a slice that is too small, their generator and the CC BY 4.0 attribution; the end-to-end test; the library cross-check | `tests/fixtures/online_retail/`, `tests/integration/test_order_log_workflow.py` |

What `build_from_order_log` takes and refuses, for the tool that wraps it:

- The customer column is a person's choice among the reading's candidates (`ORDER_LOG_CUSTOMER_UNKNOWN` otherwise); an
  order-number column cannot be it. The answer to Q2 (the lines with no shopper) is a `GuestsAnswer`; unanswered, a stop or a
  yes tied to another count (`finding_key`) is refused (`ORDER_LOG_GUESTS_UNANSWERED`, `ORDER_LOG_STOPPED`,
  `ORDER_LOG_GUESTS_STALE`); an order with and without a shopper needs `mixed_answer="keep_named"`.
- A sample of the log (`LoadedFile.file_rows` set) is refused (`ORDER_LOG_SAMPLED`): the path rolls up the whole log.
- Off, or a use case without `order_log_label` and an `order_log` template, is `ORDER_LOG_OFF` or `ORDER_LOG_USE_CASE`.
- The two approvals are checked for the hash and the exact sentence only (`OUTCOME_NOT_APPROVED`,
  `APPROVAL_SENTENCE_CHANGED`, `BUILD_PLAN_NOT_APPROVED`). That the approver is an Analyst (`data.define_outcome`,
  `data.build_dataset`) is the caller's policy check; this function does not know who is asking.
- `STOP_CODES`, `min_rows`, `min_positive` and `LOSS_STOP_PCT` are not touched: a build that does not reach them is
  refused with the engine's own words (the slice below).

## B5 to B7 run

Every figure is from a command run on this branch, on this machine.

| Command | Result |
|---|---|
| `python -m tests.fixtures.online_retail.make_fixture <full csv>`, then again with `--check` | Full file: 541,909 data rows, the 8 expected columns, 135,080 lines with no `CustomerID`, 4,372 customers (the script refuses anything else). Sample: 114,440 lines, 1,000 customers, 31,357 lines (27.4%) with no `CustomerID`, 1,601,381 bytes gzipped. Small slice: 4,797 lines, 46 customers, 68,840 bytes. `--check`: both files identical to a fresh run |
| A one-off pandas script over the full file (the same rule as the test's `lapsed_rows`: an order on or before the month-end, none in the 90 days up to it, outcome an order in the 90 after) | 5,132 shopper-months and 1,724 'yes' over the six month-ends 31 Mar to 31 Aug 2011 (361/123, 533/185, 743/229, 977/288, 1,127/349, 1,391/550): the proposal's 5,132 and 1,724 |
| `tests/integration/test_order_log_workflow.py` (the sample through `LocalBuilderPort`) | 6 passed in 54.46 s (module setup, the date profiling and the one build, 35 s; the scoring replay, 13.5 s). The sample builds **1,190 rows** over the six month-ends (83, 133, 166, 223, 257 and 328 rows), **413 yes and 777 no**, passes the engine's minimums (1,000 rows, 200 positives) unchanged, and the readiness verdict is `ready_with_warnings` with three warnings: `ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED` (the shopper table's only attribute is `signup_date`) and two `HIGH_NULL_COLUMN` (`days_between_orders_mean`, empty for one-order shoppers, 76%; `returned_items`, empty where there is no return, 73%) |
| The same test file: the audience against the raw lines | At each of the six month-ends the built rows are exactly the shoppers with an order on or before it and none in the 90 days up to it, with the outcome an order in the 90 after; the scoring replay has one snapshot, 9 Dec 2011, with the same rule. At 31 Aug 2011 the built table has 328 shoppers, the same 328 as `library/online-retail/fetch.py`'s own functions give at that day |
| The same test file: the cross-check at 31 Aug 2011, for all 328 shoppers | Equal: `orders_total`, `items_total`, `distinct_products`, `active_months`, `returned_orders`, `country`, the outcome, `returned_items` where there is a return, and `spend_total`, `order_value_mean`, `order_value_max` (the library rounds to 2 places; within 0.0051). Different by rule, pinned in the test: `recency_days` and `tenure_days` are one day more for every shopper (the feature engine counts calendar days from the order's date, the library whole 24-hour periods from its time; equal to the calendar count exactly); `days_between_orders_mean` is blank for the 220 one-order shoppers (the library: 0) and is the calendar span over orders minus one for the other 108 (53 differ from the library's by up to a day); `returned_items` is blank for the 236 shoppers with no return (the library: 0) |
| The same test file: the small slice | `OrderLogWorkflowError`: 66 rows and 36 positives, `ROWS_TOO_FEW` ("At least 1,000 are needed") and `TARGET_TOO_FEW_POSITIVES`, the engine's own words, and no dataset written |
| `tests/unit/onboarding/test_audience.py`, `tests/unit/doctor/test_order_log_audience.py`, `tests/unit/doctor/test_order_log_workflow.py` | 15, 8 and 13 tests pass (the point-in-time cases: a later event never changes a row, the boundary is 90 whole days, a first order on the snapshot day is not lapsed) |
| `tests/unit/doctor/test_order_log.py test_order_log_parity.py test_order_log_config.py test_order_log_outcome.py test_order_log_cleaning.py test_order_log_build.py test_order_log_audience.py test_order_log_workflow.py test_order_roll_up.py test_build_driver.py test_outcome_tools.py test_outcomes.py`, `tests/integration/test_order_log_api.py`, `tests/unit/onboarding/test_specs.py test_snapshots.py test_labels.py test_audience.py test_replay.py test_features.py test_validate.py`, `-n 2 -W ignore::FutureWarning` | 419 passed in 46.52 s |
| `tests/integration/test_order_log_build.py test_api_onboarding_replay.py test_api_datasets.py`, `-n 2` | 63 passed in 28.36 s |
| `ruff check`, `black --check` on every file the branch changed; `mypy --strict` on the changed and new `engine/` and `api/` files; `python -m scripts.gen_api_docs --check` and `gen_templates --check` | clean |

A one-off run (a script, not a committed test) of the same workflow on the synthetic log with 1,500 shoppers stopped at
169 positives with `TARGET_TOO_FEW_POSITIVES`: the audience leaves the synthetic log below the minimum, and the path does
not hide it. That is why the real sample is what the end-to-end test builds.

Pending CI (not run here, by the compute rule):

```
python -m pytest tests/unit/doctor tests/unit/onboarding tests/unit/agent tests/integration/test_order_log_build.py \
    tests/integration/test_order_log_workflow.py -n 4 -p no:cacheprovider -m "not frozen and not bedrock and not aws"
make lint
```

and the parity runs with `data_doctor.order_log` on (the goldens byte-identical, the M160 benchmark with no diff), which
need the visit and are the visit-wiring stream's.

## What is left

- **For the AI's tool (A3).** Wrap `propose_order_log` and `build_from_order_log`: ask Q1 and Q2 in the AI's own words, show
  the two sentences, check the approver's role (Analyst) and call the builder through `LocalBuilderPort.for_app(app,
  client_id)`. The built dataset still carries `signup_date` as an entity attribute, as for every use case; keeping it out of
  the model's features is the training step's. `read_dataset` is on `LocalBuilderPort` only; `ApiBuilderPort` has no
  equivalent.
- **Visit wiring (B6, not built).** The `order_log_build` plan item, PREVIEW sampled by customer, PRESCRIBE running
  `drive_builder`, `current_upload` following the built table, VERIFY on the dataset, `_goal_rules` calling `order_log.rules`,
  readiness and training reading `built_split` for such a dataset in the visit and in `POST /runs`. `build_from_order_log`
  reads `built_split` itself for its readiness check.
- **The offer's counts.** `offer_outcome` (the legacy offer, a list of window choices with counts) does not take the
  audience; only `propose_event_outcome` does. The legacy offer is not part of A3.
- **Next month's scoring.** The score-time audience is tested through `replay_build` on the saved record
  (`LocalBuilderPort.load_record(build.spec_id)`); a tool for scoring would wrap that.

## Not checked

- The Q1, Q2 and outcome wording is the proposal's, checked against golden strings on a ten-line log; it has not been read by
  a person on the real file. The audience sentence ("Only shoppers who have lapsed count: ...") is new and has not been read
  by a person either.
- The whole real file was not built through the product (only the 1,000-customer sample was); the full-file figures above are
  the independent pandas count, not a product run. The roll-up of a tiled log of 586,480 lines (16.4 s, previous builder)
  was not re-run.
- The ready verdict was not followed by a training run, and `POST /runs` was not called on the built dataset.
