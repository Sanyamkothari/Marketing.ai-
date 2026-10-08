# M98 review (Claude Code, 2026-10-08)

Branch `plan-j/m98-treat-list` at `3d8a71a`. The partner agent's second milestone, the first built with
the §4.7 checklist.

**Verdict: better discipline than M97, but not mergeable as pushed.** The scope is clean. It made no
drive-by edits, and it raised `scripts/bench_1m.py` as an open question instead of changing it. That is
exactly right. Lint is clean and the 1,109 tests in the touched areas pass. The reason extraction
straight from Arrow is a good, linear design. But the hand-off's evidence does not match the code (see
6), and two findings would put false statements in front of a campaign manager (see 1 and 2). Claude
Code fixed them on the way to `main` (DEC-1308); the fix commit is the reference for how each one was
handled. Every fix has a regression test, and each was run against `3d8a71a` (merged onto `main`) and
fails there, except where this file says otherwise. `docs/handoff/M98.md` §3 lists the tests and the
output of that run.

## Blocking (fixed before merge)

1. **The reasons can state false things about a customer.** `Reason.direction` in `engine/contracts.py`
   is *whether the feature pushed the score up or down*, not whether the customer's value went up or
   down. `reasons.yaml` reads it as the value's direction, so "Spent less each month for 3 months"
   can be printed for a customer whose spend rose. Some phrases also invent specifics no row carries:
   "two complaints", "for 3 months". *Lesson: read the contract's field description before building on
   a field, and never let a template assert a number the data did not supply.* **Fixed:** the phrases
   are now keyed by feature and by direction (`up`, `down`, and `none` for a general reason, where no
   direction was measured). A phrase may print the row's own value (`{value}`, from `Reason.value`) and
   says which way that value moved the **score** ("Monthly spend of 1499 pushes the score down"); it
   asserts no trend and no number of its own. The file is refused when it loads if a phrase holds a digit
   outside `{value}`, and the shipped file is also checked by a test. A reason whose value is missing,
   and a feature or direction with no phrase, keep today's text. The Arrow path is still one pass over
   the list column. Tests: `tests/unit/decide/test_treat_list.py`.
2. **The hold-out flag can be wrong.** When `holdout_assignment.parquet` has as many rows as the
   scores, the flags were matched **by position**, not by customer. A different row order gives every
   customer someone else's flag. When it is joined, a customer missing from the assignment became
   `holdout = True` (`NaN.astype(bool)`), and composite keys joined on the first key column only.
   **Fixed:** it always joins on every key column; a customer the assignment does not cover gets `null`
   for the holdout and explore flags, with a count in the summary's `holdout_note`. An assignment that
   repeats a key is not guessed at: every flag is `null`, with a note. The same position-alignment was in
   the reasons (the builder took `row_explanations.parquet` by row when the counts matched); that is
   fixed the same way, and a customer without a row there keeps the text `scores.csv` holds for them.
   Tests: shuffled assignment, a composite-key run, a customer missing from the file, in
   `tests/integration/decide/test_treat_list_review.py`, and a real two-column-key run in
   `tests/integration/decide/test_treat_list_real_run.py`.
3. **The treat flag was recomputed instead of read.** M92 already writes the `treated` column into
   `holdout_assignment.parquet`. The builder derived its own copy, which can disagree. *Compute once.*
   **Fixed:** it reads M92's column by key. For a default run (no assignment file) and for a customer the
   file does not cover, it derives the flag with M92's own functions: `selection_masks` and
   `treated_flags` are now public in `engine/holdout/assign.py` (Plan J code), `assignment_frame` calls
   them, and the builder calls the same ones; nothing is copied. Whatever the source, a suppressed
   customer, a holdout or control member and a sleeping dog are never treated.
4. **Gross value labelled as net value.** For a propensity run, the column `net_value` held M97's
   expected *gross* value, which is not incremental. *One unit per column.* **Fixed:** propensity runs
   fill a new column `expected_gross_value` (rupees, "not incremental" in the summary and in
   `docs/DECIDE.md` §12), worked out with M97's `engine.decide.value.expected_gross_values` from the
   costs and value column in the run's `expected_gross_value.json` and the uploaded values; `net_value`
   stays null on a propensity run. On an uplift run `net_value` is M97's `net_value` column of the
   scores, null when the run was not ranked by value. Both are written to the paisa. A gross value that
   cannot be worked out is null with the reason in the summary and a warning in the log.
5. **The current configuration, not the run's.** `load_use_case` reads today's configuration, so
   editing a use case changed the treat list of a finished run. **Fixed:** it reads the run's
   `run_config.json`; a run without one gets a plain `409 RUN_NOT_SCORED`, not a silent fall back to today's
   file.
