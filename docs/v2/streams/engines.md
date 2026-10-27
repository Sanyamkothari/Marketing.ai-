# Stream: Engines (public engine APIs)

Three of the six engines get their own public HTTP API under `/v2/engines/...`, plus one router
that gathers every engine. Each endpoint takes its inputs in the request, so an engine can be used
on its own with a client's other tools. Time and seeds are passed in; no LLM runs per customer.
The user-facing guide, with request and response examples, is [docs/v2/ENGINES.md](../ENGINES.md).

## What was built (`api/v2/engines/`)

| Module | What it does |
|---|---|
| `__init__.py` | Docstring only, so importing one engine does not load the others. |
| `router.py` | `router` (prefix `/v2/engines`): imports each engine module by name and includes its `router`. A module that does not exist yet is skipped; one that fails to import raises. `GET /v2/engines` lists P1 to P6 and which are mounted. |
| `doctor.py` | P1 Data Doctor. `POST /doctor/profile`: CSV in, readiness score, personal-data findings (email, phone, Luhn cards, Verhoeff Aadhaar, PAN, names/addresses/DOB by header), key, date and leak checks out; optional v1 Indian-ID checks. `POST /doctor/map-events`: CSV plus `EventMapping` in, events and problem rows out. Stdlib multipart parsing, 20 MB cap. |
| `decisions.py` | P3 Decision Hub. `POST /decisions/evaluate` and `/decisions/evaluate-batch` (up to 1,000): one customer's decision from a freshly built agent policy, with the winning action and probability, every suppressed option and why, the hard-rule blocks and the auction's clearing price. `brief_ids` and content can come from the attached `Services` workspace. |
| `measurement.py` | P6 Incrementality Ledger. `POST /measurement/summary` (decisions + events in; treated vs holdout lift with interval and p-value, incremental revenue, costs, net profit, ROI out), `GET /measurement/goals/{goal_id}/summary` (same, from `Services`), `POST /measurement/ope` (IPS, SNIPS, DR), `POST /measurement/holdout/assign`. |

P2 (mandates), P4 (studio) and P5 (dispatch) are mounted automatically when their modules
(`api/v2/engines/mandates.py`, `studio.py`, `dispatch.py`) land with a `router`.

## How other streams use it

```python
# The integrator: mount once, next to the /v2 routes.
from api.v2.engines.router import router as engines_router
app.include_router(engines_router)            # serves /v2/engines/...

# A new engine module: export an APIRouter named `router` with only its sub-prefix.
router = APIRouter(prefix="/studio", tags=["engines: P4 studio"])

# Every engine's core is callable without HTTP.
from api.v2.engines.decisions import evaluate, evaluate_many, resolve_inputs
evaluate(state, briefs, content, *, now, seed=0, budget_left=None, kinds=None,
         exploration_floor=None) -> EvaluateResponse
evaluate_many(states, briefs, content, *, now, seed=0, ...) -> EvaluateBatchResponse
resolve_inputs(inputs: EvaluateInputs, services: Services | None)
    -> tuple[tuple[Brief, ...], tuple[ContentItem, ...], dict[str, GoalKind]]

from api.v2.engines.measurement import measurement_summary, off_policy_estimates, p_value
measurement_summary(decisions, events, *, horizon_days, objective, costs: Costs, now,
                    goal_id=None, min_group=MIN_GROUP, alpha=0.05) -> MeasurementSummary
off_policy_estimates(rows: Sequence[LoggedDecision], target, *, clip=None) -> OpeResponse

from api.v2.engines.doctor import read_csv_bytes, profile_frame, is_luhn_valid, is_verhoeff_valid
profile_frame(read_csv_bytes(data), *, snapshot=None, primary_key=None, target=None,
              date_columns=None, use_v1=True) -> ProfileReport
```

`Services` is optional everywhere: `brief_ids` need it (400 without), and the per-goal summary
returns 503 without it.

## Contract additions requested

None made. Possible additions for later:

- A public "policy view" or "explain bids" hook on `AgentPolicy`. `decisions.suppressed_actions`
  mirrors how `AgentPolicy.decide` prepares the customer and calls two private methods
  (`agent._send_hour`, `agent.policy._timed`); it is exact today but must change if `decide` does.
- A read-only copy of the runtime's learned `ModelBundle`, so `evaluate` could decide on the
  business's learned models instead of default priors.

## Dependencies requested

None. If FastAPI's own `File`/`Form` parameters are ever wanted, `python-multipart` would need to
join the light v2 CI install; the current endpoints parse multipart with the standard library.

## Known gaps

- `mypy --strict` has not run under Python 3.11 with pandas-stubs; CI is the check. A pandas-stubs
  complaint in `doctor.py` is the most likely failure.
- `api.v2.engines.router.router` is not yet mounted in `app.py` (the integrator's job).
- P3 `evaluate` always decides on default priors (fresh policy per call), not learned models.
- P3: allowed bids cut by `max_options` (10) are labelled `outbid`/`lost_draw` though they were
  never in the draw; a goal with a reachable channel but no usable content produces no suppressed
  entry; a `brief_id` for a paused goal is still evaluated.
- P6: the status follows the conversion comparison, not the money metric (profit has its own
  p-value and a note flags conversions up with money down). The lift % interval (delta method) can
  disagree slightly with the p-value in borderline cases. DR with `q_act` is an approximation for
  policies that choose among several actions. The per-goal summary loads every decision into
  memory and prices sends at the shop's own prices (`Services.send_prices`, docs/v4/ONBOARDING_MARGIN.md).
- P1: personal-data detection samples the first 1,000 non-empty cells per column in file order;
  `snapshot` must carry a zone offset. Column mapping proposals, identity linking and drift alerts
  are not in the API yet.
