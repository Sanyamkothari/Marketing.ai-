# Agentic-AI competitor map: marketing / CRM / CVM software, 2025-2026 (claims vs proof)

Method note: WebFetch was blocked (DNS failure on every domain tried), so all findings come from WebSearch result summaries. Each fact carries the URL the search returned. Vendor press releases and vendor blogs are marked as vendor claims. "Unverified" means I could not confirm it in a source. Research date: 2026-10-10. Coverage is uneven: Salesforce, Adobe, HubSpot, Klaviyo, Iterable, Bloomreach, Persado, Amplitude, Mixpanel and Segment got thin or no 2026 coverage. Several vendors are not covered in depth and are listed in Gaps.

## 1. What each vendor's agent does, its autonomy level and its decisioning method

### Takeaway
Almost every vendor now ships "agents". Most are LLM copilots and orchestrators (build segments, campaigns, journeys and content from a prompt) with human approval. A smaller group has a real per-user decisioning engine (Braze/OfferFit RL, Aampe per-user agents, Optimove self-optimizing A/B/n, CleverTap and MoEngage decisioning, Movable Ink Da Vinci). The decisioning-engine vendors are the ones that matter to a causal-proof-first product.

### Cited Findings

**Braze (US, public) - the closest functional competitor**
- Braze agreed to buy OfferFit for $325M, announced 27 Mar 2025. It closed on 2 Jun 2025 — [BusinessWire 2025-03-27](https://www.businesswire.com/news/home/20250327329432/en), [TipRanks](https://www.tipranks.com/news/the-fly/braze-to-acquire-offerfit-for-325m-in-cash-and-stock-deal), [Frontier Enterprise](https://www.frontier-enterprise.com/braze-pushes-agentic-ai-with-325m-acquisition-of-offerfit/). The closing date is from the Q2 summary at [VectorShift](https://vectorshift.ai/research/companies/braze/earnings/2026-q2-summary.html), which is a third party.
- A third-party aggregator reports a final adjusted price of about $303.2M ($195.3M cash plus $107.6M stock). Treat it as lower authority — [VectorShift deal page](https://vectorshift.ai/research/deals/braze-offerfit-2025).
- OfferFit was rebranded BrazeAI Decisioning Studio. Braze describes it as optimising "any business KPI" through 1:1 personalisation of channel, message, creative, offer, time, day and frequency — [BusinessWire 2025-06-02](https://www.businesswire.com/news/home/20250602954882/en), [Braze: what is OfferFit](https://www.braze.com/resources/articles/what-is-offerfit).
- Method: reinforcement learning that replaces A/B testing — [VentureBeat on OfferFit's $25M raise](https://venturebeat.com/business/offerfit-gets-25m-to-kill-a-b-testing-for-marketing-with-machine-learning-personalization), [Frontier Enterprise](https://www.frontier-enterprise.com/braze-pushes-agentic-ai-with-325m-acquisition-of-offerfit/).
- It was made available on Google Cloud Marketplace in Dec 2025 — [BusinessWire](https://businesswire.com/news/home/20251202496602/en/Braze-Delivers-BrazeAI-Decisioning-Studio-Through-Google-Cloud-Marketplace).
- Braze's wider agent work is called "Project Catalyst" in the deal coverage and "Agent Console" at the Needham conference in Jan 2026. I did not confirm the product details — [Frontier Enterprise](https://www.frontier-enterprise.com/braze-pushes-agentic-ai-with-325m-acquisition-of-offerfit/), [MarketBeat, Jan 2026](https://www.marketbeat.com/instant-alerts/braze-highlights-chatgpt-sdk-agent-console-and-ai-personalization-push-at-needham-conference-2026-01-14/).

**MoEngage (India-origin, now US/India)**
- MoEngage acquired Aampe on 24 Jun 2026. The price was undisclosed. Inc42 sources describe an all-cash deal of "tens of millions of dollars". This is an acquisition, not a partnership or minority investment as the brief assumed — [Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/), [CMSWire](https://www.cmswire.com/customer-experience/moengage-buys-aampe-to-add-peruser-ai-agents/), [AAP release](https://www.aap.com.au/aapreleases/cision20260623ae89902/).
- Aampe's founders join MoEngage to lead agentic decisioning. Aampe customers Grab and Taxfix move across. MoEngage frames the product as an "Agentic Customer Engagement Platform" in which Aampe is the decisioning layer. Aampe claims millions of per-customer agents and 200 billion decisions a week (vendor claim). Reporting links the deal to IPO preparation after a reverse flip to India — [Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/), [Shopifreaks](https://www.shopifreaks.com/moengage-acquires-us-ai-startup-aampe-in-a-tens-of-millions-all-cash-deal-to-scale-agentic-per-customer-marketing-ahead-of-a-planned-ipo/).
- Merlin AI is a suite of agents. It includes decisioning agents that choose each customer's next step. "Merlin AI Agents" launched in 2025 — [MoEngage Gartner blog](https://www.moengage.com/blog/visionary-gartner-magic-quadrant-personalization-engines/).
- MoEngage raised $100M led by Goldman Sachs (TechCrunch dated 2025-11-04), "to scale marketing AI agents" — [TechCrunch](https://techcrunch.com/2025/11/04/goldman-sachs-doubles-down-on-moengage-in-100m-round-to-fuel-global-expansion), [MoEngage](https://www.moengage.com/in-the-news/moengage-gets-100m-to-scale-marketing-ai-agents-and-accelerate-expansion-in-north-america/).
- Gartner placed MoEngage as a Visionary in the 2026 Magic Quadrant for Personalization Engines (26 Mar 2026) — [MoEngage](https://www.moengage.com/blog/visionary-gartner-magic-quadrant-personalization-engines/). CleverTap was named a Leader in the same report — [CleverTap](https://clevertap.com/blog/clevertap-named-a-leader-in-2026-gartner-magic-quadrant-for-personalization-engines/).

**Aampe (now MoEngage)**
- Gives each customer an agent that picks message, timing, frequency and channel and learns from outcomes — [Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/).

**CleverTap (India, Mumbai)**
- Launched "Clever.AI" on 3 May 2024, built on predictive, generative and prescriptive pillars. The release claims +66% conversion, +35% operational efficiency and 3x CTR (vendor claims, no methodology) — [Outlook Business](https://www.outlookbusiness.com/corporate/clevertap-launches-clever-ai-ai-driven-edge-for-customer-engagement-and-retention), [CMSWire](https://www.cmswire.com/the-wire/clevertap-launches-cleverai-the-ai-driven-edge-for-customer-engagement-retention/).
- By 2025-26 the brand is "CleverAI": a decisioning engine plus an "Agentic Universe" of prompt-to-action agents (Analytics, Journey, Segment, Content, Recommendations). The FAQ says humans review, approve and launch agent outputs — [CleverTap CleverAI Agents](https://clevertap.com/ai/cleverai-agents/), [CleverTap blog](https://clevertap.com/blog/meet-cleverai-agents-powering-the-next-chapter-of-customer-engagement/).
- "Scout" was not found anywhere. Treat it as unverified.
- Acquisitions: Leanplum (2022) and rehook.ai (2025, undisclosed) — [Wikipedia](https://en.wikipedia.org/wiki/CleverTap). Last confirmed funding was the $105M Series D in 2022 — [Business Standard](https://www.business-standard.com/article/companies/b2b-platform-clevertap-to-raise-105-million-from-cdpq-and-others-122081000780_1.html).

**Netcore (India, Mumbai), rebranded Netcore.ai on 30 Jul 2026**
- Seven autonomous agents plan, execute, analyse and optimise campaigns, with "human growth engineers" co-owning outcomes. It markets an outcome-based model and "shared accountability" — [Webull/PR](https://www.webull.com/news/15313552108823552), [Tribune India](https://www.tribuneindia.com/news/business/netcore-is-now-netcore-ai-becoming-the-first-agentic-marketing-platform-to-share-accountability-for-customer-growth/).
- The EDGE 2025 recap describes the Co-Marketer as the orchestrator over a Scheduler, Designer Agent and User Twins. The Insights Agent was the one officially launched. Claimed "10x conversions at 10% of the effort" is a vendor claim with no methodology — [Netcore blog](https://netcorecloud.com/blog/edge-2025-agentic-ai-recap/).
- A sceptical trade note (Renascence) calls the rebrand largely marketing and asks which metric the fee is tied to and what happens when it is missed — [Renascence](https://www.renascence.io/news/9328/netcore-ai-rebrands-as-agentic-marketing-platform-with-outcome-based-model).

**Insider One (Turkey/Singapore, strong India and emerging-market presence)**
- Agent One (formerly Sirius AI) was announced on 25 Mar 2025 as a set of autonomous agents. A 17 Sep 2026 release describes a self-optimising "Agent One Teams / Audiences" system with MCP connectivity. The Shopping and Support agents use OpenAI models — [Insider (original)](https://insiderone.com/news/insider-lauches-agent-one/), [Insider (Sep 2026, Chinese-language)](https://insiderone.com/zh-hant/news/insider-one-autonomous-customer-engagment/), [Insider (Portuguese, OpenAI partnership)](https://insiderone.com/pt/news/insider-one-anuncia-parceria-estrategica-com-a-openai-para-acelerar-engajamento-do-cliente/), [G2 AI](https://ai.g2.com/evaluations/categories/marketing/insider-one-agent-one-ex-sirius-ai).
- Reported acquisition of Bluecore on 13 May 2026. Source is a Vietnamese-language article, so treat it as lightly verified — [BrandsVietnam](https://www.brandsvietnam.com/congdong/topic/insider-one-nen-tang-agentic-ai-hang-dau-chinh-thuc-mua-lai-bluecore).

**Optimove**
- Optibot (launched about 2020, now documented as "AI Insights Agent") scans the customer model and campaign calendar and recommends actions, including adding or enlarging control groups. Recommendations are one-click for the human to accept — [Optimove blog](https://www.optimove.com/blog/optibot-the-first-marketing-optimization-bot), [Optimove Academy](https://academy.optimove.com/hc/en-us/articles/8581296923933-Understanding-AI-Insights-Agent-formerly-Optibot-Cards), [MarTech.org](https://martech.org/optimove-adds-intelligent-agent-self-optimizing-marketing-campaigns).
- Self-optimising campaigns are A/B/n with 2+ competing actions plus a control group, personalised per micro-segment — [Optimove blog](https://www.optimove.com/blog/optibot-the-first-marketing-optimization-bot).
- AI Decisioning Studio launched with four agents: Journey, Offer, Content and Send-Time Optimization. It sits under "Positionless Marketing" and "Optimove AI", which also works from outside tools such as Claude. Launch date unclear (2026 by context) — [MarTech Cube](https://www.martechcube.com/tag/optimove/), [Optimove](https://www.optimove.com/resources/blog/ai-decisioning-studio-optimove-s-new-hub-for-positionless-marketing), [CX Today](https://www.cxtoday.com/marketing-sales-technology/optimove-ai-marketing-tools-hub-positionless-marketing/).

**Salesforce Agentforce (Marketing Cloud Next)**
- Marketing Cloud Next, announced June 2025, is described as powered by autonomous agents. It includes Agentforce Campaign Creation (brief to audience, content and a Flow journey) and Agentforce Personalization Decisioning (real-time content and offer recommendations). Autonomy defaults are unclear in the sources — [DestinationCRM](https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Salesforce-Launches%c2%a0Marketing-Cloud-Next-169980.aspx), [Tectonic](https://gettectonic.com/scale-your-marketing-with-agentforce/).

**Adobe**
- Agent Orchestrator is the agentic layer in Adobe Experience Platform. It routes natural-language requests to specialised agents. Named agents include Audience, Journey and Experimentation (in Journey Optimizer Experimentation Accelerator). General availability was reported Sep 2025 — [Adobe Experience League](https://experienceleague.adobe.com/en/docs/cx-enterprise-ai/experience-cloud-ai/agents/agent-orchestrator), [ChannelBuzz](https://channelbuzz.ca/2025/09/adobe-announces-ga-of-ai-agents-to-embed-them-in-data-and-elevate-customer-experience-44439/), [Adobe India page](https://business.adobe.com/in/products/experience-platform/agent-orchestrator/audiences.html).

**HubSpot Breeze** - agents are Customer Agent (support) and Prospecting Agent (sales), plus Data Agent. These are CRM/service/sales-oriented, not CVM decisioning — [Constellation Research](https://www.constellationr.com/insights/news/hubspot-price-breeze-customer-agent-breeze-prospecting-agent-outcomes), [resolve247](https://resolve247.ai/blog/hubspot-breeze-pricing-change).

**Klaviyo** - Marketing Agent and Customer Agent launched 25 Sep 2025 at K:BOS. Marketing Agent turns a URL into a plan with campaigns, flows and forms. Users set guardrails and approval steps — [BusinessWire](https://www.businesswire.com/news/home/20250925521199/en/Klaviyo-Advances-AI-first-B2C-CRM-with-the-Launch-of-Marketing-Agent-and-Customer-Agent), [DestinationCRM](https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Klaviyo-Launches-Marketing-Agent-and-Customer-Agent-171625.aspx).

**Iterable** - Nova (Activate Summit 2025, goal-based orchestration/advice). Nova Agent launched 22 Apr 2026: acts on customer behaviour in real time, with a Command Center and Google Ads integration — [BusinessWire 2026-04-22](https://www.businesswire.com/news/home/20260422200702/en/Iterable-Unveils-Nova-Agent-and-a-New-Wave-of-AI-Innovations-to-Power-Real-Time-Personalization-and-Predictable-Growth), [DemandGen Report](https://www.demandgenreport.com/analyst-insights/iterable-launches-ai-agent-to-scale-personalization/).

**Bloomreach** - Loomi AI agentic platform. A Loomi marketing agent is reported to turn one prompt into a campaign workflow (date unconfirmed). Loomi Connect (Jan 2026) extends into ChatGPT via MCP. Bloomreach claims more than $260M ARR in 2025 (vendor claim). Clarity conversational shopping was announced Aug 2023 — [StockAnalysis listing of BusinessWire item](https://stockanalysis.com/private/bloomreach/), [WPP partner page](https://enterprisesolutions.wpp.com/en/partners/bloomreach), [BusinessWire 2023](https://www.businesswire.com/news/home/20230824866946/en/Bloomreach-Announces-Clarity%E2%84%A2-The-First-E-Commerce-AI-to-Connect-Every-Customer-to-Personalized-Human-Like-Product-Expertise-%E2%80%94-Anytime-Anywhere).

**Movable Ink** - Da Vinci "Agentic Campaigns" (June 2025): upload creative, set guardrails, define objectives, and multiple decisioning models choose message, creative, subject line and frequency per person. Expanded to UK/Europe in Oct 2025 with ISO/IEC 42001 certification. New CEO Adam Stambleck, Mar 2026 — [StreetInsider/PRN](https://www.streetinsider.com/PRNewswire/Movable+Ink+Unveils+Autonomous+Marketing+Capabilities/24945917.html), [Movable Ink blog](https://movableink.com/blog/da-vinci-expands-across-the-uk-and-europe-with-new-product-updates), [MarTech Series](https://martechseries.com/predictive-ai/ai-platforms-machine-learning/movable-ink-launches-programmatic-crm-with-ai-agents-for-personalized-content-creation-and-customer-engagement/).

**Pecan AI** - launched a "Predictive AI Agent" on 28 Jan 2026 to forecast revenue, sentiment and demand and to rank lapsed customers by re-engagement likelihood. It is predictive, not a treatment-assignment engine. Vendor-cited figures (12% lower churn, 15% better ROAS) are unverified — [BusinessWire](https://www.businesswire.com/news/home/20260128325003/en/Pecan-Launches-Worlds-First-Predictive-AI-Agent-Ushering-in-the-End-of-Reactive-Business), [SiliconANGLE](https://siliconangle.com/2026/01/28/pecan-ai-unveils-predictive-agent-automates-business-forecasting/), [Fritz.ai](https://fritz.ai/go/pecan).

**Twilio Segment** - CustomerAI Predictions (Aug 2023, so old). No 2026 agent update found — [Twilio IR](https://investors.twilio.com/news-releases/news-release-details/twilio-customerai-fuels-next-generation-customer-relationships).

### Inferences
- Three tiers: (a) LLM copilots that build assets and segments with human approval (CleverTap agents, Klaviyo, HubSpot, Adobe, Salesforce); (b) per-user decisioning engines (OfferFit/Braze, Aampe/MoEngage, Optimove, Movable Ink, CleverTap decisioning); (c) predictive models that feed a human (Pecan, Segment CustomerAI).
- Consolidation is converging on the same shape: engagement platform plus a bandit/RL decisioning acquisition (Braze+OfferFit, MoEngage+Aampe, Insider+Bluecore, CleverTap+rehook.ai). A standalone decisioning vendor now competes against bundled ones.
- Two of the five Indian-relevant acquirers (MoEngage, CleverTap) are the very platforms the user's product hands treat lists to. That is both a channel-partner dependency and a competitive threat.

### Gaps
- Salesforce, HubSpot, Adobe, Klaviyo, Iterable and Bloomreach had little 2026 detail. Their autonomy defaults, decisioning method and measurement features are not confirmed.
- No sources at all on Persado, Amplitude or Mixpanel experiment agents. Not researched beyond the search that returned nothing.
- CleverTap "Scout" is unverified.
- Exact Braze "Agent Console"/"Project Catalyst" scope is unconfirmed.

## 2. Measurement and proof: control groups, incrementality, published results, criticism

### Takeaway
Only a few vendors say publicly that they use randomised controls by default or by design: OfferFit/Braze (RCT vs business-as-usual), Optimove (control group built into every self-optimising campaign), Aampe (own holdout and synthetic-control approach, with a peer-reviewed-style arXiv case study). No vendor was found that publishes audited, rupee-denominated results with confidence intervals and an explicit evidence-quality label. Most others report headline lifts with no methodology.

### Cited Findings
- Braze's own FAQ says OfferFit measures impact with a randomised controlled trial: a treatment group gets AI-chosen communications and a control group gets "business as usual", with uplift measured on the top-level KPI — [Braze OfferFit webinar FAQ](https://www.braze.com/resources/articles/offerfit-ai-decisioning-webinar-questions).
- Headline claim: 92% higher conversion than business-as-usual, about $16M annualised value. The client and control design are not named in the snippet — [Braze: what is OfferFit](https://www.braze.com/resources/articles/what-is-offerfit). Management cites Yum! Brands and Capital One as customers with measurable uplift without publishing figures — [Motley Fool data news, Jun 2025](https://www.fool.com/data-news/2025/06/06/braze-grows-revenue-and-expands-ai-suite/). Capital One Ventures invested in OfferFit — same source.
- Optimove: self-optimising campaigns always include a control group. "Automatic control group optimization" shrinks the control as the campaign proves itself, never to zero, and enlarges it when performance weakens. Optimove blog claims over a year of data behind an "Ideal Control Groups Algorithm" (results not retrieved, vendor claim) — [Optimove blog](https://www.optimove.com/blog/ending-the-control-groups-leave-money-on-the-table-conversation), [Optimove blog](https://www.optimove.com/blog/optibot-the-first-marketing-optimization-bot).
- Aampe argues that classic holdouts have a short shelf life when messaging adapts continuously, and measures with switchback holdouts, synthetic controls and Coarsened Exact Matching. It also recommends a three-arm comparison: no-message, business-as-usual, Aampe — [Aampe docs: control groups](https://docs.aampe.com/docs/control-groups), [Aampe blog: adaptive synthetic control](https://aampe.com/blog/validating-aampe-performance-with-adaptive-synthetic-control-groups), [Aampe KB holdout part 2](https://kb.aampe.com/user-guide/how-do-i-know/experiments/holdout-part2).
- Published research (vendor-authored, arXiv): an 11-month longitudinal study on a consumer delivery app, about 8.8M users, 90% agentic vs 10% BAU persistent holdout, using ML-RATE variance reduction. Authors report agentic personalisation kept a positive lift when humans stopped curating — [arXiv 2604.08621](https://arxiv.org/pdf/2604.08621). A secondary summary quotes 65.3% lift in notification-driven app opens in the active phase, plus small (~0.3%) gains in conversion and intent days. That number comes from a republished third-party page and is unverified against the paper — [besthub summary](https://www.besthub.dev/articles/what-an-8-8-m-user-experiment-shows-about-the-true-limits-of-agent-based-marketing-14bdd3611b21). Earlier Aampe papers: [arXiv 2506.16429](https://arxiv.org/pdf/2506.16429), [arXiv 2512.17462](https://arxiv.org/pdf/2512.17462) (financial-services app case study).
- Vendor-claimed lifts without a stated control design: CleverTap Clever.AI +66% conversion (2024), Netcore "10x conversions", Bloomreach Clarity 9% conversion lift via a WPP partner page, Pecan 12% churn reduction, Netcore/CleverTap cases — see section 1 sources.
- Criticism and scepticism is thin and mostly from measurement vendors with their own interest: Haus argues agentic marketing needs causal incrementality, not platform-reported ROAS — [Haus buyer's guide](https://haus.io/article/agentic-marketing-a-buyers-guide), [Haus](https://haus.io/article/what-is-agentic-incrementality-testing). eMarketer reports persistent marketer distrust of agentic ad-platform features (headline only) — [eMarketer](https://www.emarketer.com/content/ad-platforms-launch-agentic-ai-features--marketer-distrust-persists). Renascence questions Netcore's outcome-based pricing (see section 1).
- A "Towards Data Science" article titled "No, you don't need a holdout group" is the type of counter-argument vendors use against persistent holdouts — [TDS](https://towardsdatascience.com/no-you-dont-need-a-holdout-group-ab0995860aca/). I did not read it, so its argument is unverified.

### Inferences
- The brief's thesis ("few build agents that PROVE") is partly challenged. OfferFit/Braze and Optimove do run controls, and Aampe publishes arXiv-level case studies. The defensible differentiator is the output: an audited, buyer-owned rupee figure with confidence interval and an explicit evidence label, with the control kept persistent and not shrunk. Optimove actively shrinks controls and Aampe argues against classic holdouts, so both trade proof for exploitation.
- Vendor-grade proof is self-reported by the same party that sells the product. Nobody was found offering a third-party or customer-finance-signed measurement.

### Gaps
- No G2 review text was retrievable on whether buyers trust the lift numbers.
- I found no Forrester or Gartner statement about agentic marketing measurement. Only press-reported Magic Quadrant placements.
- The arXiv PDFs were not opened, so numbers beyond the abstract are unverified.
- Salesforce, Adobe, HubSpot, Klaviyo, Iterable and Bloomreach measurement practices were not found.

## 3. Latest moves in 2025-2026: acquisitions, funding, launches

### Takeaway
2025-26 is a consolidation wave around decisioning: Braze bought OfferFit, MoEngage bought Aampe, Insider reportedly bought Bluecore, and every vendor launched an agent suite. "Proving" moves are limited to measurement features inside these suites.

### Cited Findings
- 2025-03-27 Braze announces OfferFit ($325M); closes 2 Jun 2025 — see section 1.
- 2025-03-25 Insider One launches Agent One — see section 1.
- 2025-06 Salesforce Marketing Cloud Next; Movable Ink Agentic Campaigns — see section 1.
- 2025-09 Adobe agents GA; Klaviyo agents (25 Sep) — see section 1.
- 2025-11-04 MoEngage $100M (Goldman Sachs) — [TechCrunch](https://techcrunch.com/2025/11/04/goldman-sachs-doubles-down-on-moengage-in-100m-round-to-fuel-global-expansion).
- 2025-12 Braze Decisioning Studio on Google Cloud Marketplace — see section 1.
- 2026-01-28 Pecan Predictive AI Agent; Jan 2026 Bloomreach Loomi Connect — see section 1.
- 2026-03 Gartner Magic Quadrant for Personalization Engines: CleverTap Leader, MoEngage Visionary; Movable Ink new CEO — see section 1.
- 2026-04-22 Iterable Nova Agent — see section 1.
- 2026-05-13 Insider One reported to acquire Bluecore (lightly verified) — see section 1.
- 2026-06-24 MoEngage acquires Aampe — see section 1.
- 2026-07-30 Netcore becomes Netcore.ai with seven agents and outcome-based positioning — see section 1.
- 2026-09-08 Braze fiscal Q2 FY27 call: revenue growth 26%, FY27 guide $910-913M, paid AI-tool adoption at roughly one third of large customers, per summaries of its 8-K and call — [Braze 8-K (SEC)](https://www.sec.gov/Archives/edgar/data/0001676238/000167623826000039/a20260731-brazeincxq227ear.htm), [Nasdaq call highlights](https://www.nasdaq.com/articles/braze-q2-earnings-call-highlights), [MarketBeat transcript](https://www.marketbeat.com/instant-alerts/transcript-braze-q2-earnings-call-highlights-2026-09-08/).
- 2026-09-17 Insider One Agent One self-optimising system — see section 1.

### Inferences
- Within about 18 months both of the biggest India-relevant engagement platforms with a decisioning gap have either acquired one (MoEngage) or shipped one (CleverTap decisioning engine, Netcore agents). An independent decisioning/proof layer will be positioned against these bundles and needs a clear reason to sit outside them.

### Gaps
- Optimove launch dates for AI Decisioning Studio and any 2026 funding or M&A not confirmed.
- Salesforce, Adobe and HubSpot 2026 marketing-agent roadmaps not covered.

## 4. India-specific players and Indian buyer adoption

### Takeaway
India has three home-grown engagement platforms (CleverTap, MoEngage, Netcore), all now marketing agentic features, and all of which are potential distribution partners as well as competitors. I found only generic evidence on Indian buyer adoption, with no named fintech or telecom deployments of agentic decisioning.

### Cited Findings
- CleverTap, MoEngage and Netcore details: section 1. MoEngage's reverse flip to India and IPO plan are from [Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/) (single source, reported not confirmed).
- Gartner 2026 Personalization Engines MQ lists both CleverTap (Leader) and MoEngage (Visionary) — see section 1.
- Adobe has an India page for Agent Orchestrator, and its pricing page is generic across regions — [Adobe India pricing](https://business.adobe.com/in/products/experience-platform/agent-orchestrator/pricing.html).
- Dentsu survey (reported Oct 2026): 63% of CMOs globally are uncomfortable delegating high-stakes tasks to agentic AI because of reputational concerns, versus 46% in India. Another dentsu item says 94% of Indian CMOs expect AI-enabled agency workflows to become the norm — [Medianews4u (dentsu, 94%)](https://medianews4u.com/94-of-indian-cmos-expect-ai-enabled-agency-workflows-to-become-the-norm-dentsu). The 63%/46% figure came via a search summary and is not verified on the page.
- BCG: 53% of Indian CMOs project AI-driven revenue growth, above the global 43% average — [Medianews4u (BCG)](https://www.medianews4u.com/53-of-indian-cmos-project-ai-driven-revenue-growth-surpassing-global-43-average-bcg-global-report/).
- exchange4media (Sep 2025) says agentic AI is quietly powering CRM, WhatsApp bots and campaign automation in Indian marketing departments, with human oversight still considered essential — [exchange4media](https://www.exchange4media.com/marketing-news/marketing-is-quietly-being-rewritten-by-agentic-ai-147421.html).
- ReBid positions itself as India's first agentic full-funnel agency (self-reported) — [Branding in Asia](https://www.brandinginasia.com/three-trends-rajiv-dingra/).
- Aampe customers Grab and Taxfix are Asia/Europe, not Indian — [Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/).
- Insider One is widely used across emerging markets. The searches did not confirm its India customer base or revenue, so this is unverified.

### Inferences
- Indian CMO attitudes look more open to delegation than global ones (46% vs 63% uncomfortable), which supports demand for agents. It does not support the claim that they demand proof. The sources do not say.
- Indian buyers in BFSI and telecom (regulated, consent-sensitive) are the segment where rupee-denominated, auditable proof would matter most, but I found no source quantifying this.

### Gaps
- No named Indian retail, e-commerce, fintech or telecom deployments of agentic decisioning. No Inc42/YourStory/ET coverage retrieved on Indian buyer adoption.
- Telecom CVM incumbents (e.g. global CVM vendors such as Amdocs, Pegasystems, SAS, Salesforce/Adobe in telco) were not researched, and are relevant to the stated telecom segment.
- Indian regulatory angle (DPDP Act consent) not researched.

## 5. Pricing models for agentic decisioning

### Takeaway
Pricing is converging on credits and outcomes for generalist agents, while true decisioning engines are sold as high-ticket annual use-case or platform contracts. Only a few vendors publish rates.

### Cited Findings
- Salesforce Agentforce: Flex Credits at $0.10 per action (20 credits per action, credits sold in packs of 100,000 for $500), replacing the earlier $2-per-conversation model. No Marketing-specific rate card found. One third-party site (Redress) shows a conflicting reading and should not be relied on — [Salesforce blog: flex credits](https://www.salesforce.com/blog/flex-credits/), [Tectonic](https://gettectonic.com/agentforce-flex-pricing/), [Redress Compliance](https://www.redresscompliance.com/salesforce-agentforce-pricing-evolution-2026.html).
- HubSpot Breeze (from 14 Apr 2026): Customer Agent $0.50 per resolved conversation (was $1), Prospecting Agent $1 per lead recommended for outreach, extra credits $10 per 1,000; Professional/Enterprise tiers only. This is the clearest outcome-based pricing among large vendors — [Constellation Research](https://www.constellationr.com/insights/news/hubspot-price-breeze-customer-agent-breeze-prospecting-agent-outcomes), [resolve247](https://resolve247.ai/blog/hubspot-breeze-pricing-change), [eesel](https://www.eesel.ai/blog/hubspot-ai-agent-pricing-2026).
- Adobe: annual contracted AI credits under a single core license, with overage by purchase. AI-first apps are priced on value-based metrics. No public rate — [Adobe Agent Orchestrator pricing](https://business.adobe.com/products/experience-platform/agent-orchestrator/pricing.html).
- Braze Decisioning Studio: management has said it is sold per use case at roughly $250k-$300k, and OfferFit was projected to add about $11-12M to fiscal 2026 revenue. This comes from a search summary, so the exact source statement is unconfirmed — [Motley Fool data news](https://www.fool.com/data-news/2025/06/06/braze-grows-revenue-and-expands-ai-suite/), [Braze Q2 FY27 summary](https://www.nasdaq.com/articles/braze-q2-earnings-call-highlights). No published list price.
- Netcore.ai: outcome-based / "shared accountability" pricing claimed in press, with undefined metric and fee at risk. Unverified in practice — [Renascence](https://www.renascence.io/news/9328/netcore-ai-rebrands-as-agentic-marketing-platform-with-outcome-based-model).
- No pricing found for MoEngage, CleverTap, Aampe, Optimove, Insider, Klaviyo, Iterable, Bloomreach, Movable Ink or Pecan.

### Inferences
- Outcome-based pricing already exists but is keyed to activity outcomes (resolved conversation, qualified lead), not causally incremental revenue. A fee tied to measured incremental rupees would be distinct.
- A reference point for sizing: $250-300k per use case at the premium end (single, unverified source).

### Gaps
- No Indian-rupee pricing from any vendor. No per-decision pricing found.

## 6. Gaps a causal-proof-first product could own

### Takeaway
Based on what was found, no one offers a persistent randomised hold-out as a non-negotiable default, plus buyer-auditable rupee impact with confidence intervals and an explicit evidence grade, plus independence from an engagement platform. This is an inference from absence of evidence in limited searching, not a verified fact.

### Cited Findings
- Optimove shrinks control groups as campaigns "prove" themselves, never to zero — [Optimove](https://www.optimove.com/blog/ending-the-control-groups-leave-money-on-the-table-conversation).
- Aampe argues standard holdouts decay and uses synthetic controls and matching instead — [Aampe docs](https://docs.aampe.com/docs/control-groups).
- OfferFit/Braze uses an RCT against business-as-usual, but published results are headline figures such as 92% and $16M — [Braze](https://www.braze.com/resources/articles/what-is-offerfit).
- The incrementality-measurement vendors (Haus and similar) focus on ad channels, not CRM treatment assignment — [Haus](https://haus.io/article/agentic-marketing-a-buyers-guide).
- Vendor outcome pricing in market is tied to activity outcomes — HubSpot, above.
- Netcore and Gartner-placed incumbents claim lifts without disclosed methodology — [Netcore](https://netcorecloud.com/blog/edge-2025-agentic-ai-recap/), [CleverTap](https://www.outlookbusiness.com/corporate/clevertap-launches-clever-ai-ai-driven-edge-for-customer-engagement-and-retention).

### Inferences
- Possible white space: (1) persistent control by default, with the cost of the control quantified so the buyer chooses it knowingly; (2) an evidence label (Causal / Estimated / Not proven) that is honest where a vendor would claim a win; (3) rupee-denominated results with confidence intervals; (4) vendor-neutral hand-off to CleverTap, MoEngage and Netcore, though those three now own agentic stacks of their own and may resist a third-party "proof" layer that could contradict their lift claims; (5) fees tied to proven incremental value.
- Risk: Braze+OfferFit and Optimove already run RCTs and could add proof packaging. Aampe's published research gives MoEngage credible measurement material.

### Gaps
- I did not check Amdocs, Pega, SAS, Optimizely, Dynamic Yield, Mastercard/Visa-style CVM providers, or Indian telecom CVM stacks, which may already offer control-group reporting.
- I did not search for Meta/Google/Adobe causal or MMM tooling that could extend into CRM.
- Whether any buyers pay a premium for evidence labels is unknown.
