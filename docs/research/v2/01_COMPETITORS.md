# 01 - Competitive landscape: autonomous / AI-decisioning marketing (as of Oct 2026)

Research date: 2026-10-19. Most numbers are vendor-reported. Items marked (unverified) come from a single third-party source.

## 1. The short version

The market has split into two kinds of product that both call themselves "agents":

1. **Gen-AI campaign builders.** You type a goal, the agent drafts segments, copy, and a journey, and a human approves it. Klaviyo Composer, Bloomreach Loomi, Iterable Nova, Salesforce Agentforce, Adobe Journey Agent, HubSpot Breeze, Optimove OptiGenie, CleverTap, WebEngage, Netcore, Customer.io. These save marketers time. They do not learn per customer.
2. **Learning decision engines.** Per-customer bandits / reinforcement learning (RL) that pick time, channel, offer, and frequency, and learn from outcomes. OfferFit (now BrazeAI Decisioning Studio), Aampe (now MoEngage), Hightouch AI Decisioning, Movable Ink Da Vinci (content only), Attentive AI Journeys (SMS). These improve revenue, but they are enterprise-priced and need a lot of services.

The big 2025-26 story is **consolidation**. The two independent decision engines were bought by engagement platforms: Braze bought OfferFit (USD 325M, closed June 2025), and MoEngage bought Aampe (announced 24 June 2026, terms undisclosed, reported as "tens of millions" all-cash, unverified). Hightouch is the main independent left. It raised a USD 150M Series D at a USD 2.75B valuation in April 2026.

## 2. Player by player

### Pure decisioning engines

**BrazeAI Decisioning Studio (ex-OfferFit).**
- **What it decides:** per customer, using RL / contextual bandits: offer, channel, timing, frequency, and creative.
- **Two tiers.** *Pro* is the original OfferFit. It optimises any business KPI (revenue, CLV), uses first-party data, covers several channels, and is delivered with Braze's "AI Decisioning Services" team. *Go* was announced at Forge on 29 Sep 2026 (GA expected in October). It is self-serve, but covers **email only** and optimises **clicks only**: it picks a subject line and send time per recipient from options the marketer supplies, and keeps a random control group of at least 5%.
- **Proof:** "10-20%+ program revenue lift" (generic claim). Brinks Home reported "450% increase in contract extension value". Named customers: Chime, MetLife, Trainline, Wyndham, Canadian Tire.
- **Weaknesses:** G2 reviewers (rating 4.7 from 15 reviews, many of them invited) mention a steep learning curve and slow implementation. Pro needs services and is quote-only (no public price). Go runs inside Braze only.

**Aampe (now MoEngage).**
- **What it decides:** gives every user an individual agent (the "agent per user" model) that chooses day, time, frequency, and message variant. It learns from clicks, purchases, unsubscribes, and *pauses*. Aampe says the agents use causal analysis rather than plain correlation.
- **Scale:** "200B decisions/week" claimed.
- **Customers:** Grab, Swiggy, Taxfix, ZenBusiness, Deezer.
- **Role:** sits on top of an existing sender. It will now be merged with MoEngage's Merlin AI workflow agents.
- **Weaknesses:** no public pricing. Its own founders argue that explaining any single decision is unreliable, which makes it hard for a marketer to audit.

**Hightouch AI Decisioning.**
- **What it decides:** RL agents choose message, timing, and channel per customer toward a goal the marketer sets.
- **Role:** sits on the customer's data warehouse (Snowflake, Databricks, and so on) and sends through the customer's existing email or SMS platform. It does not send messages itself.
- **Proof:** WHOOP got +10% on cross-sell conversions within 6 weeks. PetSmart got +22% incremental salon bookings and +13% autoship (method not stated).
- **Weaknesses:** needs a mature warehouse and data team. Enterprise pricing only. In 2026 it is also expanding into ads (Ad Studio), which could spread its focus.

### Big engagement platforms (customer engagement platforms, or CEPs, which also send the messages)

**Braze.**
- **Agents:** BrazeAI Operator (assistant that can take actions; now GA), Agent Console (build custom agents inside Canvas and Catalogs), and Decisioning Studio. Forge 2026 added *Operator Connect* (run Braze from Claude or ChatGPT over MCP), *Agentic Standards* (automatic checks against brand rules, with a log of every fix), and conversational agents.
- **Price:** mid-market roughly USD 40-100k/yr, enterprise USD 200k-1M+, plus USD 50-300k to implement (third-party benchmark).
- **Complaints:** expensive, frequent price rises, paid add-ons, long implementation.

