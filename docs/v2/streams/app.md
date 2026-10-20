# Stream app: the marketer app on the real API

This stream took the WF6 screens in `ui/v2/` and made them work against the running `/v2` API instead of
only the sample files. It added the approval-batch experience, a single Pause control, and screenshots of
every screen. The full guide to running and using the app is `docs/v2/APP.md`.

## What was built

| File | What it does |
| --- | --- |
| `ui/v2/api.js` | Adds `listBatches`, `approveBatch`, `rejectBatch`, `onBatchChange` and `routeMissing`. 422 errors now name the field ("Budget: Input should be ..."). Mock `getCustomer` builds a bundle for any customer in `decisions.json`. |
| `ui/v2/app.js` | Adds a "To approve n" link in the top bar next to Inbox. Passes `ctx.pauseInTopBar`, so the top bar's Pause is the one control. The inbox count now counts only this goal's alerts plus account-wide ones. |
| `ui/v2/screens/live.js` | A "Waiting for your approval (n)" card at the APPROVE level, with Approve and Reject on each batch and decided batches folded away. Live follows the top bar's pause without re-rendering. Shows "not found" for an unknown goal. |
| `ui/v2/screens/plan.js`, `proof.js` | Show "not found" for an unknown goal. Proof shows each estimate and its range at one precision (`estimateFormat`). |
| `ui/v2/screens/operator.js` | Shows the goal's batches as raw JSON. |
| `ui/v2/styles.css`, `screens/feed.css` | Layout fixes found in the screenshots: a one-row phone top bar, double padding removed, Proof chart text kept readable, the Operator table fitted to its card. |
| `ui/v2/mock/batches.json` | Two pending batches and one sent, built through the `Batch` contract from the sample act decisions. |
| `docs/v2/APP.md` | How to run the app, each screen, mock mode, batches, pausing and the tests. |
| `docs/v2/screenshots/` | 16 PNGs (6 screens, desktop and phone, Live and Proof in dark mode too), `capture.mjs` to take them again, and a README. |
| `tests/v2/app/` | `test_live_wiring.py` (+ `live_wiring.test.mjs`, 11 node tests) and `test_screenshots.py`. |

## How other streams use it

- **API (WF5)** needs to serve the batch routes the UI already calls, with bodies that are the `Batch`
  contract's `model_dump(mode="json")`:
  - `GET /v2/batches?goal_id=` returns `list[Batch]`.
  - `POST /v2/batches/{batch_id}/approve` and `POST /v2/batches/{batch_id}/reject` take an empty `{}` body and
    return the updated `Batch`. An unknown batch should be a 404 with a named detail; a batch already decided
    should be a 409.
- **The API client** (`ui/v2/api.js`), added to what `createApi()` already returns:
  - `listBatches(goalId) -> Promise<Batch[] | null>`: `null` means the server has no batch route yet (a bare
    FastAPI 404 "Not Found" or a 405).
  - `approveBatch(batchId) -> Promise<Batch>` and `rejectBatch(batchId) -> Promise<Batch>`.
  - `onBatchChange(fn) -> unsubscribe`: called after any approve or reject.
  - `routeMissing(err: ApiError) -> boolean` (exported).
- **Screens** get `ctx.pauseInTopBar: boolean`. When it is true, a screen shows the goal's state and follows
  `api.onGoalChange` rather than drawing its own Pause button.
- **Live helpers** (exported from `screens/live.js`): `splitBatches(batches) -> {pending, decided}` and
  `batchFacts(batch, currency) -> string` (one line joined with " · ").

## Contract additions requested (none were made)

- The batch endpoints above are not in `api/v2` yet. If WF5's approve or reject request model needs a body
  (for example `decided_by`), the UI's `{}` body has to change to match.
- Optional: `goal_id` on `Decision`. "Left alone" decisions with no bids cannot be tied to a goal, so the mock
  feed's "Left alone" count for the sample goal is lower than the file holds (also noted by WF6).

## Dependencies requested

None. The node tests use node 22's built-in test runner and fake DOM, and `capture.mjs` drives headless Chrome
over the DevTools protocol with node's built-in WebSocket, so no npm packages are needed.

## Known gaps

- The batch flow was checked only against fake `/v2` answers and the mock, because the batch routes have not
  merged. Once they land, click Approve and Reject once against the live API.
- 14 of the 16 screenshots were taken while the batch UI was still being written. Retake them with
  `node docs/v2/screenshots/capture.mjs` once the batch routes land, then quantize them as the README says.
- The top-bar Inbox count leaves out "info" alerts, but Live's Inbox heading counts them and also merges the
  briefing's alerts, so the two numbers can differ.
- On Proof, a value label can cross the dashed zero line when a range spans zero. On a phone, the current
  "Operator mode" tab is not scrolled into view.
- `estimateFormat` still mixes precision for small estimates when one end is a whole number (for example "$0"
  next to "$0.38").
- `screens/live.js` has its own `money`, `range` and `when`, next to the ones in `components.js`. This came
  from WF6; the signatures differ, so merging them is left for a later clean-up.
- A bare `HTTPException(404)` from a future batch route would read as "route missing", so Live would say
  "not listed yet". The batch route should give a named detail.
- `black --check docs/v2/APP.md` fails because black reads any file it is given as Python; the Python files
  pass. mypy --strict was not run locally (not installed); CI's Python 3.11 run is the real check.
