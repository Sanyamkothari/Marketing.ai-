# The fully autonomous marketing agent: what it is, what exists, and how Marketing AI gets there

**Prepared:** 11 October 2026, by the lead strategist, Marketing AI (Minfy AI/ML team)
**Status:** research report and recommendation for management. Section 5 is a risk scan for product design, **not legal advice**.
**Evidence:** six research notes written for this study, copied verbatim to
[`autonomous_agent_2026_10_notes/`](autonomous_agent_2026_10_notes/) (01 market, 02 buyers, 03 technology, 04 regulation,
05 internal gap, 06 gap fill and corrections). It builds on
[`AGENTIC_AI_OPPORTUNITIES_2026_10.md`](AGENTIC_AI_OPPORTUNITIES_2026_10.md),
[`POSITIONING_DECISION_2026_10.md`](POSITIONING_DECISION_2026_10.md),
[`../plans/MARKETING_AI_PLAN_K_PROPOSAL.md`](../plans/MARKETING_AI_PLAN_K_PROPOSAL.md) and
[`../plans/MARKETING_AI_PLAN_J_LAYER.md`](../plans/MARKETING_AI_PLAN_J_LAYER.md).

---

## Read this first: most web sources were blocked

**Most web sources could not be opened in this environment.** Page fetching failed on DNS for every domain tried.
The egress proxy refused direct downloads from nearly every host, including TRAI, PIB, MeitY, Gartner, Braze, MoEngage,
the FTC and EUR-Lex. The shared search budget ran out during several passes. Every note says so in its own section 0.

**What could be opened.**
- 22 vendor and open-source documents, mostly Adobe's documentation repositories on GitHub (note 01).
- 36 documents from 27 public GitHub repositories: bandit libraries, agent SDKs, protocol specifications (note 03).
- One buyer survey in full: the StackAdapt "AI Delegation Gap" report (note 02).
- Google Ads API v25 and Meta Marketing API v26.0 source code (note 06).
- Our own repository and plans.
- **No primary legal text was opened.** Every law and regulation finding rests on search summaries (note 04).

**So most claims below rest on search summaries, not on the pages themselves.**

**How citations read.** Each factual claim ends with a citation such as (R12 [S], M):
- **R12** is source 12 in §9.
- **The tag** says how we saw it: **[O]** opened primary source; **[S]** search-result summary only, page not opened;
  **[M]** unverified memory or background knowledge.
- **The confidence** is **H** (high), **M** (medium) or **L** (low). Note 01's "strong/medium/weak" and note 03's
  "High/Medium/Low" map to H/M/L.
- "(inference)" marks our reasoning, not a sourced fact.
- A vendor's lift figure is a vendor claim, whatever the confidence that the vendor said it.

**Verify these before any external use** (deck, client, partner, investor):

| # | Claim family | Why it needs checking | Where used |
|---|---|---|---|
| V1 | Haus Meta and PMax results (+9% → −12%; 58%; Caraway ~33%) | Search summaries of a testing vendor's own pages | §2.4, executive summary |
| V2 | Braze Decisioning Studio Go and MoEngage control definitions | Vendor docs not opened | §2.2 |
| V3 | Aampe trial figures (+65.3%, +0.3%) | Checked against the paper on 10 Oct (R175); this study could not re-open it | §2.3, executive summary |
| V4 | TRAI rules:<br>• complaint triggers (5 vs 3 in 10 days)<br>• the 15-day bar on registered headers<br>• the opt-out SLA and Sunday time band<br>• the "7-day" window<br>• Third Amendment start dates | Gazette text not opened; sources conflict | §5.2 |
| V5 | DPDP: whether MeitY notifies a shortened timeline before 13 Nov 2026; Rule 10 mechanics; holdouts as a purpose | No notification found; no guidance on holdouts exists | §5.2, §7 |
| V6 | RBI sales-practice clauses; ASCI final guideline tiers; IRDAI and SEBI details | Law-firm summaries only | §5.2 |
| V7 | WhatsApp India rates, per-user caps, portfolio limits, the 1 Oct 2026 service-message change | Meta pages not opened; rates conflict across sources | §2.5, §5.2 |
| V8 | All pricing points (Agentforce, HubSpot, Intercom, Braze, MoEngage, Klaviyo) | Trackers and secondary summaries | §3.6 |
| V9 | All incident cases except those from opened docs | Trade press and aggregators | §5.5 |
| V10 | Dentsu India figures (46% vs 63%) | One trade source in this study; confirmed on 10 Oct (R175) | §3.5 |
| V11 | Any market-size figure | Broader categories; methods unknown. **Do not use** | §2.1 |

---

## Executive summary

**The question.** Should Marketing AI's future be a fully autonomous marketing agent, and how do we get there?

**The answer.** Yes to autonomy, no to "fully". Our future is an agent that **acts unattended inside a mandate the
client signs**, for one decision type at a time. It proves every rupee against a holdout it cannot touch. It earns
each step up with evidence and drops back automatically on harm. An unbounded agent that sets its own goals,
budgets, audiences and content (level L5) is not a product anyone credible sells (R184 [O], H). We should never
build it.

**The five numbers that carry the recommendation.**

1. **78% versus 50%.** 78% of 500 advertisers are comfortable with AI acting within rules they set. Only 50% accept
   autonomy, even "when performance is proven" (R1 [O], H). The market boundary is the mandate, not the AI.
2. **+65.3% versus +0.3%.** Aampe's 11-month randomised trial on 8.8 million users raised push-driven app-open days by
   65.3%. Conversion days rose only 0.3% (±0.24) (R45 [S], M; checked against the paper on 10 Oct, R175). Unattended
   engines optimise what they see fast, and that is engagement, not money.
3. **+9% at midpoint, −12% at the end.** Across 640 Haus tests, Meta's Advantage+ beat manual campaigns halfway, then
   lost by the end (R36 [S], M). In one geo test, Google's reporting overstated Performance Max by about 33% (R37 [S], M).
   Autonomy that grades itself flatters itself.
4. **No default no-message holdout.** Braze Decisioning Studio Go and MoEngage measure their agents against random
   picks among the same approved options. Braze's FAQ says that control is not a no-email group (R41, R43 [S], M).
   We found no one who sells a customer-owned, no-message holdout reported in rupees with an error band (R184 [O], M;
   absence of results).
5. **263 senders blacklisted in one quarter.** TRAI blacklisted 263 senders for a year in April–June 2026 (R139 [S], M).
   Action starts at 5 complaints in 10 days (R137 [S], H). One bad unattended run can cost a client its SMS rails.

**What follows.** The ad platforms reached autonomy because their downside is a capped budget and their feedback is
dense. CRM sends are irreversible, sparse in feedback, and in India tied to the brand's registered rails (inference, M;
§2.4). So the
winning design is the one buyers already asked Google for: **fences, receipts and tests** (R64 [O], H; R66 [S], M).
Marketing AI already has the hardest parts: a persistent randomised holdout with logged propensities, doubly robust
evaluation, a registered test plan, a harm alarm, a compliance gate and a signed hash-chained ledger (R182 [O], H).
It lacks the action path: mandates, a single deterministic writer, breakers that act, and send-time state.

**Where we stand, honestly.** We have no client pilot. On Hillstrom, uplift did not beat plain risk ranking. Plan K's
measurement upgrades showed no gain there either (R180 [O], H). The usability test is pending (R181 [O], H). Safety
findings in today's product are being fixed on a branch (M184). None of this argues against the path. It sets the order.

**The path, in five gated phases** (estimates ours, unmeasured):

| Phase | Gate evidence | Earliest gate |
|---|---|---|
| 0. Make today safe; decide | M184 merged; decisions D1–D10 taken; legal opinions commissioned | week 4 (8 Nov 2026) |
| 1. One real cycle, human-approved (L2) | A real list handed off, reconciled and measured | week 16 (early Feb 2027) |
| 2. Shadow autonomy (L3, no sends) | Two shadow cycles with zero mandate violations; breaker drill passed | week 26 (mid-Apr 2027) |
| 3. L3 pilot inside a signed mandate, on the client's rails | One clean quarter; lower bound above zero, or an honest "cannot tell" | week 40 (mid-Jul 2027) |
| 4. Govern other engines; widen | Two clients on two rails; a Braze or MoEngage engine audited against a no-message holdout | about week 64 |
| 5. L4 for narrow portfolios | A clean L3 quarter; portfolio lower bound above zero; independent verifier passing | 2028, evidence only |

Reaching the end of the L3 pilot needs about **75 person-weeks** (66 engineering, 9 data science). Calendar time is set by
client cycles, not effort (inference). **The first decision management must take** is D2: may a deterministic dispatcher
act inside a signed mandate, on the client's rails, once gate G2 passes? Today K1, DEC-1100 and the Plan J layer's
no-SEND rule forbid it (R177, R178 [O], H).

---

## 1. What "fully autonomous" means

### 1.1 A precise definition

A marketing system is **autonomous for a decision type** when three things hold:
- it chooses and carries out actions of that type for real customers;
- no person approves each action;
- it monitors itself and falls back to a safe state on its own.

A system is **fully autonomous** when that holds for every decision type, including the goal, total budget, audience
and new content, with no envelope a person drew. That is level L5 below.

Autonomy is a **design choice separate from capability**. The best frameworks attach a level to one feature inside an
operating domain. They define it by who decides, who monitors and who is the fallback. That is SAE J3016 for cars
(R130 [S], H) and Feng, McDonald and Zhang for agents (R128 [S], H). No regulator has published a marketing autonomy
scale, as far as the searches found (R184 [O], M; absence of results).

### 1.2 The levels, L0–L5 (Marketing Decision Autonomy Levels)

The levels come from note 03 §5.2 (R184 [O]). A level attaches to **one decision type inside one operating envelope**.
The envelope covers:
- use case and objective;
- action set: offers, channels, registered templates;
- audience rule and maximum size;
- contact and pressure caps;
- spend per day and per period;
- offer-value ceiling and margin floor;
- region and regulatory regime;
- data-freshness limits;
- the certified policy version;
- an expiry date.

| Level | Who decides each action | Who monitors / fallback | Evidence required to enter | In our product |
|---|---|---|---|---|
| **L0 Manual** | A person | A person / a person | Nothing | Before the product |
| **L1 Recommend** | A person; the system scores, ranks, sizes tests and explains | A person / a person | Deterministic scoring with evidence labels; holdouts; audit log | **Built** (Plan J) |
| **L2 Propose and approve** | The system drafts a complete action; a person approves each one | A person warned by alerts / a person | Default-deny policy; compliance gate; signed ledger; claim verifier; pass^k evaluation | **Largely built** (Plan K), mostly off by default |
| **L3 Act inside a mandate** | The system acts alone inside one signed mandate for one decision type; anything outside goes the L2 route | The system, with a daily digest / **the system**: it pauses and reverts to the last certified policy; a person resumes | • a signed mandate, checked per action by code<br>• a single writer<br>• stochastic choice with logged propensities<br>• an always-on holdout and explore slice<br>• certification for this envelope<br>• ≥2 shadow cycles with zero violations<br>• guardrails with non-inferiority margins<br>• breakers wired to the harm alarm<br>• a monthly kill-switch drill | **Not built** |
| **L4 Self-directed in a portfolio** | The system also creates and retires campaigns, moves budget between them and rotates approved content, inside a portfolio mandate | Monitor: the system and an independent verifier with its own key; weekly human review.<br>Fallback: the system per campaign; a portfolio breaker returns all to L2 | • ≥1 quarter of clean L3<br>• portfolio lower bound >0 against the universal holdout<br>• a claims library and content gate<br>• a verification bundle passing every cycle<br>• incident drills<br>• a written oversight procedure | Research-grade; narrow programmes only |
| **L5 Unbounded** | The system sets goals, budgets, audiences and content with no envelope | None / none | Nothing would be enough | **Not recommended.** Consent, DLT and claim rules assume an accountable person (R184 [O]; R129 [S], H) |

**Promotion** moves one level at a time, per envelope. It is a signed Admin act that quotes evidence ids: shadow
results, the certification bound and the violation count. **Widening** an envelope is a new mandate, never an edit.

**Automatic demotion to L2** happens on any of these (R184 [O], design):
- a harm alarm on the primary metric or any guardrail;
- a spend or volume breaker;
- a data-freshness breaker;
- the compliance-block rate above its threshold;
- a mismatch between our send record and the channel's send log;
- drift above threshold;
- mandate expiry;
- a policy-version change that was not certified.

### 1.3 The level each decision type should reach

Buyers want autonomy per job, not per account. The appetite column summarises note 02 §3, which combines the
StackAdapt survey (R1 [O], H), PwC (R9 [S], M) and adjacent evidence. No survey asked CRM marketers task by task
(R184 [O], M; absence).

| Decision type | Buyer appetite | Highest level we recommend | Why |
|---|---|---|---|
| Reporting and "what changed and why" | Yes, first | L3 (read-only, no customer effect) | Most used, most delegated job (77% use; 53% comfortable automating) (R1 [O], H) |
| Send time inside allowed hours | Yes | L3 | Low risk, reversible; already autonomous in most platforms ([M], M) |
| Frequency, including "do not send" | Yes, within a hard ceiling | L3 | The agent may only send less than the cap |
| Channel among consented channels | Mostly yes | L3 | Allowed channels follow consent and registered templates |
| Audience refresh inside a rule | Yes, with a preview | L3 (preview in digest) | Suppression and consent always applied |
| Offer choice from an approved menu | Yes, if the menu is fixed | L3 | The agent picks only among approved items; a holdout shows value |
| Copy: pick among approved variants | Partly | L3 | Variants are registered and measured as arms |
| Copy: write new SMS or WhatsApp text | No | **L2 forever** | DLT and Meta require pre-registered templates (R138, R156 [S], H) |
| Discount depth or new offers | No | **L2** | Money tasks are trusted least: 20% (R9 [S], M) |
| Budget pacing inside a set total | Yes, within a ceiling | L3 | Only between approved lines; pause and undo |
| Total budget, goal, strategy | No | **L1–L2** | Buyers keep "strategy, brand, budget, accountability" (R1 [O], H) |
| Compliance checks | Automate the check, never the override | Deterministic rules at every level | 13% of IT leaders say they have the right governance (R3 [S], M) |
| Customer conversations | Cautious | L2–L3 with escalation | Forrester expects a third of brands to erode trust with poor AI self-service (R12 [S], M) |
| Price, eligibility, credit (BFSI) | Not a marketing decision | **Excluded** | GDPR Art. 22 and RBI rules; human review required (R167, R148 [S], M) |

---

## 2. The market

### 2.1 Who sells what, at which level

**Levels below** are the highest generally available level: L1 suggests; L2 acts once a human approves; L3 acts
unattended inside human-set guardrails; L4 acts towards a goal and budget alone. Rows apply note 06's corrections.

| Vendor / product (Oct 2026) | What runs unattended | Level (GA) | How results are measured | India | Evidence |
|---|---|---|---|---|---|
| **Google Performance Max / AI Max** | Bids, placements, queries, audiences, asset assembly | **L4** inside budget and goal | Platform-reported; optional lift tests; holdback field new in API v25 (R64 [O], H) | Sold in India ([M]) | M |
| **Meta Advantage+** | Audience (now a "signal"), placements, creative enhancements on by default | **L4** inside budget | Platform-reported; Conversion Lift; GeoLift open source (R61 [O], H) | Sold in India ([M]) | M |
| **Braze**: Decisioning Studio (ex-OfferFit), Operator, Agent Console | Per-user channel, time, offer and frequency from an approved menu | **L3** decisioning; L2 Operator | Random control, holdout or business-as-usual per deployment; Go defaults to a random-option control (R41, R42 [S], M) | Not found | M |
| **MoEngage + Aampe** (acquired 24 Jun 2026) | One RL agent per customer: what, when, where, whether to send | **L3** | MoEngage: randomised-message control; Aampe: recommends no-message / BAU / agent arms (R43, R44 [S], M) | **Home market**; Swiggy a named user (R31 [S], M) | M |
| **Salesforce Agentforce Marketing** | Segmentation drafts; goal agent in pilot | **L2** (L3 in pilot) | Not found | – | L–M (R52 [S]) |
| **Adobe** Journey Optimizer, Coworker | Offer ranking and send time only; agents draft, a human clicks "Review and launch" | **L2** agents; L3 offer ranking | Decisioning "holdout" is random-offer traffic; journey holdout limited release from 1 Sep 2026 (R48, R49, R50 [O], H) | Adobe India pages | **H** |
| **HubSpot** Agent Hub | Drafts | **L2** | Not found | – | L |
| **Klaviyo Composer** (beta 30 Jun 2026) | Drafts; "nothing goes live without human approval" | **L2** | Not found | – | M (R51 [S]) |
| **Iterable Nova** | Claimed goal-driven building and experiments | L2–L3 | No customer metrics (R54 [S], M) | – | L |
| **Bloomreach Loomi** marketing agent (GA 2 Jun 2026) | Prompt to campaign workflow, with review controls | L2–L3 | One case: 2.4× conversion rate on 82% fewer customers; a rate, not a total (R53 [S], L) | – | L |
| **Insider One** Agent One (+ Bluecore, 13 May 2026) | Claimed decisioning | L2–L3 | Vendor claims (R57 [S], M on the deal) | Not confirmed | L–M |
| **Optimove AI Decisioning Studio** (27 Mar 2026) | Journey, offer, content, send-time agents | **L3** | Control that shrinks as a campaign "proves itself" (R55 [S]; R175 [O], L–M) | Not found | L–M |
| **CleverTap CleverAI** | Decision engine; agents "at your chosen trust level" | L2–L3 | "Humans review, approve, and launch" (R32 [S], M) | **Home market** | M |
| **Netcore.ai** (30 Jul 2026) | Seven agents claimed, plus "growth engineers" | L3 claimed, as a managed service | Fees tied to "agreed business KPIs"; no published results (R34 [S], M) | **Home market** | L |
| **WebEngage** | No agent launch found in 2026 | – | – | Home market | L (absence; R56 [S]) |
| **Movable Ink** Agentic Campaigns | Per-person message, subject, time, frequency within caps | **L3** | Vendor "+20% revenue lift", no method (R58 [S], L) | – | L |
| **Hightouch** AI Decisioning ($150M at $2.75B, Apr 2026) | Per-customer RL | **L3** | "Every decision measured against a control/holdout" (R46, R59 [S], M) | Not found | M |
| **Twilio Segment** | Predictions only | **L1** | – | – | **H** (R60 [O]) |

