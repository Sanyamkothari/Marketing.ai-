# Connectors: how competitors integrate, build vs buy, and what "hands-off" takes

Research date: 20 Oct 2026. Scope: the ~10 connectors in `01_market_share.md` §8 (Shopify, WooCommerce,
Klaviyo, Mailchimp, HubSpot, Stripe, WhatsApp Cloud, Twilio, a Segment-spec event API, warehouse SQL)
and how they should run with no human help after a one-time connect.

**How to read this.** The page fetcher could not reach most vendor sites in this session, so vendor
facts come from search-engine extracts of the linked pages. Licences were checked directly from the
source files on GitHub (marked **checked**). Marketing claims (onboarding time, lift) are the vendor's
own words. Re-check prices before quoting them.

---

## 1. Bottom line

1. **Every serious "decisioning layer" sends through the customer's existing tool, not its own.**
   OfferFit/Braze, Hightouch, Aampe and Movable Ink all decide, then trigger a send in Braze, SFMC,
   Klaviyo, Iterable, MoEngage etc. They get engagement back from that tool's export (Braze Currents)
   or from the warehouse. The platforms (Klaviyo, Braze, MoEngage, CleverTap, Insider, Bloomreach,
   Optimove) own both sending and data. We already support both shapes; we should keep both.
2. **Licences rule out "just embed Airbyte for free" less than people think, but they do matter.**
   Airbyte connectors, PyAirbyte, Nango and RudderStack are all **Elastic License 2.0** (checked).
   ELv2 lets us use them inside our product; it forbids offering *them* as a hosted service.
   dlt and its verified sources are **Apache 2.0** (checked). Singer's official `singer-io` taps are
   **AGPL-3.0** (checked), which we should avoid.
3. **For a 2-person team: build the runtime and the write side ourselves, borrow extraction code,
   and partner for warehouses.** Write-back (decision → ESP) is our core loop and a few endpoints per
   platform; no unified API covers it well. Read-side extraction is commodity: use dlt as a library
   where it has a source, and write the rest by hand against our existing `Connector` and
   `EventMapping` contracts. Buy an embedded platform (Airbyte Embedded or Nango) only when the long
   tail starts costing more than ~1 engineer-week a month.
4. **"Hands-off" is mostly not about connectors, it is about the runtime around them**: a token vault
   that refreshes and notices revocation, a cursor per stream, a reconciliation poll behind every
   webhook, schema-drift checks, and alerts on *freshness and row counts*, not just HTTP errors.
   We have about half of this already (§5).

---

## 2. How comparable AI decisioning products integrate

