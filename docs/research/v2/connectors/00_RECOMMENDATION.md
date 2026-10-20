# Connectors: what the research says and what we should do

Short summary of reports 01 (market share), 02 (APIs and write-back) and 03 (competitors, build vs
buy). Figures came from search extracts of the cited pages, not the pages themselves, because the
session's network blocked most sites: check a number before it leaves the team.

## What we learned

- **"10 connectors cover 80%" is true only for US/UK/Australia Shopify-style shops (70-80%).**
  Blended global is about 50-60%; a CSV/webhook catch-all lifts it to 65-75%. Global 80% takes
  15-18 connectors (an India pack and a Europe/LatAm pack on top).
- **Shopify + Klaviyo is the one pair that matters most.** About 78% of Klaviyo's recurring revenue
  comes from Shopify merchants, and Klaviyo is on about half of Shopify Plus stores.
- **"Decide here, send there" is how every AI decisioning competitor works** (OfferFit/Braze,
  Hightouch AI Decisioning, Aampe). They rely on bulk exports or warehouses for engagement data,
  not webhooks.
- **Write-back varies a lot:** clean on Klaviyo, Braze, Iterable, Customer.io, Brevo and SFMC;
  Enterprise-only on HubSpot; awkward on Mailchimp; impossible on Shopify (data only). For SMS and
  WhatsApp we send ourselves and own compliance.
- **"No human after connect" has two real limits:**
  - Most email tools need a receiving flow built once in the customer's account (only Klaviyo can
    have it created through the API), so plan a 10-minute guided setup.
  - Their tool can silently skip our sends, so we must read delivery back or the holdout
    measurement is wrong.
- **App store listings need real installs first** (Klaviyo 5, HubSpot 3; Shopify review takes
  weeks). Start with private/custom-app installs for pilots.
- **Klaviyo's own AI needs 500+ ordering customers and 180 days of history.** Smaller shops are a
  gap we can fill.

## Recommendation

1. **Build the connector runtime ourselves** (2-3 weeks): OAuth and token store with keep-alive,
   sync positions and hourly catch-up behind every webhook, health and freshness checks that stop
   the agent acting on stale data, one alert per real problem. Reuse v1's `Connector` contract,
   credential store and job queue, and the existing dispatcher and gateway.
2. **First pair: Shopify (read) + Klaviyo (read and write).** This alone is a sellable product for
   the biggest segment.
3. **Then:**
   - Stripe and WooCommerce (read).
   - WhatsApp and Twilio sending (adapters exist; registration lead times start early).
   - HubSpot and Mailchimp.
   - A Segment-format event endpoint, plus a warehouse `decisions` table that the customer's
     Hightouch or Census sends on.
   - Braze.
4. **Libraries, not platforms:** dlt (Apache 2.0) for HubSpot and Stripe reads. Avoid AGPL Singer
   taps. Consider Airbyte Embedded or Nango (ELv2) only for the long tail, once upkeep passes about
   a week of engineer time a month.
5. **Always ship CSV upload and an incoming webhook** as the catch-all for everything else.
6. **Regions:** decide whether the first pilots are US/UK/Australia (top 10 covers most) or India
   (needs WhatsApp, Razorpay, MoEngage or CleverTap first).

## Open questions for the founders

- Which region are the first 3-5 pilot customers in?
- Do we offer sending ourselves (email via SES) for shops without an email tool, or only
  "send through yours"?
- Do we accept a short guided setup in the customer's email tool, or only list platforms that are
  fully automatic?