**MoEngage (Merlin AI).**
- **Agents:** Merlin AI workflow agents, plus Aampe's per-user decisioning. Raised USD 100M+ (late 2025) to push agents in North America, SEA, and ANZ.
- **Price:** about USD 999/mo for 10k monthly active users (MAU); WhatsApp and AI predictions cost extra (unverified).
- **Position:** strong in India and SEA.

**CleverTap.**
- **AI:** Clever.AI (predictive, generative, prescriptive) with agents for analytics, journeys, segments, content, and recommendations. Bought Rehook.ai (incentives / loyalty). Named a Leader in Gartner's 2026 Magic Quadrant for Personalization Engines.
- **Price:** Essentials quoted anywhere from INR 6,000/mo to USD 2,000/mo depending on the source.

**Iterable.** Nova Agent (22 Apr 2026) runs build / audit / personalise / optimise agents, plus a Command Center and goal-driven experiments. No public lift numbers.

**Klaviyo.**
- **Agents:** Composer (announced Mar 2026, public beta 30 Jun 2026) builds whole campaigns and flows from one prompt and audits every live flow for "revenue opportunities". Klaviyo says: "Nothing goes live without human approval." Customer Agent handles service conversations and writes preferences and intent back to the profile.
- **Price:** Composer is about USD 20/mo plus usage (changed 23 Sep 2026); free plan gets USD 5/mo of usage.
- **Complaints:** the Feb 2025 switch to billing on "active profiles" raised bills sharply (pricing is the top complaint, in about 4 of 10 reviews). AI features are called "generic best-practice checklists".

**Salesforce Agentforce Marketing.**
- **Agents:** Marketing Goals Agent (pilot) takes objectives, budget, guardrails, and approval rules, then picks audience, content, channel, and timing. Content Agent (pilot). Agentic Segmentation is GA.
- **Price:** Flex Credits at USD 500 per 100k credits (about USD 0.10 per action). Data Cloud is extra, roughly USD 12-50k/yr.
- **Complaints:** first-year cost often 3-5x the quote (estimate from a competitor). Setup takes about 6 months and needs admin and developer skills.

**Adobe Journey Optimizer + Agent Orchestrator.**
- **Agents:** about 10 agents, including Journey Agent (builds journeys from prompts, flags conflicts between journeys) and experimentation and audience agents.
- **Maturity:** Merkle (Mar 2026) says recovery when one agent in a chain fails is "inconsistent", and fully autonomous execution without human checkpoints is "not yet available".
- **Price:** enterprise; roughly USD 1.4-2.2 per active profile per year for the Real-Time CDP (unverified).

**HubSpot Breeze.**
- **Agents:** mostly B2B. The Prospecting Agent costs about USD 1 per recommended lead; Customer Agent USD 0.50 per resolved conversation (Apr 2026). Credits cost about USD 10 per 1,000.
- **Pricing shift:** an early sign that agents are moving to **outcome-based pricing**.

**Bloomreach (Loomi).**
- **Agents:** Loomi Marketing Agent (GA 2 Jun 2026) turns one prompt into a full journey, splitting audience, content, personalisation, and send time across sub-agents. Loomi Connect (Jan 2026) exposes Bloomreach to ChatGPT over MCP.
- **Proof:** 260 Sample Sale targeted **82% fewer customers** and got **2.4x conversion**. That is the clearest public "who to leave alone" result in the market.
- **Price:** about USD 180k/yr (estimate from a competitor).

**Insider One.** Agent One (Shopping, Insights and other agents), OpenAI partnership (Mar 2026); the earlier Sirius AI brand appears folded in. No public pricing.

**Optimove.**
- **AI:** Optimove AI suite (Jun 2026) with native decisioning, creation, and analysis agents; an Optimove MCP for Claude and ChatGPT; OptiGenie in-app agent (Aug 2026); and an "AI Decisioning Studio" view showing which agents are active. Strong in gaming and betting. Long track record with self-optimising campaigns and control groups.

**Netcore.ai.**
- **Agents:** rebranded 30 Jul 2026. Seven agents plan, execute, analyse, and optimise campaigns.
- **Model:** "growth engineers" (Netcore staff) share accountability for results, under an **outcome-based** commercial model. It is the first CEP to sell results rather than seats. This is half software, half managed service.

