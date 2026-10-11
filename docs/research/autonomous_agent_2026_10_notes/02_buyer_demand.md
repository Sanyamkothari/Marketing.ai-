# What marketers and their bosses want, and fear, from fully autonomous marketing agents (as of 11 Oct 2026)

Scope: this note goes deeper on **autonomy** only. It does not repeat `docs/research/agentic_ai_2026_10_notes/buyer_pain_and_trust.md` or the persona section of `docs/research/MARKET_RESEARCH_2026_10.md`. It updates or corrects them where newer evidence exists (section 7).

## How this was researched (read first)

- **WebFetch was blocked** (DNS failure on every host tried: capgemini.com, itbrief.co.uk). Direct downloads through the proxy were refused for gartner.com, ey.com, kpmg.com, adobe.com and capgemini.com. **One primary document was read in full**: the StackAdapt "AI Delegation Gap" report PDF (hosted on Amazon S3, which the proxy allowed). Everything else comes from **WebSearch result summaries** of the cited pages. A figure seen only in a summary is never better than "medium".
- **The search budget ran out** after about 35 searches (the limit of 200 per turn is shared by every agent in the session). Not covered as a result: Validity State of CRM Data 2026, HubSpot's own AI report numbers, Forrester's paywalled agentic guide, and fresh pricing surveys. A follow-up session can close these.
- Items marked **[background, unverified]** come from memory, not from this session's searches. Check them before they reach a client.
- Confidence: **strong** = a primary document I read, or an official release whose figures agree across several summaries. **medium** = an official source seen only through search summaries, or a credible secondary source. **weak** = a single secondary or vendor source, an inconsistency, or a date I could not confirm.

---

## 1. Headline: buyers want "autonomy within rules", not "full autonomy"

Across 2025-2026 surveys the pattern is the same. Almost everyone accepts AI that **recommends** or **prepares actions for approval**. Most accept AI that **acts inside rules a human set**. Only about half accept AI that **acts on its own**, and fewer still in practice. The drop between "within rules" and "on its own" is the real market boundary.

| Level of authority | Share comfortable | Source |
|---|---|---|
| AI recommends, human decides | 90% | StackAdapt, 500 advertisers (US, CA, UK, DE, AU, SG), NewtonX fieldwork 19 May-8 Jun 2026 **[strong, primary read]** |
| AI prepares actions for human approval | 89% | same |
| AI acts within rules humans set | 78% | same |
| AI operates autonomously "when performance is proven" | 50% | same |
| Trust in fully autonomous AI agents (cross-function) | 27% in 2025, down from 43% a year earlier | Capgemini Research Institute, 1,500 executives, 14 countries, July 2025 **[medium]** |
| IT application leaders considering, piloting or deploying fully autonomous agents | 15% (vs 75% piloting or deploying some kind of agent) | Gartner, 360 IT application leaders, May-June 2025, release 30 Sep 2025 **[medium]** |
| CMOs running campaigns where several agents act autonomously | 8% | BCG, 300 CMOs plus 50 interviews, release 15 June 2026 **[medium]** |

