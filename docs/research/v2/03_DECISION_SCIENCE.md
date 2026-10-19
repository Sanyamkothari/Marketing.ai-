# 03 · Decision Science for the 9 Decisions

Researched 2026-10-19. This covers what actually works in production for each decision, how much lift is real, and what data each method needs. Vendor numbers are marked as vendor claims.

## 1. Bottom line

- **The market has moved to "decisioning", and the incumbents bought their way in.** Braze bought OfferFit (RL decisioning) for about $325M; the deal closed 2 June 2025. OfferFit contracts reportedly started above $250K a year. MoEngage bought Aampe (a per-user agent built on Thompson sampling) on 24 June 2026. Hightouch sells "AI Decisioning", which is contextual bandits running on the customer's data warehouse. Our "inner loop" idea is therefore the category, not a novelty. Our edge has to come from *causal honesty* (holdout-measured incrementality), *small-data competence*, and price.
- **Rigorous lifts are small. Vendor lifts are huge.** Peer-reviewed or engineering-blog results: Duolingo bandit +0.5% DAU and +2% new-user retention; Nextdoor LLM subject lines +1% sessions and +0.4% WAU; Twitter RL push sent fewer notifications with equal engagement; DoorDash causal targeting matched the order lift at about half the cost. Vendor claims: Persado "+41% conversion", OfferFit "+450%", Hightouch "10-20% incremental sales". **Selling a measured, holdout-verified 3-10% is credible. Promising 40% is not.**
- **The biggest money is in "who" and "how much discount", not in "when".** Uplift targeting plus discount depth (who not to contact, who needs no coupon) cuts cost per incremental order by around 2x (DoorDash). Send-time optimisation has no independent evidence of large lift. Its own vendors gate it behind data thresholds (Klaviyo needs 12,000+ recipients; Braze falls back to a default time below 5 events per user).
- **Raw LLM copy loses. LLM copy plus a learned selector wins.** Nextdoor's raw ChatGPT subject lines got **56% of the clicks** of the baseline. Only after adding a reward model and rejection sampling did they beat it. So the Creative Director agent must feed a bandit or scorer. It must not ship its own pick.
- **Doing nothing is a first-class action.** LinkedIn cut emails by 40-50% and complaints fell by half or more. Twitter's and Facebook's RL notification systems learned to send *less*. Netflix's messaging system only sends when estimated incrementality clears a threshold.

## 2. The 9 decisions

