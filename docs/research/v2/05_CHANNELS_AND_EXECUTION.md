# 05 — Channels and Execution

*Research date: 2026-10-19. Most facts come from 2025–2026 sources. Many pricing figures come from vendor and BSP blogs rather than Meta, Google or carrier pages. These are marked "(vendor)" and need checking before they go into a price sheet.*

## 1. The short version

- Channels differ by about **100x in cost per contact**. They also differ in **what an AI is allowed to write** and in **how trustworthy their feedback is**. The inner loop needs all three as inputs, not only "consented: yes/no".
- **Email** is the best first channel for a general product. It is cheap, works everywhere, and AI can write the copy freely. Opens are not a usable signal, though, and clicks are polluted by bots.
- **WhatsApp** is the strongest channel in India, LatAm, the Middle East and much of Europe. Every marketing message must be a **pre-approved template**. Meta also applies a **hidden per-user frequency cap that counts messages from all businesses**, and **marketing templates to US numbers have been blocked since April 2025** (still blocked as of Aug 2026, per vendor sources).
- **SMS in India** under DLT is almost hostile to AI copy, because a message must exactly match a registered template. In the US, 10DLC makes SMS expensive and slow to set up. SMS belongs late in the roadmap.
- The market has settled on **"decide here, send there."** OfferFit (now owned by Braze), Hightouch AI Decisioning and Aampe all decide and then trigger the customer's existing send platform. Klaviyo is the opposite case: it owns sending and has now added its own agent.
- **Recommendation:** build one dispatcher interface with three kinds of adapter. (a) Plug into the customer's existing platform for teams that already have one. (b) Send natively over email and WhatsApp for SMBs and Indian mid-market teams that have no platform. (c) Sync audiences to Meta and Google. Do not make money on message markup.

## 2. Channel-by-channel facts

### Cost per message (approximate, Oct 2026)

| Channel | India | US | Notes |
|---|---|---|---|
| Email (Amazon SES) | ~$0.0001 | ~$0.0001 | $0.10 per 1,000 emails. Postmark: $15/mo for 10k. SendGrid Essentials: $19.95/mo, and its free plan was replaced by a 60-day trial in 2025 (vendor) |
| WhatsApp marketing | ~₹0.78–0.88 (~$0.01) | $0.025, but **marketing templates to US numbers are blocked** | Billed per delivered message since 1 Jul 2025. Marketing gets no volume discount |
| WhatsApp utility / auth | ~₹0.115–0.13 | low | Volume tiers. **From 1 Oct 2026, service replies and in-window utility messages are billed after 1,000 free per number per month** |
| BSP markup on WhatsApp | Gupshup ~$0.001/msg. Interakt/AiSensy charge ₹1.5k–3.5k/mo plus markup (vendor) | Twilio has its own markup | Meta's base rate is the same through every BSP |
| SMS | ~₹0.10–0.25 + DLT (vendor) | ~$0.008 base + $0.003–0.005 carrier fee per segment, plus 10DLC brand and campaign fees | The US Verizon fee rose in Jun 2025 (vendor) |
| RCS | Jio, for iPhone business RCS from 1 Sep 2026: ₹0.27–0.30 marketing (news) | ~$0.004–0.005 for utility/auth (vendor) | Growing fast in India (Infobip: +108% H1'25→H1'26), mostly fintech |
| Mobile push | FCM is free | FCM is free | OneSignal: $19/mo + $0.012 per mobile MAU, and web push $0.004 per subscriber |
| Paid audiences | media cost | media cost | Sync is free. You pay for impressions |

**Why this matters for us:** cost per contact belongs inside the expected-net-value formula. In India, sending a ₹0.86 WhatsApp message instead of a ₹0.01 email only makes sense when the expected uplift difference covers the gap. v1 has no per-channel cost model. v2 needs one, fed from live rate cards.

### Email

