# Market research: retention marketing, competitors and what customers want

**Prepared:** 2 October 2026, for brainstorming and for Plan J (`docs/plans/MARKETING_AI_PLAN_J_LAYER.md`)
**Owner:** Minfy — AI/ML team
**Status:** research input, not a decision record

> **Read this first.**
> - **Telecom was only the worked example.** The research used a telecom pilot as its example. Marketing AI is industry-agnostic: any B2C business with customer data (retail and e-commerce, BFSI, subscriptions, travel, utilities, telecom).
> - **How the evidence was gathered.** 14 research areas, about 300 findings, each fact-checked by a second agent. The research environment's network policy blocked opening most web pages, and the shared web-search allowance ran out partway through. Findings rest on search-result extracts plus each fact-checker's background knowledge.
> - **Grades.** Every claim carries one: **[strong]** (peer-reviewed or several independent sources), **[moderate]** (one credible source or practitioner consensus), **[weak]** (anecdote or unchecked snippet) or **[vendor claim]** (the vendor's own numbers). Check *weak* and *vendor claim* items before quoting them outside Minfy.
> - **Context.** The positioning decision that followed is in [`POSITIONING_DECISION_2026_10.md`](POSITIONING_DECISION_2026_10.md).

## Executive summary

- **Real effects are small.** A well-run telecom retention campaign usually cuts churn by about 1 to 3 points in the segment it targets. The common headline claims ("28–33% churn drop", "10x campaign revenue") give no control group. Our honest numbers will look smaller than competitors' numbers. We should present that as the point of the product. **[moderate; competitor figures are vendor claims]**
- **Contacting customers can raise churn.** In a randomised trial with about 65,000 wireless customers, proactive plan suggestions raised 3-month churn from about 6% to 10%. That makes "sleeping-dog" suppression a safety feature, and we should sell it that way. **[strong]**
- **Target by who responds, not by who is at risk.** Field experiments at a wireless carrier found that targeting by lift beat targeting by risk. The direction is solid. The figures people quote ("16% of the top-risk group receptive", "+8% vs +2%") come from secondary sources nobody has checked. Do not use them outside Minfy until someone reads the paper. **[strong direction; weak magnitudes]**
- **Weighting by value and cost beats uplift alone.** Ranking customers by expected net rupees (uplift × ARPU or margin − offer cost − contact cost) gave much higher profit in offline evaluations. This is our cheapest gap to close, and the evidence for it is the best of any gap. **[moderate]**
- **No one in Indian telecom proves their results.** None of the Indian telco case studies we found reports churn saved against a control group. Aampe advises against external controls. Netcore bills on "CRM revenue" without saying how it is attributed. In effect, vendors mark their own homework. **[moderate; this is a claim that something is absent]**
- **Buyers distrust AI claims and want proof in money.** 45% of martech leaders say vendor AI agents miss the performance they promised. Only 52% of marketing leaders can prove their value, and CFOs are the most sceptical executives. **[moderate]**
- **Prediction is now a commodity.** Every engagement platform ships gradient-boosted scores in High/Medium/Low bands. AWS gives churn guidance away free, and AutoGluon is not a moat. Our most direct competitor is DIY on AWS: SageMaker Canvas or AutoGluon, built by the client or by another partner. **[moderate]**
- **The activation layer is being bought up.** Fivetran bought Census, Rokt bought mParticle and Uniphore bought ActionIQ. Do not build a CDP or reverse-ETL tool. Write a governed, versioned table that the client's existing tools can read. **[moderate]**
- **The hand-off is our weakest link.** Today the output stops at `scores.csv`, and the product sends nothing. Campaign managers judge a tool first on whether lists get into the tool they use. Five separate research areas flag this. **[weak per source; consistent across sources]**
- **Host platforms are moving into "decide" and "prove".** MoEngage bought Aampe and partnered with Boldest/Prodapt to sell telcos a managed churn service (June 2026). Braze bought OfferFit for about $325M. Optimove and CleverTap ship control groups. **[strong]**
- **Telco CVM buyers expect a choice between offers.** Every incumbent pitch leads with next-best-offer across data bonuses, discounts and loyalty rewards. Our uplift handles one treatment only, and cross-use-case arbitration is just a frequency cap. **[vendor claim]**
- **There are few Indian telco buyers.** Jio and Airtel build their own stacks (JioBrain, Xtelify). Vi has the most churn pain (3.8% a month) but little cash, and it has signed IBM and TCS. BSNL is plausible but slow. Regional ISPs and DTH operators are small deals. **[moderate]**
- **Win-back to lapsed prepaid customers may not be lawful as planned.** Under TCCCPR, inferred consent reportedly ends with the contract **[weak; needs legal confirmation]**. Consent-manager rules apply from 13 Nov 2026 and the rest of DPDP from 13 May 2027, and that date may be brought forward **[strong]**.
- **An underpowered pilot is the likeliest way to lose the deal.** Detecting a drop from 4% to 3% churn needs about 5,300 customers per arm. A pilot that is too small produces a 95% range that crosses zero, even when the campaign worked. **[weak: standard formula, not verified]**
- **Price per use case, never per audience.** Audience meters charge for the lapsed customers that churn and win-back work targets. Our randomised net value is what makes an outcome fee possible to settle in a contract. Indian engagement platform contracts anchor around INR 15–40 lakh a year. **[weak]**

**Bottom line.** Position Marketing AI as the layer that decides who to treat and proves what it earned, sitting on top of the engagement or CVM tool the operator already pays for. Sell it through a Minfy-run retention pod for the first two or three clients, and treat telecom prepaid know-how as the first vertical pack, not as the product's identity. The pilot is paid, but its most valuable output is one published Indian telco result, with its method disclosed, in rupees, against a randomised control. Every later deal, price and partner conversation depends on that result.

---

## Positioning: the options and our recommendation

### The three options compared

| | **A. Decide-and-prove layer** | **B. Telecom CVM product** | **C. Services-led programme** |
|---|---|---|---|
| **Core idea** | Decide who to treat (uplift, value, suppression). Hand a governed list to the client's existing tool. Prove the result against a randomised control. | A full telecom CVM product covering labels, root causes, targeting, channels and proof, for challenger and mid-tier operators. | "Minfy Measured Retention Programme": paid assessment, then a paid proof of value, then a managed pod, then an optional outcome fee. |
| **Evidence behind it** | Strongest. Four research areas reached this position on their own: market structure, telecom/India, both competitor reviews. It also matches the commodity and consolidation findings. | Mixed. The method's evidence comes from telecom. But our own research says "do not compete as a CVM suite": every incumbent bundles real-time decisions, multi-offer choice, loyalty and managed data science. | Good for entry. The market review recommends Model C (licence plus pod) to win the first logos. The evidence for outcome fees is thin: gain-share churn contracts are rarely published. |
| **Fit with product today** | High. Most of "decide" and "prove" is built. The gaps are the hand-off, a control that survives the hand-off, and value weighting. | Low to medium. The end-to-end scope is mostly unbuilt. The product deliberately moved to one generic journey. The `telco_churn` config is mapped to the Kaggle postpaid file. | High as a wrapper. The pilot playbook is already Minfy-staffed, and a pod can cover the activation and arbitration gaps by hand. |
| **Can we win in India/telecom?** | Medium to high. It sits beside the operator's tools instead of replacing them. It must beat "our CEP already has a control group". | Low in formal tenders. It loses on checklists (real time, loyalty, next-best-offer), the buyer pool is tiny, and well-funded platform vendors are already entering. | Medium. Indian buyers buy "accelerator + team" this way. But Fractal, LatentView and the large SIs sell the same shape with bigger benches, and a capable firm could copy the RCT. |
| **Time to revenue** | About 10 weeks to a measured result (set by the 60-day outcome window), inside a paid proof of value. | Long. The label builder, the root-cause pack and several CVM features come before we are ready for tenders. | Fastest. A paid 2–4 week Data Readiness Assessment can be sold now. |
| **Main risk** | The hand-off fails or contaminates the control. Host platforms close the gap first. | A head-on fight with incumbents in a small market. | Low margin, does not scale, seen as "just another services firm". Pod staff may cover up product gaps instead of fixing them. |
| **Verdict** | **The product position** | **The first vertical pack, not the identity** | **The sales motion for the first 2–3 logos** |

### Recommendation: a sequenced combination, led by A

**The product is the decide-and-prove layer (Option A).** It is the only option where the evidence, the existing code and the market structure all point the same way. Built-in prediction is everywhere. Activation is being consolidated into large platforms. What is scarce is a credible answer to two questions: who should we treat, given who actually responds, and what did that earn, measured against a random control? The repository already answers both. It has S/T/X uplift learners, four segments with sleeping dogs never treated, a budget and profit curve, campaign results with 95% Newcombe intervals and maturity windows, a "causal" label only when the control was random, off-policy evaluation, and a governed step that turns a measured campaign into the next uplift run. The gaps A leaves open (channels, real-time) are the host platform's job. The gaps A cannot hand off (value weighting, multi-offer choice, arbitration) are batch problems we can build.

**We sell it through a services wrapper first (Option C).** Indian telcos buy analytics as "accelerator + team". Operator CVM teams are short of data scientists. A pod can cover the hand-off by hand while the product catches up. Lead with a paid Data Readiness Assessment. Follow with a fixed-fee proof of value whose conversion price is agreed at signing. Then convert to licence plus pod. Offer an outcome rider only at renewal, after one randomised test has shown credible lift for that client. Option C is a bridge, not a destination. Its proposer said so, and the kill criteria below hold us to it.

**Telecom depth is the first vertical pack (Option B), not a CVM suite.** Prepaid churn labels, network and billing root causes, and TRAI/DLT-aware actions are what neither the engagement platforms nor the DIY path will build. They also decide whether the pilot works. We build them as a pack on top of the generic product, so the same layer can later serve BFSI (payment propensity, win-back), where the configs already exist.

**What we take from each option.**
- From A:
  - the Activation Contract;
  - a persistent universal holdout;
  - send-log reconciliation;
  - an "audit mode" for campaigns other tools run;
  - the explicit "do not build" list.
- From B:
  - the prepaid label builder;
  - the telecom root-cause feature pack;
  - the regulated action catalogue;
  - the save-desk card;
  - the "tariff-hike retention readiness" pilot;
  - later, emerging-market operators reached through AWS.
- From C:
  - the "Value Proof Pack" as the document that settles any fee;
  - pod-scaling tools (insight cards, an experiment registry with "proven value to date");
  - AWS ACE co-sell registration;
  - a kill criterion on pod effort per client.

### What we say to a buyer

"Your engagement platform sends the messages. Marketing AI decides who should get them, and who should be left alone, and proves what that earned in rupees against a randomised control group. It runs inside your AWS account with human approval and a full audit trail, and a Minfy team runs it with you until your own team can."

Avoid "agent", "decisioning platform" and any uplift figure without its control design and interval.

### Decision checkpoints (stop or re-position if any holds)

1. **The hand-off fails.** In the pilot, the operator's tool cannot ingest the Activation Contract within 3 weeks. Or, after one corrective cycle, more than about 5% of holdout IDs are contacted, or fewer than about 70% of the treat list is contacted.
2. **"Decide" does not beat risk targeting.** In the three-arm test at equal budget, uplift top-N fails to beat risk top-N on incremental saves per rupee, in the pilot and in one repeat cycle.
3. **"Prove" has no buyer.** Of the first 5 qualified sponsors or CFOs, 3 or more say their CEP's control group or attribution is good enough, or refuse to fund measurement unless it is bundled.
4. **No lawful, random test is possible.** The operator refuses a random holdout (playbook E4), or legal blocks win-back and no contactable substitute exists (pre-churn retention, recharge reminders).
5. **Multi-offer or real-time is a precondition.** Two of the first three target operators require multi-offer or real-time before a pilot, and we cannot ship batch multi-offer within one quarter.
6. **A host platform gets there first.** MoEngage, CleverTap or Netcore ships native uplift targeting with confidence-interval net value in India before we publish a case study. Review this every two quarters.
7. **The economics fail.** A proof of value that meets its written criteria does not convert within 3 months, or the pod still needs more than about 1.5 people per client by the third monthly cycle.

---

## What actually works in real life, and what fails

**Bottom line.** Four things have independent evidence behind them. Randomised controls are the arbiter. A customer's churn risk is a different thing from how they respond to an offer. Campaigns can raise churn. Targeting that accounts for customer value and offer cost beats targeting on uplift alone. Most headline numbers are much weaker than that: "28–33% churn drop", "10x revenue", "70% save rate". They are vendor claims with no disclosed control. A realistic incremental effect from a well-run telecom retention campaign is **one to three percentage points of churn in the targeted segment**, not tens of percent.

Caveat for the whole section: the research agents could not open most primary pages because network access was blocked. Many numbers come from search extracts plus the verifier's own knowledge. Where the verifier corrected or downgraded a claim, the corrected version is used below.

---

### Part 1: What works

#### 1. A randomised control group on every campaign
- **Mechanism.** Comparing treated customers with randomly withheld ones separates "stayed because of us" from "would have stayed anyway". Nothing else does this reliably.
- **Evidence.** Large Facebook RCTs show that observational and attribution methods often fail to recover the lift measured by experiments, even with rich data ([Gordon et al. 2019](https://pubsonline.informs.org/doi/10.1287/mksc.2018.1135)) **[strong]**. Booking.com's review of 150 production models found that offline metric gains did not track business gains in RCTs ([Bernardi et al., KDD 2019](https://dl.acm.org/doi/10.1145/3292500.3330744)) **[moderate]**. Wayfair built public-service-ad (PSA) "ghost ad" controls into its normal process to keep producing uplift training data ([Wayfair](https://www.aboutwayfair.com/2018/05/uplift-modeling-in-display-remarketing/)) **[moderate]**.
- **Magnitude.** No lift figure applies here. The value is that you can trust whatever number you report.
- **Marketing AI.** Our randomised-control measurement with 95% ranges and net money value is the most defensible part of the product. Make the holdout mandatory on every treat-list export, not optional.

#### 2. Target by uplift, not by churn risk
- **Mechanism.** The customers most likely to churn are often not the ones an offer can move. Some are "lost causes" and some react badly. Uplift models estimate the *change* in behaviour that treatment causes.
- **Evidence.** The direction is **[strong]**. Ascarza's "Retention Futility" ([JMR 2018](https://doi.org/10.1509/jmr.16.0163)) used field experiments at a wireless carrier and a membership organisation and found that lift-based targeting beat risk-based targeting. [Devriendt, Berrevoets & Verbeke 2021](https://doi.org/10.1016/j.ins.2020.12.075) make the same argument for churn. [Gubela et al. 2019](https://www.wiwi.hu-berlin.de/de/forschung/irtg/results/discussion-papers/discussion-papers-2017-1/irtg1792dp2018-062.pdf) found uplift models beat response models, and which uplift method wins depends on the base learner **[moderate]**.
- **Magnitudes (treat with care).** Three figures circulate in secondary summaries, and none was checked against the paper **[weak]**:
  - "up to 6.8 pp more churn reduction";
  - "only ~16% of the top-10% risk group were receptive";
  - "+2% retention by risk vs +8% by uplift".

  The Telenor case is old and vendor-authored ([Portrait/Stochastic Solutions, c. 2010](https://www.stochasticsolutions.com/pdf/uplift-modelling-for-retention.pdf)) **[vendor claim]**. A risk-targeted mailing pushed churn from about 9% to 10%. Treating only an uplift-selected 30% of the targets brought churn to about 7%, with a reported ~11x campaign ROI. That works out to about 2 points against no treatment and about 3 points against the blanket campaign. Uber says uplift-targeting about 30% of users matched the conversion gain of treating everyone ([CausalML whitepaper](https://arxiv.org/pdf/2002.11631)) **[vendor claim, unaudited]**.
- **Marketing AI.** Our two-step design (churn model + root-cause analysis, then an uplift treat list) is the right shape. Make the risk vs responsiveness split visible by default: Persuadables, Sure things, Lost causes and Sleeping dogs, with headcount and rupees per group. Do not quote the 16%, 6.8 pp or 8% figures externally until someone has read the paper.

#### 3. Value- and cost-aware targeting
- **Mechanism.** Two customers with equal uplift are not worth the same if their ARPU differs, and an offer that costs more than the margin it saves destroys value. Rank by expected incremental profit, not by uplift.
- **Evidence.** [Lemmens & Gupta 2020](https://doi.org/10.1287/mksc.2020.1229) used a profit-based loss and beat risk-based targeting by a large margin **[moderate]**. Correction: the reported "more than 100% profit gain" comes from an HBS summary, and it was an *offline* evaluation on data from two field experiments, not a live roll-out. [Gubela & Lessmann 2021](https://datalearner.com/academic/journal-papers/0167-9236/volumes-and-issues/312/paper-detail/4987) found that value-driven metrics gave higher profit than ranking on individual treatment effect alone **[moderate]**. Uber ranks retention spend by effect per unit cost ([Du et al. 2019](https://proceedings.mlr.press/v104/du19a.html)) **[moderate]**. Booking frames promotions as a knapsack problem under an ROI constraint and validated it in online RCTs ([Free Lunch, RecSys 2020](https://arxiv.org/abs/2008.06293)) **[moderate]**.
- **Marketing AI.** This is our cheapest high-value gap ("no CLV weighting"). Rank by uplift × (ARPU or margin × horizon) − offer cost − contact cost. The budget simulator should optimise profit under a spend cap or ROI floor, not just headcount.

#### 4. Variance reduction (CUPED / ANCOVA)
- **Mechanism.** Adjusting outcomes for pre-period behaviour removes noise. That narrows confidence ranges and lets you run smaller controls.
- **Evidence.** The method (Deng et al., Microsoft, 2013) is standard at Booking, Netflix and Nubank **[strong]**. Magnitudes come from vendor pages: often 30–50%+ on metrics strongly correlated with their pre-period values ([Statsig](https://docs.statsig.com/experiments/statistical-methods/variance-reduction)) **[vendor claim]**.
- **Realistic magnitude.** The gain is large for continuous metrics such as ARPU or recharge value. It is small for sparse binary outcomes such as churned yes/no, and zero for customers with no history.
- **Marketing AI.** Data Doctor already builds point-in-time, one-row-per-customer features, so pre-period ARPU, recharge and usage are available at no extra cost. Apply CUPED to revenue-retained outcomes first. Show "variance reduced by X%" and the smaller control size it allows.

#### 5. A persistent universal holdout
- **Mechanism.** A random group withheld from *all* CRM activity measures whether the whole programme pays, not just one campaign.
- **Evidence.** Braze and Loymax ship this as standard, with hash-based buckets and lift-with-confidence reporting ([Braze GCG](https://braze.com/docs/user_guide/audience/global_control_group)) **[moderate]**. The commonly cited ~10% size is community anecdote **[weak]**.
- **Marketing AI.** Our recipes replay monthly. If the control is re-randomised each month it gets contaminated. Add a hash-salted 3–10% universal holdout that survives reruns and applies across all use cases. Report programme lift quarterly.

#### 6. Plan the test before you run it
- **Mechanism.** If the expected effect is smaller than the noise, the result is "not significant", and that kills trust even when the campaign worked.
- **Evidence.** [Lewis & Rao (QJE 2015)](https://academic.oup.com/qje/article/130/4/1941/1914593) show that realistic marketing effects need very large samples **[strong]**. Meta's [GeoLift best practices](https://github.com/facebookincubator/GeoLift/blob/main/website/docs/Best%20Practices/BestPractices.md) require power analysis, 20+ geos and 25+ pre-periods **[moderate]**.
- **Marketing AI.** Add a power and minimum-detectable-effect (MDE) calculator in Guided setup, with a control-size slider and "this holdout forgoes about INR X". Pre-register the plan in the append-only audit log.

#### 7. Use experiments to calibrate models
- **Mechanism.** Compare predicted uplift with observed uplift by decile on each randomised campaign. This is the same "experiments calibrate models" pattern as Robyn's MAPE.LIFT objective and Meridian's priors **[moderate]**.
- **Marketing AI.** Feed a predicted-vs-observed calibration chart into the Approver's champion/challenger decision.

#### 8. Structural and care-side levers (context, not campaigns)
- AT&T credits fibre-plus-mobile convergence with lower churn ([Light Reading](https://www.lightreading.com/wireless/at-t-banks-on-convergence-to-halt-wireless-churn)). The comparison is self-selected, so it is correlational **[weak]**.
- T-Mobile's Team of Experts care model is credited with ~13% lower care costs and record-low churn ([HBR 2018](https://hbr.org/2018/07/the-surprising-power-of-online-experiments)). That gain is confounded with pricing and network changes made at the same time **[weak]**.
- **Marketing AI.** Campaigns are not the only lever. Add product-holding and household features (broadband, family SIMs). Package our per-customer "reason + recommended action" as a save-desk or agent card.

**What Part 1 means for Marketing AI.** Our core (uplift with S/T/X learners, a budgeted treat list that excludes sleeping dogs, randomised-control measurement) matches what the evidence supports. The gaps with the best evidence behind them, cheapest first:
1. Value and cost weighting.
2. CUPED.
3. A power/MDE planner.
4. A persistent universal holdout.
5. A predicted-vs-observed calibration chart in approval.

None needs new research. All five are product work on top of what we have.

---

### Realistic numbers practitioners should expect

| Metric | Realistic expectation | What you'll hear instead | Grade |
|---|---|---|---|
| Incremental churn reduction, well-targeted retention campaign | **1–3 pp** in the targeted segment (Telenor 9%→7%) | "28–33% churn drop" ([Flytxt](https://flytxt.com/?p=6291)); "50% cut in blended churn" ([TTEC](https://www.casestudies.com/company/ttec/case-study/communications-media-and-technology-company-customer-case-study-3)) | [vendor claim] for both; small-effect direction consistent with [strong] academic work |
| Proactive or risk-targeted outreach | Can be **zero or negative**: 6%→10% churn in a ~65k-customer RCT ([Ascarza et al. 2016](https://doi.org/10.1509/jmr.13.0483)) | "Every save counts" | [strong] |
| Share of the base you need to treat | ~30% gives most of treat-all's effect (Uber; Telenor's 30%) | Treat all high-risk customers | [vendor claim] |
| Value-aware vs risk targeting | Profit gain large; ">100%" in an offline evaluation ([Lemmens & Gupta](https://doi.org/10.1287/mksc.2020.1229)) | — | [moderate] |
| Uplift vs risk targeting gap | "+8% vs +2% retention", "16% of top-risk receptive" | — | [weak] (unverified secondary figures; direction [strong]) |
| Incremental share of attributed CRM revenue | ~30–60% at first email holdout ([ATTN](https://www.attnagency.com/blog/holdout-testing-marketing-guide)) | 100% (attribution) | [weak] |
| Save-desk "save rate" | 64–76% gross; incremental share unknown ([Foundever](https://foundever.com/case-studies/u-s-telecoms-provider-surpasses-its-customer-retention-targets-in-just-five-months/)) | Read as retention | [vendor claim] |
| CUPED variance reduction | 30–50%+ for continuous metrics with strong pre-period; small for binary churn; 0 for new customers | "30–70%" flat | [moderate] mechanism, [vendor claim] range |
| Monthly churn base rate | Vi ~4.1–4.3%, Jio ~2%, Airtel ~2.5–3% (late 2025, [Voice&Data](https://www.voicendata.com/news/vodafone-idea-q2-fy26-losses-narrow-stability-returns-10646051)); US postpaid ~0.9–1.0% | — | [moderate], now about a year old |
| Control size needed to detect a churn change, 80% power, 95% confidence | About **5,300 per arm** for 4%→3%; about **3,200 per arm** for 10%→8%. Detecting *heterogeneous* uplift needs several times more. | "A 1,000-customer pilot will show it" | (background knowledge, not verified: standard two-proportion formula) |
| Churn classifier accuracy in papers | 85–98%; says nothing about campaign value | "Our model is 97% accurate" | [moderate] |
| Geo-lift tests (paid media only) | MDE 5–10%, 4–8 weeks; with one treated geo, GeoLift missed ~89–96% of true 7.5% lifts ([Recast](https://github.com/getrecast/geolift-simulation-study)) | "Geo tests are the source of truth" | [moderate] (competitor-run simulation, public repo) |
| CVM platform revenue uplift | — | "Up to 10% revenue, 20% CLTV" ([McKinsey](https://www.mckinsey.com/capabilities/growth-marketing-and-sales/our-insights/unlocking-the-value-of-personalization-at-scale-for-operators)); "10x campaign revenue" ([Comviva](https://www.comviva.com/wp-content/uploads/2025/03/MobiLytix-client-success-stories.pdf)) | [vendor claim]; treat as ceilings |

**What this table means for Marketing AI.** Our honest numbers will look smaller than incumbents'. Present that as the point, not as a weakness: "2 points, with a 95% range, in rupees, against a randomised control" beats "33%, method undisclosed" with a CFO. Gartner reports that only 52% of marketing leaders can prove value and get credit for it ([Gartner 2024](https://www.gartner.com/en/newsroom/press-releases/2024-09-18-gartner-survey-finds-only-52-of-senior-marketing-leaders-can-prove-marketings-value-and-receive-credit-for-its-contribution-to-business-outcomes)) **[moderate]**. Set pilot expectations in writing at 1–3 pp before launch.

---

### Part 2: What fails, and why

#### Model failures

**Sleeping dogs (campaigns that raise churn)**
- **Mechanism.** Contact breaks inertia or makes a bad deal salient, so the customer leaves.
- **Evidence.** Proactive cheaper-plan recommendations to ~65,000 South American mobile customers raised 3-month churn from ~6% (control) to ~10% (treated), with very different effects across customers ([Ascarza, Iyengar & Schleicher, JMR 2016](https://doi.org/10.1509/jmr.13.0483)) **[strong]**. The Telenor 9%→10% case and a "9%→10% after a whole-base offer" story in the literature are **[weak]**: unaudited anecdotes.
- **Do.** Add a backfire detector after every campaign: treated-minus-control by band × RCA driver × tenure, with 95% ranges. Auto-suppress significantly harmed segments in the next monthly run. Market it as a safety feature.

**Unstable uplift models**
- **Mechanism.** Uplift is a difference between two noisy quantities, so rankings swing with the data split.
- **Evidence.** [Devriendt, Moldovan & Verbeke 2018](https://researchportal.vub.be/en/publications/uplift-modeling-state-of-the-art-and-novel-approaches/) tested four real datasets: uplift random forests did best, but performance swung strongly across cross-validation folds **[moderate]**.
- **Do.** The Approver should see AUUC as a distribution across folds or bootstraps with a stability badge, not one number.

**Noisy, and possibly misleading, Qini/AUUC**
- **Mechanism.** Qini measures ranking quality on finite RCT data. It has high variance and is not a business objective.
- **Evidence.** [Bokelmann & Lessmann 2022](https://deepai.org/publication/improving-uplift-model-evaluation-on-rct-data) show high variance and propose outcome adjustment **[moderate]**. An ICML 2025 paper reportedly shows that biased models can outrank unbiased ones ([Zhu et al.](https://proceedings.mlr.press/v267/zhu25s.html)) **[weak]**, unverified.
- **Do.** Block promotion unless uplift beats random *and* beats propensity targeting with significance. Add an R-score (EconML's RScorer, a ready-made model-selection score) and profit at the planned budget next to Qini.

**Offline gains that do not survive deployment**
- **Mechanism.** The population, offer or season shifts between the training experiment and live use.
- **Evidence.** Booking.com's 150-model review **[moderate]**. [Simester et al. 2020](https://pubsonline.informs.org/doi/10.1287/mnsc.2019.3388) found that ML targeting policies lose much of their value under typical data shifts **[strong]**. Lemmens & Gupta's >100% and Uber's 30% are offline or self-reported, so treat them as **upper bounds**.
- **Do.** Tie champion status to measured online lift. Track realised vs predicted lift by band monthly, as a separate "uplift drift" alongside PSI.

**Short-term targets that mislead**
- **Mechanism.** A 30-day "recharged" label can reward pull-forward rather than lasting retention.
- **Evidence.** [Yang et al.](https://arxiv.org/abs/2010.15835) argue for surrogate or long-term outcome targeting **[moderate]**. Whether win-back gains persist is an open question in our corpus.
- **Do.** Keep a subset of controls for 90–180 days and report 30/60/90-day survival.

**Accuracy theatre**
- **Mechanism.** On imbalanced churn data, high accuracy is easy to get and does not connect to a retention decision.
- **Evidence.** The literature is full of 85–98% accuracy claims that never reach campaign value. An Indian study claims "87% of potential churners retained" with no control ([NITK](https://idr.nitk.ac.in/handle/123456789/21154)) **[weak]**.
- **Do.** Lead with Qini, the profit curve and measured net value. Show "treated and stayed" next to "incremental vs control".

#### Data failures

**No randomised history, or randomisation only inside the high-risk group**
- **Mechanism.** Uplift needs treated and untreated customers who are comparable. If past treatment was chosen by a model or a channel, effects are confounded.
- **Evidence.** In the Orange Belgium benchmark (three campaigns, Sept–Dec 2020), the control was randomised only within customers the churn model had flagged ([Verhelst et al.](https://arxiv.org/html/2312.07206)) **[moderate]**. Uplift is therefore unknown for low-risk customers. EconML's validity rests on there being no unobserved confounders ([EconML](https://pypi.org/project/econml/)) **[strong]**.
- **Do.** Add a Data Doctor "treatment-assignment audit": was treatment random, propensity-selected or channel-selected? Check balance and overlap, and warn on extrapolation. Add a small random "explore" slice outside the targeted band on every export so randomised history builds up through monthly recipes.

**Too little data for rare outcomes**
- **Mechanism.** Win-back conversion is often below 5%. Estimating *differences* in effect needs far more data than estimating propensity.
- **Evidence.** Correction: the Criteo dataset that is "large enough to compare baselines significantly" on conversion is v2/v2.1, with 13.98M rows ([Criteo 2021](https://ar5iv.labs.arxiv.org/html/2111.10106)) **[moderate]**. The 25M-row figure was the older 2018 version. Lewis & Rao **[strong]**.
- **Do.** Pre-flight check: if the sample is too small, recommend "propensity + randomised control" this month and uplift once enough history exists.

**Ambiguous prepaid churn labels**
- **Mechanism.** Prepaid has no contract event. Churn means inactivity, a recharge lapse or a port-out, and dual-SIM users keep a secondary SIM alive on minimum recharges.
- **Evidence.** TRAI headline counts include inactive SIMs, so net adds are not churn **[moderate]**. Monthly churn of ~2–4.3% implies roughly 25–50% a year **[moderate]**.
- **Do.** Build a prepaid label builder (lapse windows, grace periods, port-out flags, secondary-SIM detection) with leak checks against the label window. Offer "recharge within N days" and "revenue retained" as outcomes ([Huang et al., SIGMOD 2015](https://users.wpi.edu/~yli15/Includes/SIGMOD15-huang.pdf)) **[moderate]**.

**Market shocks**
- **Mechanism.** Feature distributions *and* responses to offers change after tariff hikes.
- **Evidence.** After the July 2024 tariff hikes, some Vi users ported to BSNL. The effect was temporary ([Business Standard](https://www.business-standard.com/companies/news/vodafone-idea-subscribers-porting-out-to-bsnl-after-tariff-hike-ceo-124081301608_1.html)) **[moderate]**.
- **Do.** Let users annotate events on PSI charts and in the LLM RCA summaries. Retrain uplift after a known shock, not only when PSI fires.

#### Measurement failures

**Attribution and gross "save" metrics over-credit**
- **Mechanism.** Customers who would have stayed or bought anyway get counted as wins.
- **Evidence.** First email holdouts reportedly show only ~30–60% of attributed revenue is incremental **[weak]**. Save-desk save rates of 64–76% count call-level acceptance, not 90-day retention **[vendor claim]**.
- **Do.** Every campaign report shows "naive credit" next to "measured incremental", plus wasted offer cost on sure things. This is the moment that wins over a CFO.

**Vendor lifts with no disclosed control**
- **Mechanism.** No randomisation, a tiny or selected control, or a cherry-picked window.
- **Evidence.** B2Metric claims +47.3% revenue per user, and Subex claims ~20% sales lift ([Subex](https://info.subex.com/unlock-telecom-revenue-growth-with-ai-driven-next-best-offer-solutions)) **[vendor claim]**. A 2015 B2B telecom study reports 6% vs 34% churn on 1,007 accounts ([arXiv](https://arxiv.org/pdf/1506.03214)). A gap that large usually means a non-random or selected control **[weak]**.
- **Do.** Publish our method (randomisation proof, ranges, audit ledger) and push buyers to ask competitors for theirs.

**Underpowered tests, peeking and fragile quasi-experiments**
- **Mechanism.** Checking results daily inflates false positives. Synthetic-control methods with few units miss real effects or invent fake ones.
- **Evidence.** Recast's public simulation (8,000 panels, 32,000 model fits): with one treated geo, GeoLift missed ~89–96% of true 7.5% lifts, and other tools showed 15–30% false positives **[moderate]**. Recast competes with these vendors.
- **Do.** Lock the analysis date or use always-valid intervals. Individual randomisation is our advantage in CRM, so say so.

**Control contamination and partial execution**
- **Mechanism.** We hand over a CSV. The client's team may contact control customers or skip treated ones. Monthly re-randomisation also erodes the controls.
- **Evidence.** This is standard holdout-design consensus **[moderate]**.
- **Do.** Add contact-log reconciliation: report intent-to-treat and complier-adjusted effects, and flag contamination. Use persistent hashed assignment.

#### Organisational and commercial failures

**Offers that teach customers to threaten churn**
- **Mechanism.** If calling to cancel reliably earns a discount, customers who were never going to leave learn to call.
- **Evidence.** The corpus supports only part of this: save rates are inflated by customers using cancellation threats to bargain **[weak]**. No study here measures a long-run "training" effect. The learning dynamic itself is (background knowledge, not verified).
- **Do.** Add a "likely sure thing, do not discount" flag on save-desk cards. Track repeat-threat rates and offer cost per *incremental* save over time.

**Resistance to holdouts and finance scepticism**
- **Mechanism.** Withheld customers mean forgone revenue, and long-running holdouts drift.
- **Evidence.** CMOs say CFOs (40%) and CEOs (39%) are the most sceptical executives **[weak]**: unverified figures, though the 52% headline is confirmed.
- **Do.** Show the cost of the holdout next to the value of certainty. Use CUPED to shrink controls. Build a "CFO report" in INR.

**Thin data-science teams and slow time-to-value**
- **Mechanism.** Operator CVM teams lack data scientists. Pilots that take a quarter lose credibility.
- **Evidence.** Incumbents bundle managed data-science-as-a-service with their platforms (Comviva). Buyers cite about 4 weeks to the first live campaign **[vendor claim]**.
- **Do.** Sell the pilot as a managed outcome: Minfy data scientists, 4–6 weeks to the first campaign, and a three-arm design (risk top-N vs uplift top-N vs random control, same budget) that reproduces Ascarza on the operator's own data.

**What Part 2 means for Marketing AI.** Most failures are not about algorithms. They come from:
- missing randomised history;
- wrong prepaid labels;
- unexecuted or contaminated controls;
- noisy evaluation metrics;
- gross numbers being read as incremental.

We already own the right control points: Data Doctor, Guided setup, the Approver gate, the audit log and campaign measurement. The highest-leverage additions for the telecom pilot:
1. Treatment-assignment audit and explore slice.
2. Prepaid label builder.
3. Power/MDE planner.
4. Uncertainty-aware approval (fold bands, significance vs random and propensity, R-score).
5. Backfire detector.
6. Contact-log reconciliation.

Without activation we cannot guarantee execution. We can, and must, prove what happened.

## What customers actually want

**How to read this section.** The source research could not open most web pages: the proxy blocked them and the search budget ran out. Most figures below therefore come from search-result summaries, and a verifier checked them against URL slugs and prior knowledge. Each persona's needs are ranked by how often they recur across sources and how strong the evidence is. Where the corpus has little or nothing on a persona, the section says so. Tags: **[strong]**, **[moderate]**, **[weak]**, **[vendor claim]**.

The overall picture is consistent. Buyers have flat budgets. They already own tools they barely use, and they distrust AI claims. They want three things: fixes for their data, scores that reach the channels they already run, and proof of incremental value in money.

---

### 1. CMO / marketing head

**Ranked wants**
1. **Measured business results, not AI promises.** Gartner (Oct 2025) found that [45% of martech leaders say vendor-offered AI agents miss the business performance promised](https://www.gartner.com/en/newsroom/press-releases/2025-10-29-gartner-survey-finds-45-percent-of-martech-leaders-say-existing-vendor-offered-ai-agents-fail-to-meet-their-expectations-of-promised-business-performance) **[moderate]**. This is perception data. The sample size is unchecked. Gartner also expects more than 40% of agentic projects to be cancelled, citing inadequate risk controls as a main reason **[moderate]**.
2. **More output from a flat budget.** In the [Gartner 2025 CMO Spend Survey](https://secure.businesswire.com/news/home/20250512782208/en/Gartner-2025-CMO-Spend-Survey-Reveals-Marketing-Budgets-Have-Flatlined-at-7.7-of-Overall-Company-Revenue), budgets were flat at 7.7% of revenue and 59% of CMOs said that was not enough for their strategy **[strong]**. The sample was 402 CMOs, mostly in North America and Europe, so it is not India-specific. A 2026 edition may have replaced it.
3. **Value from tools they already own.** Martech utilization is consistently low. Secondary sources say 49% for 2025, but Gartner's own series has swung between 33% and 58% across years, so read it as "low", not as a trend **[weak]**. One publisher's reader survey and commentary say [rip-and-replace pitches no longer fit buyers](https://martech.org/the-rip-and-replace-pitch-is-out-of-step-with-todays-buyers/) **[weak]**.
4. **GenAI that saves time.** CMOs report GenAI ROI as time efficiency (49%), cost efficiency (40%) and more output capacity (27%) [Gartner CMO Spend 2025](https://secure.businesswire.com/news/home/20250512782208/en/Gartner-2025-CMO-Spend-Survey-Reveals-Marketing-Budgets-Have-Flatlined-at-7.7-of-Overall-Company-Revenue) **[moderate]**. The split was not re-checked.

**Top complaints:** shelfware, cost, and "agent-washing" (products sold as autonomous agents that fall short of the claim).

**Why they switch:** The [2025 MarTech Replacement Survey](https://martech.org/features-drive-marketers-to-replace-martech-but-cost-is-top-of-mind-for-new-apps/) gives these reasons: features (36–48%), cost reduction (43.8%, nearly double the prior year) and AI capability (37.1%) **[weak]**. The sample is self-selected and the figures use inconsistent bases. Overall, replacements are slowing.

**AI they trust:** Assistive GenAI that measurably saves time. Decision AI with narrow scope, clean data and human oversight. They do not trust autonomous agents.

**For Marketing AI:** Lead with the control-group result (net value in rupees, with a 95% range), not "AI agents". Present the product as an intelligence layer on top of the client's existing CRM and engagement stack, not a new platform. Report GenAI value as analyst hours saved per RCA summary and per copy variant, next to money uplift.

---

### 2. CRM / CVM campaign manager

**Ranked wants**
1. **Scores inside the tool where campaigns are built.** In an old, undated survey, only [24% of CDP owners were satisfied with integration to campaign endpoints and 22% with personalization](https://martech.org/ninety-percent-of-marketers-say-their-cdp-doesnt-meet-current-business-needs) **[weak]**. The survey is possibly pre-2023. In a Hightouch case study, ML outputs "only existed in the data warehouse" and Braze could not use them **[vendor claim]**. CleverTap sells warehouse exports as a paid add-on, which shows buyers pay for data-out **[weak]**.
2. **Live campaigns in weeks.** Reviewers name Braze's 3–6 month implementation as a top complaint, and WebEngage reviews cite 3+ months to get started **[weak]**. A Lagos operator case highlights launching in 4 weeks **[vendor claim]**. Pega markets 8–12 weeks. Adobe Journey Optimizer reviewers report about 4 months to implement and about 11 months to ROI **[weak]**.
3. **The right offer, and one decision per customer.** Every incumbent telco pitch (Subex, Flytxt, Comviva) leads with next-best-offer. Verizon's engine covers 200+ offers across 5 lines of business (Pega, pega.com/node/112471) **[vendor claim]**. CX Today names "did another campaign reach them first" as a core buyer worry **[weak]**.
4. **Smaller holdouts.** Managers see control groups as "money left on the table". Optimove built automatic control-group shrinking: it starts after about 3 runs and is 16–20% smaller after about 57 **[vendor claim]**.
5. **Prepaid-native signals:** recharge lapse, MNP port-out requests, usage revival. Contract-style churn flags do not fit **[weak]**.

**Top complaints:**
- MoEngage's interface overwhelms new users, and buyers find it expensive.
- CleverTap's setup is more complex ([G2 comparison](https://www.g2.com/compare/clevertap-vs-moengage): CleverTap 4.6/5, MoEngage 4.5/5) **[weak]**.
- CleverTap's "MAU based pricing with a flawed sense of MAU calculation" and 1.2x overage. Under MAU pricing, the inactive customers that churn and win-back campaigns target still cost money **[weak]**.
- Metric definitions are not standardised, and CEP reports show data discrepancies. Every Indian telco CEP case study found (Airtel Xstream, Tata Play, Vodafone) reports CTR, MAU or conversion uplift. None reports incremental churn saved against a control group **[weak]**.

**Why they switch:** Pricing model, integration pain and missing features. Netcore's 75/25 outcome-linked pricing suggests buyers want vendors to share the risk **[vendor claim]**.

**AI they trust:** Agents with a "full activity log" and marketer-defined guardrails, which is how MoEngage and Aampe market theirs. Also automated insight cards that come with a one-click action, as in Optimove's Optibot.

**For Marketing AI:** This persona is our biggest risk. The output stops at scores.csv, which is exactly where campaign managers say tools fail them. Binary-only treatment and a plain "recently contacted" cap also fall short of their next-best-offer expectation. Our strength for them is incremental measurement, which their CEP does not provide.

---

### 3. Data / analytics team

**Ranked wants**
1. **Data readiness before modelling.** About half of martech leaders say they lack the technical and data readiness for AI agents (Gartner, Oct 2025) **[moderate]**. Only [26% of marketers are fully satisfied with their data unification](https://www.salesforce.com/news/stories/state-of-marketing-2026/) **[vendor claim]**. Salesforce sells data unification, and its "60% more likely to use agents" figure is a correlation. Salesforce paying about $8B for Informatica points the same way **[moderate]**.
2. **Correct labels and no leakage.** Indian prepaid churn has no contract event. Definitions vary (inactivity, recharge lapse, port-out), and dual-SIM behaviour blurs them. One academic Indian telco study reported 99.97% accuracy, a typical sign of leakage **[weak]**.
3. **Evaluation they can defend.** Uplift models are unstable across folds (Devriendt 2018). Qini is noisy and can rank biased models higher (2022 work and ICML 2025). Criteo needed 25M randomised rows for significant comparisons **[moderate]**: these are academic sources, not re-read in full.
4. **Clear data-sufficiency rules.** Klaviyo publishes its thresholds: 500 ordering customers, 180 days of history, orders in the last 30 days **[moderate]**.
5. **No data copy.** They want the tool to work where the data already sits.

**Top complaints:**
- Steep learning curve and difficulty understanding what the AI is doing. This is the most-cited G2 con for Braze Decisioning Studio **[weak]**.
- Surprise bills in SageMaker Canvas: $1.90/hour while idle, about $150 for unused time **[weak]**.
- A data-team ticket for every campaign audience.

**Why they switch (or build):** Do-it-yourself is always on the table. Canvas runs in the same AWS account, and BigQuery ML costs a few dollars per TiB. Prediction-only tools that never reach production get dropped. Obviously AI reportedly wound down, but that rests on a single secondary source **[weak]**.

**AI they trust:** Models they can inspect, test and calibrate. That means intervals instead of a single AUUC, and predicted lift checked against observed lift.

**For Marketing AI:** Data Doctor, the one-row-per-customer builder, future-leak checks and AutoGluon match this persona well. The gaps:
- data-sufficiency gates per use case
- a prepaid churn-label builder
- an identity-linkage check (can each customer be reached on the planned channel?)
- uncertainty bands and a significance test on Qini
- an audit of how treatment was assigned in historical data

We also need a "why not just SageMaker Canvas?" battlecard. Its main points: uplift, approval workflow, DPDP and measured ROI.

---

### 4. CIO / CDO / security and privacy

**Ranked wants**
1. **Data stays in their own account.** Gartner's 2026 CDP Magic Quadrant praises warehouse-native, zero-copy design, and Amperity (BYOC) and Treasure Data (Live Connect) answer the same demand **[moderate]**. Under DPDP, the largest penalty (INR 250 crore) is for failing to keep reasonable security safeguards **[moderate]**.
2. **Output that is compliant out of the box.**
   - DPDP core obligations apply from 13 May 2027, and consent managers from 13 Nov 2026 **[moderate]**.
   - Under TCCCPR 2025, inferred consent ends with the contract. This hits win-back of lapsed customers directly **[weak]**.
   - RBI's draft rules say consent to transactional alerts is not marketing consent **[weak]**.
   - Each action needs the right promotional/service/transactional category and a matching DLT template **[weak]**.
3. **Audit evidence for DPIAs and auditors.** That means model approval history, PII used, drift history, and logs kept 1–3 years. Erasure must also flow into derived datasets and models, with 48-hour notice in Third-Schedule cases **[moderate]**.
4. **Risk controls that make AI deployable:** human approval, separation of duties, audit. Gartner reports only 30% of marketing leaders have mature AI readiness (via CX Today) **[weak]**.
5. **Vendor neutrality.** Census (now Fivetran), mParticle and ActionIQ have all been acquired since 2024 **[moderate]**. Forrester praised ActionIQ for serving both marketing and IT **[moderate]**.

**Top complaints:** PII in SaaS tools, opaque credit-based usage, and steep integration outside a single-vendor stack, as reported for Adobe AJO **[weak]**.

**Why they switch:** Vendor lock-in risk and data residency. Indian CEPs have responded with MoEngage tokenized sending, WebEngage auto-encryption and CleverTap's AWS India data centre.

**AI they trust:** Systems that are auditable, human-approved and keep data in their own account. Which LLMs they trust for customer-facing copy (Bedrock-hosted vs OpenAI), and whether they require India residency, is still an open research question.

**For Marketing AI:** This is our strongest fit: the product runs in the client's AWS, has a maker-checker Approver, an append-only audit log, and DPDP consent, retention and erasure. The gaps:
- a consent and contactability gate on every scored row
- a regulated action catalogue (category, sender series, DLT template ID)
- a one-click DPDP/DPIA evidence pack
- erasure that propagates into training snapshots

---

### 5. CFO / finance

**Ranked wants**
1. **Value in money, with confidence ranges.** Only 52% of marketing leaders can prove value and get credit for it, and CFOs are the most sceptical executives at 40% (Gartner 2024) **[moderate]**. Practitioners say first holdouts often show only 30–60% of attributed email revenue is incremental **[weak]**. That figure is e-commerce, not telecom.
2. **No wasted retention offers.**
   - In Ascarza's telecom work, risk-based targeting gave +2% retention against +8% for uplift-based, and only about 16% of the top-risk group was receptive **[moderate]**. These numbers need a full-text check.
   - A proactive telecom offer raised churn from 6% to 10% in a 65k-customer experiment **[moderate]**.
   - [56% of consumers are spending as if in a recession](https://www.gartner.com/en/newsroom/press-releases/2025-11-25-gartner-marketing-survey-finds-56-percent-of-consumers-are-already-spending-like-its-a-recession) **[moderate]**. This is self-reported and probably US data.
3. **Predictable, visible cost.** Small Australian CDP users report ["bill shock"](https://ami.org.au/knowledge-hub/bill-shock-api-blowouts-and-vendor-pile-on-cool-cdp-bender-but-roi-starting-to-climb-as-news-corp-carsales-sca-tas-uni-compare-club-make-customer-data-pay/) **[weak]**. The anecdote comes from a few practitioners at one event. Price anchors:
   - Vendr medians: Braze about $88.8K/yr, Optimove about $173K/yr
   - Braze Decisioning Studio list price: $300K/yr
   - Incumbent NBA year-one cost: $0.5M+ including services
   - Indian CEP enterprise contracts: INR 15–40 lakh/yr, with 15–30% first-contract discounts

   All of these are **[weak]**.
4. **Vendors who share risk.** Examples are Netcore's 75/25 model, Intercom's $0.99 per resolution and Flytxt's outcome-based SaaS **[vendor claim]**.
5. **Buying through existing cloud procurement.** Since Nov 2025, AWS Marketplace supports INR private offers with GST **[moderate]**.

**Top complaints:** Vendor results with no control group, such as Flytxt's 33% save rate and a BPO's 70% save rate **[vendor claim]**. The cost of holding out a control group. Usage-based bills.

**Why they switch:** Cost, which is now almost as strong a replacement driver as features **[weak]**.

**AI they trust:** Measurement they can audit. This means a pre-registered metric, a randomised holdout, and paying only on the lower bound of the confidence interval.

**For Marketing AI:** Our randomised control, 95% ranges and net value are exactly what a CFO asks for. To add:
- a one-page CFO report showing "gross saves vs incremental saves" and wasted offer cost
- CLV/ARPU weighting (missing today, so the list optimises heads saved, not revenue saved)
- a cost estimate before each run
- an optional outcome fee tied to the confidence-interval lower bound

---

### 6. Frontline (call-centre, save desk, retail)

**Evidence is thin here, and all of it is vendor-published.**

**Ranked wants**
1. **Next-best-action with a reason at the moment of contact.** T-Mobile reps get flags for likely cancellers plus suggested fixes. At VodafoneZiggo, agents accepted 45% of recommendations, agent NPS rose 25 points and handle time did not increase (Pega, pega.com/node/158131 and /node/82241) **[vendor claim]**.
2. **Help at the decisive moment.** That means MNP port-out or a recharge lapse, in the local language. Oriserve's Vi voicebot reported 4.1x better retention mid-port-out, the strongest Indian telco retention evidence found **[vendor claim]**.
3. **Save-desk offer guidance.** Save desks use tiered offers by tenure, reason and lifetime value, and they give discounts to customers who would have stayed anyway **[weak]**.

**Complaints and switching:** No direct evidence was collected. The research did not cover retail or retailer-led channels at all.

**AI they trust:** Suggestions with a short reason that the agent can accept or ignore, and no extra work.

**For Marketing AI:** We have nothing for this persona today, even though we already produce band, reason and action. The cheapest bridge is an "agent card": top 3 reasons, recommended offer tier and a "likely sure thing, do not discount" flag. Deliver it as CSV, plus a read-only lookup an Amazon Connect or CRM screen can call. That closes most of the perceived real-time gap without a streaming engine.

---

### Cross-persona synthesis: the 10 needs that recur

| # | Need | Personas who raise it | Evidence | Marketing AI today | Concrete product implication |
|---|---|---|---|---|---|
| 1 | **Proof of incremental value in money** | CMO, CFO, CVM, analytics | Gartner 45% agent shortfall [moderate]; 52%/40% CFO scepticism [moderate]; Indian CEP case studies report no controlled churn saves [weak] | **Strong.** Randomised control, 95% ranges, net value | Make it the pilot's contracted success metric. Add a CFO report (gross vs incremental), a power and sample-size planner, a persistent universal holdout, and adaptive holdout shrinking to blunt "lost revenue" objections |
| 2 | **Activation into existing channels** | CVM, CMO, frontline | 24% endpoint satisfaction [weak]; Hightouch "outputs only in warehouse" [vendor claim]; exports sold as paid add-ons [weak] | **Missing.** scores.csv only | Ship an "Activation Contract": a versioned scores table in the client's Redshift/S3 (band, reason, action, treat_flag, holdout_flag, consent, UPDATED_AT), plus CleverTap/MoEngage/Netcore import formats and an SFTP drop. Carry the holdout flag through and check send logs for control contamination |
| 3 | **Data readiness and unification** | Analytics, CIO/CDO, CMO | Half lack data readiness (Gartner) [moderate]; 26% satisfied [vendor claim]; [CDP failures are organisational](https://customerthink.com/when-cdps-fail-insights-from-the-cdp-institute-survey/) (46% vs 12%) [weak] | **Strong.** Data Doctor, one-row builder, leak checks | Lead demos with a readiness score. Add data-sufficiency gates, a prepaid label builder, identity linkage, and a named data-quality owner in the audit log. Sell a 2–4 week Data Readiness Assessment as the entry offer |
| 4 | **Explainable scores with an action** | CVM, frontline, analytics | [Churn scores go unused without drivers and actions](https://www.scoopanalytics.com/blog/the-hidden-cost-of-black-box-ai) [weak]; Decisioning Studio's "can't tell what AI is doing" [weak] | **Partial.** Band, reason and action exist; reasons may read as feature names | Rewrite reasons in business terms ("3 dropped calls in 14 days"). Add a risk × responsiveness quadrant and an exportable model card for the Approver |
| 5 | **Predictable, visible cost** | CFO, CMO, analytics | Cost 43.8% as a replacement driver [weak]; bill shock [weak]; Canvas idle bills [weak] | **Partial.** Runs at cost in the client's AWS, but spend is not shown | Show a cost estimate before every run, a monthly spend panel (SageMaker, LLM tokens, per 1,000 scored) and budget alerts. Price per use case, not per profile or MAU |
| 6 | **Fast time to value and good support** | CVM, CMO, analytics | Braze 3–6 months, WebEngage 3+ months [weak]; Lagos in 4 weeks [vendor claim]; [Pecan praised for simplicity and support](https://g2.com/products/pecan/reviews) [weak] | **Partial.** Guided setup and RAG assistant, but no measured KPI | Track "time to first scores" and "time to first measured campaign". Commit to a 6–8 week pilot plan with Minfy data scientists |
| 7 | **Human-governed AI, not autonomous agents** | CMO, CIO, CFO | Gartner 45% and >40% cancellations [moderate]; MoEngage and Aampe market guardrails [vendor claim] | **Strong.** Approver gate, no self-approval, audit, drift | Position as "governed decision intelligence". Add insight cards (drift, sleeping dogs, challenger ready). Offer an optional policy-based auto-promotion later |
| 8 | **Data control and compliant output** | CIO/CDO, CVM | Zero-copy praise [moderate]; DPDP INR 250 crore penalty [moderate]; TCCCPR and RBI consent rules [weak] | **Strong** on data control. **Partial** on contactability | Add a consent and contactability gate with suppression reasons, a regulated action catalogue (DLT category and template), a DPIA evidence pack, and erasure that flows into training snapshots |
| 9 | **Value-aware choice of who and what (offer, CLV, arbitration)** | CVM, CFO | Every telco CVM pitch leads with next-best-offer [vendor claim]; Ascarza and Lemmens & Gupta on value targeting [moderate] | **Missing.** Binary treatment, no CLV, only a frequency cap | In order: CLV/ARPU weighting on the profit curve, then P × V × L arbitration into one action per customer per cycle, then 2–4 offer arms as parallel per-arm uplift models |
| 10 | **Recommendations at the point of contact** | Frontline, CVM | T-Mobile and VodafoneZiggo [vendor claim]; Oriserve–Vi 4.1x [vendor claim] | **Missing.** No API, no agent output, no real time | Agent-card export plus a batch-scored lookup API (Amazon Connect). No streaming engine yet |

### What this means for Marketing AI

1. **We are strong where buyers are most sceptical:** measured incrementality, data readiness and governance. These match the top needs of the CMO, CFO and CIO. Make them the headline, and make the control-group net value the pilot's written success criterion.
2. **We are missing what campaign managers judge first:** getting the treat list into their CEP or call centre. The cheapest fix that matches what buyers ask for is a governed output table plus CleverTap, MoEngage and Netcore file formats. A full reverse-ETL product is not needed. This is the most urgent build because it gates use, and use gates renewal.
3. **Next comes value-aware decisioning:** CLV weighting, then arbitration, then multi-offer. This is where competitors (OfferFit, now part of Braze, plus Optimove, Pega, Flytxt and Comviva) compete. We should build it in batch, on top of our existing uplift and approval workflow.
4. **Research gaps to close before external use:**
   - real practitioner quotes (Reddit and Hacker News were not collected)
   - which CEP and CVM stack the pilot telco runs
   - Indian buyer pricing sensitivity
   - whether TRAI and DPDP consent rules allow win-back contact of lapsed prepaid customers at all

## Competitors I: engagement platforms and customer data platforms

**How to read this section.** All three research passes ran with web page fetching blocked, so every number below comes from search-result extracts. The verifier then checked those extracts against its own knowledge (to about June 2026), not against the live pages. Most quantified results are vendor-reported. Not one competitor result we found was measured against an independent randomised holdout. Bloomreach and Insider were not researched at all.

The overall picture is consistent. Built-in prediction is now a commodity: gradient-boosted trees, a 0-100 score, High/Medium/Low bands, and retraining every one or two weeks. Vendors now compete on **decisioning** (which offer, channel and time for each customer), on **measurement** and on **agents**. Our real advantages are causal targeting, honest measurement, telco-specific data, and running in the client's own AWS. Our real gaps are activation, multi-offer choice, and arbitration across use cases.

---

### 1. Global engagement platforms (CEPs) with built-in prediction

#### Braze
**What it is.** The market-leading CEP. It was named a Leader in the 2026 Gartner multichannel marketing hub (MMH) Magic Quadrant, alongside Salesforce and Adobe ([Braze](https://www.braze.com/resources/articles/braze-named-leader-2026-gartner-magic-quadrant)) **[moderate]**. Q2 FY2027 revenue was about $227.2M (+26% YoY) from about 2,789 customers ([Pulse 2.0](https://pulse2.com/braze-q2-revenue-rises-26-to-227-2-million-as-ai-adoption-accelerates/)). The verifier found these figures plausible but did not open the release **[moderate]**.

**How prediction works.** Predictive Churn trains a gradient-boosted tree on past churners and non-churners, using a churn definition the customer writes. Each user gets a 0-100 risk score. The model retrains every two weeks, and each retrain checks the previous two weeks of predictions against what users actually did ([Braze docs](https://braze.com/docs/user_guide/brazeai/predictive_suite/predictive_churn)) **[moderate]**. User scores are refreshed on a separate schedule that the customer sets (daily, weekly or monthly). Each plan also limits how many predictions can be active at once. Predictive Events works the same way for any event, with bands Low 0-50, Medium 50-75 and High 75-100 ([Braze docs](https://braze.com/docs/user_guide/brazeai/predictive_suite/predictive_events/analytics)) **[moderate]**.

**How it measures model quality.** Braze shows one "Prediction Quality" number: average lift over random targeting on held-out data. The bands are Poor 0-20%, Fair 20-40%, Good 40-60% and Excellent 60-100%. Fair or better counts as usable. At Poor, Braze tells the marketer to redefine the audience or the churn definition ([Braze docs](https://www.braze.com/docs/user_guide/predictive_suite/predictive_churn/prediction_analytics/prediction_quality/)) **[moderate]**.

**Decisioning.** Braze bought OfferFit in 2025, reportedly for about $325M. OfferFit is now BrazeAI Decisioning Studio. It uses reinforcement learning to pick the offer, message, channel and timing for each customer ([Braze](https://www.braze.com/resources/articles/what-is-offerfit)) **[moderate]**. The supporting evidence is a Forrester TEI study commissioned by Braze, and its numbers were not visible to us ([Forrester TEI](https://tei.forrester.com/go/Braze/AIDecisioningSpotlight/?lang=en-us)) **[vendor claim]**. Brinks Home reported a "200%-plus" retention improvement, but that was a press quote from around 2022 with no baseline or method ([VentureBeat](https://venturebeat.com/business/how-offerfit-uses-self-learning-ai-to-stop-customer-churn)) **[weak]**.

**Activation.** Cloud Data Ingestion (CDI) reads warehouse tables directly from Snowflake, Redshift, BigQuery, Databricks and Fabric, and reads files from S3. A user-attribute sync needs an `UPDATED_AT` column, an identifier such as `EXTERNAL_ID`, and a `PAYLOAD` JSON column ([Braze CDI](https://braze.com/docs/user_guide/data/unification/cloud_ingestion/integrations)) **[moderate]**.

**Pricing and reviews.** Pricing is not published. Vendr's median contract is about $89K a year ([Vendr](https://www.vendr.com/marketplace/braze)) **[moderate]**. G2 rates Braze about 4.5. Reported complaints are opaque pricing, AI spend that can't be forecast, 3-6 month rollouts, and the Liquid templating learning curve. Two of the three sources for these complaints are rival vendors (Vero, Maestra) **[weak]**.

#### Klaviyo
**Prediction.** Predictions switch on only once an account clears published data thresholds: 500+ customers with orders, 180+ days of order history, orders in the last 30 days, and some customers with 3+ orders ([Klaviyo help](https://help.klaviyo.com/hc/en-us/articles/360020919731-Understanding-Klaviyo-s-predictive-analytics)) **[moderate]**.

Klaviyo outputs:
- historic, predicted and total CLV
- predicted number of orders and average order value
- churn risk
- expected next order date
- predicted gender

Refresh is about weekly, but that figure comes from a third-party guide **[weak]**. Klaviyo has partly disclosed its CLV method as a "Buy Till You Die" (BTYD) style model combined with machine learning **[weak]**.

**What practitioners say.** Agency blogs say predicted CLV works better as a ranking than as a money forecast, and that next-order dates are weak for customers with only 1-2 orders ([Mailflow Authority](https://mailflowauthority.com/ai-email/klaviyo-ai-features)) **[weak]**.

#### Iterable
Predictive Goals let the marketer define any goal from profile fields or events. The AI scores each user's likelihood to reach it, keeps segments updated, and shows an "Explainable AI" view of the data behind each prediction. Brand Affinity turns engagement depth, recency and consistency into labels such as Loyal and Negative ([Iterable](https://iterable.com/blog/predictive-marketing-iterable-ai/)) **[moderate]**. Our sources have no pricing or named results for Iterable.

#### Optimove
**What it is.** A CRM marketing platform built around measurement. It was a Gartner Visionary in MMH 2026 and ranked 1st for Real-Time Personalization and Decisioning in Gartner's 2026 Critical Capabilities report **[moderate]**. That makes it the most relevant decisioning benchmark for us.

**Measurement.** Control Group Optimization resizes the holdout for each recurring campaign. When uplift is statistically credible, the control group shrinks. It never reaches zero, and it grows back if the campaign stops working. The mechanism is credible **[moderate]**. The reported figures, that shrinking starts after about 3 runs and the group is 16-20% smaller after about 57 runs, could not be checked ([Optimove Academy](https://academy.optimove.com/hc/en-us/articles/8699001780893-Using-Control-Group-Optimization)) **[vendor claim]**. Optibot (later renamed the AI Insights Agent) flags campaigns with no control group, campaigns that are underperforming, and campaigns ready for self-optimisation, each with a one-click fix **[moderate]**.

**Decisioning.**
- Self-Optimizing Campaigns pick the best variant for each customer.
- Self-Optimizing Journeys pick the best campaign for each customer among all the campaigns they qualify for, optimising for CLV **[moderate]**.
- A 2026 "AI Decisioning Studio" with four agents (journey, offer, content, send-time) is unverified. Its name may have been confused with Braze's product **[weak]**.
- An MCP server (June 2026) and the OptiGenie agent (26 Aug 2026) were reported but not verified **[weak]**.

**Pricing and reviews.** Vendr's median is $173,163 a year, with a range of $26,758-$536,541 ([Vendr](https://www.vendr.com/marketplace/optimove)) **[moderate]**. G2 rates it 4.6 from 234 reviews. Reviewers praise segmentation, A/B testing and support. They complain about complexity for new users and slow data loading **[weak]**.

**What this means for Marketing AI.** Our band + reason + action for each customer already goes beyond a bare Braze score. What we lack is what makes these products easy to adopt:
- **readiness gates:** Klaviyo-style "not ready yet because…" rules in Data Doctor
- **one plain quality number:** a Braze-style lift score with bands on the Approver screen
- **a forward backtest:** at each monthly replay, check last month's scores against actual churn
- **an adaptive holdout:** Optimove-style, for the recurring win-back campaign

Optimove's Journey/Offer decisioning and Braze's Decisioning Studio sit exactly on our known gaps: binary-only treatment and no arbitration across use cases.

---

### 2. Indian engagement platforms

#### MoEngage
**What it is.** About $100M FY25 revenue. It raised $280M in a Series F (Nov-Dec 2025) and cites 1,350+ brands **[moderate]**. It is in the 2026 MMH Magic Quadrant but not as a Leader.

**Prediction.** Uses the last 60 days of data. Horizons come only in multiples of 7 days, scores refresh every 7th day, and output is High/Medium/Low buckets. Templates include Uninstall and Dormancy ("no app or site open in the next 7 days") ([MoEngage docs](https://moengage.com/docs/user-guide/ai-and-intelligence/predict/create/create-predictions)) **[weak]**.

**Measurement.** Uplift = (flow conversion rate − control conversion rate) / control conversion rate. The docs we found show no confidence ranges, no money value and no per-customer effects ([MoEngage help](https://help.moengage.com/hc/en-us/articles/217909963)) **[weak]**.

**2026 moves.**
- **Merlin AI Custom Agents with an open MCP server** (3 June 2026), so Claude or ChatGPT can reach MoEngage data and tools ([PR Newswire Asia](https://en.prnasia.com/releases/global/moengage-launches-merlin-ai-custom-agents-with-full-visibility-marketer-defined-guardrails-and-open-mcp-architecture-535802.shtml)) **[strong]**.
- **Acquired Aampe** (24 June 2026, all cash), which runs one reinforcement-learning agent per customer to choose message, timing, frequency and channel ([Inc42](https://inc42.com/buzz/moengage-acquires-us-based-ai-startup-aampe-to-scale-agentic-marketing/)) **[strong]**. Aampe had only about 30 brands, and MoEngage's stated reason for the deal was winning enterprise migrations from Salesforce and Adobe, not telecom.
- **Aampe's measurement stance.** Aampe advises *against* external control groups. It estimates impact with switchback holdouts and matched synthetic controls instead ([Aampe docs](https://docs.aampe.com/docs/control-groups)) **[vendor claim]**. Its own randomised trials report a 21% cut in unsubscribes, and an 11-month RCT on 8.8M users found that gains faded unless people refreshed the content and strategy ([arXiv](https://arxiv.org/pdf/2604.08621)) **[vendor claim]**.
- **Partnership with Boldest (Prodapt)** (30 June 2026) to sell telcos a managed, "hands-off path to reduce churn" ([MoEngage](https://www.moengage.com/in-the-news/moengage-and-boldest-announce-a-strategic-partnership-to-drive-cognitive-backed-customer-engagement-for-telecom-operators/)) **[strong]**. Prodapt itself is about $1.5B revenue; the $3B figure is its wider group.

**Named telco results.** Airtel Xstream reported 61.4% mobile retention during a cricket series. Tata Play reported 2X MAU and 2X upsell revenue. Both are engagement metrics, not churn saved against a control **[vendor claim]**.

**Pricing.** About ₹2.5-5 lakh/yr (Growth) up to ₹25-40 lakh (Enterprise), plus implementation and WhatsApp add-ons ([Sequenzy](https://www.sequenzy.com/versus/moengage-vs-clevertap)) **[weak]**. Reviewers complain about cost, paid add-ons, weak reporting, and "non standardized definition of metrics across channels" **[weak]**.

#### CleverTap
**What it is.** FY25 revenue ₹523.1 Cr (+17.9%), with a profit ([Inc42](https://inc42.com/company/clevertap/financials/)) **[moderate]**. Named a Gartner Leader for Personalization Engines in Feb 2026 ([CleverTap](https://clevertap.com/news/press-release/clevertap-recognized-as-a-leader-in-latest-gartner-magic-quadrant-for-personalization-engines/)) **[strong]**.

**Prediction.** XGBoost. The model is rebuilt every 15 days and users are rescored every 24 hours. Up to 3 goal events, output in High/Medium/Low buckets ([CleverTap docs](https://docs.clevertap.com/docs/predictions)) **[weak]**. An RFM grid sorts users into 10 types, from Champions to Hibernating, and shows how users move between them **[weak]**. Scribe (2023) scores copy against five emotions. CleverAI agents followed in 2025-26 **[moderate]**.

**Measurement.** An account-wide random System Control Group (for example 2%) plus custom control groups per campaign **[weak]**.

**Integration.** Imports from SFTP and warehouses (including Redshift). Also accepts CSV custom lists and a 3-step Custom List API that uses pre-signed URLs **[weak]**. It has an India data centre on AWS.

**Named results.**
- Vodafone (global, not Vi): +15% onboarding click-through, 2x conversions.
- MobiKwik: 20% fewer uninstalls.
- An Indian private bank's own Next Best Offer model, delivered through CleverTap in-app messages ([CleverTap](https://clevertap.com/case-study/bank-boosted-business-10-engagement-analytics/)) **[vendor claim]**.

**Pricing.** Essentials from ₹6,000/month; Advanced about ₹5-12 lakh/yr. Overage is billed at 1.2x the base rate **[weak]**. Reviewers complain of "MAU based pricing with a flawed sense of MAU calculation", data mismatches between modules, and support staff turnover **[weak]**.

#### Netcore.ai
Rebranded on 30 July 2026, with seven agents plus human "MarTech growth engineers" who share accountability for the client's KPIs. Its published pricing model is 75% fixed and 25% variable, tied to "CRM Revenue" with quarterly true-ups, plus a kicker of up to 1% of attributed GMV above target ([Netcore](https://community.netcorecloud.com/plan-and-pricing/)) **[strong]**. **Nothing we found says how "CRM Revenue" is attributed.** Netcore is not in the 2026 MMH Magic Quadrant. Named BFSI results: Ujjivan SFB "200x ROI"; IndusInd 75% app activation in 75 days **[vendor claim]**.

#### WebEngage
The smallest of the four: about $22.8M ARR in FY25, about 50% of it from India **[moderate]**. It encrypts email and phone on ingestion and decrypts them only at send time **[weak]**. Airtel Africa runs digital customer value management on WebEngage across 150M+ subscribers. The "30%+ conversions" figure covers digital properties only ([Intelligent CIO](https://www.intelligentcio.com/africa/2024/06/24/airtel-africa-embarks-on-digital-customer-value-management-of-150-million-subscribers-with-webengage/)) **[vendor claim]**. List pricing starts from about $1,000/month. Reviewers cite onboarding "upwards of 3 months" **[weak]**.

#### Others a telco buyer will compare us with
- **Oriserve** (GenAI voicebot). Vi's best-documented retention result came from Oriserve, not a CEP: "4.1x better retention" among customers already in the number-porting (MNP) window, in local languages. The sources are staging WordPress pages **[weak]**.
- **In-house stacks.** Airtel Xtelify, JioBrain, Vi with IBM, BSNL Selfcare **[moderate]**. Airtel and Jio are unlikely buyers of a packaged product.
- **AWS reference: Dialog Axiata.** CatBoost plus an ensemble on SageMaker, about 100 features including network outages, a 45-day horizon, and separate training and inference pipelines. "Reduced churn within 3 months", with no control group ([AWS blog](https://aws.amazon.com/blogs/machine-learning/how-dialog-axiata-used-amazon-sagemaker-to-scale-ml-models-in-production-with-ai-factory-and-reduced-customer-churn-within-3-months/)) **[vendor claim]**.

**What this means for Marketing AI.** Indian CEP churn models predict *app dormancy* from in-app events over a 60-day window. They never see recharge gaps, billing shocks, call drops or porting requests. In a 2024 LocalCircles poll, 89% of respondents reported call-drop problems ([Tribune](https://www.tribuneindia.com/news/business/89-telecom-users-face-call-drop-issue-survey-641471)) **[weak]**.

No Indian telco case study we found reports churn saved against a control group. The vendors also mark their own homework: Aampe argues against external controls, and Netcore bills on attributed revenue. That gives us three openings:
- build the telco root-cause feature pack in Data Doctor
- act as the independent impact auditor next to whichever CEP the operator already runs
- produce the first published India telco number for "incremental churn saved per ₹, measured against a randomised control"

MoEngage + Boldest is our most direct competitor for the pilot, because Minfy has the same shape: a platform plus a services partner.

---

### 3. CDPs, composable stacks and reverse ETL

Most of these vendors do not build churn models. They own identity, audiences and data movement. What matters to us is how they pick up a score and deliver it.

| Vendor | Status (per sources) | Relevance to a predictive layer |
|---|---|---|
| **Hightouch** | Moved from reverse ETL to a "Composable CDP" for marketers. Reportedly a Leader in Gartner's 2026 CDP Magic Quadrant (unconfirmed). Raised an $80M Series C at a $1.2B valuation (2025) | Syncs warehouse attributes and segments to Braze and others. Its churn playbook: score on a schedule, write to a warehouse table, sync ([Hightouch](https://hightouch.com/es/playbooks/predict-churn-with-modelbit-salesforce-snowflake)) **[moderate]**. Has an AI Decisioning module (Snowflake-native) that overlaps with our next-best-action plans. Pricing: aggregators say ~$15K-$132K/yr; historically priced per destination **[weak]** |
| **Fivetran / Census** | Fivetran agreed to buy Census on 1 May 2025; the Census brand is being folded into Fivetran ([CMSWire](https://www.cmswire.com/the-wire/fivetran-signs-agreement-to-acquire-census-delivering-the-first-end-to-end-data-movement-platform-for-the-ai-era/)) **[strong]**. Fivetran also announced a merger with dbt Labs (Oct 2025) | Reverse ETL becomes a feature of a data-movement suite |
| **Segment (Twilio)** | Twilio kept it in 2024 while calling it underperforming ([CX Today](https://www.cxtoday.com/customer-data-platform/twilio-refuses-to-sell-segment-appoints-new-president-to-oversee-the-business/)) **[moderate]**. Self-serve plans still priced per tracked user (MTU) | Some telcos use it as their event layer; its Reverse ETL can read our table |
| **mParticle** | Bought by Rokt for a reported ~$300M (Jan 2025). "Up to 50%" performance claim **[vendor claim]** | CDP absorbed into ad-tech/commerce media; neutrality in question |
| **Amperity** | Quote-only, priced in "Amps" credits. Estimated $200K-$500K+/yr enterprise **[weak]**. "Bring Your Own Compute" runs on the client's lakehouse | Identity resolution partner; price shows how expensive the CDP layer is |
| **Treasure Data** | "No Compute" pricing (13 Aug 2025) charges on profiles to avoid "runaway bills". Hybrid CDP has a "Complete" mode and a "Composable" mode; Live Connect gives zero-copy access to Redshift and others ([Agile Brand Guide](https://agilebrandguide.com/treasure-data-introduces-no-compute-pricing-delivering-predictable-economics-with-hybrid-cdp-architecture/)) **[moderate]** | Shows buyers fear unpredictable warehouse compute bills |
| **Tealium** | Its own 2023 survey reports 90% satisfaction and 74% strong first-year ROI **[vendor claim]** | Compare with the CDP Institute (2021): 58% reported "significant value" **[weak]** |
| **Uniphore (ActionIQ)**, **Lytics (Contentstack)** | Acquired 2024-25. Uniphore praised for zero-copy design (claim relayed in its own press release) | More consolidation; roadmap risk for connectors |

**Why CDP projects fail.** CDP Institute data, as reported, shows consumer businesses citing organisational problems four times as often as the CDP not performing (46% vs 12%) ([MarTech](https://martech.org/the-hidden-reasons-your-cdp-project-is-failing/)) **[moderate]**. Practitioner lists name the same causes again and again:
- unclear goals
- no owner for data quality
- skills gaps
- confusion between offline records and online identity: importing offline data does not make those people reachable online **[moderate]**

**What this means for Marketing AI.** Do not build a CDP or a reverse-ETL engine. That layer is consolidating, getting cheaper, and turning into a bundled feature. The verifier also noted that big suites (Salesforce Data Cloud Zero Copy, Adobe Federated Audience Composition) now read warehouses directly.

Our pilot playbook should copy the failure lessons: a named business owner, a named data owner, one money KPI, and an identity-linkage check. That check confirms each scored customer has an ID a channel can reach (MSISDN, app user ID, hashed email).

---

### 4. Compact comparison

| Player | Model / method | Retrain / refresh | Data needs | How impact is measured | Pricing signal | Top complaint |
|---|---|---|---|---|---|---|
| Braze | Gradient-boosted trees; 0-100 score; RL decisioning (ex-OfferFit) | Retrain every 2 weeks; score refresh daily, weekly or monthly | Marketer-defined churn label and data windows | Prediction Quality (lift over random); vendor-commissioned TEI study | ~$89K/yr median | Opaque cost, 3-6 month rollout |
| Klaviyo | CLV, churn, next order (BTYD-style + ML, partly disclosed) | ~Weekly (third-party source) | 500 customers with orders, 180 days, recent orders | Not in sources | Not in sources | CLV not reliable as a money figure |
| Iterable | Custom-goal propensity + explanations | Continuous segment updates | Any event/field | Not in sources | Not in sources | Not in sources |
| Optimove | Self-optimising campaigns and journeys | Holdout resized between runs | Recurring campaigns | Adaptive control groups; insight bot | $173K/yr median | Complexity, slow data loading |
| MoEngage (+Aampe) | Buckets; per-user RL agents | Weekly | 60 days of app events | Relative CVR uplift; Aampe avoids external controls | ₹2.5-40 lakh/yr | Metrics inconsistent, add-ons |
| CleverTap | XGBoost, H/M/L, RFM | Model every 15 days; rescoring daily | ≤3 goal events | 2% system control group | From ₹6K/mo; 1.2x overage | MAU pricing, data mismatches |
| Netcore.ai | 7 agents + humans | Not in sources | Not in sources | "CRM revenue" (undefined) | 75% fixed / 25% outcome-linked | Not in sources |
| WebEngage | CEP/CDP | Not in sources | App/web events | Conversions | From ~$1K/mo | 3+ month onboarding |
| Hightouch | Reverse ETL + AI Decisioning | Scheduled syncs | Warehouse tables | Not in sources | ~$15K-$132K/yr (unverified) | Not in sources |
| Amperity / Treasure Data | Identity + profiles; hybrid/zero-copy | Not in sources | Not in sources | Not in sources | $200K+ / profile-based | Cost predictability |
| **Marketing AI** | AutoGluon + S/T/X uplift learners, reason codes | Monthly replay; PSI drift; human approval | Data Doctor builds the dataset from raw telco tables | Randomised control, 95% ranges, net ₹ | TBD | No activation, binary treatment only |

---

### 5. How the composable trend changes our score handoff

The standard pattern now looks like this:
1. A model scores every customer on a schedule.
2. It writes a **persistent, versioned table in the client's warehouse**.
3. A sync tool (Hightouch, Census, Segment) or the engagement platform's own ingest (Braze CDI, CleverTap/MoEngage S3 or warehouse imports) reads that table as a source.

Hightouch's own case study says the ML outputs "only existed in the data warehouse" until they were synced to Braze **[moderate]**. Our `scores.csv` has the right content in the wrong place.

We should define a stable **Activation Contract**, written to the client's Redshift/Postgres and S3 on every run:
- **IDs:** `customer_id` plus channel IDs (msisdn, app_user_id, email_hash)
- **Model fields:** `use_case`, `champion_version`, `score`, `band`, `reason_code`, `recommended_action`, `uplift_score`
- **Treatment and compliance fields:** `treat_flag`, **`holdout_flag`**, `consent_status`, `suppress_reason`
- **Timing and sync fields:** `valid_until`, `UPDATED_AT`, and a `PAYLOAD` JSON column (so Braze CDI can read it directly)

Three rules follow from the evidence:
- **Write only changed rows.** Sync tools and engagement platforms charge per row or data point, and re-sending unchanged rows re-triggers journeys.
- **Make the control group survive activation.** Export holdouts as suppressed. Read send logs back and raise an alert if any holdout ID was messaged. Otherwise our main differentiator breaks after the handoff.
- **Push minimal fields, never raw features or PII.** This matches MoEngage's tokenised sending and WebEngage's encryption. It also fits DPDP and the TCCCPR rule that inferred consent ends when the contract does, which matters for win-back. The consent-manager phase of DPDP starts 13 Nov 2026 **[moderate]**.

The same contract also feeds CleverTap/MoEngage imports, a generic SFTP drop for voicebot and call-centre vendors (where Indian telco saves actually happen), and the MoEngage MCP server.

---

### 6. Methods worth learning

1. **Data-sufficiency gates** (Klaviyo). For each use case, set minimum positive labels, months of history and recent activity, and show a clear "not ready yet because…" message.
2. **One plain-language quality score with bands and a next step** (Braze). Put it on the Approver screen next to AUC and Qini.
3. **Forward backtest at every retrain** (Braze). At each monthly replay, compare last month's bands with what actually happened.
4. **Adaptive holdout** (Optimove). Shrink the control share for recurring win-back once uplift is significant, keep a floor, grow it back if uplift fades, and log every resize.
5. **Insight cards with one-click actions** (Optibot). Flag no control group, results that aren't significant, PSI drift, a challenger ready to approve, and sleeping-dog segments. We already compute every one of these signals.
6. **Uplift-decay monitoring** (Aampe's 11-month RCT). Watch for fading realised lift, not just feature drift.
7. **Value-weighted decisions** (Optimove's CLV journeys, Klaviyo CLV). Rank by uplift × CLV − offer cost. ARPU × expected tenure, or BTYD for prepaid recharges, is cheap to add.
8. **One action per customer** (Optimove Journeys, Braze Decisioning Studio). Arbitrate across use cases before handoff. Then add a two-offer option (for example, data add-on vs discount) as the first step beyond binary treatment.
9. **Familiar views** (CleverTap RFM). Lay risk and uplift bands over an RFM grid with month-to-month movement between segments.
10. **MCP access governed by our roles and audit log** (MoEngage, Optimove).
11. **Outcome-linked pricing** (Netcore 75/25), but measured against our randomised control rather than attributed GMV.

---

### 7. What not to copy

- **Self-graded measurement.** Do not copy Aampe's argument against external controls, Netcore's undefined "CRM revenue", or relative-CVR uplift without ranges. Our randomised readout is the wedge, so keep it as the default.
- **App-event-only churn definitions.** "No app open in 7 days" is not prepaid churn, and it misses customers who rarely use the app and can only be reached by SMS, IVR or retail.
- **Fully automatic model promotion.** Braze and Klaviyo retrain on their own. Instead, offer an optional *pre-approved promotion policy* (promote only if the challenger beats the champion by X and passes drift checks, with every promotion audited), and keep human approval as the default.
- **Money-precise CLV.** Show it as ranks or bands. Practitioners don't trust Klaviyo's CLV in rupees.
- **Per-MAU or per-profile pricing and opaque quotes.** This is the top complaint for both CleverTap and Braze, and it would cost crores at telco scale. Use flat per-use-case pricing and show expected AWS/LLM cost per run.
- **Building our own channel connectors or CDP.** Census, mParticle and ActionIQ were all acquired within 18 months. A vendor-neutral table/S3 contract protects the client from that churn.
- **Headline numbers without a stated method** ("200%+", "200x ROI", "4.1x"). Publish the pilot result with the control design, the 95% range and net ₹ value.
- **Treating generated copy as a differentiator.** CleverTap has shipped Scribe since 2023. Our copy only stands out if it addresses the RCA reason for the segment and is checked in a randomised test.

## Competitors II: enterprise decisioning, predictive AutoML, AI decisioning and services firms

**How to read this section.** The research agents could not open most vendor pages, and the search budget ran out early. Almost every result number below comes from the vendor or from work the vendor paid for. Only a few claims were checked against primary sources: Google pricing, PyPI, the GitHub repos and the Braze–OfferFit deal. Where a verifier corrected the wording, the corrected version is used here. Several requested vendors have **no evidence in the corpus**: SAS CI360, H2O.ai, Salesforce Next Best Action outcomes, and Adobe Journey Agent. They are named as gaps rather than filled in from memory.

---

### 1. Enterprise decisioning suites: Pega, Salesforce, Adobe, SAS

#### Pega Customer Decision Hub (CDH)

**How it works.** Pega first filters actions with engagement policies: eligibility, applicability and suitability. It then ranks what is left by **Propensity x Context weighting x Business Value x Levers** ([Pega Academy](https://prod.academy.pega.com/topic/action-prioritization-ai/v3/in/45381)) **[moderate]**. The top action per channel, after constraints, becomes the next best action (NBA). Propensity comes from adaptive models that learn from responses. Fields from the configured customer class are offered as candidate predictors. The model activates the ones that pass a performance threshold and groups correlated ones ([Pega Academy](https://academy.pega.com/fr/topic/configuring-adaptive-model/v1/in/6991)) **[moderate]**. The verifier noted that this is *not* "every field by default".

**Claims vs reality.**

| Claim | Source | What we can actually say |
|---|---|---|
| Vodafone: -14% churn and a large NPS rise vs control | [Pega case](https://www.pega.com/node/82241) | Undated, no method given. "+50% NPS" is a relative change on a score, so the real move could be small **[vendor claim]** |
| VodafoneZiggo: 45% offer acceptance | same | Agent-assisted; method not given **[vendor claim]** |
| T-Mobile: +8 NPS through agent recommendations | [Pega case](https://www.pega.com/node/158131) | No baseline or timeframe **[vendor claim]** |
| Verizon: 2x VAS attach, +15% win rate, "12 weeks" | [Pega/Cognizant](https://www.pega.com/node/112471) | 12 weeks was phase one only. The 5-LOB, 200+ offer programme took years **[vendor claim]** |
| ~15% churn reduction by year 3, ~$385M/yr retained revenue | [Forrester TEI, Pega-commissioned](https://www.pega.com/forrester-tei) | Modelled composite of a tier-1 operator; does not scale down **[vendor claim]** |

**Cost and time.** Pega quotes CDH only on request. It is usually licensed by customer profiles or decision volume, not per user. Third-party estimates put licences in the low-to-mid six figures a year, and services often match or exceed the licence ([TrustRadius](https://www.trustradius.com/products/pega-customer-decision-hub/pricing)) **[weak]**. The researcher's year-one total of $0.5M–$1M+ is an inference, not a data point. Pega markets first value in 8–12 weeks. That conflicts with reviewer reports of slow first cycles ([Software Advice](https://www.softwareadvice.com/customer-communications-mngt/pega-customer-decision-hub-profile/)) **[weak]**.

**What customers say.** Gartner Peer Insights shows 4.6/5 from 107 reviews ([Gartner](https://www.gartner.com/reviews/market/multichannel-marketing-hubs/vendor/pega/product/pega-customer-decision-hub)) **[moderate]**. Reviewers praise decisioning depth. They complain about the learning curve, slow first cycles caused by rule and model setup, a cluttered UI, and needing dedicated ops and data teams. **Why deployments fail:** the corpus found no independent post-mortems. The recurring friction points are complexity, staffing and time to value.

#### Salesforce (Einstein, Data Cloud / Data 360, Agentforce)

Data Cloud was renamed Data 360 and is billed on consumption credits. Entry estimates vary widely, and some editions bundle credits ([Redress Compliance](https://redresscompliance.com/salesforce-data-cloud-pricing)) **[weak]**. Agentforce launched at $2 per conversation. From May 2025 it moved to Flex Credits at about $0.10 per standard action (about $500 per 100K credits). Salesforce also sells per-user and enterprise-agreement options ([Redress](https://redresscompliance.com/salesforce-agentforce-licensing-guide-2026.html)) **[moderate]**. The verifier warned that the $500/100K figure is Agentforce pricing and should not be read as Data Cloud pricing. **The corpus has no independent telco NBA outcomes or implementation-effort data for Salesforce.**

#### Adobe (AEP Customer AI, Journey Optimizer)

**Customer AI** predicts churn or conversion propensity from event data. It shows each profile's "influential factors" and lets users build segments on scores ([Experience League](https://experienceleague.adobe.com/en/docs/experience-platform/intelligent-services/customer-ai/overview)) **[moderate]**. It needs about 30 days of event data, but the verifier says the full requirements may be stricter. Its 2026 status is unconfirmed. It ranks by propensity and does **not** model uplift. **Journey Optimizer (AJO)** G2 reviewers report about 4 months to implement, about 11 months to ROI and a "$$$$$" cost rating ([G2](https://www.g2.com/products/adobe-journey-optimizer/pricing)) **[weak]**: the samples are small and self-selected. One reviewer reported timeouts early in implementation, which is an anecdote, not a pattern. Integration is steeper outside Adobe-centric stacks **[weak]**.

#### SAS CI360

**There is no evidence in the corpus.** The open question is how SAS's constrained-allocation optimisation compares with Pega arbitration.

**What it means for Marketing AI.** The incumbents set three buyer expectations. Results are judged against a control group. First value should come in 8–12 weeks. One decision layer should cover many offers. We already beat them on measurement: Adobe Customer AI stops at propensity, and Pega's headline numbers have no disclosed method. We lose on cross-use-case arbitration, CLV weighting and assisted-channel delivery. Pega was named a Leader in Forrester's AI Decisioning Wave ([press release](https://seekingalpha.com/pr/20132161-pega-named-a-leader-in-ai-decisioning-platforms-by-independent-research-firm)). We should not claim to be a "decisioning platform". We should sell uplift targeting and measurement that sits beside or in front of one.

---

### 2. Predictive AutoML and no-code ML

| Vendor | How it works / positioning | Cost signal | What happened |
|---|---|---|---|
| **Pecan** | Markets itself as a "Predictive AI Agent": a business question plus connected data becomes a model **[vendor claim]** | ~$760/mo Starter (2 prediction batches), ~$1,400/mo Team (10 batches), $50 per extra batch ([dupple](https://dupple.com/learn/best-predictive-analytics-tools)) **[weak]**: aggregator data, likely annual-discount rate | Active. Prices per scored batch, not per model |
| **DataRobot** | Horizontal AutoML/MLOps, now moving to agents | Not found | ARR ~$225M end-2023, +13% ([Sacra estimate](https://sacra.com/research/datarobot)) **[weak]**. 26% layoff in 2022 ([TechTarget](https://www.techtarget.com/ai/news/252524275/Troubled-AI-vendor-DataRobot-hit-by-more-layoffs)). Bought Agnostiq Feb 2025 ([BigDATAwire](https://bigdatawire.com/2025/02/10/datarobot-expands-ai-capabilities-with-agnostiq-acquisition)) **[moderate]** |
| **H2O.ai** | No evidence in corpus | — | — |
| **SageMaker Canvas** | No-code AutoML inside the client's AWS account | ~$1.90 per session-hour plus training; 160 free session-hours a month for 2 months ([AWS](https://aws.amazon.com/sagemaker/canvas/pricing/)) **[moderate]** | Users reported surprise bills from sessions left open ([re:Post](https://repost.aws/questions/QUfiwyktAVTY25nMRtODA6jA/unexpected-sagemaker-costs)) **[weak]**. AWS has since added idle shutdown |
| **Vertex AI / BigQuery ML** | SQL-based ML; Vertex rebranded "Gemini Enterprise Agent Platform" | BQML $312.50/TiB for linear/logistic models, $6.25/TiB plus passthrough for boosted tree/AutoML; Vertex AutoML tabular $21.252 per node-hour ([Google](https://cloud.google.com/bigquery/pricing), [Vertex](https://cloud.google.com/vertex-ai/pricing)) **[strong]** | Legacy AutoML Tables retired. Airflow provider 22.6.0 removed the AutoML operators entirely ([PyPI](https://pypi.org/project/apache-airflow-providers-google/)) **[strong]** |
| **Akkio** | Went vertical: white-label generative BI for agencies (Jan 2024), Horizon Media partnership (Oct 2024) ([BusinessWire](https://www.businesswire.com/news/home/20241007239865/en/5723803/Horizon-Media-and-Akkio-Usher-in-Transformational-New-Era-of-AI-Driven-Marketing)) **[moderate]** | Not found | Survived by narrowing to one vertical |
| **Faraday** | ~1,500 US consumer attributes, scores pushed into HubSpot **[vendor claim]** | Not found | Claims $41k/month saved on affiliate lead buying ([case](https://faraday.ai/stories/home-services-lead-purchasing)) **[vendor claim]** |
| **Obviously AI** (cautionary) | "Models in minutes", ~$99/mo (historical) | — | Rebranded Zams and pivoted to sales agents ([echai](https://basic.echai.ventures/stream/zams-wants-ai-to-move-from-assistant-to-teammate-for-sales-teams)). A shutdown is claimed by one low-authority source **[weak]** |

**Why these projects miss business impact.** The corpus has no sourced statistics on predictive-project failure rates. That was an explicit gap. The pattern from the pivots is consistent, though it rests on a small sample: tools that stopped at a prediction struggled. The ones that held on tied scores to an action (Faraday into CRM) or to a money result **[weak]**.

**What it means for Marketing AI.** AutoGluon is not a moat: AWS itself blogs it for churn. Pecan's pricing per batch confirms that the unit of value is a **scored list delivered each period**. That matches our monthly recipe replay. Canvas and BQML will come up as the cheap DIY option. We win only on what they lack: uplift, sleeping-dog avoidance, maker-checker approval, leak checks, DPDP handling and measured money value. We also run SageMaker in the client's account, so a Canvas-style surprise bill would damage Minfy's credibility. Cost estimates before each run and auto-stop are table stakes.

---

### 3. AI decisioning (reinforcement learning / bandits)

#### OfferFit, now BrazeAI Decisioning Studio

**Deal.** Braze announced the acquisition on 27 Mar 2025 at $325M and closed on 2 Jun 2025 ([Braze](https://www.braze.com/press-releases/braze-completes-acquisition-of-offerfit)) **[strong]**. The reported adjusted price was $303.2M.

**How it works.** A contextual bandit tests combinations of offer, message, channel and timing against customer features. It balances exploring new options with exploiting proven ones, inside guardrails the marketer sets ([Braze](https://www.braze.com/resources/articles/what-is-offerfit)) **[moderate]**.

**Results vs claims.** Brinks Home is cited for a "200%+ retention performance" and "450% contract extension value" ([VentureBeat](https://venturebeat.com/business/how-offerfit-uses-self-learning-ai-to-stop-customer-churn)) **[vendor claim]**, with no baseline or control. Liberty Latin America is cited for about +120% ARPU **[vendor claim]**. Independent field evidence for bandits is much more modest. Schwartz, Bradlow & Fader found about **8%** better acquisition than a balanced A/B design ([Marketing Science 2017](https://doi.org/10.1287/mksc.2016.1023)) **[moderate]**. Adaptive experiments also bias naive lift estimates ([Hadad et al., PNAS 2021](https://doi.org/10.1073/pnas.2014602118)) **[moderate]**.

**Cost and time.** The AWS Marketplace listing shows $300K for 12 months, which may be a placeholder for private offers ([AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-w6nqc7p5gtm42)) **[weak]**. Implementation is reported at 12–18 weeks with "forward-deployed data science", but may describe Braze in general **[weak]**. Revenue was $4.8M, $5.7M and $6.6M in successive quarters (about a $26M run-rate), mostly upsell to existing Braze customers ([Q2 FY27 call](https://www.fool.com/earnings/call-transcripts/2026/09/09/braze-brze-q2-2027-earnings-call-transcript/)) **[weak]**: unverified, and one quarter is missing. Braze has since introduced "Decisioning Studio Go", a near-self-serve tier priced in action credits **[weak]**.

**What customers say.** G2 rates it 4.7/5, but on only 15 reviews. Users praise the OfferFit team. The top complaint is a steep learning curve and difficulty understanding what the model is doing ([G2](https://www.g2.com/products/braze-ai-decisioning-studio/reviews)) **[weak]**.

**Why it fails.** RL needs large interaction volumes, and no vendor publishes a minimum **[moderate, practitioner consensus]**. A low-frequency win-back with a few thousand lapsed users learns slowly.

#### Aampe

Aampe runs one RL agent per user. Each agent decides content, timing, frequency, channel and **whether to send at all** ([Aampe](https://aampe.com/product/agentic-infrastructure)) **[vendor claim]**. It raised $18M in Dec 2024 and serves food-delivery and on-demand apps in South and Southeast Asia ([Reuters via TradingView](https://de.tradingview.com/news/reuters.com%2C2024-12-10%3Anewsml_GNE80kM6X%3A0-aampe-deploys-100-million-ai-agents-to-power-the-next-wave-of-personalization-for-consumer-apps-as-it-raises-18m)) **[moderate]**. It publishes no pricing. "100M agents" counts per-user model instances. It is not a measure of results.

#### Hightouch AI Decisioning

Hightouch is warehouse-native. It reads Snowflake, BigQuery, Databricks or a CDP. Agents pick message, timing and channel, learn from outcomes, and offer an optional holdout, for example 20% ([docs](https://site.vercel.hightouch.com/docs/ai-decisioning/agents)) **[vendor claim]**. It launched "Smart Suppression" on 8 Oct 2025 ([BusinessWire](https://www.businesswire.com/news/home/20251008017466/en/Hightouch-Launches-Smart-Suppression-to-Protect-Brand-Relationships-Without-Sacrificing-Conversions)) and was valued at $1.2B in its Series C ([Hightouch](https://hightouch.com/es/blog/hightouch-funding-series-c)) **[moderate]**.

**What it means for Marketing AI.** Between our batch uplift and "AI decisioning" there are three real gaps: choosing among several offers, learning from outcomes, and pushing to channels. At Indian telco win-back volumes, batch multi-arm uplift retrained monthly gets most of the value without real-time RL. Our sleeping-dog logic is the same idea as Aampe's "don't send" and Hightouch's Smart Suppression, but we don't sell it yet. No independent lift study exists for any of these products, so our mandatory randomised control is a real trust advantage.

---

### 4. Services firms and hyperscaler accelerators

**Hyperscalers give churn scoring away.**
- **AWS** publishes free *Guidance for Subscriber Churn Prediction and Retention*: S3, SageMaker, and feature importance in QuickSight ([AWS](https://aws.amazon.com/solutions/guidance/subscriber-churn-prediction-and-retention-on-aws)) **[vendor claim]**. AWS also has a blog on churn with AutoGluon-Tabular.
- The aws-samples repos are stale conference demos. One is a Connect real-time churn demo, last code commit June 2023 ([GitHub](https://github.com/aws-samples/real-time-churn-prediction-with-amazon-connect-and-amazon-sagemaker)) **[strong]**.
- **Google Telecom Subscriber Insights** (Feb 2023) is priced pay-as-you-go "for the population of subscriber data analyzed". It starts with a small PoC and includes SMS, in-app and SDK activation ([Google](https://cloud.google.com/blog/topics/telecommunications/introducing-telecom-subscriber-insights)) **[strong]**. Its landing page now returns 404, so its current status is unclear.
- Google's CLV sample gets only housekeeping updates ([GitHub](https://github.com/GoogleCloudPlatform/tensorflow-lifetime-value)) **[strong]**.

**Global SIs win through large platform deals.**
- TCS runs HOBS (BSS) and TwinX (AI personalisation) at Vodafone Idea ([TCS](https://www.tcs.com/who-we-are/newsroom/press-release/vodafone-idea-tcs-collaboration-ai-powered-customer-experience-platform)) **[vendor claim]**.
- Accenture's "GenAI Genie" claims -21% churn and +$2.50 ARPU over three years for an unnamed operator ([TM Forum](https://inform.tmforum.org/features-and-opinion/revolutionizing-telecommunications-with-ai-driven-customer-experience-ai-ops-a-collaborative-approach/)) **[vendor claim]**.
- Wipro claims 12–25% churn reduction in an undated, likely pre-2023 document ([PDF](https://www.wipro.com/content/dam/nexus/en/industries/communication-service-providers/latest-thinking/534-revenue-enhancement-and-churn-prevention-for-telecom-service-providers.pdf)) **[vendor claim; give no weight]**.
- HCLTech claims plan launches cut to 2–3 weeks ([HCLTech](https://www.hcltech.com/blogs/ai-powered-dynamic-plan-builder-a-solution-to-reduce-customer-churn-in-telecom)) **[vendor claim]**.
- Infosys Topaz churn work grew into contact-centre programmes **[vendor claim]**.

**Indian analytics firms sell "accelerator + team".**
- LatentView SolveIT CLV claims "reduce churn by 4% on average" (2021, not telco) ([PDF](https://www.latentview.com/wp-content/uploads/2021/03/case-studies-machine-learning-driven-customer-lifetime-value-analysis.pdf)) **[vendor claim]**.
- Fractal claims about 10% incremental sales (not telco) **[vendor claim]**.
- Mu Sigma sells decision-scientist teams that find churn drivers with NLP on case notes ([Mu Sigma](https://www.mu-sigma.com/industries/telecom/)) **[vendor claim]**.
- Tredence launched Customer 360 at Google Cloud Next 2025 ([CMSWire](https://www.cmswire.com/the-wire/tredence-unveils-customer-360-at-google-cloud-next-2025-the-ultimate-customer-intelligence-engine/)) **[weak]**.
- Brillio sells "agentic AI pods" **[vendor claim]**.
- None of these firms published a named telco churn result with a control group.

**Activation sits with martech and CVM vendors.** Airtel Africa reportedly runs digital CVM for about 150M subscribers on WebEngage ([Gulf News, sponsored](https://gulfnews.com/gn-focus/airtel-africa-embarks-on-digital-customer-value-management-of-150-million-subscribers-with-webengage-1.1719233959774)) **[weak]**. The verifier adds that dedicated telco CVM vendors were not researched: Comviva MobiLytix, Flytxt, Pelatro and Evolving Systems/Lumine. Several of them have historically used revenue-share pricing **[weak, verifier background]**.

**Benchmarks buyers carry.**
- McKinsey (c. 2017–18): analytics-driven base management cuts churn by "up to ~15%" ([McKinsey](https://www.mckinsey.com/industries/technology-media-and-telecommunications/our-insights/reducing-churn-in-telecom-through-advanced-analytics)) **[moderate]**. The 20–30% figure is unverified.
- A 2020 PR claimed a 50% churn cut **[vendor claim; discount]**.
- An Indian telco churn paper reported 99.97% accuracy, which almost certainly means target leakage ([SDMIMD](https://sdmimd.ac.in/marketingconference2024/papers/IMC2467.pdf)) **[weak]**.

**What it means for Marketing AI.** Churn scoring on AWS is free reference material that any AWS partner can deploy. Our pitch should be "the productised, governed, maintained version of AWS's churn guidance", which also gives us an AWS co-sell angle. Large Indian telcos already have GSI-owned BSS and CVM stacks. We should sell a bolt-on that proves incremental value and hands audiences to the existing campaign engine, not a replacement.

---

### 5. Comparison table

| Player | Core method | Incrementality / uplift | Activation | Cost signal | Time to value | Evidence quality | Threat to us |
|---|---|---|---|---|---|---|---|
| Pega CDH | Adaptive propensity + P×C×V×L arbitration | Control groups in case studies | Real-time, all channels | Six figures/yr + services **[weak]** | "8–12 wks" first use case; years at scale | Vendor-only | Low at mid-tier; high at tier-1 |
| Salesforce Data 360/Agentforce | Credits-metered CDP + agents | Not found | Native SF channels | Credits; $0.10/action | Not found | Pricing only | Medium if operator is on Salesforce |
| Adobe Customer AI / AJO | Propensity + influential factors | No uplift | Native AJO | $$$$$ | ~4 mo implement, ~11 mo ROI **[weak]** | Small G2 samples | Low-medium |
| SAS CI360 | No data | — | — | — | — | None | Unknown |
| Pecan | Predictive agent, auto data prep | Not shown | Not shown | ~$760–1,400/mo **[weak]** | "Fast" **[vendor claim]** | Aggregators | **High (product overlap)** |
| DataRobot / H2O | Horizontal AutoML → agents | No | No | Enterprise quote | — | Weak / none | Low |
| SageMaker Canvas | No-code AutoML in AWS | No | No | $1.90/session-hr | Days for a model | AWS pricing | **High (same account, DIY)** |
| Vertex / BQML | SQL ML | No | No | $6.25–312.50/TiB **[strong]** | Days, needs SQL skills | Strong (pricing) | Medium on GCP clients |
| Akkio / Faraday | Vertical: agencies / US consumer data + CRM push | No | Yes (CRM) | Not found | — | Vendor | Low in India |
| BrazeAI Decisioning Studio | Contextual bandits | Holdout as practice | Braze channels | ~$300K/yr list **[weak]** | 12–18 wks **[weak]** | Vendor; G2 n=15 | Medium if operator uses Braze |
| Aampe | Per-user RL agents, "don't send" | Not shown | Push/in-app | Not public | — | Vendor | Low (high-frequency apps) |
| Hightouch AI Decisioning | Warehouse-native RL + reverse-ETL | Optional holdout | Core strength | Not public | — | Vendor | Medium; also a possible partner |
| AWS churn guidance | SageMaker churn model + QuickSight | No | No | Free (infra only) | Partner-dependent | Vendor page | **High (free baseline)** |
| Google TSI | Churn/upsell/NBO + activation | Not shown | SMS, in-app, SDK | Per subscriber analysed **[strong]** | Small PoC first | Launch post; status unclear | Medium |
| GSIs (TCS, Accenture, Wipro, HCL, Infosys) | BSS/CDP transformation | Rarely disclosed | Via platform | Multi-year programmes | Years | Vendor | Medium (own the stack) |
| Indian analytics firms | Accelerator + decision-science team | Rarely disclosed | Via client tools | T&M / pods (no prices found) | Roadmap first | Vendor | **High (same buyer, same motion)** |

---

### 6. Methods worth learning

1. **Pega's arbitration formula, policies first.** Filter by eligibility, applicability and suitability, then rank by P × C × V × L. For us, P is uplift or propensity, V is action value (ARPU or margin, CLV later), and L is a marketer weight. Merge every use case's treat list into one "next best action per customer" file. This closes both the arbitration gap and the CLV-weighting gap with little ML work.
2. **A learning loop without giving up governance.** Pega and bandit vendors win on "self-learning". Our version: ingest campaign outcomes, label the next uplift training run automatically, and show "learned from last campaign" on the challenger card the Approver signs off.
3. **Batch multi-arm uplift, with an optional small exploration share.** Use 2–5 win-back offers plus a "no offer" arm, retrained monthly. That is the bandit idea at telco volumes. Pair it with a power/sample-size check that tells marketers how many arms their base can support.
4. **"Don't send" as a named action.** Aampe and Hightouch Smart Suppression show it sells. Package our sleeping-dog and budget-curve output as *Smart Suppression for telecom*, reporting SMS, OBD and call-centre cost saved and DND complaints avoided.
5. **Marketer-defined guardrails as a governed object.** Braze and Hightouch frame decisioning as acting inside an audience, action and guardrail set the marketer defines. Extend our Approver to approve the *action catalogue* (offers, costs, eligibility, caps) as well as models.
6. **Persistent universal holdout.** Vodafone's headline result is "exposed vs control". A stable global holdout lets us report cumulative incremental churn reduction across monthly replays.
7. **Agent-assist delivery.** The T-Mobile and VodafoneZiggo results came through human agents. An Amazon Connect lookup or CSV "agent card" (band, top reasons, action, script) closes most of the perceived real-time gap.
8. **Pricing on the recurring unit.** Pecan charges per batch and Google per subscriber analysed. Price per scored customer or per live use case, with an optional success fee that our randomised control can settle.
9. **Accelerator + team, sold through the hyperscaler.** Tredence launched through Google Cloud Next, and Braze relies on forward-deployed scientists. Minfy's equivalent is an AWS Marketplace listing plus a fixed-fee "retention pod".

**What it means for Marketing AI.** Items 1, 4, 6 and 8 reuse logic we already have and are small builds. Items 2, 3 and 7 are medium builds that answer the most common objections.

---

### 7. What not to copy

- **Unmeasured headline numbers.** Examples are "+450%", "50% churn cut" and "12–25%". Sophisticated buyers discount them. Never quote lift without the holdout and the interval. Use McKinsey's "up to ~15%" and LatentView's "~4%" as realistic anchors, so a 3–5% incremental reduction on the treated base doesn't read as failure.
- **Optional holdouts and naive adaptive-lift reporting.** Keep the control mandatory (see Hadad et al. above).
- **Opaque, quote-only or credit-metered pricing.** This is a complaint across Pega, AJO and Salesforce.
- **Multi-year rip-and-replace programmes.** That is the GSI shape. Ours is weeks to a measured result.
- **Real-time per-user RL for low-volume, low-frequency win-back.** It is under-powered and over-engineered for our pilot.
- **Opaque models that need vendor data scientists to explain them.** This is the top G2 complaint about Decisioning Studio.
- **Horizontal "any CSV → model in minutes" as the headline.** Obviously AI and DataRobot show this does not hold on its own.
- **Leaky "99.97% accuracy" models.** Show Data Doctor's leak findings in sales instead.
- **Cost meters left running in the client's account.** This is the Canvas lesson.
- **Tight coupling to hyperscaler ML APIs.** Google removed AutoML Tables, so keep the AutoGluon/SageMaker layer thin.

---

### 8. Minfy's most direct competitor

**The most direct competitor is the AWS-native DIY path: SageMaker Canvas or AutoGluon plus AWS's free churn guidance, built by the client's own team, another AWS partner or an Indian analytics firm.** This is our judgement from the evidence above, not a sourced finding.

- **Same account, same buyer, same tooling.** It runs where we run, uses the same AutoGluon, and the client's AWS team will raise it first. Licence cost is near zero ($1.90 per session-hour).
- **It covers our baseline story.** Churn scores plus driver charts is exactly what AWS publishes for free. The firms that would build it (Fractal, LatentView, Mu Sigma, Tredence) sell the same "accelerator + team" shape Minfy sells.
- **Product-for-product, Pecan is the closest analogue.** Its business-question → data prep → model flow mirrors our guided setup and Data Doctor, and it prices per scored batch. But the corpus shows no Indian telco presence for Pecan.
- **Pega, Braze and the GSIs compete for the tier-1 transformation budget, not for a fast pilot.** Their year-one cost and multi-quarter timelines put them in a different deal.

**How we win against it.** Everything the DIY path lacks, shown in the pilot:
- uplift with sleeping-dog suppression
- a randomised control with 95% ranges and net value in INR
- maker-checker approval and drift-triggered retraining
- leak checks and DPDP workflows
- a maintained "day-2" monthly replay, where free accelerators go stale

**Where we are exposed.** Every competitor tier ships some activation, and buyers weigh offers by value. Before the first head-to-head we need file-based export connectors (S3 drops in WebEngage, MoEngage, CleverTap or Netcore formats, or an Amazon Connect lookup) and CLV/ARPU-weighted treat lists. Multi-offer win-back is the next expansion requirement after a binary pilot.

## Telecom and India deep-dive

A note on sources first. All three research files were built from search-result extracts. Our verifier could not open most primary pages because the network was blocked. So "weakened" below usually means "not re-checked", not "wrong". Any number we plan to quote outside Minfy should be checked against the primary page first.

### 1. How telecom retention works in practice

#### Prepaid and postpaid are different problems

| | Postpaid (US benchmark) | Prepaid (India) |
|---|---|---|
| What counts as churn | Port-out or disconnection, which is a contract event | No contract event. Defined by inactivity, recharge lapse or MNP port-out, and each operator defines it differently |
| Monthly churn | Verizon consumer about 0.90-0.91% (Q2-Q3 2025). AT&T up from 0.85% to a reported 0.98% (Q4 2024 to Q4 2025) [moderate] ([Verizon Q3](https://verizon.com/about/news/verizon-reports-3q-2025-earnings-reiterates-full-year-financial-guidance)) | Jio 1.6%, Vi 3.8% blended (Q1 FY27). Airtel about 2.5-3% (older figure) [moderate] ([Jio Q1 FY27](https://telecomtalk.info/jio-adds-million-subscribers-q1fy27-arpu-rs215/1009790/), [Vi Q1 FY27](https://telecomlead.com/4g-lte/vodafone-idea-q1-fy27-revenue-hits-rs-11689-cr-arpu-rs-195-as-5g-expands-to-200-cities-127199)) |
| Annualised | About 11-12% | Roughly 25-50%. That is above the generic 15-30% global figure [moderate] |
| Main drivers | The operator's own price rises and competitor promotions | Tariff hikes, SIM consolidation, network quality, dual-SIM behaviour, and two-player share gains |
| Main levers | Structural ones: convergence bundles at AT&T, the care model at T-Mobile | Saves in the recharge-lapse and MNP windows, offers sent by voice or SMS, and network fixes |
| Success metric | Churn rate | Recharge within N days, repeat recharge, revenue retained |

Two Indian details matter for modelling.

- TRAI headline subscriber counts include inactive SIMs, so monthly net adds are not a churn measure. Active (VLR) data is better [moderate].
- In July 2024, tariff hikes set off a wave of port-outs to BSNL. The wave reversed by late 2024 [moderate] ([Business Standard](https://www.business-standard.com/companies/news/vodafone-idea-subscribers-porting-out-to-bsnl-after-tariff-hike-ceo-124081301608_1.html)). A model trained before a price event will drift sharply after it.

The best prepaid evidence is old but clear. At a large Chinese operator, the top 50,000 predicted prepaid churners were flagged with 0.96 precision, and retention campaigns matched to them significantly raised recharge rates [moderate, 2015] ([Huang et al., SIGMOD](https://users.wpi.edu/~yli15/Includes/SIGMOD15-huang.pdf)).

#### Save desks and offer economics

- **Save rate is not retention.** BPO vendors claim save-desk save rates of 64-76% [vendor claim] ([Foundever](https://foundever.com/case-studies/u-s-telecoms-provider-surpasses-its-customer-retention-targets-in-just-five-months/)). That counts callers who agreed on the call to stay. It does not count who was still a customer 90 days later. Many callers threaten to cancel only to bargain, so desks hand discounts to "sure things".
- **Blanket offers waste money and can backfire.** In an independent field experiment at a wireless operator, proactively suggesting better-fitting plans *increased* churn [strong] ([Ascarza et al. 2016](https://doi.org/10.1509/jmr.13.0483)).
- **Ranking by churn score is the wrong rule.** The highest-risk customers are often not the most responsive to treatment [strong] ([Ascarza 2018](https://doi.org/10.1509/jmr.16.0163)). Profit-based targeting beats probability-based targeting [strong] ([Lemmens & Gupta 2020](https://doi.org/10.1287/mksc.2020.1229); [Devriendt et al. 2021](https://doi.org/10.1016/j.ins.2020.12.075)).
- **Service is a lever too.** T-Mobile credits its Team of Experts care model with about 13% lower care costs, about 56% higher NPS and record-low churn [weak]. The figures come from the company and press, date from 2018-19, and are mixed up with pricing changes made at the same time.
- **Product holding helps.** AT&T says converged (fibre plus mobile) customers churn less [moderate]. Customers choose to converge, though, so this is correlation, not cause.

#### What realistic results look like

| Claim | Source type | Grade |
|---|---|---|
| Telenor: a churn-score mailing pushed churn among targets from about 9% to 10%. Treating only the 30% picked by an uplift model brought churn to about 7%, with about 11x campaign ROI | Vendor case (Portrait/Stochastic Solutions, about 2010) | [vendor claim] ([case](https://www.stochasticsolutions.com/pdf/uplift-modelling-for-retention.pdf)) |
| Flytxt: churn down 28-33%, usage up 9-14% | Vendor marketing, no control described | [vendor claim] ([Flytxt](https://flytxt.com/?p=6291)) |
| Comviva: 10x real-time campaign revenue, +21% repeat recharges, 8% peak incremental revenue | Vendor deck, unnamed clients, no baseline | [vendor claim] ([MobiLytix deck](https://www.comviva.com/wp-content/uploads/2025/03/MobiLytix-client-success-stories.pdf)) |
| CVM lifts revenue "up to 10%" | Consultancy ceiling, not an expected value | [weak] ([McKinsey](https://www.mckinsey.com/capabilities/growth-marketing-and-sales/our-insights/unlocking-the-value-of-personalization-at-scale-for-operators)) |
| Indian study: "retained 87% of potential churners" | Academic, no control group | [weak]. Not a treatment effect |

**Realistic expectation:** an incremental gain of about 1-2 points of churn in the targeted segment, not 30% [moderate]. Even the Telenor case only implies about 2 points against no treatment. The often-quoted "about 30% of propensity-targeted customers are sleeping dogs" comes from one consultancy's pre-2023 material. Our pilot should re-measure it, not quote it.

#### What CVM teams need

From the vendor pitches and the customer wants in the files:

1. Incremental, money-valued proof against a holdout. CVM heads are judged on ARPU and revenue, not AUC.
2. A first live campaign in about 4 weeks. A 6D Lagos case pitches 4 weeks and +6.22% net ARPU [vendor claim].
3. Prepaid-native outcomes: recharge, repeat recharge, revival of usage.
4. Next-best-offer across offer types (data bonus, discount, loyalty, upsell).
5. Managed data-science capacity, because operator CVM teams are thin.
6. Agent-facing next-best-action for care and save desks.
7. Root causes that include price changes, competitor moves and network quality.

**What this means for Marketing AI**

- Lead with Qini/AUUC, the profit curve and net ₹ against control. Do not lead with accuracy. Expect our numbers to look smaller than vendor numbers, and present that as honesty.
- Build a **prepaid churn-label builder** in Guided Setup and Data Doctor:
  - recharge-lapse and inactivity windows, with grace periods
  - MNP port-out flags and secondary-SIM detection
  - leak checks against the label window

  Offer "recharge within N days" and "revenue retained" as outcome labels. A wrong label is the most likely silent pilot failure.
- Add a **power and sample-size calculator** to win-back setup. With effects of 1-2 points, an underpowered pilot will produce a 95% range that crosses zero.
- Add a **"gross save vs incremental save" report**: treated-and-stayed % shown next to the control-adjusted lift and the offer cost wasted on sure things. This is our sharpest story for a CFO.
- Close the **CLV gap** in the budget simulator. Without ARPU or margin weighting, it misprices high-value customers.
- Package each customer's "reason + action" as a **save-desk card**, with a "likely sure thing, don't discount" flag.
- Add **market-event annotations** (tariff hike, competitor launch, 5G rollout) to the PSI drift and RCA views.

### 2. The CVM vendor ecosystem

| Vendor | What they sell | Evidence we have |
|---|---|---|
| Comviva (MobiLytix) | Real-time event-triggered CVM, loyalty, "Data Science as a Service" | Old Indosat case: 800+ attributes, points-based loyalty. The "59M subs" figure predates the 2022 merger [vendor claim] ([Comviva](https://www.comviva.com/resources/success-stories/how-indosat-grew-revenue-and-reduced-churn-with-a-targeted-loyalty-program/)) |
| Flytxt | AI CVM, churn and usage programmes | Claims of 28-33% churn reduction [vendor claim] |
| Subex | Next-best-offer engines | "About 20% sales lift", no baseline [vendor claim] ([Subex](https://info.subex.com/unlock-telecom-revenue-growth-with-ai-driven-next-best-offer-solutions)) |
| Amdocs, Whale Cloud, AsiaInfo | CVM integrated with the operator's BSS | Grouped with Flytxt and Subex on comparison sites (rfp.wiki is not an analyst) [weak]. No retention case retrieved |
| 6D Technologies, Monty Mobile | Telco CVM suites | 6D Lagos case: 4 weeks to launch, +6.22% net ARPU [vendor claim] |
| Evolving Systems | Historically real-time telco marketing | Reportedly sold its operating business around 2021-22. Current status **not verified** |
| Oriserve | GenAI voicebot | Vi: 4.1x better retention mid-port-out, in 7 circles [vendor claim, sourced from staging pages] |

Every incumbent bundles four things: real-time decisioning, multi-offer NBO, loyalty, and managed data scientists. Those are exactly our known gaps.

**What this means for Marketing AI**

Do not compete as a CVM suite. Position Marketing AI as an AWS-native **causal targeting and measurement layer** that feeds whatever campaign tool the operator already runs. Concretely:

- Scheduled S3/SFTP **export adapters** in the field layouts that Comviva, Flytxt, Amdocs-style tools import, with campaign-ID and control-flag columns.
- A **minimum multi-offer mode**: up to 3 offer arms plus control, modelled as parallel binary T-learners, picking the best expected-net-value offer per customer under a budget. This answers the NBO objection without a bandit engine.
- Sell the pilot as a **managed outcome**: Minfy data scientists run two monthly cycles, first campaign in 4-6 weeks.

### 3. The Indian market

| Operator | Q1 FY27 position | Likely buyer fit |
|---|---|---|
| Jio | 533.3M subs, churn 1.6%, ARPU ₹215.6 [moderate] | Low. Builds in-house (JioBrain: 500+ APIs, GenAI as a service) |
| Airtel | ARPU ₹264, a record 1M postpaid adds to 30M. Q1 FY27 churn not found [moderate] | Low for packaged churn. Builds and sells its own stack (Xtelify: Data Engine, IQ, Serve) |
| Vi | 193.1M subs, first positive net adds (about +0.3M), ARPU ₹195, churn 3.8% (from 4.4%) [moderate] ([Business Today](https://www.businesstoday.in/markets/stocks/story/vodafone-idea-q1-results-loss-narrows-to-rs3754-crore-as-revenue-rises-customer-arpu-reaches-rs195-548396-2026-08-10)) | High pain, but cash-constrained. Signed IBM for an AI hub. Government stake about 49% |
| BSNL | Relaunched its Selfcare app with an AI chatbot (Aug 2026). Ordered 26,300 more 4G sites [weak] | Plausible. State-owned, data must stay in India, values approvals and audit |

Context for the pitch:

- **Tariff hikes.** Morgan Stanley forecast 16-20% hikes in 2026. As of about August 2026 there was no industry-wide hike, only selective changes such as Airtel replacing its ₹299 plan with ₹349 [moderate]. The post-hike churn window has not opened yet.
- **Network quality is the top complaint.** In a 2024 LocalCircles poll, 89% reported call-connection or call-drop problems [weak; self-selected 2024 poll] ([Tribune](https://www.tribuneindia.com/news/business/89-telecom-users-face-call-drop-issue-survey-641471)).
- **AWS reference case.** Dialog Axiata predicts home-broadband churn 45 days ahead on SageMaker, using about 100 features including outage data [vendor claim] ([AWS](https://aws.amazon.com/blogs/machine-learning/how-dialog-axiata-used-amazon-sagemaker-to-scale-ml-models-in-production-with-ai-factory-and-reduced-customer-churn-within-3-months/)).

**What this means for Marketing AI**

- Target Vi, BSNL, regional ISPs and DTH/cable operators first.
- Pitch a **"tariff-hike retention readiness"** pilot: set up the randomised control and baseline before the hike, then measure the post-hike save.
- Build a **telco RCA feature pack** that joins:
  - cell- and site-level KPIs and outages
  - complaint tickets
  - recharge gaps
  - MNP/UPC requests
  - billing shocks
  - household and product holding (broadband, family SIMs, DTH)

  The goal is for RCA summaries to name network and billing causes, and for fault prediction to feed "fix before discount".
- For Airtel and Jio-style buyers, CLV weighting and postpaid migration matter more than raw churn.

### 4. Indian engagement platforms and their telco footprint

| Platform | 2026 moves | Telco footprint (all vendor-reported) | Churn modelling / measurement |
|---|---|---|---|
| MoEngage | Merlin AI Custom Agents and an open MCP server (3 Jun 2026). Bought Aampe for per-user RL decisioning (24 Jun 2026). Telco partnership with Boldest/Prodapt for a "hands-off path to reduce churn" (30 Jun 2026) [strong] ([PR](https://www.moengage.com/in-the-news/moengage-and-boldest-announce-a-strategic-partnership-to-drive-cognitive-backed-customer-engagement-for-telecom-operators/)) | Airtel Xstream (61.4% retention on mobile during a cricket series), Tata Play (2x MAU, 2x upsell revenue) | 60-day window, 7-day horizons, H/M/L buckets. Uplift reported as relative change in CVR [weak] |
| CleverTap | CleverAI agents. Gartner Leader for Personalization Engines (Feb 2026). FY25 revenue ₹523.1 Cr | Vodafone global (not Vi): +15% onboarding CTR, 2x conversions | XGBoost, rebuilt every 15 days, max 3 goal events. Account-wide random control group (e.g. 2%) [weak] |
| Netcore.ai | Rebrand on 30 Jul 2026: 7 agents plus "growth engineers". 75% fixed / 25% fee tied to "CRM revenue", attribution method undisclosed [strong] | BFSI-heavy: Ujjivan SFB, IndusInd | Not documented |
| WebEngage | About $22.8M ARR (FY25). Sold more on privacy and implementation | Airtel Africa: 150M+ subs, 14 countries, digital properties only | Onboarding "3+ months" in reviews [weak] |

Two points stand out.

- **None of the Indian telco case studies found reports incremental churn saved against a control group** [moderate; this is an absence claim]. Aampe even advises against external control groups and relies on switchback and matched synthetic controls [weak]. The vendors are effectively grading their own work.
- **CEP churn models see only app events.** Many prepaid churners rarely use the app and can only be reached by SMS, IVR or retail.

On integration, MoEngage imports from S3, SFTP and the main warehouses. CleverTap supports SFTP, warehouse imports and a 3-step Custom List API [weak; docs not re-checked]. Identity matching and refresh cadence still take real effort.

On rough pricing, enterprise MoEngage deals often run ₹25-40 lakh a year. CleverTap charges a 1.2x MAU overage. At telco scale, contracts run to crores [weak; third-party estimates].

**What this means for Marketing AI**

- Be the **independent impact auditor** next to the operator's CEP, not another CEP. Our edge: 95% ranges, net ₹ value, and Qini computed only on RCT data with balance disclosed.
- Ship **CEP-ready exports**: tokenised ID, band, reason code, action, treatment/control flag and consent-eligible flag, written to S3 in CleverTap and MoEngage formats. Add a generic SFTP CSV for voicebot and call-centre vendors.
- Consider the **MoEngage MCP server** as a cheap two-way path: push an approved segment, pull campaign results back for measurement.
- Run a **head-to-head in the pilot**: our uplift list against a CEP-style "dormancy" H/M/L list, on the same randomised campaign.
- Add **uplift-decay monitoring**. Aampe's own 11-month RCT on 8.8M users showed gains fading without human refresh [weak; vendor-authored].
- Price per use case or per model, not per MAU. An outcome share measured against control is more defensible than Netcore's attributed GMV.

### 5. Regulation as product requirements

#### DPDP Act and Rules 2025

| Date | What applies | Grade |
|---|---|---|
| 13 Nov 2025 | Rules notified. Data Protection Board provisions in force | [strong] ([EY](https://www.ey.com/content/dam/ey-unified-site/ey-com/en-in/alerts-hub/2025/11/digital-data-protection-act-rules-notified-by-meity.pdf)) |
| **13 Nov 2026** | Consent-manager registration (Rule 4) | [strong] |
| **13 May 2027** | Everything else: notices, safeguards, breach notification, retention/erasure, SDF duties, penalties | [strong] |
| Risk | In Jan-Feb 2026, MeitY proposed cutting the window to 12 months, which would make 13 Nov 2026 the full-compliance date. Not confirmed as notified | [moderate] ([Storyboard18](https://www.storyboard18.com/amp/digital/meity-seeks-industry-views-on-fast-tracking-dpdp-act-rollout-proposes-12-month-compliance-timeline-88332.htm)) |

Duties that become product features:

- **Consent.** Consent must be free, specific, informed, unconditional and unambiguous, and as easy to withdraw as to give [strong]. Section 7 "legitimate uses" allow some processing without consent. It is still open whether "service delivery" consent covers building churn models on usage and billing data.
- **Logs.** Personal data, traffic data and logs must be kept for **at least 1 year** (Rule 8(3)) [moderate]. The 3-year figure in the Third Schedule is *not* a log rule. It is the inactivity period after which large e-commerce, social-media (2 crore+ users) and gaming (50 lakh+) platforms must erase data. For those platforms, a **48-hour notice** must go out before erasure [moderate].
- **Breaches.** A detailed report to the Board is due within **72 hours** (Rule 7). Separately, CERT-In requires 6-hour incident reporting and 180-day log retention [moderate] ([CERT-In](https://www.cert-in.org.in/PDF/CERT-In_Directions_70B_28.04.2022.pdf)).
- **Significant Data Fiduciaries** (likely to include large telcos and banks): an annual DPIA and audit, due diligence that algorithmic software does not endanger people's rights, an India-based DPO, and possible localisation [moderate].
- **Penalty caps** set by the Board: up to INR 250 crore for security safeguards, INR 200 crore for breach notification, INR 200 crore for children's data [moderate] ([IndiaLaw](https://www.indialaw.in/blog/data-privacy/digital-personal-data-protection-act-2025-timelines-penalties)).
- **Consent managers** must be Indian companies with at least INR 2 crore net worth, run a certified interoperable platform, and be unable to read the data they handle [moderate]. We should consume consent managers, not become one.

#### TRAI TCCCPR, DLT, 140/1600 and the DCA pilot

- **Feb 2025 amendment** [moderate] ([TRAI PR](https://cms.trai.gov.in/sites/default/files/2025-02/PR_No.11of2025.pdf)):
  - promotional calls on the **140** series; service and transactional calls on **1600**
  - no 10-digit numbers for telemarketing
  - customers can complain within **7 days** without registering a DND preference
  - repeat violators lose **all telecom resources across all operators for 1 year** and are blacklisted [moderate]
  - access providers face reported graded penalties (about ₹2/5/10 lakh) and a complaint-action window cut from 30 to 5 days [weak]
- **SMS headers** carry suffixes: -P (promotional), -S (service), -T (transactional), -G (government) [weak].
- **Aug 2024 directions:** only whitelisted URLs, APKs, OTT links and callback numbers may appear in commercial SMS. The chain from principal entity to telemarketer must be traceable. Non-conforming messages are blocked at the network [moderate].
- **BFSI 1600 deadlines:**
  - commercial banks: 1 Jan 2026
  - large NBFCs, payments banks and small finance banks: 1 Feb 2026
  - other RBI-regulated entities: 1 Mar 2026
  - mutual funds/AMCs: 15 Feb 2026
  - qualified stockbrokers: 15 Mar 2026
  - insurance: separate notice

  About 485 entities held 2,800+ numbers by Nov 2025 [moderate] ([Outlook Business](https://www.outlookbusiness.com/news/trai-sets-firm-deadlines-for-bfsi-entities-to-adopt-1600-series-for-service-transactional-calls)).
- **Digital Consent Acquisition (DCA) pilot.** Run by TRAI and RBI with about 11 banks and the access providers, starting Dec 2025. Customers review or revoke old promotional consents via short code 127000, and the result is recorded on DLT. The national rollout status as of Oct 2026 is **unverified** [moderate] ([Business Standard](https://www.business-standard.com/industry/news/trai-rbi-to-begin-pilot-of-digital-consent-acquisition-125121001246_1.html)).
- **Win-back risk.** Under TCCCPR, inferred consent reportedly ends when the contract ends. That would directly limit win-back contact with lapsed customers. This needs legal confirmation before the pilot [weak].

#### RBI marketing rules for BFSI

RBI issued draft sales-practice directions in Feb 2026, proposed to take effect on 1 Jul 2026 [moderate] ([Vinod Kothari](https://vinodkothari.com/2026/02/rbis-draft-directions-on-sales-practices/)). The draft:

- treats consent to transactional alerts as **not** consent to marketing
- makes declining as easy as accepting
- bans making services conditional on marketing consent
- bans dark patterns, mis-selling incentives and forced bundling

Whether the draft was finalised, possibly as RBI/2026-27/115, is **unverified**.

#### WhatsApp and RCS

- Since **1 Jul 2025**, WhatsApp charges per delivered template. In India, marketing costs about **INR 0.86** and utility/authentication about **INR 0.13**, roughly 6.6x apart [moderate] ([Meta](https://developers.facebook.com/docs/whatsapp/pricing)). These are converted figures. Meta now bills in INR and revises rates often.
- Undelivered messages are not billed.
- Utility templates inside an open 24-hour service window are free.
- Click-to-WhatsApp ads open a 72-hour free window.
- **RCS was not researched.** Jio, Airtel and Vi readiness, pricing, and whether RCS falls under DLT scrubbing are all open.

**What this means for Marketing AI**

A treat list can no longer be just score × budget. It must be:

- filtered by purpose-, channel- and time-stamped consent
- tagged with the regulated category and sender type
- priced per channel
- reproducible for an auditor

Our approver gate, append-only audit, DPDP module and in-client-AWS deployment fit this well. INR 250 crore is the largest cap, and it attaches to security safeguards. The gaps are consent and channel awareness in the treat list, and SDF-grade evidence artefacts.

### 6. Product requirements checklist

| # | Requirement | Driver | Deadline | Size | Status today |
|---|---|---|---|---|---|
| 1 | **Consent & contactability gate**: join consent (purpose, channel, timestamp, source: CRM / DLT-DND / DCA / DPDP withdrawal) to every scored row. Output `contactable_by_channel` and `suppression_reason`. Drop suppressed customers before budgeting | TCCCPR 2025, RBI draft, DPDP s.6 | Now (TRAI). 13 May 2027 (DPDP), possibly 13 Nov 2026 | M | Missing. Only a "recently contacted" cap exists |
| 2 | **Consent snapshot ID** stored with every treat list. Require a fresh consent extract per campaign | DCA revocations on DLT | Now | S | Missing |
| 3 | **Consent reconciliation** check in Data Doctor: flag conflicts between CRM opt-in, DLT/DND, DCA and DPDP withdrawal, and stale consents | Several consent systems | Before 13 Nov 2026 | M | Missing |
| 4 | **CMP / consent-manager adapter** (consume, don't build) | Rule 4 | 13 Nov 2026 | M | Missing |
| 5 | **Regulated action catalogue**: each action carries category (P/S/T), allowed channels, sender series (140/1600, header suffix) and DLT template ID, validated when the list is built. Win-back = promotional; fault notice = service; payment reminder = transactional or service | TCCCPR, BFSI 1600 | Now (BFSI by Mar 2026) | S | Missing |
| 6 | **Win-back eligibility rule** for lapsed customers (inferred-consent expiry), signed off by legal | TCCCPR | Before pilot campaign | S | Missing |
| 7 | **Cost per contact by channel** in the profit/budget simulator, read from config (India defaults: WA marketing about ₹0.86, utility about ₹0.13, plus SMS, OBD, voice). Pick the cheapest consented channel | WhatsApp pricing since 1 Jul 2025 | Now | S | Missing |
| 8 | **Delivery-receipt return path**: intent-to-treat as the primary analysis, cost per incremental save computed on delivered contacts | Billing on delivery | Pilot | M | Missing |
| 9 | **DLT-template-constrained copy**: the LLM fills registered templates; the judge checks template match, whitelisted URLs, and RBI dark-pattern/false-urgency/bundling rules | TRAI Aug 2024, RBI draft | Now | S | Partial (LLM judge exists) |
| 10 | **Audit retention floor**: default ≥1 year, configurable longer, no early purge, pseudonymous IDs in logs. Note CERT-In's 180 days | DPDP Rule 8(3), CERT-In | 13 May 2027 | S | Partial (append-only audit) |
| 11 | **Erasure propagation**: erasure-due flag, exclusion from win-back (or 48-hour notice routing where the Third Schedule applies), purge from snapshots and recipes on the next replay, tombstones in the audit | DPDP Rule 8 | 13 May 2027 | M | Partial (DPDP erasure module) |
| 12 | **Purpose-aware lineage**: record a purpose per recipe and block scoring for purposes the source consent doesn't cover | DPDP s.5-6 | 13 May 2027 | M | Missing |
| 13 | **SDF evidence pack** per model and campaign: purpose, sources, PII fields, masking, builder vs approver, drift history, consent snapshot, suppression counts, plus a pre-filled DPIA template | DPDP s.10 / Rule 13 | 13 May 2027 | M | Data exists, no export |
| 14 | **Breach-readiness hooks**: access logs and an incident export that supports a 72-hour Board report (6 hours for CERT-In) | Rule 7, CERT-In | 13 May 2027 (CERT-In now) | S | Missing |
| 15 | **Complaint-risk guardrail** plus a "contacts avoided" KPI against a propensity-only list | 1-year disconnection for repeat UCC | Now | S | Missing |
| 16 | **Prepaid churn-label builder** (lapse windows, grace periods, MNP, secondary SIM) | Telco reality | Pilot | M | Missing |
| 17 | **Power/holdout calculator** plus adaptive holdout shrinking on monthly replays | 1-2 point effects | Pilot | S | Missing |
| 18 | **CLV/ARPU weighting** and **minimum multi-offer (≤3 arms)** | NBO is table stakes | Post-pilot | M | Known gaps |
| 19 | **CEP and CVM export adapters** (CleverTap, MoEngage, Comviva/Flytxt layouts, SFTP for voicebots) with tokens only, no PII | No activation | Pilot | S-M | Missing |

### Open questions to close before the pilot

- Which operator is it? Vi or BSNL economics differ sharply from Airtel or Jio.
- Which channels can the operator actually use for treatment, and with what lead times?
- Can the operator lawfully contact lapsed prepaid customers for win-back, and under which header?
- Has RBI finalised its directions? Did DCA go national? Did MeitY shorten the DPDP window?
- What does RCS cost in India, and does it fall under DLT scrubbing?
- Which CEP, if any, does the pilot operator run today?
- What does a "free" data bonus actually cost in margin, so we can set the per-customer offer budget?

## Market structure, pricing and go-to-market

*Evidence note: the researchers could not open most primary pages, because the network blocked them and the search budget ran out. Most figures below come from search-result extracts and the verifier's own checks. Where the verifier corrected a claim, the corrected version is used here.*

### 1. How the market is structured

#### Five camps sell into "marketing AI for churn and retention"

| Camp | Named players | How they make money | Where Marketing AI sits against them |
|---|---|---|---|
| Engagement hubs (MMH / CEP) | Adobe, Braze, MoEngage, CleverTap, WebEngage, Netcore, Optimove, Insider | Audience-size licence (MAU/MTU) plus channels and add-ons | We are not one. We have no channels. We should feed them. |
| Data / CDP / activation | Salesforce (Data 360 + Informatica), Tealium, Hightouch, Fivetran (Census), Treasure Data | Platform licence, credits, connectors | We read the client's warehouse. Reverse-ETL tools can carry our outputs to channels. |
| Real-time decisioning / telco CVM | Pega, SAS, Comviva MobiLytix, Flytxt, Pelatro, Evolving Systems/Lumine, Amdocs/Ericsson/Huawei CVM modules | Large custom licences, often services-heavy; some revenue-share in emerging markets | Do not compete head-on. We can be the "prove uplift first" layer. |
| Services and hyperscaler accelerators | TCS, Accenture, Wipro, Infosys, HCLTech; Fractal, LatentView, Mu Sigma, Tredence; AWS churn guidance; Google Telecom Subscriber Insights | Multi-year programmes, staffed teams, accelerator + team bundles; free reference architectures | Minfy itself sits in this camp. The product makes it different from the rest. |
| Predictive point tools | Pecan AI, VOZIQ | Self-serve tiers, prediction batches | Our closest analogues. They also stop at a file of predictions. |

#### Analyst categories and leaders

| Analyst view | Leaders (as reported) | Grade | Note |
|---|---|---|---|
| Gartner MQ, Multichannel Marketing Hubs (Sept 2025) | Adobe, Braze ([Braze](https://www.braze.com/press-releases/braze-named-a-leader-in-2025-gartner-magic-quadrant-for-multichannel-marketing-hubs-for-third-consecutive-year-)); Bloomreach a Visionary; MoEngage evaluated | [moderate] | The full Leader list was not verified |
| Gartner MQ, CDPs (2025 edition, Jan 2026) | Salesforce, Tealium, Hightouch (Leader on its first inclusion) ([Business Wire](https://www.businesswire.com/news/home/20260129112125/en/Hightouch-Named-a-Leader-in-the-2025-Gartner-Magic-Quadrant-for-Customer-Data-Platforms)) | [weak] | This is **not** the first CDP MQ. A 2024 edition exists. |
| Forrester Wave, Real-Time Interaction Management (Q4 2025) | Pega, SAS ([SAS](https://www.sas.com/en_be/news/press-releases/2025/november/sas-named-a-leader-in-real-time-interaction-management--says-ind.html)) | [moderate] | "25 of 28 top scores" is Pega's own marketing claim [vendor claim] |
| Forrester Wave, B2C CDP (Q3 2024) | ActionIQ, Adobe, Salesforce, Treasure Data ([CX Today](https://www.cxtoday.com/customer-data-platform/the-forrester-wave-for-customer-data-platforms-2024-top-takeaways/)) | [moderate] | Uniphore has since bought ActionIQ |
| B2C marketing automation | No B2C MQ exists. Gartner covers B2C under MMH. The B2B MAP MQ is not relevant to a telco. | [moderate] | |

Telco procurement teams will place us under "AI decisioning / predictive analytics". No analyst quadrant matches us exactly, so we need our own category story.

#### Consolidation: decisioning and activation are being absorbed into the large platforms

| Deal | Value | Grade |
|---|---|---|
| Braze buys OfferFit (RL decisioning), now BrazeAI Decisioning Studio, closed mid-2025 | ~$325M announced; ~$303M final per an aggregator, not checked against SEC filings ([Braze](https://www.braze.com/press-releases/braze-announces-agreement-to-acquire-offerfit)) | [moderate] |
| Salesforce completes Informatica purchase, Nov 2025 | ~$8B equity ([Pulse 2.0](https://pulse2.com/salesforce-completes-8-billion-acquisition-of-informatica-to-accelerate-agentic-ai-data-platform)) | [strong] |
| Fivetran agrees to buy Census (reverse ETL), May 2025 | Undisclosed ([Fivetran](https://www.fivetran.com/press/fivetran-signs-agreement-to-acquire-census-delivering-the-first-end-to-end-data-movement-platform-for-the-ai-era)) | [moderate] |
| Rokt buys mParticle, Jan 2025 | ~$300M, against a reported ~$800M valuation in 2021 ([MarTech](https://martech.org/cdp-consolidation-continues-as-rokt-scoops-up-mparticle/)) | [moderate] |
| Uniphore buys ActionIQ (late 2024); Fivetran–dbt Labs merger (Oct 2025); Adobe–Semrush (~$1.9B, planned) | Verifier's knowledge; no URL checked | [weak] |

Don't over-read this as "every engagement platform is buying decisioning". Only Braze–OfferFit fits that pattern. Fivetran is a data-pipeline company and Rokt is an e-commerce ads company. Two patterns do hold. Standalone CDP valuations are falling. And multi-option decisioning was valued at about $300M+.

#### The agentic wave: mostly expectation so far

- Gartner, Oct 2025: **45%** of martech leaders say vendor AI agents miss the promised business performance ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-10-29-gartner-survey-finds-45-percent-of-martech-leaders-say-existing-vendor-offered-ai-agents-fail-to-meet-their-expectations-of-promised-business-performance)) **[moderate]**. The headline figure is in the press-release URL itself. The companion figures (81% piloting agents, about half lacking data readiness) were not verified **[weak]**.
- Gartner, June 2025: **over 40%** of agentic AI projects will be cancelled by end-2027, and only about **130** of thousands of "agentic" vendors are genuine ([TechSpot](https://www.techspot.com/news/108499-gartner-warns-agentic-ai-projects-fail.html)) **[strong]**. This covers agentic AI in general, not marketing specifically. Gartner's related Jan 2025 poll (19% invested significantly) used a self-selected sample of about 3,400 webinar attendees.
- MIT NANDA, July 2025: **95%** of GenAI pilots show no P&L impact ([Virtualization Review](https://virtualizationreview.com/Articles/2025/08/19/MIT-Report-Finds-Most-AI-Business-Investments-Fail-Reveals-GenAI-Divide.aspx)) **[weak]**. The study has been heavily criticised: about 52 interviews, 153 survey responses, and success defined narrowly as P&L impact within about 6 months. Other 2025 studies found most firms reporting positive ROI.
- What is real: narrow RL/bandit decisioning (OfferFit) and content generation have the most proof. That is the researcher's synthesis, not a Gartner finding **[weak]**.

#### Budgets, utilisation and the buying committee

- **Budgets.** Marketing budgets are flat at **7.7%** of revenue. **59%** of CMOs say budget is insufficient, and **39%** plan to cut agency spend. CMOs name data/analytics and AI automation as their top productivity levers ([Gartner CMO Spend 2025](https://www.gartner.com/en/newsroom/press-releases/2025-05-12-gartner-2025-cmo-spend-survey-reveals-marketing-budgets-have-flatlined-at-seven-percent-of-overall-company-revenue)) **[strong]**. The sample was 402 CMOs in North America and Europe, not India.
- **Martech utilisation.** About **49%** in 2025 per secondary blogs ([shno.co](https://www.shno.co/marketing-statistics/martech-adoption-statistics)) **[weak]**. Gartner's own published series is 58% (2020), 42% (2022) and 33% (2023), so the "down from 56%" story is wrong. The safe message is "roughly half of the stack goes unused". Check the figure before quoting it to a client.
- **Market size depends on who measures.** The CDP Institute puts the CDP market at about $1.9B (2025, +19%; no source cited). [MarketsandMarkets](https://www.marketsandmarkets.com/PressReleases/customer-data-platform.asp) puts it at $9.7B with a 30.7% CAGR, using a broader definition **[weak]**. Use the conservative number.
- **Buying committee.** We found no survey data on the split between CMO and CIO. The inference **[weak]**: the CMO or CVM head owns the use case and budget, while the CIO/CDO and security own data access, DPDP and hosting. Salesforce paying $8B for data governance supports the view that data leaders now sit in martech decisions.

**What this means for Marketing AI.** The three things buyers say are missing are proof of incremental value, data readiness and governance. We already have each one: the RCT measurement module, Guided Setup + Data Doctor, and approver gates + audit + in-account deployment. Our weak spots are the features the leaders treat as basic: activation, cross-offer arbitration and real-time decisioning. So:

- Position as **governed, human-approved decision intelligence that feeds the MMH/CEP the client already owns**, not as an "autonomous marketing agent".
- Avoid the word "agent" in headlines. Buyers now discount it.
- Write the sales kit for two audiences: CVM/CMO (net value, profit curve) and CIO/CISO (runs in your AWS account, audit, DPDP, separation of duties).

### 2. Pricing

#### Pricing units in use

| Unit | Who uses it | Evidence | Fit for Marketing AI |
|---|---|---|---|
| MAU + channels + add-ons, quote-only | Braze | [Automation Atlas](https://automationatlas.io/answers/braze-pricing-explained-2026/) **[moderate]**, verifier confirmed the structure | Poor. A telco with tens of millions of subscribers would need deep discounts. |
| MTU (monthly tracked users) | MoEngage | [CampaignHQ](https://blog.campaignhq.co/tag/moengage-pricing) **[weak]** | Poor, same reason |
| MAU tiers with a published entry price | CleverTap | [Toolradar](https://toolradar.com/tools/clevertap/pricing) **[moderate]** | Useful as an entry-SKU pattern |
| Customer attributes / channels, tier-gated AI | Optimove (self-optimizing campaigns top tier only) | [Spendbase](https://www.spendbase.co/?p=29225) **[weak]** | Good: gate by capability |
| Prediction batches + data rows | Pecan AI, $950–2,500/month self-serve | [Pecan](https://www.pecan.ai/pricing) **[weak]**, may be out of date | Good: matches our monthly recipe replays |
| Subscribers analysed, pay-as-you-go, small PoC first | Google Telecom Subscriber Insights (2023) | [Google Cloud](https://cloud.google.com/blog/topics/telecommunications/introducing-telecom-subscriber-insights) **[strong]** for what Google said | Possible, if banded. The product page now returns 404, so it may not still be sold. |
| Customer or interaction volume, custom | Pega Customer Decision Hub | [Capterra](https://www.capterra.ie/software/1041950/pega-customer-decision-hub) **[weak]** | The listed $97–260/user/month prices are Pega Platform editions, not CDH pricing |
| Per verified outcome | Intercom Fin, $0.99/resolution | [Stripe](https://stripe.com/customers/fin-ai) **[moderate]** | Model for an outcome kicker |
| Credits (Salesforce, Adobe) | Not researched | n/a | Open question |

There is a buyer complaint about audience-based pricing: costs jump at tier thresholds, and inactive contacts can count toward the bill. The complaint mainly applies to contact/profile models. Under MTU/MAU, dormant users usually don't count. The sources are competitors' blogs **[weak]**. Even so, churn and win-back deliberately score lapsed customers, so any audience meter cuts against our core use case.

#### Typical contract values by segment

| Segment | Figure | Grade |
|---|---|---|
| Braze median | ~$88.8k/yr across 176 purchases; ~14% saved off the first quote ([Vendr](https://www.vendr.com/marketplace/braze)) | [moderate] (crowd-sourced snapshot) |
| Global mid-market engagement | ~$40–100k/yr | [weak] |
| Large enterprise engagement | $250k to $1M+/yr; Braze customers at $500k+ ARR rose from 247 to 333 in FY26 ([SEC 10-K](https://www.sec.gov/Archives/edgar/data/1676238/000167623826000011/a20260131-brazeincxfy26ear.htm)) | [moderate] |
| Braze expansion | Net retention ~109%, down from earlier years. Expansion inside accounts is slowing, not speeding up. | [moderate] |

#### India price points

| Item | Figure | Grade |
|---|---|---|
| CleverTap Essentials | ~$75/month (~INR 6,000) for 5k MAU, plus a trial | [moderate] |
| CleverTap Advanced, 50–200k MAU | INR 5–12 lakh/yr | [weak] |
| MoEngage mid-market | INR 2.5–15 lakh/yr | [weak] |
| MoEngage / CleverTap enterprise | INR 25–40 lakh+/yr | [weak] |
| First-contract discount | 15–30% (CleverTap, anecdotal); this is not India-specific evidence | [weak] |

All India figures come from comparison blogs, not transaction data. Treat them as anchors to test, not facts.

#### Outcome-based pricing: what the evidence supports

- **Works when the outcome is binary, observable automatically and hard to dispute.** Intercom Fin charges $0.99 when the customer confirms a resolution or doesn't ask for more help. Intercom says Fin is near or past about $100M ARR **[moderate]**. Some practitioners call the billing unpredictable and say "user just left" inflates the count.
- **Telco CVM has used it.** Flytxt markets "outcome-based SaaS" with claims of up to 7% net revenue uplift, partly from a 2016 release ([LXA Hub](https://www.lxahub.com/cdp-directory/flytxt)) **[vendor claim]**. The verifier adds that Flytxt, Evolving Systems/Lumata and others have disclosed revenue-share contracts with operators in Africa and Asia **[weak]**.
- **Churn analytics services almost never publish gain-share terms.** The likely reason is disputed attribution **[weak]**.
- **Every uplift claim we found is self-reported**, with no audit: Flytxt 7%, Pecan 12%, Optimove "37.4x" ([Optimove](https://www.optimove.com/resources/blog/optimove-and-empresa-da-sorte-maximum-impact-with-self-optimizing-campaigns)), Wipro 12–25%, and a 2020 release claiming 50% **[vendor claim]**.

**What this means for Marketing AI.**

- Don't meter on audience. Price per use case or recipe and by capability tier, with scoring runs included.
- Our randomised control group, 95% ranges and net value in rupees are exactly the auditable unit outcome pricing needs. Most competitors can't offer it credibly.
- Plan for buyers comparing us to a CEP bill of INR 15–40 lakh/yr. A decision layer that sits next to that bill must cost less, or show a measured return.

### 3. Go-to-market

#### Services-led vs product-led

We found no direct study of conversion between these models. Here is what the camps show:

- **Product-led signals are at the low end.** CleverTap publishes an INR entry tier and offers a trial. Pecan has self-serve tiers. Google TSI offered a small-footprint PoC **[moderate]**.
- **Telco enterprise deals are services-led.**
  - TCS–Vodafone Idea runs HOBS (BSS) + TwinX (AI personalisation) ([TCS](https://www.tcs.com/who-we-are/newsroom/press-release/vodafone-idea-tcs-collaboration-ai-powered-customer-experience-platform)) **[weak]**.
  - Accenture's "GenAI Genie" claims 21% churn reduction over **three years** on its CDP, for an unnamed operator ([TM Forum](https://inform.tmforum.org/features-and-opinion/revolutionizing-telecommunications-with-ai-driven-customer-experience-ai-ops-a-collaborative-approach/)) **[vendor claim]**.
  - Infosys Topaz churn work grew into contact-centre and architecture programmes **[vendor claim]**.
- **Execution sits with martech or specialist CVM tools.** WebEngage runs digital CVM for Airtel Africa's ~150M subscribers ([Gulf News, sponsored](https://gulfnews.com/gn-focus/airtel-africa-embarks-on-digital-customer-value-management-of-150-million-subscribers-with-webengage-1.1719233959774)) **[weak]**. Comviva, Flytxt and Pelatro compete in the same space.

#### How SIs and analytics firms package churn and CVM

| Shape | Example | Grade |
|---|---|---|
| Multi-year platform / BSS programme | TCS–Vi; Accenture GenAI Genie (3 years) | [vendor claim] |
| Accelerator + team | LatentView SolveIT CLV, "reduce churn by 4% on average" ([PDF](https://www.latentview.com/wp-content/uploads/2021/03/case-studies-machine-learning-driven-customer-lifetime-value-analysis.pdf)); Fractal claims ~10% incremental sales | [vendor claim] |
| Decision-science team that starts with a roadmap | Mu Sigma: NLP on service requests and escalations to find churn drivers ([Mu Sigma](https://www.mu-sigma.com/industries/telecom/)) | [vendor claim] |
| Accelerator launched through a hyperscaler | Tredence Customer 360 at Google Cloud Next 2025 ([CMSWire](https://www.cmswire.com/the-wire/tredence-unveils-customer-360-at-google-cloud-next-2025-the-ultimate-customer-intelligence-engine/)) | [weak] |
| Outcome-oriented "pods" | Brillio "agentic AI pods" (support, not churn) | [weak] |
| Free reference architecture | AWS Guidance for Subscriber Churn Prediction and Retention: SageMaker + QuickSight feature importance, with no uplift, measurement or activation as described ([AWS](https://aws.amazon.com/solutions/guidance/subscriber-churn-prediction-and-retention-on-aws)); AWS also blogs AutoGluon for churn | [weak] (page not opened) |

The AWS sample repos are stale conference demos, with last commits in 2023 and 2021 **[strong]**, since the verifier cloned them. Google's CLV sample gets only housekeeping updates **[strong]**.

Realistic results are much smaller than headline claims. McKinsey (c. 2017–18) says "up to ~15%" churn reduction ([McKinsey](https://www.mckinsey.com/industries/technology-media-and-telecommunications/our-insights/reducing-churn-in-telecom-through-advanced-analytics)) **[weak]**. The 20–30% figure was not confirmed. LatentView's ~4% is a more sober benchmark.

#### Cloud marketplace co-sell

- **India sellers.** AWS Marketplace reportedly began supporting India-based sellers transacting in INR through AWS India, around Nov 2025, with GST handling ([AWS](https://aws.amazon.com/about-aws/whats-new/2025/11/aws-marketplace-india-based-sellers-transactions-inr/)) **[weak]**. Plausible, but not opened. Still unconfirmed: GST/TCS/WHT mechanics, whether purchases draw down the client's AWS commitment, and the private-offer fee.
- **Co-sell uplift.** AWS-commissioned studies say Marketplace deals are 81% larger and 27% more likely to close ([AWS blog](https://aws.amazon.com/blogs/awsmarketplace/how-to-drive-sales-alignment-to-grow-in-aws-marketplace)) **[vendor claim]**. The data is selection-biased. Don't forecast on it.

#### Pilots and proofs of value

- **No data found** on pilot-to-contract conversion rates, typical pilot length or pilot price in marketing AI or telco CVM. This is an open gap.
- The best public pattern is Google TSI's: a **small-footprint PoC, then enterprise-wide rollout on usage pricing** **[strong]** for what Google stated.
- A common failure is leakage. An Indian business-school churn paper reported **99.97% accuracy**, which almost always means target leakage ([SDMIMD](https://sdmimd.ac.in/marketingconference2024/papers/IMC2467.pdf)) **[weak]**.

**What this means for Marketing AI.**

- Churn scoring on AWS is free reference material, and AutoGluon is not a differentiator. Sell the **productised, governed, day-2 version of AWS's churn guidance**: monthly recipe replay, drift monitoring and retrain, approver gates, uplift, and RCT measurement. That gives AWS co-sell a clear story.
- Telco incumbents (GSI BSS stacks, Pega, CVM vendors) won't be displaced. Our pilot should **export into the operator's existing campaign tool** and prove measured value in weeks, against GSI programmes measured in years.
- Map the operator's current stack (in-house, TCS, Amdocs, WebEngage, Netcore, MoEngage) before the pilot starts.

### 4. Pricing and GTM options for Minfy's Marketing AI

All INR figures below are **our proposals**, built from the weak third-party anchors above. They are hypotheses to test with the telco and the AWS team, not market evidence.

#### Three packaging models

| | **A. Use-case subscription by capability tier** | **B. Lower platform fee + outcome kicker** | **C. Product + managed "retention pod"** |
|---|---|---|---|
| What the client buys | An annual fee per deployed use case/recipe. Tiers: **Predict** (scores, band, reason, action, Data Doctor), **Uplift & Measure** (budgeted treat list, RCT, profit curve), **Generate** (RCA summaries, win-back copy with judge, RAG assistant). Monthly scoring runs included; extra runs metered, Pecan-style. Add an Activation add-on once export connectors exist. | Platform fee set at about 50–60% of Model A, plus a share of incremental net value measured against the randomised control. Paid only on the **lower bound of the 95% range**, with a floor and a cap. | Model A licence plus 1–2 Minfy analysts who run the monthly churn → RCA → treat list → measurement cycle, for a fixed monthly fee |
| Illustrative India price (proposal) | Single use case, file upload: low entry SKU for mid-market. Enterprise multi-use-case: INR 15–40 lakh/yr list, with ~25% negotiation headroom. | Fee below Model A; the kicker is capped so year-1 total is no more than ~1.5x Model A | Licence + pod fee; the pod is priced on a monthly cycle, not on FTE hours |
| Pros | Predictable for both sides. Doesn't penalise scoring lapsed customers. Maps to land-and-expand across lifecycle stages. Fits how Optimove and CleverTap gate capability. | Uses our strongest asset: auditable incrementality. Directly answers the 45% "agents miss promised value" complaint. Telco CVM buyers already know outcome models (Flytxt). | Fits how Indian buyers already purchase analytics (accelerator + team). Covers our activation and arbitration gaps with people for now. Uses Minfy's services strength. Captures agency budgets that CMOs are cutting. |
| Cons | Price isn't tied to value. At telco scale it may be underpriced, or challenged against a CEP bill. CSV-only output weakens the value story. | Revenue arrives late and varies. The outcome depends on the client actually running the campaign, because we have no activation. Holdout-measured lifts are small (single-digit %). Procurement and finance complexity. Attribution disputes, though the RCT reduces them. | Lower margin, and people don't scale. Risk of being seen as "just another services firm". Pod staff may paper over product gaps instead of driving fixes. |
| Best fit | Mid-size operators, BFSI and retail, and the post-pilot renewal | Mature telco CVM teams with a CFO sponsor | First 2–3 enterprise logos, including this telco |

We considered a per-scored-customer meter (Google TSI style) but don't recommend it as the main unit. At tens of millions of subscribers it either becomes unaffordable or has to be banded so heavily that it behaves like Model A.

#### Recommendation

- **Lead with Model C now**, priced so it converts into Model A.
- **Offer Model B as an optional rider** on renewal, once one RCT has shown credible lift for this client.
- **Sell through AWS Marketplace INR private offers**, registered in ACE for co-sell, if India-seller mechanics and commit drawdown are confirmed.
- **Don't headline "agentic".** Headline "measured, governed, runs in your AWS account".

#### Recommended pilot-to-contract path (telco)

| Stage | Length | What happens | Exit gate |
|---|---|---|---|
| 0. Data Readiness Assessment (paid, small) | 2–4 weeks | Guided Setup + Data Doctor on the operator's customer master and events: data-quality score, leakage report, PII map, one-row-per-customer dataset for churn. Early deliverable for the CIO/CDO. | Data signed off; target window and leak checks agreed |
| 1. Proof of value (paid, fixed fee, credited against year-1) | 8–12 weeks | Churn model with Approver gate → RCA summaries per risk segment → uplift-based **budgeted win-back treat list** → operator executes through **its own campaign tool** from our export table → randomised holdout. | Written in advance: (a) Qini/AUUC above random on held-out data; (b) statistically significant incremental retention in the treated cell vs control; (c) net INR value positive at the 95% lower bound |
| 2. Pre-agreed conversion | At signing of Stage 1 | Conversion price and scope (Model C, then Model A) written into the PoV contract so it can't become an endless free pilot | Criteria met, so the annual contract triggers via AWS private offer |
| 3. Expand | Quarters 2–4 | Add use cases by lifecycle stage (payment propensity, fault prediction, onboarding). Offer the Model B rider. Publish a named, holdout-measured case study with its method. | Second use case live; case study approved |

Pilot rules that matter:

- **Set expectations in writing.** Holdout-measured incremental saves may be 3–5% on the treated base, not the 10–30% vendors claim. Show this in the profit-curve simulator before launch.
- **Size the holdout before launch** so a real effect can be detected (background knowledge, not verified). Indian prepaid churn is reportedly 2–4%+ a month **[weak]**, so the churner population is likely big enough.
- **Close the activation gap for the pilot** with a stable export table: customer_id, use_case, band, reason, action, treat_flag, holdout_flag. Write it to S3/Redshift so the operator's MoEngage, CleverTap, WebEngage, Netcore or Pega can pick it up, or a reverse-ETL tool (Fivetran/Census, Hightouch) can sync it.
- **Show leak checks openly.** "We found and removed leaking features X, Y" separates us from "99.97% accuracy" claims.

#### Open questions to close before setting prices

1. Real Indian deal values for MoEngage, CleverTap, Netcore and CVM vendors at telco scale. How are 50M+ subscriber bases metered?
2. Do Indian operators buy CVM on revenue-share today (Flytxt, Comviva), and at what share?
3. AWS Marketplace India: who handles GST/TCS, does a purchase count toward the client's AWS commitment, and what is the private-offer fee?
4. Pilot conversion benchmarks and typical PoV fees in Indian enterprise AI.
5. Which campaign or CVM stack does the pilot operator run, and in what format must treat lists arrive?

## Where we are already strong, and the gaps

Status was checked against the repository: README, `docs/UPLIFT.md`, `docs/V1_READINESS.md`, `docs/pilot/PLAYBOOK.md`, `configs/use_cases/` and `engine/stages/actions.py`.

| Capability | What the market expects | Our status today | Evidence |
|---|---|---|---|
| Data readiness (Guided setup, Data Doctor, one-row-per-customer build, future-leak checks, PII masking, monthly recipe replay) | Help with data before modelling; about half of martech leaders lack data readiness | **Strong** (journeys b, c, g pass) | Gartner via market review [moderate]; CDP failures are mostly organisational [weak] |
| Prepaid churn label (recharge lapse, grace periods, MNP, secondary SIM) | Prepaid-native outcomes such as "recharge within N days" | **Missing.** `telco_churn.yaml` is mapped to the Kaggle postpaid file | A wrong label is "the most likely silent pilot failure" (Telecom and India) |
| Propensity scoring with band, reason and action per customer | Table stakes everywhere | **Strong**, but a commodity. Reasons may read as feature names, so business-language reasons are **partial** | Built-in GBT scores at Braze, MoEngage, CleverTap [moderate] |
| Model governance (Approver cannot approve own model, champion rule, drift PSI with retrain) | Human-approved AI, separation of duties | **Strong** (journey e passes) | >40% of agentic projects expected to be cancelled over risk controls [strong] |
| Uplift targeting (S/T/X learners, four segments, sleeping dogs never treated) | Rarely offered; CEPs and Adobe Customer AI rank by propensity | **Strong** | Ascarza 2018 [strong]; Adobe does not model uplift [moderate] |
| Uplift evaluation you can trust | Intervals, stability, beating propensity as well as random | **Partial.** Bootstrap AUUC interval, and the champion gate requires the interval above zero. No fold stability, no test against propensity, no predicted-vs-observed calibration | Uplift models are unstable across folds; Qini is noisy [moderate] |
| Budget and profit curve | Optimise money under a budget | **Strong** for one value per conversion. **Missing** per-customer ARPU, margin or CLV, and per-channel cost | Lemmens & Gupta [moderate]; WhatsApp ₹0.86 vs ₹0.13 [moderate] |
| Campaign measurement (95% Newcombe interval, p-value, maturity, causal label, rupee value view) | Money proof against a control | **Strong** (journeys d, f) | Nobody else in Indian telco reports it [moderate] |
| Control that survives the hand-off | Persistent global holdout; send-log checks | **Missing.** The control is drawn per scoring run, seeded by run id, so monthly replays re-randomise it. No send-log reconciliation | Braze and Loymax ship global holdouts [moderate]; contamination is a top failure mode [moderate] |
| Test planning (power/MDE calculator, pre-registration, CUPED) | Avoid inconclusive tests; smaller holdouts | **Missing** | Lewis & Rao [strong]; CUPED [strong method] |
| Learning loop | "Self-learning" (Pega, bandits) | **Partial.** `measure/learn` builds the next uplift run behind the Approver. No automatic loop (Phase 5 is out of v1) | Pega's adaptive models [moderate] |
| Off-policy evaluation | Rare | **Strong** (IPS, SNIPS, DR with intervals) | Repo §10 |
| Activation into CEP/CVM tools | Scores inside the campaign tool | **Missing.** `scores.csv` only; sends nothing | 24% satisfied with endpoint integration [weak]; the Obviously AI pivot [weak] |
| Consent and contactability | Purpose-, channel- and time-stamped consent; DLT category and template | **Partial.** Rows are suppressed on a consent column, an opt-out flag and recent contact. Nothing per channel or purpose, and no regulated action catalogue | TCCCPR, RBI draft, DPDP (Telecom and India §5) |
| Multi-offer choice | Next-best-offer across offer types | **Missing.** Binary treatment only (an extension path is recorded in DEC-668) | Every incumbent pitch [vendor claim] |
| Cross-use-case arbitration | One action per customer per cycle | **Missing.** "Recently contacted" cap only | Pega P×C×V×L [moderate] |
| Revenue outcomes | Revenue retained, ARPU | **Missing.** Binary outcomes only | CVM heads are judged on ARPU [vendor claim] |
| Real time and frontline | Agent-assist next-best-action, MNP-window saves | **Missing.** No API lookup, no agent card | T-Mobile, VodafoneZiggo, Oriserve-Vi [vendor claim] |
| DPDP consent, retention, erasure; append-only audit | Audit evidence, erasure into derived data | **Strong** for the basics (journey h). **Partial** for SDF/DPIA evidence packs and erasure into training snapshots | INR 250 crore penalty cap [moderate] |
| Runs in the client's AWS account | Data stays in their account | **Partial.** Infrastructure written and tested offline; nothing deployed yet | Zero-copy design praised [moderate] |
| Generative features (RCA summaries, win-back copy with LLM judge) | Time saved; copy that matches DLT templates | **Partial.** Works only with Bedrock configured, not shown in the demo, not constrained to DLT templates | DLT URL whitelisting [moderate] |
| Cost visibility | No bill shock | **Partial.** A per-run LLM cost ceiling exists. No SageMaker estimate per run | The Canvas idle-bill lesson [weak] |
| Real-client proof | A measured case | **Missing.** Everything is synthetic. The demo's 12.7-point churn cut is planted and must never be shown as an expectation | `V1_READINESS.md` caveat 1 |

In short, we are strong where buyers are most sceptical: measured incrementality, data readiness and governance. We are missing what campaign managers judge first (the hand-off) and what CVM heads expect next (value, multi-offer choice, arbitration).

---

## Roadmap: now, next, later

Effort: **S** = up to 3 engineer-weeks, **M** = 1–2 months, **L** = a quarter or more. All metrics are proposals to agree with the pilot sponsor.

### Now (0–3 months): make the telecom pilot work and be believed

| # | What | Why (evidence) | Effort | It worked if |
|---|---|---|---|---|
| N1 | **Run in a real AWS account with Bedrock.** Deploy into the pilot operator's account (or a Minfy-hosted account the operator approves). | Nothing is deployed yet. Data residency is the CIO's first test [moderate] | M | The pilot runs end to end in the client's account; the security review is passed |
| N2 | **Activation Contract v1.** A versioned table in the client's S3/Redshift on every run: customer token plus channel IDs, use case, champion version, uplift, segment, reason, action, `treat_flag`, `holdout_flag`, `consent_status`, `suppress_reason`, `valid_until`, `UPDATED_AT`, `PAYLOAD`. Changed rows only, no raw features or PII. One file layout for the pilot's actual tool, plus a generic SFTP CSV for voicebot and call-centre vendors. | Campaign managers judge on the hand-off [weak]. The composable pattern is standard [moderate] | S–M | The operator ingests the list within 3 weeks of it being ready, with no manual reformatting |
| N3 | **A control that survives the hand-off.** A persistent, hash-salted universal holdout (3–10%) that stays stable across replays and use cases. Send-log reconciliation reporting intent-to-treat and complier-adjusted lift. An alert when a holdout ID appears in a send log. | Contamination is a top failure mode [moderate]. The current control is re-drawn per run (repo) | M | Contamination under 5%; at least 70% of the treat list actually contacted |
| N4 | **Pre-launch test planner.** Power/MDE calculator with a control-size slider and "this holdout forgoes about INR X". The plan (metric, arms, analysis date) is pre-registered in the audit log. | Realistic effects need large samples [strong]. An inconclusive pilot kills the deal | S | The pilot is powered for a 1–2 point effect at a holdout size the sponsor accepts; the plan is logged before launch |
| N5 | **Prepaid label builder.** Lapse and inactivity windows, grace periods, MNP flags, secondary-SIM detection, leak checks against the label window. Adds "recharge within N days" as an outcome. | Prepaid has no contract event [moderate]. 99.97% accuracy claims in the field point to leakage [weak] | M | The operator signs off the label by week 3; the model beats the yardstick (playbook S3) |
| N6 | **Consent gate and regulated action catalogue.** A contactable-by-channel flag and suppression reason on every row. A consent snapshot ID stored with each list. Each action carries its P/S/T category, 140/1600 series and DLT template ID. A legal opinion on win-back eligibility. | TCCCPR, RBI draft, DPDP [weak–strong]. Required before any send | S–M | Legal sign-off before the campaign; zero DLT rejections; every exclusion has a reason |
| N7 | **Value weighting v1.** Per-customer ARPU or margin × horizon, minus offer cost, minus per-channel contact cost (from config, with India defaults). Optimise net INR under a spend cap or ROI floor. | Best-evidenced gap [moderate]; channel costs differ about 6.6x [moderate] | S–M | The treat list ranks on net INR; finance accepts the value inputs in writing |
| N8 | **Value Proof Pack / CFO report.** Gross saves next to incremental saves, naive credit next to measured, offer cost wasted on sure things, cost of the holdout, net INR with a 95% range, and a backfire detector by segment. | CFOs are the most sceptical executives [moderate]. "Naive vs incremental" is the most persuasive single view [weak] | S | The sponsor and finance read it without a walkthrough |
| N9 | **Three-arm pilot design** (risk top-N vs uplift top-N vs random control, equal budget), with expectations set at 1–3 points in writing. A pilot design, not a build. | Replicates Ascarza on the client's own data [strong method] | S | Agreed at kick-off; result reported whichever way it falls |
| N10 | **Cost estimate before each run** (SageMaker and LLM, in rupees) with auto-stop. | We run on the client's bill [weak] | S | No unplanned spend in the pilot |

### Next (3–9 months): deepen "decide", sell "prove" on its own, scale the pod

| # | What | Why (evidence) | Effort | It worked if |
|---|---|---|---|---|
| X1 | **Uncertainty-aware approval.** AUUC across folds with a stability badge; significance against random *and* propensity; predicted-vs-observed calibration by decile; treatment-assignment audit; a small random "explore" slice on every export. | Unstable uplift and noisy Qini [moderate]; operators often randomise only within high-risk customers [moderate] | M | No model is promoted unless it beats propensity; randomised history builds up every month |
| X2 | **Telecom root-cause feature pack.** Cell and site KPIs and outages, complaint and call-note text, recharge gaps, MNP/UPC requests, billing shocks, household and product holding. Market-event notes on drift and RCA views. | CEP models see only app events [weak]; 89% report call drops [weak] | M | RCA summaries name network and billing causes; the operator's network team uses them |
| X3 | **Minimum multi-offer mode.** Up to 3 offer arms plus a no-offer arm, as parallel per-arm uplift models, choosing the best expected net value under a budget, with a power check on how many arms the base supports. | Next-best-offer is table stakes [vendor claim]; batch multi-arm works at telco volumes [moderate] | M–L | A multi-arm win-back runs with a per-arm control; at least one target operator stops naming it as a blocker |
| X4 | **Batch P×V×L arbitration.** One action per customer per cycle across churn, payment, fault and win-back. The Approver also approves the action catalogue (offers, costs, eligibility, caps). | "Did another campaign reach them first?" [weak]; Pega's formula [moderate] | M | No customer receives conflicting actions; second use case live |
| X5 | **Audit mode.** Ingest decisions and outcomes from campaigns run by the CEP, its agents or another tool; report lift against our universal holdout. | No independent lift study exists for any RL product [moderate]; vendors mark their own homework | M | One operator pays for an audit readout on a campaign we did not target |
| X6 | **Save-desk/agent card** (top 3 reasons, offer tier, "likely sure thing, do not discount") as CSV plus a batch-scored lookup for Amazon Connect or the care CRM. | Most telco saves happen at the save desk and in the MNP window [vendor claim] | M | Agents accept a measurable share of suggestions; discounts to sure things fall |
| X7 | **Revenue outcomes and CUPED.** Continuous outcomes (revenue retained), with CUPED on pre-period ARPU and recharge. Adaptive holdout shrinking with a floor for recurring campaigns. | CUPED is standard [strong]; holdouts are resented as "money left on the table" [vendor claim] | M | Variance reduction is shown; the control share falls without losing significance |
| X8 | **Pod-scaling tools.** Insight cards (no control, not significant, drift, challenger ready, sleeping dogs, uplift decay) and an experiment registry with "proven value to date". | Optibot pattern [moderate]; gains fade without refresh [vendor claim] | S | Pod effort per client falls cycle on cycle |
| X9 | **DPDP evidence pack, erasure into training snapshots, consent-manager adapter** (consume, never become one). | Consent managers from 13 Nov 2026, the rest from 13 May 2027 [strong] | M | The operator's DPO accepts the pack for its DPIA |
| X10 | **AWS Marketplace INR private offer and ACE co-sell,** pitched as "the governed, maintained version of AWS's churn guidance". | AWS sample repos are stale [strong]; India-seller mechanics unconfirmed [weak] | S | One deal transacts through Marketplace or is sourced through ACE |

### Later (9–18 months): extend, once the first result is public

| # | What | Why (evidence) | Effort | It worked if |
|---|---|---|---|---|
| L1 | **Publish the first India telco case study** with its method (randomisation proof, ranges, net INR). Start analyst briefings. | Telco procurement anchors on analyst lists [moderate]; no rival publishes this | S | Cited in at least 3 later sales cycles |
| L2 | **Outcome rider (Model B)** at renewal: a lower fee plus a share of net value on the 95% lower bound, with a floor and cap. Settled by the Value Proof Pack. | Outcome pricing works when the outcome is auditable [moderate] | S (commercial) + reconciliation from N3 | One renewal signed on these terms without a settlement dispute |
| L3 | **Second vertical: BFSI** (payment propensity, card default, win-back), with a 1600-series and RBI-consent action catalogue. | Configs exist; regulatory urgency [moderate] | M | One BFSI pilot on the same layer with no core changes |
| L4 | **Batch multi-arm with exploration** (for example, Thompson-sampling allocation on the monthly replay), still behind the Approver. | Answers the "self-learning" objection without per-user RL [moderate] | M | Allocation shifts measurably between cycles; the control is kept |
| L5 | **Two-way platform integration** through MoEngage's MCP server or CleverTap APIs (push an approved segment, pull results back), governed by our roles and audit. | Cheap two-way path [strong for MoEngage MCP's existence] | M | Results return without manual file uploads |
| L6 | **Emerging-market operators through AWS** (the Dialog Axiata pattern). | Plausible, but not evidenced in the research | S (GTM) | Two qualified opportunities outside India |
| L7 | **Optional pre-approved promotion policy** (auto-promote when the challenger beats the champion by X and passes checks, fully audited); human approval stays the default. | Competitors retrain automatically [moderate] | S | Used by at least one client without an audit finding |

### Do not build

- **Our own channel sending** (SMS, WhatsApp, email, OBD). The host platform's job.
- **A CDP or a reverse-ETL engine.** That layer is being bought up and bundled.
- **A real-time streaming decision engine, or per-user RL.** Under-powered at win-back volumes, and expensive. A batch lookup covers most of the perceived gap.
- **A consent manager.** Consume consent managers through an adapter.
- **CVM-suite extras such as loyalty points.** This is where we would lose to Comviva and Flytxt.
- **An "agent" headline**, or fully automatic model promotion as the default.
- **Money-precise CLV.** Use ranks or bands. Practitioners do not trust CLV in rupees.
- **Per-MAU or per-profile pricing.**

---

## Risks and what the evidence does not settle

### The main risks

| Risk | Why it matters | What reduces it |
|---|---|---|
| **The hand-off breaks the proof.** We do not run the campaign. The operator's team may message control customers or skip treated ones. | Without a clean control, our main differentiator is gone and outcome fees cannot be settled | N2, N3; the pod owns the hand-off in the pilot; checkpoint 1 |
| **Effects too small to see.** Expect 1–3 points; a range crossing zero reads as failure. | One null pilot could cost the vertical, because the next client depends on the case study | N4, X7 (CUPED, revenue outcomes); written expectations; three-arm design |
| **Uplift needs randomised history the operator may not have.** Uplift arms need at least 1,000 rows and 50 positives each. | Month one may show only "propensity + randomised control", so "decide" is invisible | Say so up front; the explore slice and `measure/learn` build history; the three-arm test is itself the first randomised history |
| **Win-back may be legally blocked.** | It removes the flagship use case and the cleanest outcome to measure | N6 legal opinion before kick-off; substitute pre-churn retention or service-category recharge reminders |
| **Host platforms close the gap.** MoEngage + Aampe + Boldest has the same platform-plus-services shape as Minfy. | The operator's platform says "we already do this" | Speed to a published case; audit mode (X5) positions us as the neutral auditor; telecom depth they lack (N5, X2) |
| **"Decide" is too thin for CVM buyers.** Binary only, frequency cap only. | Lost in RFPs that lead with next-best-offer | N7 now, X3 and X4 next; checkpoint 5 pulls them forward if needed |
| **Small, slow buyer pool.** | Few logos, long cycles, cash-constrained buyers | Regional ISPs and DTH as faster logos; BFSI as the second vertical; AWS reach to emerging markets later |
| **The DIY path is "good enough".** Canvas plus free AWS guidance covers the risk half almost free. | We look like an expensive version of a free reference architecture | Win only on what DIY lacks, shown on the client's data: uplift, sleeping dogs, RCT net value, approval, DPDP |
| **The services wrapper becomes the business.** | Low margin; product gaps covered up by people | Pod-effort checkpoint; X8 tooling; convert to licence by the second renewal |
| **Gains fade.** Aampe's 11-month RCT; ML policies lose value under data shift [strong]. | Year-two value falls | Uplift-decay monitoring (X8); retrain after market shocks; keep some controls for 90–180 days |
| **Our own credibility.** All validation is synthetic, and the demo effect is planted at 12.7 points. | Showing demo numbers as expectations would undo the honesty pitch | Never show demo magnitudes as a forecast; lead with Data Doctor leak findings |

### What the evidence does not settle

- **The pilot operator's stack.** Which CEP or CVM tool, which import formats, which channels and lead times. Every research area flagged this as open. It decides N2's first layout.
- **Whether uplift beats propensity in Indian prepaid,** where churn is blurred by dual-SIM use and recharge-based definitions. Only the pilot can answer this.
- **Key magnitudes.** Ascarza's "16% receptive", "+8% vs +2%", Lemmens & Gupta's ">100%", the "30% sleeping dogs" figure and the 5,300-per-arm estimate are all unverified. Most primary pages could not be opened during the research.
- **Whether a CFO will pay separately for measurement** or expects it bundled. Not researched.
- **Legal questions.** Whether operators can lawfully contact lapsed prepaid customers, under which header. Whether DPDP "service delivery" consent covers churn modelling. Whether RBI's draft is final and DCA has gone national.
- **Prices and conversion.** Indian telco-scale deal values, revenue-share rates, pilot-to-contract conversion and typical proof-of-value fees: no data found. All INR figures here are hypotheses.
- **How much CUPED helps on prepaid outcomes.** Probably a lot for revenue, little for binary churn. Unmeasured.
- **Whether win-back gains persist** or are pull-forward.
- **Competitors not researched:** Tiger Analytics, Bloomreach, Insider, SAS CI360. Comviva, Flytxt and Pelatro were covered only through vendor pages. We may be under-estimating incumbents' installed base and switching costs.
- **Whether any Indian operator runs Pega,** so "on top of Pega" may not matter in India.
- **AWS Marketplace India mechanics:** GST and TCS handling, and whether purchases draw down the client's AWS commitment.

---

## Customer discovery plan for the next 4–8 weeks

### Experiments to run

| # | Experiment | How | Decision it informs |
|---|---|---|---|
| E1 | **Hand-off dry run** | Give the pilot operator's campaign team a synthetic treat list in the Activation Contract format. Time how long it takes to load into their tool with the holdout honoured, and log every error. Repeat with one regional ISP. | First file layout (N2); checkpoint 1 risk |
| E2 | **Power feasibility** | From the data request, compute base churn, win-back volume and the minimum detectable effect at 5%, 10% and 15% controls. Ask the sponsor which control size they will accept. | Pilot design (N4, N9); whether win-back or pre-churn is the first campaign |
| E3 | **Legal read** | A written opinion from the operator's counsel and Minfy's on TCCCPR win-back contact and DPDP modelling purpose. | N6; whether to substitute the use case |
| E4 | **Retrospective "prove" test** | Ask 3 prospects for one past retention campaign (assignment and outcomes). Run the campaign-results module on it and show what can and cannot be claimed. | Does "prove" sell on its own (checkpoint 3); demand for audit mode (X5) |
| E5 | **Packaging test** | Show 6–8 prospects three one-page offers (layer licence, licence plus pod, pod with an outcome rider) at proposal INR prices. Record which one they would take to procurement and why. | Commercial model and price anchors |
| E6 | **Message test** | Test two positioning lines ("decide-and-prove layer beside your CEP" vs "the telecom CVM whose numbers you can audit") with CVM heads and AWS India partner managers. | Final positioning wording |
| E7 | **CEP overlap check** | Ask 4–5 MoEngage/CleverTap/Netcore users whether their platform's control group satisfies finance, and whether they have seen uplift targeting there. | Checkpoint 6; size of the "prove" gap |

### Who to interview

| Persona | Number | Where |
|---|---|---|
| CVM / retention head (economic buyer) | 5–6 | Pilot operator, Vi, BSNL, 2 regional ISPs, 1 DTH/cable operator |
| CFO or finance controller | 3 | Same accounts |
| CIO / CDO / DPO / security | 3 | Pilot operator, BSNL, one ISP |
| Campaign manager / CRM operations | 4–5 | Pilot operator plus two accounts using an Indian CEP |
| Data analyst | 2–3 | Pilot operator, one ISP |
| Save-desk or care lead | 2 | Pilot operator, one ISP |
| Ecosystem | 4–5 | AWS India telco partner managers (2); a CEP solutions engineer; an ex-CVM consultant; 1–2 BFSI marketing heads for contrast |

### Interview guide (15 questions)

**CVM / retention head**
1. Walk me through your last retention or win-back campaign. Who chose the list, how was success reported, and was there a randomly held-out group?
2. What number are you judged on each quarter (churn, ARPU, revenue retained), and who checks it?
3. If one vendor showed "2 points of churn saved, 95% range, ₹X net" and another claimed "30% churn reduction", which would you take to your CFO? Why?
4. What would make you add a tool beside your current engagement or CVM platform, rather than asking that vendor for the feature?

**Campaign manager**

5. What steps does a new audience go through to go live in your tool today, in what format, and how long does it take?
6. How do you keep a holdout group out of every campaign, including other teams' campaigns? Has a control group ever been contacted by mistake?
7. Which channels do lapsed or at-risk prepaid customers actually respond to, and which headers and DLT templates do you use for each?

**CFO / finance**

8. How do you judge retention spend today: cost per save, ARPU retained, or value net of offer cost?
9. Would you pay for independent measurement separately, or do you expect it inside the platform fee?
10. Would you sign a fee paid only on the lower bound of a measured 95% range? What floor and cap would you need?

**CIO / CDO / DPO**

11. What must be true before vendor software can run in your AWS account and read subscriber data, and how long does that approval usually take?
12. Under TCCCPR and DPDP, can you contact a lapsed prepaid customer, and on what consent? Does your service-delivery consent cover churn modelling?
13. Which consent records do you reconcile (CRM opt-in, DLT/DND, DCA, DPDP withdrawals), and who owns each one?

**Data analyst**

14. How do you define prepaid churn today (inactivity days, recharge lapse, port-out), and how do you handle secondary SIMs kept alive on minimum recharges?

**Save desk / care lead**

15. When a customer threatens to leave, what offer do you give, and how would you know whether they would have stayed anyway?

### What answers would change the plan

| If we hear… | We change… |
|---|---|
| The operator's tool cannot ingest a file list within 3 weeks, or the incumbent vendor or SI blocks imports | Build a thin direct push for that one tool, or drop the layer position for that account (checkpoint 1) |
| 3 of 5 sponsors or CFOs say their CEP's control group or attribution is enough | Stop pricing "prove" separately; bundle it and lead with value-weighted targeting |
| 2 of 3 operators make multi-offer or real-time a precondition | Pull X3 (multi-offer) and X6 (agent lookup) into Now; delay X5 |
| Legal says win-back to lapsed prepaid is not permitted | Make pre-churn retention or service-category recharge reminders the pilot campaign; keep win-back for postpaid, ISP and DTH |
| The operator will not hold a random control | Do not run the campaign phase (playbook E4); offer the readiness assessment and a retrospective audit only |
| Buyers want a managed service, not a licence | Keep Model C longer, but enforce the pod-effort checkpoint and price the pod per cycle |
| Power analysis shows the base cannot detect a 1–2 point effect at an acceptable holdout | Switch the outcome to revenue retained with CUPED, extend the window, or pick a larger segment before launch |
| BFSI interviews show stronger pull than telecom | Bring L3 (BFSI pack) forward; the layer design already allows it |
| A CEP vendor already ships uplift with interval-based net value in India | Move audit mode (X5) and telecom depth (N5, X2) to the front of the pitch |
| Finance values the "gross vs incremental" view above everything else | Make the Value Proof Pack the lead deliverable in every sales meeting, before any model screen |