| # | Decision | State of the art | 80%-value simple method | Data needed | Cold start | Pitfalls |
|---|---|---|---|---|---|---|
| 1 | **Who to contact** | Uplift / CATE (meta-learners, DML, causal forests) + budget knapsack (Booking.com "Free Lunch", DoorDash DML) | Propensity-to-churn/buy score × a coarse uplift adjustment from past holdout; never contact predicted "sure things" or "sleeping dogs" | Randomised treat/control history. About 1k+ rows per arm for coarse segments; tens of thousands for individual CATE | Random holdout plus treat-all for the first cycles, then segment-level ATEs | Propensity targets "sure things". Qini/AUUC formulas differ by library. Individual CATE on small data is mostly noise |
| 2 | **Best time** | Per-user engagement-time histograms, smoothed by population (Braze, Klaviyo STO, Seventh Sense) | Local-timezone "most popular hour" by segment, plus each user's last-active hour | Opens (excluding machine opens), clicks, sessions with timestamps. Braze needs at least 5 events per user | Population mode. Randomise times for new users (Seventh Sense) | Optimises opens, not revenue. Apple Mail Privacy Protection corrupts opens. Weak independent evidence |
| 3 | **Best offer** | Multi-treatment uplift with costs → net value per offer; contextual bandit over offers with ROI/budget constraints | Two or three discount tiers + "no discount" arm; pick argmax of (uplift × margin − cost) | Offer-level randomisation, margins, redemption | Uniform random across tiers inside a capped budget | Discount cannibalisation; trains customers to wait; must log the offer cost |
| 4 | **Best channel** | Channel as a treatment in uplift, with per-message cost (LinkedIn badge/push/email selection, WSDM 2022) | Cheapest consented channel that has historical response; escalate to costly channels only for high value | Per-channel sends, costs, responses | Braze Intelligent Channel rule: at least 3 messages per channel on 2+ channels before personalising | Last-touch attribution double-counts. Cost differs ~10x (WhatsApp India ₹0.86 per marketing message from Jan 2026, plus 18% GST) |
| 5 | **Best format** | Contextual bandit over creative (Netflix artwork) | Per-segment A/B, or Thompson sampling over 2-4 formats | Format tag on every send | Even split | Evidence conflicts: HubSpot found plain text beat HTML/images by 21-51% on clicks; others found the reverse. Audience-dependent; deliverability effects |
| 6 | **Message / value prop** | LLM generates → reward model filters → bandit allocates (Nextdoor; Persado-style); learning on semantic *labels*, not strings (Aampe) | Thompson sampling over 3-6 human-approved variants, tagged by theme (price, urgency, social proof) | Variant tags + outcomes | Shared priors per theme across campaigns | Variants churn too fast to learn. Raw LLM copy underperforms |
| 7 | **Frequency & sequence** | Offline/model-based RL with a long horizon and multi-objective rewards (LinkedIn offline RL 2022; Twitter 2022; FB Horizon DQN) | Per-user caps learned from uplift-by-volume tiers (LinkedIn personalised caps), plus a fatigue penalty per send | Send history, unsubscribes, complaints, sessions over weeks | Conservative global cap (e.g. 2-3 per week) | Myopic models always say "send". Existing lists are survivor-biased (RRD). Unsubscribes spike early in the relationship and after purchase |
| 8 | **Which objective** | Multi-objective with guardrails; surrogate index for long-term value (Athey et al., REStud 2025) | Single primary metric (incremental gross margin) + hard guardrails (unsubscribe rate, complaint rate, discount spend) | Margin data, a long holdout | Revenue proxy | Optimising clicks or opens; ignoring the cost of the discount |
| 9 | **Personalisation** | Relational/graph foundation models (KumoRFM, May 2025) and per-user agents with shared priors | RFM + category affinity + engagement recency as bandit context features | Transactions and events | Population priors | Vendor-only evidence for foundation models; high latency and cost |

## 3. Notes on the decisions that matter most

**Who to contact.** This is the clearest proven win. Booking.com ran online RCTs of uplift targeting under ROI constraints and stayed within budget. DoorDash's double-ML promotion targeting matched a uniform campaign's order lift at about half the cost per incremental order. The catch is that treatment effects are tiny next to outcome noise: about 1/20 of the outcome value, per one survey. The arithmetic follows from that. With 5k customers you can estimate an average effect and 3-5 coarse segment effects, nothing finer. v1's S/T/X learners are the right tools; v2 should add the budget-knapsack layer and treat "do nothing" as an arm.

**Send time.** Every major platform has it. None publishes independent lift. Klaviyo itself only shows expected lift when a recent campaign reached 5,000 or more recipients. Seventh Sense's largest reported gains came from *randomising* times for unengaged contacts, which suggests a deliverability and throttling effect more than real "optimal time" learning. Treat send time as a cheap feature to include, not a headline.

**Frequency and fatigue.** This is the most under-served decision in SMB tools, and the one with strong large-company evidence. LinkedIn's 2016 volume optimisation maximised downstream sessions while minimising sends and complaints. Its 2022 offline-RL notification system beat a supervised multi-objective baseline in A/B tests and was fully deployed. Twitter's model-based RL looked several days ahead. In a 33-day test it sent fewer notifications with the same engagement. A practical version: give every send a *cost* in the expected-net-value formula equal to the predicted lost future value (unsubscribe probability × customer value, plus fatigue decay). That makes "do nothing" win automatically.

