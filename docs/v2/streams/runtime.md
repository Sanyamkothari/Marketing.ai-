# Stream: Runtime

Branch `v2/runtime`. Owns `autonomy/runtime/service.py`, `worker.py`, `settings.py`, `demo.py`,
`batches.py` and the runtime additions to `api/v2/`. Tests are in `tests/v2/runtime/` and
`tests/v2/api/test_api_runtime.py`.

This stream joins the other streams into one running system: events come in, state is folded,
goals decide, batches wait for approval, the dispatcher sends, the nightly learner and the weekly
Analyst/Strategist cycle run, and the circuit breakers can stop all of it.

## What was built

| Module | What it does |
|---|---|
| `autonomy/runtime/service.py` | `AutonomyRuntime`: the whole loop, one idempotent `tick(now)` at a time (fold, replies, breakers, approved batches, queued sends, decide, nightly, weekly). Also `RuntimeConfig`, `RuntimeCursor`, `TickReport`, `standard_breakers`, `make_batch` and `build_worker`. |
| `autonomy/runtime/batches.py` | `BatchStore` (in memory and SQL) for decisions held at the APPROVE level, with compare-and-set moves: PENDING to APPROVED or REJECTED, APPROVED to SENT or REJECTED (expiry). |
| `autonomy/runtime/settings.py` | `RuntimeSettings.from_env()` (`AUTONOMY_*` variables, secrets kept as references) and `build_runtime()`, which wires SQL stores, adapters, dispatcher, breakers, the group split and the agent policy. |
| `autonomy/runtime/worker.py` | `Worker` over the job queue: tick, send and webhook handlers, retries with backoff, a tick schedule that does not drift, and `python -m autonomy.runtime.worker [--demo] [--once]`. The only place that sleeps or reads the clock. |
| `autonomy/runtime/demo.py` | `run_demo()`: the real runtime, agents and dispatcher on a Gym shop, with two goals (one AUTOPILOT, one APPROVE), daily briefings, proof and health. Deterministic by seed. |
| `api/v2/` | Batch endpoints (`GET /v2/batches`, `POST /v2/batches/{id}/approve`, `/reject`), the operator `POST /v2/runtime/tick`, `build_services_from_runtime()`, and a demo served from the simulation (`python -m api.v2.app`; `--fake` for the old fast demo). |

## How other streams use it

```python
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, RuntimeCursor, TickReport, standard_breakers

AutonomyRuntime(*, events, states, decisions, workspace, folder,
                policy_factory: Callable[[Sequence[Brief], Sequence[ContentItem]], Policy],
                queue=None, dispatcher=None, batches=None, breakers_fn=None, conversation=None,
                text_signals=None, analyst=None, strategist=None,
                spend_fn: Callable[[str, datetime], float] | None = None,
                assign_group=None, config: RuntimeConfig | None = None, cursor: RuntimeCursor | None = None)
  .tick(now) -> TickReport                     # safe to repeat; decision ids are stable
  .list_batches(...) / .approve_batch(id, *, by, now) / .reject_batch(id, *, by, now) -> Batch
  .resume_channels(channels)
TickReport.summary() -> dict                   # what POST /v2/runtime/tick returns

# A policy may add nightly(decisions, events_by_customer, now); the runtime calls it once a day
# with that goal's decisions only. Each goal gets its own policy from policy_factory([brief], content).

from autonomy.runtime.batches import BatchStore, InMemoryBatchStore, SqlBatchStore, create_batch_tables
from autonomy.runtime.batches import approve, reject, mark_sent, UnknownBatchError, BatchTransitionError

from autonomy.runtime.settings import RuntimeSettings, build_runtime, workspace_policy_factory
build_runtime(settings=None, *, environ=None, engine=None, policy_factory=None, adapters=None,
              address_book=None, profiles=None, text_signals=None, analyst=None, strategist=None,
              spend_fn=None, breakers_fn=None, baselines=None) -> AutonomyRuntime

from autonomy.runtime.worker import Worker, tick_job, tick_handler, send_handler, webhook_handler, webhook_job
Worker(queue, handlers, name, max_attempts=5).run_once(now) / .drain(now)

from autonomy.runtime.demo import run_demo, live_demo_worker
run_demo(scenario="baseline_shop", days=30, customers=1500, seed=0, *, start, stores=None) -> DemoResult

from api.v2.deps import build_services_from_runtime, build_demo_services
build_demo_services(now, *, simulation=True)   # API backed by a real runtime over a simulated shop
```

