# Data Doctor scenarios

Each folder is one scenario that `scripts/doctor_scenarios.py` plays against the real app, in process.

| File | What it is |
|---|---|
| `person.json` | The person's actions in order: `upload`, `consent`, `visit`, `goal`, `answer`, `decide`, `chat`, `approve`. A step may carry a `name`. A `chat` step carries an `instruction_id` and what it `expects` (`answer`, `change` or `refusal`). |
| `expect.json` | `after`: what must be true once a named step is done (`"step#label"` is a second group for the same step). `final`: the last step. `xfail`: the checks that fail today, each with the stage of the AI-first build that fixes it. |
| `replies.jsonl` | The AI's replies, recorded by an AI player (one JSON line per model call): the player read each prompt in `next_prompt.txt`, used only what it showed, followed its rules and guides, and never invented a number. |
| `prompts.sha` | The hash of the instructions the AI was shown at each call when the recording was sealed (`--seal`). A change to the prompt, the actions or the tools makes `test_every_recording_was_made_on_the_instructions_it_replays_on` name the scenarios to record again. |
| `baseline_replies.jsonl` | Hand-written prose that stood in for the AI before the recording, with no tool use. It is the baseline of the code before the AI-first build and nothing else. It is used only while there is no `replies.jsonl`. |

`xfail` keys are checkpoint names, or `invariant:<name>` for the seven invariants of the brief (Stage 0, item 5; the first
is two checks: `accepted_not_by_ai`, and `accepted_needs_person`: a change the author put in the plan is "in the plan, undecided" until the approval, it never carries the decision of the rules or the system, and past the approval every change in the plan is the person's). What must hold
whatever the AI does (`approvals`, `none_accepted_by_ai`) goes in its own `"step#safety"` group, never in a group that is
marked `xfail` (the "#safety" group does not fail for a step that did nothing). `approvals` is an exact count. The
test marks each one `xfail(strict=True)`: the day a check passes, the suite fails until its entry is removed.

Run one scenario with `python -m scripts.doctor_scenarios --scenario 01` (add `--no-ai` for the default author). When the
recorded replies run out, the run stops and writes the next prompt to `next_prompt.txt`; the player appends one JSON reply
line to `replies.jsonl` and runs it again. When the scenario is done, `--seal` keeps the hash of each call's instructions
in `prompts.sha`. A reply names a change by its handle: upload ids are the same in every run, so the handles are too.

The `xfail` left are `05` (Customer Spend with no date column to count the days after a snapshot from) and `07`
(Retail Win-back from two files: a query is a post-build step and the run gives it one working table, so a query over
the orders cannot be applied beside the combine step), and `invariant:same_plan_without_ai` on `06` (the unpivot and the
spend table are the AI's). The reason of each entry says why.
