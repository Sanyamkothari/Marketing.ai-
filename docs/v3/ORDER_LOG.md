# The order-log path: status of phase 0 (B1 to B4)

Branch `v3/dd-order-log`. Design: [`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`](../plans/DATA_DOCTOR_EVENT_LOG_PATH.md).
The owner's answers to its open questions are rows B-1 to B-8 of [`DECISIONS.md`](DECISIONS.md); the choices the proposal
left open are in [`docs/DECISIONS.md`](../DECISIONS.md) as DEC-1822.

The path turns a raw order log (UCI Online Retail: one row per line of an order, no customer file, no outcome column) into
a dataset of one row per shopper per month-end, with the outcome "an order within 90 days after the snapshot". It is behind
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

How the pieces meet, in the order a visit will call them (B6 wires this; today a caller passes each answer in):

1. `order_log_reading(file, config)` says the file reads as an order log, with the shopper candidates.
2. `customer_question(reading)` is Q1. A person answers it.
3. `derive_customer_list(...)` and `blank_customer_question(...)` (Q2) and `mixed_orders_question(...)`. A person answers them.
4. `derived_entity(...)` gives `offer_outcome` and `propose_event_outcome` the list, and the outcome is measured on shoppers who had
   started by each month-end. A person approves the sentence.
5. `plan_build(..., label_spec=config.order_log_label, order_log=OrderLogInput(...), suggested=config.order_log_features)` plans the dataset; a person approves
   the plan's exact sentence; `drive_builder(...)` builds it through the builder's routes.

## Run its tests

From the repository root, with the shared virtual environment. The integration test is marked `slow` (about 12 s), so
`make test` skips it and CI runs it.

```
python -m pytest tests/unit/doctor/test_order_log.py tests/unit/doctor/test_order_log_parity.py \
    tests/unit/doctor/test_order_log_config.py tests/unit/doctor/test_order_roll_up.py \
    tests/unit/doctor/test_order_log_outcome.py tests/unit/doctor/test_order_log_cleaning.py \
    tests/unit/doctor/test_order_log_build.py tests/integration/test_order_log_build.py \
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
It is not the real file.

## What was measured

Every figure is from a command run on this branch.

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

## What is left

- **B5.** The audience filter (`audience: {role: orders, no_event_days: 90}`) on the snapshot rows at train and score time,
  with point-in-time tests. It also has to require an earlier order: a shopper whose first order is on the snapshot day has a
  `signup_date` on or before it and no history.
- **B6.** The `order_log_build` plan item and PREVIEW sampled by customer; the in-process `BuilderPort`; PRESCRIBE running
  `drive_builder`; `current_upload` following the built table; VERIFY on the dataset; `_goal_rules` calling `order_log.rules`;
  readiness and training reading `built_split` for such a dataset; keeping `signup_date` out of the model's features (the
  builder carries it as an entity attribute, as for every use case).
- **B7.** The real-data fixture (about 1,000 customers, gzipped, with its generator and attribution), the end-to-end test to
  `ready`, the library cross-check (`recency_days`, `orders_total`, `spend_total`, `order_value_mean` against
  `library/online-retail/fetch.py` at T = 2011-08-31) and the parity runs with the switch on. The cross-check is also where
  `days_between_orders_mean` (blank for a shopper with one order here; 0 in the library file) is compared.
- **B8.** The no-goal "can be built from this log" wording, the legacy offer's next step, the Guided-setup pointer behind the
  flag (B-6) and the new questions in `ui/modules/datadoctor/`.

## Not checked

- The real UCI file was not used (its fixture is B7, a later wave; no download was attempted). Every figure above is on the
  synthetic log.
- The Q1, Q2 and outcome wording is the proposal's, checked against golden strings on a ten-line log; it has not been read by
  a person on the real file.
