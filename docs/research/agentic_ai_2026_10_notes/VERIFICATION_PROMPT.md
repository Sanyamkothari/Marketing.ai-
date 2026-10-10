# Prompt for a research agent with full web access

Copy everything below the line into an agent that can open web pages and PDFs, for example Claude.ai with web search, Gemini Deep Research or Perplexity Pro. Attach the report "Agentic AI opportunities for Marketing AI.md" if you can.

---

You are a careful research analyst. Your job is to **verify and deepen** a research report about agentic AI and new machine-learning opportunities for a B2B software product called **Marketing AI**. The first round of research could only read search-engine snippets, never the source pages. Your value is to open the **primary sources** (official press releases, regulations, papers, vendor documentation) and confirm, correct or reject each claim. Then fill the listed gaps.

## The product (context, do not research)

Marketing AI decides which customers of Indian consumer brands to contact, with which offer and on which channel. The brands are in retail, e-commerce, quick-commerce, D2C, fintech, insurance and telecom; the channels are SMS, WhatsApp, e-mail, push and calls. It:

- predicts churn and customer value;
- uses causal "uplift" models to target the customers a campaign would actually change;
- keeps randomised hold-out groups that are not contacted;
- measures incremental results in rupees, with confidence intervals and an evidence label ("Causal / Estimated / Not proven");
- hands the contact lists to CleverTap, MoEngage or Netcore;
- keeps a per-channel consent ledger and DLT template ids.

Its thesis: most vendors build AI agents that act, but few prove that the action caused a result. The next plan ("Plan K") considers:

1. a governed campaign agent that plans, acts with human approval, proves the result and learns;
2. a "trust layer" (an MCP server) that other companies' marketing agents call to ask "is this worth doing?" and "did it work?";
3. faster, smaller experiments;
4. AI agents for data preparation and consent/DLT compliance.

## Output format

For **every claim** below, give:
- a verdict: **Confirmed / Corrected / Not found / Contradicted**;
- the exact figure or wording from the primary source;
- the source title, publisher, date and URL;
- for surveys: who was surveyed, how many, and when;
- a one-line note if the claim's wording overstates what the source says.

Then answer each gap question with sources. End with a list of **new facts that would change the recommendations**.

Rules:
- Prefer primary sources. Secondary write-ups only if the primary is paywalled, and say so.
- Don't round or reword statistics.
- If two sources conflict, report both.
- Say "not found" rather than guess.
- Today's date is October 2026. Flag anything outdated.

## A. High-stakes claims to verify (used in front of buyers and management)

### Buyer distrust and proof of value
1. Gartner press release, 29 Oct 2025: a survey of 413 martech leaders (June–Aug 2025) found that **45%** of those with AI agents in pilot or production say vendor-offered agents fall short of promised business performance. Also: 81% are piloting or deploying agents, and about half say their stack or data is not ready.
2. Gartner, 2024 (378 senior marketers): only **52%** can prove marketing's value and get credit; **40%** name the CFO as the most sceptical executive.
3. Gartner, June 2025: **over 40%** of agentic AI projects will be cancelled by end-2027, and only about 130 "agentic" vendors are real ("agent-washing").
4. Gartner: "guardian agents" will take 10–15% of the agentic AI market by 2030. Also the Gartner Market Guide for Guardian Agents (Feb 2026): 80% of unauthorised agent transactions will come from internal policy violations through 2028.
5. Gartner: 63% of organisations lack, or are unsure they have, AI-ready data practices.
6. MIT NANDA "GenAI Divide" (July 2025): the "95% of generative-AI pilots fail" claim. What is the actual sample and method?
7. TransUnion/EMARKETER (July 2025, 196 US marketers): 67.4% say proving incremental ROI is the top challenge. Is "52% use incrementality testing" in the original?
8. BCG, 30 Sep 2025: about 5% of companies get substantial value from AI. BCG 2026: 53% of Indian CMOs expect 5–9% revenue uplift from AI.
9. Dentsu: 46% of Indian CMOs are uncomfortable delegating high-stakes tasks to agents, versus 63% globally.
10. Salesforce State of Marketing, 10th edition, India figures (81% have adopted AI) and the India sample size.
11. IBM CMO Study 2025, India: 26% feel talent-ready and 23% have prepared teams for AI agents.
12. Optimove 2025: 70% of consumers unsubscribed from three or more brands in three months. Give the sample size.

### Competitors
13. MoEngage acquired Aampe (24 June 2026; reported as "tens of millions"). MoEngage raised $100M led by Goldman Sachs (Nov 2025).
14. Netcore rebranded as Netcore.ai (30 July 2026), with seven agents and outcome-based "shared accountability" pricing. What exactly is the fee tied to?
15. Braze/OfferFit (BrazeAI Decisioning Studio): is a randomised control against "business as usual" the default? Are the "+92% conversion" and "$16M annualised" figures published with any method? Is "$250–300k per use case" real?
16. Optimove: control groups in every self-optimising campaign, shrunk but never to zero.
17. Aampe preprint arXiv 2604.08621: an 8.8M-user, 11-month study with a 90/10 agentic vs business-as-usual hold-out. Is the "65.3% lift" figure in the paper, and what does it measure?
18. CleverTap "CleverAI" agents. Is there a product called "Scout"? Its +66% claim and its method.
19. Insider: Sirius AI renamed "Agent One" (Mar 2025); Insider acquired Bluecore (May 2026)?
20. Pricing: Salesforce Agentforce Flex Credits ($0.10 per action); HubSpot credits from 14 Apr 2026 ($0.50 per resolved conversation, $1 per recommended lead).
21. Gartner 2026 Magic Quadrant for Personalization Engines: CleverTap a Leader, MoEngage a Visionary?

