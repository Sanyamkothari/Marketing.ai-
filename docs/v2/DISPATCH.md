# Dispatch Gateway (P5)

The gateway is the one place a message leaves the system. It wraps the existing `Dispatcher` and
adds what can only be checked **at the moment of sending**: consent proof, do-not-disturb
registers, quiet hours in the recipient's own time zone, mandates, idempotency, rate limits,
carrier failover and buffered retry. Country rules are pluggable (`autonomy/compliance_packs`).

| File | What it holds |
|---|---|
| `autonomy/channels/gateway.py` | `DispatchGateway`, `SendRequest`, `MessageRecord`, `SendRules`, `default_rules`, `pack_rules`, `FailoverAdapter` |
| `autonomy/channels/sms.py` | `SmsAdapter` (a `ChannelAdapter`), GSM-7 / UCS-2 segmenting, DLT templates, `TwilioStyle`, `Msg91Style` |
| `autonomy/channels/consent.py` | `ConsentProof`, `ConsentRegistry` (in memory, SQL), `DndRegistry` (in memory, file) |
| `autonomy/channels/ratelimit.py` | `TokenBucket`, `RateLimiter` (per provider+channel, per recipient) |
| `api/v2/engines/dispatch.py` | `router` (prefix `/dispatch`), `attach_gateway(app, gateway, clock)` |
| `autonomy/compliance_packs/` | `pack_for(country, settings)`: India (DLT, NCPR, DPDP), US (TCPA, CAN-SPAM), EU/UK (GDPR); unknown countries get the strictest combination |
| `autonomy/agents/critic.py` | `BrandRules(countries=..., purpose=..., pack_settings=...)`: the Critic adds each pack's content rules |
| `api/v2/engines/studio.py` | `router` (prefix `/studio`): `POST /assemble` renders a template for one customer and certifies or rejects it |

The same packs judge content twice: the Critic and Studio check the template and the filled words
before approval, and the gateway (through `pack_rules`) checks consent, DND, DLT and quiet hours at
the moment of sending.

## Two ways in

1. **A request** (`gateway.send(request, now)`, or `POST /dispatch/send`): an idempotency key, a
   channel, a recipient (address, customer id, time zone, country), a payload (body or template
   and params, purpose) and a consent proof. Used by the API, by the client's own tools and by
   anything that sends outside the inner loop.
2. **Decisions** (`gateway.attach_dispatcher(...)` once, then `gateway.dispatch(decisions, states, now)`):
   the gate runs per decision, then the wrapped `Dispatcher` renders and sends through one
   `FailoverAdapter` per channel. Deferred decisions become `gateway_send_decision` jobs; finish
   them with `run_decision_job` (or `run_due(now, decisions=..., states=...)`). Do not run the
   Dispatcher's own `send_message` jobs for a gateway-wrapped dispatcher: that would skip the gate.

## The gate, in order

Hard refusals (status `refused`; every reason is listed, not just the first):

