# 03: The technology a fully autonomous marketing agent needs, and the state of the art (11 October 2026)

**Role:** principal ML/AI engineer.
**Builds on, does not repeat:**
- `docs/research/agentic_ai_2026_10_notes/causal_ml_experimentation.md`: CUPED/MLRATE, sequential tests, EB pooling, OPE basics, uplift benchmarks, surrogates.
- `agent_protocols_and_trust_layer.md`: MCP history, AAIF, vendor MCP servers, guardian agents, MCPTox, tau-bench, CRMArena-Pro.
- `docs/plans/PLAN_K_TECHNICAL_DESIGN.md`: registry, default-deny policy, proposals, signed ledger, claim verifier, M135-M138, MCP server.

**The question this note answers:** what has to exist before the product may act **without a person approving each action**? Why does each technique work, and where does it break? How far does the state of the art support running unattended? How does all of this map onto the code we already have?

---

## 0. Method, sources and confidence

### 0.1 What could and could not be opened

- **The web was mostly closed.**
  - `WebFetch` failed with DNS `ENOTFOUND` on every domain tried (braze.com, aampe.com, arxiv.org).
  - `curl` through the egress proxy was refused (`403 connect_rejected`, organisation policy).
  - `WebSearch` worked for about 70 queries, until the session-wide budget of 200 searches (shared with other agents) ran out.
  - So claims marked **[S]** rest on a search engine's summary of the page, not on my reading of it.
- **Public GitHub repositories could be cloned** through the session's git proxy. I cloned 29 repositories (shallow, docs only) and **read** documents from 27 of them: their specifications, docs and READMEs. These are primary sources for protocols, libraries and some vendor products, and are marked **[O]** (opened). Section 9 lists the 36 opened documents with the date of their latest commit.
- **Papers themselves (arXiv, KDD, RecSys) could not be opened.** Where a paper matters, I cite either its abstract as summarised [S], or a library's documentation that implements and cites it [O].
- **Our own code was read** (`engine/agentic`, `engine/decide`, `engine/uplift`, `engine/measurement`, `engine/scheduling`, `engine/holdout`, `engine/compliance`, `engine/generative`, `api/mcp`). It is marked **[code]** with file paths.
- **Standard theory from my training** (to June 2026) is marked **[theory]**. These are textbook results: IPS unbiasedness, the Kish effective sample size, bandit regret rates. Where a specific number or attribution comes from memory and could not be checked, it is marked **(memory, unverified)**.

### 0.2 Confidence labels

| Label | Meaning |
|---|---|
| **High** | Opened primary source, or a primary source summarised consistently by several sources |
| **Medium** | A primary source with conflicting secondary detail, or several consistent secondary sources |
| **Low** | A single secondary or vendor-marketing source, or memory |

Technical terms are explained in plain words the first time they appear.

---

## 1. Summary

1. **Unattended marketing decisioning is a product today, but only inside a box a person drew.**
   - Braze (OfferFit, closed June 2025) and MoEngage (Aampe, June 2026) sell reinforcement-learning agents that pick offer, channel, timing and frequency per customer, with no approval of each send.
   - The marketer still sets the goal, the allowed actions and the guardrails.
   - Nobody credible sells an agent that also sets strategy, writes and publishes content, picks new audiences and moves budget with no person involved. (High)
2. **The per-customer decision is a contextual-bandit problem, not an LLM problem.**
   - Every published unattended engine logs the probability with which it chose each action, and learns from those logs. This holds for Spotify, Netflix, Duolingo, LinkedIn, Wayfair, Microsoft and Meta.
   - The LLM belongs in the outer loop: turning goals into plans, drafting content, explaining results. (High)
3. **The binding constraints come from statistics and the channels, not from AI.** Five problems decide what can safely run alone:
   - delayed and censored rewards (a customer who has not bought yet looks like one who never will);
   - **support** (off-policy evaluation can only judge actions the logs actually tried);
   - interference between campaigns;
   - an optimiser gaming its own reward;
   - channel rules: WhatsApp portfolio limits, India's DLT template scrubbing, Gmail's 0.3% spam threshold.

   Section 3 works through each from first principles. (High, [theory] + [O])
4. **LLM agents compound errors.**
   - If each step is right 95% of the time, a 10-step chain is right about 60% of the time.
   - tau-bench shows the best 2024 model fell from 69% (pass^1) to 46% (pass^4) on retail tasks [O].
   - So a marketing agent must keep **few unverified LLM steps** between deterministic checks, and must have a **single writer** that commits actions. (High)
5. **The open-source bandit infrastructure has thinned.**
   - Microsoft retired Azure Personalizer on 1 October 2026 [S]. Its successor repository `microsoft/learning-loop` was last changed in November 2024 [O].
   - Meta's ReAgent is archived in favour of Pearl [O]. The Open Bandit Pipeline was last changed in 2022 [O].
   - Vowpal Wabbit's documentation is still maintained (September 2026) [O].
   - Implication: own the small decision core ourselves; borrow algorithms, not platforms.
6. **Autonomy is a design choice separate from capability.**
   - The best frameworks define a level by who decides, who monitors and who is the fallback. These are SAE J3016 for cars, and Feng, McDonald and Zhang (2025) for agents [S].
   - The level is attached to **a feature inside an operating domain**.
   - Section 5.2 proposes MDAL L0-L5 for marketing, with entry, exit and demotion rules.
7. **The key design move for us is to replace "a person approves each proposal" with "a person signs a bounded, expiring mandate".**
   - Code then checks every action against the mandate, and a circuit breaker returns control on harm.
   - This mirrors AP2's **open/closed mandates** and **Trusted Surface**, read in the AP2 v0.2 docs (28 April 2026) [O].
8. **Our existing engine already has the hardest parts an unattended system needs and vendors lack:**
   - randomised logging with known propensities (`engine/holdout/assign.py` writes `treatment_probability`);
   - doubly robust OPE (`engine/uplift/ope.py`);
   - a registered test plan;
   - a harm-only always-valid alarm (`engine/measurement/harm.py`);
   - a compliance gate;
   - a signed hash-chained ledger.

   What is missing is the **action path**:
   - mandates;
   - a dispatcher, the single writer that holds channel credentials;
   - per-action compliance and mandate checks;
   - breakers that act rather than warn;
   - pacing and fatigue state;
   - stochastic choice with logged propensities for multi-offer decisions.

   Section 7 maps each one onto modules, **extend** vs **new**.

---

## 2. What runs unattended today (state of the art)

### 2.1 Vendor and big-tech systems

