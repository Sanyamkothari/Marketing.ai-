# Connectors: connect once, then run with nobody watching

Autonomy decides per customer and, by default, **sends through the customer's own tool** (their
unsubscribes, sender reputation and templates stay there). To do that it must read the shop's
customers, orders, consent and engagement continuously, and write decisions back, for months,
without a person. This page explains the connector base in `autonomy/connectors/`: how it works,
how to add a connector, and what it guarantees after the one-time connect (each guarantee names
the test that holds it).

Research behind the design: `docs/research/v2/connectors/` (start with `00_RECOMMENDATION.md`; the
hands-off checklist is in `03_competitors_build_vs_buy.md` section 4).

## The pieces

| Module | What it does |
|---|---|
| `base.py` | The SaaS connector contract: `SaasConnector`, `ConnectorCapabilities`, `StreamSpec`, `Connection`, `StreamPage`, `WebhookDelivery`; the failure classes; `map_with` (records → events through `autonomy.data.mapping`); `redact` |
| `http.py` | `ConnectorHttp`, the polite client: pacing, pinned versions, retries with backoff and jitter, `Retry-After`, idempotency keys, pagination helpers, secret-free errors; `ManualClock` / `SystemClock` |
| `oauth.py` | Authorization code flow: state, PKCE (S256), single-use callback, code exchange, refresh, revoked-versus-transient |
| `tokens.py` | `TokenSet`, the encrypted `SqlTokenVault` (v1's credential-store key and fingerprint), `TokenManager` (refresh under a lease, keep-alive, revoked → `needs_reconnect`) |
| `store.py` | All connector state in SQL: connections, positions per (stream, lane), run log, leases, webhook deliveries, record versions, OAuth states, incidents, secrets, privacy requests |
| `sync.py` | `ConnectorEngine`: connect, two-lane sync, ingest, scheduling on the job queue, failure handling, daily maintenance |
| `webhooks.py` | `WebhookIntake`: verify (constant time), dedupe, queue, ack fast; process later; `hmac_matches`, `query_hmac_matches` |
| `health.py` | `HealthMonitor`: freshness, history readiness, row drops, drift, error streaks; `ready_to_act`; `send_gate`; `IncidentDesk` (one alert per problem) |
| `registry.py` | `ConnectorRegistry`, `default_registry()`, the catalogue for the API and the Connect screen |
| `wiring.py` | How it plugs in: `production_registry`, `after_connect`, `worker_handlers`, `send_providers` / `send_adapters` (the customer's Klaviyo first, our SES as the fallback) |
| `shopify/`, `klaviyo/` | The first two platforms (sections below); Klaviyo's send channel is `autonomy/channels/klaviyo.py` |

Nothing imports httpx, cryptography or v1's `engine` at module scope, so `import autonomy.connectors`
works in the light v2 environment.

What is reused rather than rebuilt: v1's credential-store key, Fernet scheme and key fingerprint
(`engine.connections.store.connections_key`, `key_fingerprint`), v1's address guard
(`engine.connections.net.check_url`) and timeouts, `autonomy.data.mapping` (stable event ids),
the SQL job queue and its `SKIP LOCKED` claim (`autonomy.store.SqlJobQueue`), the store helpers
(`insert_ignore`, `upsert`, `UtcDateTime`), the gateway's deterministic-jitter `RetryPolicy`, the
token bucket (`autonomy.channels.ratelimit`), the `Alert` contract and `AlertStore`, and the worker's
`SendGate` and `PermanentJobError`.

## How it works

### Connect (the only human step)

```
browser ──▶ API: begin_oauth(kind, business_id, redirect_uri, now, params={"shop": ...})
            └─ stores PendingAuthorization(state, PKCE verifier) for 10 min, returns consent URL
platform ─▶ API: finish_oauth(kind, query, now, queue)
            ├─ connector.validate_callback(query, app)        (Shopify: query hmac)
            ├─ state taken (single use), error=access_denied → AuthorizationDenied
            ├─ code (+ verifier) exchanged for tokens → SqlTokenVault (encrypted)
            ├─ connector.identify(ctx) → external_id, label     (who the account is)
            ├─ Connection stored; id = stable_id(kind, business, account)  → a reconnect reuses it
            └─ seed(): live job per stream, history job per stream with history, daily job
```

`connect_api_key(...)` does the same for a pasted private key (Klaviyo private keys, Shopify
custom-app tokens), which never expire or refresh.

### Two lanes per stream

Every stream (`customers, orders, products, events, consent, engagement`) syncs in two lanes, each
with its own resumable `SyncPosition`:

* **live**: `[high_watermark - overlap, now)` every `cadence` (default hourly, 2 h overlap). The
  first live window starts at `connect time - overlap`. The watermark moves to the window's end
  only when the window finishes.
* **history**: walks back from the connect time in `backfill_chunk` windows (default 7 days),
  newest first, until `backfill_days` (default 730). Decisions may rely on a stream once history
  covers `min_history_days` (default 30), long before the backfill ends.

Within a window the connector returns pages (`StreamPage`); the position (window + cursor) is saved
after every stored page, so a crash, a deploy or a long 429 resumes exactly there. A platform that
exports history asynchronously (Shopify bulk operations) returns `StreamPage(pending_until=...,
next_cursor=<operation id>)`; the engine saves it and comes back then, without holding a worker.

### Ingest: dedupe twice, never lose

For each page:

1. `record_key(stream, record)` gives the record's id and `updated_at`.
2. Records not newer than the version already seen (`connector_record`) are skipped: the overlap
   re-read and out-of-order webhooks cost nothing and never overwrite a later version.
3. `map_records` turns the rest into `Event`s with **stable ids** (use `map_with` and an
   `EventMapping` with `row_key`; the account is prepended so two shops never collide).
4. The sink (`EventStore.append`) drops ids it already has.
5. Only then is the record version written, so a crash between 4 and 5 replays rather than loses.

The engine does not fold customer state: the runtime folds new events itself on its next tick.
Events older than two days (history) are outside the runtime's late window, so `history_hook` is
called and the runtime should rebuild state (`autonomy.data.rebuild_states`) once a history chunk
lands. An event for a record version is immutable: connectors express corrections as their own
events (a refund is a `REFUND` event, not an edit of the purchase).

### Scheduling

All work is on the SQL job queue, so any number of workers can run it:

| Job | Payload | Successor |
|---|---|---|
| `connector_sync` | connection, stream, lane | live: next cadence slot (or `retry_at`, or now if pages are left); history: now until done |
| `connector_webhook` | connection, raw headers, raw body | none |
| `connector_daily` | connection | the same time tomorrow |

Live jobs land on a grid (`slot_after(now, cadence)`), and a webhook asks for its stream's catch-up
on the next hourly slot: same slot, same job id, so a burst of webhooks queues **one** poll. The
daily job re-registers webhooks, refreshes idle tokens, evaluates health, prunes old deliveries and
runs, and restarts any chain that ended. Wire it with
`Worker(handlers={**other, **autonomy.runtime.worker.connector_handlers(engine, queue)})`.

### Failures say what to do

| Failure | Raised when | Engine does |
|---|---|---|
| `RateLimited` | 429 whose wait is longer than 30 s, or retries exhausted on 429 | saves the position, re-queues at `retry_at` |
| `TransientFailure` | timeouts, resets, 5xx after 5 attempts | error streak +1, retry with backoff (1, 2, 4 … min, capped at the cadence) |
| `SourceUnavailable` | 402 (frozen shop), 423 (locked) | retry in an hour; alerts after the error streak |
| `CursorExpired` | the platform no longer accepts a saved cursor | restarts the window (ids dedupe) |
| `NeedsReconnect` (`AuthRejected`, `AccessDenied`, `VaultUnreadable`) | refresh `invalid_grant`, 401 after a fresh token, 403, uninstall, unreadable vault | `needs_reconnect`, one critical alert, every job for it stops |
| `RemoteError`, `NotSupported` | a 4xx that retrying will not change | recorded as a failed run |
| anything else (a connector bug) | | recorded as a failed run; the chain continues |

## Adding a connector

1. Create `autonomy/connectors/<kind>.py` with a `SaasConnector` subclass:

```python
class AcmeConnector(SaasConnector):
    capabilities = ConnectorCapabilities(
        kind="acme", label="Acme", description="...", auth=AuthMethod.OAUTH2, setup=SetupMode.AUTOMATIC,
        streams=(
            StreamSpec(stream=Stream.ORDERS, required_fields=("id", "updated_at"), webhook_topics=("orders/updated",)),
            StreamSpec(stream=Stream.CONSENT, backfill_days=0),            # a current-state stream
            StreamSpec(stream=Stream.PRODUCTS, critical=False, snapshot=True, cadence=timedelta(hours=6)),
        ),
        webhook_topics=(WebhookTopic(topic="orders/updated", stream=Stream.ORDERS),
                        WebhookTopic(topic="app/uninstalled", uninstall=True)),
        api_version=ApiVersion(value="2026-07", header=None, doc_date=date(2026, 10, 20),
                               docs_url="https://...", review_by=date(2027, 4, 1)),
        rate=RateSpec(requests=2, per=timedelta(seconds=1), burst=40),
    )
    oauth = OAuthProvider(name="Acme", authorize_url="https://{shop}/oauth/authorize",
                          token_url="https://{shop}/oauth/token", scopes=(...),
                          param_patterns=(("shop", r"[a-z0-9-]+\.acme\.com"),))

    def base_url(self, connection): ...
    def read(self, ctx, stream, window, cursor) -> StreamPage: ...      # ctx.http.json / .graphql
    def record_key(self, stream, record) -> RecordKey: ...
    def map_records(self, stream, records, connection) -> MappedRecords:
        return map_with(MAPPING[stream], records, source="acme", account=connection.external_id or connection.connection_id)
```

2. Optional overrides: `auth_headers` (default bearer), `identify`, `check`, `pacing` /
   `retry_signal` (GraphQL cost), `validate_callback`, the webhook methods (`webhook_account`,
   `verify_webhook`, `webhook_delivery_id`, `webhook_topic`, `parse_webhook`, `register_webhooks`,
   `webhook_secret`), and `write` for send-through actions (always with the idempotency key).
3. Add one line to `registry.BUILTIN`.
4. Tests in `tests/v2/connectors/test_<kind>_*.py` with `httpx.MockTransport` fixtures in the
   documented shapes; reuse `tests/v2/connectors/base_support.py` (`make_rig` takes your connector).
   Live tests carry `@pytest.mark.live_connector` and call the `live_env(...)` fixture with the
   variables they need (both registered in `tests/v2/connectors/conftest.py`).

### Notes for the first two connectors

* **Shopify** (Admin GraphQL, pin a quarterly version such as `2026-07` in the URL; public apps
  must move to expiring offline tokens by 1 Jan 2027). Use `OAuthProvider(client_auth="body",
  revoked_errors={"invalid_grant", "invalid_request"}, param_patterns=(("shop", ...myshopify.com),),
  extra_token_params=(("expiring", "1"),), keep_alive_every=30 days)`: per shopify.dev,
  `expiring=1` goes in the code-exchange POST to `/admin/oauth/access_token`, not in the authorize
  URL. The refresh token rotates and the old one dies at once: the lease in `TokenManager` (5 min,
  longer than the bounded token call) is what makes that safe; on a timeout, 5xx or 429 Shopify says
  to retry with the same refresh token, which the token client does. Validate the
  callback `hmac` with `query_hmac_matches(app.client_secret, query)` in `validate_callback`.
  Webhooks: `verify_webhook` = `hmac_matches(secret, body, headers["x-shopify-hmac-sha256"])`,
  dedupe on `x-shopify-webhook-id`, account from `x-shopify-shop-domain`, topic from
  `x-shopify-topic`; the three mandatory compliance topics map to `PrivacyKind`; `app/uninstalled`
  sets `uninstalled=True`. History through bulk operations: return `pending_until` while the
  operation runs, then page through the JSONL with
  `ctx.http.download_lines_from(url, start=offset)`, stopping after a bounded number of lines and
  keeping the byte offset it yields in the cursor (`"<op id>@<offset>"`): memory stays bounded and
  each page resumes with a `Range` request instead of downloading the file again. Shopify sends
  `shop/redact` about 48 h after `app/uninstalled`; the intake still finds the disconnected
  connection for privacy and uninstall topics, so the request is recorded. Pace with `pacing=graphql_throttle_wait`, `retry_signal=graphql_throttled`.
* **Klaviyo** (JSON:API, dated revision in the `revision` header). `OAuthProvider(use_pkce=True,
  client_auth="basic", keep_alive_every=timedelta(days=30), min_refresh_interval=timedelta(seconds=10))`
  (refresh tokens die after 90 days unused; 10 refreshes a minute). `auth_headers` for a private key is
  `Authorization: Klaviyo-API-Key <key>`. Pages: `jsonapi_next(body)` gives an absolute URL on the API
  host, which `ConnectorHttp` accepts. Klaviyo has no general webhooks: engagement comes from the
  Events API on the live lane. Rate limits arrive as `RateLimit-*` headers and 429 `Retry-After`,
  both handled by the client.

## The hands-off guarantees, and the tests that hold them

| Guarantee | How | Test |
|---|---|---|
| Tokens are encrypted at rest, never in a repr, log or error | `SqlTokenVault` + v1 key scheme; `Field(repr=False)`; `redact` | `test_base_tokens::test_sql_vault_encrypts_and_refuses_another_key`, `::test_fernet_cipher_uses_the_v1_credential_store_scheme`, `test_base_http::test_errors_are_secret_free` |
| Refresh before expiry; never two refreshers | 5-min margin, lease `token:<id>`, re-read inside the lease | `test_base_tokens::test_a_token_near_expiry_is_refreshed_and_saved`, `::test_a_second_worker_does_not_spend_a_rotating_refresh_token_twice`, `::test_a_busy_lease_uses_the_current_token_or_asks_to_retry` |
| Keep-alive for idle connections (Klaviyo 90 days) | daily job, `keep_alive_every` | `test_base_tokens::test_keep_alive_refreshes_idle_connections_before_the_90_day_revocation`, `test_base_sync::test_daily_job_renews_webhooks_and_keeps_idle_tokens_alive` |
| Revoked access → `needs_reconnect`, exactly one alert, no loops | `on_revoked`, root-cause incidents | `test_base_tokens::test_end_to_end_the_engine_marks_needs_reconnect_and_alerts_once`, `test_base_sync::test_revoked_access_stops_the_connection_with_one_alert_and_reconnect_resumes`, `test_base_health::test_needs_reconnect_hides_every_symptom` |
| OAuth state single use, PKCE, no free-text hosts | `PendingAuthorization`, `pkce_challenge`, `param_patterns` | `test_base_oauth` (all) |
| Webhooks verified in constant time, acked fast, deduped | `WebhookIntake.receive` does no parsing | `test_base_webhooks::test_receive_verifies_queues_and_asks_for_one_catch_up_poll`, `::test_bad_signatures_and_unknown_accounts`, `::test_a_duplicate_delivery_is_acked_but_not_queued_again` |
| Out-of-order and replayed deliveries are harmless | record versions + stable event ids | `test_base_webhooks::test_processing_tolerates_out_of_order_and_replayed_deliveries` |
| An hourly catch-up poll behind every webhook | grid-slot job ids | `test_base_sync::test_webhook_catch_up_requests_collapse_to_one_poll_per_hour` |
| Subscriptions re-registered daily | `daily()` → `register_webhooks` | `test_base_sync::test_daily_job_renews_webhooks_and_keeps_idle_tokens_alive` |
| Sync positions per stream, advanced only after storing | position saved per page; watermark per window | `test_base_sync::test_a_failure_mid_window_resumes_at_the_saved_cursor`, `test_base_store::test_positions_round_trip` |
| Backfill in date chunks, newest first, resumable | history lane | `test_base_sync::test_worker_backfills_newest_first_in_chunks_and_stores_each_order_once` |
| Incremental with overlap; replays are no-ops | live lane, stable ids | `test_base_sync::test_incremental_reads_from_the_watermark_minus_the_overlap_and_dedupes`, `test_base_registry::test_map_with_gives_stable_per_account_ids_and_collects_bad_rows` |
| Pinned API versions | `ApiVersion` header or URL | `test_base_http::test_pins_the_version_and_sends_auth_on_every_request` (the fake platform refuses requests without it) |
| Rate limits: pace first, honour `Retry-After`, back off with jitter | token bucket, `RateLimit-*`, GraphQL cost, `RetryPolicy` | `test_base_http::test_token_bucket_paces_to_the_documented_rate`, `::test_429_*`, `::test_5xx_backs_off_with_jitter_then_gives_up_with_a_retry_time`, `::test_graphql_cost_pacing_and_throttled_retry`, `test_base_sync::test_a_long_rate_limit_parks_the_job_until_retry_after` |
| Idempotent writes | `idempotency_key`, POST retried only with a key | `test_base_http::test_a_post_is_retried_after_a_timeout_only_with_an_idempotency_key` |
| Schema drift noticed, not fatal | `required_fields` counted, unknown fields ignored, bad rows are problems | `test_base_sync::test_schema_drift_is_counted_and_unknown_fields_are_ignored`, `test_base_http::test_pagination_helpers_tolerate_drift` |
| Expired cursors recover | `CursorExpired` restarts the window | `test_base_sync::test_an_expired_cursor_restarts_its_window` |
| Stale data stops the agent acting | `ready_to_act`, `send_gate` | `test_base_health::test_a_stale_critical_stream_holds_the_business_with_one_alert`, `::test_never_synced_and_short_history_hold_decisions`, `::test_the_send_gate_holds_queued_messages_while_data_is_stale` |
| "Succeeds with fewer rows" is caught | 24 h rows vs the 7-day median | `test_base_health::test_row_count_drop_against_the_previous_week` |
| One alert per real problem | atomic incidents, root-cause suppression, re-alert only after it cleared | `test_base_health` (all), `test_base_store::test_an_incident_opens_once_and_reopens_after_it_closed` |
| One syncer per stream | lease `sync:<id>:<stream>:<lane>` | `test_base_sync::test_one_syncer_per_stream`, `test_base_store::test_a_lease_has_one_holder_until_it_expires` |
| Uninstall and privacy webhooks handled | `uninstalled()`, `connector_privacy` | `test_base_webhooks::test_uninstall_disconnects_and_alerts_once`, `::test_privacy_requests_are_recorded_with_one_alert_each` |

The store tests run on SQLite locally and on Postgres in CI (`MARKETING_AI_TEST_DATABASE_URL`).

## Wiring (for the API and the runtime)

`autonomy/connectors/wiring.py` is the one place the pieces meet, so the API, the worker and the
runtime agree:

```python
db = create_engine(url); create_all(db); create_connector_tables(db); create_privacy_tables(db)
store, events = ConnectorStore(db), SqlEventStore(db)
registry = production_registry(events, product_sink=catalogue.add)  # Klaviyo linked to hand-offs
engine = ConnectorEngine(
    registry=registry, store=store,
    vault=SqlTokenVault(store, FernetCipher.from_settings(settings, storage)),
    sink=events.append, clock=SystemClock(),
    apps={k: a for k in registry.kinds() if (a := AppCredentials.from_env(k, os.environ))},
    monitor=HealthMonitor(store, registry, desk=IncidentDesk(store, SqlAlertStore(db))),
    history_hook=on_history,   # runtime-side: rebuild state once history lands (rebuild_states)
    webhook_base_url="https://<host>/v2/connectors/webhooks", holder=worker_name,
)
desk = ShopifyPrivacyDesk(store, db, desk=engine.monitor.desk)
worker = Worker(queue, handlers={**runtime_handlers,
                                 **connector_handlers(engine, queue, events=events, privacy_desk=desk)})

# Sending: decide in Autonomy, send through the customer's own tool; our SES stays the fallback.
providers = send_providers(engine, business_id, fallback=build_adapters(settings),
                           details=details_from(decision_log, workspace, discount_code=codes))
gateway = DispatchGateway(providers, limiter=RateLimiter(provider_rates=provider_rates()), ...)
# (a plain Dispatcher without failover: send_adapters(engine, business_id, fallback=...))
```

* **After every connect** the API calls `after_connect(connection, queue, now)` (idempotent): it
  starts Klaviyo's hourly setup-and-delivery watch, and the Shopify privacy chain. `engine.seed`
  (called by `finish_oauth` / `connect_api_key` with a queue) starts the syncs and the daily job.
* **Connecting never disturbs a live connection.** `ConnectorEngine._connected` asks the platform
  who the account is with the new tokens unsaved (`engine.context(..., candidate=tokens)`, which
  never refreshes and never touches the vault), and stores them only once `identify` succeeds. A
  mistyped key for a shop that is already connected is a 400, and the live tokens stay as they were
  (`test_base_sync::test_a_mistyped_key_for_a_connected_account_leaves_the_live_connection_alone`).
* **Providers.** `send_providers` gives each channel the business's active Klaviyo connection sends
  (email, plus SMS with `sms=on`) as `[Provider("klaviyo", ...), Provider("<channel>", fallback)]`.
  Klaviyo's definite refusals (`not_ready:`, `needs_reconnect:`, `rate_limited:`) fail over to our
  own adapter; `ambiguous:` never does (it may have arrived). With no active Klaviyo connection
  (none yet, or one that needs reconnecting) the fallback sends alone. Rebuild the providers when a
  connection changes (the runtime reads them at start-up; a restart or a periodic rebuild is enough).
* **API routes** (`api/v2/connectors.py`, under `/v2/connectors`; its module docstring has the
  full table): `GET /` (platforms, connections and `can_act` in words), `POST /{kind}/connect`
  (OAuth consent URL), `GET /{kind}/callback`, `GET /{kind}/install` (installs started in the
  Shopify admin), `POST /{kind}/key` (pasted key or custom-app token), `GET /connections`,
  `GET|DELETE /connections/{id}`, `POST /connections/{id}/sync`, `GET /connections/{id}/setup`
  (Klaviyo's automatic setup or its guided steps), `POST /webhooks/{kind}` and
  `POST /webhooks/{kind}/{connection_id}` (raw body to `WebhookIntake.receive`, answered at once).
* **Runtime**: call `monitor.ready_to_act(business_id, now)` before deciding and hold the business
  when `can_act` is false (its `reasons` are plain words); pass `monitor.send_gate(business_id)` as
  the worker's send `gate` so queued messages wait too. Not wired yet (see "Before going live").

## Environment variables

| Variable | For |
|---|---|
| `AUTONOMY_<KIND>_CLIENT_ID`, `AUTONOMY_<KIND>_CLIENT_SECRET` | our app's OAuth credentials per platform (e.g. `AUTONOMY_SHOPIFY_CLIENT_ID`); Shopify webhooks are signed with the client secret |
| `MARKETING_AI_CONNECTIONS_KEY` | the encryption key (v1's), shared with saved v1 connections |
| `AUTONOMY_LIVE_SHOPIFY_SHOP`, `AUTONOMY_LIVE_SHOPIFY_TOKEN` | live tests against a Shopify development store (custom-app token, read scopes) |
| `AUTONOMY_LIVE_KLAVIYO_API_KEY` | live tests against a Klaviyo test account (private key, read scopes) |
| `MARKETING_AI_TEST_DATABASE_URL` | the Postgres variant of the store tests (CI sets it) |

## Known limits

* Privacy requests are recorded with one alert each. For Shopify, `ShopifyPrivacyDesk` (the hourly
  `shopify_privacy` job) carries them out on our copy; a data-request export still needs a person
  to send it to the merchant, so its alert stays open.
* A store-initiated Shopify install (from the Shopify admin, not our Connect screen) arrives without
  a `business_id`; the API attaches it to the deployment's one business (the pilot shape). A
  multi-tenant deployment must map the shop to a business (or a pending sign-up) first.
* The token bucket is per process; across processes the 429 path is the limiter. Klaviyo gives OAuth
  apps their own quota per install, Shopify's GraphQL cost header lets each process pace itself.
* `ready_to_act` treats every connection of a business as required; a business that wants to keep
  deciding without a disconnected source must remove the connection.

## Before going live

Everything above was written against the documented shapes and tested against fakes that follow
them (`httpx.MockTransport`). This session could not reach shopify.dev or developers.klaviyo.com
(only search extracts and Klaviyo's public OpenAPI file on GitHub), so the items below must be
checked against the real APIs, in a sandbox, before the first pilot shop is connected. Each item
names where the assumption lives in the code.

### 1. Accounts and sandboxes to set up

| What | Why | Notes |
|---|---|---|
| **Shopify Partner account** (or the Dev Dashboard) | owns our OAuth app and its settings | pilots use *custom distribution* (an install link per store, no App Store review) |
| **Shopify development store** with sample data | the live tests and an end-to-end dry run | add customers with email/SMS consent, orders with discounts and a refund, a voided order, products with types |
| A **legacy custom-app Admin token** (`shpat_...`) on that store | `AUTONOMY_LIVE_SHOPIFY_TOKEN`; the pasted-key path | read scopes; plus `write_discounts`, `write_customers` for the write test |
| **Klaviyo test account** (free) with a verified sending domain and a default sender | setup creates flows that send with the account's own sender | connect Klaviyo's Shopify integration to the development store, so profiles carry Shopify data |
| **Klaviyo OAuth app** (Klaviyo developer settings) | the automatic connect | redirect URI below; scopes = `KLAVIYO_SCOPES` |
| Two private Klaviyo keys on the test account | live tests | one read-only, one with write scopes (events, flows, templates) |
| A test inbox (and phone for SMS) you own | the one live hand-off | `AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL` |
| A public HTTPS host for the API | OAuth callbacks and webhooks | `AUTONOMY_PUBLIC_URL`; a tunnel is fine for the dry run |

### 2. Settings and environment variables

| Variable | Value |
|---|---|
| `AUTONOMY_SHOPIFY_CLIENT_ID`, `AUTONOMY_SHOPIFY_CLIENT_SECRET` | the Shopify app's client id and secret (also the webhook signing secret) |
| `AUTONOMY_KLAVIYO_CLIENT_ID`, `AUTONOMY_KLAVIYO_CLIENT_SECRET` | the Klaviyo OAuth app's credentials |
| `AUTONOMY_PUBLIC_URL` | `https://<host>`, used for callbacks and webhook addresses |
| `MARKETING_AI_CONNECTIONS_KEY` | the Fernet key tokens are encrypted with (v1's key) |
| `AUTONOMY_LIVE_SHOPIFY_SHOP`, `AUTONOMY_LIVE_SHOPIFY_TOKEN` | live read tests on the development store |
| `AUTONOMY_LIVE_SHOPIFY_WRITE=1`, `AUTONOMY_LIVE_SHOPIFY_CUSTOMER_ID` (optional) | also create a 1-day discount code, add and remove a tag |
| `AUTONOMY_LIVE_KLAVIYO_API_KEY` | live read tests on the Klaviyo test account |
| `AUTONOMY_LIVE_KLAVIYO_WRITE_KEY`, `AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL` | live setup plus one real hand-off |
| `MARKETING_AI_TEST_DATABASE_URL` | Postgres for the store tests (CI sets it) |

Run the live tests with
`python3 -m pytest tests/v2/connectors -m live_connector -q -p no:cacheprovider`; each test skips
and names the variables it is missing.

Platform settings, exactly:

* Shopify app URL: `https://<host>/v2/connectors/shopify/install`
* Shopify allowed redirect URL: `https://<host>/v2/connectors/shopify/callback`
* Shopify compliance webhooks (in `shopify.app.toml`, `compliance_topics`, see "Shopify / Webhooks"):
  `https://<host>/v2/connectors/webhooks/shopify`
* Klaviyo redirect URI: `https://<host>/v2/connectors/klaviyo/callback`

### 3. Verify against the real APIs

**OAuth and tokens (base, `oauth.py` / `tokens.py`, `shopify/api.py`, `klaviyo/api.py`)**

- [ ] Klaviyo token endpoint `https://a.klaviyo.com/oauth/token`: HTTP Basic client auth, form body,
  fields `grant_type`, `code`, `code_verifier`, `redirect_uri`, `refresh_token`; PKCE S256;
  `invalid_grant` for a dead refresh token.
- [ ] Klaviyo authorize URL `https://www.klaviyo.com/oauth/authorize`, space-separated scopes, and
  whether a refresh returns a new `refresh_token` (the code keeps the old one when absent). The
  revoke URL `https://a.klaviyo.com/oauth/revoke` (not called yet; see "Still open").
- [ ] Klaviyo refresh rules: access token about 1 h (`expires_in` is read), refresh token revoked
  after 90 days unused (keep-alive every 30 days), at most 10 refreshes a minute.
- [ ] Shopify expiring offline tokens: `expiring=1` in the code-exchange POST (not the authorize
  URL); refresh at `https://{shop}/admin/oauth/access_token` with `client_id`, `client_secret`,
  `grant_type=refresh_token`, `refresh_token`; the response carries `refresh_token` and
  `refresh_token_expires_in`; a dead refresh token is 401 `{"error":"invalid_request"}`; refresh
  tokens rotate and the old one dies at once. Required for public apps by 1 Jan 2027.
- [ ] Shopify OAuth callback and install `hmac`: hex HMAC-SHA256 over the sorted, escaped query
  (`hmac` and `signature` excluded), as in `query_hmac_matches`; install links carry `shop`,
  `timestamp`, `host`, `hmac`; an already-installed app opened from the admin should go straight to
  the app (that is what `/install` does).
- [ ] Dev Dashboard custom apps (the only kind merchants can create since 2026-01-01) reportedly use
  `grant_type=client_credentials` with 24 h tokens. If so, pilots must install through our OAuth app
  (the base does not renew client-credentials tokens yet); legacy `shpat_` tokens still paste.

**Rate limits and versions (`http.py`, `klaviyo/api.py`, `shopify/api.py`)**

- [ ] Klaviyo `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` on 2xx (search says they
  are omitted on a 429, which sends `Retry-After` in whole seconds), and the per-endpoint limits
  copied from the OpenAPI file into `ENDPOINT_LIMITS`.
- [ ] Klaviyo revision `2026-10-15` in the `revision` header (review by 2027-07-15).
- [ ] Shopify Admin GraphQL `2026-10` is stable (else pin `2026-07`); support window behind
  review_by 2027-07-01.
- [ ] Shopify cost: `extensions.cost.{requestedQueryCost, throttleStatus.{maximumAvailable,
  currentlyAvailable, restoreRate}}`, `THROTTLED`, `MAX_COST_EXCEEDED`; compare the live
  `requestedQueryCost` of the orders query with our estimate (about 690 of 1,000 points).

**Shopify reads and bulk (`shopify/api.py`, `normalize.py`, `bulk.py`)**

- [ ] Customer fields: `defaultEmailAddress {marketingState marketingOptInLevel marketingUpdatedAt}`,
  `defaultPhoneNumber {marketingState marketingOptInLevel marketingUpdatedAt marketingCollectedFrom}`
  (and the 2026-10 `smsMarketingConsent`), `numberOfOrders`, `amountSpent`; confirm they need only
  protected customer data level 1.
- [ ] Order fields: `totalPriceSet/subtotalPriceSet/totalDiscountsSet.shopMoney`, `discountCodes`,
  `displayFinancialStatus`, `cancelledAt`, `refunds(first:) {totalRefundedSet}`, `lineItems
  {originalUnitPriceSet discountedTotalSet product{productType} variant{id}}`;
  `Product.priceRangeV2.minVariantPrice`.
- [ ] Search syntax `updated_at:>='...Z' AND updated_at:<'...Z'` for live and bulk queries.
- [ ] Bulk: `bulkOperationRunQuery(query:, groupObjects: true)` (default false since 2026-01) with
  each child line after its parent; `bulkOperation(id:)`; `LIMIT_REACHED`, `OPERATION_IN_PROGRESS`;
  the concurrent bulk limit per shop (three history streams may run at once); the result URL's
  expiry; Range requests on the result host.

**Shopify webhooks and privacy (`webhooks.py`, `shopify/connector.py`, `shopify/privacy.py`)**

- [ ] Headers: `X-Shopify-Hmac-Sha256` (base64 over the raw body with the client secret),
  `X-Shopify-Event-Id` (same across retries and subscriptions; our dedupe key with the topic),
  `X-Shopify-Webhook-Id` (fallback), `X-Shopify-Triggered-At`, `X-Shopify-Shop-Domain`,
  `X-Shopify-Topic`; whether app-config compliance webhooks carry `X-Shopify-Event-Id`.
- [ ] Subscriptions: `webhookSubscriptionCreate(topic:, webhookSubscription: {uri, format: JSON})`,
  `webhookSubscriptionUpdate(id:, webhookSubscription: {uri})`, `webhookSubscriptions { nodes { id
  topic uri } }`; whether per-shop subscriptions are still removed after 8 failed tries over about
  4 h (why we re-register daily and poll hourly).
- [ ] Compliance payloads: `customers/data_request`, `customers/redact` (`orders_to_redact`),
  `shop/redact` (about 48 h after `app/uninstalled`); declared in `shopify.app.toml`, not per shop.
- [ ] REST bodies: `email_marketing_consent` / `sms_marketing_consent {state, opt_in_level,
  consent_updated_at}`, `total_price_set.shop_money`, `presentment_currency`,
  `refunds[].transactions[{kind, status, amount, amount_set?}]`.

**Shopify writes (`shopify/writes.py`)**

- [ ] `DiscountCodeBasicInput.context {customers: {add: [gid]}}` / `{all: ALL}`, `customerGets.value
  {percentage}` or `{discountAmount {amount appliesOnEachItem}}`,
  `minimumRequirement.subtotal.greaterThanOrEqualToSubtotal`, `combinesWith`; duplicate code
  `TAKEN`; `codeDiscountNodeByCode(code:)`; `tagsAdd` / `tagsRemove`.
- [ ] `currentAppInstallation.app.apiKey` equals our client id for an OAuth install (how a legacy
  custom-app token is detected and polled only).

**Klaviyo reads, setup and sending (`klaviyo/mapping.py`, `setup.py`, `channels/klaviyo.py`)**

- [ ] Built-in metric names: `Received Email`, `Opened Email`, `Clicked Email`, `Bounced Email`,
  `Marked Email as Spam`, `Unsubscribed` / `Unsubscribed from Email Marketing`, `Dropped Email`,
  `Received SMS`, `Clicked SMS`, `Failed to Deliver SMS`, `Unsubscribed from SMS Marketing`; their
  properties `$flow`, `$message`, `URL`, `Bounce Type`, `Reason`.
- [ ] Whether a profile's `updated` moves when only its subscriptions change (else a consent-only
  change waits for the next profile update or the history lane).
- [ ] **Identity (the biggest open risk):** whether Klaviyo's Shopify integration puts the Shopify
  customer id in the profile's `external_id`. Community threads from 2022-2023 suggest it does
  not. If not, the default identity `auto` will not meet Shopify's ids: the watch holds and raises
  `connector.klaviyo_unlinked` instead of writing false FAILs, but linking needs a customer-id to
  email map before go-live.
- [ ] A `backfill: true` Create Event still creates the custom metric (setup falls back to one
  plain event after 30 minutes if not).
- [ ] Template tags: `{{ event.body|linebreaks }}`, `{% unsubscribe %}`, `{% unsubscribe_link %}`,
  `{{ organization.name }}`, `{{ organization.full_address }}`; whether event properties are
  HTML-escaped automatically.
- [ ] The flow trigger filter on the metric property `channel`; `reentry_criteria {duration: 0,
  unit: 'alltime'}` allows re-entry; whether some accounts need Klaviyo's approval before an
  API-created flow can go live (the 4xx path already falls back to the guided steps).

### 4. The dry run (development store + Klaviyo test account)

1. Connect Shopify through OAuth from the Connect screen; watch the backfill reach "Up to date".
2. Connect Klaviyo through OAuth; `GET /v2/connectors/connections/{id}/setup` should say `ready`
   with no person (metric, template and live `Autonomy · email` flow exist in Klaviyo).
3. Place an order, refund part of it, cancel an unpaid one, change a customer's consent: each
   should land once (webhook, then the hourly poll) with the same event ids.
4. Hand one decision to the test inbox and see `Received Email` link back as `DELIVER`.
5. Revoke the Klaviyo app in Klaviyo: exactly one "needs reconnect" alert, sends fail over to SES,
   reconnect resumes the same connection id.
6. Uninstall the Shopify app: one alert; `shop/redact` 48 h later is still recorded and executed.

### 5. App review

**Shopify.** Pilots: custom distribution in the Partner Dashboard (one install link per store, no
App Store review). Before any of that:

1. Protected customer data: in the app's API access settings, request **level 1** access (we read
   no name, email, phone or address) and answer the data-protection questions (purpose, retention,
   encryption at rest, staff access, deletion on redact).
2. Request **`read_all_orders`** with the reason "two-year purchase history to measure repeat
   behaviour"; until it is granted, history is the last 60 days.
3. Deploy the compliance webhooks with `shopify app deploy` from the `shopify.app.toml` snippet.
4. Use expiring offline tokens (already on) and confirm installs redirect straight into OAuth.
5. Shopify's Feb 2026 partner terms bar training AI/ML models on merchant data without consent.
   Per-shop decisions are understood to be allowed; a cross-merchant model needs a founder or legal
   decision first.
6. For a public App Store listing later: listing copy, screencast, test credentials for the
   reviewer, privacy policy URL, and the App Store requirements checklist (installs start from
   Shopify surfaces, the embedded admin experience, billing through Shopify if charged there).

**Klaviyo.** Create the OAuth app in Klaviyo's developer settings with the redirect URI and the
scopes in `KLAVIYO_SCOPES` (accounts, profiles, lists, segments, metrics and templates read; events,
flows and templates write). Check how many accounts an unreviewed app may be installed on; if
pilots exceed it, they connect with a private key meanwhile (`POST /v2/connectors/klaviyo/key`).
For the Klaviyo App Marketplace: submit the listing (description, logo, support contact, privacy
policy), a scope-by-scope justification (flows:write and templates:write create the one send flow),
and a test account for the reviewer.

### 6. Still open after wave 1 (integration, not platform checks)

- The runtime does not yet call `monitor.ready_to_act` before deciding or pass
  `monitor.send_gate(business_id)` to the worker, and the runtime has no `business_id` yet.
- `history_hook` is not yet wired to `rebuild_states`.
- `autonomy/runtime/settings.build_runtime` does not yet build the connector engine or use
  `send_providers`; the production `api/main.py` does not yet attach `ConnectorServices`.
- Proof (`autonomy/proof/rewards.py`) still counts a SEND as sent when a `FAIL`
  `not_delivered_by_tool` exists.
- `ready_to_act` counts a person-disconnected connection as blocking the business for ever.
- `DELETE /connections/{id}` forgets our tokens but does not revoke them at the platform or remove
  the webhooks and Klaviyo flows.
- `Alert` has no `connection_id`, so alerts name the connection in their text only.
- No per-connection webhook secret (legacy Shopify custom-app tokens are poll-only) and no
  client-credentials grant.
- A 403 on one optional stream marks the whole connection `needs_reconnect`.

## Shopify

Package `autonomy/connectors/shopify/` (`ShopifyConnector`, loaded by `registry.BUILTIN`). Shopify is
read-only for messages: it feeds customers, consent, orders and products, and takes two writes
(discount codes and tags). Messages go out through the customer's email tool. Written against the
shopify.dev docs as of 2026-10-20 (read through search extracts; see "verify" below).

| | |
|---|---|
| API | Admin GraphQL, **pinned to `2026-10`** in the URL (`/admin/api/2026-10/graphql.json`); `ApiVersion.review_by` 2027-07-01. All queries live in `shopify/api.py`, so the quarterly bump reads one file |
| Auth | `X-Shopify-Access-Token`. Public app: OAuth, offline **expiring** tokens (`expiring=1` in the code exchange, 1 h access, 90-day rotating refresh, client id and secret in the body). Pilot before app review: a custom-app Admin API token through `connect_api_key(..., config={"shop": "acme.myshopify.com"})`, never refreshed |
| Scopes | `read_customers, write_customers` (tags only), `read_orders, read_all_orders` (older than 60 days; a Partner app must request it), `read_products, write_discounts` |
| Protected data | The queries read no name, email, phone or address, so level 1 protected customer data access is enough. Consent comes from `defaultEmailAddress` / `defaultPhoneNumber` (`marketingState`, `marketingOptInLevel`, `marketingUpdatedAt`) |
| Rate limit | Cost-based: `pacing` = `graphql_throttle_wait` (waits for points before the next query), `retry_signal` = `graphql_throttled` (a `THROTTLED` 200 waits for the restore time). Page sizes keep each query's requested cost (2 + `first` x node cost, multiplied through nesting) under 80% of the 1,000-point single-query limit; `test_shopify_mapping::test_every_live_query_stays_under_shopifys_single_query_cost_limit` recomputes it |

### Streams

| Stream | Live lane | History lane | Events |
|---|---|---|---|
| `orders` (critical) | hourly, 2 h overlap, `orders(sortKey: UPDATED_AT, query: "updated_at:>='…' AND updated_at:<'…'")`, 4 per page with 20 line items and 10 refunds each (about 690 points) | 730 days in 30-day bulk exports; ready for decisions after 30 days | `PURCHASE` per order (value = `totalPriceSet.shopMoney`, `item_id`/`category` = the biggest line's product and type, `attrs.offer_id` = first discount code, `discount_codes`, `quantity`, `lines`); `REFUND` per refund at its own time; a `VOIDED`/`EXPIRED` order (cancelled, never captured) keeps its purchase and gets one `REFUND` (refund id `void`) of total less refunds at `cancelled_at`. Times are canonical UTC text, so webhook and polled copies key the same. Test orders and guest checkouts are reported as excluded rows |
| `customers` (critical: consent rides here) | hourly, 100 per page | 10 years in yearly bulk exports; ready after the first year | `SIGNUP` at `createdAt`; `CONSENT_GRANTED` (`SUBSCRIBED`) or `CONSENT_REVOKED` (anything else) per channel, keyed by state and time so each change is kept |
| `products` (not critical) | every 6 h plus the hourly catch-up | one bulk export of everything (20 years) | none: `Product(item_id, name, category, price)` to the `product_sink` given to `ShopifyConnector(product_sink=...)` (the catalogue's upsert) |

The customer id is Shopify's numeric customer id (the id Klaviyo's Shopify integration knows the
profile by). Any window longer than two days is read as a bulk export, including a live catch-up
after an outage.

**Bulk operations** (`shopify/bulk.py`): `bulkOperationRunQuery(groupObjects: true)` starts the
export and the page comes back with `pending_until`. `bulkOperation(id:)` is polled after a quarter
of the export's running time (15 s to 2 min). The JSONL is then paged from the byte offset in the
cursor (`bulk:<op gid>@<offset>~<failures>`) with `download_lines_from`, 500 top-level objects per
page, with line-item lines (`__parentId`) folded back into their order. A shop at its export limit
(`LIMIT_REACHED` / `OPERATION_IN_PROGRESS`) is asked again a minute later. An expired download link
or a vanished operation restarts the window. A failed export is started again twice; the third
failure in a row raises `TransientFailure` (the history lane's error streak, then its one alert), and
the export is tried afresh 6 hours later.

### Webhooks

* The data topics (`orders/create`, `orders/updated`, `customers/create`, `customers/update`,
  `products/update`) and `app/uninstalled` are subscribed per shop by `register_webhooks`
  (`webhookSubscriptionCreate` with `uri`) to `POST /v2/connectors/webhooks/shopify/<connection_id>`.
  The daily job re-registers them idempotently: it creates any that Shopify removed and moves any
  whose address changed.
* The three **mandatory compliance topics** cannot be subscribed per shop. Declare them once in the
  app configuration, pointing at the app-level route `POST /v2/connectors/webhooks/shopify` (the
  shop comes from `X-Shopify-Shop-Domain`):

  ```toml
  [webhooks]
  api_version = "2026-10"
  [[webhooks.subscriptions]]
  compliance_topics = ["customers/data_request", "customers/redact", "shop/redact"]
  uri = "https://<host>/v2/connectors/webhooks/shopify"
  ```
* Verification: `X-Shopify-Hmac-Sha256`, base64 HMAC-SHA256 of the raw body with the app's client
  secret, compared in constant time; a bad signature is answered 401 (Shopify's review checks this).
  Dedupe key: `<topic>:<X-Shopify-Event-Id>`, falling back to `X-Shopify-Webhook-Id`, so a retry and
  a second subscription of the same event are one delivery.
* Webhook bodies are the REST resources and are read by `normalize.*_from_rest`. A webhook copy can
  lack what only GraphQL has (a line's product type, a refund amount when the order was paid in
  another currency), so its version ranks **1 ms below** the polled copy of the same `updated_at`.
  The hourly catch-up poll therefore reads the full copy once. An older copy that arrives late is
  skipped, and the events' stable ids make replays free.
* A **custom-app token connection is polled only**. Its webhooks would be signed with that app's
  own secret, which the base cannot hold per connection. `register_webhooks` sees that the
  installation is not our app (`currentAppInstallation.app.apiKey`) and subscribes nothing, so the
  hourly poll is the delivery path and no webhook warning is raised.

### Writes (`shopify/writes.py`)

| Action | Payload | Idempotency |
|---|---|---|
| `create_discount_code` | exactly one of `percentage` (0-1) and `amount`; optional `customer_id` (limits the code to that customer), `ends_at` or `valid_days` (default 14), `starts_at`, `minimum_subtotal`, `title`, `code_prefix`, `combines_with_shipping` | the code is derived from the idempotency key (`discount_code(key)` = `AUTO-` + 10 base32 characters). It is looked up first (`codeDiscountNodeByCode`), and a `TAKEN` on create is answered by the same lookup. The same decision always ends with exactly one code, and the caller can compute the code without asking Shopify. `usageLimit: 1`, `appliesOncePerCustomer`, no stacking with order or product discounts |
| `tag_customer` / `remove_customer_tags` | `customer_id`, `tags` (e.g. `autonomy-holdout`) | `tagsAdd`/`tagsRemove` are naturally idempotent |

Use them as `connector.write(engine.context(connection, now), "create_discount_code", payload,
idempotency_key=idempotency_key(decision_id, "create_discount_code"))`. `WriteResult.detail` is the
code, and `provider_id` is the discount's gid. Bad input and Shopify's `userErrors` come back as
`accepted=False` with plain words.

### Privacy (`shopify/privacy.py`)

The base records each compliance webhook as a pending `PrivacyRequest`, keeping ids only (an email
or phone is kept as a SHA-256), and raises one alert. `ShopifyPrivacyDesk(store, db,
desk=monitor.desk, erasers=[...]).run_pending(now)` carries the requests out. Wire it with
`privacy_handlers(desk, queue)` and start the hourly chain once with
`enqueue_once(queue, privacy_job(now))`.

* `customers/redact`: deletes the customer's events (all sources), customer state, decisions and the
  connector's record versions, then closes the alert.
* `shop/redact`: deletes the Shopify-sourced events of the shop's customers, their states and
  decisions, and the connection's record versions and positions, then closes the alert.
* `customers/data_request`: exports events, state and decisions as one JSON document
  (`desk.result(request_id)["export"]`, table `shopify_privacy_result`). The alert stays open until a
  person has sent the export to the merchant.

### Tests

`tests/v2/connectors/shopify_support.py` holds `FakeShopify`, which serves the OAuth endpoint,
GraphQL with cost extensions, bulk operations, a Range-capable JSONL host, webhook subscriptions,
discounts and tags, all in documented shapes, through `httpx.MockTransport`. The test files are
`test_shopify_{connect,sync,webhooks,writes,privacy,mapping}.py`.
`test_shopify_live.py` runs against a development store when `AUTONOMY_LIVE_SHOPIFY_SHOP` and
`AUTONOMY_LIVE_SHOPIFY_TOKEN` are set. `AUTONOMY_LIVE_SHOPIFY_WRITE=1` also lets it create a 1-day
discount code, and `AUTONOMY_LIVE_SHOPIFY_CUSTOMER_ID` lets it add and remove a tag.

### Known limits and things to verify against the live docs

* Not read: abandoned checkouts and storefront behaviour (views, carts need a Web Pixel), deleted
  customers or products (`customers/delete`), order edits beyond refunds, inventory cost (margin).
* A webhook-made purchase that arrives before the poll keeps its event as first written. Events are
  immutable, so its `category` stays empty when REST lacked the product type; `item_id` is
  always set.
* Live orders read 30 line items each. An order with more is marked `line_items_complete=False` in its
  record (its purchase is still whole), and history exports read every line.
* Dev Dashboard custom apps (the only kind merchants can create since 2026-01-01) are reported to use
  the client-credentials grant with 24 h tokens. The base's `TokenManager` refreshes only with a
  refresh token, so such an app has to install through our OAuth app until the base learns
  `grant_type=client_credentials`. Legacy admin-created custom-app tokens (`shpat_…`) work as
  pasted keys.
* Shopify's Feb 2026 partner terms bar training AI or ML models on merchant data without consent.
  Per-shop inference is understood to be allowed. A cross-merchant model needs a decision first.
* Check before launch: the 2026-10 field names used (`defaultEmailAddress.marketingUpdatedAt`,
  `defaultPhoneNumber.marketingCollectedFrom`, `numberOfOrders`, `bulkOperation(id:)`,
  `groupObjects`, `webhookSubscriptionCreate(webhookSubscription: {uri})`,
  `DiscountCodeBasicInput.context {customers {add}} / {all: ALL}`,
  `currentAppInstallation.app.apiKey`). Also check the bulk concurrency limit for 2026-10, and
  whether app-config compliance subscriptions carry `X-Shopify-Event-Id`.

## Klaviyo

Package `autonomy/connectors/klaviyo/` (`KlaviyoConnector`, loaded by `registry.BUILTIN`) reads
profiles, consent and engagement. The channel adapter `autonomy/channels/klaviyo.py` hands decided
messages to the shop's own Klaviyo to send. Written against **Klaviyo's own OpenAPI document for
revision 2026-10-15** (`openapi/stable.json` in github.com/klaviyo/openapi, read 2026-10-20). The
shapes, filters, page sizes, scopes and per-endpoint limits below come from it. OAuth and the names
of the built-in metrics are not in the spec; they are under "verify" below.

| | |
|---|---|
| API | JSON:API at `https://a.klaviyo.com/api`, **`revision: 2026-10-15`** on every request (`klaviyo/api.py: API_REVISION`, `review_by` 2027-07-15). Media type `application/vnd.api+json` |
| Auth | Public app: OAuth with PKCE S256. Authorize at `https://www.klaviyo.com/oauth/authorize`; tokens at `https://a.klaviyo.com/oauth/token` with HTTP Basic client auth and a form body. The 1 h access token is refreshed 5 min early. A keep-alive refresh runs every 30 days, because refresh tokens die after 90 days unused, and refreshes are at most one per 10 s (Klaviyo allows 10 a minute). `invalid_grant` → `needs_reconnect`. A 401 from the token endpoint is `invalid_client`: our app's credentials, an error streak for an operator, not a reconnect. Pilots: `connect_api_key(...)` with a private key, sent as `Authorization: Klaviyo-API-Key <key>` and never refreshed |
| Scopes | `accounts:read profiles:read lists:read segments:read metrics:read events:read events:write flows:read flows:write templates:read templates:write` |
| Rate limits | Per endpoint, a burst (1 s) and a steady (1 min) window per account. OAuth apps get their own quota per install; a private key shares the account's. `EndpointPacer` keeps a pair of token buckets per (connection, endpoint), sized so that no fixed second or minute can overflow: events 350/s and 3,500/min, profiles, lists, segments and templates 75/s and 750/min, metrics 10/s and 150/min, flows read 3/s and 60/min, flow create 1/s and 15/min, accounts 1/s and 15/min. A wait over 30 s raises `RateLimited(retry_at)`, so the job is re-queued. On top of that, the base client follows `RateLimit-*` and honours 429 `Retry-After`. `channels.klaviyo.provider_rates()` gives the gateway's `RateLimiter` the same Create Event limit |
| Webhooks | None: Klaviyo has no webhooks for this data. Every stream is polled hourly with an overlap, and that poll is also the catch-up |

### Streams

| Stream | Endpoint | Live lane | History lane | Events |
|---|---|---|---|---|
| `consent` (critical) | `GET /api/profiles?additional-fields[profile]=subscriptions` | hourly, 2 h overlap, `and(greater-than(updated,…),less-than(updated,…))`, 100 per page, `sort=updated` | 10 years in 90-day windows (consent given long ago still counts); ready after 30 days | `SIGNUP` at `created` (with `country`, `timezone`); per channel `CONSENT_GRANTED` (`SUBSCRIBED` and can receive) or `CONSENT_REVOKED` (`UNSUBSCRIBED`, or any suppression, with the reason, e.g. `HARD_BOUNCE`). `NEVER_SUBSCRIBED` grants nothing unless the connection says `email_consent=can_receive`. SMS always needs `SUBSCRIBED` |
| `engagement` (critical) | `GET /api/events?filter=and(equals(metric_id,"…"),greater-or-equal(datetime,…),less-than(datetime,…))&include=profile`, one pass per message metric | hourly, **3 h overlap** (Klaviyo processes events asynchronously), 200 per page | 180 days in 7-day windows | Received → `DELIVER`, Opened → `OPEN`, Clicked → `CLICK` (`url`), Bounced → `BOUNCE` (`bounce_type`), Marked as Spam → `COMPLAINT`, Unsubscribed → `UNSUBSCRIBE`, Dropped Email / Failed to Deliver SMS → `FAIL` (`reason`), for email and SMS. Events from Autonomy's flows carry `via_autonomy` and the decision id |
| `events` (not critical) | as above, one pass per browsing metric | hourly | 90 days | Viewed Product → `VIEW` (`item_id`, `category`), Added to Cart / Started Checkout → `CART`, Active on Site → `VISIT` |

Events are read **per metric id**, so the shop's other metrics are never downloaded. `Placed Order`
is deliberately not read, because orders come from Shopify and Klaviyo's copy would double the
revenue. The page cursor is `<metric id>|<links.next>`, and the metric list (`GET /api/metrics`,
cached for 6 h) is walked in id order, so a window resumes inside the metric it stopped in. A
cursor Klaviyo no longer accepts (400/404/410) restarts the window; ids deduplicate the repeat.
Unknown fields are ignored. A record without `id` or `attributes` counts toward drift. A profile
with nothing usable yields only what it has: it never invents consent.

**Which customer** (`identity` in the connection's config): `auto` (the default) takes the
profile's `external_id` first, then the lower-cased email, then the phone, then `klaviyo:<profile
id>`. Klaviyo's Shopify integration keys Shopify-synced profiles by the Shopify customer id, which is
the id the Shopify connector uses, and the Shopify connector reads no email. `email` skips the
external id; `profile_id` always uses Klaviyo's id. Choose one per deployment; it is in every event id.

### Send through Klaviyo (decide here, send there)

1. The dispatcher/gateway applies holdout, consent, the envelope, quiet hours, rate limits and
   exactly-once, then calls `KlaviyoChannelAdapter(engine, connection_id, channel=EMAIL|SMS,
   details=details_from(decision_log, workspace, discount_code=...)).send(message)`.
2. The adapter creates **one event** of the custom metric **`Autonomy Decision`** on the person's
   profile (`POST /api/events`). The profile is the email or phone the address book gave or, when
   the address book gave the customer id, `external_id`. The properties are flat, so the template
   reads them and the shop can segment on them: `decision_id, message_id, customer_id, channel,
   subject, body, asset_url, send_at` (the intended send time), `handed_at, goal_id, content_id,
   offer_id, offer_label, offer_kind, offer_amount, discount_code, follow_up_step, item_id,
   autonomy_schema=autonomy.klaviyo.v1`.
3. **`unique_id` = the decision id.** Klaviyo records only the first event per (profile, metric,
   unique_id), so the POST carries an idempotency key and the base client retries it after a
   timeout. A retried job never sends twice (test: `test_a_retry_after_a_lost_answer_is_recorded_once_by_klaviyo`).
4. Autonomy's flow **`Autonomy · email`** (or **`Autonomy · SMS`**) sends it. The trigger is the
   metric, with a trigger filter `channel equals email|sms`. The message is live with Smart Sending
   off (Autonomy already paces each person, and Smart Sending would silently skip decided sends).
   SMS keeps quiet hours on and adds the opt-out language. The subject is `{{ event.subject }}`, and
   the template `Autonomy · decision email` (CODE) renders `{{ event.body|linebreaks }}`, the
   discount code, `{% unsubscribe %}` and the organisation's address. Sender, footer, unsubscribes
   and suppressions all stay Klaviyo's.

Receipts use the prefixes the gateway acts on. `invalid:` (a 4xx, e.g. a malformed email) is never
retried or failed over. `ambiguous:` (a timeout after the POST may have arrived) is retried on
Klaviyo and never failed over, which is safe because of `unique_id`. `not_ready:` means the
channel's flow is not live yet, or the connection is not active. `needs_reconnect:` means access is
gone: the connection is marked and its one alert raised through `engine.tokens.on_revoked`.
`rate_limited:` carries the time to come back. `not_ready:`, `needs_reconnect:` and `rate_limited:`
are definite refusals, so a gateway with SES as the second provider falls back to our own sending
for them. The adapter refuses rather than hands a message to a flow that does not exist.

### Setup: automatic, with exact guided steps where Klaviyo refuses

`setup.ensure_send_setup` runs at connect (`identify`, best effort: connecting never fails because
of it), before each send (cached: 1 h when ready, 10 min otherwise), and in the hourly
`klaviyo_delivery_watch` job:

1. **Metric.** A custom metric exists once an event of it does. If `Autonomy Decision` is missing,
   one setup event is created with `backfill: true` ("recorded but does NOT trigger flows"). It goes
   on a profile that has only `external_id=autonomy-setup`, so it is never messageable or billed
   as active, and its `unique_id` keeps it to one. Klaviyo processes it asynchronously, so the state
   is `pending` until a later check sees the metric. If the setup profile is over 30 minutes old and
   the metric is still missing, a backfill event did not create the metric, and one plain event
   (`unique_id` `autonomy-setup-metric-2`) is sent. It is safe: no flow can trigger on a metric that
   does not exist yet, and the profile has no address. The setup profile is never mapped as a
   customer.
2. **Template.** It is found by name, or created as a CODE template.
3. **Flow per channel.** It is found by name (`equals(name,…)`, not archived), or created with
   `POST /api/flows`. If the new flow comes back as a draft it is set live (`PATCH /api/flows/{id}`
   status `live`). An existing flow in `draft` (ours, never switched on) is set live. A flow a person
   set to `manual` is **left alone** and reported: turning it back on is their call. The definition
   sets `reentry_criteria` to `{"duration": 0, "unit": "alltime"}` (re-entry allowed, per the spec),
   so a person's second decision is not dropped by a once-only default. Two live flows under our
   name would each send every decision, for example after a create that timed out once Klaviyo had
   stored it. So the first live one by id is kept and the others are set to `draft`. Checks for one
   connection also run one at a time in each process.

The result is `ready`, `pending` or `guided`. Klaviyo refusing part of it gives `guided`, with the
exact one-time steps in Klaviyo's UI (`setup.guided_steps`): a 4xx on Create Flow, a missing scope
(403, which does **not** mark the connection for reconnect, since reads still work), no default
sender (or set `from_email` / `from_label` in the config), or an account that cannot create live
flows. `ensure_klaviyo_setup` raises **one** alert carrying those steps. Its id is stable per
problem (`SetupStatus.fingerprint`), and alert stores ignore a repeated id, so the hourly check
never repeats it. Once a person has built the flow by hand under the documented name, the next
check finds it and sending resumes.

### Delivery read-back and silent drops (keeping the holdout honest)

Klaviyo does not copy a trigger event's properties onto the message events of the flow it starts,
so a "Received Email" cannot name our decision. It does carry `$flow`. The engagement reader marks
events from Autonomy's flows, whose ids it finds by the name prefix `Autonomy ·`, cached for an hour.
`EventStoreHandoffs` (pass it as `KlaviyoConnector(handoffs=EventStoreHandoffs(event_store))`)
links each such event to the decision handed over most recently for that customer and channel,
looking back 3 days with 5 min of clock skew. The shop's own campaigns and flows are never linked.

`KlaviyoDeliveryWatch(engine, events, connection_id).check(now)` (hourly, in the watch job) looks at
every hand-off (a SEND whose `provider_message_id` is `klaviyo:<connection>:<decision>`) older than
its window: 6 h for email, 18 h for SMS, because an SMS handed over in the evening waits for quiet
hours. If the hand-off has no `DELIVER`, `FAIL`, `BOUNCE` or `BLOCK`, and the engagement stream has
been read past the window (live watermark minus the 3 h overlap), the watch records
**`FAIL` with `reason=not_delivered_by_tool`** at the window's end. Causes include a suppression, a
flow filter, a flow someone switched off, or quiet hours that ran out. The watch never concludes
while engagement data is stale (`held` says why). It also holds when the connector has no
`handoffs` lookup, and when hand-offs are overdue but **none** in the lookback was delivered.
That happens when Klaviyo's profiles do not carry Autonomy's customer ids, so nothing links, or
when the flows never send. A false "not delivered" would take real sends out of the proof. Once
3 hand-offs are held this way it raises one alert, `connector.klaviyo_unlinked`. Klaviyo's own `Dropped Email` / `Failed to
Deliver SMS` count at once. A late `Received` still lands as `DELIVER`, and learning already
prefers `DELIVER` over `FAIL`.

### Wiring

```python
registry = production_registry(event_store)          # KlaviyoConnector(handoffs=EventStoreHandoffs(...))
engine = ConnectorEngine(registry=registry, ...)      # as in "Wiring" above
providers = send_providers(engine, business_id, fallback={Channel.EMAIL: ses_adapter},
                           details=details_from(decision_log, workspace, discount_code=codes))
# -> {EMAIL: (Provider("klaviyo", KlaviyoChannelAdapter(...)), Provider("email", ses_adapter))}
limiter = RateLimiter(provider_rates=provider_rates("klaviyo"))
handlers = connector_handlers(engine, queue, events=event_store)   # includes klaviyo_delivery_watch
after_connect(connection, queue, now)                 # the API does this; it seeds the watch once
```

Per-connection config keys: `identity` (`auto` | `email` | `profile_id`), `email_consent`
(`subscribed` (the default) | `can_receive`), `send` (`on` | `off`: off creates nothing in Klaviyo),
`sms` (`on` creates the SMS flow too), `from_email`, `from_label`.

### Guarantees and the tests that hold them

| Guarantee | Test (`tests/v2/connectors/`) |
|---|---|
| Revision pinned on every call; OAuth PKCE + Basic; key header for private keys | `test_klaviyo_connector::test_oauth_connect_uses_pkce_and_basic_client_auth_and_identifies_the_account`, `::test_a_private_key_is_sent_as_klaviyo_api_key_and_never_refreshed` |
| Refresh before the hour; keep-alive before 90 days; revoked → one reconnect alert | `::test_tokens_refresh_before_the_hour_and_keep_alive_before_ninety_days`, `::test_a_revoked_grant_marks_needs_reconnect_with_exactly_one_alert`, `test_klaviyo_channel::test_revoked_access_while_sending_asks_for_a_reconnect_once` |
| Consent per channel, suppressions revoke, implied consent only by choice; re-reads free | `::test_profiles_become_signups_and_per_channel_consent`, `::test_implied_email_consent_is_an_explicit_per_connection_choice`, `::test_a_reread_is_free_and_a_consent_change_is_a_new_event` |
| Engagement per metric with resumable cursors; no Placed Order; history in chunks | `::test_engagement_reads_each_message_metric_through_cursor_pages`, `::test_engagement_history_backfills_in_weekly_chunks` |
| 429 `Retry-After` honoured or parked; expired cursors restart; drift tolerated; errors secret-free | `::test_a_429_with_retry_after_is_waited_out_and_the_page_still_lands`, `::test_a_long_retry_after_parks_the_job_until_then`, `::test_an_expired_page_cursor_restarts_the_window_without_duplicates`, `::test_schema_drift_is_tolerated_and_counted`, `::test_errors_are_secret_free` |
| Per-endpoint burst/steady limits never overflowed | `::test_the_endpoint_pacer_keeps_each_endpoint_inside_its_burst_and_steady_limits`, `test_klaviyo_channel::test_gateway_rates_match_klaviyos_create_event_limits` |
| Flow, template and metric created by API with no person; idempotent; person's pause respected; guided steps + one alert | `test_klaviyo_setup` (all) |
| One hand-off per decision with everything the template needs; holdout never reaches Klaviyo; retries recorded once | `test_klaviyo_channel::test_a_decision_is_handed_to_klaviyo_once_with_everything_the_template_needs`, `::test_holdout_customers_never_reach_klaviyo`, `::test_a_retry_after_a_lost_answer_is_recorded_once_by_klaviyo` |
| Nothing handed to a flow that does not exist; refusal codes the gateway understands | `::test_nothing_is_handed_over_before_the_flow_is_live`, `::test_refusals_carry_the_codes_the_gateway_acts_on` |
| Engagement from our flow linked to the decision; the shop's own not linked; silent drops become not delivered, only on fresh data, once | `::test_engagement_from_our_flow_is_linked_to_the_decision_and_counts_as_delivered`, `::test_with_shopify_customer_ids_the_profile_is_named_by_external_id_and_engagement_links_back`, `::test_a_silent_drop_becomes_not_delivered_but_only_once_engagement_is_fresh`, `::test_klaviyos_own_drop_report_counts_without_waiting` |

Live tests (`test_klaviyo_live.py`, `@pytest.mark.live_connector`). `AUTONOMY_LIVE_KLAVIYO_API_KEY`
(read scopes) reads one page of every stream. `AUTONOMY_LIVE_KLAVIYO_WRITE_KEY` plus
`AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL` (a Klaviyo **test account** and an inbox you own) runs the setup
and hands one decision over. With a write-capable key the base `test_live_klaviyo_sync` also runs
the connect-time setup.

### Needed outside this package, and known limits

* **Proof must not count a silent drop as sent.** `proof/rewards.py` counts a decision as sent when
  it has a SEND or a DELIVER. A hand-off is a SEND, so it should also exclude decisions with a
  `FAIL` (`reason=not_delivered_by_tool`, or any FAIL/BOUNCE/BLOCK) and no `DELIVER`, as
  `learning/observations.Timeline.undelivered` already does. No contract change is needed:
  existing event types carry it.
* `Alert` has no `connection_id`, so the setup alert names the connection in its message.
* The watch scans the event store for the lookback window (2 days plus the longest window) every
  hour. That is fine for pilots; a big store wants an index on (type, source) or a query by
  provider id.
* Engagement is linked to the **latest** hand-off for that customer and channel. If two hand-offs
  land within minutes and the first is dropped, the second's Received can be credited to the
  first. That is rare, because Autonomy paces each person.
* Lists and segments are listed (`KlaviyoConnector.audiences`), not synced as membership.
* The Klaviyo setup profile (`external_id=autonomy-setup`) and the `Autonomy ·` flows and
  template stay in the customer's account after a disconnect.
* Verify against the live docs: the OAuth details (Basic header, whether a refresh returns a new
  refresh token, `invalid_grant`); the names of the built-in metrics (`Received Email`, `Opened
  Email`, `Clicked Email`, `Bounced Email`, `Marked Email as Spam`, `Unsubscribed` / `Unsubscribed
  from Email Marketing`, `Dropped Email`, `Received SMS`, `Clicked SMS`, `Failed to Deliver SMS`,
  `Unsubscribed from SMS Marketing`) and their properties (`$flow`, `$message`, `URL`, `Bounce
  Type`); whether a profile's `updated` moves when only its subscriptions change; template tags
  (`linebreaks`, `{% unsubscribe %}`, `{% unsubscribe_link %}`, `organization.full_address`);
  that Klaviyo's Shopify integration sets `external_id` to the Shopify customer id; that the
  metric-property trigger filter uses the field `channel` as written; and whether accounts need
  Klaviyo's approval before a flow created by API can go live.
