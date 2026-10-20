# Stream WF5: Channels & API

Branch `v2/channels`. Owns `autonomy/channels/` and `api/v2/`, with tests in `tests/v2/channels/`
and `tests/v2/api/`, and the published API contract in `docs/v2/openapi.json`.

## What was built

`autonomy/channels/` (imports stay light: no httpx, boto3 or FastAPI at import time)

| Module | What it does |
|---|---|
| `render.py` | Fills a content item's `{{slots}}` (first_name, top_category, offer_label, weekday) for one customer; refuses unknown slots and offers the decision did not grant. |
| `dispatcher.py` | Turns ACT decisions into messages through channel adapters, at most once per decision, and re-checks every hard rule it can see before sending. |
| `breakers.py` | Circuit breakers: complaint rate, unsubscribe multiple, delivery rate, spend pace and WhatsApp quality, which pause goals or channels. |
| `common.py` | Helpers the dispatcher and adapters share, mainly `send_event_id`, so a provider's send confirmation and the dispatcher's SEND are one event. |
| `ses.py` | Amazon SES v2 email adapter: SendEmail with List-Unsubscribe (One-Click), decision tags, and SNS webhook parsing and verification. |
| `whatsapp.py` | WhatsApp Cloud API adapter: template sends, status and reply webhooks, and X-Hub-Signature-256 checks. |
| `client_tool.py` | `WebhookToolAdapter` (signed POST to the client's own tool, plus signed callbacks) and `CsvExportAdapter` (rows for a file import). |

`api/v2/`

| Module | What it does |
|---|---|
| `deps.py` | The `Services` container and the logic behind every endpoint, plus `build_demo_services()`, a deterministic in-memory demo. Needs only pydantic and starlette. |
| `routes.py` | The `/v2` FastAPI router: 16 endpoints for goals, approval, pause/resume, content, decisions, customers, briefing, proof, health, alerts, opportunities and webhooks. |
| `app.py` | `create_app()` (router, localhost CORS, ui/v2 at `/v2/app`), `export_openapi()`, and `python -m api.v2.app` to serve the demo on port 8010. |

## How other streams use it

```python
from autonomy.channels import Dispatcher, evaluate, evaluate_goals, render, send_event_id

Dispatcher(adapters: Mapping[Channel, ChannelAdapter], address_book: Callable[[str, Channel], str | None],
           workspace: Workspace, events: EventStore, queue: JobQueue | None = None, *,
           profiles: Callable[[str], Mapping[str, str]] | None = None,
           paused_goals: Iterable[str] = (), paused_channels: Iterable[Channel] = ())
  .dispatch(decisions, states: Mapping[str, CustomerState], now) -> DispatchReport
  .run_send_job(job, decisions: DecisionLog, states: StateStore, now) -> DispatchReport  # job kind SEND_JOB_KIND
  .pause(*, goals=(), channels=()) / .resume(*, goals=(), channels=())

evaluate(stop_rules, recent_events, baselines, spend, planned_spend, now, *, goal_id=None,
         guards=Guards()) -> BreakerResult(alerts, paused_goals, paused_channels)
evaluate_goals(briefs, recent_events, baselines, spend_by_goal, planned_by_goal, now) -> BreakerResult

render(item, state, profile, offer, now) -> Rendered(subject, body, template_params)  # raises RenderError
send_event_id(message_id) -> str
```

- Each skipped decision gets a `SkipReason` (holdout, goal not active, paused, no consent, content not
  approved, offer or channel outside the brief's envelope, no address, and so on). `message_id` is
  `stable_id('msg', decision_id)`; providers can use it as an idempotency key.
- A future `send_at` with a queue enqueues a `send_message` job; the worker calls `run_send_job`, which
  re-runs every check, and then marks the job done itself.
- Feed `BreakerResult` into `Dispatcher.pause(...)` and surface its `alerts`.
- Adapters are imported from their modules and all satisfy `ChannelAdapter`:
  `SesEmailAdapter(client, from_address, configuration_set, *, verifier, topic_arns, unsubscribe_url,
  unsubscribe_mailto)` (or `SesEmailAdapter.from_boto3(...)`),
  `WhatsAppCloudAdapter(phone_number_id, access_token, app_secret, transport=None, api_version='v21.0',
  *, verify_token, language, customer_lookup, decision_lookup, timeout)`,
  `WebhookToolAdapter(url, secret, *, transport, timeout, headers, source)` and
  `CsvExportAdapter(path, *, escape_formulas=True)`. `send()` never raises; webhook events carry the
  `decision_id`, and event ids are stable so provider retries dedupe.
- API: `create_app(services)` from `api.v2.app`. To mount on the main app instead, call
  `api.v2.deps.attach_services(app, services)` next to `app.include_router(api.v2.routes.router)`.
  `make_ingest(events, states, folder)` builds the webhook ingest callable.
- The UI codes against `docs/v2/openapi.json`.

## Contract additions requested (none were made)

- `Message.language: str | None` so WhatsApp can pick a template language per message (today one
  language per adapter).
- `Event.goal_id: str | None` (optional). Today the dispatcher writes `goal_id` into SEND attrs and the
  breakers map provider callbacks to goals through that SEND.
- An `EventType` for account-level channel signals, such as WhatsApp phone-number quality (optional).
  Today it rides on an existing WhatsApp event's attrs (`quality_rating`).

## Dependencies requested

None beyond the light set. boto3 (SES) is imported lazily only when a real client is built. A
production SNS signature verifier will need `cryptography` or similar; the SES adapter takes one
through `verifier=` and rejects every webhook without it.

## Known gaps

- Sending is at-least-once if the process dies between `adapter.send` and recording the SEND, and two
  workers are not locked against each other. WF1's Postgres event store or queue needs a unique
  constraint or claim, and its queue should insert-on-conflict-do-nothing for send jobs.
- The dispatcher does not check that the latest brief is approved, does not gate APPROVE-level goals
  on a batch approval, and does not re-check quiet hours or contact caps (those stay with `decide()`).
- Webhook-channel consent: only an unsubscribe is honoured, because "no consent" and "unknown" look
  the same there.
- Breaker guards (100 deliveries, 2 bad events, 100 sends, 25 of planned spend) are heuristics. The
  Gym should confirm planted harms trip within 2 batches. There is no early-warning alert.
- A WhatsApp "Stop promotions" button arrives as a REPLY; WF4's reply classifier or the webhook route
  must turn it into an UNSUBSCRIBE. Replies need a `customer_lookup` to map wa_id to a customer.
- There is no GET webhook handshake route (WhatsApp `hub.challenge`) in the API yet;
  `WhatsAppCloudAdapter.verify_subscription()` is ready for it.
- `docs/v2/openapi.json` was generated by a stand-in, because FastAPI is not installed in the build
  sandbox. Run `python -m api.v2.app --openapi` once with FastAPI and commit the result. The CI test
  checks paths and methods only.
- Locally, 9 FastAPI TestClient tests and 3 httpx send tests skip (packages not installed); the v2 CI
  job runs them. Strict mypy on the channels modules is clean apart from missing httpx/boto3 stubs,
  which CI ignores; mypy for `api/v2` against real FastAPI runs only in `ci.yml`.
- The demo is in memory and not thread-safe under uvicorn's threadpool, and `recent_decisions` scans
  the whole log. Both are fine for the demo; Wave 2 swaps in the Postgres stores.
