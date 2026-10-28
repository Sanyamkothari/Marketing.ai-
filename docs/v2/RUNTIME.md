# The Autonomy runtime

The runtime is the loop that runs every goal day to day: events come in, customer state is folded,
each goal decides, decisions are logged, held for approval or sent, the agent learns overnight, the
Analyst and Strategist review each goal weekly, and circuit breakers can stop any of it. This page
says how it works and how to run it. The code is `autonomy/runtime/`.

| Module | What it holds |
|---|---|
| `service.py` | `AutonomyRuntime` and its `tick(now)`; `RuntimeConfig`, `RuntimeCursor`, `TickReport`; spend (`SpendLedger`, `EvenPacing`); the breakers (`standard_breakers`); the split check (`check_split`); `build_worker`. |
| `batches.py` | `BatchStore` (in memory and SQL): decisions held for a person at the APPROVE level. |
| `stores.py` | `AlertStore` and `BriefingStore` (in memory and SQL): every alert and briefing the runtime produces. |
| `settings.py` | `RuntimeSettings.from_env()` (`AUTONOMY_*` variables) and `build_runtime()`, the production wiring. |
| `worker.py` | `Worker` over the job queue (tick, send and webhook jobs, retries with backoff) and the command line. |
| `demo.py` | `run_demo()`: the real runtime on a simulated Gym shop. |

## One tick

A worker calls `runtime.tick(now)` every few minutes (`AUTONOMY_TICK_EVERY_MINUTES`, default 5).
Nothing in a tick reads the clock: `now` is passed in, and decision ids are derived from (goal,
customer, trigger, local day or event), so the same tick twice is a no-op. Each tick, in order:

1. **Fold.** Events appended since the last tick are folded into customer state. A watermark and
   the ids already folded near it let a late event (up to `late_window`, 48 hours) in without
   folding anything twice. The first tick of a process, and each nightly run, rebuild every state
   from the log instead. The spend ledger is fed from the same pass.
2. **Replies** go to the Conversation agent. STOP becomes an UNSUBSCRIBE before anything is
   decided; replies a person should read become alerts.
3. **Circuit breakers** run over the last week of events (below).
4. **Batches.** Batches nobody acted on within `batch_max_age` (72 hours) expire. Batches a person
   approved since the last tick are sent, if their goal is still active and not paused.
5. **Queued sends** that are due run here only when no worker owns them (see "Send jobs").
6. **Decide.** For each active goal with an approved brief: at once for a customer with a new REPLY
   or CART, at the daily sweep hour (`sweep_hour`, local time) for the goal's whole audience, and
   after the sweep for customers who just crossed a time threshold of the audience (a lapse). A
   customer is decided once per goal per day, and contacted by at most one goal per tick.
7. **Nightly**, once a day after `nightly_hour`: each goal's policy gets
   `nightly(decisions, events_by_customer, now)` with its own goal's decisions (the fast learning
   loop).
8. **Weekly**, once a week (`weekly_weekday`, `weekly_hour`), when an Analyst and a Strategist are
   configured: the Orchestrator's weekly cycle writes a briefing and may narrow the brief.

At the end of the tick every alert and briefing it produced is written to the alert and briefing
stores. `TickReport.summary()` is what `POST /v2/runtime/tick` returns.

## Modes (autonomy levels)

| Level | What a tick does with a goal's ACT decisions |
|---|---|
| SHADOW | Logs them. Nothing is sent and nothing waits for a person. |
| APPROVE | Logs them and holds them in one pending `Batch` per sweep. A person approves or rejects the whole batch (`POST /v2/batches/{id}/approve` or `/reject`); the next tick sends an approved batch. |
| AUTOPILOT | Logs them and hands them to the dispatcher, which re-checks every rule (consent, quiet hours, caps, envelope) and sends now or queues a send job for the decision's `send_at`. |

Holdout customers are decided for (their "do nothing" is logged for the proof) but never contacted.

## Batches

