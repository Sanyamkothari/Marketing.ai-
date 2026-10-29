# Data Doctor scenarios

Each folder is one scenario that `scripts/doctor_scenarios.py` plays against the real app, in process.

| File | What it is |
|---|---|
| `person.json` | The person's actions in order: `upload`, `consent`, `visit`, `goal`, `answer`, `decide`, `chat`, `approve`. A step may carry a `name`. A `chat` step carries an `instruction_id` and what it `expects` (`answer`, `change` or `refusal`). |
| `expect.json` | `after`: what must be true once a named step is done (`"step#label"` is a second group for the same step). `final`: the last step. `xfail`: the checks that fail today, each with the stage of the AI-first build that fixes it. |
| `replies.jsonl` | The AI's replies, recorded by an AI player (one JSON line per model call). Not recorded yet: the player records them after Stage 2. |
| `baseline_replies.jsonl` | Hand-written prose that stands in for the AI until then, with no tool use. It is the baseline of today's behaviour and nothing else. It is used only while there is no `replies.jsonl`. |

`xfail` keys are checkpoint names, or `invariant:<name>` for the seven invariants of the brief (Stage 0, item 5; the first
is two checks, `accepted_not_by_ai`, which holds today, and `accepted_needs_person`, which holds from Stage 2). What must hold
whatever the AI does (`approvals`, `none_accepted_by_ai`) goes in its own `"step#safety"` group, never in a group that is
marked `xfail` (the "#safety" group does not fail for a step that did nothing). `approvals` is an exact count. The
test marks each one `xfail(strict=True)`: the day a check passes, the suite fails until its entry is removed.

Run one scenario with `python -m scripts.doctor_scenarios --scenario 01` (add `--no-ai` for the default author). When the
recorded replies run out, the run stops and writes the next prompt to `next_prompt.txt`; the player appends one JSON reply
line to `replies.jsonl` and runs it again.
