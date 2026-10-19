# 06 - Measurement and Self-Check: how the system proves and checks itself

*Research date: 2026-10-19. Web sources mostly from 2025-2026. Vendor claims are marked as such. WebFetch was blocked for most hosts, so some points rest on search-result summaries; these are flagged "(unverified)".*

## 1. The core problem in one paragraph

An autonomous marketing agent is only worth paying for if it can show money it *caused*, not money that would have arrived anyway. In 2026 the industry has mostly agreed on this. BCG (July 2026) reports that when companies start running proper incrementality tests on their next-best-action (NBA) programmes, **20-40% of live programmes turn out to have marginal or negative lift**. Haus case studies put the gap between what ad platforms report and what geo tests measure at **1.5x-3x**, worst on brand search and retargeting, where the customer was going to buy anyway (third-party summary, vendor cases). So "trust me, open rates went up" is dead. The question every buyer asks is: *versus what?*

## 2. How the industry proves marketing works

| Method | What it answers | Strength | Weakness | Fit for us |
|---|---|---|---|---|
| **Persistent random holdout / global (universal) control group** | "What did the whole programme add?" | The gold standard for owned channels (email, WhatsApp, SMS, push). Cheap, clean, customer-level. | Costs revenue (you deliberately do not market to the holdout). BCG warns it can *overstate* impact through halo, pull-forward (borrowing next quarter's sales) and contamination between overlapping tests. | Core. v1 already has a hashed, nested, persistent holdout (`engine/holdout/assign.py`). |
| **Per-decision or per-arm randomised tests** | "Was offer A better than B?" | Fast learning, feeds bandits. | Many small tests, multiple-comparison traps. | Core, via exploration slice. |
| **Geo / matched-market tests** | Effect of paid media or anything you cannot randomise per person | Works without user IDs. Google's Meridian GeoX and Meta GeoLift are open source. | Needs many regions and weeks. Noisy for small businesses. | Later, for paid media only. |
| **MMM (marketing mix modelling)** | Budget split across channels, top-down | Privacy-proof, covers offline. Google Meridian (open source, launched March 2024) is calibrated with experiment results. | Weekly aggregates, slow, wide error bars. Useless for per-customer decisions. | Not for v2's core. Maybe an "econometric analyst" add-on. |
| **Attribution (last-click, multi-touch)** | Who gets credit | Easy, every tool has it. | Not causal. Only 21.5% of marketers say last-click reflects long-term impact (eMarketer/Snap). | Show only as context, never as proof. |

**How the AI decisioning vendors do it**

- **OfferFit / BrazeAI Decisioning Studio.** They run a randomised trial: treatment gets AI-chosen messages, control gets "business as usual" (BAU), and uplift is read on one top-level KPI. Braze docs tell customers *not* to set a separate Braze control group, so OfferFit owns the control. Headline claim: +92% conversion vs BAU, $16M annualised (vendor Q&A, no method published). A Forrester TEI study paid for by Braze (April 2026, six interviews, one composite company) claims 457% ROI for Braze plus Decisioning Studio, and $76.7M from a cross-sell use case.
- **Hightouch AI Decisioning.** There are three groups: `treatment` (agent), `holdout` (no messages for this use case), `customer_managed` (BAU). Users are assigned by a deterministic hash of user ID + experiment key, mod 100. Assignments are written to the client's warehouse, so the client can check the lift themselves. Hightouch hires "forward-deployed marketing data scientists" to explain lift with counterfactuals and cohort breakdowns. In other words, **proving lift is partly a human service**.
- **Aampe.** Their docs say campaign-style holdouts do not fit an agent that builds a different sequence per user. They recommend **longer tests, a randomised share of users assigned to Aampe vs BAU, and cumulative metrics** instead of per-message metrics.

**Pattern:** the winning pitch is not "our model is accurate". It is **"agent vs your BAU vs nothing, randomised, in your own warehouse"**. That three-way split matters. Agent vs nothing proves marketing works at all. Agent vs BAU proves the *AI* is worth its fee.

## 3. How an autonomous agent should monitor itself (five layers)

**Business level (what the marketer sees)**
- Incremental revenue and incremental profit vs holdout, with a 95% interval, cumulative and per week.
- **Agent vs simple baseline.** Keep a slice (5-10%) running a dumb, transparent rule such as "best-selling offer, email, Tuesday 10am" or the client's BAU. If the agent cannot beat the baseline, it should say so.
- Guard against pull-forward: track holdout-vs-treated gaps at 30, 60 and 90 days after contact, not only on the day of the campaign.

**Learning level (is the policy getting smarter?)**
- Estimated regret: how much value was lost to exploration vs the best arm in hindsight.
- **Off-policy evaluation (OPE) before rollout.** Any new policy is scored on logged data first, using inverse propensity scoring (IPS), self-normalised IPS (SNIPS) and doubly robust estimators. This needs logged propensities (the probability each action had of being chosen), so **log propensities from day one**. Open Bandit Pipeline (ZOZOTOWN, NeurIPS 2021) showed that a well-known estimator can fail badly, so choose estimators by benchmark, not by habit.
- Exploration share and how fast it shrinks. Effective sample size per arm.

**Model level**
- Drift: population stability index (PSI) on features and scores.
- Calibration: predicted vs actual conversion by decile. Expected net value depends on calibrated probabilities, not just ranking.
- Uplift quality: Qini/AUUC with intervals on the holdout. v1's champion rule already refuses to promote a model whose AUUC interval includes zero (`engine/uplift/champion.py`). Keep that.

**Agent level (the LLM outer loop)**
- Fixed eval sets per agent: Strategist (goal → plan), Creative (brief → copy), Critic (copy → pass/fail). Re-run them on every prompt or model change.
- **LLM-as-judge is biased.** A study of 15 judges and 150k+ items (Shi et al., IJCNLP-AACL 2025) found real, systematic position bias. A 2026 AMCIS paper found the first option preferred 65.7% of the time, strong self-preference, and inter-judge kappa of only 0.36. So: swap answer order, use a different model family as judge, and check judges against a small human-labelled set every month.
- **Consistency, not just accuracy.** τ-bench introduced pass^k (all k runs succeed). GPT-4o fell from roughly 60% at pass^1 to about 25% at pass^8 in retail. Run each Strategist plan several times and flag unstable outputs.
- Trajectory checks: did the agent call the right tools, stay within budget, and respect the frequency cap and opt-outs? Score the final state (as τ-bench does), plus hard rules on every step.

**System level (circuit breakers)**
- Delivery rate, bounces, unsubscribe rate, spam complaints and WhatsApp blocks, per send batch, compared with each account's own baseline.
- External ceilings that matter commercially: Gmail and Yahoo want spam complaints **below 0.1% and never at or above 0.3%** (bulk senders, about 5,000+ messages a day). WhatsApp quality rating (green, yellow, red) is driven by blocks and reports over roughly 7 days. Message tiers run 250 → 2k → 10k → 100k → unlimited and are now set per business portfolio (since about October 2025). Details vary by source, so check them against Meta's own docs.
- **Auto-pause rules:** stop a campaign or channel when complaints exceed 0.08% in a batch, unsubscribes exceed 3x baseline, WhatsApp drops to yellow, delivery falls below 95%, or spend runs ahead of plan. Restart only with human approval.
- **Sequential tests for harm, not wins.** mSPRT (Johari, Pekelis and Walsh, *Operations Research* 2022) gives always-valid p-values, so you can look at every refresh without inflating false alarms. v1 already uses one-sided mSPRT for harm (`engine/measurement/harm.py`). Keep the asymmetry: **stop early for harm, never declare an early win.**

## 4. Simulators: test the agent before it touches real customers

| Simulator | What it is | Use for us |
|---|---|---|
| **RecoGym** (Criteo) | Bandit-style environment mixing organic browsing with recommendations | Pattern for click-and-convert loops; user state is not configurable |
| **RecSim NG** (Google, 2021) | Multi-actor, differentiable, probabilistic user models | Pattern for user state over time (fatigue, interest decay) |
| **KuaiSim** (2023) | Request, session and cross-session levels, fit to real logs | Pattern for retention-style, long-horizon goals |
| **Semi-synthetic uplift data** (IHDP, Criteo uplift 13.9M, Hillstrom-based, UpliftBench 2026) | Real features plus simulated outcomes with **known** per-person effects | Checks whether uplift models recover the truth |
| **LLM-simulated customers / personas** | An LLM role-plays a customer | Copy smoke tests only (see limits below) |

**Limits of LLM personas (2025-2026 evidence).** Personas match *averages* on stable topics but **fail at the individual level and do not recover relationships between variables** (2026 study). They underestimate how much real people vary and lean toward socially desirable answers (NIM Marketing Intelligence Review). Verasight (2025, compared with 1,500 real interviews) concludes they are not ready to replace real samples. Persona prompting can even make alignment worse for small subgroups. **Never use LLM customers to estimate lift or tune the policy.** Use them to catch broken, offensive or confusing copy.

**Key point:** generic simulators do not know *your client's* customers. The honest design is a **planted-truth gym**. Generate customers whose true response to each (offer, channel, time, format, frequency) is known, including sleeping dogs (people a message makes *less* likely to buy), fatigue, saturation and pull-forward. Then check whether the full agent finds them. v1 already has the seed of this: `engine/measurement/simulate.py` builds campaigns with a known effect, contamination and partial compliance, and checks that the intervals cover the truth.

## 5. Recommended self-check design for v2

**Stage 0 - The Gym (offline, every build).**
- A generator of synthetic populations with planted truth. Knobs: base rate, uplift per arm and segment, sleeping-dog share, fatigue curve, best send time per segment, channel cost, unsubscribe hazard, noise, contamination.
- Scenario library: "win-back with 15% sleeping dogs", "discount cannibalises full-price buyers", "WhatsApp outperforms email but fatigues fast", "data drift mid-campaign".
- Pass criteria: the policy captures at least X% of oracle value; it beats the baseline in at least 95% of seeds; the 95% intervals cover the truth 93-97% of the time; it never breaks the frequency cap or contacts opted-out customers; the circuit breaker trips within N batches in planted-harm scenarios.
- Also replay real logged data with OPE as a second, independent check.

**Stage 1 - Shadow mode (live data, no sends, 2-4 weeks).** The agent makes every decision and logs it with propensities, but BAU still runs. Compare its predicted value with what BAU produced. Check calibration and the Critic's rejection rate. Cheap, and it builds trust.

**Stage 2 - Live with a three-way split.** Recommended starting split: **agent 80% / BAU or simple baseline 10% / persistent no-contact holdout 10%**. Shrink the holdout toward 5% once the interval is tight. Reshuffle at most monthly, per Braze's guidance for global controls. Report agent vs holdout (total lift) and agent vs BAU (the AI's own value) separately.