| Product | What it connects to | Sends itself or via the customer's tool? | How engagement comes back | Onboarding claimed |
|---|---|---|---|---|
| **BrazeAI Decisioning Studio** (was OfferFit; Braze paid $325M, 2025) | Braze, SFMC and Klaviyo as supported engagement platforms; data from Braze Data Platform / Cloud Data Ingestion, cloud storage, or SFMC/Klaviyo data extensions. Other platforms "may require custom engineering" | **Via the customer's tool.** Klaviyo setup: a placeholder flow and template, triggered by an event the Go portal names; key needs full access to Profiles, Events, Flows | Braze: **Currents must be on** (including the business-as-usual comparison campaigns); audiences pulled each morning via `/users/export/segment`; no PII accepted | Not published. Vendor says "years of learning in a few weeks"; data-hungry for small bases |
| **Hightouch AI Decisioning** (launched fall 2024) | Reads the customer's warehouse; purpose-built send integrations for **Braze, Iterable, SFMC** (Klaviyo not listed for AID); "custom destinations" via the customer's own middleware (Lambda etc.) | **Via the customer's ESP.** Message copy stays in the ESP and is only referenced. "Never stores your data" | Behavioural events (purchases, clicks, opens) read **from the warehouse**; the ESP's exports land there first | "Within weeks of launch" at two retailers; WHOOP saw lift "within 6 weeks" |
| **Aampe** | Message providers: Braze, Customer.io, MoEngage, CleverTap, OneSignal and others; data via daily dumps to a GCP bucket Aampe creates (JSON/NDJSON/CSV/Parquet, loose schema), or an Amplitude/CDP event stream | **Via the customer's provider APIs** (e.g. calls MoEngage Push API / Customer.io API with the customer's key). Stores no PII | Event dumps / streams from the customer's CDP or warehouse | Warehouse feed ~1 day; MoEngage hookup ~15 min; theCut case study "~8 hours of total work" |
| **Movable Ink Da Vinci** | Braze, Zeta, Epsilon, Iterable natively; SFMC, Adobe, Responsys etc. as partners | **Via the ESP** (Braze Messaging API with `messages.send`; a dedicated Da Vinci app in Braze) | Braze **Currents** export to Movable Ink | Account-team led set-up plus QA; no figure |
| **Persado** | SFMC, Braze (campaigns and canvases), Adobe Campaign; "20+ ESP integrations", generic API | **Via the ESP**; Persado supplies the copy | Campaign results flow back through the same integrations | Self-serve "Essential" tier with 30-day trial; enterprise is services-led |
| **Optimove** | Data via SFTP, cloud storage or a **Snowflake share** (daily batch plus real time); Braze for execution; own email (formerly Optimail) | **Both**: own email, plus executes through Braze (syncs campaigns every 30 min, pushes attributes via `/users/track`) | Daily ETL / Snowflake share; Braze events | Solution-engineer led |
| **Bloomreach (Loomi AI)** | Shopify app via Data Hub plus web SDK; many native channels | **Sends itself** | Real-time sync for most Shopify data; products without webhooks once a day | Connect "under 10 minutes"; full use ~1 week–1 month (one case: 27 days) |
| **Insider One (Sirius AI)** | Shopify app; WhatsApp, SMS, email, web, app natively | **Sends itself** | Own SDK and events | Dedicated onboarding team; no figure |
| **MoEngage (Merlin AI)** / **CleverTap** | Own SDKs and data | **Send themselves.** MoEngage Next Best Action picks channel and best time from a **60-day** lookback; CleverTap Best Time uses 12 two-hour buckets from **180 days** of activity | Native | n/a (feature inside the platform) |
| **Klaviyo AI** | Own data (Shopify etc. integrations) | **Sends itself** | Native | Predictive analytics needs **≥500 customers with orders, ~180 days of history, orders in the last 30 days, some 3+-order customers**. Smart Send Time needs an audience of **≥12,000** |

**What this means for us**

- The pattern "decide here, send there" is proven and is what enterprise buyers trust. Our
  `client_tool` adapter already does this generically; native Klaviyo/HubSpot/Braze write adapters
  are the upgrade.
- The best players **refuse to depend on ESP webhooks for engagement**: Braze-side products require
  Currents (a bulk export), Hightouch and Aampe read a warehouse or a daily dump. For us: pull
  engagement in bulk on a schedule, treat webhooks as a speed-up.
- Onboarding claims in the "hours to a day" range (Aampe) come from API-key connects plus loose
  schemas. Our Data Doctor and `EventMapping` give us the same looseness.
- Klaviyo's own AI switches off below 500 buyers / 12k recipients. **Small Klaviyo stores are a gap
  we can fill**, as long as our proof says honestly when data is too thin.

---

## 3. Build vs buy for connectors

