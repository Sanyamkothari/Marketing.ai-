# Data Doctor: the root causes, and one generic fix for each

Status: proposal for the owner's approval (2026-10-27). Replaces symptom-by-symptom patching. Evidence: six read-only
code reviews (one per hypothesis plus an open hunt), spot-checked against the code; full notes kept with the session.

## Why the fixes never ended

Every blocker hit while replaying the real visit on the Online Retail log was a symptom of six structural faults. Fixing
a symptom moved the failure one step further, where the next symptom of the same fault was waiting.

## The six root causes (plain words, then the evidence)

1. **The plan is worked out again by every part of the product, on different data.** The preview reads the first 1,000
   rows, the plan check 1,000 rows of a sample, the AI's query trial 100,000, the prescription the whole sample, the
   final check the full file. Each also picks its own steps, its own key and outcome, its own table names.
   *Evidence:* `preview.py:366`, `plan_check.py:150-152,216,240`, `tools_sql.py:78,228,397` (bypasses `run_recipe`),
   `prescription.py:531-571,793` (swallows errors), `run_prescriptions.py:176-185`.
   *Symptoms:* "Binder Error", missing tables, the 22.9% stop seen only at the very end.

2. **Decisions have no single home.** The customer column, the outcome, the as-of date and the meaning of an answer
   live in question answers, five kinds of items, option labels and step parameters; 8+ readers each apply their own
   rule (first match in one place, last match in another, a regex over an option label in a third).
   *Evidence:* `visit.py:1626` `_key_of`, `prescription.py:889-903`, `agent_review.py:823-846` `case_as_of`,
   `tools_sql.py:388` (as-of frozen into the step), `protocol.py:271` ("does not change the data").
   *Symptoms:* answers that did nothing, the as-of deadlock, the outcome not found.

3. **Checks stop instead of reporting, and the stops live in six places.** Only plan-check findings can be acknowledged;
   the final check's findings are a plain string; doubts have no state; a stop's key includes its count, so a yes given
   on the sample never matches the full file.
   *Evidence:* `guards.py:119-122` (key = code, order, **count**), `visit.py:1462-1469` (not ready → back to TREAT with the
   reason only in an event), `case_visit.py:396-401`.
   *Symptoms:* the dead loop TREAT → APPROVE → VERIFY, "could not answer", stops you cannot say yes to.

4. **Everything reads the raw upload, not the current table.** Rules, detectors, the AI's look tools and the goal check
   read the uploaded file; tables the plan builds exist only briefly inside single checks.
   *Evidence:* `visit.py:1650-2072` via `run.files(case)`; `prescription.py:401` `prepared_key` (a patch on this).
   *Symptoms:* the goal check could not see the AI's outcome; `describe_outcome` failed on a built column.

5. **Derived results are remembered on an incomplete key, and proposals are never retracted.** The rules re-run only
   when files, goal or answers change - not when the plan changes; the AI review runs once; items and questions are
   append-only (no "superseded", no "no longer true"); review, chat and rules each add items their own way.
   *Evidence:* `visit.py:1031-1044` (TREAT memo), `visit.py:1050-1057`, `_write`/`_settle_asks`, `_review` without twin
   check, `_taken_back` ignores replacements of a person-owned item.
   *Symptoms:* "no column has the outcome" and "which column is the outcome?" at once; old and new queries both active;
   rejected changes coming back.

6. **Four different kinds of "yes"** (approve, own-yes confirmation, acknowledge-a-stop, answers that create accepted
   items), each with its own digest; who proposed what is guessed from chat order.
   *Symptoms:* `APPROVAL_CONFIRMATION_MISSING`, extra confirmation sentences, a single analyst blocked by separation of
   duties.

Smaller, real but lower leverage: prompts describing tools and arguments by hand, and limits scattered across modules
(`:as_of`, `load_goal_brief`, 800/2000-character replies, one suggestion per turn).

## The generic fixes

**F1. One materialisation** (`engine/doctor/materialize.py`): `materialize(case, plan, scope=Sample(n, seed)|Full)`
returns the prepared tables, receipts and findings. It is the only caller of `run_recipe`; always all tables, the
case's as-of, the leak check; an error becomes a stop finding, never a silent fallback. The query trial, plan check,
preview, prescription and final check all call it (a trial = the plan plus the candidate step). Before APPROVE the plan
is materialised on the **full** file once, so nothing new can appear after the person approves. Deletes:
`tools_sql._prepared`, `trial_steps_of`, both `case_tables`, `PLAN_QUERY_TRIED_BEFORE_CHANGE`, the raw fallback.

**F2. One decisions model** (`engine/doctor/decisions.py`): `decisions(case)` → key per table, outcome per goal, as-of
date, as-of column, outcome window, consent, … each with value, who decided, when and from what. An answered question
*is* a decision (each option declares which decision it sets; "a note" is explicit). Every reader calls it; SQL keeps
`$as_of` symbolic and the materialiser binds it. Deletes `_key_of`, both `_decided`, `accepted_column`, `case_as_of`,
`prepared_key`.

**F3. One findings ledger**: `Finding{subject, code, level, scope, count}` with `key = hash(code, subject)`; a yes holds
while the count stays at or below the acknowledged one. Guards, query trials, the final check, detectors and doubts all
write to it; the screen, the approval gate and the AI read it. The final check's findings land in the ledger and the
visit rests at APPROVE with them acknowledgeable; a "no progress" guard refuses to loop. Hard rules (min rows, min
positives, leakage, consent) are a non-acknowledgeable level.