**WebEngage.** Assistive Insight, Segment, Content, and Journey agents; no major 2026 launch found.

### Copy / creative AI

- **Persado:** enterprise only, about USD 60k-250k+/yr. Claims 10-40% conversion lift over human-written copy, with uplift that shrinks after year one.
- **Jacquard (ex-Phrasee):** "agentic" brand-language AI; no public prices found.
- **Movable Ink Da Vinci:** picks creative and content per person. Claims +28% conversion and +35% revenue per send versus marketer-chosen creative (2025). Is expanding to push, SMS, and RCS, starting with Attentive.

### Other notable products and startups

- **Attentive:** AI Journeys (SMS) chooses timing, frequency, copy, images, and products per shopper, sold as a flat-fee AI Pro add-on. AI Campaigns ships before Black Friday 2026.
- **Postscript:** Shopify-only; AI Shopper and "Infinity Testing".
- **Customer.io, Blueshift:** AI agents (beta) that turn a prompt into a configured campaign.
- **Marklo:** USD 5M seed, Oct 2026, led by Forerunner. Reads Shopify sales and inventory data, proposes campaigns weekly in Slack, and a human approves.
- **Kana Intelligence:** USD 6M seed, Mar 2026 (unverified).
- **India WhatsApp platforms (AiSensy, Wati, Interakt from about INR 999/mo, Gupshup):** chatbots and bulk broadcasts only. No per-customer decisioning.

## 3. What buyers actually trust (market reality)

- **Hands-off autonomy is rare.** Epsilon 2026: only **2%** let agents run with no human involved. Contentful survey: 43% would allow autonomy on most tasks, 34% only on low-stakes tasks, 21% require approval before anything runs.
- **Brand risk is the number-one blocker.** It was cited by 63% (StackAdapt), ahead of data quality at 56%. Senior leaders are more comfortable with autonomy (58%) than hands-on practitioners (34%).
- **Vendors design around this.** Every 2026 launch ships "human approves" by default, plus rule checks (Braze Agentic Standards, Iterable audit agents).
- **Analysts are cautious.** Gartner (Jun 2026): AI decisioning engines will drive growth, but CMOs must set governance first. Secondary sources quote Gartner as expecting more than 40% of agentic AI projects to be scrapped by 2027.

## 4. Comparison on the 9 decisions

Legend: **A** = learns and decides per customer automatically; **G** = gen-AI drafts it, human approves; **R** = rules or manual; **-** = not offered.

| Product | 1 Who / leave alone | 2 Time | 3 Offer | 4 Channel | 5 Format | 6 Message | 7 Freq / sequence | 8 Objective | 9 Personalisation | Sends itself? |
|---|---|---|---|---|---|---|---|---|---|---|
| BrazeAI Decisioning Pro | A | A | A | A | R | A (pick from variants) | A | Human picks KPI | A | Via Braze |
| Braze Decisioning Go | R | A | - | - (email) | - | A (subject) | R | Clicks only | A | Yes |
| Aampe / MoEngage | A (incl. pauses) | A | A | A | R | A (variants) | A | Human | A | Via host CEP |
| Hightouch AI Decisioning | A | A | A | A | R | A (variants) | A | Human | A | No, uses your sender |
| Klaviyo Composer | G | A (predictive send time) | G | G | G | G | G | Human | G | Yes |
| Salesforce Goals Agent (pilot) | G | G | G | G | G | G | G | Human + budget | G | Yes |
| Adobe AJO agents | G | A | R/A (offer decisioning) | G | G | G | G | Human | G | Yes |
| Bloomreach Loomi | G/A | A | G | G | G | G | G | Human | A | Yes |
| Iterable Nova | G | A | G | G | G | G | G | Human | G | Yes |
| CleverTap / WebEngage / Netcore | G | A | G | A (best channel) | G | G | G | Human (Netcore shares KPI) | G | Yes |
| Optimove | A (control groups) | A | A | G | G | G | A | Human | A | Yes |
| Insider One | G | A | G | A | G | G | G | Human | G | Yes |
| HubSpot Breeze | G | R | R | R | G | G | G | Human | G | Yes |
| Persado / Jacquard | - | - | - | - | R | A (language) | - | Human | A (language) | No |
| Movable Ink Da Vinci | - | R | A (content) | - | A (image / content) | A | R | Human | A | No |
| Attentive AI Journeys | R | A | A (products) | - (SMS) | A | A | A | Human | A | Yes |

