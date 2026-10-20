# Stream P5: Dispatch Pro

Branch `v2/dispatch-pro`. Adds the send-time gate in front of the existing `Dispatcher`, an SMS
adapter, consent and DND registries, rate limits, country compliance packs, pack rules in the
Critic, and a Studio endpoint that certifies one rendered message. Tests are in
`tests/v2/dispatch_pro/` (80 tests, under 2 s, no network). Full detail: `docs/v2/DISPATCH.md`.

## What was built

| Module | What it does |
|---|---|
| `autonomy/channels/gateway.py` | `DispatchGateway`: consent proof, DND, mandate, DLT, quiet hours in the recipient's zone, idempotency, rate limits, carrier failover (never on an ambiguous error), backoff retry through the `JobQueue`. Wraps the `Dispatcher` for the decision path. |
| `autonomy/channels/sms.py` | `SmsAdapter` (a `ChannelAdapter`): GSM-7 / UCS-2 segments, India DLT templates, `TwilioStyle` and `Msg91Style` requests, signed delivery reports and STOP replies. |
| `autonomy/channels/consent.py` | `ConsentProof`, `ConsentRegistry` (in memory and SQL), `DndRegistry` (in memory and NCPR-style file), MSISDN and email normalisation. |
| `autonomy/channels/ratelimit.py` | Deterministic token buckets per (provider, channel) and per recipient; time is passed in. |
| `autonomy/compliance_packs/` | `CompliancePack` protocol and `IndiaPack` (TRAI DLT, NCPR, DPDP), `USPack` (TCPA, CAN-SPAM), `EUPack` (GDPR, ePrivacy, UK). Unknown countries get the strictest combination. |
| `autonomy/agents/critic.py` | `BrandRules` gains `countries`, `purpose`, `pack_settings`; `check_rules` adds each pack's problems, prefixed with the pack name. Unchanged when no country is named. |
| `api/v2/engines/dispatch.py` | `/dispatch` router: `POST /send` (202), `POST /webhooks/{provider}`, `GET /messages/{key}`. |
| `api/v2/engines/studio.py` | `/studio` router: `POST /assemble` renders a template for one customer and returns CERTIFIED or REJECTED with a full audit (packs, slots, PII, unbacked numbers, optional judge). |

## How other streams use it

```python
from autonomy.channels.gateway import DispatchGateway, Provider, SendRequest, pack_rules
from autonomy.channels.sms import SmsAdapter, Msg91Style, TwilioStyle, DltConfig
from autonomy.channels.consent import ConsentProof, SqlConsentRegistry, FileDndRegistry
from autonomy.channels.ratelimit import RateLimiter, Rate
from autonomy.compliance_packs import pack_for, PackSettings, Purpose

gateway = DispatchGateway(
    providers={Channel.SMS: [Provider("msg91", SmsAdapter(Msg91Style(...), dlt=DltConfig.from_registrations(settings.dlt_templates))),
                             Provider("twilio", SmsAdapter(TwilioStyle(...), sender="+1..."))]},
    consent=SqlConsentRegistry(engine), dnd={"IN": FileDndRegistry(path), "US": us_dnc},
    rules=pack_rules(settings), limiter=RateLimiter(...), queue=queue, events=events,
    require_mandate=False, mandate_check=None, retry=None, telemetry=None,
    default_country="IN", decision_purpose="promotional")

gateway.send(request: SendRequest, now) -> SendOutcome(record: MessageRecord, replayed: bool)
gateway.run_job(job, now) -> MessageRecord | None          # job kind GATEWAY_SEND_JOB
gateway.handle_webhook(provider_name, headers, body, now) -> WebhookOutcome   # raises GatewayError(status)

# Decision path (the loop's ACT decisions):
gateway.attach_dispatcher(address_book, workspace, events, profiles=None) -> Dispatcher
gateway.dispatch(decisions, states, now) -> DispatchReport
gateway.run_decision_job(job, decisions, states, now) -> DispatchReport   # job kind GATEWAY_DECISION_JOB
gateway.run_due(now, *, worker="gateway", limit=1000, decisions=None, states=None) -> int

# API: include the routers and give the dispatch one its gateway and clock.
from api.v2.engines import dispatch, studio
app.include_router(dispatch.router); app.include_router(studio.router)
dispatch.attach_gateway(app, gateway, clock)
app.dependency_overrides[studio.get_template_library] = ...; [studio.get_judge_llm] = ...

# Packs and the Critic:
pack_for(country: str | None, settings: PackSettings | None = None) -> CompliancePack
pack.quiet_hours(local_dt, channel, purpose) / .consent_requirements(channel, purpose)
pack.needs_dnd_check(channel, purpose) / .dnd_registry() / .content_problems(item, channel, purpose)
BrandRules.from_brief(brief, ..., countries=("IN",), pack_settings=settings)
check_rules(item, rules, *, packs=None) -> list[str]
studio.assemble(req: AssembleRequest, library=(), judge_llm=None) -> AssembleResponse
```

