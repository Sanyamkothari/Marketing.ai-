# Marketing decisioning ML in production: lessons for Autonomy's brain

21 Oct 2026. This note follows `docs/v2/REAL_DATA_RESULTS.md` and `docs/v2/AGENT_BRAIN.md`. The
problem it starts from: on randomised data (Hillstrom, X5, Criteo, MegaFon) the brain does not beat
simple policies. It values a message at about 10x what it is worth, learns uplift from clicks,
ignores history, cannot learn on channels without clicks, and cannot use customer features.

**How the sources were read.** This session's network allowed only GitHub, PyPI and microsoft.com.
Sources marked **[full]** were read in full from the original text. Sources marked **[summary]**
come from search-engine extracts of the paper or post: arXiv, ACM, Medium, Netflix, LinkedIn and
the vendor sites were blocked. Quotes from [summary] sources are as the extract gave them. Check
them against the original before quoting them anywhere else.

---

## The short version: what production teams converge on

1. **Optimise and calibrate on the business outcome, measured against a randomised control.
   Never on clicks.** Booking, Microsoft, Pinterest, Twitter and Netflix all say this in different
   words. Our 10x overestimate is the textbook "proxy over-optimisation" failure.
2. **Blanket sending is a strong default, and a ranking has to earn the right to target.** On
   Hillstrom, an independent replication found that no targeting rule beats "e-mail everyone"
   out of sample. On Criteo, a plain response model ranks better than uplift learners.
3. **Volume is a per-user budget with a long-term price.** Teams set that price from
   unsubscribe models measured weeks later and tune it with online volume experiments. Several
   reported cutting volume by 6-30% at no cost to the business metric.
4. **Exploration is small, logged and boring.** Teams use epsilon or softmax with a floor of
   about 5%, and log the exact probability of every action, including "do nothing", when they
   decide. Off-policy evaluation uses doubly robust estimates with overlap checks.
5. **A permanent long-term holdout is the ground truth.** Short-term surrogates (about 2 weeks)
   are used to decide faster, but teams keep checking them against the holdout. Every test gets
   an automatic sample-ratio-mismatch (SRM) check.

---

## 1. Booking.com: "150 successful ML models: 6 lessons" (KDD 2019) [full]

PDF: <https://github.com/manjunath5496/ML-Papers/blob/main/150%20Successful%20Machine%20Learning%20Models-%206%20Lessons%20Learned%20at%20Booking.pdf>
(paper DOI 10.1145/3292500.3330744)

Takeaways:
- Offline gains do not predict business gains. Across 23 model comparisons, offline performance
  gain against business-value gain had a "Pearson correlation [of] -0.1".
- Proxy over-optimisation: "we might build a recommender system based on Click Through Rate
  because we know that CTR has a strong correlation ... with Conversion Rate ... But as models get
  better and better, they might end up 'just driving clicks', without any actual effect on
  conversion ... over-optimizing proxies leads to distracting the user away from their goal."
- Problem construction matters more than model tuning. "Setups where simple models can do
  significantly better than trivial models [random, popularity] are preferred."
- Response Distribution Analysis: look at the histogram of the model's outputs. Odd shapes "detect
  defects in the models very early."
- Triggered analysis: when comparing two models, analyse only "users for which the models
  disagree".

What this means for Autonomy's brain:
- The click-to-purchase bridge (`LiftPerClick`) is exactly the proxy Booking warns about. Value
  must be learned on purchases and profit, with clicks used at most as a feature or a ranking
  signal.
- Add a response-distribution check that runs every night. If the brain predicts positive net
  value for 95%+ of eligible customers, or if the mean predicted value per send is more than about
  2x the holdout-measured value, raise an alarm. This check would have caught the Hillstrom $4.08
  vs $0.38 gap on day one.
- Report agent vs rule on the triggered set, meaning customers where the two policies chose
  differently. That is where the evidence sits, and it is smaller and sharper than the whole 80/10
  split.

## 2. Microsoft ExP: "A Dirty Dozen: twelve metric interpretation pitfalls" (KDD 2017) [full]

<https://www.microsoft.com/en-us/research/wp-content/uploads/2020/08/2017-08-KDDMetricInterpretationPitfalls.pdf>