**What the table shows.**
- **Only the ad platforms run at L4 in production**, and only inside a spending cap (R184 [O], M).
- **Every CRM vendor that sends unattended does so at L3.** The machine picks which approved thing, when, where and how often.
  It does not write and publish new things on its own (R184 [O], M).
- **Every LLM "campaign agent" launched in 2025–26 is L2.** The agent drafts; a person launches (R50 [O], H; R51 [S], M).
- **Forrester expects agents working without human review to be "years away"**, with benefits in two to five years (R13 [S], M).
- **No credible "autonomous marketing" market size exists.** Figures found size all agentic AI, with unknown methods
  (R74 [S], L). Do not quote them.
- **The useful demand signal is paid adoption.** About a third of Braze's large customers now pay for its AI tools, up
  about 900 basis points in a quarter. Braze's CEO links this to customers seeking "proof of ROI" (R38 [S], M).
  Klaviyo's Composer passed 95,000 users in its first month (R51 [S], M).
- **Leaders expect more automation.** Marketing leaders expect AI to automate 16% of marketing work in 2026 and 36% by
  2028 (R5 [S], M; an expectation, not a measurement). Gartner's 2026 Hype Cycle frames "autonomous marketing" as the
  destination, reached through cost and trust governance (R24 [S], M).

### 2.2 How the vendors measure: who is the "control"?

This is the single most important market finding for us.

| Vendor | Default comparison | No-message holdout? | Source |
|---|---|---|---|
| Braze Decisioning Studio **Go** (self-serve) | Random control (≥5%) getting random combinations of the same approved options. FAQ: "Is the random control a holdout group of users who receive no email? **No.**" | Not by default | R41 [S], M |
| Braze Decisioning Studio **Pro** | Random control, BAU or holdout, chosen per deployment; metric agreed with Braze | Optional | R42 [S], M |
| MoEngage Merlin Decisioning | Policy control group receives **randomised messages** from the policy | Not by default | R43 [S], M |
| Aampe | Recommends three groups: no-message, BAU, Aampe; prefers switchbacks and synthetic controls | Yes, recommended | R44 [S], M |
| Hightouch | "Every decision is measured against a control/holdout group" | Yes, stated | R46 [S], M |
| Adobe decisioning | "Holdout" = content **served randomly** during exploration | No | R49 [O], H |
| Adobe content experiment | 10% holdout, for that one action only; profiles can then get other messages | Leaks | R49 [O], H |
| Adobe journey-level holdout | "To measure the incremental lift — the true impact" | Yes, limited availability from 1 Sep 2026, by request | R49 [O], H |
| Adobe send-time optimisation | Random send-time control; two-proportion Z-test on opens or clicks | Not applicable; engagement only | R49 [O], H |

**What this means** (inference, H for the pattern). The mass-market defaults answer "is the model better than random?"
They do not answer "is messaging better than not messaging?" or "is the agent better than our current programme?"
Even independent geo-test tools disagree a lot. In Recast's simulation study, false-positive rates ran from 4.6% to 27.8%.
Tools missed a real 7.5% effect 38–91% of the time (R61 [O], H; Recast sells MMM).

### 2.3 Published results, graded

| Claim | Who measured | Design | Metric | Grade |
|---|---|---|---|---|
| Aampe: +65.3% push-open days; +0.3% (±0.24) conversion days | Aampe staff | 11 months, persistent 10% holdout vs BAU, 8.8M users, one app | Engagement large; conversion tiny | M (R45 [S]; confirmed 10 Oct, R175) |
| Aampe paper: "active human management generates the highest relative lift" | Aampe staff | Same trial | Relative lift | M (R45 [S]) |
| Haus: Meta lift ~19%; Advantage+ +9% at midpoint, −12% at end; 58% of brands better on manual | Haus (independent of Meta; sells testing) | 640 geo tests, Jan 2024–Jun 2025 | Business KPI | M (R36 [S]) |
| Caraway: platform overstated PMax by ~33% | Haus platform | Three-cell geo test | Revenue | M (R37 [S]) |
| Braze TEI: 457% ROI, $23.5M NPV | Forrester, **commissioned by Braze** | Modelled composite organisation | Modelled ROI | L as proof (R40 [S]) |
| Swiggy: in-app CTR 10% → 21%, 3× faster go-live, 6,000+ campaigns a month | MoEngage | Not stated | Engagement and operations | L as value (R31 [S]) |
| Mercari LLM e-mail titles: taps +23.6%, purchases −0.42% (not significant) | Mercari | Randomised, >1M lapsed users | Conversion | M (R101 [S]; confirmed 10 Oct) |
| Nextdoor: reranked LLM subject lines +1% sessions; plain output −6.5% | Nextdoor | A/B tests | Sessions | M (R100 [S]; confirmed 10 Oct) |
| PIE method: R² 0.88 for incremental conversions per dollar, against 0.19 for 7-day last-click | Academics (NBER w35044) | Built on Meta experiments | Incremental conversions | M (R73 [S]) |
| Hillstrom (our own): uplift did not beat risk ranking | Marketing AI | Public randomised data, replay | Conversion | H (R180 [O]) |

**The pattern** (inference, M). The more independent and the longer the measurement, the smaller the claimed effect.
Short windows flatter autonomous systems: the Haus reversal and Aampe's engagement–conversion gap both show it.

### 2.4 Why ad platforms reached autonomy and CRM lags

Note 01 §6 gives seven root causes; note 06 §1 dates the ad-platform history. The five that matter:

1. **Feedback density.** Ad auctions give billions of fast, cheap feedback events. CRM gives a few messages per
   customer per week. Adobe's own model floors show the gap: 100 displays and 5 clicks per offer in 14 days for its
   bandit. Its personalised model wants 1,000 impressions and 50–100 conversions per offer per week (R48 [O], H). Most
   mid-market CRM programmes cannot meet those floors (inference, M).
2. **Bounded, reversible downside.** An ad engine's worst case is wasted spend inside a cap. Even Meta's 2023
   overspend glitch was money, partly refunded (R62 [S], M). A CRM send cannot be recalled. In India it can cost the
   sender its registrations (R139 [S], M). So vendors let machines choose only among assets a human approved. DLT and
   WhatsApp templates formalise that rule (inference, M).
3. **Autonomy was imposed through defaults, and platforms grade their own homework.** Advantage+ and creative
   enhancements are on by default (R63 [S], M; R65 [O], H). Only 22% of advertisers chose most of their automation
   features deliberately (R1 [O], H). Independent tests found the self-reported value inflated (R36, R37 [S], M).
4. **Optimising what is quick to observe gives engagement, not money.** Adobe's bandit maximises click-through
   (R48 [O], H). Aampe's conversion lift is a fraction of its engagement lift (R45 [S], M).
5. **"Holdout" means different things,** so vendor claims are not comparable (§2.2).

**How advertisers won control back** (note 06 §1, dated):
- **Google conceded controls slowly, as fences, receipts and tests, never per-decision approval.** About 15–19 months
  passed before the first fences (account negatives Jan 2023; brand exclusions GA 29 Jun 2023) (R66 [S], M–H).
  About 3.5 years passed before full search-term and channel reporting (30 Apr 2025) (R66 [S], H). Incrementality
  tests now start from about $5,000, down from about $100,000 (R67 [S], M).
- **Controls were also the price of more automation.** In API v25, brand lists and text automation are bundled with
  AI Max. Brand Guidelines cannot be turned off once enabled (R64 [O], H). Text guidelines allow 25 hard term
  exclusions and 40 free-text "messaging restrictions" for generated copy (R64 [O], H).
- **Google delayed its forced DSA→AI Max migration to February 2027 "after advertiser feedback"** (R68 [S], M).
- **Meta moved the other way.** It removed the existing-customer budget cap from the UI and softened placement
  exclusions to allow 5% "limited spend" (R71, R69 [S], M–H). The API still carries both fields (R65 [O], H). It
  gave receipts and tests instead: segment reporting and incremental attribution (note 06 §1.2).
- **Distrust was public and sustained.** Trade press describes advertisers "being asked to trust a system that offered
  fewer levers" (R72 [S], M). No percentage survey of PMax trust was found (R184 [O], M; absence).

**The lesson for us** (inference, H). The durable, negotiated controls fall into four types. Build all four from day one:
- (a) **exclusion lists:** never-contact cohorts, claim and term exclusions;
- (b) **goal fences:** new-versus-existing customer goals, caps;
- (c) **per-decision receipts:** what was sent to whom, where, at what cost;
- (d) **cheap self-serve holdout tests.**

### 2.5 The white space

1. **Proof of incremental value in money, by default, from someone other than the vendor.** In paid ads neutral
   measurers exist. In owned channels we found nobody who keeps a persistent, customer-owned no-message holdout,
   reports rupees with an interval, and says "not proven" when it is not (R184 [O], M; absence of results).
2. **Long-horizon re-checks.** Nobody sells "re-check the lift at 90 days against the persistent holdout" (inference, M).
3. **India compliance joined to decisioning.** A marketing WhatsApp message costs about ₹0.86 before GST against
   about ₹0.115 for utility, about 7.5 times more (R159 [S], M). So "send fewer, prove each one" has direct rupee value.
   No vendor was found offering decisions that are India-compliance-aware and come with proof (R184 [O], M).
4. **The mid-market.** Data floors and opaque pricing leave mid-market Indian brands with drafting tools but no
   affordable decision engine that proves value (inference, M).
5. **Honest evidence labels.** Segment publishes "AI Nutrition Facts" about what its AI is (R60 [O], H). Nobody publishes
   whether it worked, graded by evidence strength. FTC "AI-washing" enforcement is a tailwind: by one law firm's count,
   13 cases since 2024 (R75 [S], M).

### 2.6 Threats

