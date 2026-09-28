# Plan G (Guided setup) on a 1,000,000-row file

**Milestone:** M77 (hardening). **Date:** 2026-09-28. **Code:** `claude/intelligent-brahmagupta-jauho7`
@ 48d55d5 (M74–M76) plus the M77 branch.

## Method

`python -m tests.fixtures.agent_bench.perf --rows 1000000` (the script is in the repository):

1. Writes the messy Targeted Advertisement file (`tests/fixtures/agent_bench/make_messy.py`,
   1,000,000 rows, 15 columns: `"2.4%"`, `"₹1,234.50"` / `"Rs. 1,234.50"`, three date styles, `Y/yes/N/No`,
   case and space variants, an e-mail column, a leaky column) to Parquet, and finds the recipe of
   the fixes the advisor is sure of on its first 20,000 rows.
2. Writes a Retail Win-back order log of about the same size (`make_multirow.py`, 197,000 shoppers,
   998,765 rows) and plans its `combine_rows` step the same way.
3. Runs each step in a **fresh process** that reads the Parquet file, and reports wall time and
   memory: the frame's own size, and the process's peak resident set before and after the step
   (`ru_maxrss`; the peak includes reading Parquet, so a step that stays under it shows no rise).

| Step | What it measures |
|---|---|
| `profile` | `ingest.profile_dataset` - done once at upload, not by Guided setup; shown for scale. |
| `formats` | `find_format_issues` on the frame the helper sees. The profile cap is 2,000,000 rows, so for this file that is every row. |
| `advise` | The whole advisor, rules only - what `POST …/agent-session` runs. |
| `recipe` | `run_recipe` on every row with the six sure fixes (`parse_number` ×2, `normalise_text` ×2, `parse_date`, `map_boolean`) - the recipe part of Approve. |
| `multirow_advise` | The advisor on the order log (it asks whether to combine). |
| `combine` | `run_recipe` with the `combine_rows` step and the **full** future-data leak check, as Approve runs it on a training file. |

**Machine:** a Linux container, 4 vCPU Intel Xeon @ 2.10 GHz, 15 GiB RAM, no swap, Python 3.11.15,
pandas 2.3.3. One run per step; other work on the machine was kept light during the runs.

## Results

| Step (1M rows) | Before (cb5a8d6 / 48d55d5) | After (M77) | Peak RSS after |
|---|---|---|---|
| `profile` (reference) | 78.5 s | 83.2 s | 1,123 MiB (frame 736 MiB) |
| `formats` | **72.0 s** | **25.4 s** | 1,321 MiB |
| `advise` | **285.9 s** | **110.4 s** | 1,590 MiB |
| `recipe` (six fixes, every row) | **58.3 s** | **3.7 s** | ≤ 1,123 MiB (under the read peak) |
| `multirow_advise` | — | 8.4 s | ≤ 844 MiB |
| `combine` (full leak check) | **fails**: false `FUTURE_EVENTS_LEAKED` | **6.0 s** | 1,044 MiB (frame 284 MiB) |

Before-numbers for the first four steps are from `cb5a8d6` (M74), which has the same parsers and
advisor data path as 48d55d5; `combine` did not exist there, and on 48d55d5 it refused every order
log above about 300,000 rows (see below).

## What changed

1. **Parsers run once per distinct value** (`engine/agent/formats.py`). `parse_number`,
   `map_boolean` and `normalise_text` looped over every cell and wrote each result with
   `Series.at`, which dominated; `date_order` scanned every cell; `parse_date` parsed every cell.
   A parser's output depends only on the cell's text (DEC-1004), so each now runs on the distinct
   values and the answers are spread back with a hash lookup - the same function, value for value
   (`test_parse_numbers_matches_the_row_by_row_loop`, `…_map_booleans_and_normalise_texts…`,
   `test_parse_dates_parses_each_spelling_once_with_the_same_answer`, and the existing row-wise
   tests). A million-row column of a few thousand spellings costs a few thousand parses. `recipe`
   went from 58.3 s to 3.7 s; `formats` from 72.0 s to 25.4 s (what is left is the detector's
   own passes over every value, mostly on columns where every value differs - e-mails, IDs).
2. **The outcome's label is resolved on the outcome column only** (`engine/agent/tools.py`).
   `describe_outcome` and the outcome-rate buckets passed the whole frame to
   `validate.resolve_positive_label`, which derives facts (type inference, personal-data scans) for
   every column and reads one. That was about 40% of `advise`.
3. **The combine leak check runs single-threaded** (`engine/agent/reshape.py`). The combine build
   runs DuckDB on one thread so `latest` breaks same-day ties the same way every time; onboarding's
   leak probe opened its own connection with DuckDB's default thread count. From about 300,000 rows
   DuckDB scans in parallel, breaks those ties differently, and the probe reported a value that
   "moved": Approve answered `RECIPE_STEP_INVALID: FUTURE_EVENTS_LEAKED` for every realistically
   sized order log (reproduced at 303,258, 404,342, 606,370 and 998,765 rows; passes at 201,888).
   `test_a_large_order_log_passes_the_full_leak_check` pins it (~300,000 rows).
4. **Preview runs on a sample** (`api/routes/agent.py`): the first 1,000 rows, or every row of the
   first 1,000 entities when the recipe combines rows (Plan G §6.3). Before, it ran every step on up
   to the whole profile-capped file to show five rows.

## What is left, and where the time goes

- `advise` at 110 s is now mostly the engine's own work on a million rows: `check_data` is
  `validate_for_training` (type inference with date parsing over every text column, the leakage
  probe) and the advisor re-profiles the prepared file (`ingest.profile_dataset`, ~80 s alone on
  this file) so the settings rules see cleaned types. Both are shared engine code, measured the same
  way the Run button measures it; neither is a Plan G loop. It is paid once, when Guided setup is
  opened, not on each click.
- Every other session request re-reads the upload (up to the profile cap) and, for a scoring file
  of a model with a recipe, replays it: on a million-row CSV a decision costs the read, a few
  seconds. Answering a role question re-runs the advisor.
- Approve on this file = read every row + `recipe` (3.7 s) + write Parquet + profile the prepared
  file (~80 s, engine) + the Run button's checks.
- Everything fits comfortably in memory on this machine: peak ≈ 2.2× the frame.

Reproduce: `python -m tests.fixtures.agent_bench.perf --rows 1000000` (or `--only formats recipe`).