**Message.** Duolingo's "sleeping, recovering" bandit is the best public template. Arms (templates) can be unavailable, and novelty decays and then recovers. On millions of reminders a day it gained +0.5% DAU, and Duolingo released 200M rows of replication data. Aampe's idea of learning on *labels* (tone, offer type, theme) rather than individual messages is what makes learning possible when copy is regenerated constantly. That suits LLM-generated variants.

**Netflix (MESA, 2020-2025 talks).** A causal bandit estimates each message's *incrementality* and sends only above a threshold. This is the closest public analogue to our "do-nothing vs dispatch" inner loop.

## 4. Contextual bandits in production

- **Log the propensity of every action at decision time.** Without it, off-policy evaluation and unbiased retraining are impossible. Microsoft's Decision Service frames this as explore → log → learn → deploy and reported 25-30% CTR gains.
- **Set exploration floors.** Mix in a minimum per-action probability (ε-floor, e.g. 5-10%) on top of Thompson sampling. Otherwise importance weights explode and effective sample size collapses (Microsoft customer-support bot lessons). Capping weights is a stopgap, not a fix.
- **Evaluate off-policy before going live.** Doubly robust estimators, with SWITCH or shrinkage when propensities are small. Use them as a gate before deploying a new policy, but always confirm with the live holdout.
- **The holdout stays.** A bandit optimises within the treated population. Only a persistent random holdout proves total incrementality, and it is what makes ROI reports believable.
- **Build, don't rent.** Azure Personalizer was retired on 1 Oct 2026. Open alternatives are Vowpal Wabbit, microsoft/learning-loop, or a hand-rolled Bayesian linear/logistic Thompson sampler, which is enough for our arm counts.

## 5. What works at what scale

| Scale | Works | Doesn't yet |
|---|---|---|
| **~5k customers, low volume (SMB)** | Holdout + ATE; 2-5 segment uplifts; Thompson sampling over 2-4 variants with shared priors across campaigns; rule-based caps; population send time | Individual CATE, per-user STO (Klaviyo requires 12k), RL sequencing, per-user agents |
| **50k-500k** | T/X-learner uplift; multi-offer net-value targeting; contextual bandits on ~10-30 features; learned caps by tier | Long-horizon RL |
| **5M+, daily sends** | Per-user contextual bandits, offline RL for frequency and sequence, surrogate-index objectives, embeddings | Still needs holdouts and guardrails |

A rough per-arm rule: to detect a 1-point lift on a 5% base rate you need roughly 8k customers per arm (standard power maths, 80% power). A 5k-customer business cannot learn a 10-arm problem in one campaign. It has to pool learning across campaigns and over time, and reuse priors from similar businesses or themes.

## What this means for us

**Do**
- Make the inner loop an expected-net-value policy: `E[uplift × margin] − offer cost − channel cost − fatigue cost`. Include "do nothing" as an explicit arm, log propensities, and set ε-floors.
- Keep and promote the persistent holdout. Report incremental margin with confidence intervals, which competitors' "lift" claims usually lack.
- Pool learning across campaigns: hierarchical/Bayesian priors on *labels* (offer type, theme, channel, format), not on individual messages.
- Gate methods by data size automatically (ATE → segment uplift → contextual bandit → RL), and tell the user plainly which tier they are on.
- Put LLM copy through a scorer/reward model and then a bandit, never straight to send.
- Add a learned unsubscribe/fatigue cost early. It is cheap and differentiating for SMBs.

**Don't (or not yet)**
- Don't headline send-time optimisation or promise vendor-style 40% lifts.
- Don't build full RL sequencing or per-user agents for v2 launch. The data scale isn't there for most target customers.
- Don't optimise opens or clicks as the primary objective.
- Don't rely on a rented bandit service.

**Open questions**
- How many campaigns per month does a typical target customer run? This decides whether cross-campaign pooling is enough.
- Can we legally and practically share anonymised priors across tenants (an "industry prior") to solve cold start?
- What is the right default fatigue cost per channel (WhatsApp vs email) before we have data?
- Should the surrogate-index approach be used to proxy 90-day margin from 7-day signals?
- Is a relational foundation model (Kumo-style) worth evaluating against AutoGluon features for v2.x?