Patterns:

- **Decision 5 (format: text vs image vs video, per person) is almost empty.** Only content tools like Movable Ink choose creative per person, and only within one channel.
- **Decision 8 (which outcome to optimise) is always human-set.** No product advises *which* goal or which customers deserve budget given profit.
- **Decision 1 framed as uplift (persuadable vs sleeping dog)** is claimed only by decision engines and Optimove, and is rarely shown. Bloomreach's 82%-fewer-sends case is the best public proof that it sells.

## 5. White space: where a new general product could win

1. **A "decision brain" that sits on top of senders, for the mid-market.** OfferFit, Aampe, and Hightouch proved that per-customer decisioning sells to enterprises, then two of them were absorbed into USD 100k+ suites. No one offers it self-serve, quick to set up, at mid-market prices, working from files or a read-only database instead of a Snowflake warehouse.
2. **Profit-first "who to leave alone".** Uplift with sleeping-dog detection, discount cost, and contact cost. Most tools still optimise clicks or conversions (Braze Go optimises clicks only). Saving money by *not* sending is easy to prove and easy for a CFO to like.
3. **Honest, holdout-based ROI in plain English.** Vendors report their own lifts. A product whose permanent random holdout and incrementality report a finance team can audit is a trust differentiator. Our v1 already has this.
4. **Goal and budget to plan.** "Win back churners, budget 1 lakh" turned into who, what offer, and expected return. Salesforce's Goals Agent is only a pilot. No mid-market product does this with real economics.
5. **WhatsApp-first markets (India, SEA, LATAM, MENA).** WhatsApp platforms have chatbots and broadcasts, not decisioning. Indian CEPs (MoEngage, CleverTap, Netcore, WebEngage) are moving upmarket and charge extra for WhatsApp.
6. **Plug into the MCP wave.** Braze, Optimove, and Bloomreach now expose themselves to Claude and ChatGPT. A neutral brain could *drive* these tools rather than replace them.
7. **Format choice and creative cost.** Picking text vs image vs video per customer, priced against production cost, is open.

## Sources

- https://www.nasdaq.com/press-release/braze-completes-acquisition-offerfit-2025-06-02
- https://www.braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_go
- https://braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_go/faq
- https://www.cmswire.com/customer-experience/braze-forge-2026-recap-self-service-speed-and-autonomy-require-governance/
- https://www.g2.com/products/offerfit-by-braze/reviews
- https://www.airframe.ai/product/offerfit-ai/analysis
- https://www.casestudies.com/company/offerfit
- https://automationatlas.io/answers/braze-pricing-explained-2026/
- https://www.moengage.com/blog/moengage-acquires-aampe/
- https://www.cmswire.com/customer-experience/moengage-buys-aampe-to-add-peruser-ai-agents/
- https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/
- https://aampe.com/blog/how-aampe-s-agents-use-causal-analysis-to-measure-impact-amidst-external-messaging
- https://aampe.com/blog/why-auditing-a-single-agent-decision-misses-the-point
- https://hightouch.com/customers/whoop-ai-decisioning
- https://www.databricks.com/br/customers/petsmart/ai
- https://www.mrweb.com/drno/news39762.htm
- https://techcrunch.com/2025/02/18/hightouch-raises-80m-on-a-1-2b-valuation-for-marketing-tools-powered-by-ai
- https://www.klaviyo.com/newsroom/CRM-agents
- https://community.klaviyo.com/product-updates-and-announcements-51/what-is-klaviyo-composer-public-beta-access-credits-and-what-you-can-do-today-19495
- https://www.usagepricing.com/blueprint/activity/klaviyo-2026-09-23-composer-credits-to-dollars
- https://www.omnisend.com/blog/klaviyo-reviews-research
- https://newsbytes.ph/2026/06/08/salesforce-expands-agentforce-with-ai-tools-for-marketing-teams/
- https://redresscompliance.com/salesforce-agentforce-licensing-guide-2026.html
- https://www.11x.ai/guides/agentforce-pricing
- https://business.adobe.com/products/experience-platform/agent-orchestrator/journeys
- https://www.merkle.com/en/merkle-now/articles-blogs/2026/agent-orchestration-adobe-summit.html
- https://resolve247.ai/blog/hubspot-breeze-pricing-change
- https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Bloomreach-Launches-Loomi-Marketing-Agents-175071.aspx
- https://enterprisesolutions.wpp.com/en/partners/bloomreach
- https://insiderone.com/news/openai-partnership/
- https://cmswire.com/the-wire/optimove-launches-optimove-ai-the-only-marketing-ai-suite/
- https://agilebrandguide.com/optimove-introduces-the-new-optigenie-natural-language-conversational-agent-directly-inside-the-platform/
- https://macaubusiness.com/netcore-is-now-netcore-ai-becoming-the-first-agentic-marketing-platform-to-share-accountability-for-customer-growth/
- https://webengage.com/blog/webengage-ai-agents/
- https://clevertap.com/news/
- https://siliconangle.com/2026/04/22/iterable-launches-nova-agent-assist-markets-scaling-customer-personalization
- https://us.fitgap.com/products/persado
- https://www.finanznachrichten.de/nachrichten-2025-10/66688386-movable-ink-expands-da-vinci-to-europe-and-accelerates-product-suite-008.htm
- https://www.usecarly.com/blog/attentive-ai/
- https://secure.businesswire.com/news/home/20260526090213/en/Attentive-Unveils-Next-Generation-of-Agentic-AI-Marketing-Innovation-at-Thread-2026
- https://pulse2.com/marklo-raises-5-million-seed-led-by-forerunner-to-launch-agentic-marketing-platform-for-shopify-brands/
- https://pureai.com/blogs/the-pure-ai-blog/2025/12/blueshift-launches-next-era-of-customer-ai-agents.aspx
- https://pricingsaas.com/companies/customer/diffs/2026W16
- https://echai.ventures/d2c/d2c-whatsapp-sms/wati-interakt-aisensy-gupshup-which-to-use
- https://www.sequenzy.com/versus/moengage-vs-clevertap
- https://www.gartner.com/en/documents/7973837
- https://martechvibe.com/?p=33967
- https://research.agilebrandguide.com/reports/contentful/contentful-the-agentic-web-critical-ai-questions-for-marketing-and-cx-leadership
- https://s3.amazonaws.com/media.mediapost.com/uploads/EPSILON-2026-benchmark-ai-research-report.pdf