| Code | When |
|---|---|
| `no_provider`, `no_address` | Nothing to send through or to. A national-format phone number is read with the recipient country's code (India, US, UK); for any other country it must be international (+CC or 00CC), or it is refused rather than guessed |
| `mandate_missing`, `mandate_invalid` (or the check's own code) | `require_mandate=True` and no id, or `mandate_check` says no |
| `opted_out` | The address sent STOP, blocked us or complained (seen through a webhook) |
| `consent_missing`, `consent_channel_mismatch`, `consent_purpose_mismatch`, `consent_recipient_mismatch`, `consent_expired`, `consent_revoked`, `consent_unknown`, `consent_evidence_mismatch`, `consent_not_yet_valid` | The proof does not cover this channel, purpose and address now. With a registry, a proof is trusted only if the registry holds the same id and evidence hash; on the decision path the registry's live proof for the address is used |
| `dnd_registered`, `dnd_registry_unavailable` | The rules require a DND lookup (India NCPR for promotional SMS; the US pack asks for the National DNC) and the number is listed, or no registry is configured: **fails closed** |
| `dlt_template_missing` | India SMS without a template name |
| `unsubscribe_link_missing` | EU/UK promotional email without an unsubscribe link (built-in rules only; with packs the Critic checks disclosures) |
| `already_sent` | The request carries a decision id that already has a SEND event (under another key, or through the decision path) |
| `invalid_message` | The adapter refused the message itself (`invalid:` errors: DLT variable count, length or type, unknown template, bad number, too many segments). Never retried, never failed over |

Deferrals (status `queued`, re-checked from the top when the job runs):

| Code | When |
|---|---|
| `scheduled` | `send_at` is in the future |
| `quiet_hours` | The rules' quiet window, in the recipient's zone. Unknown zone: quiet if quiet in any of the country's zones (US: Eastern to Hawaii, Arizona included) |
| `rate_limited_recipient`, `rate_limited_provider` | A bucket is empty; queued until the next token |
| `provider_error` | Every provider refused; retried with exponential backoff (30 s, 60 s, ... capped at 1 h, deterministic jitter from the key) up to `max_rounds`, then `failed` |
| `ambiguous_retry` | A provider may have taken the message (read timeout); retried on the **same** provider only |

Without a queue, a deferral is a refusal with `no_queue` added (or `failed` after attempts).

## Never twice

- **Idempotency.** The key is `idempotency_key`, or the decision id. A second request with a known
  key returns the first record (`replayed: true`) and sends nothing, whatever its body says.
- **Attempts are recorded** on the record before the next provider is tried.
- **Failover only on definite errors** (an HTTP error answer, a refused connection). An ambiguous
  error pins the provider; the retry reuses the message id, which providers that support
  idempotency keys de-duplicate (`TwilioStyle` sends it as `Idempotency-Key`, `Msg91Style` as `CRQID`).
- **International numbers to adapters.** The gateway hands adapters `+<digits>`, so no adapter re-reads a
  normalised number with its own default country code.
- **Stale jobs do nothing.** A job runs only if it is the record's current `job_id`.

## Country rules

`RulesFor = (country, channel) -> SendRules`. Pass `rules=pack_rules(settings)` in production: it
reads `autonomy.compliance_packs` (per channel and purpose) and merges windows that differ by
purpose the strict way. `default_rules` is a small built-in set for tests and fallbacks:

| | India | US | EU/UK | Unknown |
|---|---|---|---|---|
| Quiet hours (local) | 21:00-09:00, promotional | 21:00-08:00 (TCPA), promotional and service | none statutory | 21:00-09:00 |
| Consent proof | all but transactional | all but transactional | all but transactional | all but transactional |
| DND | NCPR, promotional SMS | (pack: National DNC) | - | - |
| DLT template | required for SMS | - | - | - |

The country is the recipient's `country`, else inferred from a `+CC` number (91, 1, 44), else the
gateway's `default_country` for phone channels.

## SMS

- `analyze(text)`: GSM-7 (160 per single SMS, 153 per part; `€ [ ] { } ^ ~ | \` count two) or UCS-2
  (70, then 67; emoji count two units). Characters are never split across parts.
- With a `DltConfig` the text is **built from the registered template** (`{#var#}`, `{#numeric#}`,
  `{#alphanumeric#}`, `{#url#}`, `{#cbn#}`), so the operator's template match holds by construction;
  the entity id, header and template id go in every request. `DltConfig.from_registrations(settings.dlt_templates)`
  builds the config from the brand's `PackSettings`, so the text the India pack approved is the text sent.
- Webhooks: a shared-secret HMAC-SHA256 of the raw body in `X-Signature`. Delivery reports map to
  the decision through the ids the adapter sent, or the gateway's ledger (`receipt_lookup`).
  Inbound STOP / UNSUBSCRIBE / CANCEL / END / QUIT / STOPALL / OPTOUT become UNSUBSCRIBE events;
  the gateway suppresses the number at once and revokes its proofs in the registry.

## API (`/v2/engines/dispatch/...` once mounted)

- `POST /send` `{idempotency_key, mandate_id?, channel, recipient, payload, consent_proof?, decision_id?, send_at?}`
  -> **202** `{idempotency_key, status: queued|sent|refused, reasons, message_id, provider, provider_message_id, next_attempt_at, replayed}`.
- `POST /webhooks/{provider}` -> 200 `{provider, events, stored, suppressed}`; 401 bad signature, 404 unknown provider, 400 unreadable body.
- `GET /messages/{idempotency_key}` -> the `MessageRecord` (attempts, rounds, delivery), or 404.

## Telemetry

Every step emits a `TelemetryEvent` to `telemetry=` (and the gateway's bounded `telemetry_log`):
`dispatch.received`, `.refused`, `.queued`, `.attempt`, `.sent`, `.failed`, `.replayed`,
`.webhook`, and `dispatch.decision.<status>` on the decision path. Request-path sends and final
failures are also written to the event store as SEND / FAIL events (SEND uses `send_event_id`, so
a provider's own "sent" callback does not double it).

## Known limits

- The ledger is in memory (`InMemoryMessageLedger`); a SQL ledger is the next step for
  multi-process deployments. The suppression set is in memory too, though STOPs also revoke proofs
  in a SQL consent registry, which survives a restart.
- One lock serialises sends, including provider calls: correct, not fast. Per-key locking is the
  upgrade when volume needs it.
- `TwilioStyle` verifies callbacks with our HMAC scheme, not Twilio's URL-based signature.