### Methods (open the papers)
22. Etsy: CUPED 7% vs CUPAC-style 27% variance reduction. Meta MLRATE: over 70% variance reduction. DoorDash CUPAC: more than 25% shorter tests.
23. Statsig: A/A false-positive rate 1.1% with mSPRT vs 17.3% with naive peeking.
24. Netflix surrogate index: 200 A/B tests, 14-day data matched 63-day decisions about 95% of the time.
25. Booking.com: ranking by incremental profit (arXiv 2405.02183) and knapsack allocation under ROI constraints (RecSys 2020).
26. "Let AI Entertain You": LLM-generated text gave about +1% sessions with reward-model reranking, and −6.5% unfiltered. Also the Mercari randomised e-mail title test (about 1M lapsed users): what were the effect sizes?

### Technology
27. AutoGluon 1.4 / 1.5 / 1.6 release notes: which tabular foundation models are included, and in which presets? Licence of TabPFN-2.5, 2.6 and 3 weights (non-commercial?), TabPFNv2 (commercial with attribution?), Mitra (Apache-2.0)? Prior Labs commercial API terms?
28. TabPFN-3 (May 2026): limits (1M rows × 200 features?) and independent benchmarks on TabArena.
29. MCP spec 2026-07-28: the stateless transport, Tasks, the authorization model. MCP donated to the Agentic AI Foundation (9 Dec 2025). A2A v1.0 (12 Mar 2026) and its move into the AAIF (Aug 2026)?
30. Official MCP servers: MoEngage (read-only, 9 tools?), Braze (beta, read-only?), Netcore, HubSpot, Klaviyo, Salesforce (GA date?). Is there an official CleverTap MCP?
31. MCPTox benchmark: 36.5% average tool-poisoning success rate.
32. tau-bench and CRMArena-Pro reliability numbers. Give current (2026) results for the latest models.

### India regulation (open the Gazette, MeitY, TRAI and PIB pages)
33. DPDP Rules 2025, notified 13 Nov 2025 (G.S.R. 846(E)): exact commencement dates for Rule 4 (consent managers, 12 months?) and for the notice, breach, erasure, children's and rights duties (18 months?). Has the timeline been shortened? MeitY's 22 Jan 2026 proposal.
34. Consent manager eligibility: Indian company, ₹2 crore net worth, platform certification. Has registration opened? Any registered consent managers?
35. Penalty schedule amounts. Is there a legitimate-interest basis for marketing (none)? What does section 7, "certain legitimate uses", cover?
36. TRAI TCCCPR amendments: Feb 2025 (penalty ladder), the 18 Nov 2025 direction on template variables and URL/APK/callback whitelisting, and the Third Amendment Regulations 2026 (notified 18 Sept 2026?). What do they actually say about AI/ML spam detection and automated calls?
37. The DCA (digital consent acquisition) registry: status in 2026, the 127xxx short code, and the TRAI–RBI legacy-consent pilot (Dec 2025).
38. India AI Governance Guidelines (5 Nov 2025): what they say about personal data used for AI training.

## B. Gaps to fill (new research)

1. **Hold-out groups under DPDP and TRAI.** Is deliberately not contacting a randomised group of consenting customers, and using their data to measure, a lawful "specified purpose" under consent? Any guidance or law-firm view on A/B testing and experimentation under DPDP?
2. **Campaigns without control groups.** Any survey on the share of CRM/lifecycle campaigns run without a hold-out, or on under-powered tests?
3. **Discount waste, 2023–2026.** Share of promotions or discounts going to customers who would have bought anyway, with the source.
4. **India market size.** Indian martech, CDP and customer-engagement market size and growth, 2025–2026. Revenue of CleverTap, MoEngage, Netcore and WebEngage. Telecom churn rates (prepaid monthly churn).
5. **Telecom retention incumbents.** Amdocs, Pega, SAS, Evolving Systems and Flytxt: their agentic / next-best-action features and how they prove value. Who serves Jio, Airtel and Vi?
6. **Neutral verification.** Is any company positioned as an independent judge of AI marketing agents' decisions (causal proof, not just policy compliance)? Check Haus, Measured, INCRMNTAL, Recast, Lifesight and Mutinex, and whether any have agent or MCP offerings.
7. **Uplift at small effect sizes.** Evidence (papers or benchmarks, 2023–2026) on when uplift models beat simple response or risk ranking in CRM retention, especially at effects of 0.5–3 percentage points. Benchmarks on Hillstrom, Criteo, X5 and Lenta.
8. **Tabular foundation models for churn.** Any benchmark of TabPFN, TabICL or Mitra against gradient boosting or AutoGluon on churn or CRM data. Any causal PFN tested on marketing uplift data.
9. **Synthetic personas.** Validation of LLM "synthetic customers" on Indian consumers or Hindi/Hinglish content.
10. **Outcome-based pricing.** Appetite for pricing tied to proven incremental revenue in marketing software, especially in India.
11. **Agent frameworks in production on AWS.** Maturity of Bedrock AgentCore (Policy, Evaluations), the Claude Agent SDK on Bedrock, LangGraph and the OpenAI Agents SDK, with real production case studies.

## C. Deliver

A Markdown document with sections A (a verification table) and B (gap answers), and a final section headed **"What changes the recommendation"**. Keep it factual and sourced.
