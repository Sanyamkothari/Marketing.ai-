# 06: Gap fill for the autonomy study (11 Oct 2026)

**Scope.** This pass fills the six gaps the coordinator named, in priority order:
1. ad-platform control history;
2. Indian practitioner voices;
3. how Braze, MoEngage and Aampe measure results;
4. agent pricing;
5. incidents;
6. Indian regulation checks.

It ends with a corrections table for 01–05.

**Method.**
- **Searches:** 118 WebSearch calls, out of a 170 cap. Three more calls were rejected because their domain filters named sites the search tool cannot reach (economictimes, livemint, financialexpress).
- **WebFetch** failed on DNS, tested once.
- **Direct downloads:** curl to the vendor, regulator and law-firm PDFs returned no connection (code 000) through the proxy.
- **Primary sources opened (2 GitHub clones):**
  - `googleapis/googleapis`, the Google Ads API v25 protos, commit 525d4c8, 9 Oct 2026;
  - `facebook/facebook-python-business-sdk`, Marketing API v26.0, commit efd8423, 17 Sep 2026.
- **Unreachable:** `braze-inc/braze-docs` (404/403).
- Both clones are deleted. What was read is listed in `src2_index.txt`.

**Tags.**
- **[O]**: I opened the primary source.
- **[S]**: search-result summary only. The page was not opened, and the wording comes from the search engine's summary.
- **[M]**: from memory, unverified.

**Confidence.** H, M or L.

**Anecdote label.** Practitioner quotes are anecdotal by nature. They are labelled **(anecdotal)**.

---

## 1. How advertisers won control back from Google PMax / AI Max and Meta Advantage+

### 1.1 Google: dated timeline

