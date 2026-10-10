# Build the agent that checks agents

**Bottom line.** Marketing AI should not try to become one more "autonomous marketing agent". That market is crowded. Braze bought OfferFit, MoEngage bought Aampe, Netcore renamed itself Netcore.ai, and every engagement platform now ships an agent suite. Buyers are already disappointed: **45% of martech leaders running vendor agents say those agents miss the business performance promised**, and **67% of marketers now prioritise proving incremental ROI** (TransUnion). The best opportunity is a **governed agent layer ("Plan K") whose tools are the deterministic pieces Plan J already ships**. These are the persistent holdout, the test planner, uplift with the beats-risk fallback, net value, the consent ledger and DLT template rules, the measure/learn schedule and the Value Proof Pack. The agent itself is a thin planner and narrator, and it can never send. The highest-value additions are not agents at all:

- **Measurement upgrades** that are cheap, proven and fit existing code: ML-based variance reduction (MLRATE/CUPAC) beyond CUPED, empirical-Bayes shrinkage of campaign lifts, doubly robust off-policy evaluation with a high-confidence "safe to deploy" gate, and a one-sided harm monitor.
- **India compliance agents.** DPDP Rules 2025 and the TCCCPR Third Amendment (notified 18 September 2026) turn every promotional send into a check across consent, DCA, DLT and WhatsApp. A rules engine with an audit log handles that well.
- **An MCP server**, read and propose only, that lets other companies' marketing agents ask Marketing AI two questions: "is this worth doing, and is it allowed?" before they act, and "did it work?" afterwards.

Tabular foundation models are one configuration change away, because AutoGluon 1.6.3 is already pinned. AutoGluon 1.6's `extreme_quality` preset uses only commercially free models (Nori, TabICLv2, TabDPT-Turbo). TabPFN-2.5 and later are non-commercial, even for using their outputs in business decisions. They are worth a challenger slot, not a pivot. LLM data-prep agents are already largely built as Plan G's Guided setup. Generative copy is partly built but not yet measured as randomised arms. The honest Hillstrom result is an asset to protect: uplift did not beat risk ranking (risk AUC 0.534), and the chosen list lost on conversions to "men's email to everyone". No agentic feature should be sold as a fix for that.

**A note on evidence.** All six research notes behind this report were compiled from web-search result summaries. Direct page fetching was blocked in the research environment, except one MCP blog page. Every external figure is therefore secondary-source unless noted. Vendor-reported numbers are marked as such. The final section lists the claims that must be checked against primary sources before they reach buyers or the manager review. Statements about the repository come from reading the code and plans directly.

## Verification update (10 October 2026)

A second agent with full web access checked the highest-stakes claims against primary sources: press releases, the papers themselves, the MCP spec, PIB and the TRAI Gazette. This section records what changed. The text below has been corrected to match. Claims that pass did not check are marked "not yet verified" in the final table.

**Confirmed against the primary source:**
- Gartner's 45% and 81% (n=413, June–Aug 2025). Note that the 45% base is leaders *with agents in pilot or production*, not all 413.
- Gartner 2024's 52% and the CFO at 40%. The CEO was at 39%, so "the CFO is most sceptical" is a narrow lead.
- Gartner's >40% cancellation forecast and the ~130 real vendors. This is a prediction, backed by a webinar poll.
- Guardian agents at 10–15% by 2030. Gartner defines guardian agents as security and policy controls, not causal proof.
- Dentsu 2026: 46% of Indian CMOs vs 63% globally are uncomfortable delegating high-stakes tasks to agents "for reputational reasons" (n=1,950, 14 markets).
- Optimove's 70%. The sample was only 329 affluent US consumers, so it is not evidence about Indian customers.
- Etsy 7% vs 27%, MLRATE, DoorDash, Statsig's 1.1% vs 17.3%, Netflix's surrogate index, Booking's two papers, and Nextdoor's +1% / −6.5%.
- The MoEngage–Aampe acquisition (24 June 2026) and the Netcore.ai relaunch (30 July 2026).
- The MCP 2026-07-28 spec (stateless, Tasks, hardened authorisation).
- The DPDP timeline (not shortened) and the TRAI Third Amendment.

**Corrected:**

