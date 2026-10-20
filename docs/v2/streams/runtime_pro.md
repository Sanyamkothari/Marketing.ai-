# Stream: Runtime Pro

Branch `v2/runtime-pro`. Owns `autonomy/runtime/service.py`, `settings.py`, `worker.py`,
`autonomy/channels/breakers.py`, `tests/v2/failure/` and `docs/v2/FAILURE_ISOLATION.md`. Stream
surface checks are in `tests/v2/runtime_pro/`.

This stream wires the newer engines into the always-on runtime (state enrichment, signed mandates,
the send-time compliance gateway), adds two breakers (a last-hour complaint window and a nightly
negative-lift check), and turns the founder's failure-isolation table into tests. Everything new is
off by default in `AutonomyRuntime`, so a runtime built without it behaves as before.

## What was built

| Module | What it does |
|---|---|
| `autonomy/runtime/service.py` | `AutonomyRuntime` gains `state_enricher=`, `mandates=`, `gateway=` and `lift_guards=`. Adds `MandateSource` (protocol), `MandateBook` (in memory), `StateEnricher`, `send_gate()` for queued sends, and new `TickReport` fields `halted_goals` and `mandate_blocked`. |
| `autonomy/runtime/settings.py` | Gateway settings (`gateway_enabled`, `countries`, `consent_registry`, `dnd_registries`, `gateway_purpose`), negative-lift settings (`negative_lift_breaker`, `lift_min_group`, `lift_min_closed`), `build_gateway()`, and `build_runtime()` overrides for `state_enricher`, `mandates`, `consent`, `dnd`. |
| `autonomy/runtime/worker.py` | `SendGate`: send and gateway handlers re-check a queued send against its mandate when it comes due. New `GatewayHandler` / `gateway_handler` for held gateway decisions (`GATEWAY_KIND` jobs). |
| `autonomy/channels/breakers.py` | `Guards.fast_window` (1 hour) for complaint rates, counting only complaints about that hour's own messages. `negative_lift()` with `LiftGuards`: pauses a goal whose proof shows it confidently losing money against the holdout. |
| `tests/v2/failure/` | `test_runtime_wiring.py` (the four wiring parts above) and `test_failure_isolation.py` (the P1 to P6 failure table: 17 pass, 10 xfail with the gap named). |
| `docs/v2/FAILURE_ISOLATION.md` | The failure table, test names and status, and where each xfail needs fixing. |

## How other streams use it

```python
from autonomy.runtime.service import AutonomyRuntime, MandateBook, MandateSource, StateEnricher

AutonomyRuntime(..., state_enricher: StateEnricher | None = None,
                mandates: MandateSource | None = None,
                gateway: DispatchGateway | None = None,   # must have a dispatcher attached
                lift_guards: LiftGuards | None = None)
  .send_gate(decision, now) -> str | None                # a worker SendGate: a skip reason or None

StateEnricher = Callable[[list[CustomerState], Mapping[str, list[Event]], datetime], list[CustomerState]]

class MandateSource(Protocol):
    def governs(self, goal_id: str) -> bool: ...
    def lookup(self, goal_id: str) -> Mandate | None: ...
    def problems(self, mandate: Mandate, now: datetime) -> list[str]: ...

MandateBook(mandates=(), *, required_goals=(), require_all=False,
            verify: Callable[[Mandate], list[str]] | None = None).put(mandate)

from autonomy.runtime.settings import RuntimeSettings, build_runtime, build_gateway
build_runtime(settings=None, *, ..., state_enricher=None, mandates=None, consent=None, dnd=None)
build_gateway(settings, adapters, *, engine, queue, events, workspace, address_book,
              profiles=None, consent=None, dnd=None) -> DispatchGateway
RuntimeSettings.lift_guards() -> LiftGuards | None      # on by default in build_runtime

from autonomy.runtime.worker import SendGate, send_handler, gateway_handler, GATEWAY_KIND
send_handler(dispatcher, decisions, states, *, on_report=None, gate: SendGate | None = None)
gateway_handler(gateway, decisions, states, *, on_report=None, gate: SendGate | None = None)

from autonomy.channels.breakers import negative_lift, LiftGuards, NEGATIVE_LIFT_CODE
negative_lift(brief, decisions, events_by_customer, now, *, costs=None,
              guards=LiftGuards(min_group=MIN_GROUP, min_closed=100, bound="high")) -> BreakerResult
```