## What this means for us

**Do**
- Position v2 as a **neutral decision brain** that plugs into the tools customers already send with (Braze, MoEngage, Klaviyo, WhatsApp platforms) through APIs or MCP, plus our own WhatsApp and email dispatcher for those who have none.
- Lead with **profit and "who to leave alone"**: uplift, sleeping dogs, discount cost. Show money saved by *not* sending, as Bloomreach's "82% fewer sends, 2.4x conversion" case does.
- Make the **permanent holdout and plain-English incrementality report** the core trust feature. Nobody else gives an auditable, vendor-neutral number.
- Ship **approve-first autonomy with a dial**: start with human approval, then let customers raise autonomy per segment or channel once the holdout proves lift. This matches the 2% / 21% / 43% trust data.
- Target **mid-market and WhatsApp-first markets** (India first) with self-serve setup from files or a read-only database, and no warehouse required.
- Price with a **platform fee plus an outcome component** tied to measured incremental revenue (Netcore and HubSpot show buyers accept this).

**Don't (or not yet)**
- Don't try to out-build Braze, Klaviyo, or Salesforce as a full message-sending suite. Their "build a campaign from a prompt" agents are table stakes and free or cheap.
- Don't promise "fully autonomous, no human" at launch. Buyers don't want it yet, and brand risk is the number-one blocker.
- Don't chase autonomous video generation (decision 5) before text and image work end to end. Format choice should start as "choose among assets the customer has".
- Don't compete on gen-AI copy alone. Persado and Jacquard own the enterprise end, and every CEP now includes copy generation.

**Open questions**
- Will Braze and MoEngage keep decisioning working with other senders, or lock it to their own platforms? That decides whether "sit on top" stays open.
- What do Decisioning Studio Pro, Hightouch, and Aampe actually cost? We need real quotes or customer interviews to set our price.
- How much lift do RL engines really add over a good uplift model plus simple bandits, on mid-market data volumes (cold start, fewer than 100k customers)?
- Can we get a WhatsApp Business partner (BSP) relationship, or must we ride on Gupshup, Wati, and similar platforms for sending?
- Will marketers accept outcome-based pricing measured against *our* holdout, or will they want a third party to audit it?
