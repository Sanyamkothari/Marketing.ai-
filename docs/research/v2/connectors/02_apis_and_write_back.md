# Connector APIs and write-back: can Autonomy "decide here, send there"?

Research date: October 2026. Sources are official developer docs found through web search, with community or vendor posts flagged where the official docs were silent. Direct page fetches were blocked in this environment, so numbers marked **(verify)** came from search snippets or memory and need checking against the live page before we build on them.

## The core question

Autonomy decides who gets what, and when, every day. Then it has to act. The cleanest way to act is to make the customer's own tool send the message, so their unsubscribes, sender reputation, templates and reporting stay in one place. Every platform below was checked for one thing: **can we make it send a specific message to a specific person at a time we choose, with per-person content, from an API call?**

Nearly every ESP needs the same pattern: **a one-time "receiver" set up in the customer's tool** (a flow, journey, canvas or workflow that starts on an API event and renders our per-person fields into a template). After that, each decision is one API call. A few platforms let us create that receiver ourselves through the API. Most need a human to build it once during onboarding, or a guided setup step.

## Comparison table

| Platform | Read | Incremental | Write / act per person | Rate limits | Auth / review | Hardest part |
|---|---|---|---|---|---|---|
| **Shopify** | Customers, orders, products, consent (email/SMS marketing state), discounts. No email engagement (Shopify Email stats aren't exposed). | `updated_at` query filters, bulk operations (JSONL export), webhooks (at-least-once, need reconciliation) | No direct "send" API. Options: tags/metafields that other tools segment on; `flowTriggerReceive` to start a Shopify Flow, which can run a send-marketing-email action **(verify)**; create per-person discount codes. | Cost-based leaky bucket: 100 pts/s Standard, 200 Advanced, 1,000 Plus, 2,000 Enterprise. 1,000 pts max per query. Bulk ops: 5 at a time per shop. | OAuth public app. App Store review is officially 5-10 business days, often 2-4+ weeks in practice. Level 2 protected customer data (name/email/phone/address) needs a separate request and review, with no published timeline. | PCD Level 2 approval, plus the Feb 2026 Partner terms ban on training AI with merchant data. Inference-only use appears allowed. |
| **WooCommerce** | Customers, orders, products, coupons. No consent or engagement (those live in plugins). | `modified_after` on orders/products (WC 5.8+); webhooks auto-disable after 5 failures in a row | None native. Write coupons, customer meta, order notes. Sending goes through whatever ESP plugin they use. | No platform limit. Depends on the host, and many shared hosts throttle. | Per-store REST keys via `/wc-auth/v1/authorize` (merchant clicks approve). No review. | Every store is different: hosting, plugins, caching, security plugins blocking the API. |
| **Klaviyo** | Profiles, events/metrics (placed order, viewed product, received/opened/clicked/bounced/unsubscribed/spam), lists, segments, catalog, consent per channel, campaign/flow reporting | Filter `updated` (profiles) / `datetime` (events) with cursor pagination. Bulk profile import jobs. No outbound webhooks for engagement (flow webhooks only). | **Best in class.** Create event (custom metric) with per-person properties → a metric-triggered flow sends the email/SMS and renders `{{ event.* }}`. Also: set profile properties, add to list (list-triggered flow), or Campaigns API (create + send job to a list/segment, scheduled). | Per-account fixed windows (burst 1s / steady 1m). Tiers XS-XL (XL 350/s, 3,500/m). Create/bulk events about 10/s, 150/m, 1,000 events per bulk call **(verify per endpoint)**. OAuth apps get their own quota per install. | OAuth app. Listing needs 5 real customer installs with API activity, a security review, diagrams and 365-day log retention. Review time not published; community reports of 1+ month. Unlisted OAuth works before listing. | Getting merchants to switch on the receiving flow. Smart Sending and quiet hours can silently skip sends. |
| **Mailchimp** | Audience members (subscribed/unsubscribed/cleaned), tags, merge fields, e-commerce store data (only if connected), campaign reports with per-member opens/clicks/bounces, member activity | `since_last_changed` on members. List webhooks (subscribe/unsubscribe/profile/cleaned/campaign). No open/click webhooks. Batch endpoint for big reads. | Customer Journeys API trigger step: POST with an email to fire a journey for one contact (contact must already be in the audience). Events API on a member can also start journeys. Per-person content via merge fields or event properties (set first). Campaigns to a static segment/tag for batches. | 10 concurrent connections per user, 120 s timeout. Can't be raised. | OAuth2 (tokens don't expire) or API key. Integrations directory listing is optional. | No per-message personalisation payload on the journey trigger: content must go through merge fields. Journeys need a paid plan. |
| **Mailchimp Transactional (Mandrill)** | Message activity, opens/clicks/bounces/rejects, webhooks for each | Webhooks, search by date | Send any content to one person now or at `send_at`. Per-person merge vars. | Generous, per account | API key. Paid add-on on top of a paid Mailchimp plan, sold in blocks of 25k emails (about $20 down to $10 per block) **(verify)** | Meant for transactional / one-to-one email. Its own suppression list is separate from the Marketing audience, so marketing unsubscribes don't carry over automatically. |
| **HubSpot** | Contacts, companies, deals, products, line items, commerce orders/carts, email events (open/click/bounce/unsub), subscription status per subscription type | Search API on `lastmodifieddate` (10k result cap per query, low rate). Webhooks for public apps (create/property change/delete). | Set a contact property or send a custom event → workflow starts and sends a marketing email (custom events need Enterprise). Enroll in workflow (v2 endpoint, v4 is beta). Single-send API: marketing email with per-person `customProperties`. Transactional email needs the add-on and Marketing Hub Pro+. | Marketplace OAuth apps: 110 requests / 10 s per installing account. Search API much lower. | OAuth only for listing. Marketplace needs 3 active installs. Manual review can take up to 60 days. One app in review at a time. | Sending marketing email only works for "marketing contacts" (paid). Event triggers and single-send are gated by tier (Enterprise). |
| **Braze** | User profiles (`/users/export/ids`, segment export to S3/GCS), subscription groups, hard bounces/unsubscribes endpoints. Engagement events (sends/opens/clicks/conversions) at scale only via **Currents** (paid entitlement). | Currents streaming (best). Otherwise daily segment exports. No `updated_since` user query. | **Excellent.** `/campaigns/trigger/send` (with `trigger_properties`) or `/canvas/trigger/send` (with `canvas_entry_properties`) to named users. `/messages/send` with fully API-defined content. Schedule endpoints let us pick a time, including the user's local time. `/users/track` events can start action-based campaigns. | Trigger/send endpoints: 250k req/hr shared (per user ID). `/users/track`: 3,000 req / 3 s, 75 objects per type per call. User export: 250/min (newer accounts). | REST API key with scoped permissions (no OAuth). Partner program ("Alloys") for listings. | Currents costs extra and needs the customer's contract. Each customer must create the API-triggered campaign/canvas and give us its ID. |
| **Brevo** | Contacts and attributes, lists, campaign stats, transactional event logs (delivered/opened/clicked/bounced/unsubscribed), webhooks | `modifiedSince` on contacts. Transactional and marketing webhooks. | Transactional email API: template ID + per-person `params`, `scheduledAt` for timing. Events API (`/v3/events`) triggers automations. Contact attributes and lists. SMS and WhatsApp APIs too. | `/v3/events` 10 rps / 36k per hour. Batch of 200 events. Automation processes 6k-10k events/min depending on plan. | API key (v3). OAuth exists for partners. | Using the transactional API for marketing content skips their marketing unsubscribe checks. The events-to-automation path is the right one. |
| **Stripe** | Customers, subscriptions, invoices, charges, payment intents, products/prices, coupons | Events API covers only the last 30 days. Webhooks retry for up to 3 days. List filters on `created`, not `updated`. | No marketing sends. We can create coupons/promotion codes and write customer metadata. Stripe only emails receipts/invoices. | 100 ops/s live, 25 in sandbox. Search 20 reads/s. | Stripe Apps (OAuth, marketplace review) or Connect OAuth, or a restricted key. | Rebuilding state from events after 30 days. Initial backfill must page through every object. |
| **Segment** | As a **destination**: every identify/track call from the customer's sources, live. Profiles API for traits (Unify/Engage tiers). | It pushes to us (streaming). Replays possible on paid plans. | As a **source**: we send `track` events (e.g. "Autonomy Message Requested" with content fields) into their workspace, and their Klaviyo/Braze/etc. destinations trigger the flow. Public API is config only. | HTTP tracking API is high-throughput (no hard drop limit published) **(verify)**. Public API needs Team/Business. | Partner program: build an Action Destination and/or partner source in the Developer Portal, then submit for review. | Two hops, so failures are harder to see. We depend on the customer having mapped the destination correctly. |
| **Twilio SMS** | Message logs and statuses, inbound messages, opt-out signals (inbound STOP webhook, error 21610 on send) | Status callbacks plus inbound webhooks. List messages by date. | Direct send to one number with a Messaging Service. `SendAt` scheduling (up to 35 days ahead) **(verify)**. We own the content. | Throughput set by 10DLC brand trust score and campaign type. Toll-free and short code differ. | Account SID + auth token or API keys. No OAuth. ISV model: subaccount per customer, register brand + 10DLC campaign (reviews 10-15 days). | 10DLC registration per customer, TCPA consent and quiet hours. Opt-outs live in Twilio, not in the customer's ESP. |
| **WhatsApp Cloud API** | Inbound messages, message status webhooks (sent/delivered/read/failed). No history API. | Webhooks only | Approved **template** messages to opted-in users at any time, with per-person variables. Free-form only inside the 24h customer-service window. | About 80 msgs/s per number by default. Daily unique-user tiers grow with quality. Per-user marketing caps (error 131049). | Meta app + Tech Provider / Embedded Signup, App Review for messaging permissions, business verification. | Marketing templates to US (+1) numbers paused since April 2025 and still paused mid-2026. Per-user marketing caps. Jan 2026 terms bar general-purpose AI chatbots. |
| **BigQuery** | Anything the customer has modelled | `updated_at` watermarks, `APPENDS()`/`CHANGES()` table functions (CHANGES needs change history switched on), Storage Read API for bulk | Write decision/holdout tables into a dataset they grant. Their reverse-ETL tool acts on them. | Query and slot quotas. Cost lands on whichever project runs the job. | Customer grants our service account dataset access (or we use workload identity federation). No review. | Schema mapping per customer. Query cost ownership. |
| **Snowflake** | Same | Streams (need privileges on their side), `CHANGES` clause (change tracking on), watermarks, Secure Data Sharing | Write tables back to a schema they grant | Warehouse size and credits (their bill) | Key-pair or PAT for a SERVICE user. OAuth also works. Password-only service logins being phased out through 2025-2026. | Same as BigQuery, plus warehouse cost and auto-suspend tuning |

