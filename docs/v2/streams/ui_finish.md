# Stream: UI finish

Branch `v2/ui-finish`. Owns `ui/v2/`, `api/v2/deps.py`, `api/v2/routes.py`,
`autonomy/agents/analyst.py`, `docs/v2/DESIGN.md`, `docs/v2/PROMISE_MAP.md` and
`docs/v2/screenshots/`. Its own tests are in `tests/v2/ui_finish/`; it also updated promise tests in
`tests/v2/promise/` and the Analyst's wording tests in `tests/v2/agents/test_analyst_conversation.py`.

This stream makes the numbers agree across screens and cuts the words on the busiest ones. The
Briefing on Live now shows the same profit as Proof, because the API rebuilds the briefing's measured
sections from the proof it serves now instead of returning an old snapshot.

## What was built

| Module | What changed |
|---|---|
| `autonomy/agents/analyst.py` | Plain section titles ("Extra profit vs people we never contact", "Against the simple rule", "Customer health", "What it learned") as constants. The template headline leads with profit and its likely range; "holdout" is gone from the words. New code-only `build_sections()` and `template_briefing()`, and an optional `learned=` that adds "What it learned" only when every number in a line is backed. Learned money uses the goal's currency. |
| `api/v2/deps.py` | `Services.current_briefing(goal_id)`: the stored briefing with its three measured sections rebuilt from that goal's own proof and the current health; `as_of` becomes the measurement time; other sections and ids are kept. Non-Analyst briefings, or ones with no proof, are served as stored. |
| `api/v2/routes.py` | `GET /v2/briefing` serves `current_briefing`. |
| `ui/v2/screens/live.js`, `live.css` | Briefing card shows one figure per line under its label, with the range; the sentences move to the label's hover description. |
| `ui/v2/screens/plan.js`, `plan.css` | Messages that passed every editor check sit under one "Checked by the editor" line; messages needing a look come first and never fold; "Don't use this one" is a quiet icon; pause rules grouped by what they pause. 593 to 447 words. |
| `ui/v2/screens/proof.js`, `proof.css` | Clearer forecast sentence; the too-early state shows a strip with the three group sizes, when the first contacts count, and spend. |
| `ui/v2/screens/operator.js` | Under 640px it draws 50 rows with "Show all N"; filters still search everything fetched. |
| `ui/v2/screens/onboarding.js`, `goal.js` | Getting started (200 words) and Goal (160 words) trimmed; hints moved into (i) descriptions. |
| `docs/v2/DESIGN.md`, `PROMISE_MAP.md`, `screenshots/` | Word budget table with a Polish column, updated promise rows, all 22 screenshots retaken. |

## How other streams use it

```python
from autonomy.agents.analyst import (
    Analyst, LearnedNote, Learned, build_sections, template_briefing,
    MONEY_TITLE, BASELINE_TITLE, HEALTH_TITLE, LEARNED_TITLE,
)
LearnedNote(text: str, numbers: Mapping[str, float] = {})
Learned = Sequence[str | LearnedNote]
build_sections(goal, proof, health, facts, *, learned=()) -> list[SectionDraft]
template_briefing(goal, proof, health, facts, period, now, *, learned=(), briefing_id=None) -> Briefing
Analyst(llm, *, model_id=None)
  .briefing(goal, proof, health, facts, period, now, *, learned=()) -> Briefing
  .write_briefing(...same..., *, learned=()) -> BriefingResult

from api.v2.deps import Services
Services.current_briefing(goal_id: str | None) -> Briefing | None   # what GET /v2/briefing serves
```

The runtime (`autonomy/runtime/demo.py`) can pass `learned=` to `Analyst.briefing` to fill Live's
"What it learned" card; nothing else needs to change.

## Contract additions requested

None. One possible later addition: Proof's summary could report its own horizon, so Proof's
"First from" date need not use the latest brief's horizon (they match on the demo).

## Dependencies requested

None.

## Known gaps

- `autonomy/runtime/demo.py` still stores briefings written from the programme-wide proof and never
  passes `learned=`, so on the demo the "What it learned" card stays empty. The API corrects the
  numbers when serving.
- `autonomy/agents/prompts/analyst.md` still explains "Holdout" to the model; only the per-briefing
  prompt asks for "people we never contact". Live hides the word on screen.
- A rebuilt briefing's `as_of` is the measurement time, so Live does not show when the weekly run
  happened.
- Health is rebuilt from current health, so a model-worded briefing usually falls back to template
  wording after a day. Numbers win over model words by design.
- `?mock=1`'s `briefing.json` is not linked to `proof.json`, so mock figures can differ.
- Live's "next check was due ... has not run yet" line appears in the retaken shots, and Goal's
  opportunity headline repeats its count; both come from the demo data.
- Plan is 3 words under its target; the too-early Proof layout is not among the committed shots;
  screenshots were not retaken after the review fixes (they do not change the demo's look).
- `mypy --strict` was not run locally (the local mypy has no pydantic or fastapi); CI must confirm.