Sources: [StackAdapt AI Delegation Gap report (PDF)](https://s3.amazonaws.com/media.mediapost.com/uploads/StackAdapt_AI-Delegation-Gap-Report_2026.pdf); [Business Wire via Morningstar, 18 Aug 2026](https://www.morningstar.com/news/business-wire/20260818027002/stackadapts-ai-delegation-gap-report-reveals-how-marketers-are-redefining-ai-decision-making-in-advertising); [Capgemini press release](https://www.capgemini.com/news/press-releases/trust-and-human-ai-collaboration-set-to-define-the-next-era-of-agentic-ai-unlocking-450-billion-opportunity-by-2028/); [Capgemini India research page](https://www.capgemini.com/in-en/insights/research-library/ai-agents/); [CRN India on Gartner](https://www.crn.in/?p=73300); [IT Brief UK on Gartner](https://itbrief.co.uk/story/survey-finds-slow-adoption-of-autonomous-ai-agents-in-enterprises); [BCG press release](https://www.bcg.com/press/15june2026-cmos-ai-transforming-marketing-transform-function); [Communicate Online on BCG](https://communicateonline.me/insights/most-cmos-see-ai-transforming-marketing-but-few-adopt-agentic-ai-bcg/)

Caveats: StackAdapt is an ad-tech vendor, the sample is programmatic advertising (not CRM or retention), and no respondent is in India. It is still the most granular public data on *levels* of delegation, and I read it in full.

---

## 2. Adoption and analyst outlook, 2025-2026 (new since the earlier notes)

### Gartner
- **Marketing work automated by AI: 16% in 2026, expected 36% by 2028.** Survey of 402 CMOs, Aug-Oct 2025, released 11 May 2026. Gartner (Kristina LaRocca-Cerrone) warns of "AI competency traps" for CMOs who stop at early use cases. No task-level breakdown was visible. **[medium]** — [Gartner release](https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-survey-reveals-marketing-leaders-expect-ai-automation-of-marketing-work-to-double-to-36-percent-by-2028); [Enterprise DNA](https://enterprisedna.co/resources/news/gartner-marketing-ai-automation-doubles-36-percent-2028/)
- **2026 CMO Spend Survey (11 May 2026):** CMOs put 15.3% of marketing budgets into AI. 70% call AI leadership a critical 2026 goal, but only 30% report mature AI readiness. Ewan McIntyre: CMOs buy tools faster than they build data, process, governance and talent. This **replaces** the 2025 CMO Spend figures in MARKET_RESEARCH as the latest edition. One secondary source cites 401 respondents and 21.3%; use Gartner's own figures. **[medium]** — [Gartner release](https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-2026-cmo-spend-survey-finds-cmos-allocate-15-point-3-percent-of-marketing-budgets-to-ai-but-only-30-percent-are-ready-to-scale-ai-capabilities); [Marketing Dive](https://www.marketingdive.com/news/ai-remains-a-top-priority-for-cmos-but-spending-lags-gartner/819806/)
- **Hype Cycle for Digital Marketing, 2026 (published about 10 July 2026):** Gartner's abstract frames the cycle as helping CMOs "move toward autonomous marketing" while governing AI costs and protecting brand trust, under a "trilemma" of flat budgets, high growth demands and answer engines. **[medium]** for the framing; positions on the curve were not seen. — [Gartner abstract](https://www.gartner.com/en/documents/8115797); [MarketScale](https://www.marketscale.com/industries/marketing-tech/gartners-2026-digital-marketing-hype-cycle-frames-ai-spend-as-a-governance-problem-not-a-tooling-race)
- **Staged autonomy is Gartner's stated path.** A 2026 Gartner Hype Cycle PDF (the file name says Digital Commerce; the search title said Digital Advertising) advises planning for "staged autonomy rather than full automation" and says conversational programmes may stay supervised for 18-24 months. **[weak]**: seen only as a search summary, and the document title is ambiguous. — [Hype Cycle PDF](https://assets.ctfassets.net/0zod1qp6n9mn/4DVFF0AvVZk2maGB6NcCCY/dff6e105d575f2973e506532d88b4e1e/July_2026_Hype_Cycle_for_Digital_Commerce.pdf)
- **First Hype Cycle for Agentic AI (2 April 2026):** more than 30 technologies, with agents at the Peak of Inflated Expectations. Secondary summaries say 17% have deployed agents and 42% plan to within 12 months. **[medium]** for the placement, **[weak]** for the percentages. — [Gartner article](https://www.gartner.com/en/articles/hype-cycle-for-agentic-ai.html); [Enterprise DNA](https://enterprisedna.co/resources/news/gartner-hype-cycle-agentic-ai-2026-enterprise-readiness); [Tray.ai](https://tray.ai/blog/gartner-agentic-ai-hype-cycle-2026/)
- **2025 AI Hype Cycle:** AI agents near the Peak, generative AI in the Trough. **[weak]** (secondary only) — [TMCnet](https://blog.tmcnet.com/blog/rich-tehrani/?p=24527)
- **Predictions (forecasts, not measurements):**
  - At least 15% of day-to-day work decisions made autonomously by 2028 (0% in 2024).
  - 33% of enterprise software apps will include agentic AI by 2028 (<1% in 2024).
  - Over 40% of agentic projects will be cancelled by end-2027.
  - 60% of brands will use agentic AI for "streamlined one-to-one interactions" by 2028 (release 15 Jan 2026; analyst Emily Weiss calls it "the end of channel-based marketing as we know it").

  **[medium]** — [SD Times](https://sdtimes.com/agentic-ai/gartner-more-than-40-of-agentic-ai-projects-will-be-canceled-in-next-few-years/); [Gartner 60% release](https://www.gartner.com/en/newsroom/press-releases/2026-01-15-gartner-predicts-60-percent-of-brands-will-use-agentic-ai-to-deliver-streamlined-one-to-one-interactions-by-2028)
- **Jobs (release 29 Sep 2026):** survey of 1,303 senior leaders, Jan-Apr 2026. 18% of marketing leaders have cut some functional roles because of automation, 16% have created new roles, and nearly a third have redesigned roles. Gartner predicts that by 2030 most high-performing marketing teams will remove the traditional "bottom rungs", and warns against mistaking automated entry-level tasks for no need for entry-level talent. **[medium]** — [Gartner release](https://www.gartner.com/en/newsroom/press-releases/2026-09-29-gartner-predicts-ai-will-allow-the-majority-of-high-performing-marketing-teams-to-eliminate-traditional-bottom-rungs-of-the-corporate-ladder-by-2030); [Marketing Dive](https://www.marketingdive.com/news/cmos-must-rethink-entry-level-talent-as-ai-alters-marketing-needs-gartner/831604/)
- **Gartner IT application leaders (Sep 2025), detail not in the earlier notes:**
  - 74% see agents as a new attack vector.
  - Only 13% strongly agree they have the right governance.
  - Only 19% have high or complete trust in their vendor's hallucination protection.
  - Half limited rollouts to low-risk or trusted users.

  **[medium]** — [IDM](https://idm.net.au/node/15402); [Let's Data Science](https://letsdatascience.com/news/gartner-warns-of-enterprise-ai-agent-sprawl-2d76c38f)

### Other analysts and consultancies
- **McKinsey, State of AI 2025 (Nov 2025):** 62% are at least experimenting with agents and 23% are scaling them somewhere. **No more than 10% are scaling agents in any single function, marketing and sales included.** The exact marketing-and-sales figure was not legible. **[medium]** — [CoSupport summary](https://cosupport.ai/articles/the-state-of-ai-mckinsey-report-key-findings); [IT Brief](https://itbrief.co.uk/story/mckinsey-report-shows-ai-interest-but-slow-scaling)
- **Capgemini (July 2025):** only 2% have agents at scale and 12% at partial scale. 40% trust agents to run tasks autonomously. Nearly three-quarters say the benefits of human oversight outweigh its cost, and 90% see human involvement as positive or cost-neutral. Fewer than one in five have high data readiness. **[medium]** — [Capgemini release](https://www.capgemini.com/news/press-releases/trust-and-human-ai-collaboration-set-to-define-the-next-era-of-agentic-ai-unlocking-450-billion-opportunity-by-2028/); [The Fast Mode](https://www.thefastmode.com/technology-and-solution-trends/43323-capgemini-report-trust-and-human-oversight-key-to-unlocking-450b-agentic-ai-opportunity)
- **Deloitte, State of AI in the Enterprise 2026:** 3,235 business and IT leaders in 24 countries. Only 21% have mature agentic-AI governance. Deloitte's definition of mature governance is clear limits on which decisions agents may take alone, real-time monitoring, and audit trails. 23% use agents at least moderately, expected to reach 74% within two years. A separate US Deloitte survey (501 executives) found 61% expect most agents to be largely autonomous, with humans mainly overseeing. **[medium]** — [Deloitte Insights](https://deloitte.com/us/en/insights/topics/emerging-technologies/ai-agents-scaling-faster.html); [GRC Report](https://www.grcreport.com/post/deloitte-survey-finds-enterprises-charging-ahead-with-ai-agents-while-governance-struggles-to-keep-up)
- **PwC AI Agent Survey (300 US senior executives, May 2025):** 79% say agents are being adopted. Trust is highest for data analysis (38%) and performance improvement (35%), and lowest for financial transactions (20%) and autonomous employee interactions (22%). **[medium]** — [PwC](https://www.pwc.com/us/en/tech-effect/ai-analytics/ai-agent-survey.html); [Techstrong.ai](https://techstrong.ai/features/ai-agents-arent-entirely-trusted-with-high-stake-use-cases-pwc-survey/)
- **BCG CMO survey (June 2026, 300 CMOs):**
  - 96% say AI is transforming marketing end to end, but only 8% run autonomous multi-agent campaigns.
  - 42% use GenAI only as an assistant in a few workflows, and just under a third have transformed significant parts of the function with agents.
  - About half say marketing now leads AI investment in its own function.
  - 94% feel CEOs' expectations have risen significantly.

  **[medium]** — [BCG release](https://www.bcg.com/press/15june2026-cmos-ai-transforming-marketing-transform-function); [BCG article](https://www.bcg.com/publications/2026/making-the-agentic-marketing-transformation-a-reality); [IADS summary](https://www.iads.org/member/articles-reports/moving-the-agentic-marketing-transformation-from-illusion-to-reality)
- **Forrester 2026 B2C predictions (28 Oct 2025):** a third of brands will damage customer trust with poor AI self-service, pushed by cost pressure to launch agents too early. Confidence in measurement is expected to fall 7% from 79% of B2C leaders. **[medium]** — [Business Wire](https://secure.businesswire.com/news/home/20251028963453/en/Forresters-2026-B2C-Marketing-CX-Digital-Business-Predictions-One-Third-Of-Brands-Will-Erode-Customer-Trust-Through-Self-Service-AI); [Forrester blog](https://www.forrester.com/blogs/four-big-themes-of-forresters-2026-b2c-marketing-predictions/)
- **G2 AI Agents Insight Report (9 Oct 2025):**
  - Nearly 50% of buyers would grant full autonomy, but **only in low-risk workflows**.
  - Programmes with a human in the loop were twice as likely to save 75%+ of costs as fully autonomous ones.
  - G2 names two modes: "guardrailed autonomy" (pause for approval at key moments) and "full autonomy" (act first, humans review after).

  **[medium]** — [Business Wire](https://secure.businesswire.com/news/home/20251009111961/en/G2-Report-Reveals-AI-Agents-Are-Delivering-the-Growth-Generative-AI-Promised); [G2 PDF](https://learn.g2.com/hubfs/G2-InsightReport-AIAgents2025.pdf?hsLang=en)

### Vendor surveys on marketers
- **Salesforce State of Marketing, 10th edition (2026, about 4,450 marketers):** 75-76% use AI, but **only 13% have moved to agents**. 82% of those using or planning agents expect major or moderate ROI gains. Privacy and data security are the top concerns, followed by accuracy. **[medium]**; vendor survey. — [Salesforce news](https://www.salesforce.com/news/stories/state-of-marketing-2026/); [Salesforce blog](https://www.salesforce.com/blog/tenth-state-of-marketing/); [Diginomica](https://diginomica.com/salesforces-state-marketing-ai-dominates-agenda-data-still-holds-marketers-back)
  - A statistics site attributes "34% of enterprise marketing teams run at least one autonomous agent in production" to Salesforce. This conflicts with the 13% figure, so **do not use it**. **[weak]** — [Konabayev](https://konabayev.com/blog/ai-agents-for-marketing-statistics-2026/)
- **Braze 2026 Customer Engagement Review (24 Feb 2026), "the trust plateau":** 93% of marketing leaders think AI helps them understand customers, but only 53% of consumers feel brands predict their wants well. A secondary breakdown says only 10% of consumers want AI agents working without human oversight. **[medium]** for the release figures, **[weak]** for the 10%. — [Business Wire](https://www.businesswire.com/news/home/20260224711754/en/The-2026-Braze-Customer-Engagement-Review-AI-Innovation-Meets-the-Trust-Plateau); [Composed Digital](https://www.composeddigital.com.au/insights/braze-customer-engagement-review-2026)
- **ANA / Harris Poll (151 US marketers, 21-24 Oct 2025):** 71% say setting ethical and privacy standards is the top step in preparing for agent-led commerce. eMarketer's headline: "guardrails before growth". **[medium]** — [eMarketer](https://www.emarketer.com/content/marketers-putting-guardrails-before-growth-on-ai-agents)
- **Yahoo DSP via eMarketer (Jan 2026):** 55% of marketers trust agentic AI to plan and execute tasks, and one in five distrust it. Sample not seen. **[weak]** — [eMarketer](https://www.emarketer.com/content/ad-platforms-launch-agentic-ai-features--marketer-distrust-persists)
- **Contentful / Atlantic Insights (350 marketing decision-makers, 2026):**
  - 43% would let agents act autonomously across most marketing tasks.
  - 34% confine agents to low-stakes decisions.
  - 21% require approval before execution.

  These came from a chart page; a second search could not confirm them. **[weak]** — [Agile Brand Guide chart](https://research.agilebrandguide.com/chart/43-of-teams-would-allow-ai-agents-to-act-autonomously-across-most-marketing-task); [Contentful report page](https://www.contentful.com/resources/atlantic-report-success-in-the-agentic-era/)
- **Iterable:** no 2025-26 autonomy survey was found. Its last marketer survey is from 2023 fieldwork. Not used.
- **HubSpot:** the 2026 State of Marketing exists, but no autonomy numbers surfaced. Not used. — [HubSpot](https://www.hubspot.com/stateofmarketing)

### India
- **EY-CII "The AIdea of India: Outlook 2026"** (200 organisations, 20+ industries, C-suite respondents, released Nov 2025):
  - 24% report active agentic-AI deployment.
  - 47% have several GenAI use cases live and 23% are in pilot.
  - 64.5% rate data governance and security as a "very severe" challenge, and 78% struggle with system integration.
  - Agentic use cases named are operations, supply chain, service and finance; **marketing is not called out**.

  **[medium]** — [EY press release](https://www.ey.com/en_in/newsroom/2025/11/india-s-ai-shift-from-pilots-to-performance-47-percent-of-enterprises-have-multiple-ai-use-cases-live-in-production-ey-cii-report); [EY survey page](https://www.ey.com/en_in/insights/ai/agentic-ai-india/aidea-survey-findings); [exchange4media](https://www.exchange4media.com/digital-news/indias-ai-adoption-surges-as-47-of-enterprises-scale-multiple-genai-use-cases-149445.html)
- **BCG CMO survey, India cut (June 2026):** 53% of Indian CMOs project 5-9% AI-driven topline growth (global 43%). Indian CMOs directly own AI investment decisions. BCG/ET commentary says few Indian firms have moved past pilots to autonomous marketing operations, and that this ambition-reality gap is larger in India than globally. **[medium]** — [Business Standard](https://www.business-standard.com/technology/tech-news/transforming-the-terrain-india-s-marketing-space-ready-for-ai-revolution-126062100703_1.html); [MediaNews4U](https://www.medianews4u.com/53-of-indian-cmos-project-ai-driven-revenue-growth-surpassing-global-43-average-bcg-global-report/); [BCG India piece](https://www.bcg.com/ja-jp/publications/2026/india-marketers-prepare-for-agentic-ai)
- **Adobe 2026 AI and Digital Trends, India release (25 June 2026; Oxford Economics, 3,000 executives and 4,000 consumers globally; India sample not seen):**
  - 60% of Indian consumers are interested in creating a personal AI agent, and 55% would interact with a brand's agent. Both are the highest in APAC.
  - Data integration and quality are the main barriers to scaling.
  - Indian executives show the highest personal confidence in adopting new tools in APAC (26%).
  - A search summary also said only 10% of brands have embedded agentic AI in customer support and 53% cite internal resistance to change. **I could not confirm whether those two figures are India or APAC.**

  **[medium]** for the consumer figures, **[weak]** for the 10% and 53%. — [Adobe India release](https://news.adobe.com/en/apac/news/2026/06/ai-digital-trends-india); [Adobe India media alert PDF](https://news.adobe.com/en/apac/news/downloads/pdfs/2026/06/260625adobe-2026-ai-and-digital-trends-india-media-alert-final.pdf); [Deccan Herald](https://www.deccanherald.com/technology/artificial-intelligence/65-of-indian-shoppers-trust-ai-recommendations-highest-in-asia-pacific-adobe-report-4061340)
- **KPMG / University of Melbourne, Trust in AI 2025** (48,000+ people, 47 countries, Nov 2024-Jan 2025):
  - 76% of Indians are willing to trust AI, against 46% globally.
  - 97% use AI at work intentionally.
  - 78% are still concerned about negative outcomes.

  This is about the public and workers, not marketing buyers. **[medium]** — [KPMG India](https://kpmg.com/in/en/insights/2025/05/trust-attitudes-and-use-of-ai.html); [India snapshot PDF](https://assets.kpmg.com/content/dam/kpmg/xx/pdf/2025/02/trust-attitudes-and-use-of-ai-india-snapshot.pdf); [CRN India](https://www.crn.in/news/india-leads-the-ai-trust-revolution-kpmg-study-shows-76-of-indians-confident-in-ai)
- **NASSCOM "Enterprise Experiments with AI Agents - 2025 Global Trends"** (100+ large and mid enterprises, global, not India-only):
  - 88% are ready to set aside budget to test agents in 2025.
  - 60%+ are somewhere between pilot and production.
  - Most focus on task-based agents and process automation **with human feedback**. This is qualitative; no percentage was given.

  **[medium]** — [NASSCOM Community](https://community.nasscom.in/communities/nasscom-insights/enterprise-experiments-ai-agents-2025-global-trends)
- **Meta/BCG India:** no 2025-26 Meta-BCG report on agentic marketing was found. Gap.
- **KPMG India CXO agent survey:** not found. Gap.
- **Market signal:** MoEngage (Bengaluru) bought Aampe, which runs one RL agent per customer deciding message, timing, frequency and channel. The deal was announced 23-24 June 2026; the amount is undisclosed (one unconfirmed report says "tens of millions", all cash). Swiggy is named as an Aampe user. Aampe will plug into other engagement platforms ("Start Anywhere") as well as run natively in MoEngage. This updates MARKET_RESEARCH, which lists Aampe as independent. **[medium]** — [MoEngage](https://www.moengage.com/in-the-news/moengage-acquires-aampe-to-bring-11-agentic-decisioning-to-b2c-marketing-teams/); [YourStory, June 2026](https://yourstory.com/2026/06/moengage-acquires-san-francisco-based-ai-infra-startup-aampe); [Shopifreaks](https://www.shopifreaks.com/moengage-acquires-us-ai-startup-aampe-in-a-tens-of-millions-all-cash-deal-to-scale-agentic-per-customer-marketing-ahead-of-a-planned-ipo/)

---

## 3. Jobs to be done: what people want automated end to end, and what they keep

### What the evidence says directly
- **Reporting and analysis are the first jobs people hand over.** In StackAdapt's survey, AI use is highest for reporting and summaries (77%) and performance analysis (74%), and lowest for campaign set-up (40%) and budgeting and bidding (40%). Respondents were most comfortable letting automation run in reporting and analysis (53%). **[strong, primary read]** — [StackAdapt PDF](https://s3.amazonaws.com/media.mediapost.com/uploads/StackAdapt_AI-Delegation-Gap-Report_2026.pdf)
- **Strategy, brand, budget and accountability stay human.** StackAdapt's conclusion, also quoted from EMARKETER analyst Nate Elliott: advertisers will "retain some level of control over existential questions like strategy, positioning, and budget", so "co-pilot models are more realistic than fully autonomous agents". **[strong for what the report says; it is opinion, not a measurement]**
- **Trust by task (PwC, executives, all functions):** data analysis 38% and performance improvement 35% are trusted most. Anything touching money (financial transactions 20%) is trusted least. **[medium]** — [PwC](https://www.pwc.com/us/en/tech-effect/ai-analytics/ai-agent-survey.html)
- **Generic advice gets ignored.** Only 6% of advertisers act on in-platform AI recommendations "almost always". The top reason for ignoring them is that they feel generic or irrelevant (42%). Others: not aligned with strategy (22%), no explanation (17%), and "timed to drive spend rather than performance" (12%), which is a conflict-of-interest fear. **[strong, primary read]**
- **Defaults quietly hand over authority.** Only 22% chose most of their platform automation features deliberately. 11% say most active features are defaults or unclear. **[strong, primary read]**

### What no survey measured
I found **no 2025-26 survey that asks CRM or retention marketers, task by task, whether they would let an agent own send time, frequency, offer choice or discount depth**. The table below therefore combines the direct evidence above with adjacent signals. The "Evidence" column says which is which.

### Table: task × want automated? × condition for trust

| Marketing task (B2C retention/CRM lens) | Want it automated end to end? | Condition buyers set before they trust it | Evidence |
|---|---|---|---|
| Performance reporting, summaries, "what changed and why" | **Yes, first.** Most used and most delegated job | Ties to a named KPI, explains its reasoning, reconciles with finance numbers | StackAdapt 77%/74% use, 53% comfortable automating **[strong]**; PwC data analysis most trusted **[medium]** |
| Send-time optimisation | **Yes.** Already autonomous in most engagement platforms | Stays inside quiet hours and regulatory windows; a lift test against a fixed send time | Adjacent: low-risk, reversible jobs get full autonomy (G2 ~50% for low-risk **[medium]**). Platform defaults **[background, unverified]** |
| Frequency and fatigue capping (including deciding *not* to send) | **Yes, within a hard ceiling** | Human sets the maximum touches per week and per channel; the agent may only send less; unsubscribe and complaint rates are monitored | Adjacent: opt-out pain in earlier notes; Aampe and MoEngage sell "whether to send at all" **[vendor claim]**; "acts within rules" 78% **[strong]** |
| Channel choice (push, email, SMS, WhatsApp) | **Mostly yes, within rules** | Allowed channels per consent status and DLT template; cost per message counted | Adjacent: consent rules in MARKET_RESEARCH **[moderate]**; "within rules" 78% **[strong]** |
| Audience or list building and segment refresh | **Yes, with a preview** | Shows who is in or out and why; suppression lists and consent are always applied; size change alerts | Adjacent: segment building is a common agent job (vendor roundup **[weak]** — [Digital Applied](https://www.digitalapplied.com/blog/marketing-automation-statistics-2026-data-points)) |
| Creative and copy variants | **Partly.** Draft yes, publish on brand-sensitive lines no | Brand voice rules, banned claims list, legal review for offers; human approval for new templates | Brand risk is the top barrier, 63% **[strong]**; consumer distrust of fully AI-made content (earlier notes) |
| Offer choice from an approved catalogue | **Yes, if the catalogue is fixed** | Human approves the offer menu; the agent only picks among approved items; a holdout shows incremental value | Adjacent: "prepare for approval" 89% and "within rules" 78% **[strong]**; offer-waste concern in earlier notes |
| Discount depth or new offers | **No.** Humans keep it | Hard floor on margin; finance sign-off; per-customer and total budget caps | PwC: money tasks least trusted (20%) **[medium]**; "budget" kept human (StackAdapt) **[strong]**; CFO scepticism (earlier notes) |
| Budget pacing inside a set total | **Yes, within a ceiling** | Human sets the total and daily cap; the agent can move money only between approved lines; pause and undo | StackAdapt: budgeting and bidding least used (40%), with limited-budget testing (36%) and undo (31%) as unlocks **[strong]** |
| Setting total budget or the strategy and goal | **No** | Remains a human decision | StackAdapt "strategy, brand, budget, accountability" **[strong]** |
| Experiment design and holdouts | **Yes, with guardrails.** A system-run holdout is welcome because it proves value | Holdout size agreed up front; nobody can turn it off silently | Earlier notes: buyers want proof but see holdouts as "lost revenue" **[weak]**; Haus: 60% trust incrementality most **[medium]** |
| Compliance checks (consent, DND, DLT, DPDP) | **Automate the check, never the override** | Deterministic rules, not model judgement; a full log for auditors | IT leaders: 13% have governance, 19% trust vendor hallucination protection **[medium]**; DPDP from MARKET_RESEARCH |
| Customer-facing conversational replies | **Cautious** | Supervised for now; escalation to a human | Gartner "supervised 18-24 months" **[weak]**; Forrester one-third of brands erode trust **[medium]**; Salesforce India: 86% would trust AI to respond (earlier notes, vendor) |

---

## 4. Trust barriers, and what earns more autonomy over time

### Barriers, ranked by evidence
1. **Brand risk and brand safety.** The top barrier for 63% of advertisers, and the top first-ranked concern. **[strong]** — [StackAdapt PDF](https://s3.amazonaws.com/media.mediapost.com/uploads/StackAdapt_AI-Delegation-Gap-Report_2026.pdf)
2. **Data quality.** 56% (StackAdapt) **[strong]**. Fragmented data pipelines 49% (secondary). Fewer than one in five have high data readiness (Capgemini) **[medium]**. Data integration is the top scaling barrier in India (Adobe 2026) **[medium]**.
3. **Lack of transparency or explanation.** 36% (StackAdapt). Explanation and reasoning rated lowest of all quality dimensions of AI output, at 31% positive **[strong]**. Lack of transparency is also named by Capgemini **[medium]**.
4. **Hallucination and accuracy.** Only 19% of IT application leaders highly trust vendor hallucination protection (Gartner) **[medium]**. Accuracy is the second concern after privacy for Salesforce's marketers **[medium]**.
5. **Privacy and security.** The top concern in Salesforce State of Marketing **[medium]**. 71% of US marketers put ethical and privacy standards first (ANA/Harris) **[medium]**. 64.5% of Indian firms call data governance and security "very severe" (EY-CII) **[medium]**. 74% of IT leaders see agents as a new attack vector (Gartner) **[medium]**.
6. **Performance volatility.** 36%. **Poor integration** 34% (StackAdapt) **[strong]**. 78% of Indian firms struggle with integration (EY-CII) **[medium]**.
7. **Proof of ROI.** 45% of martech leaders say vendor agents miss the promise (earlier notes). Gartner expects 40%+ of agentic projects to be cancelled, citing unclear value. Only 30% of CMOs are AI-ready (Gartner 2026) **[medium]**.
8. **Accountability ("who is responsible when the agent spams").** StackAdapt asked it directly ("Who is accountable for AI outcomes?", multiple answers allowed):
   - the team collectively: 31%
   - leadership, or whoever approved the recommendation: 31%
   - the campaign manager: 28%
   - the individual operator: 26%
   - **no clear accountability: 17%**

   Brands are more likely than agencies to blame the individual operator. **[strong, primary read]** Related signals:
   - Practitioners, who carry the consequences, are much less comfortable than leaders. Autonomous AI comfort is about 34% for hands-on practitioners against about 58% for strategic decision-makers (StackAdapt, read from a chart) **[strong for direction, approximate values]**.
   - Agencies are more likely than brands to say leadership expectations run ahead of readiness (63% vs 55%) **[strong]**.
   - 59% overall say leadership expectations run ahead of readiness (secondary summary of StackAdapt) **[medium]**.
9. **Job fears.** 18% of marketing leaders have cut roles due to automation, and 16% have created roles (Gartner, Sep 2026) **[medium]**. Capgemini: 70% expect agents to force restructuring **[medium]**. This fear sits mainly with practitioners, the same people who must operate the agent.
10. **Conflict of interest.** 12% ignore platform AI advice because it seems "timed to drive spend rather than performance" **[strong]**. This matters for any agent paid by usage or volume.

### What unlocks more autonomy (stated by buyers)
- StackAdapt "top unlocks" **[strong]**:
  - confidence level or predicted impact shown before a change is applied: 37%
  - ability to test on a limited budget before scaling: 36%
  - clear explanation of why a change was made: 35%
  - full audit trail and change log: 31%
  - easy undo or revert: 31%

  No single item dominates; buyers want the **bundle**.
- Practitioners specifically ask for limited-budget tests, brand-safety constraints, and the ability to **pause or reverse** AI changes. That is a kill switch plus rollback **[strong]**.
- **Mature governance as defined by Deloitte:** written limits on which decisions agents take alone versus with approval, real-time monitoring, and audit trails. Only 21% have it **[medium]**.
- **Staged rollout works better.** Half of organisations restrict agents to low-risk or trusted users first (Gartner) **[medium]**. Human-in-the-loop programmes were twice as likely to hit big savings (G2) **[medium]**. 90% of executives see human involvement as positive or cost-neutral (Capgemini) **[medium]**.
- **Proof through holdouts.** Incrementality testing is the most trusted method (Haus, 60%, earlier notes) **[medium]**. Nothing directly ties "holdout evidence" to "granted more autonomy"; that link is our inference.
- **Gartner's direction of travel:** "staged autonomy" with conversational programmes supervised for 18-24 months **[weak]**. Its 2026 Digital Marketing Hype Cycle frames autonomous marketing as the destination, reached through cost and trust governance **[medium]**.

---

## 5. Buyer personas for autonomy in India B2C, and their buying criteria

Evidence specific to India and to each role is thin. Each row says how much is evidence and how much is inference.

| Persona | What they want from autonomy | What they fear | Buying criteria for an autonomous agent | Evidence |
|---|---|---|---|---|
| **CMO / marketing head** | Growth from flat budgets; seen as the "AI leader"; output at scale. Indian CMOs are the most bullish (53% expect 5-9% AI-led topline growth) and own AI budgets | Agent-washing, public brand incidents, failing the CEO's raised expectations (94% feel them) | Rupee results with a control group; a brand-safety guarantee; a story for the board; time to value | BCG 2026 **[medium]**; Gartner 30% ready **[medium]**; IBM India (earlier notes: 44% ready for agentic, 26% have responsible-AI guidelines) **[medium]** |
| **CRM / retention / CVM head** (runs the engagement platform day to day) | Less manual journey-building; send-time, frequency and channel decided per customer; fewer wasted sends | Spam incidents traced to them; unsubscribes; DLT or consent breaches; losing control of a channel they are measured on | Hard caps they set; preview before launch; a per-action log; one-click pause and rollback; works inside their existing CEP (MoEngage, CleverTap, Netcore) | StackAdapt practitioner gap **[strong, non-India]**; MoEngage-Aampe deal shows Indian CEPs selling per-user agents **[medium]** |
| **Growth / performance lead** (often at D2C or quick commerce) | Speed; many experiments; automatic budget pacing; fast creative iteration | Volatile results; agents that spend to please the platform; no clear attribution | Limited-budget test first; predicted impact before applying; outcome-linked fees | StackAdapt unlocks **[strong, non-India]**; D2C CAC pressure in earlier notes **[weak]** |
| **CFO / finance** | Lower cost per retained customer; less discount leakage; predictable bills | Discount depth set by a machine; usage bills that grow with volume; ROI claims without controls | Discount floors and budget ceilings enforced by the system; holdout-based incremental value; fixed or capped pricing, or fees tied to measured lift | Earlier notes: 40% of CMOs name the CFO as most sceptical **[medium]**; PwC money tasks least trusted **[medium]** |
| **Compliance / legal / DPO** | Fewer manual checks; evidence ready for audits | Messages to people without consent or registered on DND; DPDP penalties (up to INR 250 crore); AI-written claims about offers or products | Deterministic consent, DND and DLT gates that the agent cannot override; a full decision log kept at least 1 year; erasure carried into models | DPDP and TRAI details in MARKET_RESEARCH **[moderate]**; Gartner 13% have governance **[medium]** |
| **IT / CIO / security** | One governed platform, not agent sprawl | New attack surface (74%); data leaving the account; hallucination; vendor lock-in | Runs in their cloud account; role-based access; agent identity and permissions; monitoring; vendor trust on hallucination (only 19% have it) | Gartner IT leaders 2025 **[medium]**; EY-CII India 64.5% and 78% **[medium]** |

**Mid-market vs enterprise (mostly inference; no India split found):**
- **Enterprise** (telcos, banks, large retailers) buys through committees in which compliance, IT and the CFO hold a veto. They want staged autonomy, written authority limits and audit evidence. Deloitte, Gartner and Capgemini governance figures describe them. Expect pilots in low-risk jobs (reporting, send time, frequency) for 6-12 months before any offer or budget autonomy.
- **Mid-market and D2C** has thinner teams, less data science and more CEP-native buying. They accept more autonomy faster where the platform default already does it, and are more price-sensitive. The risk is "defaults become delegation": autonomy switched on without review (only 22% chose features deliberately, StackAdapt) **[strong, non-India]**. They will want outcome-linked or low entry pricing (section 6) **[inference]**.

---

## 6. Willingness to pay and pricing models for autonomous agents

**No 2025-26 survey of marketing buyers' (or Indian buyers') willingness to pay for autonomous agents was found.** The search budget ran out before more targeted searches. What exists:

- **Enterprise software buyers' stated preference for AI pricing (Futurum):** 1H 2025: consumption 38%, outcome-based 31%, per-user 20%, bundled 11%. 1H 2026: consumption 43%, outcome-based 27%, fewer than one in five prefer per-user. Outcome pricing has a stable but minority appeal, and per-seat is out of favour. **[medium]** (from earlier notes; not re-checked this session) — [Futurum 2025](https://futurumgroup.com/press-release/enterprise-software-buyers-prefer-consumption-and-outcome-based-pricing/); [Futurum 2026](https://futurumgroup.com/press-release/are-outcome-based-and-hybrid-ai-pricing-models-rewriting-the-vendor-playbook/)
- **G2 2025 Buyer Behavior:** 39% prefer pay-as-you-go. This is not specific to agents. **[medium]** — [SaaStr summary](https://www.saastr.com/%f0%9f%9a%80-top-5-saastr-learnings-from-g2s-2025-buyer-behavior-report-you-gotta-be-ai)
- **BCG 2025 B2B pricing:** 40% of IT buyers cite cutting seats as their main lever on software spend. Buyers value complete task execution over isolated steps. **[medium]** (earlier notes) — [BCG](https://www.bcg.com/publications/2025/rethinking-b2b-software-pricing-in-the-era-of-ai)
- **Outcome pricing in practice:**
  - Intercom Fin charges $0.99 per resolution **[moderate]**.
  - Netcore offers a 75/25 outcome-linked model **[vendor claim]**.
  - Flytxt sells "outcome-based SaaS" in telco CVM **[vendor claim]**.

  All three are from MARKET_RESEARCH, not re-checked.
- **[background, unverified]:**
  - Salesforce launched Agentforce at about $2 per conversation (late 2024), then added "Flex Credits" priced per action (about $0.10, mid-2025) and per-user add-ons.
  - HubSpot meters Breeze agents with credits.
  - Zendesk and Sierra have announced outcome- or resolution-based pricing for AI agents.

  Together these show the market moving from seats to **actions or outcomes**, usually as a **hybrid**: a platform fee plus usage or outcome.
- **The conflict-of-interest signal matters for pricing.** 12% of advertisers already distrust AI advice "timed to drive spend" **[strong]**. Pricing per message sent, or per action, rewards an agent for doing more, which is the opposite of what the CRM head and CFO want (fewer, better sends). Pricing on measured incremental outcome, or a flat platform fee with a cap, avoids that.
- **India anchors** (from MARKET_RESEARCH, **[weak]**): Indian CEP contracts run INR 2.5-15 lakh/yr in the mid-market and INR 15-40 lakh+/yr in enterprise, with 15-30% first-contract discounts. An autonomous decision layer will be judged against that bill.
- **% of incremental revenue:** no survey found of how many buyers accept it. Earlier notes say churn and analytics services rarely publish gain-share terms because attribution is disputed **[weak]**. A randomised holdout is the precondition for anyone accepting this model.

---

## 7. Updates and corrections to the earlier notes

1. **"They do not trust autonomous agents" (MARKET_RESEARCH, CMO persona) is too absolute.** Better: buyers broadly accept **bounded autonomy**: 78% are comfortable with AI acting inside human rules, and about half would grant full autonomy for low-risk or proven work. What they refuse is autonomy over strategy, brand, total budget and discount depth. Cross-function trust in *fully* autonomous agents did fall (Capgemini 43% to 27%).
2. **Gartner 15% (Sep 2025)** in buyer_pain_and_trust.md: now has detail. n=360, May-June 2025; 75% are piloting or deploying some agents; 13% strongly agree they have the right governance; 19% highly trust vendor hallucination protection; 74% see a new attack vector.
3. **Gartner 2025 CMO Spend** is superseded by the **2026 CMO Spend Survey**: 15.3% of marketing budget goes to AI, and 30% are AI-ready. MARKET_RESEARCH anticipated this ("a 2026 edition may have replaced it").
4. **New Gartner baseline:** 16% of marketing work is AI-automated in 2026, expected 36% by 2028. Use it instead of vague "AI adoption" percentages.
5. **McKinsey marketing gap:** at most 10% of companies scale agents in any single function, marketing and sales included. This fills the "no marketing-specific McKinsey figure" gap partly; the exact figure is still not read.
6. **Aampe is now part of MoEngage** (June 2026). Update the competitor tables. The leading Indian CEP now sells per-customer autonomous agents, which raises the bar on "AI decisioning" for any Indian CRM pitch.
7. **India gaps partly closed:**
   - EY-CII: 24% of Indian firms have active agentic deployment.
   - BCG: India's CMOs are the most bullish.
   - Adobe: Indian consumers are the most open to agents in APAC.
   - KPMG: 76% public trust in AI.

   Still no India marketing-buyer autonomy survey, and nothing from NASSCOM, KPMG India or Meta-BCG.
8. **Salesforce agent adoption among marketers is 13%** (10th edition). Do not use the "34% run autonomous agents in production" figure that circulates on statistics sites.

---

## 8. Implications for Marketing AI

1. **Sell "autonomy within rules you set", not "autonomous marketing".**
   - The demand curve breaks between "acts within rules" (78%) and "acts alone" (50%).
   - Design the product as explicit levels: recommend, prepare for approval, act within rules, act alone on proven low-risk jobs.
   - Let the buyer choose a level **per job**, not per account.
2. **Start autonomy where buyers already accept it** (reporting, send time, frequency within caps, channel within consent, audience refresh with preview). Keep **discount depth, total budget, new offers and brand-sensitive copy** behind approval by default. This matches the table in section 3 and the PwC and StackAdapt task gradients.
3. **Ship the trust bundle, not one feature.** Buyers asked for all of these, and none dominated:
   - predicted impact and confidence before a change is applied
   - a limited-budget or small-cohort test first
   - a plain-English reason for every action
   - an append-only action log
   - one-click pause and undo
   - hard ceilings on frequency, budget and discount that the agent cannot override
4. **Make the holdout the bridge to more autonomy.** A persistent randomised holdout turns "trust me" into rupee evidence. The CFO and CMO both ask for this, and it is what an outcome fee would need.
   - **Our inference, not survey-backed:** unlock the next autonomy level automatically only when a job has shown significant incremental lift over N cycles with no guardrail breaches.
5. **Design for the practitioner, who is the real blocker.** Leaders are about twice as comfortable with autonomy as hands-on practitioners. Practitioners also carry the job fear and the blame. Give the CRM head control (caps, preview, kill switch) and visible credit ("agent saved X hours, your caps prevented Y over-sends").
6. **Make compliance a hard gate, not a model judgement.** In India this means consent, DND, DLT templates and DPDP logs, all deterministic, with no override by the agent. That is what compliance and IT will buy.
7. **Pricing: avoid per-message or per-action fees that reward sending more.** Lead with a platform fee plus an optional fee tied to holdout-measured incremental value (lower bound of the confidence interval). Price it below the CEP bill the buyer already pays. Expect only a minority (about a quarter to a third) to prefer pure outcome pricing.
8. **Watch MoEngage-Aampe.** Per-customer autonomous decisioning is now native to India's leading CEP. Our edge has to be governed, measured, auditable autonomy that works alongside the client's existing CEP, not a rival "more autonomous" agent.

## 9. Root causes: why buyers resist autonomy, and what would let an agent run unattended

**Status of the "go deeper" request.** The coordinator asked for 25+ opened primary sources, practitioner voices and ad-platform case histories. This turn could not deliver that:
- **The WebSearch budget was used up** (200 calls per turn, shared by every agent).
- **WebFetch fails on DNS.**
- **The proxy blocks direct downloads** from every host tried except Amazon S3 and raw.githubusercontent.com. That includes Gartner, EY, KPMG, Adobe, BCG, Capgemini, Business Wire, eMarketer, PPC Land, Reddit and Wikipedia.

So this section rests on three kinds of evidence:
- **One primary report read in full:** StackAdapt / NewtonX. It is the only source where I saw question wording, sample and sponsor.
- **Search summaries** of the sources in sections 1-6.
- **Background knowledge**, marked **[background, unverified]**.

Treat the root causes below as **well-argued hypotheses with partial evidence**, not settled findings. The follow-up list at the end says exactly what to open next.

### 9.1 Sponsor bias in the evidence base
| Source | Sponsor's interest | How to read it |
|---|---|---|
| StackAdapt "AI Delegation Gap" (NewtonX, n=500 + 187 clients, May-June 2026) | Sells AI ad-buying. It benefits from "co-pilot with controls", which is its product framing | The question wording is neutral and sample sizes per country are published. The conclusions ("proactive intelligence, not unchecked automation") match its product. Trust the percentages more than the framing. **[primary read]** |
| Salesforce State of Marketing | Sells Data Cloud and Agentforce | The "unified data → more agent use" link is correlation sold as cause |
| Braze CER | Sells engagement AI | The "trust gap" framing positions Braze as the fix |
| G2 AI Agents report | Review marketplace; neutral-ish | "Human in the loop was twice as likely to hit 75%+ savings" is self-reported and probably shaped by who answered |
| Capgemini, Deloitte, PwC, BCG, EY, KPMG | Sell transformation and governance consulting | All conclude "governance and human oversight are needed", which is also what they sell. Their adoption and trust percentages are still useful |
| Gartner | Sells advice to buyers; no product interest | The most neutral. Predictions are opinions, not measurements |

### 9.2 Root causes of resistance (deepest first)

1. **Accountability without authority: career risk.**
   - When an agent makes a bad call, nobody knows who owns it. 17% say there is no clear accountability, and the rest split it between the team, approver, manager and operator. Brands tend to blame the individual operator. **[strong, StackAdapt]**
   - The person most exposed (the hands-on practitioner) is the least comfortable with autonomy: about 34%, against about 58% for leaders. **[strong]**
   - EMARKETER's analyst in the report: "The minute automation scales past the point where someone can answer for it, advertisers will pull back." **[primary read, expert opinion]**
   - *Root cause:* autonomy moves decisions to a machine but leaves the blame with a named human. People rationally refuse that trade.
2. **Outcomes they cannot verify.**
   - Only 22% (EMEA) are "very confident evaluating whether AI is making the right campaign decisions". **[strong]**
   - AI's proven benefits are efficiency: 62% report less manual optimisation time. Business results are much weaker: 27% report better ROAS and 22% lower CPC. **[strong]**
   - The report itself says it cannot tell whether AI caused better results or better teams simply adopt more AI. **[strong]**
   - Measurement confidence was already stalling before agents (earlier notes: TransUnion, Haus, Forrester). **[medium]**
   - *Root cause:* without a counterfactual, an autonomous agent's results are unfalsifiable. A buyer cannot tell a good agent from a lucky or self-crediting one.
3. **Incentive conflict with the vendor.**
   - 12% ignore platform AI advice because it seems "timed to drive spend rather than performance". **[strong]**
   - *Root cause:* when the agent's maker earns more as the buyer spends or sends more, the buyer suspects every "optimisation". This was the core distrust of platform automation in advertising **[background, unverified]**.
4. **Brand risk is asymmetric.**
   - Brand risk is the top barrier (63%) and the top first-ranked concern. **[strong]**
   - *Root cause:* the gain from one more autonomous optimisation is small and spread out. The loss from one public mistake (a wrong offer, an offensive line, spam to a DND customer) is large, visible and personal. Loss aversion plus accountability (cause 1) makes a "no" rational.
5. **Context blindness from fragmented data.**
   - 34% say AI advice ignores their goals or context, and 42% ignore advice as "generic or irrelevant". **[strong]**
   - Data barriers: 49% cite fragmented pipelines, 41% unintegrated CRM or first-party data, and only 19% have AI fully integrated into workflows. **[strong]**
   - India: 78% struggle with integration (EY-CII). **[medium]**
   - *Root cause:* an agent that cannot see margin, stock, consent, or what another campaign just sent will make locally optimal, globally wrong decisions. Buyers have learned this from experience.
6. **Opacity.**
   - Explanation and reasoning is the lowest-rated quality of AI output (31% positive). 37% cite limited ability to explain why a recommendation was made. **[strong]**
   - *Root cause:* if you cannot see why an agent acted, you cannot defend it to your boss, so you cannot let it act.
7. **Top-down pressure without an operating model.**
   - 79% feel pressure to use more AI, and 35% rate the C-suite as an extreme source of it. 59% say expectations exceed readiness. **[strong]**
   - Only 30% of CMOs are AI-ready (Gartner 2026), and only 21% have mature agent governance (Deloitte). **[medium]**
   - *Root cause:* autonomy is being mandated before anyone has written down who approves what. Practitioners resist quietly by accepting defaults, ignoring advice (only 6% act on it "almost always") or keeping manual overrides.
8. **Past automation disappointments and agent-washing.**
   - 45% of martech leaders say vendor agents miss their promises (earlier notes), and Gartner expects 40%+ of agentic projects to be cancelled. **[medium]**
   - Practitioner folklore about black-box ad automation (wasted spend on brand terms, low-quality placements, no search-term visibility) is widespread **[background, unverified; anecdotal sources not collected this turn]**.
9. **Job threat (secondary, but real).**
   - 18% of marketing leaders have already cut roles because of automation (Gartner, Sep 2026). **[medium]**
   - The people asked to switch the agent on are often the people whose tasks it replaces.

### 9.3 What moves buyers: the ad-platform precedent
The strongest real-world evidence of how marketers come to accept autonomy is Google's Smart Bidding and Performance Max and Meta's Advantage+. This turn could not open sources on them. The summary below is **[background, unverified]** and must be checked before external use.
- **Adoption came through performance plus defaults, not trust.** Automated bidding became the default; Performance Max replaced older campaign types (Smart Shopping and Local campaigns were upgraded automatically in 2022); Meta folded options into Advantage+. Many advertisers adopted because the manual alternative was removed or performed worse in side-by-side tests, not because they trusted the black box. This matches StackAdapt's finding that only 22% deliberately chose most of their automation.
- **Advertisers then won controls back, piece by piece.** Through 2023-2025 Google added (from memory):
  - brand exclusions for Performance Max
  - account-level and then campaign-level negative keywords
  - search-term insights, then fuller search-terms reporting
  - channel-level performance reporting
  - asset-level reporting
  - placement exclusions

  Meta added caps on how much budget goes to existing customers, and the ability to exclude or define audiences in Advantage+.
- **The pattern:** controls that limit downside (what the agent may never do) and visibility (what it did, where and why) were the price of continued use. Advertisers did not ask to approve every bid. They asked for **fences, receipts and the ability to test against a control**.
- **Experiments made it measurable.** Google's built-in experiments and Meta's lift and A/B tools let advertisers compare automated against manual on a split of traffic **[background, unverified]**. This is the ad-world version of the holdout.
- **Lesson for CRM agents:** autonomy spreads when (a) the agent is clearly better on a test the buyer can see, (b) the buyer keeps hard fences on brand, budget and audience, and (c) every action is reported. It does not spread from trust statements.

### 9.4 Practitioner voices (India: D2C, BFSI, telecom)
**Not collected.** Reddit, LinkedIn, conference talks and Indian trade press (exchange4media, afaqs, Storyboard18, ET BrandEquity) could not be opened. Two pages surfaced in searches and are worth opening first:
- [exchange4media, "Is Brand India ready for agentic AI and autonomous marketers?"](https://www.exchange4media.com/digital-news/is-brand-india-ready-for-agentic-ai-and-autonomous-marketers-141206.html)
- [ISB, "Marketing is quietly being rewritten by agentic AI"](https://www.isb.edu/news/isb-in-the-news/marketing-is-quietly-being-rewritten-by-agentic-ai)

Netcore's BFSI 2026 trends piece frames agentic marketing as "AI trusted to act within clear guardrails" [devdiscourse](https://www.devdiscourse.com/article/technology/3802365-netcores-financial-services-trends-2026-agentic-marketing-marks-the-next-growth-shift-for-financial-services-in-india) **[vendor, weak]**.

### 9.5 Conclusions: what would make a buyer let an agent run unattended
An agent is left unattended on a job when **all six** conditions hold. Each one answers a root cause above. The evidence grade is per link, not per conclusion.

1. **Someone can still answer for it** (cause 1). There are written authority limits per job, a named owner who set those limits, and a log that shows the agent stayed inside them. When something goes wrong, the question becomes "were the limits right?", not "who let the robot do this?". This shifts blame from the operator to an agreed policy. *Evidence:* accountability split and 17% unclear **[strong]**; Deloitte's governance definition **[medium]**.
2. **The downside is fenced, not just monitored** (causes 4 and 6). Hard limits the agent cannot cross:
   - frequency per customer
   - discount floor
   - budget ceiling
   - consent, DND and DLT gates
   - banned claims

   A kill switch and undo sit on top. *Evidence:* StackAdapt unlocks (undo 31%, limited-budget test 36%) **[strong]**; the ad-platform give-back pattern **[background]**.
3. **Results are proven against a counterfactual the buyer controls** (cause 2). There is a persistent random holdout the agent cannot touch, and incremental value is reported with a confidence range. Autonomy on a job is extended only after it has beaten the holdout. *Evidence:* only 22% confident judging AI decisions **[strong]**; incrementality is the most trusted method (Haus) **[medium]**. The step "proof unlocks autonomy" is our inference, consistent with the ad-platform history.
4. **Incentives line up** (cause 3). The vendor does not earn more when the agent sends or spends more. Use a flat fee, or a fee tied to measured incremental value, never per message. *Evidence:* 12% suspect spend-driven advice **[strong]**; pricing preferences **[medium]**.
5. **The agent sees enough context and says why** (causes 5 and 6). Each action carries a plain reason, the data it used, and the predicted impact before it is applied. The agent refuses to act when key data (consent, margin, recent contacts) is missing. *Evidence:* 34% context complaints, 37% cannot explain, 37% want predicted impact **[strong]**.
6. **Autonomy is earned by job, in stages, starting where the cost of error is low** (causes 7 and 8):
   - first reporting
   - then send time and frequency within caps
   - then channel, audience refresh and offer choice from an approved menu
   - **never** discount depth or total budget without sign-off

   *Evidence:* the authority ladder of 90%, 89%, 78% and 50% **[strong]**; G2 says about 50% grant full autonomy only for low-risk work **[medium]**; Gartner advises staged autonomy **[weak]**.

**Net:** buyers do not lack trust in AI in the abstract. They lack a **safe way to be accountable** for decisions they did not make and cannot verify. A product that turns autonomy into **pre-agreed policy, fenced actions, holdout-proven results and aligned fees** removes the reasons to refuse. One that just promises better AI does not.

## Open gaps for a follow-up session
**Priority list for the next turn (the search budget resets on a new user message).**
- **Open the primary documents:**
  - Capgemini "Rise of agentic AI" PDF
  - Gartner press releases (2026 CMO Spend, 16%→36%, Sep 2026 jobs)
  - EY-CII AIdea of India 2026 PDF
  - Adobe India media alert PDF
  - Deloitte State of AI 2026
  - G2 AI Agents PDF
  - Braze CER full report
  - BCG agentic marketing 2026

  Capture the question wording and sponsor for each. These downloads are blocked by the proxy today, so they need search-summary deep reads or a policy change.
- **Ad-platform history:** dated Google Ads and Meta announcements of PMax brand exclusions, negative keywords, search-terms reporting and channel reporting, and Advantage+ existing-customer caps. Pair them with practitioner reaction (Search Engine Land, PPC Land, r/PPC).
- **Practitioner and India voices:** exchange4media, afaqs, ET BrandEquity, Storyboard18 interviews with D2C, BFSI and telecom CRM leaders; Reddit r/marketing and r/PPC; LinkedIn posts. Label all as anecdotal.
- Validity State of CRM Data 2026: secondary reports say two in three organisations increased decisions delegated to agents, and 92% of SVP/VPs acted on a bad AI recommendation. Not verified.
- Any survey on accountability ("who is responsible") for agent errors in marketing.
- India-specific marketing-buyer autonomy or willingness-to-pay data (NASSCOM, KPMG India, Meta-BCG, MoEngage or CleverTap buyer surveys).
- Forrester's "B2C Marketer's Guide to Agentic AI" (paywalled).
- Current Agentforce, HubSpot and Braze agent pricing pages (I relied on memory here).