**Stage 3 - Champion / challenger for strategies.** A new strategy (Strategist output, new offer set, new model) must pass the Gym, then OPE, then win a 10-20% live slice on incremental profit with an interval above zero, before it gets promoted. Extend v1's champion rule from models to whole strategies.

**Always on - Daily health dashboard (plain English):**
1. **Money:** incremental revenue and profit to date vs holdout, with interval ("we are 90% sure it added ₹1.8-2.6 lakh"), and ROI on the budget.
2. **Agent vs your old way:** lift over BAU or baseline.
3. **Who we left alone and why:** count of sleeping dogs and over-contacted customers skipped.
4. **Customer health:** unsubscribe, complaint and block rates vs baseline; WhatsApp quality; contacts per customer per week.
5. **Learning:** exploration share, top winning offer, channel and time per segment, confidence in each.
6. **Alerts and auto-pauses:** what tripped, what the agent did, what needs approval.
7. **Receipts:** every decision traceable to the data, the predicted value and the rule that allowed it.

**What makes marketers trust autonomy.** StackAdapt's August 2026 survey of 500 marketers found **89% comfortable with AI that prepares actions for approval, 78% with AI acting within human-set rules, but only 50% with full autonomy, even after proven performance**. Practitioners (34%) are far less comfortable than leaders (58%). Epsilon's 2026 benchmark says only 2% let agents run with no human involved. The main blockers are brand risk (63%), data quality (56%) and lack of transparency (36%). So autonomy has to be **earned in steps**: shadow, then approve-each-batch, then autonomous within guardrails. Each step is unlocked by the dashboard's evidence, not by a settings switch.

