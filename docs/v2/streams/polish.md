# Stream: Polish

Branch `v2/polish`. Owns `autonomy/runtime/service.py`, `demo.py`, `worker.py`, `settings.py`,
`batches.py`, `stores.py`, `api/v2/`, `docs/v2/screenshots/` and `docs/v2/RUNTIME.md`. Tests are in
`tests/v2/polish/`.

This stream closes the runtime's known gaps (spend that counts offers, stored alerts and briefings,
relearning on restart, one proof split per business, one owner for send jobs) and makes the demo
and its screens believable. `docs/v2/RUNTIME.md` is the operator's guide to all of it.

## What was built

| Module | What changed |
|---|---|
| `autonomy/runtime/service.py` | `SpendLedger` (channel cost per SEND plus offer cost per redemption, from the log) is the default `spend_fn`; `EvenPacing` is the default planned spend; `StandardBreakers` fills either in. Policies are rebuilt only when `material_brief()` changes, and a new policy replays 30 days through `nightly()` before it decides. Briefs asking for another split are refused (`check_split`, `SplitMismatchError`, a `brief.split_mismatch` alert). Send jobs run through the worker's `SendHandler`. `tick_slot` puts tick jobs on an epoch grid. |
| `autonomy/runtime/stores.py` | New. `AlertStore` and `BriefingStore`, in memory and SQL (`runtime_alert`, `runtime_briefing`): idempotent by id, newest first, filtered and limited in SQL. |
| `autonomy/runtime/settings.py` | `build_runtime` wires the SQL alert and briefing stores, the business's holdout/baseline shares and `channel_costs`, and leaves send jobs to the worker. |
| `autonomy/runtime/worker.py` | `SendHandler` / `send_handler` (the one send code path), `worker_owns_sends`; the `--once` CLI is tested without `--demo`. |
| `autonomy/runtime/demo.py` | Realistic costs (`DEMO_CHANNEL_COST`: $0.001 email, $0.04 WhatsApp); `run_demo(approve_goal_days=, hold_last_batches=)` so the APPROVE goal starts late and leaves a batch pending; `goal_pacing` paces each goal from its own start. |
| `api/v2/` | Alerts and briefings read from the runtime's stores (merged with the in-process logs); batch routes give a 404 that names the batch and a 409 that says what happened; `{}` or no body accepted; OpenAPI updated. The served demo simulates 1,200 customers over 56 days. |
| `docs/v2/screenshots/` | All 16 shots retaken with the real agent plus two batch shots; the README lists what still looks wrong and who owns each fix. |
| `docs/v2/RUNTIME.md` | New. How a tick runs, autonomy modes, batches, breakers and spend, the split, learning replay, send ownership, persistence, running the worker, known gaps. |

## How other streams use it

```python
from autonomy.runtime.service import (
    AutonomyRuntime, RuntimeConfig, SpendLedger, EvenPacing, SplitMismatchError, check_split,
    standard_breakers, material_brief, build_worker, tick_slot,
)
AutonomyRuntime(*, events, states, decisions, workspace, folder, policy_factory, queue=None,
                dispatcher=None, batches=None, breakers_fn=None, conversation=None, text_signals=None,
                analyst=None, strategist=None, spend_fn=None, planned_fn=None, assign_group=None,
                config=None, cursor=None, alerts: AlertStore | None = None,
                briefings: BriefingStore | None = None, channel_costs=None)
  .tick(now) -> TickReport          # TickReport.replayed_goals lists goals that relearned
  .check_brief(brief) -> None       # raises SplitMismatchError; call it when approving a brief
  .record(*, alerts=(), briefings=())
  .alert_store / .briefing_store
RuntimeConfig(..., run_send_jobs: bool | None = None, holdout_share=None, baseline_share=None,
              replay_lookback=timedelta(days=30), plan_days=30)
SpendLedger(decisions, workspace, *, channel_costs=None)(goal_id, now) -> float
EvenPacing(workspace, *, plan_days=30)(goal_id, now) -> float
check_split(brief, holdout_share, baseline_share) -> None
build_worker(name="worker-1", *, environ=None, start=None) -> Worker

from autonomy.runtime.stores import (AlertStore, BriefingStore, InMemoryAlertStore,
    InMemoryBriefingStore, SqlAlertStore, SqlBriefingStore, create_record_tables)
AlertStore.put(alerts) -> int; .list_alerts(*, goal_id=None, since=None, limit=None) -> list[Alert]
BriefingStore.put(briefings) -> int; .latest(goal_id, *, period=None) -> Briefing | None
BriefingStore.list_briefings(*, limit=None) -> list[Briefing]

from autonomy.runtime.settings import build_runtime   # also takes planned_fn=, channel_costs=,
                                                     # run_send_jobs= (default False)
from autonomy.runtime.worker import SendHandler, send_handler, worker_owns_sends
from autonomy.runtime.demo import run_demo, goal_pacing, DEMO_CHANNEL_COST
run_demo(..., channel_cost=None, approve_goal_days: int | None = None, hold_last_batches=False)
goal_pacing(goals, start, *, plan_days=30) -> SpendFn
```

A runtime-backed API (`api.v2.deps.build_services_from_runtime`) finds the stores by the
`alert_store` / `briefing_store` attributes, so nothing extra is wired.

## Contract additions requested

- An optional formal `nightly(decisions, events_by_customer, now)` hook on the `Policy` protocol
  (the runtime still finds it with `getattr`).
- Optional `JobQueue.next_due(kinds)` or a way to list jobs, so a restarted worker can see its
  tick chain (worked around with epoch-aligned tick ids, which merge chains).
- Optional per-goal spend on events (for example `attrs['channel_cost']` on SEND from the
  dispatcher), so the ledger need not price channels itself.

## Dependencies requested

None.

## Known gaps

- The demo and the API demo still count spend with their own channel-only tracker
  (`SpendTrackingEventStore`), so offer costs are not in their budgets; dropping `spend_fn`
  switches them to `SpendLedger` but changes the demo's numbers and screenshots.
- The API does not yet call `runtime.check_brief()` when a brief is approved; today the tick
  refuses the goal and raises a critical alert. A refused goal's approved batches, queued sends and
  weekly cycle still run.
- `RuntimeCursor` is not persisted: after a restart that day's nightly run repeats (harmless) and
  the weekly cycle can repeat within the week.
- Restart replay labels outcomes with each customer's current segment, not their segment at
  decision time. The agent's own `nightly()` pause messages are not turned into stored alerts.
- `worker_owns_sends` only works within one process; a hand-built runtime ticking beside a worker
  in another process must set `run_send_jobs=False` (`build_runtime` does).
- If `AUTONOMY_TICK_EVERY_MINUTES` changes between restarts, the old tick chain's cadence wins at
  shared slots.
- One shared holdout means per-goal proof is withheld once two goals' outcomes are measured, so the
  served demo's autopilot Proof page turns to a refusal note about 4 days after the API starts.
- Screenshot findings owned elsewhere (ui/v2 overflow and held-decision wording, explain.py
  reasons, the Analyst's programme-wide briefing) are listed in `docs/v2/screenshots/README.md`.
- `autonomy/runtime/` has no `__init__.py` (it is a namespace package, outside this stream's files).
- Not run locally: Postgres for the SQL alert and briefing stores, and mypy on Python 3.11
  (local mypy used `--python-version 3.13`; the stream's files were clean). Both are left to CI.