## Brief: other ESPs (do they have API-triggered per-user sends?)

| Platform | Per-user API trigger? | How |
|---|---|---|
| **Salesforce Marketing Cloud** | Yes | Journey Builder API entry event (`POST /interaction/v1/events` with an event definition key and per-contact data written to a data extension). Transactional Messaging API (`/messaging/v1/email/messages/`) for immediate sends. Installed-package OAuth (client credentials). AppExchange listing needs a security review that takes weeks to months. |
| **Iterable** | Yes | `/api/workflows/triggerWorkflow` (email + `dataFields`), `/api/email/target` (campaign ID + `dataFields`, optional `sendAt`), `/api/events/track` to start event-triggered journeys. Server-side API keys. |
| **Customer.io** | Yes | Track API events start event-triggered campaigns, with event data available in Liquid. Transactional API (`/v1/send/email`) for one message to one person. API-triggered broadcasts are for groups (1 call per 10 s, 10k recipients). Transactional and Track share a soft limit of 3,000 requests / 3 s. |
| **MoEngage** | Yes, with caveats | Track user events through the Data API to start event-triggered campaigns per user. "Business events" are a broadcast-style trigger, capped at 10 triggers / 5 min and 200 / day. Basic auth (workspace ID + key). |
| **CleverTap** | Yes (beta) | External Trigger API (`/1/send/externaltrigger.json`): campaign ID + user identity + string personalisation fields. Public beta, needs the CSM to enable it. Event upload API also feeds live-behaviour campaigns. |