Takeaways:
- SRM: "Most of the time SRM implies a severe selection bias, enough to render the experiment
  results invalid." "It is critical that the experimentation system automatically detects sample
  ratio mismatches."
- Incomplete funnel (Xbox sale promotions): changes had "large positive impacts on the number of
  users who click through to the sale", but "we did not see any corresponding revenue increase
  even when it had sufficient power."
- Ratio metrics such as CTR can fall while both clicks and views rise. "Always define count
  metrics for the numerator and the denominator."
- Peeking: "Continuously checking the results and stopping as soon as statistical significance is
  achieved leads to an increase in Type-I error."
- Novelty and primacy: personalisation algorithms that must gather data have a primacy effect.
  "We can mitigate these effects by doing a warm start." Also, segment the effect "by different
  days of the experiment".

What this means for Autonomy's brain:
- Add an automatic SRM test on the 80/10/10 hash split, and on every "metric SRM". An example is
  the share of each group that has any recorded event, because telemetry loss can differ by
  group. Block the proof report when SRM fails.
- The Xbox result matches our Hillstrom result: clicks moved and revenue did not. Report
  incremental purchases and profit per customer as the headline (the OEC). Report clicks only
  as diagnostics.
- The nightly CUPED proof is a continuous look at the data. Use an always-valid (sequential)
  interval or a pre-set horizon for any "Autonomy is beating X" claim. The breaker's exact Poisson
  tail is already the right spirit.
- Warm start matters (see failure 2 in REAL_DATA_RESULTS). Report the agent's lift by week since
  start, so primacy (the slow first month) shows up instead of hiding in a pooled number.

## 3. Microsoft ExP: "Patterns of Trustworthy Experimentation" (pre- and during-experiment) [full]

<https://www.microsoft.com/en-us/research/articles/patterns-of-trustworthy-experimentation-during-experiment-stage/>,
<https://www.microsoft.com/en-us/research/articles/patterns-of-trustworthy-experimentation-pre-experiment-stage/>

Takeaways:
- "SRMs typically invalidate the A/B test." Track "error rates, data loss, join rates" as data
  quality metrics.
- "Rapid decline of estimated treatment effect across date segments ... is an indicator of novelty
  effect."
- Retrospective A/A: "computes the same metric for the treatment and control group in the period
  before the experiment start." Combine this with variance reduction.
- Guardrails with "auto-shutdown for A/B tests that significantly degrade the product".

What this means for Autonomy's brain:
- Before any proof claim, run a retrospective A/A. The pre-period purchase rate of agent, rule and
  holdout should match. We already compute that pre-period rate for CUPED, so this check is
  nearly free.
- The complaint and unsubscribe breaker is our auto-shutdown guardrail. Keep it.

## 4. Uber CausalML methodology docs (meta-learners, CATE evaluation, value estimator) [full]

