# Stream: Connectors (wave 1)

Branch `v2/connectors`. Owns `autonomy/connectors/` (base, `shopify/`, `klaviyo/`, `wiring.py`),
`autonomy/channels/klaviyo.py`, `api/v2/connectors.py` (mounted in `api/v2/app.py`), the Connect
screen (`ui/v2/screens/connect.js` / `.css`, `ui/v2/api.js`, `ui/v2/mock/connector*.json`), an
additive `connector_handlers` in `autonomy/runtime/worker.py`, and `tests/v2/connectors/`. The full
design is `docs/v2/CONNECTORS.md`; its "Before going live" section is the launch checklist.

The founder's decisions this follows: first pilots are US/UK/Australia Shopify-style shops;
Autonomy decides and **sends through the customer's own tool** (Klaviyo) by default, with our SES as
the fallback; after a one-time connect nothing needs a person, and exactly one alert fires when one
must act.

## What was built

| Piece | What it does |
|---|---|
| Base (`base`, `http`, `oauth`, `tokens`, `store`, `sync`, `webhooks`, `health`, `registry`) | Connector contract; polite HTTP client (pinned versions, pacing, `Retry-After`, idempotent retries, secret-free errors); OAuth with state + PKCE; encrypted token vault with lease-locked refresh and keep-alive; two-lane sync (live from watermark minus overlap, history in date chunks newest first) with positions per stream; webhook intake (verify, dedupe, queue, ack fast, hourly catch-up poll); health and `ready_to_act`; one alert per problem. |
| Shopify (`shopify/`) | Admin GraphQL 2026-10, expiring offline tokens; customers + consent, orders (purchase, refunds, void), products; bulk-operation history paged by byte offset; HMAC webhooks and the mandatory compliance topics; privacy requests carried out (`ShopifyPrivacyDesk`); idempotent single-use discount codes and tags. |
| Klaviyo (`klaviyo/`, `channels/klaviyo.py`) | Revision 2026-10-15; OAuth (PKCE + Basic) or private key; consent, engagement and behaviour streams with per-metric cursors and per-endpoint pacing; **fully automatic send setup** (metric, template, live flow, with exact guided steps only when Klaviyo refuses); a channel adapter that hands each decision over once (`unique_id`); delivery read-back and a silent-drop watch that never concludes on stale or unlinked data. |
| Wiring (`wiring.py`) | `production_registry`, `after_connect`, `worker_handlers`, `send_providers` / `send_adapters`: one place where the API, worker and runtime meet. |
| API + Connect screen | `/v2/connectors` routes (catalogue, OAuth connect and callback, Shopify admin installs, pasted keys, health in words, sync now, disconnect, Klaviyo setup, webhooks); one card per platform with one plain status line and a single loud state, "Needs reconnect". |

## How other streams use it

```python
from autonomy.connectors import (production_registry, after_connect, worker_handlers,
                                 send_providers, send_adapters, ConnectorEngine)
registry = production_registry(events)                       # Shopify + Klaviyo (linked to hand-offs)
handlers = connector_handlers(engine, queue, events=events, privacy_desk=desk)   # runtime.worker
providers = send_providers(engine, business_id, fallback=build_adapters(settings))
verdict = engine.monitor.ready_to_act(business_id, now)      # hold deciding when data is stale
```

Event conventions: source `shopify` or `klaviyo`; `customer_id` is Shopify's numeric customer id
(Klaviyo's `auto` identity uses `external_id`, then email); `PURCHASE.attrs.offer_id` is the first
discount code (match it to `shopify.discount_code(key)` for attribution); a Klaviyo silent drop is
`FAIL` with `reason=not_delivered_by_tool`.

## Checks

ruff and black clean; `mypy --strict --python-version 3.11` clean on `autonomy/connectors`,
`autonomy/channels/klaviyo.py` and `autonomy/runtime/worker.py`; the py3.11 import works;
`pytest tests/v2/connectors tests/v2/api tests/v2/ui`: all pass, with the Postgres store variants
(need `MARKETING_AI_TEST_DATABASE_URL`) and the `live_connector` tests (need sandbox credentials)
skipped. `node --check` passes on the Connect screen.

## Open for the integrator

See `docs/v2/CONNECTORS.md`, "Before going live / Still open after wave 1": wire `ready_to_act` and
`send_gate` into the runtime, `history_hook` to `rebuild_states`, the engine and `send_providers`
into `build_runtime` and `api/main.py`; make proof exclude `not_delivered_by_tool` sends; decide
the disconnected-connection policy; close the Klaviyo identity-linking risk before the first pilot.