A batch moves `PENDING -> APPROVED -> SENT`, or `PENDING -> REJECTED`. An approved batch that could
not be sent in time (its goal was paused) expires to REJECTED with `decided_by="runtime:expired"`.
Every move is a compare-and-set, so two people clicking at once cannot both win and a batch is never
sent twice. A batch is held, not sent, while its goal or one of its channels is paused.

## Circuit breakers

`standard_breakers()` runs `autonomy.channels.breakers` per goal with that goal's own stop rules:
complaint rate, unsubscribe multiple (needs a baseline), delivery rate (pauses the channel),
WhatsApp quality and **spend pace**. A tripped rule pauses its goal (status PAUSED in the workspace,
until a person resumes it) or its channel, and raises an alert.

Spend pace is spend so far divided by planned spend so far:

- **Spend** comes from the `SpendLedger`, which reads the event log. A SEND costs its channel's
  price (`DEFAULT_CHANNEL_COSTS`, or `channel_costs=` on `build_runtime`). An **offer costs money
  when it is used**: a PURCHASE whose `attrs["offer_id"]` names the offer of a decision sent to that
  customer within the goal's outcome window (the brief's `horizon_days`) pays that offer once, at
  the purchase. The price is the purchase's `attrs["offer_cost"]` if the shop reported one, else the
  offer's `cost_per_redemption` in the brief. The latest qualifying send pays (last touch), and each
  decision pays its offer at most once. The same figure sets each goal's `budget_left` for
  `decide()`: the smaller of the goal's and the brief's budget, minus spend.
- **Planned spend** is `EvenPacing`: the budget spread evenly from the goal's `starts_at` to its
  `ends_at`, or over `plan_days` (`AUTONOMY_PLAN_DAYS`, default 30) when it has no end date.

Breakers built with `standard_breakers()` and no spend sources of their own get the runtime's ledger
and pacing automatically. A caller that passes `spend_fn` (the demo does) replaces the ledger.

## One proof split per business

Every customer is assigned to agent, baseline or holdout **once for the whole business**
(`autonomy.proof.split`, a stable hash salted with `AUTONOMY_SPLIT_SALT`), with
`AUTONOMY_HOLDOUT_SHARE` and `AUTONOMY_BASELINE_SHARE` (10% and 10% by default). One split, not one
per goal, so that proof for one goal compares with proof for another and a customer is never holdout
for one goal while another goal treats them.

A brief's `Envelope.holdout_share` and `baseline_share` must therefore equal the business's split.
`build_runtime` copies the shares into `RuntimeConfig`; a goal whose brief asks for others does not
run, and a critical `brief.split_mismatch` alert (once a day) says what to change. The API can call
`runtime.check_brief(brief)` before approving one: it raises `SplitMismatchError` with the same
message. A runtime built by hand without `holdout_share`/`baseline_share` in its config does not
check (the demo and tests).

## Policies and learning

Each goal gets its own policy from `policy_factory([brief], content)` (in production the agent,
`autonomy.runtime.agent.build_agent_policy`). The policy is rebuilt only when the brief changes
**materially** (`MATERIAL_BRIEF_FIELDS`: audience, envelope, horizon, objective, follow-ups) or its
approved content changes. A weekly brief that only restates the plan (a new version, id, rationale
or approval) keeps the policy and everything it has learned.

The agent's learning lives in memory. So that it survives a restart or a rebuild, a newly built
policy **replays** `replay_lookback` (30 days) of its goal's decisions and their events through
`nightly()` before it decides, in the same tick. `nightly()` keeps decisions and events by id, so
the next nightly run does not count them twice. `TickReport.replayed_goals` lists the goals replayed.

## Send jobs: one owner

A decision whose `send_at` is in the future becomes a `send_message` job. **The worker owns send
jobs**: its `SendHandler` (`worker.send_handler`) is the only code that runs them, with the worker's
claim, retries (1, 2, 4 ... minutes, at most an hour, 5 attempts) and re-check of every rule.

- In production, `build_runtime` sets `run_send_jobs=False`: the tick never claims a send job; the
  worker process does.