<https://github.com/uber/causalml/blob/master/docs/methodology.rst>; the cost-aware method comes
from Zhao & Harinen, "Uplift Modeling for Multiple Treatments with Cost Optimization" (DSAA 2019,
<https://arxiv.org/abs/1908.05372>) [summary].

Takeaways:
- T-, X-, R- and DR-learners are listed. The X-learner blends with "propensity scores for g(x)".
  The DR-learner is cross-fitted over 3 folds.
- On choosing a CATE model: "Cross-validation cannot be used ... the ground-truth treatment effect
  is never observed." Use the DR pseudo-outcome loss instead. "DR-based metrics dominate across 78
  benchmark datasets."
- On choosing a targeting rule: score it with RATE/Qini, plus a bootstrap test of "the null that
  the ranking is no better than random". The autoc weighting suits benefit concentrated in a small
  subgroup. The Qini weighting suits diffuse effects.
- Counterfactual value estimator: value of treatment w = E[(v - cc_w) Y_w - ic_w]. Here cc_w is
  the cost paid only on conversion (a redeemed coupon) and ic_w is the cost paid regardless (the
  send cost).
- Zhao & Harinen define a "Net Value CATE", which puts cost inside the uplift objective rather
  than subtracting it afterwards.

What this means for Autonomy's brain:
- Our net-value formula (uplift x value - offer cost x P(redeem) - channel cost) matches the
  CausalML value estimator. The formula is fine. The input is wrong: Y_w must be P(purchase | w)
  from randomised data, not clicks.
- For the light T-learner in the fix wave, cross-fit it, choose and score it with the DR loss,
  and test its ranking with RATE and a bootstrap CI before the bidder trusts it. If RATE does not
  exclude 0, the bidder should use the shop-level average effect (ATE) only.
- Offer cost must use P(redeem | offer) for people who would have bought anyway. That is the
  "sure things" cost in CausalML's unit-selection framing, and it is where discounts lose money.

## 5. Independent Hillstrom and Criteo replication: "uplift-targeting" [full]

<https://github.com/Pchambet/uplift-targeting>

This is not a company paper, but it is the most directly relevant evidence we found. It ran the
same datasets as our real-data test, with careful out-of-sample selection.

Takeaways:
- "On a 64,000-customer randomised test the honest answer is everyone." "Blanket sending earns
  $155 per 1,000 customers against $115 ... for the best uplift procedure", which "falls short of
  blanket in 48 of 50 random half-splits".
- "An in-sample model contest would have promised a gain ... +$27 per 1,000 ... chosen out of
  sample, the same idea is -$27."
- "Heterogeneity is real for visits and invisible for revenue." On spend, "0 of 22 rankings beat
  random targeting" (with 578 buyers).
- On Criteo, "the effect tracks the baseline rate, so the response model's top 20% holds 87% of
  all incremental conversions, against 73% for the DR-learner". The DR-learner "returns one
  constant score for 63% of users" at a 0.19% base rate.
- "The cost per e-mail decides between everyone and no one": blanket pays while cost is below
  margin x spend lift, "about 31 cents".

What this means for Autonomy's brain:
- **Contradiction with our framing.** On Hillstrom, contacting 97% is close to the right answer.
  The brain's fault is not that it contacts too many people. Its value estimate is wrong, so it
  cannot tell when contacting nobody would win. That is why raising the e-mail cost to $0.25
  changed nothing, when the correct break-even is about $0.31. After the fix, test this: sweep the
  channel cost, and the brain should flip from everyone to no one near the measured break-even.
- **Expect "send to all" to be the best policy often.** The bar for a release is to match the
  best blanket arm and beat it only where the data can show heterogeneity out of sample. Choose
  the targeting model on one half of the data and score it on the other, or the result will
  overfit.
- When base rates are low (Criteo, most shops), model uplift as baseline P(buy) x relative lift.
  Do not use a free-form difference of two models. This fits our multiplicative Gamma-Poisson
  structure: put P(buy | features, history) where the click rate is now.

## 6. Microsoft Decision Service / Vowpal Wabbit [full: VW docs; summary: paper]

VW wiki: <https://github.com/VowpalWabbit/vowpal_wabbit/wiki/Contextual-Bandit-algorithms>; paper:
Agarwal et al., "Making Contextual Decisions with Low Technical Debt"
(<https://www.microsoft.com/en-us/research/publication/making-contextual-decisions-with-low-technical-debt/>).

Takeaways:
- The paper says contextual bandits in production fail mostly through "incorrect data collection
  and weak debuggability". Its fix is a loop of explore, log, learn and deploy, where the explore
  and log steps produce "correct, unbiased data". Reported results: CTR +25-30%, revenue +18%.
- In VW the default exploration is epsilon-greedy with "epsilon = 0.05". Softmax exploration has a
  guarantee: "each action is guaranteed at least ε/num_actions probability." On the RND explorer:
  "mixing in a small amount of uniform exploration ... stabilizes the technique." On bagging: it
  "rules out obviously bad actions, while exploring amongst the plausibly good ones."
- On estimators, VW says IPS is "unbiased but high variance". DR is "generally a good middle
  ground". The default is per-action regression (`mtr`).
- A later MSR post adds that "unfocused exploration can negatively impact system performance and
  erode user faith" [summary].

What this means for Autonomy's brain:
- Our Thompson sampling with a 5% floor (`Goal.exploration_floor = 0.05`) and `prune_z = 2`
  matches the VW defaults and bagging. No change is needed in how much we explore.
- **What has to change is logging.** REAL_DATA_RESULTS gap 15 says the Decision drops the
  per-option probabilities. Without those probabilities, our own DR proof cannot be trusted.
  Thompson sampling probabilities have no closed form, so estimate them by Monte Carlo (draw the
  posterior about 200 times and count wins). Log them for every option, including "nothing".
- Keep the explore-log-learn-deploy discipline: what the brain learns from must be exactly what
  was logged at decision time, with the same features and probabilities.

## 7. ZOZO Open Bandit Dataset and Pipeline [full: README]

<https://github.com/st-tech/zr-obp>

Takeaways:
- "Each campaign randomly used either the Uniform Random policy or the Bernoulli Thompson Sampling
  policy for the data collection." Holding several logging policies on the same platform is what
  makes "realistic and reproducible experimental comparisons of different OPE estimators"
  possible.
- The estimator menu goes beyond DR: SNIPW, Switch, MRDR, DRos (doubly robust with optimistic
  shrinkage) and SGDR. All of them exist to control the variance that large importance weights
  cause.

What this means for Autonomy's brain:
- The rule group (10%) and the agent group (80%) are already two logging policies on one
  platform. We can validate our OPE the way OBD does: predict the rule group's value from the
  agent group's logs and compare it with the rule group's actual value. That gives a free,
  continuous check of the OPE.
- Add DRos or Switch next to DR, and the overlap warning from gap 15 (the bank data had an
  effective n of 97 out of 20,653). When effective n is small, report "cannot evaluate" and do
  not print an interval.

## 8. Duolingo: "A Sleeping, Recovering Bandit Algorithm for Optimizing Recurring Notifications" (KDD 2020) [summary]

Paper: <https://research.duolingo.com/papers/yancey.kdd20.pdf>; replication README:
<https://github.com/jakemaz66/RecoveringSleepingBandit> [full].

Takeaways:
- "Novelty effects and conditional eligibility make conventional bandit algorithms unsuitable."
  Arms "sleep" (they are sometimes ineligible) and "recover" (their effect wears off with repeated
  use and comes back after a rest).
- Each arm's score is penalised by recency: score minus γ·0.5^(d/h), where d is days since this
  user last got the arm and h is a half-life. The penalty is applied per user.
- The arm is chosen by softmax over the score differences, compared with a baseline. The reward
  is lesson completion, with a 2-hour window according to the replication. That reward is the
  business action, not opening the notification.
- Result: "+0.5% total DAUs and +2% new user retention" over a strong baseline.

What this means for Autonomy's brain:
- Add a per-customer, per-template recency penalty with a learned half-life to the bid. Our
  contact-harm posterior prices overall volume. It does not price "the same message again", which
  is a different effect.
- Use the business action in a short window as the reward (purchase within N days of the send),
  and compare it with the holdout's rate over the same window. Our bandit already has the right
  shape. Its reward is the wrong one.
- Calibrate expectations: a mature bandit at this scale added about 0.5% to DAU. A 10x
  overestimate in value is a bug, not ambition.

## 9. LinkedIn: email and notification volume optimisation (KDD 2016, CIKM 2017, WSDM 2022, 2022-25) [summary]

"Email Volume Optimization at LinkedIn" (KDD 2016, DOI 10.1145/2939672.2939692); blog
<https://engineering.linkedin.com/blog/2016/06/less-is-more--optimizing-email-volume-part-1>;
"Multi-objective Optimization of Notifications Using Offline RL" <https://arxiv.org/abs/2207.03029>;
"A State Transition Model for Mobile Notifications via Survival Analysis"
<https://arxiv.org/abs/2207.03099>; Decision Transformer follow-up <https://arxiv.org/abs/2509.02458>.

Takeaways:
- LinkedIn frames it as a constrained problem: "minimizes the total number of emails sent while
  satisfying constraints on desired levels of ... sessions and complaints". A complaint means an
  unsubscribe click or a spam report.
- "Simply dropping emails based on session probability or complaint probability individually
  leads to poor trade-offs." Both have to go into one score, with the operating point (fs = 0.985,
  fc = 0.6) tuned online.
- Later work credits engagement to a weekly count of notifications, not to single notifications.
  The team then moved to offline RL (CQL) and Decision Transformers, with the reward built from
  sessions, clicks and disables. Gains were about +0.7% sessions with fewer notifications.

What this means for Autonomy's brain:
- Our "attention price" is the Lagrange multiplier of LinkedIn's constraint. That is a good
  sign, but LinkedIn tunes it online against measured complaints and sessions. It does not derive
  it from a model alone. Set our price the same way: a guardrail target (unsubscribes per 1,000
  sends) and a per-shop multiplier that the nightly loop adjusts.
- Work at the level of the weekly budget. Fatigue is a property of how many messages a customer
  got in total, not of any one send.

## 10. Pinterest: "Notification Volume Control and Optimization System" (KDD 2018) [summary]

DOI 10.1145/3219819.3219906; summaries at <https://eugeneyan.com/writing/push/> and
<https://medium.com/nerd-for-tech/paper-explained-notification-volume-control-and-optimization-system-at-pinterest-f4a85608d05c>.

Takeaways:
- The goal is to set per-user volume "such that long term user engagement is optimized".
- They model two responses to volume: stay subscribed or unsubscribe. "Activity of users who
  unsubscribe will typically continue dropping for a few weeks until it becomes stable." Training
  labels come from activity about four weeks later.
- Hill-climbing: raise a user's weekly volume while the incremental reward stays above a
  threshold.
- Result: "reduce notification volume by 6-24% while increasing CTR by 11-31% and site engagement
  ... by 1-3%."

What this means for Autonomy's brain:
- The cost of an unsubscribe is "future value x hazard", and that cost is measurable. Compare
  unsubscribed customers with similar subscribed customers for 4 weeks afterwards. Do not assume
  an unsubscriber's future value goes to zero, because many keep buying. Our attention price may
  be too high or too low. Only this measurement tells which.
- Decide volume per customer per week, then fill the budget with the best messages. This is
  simpler and more stable than deciding send or no-send every day.

## 11. Twitter: "Should I send this notification? ... by modeling the future" (2022) [summary]

<https://arxiv.org/abs/2202.08812>

Takeaways:
- "A myopic system will always send, since the negative effects arrive later."
- They used model-based RL on logged data, with a reward equal to the discounted sum of future
  opens. In production it sent "significantly fewer notifications than baseline heuristics while
  keeping user engagement at the same level", with a higher open rate.

What this means for Autonomy's brain:
- This explains our 94-97% contact rate. Any brain that prices only today's value will send to
  nearly everyone. The attention price is the right fix, but it has to be calibrated (see
  Pinterest) and it has to be large enough to bind. Test: in the Gym's `sleeping_dogs` and in a
  fatigue scenario, the brain's contact rate must fall below the cap.

## 12. Zillow: "Volume Governing for Email and Push Messages" (RecSys 2022) [summary]

<https://dl.acm.org/doi/10.1145/3523227.3547399>

Takeaways:
- Zillow treats it as binary classification: drop a message when its predicted click value is
  low. The threshold was set to keep 98% of clicks.
- A/B result: "decreases messages per user by 30% and clicks per user by 1.5%, while doing no harm
  to core business metrics", with fewer unsubscribes.
- Process: a model is released only if the A/B test shows no decline beyond the expected 2% drop
  in clicks.

What this means for Autonomy's brain:
- This is the cheapest fatigue lever with real evidence behind it. Rank today's candidate sends
  by calibrated net value and drop the bottom X% (the "governor"). Then prove in the 80/10/10
  split that purchases did not fall. Shops can understand this, and it does not depend on a
  perfect fatigue model.

## 13. Netflix: artwork personalisation (contextual bandits) [summary]

<https://netflixtechblog.com/artwork-personalization-c589f074ad76>; QCon slides
<https://qconsf.com/sf2018/system/files/presentation-slides/2018-11_-_qcon_artwork_personalization_talk.pdf>.

Takeaways:
- Each image is an arm. The reward is "quality plays" per impression, not clicks.
- Replay evaluates offline from logged exploration. "Unbiased results when logged probabilities
  are used, but [it] needs a lot of data and can have high variance when few matches occur."
- They ran replay offline first, then A/B tested the best personalised bandit against an
  unpersonalised bandit.

What this means for Autonomy's brain:
- Netflix's personalised bandit had to beat the unpersonalised bandit, not random. Our comparison
  is the same: the brain against the best blanket policy, which is what REAL_DATA_RESULTS already
  does.
- Use a "quality" outcome. For us that is a purchase or a non-refunded order within the window,
  not a click.

## 14. Long-term value: surrogates and long-term holdouts [summary]

Netflix, "Evaluating the Surrogate Index ... Using 200 A/B Tests" <https://arxiv.org/abs/2311.11922>;
Sigerson, Eckles et al., "Evaluating for the Long Term: Learnings from Industry" (2026)
<https://arxiv.org/abs/2608.08043>; Yang, Eckles, Dhillon, Aral, "Targeting for long-term outcomes"
(Management Science 2023) <https://arxiv.org/abs/2010.15835>; Google, Hohnhold et al., "Focus on
the Long-Term" (KDD 2015)
<https://research.google/pubs/focus-on-the-long-term-its-better-for-users-and-business/>.

Takeaways:
- Netflix compared 14-day surrogate-index decisions with day-63 effects. The inferences were
  "~95% consistent", but launch recall was only 79% and 65%.
- The 2026 industry workshop (15 platforms) concluded "there is no substitute for a well-run
  long-term experiment". Participants preferred "simple, interpretable surrogates" over elaborate
  indices, and surrogates that "improve decision-making".
- Yang et al.: impute the long-term outcome from short-term surrogates fitted on historical data,
  then learn the targeting policy on the imputed outcome. It beat a policy optimised on the
  short-term proxy (retention discounts at the Boston Globe). Their 1-month surrogate did not
  beat the status quo.
- Google: ads blindness is learned from the quality of past ads. Measuring that long-term user
  learning led Google to cut mobile ad load by 50%.

What this means for Autonomy's brain:
- Keep the never-contacted 10% holdout permanent. It is the thing every one of these teams says
  cannot be replaced.
- Use a simple surrogate for learning speed: incremental purchases within 14 days of a send. Fit
  the map from the 14-day effect to the 90-day effect on the holdout over time, and re-check it
  every quarter. Do not use clicks as the surrogate. They are one step too far from revenue.
- Google's lesson for fatigue: bad messages teach people to ignore all messages. Price the
  quality of past messages, not only how many there were.

## 15. Aampe: agentic personalisation (RecSys 2025, UMAP 2026) [summary]

Abboud et al., "Agentic Personalisation of Cross-Channel Marketing Experiences"
<https://arxiv.org/abs/2506.16429>; Jeunen, Hanna, Wheeler, "Sustained Impact of Agentic
Personalisation in Marketing" <https://arxiv.org/abs/2604.08621>.

Takeaways:
- They use "a Difference-in-Differences design for Individual Treatment Effect estimation, and
  Thompson sampling to balance the explore-exploit trade-off". Behaviour around each send is
  weighted by an exponential decay with a "tuneable half-life", and the estimate "accounts for
  periodic changes in organic engagement". The system is deployed across 150M users.
- The agent's messages are "layered on top of baseline rules-based messages delivered to both
  control and treatment groups".
- In the 11-month study, the largest gains came during active human curation. Agents "maintain a
  positive lift without further human input" afterwards. A third-party review notes the study
  reports no control-group numbers.

What this means for Autonomy's brain:
- This is the closest commercial design to ours: Thompson sampling, per-user learning and
  agents. Their signal is purchase-like events before and after each send, compared with the
  user's own trend. It is not clicks. That is a ready template for learning on channels without
  clicks (SMS, X5): a per-send DiD on purchases, shrunk to the segment.
- Their weak spot is ours to win on: a clean, permanent randomised holdout with DR proof. Our
  design is ahead there, provided the value estimate is honest.

## 16. Vendors: OfferFit (now BrazeAI Decisioning Studio), Hightouch [summary]

<https://venturebeat.com/business/how-offerfit-uses-self-learning-ai-to-stop-customer-churn>,
<https://hightouch.com/blog/what-is-offerfit>, <https://www.braze.com/resources/articles/what-is-offerfit>.

Takeaways:
- In OfferFit's framing the reward is the business label: "if a customer converts, they are
  labeled 'one'". The system explores across channel, timing, frequency, creative and offer.
- No public technical detail on holdouts or calibration was found. The vendor material is
  marketing.

What this means for Autonomy's brain: the vendors converge on the same shape (a business-outcome
reward, many dimensions, a bandit). Their public evidence is weaker than ours, so our proof (a
holdout plus DR) is a real point of difference, as long as it is correct.