## Per-platform notes

### Shopify
- **Auth and review.** OAuth public app. New public apps must use the GraphQL Admin API (REST is legacy). App Store review is officially 5-10 business days, but forum reports of 3-4 weeks or more are common. Protected customer data: Level 1 covers any customer data, Level 2 covers name, email, phone and address. Both are requested in the Partner/Dev Dashboard. Level 2 adds data-protection requirements (encryption, retention limits, staff access controls, possible audit) and has no published review time. You can use requested access on dev stores right away. A custom app for a single merchant skips App Store review. The three mandatory GDPR webhooks (data request, customer redact, shop redact) are required. Docs: https://shopify.dev/docs/apps/launch/protected-customer-data , https://shopify.dev/docs/apps/launch/app-store-review/review-process
- **AI terms.** From Feb 27, 2026 the Partner Program Agreement bars using merchant or customer data, including aggregated or derived data, to train or develop AI/ML models without written consent. Per Shopify community replies, inference-only use is allowed. This matters for any cross-merchant model. https://www.shopify.com/legal/api-terms , https://community.shopify.dev/t/ppa-update-feb-27-2026-clarification-needed-on-ai-ml-restriction-scope/29331
- **Read.** `customers` (with `emailMarketingConsent` and `smsMarketingConsent`), `orders`, `products`, discounts. Storefront behaviour (product views, add to cart) needs a Web Pixel app extension. Shopify does not expose Shopify Email engagement.
- **Incremental.** Query filters such as `updated_at:>X`. Bulk operations return a JSONL file with no cost limits, and up to 5 can run at once per shop. Webhooks are retried 8 times over about 4 hours, then the subscription can be removed. Shopify itself says to run reconciliation jobs. https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits , https://shopify.dev/docs/apps/webhooks/best-practices
- **Write.** Consent mutations (`customerEmailMarketingConsentUpdate`, `customerSmsMarketingConsentUpdate`), tags and metafields, and discount code creation (useful for per-person offers). `flowTriggerReceive` fires a custom Flow trigger with a payload under 50 KB. Shopify Flow marketing automations can include Shopify Messaging "send marketing email/SMS" actions, which would make Shopify Email a send target **(verify which plans support custom triggers combined with that action; forum reports say non-Plus stores have issues)**. https://shopify.dev/docs/apps/build/flow/triggers/create
- **Sandbox.** Free development stores from the Partner/Dev Dashboard.