- A runtime that runs with no worker at all (tests, the API's operator tick over the simulated demo)
  keeps the default `run_send_jobs=None`: its tick hosts the same handler through a `Worker` marked
  `hosted`. As soon as a real `Worker` with a send handler for the runtime's dispatcher exists in the
  process (the demo builds one), the tick leaves sends to it (`worker_owns_sends`).
- `run_send_jobs=True` forces the tick to run them.

## Persistence

| What | Where | After a restart |
|---|---|---|
| Events, states, decisions, goals, briefs, content, jobs | `autonomy.store` SQL tables (`create_all`) | Kept. State is rebuilt from events on the first tick. |
| Batches | `batch` table (`create_batch_tables`) | Kept. |
| Alerts, briefings | `runtime_alert`, `runtime_briefing` (`create_record_tables`) | Kept; the API reads them. Ids are stable, so a repeated tick writes nothing new. |
| Spend | Derived from the event log (`SpendLedger`) | Rebuilt with state. |
| Learning | In the policy's memory | Replayed from the decision log and events. |
| `RuntimeCursor` (watermark, last sweep per goal, last nightly and weekly) | In memory; small and JSON-able | Lost unless the caller saves it: the first tick rebuilds state, and a nightly or weekly run may repeat. Decision ids keep repeated decisions no-ops. |
| Breaker channel pauses, reply-alert dedupe | In memory | Cleared; a breaker trips again if the harm is still in the data. |

`build_runtime` creates every table it needs; it is safe to call twice.

## Running the worker

```bash
# Production: settings from AUTONOMY_* variables, Postgres in production.
export AUTONOMY_DATABASE_URL=postgresql+psycopg://user:<password>@host/autonomy
export AUTONOMY_TIMEZONE=Europe/London        # the business's time zone
python -m autonomy.runtime.worker              # loop forever on the wall clock
python -m autonomy.runtime.worker --once       # drain what is due now, then exit

# The simulated shop, in memory, ticking hourly in real time.
python -m autonomy.runtime.worker --demo
```

`build_worker()` builds the runtime from the settings, registers the tick, send and webhook handlers
and seeds a repeating tick job. Tick jobs sit on one grid of `tick_every_minutes` counted from the
Unix epoch (`tick_slot`), and a tick job's id is its time, so a restart either queues the same job as
the running chain or one that merges with it on the next tick: never a second chain. A tick that keeps
failing does not end the schedule (its successor is queued first). If the settings are wrong the
command prints `cannot start the worker: ...` and exits with status 2.

The main settings (`AUTONOMY_<NAME>`): `DATABASE_URL`, `TIMEZONE`, `SWEEP_HOUR` (10), `NIGHTLY_HOUR`
(2), `WEEKLY_WEEKDAY` (0, Monday), `WEEKLY_HOUR` (7), `TICK_EVERY_MINUTES` (5), `SEED`, `SPLIT_SALT`,
`HOLDOUT_SHARE` and `BASELINE_SHARE` (0.10 each), `PLAN_DAYS` (30), `LATE_WINDOW_HOURS` (48),
`TRIGGER_WINDOW_HOURS` (24), `BATCH_MAX_AGE_HOURS` (72), plus each channel's settings (SES,
WhatsApp, the client's webhook or CSV export). Secrets are never in settings: `*_REF` names the
environment variable that holds each one.

The API (`python -m api.v2.app`) shares the same database: a goal or batch approved there is what
the next worker tick reads, and the alerts and briefings a worker tick writes are what `/v2/alerts`
and `/v2/briefing` serve.

## Known gaps

- The `RuntimeCursor` is not saved, so a restart repeats that day's nightly run (harmless: learning
  is idempotent) and may repeat the weekly cycle if the restart lands in the same week.
- The replay hands the policy the whole lookback at once, at `now`. The agent labels each outcome
  with the customer's segment today, not at decision time (it kept that in memory before).
- A late SEND that arrives after the purchase that used its offer is matched when it arrives; a
  purchase whose send never arrives is never charged.
- The demo and the API's simulated demo pass their own `spend_fn` (channel costs only); drop it to
  use the ledger with offer costs.