## 17. Gutierrez & Gérardy, "Causal Inference and Uplift Modelling: A Review" (PMLR 2017) and Radcliffe's Qini (2007) [summary]

<https://proceedings.mlr.press/v67/gutierrez17a.html>

Takeaways: the review splits uplift methods into three families: two-model (T-learner), class
transformation, and direct uplift modelling. Uplift is evaluated with Qini and uplift curves, in
the style of Radcliffe (2007). Those curves rank customers. They do not calibrate. CausalML's notes
make the same point: "effect-magnitude accuracy" is "a distinct question from targeting quality".

What this means for Autonomy's brain: we need both. A **ranking** (Qini/RATE) decides whom to
contact first. A **calibrated level** (the holdout ATE) decides how many to contact and whether a
cost is worth paying. The 10x bug is a level error. A better ranker will not fix it.

---

## Where the literature contradicts our current design

| Our design | What production says | Verdict |
|---|---|---|
| Uplift learned from clicks x "extra purchases per click" | Booking, Microsoft (Xbox), Netflix, Duolingo and OfferFit all use the business outcome; clicks are a proxy that gets over-optimised | **Change.** Learn uplift on purchases and profit against randomised control |
| Per-person hierarchical click model as the core of value | Even 64k customers cannot show revenue heterogeneity (Hillstrom); Criteo heterogeneity tracks baseline propensity | **Demote.** Keep per-person effects for content and hour choice. Get value from (shop-level calibrated lift) x (feature-based relative lift) |
| Attention price from a modelled unsubscribe hazard x future value | Right idea (LinkedIn, Pinterest, Twitter), but always tuned online against measured guardrails, with unsubscribers' activity measured weeks later | **Keep, calibrate.** Add an online multiplier and the 4-week unsubscribe measurement |
| No per-template repetition cost | Duolingo: novelty decays and recovers per arm per user | **Add** a recency penalty |
| Thompson sampling with a 5% floor | Matches VW defaults; exploration amount is not the problem | **Keep**, but log exact per-option probabilities |
| 10% never-contacted holdout with DR proof | Every source treats a long-term holdout as ground truth | **Keep.** Add SRM, retrospective A/A, overlap warnings and sequential intervals |
| Bar: beat the fixed rule | Bar should be the best blanket arm, chosen and scored out of sample | **Change the bar** in Gym and real-data reports |