### WooCommerce
- **Auth.** Store-level REST API keys, created by the merchant or through the `/wc-auth/v1/authorize` approval flow. Requires HTTPS. No marketplace review unless we list on WooCommerce.com.
- **Read.** `/wp-json/wc/v3/` customers, orders, products, coupons. Marketing consent and engagement are not core; they live in MailPoet, Klaviyo or another plugin.
- **Incremental.** `modified_after` on orders and products. Webhooks (order.created/updated, customer.*) are disabled after more than 5 consecutive failures and stay off silently, so we must monitor and re-enable them through the API. https://developer.woocommerce.com/docs/apis/rest-api/v3/webhooks/
- **Write.** Coupons, customer meta. No messaging.
- **Sandbox.** Local WordPress or wp-env, free.

### Klaviyo
- **Auth.** OAuth (PKCE). Private keys are fine for pilots. App listing needs 5 installs from real customers with API activity, a security review, workflow and architecture diagrams, endpoint list, dependency patching within 3/7 days, and 365-day logs. Review time is not published; one community post reports more than a month. https://developers.klaviyo.com/en/docs/klaviyo_app_listing_requirements
- **Read.** Profiles, Events (all engagement metrics including Received/Opened/Clicked/Bounced Email, Unsubscribed, SMS metrics), Lists, Segments, Catalogs, Campaigns, Flows, and the Reporting API.
- **Incremental.** JSON:API filters (`greater-than(updated,...)`, `greater-than(datetime,...)`) with cursor pages. Klaviyo has no general outbound webhooks; flow webhook actions only.
- **Write (the key path).** `POST /api/events` creates a custom metric event on a profile, with properties such as `subject_variant`, `offer_code`, `product_ids` and `copy_blocks`. A flow triggered by that metric sends it, and the template renders `{{ event.offer_code }}` and so on. Per-person content, our timing, their unsubscribes and their sender reputation all stay intact. Alternatives: profile properties with list-triggered flows, or the Campaigns API (create campaign, then a send job, scheduled or immediate) for batch sends to a list we fill. Consent: subscribe via the bulk subscribe job with explicit consent. Add-to-list without consent leaves the profile as NEVER_SUBSCRIBED. https://developers.klaviyo.com/en/docs/collect-and-sync-consent , https://developers.klaviyo.com/en/docs/rate-limits-and-error-handling
- **Watch out.** Smart Sending, flow filters and quiet hours can skip our sends, so we need to read "Received Email" back to confirm delivery. Marking flows transactional needs Klaviyo's approval. Billing is per active profile with send allowances, and SMS uses credits.
- **Sandbox.** Free secondary test account linked to the main login, plus a sample data generator. https://developers.klaviyo.com/en/docs/create-a-test-account