## What this means for us

**Do**
- Make the **three-way split (agent / BAU-or-baseline / persistent holdout)** the default for every goal, and write assignments to the client's own warehouse or export so they can check them.
- Log the **propensity** of every decision from day one. Without it there is no off-policy evaluation and no honest bandit learning.
- Build the **planted-truth Gym** as a product asset and CI gate, grown from v1's `simulate.py`. Use it in sales demos too ("watch it find the planted sleeping dogs").
- Report **incremental profit with intervals**, plus 30/60/90-day effects to catch pull-forward.
- Keep the asymmetry: **mSPRT stops for harm, never early wins.** Add hard circuit breakers tied to Gmail, Yahoo and WhatsApp thresholds, with auto-pause.
- Ship **shadow mode** as the first step of onboarding, and gate autonomy levels on evidence.
- Run LLM agents against fixed eval sets with order-swapped, cross-family judges and pass^k consistency checks.

**Don't (or not yet)**
- Don't use LLM-simulated customers to estimate lift, tune policies or prove ROI. Use them for copy smoke tests only.
- Don't build MMM or multi-touch attribution into v2's core. Geo tests and MMM are a later paid-media add-on.
- Don't show opens, clicks or attributed revenue as proof of value. Show them only as diagnostics.
- Don't promote any strategy on offline metrics alone. It must pass Gym, then OPE, then a live slice.
- Don't let full autonomy be the default setting.

