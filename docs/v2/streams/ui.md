# Stream WF6: Marketer UI (`ui/v2/`)

The five Autonomy screens (Connect, Goal, Plan, Live, Proof) plus Operator mode, as plain ES modules.
There is no build step, no CDN and no npm package: serve the `ui/v2/` folder as static files and open
`index.html`. With `?mock=1`, or when `/v2` cannot be reached, every screen runs on the JSON in `ui/v2/mock/`.

## What was built

| File | What it does |
| --- | --- |
| `index.html` | The page: top bar (wordmark, goal switcher, alerts count, Pause/Resume), numbered nav, skip link, sample-data banner. |
| `app.js` | Hash router (`#/connect`, `#/goal`, `#/plan/{id}`, `#/live/{id}`, `#/proof/{id}`, `#/operator[/{id}]`). It loads each screen with `import()` and keeps the top bar in step with the current goal. |
| `api.js` | `createApi()`: one method per `/v2` endpoint, plus a mock layer that serves `mock/*.json` and keeps writes in memory for the page load. |
| `components.js` | Shared DOM and formatting helpers (`el`, `money`, `range`, `pct`, `when`, `pill`, `table`, `toast`, …). A missing value is always an em dash, and an estimate is always shown as a range. |
| `styles.css` | Shell and Connect/Goal/Plan styles, light and dark (`prefers-color-scheme` and `[data-theme]`), with a phone layout at 720px. |
| `screens/connect.js` | How to bring data in, plus dataset and sending-health status when v1's `/datasets` and `/v2/health` answer. |
| `screens/goal.js` | "What do you want to achieve?" box, budget and currency, the Analyst's opportunity cards, and the list of goals. |
| `screens/plan.js` | The brief in plain words, the content library with Approve/Reject, and the autonomy dial with "Approve plan". |
| `screens/live.js` | Briefing, activity feed with the reason for each decision ("left alone" decisions folded under a count), alerts inbox, health against the stop rules, Pause, and the customer side panel. It also holds the helpers that proof.js and operator.js use. |
| `screens/proof.js` | Money made against the holdout and lift against the simple rule, each with its range; two SVG charts; "Copy summary". |
| `screens/operator.js` | Raw JSON for goal, brief, content and proof; a decisions table with filters; customer lookup. |
| `screens/feed.css` | Styles for Live, Proof and Operator (`ap-` classes). The screens load it themselves. |
| `mock/*.json` | Sample data built from `autonomy/fakes/samples.py` through the contract models (`model_dump(mode="json")`). |

Tests are in `tests/v2/ui/`. The Python tests parse every mock file with its contract model and check
that the files agree with each other (the customer's state matches its events, bids are in order,
holdout decisions are always "nothing", and so on). They also run two node test files that draw every
screen in a small fake DOM and click through them. The node tests skip when node 20.10 or newer is not
on PATH.

## How other streams use it

- **WF5 (API)** must serve the endpoints that `api.js` calls, with bodies that are contract
  `model_dump(mode="json")`:
  `GET /v2/goals`, `POST /v2/goals {text, budget, currency, kind?, objective?}`,
  `GET /v2/goals/{id}` (`{goal, brief, content, proof}`), `POST /v2/goals/{id}/approve {autonomy}`,
  `POST /v2/goals/{id}/pause`, `POST /v2/goals/{id}/resume`, `GET /v2/goals/{id}/content`,
  `POST /v2/content/{id}/status {status}`, `GET /v2/decisions?goal_id=&customer_id=&limit=`,
  `GET /v2/customers/{id}` (`{state, decisions, events}`), `GET /v2/briefing?goal_id=`,
  `GET /v2/proof?goal_id=`, `GET /v2/health`, `GET /v2/alerts`, `GET /v2/opportunities`.
  Errors should be FastAPI's `{detail: ...}`. WF5 should also mount `ui/v2/` as static files.
  `window.AUTONOMY_API_BASE` sets the API prefix when the API is not on the same origin.
- **The API client:** `createApi({base?, mock?, fetch?, mockBase?, now?})` returns an object with
  `listGoals()`, `createGoal(text | body, budget?, currency?)`, `getGoal(id)`, `approveGoal(id, autonomy)`,
  `pauseGoal(id)`, `resumeGoal(id)`, `getContent(id)`, `setContentStatus(contentId, status)`,
  `listDecisions({goalId, customerId, limit})`, `getCustomer(id)`, `getBriefing(goalId)`, `getProof(goalId)`,
  `getHealth()`, `listAlerts()`, `listOpportunities()`, `getDataStatus()`, `isMock()`,
  `onModeChange(fn)` and `onGoalChange(fn)`. Failures throw `ApiError(status, code, message, body)`.
- **Screens:** every screen module exports `render(root, ctx)` with
  `ctx = {api, goalId, navigate, toast, goal?, refreshGoals?}`. To add a screen, add a row to `SCREENS` in `app.js`.
- **Mock data:** if a contract changes, regenerate or edit `ui/v2/mock/*.json`. The tests in
  `tests/v2/ui/test_*_mock.py` and `test_shell_goal_plan.py` show which file has drifted.

## Contract additions requested (none were made)

- `POST /v2/goals` should accept optional `kind` and `objective` (Goal enums), so that "Start this goal"
  on an Analyst opportunity keeps them. If WF5's request model forbids extra keys, starting an
  opportunity gets a 422 in live mode. Goals typed into the form never send these fields.
- `ProofSummary.roi`: the description should say it is a ratio (incremental profit ÷ spend, shown as
  "2.3×"), not a percentage.
- Optional: a `goal_id` on `Decision`, so a "nothing" decision with no bids can be tied to a goal.
  Today only `action.goal_id` or `bids[0].goal_id` carries it.

## Dependencies requested

None. The UI is vanilla JS and CSS. The tests need node (20.10 or newer) on the CI runner. ubuntu-latest has it.

## Known gaps

- Nothing has been checked in a real browser and there are no screenshots, because no browser was
  available locally. WF6's "done when" asks for screenshots, so WF9 or CI should take them. Phone
  width, dark mode and contrast were checked only by reading the CSS.
- There are two sets of helpers. `components.js` is used by the shell, Connect, Goal and Plan.
  `live.js` has its own `h`, `money`, `range`, `when` and labels, used by Live, Proof and Operator. They
  now share the em dash, `present` and `num`. Ranges read "a – b" on Plan and "P (range a to b)" on
  Live and Proof, and Live/Proof show cents below 1,000 while components shows them below 100. They
  could be merged later.
- In mock mode, `getCustomer` only knows `cust_1042`, so other customers show "No details yet". A goal
  created in mock mode reuses the sample brief's audience wording and has no proof, briefing or decisions.
- The alerts count never goes down, because `Alert` has no resolved flag.
- The Live customer panel is `aria-modal` but does not trap focus (Escape, the backdrop and restoring
  focus do work).
- Operator mode has no brief editing, replay, Gym or exploration controls yet, because the API has no
  endpoints for them.
- The Connect screen reads v1's `GET /datasets`, which may need sign-in. When that fails, it says the
  status is not available.
- The scripts that generated the mock JSON were not committed. The JSON files are the source of truth,
  and the tests guard their shape.
- mypy is not installed locally. The Python test files are fully annotated, and CI's Python 3.11
  `mypy --strict` run is the real check.