### Mailchimp (Marketing API and Transactional)
- **Auth.** OAuth2 (tokens don't expire) or API key. Rate limit is 10 concurrent connections per user, with a 120 s timeout; use `/batches` for big jobs. https://mailchimp.com/developer/marketing/docs/fundamentals/
- **Read.** Members and status, tags, merge fields, member activity, e-commerce (stores/customers/orders/products, only if a store is connected), and reports with per-member email activity.
- **Incremental.** `since_last_changed` on list members. List webhooks cover subscribe, unsubscribe, profile, cleaned and campaign events, but not opens or clicks.
- **Write.** Customer Journeys API: `POST /customer-journeys/journeys/{journey_id}/steps/{step_id}/actions/trigger` with `email_address` returns 204. The merchant has to build the journey with an "API" starting point in the UI first, and the contact must already be in the audience. Set per-person content through merge fields first. https://mailchimp.com/developer/marketing/api/customer-journeys/create-journey-step-action-trigger
- **Transactional (Mandrill).** Fully API-defined sends with `send_at`, merge vars and per-message webhooks. It is a paid add-on in 25k-email blocks and is meant for transactional mail. Its suppression list is separate from the marketing audience, so it is a poor fit for marketing sends. https://mailchimp.com/developer/transactional/
- **Sandbox.** Free Mailchimp account for the Marketing API. Mandrill has a test mode.

### HubSpot
- **Auth and review.** OAuth is required for Marketplace. Needs 3 active installs. Manual review should finish within 60 days of feedback. One app in review at a time. Classic CRM cards stopped working June 2025. https://developers.hubspot.com/docs/apps/developer-platform/list-apps/listing-your-app/app-marketplace-listing-requirements
- **Limits.** Marketplace OAuth apps get 110 requests / 10 s per installing account, and the limit-increase add-on doesn't apply. Search API is much lower. https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines
- **Read.** CRM objects (contacts, companies, deals, products, line items, orders, carts), the email events API, and the subscription preferences API. Incremental reads use Search on `lastmodifieddate` (10k results per query) or webhooks.
- **Write.**
  1. Update a contact property, and a workflow enrolled on that property sends a marketing email that uses personalisation tokens.
  2. Enroll a contact directly in a workflow (`/automation/v2/workflows/{id}/enrollments/contacts/{email}`). It returns 204 even if enrollment criteria later fail. v4 flows API is beta.
  3. Custom events (`/events/.../send`) can trigger workflows. `NO_OBJECT` events trigger workflows only and aren't stored. Custom events need Enterprise.
  4. Single-send API: `POST /marketing/v4/email/single-send` sends a pre-built marketing email to one contact, with per-person `customProperties`. Needs Marketing Hub Enterprise.
  5. Transactional single-send (`/marketing/v3/transactional/single-email/send`) needs the transactional add-on.
  https://developers.hubspot.com/docs/api-reference/marketing-single-send-v4/single-send/post-marketing-v4-email-single-send
- **Cost trap.** Only contacts set as "marketing contacts" can receive marketing email, and the customer pays per marketing contact.
- **Sandbox.** Free developer test accounts.

### Braze
- **Auth.** REST API keys with endpoint-level permissions, per workspace. No OAuth. Partners join the Braze Alloys program.
- **Read.** `/users/export/ids`: 250/min for accounts onboarded after Aug 2024, 2,500/min for older ones, can be raised. Segment export writes files to S3, GCS or Azure. `/subscription/status/get`, plus `/email/hard_bounces` and `/email/unsubscribes`. Engagement and behaviour streams come from **Currents**, which needs Engagement Events entitlements (paid) per connector. Test connectors are capped (about 10k events/week). https://www.braze.com/docs/user_guide/data/braze_currents/
- **Write.**
  - `/campaigns/trigger/send` with `trigger_properties` and `/canvas/trigger/send` with `canvas_entry_properties` (or `context`) send to named `external_user_id`s. These share 250k requests/hour with `/messages/send`. Segment-targeted calls are capped at 250/min.
  - Schedule variants (`/campaigns/trigger/schedule/create`, with `in_local_time`) let us choose the moment.
  - `/messages/send` sends with fully API-defined content.
  - `/users/track` (3,000 req / 3 s) updates attributes and events.
  https://www.braze.com/docs/api/endpoints/messaging/send_messages/post_send_triggered_canvases , https://braze.com/docs/api/api_limits
- **Cost.** Contract-based (data points or MAU, plus message volume). No free sandbox; ask for a partner workspace.

### Brevo
- **Auth.** v3 API key. OAuth is available to partners.
- **Read.** Contacts (`modifiedSince`), lists, campaign stats, transactional event reports. Webhooks cover delivered, opened, clicked, bounced, unsubscribed and spam.
- **Write.** Transactional email (`POST /v3/smtp/email`) with `templateId`, per-person `params` and `scheduledAt`. The Events API triggers automations (10 rps / 36k per hour, batch of 200, automation throughput 6k-10k/min). SMS and WhatsApp APIs are also available. For marketing content, use events into an automation so their marketing unsubscribe logic applies. https://developers.brevo.com/docs/api-limits , https://developers.brevo.com/docs/event-endpoints
- **Sandbox.** Free plan (300 emails/day).

### Stripe
- **Auth.** Stripe App (OAuth install, Marketplace review), Connect OAuth, or a customer-made restricted key (simplest for read-only).
- **Read.** Customers, subscriptions, invoices, charges, payment intents, prices and products. The Events API returns only the last 30 days. Webhooks retry for up to 3 days in live mode. Lists filter on `created`, so the backfill is a full scan. Search API runs at 20 reads/s. https://docs.stripe.com/rate-limits , https://support.stripe.com/questions/stripe-event-retention-period
- **Write.** Coupons and promotion codes (per-person offers), customer metadata. No marketing messaging.
- **Sandbox.** Free sandboxes/test mode (25 ops/s).

### Segment
- **As a destination (read).** Build an Action Destination in Segment's Developer Portal (TypeScript, in the `action-destinations` repo), then submit for review. Customers then send us their identify, track and page streams live. Cheapest real-time behaviour feed we can get. https://www.twilio.com/docs/segment/partners/destinations
- **As a source (write).** A partner source, or simply the customer's write key, lets us emit `track` events into their workspace. Their existing ESP destination (Klaviyo, Braze, Iterable and so on) maps those events to flow triggers. That is "decide here, send there" through one connector, but it adds a hop we can't observe end to end.
- **Public API** is workspace config only, on Team/Business plans. The Profiles API (Unify) gives per-user traits.

### Twilio SMS
- **Auth.** Account SID plus auth token or API key. Use one subaccount per customer.
- **Write.** Messages API with a Messaging Service (sender pool, scheduling, opt-out handling). Default STOP/HELP keywords are on, with up to 20 custom keywords via Advanced Opt-Out. From March 2026, opt-outs are unified across SMS, MMS and RCS, and sends to opted-out users fail with error 21610. https://www.twilio.com/docs/messaging/tutorials/advanced-opt-out
- **Compliance.** US A2P 10DLC needs a brand and campaign registration per customer (ISV flow), with reviews quoted at 10-15 days. Throughput depends on trust score. TCPA consent and quiet hours are our responsibility.
- **Cost.** $0.0083 per US segment plus carrier fees (about $0.003+), 10DLC registration fees, and $0.001 per failed message. https://www.twilio.com/en-us/sms/pricing
- **Read.** Statuses come through callbacks, and inbound replies (including STOP) come through webhooks.

### WhatsApp Business Platform (Cloud API)
- **Auth.** Meta app, Tech Provider status, Embedded Signup to onboard each customer's WABA, App Review for the messaging and management permissions, and business verification.
- **Rules.**
  - Business-initiated messages must be approved **templates**, with per-person variables.
  - Free-form messages only work within 24 h of the user's last message.
  - Opt-in must be specific to WhatsApp.
  - Per-user marketing caps are applied dynamically (error 131049; wait 24 h before retrying). These caps are not active in the EEA, UK, Japan or South Korea.
  - **Marketing templates to US numbers have been paused since April 1, 2025**, and the pause was still in place mid-2026.
  https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/marketing-templates/per-user-limits/
- **Pricing.** Per message since July 1, 2025, by category (marketing, utility, authentication) and country. Utility templates are free inside the service window, and everything is free for 72 h after a click-to-WhatsApp ad. https://developers.facebook.com/docs/whatsapp/pricing
- **AI terms.** From Jan 15, 2026 the Business Solution Terms bar providers whose primary function is a general-purpose AI assistant. Business-specific AI messaging, like ours, is still allowed, but Meta decides at its discretion.
- **Read.** Webhooks only: statuses and inbound messages.

### BigQuery and Snowflake
- **BigQuery.** The customer grants our service account `dataViewer` on the dataset and we run jobs in our own project, or they grant `jobUser` so cost lands on them. Incremental reads use `updated_at` watermarks or `APPENDS()`/`CHANGES()`. CHANGES needs `enable_change_history`, costs extra, and is limited to the time-travel window. Use the Storage Read API for bulk. Free BigQuery sandbox for development. https://docs.cloud.google.com/bigquery/docs/change-history
- **Snowflake.** A SERVICE user with key-pair or programmatic access token, or OAuth. Single-factor passwords are being blocked for service users through 2025-2026. Incremental reads use streams or `CHANGES` (change tracking on) or watermarks. Secure Data Sharing avoids copying data. Compute runs on their warehouse credits. 30-day trial for development.
- **Write-back** in both cases is a decisions table that the customer's reverse-ETL tool (Hightouch, Census, Segment Reverse ETL) pushes on.

## Bottom line

**Clean "decide here, send there":** Klaviyo (event → flow), Braze (API-triggered campaign/canvas), Iterable, Customer.io, SFMC (entry event or transactional), Brevo (events → automation), HubSpot (only on Enterprise, or by property-triggered workflows), and Segment as a relay to any of these.

**Workable but clunky:** Mailchimp (journey trigger with no payload, so content goes through merge fields first) and Shopify (no send API; tags, discounts and possibly a Flow trigger).

**We own the send and the compliance:** Twilio and WhatsApp, where we carry registration, consent and opt-outs ourselves.

**Read-only for action purposes:** Stripe, WooCommerce, BigQuery and Snowflake.

**Biggest risks:**
1. Every ESP needs a one-time receiving flow built in the customer's account. Only Klaviyo (Flows API) and HubSpot (v4 beta) let us create it programmatically, so "no human after connect" is not fully true yet.
2. The ESP can silently drop our send (smart sending, frequency caps, filters, quiet hours, per-user WhatsApp caps). We must read engagement back to confirm what actually went out, or the holdout measurement is wrong.
3. Marketplace gates: Shopify PCD Level 2, Klaviyo's 5 installs and security review, and HubSpot's 3 installs and up to 60 days. Each one is chicken-and-egg for launch.
4. AI and data terms: Shopify's 2026 ban on training with merchant data, and Meta's AI-provider clause.
5. Webhooks are unreliable everywhere (Shopify retries for about 4 h, WooCommerce auto-disables, Stripe keeps 30 days of events), so we need scheduled reconciliation polls.
6. Engagement data costs extra or is hard to get: Braze Currents, Mailchimp has no open/click webhooks, and Shopify Email exposes none.