- **Braze is moving down-market** with Decisioning Studio Go (GA expected October 2026) and pre-send QA ("Agentic
  Standards") (R39 [S], M).
- **Adobe released journey-level holdouts** in limited availability on 1 September 2026 (R49 [O], H).
- **MoEngage bought Aampe's measurement team** with its RL engine (R47 [S], M).
- **If any of them ships a customer-owned, money-denominated, persistent holdout report, our gap narrows to neutrality
  alone** (inference, H). Neutrality must therefore be verifiable, not claimed (R177 [O], H).

---

## 3. What buyers want and fear

### 3.1 The autonomy ladder

| Level of authority | Share comfortable | Source |
|---|---|---|
| AI recommends, human decides | 90% | R1 [O], H (n=500, US/CA/UK/DE/AU/SG, May–Jun 2026) |
| AI prepares actions for approval | 89% | R1 [O], H |
| AI acts within rules humans set | **78%** | R1 [O], H |
| AI acts autonomously "when performance is proven" | **50%** | R1 [O], H |
| Trust in fully autonomous agents, cross-function | 27% in 2025, down from 43% | R2 [S], M |
| IT application leaders considering or deploying fully autonomous agents | 15% | R3 [S], M |
| CMOs running campaigns where several agents act autonomously | 8% | R4 [S], M |
| Would grant full autonomy, but only in low-risk workflows | Nearly 50% | R10 [S], M |

StackAdapt sells AI ad buying and its sample is programmatic advertising outside India. It is still the only
source where we saw question wording and sample (R1 [O]).

### 3.2 Root causes of distrust

Note 02 §9.2 ranks nine causes. These are well-argued hypotheses with partial evidence:

1. **Accountability without authority.** 17% say no one is clearly accountable for AI outcomes. Brands tend to blame
   the operator (R1 [O], H). Practitioners are about 34% comfortable with autonomy against about 58% for leaders
   (R1 [O], H; read from a chart). Autonomy moves the decision to a machine but leaves the blame with a person.
2. **Outcomes they cannot verify.** Only 22% (EMEA) are "very confident" judging whether AI makes the right campaign
   decisions. 62% report less manual time; only 27% report better ROAS (R1 [O], H). 67% of marketers now prioritise
   proving incremental ROI (R23 [S], M; verified 10 Oct). Braze's own review found 93% of marketing leaders think AI
   helps them understand customers. Only 53% of consumers feel brands predict their wants well (R27 [S], M).
3. **Incentive conflict.** 12% ignore platform AI advice as "timed to drive spend rather than performance" (R1 [O], H).
4. **Asymmetric brand risk.** Brand risk is the top barrier, for 63% (R1 [O], H).
5. **Context blindness from fragmented data.** 42% ignore AI advice as generic; 49% cite fragmented pipelines (R1 [O], H).
   In India, 78% of firms struggle with integration (R15 [S], M).
6. **Opacity.** Explanation is the lowest-rated quality of AI output, at 31% positive (R1 [O], H).
7. **Top-down pressure without an operating model.** 79% feel pressure to use more AI (R1 [O], H). Only 21% have mature
   agent governance (R8 [S], M). Only 30% of CMOs say they are ready to scale AI (R6 [S], M).
8. **Past disappointments.** 45% of martech leaders with agents in pilot or production say vendor agents miss the
   promised performance (R7 [S], M; verified 10 Oct). Gartner forecasts over 40% of agentic projects cancelled by
   end-2027 (R25 [S], M; a forecast).
9. **Job fear.** 18% of marketing leaders have cut roles because of automation (R26 [S], M).

**New evidence on errors and accountability** (note 06 §5):
- **Leaders act on bad AI advice.** About 92% of SVP/VP respondents acted on an AI recommendation later suspected
  wrong because of bad data. The figure is 78% for the C-suite and 41% for individual contributors (R18 [S], M;
  vendor-commissioned).
- **Agents stray.** 65% of 202 technology leaders say their agents have acted out of scope (R20 [S], M).
- **Ownership is unclear.** 40% say the Chief AI Officer should own agentic marketing; marketing executives prefer
  shared or marketing ownership (R19 [S], M).
- **Consumers blame the brand,** not the AI vendor (R21 [S], L–M).

### 3.3 What makes buyers grant autonomy

**What buyers say unlocks it** (R1 [O], H). No single item dominates; buyers want the bundle:
- confidence or predicted impact shown before a change: 37%;
- a limited-budget test first: 36%;
- a clear reason for each change: 35%;
- a full audit trail: 31%;
- easy undo: 31%.

**The six conditions for unattended running** (note 02 §9.5; each answers a root cause):
1. **Someone can still answer for it.** Written authority limits per job, a named owner and a log.
2. **The downside is fenced, not just monitored.** Hard caps the agent cannot cross, a kill switch and undo.
3. **Results are proven against a counterfactual the buyer controls.** A persistent random holdout the agent cannot touch.
4. **Incentives line up.** The vendor does not earn more when the agent sends more.
5. **The agent sees enough and says why.** It refuses to act when consent, margin or recent contacts are missing.
6. **Autonomy is earned per job, in stages,** starting where errors cost little.

"Proof unlocks autonomy" is our inference. It matches the ad-platform history (§2.4) and the authority ladder (§3.1).

### 3.4 Personas

| Persona | Wants | Fears | Will buy autonomy when |
|---|---|---|---|
| CMO | Growth from flat budgets; a board story | Brand incidents; agent-washing | Rupee results against a control; a brand-safety guarantee |
| CRM / retention head | Less manual journey-building; fewer wasted sends | Spam traced to them; DLT or consent breaches | Hard caps they set; preview; per-action log; one-click pause; works inside their CEP |
| Growth lead (D2C, quick commerce) | Speed; many experiments | Volatile results; spend that pleases the platform | Limited-budget test first; outcome-linked fees |
| CFO | Lower cost per retained customer; less discount leakage | Machine-set discounts; usage bills | Floors and ceilings enforced by the system; holdout-based value; capped or value-linked fees |
| Compliance / DPO | Evidence ready for audit | Sends without consent; DPDP penalties up to ₹250 crore | Deterministic consent, DND and DLT gates the agent cannot override; decision logs ≥1 year |
| CIO / security | One governed platform | New attack surface (74% of IT leaders) (R3 [S], M) | Runs in their account; role-based access; agent identity |

Source: note 02 §5 (R184 [O]); India-specific persona evidence is thin and mostly inference.

### 3.5 India specifics

- **Indian CMOs are less uncomfortable than global peers.** 46% of Indian CMOs, against 63% globally, are uncomfortable
  delegating high-stakes tasks to agents. On privacy the figures are 47% against 65% (R14 [S], M).
- **Indian CMOs are the most bullish on growth.** 53% expect 5–9% AI-driven topline growth, against 43% globally (R4 [S], M).
- **Deployment is real but marketing is not named.** 24% of Indian firms report active agentic deployment; the use
  cases named are operations, supply chain, service and finance (R15 [S], M).
- **Consumers are open.** 60% of Indian consumers are interested in a personal AI agent (R16 [S], M). 76% of Indians are
  willing to trust AI, against 46% globally (R17 [S], M).
- **The channel is saturated.** 96% of Indian WhatsApp users get unsolicited commercial messages daily (R33 [S], M;
  sample sizes conflict).

**Indian practitioner voices** (all anecdotal, all [S]):
- **Tata AIA's CMO** says consent must be granular across channels. An opt-out must reach every channel at once. The
  panel expects autonomy "within pre-set thresholds", with humans "recalibrating the system" (R28 [S], M).
- **BFSI panel:** marketing must own AI content, with human checks for accuracy and compliance (R28 [S], M).
- **D2C panel (boAt and others):** caution against over-reliance (R29 [S], M).
- **DataQuark's CEO:** 12–24 months before agentic AI is mainstream in Indian D2C (R30 [S], M).
- **Swiggy's growth lead:** marketers "used to schedule campaigns, and now they manage agents to run them" (R31 [S], M).
- **CleverTap's own page:** "humans review, approve, and launch" (R32 [S], M).
- **MoEngage's CEO** frames the buyer's question as "where do agents actually belong in my stack, and where do I need to
  maintain control?" (R35 [S], M).

**The pattern** (inference, M). Indian voices agree on four points:
- autonomy inside thresholds marketing sets;
- marketing owns the content;
- consent and opt-out handled across all channels;
- humans keep revenue-critical retention decisions.

No telecom CRM leader interview was found. Do **not** cite the "Airtel 135 million next-best actions" figure; 135 million
was its 5G customer count (R184 [O], note 06 §2).

### 3.6 Pricing

| Vendor | Model | Price point | Source |
|---|---|---|---|
| Salesforce Agentforce | Per resolution (service) | Help Agent $2 per resolution, GA Jul 2026; Flex Credits ≈ $0.10 per action | R76 [S], M |
| HubSpot | Per outcome (service) | Customer Agent $0.50 per resolved conversation from 14 Apr 2026 | R77 [S], M |
| Intercom Fin | Per outcome | $0.99 per outcome, 50-outcome monthly minimum | R78 [S], M |
| Braze | Consumption | Action Credits are "the only way Braze sells" since 27 May 2026; no list price | R79 [S], M |
| MoEngage / Aampe | Consumption plus enterprise quote | AI Credits metered by model processing time; no list price | R80 [S], M |
| Netcore.ai | KPI-linked | No terms published | R34 [S], M |
| Jio Haptik (India SMB) | Per conversation bundle | ₹10,000 for 2,000 conversations | R81 [S], M |
| Klaviyo Composer | Credits → low flat fee | $20 a month plus $10 top-ups (23 Sep 2026) | R82 [S], L |
| Google, Meta ad agents | No agent fee; revenue is media spend | – | [M], M |

**Reading.**
- **Outcome pricing has spread only in service,** where a resolution is visible in one session and needs no
  counterfactual (inference, M).
- **No marketing agent with incremental-revenue pricing was found** (R184 [O], M; absence).
- **Marketing CEPs converged on consumption credits,** which reward more agent activity (R79, R80 [S], M).
- **Buyers prefer consumption pricing, 43%, to outcome pricing, 27%** (R22 [S], M).
- **Indian CEP contracts run about ₹2.5–15 lakh a year in the mid-market** and ₹15–40 lakh or more in enterprise
  (R183 [O]; underlying sources weak, L). An autonomy layer will be judged against that bill.

**Our recommendation** (inference). A platform fee, plus an optional fee on the **lower bound** of holdout-measured
incremental value. Never a fee per message or per action, which pays the agent to send more. This matches decision K11:
value-linked pricing is a renewal option, not the lead model (R177 [O], H).

---

## 4. The technology

### 4.1 What runs unattended today, and what they share

| System | What it decides unattended | How it learns | Source |
|---|---|---|---|
| Braze Decisioning Studio (ex-OfferFit) | Offer, channel, timing, message per customer | Contextual bandits / RL, one model per client | R134 [S], H on mechanism |
| Aampe → MoEngage | What, when, where, whether to send | RL with continuous parallel experiments | R47 [S], H on deal |
| Duolingo reminders | Which reminder per learner per day | "Sleeping, recovering" bandit; +0.5% DAU, +2% new-user retention | R123 [S], H |
| LinkedIn notifications | Whether, where, when to notify | Constrained optimisation; offline multi-objective RL with volume guardrails | R124 [S], M |
| Microsoft Decision Service → Azure Personalizer | Generic contextual decisions | Explore → log → learn → deploy; reward joined after a uniform delay | R125 [S], H; R105 [O], H |
| Meta ReAgent → Pearl | Recommendations, bidding, creative | Batch off-policy RL; counterfactual evaluation before deploying | R107 [O], H |
| GrowthBook bandits | Variation weights | Thompson sampling; at least 1% of traffic per arm | R102 [O], H |

**Five properties every published unattended engine shares** (inference from the table, H):
1. a fixed, pre-approved action set;
2. one reward per decision type;
3. **logged propensities:** the probability with which each action was chosen;
4. learning in batches, not from each new point;
5. an exploration floor that never reaches zero.

**The open-source platforms have thinned.** Microsoft retired Azure Personalizer on 1 October 2026 (R106 [S], H). Its
successor repository was last changed in November 2024 (R105 [O], H). ReAgent is archived (R107 [O], H). The Open Bandit
Pipeline was last changed in 2022 (R104 [O], H). **So own the small decision core; borrow algorithms, not platforms**
(inference, H).

### 4.2 Why each technique works, and when it breaks

| Technique | Why it works | How marketing breaks it | Best-known answer | Source |
|---|---|---|---|---|
| **Bandits with logged propensities** | Explore in proportion to uncertainty; regret grows slowly when rewards are stable | Delayed rewards, seasonality, few decisions per customer, fatigue | Exploration floor ≥1% per arm; discounting; champion/challenger on confidence sequences; pooled contextual models | R102, R103, R108 [O], H |
| **Batch learning** | Updates on matured data; avoids chasing noise | Updating faster than the outcome window | Fixed-window labels; update cadence equal to the conversion window | R102 [O], H |
| **Delayed and censored rewards** | – | "Not yet" read as "no" biases against recent arms; urgency copy pulls purchases forward and looks better early | Cohort weighting by exposure period; time-to-event models; a uniform reward delay | R102 [O], H; R126, R125 [S], M–H |
| **Off-policy evaluation (IPS, SNIPS, doubly robust)** | Reweighting logged outcomes by π/μ is unbiased if every action had a non-zero logged probability | Variance explodes as weights grow; no data can judge an action never tried (**support**); wrong propensities bias everything | Doubly robust estimation; clipping; effective-sample-size checks; refuse when support is missing | R103, R104 [O], H |
| **Safe policy improvement** | Deploy only if a one-sided lower bound on the gain is above zero | Many candidates on the same data; drift between certification and deployment | Alpha-spending; re-certify every cycle; "cannot tell" as an outcome | R103 [O], H; R182 [O] (M137) |
| **Uplift models** | Target persuadables, skip sleeping dogs | Noisy differences: at 5% base rate and 2,000 per arm, standard error ≈ 0.69 points | Gates: random treatment only; AUUC interval above zero; must beat risk ranking at equal budget | R121 [O], H; R182 [O], H |
| **Interference between campaigns** | – | Campaign A's holdout is treated by campaign B; per-campaign effects overstate the total | A universal holdout; one decision per customer per cycle; factorial channel arms | [M] standard theory, H; R182 [O], H |
| **Metric gaming** | – | Clicks → clickbait; N-day conversions → pull-forward; revenue → targeting sure buyers | Incremental, net reward; guardrails as non-inferiority constraints; long-running holdout | R127 [S], M; R102 [O], H |
| **Pacing** | Proportional-integral control spreads spend | Integral windup at a hard cap; pacing on late signals oscillates | Pace on spend and sends, clamp the integral | R133 [S], M; [M] standard theory, H |

**The design rules that follow** (note 03 §3; inference, H):
- **An unattended policy may only take actions inside the support of the logs it will be judged on.** So the mandate's
  action set equals the exploration set.
- **Every multi-action decision must be stochastic, with its propensity logged.** Today our offer choice is
  deterministic, which is fine at L2 and blocks L3 (R182 [O], H).
- **Never report a bandit's arm means as effects.** Measure only on rows assigned with fixed, known probabilities.
- **"Proven value" comes from the universal holdout,** not from a sum of per-campaign effects.
- **This is batch, per-cycle learning, not online per-user RL.** Plan K §1.3 rules out per-customer RL at our volumes.
  A stochastic choice with an explore slice keeps to that rule (R177 [O], H).

### 4.3 Where language models belong

**Errors compound.** If each step is right 95% of the time, ten steps succeed about 60% of the time ([M] arithmetic, H).
On tau-bench retail tasks, Claude 3.5 Sonnet fell from 0.692 on one try to 0.462 when all four tries had to succeed.
GPT-4o fell from 0.604 to 0.383 (R109 [O], H). The successor benchmark fixed over 75 flawed tasks; older results "are not
comparable" (R110 [O], H). MAST lists 14 multi-agent failure modes, including resends and missing stop conditions (R111 [O], H).

**So the LLM belongs in the outer loop, not the inner one** (inference, H):
- **Outer loop (days, L2 by default):** turning goals into draft mandates; drafting copy for registration; explaining
  results under claim checks. Plan K's planner and claim verifier already work this way (R182 [O], H).
- **Inner loop (per cycle, no LLM):** scoring, choice, arbitration, pacing, compliance and dispatch, all deterministic.
- **Few unverified LLM steps between deterministic checks:** one or two.
- **Exactly one component commits actions.** Cognition's April 2026 revision concluded multi-agent patterns work where
  "writes stay single-threaded" (R131 [S], M).
- **Checks that protect a side effect run before it, in the same path.** In the OpenAI Agents SDK, parallel guardrails can
  fire after tools already ran (R112 [O], H).

### 4.4 The hardest open problems

| Problem | Best-known answer | Still open | Our position |
|---|---|---|---|
| Long-term value vs short-term response | Long-running universal holdout; surrogate indices validated on past tests | Surrogates fail under discount pull-forward; no public marketing surrogate | Universal holdout built; surrogates not built |
| Fatigue | Recovering bandits (Duolingo); volume as a constraint (LinkedIn) | **No public causal fatigue model in production** | Caps exist; randomised frequency arms needed |
| Credit across channels | Randomise channel inside the explore slice; slate bandits | Joint channel sequences with delayed rewards | Per-channel contactability built |
| Cold start | Warm-start bandits; empirical-Bayes priors; global plus per-entity models | Borrowing across clients raises privacy and contract issues | Pooling built (off); new offers start at L2 |
| Safe exploration under regulation | Explore only inside consented, registered actions; small capped floors | No regulator guidance on algorithmic exploration in marketing | Compliance gate per customer × channel × purpose |
| Non-stationarity (festivals, competitors) | Exploration floors; discounting; confidence-sequence champion/challenger | Choosing the forgetting rate automatically | Drift checks built; drift should feed a breaker |
| Evaluating the agent itself | pass^k on fixed scenarios; shadow mode; replay | No marketing-agent benchmark with causal outcomes | pass^k harness built; shadow and replay needed |
| Accountability without a person in each loop | Signed mandates on a "Trusted Surface"; tamper-evident logs; a stop-to-safe-state duty | No standard for attesting individual agent actions | Signed hash-chained ledger built; mandates needed |

Source: note 03 §4 (R184 [O]), citing R102–R132.

### 4.5 Safety engineering

**Three tiers of stop** (practitioner consensus, R135 [S], L–M):
1. a rate limit keeps each action small;
2. a **circuit breaker** stops the agent when a threshold trips;
3. a **kill switch** is the human override. Resuming is an authorised, logged act.

EU AI Act Art. 14(4)(e) asks that high-risk systems can "come to a halt in a safe state" (R132 [S], H). Marketing
personalisation is probably not high-risk, but it is the right engineering bar (inference). Knight Capital lost about
$440M in about 45 minutes from a runaway loop with no breaker outside the logic (R99 [S], H).

**The safe state for a marketing envelope** (design, note 03 §5.4):
1. the dispatcher refuses new items at once;
2. queued items go only with a valid mandate and a passed compliance decision; others are cancelled where possible;
3. the holdout is untouched and measurement continues;
4. the policy reverts to the last certified version, or to business-as-usual where a customer expects a message;
5. the trip and the evidence go to the ledger, and a person must resume.

**Breaker inputs:** spend and send rates against pace; recipients against audience size; complaint, unsubscribe and
bounce rates; offer values against the ceiling; compliance-block rate; input freshness; send-log reconciliation; model
drift. Use robust baselines with minimum samples (design, note 03 §5.4).

**Provenance.**
- **Canonical form.** A2A v1.0 signs Agent Cards with JWS after RFC 8785 canonicalisation (R113 [O], H). Our ledger should
  adopt RFC 8785 exactly, with a versioned canonicaliser so old entries still verify (inference, H).
- **Mandates.** AP2 v0.2 (28 Apr 2026) defines "open mandates" for constraints set before autonomous execution. It renders
  them on a "Trusted Surface": "a secure, non-agentic interface" (R114 [O], H). Our approval screen must render the
  mandate from its canonical JSON, never from LLM text.
- **Policy versions.** Sigstore's model-transparency can sign each policy version (R117 [O], H).
- **Tracing.** OpenTelemetry's agent spans are still "Development" status, so the signed ledger stays the system of record
  (R116 [O], H).

### 4.6 Reference architecture: two loops and a single writer

```
OUTER LOOP (days; LLM-assisted; L2 by default)
  goal or warning card -> planner -> draft mandate or campaign proposal
  content: LLM drafts -> guardrails + claims library -> registered templates -> variant arms
  policy (default deny) -> signed approval of the mandate (Trusted Surface) -> ledger

INNER LOOP (per cycle or event; no LLM; L3 inside a mandate)
  events/files -> decision-state store -> holdout and explore (fixed, logged probabilities)
  -> policy runtime: value per action -> stochastic choice + propensity
  -> arbitration + pacing + pressure caps -> mandate check + compliance decision -> OUTBOX
  -> DISPATCHER (single writer; idempotent; token buckets) -> client's CEP or client's rails
  -> receipts -> fixed-window outcomes -> measure / certify -> learn
  -> monitors (harm alarm, guardrails, anomalies, drift, reconciliation) -> BREAKER -> safe state, demote to L2
  everything -> signed hash-chained ledger -> client receipts; verifier bundle
```

**New invariants for L3 envelopes** (note 03 §7.2; Plan K's existing invariants stay):

| # | Invariant | Enforced by |
|---|---|---|
| I3a | Every executed action cites a valid, unexpired, signed mandate and lies inside its envelope | The dispatcher re-checks each action |
| I3b | Holdout and explore membership come only from the holdout service | The dispatcher refuses any held-out key |
| I3c | A breaker trip reaches the safe state within one dispatcher cycle; only a person resumes | Breaker state machine; drilled |
| I3d | Every action records its propensity, policy-version hash and mandate id | Outbox schema; evaluation refuses rows without them |
| I3e | The dispatcher alone holds channel credentials | Scoped secrets; a test for adapter imports |
| I3f | The mandate shown to the signer is rendered by code from canonical JSON | The approval screen never shows LLM text as the mandate |

### 4.7 Channel constraints an unattended sender must respect

| Channel | Constraint | Source |
|---|---|---|
| Email (Gmail, Yahoo) | SPF, DKIM, DMARC; one-click unsubscribe honoured within 2 days; spam rate below 0.3%, target 0.1%; permanent rejections since Nov 2025 | R168 [S], M–H |
| Email (Apple Mail) | Mail Privacy Protection inflates opens: **never use opens as a reward** | R169 [S], H on mechanism |
| SMS India (DLT) | Registered header and template; ≤3 variables without justification; whitelisted URLs since 1 Oct 2024; variable pre-tags scrubbed from 18 Nov 2025 | R140 [S], M |
| WhatsApp | Approved templates; RED template paused 3h, 6h, then disabled; per-user marketing caps (error 131049); limits shared across the business portfolio since 7 Oct 2025 | R156, R157, R158 [S], M–H |
| Push (FCM) | 600k messages per minute per project by default | R136 [S], H |
| Braze API | Send endpoints share 250,000 requests per hour | R136 [S], H |
| CleverTap API | Concurrency limits: ≤15 concurrent uploads per account | R136 [S], H |

**Delivery engineering for the single writer** (design, H in pattern): a transactional outbox; an idempotency key per
(customer, envelope, arm, cycle); token buckets per channel; receipts and webhooks fed back to breakers; daily
reconciliation of outbox against channel send logs.

---

## 5. Law and liability (not legal advice)

**Every finding here rests on search summaries.** No primary legal text could be opened (note 04 §0). Indian telecom and
data-protection counsel must review the final design and contracts before any client runs autonomous sends.

### 5.1 Who is the sender?

**Autonomy does not change who the law names as the sender. It changes how much proof the sender needs** (note 04 §0;
inference, H). In India the brand stays the TCCCPR Principal Entity (PE) and the DPDP Data Fiduciary.

**Three things decide sender liability** (note 04 §10; inference, M):
1. **whose registrations and accounts carry the message:** the PE's headers and templates, the telemarketer chain, the
   WhatsApp Business Account, the sending domain (R173 [S], M–H);
2. **who determines purpose and means:** DPDP s.2(i) and GDPR Art. 4(7) (R144 [S], M);
3. **who initiates the transmission:** CAN-SPAM and TCPA (R161 [S], M).

Autonomy does not move the first. It can move the second and third towards us if the agent sets objectives or runs on
our rails (inference, M).

| Mode | What it is | Our legal role (inference) | Our recommendation |
|---|---|---|---|
| **S: Suggest** | Agent proposes; a client person approves each campaign | Processor and technology vendor | Today's model |
| **C: Client rails** | Agent sends through the client's DLT entity, WABA and email domain, under a signed mandate | Most likely processor; the client stays PE and fiduciary | **Default for autonomy** |
| **O: Own rails** | Agent sends through our DLT telemarketer registration, our WABA, our domain or IP pool | Registered telemarketer in every client's chain; WABA owner; co-sender; owner of shared complaint rates | **Do not build** |

**The joint-fiduciary edge.** Under DPDP s.8(1), the fiduciary is liable for its processor "irrespective of any agreement"
(R144 [S], H). If our agent sets the objective, picks new purposes, or reuses data across clients, we may become a joint
fiduciary (inference, L–M). The mandate exists partly to prevent that: the client defines purpose and the means envelope;
the agent optimises only inside it.

### 5.2 India: the rules that bite an autonomous sender

| Rule | What it says | Dates | Source |
|---|---|---|---|
| **TCCCPR Second Amendment** | • action from **5 complaints in 10 days**<br>• a first violation bars the sender's outgoing resources for 15 days<br>• repeats mean 1-year disconnection and blacklisting<br>• a 90-day bar on re-seeking consent after an opt-out | Notified 12 Feb 2025 | R137 [S], H |
| **Digital consent acquisition (DCA)** | Consent-seeking messages go through the 127xxx short code, naming purpose and brand. **An agent must never send consent-seeking messages outside DCA** | In force by sector | R141 [S], M–H |
| **TCCCPR Third Amendment** | A2P (automated) calls declared in advance or treated as spam; AI/ML spam detection; stricter template action; up to ₹0.05/min A2P termination charge | Notified 18 Sep 2026; most provisions ~18 Oct 2026; Regs 4, 6, 8(a), 18 ~17 Nov 2026 (computed) | R138 [S], H (notified); M (detail) |
| **DLT templates** | SMS must match a registered template; tagged variables; whitelisted URLs; failures rejected after a 60-day logging window | Pre-tags from 18 Nov 2025 | R140 [S], M |
| **Promotional time band** | 09:00–21:00 IST; Sunday coverage ambiguous | No change in the Third Amendment summaries | R142 [S], M |
| **Opt-out** | Promotional messages should carry an opt-out; the revocation SLA is **unverified**; "24 hours" is unsupported | – | R137 [S], M; L for SLA |
| **DPDP Act and Rules** | • the fiduciary must prove consent (s.6(10))<br>• withdrawal as easy as consent<br>• no legitimate-interest basis<br>• children: no targeted ads or behavioural monitoring (s.9(3)) | Consent managers from 13 Nov 2026; main duties 13 May 2027 | R145 [S], M–H |
| **DPDP compression proposal** | MeitY consulted on cutting the window from 18 to 12 months, making 13 Nov 2026 the full date | Consulted 22 Jan 2026; **not notified** as of Oct 2026 | R146 [S], M |
| **DPDP Rule 10** | Verifiable parental consent, including a DigiLocker token; exemptions in Rule 12 | Duties from 13 May 2027 | R147 [S], M |
| **RBI sales-practice directions** | • explicit consent before a sale<br>• bans on dark patterns and compulsory bundling<br>• refunds for mis-selling<br>• scope includes DSAs and agents<br>• consent records kept to 1 year after the relationship<br>• NBFC promotions only with prior approval | Issued 15 Jun 2026; effective **1 Jan 2027** | R148 [S], H (date); M (clauses) |
| **RBI FREE-AI** | Board AI policy; AI disclosure; audit (a committee report) | 13 Aug 2025 | R149 [S], M |
| **RB-IOS 2026** | Ombudsman compensation up to ₹33 lakh | From 1 Jul 2026 | R150 [S], M |
| **IRDAI advertising** | Insurers and intermediaries control ad content and method; senior approval | Master circular 19 Jun 2024 | R152 [S], M |
| **SEBI** | No association with unregistered advice or return claims; AI/ML guidelines still a consultation | 2024–25 | R151 [S], L–M |
| **ASCI AI labelling** | Final guidelines; fabricated endorsements, misleading results and non-consensual deepfakes banned even with a label | Released 29 Sep 2026; effective ~29 Dec 2026 | R153 [S], H |
| **CCPA misleading ads** | Applies to advertisers and agencies; up to ₹10 lakh, then ₹50 lakh | 2022 | R154 [S], M–H |
| **IT Rules (synthetic content)** | Labels plus provenance metadata; 3-hour takedown (intermediary duties) | In force 20 Feb 2026 | R155 [S], H |
| **WhatsApp (Meta policy)** | Opt-in naming the business; approved templates; quality pausing; per-user caps; marketing ≈ ₹0.8631 vs utility ≈ ₹0.115 per message; general-purpose AI assistants barred | AI terms from 15 Jan 2026 | R156–R160 [S], M–H |

**What this means for design** (note 04 §0; inference, H):
- **In India, SMS and WhatsApp autonomy can only be "autonomous selection", not "autonomous authorship".** The agent
  picks who, when, which registered template and which variable values. New copy needs a registration step.
- **Brand-level penalties can follow one bad run.** An over-sending loop can cut off the client's transactional reach too.
- **Enforcement is operational and fast:** suspension and disconnection, not litigated fines. The realistic worst case
  is **losing the rails** (note 04 §9.3; R86 [S], M). About 500 brands, 3,000+ headers and 40,000+ templates have been
  blacklisted since 2019 (R86 [S], M). By March 2025, about 1,150 entities had been blacklisted and 18.8 lakh numbers
  disconnected (R143 [S], M).
- **No guidance exists on randomised holdouts under DPDP.** Name "measuring the effect of our messages with randomised
  comparison groups" as a purpose, and get an opinion before calling holdouts "DPDP-safe" (R175 [O], H; decision K4, R177).

### 5.3 The next 12 months of dates

| Date | Event | Effect on us |
|---|---|---|
| ~18 Oct 2026 | TCCCPR Third Amendment mostly in force | No voice agent; keep A2P out of scope |
| 30 Oct 2026 | Amazon Pinpoint engagement features end (R176 [O], M) | The replacement window has closed |
| 13 Nov 2026 | DPDP consent-manager rules; possible full start if MeitY compresses | Plan consent-manager event intake; watch V5 |
| ~17 Nov 2026 | Third Amendment Regs 4, 6, 8(a), 18 (A2P declaration) | Voice stays out until declared per client |
| ~2 Dec 2026 | EU AI Act Art. 50(2) marking grace may end (R165 [S], L) | Only for EU users |
| ~29 Dec 2026 | ASCI AI labelling in effect | Content gate must apply labels |
| 1 Jan 2027 | RBI sales-practice directions in effect | BFSI promotional copy stays at L2 |
| 31 Jan 2027 | US TCPA revoke-all rule (revised; date disputed) (R163 [S], M) | US only |
| Feb 2027 | Google DSA→AI Max migration (R68 [S], M) | Context only |
| 13 May 2027 | DPDP main duties | Any L3 pilot runs under full DPDP |

### 5.4 EU and US, briefly

- **EU AI Act Art. 50** (chatbot disclosure; synthetic-content marking) applies from 2 Aug 2026; fines up to €15M or 3%
  (R165 [S], M–H). **Art. 5** bans exploiting vulnerabilities, with no intent requirement for some prohibitions
  (R166 [S], H). A learned policy that targets distress with urgency is the risk case (inference).
- **GDPR Art. 22** covers solely automated decisions with significant effects. Behavioural advertising is usually not
  significant; offer eligibility or personalised price can be (R167 [S], M). SCHUFA held that a vendor's score can
  itself be the "decision" (R164 [S], H).
- **CAN-SPAM** makes advertiser and sender liable; up to $53,088 per violating email; opt-outs within 10 business days
  (R161 [S], M).
- **TCPA:** an AI-generated voice is an "artificial voice" (R162 [S], H). The FCC settled with a carrier for $1M and
  imposed a $6M forfeiture over AI-voice robocalls; the carrier was liable too (R95 [S], H for the facts).
- **Enforcement targets the brand for its agents.** Italy's Garante fined Enel Energia €79,107,101 over sales agents'
  conduct; the Court of Rome upheld it on 18 Sep 2025 (R93 [S], H). The UK ICO issued PECR fines of £100k–£300k in 2026
  (R94 [S], H).

### 5.5 Incidents, 2023–2026 (India first)

| Date | Incident | Lesson | Source |
|---|---|---|---|
| Aug 2024 | Zomato bans AI dish images after "increased refunds and lower customer ratings" | Generated creative causes measurable harm | R83 [S], H |
| Aug 2025 | CCPA fines Rapido ₹10 lakh for "Guaranteed Auto" copy | Offer and claim copy carries direct penalty risk | R84 [S], H |
| Dec 2025 | CCPA fines Zepto ₹7 lakh for default-on add-ons (stayed) | Default selections are dark patterns | R85 [S], H |
| Oct 2023 | Kotak customers get "₹753.48 crore credited" SMS from a template glitch | Variable-filled templates fail at scale | R87 [S], M |
| Jul 2026 | Who Gives A Crap suspends its AI e-mail agent after it confirmed a wrong price | Agents amplify upstream content errors | R92 [S], M |
| Jun 2026 | BMW Toronto's agent offers ~CAD 7,000 over value; the dealer honoured it | The brand is bound by agent offers | R91 [S], M |
| Feb 2024 | Moffatt v Air Canada: an airline liable for its chatbot's invented policy | Claims need a source of truth | R97 [S], H |
| Jul 2025 | Replit's agent deletes a production database during a "freeze" | Enforce freezes in code, not prompts | R96 [S], H |
| May 2025 | Klarna rehires humans after "lower quality" AI support | Autonomy reversals happen at flagships | R89 [S], H |
| Apr 2025 | Cursor's support bot invents a login policy; cancellations reportedly followed | Agent statements become "policy" | R88 [S], M |
| 2025 | Anthropic's Project Vend: a shop-running agent is talked into discounts and sells below cost | Offer ceilings and margin floors belong in code | R98 [S], M |
| Dec 2025 | McDonald's Netherlands pulls its AI Christmas ad after three days | Human-launched AI creative still carries brand risk | R90 [S], H |
| 2023 | Meta overspend glitch; CPAs roughly tripled | Bounded spend is the ad safety net | R62 [S], M |

**Still no public case of an autonomous CRM agent mass-sending wrong or non-compliant messages,** in India or elsewhere
(R184 [O], M). Every documented failure is one-to-one service or human-launched AI creative. That is likely because CRM
sends are still human-launched (inference, M).

### 5.6 The Autonomy Mandate

**The idea.** Replace "a person approves each action" with "a person signs a bounded, expiring mandate". Code checks
every action against it. A breaker returns control on harm (note 03 §1; note 04 §4.3).

**What a mandate contains** (note 04 §4.3, note 03 §5.2):
- purposes and objective (set by the client);
- channels, and registered templates or approved creatives per channel;
- audience rule, maximum size, and excluded cohorts (minors, vulnerability flags, BFSI-restricted products);
- frequency and pressure caps; quiet hours (product default 10:00–20:00 IST, stricter than the legal band);
- spend per day and per period; offer-value ceiling and margin floor;
- the certified policy version, the registered test plan, and the holdout;
- escalation triggers; expiry date; signers.

**Why it works three ways** (inference, M):
- **Legally,** it is the documented instruction that keeps us a processor and shows the client set purpose and means.
- **Technically,** it is a flat set of bounds a pure function can check per action. It can later compile to Cedar for
  formal analysis (R122 [O], H).
- **Commercially,** it answers the top buyer fears: accountability (a named signer), fences, and receipts.

**Rules.** A mandate can only narrow global policy, never widen it ("forbid wins"). Widening is a new mandate. A
two-person rule applies above a budget threshold. No CEP clause addressing autonomous sending was found; the market norm
is open (note 04 §9.4).

### 5.7 Design requirements

From note 04 §7, condensed. **[India]** marks a hard Indian legal or platform driver.

| Area | Requirement |
|---|---|
| **Mandate and scope** | Signed, versioned mandate per client; every decision records its version. Levels per channel and use case. Selection, not authorship, for SMS and WhatsApp **[India]** |
| **Pre-send gate (fail closed)** | • purpose- and channel-specific DPDP consent, with ID and notice version **[India]**<br>• TCCCPR consent, DND and the 90-day cooldown **[India]**<br>• Meta opt-in<br>• minor and vulnerability exclusion<br>• DLT template, tags and URL whitelist **[India]**<br>• claim and dark-pattern linter<br>• quiet hours; caps across all campaigns<br>• WhatsApp 131049 back-off<br>• **re-check at dispatch, not only at planning** |
| **Breakers and escalation** | Trip on complaints well below 5 in 10 days, any WhatsApp pause, spam ≥0.08–0.1%, bounce or unsubscribe spikes. Escalate new creatives, offers above a threshold, BFSI product or price decisions, first sends to a new segment. Two-person rule for raising caps |
| **Opt-out and rights** | Unified suppression across every channel in minutes; grievance routing counted in breakers |
| **Transparency** | AI disclosure in conversations; per-decision explanation record; brand-specific assistant identity on WhatsApp |
| **Audit** | Append-only, hash-chained send ledger, kept ≥1 year; a continuous evidence pack per client; versioned change control with one-click rollback |
| **Learning safety** | No open-based rewards; no features that proxy vulnerability; no cross-client pooling without a separate purpose and contract |

### 5.8 Contracts and insurance

- **CEP contracts put consent and content liability on the customer.** Braze's MSA, MoEngage's AUP and CleverTap's terms
  all do this (R170 [S], M). That posture works because a person configures each campaign. An autonomous agent makes it
  harder to defend commercially (inference).
- **Recommended stack** (note 04 §4.3, design): a roles clause; the mandate as an instruction schedule; a DPDP s.8(2)
  processing agreement; consent warranties both ways; **fault-based indemnities** (client: data, consents, templates;
  us: acts outside the mandate); a platform-sanction clause with step-down to Suggest; audit rights; a BFSI addendum.
- **Insurance.** Affirmative AI liability cover exists, with limits up to $25M (R172 [S], M–L). Unsolicited-communication
  exclusions are standard and read broadly, so express cover is needed (R172 [S], M). Whether DPDP penalties are
  insurable in India is untested (note 04 §4.4).
- **Model vendor terms flow down.** Anthropic's usage policy requires human review and AI disclosure for high-risk
  consumer-facing uses (R171 [S], M).

---

## 6. Where we stand

### 6.1 Capability map

From note 05, a read-only survey of the repository on 11 Oct 2026 (R182 [O], H). B built; P partial; D designed only;
M missing. Most built safety features are **off by default** (`agentic.enabled`, `compliance.gate.enabled`, `mcp.enabled`,
pooling and the harm alarm are all false).

| Capability | State | Where |
|---|---|---|
| Propensity scoring, uplift (S/T/X, multi-arm), sleeping-dog suppression | B | `engine/stages/score.py`, `engine/uplift/` |
| Offer choice (deterministic best net value), channel choice, net value in ₹ | B | `engine/decide/` |
| Cross-use-case arbitration with contact caps | B (no schedule kind) | `engine/decide/arbitrate.py` |
| Persistent holdout and 0–10% explore slice with logged propensities | B | `engine/holdout/assign.py` |
| Doubly robust OPE (binary treat / not treat) | B | `engine/uplift/ope.py` |
| Safe-to-roll-out lower bound; pooling; harm alarm (warns only) | B (off) | `engine/measurement/` |
| Generative copy with guardrails; variants as measured arms | B (each template human-approved) | `engine/generative/`, `engine/decide/variant_arms.py` |
| Claim verifier; brand control | B; P (brand is a name string) | `engine/agentic/claims.py`, `configs/guardrails.yaml` |
| Default-deny policy (six rules); proposals; separation of duties | B | `engine/agentic/policy.py`, `engine/approvals.py` |
| Signed hash-chained ledger and receipts | B | `engine/agentic/ledger.py` |
| Campaign planner with one mandatory human interrupt | B (off; Analyst-triggered only) | `engine/agentic/planner.py` |
| MCP server, read and propose only | B (off) | `api/mcp/` |
| Pre-send compliance gate (TRAI/DLT, consent, WhatsApp opt-in, URLs) | B (off; fails open in places) | `engine/compliance/` |
| Scheduler and monthly loop (score → treat list → measure → learn) | B ("nothing is sent") | `engine/scheduling/`, `engine/measurement/cycle.py` |
| Send-log import and reconciliation | P (manual upload) | `engine/measurement/reconcile.py` |
| Activation Contract, destinations, release rule, scheduled export | **D** | Plan J layer design (R178) |
| Starter sender (SES, EUM SMS and WhatsApp, BSP) | **D** (decision only) | Plan J layer M120; positioning memo |
| Any sender; channel adapters | **M** | – |
| Mandates; autonomy levels; breakers that act; global halt API | **M** | – |
| Quiet hours; product-owned contact history; per-day throttle | **M** (frequency P) | – |
| Real-time events; low-latency decisions; triggered journeys | **M** | – |
| Stochastic multi-action choice; multi-action OPE | **M** | – |

**A naming warning.** The Plan J layer design numbers its milestones M90–M120. The product plan reuses the same numbers
for different, built work. "J_LAYER M94" (Activation Contract) is unbuilt; README's "M94" (campaign record) is done
(R178 [O], R180 [O], H). This report always writes "J_LAYER" for the design.

### 6.2 Our genuine advantages

1. **The counterfactual is built in, not optional.** A persistent holdout with logged propensities, never shrunk away,
   is exactly what the vendor defaults lack (§2.2; R182 [O], H).
2. **Honest labels.** Causal, Estimated and Not proven, with measured lower bounds as the only "proven" figure (R177 [O], H).
3. **The safety substrate for L3 exists:** default-deny policy, signed ledger, receipts, pass^k evaluation, a compliance
   gate, a harm alarm and a "safe to roll out" bound (R182 [O], H).
4. **India compliance as code:** TRAI Third Amendment consent rules (M131), DLT template checks and WhatsApp opt-in in
   the gate (R180 [O], H).
5. **Neutrality.** We do not sell sends, credits or media, so the incentive conflict buyers fear (12%) does not apply
   (R1 [O], H; inference).
6. **The honest Hillstrom record.** A system that says "uplift did not beat risk ranking here" is what the 45% of
   disappointed buyers have not been shown (R175 [O], H).

### 6.3 Safety issues found in today's product

The deep pass of note 05 found issues that matter little while a person reviews every list. **Each becomes a direct
failure once nobody does.** A fix branch, **M184, is in progress** for these findings. This report does not assume any
item is closed until M184 merges with tests.

| # | Finding | Where (as surveyed) | Why it matters for autonomy |
|---|---|---|---|
| S1 | Separation of duties is not enforced with sign-in off: every request holds every role | `engine/approvals.py` ~216–223; `policy.py` role rule | A mandate signer could be the proposer |
| S2 | No model-approved check on the list path: any named model version is honoured | `engine/stages/score.py` ~333–353; firing, planner, treat list | An unapproved model could drive unattended sends |
| S3 | With `governance.approval_required` false, a winning challenger is promoted with no person | `engine/stages/register.py` ~259–279 | Silent policy changes; breaks "certified policy version" |
| S4 | Planner EXECUTE builds the run's whole treat list, not the selected channel or planned contacts | `engine/agentic/planner.py` ~1322–1420 | What is checked differs from what would be sent |
| S5 | `treat_list_step` opens campaigns with no registered test plan and no treatment start | `engine/measurement/cycle.py` | No pre-registered proof for unattended cycles |
| S6 | The compliance gate fails open in places: no catalogue, no message text or no whitelist means "not checked" but allowed | `engine/compliance/handoff.py` ~249–310 | The core fence must fail closed |
| S7 | Copy approval has no separation of duties and is not on the ledger | `api/routes/generative.py` | Content provenance is unprovable |
| S8 | `frequency.cap` checks only the payload, hard-coded to 1; no history-based fatigue | `engine/agentic/policy.py` | Over-messaging across campaigns |
| S9 | Missing policy rules: model approved, quiet hours, spend over time, consent freshness | `engine/agentic/policy.py` | Mandate checks would have gaps |
| S10 | The harm alarm only warns; no global halt; kill switch is config flags only | `engine/measurement/harm.py`, `engine/scheduling/firing.py` | No safe state exists |

Source: note 05, "Deep pass additions" (R182 [O], H).

### 6.4 Honest limits

- **No client pilot and no client deployment.** All validation is synthetic or on public data (R176, R180 [O], H).
- **Hillstrom showed no gain from uplift or from Plan K's upgrades.** The risk model's ROC-AUC is 0.534. Uplift did not
  beat risk ranking. The targeted list beat sending nothing (+0.45 points off-policy, +0.60 in replay). It lost on
  conversions to sending everyone the men's e-mail: −0.24 points (−0.43 to −0.04) (R180 [O], H).
- **Plan K's re-run on Hillstrom changed nothing.** The run-score adjustment removed essentially no variance (3.82e-07).
  The rollout verdict was "cannot tell" (−24.2 to +24.1 conversions). Pooling had too few campaigns; the harm alarm did
  not apply (R180 [O], H).
- **The usability test is pending.** Plan M's five-person test (M181) has not been run (R181 [O], H).
- **The foundation-model challenger has no benchmark;** weights were unreachable (R180 [O], H).
- **Most autonomy-relevant features are off by default** and validated mostly on synthetic data (R182 [O], H).

---

## 7. The path

### 7.1 The target and the principles

**Target.** "Autonomous within a signed mandate": L3 per decision type, on the client's rails, proven against a
client-owned holdout. L4 later, for narrow portfolios. Never L5.

**Principles.**
1. **Earn each level per envelope, with evidence; demote automatically.**
2. **Language models never send.** A deterministic dispatcher acts, inside a mandate. This keeps decision K1's spirit.
3. **Client rails first.** Hand the decision to the client's CEP where it has one; dispatch through the client's own
   accounts where it does not; never our own rails.
4. **Govern other engines rather than rival them** where clients run Braze or MoEngage decisioning.
5. **Proof stays independent.** The client owns the holdout salt; the verifier can be run by a third party.
6. **Selection, not authorship,** for SMS and WhatsApp.

### 7.2 Phases and gates

Week 0 is 12 October 2026. Dates are the earliest each gate can pass; client cycles set the pace (inference).

| Phase | Weeks (earliest) | What gets built and done | Gate | Evidence needed to pass |
|---|---|---|---|---|
| **0. Make today safe; decide** | 0–4 | • M184 safety fixes<br>• decision record and mandate schema<br>• missing policy rules<br>• verification sprint on V1–V11<br>• legal and insurance briefs | **G0** (~8 Nov 2026) | • M184 merged; every S1–S10 item closed or signed off as accepted<br>• D1–D10 decided<br>• DPDP holdout and sender-role opinions commissioned<br>• a pilot candidate named, with its stack known |
| **1. One real cycle at L2** | 2–16 | • first deployment in the pilot's account<br>• Activation Contract<br>• destination and send-log SDK<br>• hand-off with the release rule<br>• send-log import and reconciliation | **G1** (~early Feb 2027) | • a real approved list handed off<br>• contamination <5% of holdout IDs; ≥70% of the treat list contacted (positioning checkpoints, R176)<br>• a Value Proof Pack on a real campaign<br>• usability test ≥4 of 5 per task (R181) |
| **2. Shadow autonomy (L3, no sends)** | 8–26 | • mandates<br>• levels and breakers<br>• stochastic policy runtime with logged propensities<br>• multi-action OPE<br>• shadow runner and failure-injection scenarios<br>• contact history, pressure and pacing | **G2** (~mid-Apr 2027) | • ≥2 consecutive shadow cycles with **zero** mandate violations<br>• shadow policy evaluated against the approved L2 list, effective sample size above the plan minimum<br>• pass^k = 1 on autonomy scenarios<br>• breaker drill reaches the safe state within one cycle<br>• counsel has reviewed the mandate and contract<br>• management approves D2 |
| **3. L3 pilot in a signed mandate, client rails** | 20–40 | • route A: mandate-gated scheduled hand-off to the client's CEP<br>• route B: dispatcher on the client's own accounts, one channel<br>• event intake and real-time suppression<br>• dispatch-time compliance<br>• evidence bundle and verifier | **G3** (~mid-Jul 2027) | • one quarter (≥3 cycles) of L3 with zero mandate violations<br>• no DLT notice, no WhatsApp template pause, spam rate <0.1%<br>• every demotion handled correctly<br>• universal-holdout lower bound >0 in rupees, **or** an honest "cannot tell" that keeps the envelope narrow<br>• the pilot CFO accepts the readout<br>• sender operations ≤0.5 person per client per month (R176) |
| **4. Govern other engines; widen** | 40–64 | • governor adapters for Braze Decisioning Studio and MoEngage Merlin/Aampe<br>• second channel<br>• second client | **G4** (~Jan 2028) | • two clients at L3 on two different CEPs or rails<br>• one external engine audited against our no-message holdout<br>• a second channel certified on two consecutive cycles |
| **5. L4 for narrow portfolios** | 64+ | • portfolio mandates<br>• independent verifier service with its own key<br>• claims library bound to template slots | **G5** (2028, evidence only) | • ≥1 clean L3 quarter<br>• portfolio lower bound >0 against the universal holdout<br>• verifier bundle passing every cycle<br>• incident drills<br>• a written oversight procedure |

**Kill and step-back triggers** (from the positioning memo and Plan K, R176, R177 [O], H):
- 2 of the first 5 CFOs discount a readout because "you sent it yourselves": stop at route A (hand-off only).
- Any client receives a DLT violation notice, or SES review is triggered on more than one client: freeze dispatch.
- pass^k below 100% after one fix round: pause promotion.
- A major platform ships an equivalent neutral audit first: re-plan Phase 4 as integration, not competition.

### 7.3 Milestones and estimates

IDs are provisional (A0–A26). Real milestone numbers are assigned on approval; M182–M184 are already in use. Estimates
are ours and unmeasured, except where the basis column cites the Plan J layer design (R178 [O]). "Eng" and "DS" are
person-weeks.

| ID | Milestone | Phase | Eng | DS | Basis | Depends on |
|---|---|---|---|---|---|---|
| A0 | M184 safety fixes (S1–S10 as scoped by that branch) | 0 | in progress | – | Its own branch | – |
| A1 | Autonomy decision record; mandate schema; holdout glossary (no-message, random-arm, BAU, synthetic control) | 0 | 1 | – | Ours | – |
| A2 | Policy rules M184 leaves open: model approved, consent freshness, quiet hours, spend over time; sign-in required for autonomy | 0 | 2 | – | Ours | A0 |
| A3 | Primary-source verification sprint (V1–V11) | 0 | 0 (1 analyst-week) | – | Ours | – |
| A4 | First deployment in the pilot's account | 1 | 2 (+ Minfy cloud team) | – | J_LAYER M93 | A0 |
| A5 | Activation Contract | 1 | 3.5 | – | J_LAYER M94 | A4 |
| A6 | Destination and send-log SDK | 1 | 4 | – | J_LAYER M97 | A5 |
| A7 | Hand-off with the release rule; erasure and withdrawal propagation | 1 | 5 | – | J_LAYER M100 | A5, A6 |
| A8 | Send-log importer and reconciliation for the pilot's CEP | 1 | 5.5 | 1.5 | J_LAYER M102, M103 (may shrink; partly built) | A6 |
| A9 | Mandate model, pure checker, signing, Trusted Surface screen, new ledger kinds, RFC 8785 canonicaliser | 2 | 4 | – | Ours | A1, A2 |
| A10 | Levels, promotion and demotion; breaker state machine; global halt API; harm alarm wired to the breaker | 2 | 3 | – | Ours | A9 |
| A11 | Policy runtime: stochastic offer × channel choice, logged propensities, exploration floor, signed policy versions | 2 | 3 | 2 | Ours | A9 |
| A12 | Multi-action doubly robust OPE with support and sample-size refusal; fixed-window labels; non-inferiority guardrails | 2 | 1 | 4 | Ours | A11 |
| A13 | Shadow runner, replay, failure injection (empty input, stale consent, duplicate rows, 429 storms, template rejection) and breaker drill | 2 | 3 | 0.5 | Ours | A10, A11 |
| A14 | Product-owned contact history, pressure state, pacing on spend and sends | 2 | 3 | 0.5 | Ours | A8 |
| A15 | Route A: mandate-gated scheduled hand-off to the client's CEP, with suppression deltas on a breaker trip | 3 | 7 | – | J_LAYER M113 (6) + 1 | A7, A10 |
| A16 | Route B: dispatcher on client rails (outbox, idempotency, token buckets, reconciliation) with an SES e-mail adapter | 3 | 5 | – | Ours; cf. starter 9–15 for four senders (R178) | A10, A14 |
| A17 | SMS (DLT, End User Messaging) and WhatsApp (client BSP) adapters: template-only, 131049 back-off | 3 | 4 | – | Ours | A16 |
| A18 | Event intake (opt-outs, complaints, receipts); decision-state store; suppression in minutes | 3 | 4 | – | Ours | A14 |
| A19 | Dispatch-time compliance: quiet hours, template status, complaint and spam telemetry into breakers | 3 | 3 | – | Ours | A16, A18 |
| A20 | Verifiable evidence bundle and stand-alone verifier | 3 | 3 | 0.5 | Plan K design M148 (R179) | A9 |
| A21 | Governor for Braze Decisioning Studio | 4 | 5 | 1 | Ours | A15, A20 |
| A22 | Governor for MoEngage Merlin / Aampe | 4 | 5 | 1 | Ours | A15, A20 |
| A23 | Second channel and second client mandates; optional Cedar compile | 4 | 2 | – | Ours | G3 |
| A24 | Portfolio mandate: create, retire and fund campaigns inside it | 5 | 4 | 1 | Ours | G4 |
| A25 | Independent verifier service with its own key | 5 | 3 | – | Ours | A20 |
| A26 | Claims library bound to template slots; automatic withdrawal of a guardrail-tripping variant | 5 | 3 | – | Ours | A9 |

**Totals** (person-weeks):

| Phase | Eng | DS | Total |
|---|---|---|---|
| 0 | 3 (+ M184) | 0 | 3 |
| 1 | 20 | 1.5 | 21.5 |
| 2 | 17 | 7 | 24 |
| 3 (A15 + A16–A20; route A alone saves 16) | 26 | 0.5 | 26.5 |
| **To the end of the L3 pilot** | **66** | **9** | **75** |
| 4 | 12 | 2 | 14 |
| 5 | 10 | 1 | 11 |

Phase 1 is Plan J layer work needed with or without autonomy. The autonomy-specific cost to G3 is about 53.5
person-weeks. With Plan K's assumed team of 3 engineers and 1 data scientist (R177), effort fits well inside the
calendar; client cycles are the constraint (inference). Phase 2's data science (7 weeks) needs the part-time second data
scientist of decision K10.

### 7.4 The first 90 days (12 Oct 2026 – 9 Jan 2027)

| When | Work | Owner | Done means |
|---|---|---|---|
| **Weeks 0–1** (to 25 Oct) | Merge M184; record S1–S10 status | Engineering | Tests green; each finding closed or accepted in a DEC |
| | Management review of this report; decisions D1–D10 | Founders, managers | Decisions recorded |
| | Commission three legal opinions: holdouts under DPDP; processor role under a mandate; whether TCCCPR's 15-day bar reaches registered PE headers | Founders | Firms engaged with dates |
| | Confirm voice and A2P stay out of scope as the Third Amendment starts (~18 Oct) | Product | Written in the decision record |
| **Weeks 1–3** (to 1 Nov) | Verification sprint V1–V11, starting with TRAI and DPDP texts | Analyst | Each claim confirmed, corrected or marked "do not use" |
| | Publish the holdout glossary and grade vendor claims against it | Strategy | One page, ready for sales |
| | A1: mandate schema draft; A2: missing policy rules | Engineering | Schema reviewed; rules tested |
| **Weeks 2–4** (to 8 Nov) | Prospect stack survey: CEP? decisioning engine? DLT entity? WABA? send-log export? | Minfy team | Ledger rows for ≥5 prospects |
| | Hand-off dry run with a hand-made contract file in the pilot's tool | Minfy + engineering | Load time and key-match rate recorded |
| | **Gate G0** | Managers | Evidence in §7.2 met |
| **Weeks 4–6** (to 22 Nov) | A4: first deployment in the pilot's account; holdout source-of-truth agreement; send-log clause | Engineering + Minfy cloud | Deployed; agreements signed |
| | Watch DPDP: consent-manager rules start 13 Nov; check for a MeitY compression notice | Strategy | Watch note filed |
| | Watch Third Amendment Regs 4, 6, 8(a), 18 (~17 Nov) | Strategy | Watch note filed |
| **Weeks 4–10** (to 20 Dec) | A5–A7: contract, SDK, hand-off with release rule | Engineering | Conformance suites green; a valid path delivers on test infrastructure |
| | Run Plan M's usability test (M181) | Product | Results recorded, whichever way they fall |
| | Mandate and contract template reviewed by counsel | Founders | Comments received |
| **Weeks 8–13** (to 9 Jan) | First approved list live at L2 (target ~week 10–11) | Minfy + engineering | Delivery receipt; treatment start recorded |
| | A8 begins: send-log import for the pilot's CEP | Engineering | First send log pulled |
| | A9 begins: mandate model and checker | Engineering | Pure checker with property tests |
| | ASCI labelling (~29 Dec) and RBI directions (1 Jan) reflected in the content gate and BFSI defaults | Engineering | Rules in configuration, dated |
| **Day 90 report** (9 Jan 2027) | To management: G0 evidence; list live or the reason not; legal opinions; usability result; V1–V11 outcomes; any change to this plan | Strategy | Report delivered |

### 7.5 Build versus partner

| Situation | What we do | Why |
|---|---|---|
| **Client runs Braze Decisioning Studio or MoEngage Merlin / Aampe** | **Govern their engine:**<br>• translate the mandate into their action bank and constraints<br>• insist on a no-message arm we own<br>• run our compliance gate on their candidates<br>• pause through their API on a breaker trip<br>• verify from their send logs | They own per-user decisioning and an installed base; we own the counterfactual (R41–R44 [S], M; inference) |
| **Client has a CEP without decisioning** (WebEngage, CleverTap without CleverAI, Netcore, SFMC) | **Route A:** our policy decides; mandate-gated scheduled hand-off to their CEP; their CEP sends | No channel credentials held; positioning holds (R176 [O], H) |
| **Client has only its own rails** (SES, an SMS aggregator, a WhatsApp BSP) | **Route B:** our dispatcher in the client's AWS account, on the client's DLT entity, WABA and domain | The client stays the sender (Mode C, §5.1) |
| **Client has nothing** | Not a target. Route to a CEP partner or a Minfy services contract | Running channels is a permanent operations job (R176 [O], H) |
| **Decision algorithms** | Build the small core (Thompson sampling or softmax with floors; doubly robust evaluation); Vowpal Wabbit optional for slates | Platforms are retired or dormant (§4.1) |
| **Policy engine** | Keep Python rules; compile mandates to Cedar only if a client asks for gateway enforcement | Cedar is analysable; AgentCore Policy uses it (R122 [O], H) |
| **Event backbone** | SQS or EventBridge on AWS; Kafka share groups where a client already runs Kafka | Queues for Kafka are production-ready in 4.2 (R118 [O], H) |
| **Insurance** | Buy affirmative AI cover with an unsolicited-communication carve-back before route B | Standard exclusions read broadly (R172 [S], M) |

### 7.6 What we will deliberately NOT build

- **An unbounded L5 agent** that sets goals, total budgets, audiences or content with no envelope.
- **Our own sending rails (Mode O):** no DLT telemarketer registration, no WABA of our own, no shared IP pools or domains.
- **LLM-authored SMS or WhatsApp text sent without registration.** Selection only.
- **Any LLM in the inner loop,** and any LLM that sends. LLMs draft, plan and explain under claim checks.
- **Online per-user reinforcement learning or real-time bandits.** Batch, per-cycle stochastic choice only.
- **Autonomous discount depth, new offers, total budget, price, eligibility or credit decisions.**
- **Voice agents or A2P calls,** until per-client declaration is solved and a client asks.
- **Consent-seeking messages** outside the Digital Consent Acquisition flow.
- **Journeys:** waits, branches, triggers and journey builders stay with the client's CEP (R178 [O], H).
- **Cross-client pooled training** without a separate lawful purpose and contract.
- **Open-based rewards,** synthetic personas as lift predictors, and LLM causal judgements.
- **Multi-agent writers.** One dispatcher commits actions.
- **Per-message or per-action pricing.**
- **The words "fully autonomous" in sales material.** Say "autonomous within your mandate" (decision K9; R75 [S], M).

### 7.7 Risks and mitigations

| Risk | Likelihood (inference) | Mitigation |
|---|---|---|
| **No pilot client signs soon,** so G1 slips and everything behind it | High | Phase 2 build runs on the demo library and synthetic data; only its gate needs real cycles |
| **Sending undermines neutrality** ("you marked your own homework") | Medium | Client-owned holdout salt; client rails; verifier a third party can run; step back to route A if CFOs discount readouts |
| **One bad unattended run costs the client its rails** | Low per run, severe | Breakers well below TRAI and WhatsApp triggers; template-only sends; re-check at dispatch; small first mandates |
| **Shadow autonomy shows no gain over the L2 list** (as Hillstrom showed for uplift) | Medium–high | "Cannot tell" is an outcome; the envelope stays narrow or stays at L2; we report it, as we did for Hillstrom |
| **Regulation reading is wrong** (V4–V6) | Medium | Rules in dated configuration; verification sprint; three legal opinions |
| **DPDP full duties start on 13 Nov 2026** if MeitY compresses | Low–medium | Consent-manager intake and per-send consent proof designed now |
| **Braze or MoEngage ships a neutral-looking holdout report first** | Medium | Ship the glossary, verifier bundle and governor adapters; position as their auditor |
| **Data scientist overloaded** in Phase 2 | High | Part-time second data scientist (K10) |
| **Mid-market volumes too thin** for per-envelope certification | High | Decide only where data supports it; Adobe's documented floors as a public benchmark (R48 [O], H) |
| **Agent misuse through MCP or prompt injection** | Medium | No write tools on MCP; the dispatcher is not a tool; Plan G's egress gate and canaries |

### 7.8 Decisions needed from management

| # | Decision | Recommended answer |
|---|---|---|
| **D1** | Is our target "autonomous within a signed mandate" (L3 per decision type), L4 later, never L5? | **Yes** |
| **D2** | May a deterministic dispatcher act inside a signed mandate, on the client's rails, after G2? This amends K1, DEC-1100, the Plan J layer's §7 and its no-SEND design-rule test | **Yes, from G2**; language models still never send |
| **D3** | Rails policy | **Client rails only;** never our own (Mode O) |
| **D4** | Who may sign a mandate; budget threshold for a second signer; longest mandate before re-certification | Client Admin plus our Approver; two signers above an agreed ₹ threshold; 90 days at most (to be set with the pilot) |
| **D5** | Legal and insurance | Commission the three opinions now; buy affirmative AI cover before route B |
| **D6** | Pricing for autonomy | Platform fee plus an optional fee on the measured lower bound; no per-message fees |
| **D7** | First pilot envelope | One client, one use case (win-back offer choice), one channel (e-mail), small audience and budget |
| **D8** | Staffing | 3 engineers and 1 data scientist, plus a part-time second data scientist for Phase 2 |
| **D9** | Posture towards Braze, MoEngage and CleverTap | Govern and audit their engines; seek partner API access; do not compete on decisioning |
| **D10** | External language | "Autonomous within your mandate"; never "fully autonomous"; no unmeasured lift figures |

---

## 8. Corrections to earlier research, and open questions

### 8.1 Corrections

Where inputs conflict, we prefer the opened or primary source, then the later correction (note 06 §7).

| # | Earlier statement (where) | Correction or conflict | Basis |
|---|---|---|---|
| 1 | Google AI Max: forced, "no opt-out", Sep 2026 (note 01) | DSA→AI Max auto-migration **delayed to Feb 2027**; assets and broad match still migrated Sep 2026; API v25 bundles brand lists with AI Max | R68 [S], M; R64 [O], H |
| 2 | Google "Ask Advisor" powers undocumented (note 01) | Ads Advisor "will ask for approval before taking action"; SEJ reports an agent acting without asking. **Unresolved** | R70 [S], M |
| 3 | Advertisers won "fences, receipts and tests" in general, including Meta caps (note 02) | **True for Google.** Meta removed fences (existing-customer cap in the UI; 5% limited spend) and gave receipts and tests instead | R65 [O], H; R69, R71 [S], M |
| 4 | Braze "$250–300k per use case" | Seen once in a search summary; **not confirmed**. Braze sells only through Action Credits since 27 May 2026 | R79 [S], L–M |
| 5 | Braze offers control "as an option" (earlier report) | Go's **default** is a random-option control, explicitly not a no-email group | R41 [S], M |
| 6 | MoEngage/Aampe measure with holdouts | MoEngage's control receives **randomised messages**; Aampe recommends no-message / BAU / agent arms | R43, R44 [S], M |
| 7 | "No named Indian deployments of agentic decisioning" (earlier notes, R174) | **Swiggy runs MoEngage + Aampe;** engagement metrics only | R31 [S], M |
| 8 | Adobe journey-level holdouts "on the roadmap" | **Limited availability from 1 Sep 2026;** Adobe's decisioning "holdout" is random-offer traffic | R49 [O], H |
| 9 | Who Gives A Crap incident: weak (note 01) | **Medium,** corroborated by two outlets | R92 [S], M |
| 10 | ASCI AI labelling "final status not found" (note 04) | **Final 29 Sep 2026; effective ~29 Dec 2026** | R153 [S], H |
| 11 | RBI final "16 Jun 2026", single report (note 04) | **Issued 15 Jun 2026; effective 1 Jan 2027** | R148 [S], H |
| 12 | Enel fine "about €79M, 2024" from memory (note 04) | **€79,107,101;** decision 8 Feb 2024; upheld 18 Sep 2025; basis is accountability for sales agents | R93 [S], H |
| 13 | Time band "some pages say 10:00" (note 04) | No 10:00 source found; **09:00–21:00**; keep the stricter product default | R142 [S], M |
| 14 | Opt-out within "24h" from memory (note 04) | **Unsupported;** design to immediate suppression | R137 [S], L |
| 15 | "1,150 entities blacklisted" (note 04) | Add **~500 principal entities (brands)**, 3,000+ headers and 40,000+ templates since 2019 | R86 [S], M |
| 16 | Validity "92% acted on bad AI advice" (note 02) | **92% is SVP/VP only;** C-suite 78%, individual contributors 41% | R18 [S], M |
| 17 | Third Amendment: "AI spam flags plus three complaints in ten days" (Plan K §1.1) vs Second Amendment's "5 complaints in 10 days" (note 04) | **Conflict, unresolved.** Both may apply to different triggers. Design breakers below both | R138, R137 [S]; V4 |
| 18 | "Inquiry-based messages allowed for seven days only" (Plan K §1.1; built as M131) | Note 06 reads TRAI's "7-day" wording as consent "for completing any ongoing transaction". **Conflict, unresolved;** check M131's rule against the Gazette | R137 [S], M; V4 |
| 19 | tau-bench: about 61% on one try, about 25% at eight (earlier report, secondary) | Opened README: GPT-4o retail 0.604 → 0.383 at pass^4; no pass^8 seen. **Prefer the opened figures** | R109 [O], H |
| 20 | OfferFit price "about $325M" (positioning memo) | Note 03 cites "about $303M adjusted" from aggregators. **Both [S]; unresolved** | R134 [S], M |
| 21 | DPDP compression floated "23 Jan 2026" (earlier report) | Note 06: consultation on 22 Jan 2026. Minor; **not notified** either way | R146 [S], M |
| 22 | Airtel "135 million next-best actions" | **Do not use;** 135 million was a 5G customer count | note 06 §2 |
| 23 | "Contextual bandits / per-user RL: avoid for now" (earlier report) | **Refined, not reversed.** Still no online per-user RL; L3 needs batch stochastic choice with logged propensities | §4.2 |
| 24 | Plan J layer "M94" etc. vs README "M94" | **Same numbers, different work.** Layer design items are unbuilt | §6.1 |
| 25 | Salesforce "34% run autonomous agents in production" (statistics sites) | **Do not use;** Salesforce's own figure is 13% moved to agents | R11 [S], M |

### 8.2 Open questions

1. What do the TCCCPR consolidated text and the Third Amendment Gazette say on complaint triggers, the opt-out SLA,
   Sunday coverage and the 7-day window? (V4)
2. Will MeitY notify a shortened DPDP timeline before 13 November 2026? (V5)
3. Is a randomised no-message holdout lawful and adequately noticed under DPDP? (K4; no guidance exists)
4. Does a mandate keep us a processor, or does choosing recipients and timing make us a joint fiduciary?
5. Does TCCCPR's 15-day bar apply to a registered PE's headers, or mainly to unregistered senders?
6. Will Braze and MoEngage give a third party enough API access to govern their engines (pause, action bank, no-message arm)?
7. What share of Indian mid-market B2C firms have no CEP? No data was found (R176 [O], H).
8. Can mid-market Indian volumes certify any envelope within a quarter? Hillstrom suggests thin data often says "cannot tell".
9. What is the safe state per use case: stop, or revert to business as usual?
10. Whose account holds the dispatcher's credentials in route B, and how does the client revoke them?
11. Is there a causal fatigue model anyone runs in production? None was found.
12. Do India-specific buyers' willingness-to-pay figures exist for autonomous agents? None was found.

---

## 9. Sources

Tags: **[O]** opened primary; **[S]** search-result summary only; **[M]** unverified memory (not listed; marked inline).
"Verified 10 Oct" means a second agent checked it against the primary source for the earlier report (R175).

**A. Buyers and surveys**
1. StackAdapt / NewtonX, "AI Delegation Gap" report, 2026 — https://s3.amazonaws.com/media.mediapost.com/uploads/StackAdapt_AI-Delegation-Gap-Report_2026.pdf — [O]
2. Capgemini Research Institute, press release, Jul 2025 — https://www.capgemini.com/news/press-releases/trust-and-human-ai-collaboration-set-to-define-the-next-era-of-agentic-ai-unlocking-450-billion-opportunity-by-2028/ — [S]
3. Gartner IT application leaders survey, Sep 2025, via CRN India and IT Brief — https://www.crn.in/?p=73300 ; https://itbrief.co.uk/story/survey-finds-slow-adoption-of-autonomous-ai-agents-in-enterprises ; https://idm.net.au/node/15402 — [S]
4. BCG CMO survey, 15 Jun 2026, and India cut — https://www.bcg.com/press/15june2026-cmos-ai-transforming-marketing-transform-function ; https://www.business-standard.com/technology/tech-news/transforming-the-terrain-india-s-marketing-space-ready-for-ai-revolution-126062100703_1.html — [S]
5. Gartner, marketing work automated 16% → 36% — https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-survey-reveals-marketing-leaders-expect-ai-automation-of-marketing-work-to-double-to-36-percent-by-2028 — [S]
6. Gartner 2026 CMO Spend Survey — https://www.gartner.com/en/newsroom/press-releases/2026-05-11-gartner-2026-cmo-spend-survey-finds-cmos-allocate-15-point-3-percent-of-marketing-budgets-to-ai-but-only-30-percent-are-ready-to-scale-ai-capabilities — [S]
7. Gartner, 45% of martech leaders, 29 Oct 2025 — https://www.gartner.com/en/newsroom/press-releases/2025-10-29-gartner-survey-finds-45-percent-of-martech-leaders-say-existing-vendor-offered-ai-agents-fail-to-meet-their-expectations-of-promised-business-performance — [S], verified 10 Oct
8. Deloitte, State of AI in the Enterprise 2026 — https://deloitte.com/us/en/insights/topics/emerging-technologies/ai-agents-scaling-faster.html — [S]
9. PwC AI Agent Survey, May 2025 — https://www.pwc.com/us/en/tech-effect/ai-analytics/ai-agent-survey.html — [S]
10. G2 AI Agents Insight Report, Oct 2025 — https://secure.businesswire.com/news/home/20251009111961/en/G2-Report-Reveals-AI-Agents-Are-Delivering-the-Growth-Generative-AI-Promised — [S]
11. Salesforce State of Marketing, 10th edition — https://www.salesforce.com/news/stories/state-of-marketing-2026/ — [S]
12. Forrester 2026 B2C predictions — https://secure.businesswire.com/news/home/20251028963453/en/Forresters-2026-B2C-Marketing-CX-Digital-Business-Predictions-One-Third-Of-Brands-Will-Erode-Customer-Trust-Through-Self-Service-AI — [S]
13. Forrester blog, "The odyssey ahead" — https://www.forrester.com/blogs/the-odyssey-ahead-b2c-marketers-prepare-now-for-agentic-ai — [S]
14. Dentsu Creative CMO report, India cut, Oct 2026 — https://medianews4u.com/94-of-indian-cmos-expect-ai-enabled-agency-workflows-to-become-the-norm-dentsu — [S], figure verified 10 Oct
15. EY-CII, "The AIdea of India: Outlook 2026" — https://www.ey.com/en_in/newsroom/2025/11/india-s-ai-shift-from-pilots-to-performance-47-percent-of-enterprises-have-multiple-ai-use-cases-live-in-production-ey-cii-report — [S]
16. Adobe 2026 AI and Digital Trends, India — https://news.adobe.com/en/apac/news/2026/06/ai-digital-trends-india — [S]
17. KPMG / University of Melbourne, Trust in AI 2025, India — https://kpmg.com/in/en/insights/2025/05/trust-attitudes-and-use-of-ai.html — [S]
18. Validity, State of CRM Data Management 2026 — https://aijourn.com/validity-releases-state-of-crm-data-management-in-2026-report-revealing-marketers-trust-in-their-data-hasnt-caught-up-with-their-ai-goals/ — [S]
19. Kana, "Agentic Divide" — https://lasvegassun.com/news/2026/jul/14/kana-announces-new-research-70-of-enterprises-alre/ — [S]
20. Cequence / EMA via DSG — https://dsg.co.za/market-insights/two-thirds-of-enterprises-have-seen-ai-agents-act-out-of-scope — [S]
21. Delight.ai, who is responsible for AI mistakes — https://delight.ai/blog/ai-agent/who-is-responsible-ai-mistakes — [S]
22. Futurum, AI pricing preferences 2025 and 2026 — https://futurumgroup.com/press-release/are-outcome-based-and-hybrid-ai-pricing-models-rewriting-the-vendor-playbook/ — [S]
23. TransUnion, measurement confidence, 2025 — https://newsroom.transunion.com/new-transunion-research-reveals-marketers-confidence-in-measurement-has-stalled/ — [S], verified 10 Oct
24. Gartner Hype Cycle for Digital Marketing 2026 — https://www.gartner.com/en/documents/8115797 ; https://www.marketscale.com/industries/marketing-tech/gartners-2026-digital-marketing-hype-cycle-frames-ai-spend-as-a-governance-problem-not-a-tooling-race — [S]
25. Gartner predictions (40% of agentic projects cancelled) via SD Times — https://sdtimes.com/agentic-ai/gartner-more-than-40-of-agentic-ai-projects-will-be-canceled-in-next-few-years/ — [S]
26. Gartner, marketing roles, 29 Sep 2026 — https://www.gartner.com/en/newsroom/press-releases/2026-09-29-gartner-predicts-ai-will-allow-the-majority-of-high-performing-marketing-teams-to-eliminate-traditional-bottom-rungs-of-the-corporate-ladder-by-2030 — [S]
27. Braze 2026 Customer Engagement Review — https://www.businesswire.com/news/home/20260224711754/en/The-2026-Braze-Customer-Engagement-Review-AI-Innovation-Meets-the-Trust-Plateau — [S]

**B. Indian practitioner voices**
28. exchange4media, Pitch BFSI Summit 2026 (Tata AIA; "where to draw the line") — https://www.exchange4media.com/marketing-news/the-future-of-bfsi-intelligent-trust-158555.html ; https://www.exchange4media.com/marketing-news/as-bfsi-embraces-ai-marketers-spell-out-where-to-draw-the-line-158527.html — [S]
29. exchange4media, D2C brands take AI beyond marketing — https://www.exchange4media.com/marketing-news/d2c-brands-take-ai-beyond-the-marketing-function-158258.html — [S]
30. exchange4media, From D2C to D2AI — https://www.exchange4media.com/digital-news/from-d2c-to-d2ai-will-agentic-ai-help-indias-brands-outrun-the-world-145141.html — [S]
31. MoEngage, Swiggy case study — https://www.moengage.com/casestudy/swiggy-scales-campaigns-with-moengage-and-aampe/ — [S]
32. CleverTap, CleverAI Agents page and blog — https://clevertap.com/ai/cleverai-agents/ ; https://clevertap.com/blog/meet-cleverai-agents-powering-the-next-chapter-of-customer-engagement/ — [S]
33. LocalCircles via Social Samosa, WhatsApp spam — https://www.socialsamosa.com/news-2/users-india-receive-whatsapp-spam-messages-daily-11077812 — [S]
34. Netcore.ai relaunch — https://www.tribuneindia.com/news/business/netcore-is-now-netcore-ai-becoming-the-first-agentic-marketing-platform-to-share-accountability-for-customer-growth/ ; https://www.renascence.io/news/9328/netcore-ai-rebrands-as-agentic-marketing-platform-with-outcome-based-model — [S]
35. MoEngage CEO, "North" tour; TechCrunch, 23 Jun 2026 — https://techcrunch.com/2026/06/23/indias-moengage-bets-marketings-future-on-millions-of-ai-agents/ — [S]

**C. Market and vendors**
36. Haus, Meta incrementality report and blog; WARC; MediaCat — https://www.haus.io/meta ; https://www.haus.io/blog/is-meta-incremental ; https://www.warc.com/content/feed/metas-advantage-drives-strong-short-term-incrementality-but-weakens-in-the-longer-term/en-GB/10833 ; https://mediacat.uk/marketers-best-metas-black-box-report-shows/ — [S]
37. Haus, PMax brand experiment (Caraway) — https://www.haus.io/blog/pmax-experiments-including-excluding-branded-search-terms — [S]
38. Braze Q2 FY2027 earnings release — https://s25.q4cdn.com/307167611/files/content_files/2026-07-31-Braze-Inc-Q2-27-Earnings-Release-FINAL.pdf — [S]
39. Braze Forge 2026 announcements; CMSWire recap — https://www.braze.com/resources/articles/forge-2026-braze-product-announcements ; https://www.cmswire.com/customer-experience/braze-forge-2026-recap-self-service-speed-and-autonomy-require-governance/ — [S]
40. Forrester TEI for Braze (commissioned) — https://tei.forrester.com/go/Braze/AIDecisioningSpotlight/?lang=en-us — [S]
41. Braze Decisioning Studio Go FAQ — https://braze.com/docs/user_guide/brazeai/decisioning_studio/decisioning_studio_go/faq/ — [S]
42. Braze Decisioning Studio product page, performance report and design-agents docs — https://www.braze.com/product/brazeai-decisioning-studio ; https://braze.com/docs/user_guide/brazeai/decisioning_studio/reporting/performance ; https://www.braze.com/docs/user_guide/brazeai/decisioning_studio/design_agents — [S]
43. MoEngage decisioning docs, how it works — https://moengage.com/docs/user-guide/decisioning/campaign-decisioning/how-it-works — [S]
44. Aampe knowledge base and blog on holdouts and synthetic controls — https://kb.aampe.com/user-guide/how-do-i-know/experiments/holdout-part2 ; https://aampe.com/blog/how-do-i-know-aampe-is-actually-working — [S]
45. Aampe field trial, arXiv 2604.08621 (and 2512.17462) — https://arxiv.org/abs/2604.08621 — [S], figures verified 10 Oct
46. Hightouch, What is AI decisioning — https://hightouch.com/blog/what-is-ai-decisioning-what-does-hightouch-do — [S]
47. MoEngage acquires Aampe (PR Newswire via Yahoo Finance; CMSWire) — https://finance.yahoo.com/technology/ai/articles/moengage-acquires-aampe-bring-1-020000491.html ; https://www.cmswire.com/customer-experience/moengage-buys-aampe-to-add-peruser-ai-agents/ — [S]
48. Adobe Journey Optimizer decisioning docs: auto-optimization, personalized optimization, AI features — https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/experience-decisioning/ranking/auto-optimization-model.md ; .../personalized-optimization-model.md ; .../help/using/start/ai-features.md — [O]
49. Adobe Journey Optimizer reports and experiments: campaign report, content experiment, release notes, channel report — https://github.com/AdobeDocs/journey-optimizer.en/blob/main/help/using/reports/campaign-global-report-cja-code.md ; .../content-management/content-experiment.md ; .../rn/release-notes.md ; .../reports/channel-report-cja.md — [O]
50. Adobe CX Enterprise: Coworker Campaigns overview, launch, Agent Orchestrator — https://github.com/AdobeDocs/cx-enterprise-ai.en/blob/main/help/coworker/campaigns/overview.md ; .../launch-manage-campaign.md ; .../help/agents/agent-orchestrator.md — [O]
51. Klaviyo Composer newsroom; Klaviyo Q2 2026 transcript — https://www.klaviyo.com/newsroom/composer ; https://s25.q4cdn.com/801988311/files/doc_financials/2026/q2/Q2-2026-Klaviyo-Transcript.pdf — [S]
52. Salesforce Connections 2026 takeaways — https://www.salesforce.com/blog/connections-conference-takeaways/ — [S]
53. Bloomreach Loomi marketing agent GA — https://www.bloomreach.com/en/news/2026/bloomreach-announces-the-general-availability-of-loomi-marketing-agent/ — [S]
54. Iterable Nova Agent (DestinationCRM) — https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Iterable-Unveils-Nova-Agent-and-AI-Innovations-174516.aspx — [S]
55. Optimove AI Decisioning Studio — https://finance.yahoo.com/sectors/technology/articles/optimove-launches-ai-decisioning-studio-171500454.html — [S]
56. Netcore comparison of WebEngage — https://netcore.ai/blog/webengage-vs-moengage/ — [S]
57. Insider One acquires Bluecore — https://www.bluecore.com/press/insider-one-acquires-bluecore/ — [S]
58. Movable Ink autonomous capabilities — https://www.streetinsider.com/PRNewswire/Movable+Ink+Unveils+Autonomous+Marketing+Capabilities/24945917.html — [S]
59. Hightouch Series D (PYMNTS) — https://www.pymnts.com/news/investment-tracker/2026/hightouch-valued-at-2-75-billion-as-ai-agents-transform-enterprise-marketing/ — [S]
60. Twilio Segment Predictions and Nutrition Facts docs — https://github.com/segmentio/segment-docs/blob/develop/src/unify/Traits/predictions/index.md ; .../predictions-nutrition-facts.md — [O]
61. Recast geolift simulation study; Meta GeoLift README — https://github.com/getrecast/geolift-simulation-study ; https://github.com/facebookincubator/GeoLift — [O]
62. Meta 2023 overspend glitch (Search Engine Land) — https://searchengineland.com/meta-advertising-overspending-glitch-refunds-425771 — [S]
63. Meta Advantage+ creative backlash (Shopifreaks; WebProNews) — https://www.shopifreaks.com/meta-advantage-is-forcing-unwanted-ai-creative-on-brands/ ; https://www.webpronews.com/metas-advantage-ai-creates-bizarre-ads-frustrating-marketers/ — [S]
64. Google Ads API v25 protos (`campaign.proto`, `lift_measurement_config.proto`, PMax placement view), googleapis/googleapis @525d4c8, 9 Oct 2026 — https://github.com/googleapis/googleapis — [O]
65. Meta Marketing API SDK v26.0 (`adset.py`, `adcreativefeaturesspec.py`), facebook-python-business-sdk @efd8423, 17 Sep 2026 — https://github.com/facebook/facebook-python-business-sdk — [O]
66. Google PMax controls: brand exclusions help page; channel performance reporting blog; 2025 PMax features blog — https://support.google.com/google-ads/answer/13729701 ; https://blog.google/products/ads-commerce/channel-performance-reporting-coming-to-performance-max/ ; https://blog.google/products/ads-commerce/new-performance-max-features-2025/ — [S]
67. Google incrementality tests from ~$5,000 (PPC Land) — https://ppc.land/google-lowers-incrementality-testing-threshold-to-5-000-for-advertisers/ — [S]
68. Google delays DSA→AI Max auto-migration (PPC Land) — https://ppc.land/google-delays-dsa-to-ai-max-automigration-to-february-2027/ — [S]
69. Meta, limited spend on excluded placements (developer blog) — https://developers.facebook.com/blog/post/2025/10/08/introducing-limited-spend-on-excluded-placements-in-marketing-api/ — [S]
70. Google Ads Advisor blog; Search Engine Journal — https://blog.google/products/ads-commerce/ads-advisor-google-ads/ ; https://www.searchenginejournal.com/googles-new-ai-tools-promise-faster-ads-but-raise-control-concerns/547753/ — [S]
71. Meta existing-customer cap removed (Madgicx) — https://madgicx.com/blog/advantage-shopping-campaigns-farewell-customer-budget-cap — [S]
72. Search Engine Land on advertiser trust, 2025 — https://searchengineland.com/google-meta-microsoft-ads-trust-456190 — [S]
73. Gordon, Moakler, Zettelmeyer, NBER w35044 — https://www.nber.org/papers/w35044 — [S]
74. MarketsandMarkets, enterprise agentic AI market (example of sizing) — https://www.marketsandmarkets.com/Market-Reports/enterprise-agentic-ai-market-219711254.html — [S]
75. DLA Piper on FTC AI-washing enforcement — https://www.dlapiper.com/insights/publications/2026/05/ftc-ai-washing-action-underscores-enforcement-in-business-to-business-context — [S]

**D. Pricing**
76. Salesforce Agentforce pricing (Salesforce Ben; eesel) — https://salesforceben.com/huge-agentforce-pricing-shift-salesforce-introduces-pay-per-resolution ; https://www.eesel.ai/blog/agentforce-pricing — [S]
77. HubSpot outcome pricing (MarTech.org) — https://martech.org/hubspot-moves-to-outcome-based-pricing-for-some-breeze-ai-agents/ — [S]
78. Intercom Fin pricing (Gleap) — https://www.gleap.io/blog/intercom-fin-ai-pricing-2026 — [S]
79. Braze Action Credits (Vectorshift Q1 FY27 summary) — https://vectorshift.ai/research/companies/braze/earnings/2027-q1-summary — [S]
80. MoEngage Merlin AI usage billing docs — https://moengage.com/docs/user-guide/settings/account/billing/merlin-ai-usage — [S]
81. Jio Haptik SMB agents (Analytics India Magazine) — https://analyticsindiamag.com/ai-news-updates/reliances-jio-haptik-rolls-out-ai-agents-for-small-businesses-at-%e2%82%b910000 — [S]
82. Klaviyo Composer pricing (UsagePricing) — https://www.usagepricing.com/blueprint/activity/klaviyo-2026-09-23-composer-credits-to-dollars — [S]

**E. Incidents and enforcement cases**
83. Zomato bans AI dish images (Business Today) — https://businesstoday.in/technology/news/story/zomato-to-ditch-ai-generated-food-images-after-customer-complaints-442158-2024-08-20 — [S]
84. CCPA fines Rapido (MediaNama) — https://www.medianama.com/2025/08/223-rapido-fined-rs-10-lakh-misleading-auto-ad/ — [S]
85. CCPA fines Zepto (Inc42) — https://inc42.com/buzz/ccpa-slaps-inr-7-lakh-penalty-on-zepto-over-dark-patterns/ — [S]
86. Government blacklisting totals (Fonearena) — https://www.fonearena.com/blog/415968/government-fines-violators-rs-110-crore-to-curb-pesky-calls.html/amp — [S]
87. Kotak SMS template glitch (TechStory) — https://techstory.in/kotak-mahindra-bank-says-rs-753-crore-credit-alert-caused-by-sms-glitch/ — [S]
88. Cursor support bot (AI Incident Database #1039) — https://incidentdatabase.ai/entities/cursor-ai-support-bot-(%22sam%22)/ — [S]
89. Klarna rehires humans (Fortune) — https://www.fortune.com/2025/05/09/klarna-ai-humans-return-on-investment — [S]
90. McDonald's Netherlands AI ad (Washington Times) — https://www.washingtontimes.com/news/2025/dec/11/mcdonalds-netherlands-pulls-ai-ad-parodied-christmas-song/ — [S]
91. BMW Toronto agent offer (OECD AI incident) — https://oecd.ai/en/incidents/2026-06-11-2ab4 — [S]
92. Who Gives A Crap suspends AI agent (SmartCompany) — https://www.smartcompany.com.au/retail/who-gives-a-crap-suspends-ai-agent-email-error-prices-would-double/ — [S]
93. Garante, Enel Energia decision — https://www.gpdp.it/garante/doc.jsp?ID=9988921 — [S]
94. UK ICO PECR fines, 2026 — https://ico.org.uk/about-the-ico/media-centre/news-and-blogs/2026/05/glasgow-based-energy-company-fined-160-000-for-making-unsolicited-marketing-calls/ — [S]
95. FCC, Lingo Telecom and Kramer AI robocalls (CyberScoop) — https://cyberscoop.com/telecom-behind-ai-powered-biden-robocall-agrees-to-1-million-fcc-fine/ — [S]
96. Replit agent deletes a database (Fortune) — https://fortune.com/2025/07/23/ai-coding-tool-replit-wiped-database-called-it-a-catastrophic-failure — [S]
97. Moffatt v Air Canada (Law360 Canada) — https://www.law360.ca/ca/articles/1804075 — [S]
98. Anthropic Project Vend (SmartCompany) — https://www.smartcompany.com.au/artificial-intelligence/anthropic-ai-small-retail-business-trial/ — [S]
99. Knight Capital, SEC filing — https://www.sec.gov/Archives/edgar/data/0001060749/000119312512341182/d392788d424b3.htm — [S]
100. Nextdoor LLM subject lines, arXiv 2312.12457 — https://arxiv.org/pdf/2312.12457 — [S], verified 10 Oct
101. Mercari LLM e-mail titles, arXiv 2508.20024 — https://arxiv.org/abs/2508.20024 — [S], verified 10 Oct

**F. Technology**
102. GrowthBook bandit docs (overview, contextual, technical) — https://github.com/growthbook/growthbook/blob/main/docs/bandits/overview.mdx — [O]
103. Vowpal Wabbit wiki (logged CB, Explore-Eval, Epsilon Decay, cb_dro, SquareCB, warm start, slates) — https://github.com/VowpalWabbit/vowpal_wabbit/wiki — [O]
104. Open Bandit Pipeline README — https://github.com/st-tech/zr-obp — [O]
105. microsoft/learning-loop README — https://github.com/microsoft/learning-loop — [O]
106. Azure Personalizer retirement notice — https://azure.microsoft.com/updates/ai-services-personalizer-will-be-retired-on-1-october-2026 — [S]
107. Meta ReAgent and Pearl READMEs — https://github.com/facebookresearch/ReAgent ; https://github.com/facebookresearch/Pearl — [O]
108. LinkedIn lambda-learner README — https://github.com/linkedin/lambda-learner — [O]
109. tau-bench README — https://github.com/sierra-research/tau-bench — [O]
110. tau2/tau3-bench README — https://github.com/sierra-research/tau2-bench — [O]
111. MAST failure taxonomy — https://github.com/multi-agent-systems-failure-taxonomy/MAST — [O]
112. OpenAI Agents SDK, guardrails and human-in-the-loop docs — https://github.com/openai/openai-agents-python/blob/main/docs/guardrails.md — [O]
113. A2A specification and changelog — https://github.com/a2aproject/A2A — [O]
114. AP2 docs (v0.2, open and closed mandates, Trusted Surface) — https://github.com/google-agentic-commerce/AP2 — [O]
115. MCP specification 2026-07-28 changelog — https://github.com/modelcontextprotocol/modelcontextprotocol — [O]
116. OpenTelemetry GenAI semantic conventions — https://github.com/open-telemetry/semantic-conventions-genai — [O]
117. Sigstore model-transparency — https://github.com/sigstore/model-transparency — [O]
118. Apache Kafka upgrade docs — https://github.com/apache/kafka/blob/trunk/docs/getting-started/upgrade.md — [O]
119. Feast docs (point-in-time joins, Label View) — https://github.com/feast-dev/feast — [O]
120. EconML reference — https://github.com/py-why/EconML — [O]
121. Criteo large-scale uplift benchmark README; CausalML README — https://github.com/criteo-research/large-scale-ITE-UM-benchmark ; https://github.com/uber/causalml — [O]
122. Cedar README; Bedrock AgentCore SDK README — https://github.com/cedar-policy/cedar ; https://github.com/aws/bedrock-agentcore-sdk-python — [O]
123. Yancey and Settles, Duolingo, KDD 2020 — https://research.Duolingo.com/papers/yancey.kdd20.pdf — [S]
124. LinkedIn notifications, arXiv 2207.03029 — https://arxiv.org/pdf/2207.03029 — [S]
125. Microsoft Decision Service, arXiv 1606.03966 — https://arxiv.org/pdf/1606.03966 — [S]
126. Gigli and Stella, UAI 2024 — https://proceedings.mlr.press/v244/gigli24a.html — [S]
127. Spotify, risk-aware product decisions — https://engineering.atspotify.com/2024/3/risk-aware-product-decisions-in-a-b-tests-with-multiple-metrics — [S]
128. Feng, McDonald and Zhang, levels of autonomy, arXiv 2506.12469 — https://arxiv.org/abs/2506.12469v1 — [S]
129. Mitchell et al., fully autonomous agents, arXiv 2502.02649 — https://arxiv.org/abs/2502.02649v3 — [S]
130. SAE J3016 summary — https://www.masstransitmag.com/technology/document/12327182/levels-of-driving-automation-as-defined-by-sae-international-standard-j3016 — [S]
131. Cognition, multi-agents, 2026 — https://cognition.com/blog/multi-agents-working — [S]
132. EU AI Act Art. 14 (AI Act Service Desk) — https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-14 — [S]
133. Snap pacing, arXiv 2509.25429 — https://www.arxiv.org/pdf/2509.25429 — [S]
134. Braze, What is OfferFit; deal aggregator — https://www.braze.com/resources/articles/what-is-offerfit ; https://vectorshift.ai/research/deals/braze-offerfit-2025 — [S]
135. Waxell, kill switch — https://waxell.ai/blog/kill-switch-ai-agent-problem — [S]
136. Channel API limits: SES quotas, FCM, Braze, CleverTap — https://docs.aws.amazon.com/ses/latest/DeveloperGuide/manage-sending-quotas-request-increase.html ; https://firebase.google.com/docs/cloud-messaging/throttling-and-quotas ; https://www.braze.com/docs/api/api_limits ; https://developer.clevertap.com/docs/api-request-limit — [S]

**G. Regulation**
137. TRAI PR 11/2025 (TCCCPR Second Amendment); Fonearena — https://cms.trai.gov.in/sites/default/files/2025-02/PR_No.11of2025.pdf ; https://www.fonearena.com/blog/446114/trai-tcccpr-2025-tougher-rules-spam-calls-sms.html — [S]
138. TRAI Third Amendment: PR 119/2026, Gazette, PIB, TeamLease summary — https://www.trai.gov.in/sites/default/files/2026-09/PR_No119of2026.pdf ; https://www.trai.gov.in/sites/default/files/2026-09/Regulation_18092026.pdf ; https://www.pib.gov.in/PressReleseDetailm.aspx?PRID=2311989&lang=1&reg=3 ; https://teamleaseregtech.com/updates/article/59840/telecom-commercial-communications-customer-preference-third-amendment-/ — [S]
139. TRAI PR 111/2026, Q1 FY2026-27 enforcement — https://www.trai.gov.in/sites/default/files/2026-08/PR_No111of2026.pdf — [S]
140. TRAI direction on header and template misuse (TeamLease RegTech) — https://teamleaseregtech.com/updates/article/49534/trai-issues-direction-to-curb-misuse-of-sms-headers-content-templates/ — [S]
141. TRAI PR 50/2023, digital consent acquisition — https://cms.trai.gov.in/sites/default/files/2024-08/PR_No.50of2023.pdf — [S]
142. Promotional time band (Exotel; MyOperator) — https://developer.exotel.com/docs/faqs/calling-hours — [S]
143. Medianama, 1,150 entities blacklisted — https://www.medianama.com/2025/03/223-1150-entities-blacklisted-18-8-lakh-numbers-disconnected-after-spam-regulation-amendments/ — [S]
144. DPDP Act s.8 and s.2 text (DPDP wiki; dpdpa.com) — https://dpdp.myndsolution.com/wiki/act/section-8/ ; https://www.dpdpa.com/dpdpa2023/chapter-1/section2.html — [S]
145. DPDP Rules timeline (MirvoLegal) — https://www.mirvolegal.com/insights/dpdp-rules-2025-compliance-timeline/ — [S]
146. MeitY compression consultation (Storyboard18; S.S. Rana) — https://www.storyboard18.com/amp/digital/meity-seeks-industry-views-on-fast-tracking-dpdp-act-rollout-proposes-12-month-compliance-timeline-88332.htm — [S]
147. DPDP Rule 10 (SCC Online; dpdpa.com) — https://www.scconline.com/blog/post/2025/12/26/digital-personal-data-protection-rules-2025-key-highlights/ ; https://dpdpa.com/dpdparules/rule10.html — [S]
148. RBI Responsible Business Conduct (Second Amendment) Directions 2026 (Khaitan; Vinod Kothari; KSK) — https://www.khaitanco.com/sites/default/files/2026-07/Ergo_RBI%20Amendment%20Directions_NBFCs_7%20July%202026.pdf ; https://vinodkothari.com/wp-content/uploads/2026/06/From-Consent-to-Compensation_-RBIs-Directions-for-REs-on-Sales-Practices.pdf — [S]
149. RBI FREE-AI framework (S.S. Rana) — https://ssrana.in/articles/the-free-ai-framework-regulating-ai-in-financial-sector/ — [S]
150. RB-IOS 2026 (Outlook Money) — https://www.outlookmoney.com/banking/rbi-ombudsman-scheme-2026-customers-can-seek-compensation-of-up-to-rs-33-lakh — [S]
151. SEBI finfluencer rules and AI/ML consultation (Storyboard18; Taxmann) — https://www.storyboard18.com/advertising/crackdown-on-finfluencers-sebi-bans-association-of-regulated-entities-with-unregistered-finfluencers-35588.htm ; https://www.taxmann.com/post/blog/sebi-proposes-ai-ml-governance-framework-for-securities-markets — [S]
152. IRDAI advertising master circular (Taxguru) — https://taxguru.in/corporate-law/irda-master-circular-insurance-advertisements.html — [S]
153. ASCI final AI labelling guidelines (exchange4media; Fox Mandal PDF) — https://www.exchange4media.com/advertising-news/asci-issues-guidelines-on-responsible-labelling-of-ai-generated-ad-content-158710.html ; https://foxmandal.in/wp-content/uploads/2026/09/Guidelines-for-Responsible-Labelling-of-Synthetically-Generated-Content-in-Advertising.pdf — [S]
154. CCPA misleading advertisements guidelines 2022 (Vikaspedia) — https://en.vikaspedia.in/social-welfare/social-awareness/consumer-education/guidelines-for-prevention-of-misleading-advertisements-and-endorsements-for-misleading-advertisements-2022 — [S]
155. IT Rules synthetic-content amendment (Hogan Lovells) — https://www.hoganlovells.com/en/publications/india-introduces-mandatory-labelling-for-ai-and-3hour-takedown-for-illegal-content — [S]
156. Meta, WhatsApp template pausing — https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-pausing — [S]
157. WhatsApp per-user marketing caps, error 131049 (MSG91; Clickatell) — https://msg91.com/help/whatsapp/whatsapp-error---this-message-was-not-delivered-to-maintain-healthy-ecosystem-engagement — [S]
158. WhatsApp portfolio messaging limits (8x8; respond.io) — https://cpaas.8x8.com/ph/blog/important-update-whatsapp-messaging-limits-changing-october-7-2025/ ; https://respond.io/help/whatsapp/whatsapp-messaging-limits — [S]
159. WhatsApp India pricing (MyOperator; AiSensy) — https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026 ; https://m.aisensy.com/blog/whatsapp-per-message-pricing-update-effective-january-1-2026/ — [S]
160. WhatsApp terms barring general-purpose AI providers (TechCrunch) — https://techcrunch.com/2025/10/18/whatssapp-changes-its-terms-to-bar-general-purpose-chatbots-from-its-platform — [S]
161. CAN-SPAM penalties and roles (Prospeo; Transcend) — https://prospeo.io/s/can-spam-penalties-per-email ; https://transcend.io/blog/can-spam-act — [S]
162. FCC ruling: TCPA covers AI voice (Hunton) — https://huntonak.com/privacy-and-information-security-law/fcc-issues-declaratory-ruling-that-tcpa-applies-to-ai-generated-voice-calls — [S]
163. TCPA revoke-all rule delay and revision (Burr & Forman; Hunton) — https://www.burr.com/telephone-consumer-protection-act/the-fcc-delays-effective-date-of-tcpa-revoke-all-rule-until-january-31-2027 — [S]
164. CJEU SCHUFA C-634/21 (A&O Shearman) — https://www.aoshearman.com/en/insights/ao-shearman-on-data/cjeu-rules-that-a-credit-score-constitutes-automated-decision-making-under-the-gdpr — [S]
165. EU AI Act Art. 50 and the Digital Omnibus (Baker Botts; Gibson Dunn) — https://www.bakerbotts.com/thought-leadership/publications/2026/september/eu-ai-act-article-50-transparency-obligations-go-live ; https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/ — [S]
166. EU AI Act Art. 5 prohibitions (Lewis Silkin) — https://www.lewissilkin.com/insights/2025/02/03/prohibited-practices-and-ai-literacy-requirements-of-eu-ai-act-now-applicable-102jytk — [S]
167. GDPR Art. 22 (ICO) and WP251 (Bird & Bird) — https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/automated-decision-making-and-profiling/whats-new-under-the-gdpr ; https://www.twobirds.com/insights/2017/global/article-29-working-party-guidelines-on-automated-decision-making-and-profiling — [S]
168. Gmail and Yahoo bulk-sender rules (Red Sift; GMass) — https://redsift.com/resources/blog/gmails-enforcement-ramps-up-what-bulk-senders-need-to-know ; https://www.gmass.co/blog/gmail-bulk-sender-guidelines/ — [S]
169. Apple Mail Privacy Protection (Litmus) — https://www.litmus.com/blog/apple-mail-privacy-protection-for-marketers/ — [S]
170. CEP terms: Braze MSA, MoEngage AUP, CleverTap ToS — https://www.braze.com/company/legal/terms ; https://www.moengage.com/acceptable-usage-policy-for-customers/ ; https://clevertap.com/terms-service/ — [S]
171. Anthropic usage policy update — https://www.anthropic.com/news/usage-policy-update — [S]
172. AI liability insurance (FinTech Global on Armilla); TCPA exclusions (Wiley) — https://fintech.global/2026/01/23/armilla-ai-raises-25m-to-expand-ai-liability-coverage ; https://wiley.law/alert-3082 — [S]
173. India sender registration and PE–TM chain (AWS End User Messaging; Tribune) — https://docs.aws.amazon.com/sms-voice/latest/userguide/registrations-sms-senderid-india-register.html ; https://www.tribuneindia.com/news/nation/trai-cracks-down-on-unauthorised-telemarketers-directs-telcos-to-stop-misuse-of-headers-message-templates-480421 — [S]

**H. Internal (opened in this repository)**
174. `docs/research/agentic_ai_2026_10_notes/` — the earlier study's notes, corrected in §8 — [O]
175. `docs/research/AGENTIC_AI_OPPORTUNITIES_2026_10.md` — earlier report and its 10 Oct verification table — [O]
176. `docs/research/POSITIONING_DECISION_2026_10.md` — positioning memo, checkpoints and starter gate — [O]
177. `docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md` — §1, decisions K1–K12, §7–§9 — [O]
178. `docs/plans/MARKETING_AI_PLAN_J_LAYER.md` — §3.3 release rule, M93, M94, M97, M100, M102, M103, M113, M120, §7 — [O]
179. `docs/plans/PLAN_K_TECHNICAL_DESIGN.md` — §10, M145–M148 — [O]
180. `README.md` (M110, M110a, M131–M145 rows) and `docs/LIBRARY.md` (Plan K re-run on Hillstrom, DEC-1413) — [O]
181. `docs/plans/MARKETING_AI_PLAN_M_SIMPLE_UI.md` — the M181 usability test — [O]
182. Repository code survey, 11 Oct 2026 (note 05): `engine/agentic/`, `engine/decide/`, `engine/uplift/`, `engine/measurement/`, `engine/holdout/`, `engine/compliance/`, `engine/scheduling/`, `configs/agentic/agentic.yaml` — [O]
183. `docs/research/MARKET_RESEARCH_2026_10.md` — Indian CEP price anchors (underlying sources weak) — [O]
184. This study's notes 01–06, `autonomous_agent_2026_10_notes/` — [O]; each note tags its own sources
