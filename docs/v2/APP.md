# The marketer app (ui/v2)

The Autonomy app is what a marketer uses: say what you want, check the plan, choose how much the agent may do
alone, watch it work, and see the proof. It is plain ES modules and CSS in `ui/v2/`, with no build step and no
npm. It talks to the `/v2` API (`api/v2/`). With `?mock=1`, or when `/v2` cannot be reached, it uses the sample
data in `ui/v2/mock/`.

## Run it locally

One command, from the repository root:

```bash
python3 -m api.v2.app            # the demo API and the app on http://127.0.0.1:8010
```

Open **http://127.0.0.1:8010/v2/app/**. The demo API builds a simulated shop in memory (one running win-back
goal, two Analyst opportunities, a week of decisions, events, a briefing and proof). It starts fresh on every
run, and nothing is sent anywhere. `--port 8023` picks another port. It needs FastAPI and uvicorn, which are part
of the light dependency set.

To see the app on the sample data only (no API), add `?mock=1`:
**http://127.0.0.1:8010/v2/app/?mock=1**. Any static file server works too, for example
`python3 -m http.server -d ui/v2 8000` and then **http://localhost:8000/?mock=1**. If the API is on another
origin, set `window.AUTONOMY_API_BASE` (for example `"http://localhost:8010"`) before `app.js` loads.

## What each screen does

The top bar is on every screen. It has the goal switcher, **To approve** (how many batches are waiting, shown
only when the current goal is at "Approve each batch"), **Inbox** (alerts for this goal and for the whole
account that need a look) and **Pause / Resume**. Pause stops every send for the current goal at once, and it is
the app's only pause control.

| Screen | Address | What the marketer does there |
| --- | --- | --- |
| 1 Connect | `#/connect` | Reads how data comes in (the v1 Data Doctor for now), what is connected, and sending health. |
| 2 Goal | `#/goal` | Types a goal in their own words with a budget and currency, or starts one of the Analyst's opportunities. Starting an opportunity sends its `kind` and `objective` with the text, so the plan keeps what the Analyst found. The list of goals is below. |
| 3 Plan | `#/plan/{goal_id}` | Reads the Strategist's plan in plain words: who, offers, channels, caps, quiet hours, budget, stop rules and follow-ups. They approve or reject each message, set the autonomy dial (Shadow, Approve each batch, Autopilot) and approve the plan. Nothing is sent before that. |
| 4 Live | `#/live/{goal_id}` | At "Approve each batch", the batches waiting for approval come first: each shows its summary, number of contacts, expected net profit, expected spend and when it was prepared, with **Approve** and **Reject** buttons. Below that are the day's briefing, the activity feed (each decision with its reason; the "left alone" ones fold under a count), the inbox and customer health against the plan's limits. Clicking a customer opens their state and a timeline of events and decisions, each with its "why". The header shows the goal's status and follows the top bar's Pause straight away. |
| 5 Proof | `#/proof/{goal_id}` | Shows extra profit and revenue against the never-contacted holdout, and lift against the simple rule, each with its 95% range (never a bare number). It also shows group sizes, spend and return on spend, with a "Copy summary" paragraph to paste into an email. |
| Operator | `#/operator/{goal_id}` | For us, running the agent for early clients. It shows the raw JSON of the goal, brief, content, proof and batches, a filterable decisions table and a customer lookup. |

A goal id that does not exist (a stale link) shows "not found" on Plan, Live and Proof, not an error.

## Live mode and mock mode

On load, `api.js` asks `GET /v2/health` once. If that answers with JSON, every screen uses the real API, and an
API error stays an error (shown in words, never replaced by sample data). If the API cannot be reached, or the
server has no `/v2`, the app switches to the sample data and a yellow banner says why.