| System | What it decides | How it learns | Date | Confidence and source |
|---|---|---|---|---|
| **Braze AI Decisioning Studio** (ex-OfferFit) | Per customer: offer, channel, timing, message | Described as RL built on **contextual bandits** (an algorithm that repeatedly picks an action, sees a reward and shifts towards what pays, using customer features to choose per person); one model per client company | Announced 26 Mar 2025; closed 2 Jun 2025 (about $303M adjusted, per aggregators) | High on mechanism [S] ([Braze: What is OfferFit](https://www.braze.com/resources/articles/what-is-offerfit); [Braze docs](https://www.braze.com/docs/user_guide/brazeai/decisioning_studio); [Hightouch](https://hightouch.com/blog/what-is-offerfit)). Medium on the deal [S] ([VectorShift](https://vectorshift.ai/research/deals/braze-offerfit-2025)) |
| **Aampe → MoEngage** | "One agent per user": what to send, when, where, **whether to send at all** | RL with continuous parallel experiments; claims >200 billion decisions/week | Acquisition announced 23-24 Jun 2026 | High on the deal [S] ([MoEngage](https://www.moengage.com/?p=87075); [Pulse 2.0](https://pulse2.com/moengage-acquires-aampe-to-bring-11-agentic-decisioning-to-b2c-marketing-teams/)). Low on scale (self-reported) |
| **Spotify BaRT** (Bandits for Recommendations as Treatments) | Home shelves, cards and the explanation shown | Epsilon-greedy exploration; inverse-propensity-weighted off-policy training | RecSys 2018 | Medium [S] ([seminar slides](https://idea-stat.snu.ac.kr/seminar/20181207/Explore,%20exploit,%20and%20explain_%20personalizing%20explainable%20recommendations%20with%20bandits.pdf)) |
| **Netflix artwork** | Image per member per title | Contextual bandits on uniformly-randomised logs; replay evaluation; a quality check against "clickbait"; >20M requests/s | 2017-18 | Medium [S] ([QCon 2018 slides](https://qconsf.com/sf2018/system/files/presentation-slides/2018-11_-_qcon_artwork_personalization_talk.pdf)) |
| **Duolingo reminders** | Which reminder per learner per day | "Sleeping, recovering" bandit: handles novelty decay and recovery, and ineligible arms | KDD Aug 2020; +0.5% DAU, +2% new-user retention | High [S] ([Yancey and Settles](https://research.Duolingo.com/papers/yancey.kdd20.pdf)) |
| **LinkedIn notifications** | Whether, on which channel and when to notify | Constrained optimisation in a streaming system (2018); offline multi-objective RL with guardrails on volume and disables (2022) | 2018; 2022 | Medium [S] ([KDD 2018 talk](https://videolectures.net/kdd2018_gao_activity_based_notifications); [arXiv 2207.03029](https://arxiv.org/pdf/2207.03029)) |
| **LinkedIn Lambda Learner** | Per-entity personalisation layered on a global model | **GAME** (generalised additive mixed effects): a large "fixed-effect" model for everyone, plus small per-entity "random-effect" linear models updated incrementally from mini-batches; keeps coefficient uncertainty (Hessian), so the per-entity models can be used as a bandit with **Thompson sampling** (pick each action with the probability that it is the best) | 2021 | High [O] ([lambda-learner README](https://github.com/linkedin/lambda-learner)) |
| **Wayfair WayLift** | Paid media: audience, message, placement, bid | Contextual bandits via Vowpal Wabbit; notes that always picking the top ad causes fatigue | Oct 2021 | Medium [S] ([Wayfair](https://www.aboutwayfair.com/careers/tech-blog/contextual-bandit-for-marketing-treatment-optimization)) |
| **Uber / Booking / Amazon** | Budget across levers; promotions under ROI; causal bandits for persuadables | Estimate incremental response, then optimise under constraints | 2018-2024 | Medium [S] ([Uber](https://arxiv.org/pdf/2407.19078); [Booking](https://arxiv.org/pdf/2008.06293); [Amazon](https://www.amazon.science/publications/contextual-multi-armed-bandits-for-causal-marketing)) |
| **Microsoft Decision Service → Azure Personalizer** | Generic contextual decisions | **Explore → log → learn → deploy**; the reward is joined to the decision after a uniform delay to avoid delay bias; +25-30% CTR reported | Paper 2016; **Personalizer retired 1 Oct 2026** | High [S] ([arXiv 1606.03966](https://arxiv.org/pdf/1606.03966); [Azure update](https://azure.microsoft.com/updates/ai-services-personalizer-will-be-retired-on-1-october-2026)). The successor `microsoft/learning-loop` runs Vowpal Wabbit `--cb_explore_adf --epsilon 0.2 --cb_type ips` behind Event Hubs; last commit 25 Nov 2024 [O] ([README, RL_SIM.md](https://github.com/microsoft/learning-loop)) |
| **Meta ReAgent → Pearl** | Recommendations, auction bidding, creative selection | ReAgent: batch off-policy RL; policies "released slowly over time"; counterfactual policy evaluation before deploying; behaviour cloning to start safely. **Archived**; replaced by Pearl, which lists a **safety** module used for auction bidding | ReAgent archived; Pearl last commit 18 Aug 2026 | High [O] ([ReAgent README](https://github.com/facebookresearch/ReAgent); [Pearl README](https://github.com/facebookresearch/Pearl)) |
| **GrowthBook bandits** | Variation weights per experiment, or per context leaf | Thompson sampling; **minimum 1% of traffic per arm**; contextual version fits a decision tree over context and runs Thompson sampling per leaf, pooling small segments | Docs current, Oct 2026 | High [O] ([bandits overview](https://github.com/growthbook/growthbook/blob/main/docs/bandits/overview.mdx); [contextual](https://github.com/growthbook/growthbook/blob/main/docs/bandits/contextual.mdx); [technical](https://github.com/growthbook/growthbook/blob/main/docs/statistics/contextual-bandit-technical.mdx)) |

**What every engine in the table shares (inference, High):**
1. A fixed, pre-approved action set.
2. One reward per decision type.
3. **Logged propensities**: the probability with which each action was chosen.
4. Learning in batches or mini-batches, rather than trusting each new data point.
5. An exploration floor that never reaches zero.

These five properties are what let a system run without a person. Section 3 explains why each one is needed.

### 2.2 The vendor proof gap (inference)

- Vendors publish uplift against "business as usual", chosen and measured by the vendor. Braze cites one client at +92% conversion. (Low, vendor-reported [S]; [Braze Q&A](https://www-origin.braze.com/resources/articles/offerfit-ai-decisioning-webinar-questions))
- None of the material found describes a **client-verifiable** holdout, a pre-registered analysis or a signed record of who was held out.
- Our engine does all three ([code]: `engine/holdout/assign.py`, `engine/measurement/plan.py`, `engine/agentic/ledger.py`, `engine/agentic/receipts.py`).
- This is the piece an unattended system most needs: an independent, continuous answer to "is it still working, and is it hurting anyone?"

---

## 3. First principles: why each technique works, when it fails, and what that means for running unattended

### 3.1 Bandits: regret, exploration and non-stationarity

**Why it works [theory].**
- **Regret** is the reward lost by not always choosing the best action:

  `R_T = Σ_t [ μ*(x_t) − μ_{a_t}(x_t) ]`

  over T decisions.
- An algorithm that never explores can lock onto a bad action forever, and its regret grows linearly.
- Good algorithms, such as UCB (upper confidence bound: try the action whose optimistic estimate is highest) and Thompson sampling, explore in proportion to remaining uncertainty. With stationary rewards their regret grows only like √T (up to log and K factors).
- GrowthBook's implementation computes each arm's probability of being best, `w_k = P(μ_k > μ_j for all j ≠ k)`, from normal posteriors by Gauss-Hermite quadrature, and uses `w_k` as the traffic weight [O].

**The assumptions behind the guarantee, and how marketing breaks each:**

| Assumption | Marketing reality | What breaks | Best-known remedy |
|---|---|---|---|
| Rewards arrive immediately | Conversions take days to weeks; churn outcomes 30-90 days | The bandit learns from early converters only (section 3.2) | Batch updates per cycle; cohort weighting; time-to-event outcomes |
| Reward distributions are stationary | Seasonality (Diwali, sale days), competitor offers, product changes, fatigue | Yesterday's best arm keeps winning on stale evidence; a starved arm is never re-tested | **Exploration floors**: GrowthBook keeps ≥1% per arm "in case your user behavior changes over time" [O]. **Discounting**: VW `cb_dro` decays its statistics geometrically, default time constant ~1,000 examples [O]. **Champion/challenger with confidence sequences**: VW's Epsilon Decay runs models at different exploration rates and swaps one in only when its lower confidence bound beats the champion's upper bound [O] |
| Many decision rounds | A customer may get 2-8 marketing decisions a month | Little to learn per customer, so the gain comes from **context** (sharing across similar customers), not from per-person trial and error | Contextual models with pooling: GAME fixed + random effects [O]; tree leaves that pool small segments [O] |
| Context is not affected by past actions | Today's message changes tomorrow's engagement, fatigue and consent | It is really a sequential (RL) problem; one-step bandits ignore the future cost of a send | Fatigue state as a feature and a constraint; long-horizon holdouts; offline RL with care (LinkedIn 2022 [S]) |
| One reward | Conversions, revenue, margin, unsubscribes, complaints | The optimiser trades unmeasured harms for measured gains (section 3.6) | Reward = net incremental value; guardrails as constraints, not as terms in the reward |

**Bias of adaptively collected data [O + theory].**
- Arms that look lucky early get more traffic. Their estimates regress towards the truth.
- Arms that look unlucky get starved. Their low estimates are never corrected, so the bandit's own arm means are biased.
- GrowthBook says so explicitly (citing arXiv 1905.11397) and recommends a standard experiment "when you need unbiased estimates". It also notes that with **two arms a bandit can be worse** than an even split [O].
- **Implication:** never report a bandit's arm means as effects. Measure effects only on rows assigned with fixed, known probabilities: our holdout and explore slice.

**Unattended implication.** A per-customer bandit can run alone only when all of the following hold:
1. the action set is fixed and approved;
2. it keeps a hard exploration floor and a fixed-probability holdout;
3. it updates in batches whose cadence matches the outcome window;
4. a separate process decides, from the fixed-probability data, whether the policy is still better than the baseline.

### 3.2 Delayed and censored rewards

**The failure, stated exactly (GrowthBook docs) [O]:**
- "Suppose users take up to a week to convert, and two variations perform the same." Noise makes one look better, so it gets more new users. A week later, more of its users are recent and have not converted yet, so pooled naively **it now looks worse**.
- GrowthBook's fix is to group users into **cohorts by the period in which they were first exposed**, take each arm's mean within each cohort, and average with the same cohort weights for every arm.
- Even then: "an **urgency message** might make users buy sooner without making more of them buy. That variation looks better early on, so the bandit sends it more traffic even though its final conversion rate is no higher." Their advice is to set the update cadence to how long users naturally take to convert.

**Why [theory].**
- At any reading time, the outcome of a recent contact is **censored**: we know it has not happened *yet*.
- Treating "not yet" as "no" biases against arms with recent exposure, and in favour of arms that **shift timing** rather than raise the final rate.
- Survival analysis handles this by modelling time-to-event. Gigli and Stella (UAI 2024) use bootstrapped Thompson sampling with Kaplan-Meier delay estimates for exactly this case [S] ([PMLR v244](https://proceedings.mlr.press/v244/gigli24a.html)).
- Microsoft's Decision Service joins the reward after a **uniform delay** for every decision, so no arm is judged on a shorter window [S].

**Unattended implication (High).**
- Our registered test plan already fixes an outcome window and an analysis date per campaign (`engine/measurement/plan.py`, [code]).
- A per-customer policy that updates more often than that window must use either (a) **fixed-window labels** (only decisions whose window has closed count), or (b) a time-to-event model.
- The reward store should keep labels apart from features, because labels are written late and corrected. Feast's 2026 **Label View** does this: it separates "what was judged" (rewards, overwritten and debated) from append-only observations, with conflict policies across labellers [O] ([Feast label view](https://github.com/feast-dev/feast/blob/master/docs/getting-started/concepts/label-view.md)).

### 3.3 Off-policy evaluation (OPE): why it works, variance, and the support problem

**Why it works [theory].**
- Let μ be the logging policy that chose action a with probability `p = μ(a|x)`, and π a new policy.
- The value of π is `V(π) = E_x Σ_a π(a|x) r(x,a)`. Multiplying and dividing by μ gives

  `V(π) = E_{(x,a)~μ}[ (π(a|x)/μ(a|x)) · r ]`

  so the **inverse propensity score** (IPS) estimate `(1/n) Σ_i w_i r_i` with `w_i = π(a_i|x_i)/p_i` is unbiased.
- This needs two conditions:
  1. **Support** (also called positivity): μ(a|x) > 0 wherever π(a|x) > 0.
  2. **Correct propensities**: the logged p is the probability actually used.

  Vowpal Wabbit's logged-bandit format is literally `action:cost:probability | features` for this reason [O] ([VW wiki](https://github.com/VowpalWabbit/vowpal_wabbit/wiki/Logged-Contextual-Bandit-Example)).

**Why it fails.**
1. **Variance.**
   - The variance grows with `E[w²]`. A useful rule is Kish's **effective sample size**, `ESS ≈ (Σw)² / Σw²` [theory].
   - Example: a policy that treats a segment the logging policy only touched through a 5% explore slice gives those rows `w = 20`. Its evaluation is roughly as precise as an A/B test on 5% of that segment, not on all of it.
   - Remedies:
     - self-normalised IPS (SNIPS: divide by Σw instead of n; a little bias, much less variance);
     - **clipping** (cap w; bias traded for variance);
     - **doubly robust** (DR) estimation: `V̂_DR = (1/n) Σ [ q̂(x_i, π) + w_i (r_i − q̂(x_i, a_i)) ]`, unbiased if *either* the propensities *or* the outcome model q̂ is right, and lower-variance when q̂ is good;
     - Switch and DR-with-shrinkage, which cap the importance weights adaptively.

     OBP implements all of these and lists the papers [O] ([OBP README](https://github.com/st-tech/zr-obp)).
2. **Support.**
   - No amount of data lets you evaluate an action the logger never takes in that context.
   - Exploration schemes that **eliminate** actions damage future evaluation. VW warns that SquareCB's `--elim` option "eliminates actions more aggressively", so "the resulting logged data may be less useful for counterfactual evaluation/offline policy optimization" [O] ([VW SquareCB](https://github.com/VowpalWabbit/vowpal_wabbit/wiki/Contextual-Bandit-Exploration-with-SquareCB)).
3. **Evaluating learning algorithms, not fixed policies, is data-hungry.**
   - VW's `explore_eval` replays logs by rejection sampling with threshold `p_eval / p_log`.
   - Policies far from the logger get fewer accepted examples, so their confidence intervals differ. The docs say "explore eval is very data hungry" [O] ([VW Explore-Eval](https://github.com/VowpalWabbit/vowpal_wabbit/wiki/Explore-Eval)).
4. **Wrong propensities.**
   - A deterministic rule (treat the top N) has p ∈ {0, 1}, so it carries no information about the other action.
   - Logging "the model's score" instead of the actual selection probability silently biases everything.

**What our code does [code].**
- `engine/holdout/assign.py` writes `treatment_probability`, the **logging propensity**.
- It notes that "only rows with `0 < treatment_probability < 1` carry information about both actions".
- `engine/uplift/ope.py` implements IPS, SNIPS and DR with intervals, for a **binary** treat/not decision.
- The explore slice is drawn from "an eligible, non-selected row that is **not a predicted sleeping dog**". Sleeping dogs are customers predicted to react badly to contact.
  - So the logs **never** support evaluating a policy that contacts predicted sleeping dogs.
  - That is a deliberate safety choice. But it means the sleeping-dog rule can never be re-tested from logs. Re-testing it needs a deliberate, approved small experiment.

**Unattended implication (High).**
- An autonomous policy may only take actions **inside the support of the logs it will later be judged on**.
- So the mandate's action set must equal the exploration set, and every multi-action decision (offer × channel × time) must be **stochastic with logged propensities**.
- Today `engine/decide/offer_choice.py` picks offers deterministically. That is fine for L2 (a person approves the list); for L3 it needs a stochastic variant.

### 3.4 Safe policy improvement: deploying only what you can certify

**Why it works [theory].**
- Compute a **one-sided lower confidence bound** on `V(π_new) − V(π_baseline)` from data that played **no part in choosing** π_new.
- Deploy only if the bound is > 0. The chance of deploying a worse policy is then at most δ.
- This is high-confidence policy improvement (Thomas et al. 2015; causal note) and Plan K's M137 bound with a 60/40 selection/certification split ([code]: `engine/agentic/planner.py` "prefer M137's `safe`, never `not_safe`").
- VW's Epsilon Decay uses the same logic online: a challenger replaces the champion only when its lower bound exceeds the champion's upper bound, with **confidence sequences**. A confidence sequence is an interval valid at every look, not just one [O].
- VW's `cb_dro` instead optimises a **distributionally robust lower bound** of the reward ("can be viewed as optimizing a lower bound") [O].

**Why it fails.**
- Selection on the certification data. Many candidates certified on the same data (fix: Bonferroni or alpha-spending).
- Non-stationarity between certification and deployment: the bound is about last cycle's world.
- Too little data: the bound says "cannot tell", which is honest but blocks autonomy.

**Unattended implication.**
- **Certify, then act within the certified envelope; re-certify every cycle.**
- A policy version that has not been certified for the current envelope may run only at the explore share.

### 3.5 Uplift models: why they are unstable

**Why they are hard [theory].**
- Uplift is a **difference** of two noisy probabilities, and each customer shows only one of them. Its noise is roughly the sum of both arms' noise.
- Example: with a 5% base rate and 2,000 customers per arm in a segment, the standard error of the difference is about √(2 × 0.05 × 0.95 / 2000) ≈ 0.69 points.
- Real heterogeneity between segments is often 1-2 points. So decile-level uplift rankings are mostly noise unless n is large.

**Evidence.**
- The causal note's benchmarks: deep uplift models were not robust across datasets; rankings were unstable.
- The Criteo benchmark (NeurIPS 2021) supplied a large randomised dataset precisely because ground truth is missing [O] ([criteo README](https://github.com/criteo-research/large-scale-ITE-UM-benchmark)).

**Further failure modes.**
1. **Non-random training data**: "uplift" becomes the difference between who was contacted and who was not.
2. **Feedback loops**: once the model chooses who is treated, the next training set's treatment is no longer random, except in the explore and holdout rows.
3. **Distribution shift** between the training campaign and the deployment population.

**What our code already does [code].**
- `engine/uplift/champion.py` refuses to promote a model when treatment was not random, and requires the AUUC interval to lie above zero.
- `engine/measurement/compare.py` measures, cross-fitted on randomised rows, whether uplift ranking beats risk ranking **at the same budget**.
- `engine/measurement/learn.py` trains the next model on the cycle's holdout and explore rows, using `treatment_probability`.
- **Unattended implication:** these gates are what make a self-retraining loop safe. They must stay mandatory at L3+.

### 3.6 Interference and cannibalisation between campaigns

**Why it matters [theory].**
- Causal estimates assume **SUTVA**: one customer's outcome depends only on their own treatment. Marketing violates this in three ways:
  1. **Same customer, several campaigns.** Campaign A's holdout members can still be treated by campaign B. Each campaign then measures its effect *on top of* the others. When campaigns substitute for each other (both push the same purchase), **the sum of per-campaign effects overstates the total**.
  2. **Channel substitution.** An email may move a purchase that would have happened through the app anyway.
  3. **Shared resources.** A budget, a coupon pool, stock, or a WhatsApp portfolio messaging limit (all senders share it: section 6.5). One campaign's sends reduce another's capacity.

**Best-known answers.**
- A **universal holdout** that no campaign may contact measures the total. Our programme readout: `engine/measurement/programme.py` [code].
- **Arbitration**: one decision per customer per cycle across campaigns. `engine/decide/arbitrate.py` already protects every use case's holdout and explore rows and applies contact and channel caps [code].
- **Factorial randomisation** of channels within the explore slice identifies substitution.

**Unattended implication.** The decision engine must arbitrate **across all autonomous envelopes at once**, and "proven value" must be read from the universal holdout. A sum of per-campaign effects is not proven value.

### 3.7 Metric gaming: optimisers exploit the reward they are given

**Why [theory].**
- An optimiser finds the cheapest route to a higher reward. If that route is not what the business wants, the optimiser takes it anyway. This is Goodhart's law in RL form.

**Marketing routes to gaming the reward:**

| Reward | How the optimiser games it | Seen in |
|---|---|---|
| Clicks or plays | Clickbait | Netflix checked engagement quality beside play probability [S] |
| Conversions within N days | **Pulls purchases forward** (urgency) without raising the total | GrowthBook's urgency example [O] |
| Conversions | Spends margin on discounts for people who would have bought anyway | The reason uplift exists; our offer choice uses net value [code] |
| Engagement | Over-sends; fatigue and unsubscribes land outside the window | LinkedIn keeps volume and disable guardrails [S] |
| Revenue | Targets high spenders who were never at risk | Propensity vs uplift; `engine/decide/value.py` labels gross value "not incremental" [code] |

**Best-known answers.**
1. **Incremental** reward, measured against randomised control.
2. **Net** value: margin minus offer cost minus message cost.
3. **Guardrails as constraints with non-inferiority margins** (Spotify's decision rule, Mar 2024 [S]: ship only if better on a success metric and **not worse beyond a margin** on every guardrail).
4. A **long-running holdout** to catch pull-forward and fatigue.
5. Surrogate indices for long-term value, with their assumptions audited (causal note).

### 3.8 LLM agents: compounding errors and why the single writer matters

**Why errors compound [theory].**
- If each step is right with probability p and errors are independent, a k-step task succeeds with p^k:
  - 0.95^10 ≈ 0.60;
  - 0.99^10 ≈ 0.90.
- Errors are also **correlated**: a misread state early on poisons every later step.

**Evidence [O].**
- The original tau-bench leaderboard. Pass^k is the chance that all k independent tries succeed:

  | Domain | Model | pass^1 | pass^4 |
  |---|---|---|---|
  | Retail | Claude 3.5 Sonnet (Oct 2024) | 0.692 | 0.462 |
  | Retail | GPT-4o | 0.604 | 0.383 |
  | Airline | Claude 3.5 Sonnet (Oct 2024) | 0.460 | 0.225 |

  ([tau-bench README](https://github.com/sierra-research/tau-bench)).
- Its successor, tau³-bench v1.0.1 (July 2026), fixed 75+ flawed tasks and states that results before 1.0.1 "are not comparable". **Benchmarks themselves are noisy instruments** [O] ([tau2-bench README](https://github.com/sierra-research/tau2-bench)).
- MAST names 14 multi-agent failure modes [O] ([MAST definitions](https://github.com/multi-agent-systems-failure-taxonomy/MAST)). Those most relevant to marketing:
  - 1.1 disobey task specification;
  - 1.3 step repetition (a resend);
  - 1.5 unaware of termination conditions (a runaway loop);
  - 2.2 fail to ask for clarification;
  - 2.6 action-reasoning mismatch (the plan says one thing, the tool call does another);
  - 3.2 weak verification.

**Engineering lessons from current SDK docs [O].**
- **Parallel guardrails can be too late.** In the OpenAI Agents SDK, input guardrails run in parallel by default. "If the guardrail's tripwire is triggered, the agent may have already consumed tokens and **executed tools** before being cancelled." Blocking mode exists for side effects ([guardrails.md](https://github.com/openai/openai-agents-python/blob/main/docs/guardrails.md)).
  - Rule: checks that protect a side effect must run **before** it, in the same code path.
- **Fail closed.** Callable approval rules "fail closed" when arguments are malformed, and require manual approval ([human_in_the_loop.md](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md)).
- **Durable pause and resume.** LangGraph's selling points are durable execution and human-in-the-loop interrupts ([LangGraph README](https://github.com/langchain-ai/langgraph)). Plan K's planner already persists each state transition [code].

**Unattended implication (High).**
- Keep the number of **unverified LLM steps between deterministic checks** at one or two.
- Constrain LLM outputs to enumerated options or claim templates. Plan K's planner and claim verifier already do this [code: `engine/agentic/planner.py`, `engine/agentic/claims.py`].
- Allow **exactly one component to commit actions** (section 7, the dispatcher). Cognition's April 2026 revision of its "don't build multi-agents" position concluded that multi-agent patterns work "where multiple agents contribute intelligence while **writes stay single-threaded**" [S] ([Cognition 2026](https://cognition.com/blog/multi-agents-working)).

### 3.9 Pacing as a control problem

**Why [theory].**
- Spending a budget evenly is a feedback-control problem. A **PI controller** (proportional-integral: adjust the next period's allowance by the current error and the accumulated error) is the usual tool.
- The two classic failures:
  - **Integral windup**: the accumulated error grows while a hard cap binds, then overshoots when the cap lifts. Fix: clamp the integral.
  - **Pacing on a delayed signal**: pacing on conversions that arrive a week late oscillates. Fix: pace on spend and sends, which are known at once, and leave outcomes to the decision policy.
- Industry pacing splits into **throttling** (probability of taking part at all) and **bid/offer modification** [S]. Snap reported a 13% pacing-error reduction from hysteresis plus proportional feedback [S] ([arXiv 2509.25429](https://www.arxiv.org/pdf/2509.25429)).

**Unattended implication.** For CRM, pace per mandate on **spend and sends per day** (offer cost + message cost), with an anti-windup clamp and a hard ceiling from the mandate.

---

## 4. The hardest open problems, and the best-known answers (October 2026)

| Problem | Why it is hard | Best-known answer | What remains open | Our position |
|---|---|---|---|---|
| **Long-term value vs short-term response** | The reward that is fast to observe (clicks, 7-day conversions) is not the one that matters (12-month margin, retention); optimising the fast one games it (3.7) | Long-running universal holdout; surrogate indices validated on past experiments (Netflix: 14-day surrogates matched 63-day decisions ~95% of the time, causal note); offline multi-objective RL with guardrails (LinkedIn [S]); LTV as reward with discounting | Surrogates fail when treatments have direct long-term effects (discount-driven pull-forward); no validated marketing surrogate in public | Universal holdout and programme readout exist; surrogate work not built; keep "Estimated" labels |
| **Fatigue** | Each send changes the state of the next; harm is delayed and spread (unsubscribes, complaints, ignored channels) | Recovering bandits that model novelty decay (Duolingo [S]); volume as a constraint (LinkedIn [S]); per-customer pressure state | **No public causal fatigue model in production** was found; dose-response curves by channel are not published | Contact and channel caps in arbitration; **new**: pressure state + randomised frequency arms to *measure* fatigue causally |
| **Credit assignment across channels** | A customer sees email, push, WhatsApp and ads; multi-touch attribution apportions credit by rules, not by cause | Randomise channel (or channel combination) within an explore slice; slate/multi-slot bandits for joint choices (VW CCB and Slates [O]); geo experiments and MMM for paid media | Joint optimisation of channel sequences with delayed rewards is still research | Per-channel contactability exists; **new**: factorial channel arms in the explore slice |
| **Cold start** (new client, new offer, new segment) | No logs, so no OPE and no certification | Warm-starting a bandit from supervised data (VW `--warm_cb` [O]); hierarchical/empirical-Bayes priors from comparable campaigns; GAME: a global fixed-effect model plus per-entity random effects updated online (Lambda Learner [O]); explore-first with a short uniform phase | Borrowing across **clients** raises privacy and contract questions | EB pooling across comparable campaigns exists (`engine/measurement/pooling.py` [code]); new offers start at L2 by rule |
| **Safe exploration under regulation** | Exploration means deliberately sending some customers a "worse" action; marketing law constrains what may be sent to whom | Explore only inside consented, approved, DLT/WhatsApp-template-registered actions; never explore a channel without consent; exploration floors small and capped by mandate; holdouts never applied to transactional or legally required messages | Ethics of withholding beneficial offers; price personalisation scrutiny; no regulator guidance on algorithmic exploration in marketing was found | Compliance gate per customer × channel × purpose exists [code: `engine/compliance/gate.py`]; holdout excludes suppressed rows |
| **Non-stationarity** (festivals, competitor moves, product changes) | Certification describes last cycle's world | Exploration floors; discounting; champion/challenger with confidence sequences (VW [O]); regime detection with reuse of past seasonal estimates (Amazon seasonal bandit [S]) | Choosing the forgetting rate automatically | Drift checks exist (`engine/uplift/drift.py` [code]); **new**: drift as a breaker input |
| **Evaluating the agent itself in an open world** | Benchmarks are small, noisy and get corrected (tau³'s 75+ task fixes [O]); production states are unbounded | pass^k on fixed scenarios; shadow mode; replay of past cycles; outcome grading over transcript grading [S] | No marketing-agent benchmark with causal outcomes exists | Plan K's scenario harness with pass^k=1 at k=10 exists [code: `engine/agentic/evals.py`]; **new**: shadow and replay runners |
| **Accountability without a person in each loop** | Law and customers assume a responsible person; agents cannot be liable | Signed mandates (AP2 open/closed mandates rendered on a **Trusted Surface**, "a secure, non-agentic interface" [O]); tamper-evident logs; stop-to-safe-state duty (EU AI Act Art. 14(4)(e) [S]) | No standard for attesting individual agent actions (no in-toto/SLSA for actions found) | Signed hash-chained ledger and client receipts exist [code]; **new**: mandates and action-batch commitments |

---

## 5. Safety engineering for autonomy

### 5.1 Published autonomy scales

| Framework | Levels | What defines a level | Source |
|---|---|---|---|
| **SAE J3016** (driving) | 0-5 | Who performs the task, who monitors, who is the fallback; the level belongs to a **feature** inside its **operational design domain** (ODD: the conditions it is designed for); the standard does not certify safety | High [S] ([summary](https://www.masstransitmag.com/technology/document/12327182/levels-of-driving-automation-as-defined-by-sae-international-standard-j3016)) |
| **Feng, McDonald and Zhang**, arXiv 2506.12469 (Jun/Jul 2025) | L1 Operator, L2 Collaborator, L3 Consultant, L4 **Approver** (the agent involves the user only in risky or pre-specified cases), L5 **Observer** | The user's role; autonomy is a design choice separate from capability; proposes "autonomy certificates" | High [S] ([arXiv](https://arxiv.org/abs/2506.12469v1)) |
| **Morris et al.** (Google DeepMind, ICML 2024) | No AI, Tool, Consultant, Collaborator, Expert, Agent | Human-AI interaction paradigm, tied to deployment risk | Medium [S] ([arXiv 2311.02462](https://arxiv.org/abs/2311.02462v3)) |
| **Mitchell et al.** (Hugging Face, Feb 2025) | Processor → router → tool caller → multi-step agent → fully autonomous (writes and runs its own code beyond constraints) | How much control flow the model decides; argues risk rises with autonomy | High [S] ([arXiv 2502.02649](https://arxiv.org/abs/2502.02649v3)) |
| **Braze** marketing AI maturity | 5 levels; L3 AI-assisted with human approval; **L4 semi-autonomous execution** named as the near-term target | Who approves execution | Low-Medium (vendor) [S] ([Braze](https://www.braze.com/resources/articles/autonomous-marketing)) |

No regulator or standards body has published a marketing-specific autonomy scale, as far as these searches found (Medium, absence of results).

### 5.2 Proposed scale: Marketing Decision Autonomy Levels (MDAL)

**Principles.**
- A level attaches to **a decision type inside an operating envelope**, not to the product. This follows SAE's "feature in an ODD".
- A level is defined by who decides, who monitors, who is the fallback, and **what proof is required**.
- Promotion requires evidence over time. **Demotion is automatic** on a breaker trip, and only a person can undo it.
- **The operating envelope** (our ODD) has these dimensions:
  - use case and objective;
  - action set (offers, channels, registered templates);
  - audience rule and maximum size;
  - contact and pressure caps;
  - spend per day and per period;
  - offer-value ceiling and margin floor;
  - region and regulatory regime;
  - data-freshness limits;
  - certified policy version;
  - expiry date.

**L0: Manual.**
- Who decides each action: a person.
- Monitoring and fallback: a person.
- Required to enter: nothing.
- Status: before the product.

**L1: Recommend.**
- Who decides each action: a person. The system scores, ranks, sizes tests and explains.
- Monitoring and fallback: a person.
- Required to enter: deterministic scoring with evidence labels; holdouts; audit log.
- Status: **built** (Plan J).

**L2: Propose and approve.**
- Who decides each action: the system drafts a complete action; **a person approves each one**.
- Monitoring: a person, warned by alerts. Fallback: a person.
- Required to enter:
  - default-deny policy;
  - compliance gate;
  - signed ledger;
  - claim verifier;
  - pass^k evaluation.
- Status: **Plan K** (largely built).

**L3: Act inside a mandate, supervised.**
- Who decides each action: **the system acts alone inside one signed mandate** for one decision type. Anything outside the envelope goes the L2 route.
- Monitoring: the system monitors continuously, and a person reads a daily digest.
- Fallback: **the system**. It auto-pauses and reverts to the last certified policy, and a person must resume it.
- Required to enter:
  1. a signed mandate, checked per action by code;
  2. a dispatcher as the single writer;
  3. stochastic decisions with logged propensities inside the mandate's action set;
  4. an always-on holdout and explore slice;
  5. M137 certification for this envelope;
  6. at least 2 cycles in **shadow mode** with zero mandate violations;
  7. guardrail metrics with non-inferiority margins;
  8. breakers wired to the harm alarm;
  9. a kill switch drilled monthly.
- Status: **not built** (section 7).

**L4: Self-directed inside a portfolio mandate.**
- Who decides each action: the system also creates and retires campaigns, moves budget between them and rotates approved content, all inside a portfolio mandate. It escalates out-of-envelope or high-risk items.
- Monitoring: the system, plus an **independent verifier** (a separate service with its own key) that recomputes outcomes from raw files. A person reviews weekly.
- Fallback: the system per campaign; a portfolio breaker returns everything to L2.
- Required to enter:
  1. at least one quarter of clean L3 operation, with harm alarms handled correctly;
  2. a portfolio lower bound > 0 against the universal holdout;
  3. a claims library and content gate;
  4. an M148-style verification bundle passing every cycle;
  5. incident drills;
  6. a written oversight procedure.
- Status: research-grade; possible for narrow programmes.

**L5: Unbounded.**
- Who decides each action: the system sets goals, budgets, audiences and content with no envelope.
- Monitoring and fallback: none.
- Required to enter: nothing would be enough.
- Status: **not recommended**. This agrees with Mitchell et al. Consent, DLT and claim-substantiation rules assume an accountable person.

**Promotion and demotion.**
- **Promotion** moves one level at a time, per envelope. It is a signed Admin act that quotes evidence ids (shadow results, certification bound, violation count). Optionally it requires a second Admin above a budget threshold.
- **Automatic demotion** to L2 for the envelope happens on any of:
  - a harm alarm on the primary metric or any guardrail;
  - a spend or volume breaker;
  - a data-freshness breaker;
  - the compliance-block rate above its threshold;
  - send-log reconciliation mismatch;
  - drift above threshold;
  - mandate expiry;
  - an uncertified policy-version change.
- **Widening** an envelope is a new mandate, never an edit.

### 5.3 Guardrails and policy-as-code

**Engines.**
- **Cedar** is AWS's policy language. Its README stresses three properties [O] ([Cedar](https://github.com/cedar-policy/cedar)):
  - expressive for RBAC and ABAC (role- and attribute-based access);
  - fast, with bounded latency;
  - **analysable with automated reasoning**: tools can prove properties of a policy set.
- Cedar backs Bedrock AgentCore Policy at the agent-tool gateway (GA March 2026 per the protocols note). AgentCore's SDK presents the runtime as framework-agnostic [O] ([AgentCore SDK](https://github.com/aws/bedrock-agentcore-sdk-python)).
- **OPA/Rego** (Open Policy Agent and its Datalog-like language) is the common sidecar alternative [S].

**For us.**
- Plan K's Python rules are pure functions over a typed context, which is right [code: `engine/agentic/policy.py`: `holdout_immutable`, `budget_cap`, `consent_channel`, `frequency_cap`, `plan_registered`, `role_separation`].
- Autonomy adds **execution-time** evaluation: every action against the mandate, at the dispatcher.
- It also adds a **"forbid wins"** rule: a mandate can only narrow global policy, never widen it.
- A mandate's envelope is flat (sets and numeric bounds), so it can be compiled to Cedar later if a client asks for gateway enforcement or formal analysis.

**Conversation guardrails.**
- NeMo Guardrails (programmable "rails" around an LLM; latest release 0.24.1) targets dialogue safety, topic control and injection [O] ([NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails)).
- It is useful for any customer-facing chat, but it is **not** a substitute for action-level policy.

### 5.4 Kill switches, circuit breakers, anomaly detection

**Three tiers** (practitioner consensus, mostly vendor blogs [S]):
1. A rate limit keeps each action small.
2. A **circuit breaker** stops the agent automatically when a threshold trips.
3. A **kill switch** is the human override.

Severe trips need a manual reset, and **resuming is an authorised, logged act**. Error-rate breakers need minimum samples and expiry windows. A **single enforcement point** holds the credentials ([Waxell](https://waxell.ai/blog/kill-switch-ai-agent-problem); [Waxell docs](https://waxell.ai/docs/observe/governance/kill-switch)). (Low-Medium)

**Regulation.**
- EU AI Act Art. 14(4)(e) requires that overseers of high-risk systems can "interrupt the system through a 'stop' button or a similar procedure that allows the system to come to a **halt in a safe state**" [S] ([AI Act Service Desk](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-14)). (High)
- Marketing personalisation is probably not high-risk, but the requirement is the right engineering bar.

**Knight Capital, 1 August 2012.**
- About 4 million executions in roughly 45 minutes; about $440M pre-tax loss [S] ([SEC filing](https://www.sec.gov/Archives/edgar/data/0001060749/000119312512341182/d392788d424b3.htm)). (High on the loss)
- It is the canonical case for breakers sitting **outside** the logic they protect.

**Safe state for a marketing envelope (design):**
1. The dispatcher refuses new items for the envelope at once.
2. Already-queued items are sent only if they carry a valid mandate and a passed compliance decision. Otherwise they are cancelled where the channel allows it.
3. The holdout is untouched and measurement continues.
4. The policy reverts to the last certified version, or to business-as-usual rules, where a customer expects a message (a renewal reminder).
5. The trip, the in-flight state and the evidence go to the ledger, and a person must resume.

**Anomaly inputs to the breaker:**

| Area | Inputs |
|---|---|
| Spend and volume | Spend rate vs pace; sends vs plan per channel |
| Audience | Recipients vs audience size (a join that multiplies rows); share of new recipients vs last cycle |
| Customer harm | Complaint, unsubscribe and bounce rates vs margins |
| Offers | Offer-value distribution vs ceiling |
| Pipeline health | Compliance block rate; input freshness and row counts; send-log reconciliation |
| Models | Drift PSI; claim-verifier failure rate |

Use robust baselines (median and MAD) with minimum samples.

### 5.5 Human escalation

- Feng et al.'s **Approver** level is the model: involve the person only in risky or pre-specified cases [S].
- CRMArena-Pro found nearly half of multi-turn failures came from **not asking** for needed information (protocols note). MAST's mode 2.2 is the same failure [O].
- **Design.** An escalation inbox holds three kinds of item:
  1. out-of-envelope proposals (the L2 route);
  2. breaker resumes;
  3. ambiguous inputs (a missing consent file, a rejected template).

  Each item shows the evidence, the safe default ("do nothing new") and a deadline after which the safe default applies.
- Track approve-without-edit rates per item class. They are the evidence for promoting that class. Rising rejection rates are evidence for demotion.

**Using MCP 2026-07-28 for escalation [O].**
- The new **Multi Round-Trip Request** pattern returns `resultType: "input_required"` instead of server-initiated requests.
- Long work moved into the official `io.modelcontextprotocol/tasks` extension (poll with `tasks/get`, send input with `tasks/update`).
- SSE resumability was removed, so "a broken response stream loses the in-flight request; clients MUST re-issue it". **Every propose call must be idempotent.** We can de-duplicate by the proposal's `payload_digest`.
- ([MCP 2026-07-28 changelog](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx))

### 5.6 Audit logs and signed provenance

**Agent identity.**
- A2A v1.0.0 was released **12 March 2026**, then v1.0.1 on 26 May 2026 [O].
- The spec says Agent Cards "MAY be digitally signed using JSON Web Signature (JWS, RFC 7515)".
- Before signing, the card "MUST be canonicalized using the JSON Canonicalization Scheme (JCS, RFC 8785)", excluding the `signatures` field ([A2A CHANGELOG and specification §8.4](https://github.com/a2aproject/A2A)).
- **Implication:** adopt RFC 8785 exactly in `engine/agentic/canonical.py`. Ledger hashes, mandates and any Agent Card then share one interoperable canonical form, verifiable with standard libraries.

**Mandates.**
- AP2 v0.2 (28 April 2026) defines **open mandates**, which capture "the user's constraints and goals ... before a specific cart is finalized for autonomous execution".
- It defines **closed mandates**, which are bound to one finalised action.
- It requires a **Trusted Surface**, "a secure, non-agentic interface that renders Mandate Content to the User for authorization".
- In the human-not-present flow, "the Agent must repeat back to the User what they think they are expected to purchase", and the user confirms with in-session authentication ([AP2 docs](https://github.com/google-agentic-commerce/AP2)) [O].
- **Our mapping:**

  | AP2 concept | Our equivalent |
  |---|---|
  | Open mandate | Mandate |
  | Closed mandate | Each dispatch batch, bound to the mandate id |
  | Trusted Surface | The approval screen, rendered by code from the canonical mandate, never by the LLM |

**Models and artefacts.**
- Sigstore's model-transparency signs models with Sigstore, public keys, certificates or PKCS#11 devices. It produces *verifiable* integrity and provenance claims, with `sign_to_bytes()` for serverless pipelines [O] ([model-transparency](https://github.com/sigstore/model-transparency)).
- Use it to sign each **policy version** (model + configuration hash) that a mandate references.

**Observability.**
- The OpenTelemetry GenAI conventions moved to a separate repository. Agent spans (`create_agent`, `invoke_agent`, `invoke_workflow`, `plan`, `execute_tool`) are still **"Development"** status [O] ([semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)).
- MCP 2026-07-28 standardises propagating W3C trace context in `_meta` [O].
- **Implication:** emit OTel spans for operations, but keep the **signed ledger as the system of record**. The tracing standard is not stable.

**Gap.** No standard was found for attesting individual agent actions with in-toto or SLSA [S]. Our ledger plus a Merkle commitment per dispatch batch is ahead of the market.

### 5.7 Failure modes seen in production

| Incident (date) | What went wrong | Class | Control in section 7 | Confidence |
|---|---|---|---|---|
| **Replit agent deletes a production database** (Jul 2025) | During an explicit "code and action freeze", the agent ran destructive commands after misreading empty query results; data on 1,200+ executives lost | Misread state; instruction enforced in the prompt, not in code | Freeze enforced at the dispatcher; empty-input breaker | High [S] ([Fortune](https://fortune.com/2025/07/23/ai-coding-tool-replit-wiped-database-called-it-a-catastrophic-failure)) |
| **Moffatt v Air Canada** (14 Feb 2024) | A chatbot invented a refund policy; the airline was held liable for everything on its site | Unsupported claim | Claims library and template slots | High [S] ([Law360 Canada](https://www.law360.ca/ca/articles/1804075)) |
| **Anthropic Project Vend** (2025) | A shop-running agent was talked into discounts, sold below cost, and named a non-existent payment account; a later phase added a finance sub-agent | Persuadable on price; no hard limits | Offer ceiling and margin floor in the mandate, enforced in code | Medium [S] ([SmartCompany](https://www.smartcompany.com.au/artificial-intelligence/anthropic-ai-small-retail-business-trial/)) |
| **Who Gives A Crap** (Jul 2026) | Service agent confirmed a wrong price rise; company shut the agent down | Repeating a wrong fact | Claims checked against the price source | Low [S] ([CX Foundation](https://cxfoundation.com/news/ai-agent-taken-offline-for-lying-to-a-customer)) |
| **Instinct assistant** (Aug 2026) | Sent an email without checking first | Missing approval on an action class | Per-action-class policy in code | Low [S] ([AIID](https://incidentdatabase.ai/entities/instinct/)) |
| **Knight Capital** (Aug 2012) | Runaway order loop | No breaker outside the logic | Spend and volume breakers | High [S] |
| **Nextdoor unranked LLM copy** (2023) | 56% of the clicks of human subject lines; a reward model reached +1% sessions | Untested generation | Every variant a randomised arm (already M140 [code]) | High [S] ([Nextdoor](https://engblog.nextdoor.com/let-ai-entertain-you-increasing-user-engagement-with-generative-ai-and-rejection-sampling-50a402264f56)) |
| **Bandit urgency and timing gaming** | Arms that shift timing look better early | Delayed-reward bias (3.2) | Fixed-window labels; cohort weighting | High [O] (GrowthBook) |
| **Multi-agent breakdowns** | 14 MAST modes, e.g. repetition, missing stop conditions, weak verification | Design, not model | Single writer; idempotency; verification by code | High [O] |
| **Guardrail in parallel with a side-effecting tool** | Tool executed before the tripwire | Ordering | Checks in the dispatcher's path, before the send | High [O] (OpenAI SDK docs) |

No verified 2025-26 case of an autonomous agent mis-sending a **marketing campaign** was found; the closest cases are vendor illustrations [S].

---

## 6. Real-time and event-driven architecture

### 6.1 When real time matters

- **Batch daily** decisions are enough for most retention, win-back, cross-sell and renewal work. The outcome windows are weeks.
- **Real time pays** in four places:
  - triggered moments (cart abandonment, recharge or subscription lapse, failed payment);
  - **suppression**: a complaint or opt-out must stop tomorrow's batch;
  - **fatigue state**;
  - **pacing**.
- **Consistency matters more than latency.** The decision must see consent changes before it sends.

### 6.2 Event backbone

- **Apache Kafka.**
  - 4.0 (2025) is KRaft-only (ZooKeeper removed).
  - **Queues for Kafka** (KIP-932 share groups: consumers share records cooperatively, as in a work queue) was a preview in 4.1 and is "**production-ready in Apache Kafka 4.2**".
  - Share groups gained a **dead-letter queue** (KIP-1191) for records released beyond their maximum delivery count.
  - Source [O]: [Kafka upgrade docs](https://github.com/apache/kafka/blob/trunk/docs/getting-started/upgrade.md). This corrects the search summary, which said only "early access".
  - **Implication:** queue semantics for a pool of dispatcher workers are now native to Kafka. On AWS, SQS gives the same pattern with less operations work.
- **IBM acquired Confluent**: signed 8 Dec 2025 (about $11B), closed 17 Mar 2026. Kafka stays open source [S] ([Info-Tech](https://www.softwarereviews.com/vendor-technology-notes/ibm-s-smartest-ai-infrastructure-move-since-red-hat)). (High)
- **Amazon Kinesis Data Streams** latency [S] ([Conduktor](https://www.conduktor.io/glossary/kafka-vs-kinesis)). (Medium)
  - Shared-throughput consumers: about 200 ms with one consumer, rising to about 1 s with five per shard.
  - Enhanced fan-out: about 70 ms.
- **For us:** Kinesis on-demand or EventBridge for client event intake, and SQS (or Kafka share groups where a client runs Kafka) for the dispatcher queue.

### 6.3 CDP and reverse ETL

- Warehouse-native CDPs keep profiles in the client's warehouse. Reported latencies conflict [S]. (Low)
- Fivetran agreed to acquire Census on 1 May 2025 [S] ([TechTarget](https://www.techtarget.com/searchdatamanagement/news/366623554/Fivetran-adds-reverse-ETL-with-acquisition-of-Census)). (High)
- **For us:**
  - Accept batch files or a warehouse (today) **and** events (CEP webhooks, a stream) for triggers and suppressions.
  - Reverse ETL fits L1-L2 hand-off.
  - L3 needs direct activation APIs, because the policy needs delivery receipts and its own logged propensities.

### 6.4 Feature store and low-latency scoring

- **Feast** [O] ([point-in-time joins](https://github.com/feast-dev/feast/blob/master/docs/getting-started/concepts/point-in-time-joins.md)):
  - models features as time series and joins them "in a point-in-time correct way", reproducing "the state of features at a specific point in the past";
  - adds **stream feature views** and **Label Views** for rewards.
- **Databricks acquired Tecton** (announced Aug 2025), framed as real-time data for agents [S]. (Medium)
- **For us:**
  - Point-in-time logic already exists in the onboarding feature engine (`combine_rows` with a snapshot guard; `docs/AGENTS.md` §6.1).
  - L3 needs a small online **decision-state store** (DynamoDB) per customer, holding:
    - last contacts by channel;
    - pressure score;
    - consent flags with timestamps;
    - holdout and explore membership, recomputable from the salted hash;
    - a few fresh features.
  - Scoring stays in-process: a tree or linear policy evaluates in well under a millisecond. No LLM is involved.

### 6.5 Activation APIs: constraints an autonomous sender must respect

| Channel | Constraint | Date | Confidence and source |
|---|---|---|---|
| **Amazon SES** | Sandbox 200 recipients/24 h and 1/s; quotas count **recipients**; AWS may pause sending for high bounce or complaint rates (0.1% complaints is a common guideline; the 0.5% suspension figure is unverified) | Current | High on quotas [S] ([SES quotas](https://docs.aws.amazon.com/ses/latest/DeveloperGuide/manage-sending-quotas-request-increase.html)) |
| **Gmail / Yahoo bulk senders** | >5,000/day to Gmail: SPF, DKIM, DMARC; one-click unsubscribe honoured within 2 days; spam rate < **0.3%** | Feb 2024 rollout | Medium [S] ([Bounteous](https://www.bounteous.com/insights/2024/01/31/2024-gmail-and-yahoo-deliverability-changes/)) |
| **WhatsApp Business Platform** | Per-message pricing for delivered templates since 1 Jul 2025; INR billing in India from 1 Jan 2026; **messaging limits moved to the business portfolio on 7 Oct 2025** (unique users per rolling 24 h; new portfolios start at 250); tier details and India rates conflict across sources; templates must be approved | 2025-26 | Medium [S] ([8x8](https://cpaas.8x8.com/ph/blog/important-update-whatsapp-messaging-limits-changing-october-7-2025/); [Webex Connect](https://help.webexconnect.io/changelog/whatsapp-supports-business-portfolio-pacing-relaxing-messaging-limit-tiers-and-text-truncation-on-marketing-messages)) |
| **SMS in India (TRAI TCCCPR + DLT)** | Registered header and **content template**; non-whitelisted URLs, APKs and callback numbers blocked since 1 Oct 2024; ≤ 3 variables without justification and ≥ 30% fixed text; **18 Nov 2025**: variables need descriptive pre-tags, operators scrub them against whitelists, failures logged 60 days then **rejected** | 2024-26 | Medium [S] ([TeamLease RegTech](https://teamleaseregtech.com/updates/article/49534/trai-issues-direction-to-curb-misuse-of-sms-headers-content-templates/); [Vikaspedia/PIB](https://en.vikaspedia.in/news/trai-issues-directions-to-access-providers-to-stop-misuse-of-message-templates-under-tcccpr-2018)) |
| **Push (FCM)** | Default 600k messages/min per project; 429 over quota; client errors count against quota | Current | High [S] ([FCM quotas](https://firebase.google.com/docs/cloud-messaging/throttling-and-quotas)) |
| **Braze API** | `/users/track` 3,000 requests per 3 s; `/messages/send` + `/campaigns/trigger/send` + `/canvas/trigger/send` share 250,000/h by `external_id`; no guaranteed processing time on `/users/track` | Current | High [S] ([Braze API limits](https://www.braze.com/docs/api/api_limits)) |
| **CleverTap API** | **Concurrency** limits rather than per-minute limits: Upload Events ≤ 15 concurrent per account, ≤ 1,000 records per call | Current | High [S] ([CleverTap](https://developer.clevertap.com/docs/api-request-limit)) |
| **MoEngage, APNs** | Not retrieved (search budget exhausted) | — | Gap |

**Delivery engineering for the single writer [theory; High in pattern].**
- **Transactional outbox**: decision and send record are committed in one transaction. A dispatcher reads the outbox.
- **Idempotency key** per (customer, envelope, arm, cycle), passed to channels that accept a client message id.
- **Token buckets** per channel, sized from the table above and from the mandate, with 429 backoff and jitter.
- **Receipts and webhooks** (bounces, complaints, delivery states) flow back as events that feed breakers and measurement.
- **Daily reconciliation** of outbox vs channel send logs. A mismatch trips a breaker. This is also M148's check that "no held-out key appears in the send log".

---

## 7. Reference architecture: "autonomous within guardrails", mapped onto our modules

### 7.1 Where the product stands [code]

- The engine is a **batch decide-and-prove loop**. Schedules of kind `score → treat_list → measure → learn` live in `engine/scheduling/schedules.py` `ScheduleKind` and `engine/measurement/cycle.py`.
- It hands treat lists to people or CEPs.
- **Nothing in `engine/` sends a message.** There is no channel adapter (only exports and hand-off files).
- Plan K added the agent-safe core (`engine/agentic/*`), which has only `read` and `propose` tools (registry invariant I1).
- The harm alarm (`engine/measurement/harm.py`) raises a `harm_alarm` **warning** through `engine/scheduling/firing.py` `_alert_harm`. It does not stop anything.
- The holdout service logs propensities (`engine/holdout/assign.py`).
- OPE is binary treat/not-treat (`engine/uplift/ope.py`).
- Offer choice is deterministic (`engine/decide/offer_choice.py`).

### 7.2 The one design change: approval moves from each action to a signed mandate

New invariants, for L3 envelopes only (Plan K's I1-I5 stay as they are for everything else):

| # | Invariant | Enforced by |
|---|---|---|
| I3a | Every executed action cites a valid, unexpired, signed mandate and lies inside its envelope | The dispatcher re-checks per action; a test walks every execution path |
| I3b | Holdout and explore membership come only from the holdout service | Already I4; the dispatcher refuses any held-out key |
| I3c | A breaker trip puts the envelope into the safe state within one dispatcher cycle; only a person resumes | Breaker state machine; tested drill |
| I3d | Every action records its propensity, policy-version hash and mandate id | Outbox schema; OPE refuses rows without them |
| I3e | The dispatcher is the only holder of channel credentials | Secrets scoped to the dispatcher role; a test greps for adapter imports outside it |
| I3f | The mandate shown to the signer is rendered by code from its canonical JSON (AP2's Trusted Surface) | The approval UI never shows LLM text as the mandate |

### 7.3 Two loops

```
OUTER LOOP (days; LLM-assisted; L2 by default)
  goal / warning card ─► planner (engine/agentic/planner.py) ─► draft_mandate | campaign proposal
  content: LLM drafts ─► guardrails + claims library ─► registered templates ─► variant arms (M140)
  policy (engine/agentic/policy.py) ─► approvals (engine/approvals.py: decide_mandate) ─► ledger (signed)

INNER LOOP (per cycle or per event; no LLM; L3 inside a mandate)
  events/files ─► decision-state store ─► holdout & explore (engine/holdout) ─► policy runtime:
     uplift/value per action (engine/uplift, engine/decide/value) ─► stochastic choice + propensity
  ─► arbitration + pacing + pressure caps (engine/decide/arbitrate.py + new pacing/pressure)
  ─► mandate check + compliance decision (engine/compliance/gate.py) ─► OUTBOX
  ─► DISPATCHER (single writer; idempotent; token buckets) ─► SES / WhatsApp BSP / DLT SMS / FCM / CEP APIs
  ─► receipts ─► outcomes (fixed-window labels) ─► measure / certify (M135-M137) ─► learn (M106)
  ─► monitors: harm alarm (M138), guardrails, anomalies, drift, reconciliation ─► BREAKER ─► safe state, demote to L2
  everything ─► signed hash-chained ledger ─► client receipts; verifier bundle (M148)
```

### 7.4 Module map: what extends and what is new

**Extend existing modules.**

| Module | Today [code] | Change for autonomy |
|---|---|---|
| `engine/agentic/canonical.py` | Home-made canonical JSON for hashes | Adopt **RFC 8785 (JCS)** exactly, as A2A requires for signed cards [O]; keep a golden test of old hashes (versioned canonicaliser so old ledger entries verify) |
| `engine/agentic/ledger.py` | Append-only hash chain; signed approvals, plans, evidence labels | New entry kinds: `mandate.signed`, `mandate.expired`, `policy_version.registered`, `action.batch` (Merkle root of a dispatch batch, no rows), `breaker.tripped`, `breaker.resumed`, `autonomy.promoted`, `autonomy.demoted`; add mandates and resumes to `SIGNED_KINDS` |
| `engine/agentic/policy.py` | Default-deny rules at proposal time | (a) mandate rules: a mandate narrows global policy (forbid wins), needs a certified policy version, a registered test plan with holdout, and an expiry; (b) the same rule set called **per action at dispatch** |
| `engine/agentic/planner.py` | State machine to one campaign proposal | A `draft_mandate` path: FRAME → SIZE → CANDIDATES → certify (M137) → envelope sized by `plan_test` and capped by policy → INTERRUPT (signature) |
| `engine/agentic/tools.py`, `registry.py` | `read`/`propose` tools only | New read tools: `get_mandate`, `get_breaker_state`, `get_autonomy_level`, `get_shadow_report`; new propose tool `draft_mandate`. **Still no write kind**: the dispatcher is not a tool |
| `engine/agentic/proposals.py`, `engine/approvals.py` | Proposal lifecycle; `decide_proposal` | `decide_mandate` with optional two-person rule above a budget threshold; resume-after-trip as an approval kind |
| `engine/agentic/evals.py` | pass^k scenarios, policy mutation test | Shadow-mode and replay scenarios; failure injection (empty input, stale consent, duplicated rows, 429 storms, template rejection, clock skew); breaker drill scenario |
| `engine/holdout/assign.py` | Persistent holdout, explore slice, `treatment_probability` | Multi-action propensities (offer × channel); **randomised frequency arms** and **factorial channel arms** inside the explore slice (fatigue and substitution measured causally); keep the sleeping-dog exclusion, and record it as a declared support gap |
| `engine/decide/offer_choice.py` | Deterministic best net value per customer | A stochastic mode: softmax or Thompson over the mandate's allowed offers, with an exploration floor; emits the propensity of the chosen action |
| `engine/decide/arbitrate.py` | One decision per customer across use cases; holdout and explore protected; contact and channel caps | Add a **spend** dimension and pressure caps; arbitrate across all live envelopes; mandate ceilings as hard limits |
| `engine/uplift/ope.py` | IPS/SNIPS/DR for binary treat/not | Multi-action DR; effective-sample-size and support diagnostics per candidate; refuse when ESS is below the plan's minimum |
| `engine/uplift/champion.py`, `engine/measurement/compare.py` | Causal and measurable gates; uplift vs risk at the same budget | Certification per envelope becomes a precondition for any policy version above the explore share |
| `engine/measurement/harm.py` + `engine/scheduling/firing.py` | Harm alarm raises a warning | At L3+: alarm → `breaker.trip(envelope)`; add non-inferiority guardrail metrics and pre-registered subgroup checks (CLASH lesson [S]) |
| `engine/measurement/plan.py`, `cycle.py`, `learn.py` | Registered plans; the monthly loop; learning from holdout and explore | Fixed-window labels for policy updates; per-envelope plans; a `dispatch` step between `treat_list` and `measure` for L3 envelopes |
| `engine/scheduling/schedules.py`, `alerts.py` | Kinds: score, drift_check, retrain, treat_list, measure, learn | New kinds: `dispatch`, `breaker_check`, `reconcile`; new alert kinds: `breaker_tripped`, `mandate_expiring`, `reconciliation_mismatch` |
| `engine/compliance/gate.py`, `handoff.py` | Per customer × channel × purpose decision; signed record on a treat list | Called **per dispatch batch** too; inputs add WhatsApp template status and quality, sender complaint/bounce rates, quiet hours |
| `engine/generative/guardrails.py`, `win_back.py`, `engine/decide/variant_arms.py` | Deterministic rules then LLM judges; templates with placeholders; variants as measured arms | A **claims library** (approved facts with substantiation and validity dates) bound to template slots; a variant tripping a guardrail is withdrawn automatically at L3 |
| `api/mcp/server.py`, `gate.py`, `limits.py` | Read and propose only; no third-party text; rate limits | Expose the new read tools; `draft_mandate` returns `input_required` under MCP 2026-07-28 MRTR; idempotent propose by `payload_digest` |

**New modules.**

| New module | Does | Rests on |
|---|---|---|
| `engine/autonomy/mandate.py` | `Mandate` model (envelope of section 5.2), canonical hash, signing via ledger, `check(action, mandate) -> Decision` as a pure function, expiry | AP2 open/closed mandates [O]; Cedar-style forbid-wins [O] |
| `engine/autonomy/levels.py` | Level per envelope; promotion evidence; automatic demotion triggers | SAE J3016 ODD; Feng et al. [S] |
| `engine/autonomy/breaker.py` | Closed / open / half-open-by-person state machine per envelope and portfolio; inputs from section 5.4; safe-state procedure | Breaker practice [S]; EU AI Act Art. 14(4)(e) [S] |
| `engine/autonomy/shadow.py` | Runs a policy version inside a draft mandate without sending; logs decisions with propensities; OPE report vs the live policy | ReAgent's "test a new policy without deploying it" via counterfactual evaluation [O] |
| `engine/decide/pacing.py` | PI pacing on spend and sends per envelope, anti-windup clamp, carry-over state | Section 3.9 |
| `engine/decide/pressure.py` | Per-customer contact-pressure state (decayed counts by channel), used as feature and cap | Duolingo recovering bandit [S]; LinkedIn volume constraints [S] |
| `engine/decide/policy_runtime.py` | A versioned, signed policy object (model hash + config hash) that returns action and propensity; batch-updated per cycle | Decision Service loop [S]; VW `(x, a, c, p)` logging [O]; model-transparency signing [O] |
| `engine/events/` (`contracts.py`, `intake.py`) | Typed, idempotent event intake (purchases, opt-outs, complaints, receipts), dead-letter handling, suppression events processed first | Section 6.1-6.2 |
| `engine/state/store.py` | Online decision-state store (DynamoDB in production, a file or SQLite fake in tests) with freshness stamps; nightly rebuild from the warehouse | Feast online and offline consistency [O] |
| `engine/activation/outbox.py`, `dispatcher.py` | Transactional outbox; the **single writer**: re-checks mandate and compliance, token buckets, idempotency keys, cancels on breaker open | Section 6.5; Cognition single-writer lesson [S]; OpenAI SDK ordering lesson [O] |
| `engine/activation/adapters/` (`ses.py`, `whatsapp_bsp.py`, `sms_dlt.py`, `fcm.py`, `braze.py`, `moengage.py`, `clevertap.py`) | One adapter per channel or CEP, behind a protocol with a fake | Section 6.5 limits |
| `engine/activation/reconcile.py` | Outbox vs channel send logs; held-out keys must never appear | M148 design [code: Plan K §10] |
| `engine/measurement/labels.py` | Fixed-window and time-to-event outcome labels per decision; censoring-aware | GrowthBook cohort weighting [O]; Feast Label View [O] |
| `engine/measurement/guardrails.py` | Non-inferiority tests on guardrail metrics; subgroup harm checks | Spotify decision rule [S]; CLASH [S] |

### 7.5 Migration path

1. **Shadow L3 (no sends).**
   - One use case (win-back offer choice) and one client.
   - `policy_runtime` decides inside a draft mandate every cycle.
   - `shadow.py` compares it with the approved L2 list by OPE on the explore data.
   - Exit criterion: zero mandate violations over two cycles.
2. **L3 pilot.**
   - A signed mandate with a small budget and audience, on one channel: email through SES or the client's CEP.
   - The harm alarm is wired to the breaker; weekly review.
3. **Widen by new mandates** (a second channel, more budget), each after certification on two consecutive cycles. This is M146's gate in Plan K §10.
4. **L4** only for a narrow programme, with the independent verifier running and at least a quarter of clean L3.

### 7.6 Build versus buy

- **Where a client already runs Braze Decisioning Studio or MoEngage/Aampe:** act as the **governor and verifier** around their engine. That means holdout, mandate, compliance gate, breaker and proof, through their APIs.
- **Elsewhere:** build the small per-customer loop ourselves. The algorithms are few and well documented: Thompson sampling, softmax or SquareCB exploration, DR evaluation [O]. The open-source platforms that used to package them are archived or dormant:
  - ReAgent: archived;
  - `learning-loop`: last commit Nov 2024;
  - OBP: last commit 2022;
  - Azure Personalizer: retired Oct 2026.
- **Optional tooling:** Vowpal Wabbit (documentation maintained Sept 2026) for large action spaces or slates. EconML's `DRPolicyTree`/`DRPolicyForest` for interpretable policies [O] ([EconML reference](https://github.com/py-why/EconML)).
- **What clients cannot buy elsewhere is the proof and the guardrails, not the bandit.**

---

## 8. Gaps and open questions

**Not verified (pages could not be opened):**
- vendor mechanics (Braze, Aampe);
- the papers behind section 2.1's big-tech rows;
- channel rules for WhatsApp, TRAI, Gmail and SES (all [S]).

**Not retrieved (search budget exhausted):**
- Pinterest's notification-volume system (KDD 2018) and Meta's notification-reduction results (memory, unverified);
- MoEngage and APNs limits;
- Gmail/Yahoo changes after 2024;
- OfferFit's control-group methodology;
- the SEC's Knight Capital order.

**No public evidence found for:**
- a causal fatigue model in production;
- a regulator's marketing autonomy scale;
- an autonomous marketing mis-send incident;
- accuracy benchmarks for LLM claim checkers;
- action-level attestation standards.

**Design questions for us:**
- Who may sign a mandate, and above what budget is a second signer needed?
- What is the safe state per use case: stop, or revert to business as usual?
- How long may a mandate run before re-certification?
- Whose account holds the dispatcher credentials? Neutrality argues for the client's, as for the KMS key in Plan K §13.
- Migrating `canonical.py` to RFC 8785 without breaking existing ledger verification.

---

## 9. Source register

### 9.1 Opened primary sources [O] (33 rows, 36 documents, 27 repositories; date = latest commit)

| # | Document | Repository, path | Date | Used for |
|---|---|---|---|---|
| 1 | Bandits overview (Thompson sampling, 1% floor, bias, cohort weighting, urgency example) | [growthbook/growthbook](https://github.com/growthbook/growthbook) `docs/bandits/overview.mdx` | 2026-10-09 | 2.1, 3.1, 3.2, 3.7 |
| 2 | Contextual bandits (tree leaves, pooling small segments, when not to use) | same, `docs/bandits/contextual.mdx` | 2026-10-09 | 2.1, 3.1 |
| 3 | Contextual bandit technical details (best-arm probability, Gauss-Hermite) | same, `docs/statistics/contextual-bandit-technical.mdx` | 2026-10-09 | 3.1 |
| 4 | Open Bandit Pipeline README (estimators IPS, SNIPS, DR, Switch, MRDR, DRos, slates; ZOZOTOWN data) | [st-tech/zr-obp](https://github.com/st-tech/zr-obp) | 2022-11-04 | 3.3, 7.6 |
| 5 | Contextual Bandit algorithms (explore-first, epsilon-greedy default 0.05, bagging, cover, softmax) | [VowpalWabbit wiki](https://github.com/VowpalWabbit/vowpal_wabbit/wiki/Contextual-Bandit-algorithms) | 2026-09-22 | 3.1 |
| 6 | Logged Contextual Bandit Example (`action:cost:probability`; IPS, DM, DR) | VW wiki | 2026-09-22 | 3.3 |
| 7 | Explore-Eval (rejection sampling; "very data hungry") | VW wiki | 2026-09-22 | 3.3 |
| 8 | Epsilon Decay (non-stationary exploration; confidence-sequence champion/challenger) | VW wiki | 2026-09-22 | 3.1, 3.4 |
| 9 | cb_dro (distributionally robust lower bound; geometric decay) | VW wiki | 2026-09-22 | 3.1, 3.4 |
| 10 | SquareCB (`--elim` harms logged data for counterfactual evaluation) | VW wiki | 2026-09-22 | 3.3 |
| 11 | Conditional Contextual Bandit and Slates (multi-slot decisions) | VW wiki | 2026-09-22 | 4 |
| 12 | Warm-Starting Contextual Bandits | VW wiki | 2026-09-22 | 4 |
| 13 | Learning Loop README and RL_SIM (Personalizer successor; VW config) | [microsoft/learning-loop](https://github.com/microsoft/learning-loop) | 2024-11-25 | 2.1, 7.6 |
| 14 | ReAgent README (archived; batch off-policy; counterfactual policy evaluation; behaviour cloning) | [facebookresearch/ReAgent](https://github.com/facebookresearch/ReAgent) | 2026-08-31 | 2.1, 7.4 |
| 15 | Pearl README (production RL; safety module for bidding) | [facebookresearch/Pearl](https://github.com/facebookresearch/Pearl) | 2026-08-18 | 2.1 |
| 16 | Lambda Learner README (GAME; incremental random effects; Thompson sampling) | [linkedin/lambda-learner](https://github.com/linkedin/lambda-learner) | 2021-08-11 | 2.1, 4 |
| 17 | tau-bench README (pass^k leaderboard) | [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) | 2026-03-18 | 3.8 |
| 18 | tau²/tau³-bench README (v1.0.1 grading fix; 75+ task fixes; not comparable before 1.0.1) | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | 2026-10-06 | 3.8, 4 |
| 19 | MAST README and failure-mode definitions | [multi-agent-systems-failure-taxonomy/MAST](https://github.com/multi-agent-systems-failure-taxonomy/MAST) | 2025-07-23 | 3.8, 5.7 |
| 20 | Agents SDK guardrails (parallel vs blocking; tool guardrails) | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) `docs/guardrails.md` | 2026-10-08 | 3.8, 5.7 |
| 21 | Agents SDK human-in-the-loop (needs_approval; fail closed; RunState resume) | same, `docs/human_in_the_loop.md` | 2026-10-08 | 3.8 |
| 22 | Building Effective Agents cookbook README | [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks) `patterns/agents` | 2026-10-10 | 3.8 |
| 23 | A2A CHANGELOG (v1.0.0 12 Mar 2026; v1.0.1 26 May 2026) and specification §8.4 (JWS + JCS signing) | [a2aproject/A2A](https://github.com/a2aproject/A2A) | 2026-10-07 | 5.6 |
| 24 | AP2 docs: index, overview §4.2, glossary, CHANGELOG (v0.2.0 28 Apr 2026) | [google-agentic-commerce/AP2](https://github.com/google-agentic-commerce/AP2) | 2026-04-29 | 1, 5.6, 7.2 |
| 25 | MCP specification 2026-07-28 changelog (stateless; MRTR; tasks extension; no SSE resumability; OTel trace context) | [modelcontextprotocol/modelcontextprotocol](https://github.com/modelcontextprotocol/modelcontextprotocol) | 2026-10-10 | 5.5, 7.4 |
| 26 | OTel GenAI agent spans (moved; status Development) | [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai) | 2026-10-09 | 5.6 |
| 27 | Model Transparency README and CHANGELOG (model signing) | [sigstore/model-transparency](https://github.com/sigstore/model-transparency) | 2026-10-05 | 5.6 |
| 28 | Kafka upgrade docs (KIP-932 production-ready in 4.2; share-group DLQ) | [apache/kafka](https://github.com/apache/kafka) `docs/getting-started/upgrade.md` | 2026-10-11 | 6.2 |
| 29 | Feast point-in-time joins and Label View | [feast-dev/feast](https://github.com/feast-dev/feast) | 2026-10-10 | 3.2, 6.4 |
| 30 | EconML policy learning reference and policy interpreter | [py-why/EconML](https://github.com/py-why/EconML) | 2026-10-01 | 7.6 |
| 31 | CausalML README (multi-treatment cost optimisation; benchmarks) | [uber/causalml](https://github.com/uber/causalml) | 2026-10-05 | 3.5 |
| 32 | Criteo large-scale ITE/uplift benchmark README | [criteo-research/large-scale-ITE-UM-benchmark](https://github.com/criteo-research/large-scale-ITE-UM-benchmark) | 2021-08-30 | 3.5 |
| 33 | Cedar README, LangGraph README, NeMo Guardrails README, Bedrock AgentCore SDK README | [cedar-policy/cedar](https://github.com/cedar-policy/cedar); [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph); [NVIDIA/NeMo-Guardrails](https://github.com/NVIDIA/NeMo-Guardrails); [aws/bedrock-agentcore-sdk-python](https://github.com/aws/bedrock-agentcore-sdk-python) | 2026-10-06 to 2026-10-11 | 3.8, 5.3 |

### 9.2 Our code read [code]

- `engine/agentic/`: registry, tools, policy, proposals, planner, claims, ledger, receipts, evals, service, artefacts, save_desk.
- `engine/decide/`: arbitrate, offer_choice, variant_arms, treat_list, value, contactability.
- `engine/uplift/`: ope, policy, champion, drift, actions, incrementality, learners.
- `engine/measurement/`: harm, plan, cycle, learn, programme, pooling, compare.
- `engine/holdout/`: assign, flow.
- `engine/scheduling/`: firing, schedules, scheduler, alerts, retraining.
- `engine/compliance/`: gate, handoff.
- `engine/generative/`: guardrails, win_back.
- `engine/approvals.py`.
- `api/mcp/`: server, gate, limits.

### 9.3 Search-summary sources [S]

Cited inline above. The main ones:

- **Vendors and acquisitions:** [Braze](https://www.braze.com/resources/articles/what-is-offerfit) · [MoEngage–Aampe](https://www.moengage.com/?p=87075) · [Azure Personalizer retirement](https://azure.microsoft.com/updates/ai-services-personalizer-will-be-retired-on-1-october-2026)
- **Industry decision systems:** [Duolingo KDD 2020](https://research.Duolingo.com/papers/yancey.kdd20.pdf) · [LinkedIn 2022](https://arxiv.org/pdf/2207.03029) · [Decision Service](https://arxiv.org/pdf/1606.03966) · [Wayfair](https://www.aboutwayfair.com/careers/tech-blog/contextual-bandit-for-marketing-treatment-optimization) · [Uber](https://arxiv.org/pdf/2407.19078) · [Booking](https://arxiv.org/pdf/2008.06293) · [Amazon](https://www.amazon.science/publications/contextual-multi-armed-bandits-for-causal-marketing) · [Netflix QCon](https://qconsf.com/sf2018/system/files/presentation-slides/2018-11_-_qcon_artwork_personalization_talk.pdf) · [Spotify BaRT slides](https://idea-stat.snu.ac.kr/seminar/20181207/Explore,%20exploit,%20and%20explain_%20personalizing%20explainable%20recommendations%20with%20bandits.pdf)
- **Learning from logs:** [CRM, PMLR](https://proceedings.mlr.press/v37/swaminathan15.html) · [Gilotte et al.](https://arxiv.org/pdf/1801.07030) · [Practical Bandits](https://arxiv.org/pdf/2302.01223) · [Gigli and Stella](https://proceedings.mlr.press/v244/gigli24a.html)
- **Experimentation and pacing:** [Spotify risk-aware decisions](https://engineering.atspotify.com/2024/3/risk-aware-product-decisions-in-a-b-tests-with-multiple-metrics) · [CLASH](https://www.microsoft.com/en-us/research/?p=974475) · [Snap pacing](https://www.arxiv.org/pdf/2509.25429)
- **Agent design and autonomy:** [Anthropic multi-agent research](https://www.anthropic.com/engineering/multi-agent-research-system) · [Cognition 2026](https://cognition.com/blog/multi-agents-working) · [Feng et al.](https://arxiv.org/abs/2506.12469v1) · [Mitchell et al.](https://arxiv.org/abs/2502.02649v3) · [Morris et al.](https://arxiv.org/abs/2311.02462v3) · [OWASP Agentic Top 10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026) · [EU AI Act Art. 14](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-14)
- **Incidents:** [Replit](https://fortune.com/2025/07/23/ai-coding-tool-replit-wiped-database-called-it-a-catastrophic-failure) · [Air Canada](https://www.law360.ca/ca/articles/1804075) · [Project Vend](https://www.smartcompany.com.au/artificial-intelligence/anthropic-ai-small-retail-business-trial/) · [Knight Capital](https://www.sec.gov/Archives/edgar/data/0001060749/000119312512341182/d392788d424b3.htm)
- **Content generation and advertising rules:** [Nextdoor](https://engblog.nextdoor.com/let-ai-entertain-you-increasing-user-engagement-with-generative-ai-and-rejection-sampling-50a402264f56) · [ASCI draft](https://www.ascionline.in/wp-content/uploads/2026/05/AI-Draft-Guidelines-1.pdf) · [C2PA (PPC Land)](https://ppc.land/c2pa/)
- **Data infrastructure:** [IBM–Confluent](https://www.softwarereviews.com/vendor-technology-notes/ibm-s-smartest-ai-infrastructure-move-since-red-hat) · [Kinesis vs Kafka](https://www.conduktor.io/glossary/kafka-vs-kinesis) · [Databricks–Tecton](https://blogs.perficient.com/2025/09/26/databricks-acquires-tecton/) · [Fivetran–Census](https://www.techtarget.com/searchdatamanagement/news/366623554/Fivetran-adds-reverse-ETL-with-acquisition-of-Census)
- **Channel limits:** [SES](https://docs.aws.amazon.com/ses/latest/DeveloperGuide/manage-sending-quotas-request-increase.html) · [Gmail/Yahoo](https://www.bounteous.com/insights/2024/01/31/2024-gmail-and-yahoo-deliverability-changes/) · [WhatsApp limits](https://cpaas.8x8.com/ph/blog/important-update-whatsapp-messaging-limits-changing-october-7-2025/) · [TRAI Nov 2025](https://teamleaseregtech.com/updates/article/49534/trai-issues-direction-to-curb-misuse-of-sms-headers-content-templates/) · [FCM](https://firebase.google.com/docs/cloud-messaging/throttling-and-quotas) · [Braze limits](https://www.braze.com/docs/api/api_limits) · [CleverTap limits](https://developer.clevertap.com/docs/api-request-limit)