Alert codes the runtime now raises: `enrich.error`, `mandate.missing`, `mandate.not_active`,
`mandate.unverified`, `mandate.error`, `mandate.violation`, `mandate.paced`, `dispatch.refused`,
`dispatch.error`, `proof.negative_lift`, `proof.error`.

Environment: `AUTONOMY_GATEWAY_ENABLED`, `AUTONOMY_COUNTRIES` (e.g. `IN,US`),
`AUTONOMY_CONSENT_REGISTRY` (`sql` or `none`), `AUTONOMY_DND_REGISTRIES` (`NCPR=/path`),
`AUTONOMY_GATEWAY_PURPOSE`, `AUTONOMY_NEGATIVE_LIFT_BREAKER`, `AUTONOMY_LIFT_MIN_GROUP`,
`AUTONOMY_LIFT_MIN_CLOSED`.

## Contract additions requested

- Optional `StopMetric.NEGATIVE_LIFT`, so a brief's own stop rules could set the threshold. Today
  it is the separate `negative_lift()` check with alert code `proof.negative_lift`.
- A durable store for each mandate's spend pacer (`TokenBucket`), or a `MandateStore` interface in
  `autonomy/interfaces.py` with a SQL implementation. Today `MandateBook` and the pacer are in
  memory; the pacer is rebuilt from the last 24 hours of spend after a restart.
- Pack settings (DLT templates, postal address) in `RuntimeSettings` or a compliance-settings
  contract, so `build_gateway` can pass them to `pack_rules` and the SMS adapter's `DltConfig`.

## Dependencies requested

None.

## Known gaps

- **Founder to confirm:** `negative_lift` trips by default only when the upper end of the 95%
  interval of incremental profit is below zero (confidently losing money). The literal spec
  ("lower bound below zero") is `LiftGuards(bound="low")`, which pauses every goal that has not yet
  proved a profit.
- **Founder to decide:** with the mandate store down, the goal halts (`mandate.error`). The
  failure table asks it to keep running under the last unexpired mandate. Halting fails safe;
  the row stays xfail until someone picks.
- The 10 xfails in `test_failure_isolation.py` need fixes outside this stream's files (event scan
  and spend_fn guards, ingest staleness alert, DO_NOTHING logging and a policy_factory guard in
  the runtime; LLM fallbacks in the Analyst, Creative and Critic; a telemetry guard in the gateway).
  `docs/v2/FAILURE_ISOLATION.md` names each one.
- The nightly lift check is a repeated look at a fixed 95% interval with no sequential correction,
  so false trips grow over many nights. A person resuming the goal is the backstop.
- On autopilot the pacer draws before dispatch, so sends then refused or failed still use the
  day's pacer room (errs toward sending less).
- Customers whose sends a mandate blocks count as decided for the day, so they are decided again
  tomorrow, not later the same day.
- A recipient from a country outside `countries` gets the strictest of the listed packs, not their
  own country's pack.
- The gateway's own `decision_not_found` skip shows as `dispatch.refused` (cosmetic).
- `autonomy/channels/__init__.py` (not this stream's file) does not re-export `negative_lift` or
  `LiftGuards`; import them from `autonomy.channels.breakers`.
- Not run locally: mypy --strict on Python 3.11, and Postgres (`SqlConsentRegistry`, the upsert
  that re-records blocked decisions). Both need CI. `black --check` cannot parse the Markdown file
  `docs/v2/FAILURE_ISOLATION.md` when it is passed by name; every Python file is clean.
