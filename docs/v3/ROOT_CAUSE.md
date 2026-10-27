# Root cause: why v3 lost and why the plain agent has a ceiling

Two independent read-only audits (26-27 Oct 2026), each measured with tiny local runs and code reading. Full-size confirmation runs are in CI (`.github/workflows/diagnose.yml`, trigger `[diagnose]`; see [DIAGNOSE.md](DIAGNOSE.md)). Numbers below come from the auditors' scratch runs at 200 customers and one seed unless stated, so treat them as direction, not final size.

## Audit 1: rules, guards and AI prompts

# Is the agent losing value to too many rules, or to strict AI prompts?

**Short answer: neither, as a general cause.**
- **Too many rules:** not the general cause. The plain agent is held back mainly by the marketer's weekly cap of 2 messages. The guards it learns barely bind.
- **Strict AI prompts:** cannot be the cause. No LLM is called in the Gym, on the real bench or on the agent's production send path.
- **What does cause the Gym loss:** almost all of agent_v3's loss comes from one part, **journeys**, and from three fixed constants inside it.
- **Method:** the planned "read `blocked_by`" diagnostic cannot see that cause, so the way we were looking for the answer was also part of the problem.

## How the numbers were made
- Script: `/tmp/claude-0/-home-user-Marketing-ai-/b3510c52-036a-5471-a52e-fd5d81ce4bd7/scratchpad/diag/gate_tally.py`. It runs in-process on HEAD 29382881 and changes nothing in the repo.
- Every run: 200 customers, 90 days, seed 0, about 15-60 seconds each.
- Results: one JSON file per run in `/tmp/claude-0/-home-user-Marketing-ai-/b3510c52-036a-5471-a52e-fd5d81ce4bd7/scratchpad/diag/`.
- Value means the Gym's true value for the agent group, in dollars.
- These are single-seed tiny runs, so they show direction, not proof.

## 1. Every gate between "a bid exists" and "a message is sent"

