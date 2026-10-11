# Fully autonomous marketing agents: market landscape, October 2026

Research date: 11 October 2026. Author role: market analyst. Scope: products that decide, create, send and optimise marketing with little or no human approval. This builds on, and corrects where noted, `docs/research/agentic_ai_2026_10_notes/competitors_agentic_marketing.md` and `docs/research/AGENTIC_AI_OPPORTUNITIES_2026_10.md`. It does not repeat them.

## 0. Read this first: how the evidence was gathered and what it is worth

**Web access was largely blocked.** I state this plainly because it limits every claim below.

- **WebFetch failed on every domain** (`getaddrinfo ENOTFOUND` for braze.com, arxiv.org, businesswire.com and wikipedia.org). Direct `curl` through the egress proxy was refused (HTTP 403 / connect rejected) for every host tried except `github.com`, `raw.githubusercontent.com`, `pypi.org` and `cloud.google.com`. Hosts tested and refused: sec.gov, investors.braze.com, docs.aampe.com, moengage.com, clevertap.com, trai.gov.in, pib.gov.in, gartner.com, haus.io, nber.org, blog.google, support.google.com, developers.facebook.com, huggingface.co.
- **WebSearch worked until the shared budget ran out** (200 searches per turn, shared by every agent). I ran 41 searches. The remaining planned searches were not run: Optimove 2026 measurement detail, Insider One's Sept 2026 release, Hightouch customer results, HubSpot Campaign Agent detail, Gupshup/Yellow.ai/Meta Business AI in India, Google's Ads Advisor agent permissions, and Albert's current status.
- **I opened 22 primary documents** (the coordinator asked for 25). All came from vendors' public documentation repos on GitHub or from open-source repos already cloned in this workspace:
  - 17 Adobe documentation pages;
  - 2 Twilio Segment pages;
  - Google's Meridian README, Meta's GeoLift README, and Recast's geo-test simulation study (code and result tables).
- **The opened sources are lopsided.** They show how Adobe's autonomy really works in detail, but they show nothing first-hand about Braze, MoEngage, Salesforce, Google Ads or Meta. Braze's documentation repo (`braze-inc/braze-docs`) is not reachable.

**Markers used on every claim:**
- **[O]**: I opened and read the source myself.
- **[S]**: the URL is the page the search engine reported, but I saw only its search summary, **not the page**. The brief says a search snippet is not a source, so **every [S] claim is unverified** and is capped at "medium" confidence even when the URL is a primary source such as an earnings release.
- **[BK]**: background knowledge, unverified.

**Confidence labels:**
- **Strong** means opened [O] primary documentation.
- **Medium** means [S] with a primary-source URL (vendor release, earnings release, regulator), or two or more independent [S] sources that agree.
- **Weak** means a single secondary [S] source (blog, aggregator), conflicting sources, or a vendor performance claim with no stated method.

**Vendor results are a separate matter.** That a vendor *said* a lift can be medium confidence, but the lift itself is "vendor claim, unverified" unless stated otherwise.

---

## 1. Comparison table

**What the codes mean** (for each of the decide / create / send / optimise / measure steps):
- **Auto** means the system does it unattended, within guardrails a human has set.
- **Draft** means the agent prepares it and a human approves.
- **Human** means a person does it.
- **–** means not offered.
- **?** means unknown.

**Autonomy level** (the highest level that is generally available, not in pilot):
- **L1**: suggests only.
- **L2**: acts once a human approves.
- **L3**: acts unattended within human-set guardrails.
- **L4**: fully autonomous towards a goal and a budget.

| Vendor / product (as of Oct 2026) | Decide | Create | Send | Optimise | Measure | Autonomy (GA) | How results are measured | India presence | Evidence quality |
|---|---|---|---|---|---|---|---|---|---|
| **Google Performance Max / AI Max** | Auto (bids, placements, queries, audiences) | Auto (assembles assets; AI Max writes text within brand guidelines) | Auto | Auto | Platform-reported conversions | **L4** within budget and conversion goal | Platform attribution. Advertiser-run lift and geo tests are optional. One geo test found platform reporting overstated PMax by ~33% [S] | Sold in India [BK] | Medium on how it works; weak on lift |
| **Meta Advantage+** | Auto (audience is now a "signal" only) | Auto (creative enhancements on by default) | Auto | Auto | Platform-reported | **L4** within budget | Platform attribution. Meta Conversion Lift and open-source GeoLift exist [O]. Across 640 Haus tests, Advantage+'s edge faded by the end of each test [S] | Sold in India [BK] | Medium |
| **Braze**: Decisioning Studio (ex-OfferFit), Operator, Agent Console, Agentic Standards | Auto (per-user channel, time, offer, frequency from a human-approved menu) | Draft (Operator builds a Canvas from a prompt) | Auto inside approved Canvas | Auto (RL) | Random control, holdout or business-as-usual, chosen per deployment [S] | **L3** for decisioning; L2 for Operator | Vendor-run comparisons; Forrester TEI study commissioned by Braze | Not found | Weak to medium |
| **MoEngage + Aampe** (acquired 24 Jun 2026) | Auto (one RL agent per customer; Thompson sampling / bandits) | Draft (Merlin AI) | Auto | Auto | Aampe: holdouts, switchbacks, synthetic controls | **L3** | Aampe's own 11-month RCT (vendor-authored): +65.3% push-open days, +0.3% conversion days (prior notes) | **Home market**. Swiggy is a named customer [S] | Medium |
| **Salesforce Agentforce Marketing** | Draft (Agentic Segmentation) | Draft (Content Agent: pilot) | Human | Pilot (Marketing Goals / Expert Agent) | ? | **L2**; L3 only in pilot | Not found | Not India-specific | Weak |
| **Adobe**: Journey Optimizer, Agent Orchestrator, CX Enterprise Coworker | Auto only for offer ranking and send time; Draft for journeys and audiences | Draft (generative content; "always review") | Human ("Review and launch") | Auto for offer ranking and send-time optimisation | Holdouts per action (10% default); journey-level holdout in limited availability from 1 Sep 2026 | **L2** for agents; L3 for offer ranking | See §2.3: Adobe's decisioning "holdout" is random-offer traffic, not a no-message group [O] | Adobe India pages (prior notes) | **Strong** (opened docs) |
| **HubSpot** (UNBOUND 2026; Agent Hub) | ? | Draft | Human | ? | ? | **L2** | Not found | – | Weak |
| **Klaviyo Composer** (public beta 30 Jun 2026) | Draft | Draft | Human: "nothing goes live without human approval" [S] | ? | ? | **L2** ("autonomous drafting") | Not found for Composer | – | Medium |
| **Iterable Nova Agent** (Apr 2026) | Auto claimed (goal-driven) | Draft/Auto claimed | ? | Auto claimed (runs experiments) | ? | L2–L3 (approval mechanism not documented) | No customer metrics published [S] | – | Weak |
| **Optimove AI Decisioning Studio** (27 Mar 2026) | Auto (Journey, Offer, Content and Send-Time agents) | Draft (OptiGenie, Jan 2026) | Auto | Auto (self-optimising A/B/n) | Built-in control group that shrinks as a campaign "proves itself" (prior notes, not re-verified) | **L3** | Vendor | Not found | Weak to medium |
| **Bloomreach Loomi marketing agent** (GA 2 Jun 2026) | Draft/Auto | Draft | Review controls and audit trail | Auto claimed | ? | **L2–L3** | One vendor case with no method | – | Weak |
| **CleverTap CleverAI** (decisioning engine plus agents) | Auto (decisioning engine) | Draft | "At your chosen trust level", with HITL approvals | Auto | ? | **L2–L3** (customer-set) | Vendor claims, no method | **Home market** | Weak to medium |
| **Netcore.ai** (relaunched 30 Jul 2026) | Auto claimed (seven agents) | Auto claimed | Auto claimed, with "human growth engineers" | Auto claimed | Fee tied to "agreed business KPIs" | L3 claimed, delivered as a managed service | No published results [S] | **Home market** | Weak |
| **WebEngage** | – | – | – | – | – | **No agent launch found** in 2026 | – | Home market | Weak (absence) |
| **Insider One Agent One** (+ Bluecore, 13 May 2026) | Auto claimed | Draft | ? | Auto claimed | ? | L2–L3 | Vendor | Not confirmed | Weak to medium |
| **Movable Ink Da Vinci Agentic Campaigns** (Jun 2025) | Auto (per-person message, subject line, time, frequency) | Human (creative is uploaded) | Auto within quiet hours and frequency caps | Auto | ? | **L3** | Vendor average "+20% revenue lift", no method | – | Weak |
| **Persado** | – | Auto-generated language, then tested | Human | Auto (testing) | Vendor | L2 | "$1.5B incremental revenue" (2022 vendor claim); no RCT found | – | Weak |
| **Albert.ai (Zoomd)** | Auto (paid search and social budgets) | Auto (variants) | Auto | Auto | Platform | L3–L4 (ads) | No 2025–26 information found | – | Weak |
| **Hightouch AI Decisioning** | Auto (RL per customer) | – | Through downstream tools | Auto | Not found | **L3** | Not found | Not found | Weak to medium |
| **Twilio Segment** | Predictions only | Generative audiences (draft) | – | – | – | **L1** | Not applicable | – | **Strong** (opened docs) |
| **Startups 2025–26** (JustAI, Nectar Social, Epiminds, Gradial) | Mostly multi-agent workflow | Draft/Auto | Varies | Claimed | Not found | L2–L3 claimed | Not found | None found | Weak |