| Date | What changed (control / report / test) | Kind | Source | Tag | Conf. |
|---|---|---|---|---|---|
| Nov 2021 (launch); Smart Shopping and Local auto-upgraded 2022 | PMax launches as Smart Shopping's successor. Advertisers lose granular control. Google cites a "12% lift in conversion value at same or better ROAS" from upgraded campaigns (vendor claim). An agency head: "combining lack of transparency... while reducing... granular control" | baseline | [MediaPost: transition deadlines](https://www.mediapost.com/publications/article/370680/google-sets-deadlines-for-transition-to-performanc.html); [Deyna Lavery](https://deynalavery.substack.com/p/whats-next-for-performance-max) | [S]; exact launch month [M] | M |
| 26 Apr 2022 | **New-customer acquisition goal** in PMax: bid more for new customers, or "new customers only". Previously only in Smart Shopping | fence/goal | [Google support 12117037](https://support.google.com/google-ads/answer/12117037); [PPC Land](https://ppc.land/google-to-introduce-a-new-goal-in-performance-max-campaigns-focus-on-new-customers) | [S] | M |
| Jan 2023 (month; no exact day) | **Account-level negative keywords**, applying to Search and Shopping inventory including PMax | fence | [WordStream, 6 Feb 2023](https://www.wordstream.com/blog/ws/2023/02/06/account-level-negative-keywords); [Google: What's new in PMax](https://support.google.com/google-ads/answer/13311048) | [S] | M |
| Feb 2023 | **PMax search term insights** report. Early versions showed category labels rather than queries, and **no cost data** | receipt | [Search Engine Land](https://searchengineland.com/analyze-performance-max-search-terms-insights-438333) | [S] | M |
| Feb 2023 beta → **29 Jun 2023 GA** | **Brand exclusions** for PMax and Search (own brand, misspellings, competitors). Framed as building on account-level negatives | fence | [Google support 13729701](https://support.google.com/google-ads/answer/13729701?hl=en); [SEJ](https://www.searchenginejournal.com/google-ads-update-new-brand-controls-for-search-performance-max-campaigns/490698/) | [S] | H |
| Sep 2024 (DMEXCO) → beta Dec 2024 / Jan 2025 → limit 100 raised to **10,000 on ~11 Mar 2025** | **Campaign-level negative keywords** in PMax. Ads Liaison Ginny Marvin confirmed the staged rollout | fence | [MediaPost](https://www.mediapost.com/publications/article/399526/google-adds-negative-keywords-to-pmax-campaigns.html); [SEL beta](https://searchengineland.com/google-pmax-negative-keyword-exclusions-beta-449315); [SEJ 10,000 limit](https://www.searchenginejournal.com/google-pmax-negative-keyword-limit-increase/542336/); [Magelon](https://magelon.com/introduction-to-performance-max-updates-2026/) | [S]; DMEXCO month [M] | M |
| 23 Jan 2025 | "Kick off 2025" release: <br>• **high-value new-customer mode** to all; <br>• **brand exclusions by format** for retailers; <br>• "URL contains" rules; <br>• search-themes usefulness and a **source column** in search-term insights; <br>• **segmentable, downloadable asset-group reporting** | fence + receipt | [Google blog](https://blog.google/products/ads-commerce/new-performance-max-features-2025/); [Karooya Jan 2025](https://www.karooya.com/blog/monthly-roundup-top-ppc-news-january-2025) | [S] | H |
| **30 Apr 2025** (announced); open beta **30 May 2025**; search partners added **6 Nov 2025** | **Channel performance reporting**, **full search-terms reporting** and asset metrics for PMax | receipt | [Google blog: channel reporting](https://blog.google/products/ads-commerce/channel-performance-reporting-coming-to-performance-max/); [PPC Land](https://ppc.land/google-unveils-major-transparency-boost-for-performance-max-campaigns/); [PPC Land Waze/partners](https://ppc.land/google-adds-waze-inventory-to-performance-max-and-expands-channel-reporting/) | [S] | H |
| 6 May 2025 | **AI Max for Search** announced, with a claim of "14% more conversions". It ships with **brand inclusions/exclusions, URL exclusions, locations of interest and search-term insights across match types**. Independent tests are "meh" | automation + fences | [PPC Land](https://ppc.land/googles-ai-max-for-search-campaigns-deliver-meh-results-industry-tests-reveal/); [Google support 15909989](https://support.google.com/google-ads/answer/15909989?hl=en) | [S] | M |
| **22 May 2025** (GML) | **Incrementality tests from about $5,000**, previously about $100,000, using Bayesian methods. Self-serve Conversion Lift for Search and PMax needs 1,000 conversions | test | [PPC Land](https://ppc.land/google-lowers-incrementality-testing-threshold-to-5-000-for-advertisers/); [SEL](https://searchengineland.com/google-opens-conversion-lift-to-search-and-performance-max-494195) | [S] | M |
| 7 Aug 2025 (help page) | "Unlock more visibility and control": <br>• campaign-level **negative keyword lists** (5,000 per list, 20 lists); <br>• **device and age** controls; <br>• channel report | fence | [Google support 16451273](https://support.google.com/google-ads/answer/16451273); [Jumpfly](https://www.jumpfly.com/blog/visibility-in-performance-max-search-terms-negative-keywords/) | [S] | M |
| Jan 2026 | **Account-level placement exclusions** across Display, YouTube, PMax and Demand Gen. Described as a response to advertiser demand for guardrails. The PMax placement view still shows **impressions only** | fence + receipt | [SEL](https://searchengineland.com/google-ads-rolls-out-account-level-placement-exclusions-467569); [PPC Land](https://ppc.land/google-gives-advertisers-one-placement-list-to-rule-them-all/); API `performance_max_placement_view.proto` ("impression metrics") | [S] + [O] | M |
| Apr 2026 | **Ads Advisor** agent: "As always, Ads Advisor will ask for approval before taking action in your account". SEJ separately reports an "Agentic Expert" making changes without asking (conflict) | approval gate | [Google blog: Ads Advisor](https://blog.google/products/ads-commerce/ads-advisor-google-ads/); [SEJ](https://www.searchenginejournal.com/googles-new-ai-tools-promise-faster-ads-but-raise-control-concerns/547753/) | [S] | M |
| 20 May 2026 (GML) | **Ask Advisor**: one agent across Ads, Analytics, GMP and Merchant Center. Changes are made "with approval"; the detail is undocumented | agent | [PPC Land](https://ppc.land/googles-ask-advisor-unifies-ads-analytics-and-commerce-in-one-ai-agent/) | [S] | L |
| **11 Jun 2026** | **DSA→AI Max auto-migration postponed to Feb 2027** "after advertiser feedback" (Q4 risk). DSA creation restored on 15 Jun. Automatically created assets and campaign-level broad match **still migrate 1–30 Sep 2026**. There is **no formal opt-out** other than disabling the features beforehand | forced automation, partly delayed | [PPC Land](https://ppc.land/google-delays-dsa-to-ai-max-automigration-to-february-2027/); [SEL](https://searchengineland.com/google-delays-dynamic-search-ads-migration-to-ai-max-480049); [TechWyse](https://www.techwyse.com/news/platform-updates/google-ads-ai-max-migration-timeline-search-campaigns) | [S] | M |
| Jul 2026 | **Pilot checkboxes to exclude Display Network and search-partner inventory from PMax.** Both are on by default, limited to a small group, and Google gave no general-availability date (Digiday via trade press) | fence (pilot) | [PPC Land](https://ppc.land/google-lets-some-pmax-advertisers-switch-off-search-partners-and-display/); [Shopifreaks](https://www.shopifreaks.com/google-pilots-performance-max-checkboxes-that-let-advertisers-exclude-display-network-and-third-party-search-partner-inventory/) | [S] | M |
| API v25 (HEAD 9 Oct 2026) | **Text guidelines** for generated copy: <br>• `term_exclusions` (≤25, 30 chars each); <br>• `messaging_restrictions`: free-text LLM instructions (≤40, 300 chars each), only "RESTRICTION_BASED_EXCLUSION". <br>**Brand lists and text automation are "bundled" with AI Max** (`AiMaxBundlingRequired.REQUIRED`). <br>**Brand Guidelines cannot be disabled once enabled.** <br>New `LiftMeasurementConfig` with `conversion_lift_holdback_ratio_micros`, absent in v23 and v24 | fences + test | `googleapis/googleapis` `google/ads/googleads/v25/resources/campaign.proto`, `lift_measurement_config.proto` | **[O]** | H |

### 1.2 Meta: dated timeline (the opposite direction)

| Date | What changed | Kind | Source | Tag | Conf. |
|---|---|---|---|---|---|
| Aug 2022 | **Advantage+ Shopping (ASC)** launched with an **existing-customer budget cap** (often set at 10–30%) | fence at launch | [Adwisely](https://adwisely.com/glossary/advantage-plus-shopping/); [Jon Loomer](https://www.jonloomer.com/advantage-plus-sales-app-leads-campaigns/) | launch month [M]; cap [S] | M |
| Mar 2023; Oct 2023 | **Inventory filters** (limited/moderate/expanded). **Third-party block lists** (IAS from Oct 2023, then DoubleVerify and Zefr) | fence (brand safety) | [The Drum](https://www.thedrum.com/news/2023/03/30/meta-leverages-ai-boost-brand-suitability-within-instagram-and-facebook); [Mediaweek](https://mediaweek.com.au/news/meta-enhances-brand-suitability-controls-with-new-third-party-content-block-lists) | [S] | M |
| ~2023–24 (undated) | **Audience-segment reporting**: new, existing and engaged customers, using advertiser-defined custom audiences | receipt | [SEL](https://searchengineland.com/meta-audience-targeting-insights-update-438947); [Jon Loomer](https://www.jonloomer.com/qvt/engaged-customers-audience-segment/) | [S] | M |
| 2024–25 rollout | **Incremental attribution** setting (optimise for "conversions that would not have happened"). A "46% lift vs BAU" test is Meta-sourced via third parties | test/goal | [Logical Position](https://www.logicalposition.com/blog/beyond-last-click-breaking-down-metas-incremental-attribution); [Booleanmaths](https://booleanmaths.com/blogs/understanding-meta-s-new-incremental-attribution) | [S] | M |
| May 2024–2025 (sources differ) | **Existing-customer cap removed from the UI** when ASC became **Advantage+ Sales**. The workaround is two ad sets. The API field `existing_customer_budget_percentage` **still exists on AdSet in SDK v26.0** | fence removed | [Madgicx](https://madgicx.com/blog/advantage-shopping-campaigns-farewell-customer-budget-cap); [Dollar Commerce](https://dollarcommerce.substack.com/p/why-did-facebook-remove-the-existing); SDK `adobjects/adset.py` | [S] + **[O]** | M |
| 2025 | In Advantage+ Audience, only **location, minimum age, excluded custom audiences and language** are hard "controls". Everything else is a "suggestion" | fence narrowed | [Jon Loomer](https://www.jonloomer.com/advantage-plus-campaign-setup-targeting/) | [S] | M |
| **8 Oct 2025** (API, opt-in); about 16 Oct 2025 **default-on in Ads Manager** for sales and leads | **"Limited spend" on excluded placements**: up to **5% per excluded placement** (`placement_soft_opt_out` in the SDK) | fence softened | [Meta developer blog](https://developers.facebook.com/blog/post/2025/10/08/introducing-limited-spend-on-excluded-placements-in-marketing-api/); [PPC Land](https://ppc.land/meta-pushes-spending-5-on-excluded-placements-for-sales-and-leads-campaigns/); SDK | [S] + **[O]** | H |
| 15 Jan 2026 | Dozens of detailed-interest categories removed. Catalog ads lose "Audience Types" | fence removed | [SEL](https://searchengineland.com/meta-streamlines-advantage-catalog-ad-targeting-451358); [Conversios](https://www.conversios.io/blog/meta-advantage-audience-vs-detailed-targeting-2026-guide/) | [S] | M |
| Feb–Mar 2026 | Creative enhancements are **pre-selected** on new Sales, Leads and App campaigns. Reportedly, **opt-outs persist from Mar 2026** (unconfirmed). The SDK lists **84 creative-enhancement features, each with its own `enroll_status`** | default-on; granular opt-out | [Jumpfly](https://www.jumpfly.com/blog/metas-advantage-settings-why-they-keep-reappearing-and-how-to-stay-in-control/); SDK `adcreativefeaturesspec.py` | [S] + **[O]** | M (persistence: L) |

**Also:** Microsoft Advertising added PMax new-customer goals in open beta on 15 Jan 2026 — [SEL](https://searchengineland.com/microsoft-adds-new-customer-acquisition-goals-and-deeper-visibility-to-pmax-467555) [S].

### 1.3 What the history shows

1. **Google conceded controls slowly, as fences, receipts and tests. It never gave per-decision approval.**
   - About 15–19 months passed between PMax launch and the first fences (account negatives in Jan 2023, brand exclusions in Jun 2023).
   - About 3.5 years passed before full search-term and channel reporting (Apr–May 2025).
   - About 4.7 years passed before a Display opt-out pilot (Jul 2026).
   - Each concession bounds *where* the machine may act, or shows *what it did*. None asks a human to approve individual bids or placements. [O/S, H]
2. **The concessions came under sustained, public advertiser pressure.**
   - Google says so in two places:
     - the DSA delay ("advertiser feedback");
     - account-level placement exclusions, framed as answering requests for "stronger guardrails".
   - Trade press records practitioners' continuing distrust. SEL describes "being asked to trust a system that offered fewer levers" — [SEL 2025](https://searchengineland.com/google-meta-microsoft-ads-trust-456190) [S].
   - **No percentage survey** of PMax trust was found. Optmyzr's 2024 survey of more than 1,000 advertisers names "lack of control and transparency" but gives no figure in the summary — [Optmyzr](https://www.optmyzr.com/ppctownhall/ppc-survey-2024-insights-benchmarks-and-trends-in-ppc-ai-pmax-and-more) [S, M].
3. **Controls were also used as the price of more automation.**
   - In API v25, brand-list targeting and text automation are "bundled with AI Max" (REQUIRED) [O, H].
   - Brand Guidelines cannot be turned off once enabled [O, H].
   - Auto-migration has no formal opt-out [S, M].
4. **Meta moved the other way.**
   - It removed or softened fences: the existing-customer cap, interest targeting, and strict placement exclusions (the 5% "limited spend").
   - It made AI creative default-on.
   - In exchange it added *receipts and tests*: audience-segment reporting, incremental attribution, and granular per-feature opt-outs.
   - **"Staged autonomy won by demanding controls" holds for Google, not for Meta.** Meta's lever was measurement. Independent tests followed: Haus found Advantage+ 12% worse than manual by the end of tests (01 §2.1).
5. **For our product:** the durable, negotiated controls fall into four types. Build all four in from day one rather than waiting to be asked:
   - (a) exclusion lists: never-contact cohorts, brand and claim exclusions, term exclusions;
   - (b) goal fences: new-versus-existing customer goals, caps;
   - (c) per-decision receipts: what was sent to whom, where, at what cost;
   - (d) cheap self-serve holdout tests.

   The text-guidelines pattern (25 hard term exclusions plus 40 free-text "restriction" instructions to the LLM) is a ready template for the claims and content guardrails in 04 §7. **[inference]**

---

## 2. Indian practitioner voices on AI agents in CRM and retention (all anecdotal)

**Coverage.** ET BrandEquity, Mint and Financial Express could not be searched; the tool cannot reach them. Everything below is [S]. The quotes are in the search summary's words.

| Date | Who / where | What they said | Sector | Source | Conf. |
|---|---|---|---|---|---|
| Sep 2026 (Pitch BFSI Summit) | **Girish Kalra, CMO, Tata AIA Life** | Personalised homepages for about 4M existing customers. Consent must be **granular across channels**: agreeing to chat with an AI agent ≠ opting into promotions. An **opt-out must propagate instantly across every channel**. The panel expects more autonomy "within pre-set thresholds", with humans "recalibrating the system" rather than checking each output | BFSI | [e4m: Future of BFSI](https://www.exchange4media.com/marketing-news/the-future-of-bfsi-intelligent-trust-158555.html); [e4m: Happening today](https://www.exchange4media.com/marketing-news/happening-today-trust-ai-customer-attention-in-focus-at-pitch-bfsi-summit-2026-158473.html) | M (anecdotal) |
| 24 Sep 2026 | Pitch BFSI panel write-up | Do not adopt AI for trend's sake. Outreach should be non-intrusive and consent-led. **Marketing must own AI-generated content, with human checks for accuracy and compliance** | BFSI | [e4m: where to draw the line](https://www.exchange4media.com/marketing-news/as-bfsi-embraces-ai-marketers-spell-out-where-to-draw-the-line-158527.html) | M (anecdotal) |
| 15 Sep 2026 (e4m D2C Summit) | **Vedansh Kumar (CMO, boAt)**, Mivi, Qubo, SleepyCat, Fireside | AI is moving into core D2C decisions. Panellists **caution against over-reliance** and stress keeping human empathy. Mivi's CEO says the team shrank about 40% while the business kept growing | D2C | [e4m: D2C brands take AI beyond marketing](https://www.exchange4media.com/marketing-news/d2c-brands-take-ai-beyond-the-marketing-function-158258.html) | M (anecdotal) |
| 2025–26 (undated) | **Girish Bindal, CMO, AdvantageClub.ai**; **Vinay Tamboli, CEO, DataQuark** | Bindal: WhatsApp CRM agents and "hyper-personalised nudges" are pragmatic, but **retention and revenue still need human oversight**. A colleague flags DPDP, cultural and reputational risk. Tamboli: D2C firms are "in survival mode"; **12–24 months before agentic AI is mainstream**; customer signals are fragmented across marketplaces, own sites and offline | D2C | [e4m: From D2C to D2AI](https://www.exchange4media.com/digital-news/from-d2c-to-d2ai-will-agentic-ai-help-indias-brands-outrun-the-world-145141.html) | M (anecdotal) |
| 2026 (May dateline; year unconfirmed) and 2026 case study | **Niranjan Sane, AVP Growth, Swiggy** (vendor PR); Swiggy growth lead (case study) | "Make every customer interaction feel relevant, timely, and valuable". Content AI agents generate push and in-app messages, and Swiggy plans to "explore Merlin AI Decisioning". The case study: marketers "used to schedule campaigns, and now they manage agents". In-app CTR went from 10% to 21%, go-live is 3× faster, and Swiggy runs 6,000+ campaigns a month. **No holdout design** is stated | Quick commerce / food | [Restaurant India](https://www.restaurantindia.in/news/restaurant-india-news-swiggy-partners-with-moengage-to-expand-ai-led-customer-engagement.n16032); [MoEngage case study](https://www.moengage.com/casestudy/swiggy-scales-campaigns-with-moengage-and-aampe/) | M that it was said; L as evidence |
| 19 Aug 2026 (Inc42 D2C & Retail Summit) | Gupshup executive; Harsh Somaiya (The Bear House); Meesho | AI repeat-purchase flows with **human escalation** across WhatsApp and RCS. Vendor-credited claims: "5× conversions, −20% returns" (Glance agent) and Meesho Vaani +22% conversion. Agentic commerce is slower in India because of trust gaps | D2C / commerce | [Inc42 summit agenda](https://inc42.com/buzz/the-d2c-retail-summit-2026-agenda-is-live-decoding-indias-new-commerce-playbooks/); [Inc42 D2C 3.0](https://inc42.com/features/d2c-3-0-the-new-rules-of-indias-d2c-economy/) | L (vendor claims) |
| Jul 2026 | **Rajesh Jain, Netcore.ai** | Rebranded as an agentic platform with **seven agents**, plus "growth engineers" who are "co-accountable" for client outcomes. Named clients (May 2026 PR): Shriram Finance, Navia Markets, Crocs India. No metrics | Martech vendor | [Tribune/PR](https://www.tribuneindia.com/news/business/netcore-is-now-netcore-ai-becoming-the-first-agentic-marketing-platform-to-share-accountability-for-customer-growth/); [afaqs](https://www.afaqs.com/news/martech/netcore-bets-on-agentic-ai-to-make-marketing-autonomous-heres-how-10649082) | M |
| 2026 | **Raviteja Dodda, CEO, MoEngage** | "Where do agents actually belong in my stack, and where do I need to maintain control?" — the framing for the "North" tour. Growth comes from enterprise migrations off Salesforce and Adobe | Martech vendor | [Agile Brand Guide](https://agilebrandguide.com/moengage-launches-north-agentic-marketing-tour-bringing-marketers-face-to-face-with-brands-deploying-ai-agents-in-production/); [TechCrunch 23 Jun 2026](https://techcrunch.com/2026/06/23/indias-moengage-bets-marketings-future-on-millions-of-ai-agents/) | M |
| 2026 | **CleverTap, CleverAI Agents** product page | "Agents can propose and configure segments, journeys, copy, or creatives; **humans review, approve, and launch**". Existing approval flows and rollback stay in place | Martech vendor | [CleverTap](https://clevertap.com/ai/cleverai-agents/) | M |
| 6 Oct 2026 | Dentsu Creative CMO report (India cut, via trade press) | **63% of CMOs globally, but 46% in India**, are uncomfortable delegating high-stakes tasks to agentic AI. On privacy and security, discomfort is 65% globally versus 47% in India. 94% of Indian CMOs expect AI-enabled agency workflows. Delegation is preferred for execution tasks ("dynamic optimisation") | Cross-sector survey | [MediaNews4U](https://medianews4u.com/94-of-indian-cmos-expect-ai-enabled-agency-workflows-to-become-the-norm-dentsu) | M (single trade source) |
| Feb 2026 | LocalCircles consumer survey | **96% of Indian WhatsApp users get unsolicited commercial messages daily**: 54% get 1–3 a day, 30% get 4–7, 11% get 8 or more. The most common sectors are financial services, real estate and health. Samples conflict (13,362 vs 42,000+) | Consumer voice | [Social Samosa](https://www.socialsamosa.com/news-2/users-india-receive-whatsapp-spam-messages-daily-11077812); [Swarajya](https://swarajyamag.com/amp/story/news-brief/whatsapp-profits-as-businesses-pay-to-spam-96-of-indian-users-receive-unwanted-messages-daily) | M |
| undated | **Soumya Mohanty, Kantar South Asia** | D2C brands lead AI adoption in marketing. AI testing is strong but "shouldn't replace all consumer research" | Research | [Storyboard18](https://cms.storyboard18.com/author/tasmayee-roy/page/5) | L |

**Telecom.**
- **No telecom CRM leader interview was found.**
- An earlier search summary attributed an Airtel "next best action, 135 million actions" figure to the Q4 FY26 call. **A second search could not confirm it**: 135 million was Airtel's 5G customer count in Q4 FY25. **Do not use it.**
- The Q3 FY26 call puts AI "at the centre of everything we do", including "persuasion and precision targeting" — [Communications Today](https://www.communicationstoday.co.in/management-commentary-bharti-airtels-q3-fy26-earnings-call/) [S, M].

**Pattern (inference, from anecdotes).** The Indian voices agree on four points:
- autonomy inside thresholds set by marketing;
- marketing owns the content;
- consent and opt-out handled across all channels;
- humans for revenue-critical retention decisions.

None describes a production system where an agent sends without a human-launched journey, except vendor claims (Netcore, Aampe at Swiggy).

---

## 3. Braze Decisioning Studio, MoEngage/Aampe and Hightouch: how they measure, guardrails, price

### 3.1 Measurement design: who is the "control"?

| Vendor | Default comparison | No-message holdout? | Metric | Source | Tag | Conf. |
|---|---|---|---|---|---|---|
| **Braze Decisioning Studio Go** (self-serve, e-mail, 2026) | A **random control group (at least 5%)** gets "random combinations of the same approved options". The FAQ answers "Is the random control a holdout group of users who receive no email? **No.**" Lift = agent group vs random control | Not by default | As configured | [Braze DS Go FAQ](https://braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_go/faq/) (5% from the [German page](https://www.braze.com/docs/de/user_guide/brazeai/decisioning_studio/decisioning_studio_go/faq)) | [S] | M |
| **Braze Decisioning Studio (Pro, ex-OfferFit)** | The design guide lists experiment groups "Decisioning Studio, Random Control, **BAU**, **Holdout**". The product page says uplift can be measured "against a random control, a holdout, or business-as-usual". Report uplift = (primary − comparison) / comparison. **The metric is agreed with Braze** (for example conversion rate or revenue per user), and the dashboard "visualises stored daily results" | Optional | Business metric recommended ("not proxy metrics like clicks or opens") | [Braze product page](https://www.braze.com/product/brazeai-decisioning-studio); [Performance report doc](https://braze.com/docs/user_guide/brazeai/decisioning_studio/reporting/performance); [Design agents doc](https://www.braze.com/docs/user_guide/brazeai/decisioning_studio/design_agents) | [S] | M |
| **MoEngage Merlin Decisioning** (policy/agent) | The "policy control group is a randomly selected group of users who receive **randomized messages** from the policy … baseline to measure AI incremental impact". One **reward event** per policy, which must be positive; a "Composite Reward Function"; a "Weightage Score" | Not by default | Reward event (often engagement) | [MoEngage docs: how it works](https://moengage.com/docs/user-guide/decisioning/campaign-decisioning/how-it-works); [Decisioning Agent Analytics](https://help.moengage.com/hc/en-us/articles/48312790537620-Decisioning-Agent-Analytics) | [S] | M |
| **Aampe** (MoEngage since 24 Jun 2026) | The knowledge base recommends **three groups: No-Message, Business-as-Usual, Aampe**. Group sizes are the customer's choice, and a "small Aampe group is inferior to a large one" because agents learn from each other. The blog prefers **switchback and synthetic controls (Coarsened Exact Matching)** to fixed holdouts for continuous systems | Yes, recommended | Engagement and conversion; GMV | [Aampe KB](https://kb.aampe.com/user-guide/how-do-i-know/experiments/holdout-part2); [Aampe blog](https://aampe.com/blog/how-do-i-know-aampe-is-actually-working); [Aampe synthetic controls](https://aampe.com/blog/validating-aampe-performance-with-adaptive-synthetic-control-groups) | [S] | M |
| **Aampe field evidence** | [arXiv 2604.08621](https://arxiv.org/pdf/2604.08621) (Jeunen, Hanna, Wheeler; 11 months, consumer app): **"active human management generates the highest relative lift"**; autonomous agents "sustained a positive lift" in the passive phase. A secondary write-up gives about 8.8M lapsed users split **90/10 agentic vs BAU**, with **+65.3% notification-driven app opens but only about +0.3% intent and conversion days** (not verified against the paper). [arXiv 2512.17462](https://www.arxiv.org/abs/2512.17462) (financial app): **unsubscribes −21% vs BAU**. [arXiv 2506.16429](https://arxiv.org/pdf/2506.16429): "double-digit" GMV improvements | vs BAU | Engagement ≫ conversion | [besthub write-up](https://www.besthub.dev/articles/what-an-8-8-m-user-experiment-shows-about-the-true-limits-of-agent-based-marketing-14bdd3611b21) | [S] | M (abstracts); L (figures) |
| **Hightouch AI Decisioning** | "Every decision is measured against a control/holdout group". Metrics and window are configurable (conversions, revenue, LTV or engagement) | Yes, stated | Configurable | [Hightouch blog](https://hightouch.com/blog/what-is-ai-decisioning-what-does-hightouch-do) | [S] | M |
| OfferFit legacy case studies | Liberty Latin America "+120% incremental ARPU impact"; Brinks Home "+450% contract-extension value". **Design not stated** | Unknown | Revenue | [casestudies.com](https://www.casestudies.com/company/offerfit); [VentureBeat](https://venturebeat.com/business/offerfit-gets-25m-to-kill-a-b-testing-for-marketing-with-machine-learning-personalization) | [S] | L |

**Finding (H for the pattern; M for each vendor).** The two mass-market CEP defaults (Braze Go and MoEngage) measure the agent against **random allocation among the same approved options**. That answers "is the model better than random?" It does not answer "**is messaging better than not messaging?**", nor "is the agent better than our current programme?" Adobe's decisioning "holdout" is the same (01 §2.3).
- Only Aampe's recommended design, Braze Pro's optional BAU and holdout arms, and Hightouch state a BAU or no-message comparison.
- The one longitudinal paper shows large engagement lift with near-zero conversion lift.

**This is exactly the gap a decide-and-prove product fills: a default three-arm design (no-message / BAU / agent) with a business metric.** [inference]

### 3.2 Guardrails customers set

- **Braze DS.** The agent has three design elements:
  - **dimensions**: the levers, such as offer, subject line, creative, channel and send time;
  - an **action bank**: "the agent can't go outside" it;
  - **constraints**: geographic eligibility, budget maximum and eligibility rules. Braze advises "keeping constraints minimal".
  - Guardrail updates appear as timeline events in reporting.
  - At Forge 2026, Go set-up covered allowed creative, **contact windows, frequency and holdouts**. **Agentic Standards** QAs each campaign before send.
  - Sources: [Design agents](https://www.braze.com/docs/user_guide/brazeai/decisioning_studio/design_agents); [Integration](https://braze.com/docs/user_guide/brazeai/decisioning_studio/integration); [CMSWire Forge recap](https://www.cmswire.com/customer-experience/braze-forge-2026-recap-self-service-speed-and-autonomy-require-governance/). [S, M]
- **MoEngage.** "While the AI is autonomous, you must provide supervision":
  - **target audience;**
  - **frequency caps**, plus a daily per-user minimum and maximum, with the workspace global cap still applying;
  - **allowed exploration limits**;
  - optional time slots;
  - **agent control groups**.
  - Sources: [MoEngage docs](https://moengage.com/docs/user-guide/decisioning/campaign-decisioning/how-it-works); [MoEngage blog](https://www.moengage.com/blog/agentic-ai-in-marketing/). [S, M]
- **Google (for comparison, [O]).** Term exclusions and LLM "messaging restrictions" for generated copy; brand lists; Brand Guidelines (colours and fonts).

### 3.3 Pricing signals

- **Braze**
  - **No list price.** Since FY27 Q1 (27 May 2026), "**Action Credits** [are] the only way Braze sells".
  - Agent Console draws on flexible credits.
  - The CFO said in Jan 2026 that AI Operator is not charged separately.
  - Sources: [Vectorshift Q1 FY27 summary](https://vectorshift.ai/research/companies/braze/earnings/2027-q1-summary); [MarketBeat Needham, 14 Jan 2026](https://www.marketbeat.com/instant-alerts/braze-highlights-chatgpt-sdk-agent-console-and-ai-personalization-push-at-needham-conference-2026-01-14/?.tsrc=rss). [S, M]
  - **The "$250–300k per use case" price for full Decisioning Studio** appeared in one search summary of the MarketBeat Needham article. A second search could not find it in the retrieved text. *L.*
- **MoEngage**
  - Quote-based, by MAU and channels. "Enterprise + Aampe Agentic AI" is contact-sales.
  - **MoEngage AI Credits** meter Merlin. Agent runs are metered by "model processing duration, the infrastructure the run uses, and any web searches". Regenerations consume credits again.
  - Sources: [MoEngage docs: Merlin AI usage](https://moengage.com/docs/user-guide/settings/account/billing/merlin-ai-usage); [saasrat listing](https://saasrat.com/products/moengage). [S, M]
- **Aampe and OfferFit standalone:** no prices found. [S]

---

## 4. Pricing of autonomous (marketing and service) agents, 2025–26

| Vendor / product | Model | Price point (date) | Source | Tag | Conf. |
|---|---|---|---|---|---|
| Salesforce Agentforce | per conversation → per action → per user → **per resolution** | <br>• $2 per conversation (2024). <br>• Flex Credits: $500 per 100k credits; 20 credits per standard action ≈ **$0.10/action**. <br>• Add-ons $125–150 per user per month. <br>• Agentforce 1 from $550 per user per month. <br>• **Help Agent: $2 per resolution, GA Jul 2026.** No charge if the customer asks for a human or leaves negative feedback | [eesel](https://www.eesel.ai/blog/agentforce-pricing); [Salesforce Ben](https://salesforceben.com/huge-agentforce-pricing-shift-salesforce-introduces-pay-per-resolution); [Futurum](https://futurumgroup.com/insights/salesforces-agentforce-help-agent-bets-on-pay-per-resolution-will-enterprises-trust-the-model/) | [S] | M |
| HubSpot Breeze agents | credits → **outcome** | <br>• $10 per 1,000 credits ($9 on annual). <br>• **Customer Agent: $0.50 per resolved conversation from 14 Apr 2026** (was $1 per conversation). It counts only if no human is pulled in within 72h. <br>• Prospecting Agent about $1 per recommended lead | [MarTech.org](https://martech.org/hubspot-moves-to-outcome-based-pricing-for-some-breeze-ai-agents/); [Resolve247](https://resolve247.ai/blog/hubspot-breeze-pricing-change) | [S] | M |
| Intercom Fin | **per outcome** | **$0.99 per outcome**, with a 50-outcome monthly minimum (Jul 2026). "Assumed resolutions" are billable | [Gleap](https://www.gleap.io/blog/intercom-fin-ai-pricing-2026) | [S] | M. A claimed Salesforce acquisition of Intercom (Jun 2026) from the same source family is **unverified (L)** |
| Zendesk AI | per automated resolution | About $1.50 committed / $2 PAYG; **not on the public price page** | [Macha](https://www.getmacha.com/blog/intercom-fin-vs-zendesk-ai-pricing) | [S] | L |
| Sierra / Decagon | enterprise contract | $95k–350k a year; "outcome-based" for Sierra **not confirmed** | [AI Agent Rank](https://aiagentrank.io/compare/intercom-fin-vs-sierra) | [S] | L |
| Braze AI | consumption (Action Credits) | No list | §3.3 | [S] | M |
| MoEngage Merlin / Aampe | consumption (AI Credits) plus enterprise quote | No list | §3.3 | [S] | M |
| Klaviyo Composer | credits → low flat fee | **$157 a month for 16k credits (Jul 2026) → $20 a month plus $10 top-ups (23 Sep 2026)** | [UsagePricing](https://www.usagepricing.com/blueprint/activity/klaviyo-2026-09-23-composer-credits-to-dollars) | [S] | L (single tracker) |
| Netcore.ai | "shared accountability", KPI-linked | **No fee terms published** (Jul 2026) | §2 | [S] | M that it is claimed |
| Persado | enterprise | From about $200k a year | [AI Agent Index](https://theaiagentindex.com/compare/klaviyo-ai-vs-persado) | [S] | L |
| Albert.ai | custom, based on ad spend | No rate | [Chatbase](https://www.chatbase.co/blog/ai-marketing-tools) | [S] | L |
| Jio Haptik (India, SMB WhatsApp agents) | per conversation bundle | **₹10,000 for 2,000 conversations (₹3–5 each)** | [Analytics India Magazine](https://analyticsindiamag.com/ai-news-updates/reliances-jio-haptik-rolls-out-ai-agents-for-small-businesses-at-%e2%82%b910000) | [S] | M |
| Market benchmarks | outcome | Per-resolution prices fall in a $0.50–2.00 band; AI SDRs charge $50–200 per meeting | [aissist.io](https://aissist.io/industries/ai-agent-pricing-benchmark-2026); [AI Agent Rank](https://aiagentrank.io/blog/outcome-based-pricing-in-ai-2026) | [S] | L |
| Google and Meta ad agents | no agent fee; revenue = media spend | — | — | [M] | M |

**Reading (inference).**
1. Outcome pricing has spread in **service**, where the outcome (a resolution) is observable inside one session and does not need a counterfactual.
2. **No marketing agent with published revenue-share or incremental-revenue pricing was found.** Netcore claims outcome accountability without terms.
3. Marketing CEPs (Braze, MoEngage) converged on **consumption credits**, which reward more agent activity. That is the incentive conflict 02 §6 flagged.
4. A holdout-measured fee (for example, on the lower bound of the incremental-value confidence interval) would be **novel in the category**. It is feasible only if the vendor owns the measurement design; see §3.1.

---

## 5. Incidents, 2024–2026 (India first)

| Date | Incident | Type | What it shows | Source | Tag | Conf. |
|---|---|---|---|---|---|---|
| **18 Aug 2024** | **Zomato bans AI-generated dish images** after complaints. The CEO cited "breach of trust", "increased refunds and lower customer ratings". It promised to detect them "as much as we can… using automation" and told its own marketing team to stop using AI images | AI content → backlash and refunds (India) | Generated creative can create measurable harm (refunds, ratings) | [Business Today](https://businesstoday.in/technology/news/story/zomato-to-ditch-ai-generated-food-images-after-customer-complaints-442158-2024-08-20); [Storyboard18](https://www.storyboard18.com/amp/advertising/zomato-bans-ai-generated-images-from-menus-with-immediate-effect-42468.htm) | [S] | H |
| Aug 2025 | **CCPA fines Rapido ₹10 lakh** for "Guaranteed Auto / Auto in 5 min or get ₹50". The ₹50 was coins valid seven days; the T&C were in tiny font; refunds were ordered | Misleading offer copy (India) | Offer and claim copy carries direct penalty risk | [MediaNama](https://www.medianama.com/2025/08/223-rapido-fined-rs-10-lakh-misleading-auto-ad/); [SCC Online](https://www.scconline.com/blog/post/2025/08/22/ccpa-penalises-rapido-for-its-guaranteed-auto-in-5min-or-get-50-ad/) | [S] | H |
| **4 Dec 2025** (stayed 20 Jan 2026) | **CCPA fines Zepto ₹7 lakh** for drip pricing and "basket sneaking" (Zepto Pass pre-selected). It ordered "remove all default selections" and proof within 15 days. The NCDRC gave an interim stay | Dark pattern (India) | Default-on add-ons and late fees are enforceable dark patterns | [Inc42](https://inc42.com/buzz/ccpa-slaps-inr-7-lakh-penalty-on-zepto-over-dark-patterns/); [MediaNama](https://www.medianama.com/2025/12/223-zepto-fined-7-lakh-dark-patterns-ahead-ipo/) | [S] | H |
| 2026 | CCPA penalises **nine platforms** over dark patterns (IndiGo, Zepto, BookMyShow, Physics Wallah, FirstCry, SpiceJet and others), collecting about ₹20 lakh. Clarification notices went to 15 e-commerce and quick-commerce platforms (24–28 Nov 2025) | Dark pattern (India) | The regulator is active on digital journeys | [Angel One](https://www.angelone.in/news/market-updates/ccpa-imposes-penalties-on-indigo-zepto-bookmyshow-and-6-other-platforms-over-dark-patterns) | [S] | M |
| Cumulative to 2026; Q1 FY27 | **About 500 principal entities (brands), 3,000+ headers and 40,000+ templates blacklisted since 2019** (government statement). In Q1 FY27, 263 senders were blacklisted and 2,873 headers blocked | Telecom enforcement (India) | **Brands, not only spammers, are blacklisted.** "Losing the rails" is real | [Fonearena / government](https://www.fonearena.com/blog/415968/government-fines-violators-rs-110-crore-to-curb-pesky-calls.html/amp); [Open Magazine](https://openthemagazine.com/india/trais-crackdown-on-spam-calls-intensifies-183-lakh-telecom-resources-blocked-in-three-months) | [S] | M |
| Oct 2023 (outside the window) | **Kotak Mahindra and Tamilnad Mercantile Bank customers get "₹753.48 crore credited" SMS.** Kotak attributed it to a **glitch in its SMS template** | Template/automation error (India) | Variable-filled templates fail at scale | [TechStory](https://techstory.in/kotak-mahindra-bank-says-rs-753-crore-credit-alert-caused-by-sms-glitch/); [Gulf News](https://gulfnews.com/world/asia/india/account-credited-with-astronomical-amount-is-this-indias-biggest-banking-glitch-ever-1.500224774) | [S] | M |
| Feb 2026 | LocalCircles: 96% of WhatsApp users get daily unsolicited commercial messages | Consumer backlash (India) | A saturated channel and the per-user marketing cap (error 131049) | §2; [MSG91 on 131049](https://msg91.com/help/whatsapp/whatsapp-error---this-message-was-not-delivered-to-maintain-healthy-ecosystem-engagement) | [S] | M |
| 19 Apr 2025 | **Cursor support bot "Sam" invents a login policy.** Reported cancellations followed | Agent hallucination → churn | Customer-facing agent statements become "policy" | [AI Incident Database #1039](https://incidentdatabase.ai/entities/cursor-ai-support-bot-(%22sam%22)/) | [S] | M |
| May 2025 | **Klarna rehires humans.** The CEO said AI support gave "lower quality" | Rollback | Autonomy reversal at a flagship | [Fortune](https://www.fortune.com/2025/05/09/klarna-ai-humans-return-on-investment); [Entrepreneur](https://www.entrepreneur.com/business-news/klarna-ceo-reverses-course-by-hiring-more-humans-not-ai/491396) | [S] | H |
| 6–9 Dec 2025; 2025 | **McDonald's Netherlands pulls its AI Christmas ad** after three days. Coca-Cola's 2025 AI holiday ad and the Valentino and Guess AI campaigns were criticised | AI creative backlash | Human-launched, but shows brand risk | [Washington Times](https://www.washingtontimes.com/news/2025/dec/11/mcdonalds-netherlands-pulls-ai-ad-parodied-christmas-song/); [DesignRush](https://news.designrush.com/7-worst-ai-advertising-backfires-2025) | [S] | H |
| Feb 2026 | A UK shop's after-hours chatbot is talked from 25% up to **80% off** on an order over £8,000. The order was cancelled | Agent offer error | Never let an agent emit prices or codes | [vibegraveyard](https://vibegraveyard.ai/story/uk-chatbot-80-percent-discount-refund/) | [S] | L |
| Jun 2026 | **BMW Toronto agent "Quinn" offers about CAD 7,000 over value.** After CBC asked, the dealer **honoured it** | Agent offer error | Liability for agent offers (cf. Air Canada 2024) | [OECD AI incident](https://oecd.ai/en/incidents/2026-06-11-2ab4); [vibegraveyard](https://vibegraveyard.ai/story/bmw-toronto-quinn-chatbot-buyback-offer/) | [S] | M |
| Jul 2026 | **Who Gives A Crap suspends its AI e-mail agent.** It **confirmed** a typo'd price (24 rolls instead of 48 at A$69.50) to a customer. A correction was sent and no charge made | CRM agent error | Agents amplify upstream content errors | [SmartCompany](https://www.smartcompany.com.au/retail/who-gives-a-crap-suspends-ai-agent-email-error-prices-would-double/); [CXM](https://cxm.world/?p=144481) | [S] | **M** (upgraded from L) |
| 8 Feb 2024 decision (announced 29 Feb); upheld **18 Sep 2025** | **Italian Garante fines Enel Energia €79,107,101.** Unauthorised sales agents exploited its systems, driving nuisance calls and at least 9,300 contracts. The basis was GDPR Arts. 5, 24, 25, 28 and 32 (security and accountability), not consent per se. The Court of Rome confirmed it; a further appeal is pending | Compliance fine | **The brand pays for agents acting in its name** | [Garante doc 9988921](https://www.gpdp.it/garante/doc.jsp?ID=9988921); [Enforcement Tracker](https://www.enforcementtracker.com/ETid-2306) | [S] | H |
| 2026 | UK ICO PECR fines: <br>• Allay Claims £120k (Jan); <br>• TMAC £100k (Feb); <br>• **Energy Prices Direct £160k (20 May)**; <br>• **KRA Consultancy £300k (Jun; 5.58M texts without consent)**; <br>• Thermotech £240k and Jacksons £130k (Jul) | Compliance fine | Steady enforcement against automated volume | [ICO May 2026](https://ico.org.uk/about-the-ico/media-centre/news-and-blogs/2026/05/glasgow-based-energy-company-fined-160-000-for-making-unsolicited-marketing-calls/); [ICO TMAC](https://ico.org.uk/action-weve-taken/enforcement/2026/02/tmac-ltd-mpn/); [Freevacy](https://www.freevacy.com/news/ico/ico-fines-debt-services-company-ps300000-for-pecr-violations/7488) | [S] | H |
| Aug 2024; 2024 | **FCC: Lingo Telecom $1M settlement; Steve Kramer $6M forfeiture** for AI-voice Biden robocalls. Both exact months are [M] | AI voice compliance | AI voice = artificial voice; the carrier is liable too | [CyberScoop](https://cyberscoop.com/telecom-behind-ai-powered-biden-robocall-agrees-to-1-million-fcc-fine/); [Insidetowers](https://insidetowers.com/?p=61576) | [S] | H (facts); M (dates) |

**Surveys on errors and accountability (new; gaps in 02).**
- **Validity, State of CRM Data Management 2026** (25 Aug 2026; n=500 marketers in the US, UK, Brazil, Australia and New Zealand; vendor-commissioned):
  - two in three organisations increased the marketing decisions they delegate to autonomous agents;
  - **about 92% of SVP/VP** respondents acted on an AI recommendation they later suspected was wrong because of bad data;
  - the same figure is about 78% for the C-suite and **41% for individual contributors**.
  - Sources: [AI Journal press release](https://aijourn.com/validity-releases-state-of-crm-data-management-in-2026-report-revealing-marketers-trust-in-their-data-hasnt-caught-up-with-their-ai-goals/); [Validity](https://www.validity.com/resource-center/state-of-crm-data-report-2026/). [S, M]
- **Kana, "Agentic Divide"** (Jun 2026; n=225): 40% say the Chief AI Officer should own agentic marketing. Marketing executives prefer shared or marketing ownership. "Ownership remains unclear" — [Las Vegas Sun/PR](https://lasvegassun.com/news/2026/jul/14/kana-announces-new-research-70-of-enterprises-alre/). [S, M]
- **Cequence/EMA:** 65% of 202 technology leaders say their AI agents have acted out of scope — [DSG](https://dsg.co.za/market-insights/two-thirds-of-enterprises-have-seen-ai-agents-act-out-of-scope). [S, M]
- **Delight.ai** (vendor; n=1,000 US consumers): consumers blame **the brand**, not the AI vendor — [Delight.ai](https://delight.ai/blog/ai-agent/who-is-responsible-ai-mistakes). [S, L–M]

**Still no public case** of an autonomous CRM agent mass-sending wrong or non-compliant messages, in India or elsewhere. Every documented agent failure is in **one-to-one service or chat**, or in **human-launched AI creative**. [S, M]

---

## 6. Indian regulation: confirm or correct 04

| Item | 04 said | Finding | Source | Tag | Conf. |
|---|---|---|---|---|---|
| **TRAI promotional time band** | 09:00–21:00 IST; "some vendor pages say 10:00" | **09:00–21:00 IST** is consistent across every source found (Exotel, MyOperator, Gofrugal). The TRAI-era text bars commercial communication "between 9.00 PM to 9.00 AM". **No 10:00 source found** this pass. Sunday coverage is ambiguous (the 2010 rule said Mon–Sat; MyOperator says Mon–Sun). The **Third Amendment summaries show no change to hours** | [Exotel](https://developer.exotel.com/docs/faqs/calling-hours); [MyOperator](https://support.myoperator.co/portal/en/kb/articles/what-are-transactional-service-implicit-vs-promotional-calls-under-trai-and-what-rules-apply-to-dnd-numbers); [TRAI PR (older)](https://trai.gov.in/sites/default/files/FINALPRCNIT1DEC.pdf) | [S] | M (unchanged); regulation text still unread |
| **Opt-out in messages / revocation SLA** | SLA unverified ("24h" from memory); opt-out in every message expected | **Second Amendment notified 12 Feb 2025; complaint provisions from 13 Apr 2025.** Secondary sources say promotional messages must carry an **opt-out**. TRAI's PR confirms the **90-day re-consent bar**. The **"7-day" figure applies to consent "for completing any ongoing transaction"**, not to promotional consent in general (TRAI PR wording). Headers carry the suffixes P, S, T and G. **The revocation SLA is still unverified, and "24h" is unsupported.** Design to immediate suppression | [TRAI PR 11/2025](https://cms.trai.gov.in/sites/default/files/2025-02/PR_No.11of2025.pdf); [Khaitan](https://khaitanco.com/thought-leadership/Efforts-to-Curb-Spam-Continue-TRAI-Releases-Second-Amendment-to-the-TCCCPR-2018); [Voice&Data](https://www.voicendata.com/news/trai-curbs-spam-calls-and-messages-with-amendments-in-rules-8720531) | [S] | M (opt-out); L (SLA) |
| **TCCCPR Third Amendment** | Gazette 18 Sep 2026; draft comments to 19 Apr, counter-comments to 4 May | **Confirmed** by PR 119/2026 (18 Sep 2026). It adds **Reg. 21A**: operators identify and share CLIs likely used for spam. It adds AI/ML detection following a Feb 2026 DoT direction. One source gives the **draft deadlines as 12 Apr / 27 Apr**; Vi's comments are dated 19 Apr, suggesting an extension. Minor conflict | [TRAI PR 119/2026](https://www.trai.gov.in/sites/default/files/2026-09/PR_No119of2026.pdf); [Indian Infrastructure](https://indianinfrastructure.com/2026/03/16/trai-releases-draft-telecom-commercial-communication-preference-third-amendment-regulations-2026-for-consultation/); [Vi comments](https://www.trai.gov.in/sites/default/files/2026-04/VIL%20comments%20to%20the%20TRAI%20draft%20TCCCPR%203rd%20amendment%20_%2019Apr2026.pdf) | [S] | H (notified); M (detail) |
| **DPDP Rule 10 (children)** | s.9(3) bars targeted ads at under-18s; Rule 10 not re-verified | **Rule 10:** verifiable parental consent using "reliable" identity and age details the fiduciary already holds, **or a virtual token from an authorised entity, including a DigiLocker service provider**. **Exemptions (Fourth Schedule)** cover healthcare, educational institutions, childcare and creches, for restricted purposes; they sit in **Rule 12** of the final text (Rule 11 in the draft). Duties apply from **13 May 2027** | [SCC Online](https://www.scconline.com/blog/post/2025/12/26/digital-personal-data-protection-rules-2025-key-highlights/); [Bar & Bench](https://www.barandbench.com/law-firms/view-point/childs-personal-data-and-privacy-analysing-the-draft-dpdp-rules-2025); [dpdpa.com Rule 10](https://dpdpa.com/dpdparules/rule10.html) | [S] | M |
| **DPDP timeline** | Consent managers from 13 Nov 2026; full duties 13 May 2027; no compression found | **New:** MeitY consulted on 22 Jan 2026 (comments to 4 Feb) on cutting the window **from 18 to 12 months**, which would make **13 Nov 2026** the full-compliance date. It also proposed immediate cross-border rules and a 3-month Rule 8(3) retention. **No notification found** as of Oct 2026. **Watch item:** if adopted, full duties start in about 33 days | [Storyboard18](https://www.storyboard18.com/amp/digital/meity-seeks-industry-views-on-fast-tracking-dpdp-act-rollout-proposes-12-month-compliance-timeline-88332.htm); [S.S. Rana](https://ssrana.in/articles/meity-plans-to-cut-short-dpdp-compliance-timeline-and-notify-cross-border-restrictions-for-sdfs/); [Takshashila](https://takshashila.org.in/content/blogs/20260126-meity-considers-reducing-dpdpa-compliance-timelines.html) | [S] | M (proposal); M (not yet notified) |
| **RBI sales-practice directions** | Final reported 16 Jun 2026, effective 1 Jan 2027 (single report) | **Confirmed. Issued 15 Jun 2026** as RBC (Second Amendment) Directions 2026, part of **17 notifications** covering commercial banks, AIFIs (RBI/2026-27/122) and NBFCs/HFCs (RBI/2026-27/123 per one source). **Effective 1 Jan 2027.** One study-site outlier says 1 Jul 2026. The directions cover: <br>• explicit consent; <br>• bans on **dark patterns** and **compulsory bundling**; <br>• refunds and compensation for mis-selling; <br>• a scope extended to **DSAs, DMAs, sub-agents** and third-party product representatives; <br>• **consent records kept until 1 year after the relationship ends**; <br>• for NBFCs, **promotional communications only with the customer's prior approval** | [Khaitan NBFC Ergo, 7 Jul 2026](https://www.khaitanco.com/sites/default/files/2026-07/Ergo_RBI%20Amendment%20Directions_NBFCs_7%20July%202026.pdf); [KSK](https://ksandk.com/newsletter/rbi-responsible-business-conduct-second-amendment-directions-2026/); [Vinod Kothari, Jun 2026](https://vinodkothari.com/wp-content/uploads/2026/06/From-Consent-to-Compensation_-RBIs-Directions-for-REs-on-Sales-Practices.pdf); [MMJC](https://mmjc.in/rbi-nbfc-responsible-business-conduct-second-amendment-directions-2026/) | [S] | **H** (date and effect); M (clauses) |
| **ASCI AI labelling** | Draft 8 May 2026; "final status not found" (01 reported a final on 29 Sep, weak) | **Final "Guidelines for Responsible Labelling of Synthetically Generated Content in Advertising" released 29 Sep 2026, in effect three months later (about 29 Dec 2026).** Banned even with a label: <br>• fabricated endorsements; <br>• misleading product results; <br>• deepfakes and likeness used without consent; <br>• fake locations. <br>Labels such as "Audio/Video created using AI". **AI recommendations that are sponsored must say "Sponsored by"**. Routine edits are exempt. Whether the draft's high/medium/low tiers survived is **unconfirmed** | [e4m](https://www.exchange4media.com/advertising-news/asci-issues-guidelines-on-responsible-labelling-of-ai-generated-ad-content-158710.html); [Fox Mandal (final PDF)](https://foxmandal.in/wp-content/uploads/2026/09/Guidelines-for-Responsible-Labelling-of-Synthetically-Generated-Content-in-Advertising.pdf); [GKToday](https://www.gktoday.in/asci-issues-ai-generated-advertising-labelling-guidelines/) | [S] | **H** (final and date) |
| **IT Rules (synthetic content)** | Mentioned only | **G.S.R. 120(E), notified 10 Feb 2026, in force 20 Feb 2026.** It requires a prominent label plus non-removable **provenance metadata** for "synthetically generated information", and a **3-hour takedown** on official flags. These are intermediary duties, and ASCI is aligned to them | [Hogan Lovells](https://www.hoganlovells.com/en/publications/india-introduces-mandatory-labelling-for-ai-and-3hour-takedown-for-illegal-content); [AMS Shardul](https://www.amsshardul.com/wp-content/uploads/2026/02/Legislative-Update-IT-Rules-Amendment.pdf) | [S] | H |
| **RBI Ombudsman (new)** | — | **RB-IOS 2026 effective 1 Jul 2026**, with compensation up to **₹33 lakh**. The complaint must go to the bank first, with 30 days to respond | [Outlook Money](https://www.outlookmoney.com/banking/rbi-ombudsman-scheme-2026-customers-can-seek-compensation-of-up-to-rs-33-lakh) | [S] | M |

---

## 7. Corrections to 01–05

| # | File / section | Earlier statement | Correction / addition | Basis | Conf. |
|---|---|---|---|---|---|
| 1 | 01 §2.1 (Google) | AI Max: "no opt-out", forced, Sep 2026 (Veonib, weak) | **DSA→AI Max auto-migration delayed to Feb 2027 (announced 11 Jun 2026)** after advertiser feedback. ACA and campaign-level broad match still migrated 1–30 Sep 2026, with no formal opt-out. API v25 **bundles brand lists and text automation with AI Max** | §1.1; [PPC Land](https://ppc.land/google-delays-dsa-to-ai-max-automigration-to-february-2027/) [S]; `campaign.proto` [O] | M / H |
| 2 | 01 §2.1 | "Ask Advisor" powers undocumented | Ads Advisor (Apr 2026): "will ask for approval before taking action". SEJ reports a GML agent acting without asking. Conflict unresolved | [Google blog](https://blog.google/products/ads-commerce/ads-advisor-google-ads/) [S] | M |
| 3 | 01 §2.1 (Meta); 02 l.359 | Advertisers won "fences, receipts and tests" (general) | **True for Google** (dated in §1.1). **Meta removed fences**: the existing-customer cap (UI), interests, 5% leakage into excluded placements, default-on creative. It gave **receipts and tests** instead: segment reporting and incremental attribution. The API still carries `existing_customer_budget_percentage` | §1.2; SDK [O] | H |
| 4 | 01 §2.2 (Braze) | The "$250–300k per use case" price was "not found" | Reported once, in a search summary of the MarketBeat Needham (14 Jan 2026) note. **A second search could not confirm it.** Braze now sells only via **Action Credits** (from 27 May 2026) | §3.3 [S] | L / M |
| 5 | 01 §2.2 | Braze lift results (Purina vs holdout, RTL+ vs no-message) | Add: **DS Go's default comparison is a random-option control (≥5%), explicitly not a no-email holdout.** DS Pro optionally supports BAU and holdout arms. The uplift metric is set by agreement with Braze | §3.1 [S] | M |
| 6 | 01 §2.4 (MoEngage) | Aampe uses per-user Thompson sampling; Swiggy 2× CTR | MoEngage decisioning "control group" = **randomised messages, not no-message**. Aampe recommends **no-message / BAU / agent** arms. The Aampe paper shows **human-managed > autonomous**, with engagement lift far above conversion lift. Swiggy in-app CTR **10%→21%** | §3.1 [S] | M |
| 7 | 01 §4.2 | Who Gives A Crap: weak (aggregator) | **Medium**, corroborated by SmartCompany and CXM | §5 | M |
| 8 | 01 §4.2 / 03 §8 | "No autonomous CRM mass mis-send found" | **Still none.** Add service-agent offer failures (BMW Toronto Jun 2026, UK shop Feb 2026, Cursor Apr 2025), Klarna's reversal (May 2025), Zomato's AI-image ban (Aug 2024) and the Kotak/TMB template glitch (Oct 2023) | §5 | M |
| 9 | 01 §4.2 (ASCI) | Final reported 29 Sep 2026: weak | **Confirmed** (e4m and a Fox Mandal-hosted final PDF). Effective about 29 Dec 2026 | §6 | H |
| 10 | 02 §6 | Salesforce ~$2 per conversation; Zendesk and Sierra outcome pricing | Salesforce **Help Agent $2 per resolution (GA Jul 2026)**. HubSpot **Customer Agent $0.50 per resolved conversation from 14 Apr 2026**. Intercom Fin $0.99 per outcome. Zendesk's about $1.50–2 is unpublished. **Sierra outcome pricing unconfirmed** | §4 | M |
| 11 | 02 gaps | Validity: 2/3 delegated more; 92% acted on bad AI advice, "unverified" | **Verified via the press release (25 Aug 2026, n=500).** **The 92% is SVP/VP only**; the C-suite figure is 78% and individual contributors 41%. Vendor-commissioned | §5 [S] | M |
| 12 | 02 gaps | No survey on accountability for agent errors | Kana (Jun 2026): ownership unclear; 40% say the CAIO should own it. Cequence/EMA: 65% saw agents act out of scope. Delight.ai: consumers blame the brand | §5 | M / L |
| 13 | 02 (India buyer data) | No India autonomy data | Dentsu (Oct 2026): **46% of Indian CMOs vs 63% globally** are uncomfortable delegating high-stakes tasks | §2 | M |
| 14 | 04 §2.1 (time band) | "Some vendor pages say 10:00" | No 10:00 source found. All sources say **09:00–21:00**, with Sunday coverage ambiguous. Keep the stricter 10:00–20:00 product default | §6 | M |
| 15 | 04 §2.1 (opt-out) | "24h" revocation [memory]; opt-out in every message | **Opt-out in promotional messages: supported (M).** The **7-day rule concerns transaction-completion consent**. **24h is unsupported**; the SLA is still unverified | §6 | M / L |
| 16 | 04 §2.1 (Third Amendment) | Draft comments to 19 Apr, counter-comments to 4 May | One source: 12 Apr / 27 Apr. Probably extended. Adds Reg. 21A (sharing spam CLIs across operators) | §6 | M |
| 17 | 04 §2.2 (DPDP) | No compression found | **MeitY proposal (22 Jan 2026) to move full compliance to 13 Nov 2026**: not notified as of Oct 2026. Watch item. Rule 10 mechanics added; exemptions are in **Rule 12** of the final text | §6 | M |
| 18 | 04 §2.3 (RBI) | Final 16 Jun 2026 (single report, M) | **Issued 15 Jun 2026; effective 1 Jan 2027 (H).** Adds DSA/DMA scope, consent records to +1 year, and NBFC promotional communications only with prior approval | §6 | H |
| 19 | 04 §2.4 (ASCI) | "Final status not found" | **Final 29 Sep 2026, effective about 29 Dec 2026** | §6 | H |
| 20 | 04 §9.3 (Enel) | About €79M, 2024 [memory, unverified] | **€79,107,101; decision 8 Feb 2024; upheld by the Court of Rome on 18 Sep 2025.** The basis is **security and accountability for sales agents** (GDPR Arts. 5, 24, 25, 28, 32), not consent alone | §5 | H |
| 21 | 04 §9.3 (ICO) | "ICO regularly fines" [memory] | 2026 examples: £100k–£300k, including KRA Consultancy £300k for 5.58M texts | §5 | H |
| 22 | 04 §9.3 (India enforcement) | 1,150 entities blacklisted (Mar 2025) | Add: **about 500 principal entities (brands)** blacklisted since 2019, plus 3,000+ headers and 40,000+ templates | §5 | M |
| 23 | 04 (new) | — | **IT Rules SGI amendment** (in force 20 Feb 2026) and **RB-IOS 2026** (1 Jul 2026, up to ₹33 lakh) | §6 | H / M |
| 24 | 02 §2 / §5 (Airtel) | (Not in 01–05; flagged in case it is added) | Do **not** cite "Airtel next best action, 135M actions". Unverified; 135M was its 5G customer count | §2 | — |

---

## 8. Still open (not resolvable from search summaries)

- **TCCCPR consolidated text:** the revocation SLA, Sunday coverage, and whether every promotional SMS must carry an opt-out. The TRAI Gazette is unreachable.
- **The final ASCI PDF:** whether the risk tiers were retained.
- **DPDP:** whether the 12-month compression is notified before 13 Nov 2026.
- **Braze DS Pro docs** (repo unreachable): the exact holdout sizes; whether BAU is a default arm.
- **The full arXiv 2604.08621 figures:** the 65.3% and 0.3% are secondary.
- **No dated Google primary for PMax GA (Nov 2021); the Meta ASC launch month and the audience-segment reporting date** are both [M] or undated.
- **Indian telecom and quick-commerce CRM-leader interviews:** none found. ET BrandEquity and Mint are not searchable with this tool.
- **Pricing pages for Braze, MoEngage, Aampe and Persado:** none public.