| Gate | What it compares | Default | Learned or fixed |
|---|---|---|---|
| Holdout and baseline split | Hash group | 10% holdout, 10% baseline | Fixed |
| Hard rules (`rules.check_bid`) | Envelope channel and offer, offer approval, no_discounts, unsubscribed, consent, address, open complaint, snooze, weekly cap, budget left, quiet hours | Cap 2 a week (Gym envelope); quiet 21-9 | Fixed (marketer's envelope) |
| `too_soon` | Pending send or last contact within `MIN_GAP` | 6 h | Fixed |
| Breaker (stop rules) | Poisson or Wilson test on the last 7 days | Complaints 0.08%, unsubscribes 3x baseline, delivery below 95%, spend pace 1.2, at least 5 events, alpha 1e-4 | Fixed thresholds |
| Pacing | Surcharge, then a hold, as a stop rule nears | **Off** by default | Learned pressure |
| Attention price | Bid net vs P(unsubscribe or complaint) x 26 weeks of lost value + P(complaint) x $2 | — | Hazard is learned; 26 weeks and $2 are fixed |
| Weekly budget (rhythm) | Contacts in 7 days vs the best learned rhythm | — | Learned from fatigue and price |
| `plausible_z` and the bidders' `PRUNE_Z` spread floor | Net + 2 SD must beat nothing or the best option | z = 2.0 | Fixed z |
| Offer gate | An offer must beat its own plain version | — | Learned means |
| Restraint margin | Net must exceed 1.64 SD where lift is weak | Judged after 2 windows and 20 clicks | Fixed z; weak-lift flag learned |
| Leave-alone (`ContactHarm`) | P(harm) ≥ 0.9, or harm cost > every gain at +2 SD | Retried about 2% of weeks | Fixed threshold on a learned posterior |
| Exploration floor | Uniform pick | 5% | Fixed |
| v3 journeys `never_read` | 5 sends with no click or reply | 5 | **Fixed** |
| v3 journey value bar | `value_if_converted` vs bar (journeys C and D) | $50 | **Fixed**, not in the shop's currency |
| v3 journey covering | Single bids on a start channel are dropped once an email journey covers the goal | — | Fixed logic |
| v3 hold during journey, `max_defer`, positive net | Customer is held between steps | On; 2 days | Fixed |
| v3 frequency plan | Best of nothing / now / now and +10 days / wait 14 days; holds if waiting wins | 21-day horizon, 10-day memory, 0.98 a week | Fixed plans; prices learned |
| v3 who: leave-alone by traits | Group P(harm) ≥ 0.9, groups of at least 20 | — | Fixed threshold |
| v3 who: urgency | x (1 + 0.5 x churn risk) | 0.5 | **Fixed** |
| v3 who: offer ceiling | Offer cost ≤ 25% of max(90-day value, earnings) | 0.25 | Fixed |
| v3 offer budget price | Offer cost x (1 + shadow price) | Starts at 0 | Learned each day |
| v3 moments | Urgency x multiplier | 1.15-1.6 each, at most 2.5 | **Fixed** |
| v3 experiments wrapper | Assigns to running experiments | Book is empty | Does nothing |

## 2. Which gate stopped each customer-day
Shares of agent-group customer-days. "Hard cap" means the hard weekly cap; "weekly budget" means the learned rhythm hold.

| Gate | baseline_shop, agent | baseline_shop, agent_v3 | whatsapp_fatigue, agent | whatsapp_fatigue, agent_v3 |
|---|---:|---:|---:|---:|
| **Sent** | 22.9% | 14.7% | 19.6% | 11.0% |
| Hard cap (2 a week) | 55.9% | 27.2% | 35.1% | 5.4% |
| Weekly budget | 11.7% | 16.3% | 32.9% | 34.8% |
| **Journeys dropped every option** | — | **28.7%** | — | **25.1%** |
| Journey waiting for its next step | — | 3.0% | — | 3.3% |
| Frequency plan hold | — | 2.7% | — | 12.6% |
| No consent or unsubscribed | 9.2% | 6.6% | 7.9% | 6.7% |
| Restraint, value, price or Thompson | 0.3% | 0.5% | 4.4% | 1.0% |

- **Sends per customer-week:** agent 1.60 and 1.37; agent_v3 1.03 and 0.77.
- **Every journey-drop day** had `never_read` true on every channel offered, and a bid value under the $50 bar.
- **What `blocked_by` alone would show:** 32.3% and 29.4% of v3 customer-days are "nothing" decisions with **no code**.

**Ablations** (value; sends per customer-week in brackets):

| Variant | baseline_shop | whatsapp_fatigue |
|---|---:|---:|
| agent | 2517 (1.60) | 2425 (1.37) |
| agent_v3 | 1768 (1.03) | 1783 (0.77) |
| agent + journeys only | **1852 (1.09)** | **1675 (1.09)** |
| v3 without journeys | 2420 | 2656 |
| v3 without journeys or plans | **2517.14, identical to agent** | 2321 |
| v3 without plans | 1852 | 1725 |
| v3 with never_read off | 1810 | 1432 |
| v3 with never_read off and value bars off | 2246 | 2254 |

What the ablations show:
- **Journeys on their own** cost 26% (baseline_shop) and 31% (whatsapp_fatigue).
- **Fixing `never_read` alone does not help.** WhatsApp stays suppressed: the $50 bar removes the WhatsApp-first journeys, and the covering rule removes the plain WhatsApp messages. WhatsApp sends in baseline_shop: agent 1285, v3 636.
- **Fixing both** (`never_read` off and value bars off) recovers most of the gap.
- **Look-alike, who, moments, offer pricing and experiments** were inert at this size.

## 3. LLM prompts
- **Gym and real bench:** no LLM is reachable on either path, so prompts cannot affect those numbers.
  - The decision engine, the agent, the v3 preset and the Gym contain no LLM client.
  - The bench CI installs no LLM SDK.
  - The bench's ReplayLLM path runs only under `--policy scientist` and is not part of the agent or agent_v3 rows.
- **Production marketing agent:** no LLM either.
  - `api/v2/app.py` builds BrandServices without an llm.
  - `build_runtime` is called with no analyst or strategist, so the weekly LLM cycle never runs.
- **The Data Doctor (live in production):** a long, rule-heavy prompt (29 house rules, at most 3 blocking questions, at most 2 retries). It is not shown to be too strict in a way that hurts outcomes:
  - On a real UCI run, the stops it hit were product rules plus two prompt bugs (`:as_of` instead of `$as_of`, and the lakh rule R13 misreading a product code). All are fixed in HEAD.
  - Its measured gain (silent wrong results 20 to 2 of 43) came from code rules, not prompt wording.
  - Its output does not feed the agent's event store.
  - No over-asking or false-refusal rate has been measured since the fixes.

## Still open
- **The real-bench collapse on Hillstrom and X5** is still undiagnosed. `never_read` cannot fire on the bench, because it strips SEND events from pre-campaign history.
- **The CI artefacts** for run 40325405923 could not be downloaded: the proxy returned 403.
- **Settle it with a per-part ablation** on Hillstrom (pending CI): `python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_v3 --json out.json`, then once per `V3Preset(<part>=False)`, with the gate classifier wrapped around `run_agent`.

## Audit 2: the method (decision technique and evaluation)

# Is the method the problem? Audit of the decision method, evaluation, objective and ML

Read-only audit of branch `claude/beautiful-keller-kacuxh` (HEAD 29382881). Sources:
- docs/v3/STATUS.md, docs/v3/ARCHITECTURE_REVIEW.md, docs/v3/redteam/bench_results.md, docs/v2/REAL_BENCH.md (AFTER tables), docs/v2/SCIENTIST.md.
- The code: autonomy/runtime/agent.py, autonomy/decide/bidders.py, autonomy/decide/policy.py, autonomy/uplift/*, autonomy/gym/truth.py, autonomy/gym/world.py, autonomy/gym/api.py, scripts/v2_realbench/common.py.
- Two tiny probes of my own:
  - `python -I click_probe.py library/hillstrom-email/sample.csv` (6,401-row Hillstrom sample);
  - a one-liner over the Gym's ScenarioSpec defaults.

The CI artifacts holding `blocked_by` could not be downloaded: the blob host returned Forbidden.

## Short answer to "what is the real problem?"

**It is not too many rules, and not strict system prompts.**
- No LLM is in the per-customer decision. Production builds no LLM client for the marketing agent (review section 6, `api/v2/app.py:150-166`).
- On the bench, the AI (Sonnet) layer changed no outcome (SCIENTIST.md).
- The hard compliance rules do not bind on the bench: the plain agent contacts 98.3% on Hillstrom.

**It is mostly the method, and the yardsticks used to judge it.**
1. The decision engine is a per-message bidder: clicks times a lift-per-click, then many fixed multipliers, then Thompson sampling with a 5% floor. That is a tool for high-volume engagement problems. On these shops' data the clicks say who responds, not who is moved.
2. Every real-data gain came from a feature-based uplift model added on top (`autonomy/uplift`). On its own, that model ties the full agent.
3. These datasets contain little usable per-customer variation in uplift at their sizes. Even a perfect method would barely beat send-to-all or a coarse segment rule.
4. v3 adds more fixed multipliers and one-way holds that nothing fits together or checks against the holdout. The result is under-contact.
5. The bench can only test one-shot targeting, and the Gym is circular. Neither can decide the question.

## 1. The decision method

**What the agent computes** (agent.py docstring and `ClickResponseModel`, bidders.py):

`uplift = lift_per_click(segment) x click_rate(segment, action) x personal(customer, action) x fatigue`

This is then blended with the uplift score, and the bid is:

`net = urgency x who x moments x refill x (uplift x value - offer x p_redeem x (1 + shadow) - cost)`

After that come the harm dent, the offer shrink and sensitivity, and the attention price. Then the weekly-budget, frequency-plan and leave-alone holds, the lower-confidence-bound margin, and Thompson sampling with a 5% floor.

**What actually earned the bench results:**

| Dataset | BEFORE the uplift module (click model only) | AFTER (uplift score blended in) | Uplift model alone (Scientist candidate) | Bench T-learner | Best simple policy |
|---|---|---|---|---|---|
| Hillstrom | 0.6584, 97.8% contacted | 0.6513 | not eligible (send-all kept) | 0.6213 | 0.6766 send-all men's |
| X5 | 70.59, **0% contacted** | 72.23 | not picked | 72.63 | 72.33 segment rule |
| Criteo | 0.0317, **97.5% contacted** | 0.0524 (12%) | eligible, +0.0249 over send-nothing | 0.0273 (contacts nobody) | 0.0506 f8 rule |
| MegaFon | 3.6324, **0% contacted** | 4.1958 | **4.1973** | 4.2088 | 3.733 segment rule |

**Reading the table.**
- The agent's distinctive parts (click rates, personal factors, lift per click, the attention auction, Thompson sampling) either failed outright (BEFORE) or were overruled by the campaign-measured score. The blend is designed that way: `uplift/score.py` never lets the response model pull the blend more than one score sd away from the score.
- What is left of the agent's own machinery on a one-shot test is the exploration cost: -0.0253 [-0.0476, -0.0030] on Hillstrom (3.1% women's e-mail, 1.7% left alone).

**Why clicks mislead here.** My probe on the Hillstrom sample (6,401 rows):
- Control visit rate is 0.1117, against 0.1830 for the men's e-mail and 0.1463 for the women's. So **61% / 76% of treated "clicks" would have happened anyway.**
- Extra conversions per treated visit are 0.0385 for men's and 0.0062 for women's: a **6x difference between arms**. One lift-per-click per segment misranks actions unless a randomised campaign rescales each action (`action_scale`).
- The prior is 0.25 extra purchases per click (agent.py:485). Hillstrom measured 0.0144. Without a randomised past campaign, the click model's scale comes from the 0.25 prior, only partly corrected by history (`HISTORY_CLICK_TRUST=0.5`).
- The `personal` factor is learned from treated clicks only. At the person level it is a **response model, not an uplift model**: it favours habitual clickers, who are often sure things.
- Sonnet's own Doctor said the same on Hillstrom: "sent non-clickers buy at 0.0% ... clicks are only a weak, down-weighted hint" (SCIENTIST.md).

**Where the per-customer, click-driven bidder would be the right tool:**
- Very high-volume, high-frequency settings: news, apps, marketplaces with millions of sends a week.
- The click, or something right after it, is itself the goal, or is a validated stand-in for it.
- Many content arms to choose between.

Its best use here is **content choice among people already chosen to receive a message**. There, the organic baseline cancels between arms and clicks rank creatives fairly. It is the wrong tool for send vs not-send and for offer depth in retention marketing at 8k-2M customers, where the outcome is a rare, delayed purchase.

**The ceiling is low.**
- Hillstrom: no heterogeneity validated (slopes -0.21 +/- 0.42 and -0.11 +/- 0.34); the T-learner and segment rule are both below send-all.
- X5: T-learner minus best simple is +0.29 [-0.49, +1.08].
- Criteo: one feature gives +0.019.
- MegaFon: the only strong-signal set, and it is generated data.
- Lenta: no effect. Starbucks: agent 0.0839 vs segment rule 0.0857.

At these sizes, "tie the best simple rule, contact less and discount less" is the realistic win.

## 2. The evaluation method

**The bench** (one-shot randomised campaigns, DR on a held-out half).

It can show:
- one-shot targeting quality;
- arm choice;
- calibration of expected vs measured value per contact.

It cannot show:
- sequential effects: frequency, fatigue, journeys, timing;
- learning from the agent's own logs;
- long-term value;
- the production bugs: currency, WhatsApp clicks, discount codes.

It also charges exploration in full and credits none of what it learns. The DR reward model is a per-arm mean (`common.py score`), so DR here is close to IPS with a control variate. That is fine with known propensities, but the intervals are wide. The T-learner baseline is weak: on Criteo it contacts nobody. So "ties the T-learner" is a low bar.

**The Gym is circular.**
- `truth.py` plants lift as channel x offer x theme x hour x fatigue: the agent's own model form.
- `world.py:715-728` makes click chance = 0.02 + 3 x extra purchases caused. That is exactly the stand-in the click model assumes.
- With the ScenarioSpec defaults, a plain e-mail adds +42% relative purchase rate for 7 days (at_risk +150%). That is 0.0213 extra purchases, about **$0.51 margin per e-mail against a $0.001 cost** (my one-liner).

So the Gym rewards contact volume. Any part that holds people back loses there (v3: -12 to -36%), and the click model's core assumption is true by construction. It is useful as a safety and regression test. It is not evidence of value.

**What would actually decide between methods:**
1. **Semi-synthetic, multi-wave benchmarks from real logs.** Take real X5 receipts or order logs as the organic baseline, plant an uplift that is not multiplicative, tie clicks to organic propensity, use 3-10% lifts and a 30-day net-margin reward. Have someone other than the builder write it.
2. **More randomised datasets that have not been opened.**
3. **A live split in a pilot of 100k+ customers:** current agent vs uplift-threshold inside the agent group, with one shared holdout and pre-registered 30-day net margin. Section 5c's power table gives the sizes needed.

## 3. The objective and signals

- **Clicks as the learning signal:** see section 1. WhatsApp reports no clicks at all (review row 2).
- **7-day reward window** (`DEFAULT_LEARN_HORIZON_DAYS=7`): credits purchases pulled forward. The bench books X5, Criteo and MegaFon purchases at +3.5 days by assumption.
- **Holdout-only identification:** gives the regime effect (agent group vs no contact). The 2nd-4th message in a window gets the average effect, not its marginal one, which pushes toward contacting everyone (B6).
- **Margins:** 0.4, 0.3 and 1.0 are used in different places, and refunds and COD returns are not subtracted (`proof/rewards.py:60`).
- **On the bench these do not explain the results**, because the bench reward is fixed. In a live shop they bias toward contact and discounts.

## 4. The ML

- **Identifiability.**
  - `LiftPerClick` takes the agent group's purchases minus the holdout's (CUPED) and divides by clicks. That identifies an average only. How it is spread over actions and customers comes from the click model's assumptions.
  - The look-alike model's profile includes contact columns that are always 0 for the holdout, so its uplift cannot be identified once the agent is live. A null-effect probe read -2 to -4 points, sd 4-12 (B1).
- **Propensities.**
  - Logged correctly per decision: floor/n plus (1 - floor) x the Monte Carlo share (policy.py).
  - But the agent's own learning does not use them. `observe_send` uses an exposure offset, not IPW; only the warm start uses IPW.
  - Daily decisions with overlapping 7-day windows carry over, so a per-decision propensity does not describe the treatment a reward reflects.
- **Calibration.**
  - Hillstrom went from 11x to 1.46x, and Criteo from 48x to 0.48x. Both came from rescaling with a randomised campaign, not from the click model.
  - With no randomised campaign in history, the scale reverts to the 0.25 prior, 17x Hillstrom's measured 0.0144.
  - No end-to-end calibration gate checks the full stack of multipliers.
- **Feedback loops.**
  - Personal factors steer sends toward clickers. Non-clickers get fewer messages and stay shrunk; v3's `never_read` is the extreme case.
  - More clicks from keen clickers without more purchases lowers the shared lift-per-click for everyone (a plausible coupling, not yet tested).

## Ranked root causes (confidence)

1. **The method does not fit the data (high).** A per-customer, click-driven response model with fixed multipliers is used where a feature-based uplift model on purchases with a cost threshold is the right tool. The real gains came only from the uplift add-on (MegaFon: uplift alone 4.1973 vs agent 4.1958).
2. **Low exploitable heterogeneity at these sizes (high).** Send-to-all or segment rules are near-optimal on the real sets; the one strong-signal set is synthetic.
3. **Evaluation cannot decide (high).** The bench tests only one-shot targeting. The Gym is circular and rewards volume.
4. **v3 stacks unfitted multipliers and one-way holds (medium-high on the mechanism; which gate is unknown).** `blocked_by` is unread.
5. **Weak identification and feedback in live learning (medium).** Regime-only average, no IPW on the agent's own sends, overlapping windows, post-treatment features.
6. **Objective misspecification (medium for live, low for the bench).** 7-day window, assumed margins, refunds not subtracted.
7. **Rules or prompts (refuted as a cause).**

## Recommended method change

1. **Send/no-send and offer:** a cross-fitted DR- or X-learner on purchase margin (start from `autonomy/uplift` `fit_uplift`), then act when score x value - cost > 0, argmax over arms, deterministic. Customer features (leaving risk, recency, moments) are inputs to the model, not multipliers.
2. **Gate:** the Scientist rule. Deploy only if the corrected lower bound beats the best blanket or segment rule; otherwise run that rule.
3. **Learning:** randomised waves (holdout plus a small random-allocation slice), with the model or a doubly robust policy refitted between waves.
4. **Bandit:** only for content choice among people already chosen to receive a message, with a purchase guard.
5. **Rules:** keep the hard caps. Park the attention price, frequency plans and trait leave-alone.

## How to test it cheaply

| Test | What | Prediction | Status |
|---|---|---|---|
| T1 | New bench policy `uplift_threshold` (warm_start_uplift on TRAIN, greedy cost threshold), paired DR vs agent, T-learner and best simple on all 4 datasets | Ties on X5, Criteo, MegaFon; beats the agent on Hillstrom (no exploration cost) | pending CI: `python -m scripts.v2_realbench.run --dataset all --data-dir DIR --json out.json` |
| T2 | Agent ablations: floor 0 and greedy at scoring time; click model detached (uplift score only) | Shows the click model adds about 0 and the Hillstrom loss is exploration | pending CI |
| T3 | Stronger baselines: numpy DR-learner, depth-2 policy tree on DR pseudo-outcomes | Sets a fair bar above the weak T-learner | pending CI |
| T4 | Read `blocked_by` in realbench-{hillstrom,x5}-agent_v3 (run 40325405923), then v3 minus one part at a time | Names the gate behind the 1.5-1.9% contact rate | artifacts exist; download Forbidden here |
| T5 | Semi-synthetic multi-wave benchmark from X5 receipts (non-multiplicative uplift, decoupled clicks, 3-10% lifts, 30-day net margin), 12 weekly waves, agent vs uplift_threshold | Decides the method under realistic, non-circular truth | built, see [docs/v4/WAVES.md](../v4/WAVES.md); pending CI (`[waves]`) |
| T6 | Pilot of 100k+ customers: 50/50 agent vs uplift_threshold inside the agent group, shared 10% holdout, pre-registered 30-day net margin | The real answer | needs a design partner |