| Claim | Before | Now |
|---|---|---|
| TransUnion | "67.4% name incremental ROI their top challenge" | The press release says "**67% now prioritize incremental ROI**". The 67.4% and the "52% use incrementality testing" figures come from EMARKETER's write-up. |
| MIT NANDA's "95%" | "95% of pilots fail" | "**No measurable P&L impact**" in a convenience sample: 52 interviews, 153 survey responses and a review of 300+ public initiatives. Wharton's Kevin Werbach and Paul Roetzer of the Marketing AI Institute criticise it. Avoid it in decks. |
| BCG's "5% get value" | Current | **Out of date.** BCG's Applied AI Index 2026 (30 Sep 2026, 1,300+ leaders) reports **7.5% "future-built" and another 41% scaling**. |
| Gartner's guardian-agent "80%" statistic | Cited | **Not found**; dropped. |
| Braze/OfferFit | "Measures with a randomised trial against business-as-usual" | Braze offers **random control, holdout or business-as-usual as options**; the default is not stated. The "+92% / $16M" figure has no published method. The "$250–300k per use case" figure was not found. |
| Netcore.ai pricing | "Shared accountability" | Tied to "**agreed business KPIs**", with no incrementality method disclosed. |
| MLRATE | "Over 70%" | Over 70% against a plain difference in means, but **only about 19% beyond CUPED-style adjustment**. 19% is the number to expect against M102. |
| DoorDash CUPAC | "Shortened tests by over 25%" | Applies to **switchback** tests; DoorDash cites 10–20% sample savings elsewhere. |
| Netflix surrogate index | "Same ship decision 95% of the time" | Inferences ~95% consistent, but **recall for launch decisions only 65–79%**. |
| TabPFN | Older summary | TabPFN-2.5, 2.6, 3 and 3.5 weights are non-commercial, and the TabPFN-3 licence bars using **outputs** for "internal commercial decision-making". AutoGluon 1.6's `extreme_quality` preset (Nori, TabICLv2, TabDPT-Turbo) is "free for commercial use"; TabPFN-3 sits in a separate `noncommercial` preset. TabPFN-3's size limits conflict between sources (1M × 2,000 on its model card vs 500k × 160 in AutoGluon's table). |
| MoEngage MCP | "Nine read-only tools" | **Newer docs say it can build and validate drafts, create segments**, and read campaigns. SMS, WhatsApp and webhook campaigns stay read-only. |

**New facts that strengthen the thesis:**
1. **Aampe's own trial measures engagement, not money.** Its 11-month randomised trial on 8.8M users (arXiv 2604.08621, written by Aampe staff, one anonymised app) reports **+65.3% (±1.31) in days with push-driven app opens**. Days with conversions rose only **+0.3% (±0.24)**, and days with intent +0.3% (±0.23); the passive phase showed +0.2% (±0.27). So the agentic vendor MoEngage just bought shows large engagement lift and only a small conversion lift (+0.3%, about the size of its own error band). That is the gap Marketing AI's rupee-level proof addresses.
2. **Mercari's randomised LLM e-mail title test** (over 1M lapsed users) raised item taps +23.6%, but **buyer conversion moved −0.42% (not significant)**. "Most of our target metrics were not statistically significant."
3. **A neutral measurer is being absorbed.** Smartly signed a letter of intent to acquire INCRMNTAL (17 Mar 2026), moving an incrementality vendor inside an ad platform. The neutral "did it work?" seat is still unclaimed.
4. **TRAI's Third Amendment** was notified 18 Sep 2026 and takes effect **30 days after Gazette publication** (about late October 2026). Its main provisions:
   - Automated, robo and AI-voice (A2P) calls must be **pre-declared** to the telecom operator, or they are treated as spam.
   - AI/ML spam flags on a sender, **plus 3 complaints in 10 days**, trigger action.
   - Inquiry-based messages are allowed for **7 days** only.
   - Misused headers or templates are suspended **within 6 hours**.
   - Legacy consents count only if they were obtained **verifiably and are registered on the operators' digital platform**.
5. **DPDP has not been brought forward.** Rule 4 (consent managers) applies from about 13 Nov 2026. Rules 3, 5–16, 22 and 23 apply from about 13 May 2027. MeitY's compression proposal was floated on 23 Jan 2026 and has not been notified. No consent manager can register before Rule 4 starts.
6. **Hold-outs under DPDP.** No regulator or law-firm guidance on randomised hold-outs was found.
   - TRAI governs sending, so *not* contacting a group raises no telecom issue.
   - Under DPDP, using a held-out customer's data to measure campaign effect is processing for a purpose, so the notice should name it.
   - **Action:** add "measuring the effect of our messages using randomised comparison groups" to client notice templates and to the consent ledger's purposes now, and get an Indian law-firm opinion before calling hold-outs "DPDP-safe".

**What this changes in Plan K** (already reflected in the Plan K table below):
- **K5 (verification MCP server) moves up.** A thin, read-only prototype over the demo library (M110), with no client data, moves into Tier 1. Two reasons: MoEngage's MCP can now create segments and drafts, so a "check before you act" call is worth more; and a neutral measurer (INCRMNTAL) is being absorbed. Connecting it to a client still waits for the first integrated cycle.
- **K3 (compliance) gains Third Amendment checks and a hold-out purpose.** The checks are A2P call declaration, the 7-day inquiry window, legacy-consent registration status, header and template suspension alerts, and spam-flag monitoring. The purpose is a "measurement with comparison groups" consent purpose. The Third Amendment is effective about late October 2026, so this is the most time-sensitive item.
- **K8 (TFM challenger) narrows to AutoGluon's commercial-safe `extreme_quality` models.** TabPFN-2.5 and later are blocked unless a Prior Labs licence is bought.
- **Buyer messaging** leads with "rupee-level, interval-backed proof vs engagement lift", using the Aampe and Mercari results. Replace BCG's 5%, quote TransUnion as "67% prioritise", and drop the MIT 95% and the Gartner 80%.

## Proof, not autonomy, is what buyers lack

The demand signal is consistent across 2024–2026 surveys. Buyers want AI, but they do not trust what it claims.

- **Agents.** In a Gartner survey of 413 martech leaders, **45% of those with agents in pilot or production said vendor agents fell short of promised business performance**. Half said their own data and stack were not ready ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-10-29-gartner-survey-finds-45-percent-of-martech-leaders-say-existing-vendor-offered-ai-agents-fail-to-meet-their-expectations-of-promised-business-performance)).
- **Marketing's own credibility.** Only **52% of senior marketers can both prove marketing's value and receive credit**, and **40% name the CFO as the most sceptical executive** ([Business Wire](https://www.businesswire.com/news/home/20240918141146/en)).
- **Measurement.** TransUnion found **67% of marketers now prioritise incremental ROI** (n=196 US marketers, July 2025; EMARKETER's write-up gives 67.4%), and 60% say stakeholders question their metrics ([TransUnion](https://newsroom.transunion.com/new-transunion-research-reveals-marketers-confidence-in-measurement-has-stalled/)).
- **Cost of doubt.** Haus, an incrementality vendor, so the survey is vendor-sponsored, found **74% of leaders have killed or scaled back an idea for lack of measurement confidence** ([Haus](https://haus.io/pages/2026-marketing-decision-confidence-index)). Independent incrementality testing was the most trusted method (60%, against 40% for MMM) ([EMARKETER](https://www.emarketer.com/content/incrementality-testing-earns-marketers--top-trust)).
- **India.** Adoption is high but readiness is low. **Only 26% of Indian CMOs believe they have the talent to meet their goals, and only 23% have prepared teams for AI-agent shifts** ([IBM India](https://in.newsroom.ibm.com/2025-07-09-India-Marketing-Leaders-Under-Pressure-to-Deliver-Profitability,-See-AI-as-Key-Growth-Lever-IBM-CMO-Study-2025)).

The reliability evidence explains the disappointment.

- **Benchmarks.** On tau-bench retail tasks, GPT-4o passed about **61% of tasks on one attempt but only about 25% when it had to succeed eight times in a row** ([Automation Anywhere explainer](https://www.automationanywhere.com/company/blog/ai-agent-benchmarks)). Salesforce's CRMArena-Pro found leading agents at about **58% single-turn and 35% multi-turn success** on CRM tasks, with near-zero built-in awareness of confidentiality ([arXiv](https://arxiv.org/html/2505.18878v1)).
- **Forecasts.** Gartner forecasts that **over 40% of agentic AI projects will be cancelled by end-2027**. It also says only about 130 of the thousands of self-described agentic vendors are real ([Outlook Business](https://www.outlookbusiness.com/deeptech/artificial-intelligence/over-40-of-agentic-ai-projects-will-be-scrapped-by-2027-says-gartner)). Both are forecasts or opinions, not measurements, but they set the tone in every buyer meeting.

### Competitors already run controls, so neutrality is the edge

Saying "nobody measures" is no longer true, and the earlier positioning memo already dropped the claim.

- **OfferFit, now BrazeAI Decisioning Studio,** offers uplift "measured against a random control, holdout, or business-as-usual baseline", as options rather than a stated default ([Braze FAQ](https://www.braze.com/resources/articles/offerfit-ai-decisioning-webinar-questions)). Its public results are still headline figures with no published method, such as "92% higher conversion" and "$16 million in annualized value" ([Braze](https://www.braze.com/resources/articles/what-is-offerfit)).
- **Optimove** builds a control into every self-optimising campaign, then **shrinks the control as the campaign "proves itself"** ([Optimove](https://www.optimove.com/blog/ending-the-control-groups-leave-money-on-the-table-conversation)).
- **Aampe, now inside MoEngage after a June 2026 acquisition** ([Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/)), argues that classic holdouts decay and measures with switchbacks, synthetic controls and matching instead ([Aampe docs](https://docs.aampe.com/docs/control-groups)). Its own 11-month randomised trial on 8.8M users reports **+65.3% in days with push-driven app opens but only +0.3% (±0.24) in days with conversions** ([arXiv 2604.08621](https://arxiv.org/abs/2604.08621), vendor-authored). This is large engagement lift with only a small conversion lift (about the size of its own error band), which is the gap rupee-level proof fills.
- **Netcore.ai** markets seven agents with "shared accountability" pricing. The release ties fees to "agreed business KPIs rather than software utilisation alone" and discloses no incrementality method; a trade note asks the same question ([Tribune India](https://www.tribuneindia.com/news/business/netcore-is-now-netcore-ai-becoming-the-first-agentic-marketing-platform-to-share-accountability-for-customer-growth/); [Renascence](https://www.renascence.io/news/9328/netcore-ai-rebrands-as-agentic-marketing-platform-with-outcome-based-model)).

What none of them was found offering is the combination Plan J already has:

- a persistent randomised holdout that is never shrunk away;
- results in rupees with 95% ranges;
- an explicit Causal / Estimated / Not proven label;
- independence from the platform that sends the messages.

The research found no "neutral judge" of marketing-agent decisions. Ad verification (DoubleVerify's DV Neura) and MMM vendors (Recast) are becoming callable by agents over MCP ([DoubleVerify](https://doubleverify.com/company/newsroom/doubleverify-introduces-dv-neura-the-dynamic-ai-engine-powering-the-future-of-media-quality-and-effectiveness); [Recast](https://getrecast.com/mcp-is-here/)). However, DoubleVerify's agent also acts, so it marks its own homework. This gap rests on an absence of search results, not a confirmed market map. It is still the strongest reason for Plan K to be about **checking actions rather than taking them**.

## Seven bottlenecks remain after Plan J, and only three need an LLM

Plan J closed most of the "decide and prove" gaps the October market research listed. The table maps what is still open to the evidence and to Plan J's existing pieces.

| Bottleneck | Evidence (secondary) | What Plan J already covers | What is still open | Is an LLM agent the right answer? |
|---|---|---|---|---|
| Proving incremental value to finance | 67% prioritise incremental ROI ([TransUnion](https://newsroom.transunion.com/new-transunion-research-reveals-marketers-confidence-in-measurement-has-stalled/)) | Holdouts, `measure_campaign`, Value Proof Pack with labels, proven value to date (sum of lower bounds) | Small effects (0.5–3 points) give wide ranges; each campaign's lift is read alone, with no pooling across campaigns | **No.** Use statistics: MLRATE/CUPAC, empirical-Bayes pooling |
| Under-powered tests | Controls of 5–10% are often under-powered ([PPC Land](https://ppc.land/holdout-study/)) | M93 planner, test-plan registration, CUPED in replay | Variance reduction on sparse binary outcomes is probably modest | **No.** Use richer covariates and stratified assignment |
| Data readiness and talent | Half of martech leaders not stack-ready ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-10-29-gartner-survey-finds-45-percent-of-martech-leaders-say-existing-vendor-offered-ai-agents-fail-to-meet-their-expectations-of-promised-business-performance)); only 26% of Indian CMOs feel talent-ready ([IBM](https://in.newsroom.ibm.com/2025-07-09-India-Marketing-Leaders-Under-Pressure-to-Deliver-Profitability,-See-AI-as-Key-Growth-Lever-IBM-CMO-Study-2025)) | Plan G Guided setup (rules-first, LLM chat), Data Doctor, leak checks, recipes | Mapping a client's tables to the schema at integration; defining the outcome label | **Partly.** Suggest-and-confirm only |
| Compliance across systems (India) | DPDP Rules 2025, the TCCCPR 2026 amendment, DLT variable pre-tags (see India section) | Per-channel consent ledger, DLT template id rule, one-way SMS opt-out fix, consent gate on uplift runs | No pre-send check that joins DPDP consent, the DCA registry, DLT and WhatsApp; no legacy-consent remediation; no tracking of rule changes | **Yes.** A rules engine first, with an LLM only for template matching and drafting |
| Over-messaging and opt-outs | 70% unsubscribed from 3+ brands in three months ([Optimove](https://www.optimove.com/resources/downloads/optimove-insights-2025-consumer-marketing-fatigue-report), vendor) | Sleeping-dog suppression, M101 arbitration with contact caps | Contact caps are guesses; fatigue is not measured causally | **No.** Randomise contact counts within the global holdout |
| Frontline does not use scores | No survey found; NBA-equipped sellers 2.6× more likely to grow ([Gartner via Business Wire](https://www.businesswire.com/news/home/20260520536156/en/Gartner-Survey-Finds-Sales-Organizations-That-Provide-AI-Enabled-Next-Best-Actions-Are-2.6x-More-Likely-to-Achieve-Commercial-Growth)) | Business-language reasons (M98) | No agent card or lookup (out of scope in Plan J) | **Yes, narrowly.** Grounded narration of reasons with a claim check |
| Findings not acted on | 49% of B2C decision-makers say analytics do not translate into action (truncated snippet, [Forrester](https://www.forrester.com/blogs/author/brad_haag/)) | M105 warnings cards, M107 scheduled loop | Each card needs a human to work out the next step | **Yes.** A planner that turns a card into a drafted, approvable next step |

The pattern is clear. The proof bottlenecks are statistical, and an LLM adds risk there, not value. The workflow bottlenecks are where an agent earns its keep: compliance joins, setup, turning cards into actions, and narration.

Plan J already contains a **deterministic agent loop**. M107's schedule runs score → treat list → measure → learn and leaves only the approval to a person. Plan K therefore wraps a loop that already exists. It does not invent one.

## Where each technology fits, and how much of it exists already

| Technology | Maturity of evidence | Plan J piece it plugs into | Already partly built? | Verdict |
|---|---|---|---|---|
| MLRATE / CUPAC variance reduction | Peer-reviewed (Meta, NeurIPS 2021); industry-validated (DoorDash, Etsy) | M102 CUPED (`engine/measurement/continuous.py`), M93 planner, replay | **Yes:** plain CUPED ships | **Adopt** |
| Empirical-Bayes / hierarchical shrinkage | Established; local variants in 2025–26 preprints | M104 Proof Pack "Estimated" figures, M105 value to date, M106 learning | No | **Adopt** |
| Doubly robust OPE + high-confidence improvement | Mature theory; open libraries | M96 equal-budget comparison, `engine/uplift/ope.py`, J5 fallback | **Yes:** IPW OPE and beats-risk fallback | **Adopt** |
| One-sided sequential harm monitor | Mature (mSPRT in Optimizely, Statsig) | M94 "early look" rule, M105 backfire card | Partly: early looks give no verdict | **Adopt** |
| Incremental-profit ranking | Industry-validated (Booking, DoorDash) | M97 net value, M100 offer choice | **Yes:** substantially built | Refine only |
| Surrogate index for long-term value | Peer-reviewed; Netflix-validated | M106/M107 learn cycles | No | **Pilot**, always labelled Estimated |
| Synthetic control / GeoLift | Mature | M103 audit of non-randomised campaigns | Partly: audit labels them descriptive | Pilot later |
| Contextual bandits / per-user RL | Vendor-led; weak at low volume | — | No | **Avoid for now** |
| Tabular foundation models | Nature paper and NeurIPS benchmark; vendor-led at scale | Model search, M100 per-arm learners, M110 library | **Yes:** AutoGluon 1.6.3 pinned; TFMs not enabled | **Pilot** as a challenger |
| Causal PFNs | NeurIPS 2025 research; known biases | — | No | **Watch** |
| LLM data-prep agents | Benchmarks only; under 50% on hard tasks | Plan G Guided setup, M107 sources, M93 labels | **Yes:** Plan G is this pattern | Extend, suggest-only |
| LLM copy as randomised arms | One large RCT shows small gains; vendor claims large | `engine/generative/win_back.py` variants, M99 catalogue, M100 arms | **Partly:** variants A–D exist but are not randomised as measured arms | **Adopt** as arms |
| Grounded explanation and agent card | Lab studies; one large field study for assist | M98 reasons, root-cause module | Partly | Pilot |
| MCP server | De facto standard, neutral governance since Dec 2025 | Layer plan's "MCP server (later)"; M103 audit; Proof Pack | Planned, not built | **Adopt** (read and propose) |
| A2A | v1.0 reported March 2026; no marketing vendor found using it | — | No | **Watch** |
| India compliance automation | Rules notified; enforcement real | M99 per-channel ledger, DLT rule, M91 fixes, layer M119 DPDP pack | **Partly** | **Adopt**, deadline-driven |

### Causal ML upgrades are cheap, proven and slot into M96–M106

**Variance reduction.** CUPED is the right default, but its gain depends on how well a customer's past outcome predicts the next one. For sparse purchase or churn flags that link is weak.

- **Etsy** reported only **7% variance reduction from CUPED, but 27% on average from predicted control variates** (a CUPAC-style approach) ([Etsy Code as Craft](https://www.etsy.com/de/codeascraft/reducing-experiment-duration-with-predicted-control-variates)).
- **MLRATE** (Meta, NeurIPS 2021) uses cross-fitted ML predictions as the covariate. In A/A tests it cut variance by **over 70% against a plain difference in means and about 19% against single-covariate (CUPED-style) adjustment**. The 19% is the realistic gain over M102's CUPED. If the predictions turn out useless, it is asymptotically no worse than the plain difference ([NeurIPS](https://proceedings.neurips.cc/paper/2021/hash/488b084119a1c7a4950f00706ec7ea16-Abstract.html)).
- **DoorDash** reported that CUPAC shortened its *switchback* tests by **over 25%** at the same power, and cites 10–20% sample savings for ordinary tests ([DoorDash](https://careersatdoordash.com/?p=1650)).

The "no worse" guarantee matters for a product whose first principle is "nothing fabricated". The upgrade extends M102's covariate path from one pre-registered column to a pre-registered, cross-fitted prediction. The test-plan hash (M94) must cover the prediction recipe. Hillstrom, with its spend outcome and pre-period history, is the ready test bed. Re-run the library journey and report the variance reduction, whichever way it falls. Gains on sparse binary outcomes are likely in the single digits to low tens of percent, not Bing's 50%. Nobody has published numbers for CRM offers, so measure before promising anything.

**Pooling across campaigns.** Effects of 0.5–3 points measured with noise of the same size make each campaign's lift mostly noise, and the best-looking campaign's lift is biased upward (the winner's curse). Empirical-Bayes shrinkage toward a pooled prior corrects this. Recent preprints argue for **local pooling across comparable experiments only**, because global pooling is too aggressive when effects differ between segments ([arXiv 2511.21282](https://arxiv.org/html/2511.21282v1); [arXiv 2603.12867](https://arxiv.org/pdf/2603.12867)). In Plan J terms, shrunken estimates feed:

- the "Estimated" label in the Proof Pack, shown beside the raw randomised figure and never replacing it;
- M105's proven value to date;
- the prior that M106 learning uses.

**Off-policy evaluation and safe deployment.** Athey and Wager's doubly robust policy learning has strong guarantees on regret ([Econometrica](https://www.econometricsociety.org/publications/econometrica/2021/01/01/policy-learning-observational-data)). High-confidence policy improvement returns a new policy only when a lower bound on its value beats the baseline at a chosen confidence, Pr(V_new ≥ V_baseline) ≥ 1 − δ ([Thomas et al.](https://people.cs.umass.edu/%7Epthomas/papers/Thomas2015b.pdf)).

Plan J already has most of the inputs:

- randomised logs with known probabilities (the holdout plus the explore slice);
- IPW evaluation in `engine/uplift/ope.py`;
- J5's rule that the treat list falls back to risk ranking when uplift does not beat it.

The upgrade has three parts. Swap IPW for doubly robust evaluation. Express J5 as a high-confidence lower bound in rupees. Present "not certifiable at this sample size" as an outcome, not a failure. At Hillstrom's scale, expect "not certifiable" often. That is the honest result, and it is what the CFO-facing pack should say.

**Harm guardrail.** Statsig's A/A tests showed **naive peeking produced a 17.3% false-positive rate, against 1.1% under mSPRT** ([Statsig](https://statsig.com/blog/sequential-testing-on-statsig)). M94 already refuses to give a verdict on an early look. A one-sided sequential test **for harm only** lets the M107 loop flag a backfiring offer early without spending the power the final analysis needs.

**Long-term value.** Surrogate indices combine short-term signals into a predicted long-term outcome. Netflix found inferences from 14-day surrogates **about 95% consistent with the 63-day result** across 200 tests, but **recall for launch decisions was only 65–79%** ([Netflix via NSF PAR](https://par.nsf.gov//servlets/purl/10520733); [NBER](https://www.nber.org/papers/26463)). This could shorten M106's learning cycle. However, discount offers often break the surrogacy assumption, for example through stockpiling, so any surrogate figure must carry the Estimated label and be checked against the universal holdout.

**Uplift itself.** The literature backs the Hillstrom result rather than contradicting it:

- A 2024 benchmark found most deep uplift models **were not robust across test distributions** ([arXiv 2406.00335](https://arxiv.org/pdf/2406.00335)).
- An unverified 2026 single-author preprint found a plain S-learner with LightGBM had the best Qini on Criteo v2.1 ([arXiv 2604.06123](https://arxiv.org/abs/2604.06123)).
- Booking.com's ranking by incremental profit ([arXiv 2405.02183](https://arxiv.org/pdf/2405.02183)) and its budget-constrained allocation of promotions, framed as a knapsack problem ([arXiv 2008.06293](https://arxiv.org/pdf/2008.06293v1)), match M97 and M100, which are already built.

The lesson is to keep M96's beats-risk check as the gate, not to chase newer uplift architectures.

### Tabular foundation models are one config change away, but licences bite

**The evidence.**

- **For.** TabPFN v2 (Nature, January 2025) beat tuned baselines on datasets of up to 10,000 samples ([PubMed](https://pubmed.ncbi.nlm.nih.gov/39780007/)). TabArena (NeurIPS 2025) found **foundation models excel on smaller datasets while gradient-boosted trees stay competitive on practical ones** ([NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2025/hash/1697e3fb412da11dc9488249f9e7bbc9-Abstract-Datasets_and_Benchmarks_Track.html)).
- **Against, at scale.** An independent study of 261 datasets found CatBoost ranked first on the 18 large ones, where TabPFN v2 averaged 3.97 ([arXiv 2502.17361](https://arxiv.org/pdf/2502.17361)).

**What is already in the repository.** `pyproject.toml` pins **AutoGluon 1.6.3**. AutoGluon 1.4 added TabPFNv2, TabICL and Mitra in an `extreme` preset. That preset is limited to **30,000 samples or fewer** and works best on a GPU ([AutoGluon 1.4](https://auto.gluon.ai/1.4.0/whats_new/index.html)). Version 1.6 reportedly adds TabPFN-3, TabICLv2 and others ([AutoGluon 1.6](https://auto.gluon.ai/dev/whats_new/v1.6.0.html)). Today `configs/engine.yaml` maps the three strategies to `medium_quality`, `good_quality` and `best_quality`, and the uplift learners use `medium_quality`. **The models are installed but unused.**

**The licence trap.** **TabPFN-2.5, 2.6 and 3 weights are non-commercial** without a Prior Labs licence. TabPFNv2 weights (Apache 2.0 plus attribution) and Mitra (Apache 2.0) can be used commercially ([Prior Labs pricing](https://priorlabs.ai/pricing); [HF TabPFN-3 card](https://huggingface.co/Prior-Labs/tabpfn_3); [Mitra card](https://huggingface.co/autogluon/mitra-regressor-2)). The TabPFN-3 licence also bars using model **outputs** for "internal commercial decision-making", and TabPFN-3.5 is under the same terms. A client deployment that switched on the newest models would breach them. The verified good news: AutoGluon 1.6's `extreme_quality` preset uses only Nori, TabICLv2 and TabDPT-Turbo, all "free for commercial use"; TabPFN-3 sits in a separate `noncommercial` preset that needs a GPU. K8 should use only `extreme_quality`.

**The fit.** A TFM challenger is most plausible for:

- small clients of 20,000–50,000 customers;
- **per-arm outcome models in M100**, where data per offer arm is thin.

It is least plausible for the 200,000–500,000-row churn tables at the large end.

**The test.** Run it in the M110 library first, as an allow-listed, licence-checked challenger family. Hillstrom's weak risk signal (AUC 0.534) comes from a noisy outcome, so a TFM should not be expected to rescue it.

**Causal PFNs.** CausalPFN and Do-PFN estimate effects in one forward pass. Both assume ignorability, meaning no unmeasured confounding. Later work reports they shrink estimates toward zero and carry bias from their priors ([CausalPFN](https://arxiv.org/html/2506.07918v2); [arXiv 2603.12037](https://arxiv.org/pdf/2603.12037)). They are a research watch item, never a substitute for the randomised holdout.

### Data-prep agents: Plan G already built the right pattern

The research supports suggest-and-confirm, not autonomy.

- **Good enough to suggest.** LLM feature proposals (CAAFE) lifted mean ROC AUC from **0.798 to 0.822** on 14 public datasets ([arXiv 2305.03403](https://arxiv.org/abs/2305.03403v1)). Static slicing plus structured prompts improved leakage-detection F1 by **22% and 15%** over prior tools ([CiteDrive listing](https://www.citedrive.com/en/discovery/improving-data-leakage-detection-in-machine-learning-notebooks-through-static-slicing-and-structured-llm-prompts/)).
- **Not good enough to trust alone.** Google's DS-STAR reached only **45.24% on hard data-analysis tasks** ([Google Research](https://research.google/blog/ds-star-a-state-of-the-art-versatile-data-science-agent/)). Text-to-SQL is worse than it looks: an o1-preview agent solved **21.3% of Spider 2.0** tasks ([alphaxiv](https://www.alphaxiv.org/abs/2411.07763v2)), and an audit found **52.8–62.8% annotation error rates** in the benchmarks themselves ([VLDB 2026 audit](https://rmarcus.info/dbscholar/papers/14621)).

Marketing AI already embodies the right answer. Plan G's Guided setup (`docs/AGENTS.md`) works like this:

- Rules produce every suggestion; the LLM only chats.
- The helper never writes code or edits a file.
- Every number must be grounded (`numbers_grounded`).
- A default-deny egress gate masks personal data before anything reaches the model.
- "Ask your data" uses no SQL (DEC-1256), and M107 forbids free SQL in pulls.

Plan K should extend this pattern rather than rebuild it:

- **Schema mapping** for integration-phase sources and the Activation Contract: Magneto-style retrieval plus reranking, shown as top-k candidates with confidence ([arXiv 2412.08194](https://arxiv.org/html/2412.08194v1)).
- **Drafting M93 lapse-label definitions** with example customers shown.
- **An LLM leakage reviewer** that adds questions to, but never replaces, the deterministic `LEAKAGE_SUSPECTED` check.

Indian CRM exports with Hinglish or abbreviated column names are not covered by any benchmark found. Plan G's 22-file benchmark should gain such cases before any accuracy claim is made.

### Generative content earns its place only as a randomised arm

The strongest public randomised evidence is sobering.

- Nextdoor's A/B tests of LLM-written **e-mail subject lines** found the best generator, **reranked by a reward model, gave only about +1% sessions**. **Plain ChatGPT output gave −6.5%** against the rule-based baseline ([arXiv 2312.12457](https://arxiv.org/pdf/2312.12457)).
- Vendor claims are far larger and come without randomisation details, for example Persado's "up to 450% CTR lift" ([FinTech Futures](https://www.fintechfutures.com/m-a/persado-gets-five-year-deal-with-jp-morgan-chase)).
- Mercari's randomised test of LLM e-mail titles on over 1M lapsed users raised item taps by 23.6%, but **buyer conversion moved −0.42% (not significant)**: "most of our target metrics were not statistically significant" ([arXiv 2508.20024](https://arxiv.org/abs/2508.20024)).
- Synthetic personas matched about 79% of real soft-drink choices but were **systematically more positive and less varied** ([NIM](https://www.nim.org/en/publications/detail/leaving-insight-to-digital-twins)). LLM samples fail to reproduce the variance across subgroups ([arXiv 2502.16280](https://arxiv.org/html/2502.16280v1)).

**What exists.** Marketing AI already generates copy (`engine/generative/win_back.py`):

- up to four variants (A–D) per band or uplift segment and channel;
- guardrails, including the M91 rule that blocks "Reply STOP" for one-way sender IDs;
- copy written for persuadables only on uplift runs.

**What is missing.** I found no code that assigns variants at random and measures them as arms. That is the gap. Plan K should make approved variants **treatment levels in M100's multi-offer uplift**: randomised against the control template and the no-message holdout, measured by `measure_campaign` per arm, and reported in the Proof Pack.

**SMS.** Every SMS variant must be a pre-registered DLT template, which M99's catalogue already enforces via `dlt_template_id`. Generation therefore becomes "draft N candidates, screen them with guardrails, register the survivors, then randomise". Synthetic personas belong only in the screening step.

### Explanations and the frontline need grounding before scale

**Narratives.** LLM narratives of SHAP outputs convinced **over 90% of survey respondents** and improved comprehension ([arXiv 2309.17057](https://arxiv.org/pdf/2309.17057)). A follow-up found such narratives **can contradict the SHAP values they describe** ([arXiv 2412.10220](https://arxiv.org/html/2412.10220v1)). Conversational explanation interfaces also raised users' over-reliance on the model ([CEUR](https://ceur-ws.org/Vol-4061/paper1.pdf)).

So M98's business-language reasons should stay template-generated. Any LLM paraphrase, for example for a Proof Pack summary or a frontline card, should pass an automated claim check. The check compares the sign and rank of each cited factor with the computed values, applying Plan G's grounding rule to explanations.

**Frontline assist.** The best field evidence is Brynjolfsson et al.: AI assist raised issues resolved per hour by **14% overall and 34% for novices** among 5,179 support agents ([NBER](https://www.nber.org/papers/w31161)). That was chat support, not retention calls. An "agent card" (top reasons, recommended offer, a "sure thing, do not discount" flag) is worth a measured pilot. The test randomises card access across agents. The card is not worth an unmeasured feature.

### MCP is the integration surface; A2A can wait

**MCP's status.**

- **Governance.** MCP moved to the Linux Foundation's Agentic AI Foundation in December 2025, co-founded by Anthropic, Block and OpenAI ([Anthropic](https://anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation)).
- **The spec.** The 2025-06-18 version made servers OAuth resource servers and added elicitation, which gives a standard approval prompt ([MCP changelog](https://modelcontextprotocol.io/specification/2025-06-18/changelog)). The 2026-07-28 revision introduced a stateless core and a **Tasks extension for long-running work** ([MCP blog](https://blog.modelcontextprotocol.io/tags/mcp/), the one page fetched directly). Tasks fit "did it work?" jobs that take weeks to mature.
- **Marketing platforms already serve it**, mostly read-only or scoped:
  - HubSpot: remote, OAuth, CRM read/write ([HubSpot](https://developers.hubspot.com/docs/build-with-ai/remote-mcp-server)).
  - Klaviyo: write tools can be disabled ([Klaviyo](https://developers.klaviyo.com/en/docs/klaviyo_mcp_server)).
  - Braze: beta, non-PII data ([Braze](https://braze.com/docs/developer_guide/mcp_server/)).
  - MoEngage: launched with nine read-only tools; **newer docs say it can now build and validate drafts and create segments**, with SMS, WhatsApp and webhook campaigns still read-only ([MoEngage](https://moengage.com/docs/user-guide/ai-and-intelligence/merlin-ai/moengage-mcp-server)).
  - Netcore: announced May 2025 ([Netcore](https://netcorecloud.com/?p=103291)).

**The opportunity.** The positioning memo and the layer plan already list an "MCP server (later)". Plan K should pull it forward in a specific shape: **read and propose only**. A client's own agent (Claude, ChatGPT, Agentforce or an in-house agent) gets tools for these steps:

- **Before acting:** "plan this test", "is this customer contactable on this channel for this purpose?", "what is the net value of treating this segment?"
- **After acting:** "audit this campaign" (M103), "give me the Proof Pack" (M104).

M103's audit of any campaign another tool ran is already the verification service. MCP is just its front door for other agents.

**Security.** These are measured risks:

- The MCPTox benchmark reported a **36.5% average attack success rate** for poisoned tools across 20 models (secondary summary, [CSA](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/)).
- Real incidents include a GitHub MCP prompt injection and an mcp-remote CVE rated 9.6 ([UpGuard](https://www.upguard.com/blog/mcp-security-incidents)).

The server should therefore:

- return schema-validated JSON with no prose from third parties;
- expose no write tools;
- use least-privilege OAuth scopes;
- enforce approvals server-side;
- treat every customer free-text field as untrusted.

Plan G's egress gate and untrusted-content handling are reusable as they stand.

**A2A.** It reportedly reached v1.0 in March 2026 and moved under the same foundation in August 2026. Those reports are secondary ([Linux Foundation](https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year); [Techzine](https://www.techzine.eu/news/devops/143659/google-transfers-a2a-to-the-agentic-ai-foundation/)). No marketing vendor was found exposing it, so A2A waits.

One idea is worth borrowing now: the "signed mandate" in AP2, the agent payments protocol ([AP2](https://ap2-protocol.org/index.md)). Marketing AI's approvals, test plans and holdout reservations should be signed, verifiable artefacts. A plan hash already exists in M94; Plan K would add signatures and chain the hashes.

### India's 2025–26 rules turn compliance into an agent workload

**DPDP.**

- The DPDP Rules were notified on 13 November 2025 ([S.S. Rana](https://ssrana.in/articles/meity-notifies-final-digital-personal-data-protection-rules-2025/)).
- **Consent-manager rules start about 13 November 2026, and most substantive duties about 13 May 2027.** A MeitY proposal floated on 23 January 2026 would compress the second date. No amending notification was found as of 28 September 2026, and no consent manager can register before Rule 4 starts ([MirvoLegal](https://www.mirvolegal.com/insights/dpdp-rules-2025-compliance-timeline/); [Business Standard](https://www.business-standard.com/technology/tech-news/meity-may-cut-compliance-timeline-for-key-dpdp-rules-to-12-months-126012201293_1.html)).
- DPDP has **no legitimate-interest basis**, so marketing rests on specific, withdrawable consent ([Bar & Bench](https://www.barandbench.com/law-firms/view-point/the-digital-personal-data-protection-act-2023-the-inconspicuous-absence-of-legitimate-interest)).
- The **fiduciary carries the burden of proving consent** (s.6(10)) ([SCC Online](https://www.scconline.com/blog/?p=391696)).
- Maximum penalties reach **INR 250 crore** for security-safeguard failures ([Taxmann](https://www.taxmann.com/post/blog/data-privacy-breach-enforcement-penalties-under-the-dpdp-act)).

**Telecom rules.**

- The **TCCCPR Third Amendment Regulations were notified 18 September 2026** ([PIB](https://www.pib.gov.in/PressReleseDetailm.aspx?PRID=2311989&lang=1&reg=3); [TRAI PR 119](https://www.trai.gov.in/sites/default/files/2026-09/PR_No119of2026.pdf)).
- It takes effect 30 days after Gazette publication (about late October 2026). Automated, robo and AI-voice (A2P) calls must be **pre-declared** to the telecom operator or they are treated as spam. AI/ML spam flags on a sender **plus 3 complaints in 10 days** trigger graded action. Inquiry-based messages are allowed for **7 days** only. Misused headers or templates are suspended **within 6 hours**. Legacy consents count only if obtained verifiably and **registered on the operators' digital platform** ([TRAI Gazette](https://www.trai.gov.in/sites/default/files/2026-09/Regulation_18092026.pdf)).
- Since February 2025, a first violation can bar a sender's outgoing services for 15 days ([Fonearena](https://www.fonearena.com/blog/446114/trai-tcccpr-2025-tougher-rules-spam-calls-sms.html)).
- A November 2025 direction reportedly requires **descriptive pre-tags on every SMS template variable**, with non-matching messages rejected after a 60-day logging window. This rests on a single secondary source ([TeamLease RegTech](https://teamleaseregtech.com/updates/article/49534/trai-issues-direction-to-curb-misuse-of-sms-headers-content-templates/)).

**What this means.** Every promotional send must clear several checks:

- DPDP consent for this purpose and channel;
- TCCCPR consent and the DCA registry;
- a valid DLT header, template and variable tags;
- whitelisted URLs;
- on WhatsApp, Meta opt-in and template approval.

That join across systems, with an evidence trail, is exactly the job a rules engine plus an audit log does well.

**Already built.** The per-channel consent ledger (M99), the data-driven `sms_requires: [dlt_template_id, message_category]` region rule, the uplift consent-gate fix and the one-way SMS opt-out link (M91). The layer plan also defines an M119 DPDP evidence pack.

**What Plan K adds:**

1. **A pre-send compliance gate** that writes a signed decision record per customer, channel and purpose.
2. **A DLT template matcher** that maps an approved message and its variables to a registered template and checks pre-tags and whitelisted URLs. MSG91's "DLT AI" shows the market wants this ([MSG91](https://msg91.com/help/how-to-use-dlt-ai-and-what-is-the-use-of-dlt-ai)).
3. **A legacy-consent remediation planner** that segments customers by where their consent came from and drafts the re-permission journeys.
4. **A regulatory-change watcher** that proposes versioned rule updates for human sign-off.
5. **Third Amendment checks**: A2P call-line declaration status, the 7-day inquiry window, legacy-consent registration status, header and template suspension alerts, and spam-flag monitoring.
6. **A "measurement with comparison groups" purpose** in client notice templates and the consent ledger. No guidance on randomised hold-outs under DPDP was found. TRAI governs sending, so not contacting a group raises no telecom issue. But measuring with a held-out customer's data is processing for a purpose, so the notice should name it. Get an Indian law-firm opinion before calling hold-outs "DPDP-safe".

The Account Aggregator ecosystem's **500 million+ fulfilled consents** show that purpose-bound, revocable consent records work at Indian scale ([NewKerala/Sahamati](https://www.newkerala.com/news/a/indias-account-aggregator-ecosystem-facilitates-nearly-38-crore-263.htm)). The ledger should therefore export records in that style, ready for registered consent managers. Marketing AI should not register as a consent manager itself.

## Seven popular ideas fail on evidence at Marketing AI's scale

1. **Autonomous agents with permission to send.** This combines the 45% disappointment figure, reliability that collapses on repeated attempts, and the positioning memo's own warning that sending and grading means "you marked your own homework". Keep DEC-1100 and the starter-gate rules. Plan K agents propose; people approve; the client's tools send.
2. **Per-user reinforcement learning and bandits at CRM volumes.** OfferFit and Aampe sell this. However, even vendors that offer bandits advise A/B tests at low traffic ([Statsig](https://statsig.com/perspectives/bandit-algorithms-vs-ab-testing)). Contextual bandits need data in every segment ([GrowthBook](https://docs.growthbook.io/bandits/contextual)). Monthly, between-cycle learning with a randomised explore slice (M92 plus M106) is the right design at this cadence.
3. **LLM causal discovery.** Its performance is **highly sensitive to the wording of the prompt** ([arXiv 2506.00844](https://arxiv.org/html/2506.00844v1)). Models recite well-known relations and generalise poorly to rare ones ([arXiv 2407.19638](https://arxiv.org/pdf/2407.19638)). Use LLMs to list candidate confounders and leakage questions, labelled Not proven, never to identify effects.
4. **Synthetic personas as lift predictors, and causal PFNs as holdout substitutes.** Both fail in the specific way that matters here. Personas are biased positive and narrow; causal PFNs shrink effects and assume no hidden confounding.
5. **Headline vendor lifts and contested statistics.** Treat these as sentiment, not evidence, in decks:
   - Persado's 450%, Netcore's "10x conversions" and CleverTap's "+66% conversion" give no control design ([CleverTap via Outlook Business](https://www.outlookbusiness.com/corporate/clevertap-launches-clever-ai-ai-driven-edge-for-customer-engagement-and-retention)).
   - MIT NANDA's "95%" means "no measurable P&L impact" in a convenience sample (52 interviews, 153 survey responses), is preliminary, and has named critics.
   - BCG's "5% get value" is superseded by its Applied AI Index 2026: 7.5% future-built and another 41% scaling.
   - Gartner's 40% cancellation figure is a forecast.
6. **Outcome-only pricing as what buyers want.** Futurum's 2026 survey found **43% prefer consumption pricing and 27% outcome-based** ([Futurum](https://futurumgroup.com/press-release/are-outcome-based-and-hybrid-ai-pricing-models-rewriting-the-vendor-playbook/)). Existing outcome fees reward activity, such as HubSpot's $0.50 per resolved conversation ([Constellation](https://www.constellationr.com/insights/news/hubspot-price-breeze-customer-agent-breeze-prospecting-agent-outcomes)). A fee settled on the Proof Pack's measured lower bound is distinctive, but it is a renewal rider, not the lead model.
7. **A2A, "guardian agent" market sizing, and free text-to-SQL.** A2A has no marketing adopters. Gartner's 10–15% guardian-agent share by 2030 is a forecast ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-06-11-gartner-predicts-that-guardian-agents-will-capture-10-15-percent-of-the-agentic-ai-market-by-2030)). The Spider 2.0 and annotation-error results argue against letting any model write queries on client data.

The market research also advised against using the word "agent" with buyers. That still holds externally. Plan K is an internal architecture name.

## Plan K: a governed agent layer in four tiers

**The design rule.** Every Plan K agent is a thin LLM planner over **the same typed, deterministic functions that already power the API**. It calls read tools and proposal tools, never write tools. Every proposal cites the tool result it rests on (Plan G's evidence-id rule). Approval is enforced in `engine/approvals.py`, not in the prompt. Budget, consent, frequency and holdout limits are enforced in the service. Each decision, approval, holdout reservation and evidence label is written to an append-only, hash-chained log.

**Mounted twice.** The tool set is mounted twice: internally for Marketing AI's own planner, and externally as the MCP server. It is designed once.

**Evaluation.** Plan G's benchmark idea extends to agents. Run every planning scenario *k* times and require the same decision each time, a pass^k test borrowed from tau-bench. Add it to the M95 validity harness.

**Framework.** A LangGraph-style state machine maps naturally onto plan → approval interrupt → hand off → measure → learn. Bedrock AgentCore Policy (Cedar, generally available March 2026) is an AWS-native option for gateway policy ([AWS](https://aws.amazon.com/de/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/)). Policy rules should be kept portable to avoid lock-in.

**Sizes.** The sizes in the table are relative judgements (S under 3, M 3–6, L over 6 engineer-weeks), not estimates.

| # | Plan K item | What it does | Builds on (Plan J pieces) | Partly built? | Tier | Size |
|---|---|---|---|---|---|---|
| K1 | **Agent-safe core** | Typed tool registry, proposal objects, server-side approval, hash-chained decision log, policy-as-code with default deny, pass^k evaluation | Plan G JSON-action loop, `numbers_grounded`, egress gate, `RoutePolicy`, audit events, M94 plan hash, M95 harness, M108 cost cap | **Yes**, as patterns | 1: with integration | M |
| K2 | **Measurement upgrades** | MLRATE/CUPAC covariates; local empirical-Bayes shrinkage; doubly robust OPE plus high-confidence lower-bound gate; one-sided harm monitor | M102 CUPED, M96 beats-risk and J5 fallback, `ope.py`, M94 early-look rule, M104/M105 | **Yes:** CUPED, IPW OPE, fallback | 1: with integration | L (DS-heavy) |
| K3 | **India compliance agents** | Pre-send consent gate with signed records; TRAI Third Amendment checks (A2P call declaration, 7-day inquiry window, legacy-consent registration, template suspension alerts); a "measurement with comparison groups" notice purpose; DLT template and pre-tag matcher; legacy-consent remediation planner; regulatory-change watcher feeding the evidence pack | M99 per-channel ledger, DLT region rule, M91 fixes, layer M119 DPDP pack, `engine/privacy/*` | **Yes:** ledger and DLT rule | 1: Third Amendment items first (in force ~late Oct 2026) | M–L |
| K4 | **Campaign planner agent** | Turns an M105 warning card or a user goal into a drafted test plan, treat-list run, arbitration and measurement schedule for one-click approval; narrates the Proof Pack under claim checks | M93 planner, M94 test plan, M97–M101, M105 cards, M107 loop, M104 pack | **Yes:** the loop is deterministic already | 2: after the first integrated cycle | M |
| K5 | **Verification MCP server** | Read and propose tools for external agents: plan a test, check contactability, value a segment, audit a campaign, fetch a Proof Pack; Tasks for long jobs; OAuth; no write tools | M103 audit, M104 pack, M93 planner, M99 contactability, layer plan "MCP server (later)" | Planned only | 1: read-only prototype on the M110 demo library (no client data); 2: client connection after the first integrated cycle | M |
| K6 | **Copy variants as randomised arms** | Draft, screen, DLT-register, then randomise variants as M100 treatment levels against the control template and no-message holdout | `win_back.py` variants A–D, guardrails, M99 catalogue, M100 arms, `measure_campaign` | **Partly:** variants without randomisation | 2 | M |
| K7 | **Setup and data-prep extensions** | Schema-mapping candidates for integration sources; label-definition drafts; LLM leakage questions beside deterministic checks; Hinglish benchmark cases | Plan G Guided setup, M107 bound sources, M93 lapse labels, `LEAKAGE_SUSPECTED` | **Yes:** substantially | 2 | S–M |
| K8 | **TFM challenger** | AutoGluon 1.6 `extreme_quality` models only (Nori, TabICLv2, TabDPT-Turbo; commercial-safe); TabPFN-2.5+ blocked without a Prior Labs licence; in model search and as M100 per-arm learners; library benchmark first | AutoGluon 1.6.3 pin, `strategy_presets`, M110 library | Dependency present, not enabled | 3: evidence-gated | S |
| K9 | **Grounded agent card** | Template reasons plus a claim-checked LLM paraphrase for save-desk staff; card access randomised across agents | M98 reasons, root-cause module, Plan G grounding | Partly | 3: needs a frontline pilot | M |
| K10 | **Watch list** | Surrogate index (pilot inside K2 first), causal PFNs, A2A Agent Card, synthetic-persona screening, contact-fatigue randomisation | M106/M107; M101 caps | No | Watch | — |

**Tier 1 runs alongside the integration phase.** That is when real send logs, real consent states and real CEP hand-offs arrive. The compliance gate and the decision log must exist before any Activation Contract leaves the product. K2 is mostly data-science work, and the Plan J effort tables show the data scientist was already the bottleneck. A second data scientist, or a delayed start for K6, should be planned now.

**K5 is split across tiers (changed after verification).** A read-only prototype over the demo library moves into Tier 1. It is cheap, because it is a thin MCP wrapper over the existing API. The window is narrowing: MoEngage's MCP can now create segments and drafts, and the neutral measurer INCRMNTAL is being acquired by Smartly. Connecting it to client data still waits for Tier 2.

**Tier 2 waits for one integrated, measured cycle.** The planner agent and the client-facing MCP server are only as credible as the first real Proof Pack behind them.

**Tier 3 is evidence-gated.** K8 ships only if the library shows a reproducible gain in rupees at equal budget. K9 ships only with a randomised frontline pilot.

**Kill criteria**, mirroring the positioning memo's checkpoints:

- Pause K4 and K5 if pass^k consistency stays below an agreed threshold.
- Pause them if a pilot CFO discounts any agent-written narrative.
- Pause them if MoEngage, CleverTap or Netcore expose an equivalent neutral audit tool first.

## Verification status

Status after the 10 October 2026 check against primary sources. "Not yet verified" means the check ran out of budget, not that the claim is false. Do not use those claims with buyers or in the manager review until they are checked.

| Claim | Status | Use |
|---|---|---|
| Gartner 45% / 81% (n=413); base is leaders with agents in pilot or production | **Confirmed** | Yes, with the base stated |
| Gartner 2024: 52% prove value; CFO 40% (CEO 39%) | **Confirmed** | Yes; the data is two years old |
| Gartner >40% agentic projects cancelled by 2027; ~130 real vendors | **Confirmed** (a forecast) | As a forecast only |
| Gartner guardian agents 10–15% by 2030 | **Confirmed** (a forecast; security and policy, not causal proof) | With that caveat |
| Gartner guardian-agent "80% of unauthorised transactions" | **Not found** | Drop |
| Gartner 63% lack AI-ready data | Not yet verified | Hold |
| TransUnion "67% prioritize incremental ROI" | **Corrected** (67.4% and the 52% figure are EMARKETER's) | Quote "67% prioritise" |
| MIT NANDA "95%" | **Corrected** (no measurable P&L impact; convenience sample; criticised) | Avoid |
| BCG 5% | **Out of date**: 2026 index shows 7.5% future-built and 41% scaling | Use the 2026 figures |
| BCG 2026: 53% of Indian CMOs expect a 5–9% uplift | Confirmed (secondary) | An expectation, not a result |
| Dentsu 46% India vs 63% global (n=1,950) | **Confirmed** (secondary) | Keep "for reputational reasons" |
| Salesforce India 81%; IBM India 26% / 23% | Not yet verified | Hold |
| Optimove 70% (n=329 affluent US consumers) | **Confirmed** | Not as evidence about India |
| MoEngage–Aampe (24 Jun 2026); Netcore.ai (30 Jul 2026), pricing tied to "agreed business KPIs" | **Confirmed** | Yes |
| Aampe RCT: +65.3% push-open days, +0.3% (±0.24) conversion days | **Confirmed** (vendor-authored) | Yes, as engagement vs conversion |
| Braze: control is one of three options; +92% has no method; $250–300k | **Corrected** / not found | Do not quote the price |
| Optimove shrinks controls; CleverTap "Scout" and +66%; Insider–Bluecore; Agentforce and HubSpot pricing; Gartner MQ positions | Not yet verified | Hold |
| Etsy 7% vs 27%; MLRATE 70% (19% beyond CUPED); DoorDash (switchback); Statsig 1.1% vs 17.3%; Netflix (95% consistency, 65–79% launch recall) | **Confirmed** with context | Yes, with the context |
| Booking incremental profit and knapsack ROI papers | **Confirmed** | Yes |
| Nextdoor e-mail subject lines +1% / −6.5%; Mercari conversion not significant | **Confirmed** | Yes |
| AutoGluon 1.6 `extreme_quality` commercial-safe; TabPFN-2.5+ non-commercial (incl. outputs) | **Confirmed** | Yes |
| TabPFN-3 size limits | **Conflicting** (1M × 2,000 vs 500k × 160) | Check before quoting |
| MCP 2026-07-28 (stateless, Tasks, authorisation) | **Confirmed** | Yes |
| MCP donation to AAIF; A2A v1.0 and move to AAIF | Not yet verified | Hold |
| MoEngage MCP can create segments and drafts | **Confirmed** (newer docs) | Yes |
| Other vendors' MCP servers; MCPTox 36.5%; tau-bench / CRMArena 2026 | Not yet verified | Hold |
| DPDP: Rule 4 ~13 Nov 2026; Rules 3, 5–16, 22–23 ~13 May 2027; not shortened | **Confirmed** (secondary summaries; Gazette PDF not opened) | Yes |
| DPDP penalties; section 7 legitimate uses; AI governance guidelines on training data | Not yet verified | Hold |
| TRAI Third Amendment (18 Sep 2026; A2P declaration, AI/ML flags + 3 complaints, 7-day inquiry, 6-hour suspension, legacy consent) | **Confirmed** (Gazette and PIB) | Yes |
| TRAI Nov 2025 template pre-tag direction; DCA 127xxx; TRAI–RBI pilot | Not yet verified | Hold |
| Smartly to acquire INCRMNTAL (letter of intent, 17 Mar 2026) | **Confirmed** (press release) | Yes |
| Hold-outs under DPDP | **No guidance exists** | Get a legal opinion |

Still open from the gap list: the share of CRM campaigns run without controls, a recent figure for discount waste, the size of India's martech market and its telecom churn, telecom retention incumbents (Amdocs, Pega, SAS, Flytxt), synthetic personas tested on Indian customers, appetite for outcome-based pricing, and AWS agent frameworks in production.

## Conclusion

The research shifts the question from "how does Marketing AI become agentic?" to "what do agents lack that Marketing AI already has?" Agents lack four things: a randomised counterfactual, a consent record that survives an audit, a pre-registered plan, and an honest label when the result is not proven. Those are exactly the pieces Plan J shipped. That makes Marketing AI unusually well placed to be the **tool layer agents call** rather than another agent competing for the send button. It also means the Hillstrom result is not an embarrassment to hide. A system that says "uplift did not beat risk ranking here, so the list uses risk ranking" is precisely what the 45% of disappointed buyers have not been shown.

The main threat is that the bundled platforms add proof packaging to the controls they already run. The defence is to make Marketing AI's neutrality verifiable:

- signed plans and approvals;
- a holdout salt the client owns;
- measured lower bounds as the only "proven" figure;
- an MCP endpoint that any vendor's agent can be audited through.

Built in that order, with compliance and measurement first, Plan K turns India's regulatory tightening and the market's distrust of agents into the product's two strongest sales arguments.