The agent stream's `autonomy/runtime/agent.py` (`build_agent_policy`) is loaded by name with
importlib, so this branch runs with or without it. Production calls it with `kinds=` read from the
workspace; the demo also passes `costs=` and `baseline_policy=`.

## Contract additions requested

- `BatchStatus.EXPIRED`. Expired batches are now REJECTED with `decided_by="runtime:expired"`.
- `Decision.goal_id` (or another way to tie NOTHING decisions to a goal). Worked around with
  derived decision ids.
- An insertion-order cursor on `EventStore` (for example `scan_after(seq)`), so the fold need not
  rescan the 2-day late window every tick or rebuild everything on restart.
- A customer contact contract (email, phone, WhatsApp id). Until then the default address book only
  addresses the WEBHOOK channel.
- A spend figure per goal on events (channel and offer cost on SEND). Until then budgets and the
  spend-pace breaker need an injected `spend_fn`.
- An `AlertStore` / `BriefingStore`, so alerts and briefings from worker ticks reach `/v2/alerts`
  and `/v2/briefing`. Today a runtime-backed API keeps them in process memory.
- Optional: `JobQueue.next_due(kinds)` / a way to list jobs, so a restarted worker can see its
  existing tick chain.

## Dependencies requested

None. Only pydantic, sqlalchemy, fastapi/starlette and the stdlib are used; httpx and boto3 are
imported only when a channel that needs them is configured.

## Known gaps

- `autonomy/runtime/__init__.py` is not on this branch. The agent stream adds it (exporting
  `build_agent_policy`); until the merge the package works as a namespace package, and mypy over
  `autonomy` passes.
- Without `spend_fn`, budget left is the full budget and spend pace never trips. Spend counts
  channel cost only, not offer redemptions.
- A restart rebuilds every state in memory on its first tick; events later than `late_window`
  are picked up only by the nightly rebuild (which the demo turns off). Fine for demo and
  online-retail scale, not for millions of customers.
- Some guards are in memory: alerts for 'other'/'question' replies can repeat after a restart;
  nightly/weekly runs repeat unless the `RuntimeCursor` is saved; breaker channel pauses clear on
  restart until the breaker trips again.
- `build_worker` seeds its first tick from the wall clock; a restart in a later interval adds a
  second (harmless, idempotent) tick chain.
- SES has no SNS verifier wired (its webhooks are refused) and WhatsApp has no customer lookup.
- Contact caps are checked when deciding, not again when an approved batch is sent later.
- Groups use the settings' holdout and baseline shares, not each brief's envelope.
- A goal's policy is rebuilt when the weekly cycle writes a new brief version, losing in-memory
  learning until the next nightly run relearns the lookback.
- The default `run_demo()` (1,500 customers, 30 days) takes about 45 s, nearly all in `decide()`
  and send-time choice; tests use 300 customers for 10 days. A 10-day run is shorter than the
  14-day outcome window, so its proof makes no claim.
- The API's simulated demo throws the Gym world away after seeding, so messages sent by later API
  ticks get no replies or purchases (needs `run_demo` to return the `DemoSystem`).
- Not run locally: the Postgres path of `SqlBatchStore`, the real `build_agent_policy`
  integration, and mypy on Python 3.11 (local mypy ran with `--python-version 3.13`: clean).
