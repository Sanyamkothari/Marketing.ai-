# CI green on v3

Four failures were already red on `version-2` before any v3 code, so none of them comes from v3. Each is fixed at its
root: no test is skipped, xfailed or weakened. Branch `v3/ci-green`, started from `101b2e9f`.

| # | Failure | Where it ran | Root cause | Fix |
|---|---|---|---|---|
| 1 | Six Postgres variants: `test_append_counts_new_events_and_ignores_repeats[postgres]` (append returned -1, expected 2) and, in `tests/v2/connectors/test_base_store.py`, `test_a_lease_has_one_holder_until_it_expires`, `test_deliveries_dedupe_and_are_pruned`, `test_record_versions_keep_the_newest`, `test_an_incident_opens_once_and_reopens_after_it_closed`, `test_secrets_and_privacy_requests` | v2 job | `insert_ignore` (`autonomy/store/sql.py`) reads `CursorResult.rowcount` of an INSERT with no RETURNING. SQLAlchemy closes that cursor before the caller reads the count, and psycopg 3 reports -1 on a closed cursor (SQLite keeps the number, which is why only Postgres failed). Every "was this row new" check (leases, deliveries, record versions, incidents, privacy requests, `append`) then saw -1 | Both statements pass `execution_options(preserve_rowcount=True)`, so SQLAlchemy reads the count while the cursor is open. It is a per-statement option, available since SQLAlchemy 2.0.28 (CI pins 2.0.54). No other `rowcount` in `autonomy/` reads an INSERT: the rest are UPDATE or DELETE, whose count SQLAlchemy already captures |
| 2 | `make lint`: mypy, 179 errors in 29 files | CI lint | (a) `scripts/v2_realdata/` is a folder of standalone runners that import each other by bare name (`from b_common import ...`), so mypy could not find 41 modules. (b) The runners had never been type-checked: bare `dict`/`list`/`set`, untyped `itertuples` rows, strings where `EventMapping` takes `EventType`/`Channel`, numpy results typed `Any`, unused `type: ignore` comments, modules re-exporting without `__all__`. (c) `api/v2/insights.py`: one name reused for a `Decision` and a `Decision | None`, and two `type: ignore` comments that no longer silence anything | (a) `pyproject.toml`: `explicit_package_bases = true` and `mypy_path` with the repository root and `scripts/v2_realdata`. (b) The real errors are fixed in the runners. `d_ipw.py` reads `click_rate`, which exists on `ClickResponseModel` only (the default bundle's model, typed there as the base class), so it narrows with an `assert isinstance`; replacing it with the base class's method would have changed what the runner measures. (c) `insights.py` gets a second name and loses the two comments. One `type: ignore[call-overload]` is kept in `b_adapters.py` (numpy's stubs have no overload for a `None` branch in `np.where`), with its reason |
| 3 | `tests/integration/test_brand_ui.py::test_removing_m182_leaves_nothing_of_it`: "api.js reaches the brand without a mark" | image suite | The test forbids any unmarked UI file from mentioning M182's `brand.js`. Its pattern `brand\.js` also matches the start of `brand.json`, and `ui/v2/api.js` names a mock file `onboarding_brand.json`. `api.js` has nothing of M182 in it | The pattern is now `brand\.js\b`. `api.js` stays unmarked, because marking code that is not M182's would break the test's other check, that the marks sit only in the files M182 edits. A real reference (an import or a script tag naming `brand.js`) is still caught |
| 4 | `tests/integration/decide/test_m100_binary_identity.py::test_a_default_uplift_run_writes_the_same_bytes_as_before_m100`: `score/run_config.json` and `train/run_config.json` no longer byte-identical | image suite | `LlmConfig` gained `data_agent_max_output_tokens` (default 4096) with a plain default, so every dumped config, and its `sources` map, gained one line. I found it by dumping `resolve_config("win-back-campaign", <the test's overrides>)` at `62294b08` (where the golden was recorded) and at this branch: the only differences were that field and its `sources` entry | The field is excluded from a dump while it holds the shipped value (`exclude_if`), as `max_run_cost_usd`, `audience`, `order_log_*` and `data_doctor` already are. A deliberate change would regenerate the golden with `python -m tests.fixtures.decide.m100_binary --update`; this one is not deliberate, so the golden is untouched. The setting still works: code reads the attribute, and a non-default value is dumped |

## What was run, and what was not

Commands run from the worktree root with the shared venv.

- Item 1, SQLite variants: `pytest tests/v2/backbone/test_sql_stores.py tests/v2/connectors/test_base_store.py` gave
  `46 passed, 30 skipped`. The 30 skips are the Postgres variants (no `MARKETING_AI_TEST_DATABASE_URL`; there is no
  Postgres server I could start here).
- Item 1, no server needed: the new `tests/v2/backbone/test_insert_ignore_rowcount.py` wraps the SQLite cursor so that
  `rowcount` is -1 once the cursor is closed, as psycopg 3's is. Its first test shows a plain INSERT then reads -1;
  its second shows `insert_ignore` still counts new rows (`2 passed`).
- **Pending CI** for item 1: the six real Postgres variants. Command:
  `MARKETING_AI_TEST_DATABASE_URL=postgresql+psycopg://marketing:marketing@localhost:55432/marketing python -m pytest tests/v2/backbone/test_sql_stores.py tests/v2/connectors/test_base_store.py`
  (the v2 job sets that URL against its Postgres 16 service on port 55432).
- Item 2: `python -m mypy` (the Makefile's command) went from `Found 179 errors in 29 files` to
  `Success: no issues found in 769 source files`. `tests/v2/realdata` (13 passed), `tests/v2/promise/test_promise_api.py`
  and `tests/v2/polish/test_runtime_gaps.py` (47 passed) cover the touched runners and `insights.py`.
- Item 3: the named test passed.
- Item 4: the named test passed (`1 passed in 10.27s`); `tests/unit/generative/test_budget.py`,
  `tests/integration/ai_service/` (api and end to end) and four `tests/unit/test_config_*.py` files gave `176 passed`.
  `python -m scripts.gen_templates --check`, `gen_api_docs --check` and `gen_dashboard --check` were clean.
- Not run here (CI runs them): the whole v2 suite, the whole image suite, and `make lint`'s ruff and black over the
  repository. Ruff and black were run on every file changed.
