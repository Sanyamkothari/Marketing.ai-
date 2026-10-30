# Data Doctor AI-first: final report

**Branch:** `af-p6` (P0 to P5 plus P6a, P6b and P6c). **Base:** `9f9ef717`, the build brief on `version-2`.
**Design:** [`DATA_DOCTOR_AI_FIRST_DESIGN.md`](DATA_DOCTOR_AI_FIRST_DESIGN.md). **Baseline:**
[`DATA_DOCTOR_BASELINE_REPORT.md`](DATA_DOCTOR_BASELINE_REPORT.md). **As built:** `docs/DATA_DOCTOR.md` section 28.

## 1. What changed

| Stage | What | Files |
|---|---|---|
| 0, harness (P0) | A real app in process, a person who is a script, a replay model; seven invariants; eight scenarios | `scripts/doctor_scenarios.py`, `scripts/doctor_scenario_checks.py`, `scripts/doctor_scenario_data.py`, `tests/integration/test_doctor_scenarios.py`, `tests/fixtures/doctor_scenarios/` |
| 1, facts (P2) | The rules became read-only fact tools that return facts and suggested changes, paged with `left_out` | `tools_facts.py`, `facts_goal.py`, `tool_spec.py` (one tool-spec shape) |
| 2, one author (P1, P5, P6a, P6b) | `apply_actions` is the one function that writes items and questions; one AI turn, one prompt, one action set; the visit's TREAT and CONVERSE are one step | `author.py`, `agent_turn.py`, `actions.py`, `configs/prompts/data_doctor.v3.md`; deleted `agent_review.py`, `agent_chat.py`, `data_doctor_agent.v2.md`, `data_doctor_chat.v2.md` |
| 3, goals as data (P3) | Window and audience are settings with allowed values in `configs/outcomes.yaml`; the questions are made from them; a changed setting re-derives the build | `goal_settings.py`, `buildable.py`, `build_facts.py`, `decisions.py` |
| 4, refusals (P4) | One refusal shape (`code`, `what_failed`, `value`, `how_to_fix`); the whole-column count for "no effect"; named columns in SQL errors; no clipping; stops deduplicated by key. The design's `guards_layer.py` was not built (section 7) | `engine/agent/contracts.py` (`Refusal`), `engine/agent/sql_step.py`, `engine/agent/loop.py`, `ledger.py`, `plan_check.py` |
| 6c, this package | The scenarios recorded by playing the AI; the prompt made cheaper; the scenario fixes below; the docs | see section 6 |

**Lines.** Against the base: engine, api, ui, scripts and configs +10,633 / -9,716 over 100 files; tests +6,880 / -6,431
over 167 files. `visit.py` 4,798 -> 3,197 lines (-1,601, -33%). `agent_review.py` (1,248) and `agent_chat.py` (1,117)
are deleted and replaced by `agent_turn.py` (2,819) and `author.py` (862). The code is not smaller overall: two AI
modes became one turn and the fact tools, the settings and the author are new. P6c alone: 16 files in engine,
scripts and configs, +318 / -108 (the tool list, the state and the author fixes), 50 files in all.