In mock mode, writes change an in-memory copy for this page load: create a goal, approve a plan, pause, approve or
reject a message, approve or reject a batch. A reload starts again from the files. The mock never makes up a
result. A goal created there has no proof, briefing, decisions or batches, and approving a batch leaves it
"approved" (never "sent"). Any customer in the sample decisions can be opened: `cust_1042` has a full sample
record, and the others are built from their own decisions (group, the channels the agent picked, the decisions
with their reasons). Purchases, revenue and events are not in a decision, so they show as an em dash.

| Sample file | Answers |
| --- | --- |
| `goals.json`, `goal_detail.json` | `GET /v2/goals`, `GET /v2/goals/{id}` (the sample goal `goal_winback` is at "Approve each batch") |
| `opportunities.json` | `GET /v2/opportunities` |
| `decisions.json` | `GET /v2/decisions` |
| `customer.json` | `GET /v2/customers/{id}` for `cust_1042` (others come from `decisions.json`) |
| `briefing.json`, `proof.json`, `health.json`, `alerts.json` | the endpoints of the same names |
| `batches.json` | `GET /v2/batches?goal_id=`: two pending batches and one sent, each made of act decisions from `decisions.json` |

## Approval batches

At "Approve each batch", the agent holds each sweep's messages in a `Batch` (contract `autonomy.contracts.Batch`)
until a person decides. The app calls:

- `api.listBatches(goalId)`, which calls `GET /v2/batches?goal_id=` and returns `[Batch]`. It returns `null` when
  the server has no batch route yet, meaning FastAPI's own `404 {"detail": "Not Found"}` or a 405. Live then
  says the server does not list batches yet, and the top bar shows no count.
- `api.approveBatch(id)` and `api.rejectBatch(id)`, which call `POST /v2/batches/{id}/approve` or `/reject` with
  a `{}` body and return the updated `Batch`. A 404 that names the batch, or a 409 for a batch already decided,
  is shown as the server's own words.
- `api.onBatchChange(fn)`, which runs after either decision so the top-bar count stays right.

## Pausing

The top bar's Pause button is the one control, so there are no longer two buttons doing the same thing with
different words. The shell passes `ctx.pauseInTopBar = true` to screens. Live then shows only the goal's state
("Paused: … until you press Resume at the top") and follows every change through `api.onGoalChange`, without
reloading its feed or closing an open customer panel. If Live is drawn on its own without the shell (as in its
node tests), it keeps its own "Pause everything" button so a one-click stop is always there.

## Tests

- `tests/v2/ui/`: the screens drawn in a tiny fake DOM against the sample files, and the sample files checked
  against the contracts.
- `tests/v2/app/test_live_wiring.py`: `batches.json` is exactly the `Batch` contract and matches the decisions it
  holds (contacts, expected net, expected spend, groups, times). It also runs `live_wiring.test.mjs`, which covers the batch
  flow on Live, the API client against fake `/v2` answers (422 field names, a missing batch route, 409s), the
  mock customer drill-down, the single pause control, and the not-found states.
- `tests/v2/app/test_screenshots.py`: every screen has a desktop and a phone shot in `docs/v2/screenshots/`
  (Live and Proof in dark mode too), each at most two viewports tall, and all of them under 3 MB together. To
  take them again, see `docs/v2/screenshots/README.md`.

```bash
python3 -m pytest tests/v2/ui tests/v2/app -q
```

The node halves need node 20.10 or newer. Without it they skip, unless `REQUIRE_JSDOM=1` is set (CI sets it).

## Checked against the running API

Each route was opened in headless Chromium against `python3 -m api.v2.app`, both with `--dump-dom` and by
clicking through the Chrome DevTools Protocol, with no console errors. The click-through was: start an Analyst
opportunity (the POST carried `kind` and `objective`), approve its plan at "Approve each batch", open Live (which
says batches are not listed yet), open a customer from the feed, and pause and resume from the top bar. In mock
mode it also approved and rejected batches, with the top-bar count going 2, 1, then hidden, and opened a customer
with no sample record.