- **Providers:** SES is cheapest but bare-bones, with no bounce UI and no support at the base tier. Postmark and SendGrid add tooling. A dedicated IP costs about $25/mo on SES.
- **Bulk-sender rules:** they apply to senders of more than 5,000 messages a day to Gmail or Yahoo, counted per provider. Gmail and Yahoo started in Feb 2024 and Microsoft Outlook.com on 5 May 2025. The rules require SPF + DKIM + DMARC with an aligned From domain, a one-click List-Unsubscribe header, unsubscribes honoured within 2 days, and a spam-complaint rate under 0.3%. Good senders aim for under 0.1%. Enforcement moved from spam-foldering to outright rejection during 2025 (Outlook returns `550 5.7.515`).
- **Feedback events** (webhooks): delivered, bounced (hard/soft), complained, unsubscribed, opened, clicked.
  - **Opens are broken.** Apple Mail Privacy Protection pre-fetches images. Recent vendor estimates put MPP at about 49% of all tracked opens. One example dataset: 43% raw open rate against 19% human.
  - **Clicks are contaminated.** Security scanners (Defender, Proofpoint, Mimecast) click links. Omeda attributes 87% of clicks in Q1 2026 to non-human sources on its (B2B-heavy) platform. Mailchimp-cited estimates run from 20% to 50%.
  - **Implication:** the reward signal for bandits must be conversion or revenue, with filtered human clicks as a secondary proxy at most. Never use opens. Unsubscribe and complaint are the reliable negative rewards.
- **AI copy:** no approval step, so the Creative Director can generate freely. The Brand & Compliance Critic is the only gate.

### WhatsApp Business Platform

- **Access:** Meta Cloud API directly, or through a BSP (Gupshup, Twilio, Interakt, AiSensy, MSG91, 360dialog, and others). The platform-level BSPs (Gupshup, Twilio, 360dialog) also act as "tech providers" for SaaS products that onboard their own customers' numbers through Embedded Signup.
- **Pricing model:** per delivered message, by category (marketing / utility / authentication / service) and by recipient country, since 1 Jul 2025. Marketing is never discounted. Messages inside a 72-hour window opened by a Click-to-WhatsApp ad are free.
- **Templates:**
  - Every business-initiated message must use an approved template. Variables have to be numbered, cannot sit next to each other, and cannot open or close the template. Sample values are needed.
  - Meta re-categorises "utility" templates that contain promotional language as marketing (vendor reports date this from Apr 2025).
  - Templates carry a quality rating (green / yellow / red). Low-quality templates get paused and can end up disabled.