## Sources

- https://www.braze.com/press-releases/braze-completes-acquisition-of-offerfit
- https://tjthinakaran.substack.com/p/braze-buys-offerfit-and-whatsapp
- https://www.casestudies.com/company/offerfit
- https://yourstory.com/2026/06/moengage-acquires-san-francisco-based-ai-infra-startup-aampe
- https://kb.aampe.com/user-guide/welcome-to-aampe/how-does-aampe-work/agents
- https://hightouch.com/blog/what-is-ai-decisioning-what-does-hightouch-do
- https://www.amplifypartners.com/barrchives/how-hightouch-builds-rl-reinforcement-learning-agents-for-marketing-teams
- https://www.braze.com/docs/user_guide/brazeai/intelligence_suite/intelligent_timing
- https://braze.com/docs/ko/user_guide/brazeai/intelligence/faqs
- https://braze.com/docs/user_guide/brazeai/intelligence/intelligent_channel
- https://help.klaviyo.com/hc/en-us/articles/360029794371
- https://help.klaviyo.com/hc/en-us/articles/360041411132
- https://www.prismglobalmarketing.com/blog/case-study-93-percent-increase-email-performance-send-time-optimization
- https://research.duolingo.com/papers/yancey.kdd20.pdf
- https://arxiv.org/pdf/2312.12457 and https://engblog.nextdoor.com/let-ai-entertain-you-increasing-user-engagement-with-generative-ai-and-rejection-sampling-50a402264f56
- https://careersatdoordash.com/blog/doordash-smarter-promotions-with-causal-machine-learning
- https://ar5iv.labs.arxiv.org/html/2008.06293 (Booking.com, Free Lunch)
- https://arxiv.org/pdf/2207.03029 (LinkedIn offline RL notifications)
- https://arxiv.org/pdf/1901.10550 (LinkedIn personalised treatment selection)
- https://unpaywall.org/10.1145%2F3488560.3510017 (LinkedIn WSDM 2022)
- https://engineering.linkedin.com/blog/2016/06/less-is-more--optimizing-email-volume-part-1
- https://blog.linkedin.com/2015/11/10/sending-less-email-is-just-the-beginning
- https://arxiv.org/pdf/2202.08812 (Twitter push RL)
- https://arxiv.org/pdf/1811.00260 (Facebook Horizon)
- https://www.datacouncil.ai/talks25/mesa-building-a-personalized-messaging-system-at-netflix
- https://qconsf.com/sf2018/system/files/presentation-slides/2018-11_-_qcon_artwork_personalization_talk.pdf
- https://www.nber.org/papers/26463 (Surrogate index)
- https://arxiv.org/pdf/1905.02219 (contextual bandit lessons)
- https://www.microsoft.com/en-us/research/?p=297719 (Decision Service)
- https://proceedings.mlr.press/v70/wang17a.html (SWITCH OPE)
- https://arxiv.org/pdf/1907.09623 (DR with shrinkage)
- https://azure.microsoft.com/updates/ai-services-personalizer-will-be-retired-on-1-october-2026
- https://arxiv.org/pdf/2007.12769 (uplift survey)
- https://docs.h2o.ai/h2o/latest-stable/h2o-docs/data-science/algo-params/auuc_type.html
- https://www.persado.com/?p=20996
- https://blog.hubspot.com/marketing/plain-text-vs-html-emails-data
- https://blog.rrdonnelley.com/uploads/thoughts/pdfs/how-to-avoid-email-fatigue-the-four-step-process-to-determine-the-cost-of-over-contacting-customers_022402023_114946.pdf
- https://authkey.io/blogs/?p=1840 (WhatsApp India pricing)
- https://kumo.ai/research/kumo_relational_foundation_model.pdf
- https://tech.instacart.com/using-contextual-bandit-models-in-large-action-spaces-at-instacart-cb7ab4d8fa4f