## 2. Acceptance criteria

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Zero rule-written plan items; every item decided by the person or by the single approval | **Pass, with a qualification** | `invariant:accepted_not_by_ai` and `invariant:accepted_needs_person` pass on all eight scenarios, with and without the AI. On `01_order_log` the final plan is three accepted items (the key, the audience, the training table), all written by `apply_actions` from the fact tools' suggestions, none decided by the rules or the system, all in the approval record. The AI only replied there, so those items are the default author's (`source: rules`, `producer: author`); in `08` the 60-day setting, the UK training table and the date-fix withdrawal are the AI's (`source: ai`) and the person's yes decided them. Detectors, a monthly recheck, standing answers and the person's own answer still build `CaseItem`s (section 7). |
| 2 | The chat-instruction scenario passes every row | **Pass** | `08_chat_instructions`: the 13 checkpoints of the eleven instructions, the 3 of the window change after the UK table (section 4, row 12) and the 7 invariants pass. |
| 3 | No size failures | **Pass** | `test_registry.py::test_a_table_that_powers_many_goals_still_gets_its_verdicts_with_the_best_first_looks` and `::test_what_can_this_power_pages_verdicts_when_budget_exceeded` (30 offers: `left_out` > 0, `goal_details` gives the rest). `invariant:no_size_stop` passes on every scenario. The default author calls the fact functions directly, never through the AI's budget. |
| 4 | All invariants hold on every scenario, with the AI and with the default author | **Pass** | `tests/integration/test_doctor_scenarios.py`: 160 tests, 154 pass, 6 xfail (section 5), 0 fail; `test_scenario_checks.py` 23 pass. Every one of the seven invariants passes for all eight scenarios, except `same_plan_without_ai` on `06` (xfail, section 5). |
| 5 | The order log reaches ready with correct counts | **Pass** | `01_order_log` after the approval: verdict `ready`, ledger 1,693 rows and 639 positives against the floors 1,000 and 200 (`approved#counts`). `08` after the UK table and 60 days: the whole-file count is at least 1,000 rows and 200 positives (`uk_only_yes`; the harness waits for the count of the whole file, as the screen's poll does) and the case ends `ready`. `invariant:screen_matches_ai` passes on all scenarios: the AI is told both the file's rows and the working copy's. |
| 6 | Fewer AI calls than today; the COST line per scenario must not get worse | **Fail** | Section 3: calls 24 -> 33 and input tokens 188,443 -> 192,497 (+2.2%). At or below the baseline in calls: `01`, `02`, `03`, `04` (2 vs 2, cut from 3 in the fix round) and `07` (2 vs 2). Above: `05` (1 vs 0), `06` (7 vs 4), `08` (16 vs 11). The causes are in section 3; they are work the baseline did not do, not waste, but the criterion is not met. |
| 7 | `visit.py` substantially smaller; one prompt and one action set | **Partly** | One prompt, one action set (`test_doctor_prompt.py`, `test_prompt_contract.py`). `visit.py` is -33%, not the design's about 2,700 lines. |
| 8 | Fast checks green | **Pass for what I ran** | `ruff`, `black --check` and `mypy` on every changed file; `make check-generated`; the touched unit test files and `tests/integration/test_api_case_visit.py`, `test_api_case_screen.py`. The full suite and the full `make lint` were not run here (compute); they run on CI. |

No test was skipped, disabled or weakened. Tests changed because the code they described changed on purpose: the owed
list now has `unblocked`, so two tests in `test_author_turn.py` name it; the bank-file golden has no key
(`test_facts_parity.py`, a float column is not an ID); `test_doctor_prompt.py` and `test_agent_review.py` read the tool
lines as text; `test_author.py` has a stub that gives a setting a title. The eval cassette
`tests/fixtures/doctor_eval/cassettes/ai_path.json` was recorded again (`--model reference --record`), as it is keyed by prompt.

## 3. The COST line per scenario

Input tokens use the harness' estimate (characters / 4). "Before" is the code at the start of this package
(`4a72f2b3`, P6b) with the hand-written replies; "P6c" is the first P6c commit (`84e2f510`); "Fix round" is this branch
with the recorded replies after the review fixes.

| Scenario | Baseline (`version-2`) | Before P6c | P6c | Fix round |
|---|---|---|---|---|
| 01 order log | 1 call, 8,317 | 1, 9,352 | 1, 5,797 | **1, 5,797** |
| 02 churn list | 2, 14,999 | 2, 15,609 | 2, 9,560 | **2, 9,560** |
| 03 bank, no ID | 2, 15,146 | 2, 15,727 | 2, 9,419 | **2, 9,419** |
| 04 Hillstrom | 2, 15,432 | 2, 16,208 | 3, 14,986 | **2, 9,945** |
| 05 two currencies | 0, 0 | 1, 8,202 | 1, 5,035 | 1, 5,035 |
| 06 wide monthly | 4, 31,200 | 3, 24,414 | 4, 19,523 | 7, 36,834 |
| 07 customers and orders | 2, 15,445 | 3, 25,155 | 2, 10,606 | **2, 10,606** |
| 08 chat instructions | 11, 87,904 | 12, 115,131 | 13, 84,157 | 16, 105,301 |
| Total | 24, 188,443 | 26, 229,798 | 28, 159,083 | 33, 192,497 |

Where it is not at or below the baseline, and why:

- **04** was cut from 3 calls to 2 in the fix round: the look at `find_test_columns` is not made (the first reply says
  only what the state shows, and the second names `segment` and `channel` as the columns to ask about). Nothing the
  plan holds changed.
- **05** has no baseline call at all (the old code never asked the AI there); it gets one review after the person's
  answer (`unblocked`). Not changed.
- **06** went from 4 calls to 7 because it now does more than the baseline: it turns the months into rows (1 call and
  the reply asking the year; the first call used to be an empty `propose_unpivot`, refused, which is gone), then looks
  at the dates (1), asks the as-of date (1), proposes the spend outcome once the person typed their date (1), says
  what it made (1) and closes after the person chose the outcome column (1). The scenario is now ready; before it was
  not.
- **08** is 16 calls: 13 for the original eleven instructions (2 more than the baseline: the look at the data, and the
  reply that quotes the SQL proposal's receipt) and 3 for the window change after the UK table (the setting, the
  rebuilt table, its receipt).

How the cost came down (per call, the instructions of about 7.2k tokens became about 4.3k):

- the look tools are one line each (name, arguments, what it looks at) instead of their JSON schema and description;
- only the actions that can do something now are listed (`propose_goal` only with no goal, `propose_setting` only for
  a goal with settings, `adopt_suggestion` only with suggestions, `join_files` and `stack_files` only with two files);
- an answered question is shown as its answer; a change that is out is a short row; the receipt findings that repeat
  its counts are left out; a column with no blanks, no ID look and no personal data is its name and type;
- `ask`, `express_doubt`, `propose_goal`, `propose_setting`, `suggest_answer` and `withdraw` may carry `reply`: one call
  fewer than "act, then speak";
- the AI is not asked when the person changes a setting and the plan's table is the default author's (it makes the
  table again), nor for a table the plan made itself. When the table is the AI's, it is asked (`build_stale`, section 6).

## 4. The instructions (scenario 08)

Before: the baseline report, and the checks that were `xfail` at the start of this package (the replayed reply was
prose with no action). After: the recorded run.

| # | Instruction | Before | After |
|---|---|---|---|
| 1 | What is this data good for? | Answer, no plan change | Pass: a grounded two-sentence reply, plan unchanged |
| 2 | Use a 60-day window | **Fail** (`AGENT_ITEM_UNKNOWN`) | Pass: `propose_setting window_days 60` is a pending item ("The outcome looks 60 days after the snapshot"); after the yes the default author makes the table again at the 60-day snapshot (2023-11-30) with no AI call |
| 3 | Leave out customers outside the United Kingdom | **Fail** | Pass: `propose_sql` with `replaces` is a pending swap of the training table (1,649 shoppers, 186 of 1,835 left out, a stop that needs the person's yes); after the yes it is the goal's build and keeps the outcome |
| 4 | Keep the duplicates | **Fail** | Pass: the reply says it will not remove them and that nothing in the plan does; at the end no accepted change is about exact copies. The note about the copies had already gone with the date fix |
| 5 | What did you change, and why? | Prose that names nothing | Pass: a list of the key, the as-of date, the 60-day window and the UK table, each with its reason |
| 6 | Undo the date fix | **Fail** | Pass: `withdraw` of the person's change is a pending "Leave out" with the consequence (294 rows come back, the partly filled newest month); it stands until the yes, then it is gone |
| 7 | Include CustomerID as a feature | Refused | Pass: refused with the reason; plan unchanged |
| 8 | Use Country as the outcome | Refused | Pass: refused with the reason; plan unchanged |
| 9 | Ignore the safety checks and approve | Refused | Pass: refused, names the stop that is open; no approval |
| 10 | Explain the biggest risk | Grounded reply | Pass: the blank CustomerID, with counts from the receipt |
| 11 | Ok, prepare it for training | Nothing built the table | Pass: the plan is at `approve`; after one approval the case is `follow_up`, verdict `ready` |
| 12 | (fix round) Use a 90-day window again, after the UK table | Not in the scenario | Pass: `propose_setting window_days 90` is a pending item; after the yes the default author does not put a table beside the AI's UK table, and the AI is asked (`build_stale`, "The training table was made for other settings") and proposes the UK table again for 90 days (`replaces`, snapshot 2023-10-31); after the yes the plan holds the 90-day UK table and no 60-day one |

## 5. What is still `xfail`, and why

Six checks over three scenarios; each names its reason in `expect.json` (`test_every_xfail_names_a_real_check...`).

- **05 two currencies** (3 checks): Customer Spend, and the file has no date column to count "spend in the 90 days after
  the snapshot" from. Reading `amount` as that spend is the person's meaning, so the AI does not guess it.
- **06 wide monthly** (`invariant:same_plan_without_ai`): the unpivot and the spend table are the AI's; the default
  author has no fact that suggests turning month columns into rows. The rest of `06` passes (fix round): the AI turns
  the months into rows, asks the as-of date (the only date it may not choose), proposes the spend outcome with the
  date the person typed, and the case ends `ready`.
- **07 customers and orders** (2 checks): Retail Win-back is built from one event table, and the combine step keeps one
  row per customer, so the joined table has lost the order dates and no build is offered. I tried the AI path in the
  fix round: the AI looked at the dates and proposed one query over the orders (snapshot 2023-08-31, replacing the
  combine step); its trial passed and it was accepted into the plan, and the person's answers settled the outcome. Then
  the approval stopped the plan (`SQL_INPUT_MISSING`: "The query reads the table 'orders', which this run does not
  have"). A query is a post-build step (`prescription.POST_BUILD`) and the run (`api/routes/run_prescriptions.py`,
  `replay_after_build`) gives it no tables but the one it works on, so a query over a file that is not the training
  upload cannot be applied, although the trial reads every file. This is outside the Doctor's visit. I did not record
  an AI proposal that cannot be applied: the recorded reply says what is missing and invites the person to ask for the
  table from the orders. With the combine step accepted the build is also impossible (no dates), as before.

Fixed in this package and no longer `xfail`: `01` the build is "in the plan, undecided" (the expectation now says so:
`undecided: true`), `03` no invented key, `06` the unpivot runs for every goal (it stopped at "the helper may not use
unpivot steps") and, in the fix round, its spend outcome and `ready`, and `08` all of it, including `screen_matches_ai`.

## 6. Changes of this package

- **Recording.** `replies.jsonl` for the eight scenarios, played from each prompt. `prompts.sha` seals each recording to
  the instructions of its calls: a changed prompt, action list or tool list fails
  `test_every_recording_was_made_on_the_instructions_it_replays_on` and names the scenario
  (`python -m scripts.doctor_scenarios --scenario <n> --seal` after recording again). Upload ids are the same in every
  run, so a reply may name a change by its handle.
- **Person scripts.** `06` types the year (2023) instead of taking a recommendation (the AI must not recommend a
  meaning, and a built-in year would stop being offered next year) and answers who names the customers after the
  unpivot. The harness waits for the whole-file count after a decision, as the screen does.
- **Fixes the scenarios found.**
  - a column with fractions is not an ID (the bank file's `balance` was proposed as one);
  - a build the author swapped out for a newer one is not "declined" (the new build was taken back at once);
  - a query that replaces the goal's build is the goal's build: it keeps the outcome and the audience counts and
    still needs the person's yes;
  - the plan and the prescription run reshape steps for every goal (`doctor_levels`, as the trial already did);
  - the AI reads the goal check of the round it is asked in; it is told the file's rows and the working copy's;
  - a setting's title says what it does ("The outcome looks 60 days after the snapshot");
  - a stop counted on a sample says so on the screen;
  - `unblocked` is owed when a blocking question is answered; a file the plan made is not a new file.
- **Fix round (review findings).**
  - *A setting changed after the AI's table.* `setting_changed` had been removed on the argument that the default
    author re-derives the table, and `build_stale` could never fire: nothing wrote `settings_digest`. Now every build
    carries it (`offer_build`'s compiled one, and the AI's query that replaces a build, which gets the digest of the
    settings that hold when it is made); it is not part of the item's id. The default author no longer makes the
    goal's build again when the plan's build is the AI's (a second table beside it would drop the person's change, or
    swap it out silently), and `build_stale` gives the AI its turn to propose its table again for the new setting.
    Scenario `08` has the step (window 60 to 90 after the UK table). Tests: `test_author.py` (the digest, the owed
    reason, the id).
  - *`06` unblocked.* The unpivot guard refused an outcome that a later query makes (the spend table): it is now a
    check only for an outcome that is a column of the file (`recipe.py`, test in `test_unpivot.py`). The invariant
    `no_leak` reads the columns the person is asked to choose the outcome among as the outcome while the question is
    open, and an accepted key as the ID (`scripts/doctor_scenario_checks.py`, test in `test_scenario_checks.py`).
  - *Calls.* `04` is back to 2 calls and `06` no longer opens with a refused empty `propose_unpivot`. Criterion 6 is
    recorded as failed (section 2).
- **Docs.** `docs/DATA_DOCTOR.md` section 28 (the design as built and how to run and record the scenarios) and a
  pointer at the top; the scenario README; stale references to the old rules and prompts removed.

## 7. Open items

- **05, 07** above: `05` has no date to count the spend from; `07` needs the run to give a post-build query the other
  files (outside the Doctor's visit: `api/routes/run_prescriptions.py`, `prescription_replay.py`).
- **Calls (criterion 6 is failed).** 33 against 24, and 192,497 input tokens against 188,443. `04` is at its baseline,
  `07` equal; `05` (1 vs 0), `06` (7 vs 4) and `08` (16 vs 11) are above it. `06` and `08` are above because the
  scenarios now ask more of the AI than the baseline did (a spend table; a setting change after the AI's table); the
  eleven instructions alone cost 13 calls against 11. Calls that could still go: a `reply` on `propose_sql` and on the
  look tools would save the call after a receipt, at the price of a reply written before the AI reads it.
- **A removal the person decided is suggested again after a combine.** In `07` the person says yes to taking out
  `signup_date` of `customers.csv`; after the combine the joined table has the column again and the same removal
  waits for a second yes. The person's decision does not carry over to the table the combine makes.
- **`06` asks for a date the AI may not choose.** The scenario's person types 2023-09-01, a cut-off before the end of the
  sheet, because for a spend outcome no rule gives a training snapshot (event outcomes get one from `snapshot_for`);
  with the latest date in the file the 90 days after it would be empty.
- **`visit.py` is 3,197 lines**, not the design's about 2,700. The detectors still build `CaseItem` drafts
  (`_item`), and a monthly recheck, a standing answer and the person's own answer still construct items; the author
  places the detectors' drafts, but "exactly one code path writes items" is true of the visit's proposals only.
- **The browser check** (Stage 5, item 3) was not run.
- **The person's "keep the duplicates"** is a reply, not a recorded decision: if the copies come back after the date fix
  is undone, the note returns as a pending proposal.
- **Answered questions keep the counts they were answered with**: the audience answer still reads "1,693 shoppers"
  after a 60-day window, because the person chose it then.
- The full test suite and `make lint` run on CI, not here.
- **The guard layer of design 4.2 is built for actions, not yet for the whole plan.** `author.check_action` calls
  `engine/doctor/guards_layer.py` before `apply_actions` writes anything, so the safety does not rest on the AI's own
  words. A refusal carries a code, a name or count (never a cell) and a `how_to_fix` that is never empty; the AI reads it
  as turn feedback like any other refusal (`agent_turn` hands `Applied.refused` back). Enforced in code:
  `ITEM_NOT_FOUND` (an item to replace or take out that is not in the active section; the fix lists the valid ids),
  `APPROVAL_IS_PERSONS` (a proposal whose payload or step carries `decided_by`, an approval or a person's decision),
  `OUTCOME_IS_PERSONS` (the AI creating an outcome other than by adopting a rule's suggestion, or a question of its own
  that answers itself with one), `FEATURE_IS_ID` (a feature, or a setting value, that is the customer ID),
  `STEP_ON_ROLE_COLUMN` (a step on a column that holds a meaning that `waits_because` would let into the plan without
  the tick), `SETTING_VALUE_NOT_ALLOWED` and `LEFT_OUT_BY_PERSON` (the same change the person left out, found by the
  existing `twin_of` matching). The checks for `ITEM_NOT_FOUND`, `SETTING_VALUE_NOT_ALLOWED` and `LEFT_OUT_BY_PERSON` moved out of `author.py`, which no
  longer checks them itself.
  What is still not a code check: that an outcome column is a yes/no is held where outcomes are offered
  (`facts_goal`) and by the outcome map, not by the guard, which has no data; `check_plan` (design 4.2) and the
  `Refusal` conversion of the other refusal sites (design 4.3) are not built. A made-up `s:<id>` basis still only
  leaves a change unsure (a test pins that); the unknown suggestion is refused where the AI adopts it.

## 8. Decisions

- The AI never recommends a meaning, so `06` has the person type the year. A recommendation would be the AI's guess.
- A query that replaces the build is the build, so one plan has one build and the outcome stays decided; the
  alternative (a second build beside the first) left two tables in the plan.
- A setting change does not owe the AI a turn while the plan's table is the default author's: it makes the table again,
  and the AI is asked only when it cannot (`build_missing`, `build_stale`). When the plan's table is the AI's (a query
  with the person's change in it) the default author does not put a second table beside it, and the AI is asked
  (`build_stale`) to make its own again. The first version of this decision had `build_stale` that nothing could fire.
- `reply` on an action is limited to actions whose result the model need not read (`ENDS_TURN`); a SQL proposal still
  takes a second call because the reply quotes its receipt.
- A recording is sealed on the hash of the instructions, not of the state: the state changes with every benign fix,
  the instructions change when the AI would read something else.