- **Limits:**
  - Since 7 Oct 2025, business-initiated limits apply per **business portfolio**, not per number. The tiers are 2k / 10k / 100k / unlimited unique users per rolling 24h.
  - Meta has signalled that the 2k/10k tiers will be removed for verified businesses, replaced by "pacing" (unconfirmed on Meta's own page).
  - Throughput reaches 1,000 msg/s only on an unlimited portfolio with quality of medium or higher.
- **Per-user marketing cap:** error **131049** ("healthy ecosystem engagement"). Meta silently refuses a marketing template when the user has already received "enough" marketing from *any* business. The threshold varies with how active the user is and is not published. Meta's advice is to retry after 24h or later. **For us, a customer's attention is a shared, rationed resource we cannot see**, so the fatigue model has to learn from 131049 failures.
- **Marketing Messages API for WhatsApp** (formerly "MM Lite"; GA per Meta's docs updated 10 Feb 2026): a send path for marketing templates only. Meta itself chooses delivery timing based on predicted engagement and claims up to about 10% better delivery (vendor). It also supports time-to-live and benchmarks. *Meta now partly does "send-time optimisation" for us.* Our send-time model should be able to defer to it on WhatsApp.
- **Feedback events:** sent, delivered, read (unreliable when the user has turned off read receipts), failed (with error codes such as 131049), plus **inbound replies and button clicks**. Replies are the richest signal of any channel, and they open a 24h free-form service window where AI can write freely, billed after 1,000/month from Oct 2026.
- **AI copy:** the AI cannot write each outbound marketing message freely. **Pattern:** the outer loop (Creative Director + Critic) writes a *library* of templates offline and submits them for approval. The inner loop then picks template + variables + media per user. Free AI conversation is possible only once the user replies.

### SMS

- **India (TRAI/DLT):**
  - Entity, header and template must all be registered. Real-time scrubbing blocks any message that does not match an approved template.
  - Since 1 Oct 2024, every URL and callback number must be whitelisted and variables must be tagged.
  - The Feb 2025 amendments added header suffixes (-P/-S/-T/-G) and removed the "service explicit" category.
  - Promotional SMS can only go to non-DND numbers, inside roughly 10am–9pm.
  - Net effect: AI can only fill typed slots in pre-registered text. **Generative copy brings almost nothing here.**
- **US:** 10DLC brand and campaign registration (one-time fees from $4.50 to about $46, plus monthly campaign fees; sources conflict). Carrier filtering is real-time. Unregistered traffic is blocked or surcharged.
- **Feedback:** delivery receipts (quality varies by operator) and inbound STOP/replies. Clicks need link shortening, and in India the shortened link domain must be whitelisted.

### Push (mobile and web) and in-app

- FCM (Android, plus iOS via APNs) is free. APNs is free. OneSignal, Braze and MoEngage wrap both.
- Push requires the customer's **app SDK**, so we cannot own it without being inside their app. That makes it a "plug-in" channel. The exceptions are web push and on-site banners, which can be delivered with a JS snippet.
- AI copy is unconstrained, but the text is very short. Open and delivery signals come from the SDK and are reasonably reliable.

### RCS

- In the US, Apple supports RCS business messaging through carriers on iOS 18+. In India, Jio began carrying business RCS to iPhones on 1 Sep 2026, at higher prices. Airtel is holding out.
- Brand verification and per-agent approval are required.
- Treat RCS as an "SMS upgrade" option through aggregators later, not a first-wave channel.

### Paid-media audiences

- **Google:** Customer Match uploads through the Google Ads API **stopped on 1 Apr 2026**. Uploads must now go through the **Data Manager API** (launched 9 Dec 2025), which added audience CRUD in v1.5 and accepts IP addresses since May 2026.
- **Meta:** Custom Audiences through the Marketing API.
  - Since Sep 2025, Meta automatically flags audiences that "suggest health or financial status", which blocks new campaigns with error 2460003.
  - Housing, employment and credit ad sets need a certification flag (v22+).
- **Fit:** low risk and high value for an uplift engine. Use cases are (1) **suppression lists** (stop paying to retarget people who would buy anyway or who already converted), (2) seed lists of high-uplift customers for lookalikes, and (3) a "cheap reach" arm for people with no messaging consent.

## 3. Send ourselves, plug in, or both?

### What competitors do

| Player | Model | Notes |
|---|---|---|
| OfferFit → **BrazeAI Decisioning Studio** | Decides, then triggers the engagement platform | Braze bought it for $325M (announced 27 Mar 2025). The "Go" tier works with Braze, Salesforce Marketing Cloud and Klaviyo and optimises clicks. "Pro" optimises any metric and comes with a services team |
| **Hightouch AI Decisioning** | Warehouse-native decisioning, triggers the engagement platform | Content stays in Braze/Iterable/SFMC and is only referenced. Pitches "never stores your data" |
| **Aampe** | Agents decide per user and call the customer's provider APIs | Works through Braze, OneSignal, MoEngage, CleverTap, APNs and Customer.io. Delivery and compliance stay in the customer's stack |
| **Klaviyo** | Owns sending and data | K:AI Marketing Agent launched Sep 2025: it plans, writes and launches with marketer approval. Composer (2026) adds audience, timing and channel selection |
| Braze, MoEngage, CleverTap | Own sending, adding agents | Each is building its own decisioning (Braze Agents + Decisioning Studio) |

**Reading:** the companies that "sit on top" won enterprise trust because they did not ask customers to move their send infrastructure, consent records or templates. But the platforms are now **absorbing the decision layer**: Braze bought OfferFit, and Klaviyo shipped its own agent. A pure layer on top of Braze faces Braze itself as a competitor. The open space is **(1) mid-market and SMB teams, India especially, who run WhatsApp through a BSP and email through a cheap ESP, with no Braze-class platform**, and **(2) cross-platform neutrality** (one brain over email via Klaviyo, WhatsApp via Gupshup, and ads via Meta).

### Integration reality when plugging in

- **Braze:** `/campaigns/trigger/send` and `/canvas/trigger/send` carry per-user `trigger_properties`. Limit: 250k requests/hour shared across them, keyed on external_id.
- **MoEngage:** the business-event trigger is capped at 50 per hour and 200 per day *per campaign*, so per-user API sends do not scale. Write decisions as user attributes and let a pre-built flow read them.
- **CleverTap:** External Trigger is still in public beta.
- **Klaviyo and HubSpot:** push profile properties and events, and let flows fire.
- **General pattern (reverse-ETL style):** write `nba_send_at`, `nba_channel`, `nba_offer_id`, `nba_template_id` and `nba_vars` onto the profile, plus a "decision" event. The customer's flow does the sending. Outcomes come back through Braze Currents, MoEngage streams or the warehouse.
- **Trade-off:** we lose exact control of timing and see delivery events late.

### Recommendation: both, behind one interface, in this order

1. **Dispatcher contract** (inner loop → adapter): `{customer, action|do_nothing, channel, send_window, template/creative id, variables, cost_estimate, holdout_flag}`. Every outcome comes back as a common event schema (delivered / failed+code / engaged / replied / converted / unsubscribed / complained).
2. **Adapter families:**
   - (A) *Platform adapters*: Braze, Klaviyo, MoEngage, CleverTap, HubSpot, plus a generic webhook/warehouse table. v1's CSV export becomes the degenerate case.
   - (B) *Native senders*: SES email with the customer's own domain and DKIM. WhatsApp through Cloud API or one BSP partner, using Embedded Signup so the customer owns the number and WABA.
   - (C) *Audience sync*: Meta Custom Audiences and the Google Data Manager API.
3. **Never own the customer's sender reputation or number.** The customer brings the domain and the WABA. We orchestrate. That keeps the trust model the same as plugging in, while still letting us close the loop quickly.

## 4. Channel roadmap for a general product

| Phase | Channel | Why |
|---|---|---|
| 1 | **Email**: native through SES plus a Klaviyo/Braze adapter | Universal, about $0.0001 per send, no copy approval (the AI is free to write), mature webhooks. Proves the closed loop against holdout |
| 1 | **Paid-audience sync** (suppression + uplift seeds) | Low effort, no per-message consent risk, an immediate ROI story ("stopped wasting ad spend on sure-things") |
| 2 | **WhatsApp**: native Cloud API/BSP plus a template-library workflow | Highest engagement in India and emerging markets, where the founder's network likely is. Replies feed the agent. Needs the template pipeline and a fatigue model aware of 131049 |
| 3 | **Push / in-app through CEP adapters**; web push and on-site with our own snippet | Needs an app SDK, so plug in rather than own |
| 4 | **SMS / RCS through an aggregator** | Mostly for OTP, utility and fallback. DLT blocks AI copy in India. 10DLC adds US friction. RCS when iPhone reach in India matures |

## 5. Compliance (brief, only what shapes the product)

- **Consent per channel and per purpose** is a field the inner loop must read, alongside separate unsubscribe states per channel. Email unsubscribes must be honoured within 2 days. Under the TCPA, opt-outs "by any reasonable means" (STOP, "quit", etc.) have been in force since 11 Apr 2025.
- **Quiet hours** apply in the recipient's local time:
  - US TCPA: 8am–9pm. Some states are stricter, e.g. Florida 8am–8pm (verify).
  - India promotional SMS: about 10am–9pm, and no DND numbers.
  - The send-time model must optimise *inside* these windows.
- **India DPDP:** the Rules were notified 13/14 Nov 2025. Consent managers apply from about Nov 2026. Notice, consent and security duties apply from about May 2027.
- **GDPR / ePrivacy:** opt-in, or a "soft opt-in" for existing customers.
- **Meta and Google audience restrictions** on health and finance segments (see §2).

## What this means for us

**Do**
- Add a **channel economics layer** to expected net value: per-message cost by channel × country × category from rate-card tables, plus a fatigue/opportunity cost per contact.
- Use **conversion/revenue vs holdout as the reward**. Treat opens as noise, filter bot clicks, and use unsubscribe, complaint and WhatsApp 131049/blocks as negative signals.
- Build a **template-library workflow**: the LLM outer loop writes templates and variants, submits them to WhatsApp (later DLT and RCS), and tracks approval and quality. The zero-LLM inner loop picks template + slot values.
- Ship **email natively (SES, customer's domain) plus a Klaviyo/Braze adapter** first. Add Meta/Google audience sync early as the "suppression saves money" story.
- Make the customer **own the sender assets** (domain, WABA, number) through Embedded Signup and OAuth. We orchestrate and never resell messages at a markup.
- Model **WhatsApp per-user caps and portfolio quality** as hard constraints. A bad campaign hurts every future send.

**Don't (or not yet)**
- Don't build SMS for India first. DLT exact-match leaves AI nothing to do.
- Don't plan WhatsApp *marketing* for US recipients while the block holds. In the US, use email, SMS and push.
- Don't compete head-on as "a brain for Braze customers" only. Braze now sells that itself.
- Don't build our own push SDK in v2. Reach push through CEP adapters.
- Don't optimise toward opens or raw clicks, even as a stopgap.

**Open questions**
- Who is the first ICP: Indian D2C/fintech on WhatsApp+email (native sending wins), or global teams already on Klaviyo/Braze (adapter wins)? This decides the Phase 2 order.
- Do we partner with one BSP (Gupshup / 360dialog / Twilio) as a tech provider, or go direct to Cloud API? This affects onboarding speed, markup and support burden.
- Should our send-time model defer to Meta's Marketing Messages API delivery optimisation on WhatsApp, or compete with it? How do we measure incrementality when Meta controls timing?
- How do we keep holdout integrity when the customer's own CEP journeys also message the same users?
- What is the true current status of: the US WhatsApp marketing block, removal of the 2k/10k tiers, utility billing from Oct 2026, and OneSignal's free-plan cap? All of these are vendor-sourced and need confirming against primary pages.

## Sources

- https://support.myoperator.com/portal/en/kb/articles/whatsapp-has-shifted-to-per-message-pricing-effective-july-1-2025-based-on-message-categories-and-your-recipient-s-country-in-india-there-are-three-paid-categories
- https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- https://chakrahq.com/article/pricing-updates-for-whatsapp-business-platform-effective-july-2025-onwards/amp/
- https://help.twilio.com/articles/53100480177819
- https://respond.io/blog/whatsapp-pricing-change-2026
- https://chatmaxima.com/blog/whatsapp-service-message-pricing-october-2026/
- https://www.flowcall.co/blog/whatsapp-business-api-pricing
- https://www.wati.io/en/blog/whatsapp-marketing-messages-us-2026/
- https://braze.com/docs/user_guide/channels/whatsapp/meta_resources/
- https://support.chatarchitect.com/books/meta-whatsapp/page/marketing-messages-api-for-whatsapp-developer-documentation/export/markdown
- https://exotel.com/blog/whatsapp-marketing-lite-api/
- https://developers.facebook.com/documentation/business-messaging/whatsapp/upcoming-messaging-limits-changes/
- https://help.webexconnect.io/changelog/whatsapp-supports-business-portfolio-pacing-relaxing-messaging-limit-tiers-and-text-truncation-on-marketing-messages
- https://www.wati.io/en/blog/whatsapp-error-131049/
- https://chakrahq.com/article/whatsapp-template-message-approval-guide/
- https://support.telnyx.com/en/articles/13986483-whatsapp-message-templates-guide
- https://codingclave.com/blog/gupshup-whatsapp-pricing-india-2026
- https://msg91.com/guide/whatsapp-pricing-update-2026-and-save-with-msg91
- https://msg91.com/help/dlt-template-scrubbing-filtering-rules
- https://www.telerivet.com/blog/india-sms-compliance-trai-dlt-registration-and-tcccpr-guide
- https://www.messagecentral.com/blog/a2p-sms-pricing-india
- https://www.sent.dm/resources/united-states-sms-pricing
- https://support.twilio.com/hc/en-us/articles/1260803965530
- https://www.angelone.in/news/market-updates/reliance-jio-apple-start-new-rcs-pricing-for-business-messages-in-india
- https://www.infobip.com/news/indias-rcs-adoption-surges-108-percent
- https://messente.com/blog/cost-of-sending-rcs
- https://redsift.com/guides/bulk-email-sender-requirements
- https://www.warmy.io/guides/google-yahoo-sender-requirements/
- https://www.suped.com/knowledge/email-deliverability/technical/how-to-estimate-real-open-rates-with-apple-mail-privacy-protection-mpp-and-image-caching
- https://sendgrid.com/resource/apple-mail-privacy-protection-email-senders-adapt
- https://www.omeda.com/resources/report/bot-clicks-in-email/
- https://www.suped.com/knowledge/email-deliverability/troubleshooting/why-are-email-click-rates-inflated-and-how-to-solve-the-issue
- https://www.buildmvpfast.com/blog/build-vs-buy-email-infrastructure-saas-mvp-2026
- https://www.costbench.com/compare/amazon-ses-vs-postmark/
- https://onesignal.com/blog/understanding-onesignals-pricing
- https://ppc.land/google-forces-customer-match-uploads-to-data-manager-api-by-april-1/
- https://ppc.land/google-now-takes-ip-addresses-for-customer-match-but-dv360-is-left-out/
- https://developers.facebook.com/documentation/ads-commerce/marketing-api/marketing-api-changelog/version22.0
- https://docs.liveramp.com/connect/en/announcement--upcoming-meta-restrictions-on-certain-custom-audiences-and-custom-conversions--7-17-25-.html
- https://www.contentgrip.com/braze-acquires-offerfit-ai-personalization/
- https://www.braze.com/docs/user_guide/brazeai/decisioning_studio
- https://hightouch.com/blog/introducing-ai-decisioning-platform
- https://docs.aampe.com/docs/general
- https://kb.aampe.com/user-guide/using-aampe/quickstart/quickstart-messaging-tools
- https://www.klaviyo.com/blog/introducing-kai-marketing-agent
- https://martech.org/klaviyo-introduces-marketing-agent-to-automate-campaigns
- https://braze.com/docs/api/endpoints/messaging/send_messages/post_send_triggered_campaigns
- https://moengage.com/docs/api/rate-limits
- https://docs.clevertap.com/docs/external-trigger
- https://www.storyboard18.com/amp/digital/breaking-govt-notifies-final-dpdp-rules-sets-staggered-rollout-with-key-obligations-for-data-fiduciaries-84201.htm
- https://www.kelleydrye.com/viewpoints/blogs/ad-law-access/fcc-issues-one-year-limited-waiver-of-new-tcpa-consent-revocation-rule