- Refusals and deferrals carry stable reason codes (`RefusalReason`, `ConsentProblem`). Every
  refusal reason is listed, not just the first.
- Do not run the Dispatcher's own `send_message` jobs for a gateway-wrapped Dispatcher; they skip
  the gate.
- Pass `dnd` as a per-country mapping. One registry passed alone is used for every country.

## Contract additions requested (not made)

- `ContentItem`: SMS registration fields (`dlt_template_id`, `sender_header`, `entity_id`) so an India
  SMS names its DLT template directly. Today `template_name` (documented as WhatsApp-only) carries it.
- `ContentItem` or `Decision`: an optional `purpose` (promotional / transactional / service). Today it
  comes from `BrandRules.purpose`, the request, or the gateway's `decision_purpose`.
- `Channel.VOICE`. The packs already accept 'voice', 'call' and 'ivr' as strings.
- Optional: move `ConsentProof` into `autonomy/contracts` (addition only) if other streams need it.
- Infra: an empty `api/v2/engines/__init__.py`. Without it, strict mypy over `api` may name
  `dispatch.py` and `studio.py` as top-level modules, and complain once `api/v2/app.py` imports them
  as `api.v2.engines.*`. It is outside this stream's files, so it was not created.

## Dependencies requested

None. Only pydantic, sqlalchemy, httpx, fastapi and the stdlib are used. `studio.py` imports
`engine.llm` (light) for the judge's client type.

## Known gaps

- The message ledger, the suppression set and the decision-path bookkeeping are in memory. A SQL
  `MessageLedger` is needed for several processes. STOPs also revoke proofs in a SQL consent
  registry, which does survive a restart.
- One lock serialises every send, provider HTTP calls included. Correct but slow; per-key locking
  is the upgrade.
- `TwilioStyle` checks webhooks with our shared-secret HMAC, not Twilio's URL signature. The Twilio
  and MSG91 request shapes follow each vendor's pattern, not its exact API.
- HTTP 5xx answers (502/504 included) count as definite errors and fail over; a 504 may mean the
  provider took the message.
- On the decision path, a rate-limited round is reported to the Dispatcher as a failure (a FAIL
  event) and retried with backoff, not at the bucket's retry time. Round counts reset when a retried
  decision is deferred again, so the retry cap can be exceeded across alternating deferrals.
- `pack_rules` merges quiet windows and consent age across purposes the strict way.
- US promotional SMS need a National DNC registry; without one they are refused
  (`dnd_registry_unavailable`). SMS to a country with no pack also needs DND and a DLT template, so it
  is effectively blocked until that country gets a pack. Both are deliberate.
- Rule data to confirm against operator portals: India header patterns, 19-digit DLT ids, the
  2000-character template limit. State and member-state variations (e.g. Florida's 8am-8pm) are not
  modelled. The EU unsubscribe check accepts any link in an email.
- The Studio number check is strict: template literals such as '24/7' need `allowed_numbers`.
- The STOP UNSUBSCRIBE event uses the MSISDN as `customer_id` when no `customer_lookup` is set.
- Python 3.11 mypy and Postgres behaviour of `SqlConsentRegistry` are checked by reading only
  (local runs: Python 3.13, SQLite; `mypy --strict --python-version 3.11` on the sources passes).