## Cold start for a new shop (what the sources imply)

1. Start as the best blanket policy: the shop's rule or send-all on the most-consented channel.
   Spend the first weeks learning the average effect, using the holdout and the random 5% "nothing"
   decisions inside the agent group.
2. Seed priors from history when the shop has a randomised or logged-propensity past campaign
   (failure 2). Also seed from a cross-shop prior of e-mail effects. Public randomised data puts
   one e-mail at about +0.5 to +1 percentage point on conversion (Hillstrom: men's e-mail +0.68
   pp), not 10x that.
3. Switch on targeting only after the ranking passes RATE with a CI above 0 on held-out data.
4. Report lift by week since start, so primacy (a slow first month) shows up.

## The three changes to make first

1. **Calibrate value on purchases against randomised control.** Per channel and content:
   net value = baseline P(buy | features, history) x relative lift x order value x margin
   - expected offer cost on people who would have bought anyway - channel cost - attention price.
   The relative lift starts at the shop-level average effect, measured by DR or CUPED on the
   holdout and on past randomised campaigns. Feature-based heterogeneity (the cross-fitted
   T-learner) moves a customer away from that average only when RATE passes. Add a nightly
   calibration alarm: mean predicted value per send against holdout-measured value, plus the
   share of customers with positive predicted value.
2. **Make the best blanket policy the default the agent must beat, with out-of-sample
   selection.** The agent targets only where a cross-fitted DR estimate on held-out customers says
   targeting beats blanket. Everywhere else it sends to all or to none, at the cost break-even.
   Report agent vs blanket on the triggered set, where the two disagree.
3. **Fix fatigue and logging together.**
   (a) Log Monte Carlo Thompson-sampling probabilities for every option, including "nothing".
   (b) Add a per-template recency penalty in the Duolingo style.
   (c) Turn the attention price into a guardrail-tuned weekly budget: drop the lowest-value sends
       in the Zillow style, with a multiplier tuned so unsubscribes per 1,000 stay at target.
   (d) Randomise volume caps inside a small slice of the agent group, so fatigue can be measured
       on purchases 4 weeks later and not only assumed.