| Option | What it is | Licence / cost | Fit for us (2 people) |
|---|---|---|---|
| **Airbyte connectors** (run via Python CDK or PyAirbyte, no platform) | 600+ sources incl. certified Shopify, Klaviyo, HubSpot, Stripe, Mailchimp, WooCommerce, Salesforce; Braze is community-level | Connectors **ELv2** (checked in each `metadata.yaml`); PyAirbyte **ELv2**; Python CDK **MIT** (checked). Free to embed; may not be resold as a hosted ELT service | Good code to read or run for the long tail. Heavy (Docker images or big Python deps), state/cursor model is Airbyte's. Keep notices if we ship any |
| **Airbyte Embedded** (May 2025) | Hosted widget: end users connect sources, Airbyte handles OAuth/secrets, data lands per tenant | Commercial, quote only | Buy later for the long tail; adds a vendor between us and client data |
| **Airbyte Cloud / OSS platform** | Full ELT platform | OSS platform ELv2; Cloud from ~$10/mo usage-based (third-party figure) | Too much to operate for one product feature |
| **dlt (dltHub)** | Python library: incremental cursors, schema evolution, merge loads; "verified sources" | **Apache 2.0** (library and verified sources, checked) | **Best fit as a library.** Has Shopify, HubSpot, Stripe, Salesforce, GA, Facebook Ads, Zendesk; **no** Klaviyo, Mailchimp or WooCommerce (checked). Its Shopify source uses the REST Admin API, which is legacy |
| **Singer taps / Meltano** | Older tap ecosystem | `singer-io` taps **AGPL-3.0** (checked) – avoid. Meltano SDK and hotglue taps Apache 2.0 | Quality uneven; use only as reference |
| **Nango** | OAuth + token refresh for 200–800 APIs, plus syncs/actions you write in TS | **ELv2** (checked); free self-host with limited features; Cloud free plan 3 connections, Starter 20 connections, sync interval >6h on Starter | Strongest "buy OAuth only" option. Risk: we'd write sync code in their framework anyway |
| **Merge.dev** | Unified API (CRM, ticketing, HR, accounting; a marketing-automation category) | ~$650/mo for up to 10 linked accounts, ~$65 per extra account; enterprise $30–55k/yr (competitor-sourced) | Poor fit: lowest-common-denominator models, no Shopify-style commerce depth, per-account cost grows with every client |
| **Paragon / Hotglue** | Embedded iPaaS (managed sync, workflows) | Quote only; Paragon "five figures a year" (competitor-sourced) | Overkill now |
| **Fivetran (Powered by Fivetran)** | Embedded ELT into a warehouse; owns Census (reverse ETL) since 2025 | Usage (MAR) pricing, $5/connection base, deletes count from 2026 | Wrong shape: lands data in a warehouse, not in our event store |
| **RudderStack** | CDP + reverse ETL | Server **ELv2** (checked); reverse ETL not in open source | Accept its event stream (Segment spec, connector #9), don't embed |
| **Hightouch / Census as partners** | Reverse ETL from the client's warehouse to any ESP | Client pays them | **Partner, don't build** for warehouse clients: we write a `decisions` table, their Hightouch/Census pushes it to Braze etc. Zero ESP code for that segment |

**Licence note (not legal advice).** ELv2's one real limit: "You may not provide the software to third
parties as a hosted or managed service, where the service provides users with access to any substantial
set of the features or functionality of the software." Pulling a client's Shopify data into our own
decision engine is using it, not reselling ELT, which Airbyte's FAQ describes as fine. Get a one-hour
legal read before shipping ELv2 code in the product.

**Why build the write side ourselves.** Write-back is a handful of calls per platform (Klaviyo: upsert
profile properties + create an event that starts a flow; HubSpot: update contact properties + custom
event; Braze: `/users/track` + `/campaigns/trigger/send`; Mailchimp: merge fields + tag). It must carry
our `decision_id`, idempotency key and holdout rules, and none of the unified APIs model "trigger a
flow for this person with these variables" well.

---

## 4. What "runs without human intervention" requires – checklist

**Auth and tokens**
- [ ] Store `expires_at` and refresh **before** expiry, using the `expires_in` each provider returns.
      Known rules: Klaviyo access 1 h, refresh token **revoked after 90 days unused**, max 10 refreshes
      per minute; HubSpot access ~30 min, refresh tokens last until uninstall, with a cap on active
      tokens per app and user; Google refresh tokens die after **7 days if the OAuth app is in "Testing"**
      and after ~6 months unused (so publish the app); Meta long-lived user tokens ~60 days, **system-user
      tokens don't expire** (use those for audiences).
- [ ] A **keep-alive refresh** job for every connection (stops 90-day idle revocation).
- [ ] **One refresher per connection** (a lock). Parallel workers refreshing the same token cause 401 cascades.
- [ ] Classify auth errors: transient → retry; `invalid_grant`/revoked → mark the connection
      `needs_reconnect`, pause dependent goals, email the owner once. Never loop.
- [ ] Prefer non-expiring credentials where offered (Shopify custom-app tokens, Klaviyo/Mailchimp API
      keys, Meta system users).

**Webhooks**
- [ ] Answer 200 within ~5 s, then queue (Shopify and HubSpot treat >5 s as failure).
- [ ] Verify signatures (HMAC) and dedupe on the provider's event id. Delivery is at-least-once.
- [ ] Don't trust order; use the payload's updated-at (Shopify `X-Shopify-Triggered-At`) and keep the later value.
- [ ] Know the retry windows: Shopify **8 attempts over ~4 h, then the subscription is deleted**;
      HubSpot **10 retries over 24 h**; Stripe ~**3 days**, then the endpoint is disabled.
- [ ] **Re-register subscriptions daily** (idempotent) and run a **reconciliation poll** (e.g. hourly
      `updated_at > cursor`) behind every webhook. Shopify staff call this the only reliable safety net.

**Backfill and incremental sync**
- [ ] Persist a cursor per (connection, stream). Advance it only after the batch is committed.
- [ ] Backfill in date chunks (dlt's Shopify source chunks orders weekly), newest first, so decisions can start before history finishes.
- [ ] Use bulk endpoints for history (Shopify GraphQL bulk operations, Braze Currents to S3), APIs for deltas.
- [ ] Re-read a small overlap window each run (e.g. last 2 h) and rely on stable event ids to dedupe.
- [ ] Handle deletes and refunds explicitly (they are not "new rows").

**Schema drift and API versions**
- [ ] Pin API versions (Shopify quarterly versions with ≥12 months support and 9 months to migrate;
      Klaviyo dated revisions). Put a "bump the version" job on the calendar each quarter.
- [ ] New Shopify **public** apps must use GraphQL only (since 1 Apr 2025); REST is legacy.
- [ ] Validate each batch against the expected fields; alert on missing or renamed fields and on
      null-rate jumps. A 200 with reshaped data is the usual silent failure.

**Rate limits**
- [ ] Read limit headers (Klaviyo `RateLimit-Remaining`/`Reset`, Shopify GraphQL `throttleStatus`) and
      pace before you hit 429. Klaviyo: burst (1 s) and steady (1 min) windows per endpoint; OAuth apps
      get their own quota per install, private keys share the account's.
- [ ] Back off on 429/5xx with jitter and honour `Retry-After`. Cap total retries, then park the job.
- [ ] Global concurrency gate per client system (we have one, `engine/connections/limits.py`).

**Idempotent sends and write-back**
- [ ] One idempotency key per decision, sent to the provider where supported (Stripe keeps keys 24 h).
- [ ] Ambiguous errors (timeouts) retry the **same** provider with the same key, never fail over.
- [ ] Write-back is idempotent by design: "set property X = decision_id" is safe to repeat; "create
      event" carries a unique id.

**Monitoring and alerting**
- [ ] Per connection: last successful sync, last token refresh, rows per run vs trailing median,
      lag between source time and ingest time, webhook receipts per hour.
- [ ] Alert on **freshness** (no new orders in N× normal gap) and **volume drops**, not only errors.
      Dashboards can stay green while syncs are dead for days.
- [ ] A stale or broken source must **stop the agent from acting on stale state** (a breaker),
      and the proof must exclude the affected window.
- [ ] Customer-facing status ("Shopify: healthy, 4 min behind") plus one reconnect link.

**Common failure modes integration engineers report:** silent token revocation (uninstall, password
change, admin action); single-use refresh-token races; webhook subscriptions silently removed;
renamed fields without a version bump; expiring pagination cursors on big accounts; changed default
sort order; rate limits changed without notice; syncs that "succeed" with fewer rows.

---

## 5. What we already have, and the gaps

### Reusable now

| Piece | Where | Reuse for |
|---|---|---|
| `Connector` protocol: `info / test / browse / preview / fetch`, `FieldSpec` forms, named test steps (reach, sign in, list, read sample, read-only check) | `engine/connections/base.py`, `registry.py` | The shape of every SaaS source connector and its "Test connection" screen |
| Encrypted credential store (Fernet, key fingerprint, never returned or logged) | `engine/connections/store.py` | Token vault (needs fields for refresh token and expiry) |
| SSRF guard, timeouts, secret-free error text, concurrency gate | `net.py`, `base.plain_error`, `limits.py` | All HTTP connectors |
| Date-window fetch | `SqlConnector.fetch_window` in `engine/connections/sql.py` | Basis for incremental warehouse sync |
| S3, Azure Blob, BigQuery, Snowflake, Postgres, MySQL readers | `engine/connections/*` | Connector #10 as is; **Braze Currents → client's S3 → our S3 connector** for Braze engagement |
| Declarative `EventMapping`, bad rows collected not fatal, **stable event ids** | `autonomy/data/mapping.py` | Every connector outputs rows + a mapping preset; re-syncs dedupe for free |
| Late-event-safe state fold, rebuild/apply | `autonomy/data/folder.py`, `rebuild.py` | Out-of-order webhooks and backfills |
| SQL job queue with `SKIP LOCKED`, `retry_at` | `autonomy/store/queue.py` | Scheduled syncs, backfill chunks, keep-alive refreshes |
| Webhook path: verify → parse → store with dedupe | `api/v2/deps.receive_webhook` | Shopify, Stripe, HubSpot webhooks |
| Idempotent dispatcher (message id from decision id), gateway with idempotency keys, token-bucket rate limits, exponential retry with jitter, ambiguous-error handling, message ledger by provider id | `autonomy/channels/dispatcher.py`, `gateway.py`, `ratelimit.py` | Every write-back adapter |
| Signed webhook / CSV hand-off to the client's tool | `autonomy/channels/client_tool.py` | Generic fallback, and the "decisions table" for Hightouch/Census partners |
| SES, WhatsApp Cloud, SMS (Twilio-style, MSG91) adapters with webhook verification | `autonomy/channels/ses.py`, `whatsapp.py`, `sms.py` | Connectors #7 and #8 are mostly done |
| Circuit breakers (delivery rate, complaints, spend) | `autonomy/channels/breakers.py` | Add a "source stale" rule |

### Gaps

1. **No OAuth at all** (BigQuery uses a service-account key). Need: authorize/callback routes, state
   and PKCE, token refresh, revocation handling, keep-alive.
2. **No continuous sync.** v1 connectors are built for one-off, person-driven imports (500-item
   listings, size-capped fetch). Need per-stream cursors, a scheduler, backfill chunking and a
   reconciliation poll.
3. **No SaaS HTTP source connectors** (Shopify, WooCommerce, Klaviyo, Mailchimp, HubSpot, Stripe) and
   no mapping presets for them.
4. **No native write-back adapters** for Klaviyo, HubSpot, Mailchimp, Braze. `client_tool` expects the
   client's tool to call back in our format, which Klaviyo and others don't do; their engagement must
   be **pulled** (Klaviyo Events API, Braze Currents).
5. **No connection health model**: freshness, row-count drift, `needs_reconnect` state, owner alerts,
   and a breaker that stops decisions on stale data.
6. **No identity linking across sources** (Shopify customer ↔ Klaviyo profile ↔ WhatsApp number);
   the Doctor lists it as not built.
7. **"Connectors only read"** is a v1 promise (`base.py`). Keep it: write-back belongs in
   `autonomy/channels` (P5), not in `engine/connections`.
8. **App distribution work**: Shopify protected customer data (Level 2 for name/email/phone/address)
   and possible review for a public app; Klaviyo apps install before review (with a warning), and
   listing needs 5+ active installs.

---

## 6. Recommendation

**Build ourselves**
- The **connector runtime** (≈2–3 weeks): OAuth + token vault on top of `ConnectionStore`, sync jobs
  on `SqlJobQueue`, cursors, reconciliation polls, health and alerts, "source stale" breaker.
- **Write-back adapters** for Klaviyo, HubSpot, Mailchimp, later Braze: profile properties +
  decision event, carrying `decision_id` and the idempotency key, so the client's flow sends.
- **Shopify** source by hand (GraphQL bulk for history, webhooks + hourly poll for deltas) because it
  is #1 and dlt's REST version is legacy. **Klaviyo, Mailchimp, WooCommerce** read sides by hand
  (dlt has none; Airbyte's ELv2 code is a good reference).

**Borrow (library, no vendor)**
- **dlt** (Apache 2.0) for HubSpot and Stripe extraction, later Salesforce and GA: run inside our
  process, feed rows to `EventMapping`.

**Partner, don't build**
- Warehouse clients: read with our existing SQL connectors, write a `decisions` table, let their
  Hightouch/Census send it to Braze/Iterable/SFMC.
- CDP clients: accept Segment-spec events (RudderStack, Segment, Jitsu).

**Buy later, only on demand**
- **Airbyte Embedded** or **Nango** for the long tail (Brevo, Shopware, Tiendanube, Razorpay, Zoho…)
  once connector upkeep passes ~1 engineer-week a month. Keep them behind our `Connector` interface.
- Skip Merge, Paragon, Hotglue, Fivetran and `singer-io` taps.

**Order**
1. Runtime + Shopify (read) + Klaviyo (read and write). This is the D2C wedge, and covers the stores
   too small for Klaviyo's own AI.
2. Stripe (dlt) and WooCommerce (read); WhatsApp and Twilio go live (adapters exist).
3. HubSpot (dlt read + write), Mailchimp (read + write).
4. Segment-spec event endpoint; `decisions` table for warehouse partners; Braze write + Currents via S3.
5. Region packs and the long tail through an embedded platform.

---

## Sources

**Competitors**
- Braze Decisioning Studio: [overview](https://www.braze.com/docs/user_guide/brazeai/decisioning_studio), [Pro orchestration (Braze, SFMC, Klaviyo)](https://braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_pro/set_up_orchestration), [Go: configuring Klaviyo](https://braze.com/docs/user_guide/brazeai/decisioning_studio/go/configuring_klaviyo), [Go: connect data sources (Currents)](https://braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_go/connect_data_sources/), [Braze integration permissions](https://braze.com/docs/user_guide/offerfit/integration), [connect your data](https://www.braze.com/docs/user_guide/brazeai/decisioning_studio/prepare_data/connect_data_sources), [acquisition](https://www.businesswire.com/news/home/20250327329432/en), [VentureBeat on OfferFit](https://venturebeat.com/business/how-offerfit-uses-self-learning-ai-to-stop-customer-churn)
- Hightouch: [AI Decisioning quick start](https://hightouch.com/docs/ai-decisioning/quick-start), [custom destinations](https://hightouch.com/docs/ai-decisioning/custom-destinations), [what AI decisioning is](https://hightouch.com/blog/what-is-ai-decisioning-what-does-hightouch-do), [2024 review](https://hightouch.com/es/blog/rewind-2024-in-review), [Smart Suppression](https://www.businesswire.com/news/home/20251008017466/en/Hightouch-Launches-Smart-Suppression-to-Protect-Brand-Relationships-Without-Sacrificing-Conversions)
- Aampe: [integration know-how](https://docs.aampe.com/docs/integration-know-how), [data warehouse](https://docs.aampe.com/docs/data-warehouse), [MoEngage provider](https://kb.aampe.com/developer-guide/message-providers/message-pages/moengage), [Customer.io](https://docs.aampe.com/docs/customerio), [theCut case study](https://aampe.com/blog/thecut-integration-case-study)
- Movable Ink: [Da Vinci in Braze docs](https://www.braze.com/docs/partners/message_personalization/dynamic_content/visual_and_interactive_content/movable_ink/da_vinci), [ESP integrations](https://tours.movableink.com/answers/da-vinci-esp-integrations)
- Persado: [integrations](https://www.persado.com/integrations), [Essential Motivation release](https://www.persado.com/press-releases/new-persado-essential-motivation-ai-solution-empowers-marketers-to-instantly-generate-better-performing-messages-across-channels/)
- Optimove: [tech onboarding gateway](https://developer.optimove.com/docs/tech-onboarding-gateway), [data share](https://developer.optimove.com/docs/data-share-overview), [Braze execution](https://academy.optimove.com/hc/en-us/articles/8826846433181-Braze-Integration-Scheduled-and-Real-Time-Campaign-Execution), [OptiGenie](https://www.cmswire.com/the-wire/optimove-introduces-the-new-optigenie/)
- Bloomreach: [Loomi AI for Shopify](https://documentation.bloomreach.com/data-hub/docs/integrate-loomi-ai-for-shopify), [FAQ](https://documentation.bloomreach.com/data-hub/docs/loomi-ai-for-shopify-faq), [Shopify page](https://Bloomreach.com/en/commerce-platforms/shopify/engagement), [Engagement Shopify overview](https://documentation.bloomreach.com/engagement/docs/shopify-and-shopify-plus-overview)
- Insider: [Shopify app](https://apps.shopify.com/insider?locale=da), [rfp.wiki profile](https://www.rfp.wiki/vendors/insider-one)
- MoEngage: [Next Best Action in Flows](https://moengage.com/docs/user-guide/flows-cross-channel-messaging/create/intelligence-in-flows/next-best-action-in-flows); CleverTap: [Best Time](https://docs.clevertap.com/v4.0/docs/best-time)
- Klaviyo: [predictive analytics requirements](https://help.klaviyo.com/hc/en-us/articles/360020919731), [Smart Send Time](https://help.klaviyo.com/hc/en-us/articles/360029794371-Smart-Send-Time-in-Klaviyo)

**Build vs buy**
- Airbyte: [licence FAQ](https://docs.airbyte.com/developer-guides/licenses/license-faq), [repo LICENSE (ELv2)](https://github.com/airbytehq/airbyte/blob/master/LICENSE), connector licences from `metadata.yaml` e.g. [source-shopify](https://github.com/airbytehq/airbyte/blob/master/airbyte-integrations/connectors/source-shopify/metadata.yaml), [PyAirbyte LICENSE](https://github.com/airbytehq/PyAirbyte/blob/main/LICENSE.md), [Python CDK LICENSE (MIT)](https://github.com/airbytehq/airbyte-python-cdk/blob/main/LICENSE.txt), [Airbyte Embedded](https://airbyte.com/embedded-integrations), [announcement](https://airbyte.com/blog/announcing-airbyte-embedded), [pricing summary (third-party)](https://automationatlas.io/answers/airbyte-pricing-explained-2026/)
- dlt: [OSS and dltHub](https://dlthub.com/docs/hub/oss-and-dlthub.md), [Shopify source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/shopify), [Stripe source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/stripe), [verified-sources repo](https://github.com/dlt-hub/verified-sources)
- Singer: [tap-shopify LICENSE (AGPL)](https://github.com/singer-io/tap-shopify/blob/master/LICENSE); Meltano: [SDK LICENSE](https://github.com/meltano/sdk/blob/main/LICENSE)
- Nango: [pricing](https://www.nango.dev/pricing), [LICENSE (ELv2)](https://github.com/NangoHQ/nango/blob/master/LICENSE)
- Merge: [marketing automation API](https://www.merge.dev/categories-old/marketing-automation-api), [pricing analysis (Knit, a competitor)](https://getknit.dev/blog/understanding-merge-dev-pricing-finding-the-right-unified-api-for-your-integration-needs)
- Paragon: [pricing](https://useparagon.com/pricing), [Nango on Paragon pricing](https://nango.dev/blog/paragon-pricing); Hotglue: [pricing listing](https://toolradar.com/tools/hotglue/pricing)
- Fivetran: [Powered by Fivetran](https://fivetran.com/docs/developer-resources/powered-by-fivetran), [Census acquisition](https://www.techtarget.com/searchdatamanagement/news/366623554/Fivetran-adds-reverse-ETL-with-acquisition-of-Census), [2026 pricing notes](https://hevodata.com/learn/fivetran-faqs/)
- RudderStack: [rudder-server LICENSE (ELv2)](https://github.com/rudderlabs/rudder-server/blob/master/LICENSE), [hosted vs open source](https://www.rudderstack.com/docs/get-started/rudderstack-open-source/hosted-vs-open-source/index.md)

**Running without people**
- Shopify: [webhook troubleshooting](https://shopify.dev/docs/apps/build/webhooks/troubleshoot), [retry mechanism changelog](https://shopify.dev/changelog/updates-to-webhook-retry-mechanism), [community answer on reconciliation](https://community.shopify.dev/t/webhooks-retry-after-an-error/24625), [API versioning](https://shopify.dev/docs/api/usage/versioning), [REST is legacy](https://shopify.dev/docs/api/admin-rest), [protected customer data](https://shopify.dev/docs/apps/launch/protected-customer-data)
- Klaviyo: [OAuth](https://developers.klaviyo.com/en/docs/set_up_oauth), [OAuth errors](https://developers.klaviyo.com/en/docs/troubleshoot_oauth_errors), [rate limits](https://developers.klaviyo.com/en/v2026-07-15/docs/rate-limits-and-error-handling), [app review](https://developers.klaviyo.com/en/docs/submit_your_app_for_review)
- HubSpot: [token management](https://developers.hubspot.com/blog/oauth-token-management-hubspot-integrations), [webhook retries (changelog)](https://developers.hubspot.com/changelog/2018-12-10-updated-webhook-retry-logic), [error handling](https://developers.hubspot.com/docs/api-reference/error-handling), [BAD_REFRESH_TOKEN](https://nango.dev/blog/hubspot-oauth-bad-refresh-token)
- Stripe: [Hookdeck guide to Stripe webhooks](https://hookdeck.com/webhooks/platforms/guide-to-stripe-webhooks-features-and-best-practices), [idempotent handlers](https://axonbuild.com/blog/how-to-make-a-webhook-handler-idempotent/)
- Google: [OAuth setup (7-day testing tokens)](https://developers.google.com/health/setup), [refresh token limits](https://www.unipile.com/google-oauth-refresh-token/); Meta: [Marketing API authentication](https://developers.facebook.com/docs/marketing-api/get-started/authentication), [access tokens](https://developers.facebook.com/docs/facebook-login/guides/access-tokens)
- Braze: [Currents S3 to Snowflake](https://www.braze.com/docs/user_guide/data_and_analytics/braze_currents/s3_to_snowflake)
- Failure modes: [Truto: why integrations break](https://truto.one/blog/why-saas-integrations-break-after-launch-root-causes-prevention/), [Truto: token refresh failures](https://truto.one/blog/handling-oauth-token-refresh-failures-in-production-for-third-party-integrations.md), [Apideck: what breaks at scale](https://radar.apideck.com/blog/api-integration-challenges), [Hyperspell: connector maintenance](https://www.hyperspell.com/blog/connector-maintenance-burden)
