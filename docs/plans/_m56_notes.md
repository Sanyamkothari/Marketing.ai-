# M56 notes — test reliability and CI coverage (Plan D)

For the integrator: merge the decision entries below into `docs/DECISIONS.md`, then delete this file.
The measurements are the evidence behind DEC-875 and DEC-876.

## Decisions

- **DEC-875 — The prototype harness waits for the page to settle, not for the first re-render or a fixed time.**
  *Context:* two uplift tests failed about one run in three on a busy machine
  (`docs/CROSS_BRANCH_REQUESTS.md`, 2026-09-23 integration → phase-3b-uplift). Root cause, confirmed:
  `go()` sets `location.hash` and renders synchronously, and jsdom *also* queues a `hashchange`
  (a zero-delay `window.setTimeout` in its session history) whose handler, `render()`, replaces
  `#app main` a second time. `upload()` returned at the first replacement of `<main>`. On a quiet
  machine the file read (two `setImmediate` hops in jsdom's FileReader) ended first; under load the
  already-due hashchange timer fired first, so `upload()` returned after the hashchange re-render
  with the file still unread: `#f-ptype` was not drawn yet ("DEC-608 … never offers Uplift"), and
  `#f-run` was still disabled ("an uploaded file's uplift Output …", via `scoreUpload` after
  `go(dom, UP)`). A 20-line jsdom script reproduced it deterministically (a `go()` then an upload
  returns with `#f-ptype` null; 50 ms later it is drawn). Fixing only that exposed two more guesses
  of the same kind under the same load: `submit(); await wait(1300)` asserting a run that had not
  finished yet ("Campaign results for a run with a 0% control group…": `nothing to click:
  #f-again`), and `click(link); await wait(30)` where jsdom follows the link in one timer hop and
  fires `hashchange` in a second, so a late first hop let the 30 ms wait resolve in between.
  *Decision:* `tests/prototype/harness.mjs` counts the page's asynchronous work - every file read
  (through `Blob.prototype.text`, which is the only way the prototype reads a file) and every page
  timer (through `window.setTimeout`/`clearTimeout`, which jsdom's own hashchange also uses) - and
  `settle(dom)` resolves when none is left, one macrotask after the last one ended so the
  re-render it set off in microtasks has run. A leading zero-delay barrier covers jsdom's one
  untracked hop (link following on Node's clock). `upload()` now awaits `settle()` and keeps its
  check that `<main>` was replaced; every `await wait(N)` in the prototype suites (33 in uplift,
  4 in onboarding, 3 in use cases) became `await settle(dom)`, and `wait` was removed from the
  harness so a fixed sleep is not reintroduced. No assertion changed. `marketing-ai-prototype.html`
  did not change, so the screenshots and the byte-level consistency checks are untouched and
  `CHANGELOG-prototype.md` needs no entry.
  *Consequences:* the suites wait exactly as long as the page takes, so they are also faster on a
  quiet machine (89 tests: ~65-96 s → ~50 s). A page change that adds a recurring timer would make
  `settle()` time out (10 s) with a message naming the pending timers, rather than hang.

- **DEC-876 — Flakiness is measured under load with a repeatable loop.**
  *Context:* "passes three times on my machine" is how these tests shipped. *Decision:*
  `scripts/loop_prototype_tests.sh COUNT TEST_FILE` runs one suite COUNT times and prints the pass
  count; `LOOP_LOAD=N` starts N busy loops for the length of the measurement and kills them on exit,
  failing runs' TAP is kept in `LOOP_LOG_DIR`. *Consequences:* the before/after numbers below can be
  retaken by anyone with the same command.

- **DEC-877 — The jsdom and node suites run in CI, and a missing node or jsdom is a failure there.**
  *Context:* five pytest modules drive node (`test_production_ui_js`, `test_production_ops_ui_js`,
  `test_inactive_settings_ui`, `test_ui_journey`, `tests/unit/uplift/test_uplift_ui`) and skipped
  when node or jsdom was absent - which on CI was always, because CI never set up node; the prototype
  suite was not in CI at all. *Decision:* `tests/fixtures/node.py` holds the one rule
  (`skip_without_node()`, `skip_without_jsdom(dir)`), mirroring `tests/fixtures/postgres.py`:
  skip with a reason locally, fail under `REQUIRE_JSDOM=1`. The variable has no `MARKETING_AI_`
  prefix on purpose: it describes a test run, not a deployment, so `engine/settings.py`'s
  `NON_FIELD_ENV_VARS` and `infra/naming.py` stay untouched. `ci.yml`'s `lint-test` job sets
  `REQUIRE_JSDOM: "1"`, sets up node 22 (`actions/setup-node@v4`, `cache: npm` keyed on
  `tests/**/package-lock.json`), runs `npm ci` in every directory under `tests/` that has a lockfile
  (found, not listed), and runs `make prototype-test`, whose recipe now uses `npm ci` instead of
  `npm install` (a lockfile out of step with `package.json` fails instead of being rewritten).
  `tests/unit/test_container_files.py` pins all of it, including that every `package.json` under
  `tests/` has a lockfile beside it (both do: `tests/prototype`, `tests/integration/production/ui`,
  whose `ops/` subdirectory shares it; `tests/unit/uplift` needs node only).
  *Consequences:* a runner where node or `npm ci` went missing goes red, not green with a note.

- **DEC-878 — CI reads the fast suite's JUnit report for skips of must-run tests.**
  *Context:* the two `REQUIRE_*` variables only cover skips their fixtures know about, and nothing
  noticed a must-run suite that was deselected or renamed. *Decision:* `scripts/check_no_skips.py
  --junit REPORT --id REGEX…` fails (exit 1) when a test whose `classname::name` matches a pattern
  was skipped, naming it and its reason, or when a pattern matched no test that ran; exit 2 for an
  unreadable report. CI runs it after `make check-readme` on the same `junit-fast.xml`, with
  patterns for the five node/jsdom modules and the Postgres tests (`[postgres]` params,
  `test_postgres_metadata`, the Alembic and audit-table Postgres tests).
  `tests/unit/test_check_no_skips.py` covers it. *Consequences:* a new skip path, or a must-run test
  that silently leaves the selection, fails the build; skips outside the patterns (the playwright
  journeys, docker) are not its business.

- **DEC-879 — The Postgres tests already run in CI's `make test`; verified, not changed.**
  *Context:* M56 asked to confirm that CI's Postgres service is actually used. *Decision:* no
  `make test-postgres` step: `make test` is `-m "not slow and not bedrock and not aws"`, which
  selects the 42 `postgres`-marked tests, and `tests/fixtures/postgres.py`'s
  `skip_without_postgres()` calls `pytest.fail` under `MARKETING_AI_REQUIRE_POSTGRES=1`, which the
  job sets. Verified locally against a real PostgreSQL 16 on `localhost:55432`: 42 passed, 0
  skipped; against an unreachable port with the variable set, each test fails naming the URL and
  the driver's error. DEC-878's check adds the outside view. *Consequences:* none beyond DEC-878.

## Measurements

Machine: 4 vCPUs, shared with other agents' test runs (load average 9-25 during the runs).
Command: `LOOP_LOAD=N scripts/loop_prototype_tests.sh COUNT tests/prototype/uplift.test.mjs`
(38 tests in the file).

| Harness | Load | Runs | Passed | Failing tests |
|---|---|---|---|---|
| before (first-swap `upload()`, fixed sleeps), snapshot of `2248cd0` | 4 busy loops | 6 | **0** | the two reported tests, in all 6 runs |
| intermediate (read tracking only, fixed sleeps kept) | 2 × (2 busy loops + a loop) | 6 (stopped) | 4 | "0% control group…" (`wait(1300)` too short), "DEC-608…" (`wait(30)` after a link click) |
| after (DEC-875 `settle()`) | 2 × (2 busy loops + a loop), concurrent | 50 | **50** | none (25/25 and 25/25) |

The "after" row is two copies of the script running at once, 25 runs each, each with `LOOP_LOAD=2`:
four busy loops and two node suites on four cores, which is harsher than the single-loop "before".
