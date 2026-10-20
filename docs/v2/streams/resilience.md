# Stream: Resilience

Branch `v2/resilience`. Owns `autonomy/runtime/service.py`, `autonomy/agents/{analyst,creative,critic,strategist}.py`,
`autonomy/channels/gateway.py`, `tests/v2/failure/test_failure_isolation.py` and
`docs/v2/FAILURE_ISOLATION.md`. Stream tests are in `tests/v2/resilience/`.

This stream closes the 10 xfail rows of the founder's failure-isolation table. When one engine
fails, the others now fall back to what is still safe, and nothing goes out unchecked. The table
in `docs/v2/FAILURE_ISOLATION.md` is now 27 pass, 0 xfail. No test was weakened. Each fix is local
and has a one-line comment naming its row.

## What was built

| Module | What changed |
|---|---|
| `autonomy/runtime/service.py` | P1: an unreachable event feed is caught (`ingest.unavailable`, watermark kept, decides on held states), a quiet feed raises `ingest.stale`, and a rebuild that fails part way keeps the old spend ledger (`SpendLedger.adopt`). P2: the last good mandate is held while the store is down (`mandate.store_unavailable`). P3: a raising policy logs a DO_NOTHING (`LEFT_ALONE`), and a policy that cannot be built skips only its goal. P6: if `spend_fn` is down, the last known budget less our own sends since then is used (`spend.unavailable`). A raising breaker scan gives `breakers.error`. |
| `autonomy/channels/gateway.py` | P6: a raising telemetry sink is caught and counted in `telemetry_errors`. It never changes a send. |
| `autonomy/agents/analyst.py` | P4: an unreachable model gives the code-only template briefing at once, without asking again. |
| `autonomy/agents/creative.py` | P4: fixed `TEMPLATE_COPY` per theme (offer only through `TEMPLATE_OFFER_LINE`), still DRAFT. After the first failure, the rest of the build stops asking the model. `Creative.templated` lists the cells that used templates. |
| `autonomy/agents/critic.py` | P4: while the judge is down, copy that passes the rules is held as DRAFT with a `judge unavailable` note, and the rest of the review stops asking. `BrandRules.judge_optional` allows approval on the rules alone. |
| `autonomy/agents/strategist.py` | Checked. It already fell back on `LLMError`, so it is unchanged. |
| `tests/v2/failure/test_failure_isolation.py` | All 10 xfail markers removed; the rows pass for real. |
| `tests/v2/resilience/` | `test_runtime_and_gateway.py` (outage edge cases: recovery, cold start, expiry, gateway SENDs, non-ticking workers) and `test_agents_llm_fallbacks.py` (templates pass the Critic's rules in every cell, judge circuit breaker, wording with slot fallbacks). |

## How other streams use it

```python
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, SpendLedger, LEFT_ALONE

RuntimeConfig(..., ingest_stale_after: timedelta | None = timedelta(days=2))  # None turns it off
AutonomyRuntime(...).send_gate(decision, now) -> str | None
    # Uses the held mandate during a store outage. A worker that has not ticked reads the event
    # log into its spend ledger first. If nothing can stand in, it raises, so the job is retried.
SpendLedger.adopt(other: SpendLedger) -> None
LEFT_ALONE: str   # reason on the DO_NOTHING decision logged when the policy raised

from autonomy.channels.gateway import DispatchGateway
DispatchGateway(...).telemetry_errors: int

from autonomy.agents.creative import Creative, TEMPLATE_COPY, TEMPLATE_OFFER_LINE
Creative(llm).templated: list[str]   # "<cell>: <code>" for each cell written from templates
from autonomy.agents.critic import BrandRules
BrandRules(..., judge_optional: bool = False)
```

New alert codes: `ingest.stale`, `ingest.unavailable`, `mandate.store_unavailable`,
`spend.unavailable`, `breakers.error`. `decide.error` is also raised for a policy that cannot be built.
Outage alerts are stored once a day.

Callers of `Critic.review` (the runtime demo, `api/v2/engines/studio.py`, the evals runner) may
now get DRAFT items back while the judge is down. DRAFT is never sendable, so this fails safe.

## Contract additions requested

None.

## Dependencies requested

None.

## Known gaps

- **Founder to confirm:** a LEFT_ALONE DO_NOTHING takes the trigger's decision id. A cart or
  reply trigger whose policy call raised is not retried later, and a sweep customer waits until
  the next day.
- With the mandate store down, a goal that never had a mandate held still halts with `mandate.error`,
  because `governs()` cannot be read either. A held mandate is not checked again against
  `problems()` until the store is back.
- During a spend outage, our own sends are priced by the runtime's ledger, not by the injected
  `spend_fn`. If the event log cannot be read either, the budget left is 0.
- The staleness clock starts again after a restart, so a feed that died before a restart is
  flagged only once `ingest_stale_after` has passed in the new process.
- `breakers.error` is stored once per tick, not once a day.
- Template copy is English only and has no country-pack lines (US STOP, India DLT). When brand
  rules name countries, the Critic rejects it, which fails closed.
- Not run locally: mypy --strict on Python 3.11 (the local mypy cannot see pydantic) and Postgres.
  CI has to confirm both. `black --check` cannot parse `docs/v2/FAILURE_ISOLATION.md` when it is
  passed by name. Every Python file is clean.