**What the table shows:**
- **Only the ad platforms run at L4 in production.** Every CRM or engagement vendor that "sends" autonomously does so at **L3**. The machine picks *which approved thing, when, on which channel and how often* for each person. It does not write and publish new things on its own.
- **Every LLM "campaign agent" launched in 2025–26 is L2.** That covers Salesforce, Adobe, HubSpot, Klaviyo, Bloomreach and Braze's Operator: the agent drafts and a human launches.

---

## 2. Findings by vendor (newer and deeper than the earlier notes)

### 2.1 The ad platforms: the most autonomous marketing systems in production

**Meta**
- **Advantage+ is now the default.** Meta began defaulting Sales, App and Leads campaigns to Advantage+ in 2025. As of early 2026, interest and demographic inputs are only "signals"; location and minimum age remain hard limits — [AdTaxi 2026 guide](https://www.adtaxi.com/blog/metas-ai-advertising-plans-what-to-expect-in-2026-and-how-to-prepare/) [S], [Digital Applied](https://www.digitalapplied.com/blog/meta-ai-automated-ads-2026-marketing-guide) [S]. *Medium* (two secondary sources agree).
- **Full automation by end-2026 is a reported target, not a commitment.** Meta's goal of building whole ads from a product image and a budget comes from a 2025 Wall Street Journal report — [MediaPost, 2025](https://www.mediapost.com/publications/article/406315/meta-plans-to-automate-all-ad-creation-by-2026-re.html?edition=138683) [S]. I found no Meta primary statement. *Medium for the report; weak for the date.*
- **Advantage+ beat manual campaigns early but lost by the end of each test.** Haus ran 640 Meta incrementality experiments (Jan 2024 to Jun 2025, average 27.4 days):
  - Meta drove about **19%** lift on brands' primary KPI on average.
  - Advantage+ was **9% better than manual at the experiment midpoint but 12% worse by the end**.
  - Trade press reports that **58% of brands** got higher incremental ROAS from manual campaigns.
  - Sources: [Haus Meta report](https://www.haus.io/meta) [S], [Haus: Is Meta incremental?](https://www.haus.io/blog/is-meta-incremental) [S], [WARC](https://www.warc.com/content/feed/metas-advantage-drives-strong-short-term-incrementality-but-weakens-in-the-longer-term/en-GB/10833) [S], [MediaCat](https://mediacat.uk/marketers-best-metas-black-box-report-shows/) [S] (source of the 58%).
  - *Medium.* Haus is independent of Meta but sells incrementality testing, and its sample skews to larger brands.
- **Acquisition costs reportedly doubled.** Wicked Reports' data on 55,661 campaigns reportedly shows Advantage+ new-customer acquisition cost rising from $257 (May 2024) to $528 (May 2025), while manual campaigns got cheaper — reported via [admanage.ai](https://admanage.ai/blog/meta-advantage-plus-shopping-campaigns) [S]. *Weak* (one secondary source).

**Google**
- **AI Max is out of beta and absorbing older search formats.** AI Max for Search left beta in April 2026. Dynamic Search Ads are being upgraded to AI Max — [Google blog: DSA upgrade to AI Max](https://blog.google/products/ads-commerce/dsa-upgrade-to-ai-max-2026/) [S], [hawky.ai](https://hawky.ai/blog/google-ads-updates-2026) [S]. *Medium.*
- **Advertisers may have no opt-out.** One September 2026 article reports search campaigns being moved to AI Max with "no opt-out" and overridden settings — [Veonib](https://veonib.com/articles/2026-09-12/google-ads-forced-ai-max-upgrade-what-advertisers-face-in-20.html) [S]. *Weak* (single secondary source).
- **Platform reporting overstated Performance Max.** In a three-cell geo test, Caraway found Google's own reporting overstated PMax impact by about **33%** — [Haus PMax brand experiment](https://www.haus.io/blog/pmax-experiments-including-excluding-branded-search-terms) [S]. *Medium* (vendor-hosted case study; the design is stated).
- **The "Ask Advisor" agent's powers are undocumented.** The Gemini agent was announced at Google Marketing Live 2026, but no primary source describes what it may change by itself — [hawky.ai](https://hawky.ai/blog/google-ads-updates-2026) [S]. *Weak.*

**Why it matters.** These are the only systems that set budget-bounded spend, targeting, creative assembly and delivery with no per-campaign human approval. They got there partly through **defaults and forced migration**, not only because they proved better. See §6.

### 2.2 Braze: the decisioning acquirer

**Q2 FY2027 results (reported 8 Sep 2026):**
- Revenue was **$227.2M, up 26.2%** — [Braze Q2 FY27 earnings release (Q4CDN PDF)](https://s25.q4cdn.com/307167611/files/content_files/2026-07-31-Braze-Inc-Q2-27-Earnings-Release-FINAL.pdf) [S], [Pulse 2.0](https://pulse2.com/braze-q2-revenue-rises-26-to-227-2-million-as-ai-adoption-accelerates/) [S].
- The CEO said adoption of **BrazeAI Operator, Agent Console and Decisioning Studio** is accelerating "as customers seek proof of ROI".
- **Paid AI adoption reached about one third of large customers**, up about 900 basis points in a quarter.
- The full-year FY27 guide is $910–913M.
- *Medium.* This also corrects the earlier note, which could not confirm the product names. They are Operator, Agent Console and Decisioning Studio, not "Project Catalyst".

**Forge 2026 (28–30 Sep 2026)** — [Braze Forge 2026 announcements](https://www.braze.com/resources/articles/forge-2026-braze-product-announcements) [S], [CMSWire recap](https://www.cmswire.com/customer-experience/braze-forge-2026-recap-self-service-speed-and-autonomy-require-governance/) [S]. *Medium.*
- **Decisioning Studio Go** lets marketers deploy an AI decisioning agent on e-mail without engineering or data-science help (GA expected October 2026). Braze is taking per-user RL down-market.
- **Operator Connect (beta)** lets marketers analyse, plan and build Braze campaigns from Claude, ChatGPT or Copilot over MCP, under their existing Braze permissions.
- **Agentic Standards** automatically QAs every campaign and Canvas before anything reaches a customer: setup, copy, links, personalisation logic, and brand or regulatory compliance. Each check returns pass, warning or fail and is logged for audit. *A vendor building a pre-send checker is itself evidence that unattended sends are seen as risky.*
- **Conversational Agents** run on WhatsApp, SMS, RCS and web.

**Published results, and who measured them:**
- **Forrester TEI, April 2026:** **457% ROI over three years, NPV $23.5M, payback under six months** — [Forrester TEI for Braze](https://tei.forrester.com/go/Braze/AIDecisioningSpotlight/?lang=en-us) [S].
  - **Commissioned by Braze.** It models a *composite* organisation built from customer interviews; it is not a measured trial. *Medium that it was published; weak as evidence of lift.*
- **Nestlé Purina:** about **28% cross-sell lift against holdout**, and a program targeting 15% conversion reached 24% in its first quarter — [Relationship One Forge recap](https://relationshipone.com/blog/braze-forge-2026-wrap-up-the-announcements-that-matter-and-what-to-do-about-them/) [S]. *Weak* (third-party blog summarising a conference talk; holdout size and metric window unknown).
- **RTL+:** about **21% uplift in watch engagement versus a no-message holdout**, from Agent Console, not Decisioning Studio — Braze page reported via search [S]. *Weak.* It is also an engagement metric, not revenue.
- **The earlier "$250–300k per use case" price** for Decisioning Studio was already marked "not found" in the 10 Oct verification. Nothing new was found.

### 2.3 Adobe: what the documentation actually says [O]

Source: Adobe's public documentation repos on GitHub. Journey Optimizer docs are at commit `6b3013f` (11 Oct 2026); CX Enterprise AI docs at commit `81f9725` (8 Oct 2026). **All strong.**

**1. Agents draft; humans launch.**
- **Coworker Campaigns** "takes you from a single prompt to a complete **review-ready** campaign". The interface says "**Always check outputs before sending**" — [Coworker Campaigns overview](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/overview.md) [O].
- Launch is a human click on **Review and launch**. A campaign "can't launch with a sample (non-real) audience, email drafts that have not been proofed, or unconfigured sending settings". Stopping a campaign is permanent — [Launch and manage a campaign](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/launch-manage-campaign.md) [O].
- **The trial is narrow:**
  - North America only, ending 31 Dec 2026;
  - audiences come from uploaded CSV files;
  - users are told "do not upload sensitive or regulated data";
  - users are told "you are responsible for following email regulations";
  - a planned send cap of **5,000 e-mails a month**;
  - "launch and analyze" is still "coming soon" on the overview page.
- **Agent Orchestrator** describes its agents as acting "**with human oversight**" — [Agent Orchestrator](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/agents/agent-orchestrator.md) [O].
- **Journey Agent** "Create" builds journeys from prompts. The release notes describe it as helping "marketers concentrate on strategy" — [AJO release notes 2026](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/rn/release-notes-2026.md) [O], [AI features](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/start/ai-features.md) [O].
- **The Journey Optimizer MCP server is read-only**: "All operations are currently read-only" — [AI features](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/start/ai-features.md) [O].

**2. Autonomy is real only where the action space is a fixed menu.**
- **The "Auto-optimization" model** is described as:
  - "a reinforcement learning model which maximizes offer click-through rate";
  - built from an epsilon-greedy supervisor over **two Thompson-sampling bandits**, with **10% of traffic for exploration and 90% for exploitation**;
  - retrained "every six hours".
- **Data thresholds for auto-optimization:** an offer needs **100 displays and 5 clicks in 14 days** before it is treated as known. Until the first model is trained, offers are served at random — [Auto-optimization model](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/auto-optimization-model.md) [O].
- **"Personalized optimization"** is a deep-learning ranker that can optimise continuous outcomes such as revenue.
  - Its training floor is at least two offers with 250 displays and one success event in 30 days — [Personalized optimization model](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/personalized-optimization-model.md) [O].
  - The overview page recommends "at least 1,000 impressions and 100 conversion events per offer per week" and by default trains no model for an offer with fewer than 1,000 impressions or 50 conversions — [AI features](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/start/ai-features.md) [O].
  - **Those volumes are out of reach for most mid-market CRM programmes.** This is one of the root causes in §6.

**3. Adobe's "holdout" in decisioning is not a no-message control.**
- The "Conversion rate for Holdout and Model Driven traffic" report defines **Holdout** as "users receive content **served randomly** during the model's exploration phase" — [Campaign report](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/campaign-global-report-cja-code.md) [O].
- So it measures *model versus random offer*, not *message versus no message*.
- It does not even report when the AI model sits inside a ranking formula — [Ranking formulas](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/ranking-formulas.md) [O].

**4. The true no-message holdout exists, but is local or new.**
- **Content experiments** offer a holdout (10% by default). It "only applies to that specific action". Afterwards, held-out profiles "can receive messages from other actions" — [Content experiment](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/content-management/content-experiment.md) [O].
- **A journey-level holdout**, explicitly "to measure the incremental lift — the true impact", is **Limited Availability from 1 September 2026**, by request only — [AJO release notes](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/rn/release-notes.md) [O].
- **The earlier note was wrong to imply this existed.** It cited a 2024 community answer saying journey-level holdouts were "on the roadmap". They arrived only in September 2026, and only in limited release.

**5. Send-time optimisation is properly randomised, but measured on engagement.**
- The new STO lift report compares against a "Control variant (send time assigned at random)" using a **two-proportion Z-test on open or click rate**, over 60 days — [Channel report](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/channel-report-cja.md) [O].
- A sound design, but **for opens and clicks, not revenue**.

### 2.4 MoEngage + Aampe (India's flagship)

- **The deal.** MoEngage announced it was acquiring Aampe on 23–24 June 2026, in an all-cash deal with terms undisclosed. Aampe's founders lead a new "Agentic Decisioning" group. The release says each customer's agent uses "**reinforcement learning using Thompson Sampling and multi-armed bandits per individual**", plus "semantic learning and network intelligence" — [MoEngage/PR Newswire via Yahoo Finance](https://finance.yahoo.com/technology/ai/articles/moengage-acquires-aampe-bring-1-020000491.html) [S], [CMSWire](https://www.cmswire.com/customer-experience/moengage-buys-aampe-to-add-peruser-ai-agents/) [S]. *Medium.*
- **Scale claim:** "more than 200 billion decisions per week". This is company-stated; one analysis notes it is not audited — [Digital Applied](https://www.digitalapplied.com/blog/moengage-aampe-per-customer-ai-agents-martech-2026) [S]. *Weak as a fact.*
- **Correction to the earlier note**, which said "no named Indian deployments of agentic decisioning". **Swiggy runs MoEngage plus Aampe** — [MoEngage case study](https://www.moengage.com/casestudy/swiggy-scales-campaigns-with-moengage-and-aampe/) [S]:
  - **3× faster campaign go-live, 2× in-app CTR, 6,000+ campaigns a month across 11 business units and 5 apps**;
  - Swiggy's growth lead: marketers "used to schedule campaigns, and now they manage agents to run them".
  - **Who measured it:** MoEngage, the vendor. **Baseline and design: not stated** in what I saw.
  - "2× CTR" is an engagement metric, and "3× faster" is operational speed. **Neither is incremental revenue.** *Medium that it exists; weak as evidence of value.*
- **Pricing signals conflict.**
  - A G2 listing shows a free Starter tier up to 10,000 monthly tracked users.
  - A comparison site says the paid plan starts at $999 a month for 10,000 MAU.
  - An Indian blog estimates INR 3–5 lakh a year, plus INR 1–2 lakh for WhatsApp.
  - Sources: [G2](https://www.g2.com/compare/clevertap-vs-moengage) [S], [Sequenzy](https://www.sequenzy.com/versus/moengage-vs-clevertap) [S]. *Weak.*

### 2.5 Salesforce, HubSpot, Klaviyo, Bloomreach, Iterable: LLM campaign agents at L2

**Salesforce (Connections, 3–4 June 2026)**
- **What is live and what is in pilot:**
  - **Agentic Segmentation** (prompt to segment) was the headline;
  - campaign management through MCP in Slack is GA (June 2026);
  - the **Content Agent** and the **Marketing Goals / Marketing Expert Agent**, which "takes goals, budget and guardrails" to build, launch and optimise campaigns, are **in pilot**.
- **Stated stance:** Salesforce says humans still set strategy and make decisions.
- Sources: [Salesforce Connections takeaways](https://www.salesforce.com/blog/connections-conference-takeaways/) [S], [Salesforce Ben](https://www.salesforceben.com/how-many-ai-agents-is-too-many-salesforce-adds-four-more-at-connections/) [S], [Salesforce newsroom](https://www.salesforce.com/news/stories/agentic-marketing-teams-announcement/) [S]. *Medium.*
- **Pricing:** no marketing-specific rate card was found. Generic Agentforce Flex Credits cost $500 per 100,000 credits (about $0.10 per action) — [eesel](https://www.eesel.ai/blog/is-salesforce-agentforce-worth-the-cost) [S]. *Weak to medium.*

**HubSpot**
- **The renaming.** INBOUND is now **UNBOUND** (16–18 Sep 2026). "Breeze" is retired as an umbrella brand; Breeze Agents is now **Agent Hub** (public beta, Professional and Enterprise).
- **New agents:** a **Campaign Agent**, **Nurture Agent** and **Content Agent** are among eight new or updated agents — [HubSpot Fall 2026 Spotlight](https://www.hubspot.com/company-news/fall-26-spotlight) [S], [Spitfire](https://blog.spitfireinbound.com/new-features-and-agents-announced-at-hubspots-unbound26) [S].
- **I could not confirm the Campaign Agent's autonomy or price.** A second search found nothing on it. *Weak.*
- **Pricing:** credits cost $10 per 1,000, shared across agents — [eesel](https://www.eesel.ai/blog/hubspot-ai-agent-pricing-2026) [S]. *Medium.*

**Klaviyo Composer**
- **Public beta on 30 June 2026.** Klaviyo says nothing goes live without human approval. A third-party review calls it "autonomous drafting", not "autonomous publishing" — [Klaviyo newsroom](https://www.klaviyo.com/newsroom/composer) [S], [Influencers-Time review](https://www.influencers-time.com/klaviyo-composer-and-customer-agent-review-is-it-autonomous/) [S]. *Medium.*
- **Usage.** On the Q2 call (5 Aug 2026), Composer passed **95,000 users in its first month, nearly a quarter of them weekly** — [Klaviyo Q2 2026 transcript (Q4CDN)](https://s25.q4cdn.com/801988311/files/doc_financials/2026/q2/Q2-2026-Klaviyo-Transcript.pdf) [S]. A later release says 138,000. *Medium.*
- **Customer Agent (service, not marketing):**
  - Naked Wardrobe reported 86% autonomous resolution (vendor);
  - Boston Proper reported 3× ROI "in incremental revenue within two weeks" (management claim, method unknown);
  - an independent look at three mid-market retailers put resolution at about 70–75%.
  - Sources: [Klaviyo blog](https://www.klaviyo.com/blog/klaviyo-composer-customer-agent-launch) [S], [MarketBeat](https://www.marketbeat.com/instant-alerts/klaviyo-q2-earnings-call-highlights-2026-08-08/) [S]. *Weak.*

**Bloomreach**
- **Loomi marketing agent GA on 2 June 2026.** It turns a prompt into a campaign workflow, with review controls and audit trails. This corrects the earlier note's "date unconfirmed".
- **The only result:** **260 Sample Sale got 2.4× conversion rate while targeting 82% fewer customers** — [Bloomreach release](https://www.bloomreach.com/en/news/2026/bloomreach-announces-the-general-availability-of-loomi-marketing-agent/) [S].
  - A conversion *rate* on a smaller, better-chosen group does not show more conversions in total. *Weak.*

**Iterable**
- **Nova Agent (22–23 April 2026)** is described as goal-driven AI that "automatically builds campaigns, audits messages, runs experiments". **No customer metrics were published.**
- Nova Conversations enters private beta in October 2026.
- Sources: [DestinationCRM](https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Iterable-Unveils-Nova-Agent-and-AI-Innovations-174516.aspx) [S], [ContentGrip](https://www.contentgrip.com/iterable-nova-agent-ai-innovations/) [S]. *Medium.*

### 2.6 Optimove, CleverTap, Netcore, WebEngage, Insider One

**Optimove**
- **AI Decisioning Studio launched 27 March 2026** with four agents: Journey, Offer, Content and Send-Time Optimization. The OptiGenie content agent launched 20 January 2026 — [Optimove release via Yahoo Finance](https://finance.yahoo.com/sectors/technology/articles/optimove-launches-ai-decisioning-studio-171500454.html) [S]. *Medium.*
- **No product called "Optimail" was found.** Treat the name in the brief as unverified.

**CleverTap**
- **CleverAI** has Strategy, Decision, Creative and Action agents. The Action agents execute "at your chosen trust level", gated by **human-in-the-loop approvals and RBAC** — [CleverTap CleverAI agents blog](https://clevertap.com/blog/meet-cleverai-agents-powering-the-next-chapter-of-customer-engagement/) [S], [CleverTap tech stack blog](https://clevertap.com/blog/inside-cleverai-the-technology-stack-behind-live-11-personalization/) [S]. *Medium.*
- **It is a Gartner MQ Leader** for Personalization Engines (2026).
- **Pricing:** Essentials costs $75 a month for 5,000 MAU (third-party) — [toolradar](https://toolradar.com/tools/clevertap/pricing) [S]. *Weak.*

**Netcore.ai**
- **No customer results tied to its outcome-based model have been published** since the 30 July 2026 relaunch — [Renascence](https://www.renascence.io/news/9328/netcore-ai-rebrands-as-agentic-marketing-platform-with-outcome-based-model) [S]. *Medium (absence).*

**WebEngage**
- **No agent or agentic launch was found in 2026.** A competitor's comparison describes it as offering analytics and personalisation AI only — [Netcore comparison](https://netcore.ai/blog/webengage-vs-moengage/) [S]. *Weak (absence, and the source is a competitor).*

**Insider One**
- **The Bluecore acquisition (13 May 2026) is now confirmed** by Bluecore's own press page. Bluecore serves "more than 400 US enterprise brands" — [Bluecore press](https://www.bluecore.com/press/insider-one-acquires-bluecore/) [S], [PR Newswire](https://www.prnewswire.com/news-releases/insider-one-the-leading-agentic-ai-platform-powering-autonomous-end-to-end-customer-engagement-acquires-bluecore-302769645.html) [S]. *Medium.* This upgrades the earlier "lightly verified".
- **The price is unconfirmed.** It was not disclosed; a $1B figure on multiples.vc is unconfirmed.

### 2.7 Persado, Movable Ink, Albert, Hightouch, Segment

**Persado**
- **Nothing new in 2026.** The latest public impact claim is "$1.5B incremental revenue" (2022, vendor). No published RCT was found — [Gartner Peer Insights listing](https://www.gartner.com/reviews/product/persado) [S]. *Weak.*

**Movable Ink**
- **Agentic Campaigns (June 2025)** work in three steps: upload creative, set guardrails, choose objectives. The AI then picks message, subject line, send time and frequency per person, within quiet hours and frequency caps.
- **Results:** Currys is the first UK customer. The only result is a vendor average of "+20% revenue lift" — [StreetInsider/PRN](https://www.streetinsider.com/PRNewswire/Movable+Ink+Unveils+Autonomous+Marketing+Capabilities/24945917.html) [S]. *Weak.*

**Albert.ai**
- **Zoomd acquired it on 27 March 2022.** It allocates paid search and social budgets autonomously. Pricing is on request. **I found nothing on it dated 2025–26** — [Martechvibe](https://martechvibe.com/article/zoomd-acquires-ai-marketing-platform-albert-technologies) [S]. *Weak.*

**Hightouch**
- **Funding.** It raised a **$150M Series D at $2.75B (29 April 2026)**, led by Goldman Sachs Alternatives and Bain Capital Ventures. That is up from $80M at $1.2B (Feb 2025) — [PYMNTS](https://www.pymnts.com/news/investment-tracker/2026/hightouch-valued-at-2-75-billion-as-ai-agents-transform-enterprise-marketing/) [S], [TechCrunch 2025](https://techcrunch.com/2025/02/18/hightouch-raises-80m-on-a-1-2b-valuation-for-marketing-tools-powered-by-ai) [S]. *Medium.*
- **Method:** AI Decisioning is reinforcement learning.
- **Measurement:** I found no documentation of holdouts.
- **ARR:** a Sacra estimate puts it at about $100M — [Sacra](https://sacra.com/c/hightouch/) [S]. *Weak.*

**Twilio Segment**
- **SIGNAL 2026 announced four GA conversation products**: Memory, Orchestrator, Intelligence and Agent Connect. **None is a Segment marketing agent** — [BlogGeek](https://bloggeek.me/twilio-signal-2026/) [S]. *Medium.*
- **Segment's own docs show prediction only.** "Predictions" are written to profiles for others to act on, and need history covering three times the target window (for example 90 days for a 30-day window) — [Segment Predictions docs](https://github.com/segmentio/segment-docs/blob/develop/src/unify/Traits/predictions/index.md) [O].
- **Segment publishes "AI Nutrition Facts" labels** for its AI features — [Predictions Nutrition Facts](https://github.com/segmentio/segment-docs/blob/develop/src/unify/Traits/predictions/predictions-nutrition-facts.md) [O]. This is a useful precedent for an evidence label. *Strong*, though the repo was last committed in Nov 2025.

### 2.8 Startups and funding, 2025–26

| Company | Round | Date | What it does | Source | Confidence |
|---|---|---|---|---|---|
| Hightouch | $150M Series D at $2.75B | Apr 2026 | RL decisioning plus composable CDP | [PYMNTS](https://www.pymnts.com/news/investment-tracker/2026/hightouch-valued-at-2-75-billion-as-ai-agents-transform-enterprise-marketing/) [S] | Medium |
| Gradial | $65M Series C (≈$675M valuation) | ~Jun 2026 | Agentic content operations | [SiliconANGLE (search listing)](https://siliconangle.com/?p=772842) [S] | Weak |
| Nectar Social | $30M Series A (Menlo's Anthology fund) | May 2026 | "Agentic OS" for social marketing | [BusinessWire](https://www.businesswire.com/news/home/20260513604281/en/Nectar-Social-Raises-$30M-Series-A-to-Build-the-Agentic-Operating-System-for-Modern-Marketing) [S] | Medium |
| JustAI | $17M Series A (Base10; YC, Peak XV) | Jun 2026 | Multi-agent "autonomous marketing" for lifecycle; claims 5× ARR growth | [PR Newswire](https://www.prnewswire.com/news-releases/justai-raises-17m-series-a-to-build-an-agentic-marketing-platform-302807090.html) [S] | Medium |
| Epiminds | $6.6M (Lightspeed) | Oct 2025 | An "AI marketing manager" over 20+ agents | [Seedtable](https://seedtable.com/companies/epiminds) [S] | Weak |
| Eikona | $5M seed | Date unknown | Generative lifecycle messaging | [Sovereign Magazine](https://www.sovereignmagazine.com/startups/epiminds-agentic-ai-marketing-industry/) [S] | Weak |

**India:** I found **no funding round for an Indian agentic-marketing or WhatsApp-marketing agent startup in 2026**. Indian AI startup funding totals for H1 2026 conflict: **$676M** (Inc42) against **$1,067M** (Venture Intelligence) — [Inc42](https://inc42.com/features/indian-ai-startup-funding-soars-over-4x-yoy-in-h1-2026-but-is-it-enough-to-compete-globally/) [S], [StartupFeed](https://startupfeed.in/indian-ai-startup-funding-h1-2026/) [S]. *Medium (that they conflict).*

---

## 3. The autonomy spectrum in practice: what runs unattended and where humans stay

**What vendors really let run unattended**, with evidence:
1. **Choosing among pre-approved options for each person**: offer, variant, time, channel, frequency.
   - Adobe's bandits: 10% explore, 90% exploit, retrained every six hours [O].
   - Braze Decisioning Studio and Decisioning Studio Go [S].
   - Aampe and MoEngage: Thompson sampling per user [S].
   - Movable Ink, within quiet hours and frequency caps [S].
   - Optimove: self-optimising A/B/n [S].
2. **Send-time optimisation**, inside a window a human set. Adobe's lift report randomises the send time [O].
3. **Ad delivery inside a budget**: bids, placements, query matching, audience expansion and asset assembly (Google, Meta).
4. **Service conversations**, as opposed to marketing: Klaviyo Customer Agent and Braze Conversational Agents [S].

**Where vendors keep a human**, with evidence:
1. **Publishing anything new.** Klaviyo: "nothing goes live without human approval" [S]. Adobe Coworker: "Review and launch", proofed drafts only, "always check outputs before sending" [O]. CleverTap: HITL approvals [S]. Bloomreach: review controls [S]. Salesforce: humans set strategy [S].
2. **Goals, budgets and guardrails.** Every vendor, including Meta and Google, asks a human for the objective, budget, brand rules and frequency caps.
3. **Writing to production systems from outside tools.** Adobe's MCP server is read-only [O]. Braze Operator Connect works under existing permissions and is in beta [S]. The earlier notes found MoEngage's MCP keeping SMS and WhatsApp campaigns read-only.
4. **QA before sending is being automated, not dropped.** Braze "Agentic Standards" checks every campaign against marketer-defined rules before it reaches a customer [S].

**The pattern in one line.** *Machines own the selection; humans own the substance and the launch.* An L4 "goal in, campaign out, nobody approves" system exists only in paid ads, and only inside a spending cap. Forrester expects fully autonomous agents working without human review to be "years away", with benefits arriving in two to five years — [Forrester blog](https://www.forrester.com/blogs/the-odyssey-ahead-b2c-marketers-prepare-now-for-agentic-ai) [S]. *Medium.*

---

## 4. Evidence quality: published lifts, who measured them, and failures

### 4.1 Published results, graded

| Claim | Who measured it | Design and baseline | Size | Metric type | Grade |
|---|---|---|---|---|---|
| Aampe: +65.3% push-open days, +0.3% (±0.24) conversion days | Aampe staff (arXiv 2604.08621) | 11-month persistent 10% holdout against business-as-usual | ~8.8M users, one anonymised app | Engagement big, conversion tiny | Medium (vendor-authored, but a real RCT) |
| Haus: Meta +19% average lift; Advantage+ 9% better at midpoint, 12% worse at end | Haus (independent of Meta; sells testing) | 640 geo experiments, Jan 2024–Jun 2025 | Many brands, skewed large | Primary business KPI | Medium |
| Caraway: Google reporting overstated PMax by ~33% | Haus platform, Caraway data | Three-cell geo lift test | One brand | Revenue | Medium (single case, vendor-hosted) |
| PIE method: R² 0.88 for incremental conversions per dollar, against 0.19 for 7-day last-click | Academic (Gordon, Moakler, Zettelmeyer; NBER w35044) | Built on Meta experiments | Many experiments | Incremental conversions | Medium [S], via [NBER](https://www.nber.org/papers/w35044) and [arXiv 2607.09608](https://arxiv.org/pdf/2607.09608) |
| Braze TEI: 457% ROI, $23.5M NPV | Forrester, **commissioned by Braze** | Composite organisation, modelled | Interviews | Modelled ROI | Weak as proof |
| Purina: ~28% lift against holdout | Braze or the customer, reported by a blog | Holdout; size unknown | Unknown | Cross-sell | Weak |
| RTL+: ~21% against no-message holdout | Braze | No-message holdout | Unknown | Watch engagement | Weak |
| Swiggy: 2× in-app CTR, 3× faster go-live | MoEngage | Not stated | 6,000+ campaigns a month | Engagement and operations | Weak |
| 260 Sample Sale: 2.4× conversion rate, 82% fewer targeted | Bloomreach | Not stated | One campaign | Rate, not total | Weak |
| Klaviyo Boston Proper: 3× ROI in "incremental revenue" | Klaviyo management | Not stated | One brand, two weeks | Revenue | Weak |
| Movable Ink "+20% average revenue lift"; Persado "$1.5B"; CleverTap "+66% conversion"; Optimove "88% faster execution" | Vendors | Not stated | – | Mixed | Weak |
| Adobe STO lift | Adobe's product, run by each customer | Random send-time control, two-proportion Z-test [O] | Per customer | Opens and clicks only | Strong on method; no published numbers |

**What the pattern shows.** The more independent and the longer the measurement, the smaller the claimed effect.
- **Independent or long tests:** Haus end-of-test, Aampe conversion days, and (from the earlier notes) Mercari −0.42% and Nextdoor +1%.
- **Vendor or short-window figures:** Aampe engagement +65%, TEI 457%, CTR multiples.
- **Engagement is the dominant reported metric.** Only Aampe's paper and Haus's tests report a money outcome against a randomised counterfactual.

**Even "incrementality" tools disagree a lot.** I opened the result tables of Recast's simulation study (32,000 model fits, true effect about 7.5%):
- **False positives:** in the textbook scenario, the four common geo-test tools gave false-positive rates from **4.6% (Meta GeoLift) to 27.8% (CausalImpact)**;
- **Missed effects:** they missed a real 7.5% effect between **38% and 91%** of the time;
- **Coverage:** confidence intervals contained the truth only **72–92%** of the time — [Recast geolift-simulation-study, results/aggregated/table_A1.md](https://github.com/getrecast/geolift-simulation-study) [O].

Recast sells MMM, so it has an interest in this result. **Strong on the numbers it reports.**

**What this means.** A "proven lift" depends heavily on the method. A neutral party has to own the method choice and the error rates.

### 4.2 Failures, backlash and rollbacks

**Meta Advantage+ creative.**
- True Classic's best ad was replaced by an AI-generated "grandma" for a campaign aimed at men aged 30–45 (Business Insider, reported second-hand).
- Other advertisers reported a model with a twisted leg and other distorted images.
- Creative Enhancements are **on by default**, and advertisers say disabled settings switch themselves back on, including in duplicated ads.
- Sources: [Shopifreaks](https://www.shopifreaks.com/meta-advantage-is-forcing-unwanted-ai-creative-on-brands/) [S], [WebProNews](https://www.webpronews.com/metas-advantage-ai-creates-bizarre-ads-frustrating-marketers/) [S], [NiCreated](https://www.nicreated.com.au/meta-ads-updates-2026-ai-creative) [S]. *Medium* (several trade sources agree; the original BI article was not seen).

**Meta's 2023 overspend glitch.**
- An automated system overspent budgets within hours, and CPAs roughly tripled.
- Refunds were slow and inconsistent, and small advertisers were sent to chatbots — [Search Engine Land](https://searchengineland.com/meta-advertising-overspending-glitch-refunds-425771) [S], [Bloomberg](https://www.bloomberg.com/news/articles/2023-05-16/meta-starts-refunding-advertisers-after-glitch-but-small-businesses-get-chatbots) [S]. *Medium.* It is old (2023), but it shows that bounded spend is the safety net ad autonomy relies on.

**Google forced migration.**
- **AI Max:** the reported upgrade with no opt-out — [Veonib](https://veonib.com/articles/2026-09-12/google-ads-forced-ai-max-upgrade-what-advertisers-face-in-20.html) [S]. *Weak.*
- **Auto-applied recommendations:** the account-level opt-out is "hit or miss" — [Search Engine Land](https://searchengineland.com/tactical-advice-on-google-ads-auto-applied-recommendations-control-center-345740) [S]. *Medium.*

**A CRM agent error (single customer).**
- In July 2026, Who Gives A Crap suspended its AI e-mail agent after it wrongly *confirmed* a price more than double the real one to a customer. The mistake started with a typo in a price-rise e-mail — [vibegraveyard](https://vibegraveyard.ai/story/who-gives-a-crap-ai-email-price-error/) [S]. *Weak* (an aggregator; one customer; no charge made).
- **I found no documented case of an autonomous CRM agent mass-sending wrong or non-compliant messages.** That is an absence of evidence, likely because sends are still human-launched (§3).

**AI-made ads pulled.** These were made with AI but launched by humans, so they are not autonomy failures. They show the brand risk of AI content.
- McDonald's Netherlands pulled its AI Christmas ad (Dec 2025).
- Equinox pulled a poster on 28 July 2026.
- Valentino's and Coca-Cola's AI campaigns drew criticism.
- Sources: [TechRadar](https://www.techradar.com/ai-platforms-assistants/mcdonalds-pulls-ai-generated-christmas-ad-after-backlash-over-soulless-visuals-and-holiday-chaos) [S], [One Media Society](https://www.onemediasociety.com/journal/ai-advertising-backlash-wellness-hospitality-brands) [S]. *Medium.*

**Regulators.**
- **US FTC: "AI-washing" cases.** On 21 May 2026, an action over an AI "Active Listening" marketing tool was, by DLA Piper's count, the **13th AI-washing case since 2024**, and seven of the last eight involved B2B marketing claims.
  - Growth Cave (Jan 2026) was charged with marketing "AI software" as automating work users actually did by hand.
  - Workado (Aug 2025) was ordered to hold "competent and reliable evidence" for its accuracy claims.
  - Sources: [DLA Piper](https://www.dlapiper.com/insights/publications/2026/05/ftc-ai-washing-action-underscores-enforcement-in-business-to-business-context) [S], [Frankfurt Kurnit](https://advertisinglaw.fkks.com/post/102l358/ftcs-ai-crackdown-continues-workado-ordered-to-drop-unsupported-accuracy-claims) [S]. *Medium.*
  - **Relevance:** unsupported "autonomous" and "lift" claims are now an enforcement risk for vendors.
- **India TRAI, Q1 FY2026-27 (April–June 2026):**
  - **263 senders blacklisted for a year**;
  - **2,873 SMS headers blocked**;
  - **1,37,053** telecom resources barred for 15 days, and **46,786** disconnected;
  - about 1.09 million complaints, of which 5.53 lakh were actionable.
  - Source: [TRAI press release 111/2026](https://www.trai.gov.in/sites/default/files/2026-08/PR_No111of2026.pdf) [S]. *Medium.*
- **India ASCI, AI labelling in ads:**
  - **Draft published 8 May 2026**, with risk tiers. Fabricated testimonials, AI "doctors" and non-consensual deepfakes are banned whatever the label — [Khaitan & Co](https://www.khaitanco.com/sites/default/files/2026-05/ERGO_ASCI%20Draft%20Guideline_25%20May%202026.pdf) [S]. *Medium.*
  - **Final guidelines reported on 29 September 2026**, in effect three months later — [GKToday](https://www.gktoday.in/asci-issues-ai-generated-advertising-labelling-guidelines/) [S]. *Weak* (only low-authority sources).
- **EU AI Act Article 50 (transparency).**
  - Applied from 2 August 2026, and the Digital Omnibus did not delay it. The marking of synthetic content may have a grace period to 2 December 2026 — [Al Jazeera](https://www.aljazeera.com/economy/2026/8/6/what-came-into-force-with-the-eus-ai-act-this-week-and-what-didnt) [S], [compliancehub](https://compliancehub.wiki/eu-ai-act-article-50-transparency-digital-omnibus-2026/) [S].
  - *Medium on the date; weak on the grace period.*

---

## 5. Market size and growth claims

**No credible standalone "agentic marketing" or "autonomous marketing" market size exists.** Every figure below either sizes a broader category or comes from a firm that sells syndicated forecasts with undisclosed methods. Quote them, if at all, as context.

| Figure | Scope | Source | Strength |
|---|---|---|---|
| ~$46B by 2030, 47% CAGR | Enterprise agentic AI (all functions) | [MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/enterprise-agentic-ai-market-219711254.html) [S] | **Weak**: not marketing-specific; method unknown |
| $24.5B by 2030, 46.2% CAGR | Enterprise agentic AI | [Grand View Research](https://www.grandviewresearch.com/industry-analysis/enterprise-agentic-ai-market-report) [S] | **Weak** |
| $33.24B by 2030, 30.5% CAGR | Agentic AI, global | [MarkNtel](https://www.marknteladvisors.com/research-library/agentic-ai-market.html) [S] | **Weak** |
| $52.62B by 2030, 46.3% CAGR | AI agents (marketing is one role) | [MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/ai-agents-market-15761548.html) [S] | **Weak** |
| $50.31B by 2030, 45.8% CAGR | AI agents | [Grand View via PR Newswire](https://www.prnewswire.com/news-releases/ai-agents-market-size-to-hit-50-31-billion-by-2030-at-cagr-45-8---grand-view-research-inc-302447060.html) [S] | **Weak** |
| CMOs put **15.3%** of marketing budgets into AI; only **30%** say they are ready to scale (n=401) | Survey, May 2026 | [Gartner press release](https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-2026-cmo-spend-survey-finds-cmos-allocate-15-point-3-percent-of-marketing-budgets-to-ai-but-only-30-percent-are-ready-to-scale-ai-capabilities) [S] | **Medium**: a survey, not a market size |
| Marketing budgets at **7.8%** of revenue in 2026 (7.7% in 2025) | Survey | [MarketScale on Gartner](https://www.marketscale.com/industries/marketing-tech/marketing-budgets-hit-78-of-revenue-in-2026-but-ai-visibility-is-redirecting-where-the-money-actually-flows) [S] | **Medium** |
| AI automation of marketing work: **16% (2026) → 36% (2028)**, as leaders expect (n=402) | Expectation | [Gartner press release](https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-survey-reveals-marketing-leaders-expect-ai-automation-of-marketing-work-to-double-to-36-percent-by-2028) [S] | **Medium**, but an expectation, not a measurement |
| Worldwide AI spending **$2.7T in 2026, +49.5%** | All AI | [Gartner press release](https://www.gartner.com/en/newsroom/press-releases/2026-09-16-gartner-forecasts-worldwide-ai-spending-to-grow-49-point-5-percent-in-2026) [S] | **Medium**; far too broad for sizing |
| "Autonomous marketing" named as the emerging operating model | Gartner Hype Cycle for Digital Marketing 2026 (10 Jul) | [MarketScale](https://www.marketscale.com/industries/marketing-tech/gartners-2026-digital-marketing-hype-cycle-frames-ai-spend-as-a-governance-problem-not-a-tooling-race) [S] | **Weak**: secondary report |
| Only **8%** run campaigns with multiple autonomous agents | Attributed to BCG | Same Gartner search, secondary | **Weak** |
| Vendor revenue anchors: Braze FY27 guide **$910–913M**; Klaviyo FY26 guide **$1.526–1.534B**; Hightouch ARR **~$100M** | Real revenue, not "agentic" revenue | Braze and Klaviyo releases [S]; [Sacra](https://sacra.com/c/hightouch/) [S] | **Medium** (Braze, Klaviyo); **weak** (Hightouch) |
| India CRM marketing **services**: **$1.28B (2025) → $1.41B (2026)**, 11.6% CAGR | Services only, not software | [Mordor Intelligence](https://www.mordorintelligence.com/industry-reports/india-crm-marketing-services-market) [S] | **Weak** |
| India martech "₹70,000 crore to nearly ₹1 lakh crore"; martech growth 15–20% (dentsu) | Unaudited, loosely defined | [exchange4media](https://www.exchange4media.com/marketing-news/indias-rsx-crore-martech-stack-economy-where-the-money-is-actually-going-in-2026-154182.html) [S] | **Weak** |

**The most useful demand signal is not a market size.** Braze said a third of its large customers now *pay* for AI tools, up 900 basis points in one quarter, and that customers "seek proof of ROI". Klaviyo's Composer reached 95,000 users in its first month. Paid adoption is real. Proof of value is the stated buying criterion.

---

## 6. Root causes: why ad autonomy succeeded and CRM autonomy lags

These are conclusions drawn from the evidence above. Each states its basis.

**1. Feedback density.**
- Ad auctions give a decision engine billions of decisions with fast, cheap feedback. CRM gives it a few messages per customer per week, with sparse conversions.
- Adobe's own floors show the gap: 100 displays and 5 clicks per offer in 14 days for a bandit, and 1,000 impressions and 50–100 conversions *per offer per week* for the personalised model [O].
- A mid-market Indian brand with a few lakh active customers and a handful of offers cannot feed the personalised tier. It falls back to bandits optimising **clicks**, which are the wrong target.
- This is why per-user RL (OfferFit, Aampe, Hightouch) is sold to large B2C apps (Swiggy, Grab, Yum!), and why Braze needed a self-serve "Go" version to move down-market.
- *Confidence: strong on the thresholds; medium on the inference.*

**2. Bounded, reversible downside.**
- An ad engine's worst case is wasted spend inside a cap, and even a catastrophic 2023 glitch was "only" money, partly refunded.
- A CRM send cannot be recalled. It reaches a known person, can cost an unsubscribe (permanent loss of the channel), and in India can cost the sender itself: 263 senders blacklisted for a year in one quarter.
- So vendors let machines choose freely only among assets a human has already approved. Pre-registered DLT templates and Meta-approved WhatsApp templates *formalise* that rule in India.
- *Confidence: medium.*

**3. Platforms grade their own homework, and autonomy was imposed, not earned.**
- Meta and Google made autonomy the **default** (Advantage+ by default, enhancements on by default, the reported forced AI Max migration).
- Independent tests show their self-reported value is inflated (Caraway ~33%) and that the automated edge fades over a full test (Haus: +9% at midpoint, −12% at end).
- Ad autonomy "succeeded" partly through market power over defaults. CRM vendors lack that power over their customers, who own the channel and the customer relationship.
- *Confidence: medium.*

**4. Optimising what is quick to observe produces engagement, not money.**
- Every autonomous loop optimises the metric it can see quickly: Adobe's bandit maximises CTR; STO measures opens and clicks.
- The money effect, when measured randomly over months, is small: Aampe +65% engagement against +0.3% conversions; Advantage+ fading by the end of each test.
- That is the classic harvesting pattern: the system learns to reach people who would have converted anyway.
- *Confidence: medium.* It rests on two strong examples plus the earlier notes' Mercari and Nextdoor results.

**5. "Holdout" means different things, so claims are not comparable.**
- Adobe's decisioning "holdout" is random offer traffic [O].
- Its no-message holdout applies to one action and leaks afterwards [O].
- Its journey-level holdout arrived only on 1 Sep 2026, in limited release [O].
- Braze offers random control, holdout or business-as-usual as options [S]. Optimove shrinks its control [S]. Aampe prefers synthetic controls [S].
- No vendor publishes results against **a persistent, customer-owned, no-message holdout in money terms with an error band**.
- *Confidence: strong for Adobe; medium for the others.*

**6. Why OfferFit (and Aampe, Bluecore) were acquired.**
- LLM campaign builders are becoming a commodity. Every vendor shipped one in 2025–26, and Adobe gives its own away on a free trial [O].
- What cannot be copied quickly is (a) a per-user decision engine trained on first-party event data and (b) a credible proof story to justify premium pricing.
- Braze's CEO links AI adoption to customers seeking "proof of ROI". MoEngage bought Aampe to get both an RL engine and a research-grade measurement team.
- *Confidence: medium (inference from §2 evidence and the earlier notes).*

**7. Liability is moving onto claims.**
- The FTC's 13 AI-washing cases, ASCI's labelling rules and the EU's Article 50 all target *what is claimed about AI* and *disclosure of AI content*.
- An unsupported "autonomous" or "+X% lift" claim is becoming a legal exposure as well as a credibility problem.
- *Confidence: medium.*

---

## 7. White space: what nobody does well

1. **Proof of incremental value in money, by default, from someone other than the vendor.**
   - **In paid ads**, neutral measurers exist (Haus, Recast; INCRMNTAL is being absorbed by Smartly, per the earlier notes).
   - **In owned channels (CRM, WhatsApp, SMS, push)**, I found nobody who:
     - keeps a persistent no-message holdout the customer owns;
     - reports rupees with a confidence interval;
     - labels results "not proven" when they are not.
   - The vendors either measure themselves or measure engagement.
2. **Long-horizon measurement.** The Haus midpoint-versus-end reversal and Aampe's engagement-versus-conversion gap both show that short windows flatter autonomous systems. Nobody sells "re-check the lift at 90 days against the persistent holdout" as a product feature.
3. **India channel economics and compliance, joined to decisioning.**
   - **WhatsApp economics:** a marketing message costs about **₹0.86 before GST**, against about **₹0.115** for utility messages, roughly 7.5 times more. Reports also say service messages stopped being free on 1 Oct 2026 — [MyOperator](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026) [S], [AiSensy](https://m.aisensy.com/blog/whatsapp-per-message-pricing-update-effective-january-1-2026/) [S]. *Medium on the rate; weak on the service-message change.*
   - **What follows:** "send fewer marketing messages, prove each one" has direct rupee value.
   - **The compliance gap:** DLT templates, Meta template approval, TRAI's 6-hour suspensions and spam flags, and ASCI labelling all limit what an agent may create. No vendor was found offering agent decisions that are **compliance-aware for India and come with proof**. Braze's Agentic Standards is the closest QA concept, and it is not India-specific.
4. **The mid-market.**
   - Adobe's personalised model needs 1,000 impressions and 50–100 conversions per offer per week [O].
   - Braze's decisioning was enterprise-priced until "Go".
   - MoEngage and CleverTap pricing is opaque, and the only agentic add-ons I found were unpriced.
   - Indian mid-market brands paying a few lakh rupees a year have L2 drafting tools but **no affordable decisioning engine that also proves value**.
5. **Honest evidence labels.** Segment's "AI Nutrition Facts" [O] shows vendors will publish *what the AI is*. Nobody publishes *whether it worked*, graded by strength of evidence. FTC enforcement creates a tailwind for doing so.
6. **WebEngage has no agent layer** (weak, an absence of evidence). That makes it the most plausible Indian engagement-platform partner for a neutral decision-and-proof layer. MoEngage (with Aampe), CleverTap and Netcore now each own a decisioning story they would have to defend against a neutral auditor.

---

## 8. What this means for a decide-and-prove product

1. **Do not compete on autonomy.** The L4 position is owned by Google and Meta inside budgets. The L2 drafting position is crowded and given away (Adobe's free trial, Klaviyo's 95,000 first-month users). The L3 per-user RL position has been bought by Braze, MoEngage and Insider, and is funded at Hightouch.
2. **Compete on the counterfactual.**
   - Offer a persistent, customer-owned, no-message holdout across *all* senders, including the client's other vendors' agents.
   - Report money with an error band and a long recheck window.
   - State plainly that the ad market proved "platform says" and "true lift" differ by 30% or more, and that CRM has no equivalent check today.
3. **Define "holdout" in the contract.** Adobe's own documentation uses the word for random-offer traffic. Publish a one-page glossary (no-message holdout, random-arm control, business-as-usual, synthetic control) and grade every vendor claim against it. Few claims will survive that grading.
4. **Decide only where the data supports it.** Use Adobe's documented floors as a public benchmark. Below them, the honest answer is risk-ranking plus a holdout, not per-user RL. That supports the earlier finding that uplift did not beat risk ranking on Hillstrom.
5. **Make India compliance part of the decision.** The decision should come out as a pre-registered DLT template plus a WhatsApp category choice, priced in rupees per message, with frequency limits. A marketing WhatsApp message costs about 7.5 times a utility one, so the value of *not* sending is measurable.
6. **Sell to platforms without an agent first, and to buyers who already doubt.** WebEngage-type platforms, mid-market brands that cannot meet the data floors, and finance teams quoting "proof of ROI" (Braze's own words) are the opening.
7. **Know the threats.** Braze already offers random controls and is moving down-market ("Go", October 2026). Adobe released journey-level holdouts on 1 Sep 2026. MoEngage now has Aampe's measurement team. If any of them ships a customer-owned, money-denominated, persistent holdout report, the gap narrows to neutrality alone.

---

## 9. Corrections to the earlier research (10 Oct 2026 notes)

| Earlier statement | Correction | Basis |
|---|---|---|
| "No named Indian deployments of agentic decisioning" | **Swiggy uses MoEngage + Aampe** (vendor case study; engagement metrics only) | [MoEngage case study](https://www.moengage.com/casestudy/swiggy-scales-campaigns-with-moengage-and-aampe/) [S] |
| Adobe: journey-level holdouts "on the roadmap" (2024) | **Shipped in limited availability on 1 Sep 2026.** Adobe's decisioning "holdout" report is random-offer traffic, not no-message | [AJO release notes](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/rn/release-notes.md) [O], [Campaign report](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/campaign-global-report-cja-code.md) [O] |
| Adobe "Agent Orchestrator GA Sep 2025" was the agentic story | **CX Enterprise Coworker** (Summit, Apr 2026) is now the layer. Coworker Campaigns is a free trial in North America to 31 Dec 2026, produces review-ready drafts, has a planned 5,000-a-month send cap, and its MCP is read-only | [Coworker Campaigns overview](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/overview.md) [O] |
| Braze "Project Catalyst" / "Agent Console" unconfirmed | The products are **Operator, Agent Console and Decisioning Studio**. Forge 2026 added Decisioning Studio Go, Operator Connect (MCP), Agentic Standards and Conversational Agents | Braze Q2 FY27 release and Forge pages [S] |
| Insider–Bluecore "lightly verified" | **Confirmed** by Bluecore's own press page; price undisclosed | [Bluecore](https://www.bluecore.com/press/insider-one-acquires-bluecore/) [S] |
| Bloomreach Loomi agent "date unconfirmed" | **GA on 2 June 2026** | [Bloomreach](https://www.bloomreach.com/en/news/2026/bloomreach-announces-the-general-availability-of-loomi-marketing-agent/) [S] |
| Klaviyo "Marketing Agent" (Sep 2025), with guardrails and approvals | Now **Composer**, public beta 30 Jun 2026: drafts only, nothing live without approval, 95,000 first-month users | Klaviyo newsroom and Q2 transcript [S] |
| HubSpot agents are CRM, service and sales only | **UNBOUND 2026**: Breeze retired as an umbrella brand, **Agent Hub** (beta), and a Campaign Agent listed (detail unverified) | HubSpot Fall 2026 Spotlight [S] |
| Optimove AI Decisioning Studio "launch date unclear" | **27 March 2026.** "Optimail" not found | Optimove release [S] |
| Salesforce: Marketing Cloud Next (June 2025) | **Connections 2026**: Agentic Segmentation as headline; Content and Goals/Expert agents **still in pilot** | Salesforce pages [S] |
| Twilio Segment: no 2026 agent update | Confirmed. SIGNAL 2026 launched conversation infrastructure, not a Segment marketing agent | [BlogGeek](https://bloggeek.me/twilio-signal-2026/) [S] |
| (Not covered) | **Hightouch: $150M at $2.75B (Apr 2026)**; **WebEngage: no agent found**; **TRAI Q1 FY27 enforcement numbers**; **ASCI AI labelling (draft 8 May 2026, final reported 29 Sep 2026)**; **EU Article 50 live from 2 Aug 2026** | See §§2, 4 |

---

## 10. Sources opened and read [O] (22)

1. Adobe AJO: [Work with AI](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/start/ai-features.md)
2. Adobe AJO: [Release notes 2026](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/rn/release-notes-2026.md)
3. Adobe AJO: [Release notes (current)](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/rn/release-notes.md)
4. Adobe AJO: [Content experiment](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/content-management/content-experiment.md)
5. Adobe AJO: [Path experimentation](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/building-journeys/path-experimentation.md)
6. Adobe AJO: [Auto-optimization model](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/auto-optimization-model.md)
7. Adobe AJO: [Personalized optimization model](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/personalized-optimization-model.md)
8. Adobe AJO: [Ranking formulas](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/ranking-formulas.md)
9. Adobe AJO: [Campaign report (holdout and model-driven traffic)](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/campaign-global-report-cja-code.md)
10. Adobe AJO: [Channel report (STO lift)](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/channel-report-cja.md)
11. Adobe AJO: [Exclusion list](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/exclusion-list.md)
12. Adobe AJO: [Coworker skills for journeys](https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/building-journeys/journeys-coworker-skills.md)
13. Adobe CX Enterprise AI: [Coworker Campaigns overview](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/overview.md)
14. Adobe CX Enterprise AI: [Launch and manage a campaign](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/launch-manage-campaign.md)
15. Adobe CX Enterprise AI: [Coworker Campaigns release notes](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/release-notes.md)
16. Adobe CX Enterprise AI: [Agent Orchestrator](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/agents/agent-orchestrator.md)
17. Adobe CX Enterprise AI: [Journey Optimizer agent](https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/agents/ajo-agent.md)
18. Twilio Segment: [Predictions](https://github.com/segmentio/segment-docs/blob/develop/src/unify/Traits/predictions/index.md)
19. Twilio Segment: [Predictions Nutrition Facts](https://github.com/segmentio/segment-docs/blob/develop/src/unify/Traits/predictions/predictions-nutrition-facts.md)
20. Google: [Meridian README](https://github.com/google/meridian) (MMM calibrated with experiments)
21. Meta: [GeoLift README](https://github.com/facebookincubator/GeoLift) (synthetic-control geo lift)
22. Recast: [geolift-simulation-study](https://github.com/getrecast/geolift-simulation-study) (README and `results/aggregated` tables A1–A4)

Everything else above is marked [S]: a search-result summary, page not opened.