**F4. Current tables by default**: rules, detectors, look tools, the goal check and readiness read
`materialize(case, plan, Sample)` (cached by the plan digest); built tables carry their key and time column as
decisions. The raw file stays available on request.

**F5. Complete memo keys and reconciled proposals**: TREAT and review memos keyed on files + plan digest + decisions
digest + goal; each proposal has a **subject** (`outcome@goal`, `sql@name`, `key@table`, `fix@column:kind`); every
producer emits its full set and the store adds, supersedes or retracts (never touching what the person owns; a
replacement of a person-owned item is shown as a swap to approve). Questions re-ask when their options change.

**F6. One approval**: one `needs_yes(case)` list and one `Approval{subjects, digests, sentence}`; the proposer recorded on
the item, not guessed.

**F7 (small)**: limits in `engine.yaml` (`data_doctor.limits`) read through one module; the prompts' tool and argument
section generated from the registry schemas; a contract test.

Hard rules are kept and each is enforced in exactly one place: the approval gate, readiness on `materialize(Full)`, the
leak check inside `materialize`, consent as a decision with its own approval class, egress unchanged.

## Replay paths (roadmap 2.1: one place applies an approved recipe to a file)

A stored recipe's steps are applied to rows in one function, `engine/doctor/materialize.py::replay_recipe` (beside
`materialize_plan`, which applies a case's plan). Every other path is a caller. `tests/unit/doctor/test_single_replay.py`
fails when a module outside `engine/doctor` applies steps itself, or when `dataset_recipe` or `prescription_replay`
gains a runner call.

| Path | Before | After |
|---|---|---|
| Scoring a model's stored recipe (`POST /runs`, `replay_for_scoring`, readiness) | `run_saved_recipe` -> `run_replay` in `engine/agent/dataset_recipe.py` ran `run_recipe` / `run_guarded` itself | `run_replay` delegates to `materialize.replay_recipe` (same levels, limit, guards, acknowledged stops, leak check) |
| Scheduled firing (`engine/scheduling/firing.py`, `prepare_dataset`) | same `run_saved_recipe` | same, now a thin caller |
| Training from a file or built dataset with a prescription (`prescription_runs`, `api/routes/run_prescriptions.py`) | `prescription_replay.run_part_recipe` -> `run_saved_recipe` / `run_replay` | unchanged call chain; reaches `replay_recipe`. The prescription's own checks (intake, locale, currency, new yes/no spellings, step drift, outcome, contract) stay in `prescription_replay` and wrap the call; they are not steps |
| `prescription_replay.replay_prescription` | orchestrates the parts, applied steps through `run_part_recipe` | same, no runner call of its own |
| Next-month recheck (`recheck.py`) | `materialize_plan` | unchanged: it applies a case's plan, `replay_recipe` applies a stored recipe, both in `materialize.py` |
| Onboarding replay (`engine/onboarding/replay.py`) | copies last month's column mapping onto the new file; reads no row | unchanged: not a recipe-step path (the build applies the mapping's transforms) |
| `fingerprint.py`, `carry.py` | detect a known file; carry approved marks into a new case | unchanged: they apply no steps |
| Evals, `api/routes/agent.py`, the Guided-setup agent loop, advisor, edits | try a step while it is written | unchanged: authoring trials, listed in the test |

Left for later: the `TODO Step 4` entries of `test_single_materialisation.py` (modules that try steps on rows they
prepare themselves), and moving the prescription's drift translation (`step_drift`) into `materialize`, which needs
the scoring routes to accept a `PrescriptionError`.

## Order of work (each step ends with the Online Retail replay reaching further, and its tests)

| Step | Fix | Removes | Size |
|---|---|---|---|
| 1 | F1 + final-check findings into the ledger (part of F3) | trial/run drift, late stops, the dead loop | M–L |
| 2 | F2 decisions | answers that do nothing, as-of/outcome/key confusion | S–M |
| 3 | F5 memo keys + reconcile | stale rules, duplicate or contradictory items | L (riskiest) |
| 4 | F4 current tables | tools blind to built tables | M |
| 5 | F3 rest + F6 one approval | scattered stops, extra confirmations | M |
| 6 | F7 contracts and limits | prompt/code drift | S |

Step 1 leaves the preview tables (`preview.py`), the query trial (`tools_sql`, through `candidate=`), `agent_apply`'s check and the replay on their own resolution of the plan; they move onto `materialize` with the current-tables work (Step 4), so one Sample and one as-of reach every caller.
A yes to a stop without a count (a rate moved, a run that does not repeat) holds however the stop grows; counting those is part of the decisions work (Step 2).

Total roughly 2–3 engineer-weeks of work. Stored cases migrate lazily (subjects and decisions derived on read; old
count-keyed acknowledgements are dropped so the person is asked again - the safe direction).

## Tests that catch the class, not one case

1. **Trial = run**: generated plans materialised on a sample and on the full file agree on schema and error class.
2. **Order independence**: the same events in any order give the same questions, items and findings.
3. **No dead loop**: a state-machine test - every resting state offers the person an action that changes something.
4. **Single reader**: an architecture test that only `decisions.py` reads key/outcome/as-of items and only
   `materialize.py` calls `run_recipe` / `run_sql`.
5. **Contract**: every action and argument in the prompts exists in the registry; every limit comes from config.
6. **Golden replay**: the Online Retail replay reaches a prepared customer table, a readiness verdict, and receipts that
   agree between trial, plan check and run.