**Open questions**
- What is the smallest client (customers, conversion rate) where a 10% holdout can detect lift in under 8 weeks? v1's `engine/uplift/power.py` can answer this. We should publish a minimum-size rule.
- How do we account for pull-forward and halo for clients whose cycle is longer than the test (B2B, annual renewals)?
- Should BAU be the client's real current programme, which is hard to run in parallel, or our simple baseline rule? Probably let them choose.
- How do we make holdout cost acceptable to business owners? Options: cap the "revenue at risk" shown up front, or shrink the holdout adaptively.
- Can the Gym be calibrated to each client (fitted from their history) without leaking the answer we are testing for?
- Need primary-source checks for: Braze's recommended global control size, WhatsApp per-user marketing caps, and BCG's specific fixes for halo and contamination (fetch was blocked).

## Sources
- https://www.bcg.com/publications/2026/measuring-incrementality-in-next-best-action-programs
- https://web-assets.bcg.com/pdf-src/prod-live/measuring-incrementality-in-next-best-action-programs.pdf
- https://lifesight.io/blog/marketing-incrementality-gap/
- https://www.braze.com/resources/articles/offerfit-ai-decisioning-webinar-questions
- https://braze.com/docs/user_guide/offerfit/building_use_cases
- https://www.braze.com/resources/articles/2026-forrester-tei-of-braze
- https://tei.forrester.com/go/braze/aidecisioningspotlight/
- https://www.braze.com/docs/es/user_guide/engagement_tools/testing/global_control_group/
- https://hightouch.com/docs/ai-decisioning/group-assignment
- https://startup.jobs/forward-deployed-marketing-data-scientist-hightouch-7568405
- https://kb.aampe.com/user-guide/how-do-i-know/experiments/experiment-overview
- https://haus.io/blog/is-metas-incremental-attribution-outperforming-standard-attribution-what-the-data-shows
- https://eightx.co/blog/what-is-incrementality-testing
- https://www.emarketer.com/content/faq-on-incrementality-how-prove-your-ads-actually-work-2026
- https://blog.google/products/ads-commerce/meridian/
- https://developers.google.com/meridian/geox
- https://arxiv.org/pdf/1512.04922 (Johari, Pekelis, Walsh - always-valid inference / mSPRT)
- https://ideas.repec.org/a/inm/oropre/v70y2022i3p1806-1821.html
- https://zr-obp.readthedocs.io/en/latest/ and https://arxiv.org/abs/2008.07146 (Open Bandit Pipeline)
- https://arxiv.org/html/2406.07791v9 (Judging the Judges - position bias)
- https://aisel.aisnet.org/amcis2026/sig_svs/svs/1
- https://arxiv.org/pdf/2406.12045 (τ-bench, pass^k)
- https://arxiv.org/pdf/2601.06112 (ReliabilityBench)
- https://arxiv.org/pdf/2103.08057 (RecSim NG)
- https://arxiv.org/pdf/2309.12645v1 (KuaiSim)
- https://ar5iv.labs.arxiv.org/html/2111.10106 (Criteo uplift benchmark)
- https://arxiv.org/pdf/2603.20775 and https://arxiv.org/pdf/2608.00915 (semi-synthetic uplift, UpliftBench)
- https://www.verasight.io/reports/synthetic-sampling-2
- https://nim.org/en/publications/detail/leaving-insight-to-digital-twins
- https://themodelwire.com/article/when-can-digital-personas-reliably-approximate-human-survey-findings-01KRD5QC7K3Y9H2A7DDCP3XGQK
- https://www.palisade.email/learning/spam-complaint-rate-email
- https://www.courier.com/blog/email-sender-requirements
- https://uptail.ai/blog/whatsapp-business-message-limits-2026-broadcast-caps-tier-progression-what-happens-when-you-hit-the-ceiling
- https://m.aisensy.com/blog/whatsapp-message-limits-guide/
- https://academy.pega.com/sites/default/files/media/documents/2024-03/MIS54751-4-EN-StudentGuide.pdf (Pega Impact Analyzer)
- https://ppc.land/only-6-of-marketers-act-on-in-platform-ai-recommendations-stackadapt-finds/
- https://www.advanced-television.com/2026/08/20/report-reveals-marketers-ai-trust-issues/
- https://s3.amazonaws.com/media.mediapost.com/uploads/EPSILON-2026-benchmark-ai-research-report.pdf