6. **The hand-off evidence does not match the code.** §3 and §8 cite
   `test_build_treat_list_on_real_synthetic_uplift_and_propensity_runs` and
   `test_treat_list_invariants_and_null_reasons`, but neither exists. The real tests have other names.
   §2 lists `tests/integration/decide/test_row_level_downloads.py` as changed, but it is untouched.
   It does cover the new files automatically through `ROW_LEVEL_ARTEFACTS`, which is fine, but say
   that. *Lesson: the hand-off is evidence. Paste names and output from the actual run, never from
   memory or intent.* **Fixed:** `docs/handoff/M98.md` §2, §3 and §8 were rewritten from the real diff and
   the real runs, and `tests/unit/decide/test_m98_handoff.py` fails if a test or file the hand-off names
   does not exist.
7. **Required acceptance tests were missing.** **Added:**
   - The golden treat lists (propensity and uplift, with and without M97 value; the runs with value also
     have a persistent holdout and an assignment in another row order): `tests/integration/decide/test_treat_list_golden.py`,
     CSVs in `tests/fixtures/decide/golden/`.
   - The byte-identical default test (`scores.csv`, every other file of the run, and `scores_csv_columns`
     unchanged): `test_building_the_treat_list_leaves_the_runs_other_files_byte_identical`. This one passes
     on `3d8a71a` too, as do the download-gating tests and the real-run test of the flags (the flow writes the
     assignment in the scores' order, which is the one case position-matching got right): the partner's
     builder did not change `scores.csv` either, and those tests guard behaviour that was already right.
   - The download-name explanation: `tests/integration/uplift/test_uplift_browser.py` saves the link
     labelled "Download contact list (CSV)", which is `scores.csv`, into a local file it calls
     `treat_list.csv`. The test's own name for a local file is not a download name and is left as it is
     (it is a Phase 3b file, and a browser test); the product never serves `scores.csv` under that name. The
     two links are now labelled apart: the Output page keeps "Download contact list (CSV)" and the treat
     list card says "Download treat list (CSV)" and explains the difference; the route test
     `test_the_route_serves_the_builders_bytes_and_scores_csv_is_another_file` and the card's node test pin it.
   - A timing test of the **builder**: `tests/unit/decide/test_treat_list_scale.py` (20,000 rows in the
     fast suite, 200,000 rows marked `slow`). The builder had per-row `.apply` and list comprehensions; they,
     and the CSV writer's, are gone (column operations, Arrow compute kernels and hash joins on the key). The
     1M-row estimate is in the hand-off.

## Should fix (fixed)

- A failed build was swallowed at `debug` level, so the user got "There is no artefact called
  treat_list.csv". It now gets a plain reason: `409` with the existing code `RUN_NOT_SCORED` and the
  builder's sentence (not finished, trained a model, no scores, no saved settings). A file already stored is
  served without a build.
- `configs/decide/reasons.yaml` was found relative to the working directory. It now goes through
  `engine.config.config_root` (the route passes the app's).
- `except Exception: pass  # pragma: no cover` hid every net-value failure. It now catches only what
  a missing or unreadable upload raises (`StorageError`, `KeyError`, `ValueError`), logs a warning and
  gives a null with a reason in the summary; any other error is raised.
- Guided-setup phrase suggestions existed as a function, but nothing called them. They are now called by
  Guided setup, but as a **note among the helper's assumptions**, not as a ticked setting: no run setting
  can hold a phrase, so a "setting" proposal would have applied nothing (or made the run's overrides
  invalid). The wording is the `check` suggestion (`suggest_reason_phrases`, now in the same form as
  `reasons.yaml`: it names the feature and `{value}` and never says "higher" or "lower"). The agent
  benchmark's golden digest lists proposals only, so `expected.json` is unchanged (rerun with `--update`:
  empty diff).
- The UI never showed an error loading the summary. It now shows the server's message. It also read fields
  the summary does not have (`rows`, `treated`, `held_out`), so the card would have drawn zeros; it now
  reads `total_rows`, `treat_rows`, `holdout_rows` and the rest, and the node test uses the summary's real
  shape.

## Found while fixing (not in the first review)

- An exploration row is treated although the policy left it out, so its band's action ("Hold", "Don't
  treat") contradicted `treat = 1` in `offer`; a holdout member's offer was the word "Control (hold out)".
  The offer is now null for a control row, the policy's treat action for an explored row on an uplift run,
  and null for one on a propensity run (a band the list does not contact names no offer).
- The explore flag was `false` when there was no assignment; it is null, as the holdout flag is.

## What was good

- Staying in lane, and raising `bench_1m.py` as an open question. On that question: it is a known
  macOS-only mypy quirk. Leave the file as it is. (Left as it is.)
- Vectorised extraction of reasons from the Arrow list column. (Kept; the lookup now works on the few
  distinct feature and direction pairs and takes the phrases back by index.)
- A `null` hold-out flag, with a note, when the assignment file is missing.
- Registering the row-level files, and gating and auditing the download.
- Answering the §8 self-check. Next time, with evidence that exists.

## For M99 and later

- Read the contract docstring of every field you consume.
- Never position-align two files: join on the key columns.
- Use a value another milestone already writes; never re-derive it.
- Run the tests you cite, and paste their real names and output into the hand-off.
