# Marketing.ai v3: final architecture review

**For:** the founder. **Reviewed:** branch `claude/beautiful-keller-kacuxh`, commit `3af057a7`, plus six red-team lenses (A-F), an independent skeptic's verdict on every serious finding, the first real-data bench run (CI 40325405923) and a senior CMO's buyer evaluation. **Date:** 26 Oct 2026.

**How to read the evidence.** Finding ids such as `A3` or `E2` refer to the red-team files in [`redteam/`](redteam/) (`data_stats.json`, `ai_arch.json`, `market_evidence.json`; the real-data numbers are in `redteam/bench_results.md`, the buyer view in [CMO_REVIEW.md](CMO_REVIEW.md)). Where the skeptic changed a finding's severity, I use the skeptic's severity. Where the skeptic refuted part of a finding, I drop that part or say so. I checked the eight most decision-relevant findings in the code myself; those are marked **[checked]**. The Gym run (CI 40325406058) had not finished when I wrote this, so nothing here depends on it.

**A few terms used throughout.**
- *Holdout*: a random 10% of customers the agent never contacts. Comparing them with everyone else is how the product proves it caused profit.
- *Uplift*: the extra buying a message causes, over what the customer would have done anyway.
- *MDE (minimum detectable effect)*: the smallest lift a test of a given size can reliably tell apart from zero.
- *Calibration*: whether the profit the agent expects matches the profit it actually makes.
- *Gym*: the in-house simulator, where the team chooses the "true" customer behaviour.
- *Real bench*: replaying the agent on four public, randomised marketing campaigns (Hillstrom, X5, Criteo, MegaFon).
- *v2 / "plain agent"*: the decision engine before v3. *v3*: the new parts, switched on together by `AUTONOMY_V3=1`.

---

## 1. Verdict in five sentences

1. The v2 foundation is genuinely good, and it is the asset: an event store, a maths-based decision engine that records the odds of every choice, hard safety rules, and holdout-based proof that honestly reports "not sure yet". On real randomised data the plain agent now ties the best simple rule on three datasets and beats a segment rule on MegaFon.
2. v3 is not a working upgrade. It is about 13,000 lines of carefully tested library code. Five of its headline items (live experiments, AI copy per customer type, generated images, early signals, auto-budget) are not connected to the running product at all. The seven parts that are connected rest on fixed, never-learned multipliers, on a look-alike model that cannot separate cause from effect once the agent is live (B1), and on memory that is wiped on every deploy or content approval (D2).
3. On its first real-data run, v3 changed nothing on Criteo and MegaFon, because every customer looks identical to its new parts there. On Hillstrom and X5 it nearly stopped contacting anyone (1.9% and 1.5% of customers) and lost clearly to the plain agent.
4. The "AI" in the marketing agent is mostly not live. Production sends rule-checked template copy, uses a keyword-based strategist and has no LLM analyst or reply reader. Separately, several production gaps make the India and WhatsApp pitch unready whether or not v3 is on: money constants written in dollars, WhatsApp valued by clicks it never reports, no Meta template or quality-rating handling, discount codes never matched to offers, and Autopilot unlocked by the agent's own forecasts.
5. The right call is to simplify. Keep v3 off, sell v2 plus measurement plus a few proven rules to email-first pilots of 100k or more customers, fix the production gaps below, and bring each v3 part back on its own switch only when it beats the plain agent on evidence registered in advance.

---

## 2. What the real-data results say

**Source:** `bench_results.md` (CI 40325405923). The score is doubly-robust profit per customer on the held-out TEST half; doubly-robust means the estimate stays fair even though the agent's choices differ from what the original campaign did.

| Dataset | Plain agent | v3 | v3 minus agent | v3 contacted | Best simple policy |
|---|---:|---:|---:|---:|---|
| Criteo | 0.0524 | 0.0524 | 0 (identical) | 11.9% | segment rule 0.0506 (agent ties) |
| MegaFon | 4.196 | 4.196 | 0 (identical) | 46.1% | segment rule 3.733 (agent beats); T-learner 4.209 (agent ties) |
| Hillstrom | 0.651 [0.50-0.80] | 0.285 [0.19-0.38] | **-0.367, intervals do not overlap** | 1.9% | send-all men's email 0.677 (agent slightly below) |
| X5 | 72.23 [71.14-73.32] | 70.76 [69.66-71.86] | **-1.47** (intervals overlap slightly; no paired interval reported) | 1.5% | segment rule 72.33, T-learner 72.63 (agent ties) |

**What this means in plain words.**

- **v3 adds nothing where it "should have won".** The plan's real-data bar was "beat the best simple policy where customer features carry signal (MegaFon, Criteo)" (plan-v3.md section 8). On both, v3's numbers are identical to the plain agent's, down to the confidence interval. The cause is structural (F1, A7; skeptic: confirmed for the v3 parts). The bench gives each Criteo and MegaFon customer only a sign-up and a consent event. The real customer features arrive as `preferences`, which the v3 profile and look-alike model never read, apart from one key (`no_discounts`). So every customer's 52-number profile is identical, and the look-alike, leaving-risk and trait models see no differences between people. The skeptic's correction matters here: the v2 uplift model *does* read those features, and it still runs under v3. That is why v3 is not worse there, and why MegaFon's "yes" verdict for agent_v3 is really v2's result.
- **Where v3's parts do have inputs, they suppress contact and lose.** Hillstrom has purchase history (made up by the loader from recency columns, F1), and X5 has real receipts. On both, v3 contacted almost no one. On Hillstrom the best policy is to email everyone, so anything that holds people back loses heavily (-56%). On X5 v3 falls below every simple policy.
- **The exact cause of the Hillstrom/X5 collapse is not yet diagnosed, and I will not invent one.** One thing is ruled out. The journeys' "never read" rule (A2) cannot be the cause on the bench, because the bench strips SEND events from pre-campaign history (`scripts/v2_realbench/adapters.py:163`), and that rule needs 5 sends. The likely candidates are the v3 parts that can only ever *hold back* a customer. The 3-week frequency plan "can only hold, never release" (FREQUENCY.md:124). On a bench that makes one decision per customer, a one-day hold means the customer is never contacted. Other candidates are the trait-based sleeping-dog leave-alone rule and the tiredness "attention price". The bench already records the `blocked_by` codes for every decision (`common.py:455-466`), so reading them is a small first task. The never_read rule belongs to the same family of failure: **v3 parts that silently remove contact with no alert**. In production it would add a second such brake (A2).
- **The bench cannot see two of the production bugs.** It passes per-dataset channel costs (`common.py:432`, `costs=dict(inp.costs)`), while production never does (A3/E1). It also makes one decision per customer, so journeys, moments and frequency plans are mostly dormant there (PRESET.md "What the preset does not fix"). A green bench would therefore not have cleared v3 for production either.
- **The honest headline for the plain agent:** it ties the best simple rule on X5 and Criteo, ties the T-learner and beats the segment rule on MegaFon, and is slightly below send-to-all on Hillstrom. Its value-per-message overestimate fell from 11x to 1.46x on Hillstrom (REAL_DATA_RESULTS.md). That is real progress, but "ties a simple rule" is the current truth. It is not yet "beats your marketer".

---

## 3. What is genuinely sound and worth keeping

These are confirmed across lenses and by the skeptic. This is the foundation to build on.

1. **The per-customer decision has no LLM in it.** It is auditable maths: expected uplift times margin, minus offer and channel cost, with "do nothing" always an option. Every decision logs its exact probability, and a 5% exploration floor (a small share of random choices so the agent keeps learning) is counted correctly (`policy.py:188-234`; lens C sound, lens B sound).
2. **Proof by holdout.** The holdout and baseline groups are split by hash, and rewards are intent-to-treat (counted for everyone assigned, including sends that never arrived) with the same cutoff for every group (`proof/rewards.py:20-58`). The proof shows ranges and says "not sure yet" instead of claiming a win. The buyer persona singled this out as the most valuable thing in the product.
3. **Hard rules that are never learned.** Consent defaults to false (`contracts/customer.py:15-30`). Quiet hours, caps, the holdout and the envelope are enforced in code. SES sends one-click unsubscribe headers (`channels/ses.py:192,269-271`). The complaint breaker trips at 0.08%. WhatsApp opt-out error 131050 withdraws consent (`whatsapp.py:51-53`). There is an India DLT/NCPR compliance pack.
4. **Point-in-time discipline.** The profile and the slow models (leaving risk, 90-day value) never use information from after the decision date. The slow models switch themselves off on noise: they refused random data (AUC 0.51) and passed real-looking orders-only data (AUC 0.83 on a 2,000-customer synthetic shop).
5. **Operational robustness.** A failure for one customer is logged and alerted without stopping the sweep (`service.py:1993-2032`). The job queue claims safely (`SELECT ... SKIP LOCKED`). Journeys replay from the decision log after a restart. The sweep runs in batches and a 300k-customer sweep completed.
6. **LLM roles are well boxed where they exist.** The Strategist and Scientist answer only from fixed lists, with code fallbacks. In recorded real-model runs, code caught an invented p=0.048, and the judge matched the code rule 4 of 4 times. The Analyst always has a code-written briefing to fall back on. The Data Doctor (onboarding AI) has client consent, token and call caps, and an egress gate.
7. **The Data Doctor's messy-file fixes are a measured win.** Silent wrong results fell from 20 to 2 of 43 real-row cases (REAL_DATA_RESULTS.md, fix wave).
8. **v3 being off by default is verified.** A red-teamer ran the preset's fingerprint on the pre-v3 base and on HEAD and got the same output (F, sound), so the v3 merges did not change the default agent's decisions.
9. **Statistics that are correct, just underpowered.** The always-valid experiment intervals and the conservative copy-cut rule are sound (B sound). The problem is sample size, not maths.
10. **Candid sub-docs.** PRESET.md, LOOKALIKE.md, IMAGES.md and COPY.md state their own gaps openly. The overclaim is concentrated in the STATUS.md "Built" table and a few plan lines (C10, F3).

---

## 4. Assumptions that were wrong or overclaimed

Deduplicated across all six lenses. Severity is the skeptic's corrected one. "Latent" means the code is wrong but the feature is not switched on in production today, so it does no harm yet.

| # | Assumption we made | What is actually true | Evidence | Severity |
|---|---|---|---|---|
| 1 | Costs and value bars are "in the shop's currency", so WhatsApp (about 100x email) is used only where it pays | Production never passes channel costs. Every money constant is dollar-sized and read as rupees: WhatsApp ₹0.03 instead of about ₹0.8-1.0 (about 30x too cheap), complaint ₹2, "high value" bar ₹50. The spend ledger and overspend breaker use the same table, so the budget the marketer signs is not enforced on WhatsApp | **[checked]** `runtime/settings.py:370-395` (no costs passed; docstring "keeps its own default channel costs"); `decide/bidders.py:150-160`; `contracts/goals.py:57` currency "USD"; `configs/v3/journeys.yaml:38,44`; A3, E1, D8, F8 | **Blocker** |
| 2 | Uplift can be learned from clicks on every channel | The WhatsApp Cloud API never reports clicks. Live WhatsApp sends count as clickable sends that never get a click, so WhatsApp's predicted value decays toward zero (skeptic probe: 0.05 → 0.0005 after 2,000 sends). The Gym hides this by planting clicks on every channel | **[checked]** `channels/whatsapp.py:45-50` (sent/delivered/read/failed only); `runtime/agent.py:497,2314-2316` (no-click channels only set from 200+ historical sends); E2 | **Blocker** |
| 3 | WhatsApp hard limits (templates, quality rating, per-user caps) are "never learned, always on" | No code submits or syncs templates with Meta. Generated template names are ones Meta has never approved, and this already applies to v2's library, so sends fail at Meta. Quality-rating and template-status webhooks are dropped, so the quality breaker can never trip. Meta's per-user cap error (131049) counts as a delivery failure and can pause the whole channel in festival weeks | **[checked]** `whatsapp.py:251` (`field != "messages"` is skipped); no producer of `quality_rating`; no `131049` anywhere; C8, E3 | **Blocker** |
| 4 | Offer spend is charged to the goal that sent the offer, so the budget limits discounts | Shopify puts the typed discount code (e.g. `COMEBACK10`) in `offer_id`, and the agent looks for its own id (e.g. `pct10`). Redemptions are never charged, so the offer price falls to 0. The v2 money rule (SpendLedger) matches the same way, so **v2 is affected too**. The Gym passes only because it writes the agent's own id onto purchases | **[checked]** `connectors/shopify/mapping.py:151`; `runtime/agent.py:2859-2868`; `gym/world.py:523,679`; D1 | **Blocker** |
| 5 | v3's learning (look-alike crowd, sleeping-dog verdicts, offer price, tiredness, experiments) builds up over time | It all lives in process memory. The policy is rebuilt on every restart, deploy, approval of any content, or change to budget, caps, channels or offers. After a rebuild the crowd is empty and frozen sleeping dogs are re-keyed as "light" and messaged again. The skeptic adds that the offer price can jump to its ceiling for about 2 weeks after a rebuild | **[checked]** `runtime/service.py:3044-3054` (cache key = brief + every approved content id), `:303-305` (envelope is "material"); LOOKALIKE.md:72-77; A4, D2, E10, F2, D11 | **Blocker** |
| 6 | `AUTONOMY_V3` suits small to mid-size shops | The "who" book keeps every raw event of every customer per goal, at about 1.7 KB each. At 8k customers that is about 1.2-1.4 GB per goal and growing without limit. At the 50k-5M target it is multiple to tens of GB, and slow-model fits block the single worker | `decide/who.py:334,351-360`; `service.py:2807-2857`; D3, A12 | **Blocker** for the target market; marginal at 8k |
| 7 | The look-alike crowd learns true uplift (contacted vs holdout) from the profile | The profile includes contact-history columns (`contacts_7d/30d`, delivered, idle) that are always 0 for the holdout. Once the agent is live, uplift becomes unidentifiable: a null-effect probe gave -2 to -4 points with a standard deviation of 4-12 points. Honest effects are 0.1-0.5 points. In practice the blend gives the crowd near-zero weight, so the agent quietly runs on the v2 click model while v3 "look-alike learning" is reported as on | **[checked]** `decide/lookalike.py:78-79` (contacts columns in the profile); B1. Related: the 40k-row cap keeps evidence about 2.7k windows whatever the shop size (B2, major); the per-person response shift absorbs organic buying (B7, major) | **Blocker** |
| 8 | STATUS "Built" = in the product when `AUTONOMY_V3=1` | Experiments, copy per type, images, early signals and auto-budget have no caller in the runtime. The only `ExperimentBook.start` is in the Gym runner. The v3 UI is mock JSON only. plan-v3's "1.5x calibration gate", "separate-family judge" and "all AI steps through llm_http" do not exist in code | **[checked]** `experiments/gymrun.py:228` is the only start; `autonomy.content` is imported only in a docstring (`agents/creative.py`); `early_signal`/`budget` are used only by analyst/briefing; no `ImageServices(` outside its module; D4, E8, F3, C10, B9 | Major |
| 9 | It is an "AI marketing agent": AI writes copy, a weekly AI plan adjusts, an LLM judge reviews, an LLM reads replies | The production build constructs no LLM client for the marketing agent. Copy is about 20 fixed template sentences approved on rules alone. The strategist is keyword-based. The analyst, reply classifier and judge are off. (Skeptic: v1 routes do run LLM campaign copy and summaries, and the Data Doctor is live) | **[checked]** `api/v2/app.py:150-166` (BrandServices built without an llm); C1 | Major |
| 10 | Real-data bench and Gym can prove v3 | The bench cannot show v3's parts on 2 of 4 datasets (row 7 in section 2). Gym truths are planted by the same streams, in the agent's own multiplicative form, at +67% to +350% lift against an honest 3-10%. Only 4 v3 claims were pre-registered, all "mean gain > 0" with no interval. The planted refill truth was edited after a look, in the same commit that recomputed the pre-registration hash. Starbucks, the "sealed" test set, was already opened | B4, F1, F4, F5 (skeptic: a re-run on 3 seeds showed v3 sent fewer messages to everyone, not fewer to sleeping dogs, and lost value on all 3) | Major |
| 11 | Small and mid shops can measure their lift, and the agent "learns something every few weeks at 20k+" from its own experiments | With a 10% holdout, an 8k shop can detect about 46% relative lift per window (about 21% pooled over 6 months), 50k about 18%, and 300k about 7%. Experiment arms are worse: at 8k about 90-135% of profit per customer; at 300k about 14-23%. Copy cuts need about 5,000 or more sends per draft, while an 8k shop gets 300 types of about 27 people | E4 (skeptic: major, pooled figure 21%), A5, B9, F6, A6, C7, E9; my arithmetic in section 5 | Major |
| 12 | Moments and leaving-risk urgency are "starting priors the model learns", and "never turn a losing message into a winner" | They are fixed constants that no code learns. They multiply the whole bid value (up to 2.5 x 1.5 = 3.75x), but the attention price (the agent's internal "cost of someone's attention" that a send must beat) is not scaled, so they *do* push borderline messages over the line. With country IN, every customer is boosted on 121 of 365 days. Expected profit figures are inflated by the same factor, and no calibration gate exists | **[checked]** `decide/who.py:79-80`; `decide/bidders.py:1719-1721,1793`; B3, A8, B10, E6, F9, A13 | Major |
| 13 | Turning journeys on "takes no channel away" | `never_read` counts only clicks and replies, never purchases. After 5 agent emails with no click, email is dropped for that customer, single messages included, and the flag can never clear (no sends means no clicks). In practice this is an aggressive, purchase-blind sunset of most non-clickers, with no alert. (Skeptic: not on day 1, and imported Klaviyo history does not count) | **[checked]** `decide/journeys.py:338-349,413-414,424-429` (the comment on 422-423 says the opposite of what the code does); `configs/v3/journeys.yaml:16`; A2 | Major |
| 14 | Sleeping dogs (customers a message puts off) are found reliably by traits | The harm reading compares a customer's buying in weeks after a message with their other weeks, but the agent chooses when to send. Moments send just before likely purchases, which hides real harm (stylised probe: a true 0.6 reads as 22). Urgency sends to people already lapsing, which creates false dogs. For an orders-only shop the groups split only on how much the agent itself messaged them. (Skeptic refuted F7's "answers_less can never occur": the cut is set from the data) | A9, B5, E12, F7 (minor after skeptic) | Major |
| 15 | Proof measures incremental *profit* | Refunds are not subtracted (`proof/rewards.py:60`, "Refunds are not subtracted yet"), even though Shopify voids arrive as REFUND events. There is no handling of cash on delivery (COD) or return-to-origin (RTO, a COD parcel the customer refuses). With no margin data, three different margin assumptions are in use (0.4, 0.3 and 1.0) | **[checked]** `proof/rewards.py:60`; A10, E7, F11 | Major |
| 16 | Autopilot is earned by proof | From Shadow, the "pays" check sums the agent's *own* forecasts, the part that was 10x optimistic on Hillstrom. Real lift is checked only in Approve mode. A signed mandate can auto-promote with no click. DECISIONS V-1 (email autopilot from day one, WhatsApp staged) has no code | **[checked]** `runtime/promotion.py:397-398` (`pays` from forecasts), `:413-420` (real lift only `if level is APPROVE`); E5 | Major |
| 17 | Early signals give "a 90-day answer in 1-2 weeks" | The model is trained on untreated history. A nudge that only moves purchases earlier was credited ₹103 per customer of 90-day value it never earned, and still passed its own backtest. Not wired today | A1 (skeptic: major, latent), F10 | Major (latent) |
| 18 | The product tells the shop which smart parts are actually running ("Your shop is small, so I start from...") | No such message exists. Every fallback is silent: look-alike not ready, slow model refused, moments dormant, channel dropped as never-read | A11 | Major |
| 19 | The Data Doctor's order-log path is "one front door" that feeds the agent | It builds a v1 AutoML table with no link to the agent's event store. The CSV-to-events route returns events without storing them. Only Shopify and Klaviyo can feed the agent, which locks out WooCommerce, Magento, custom and CSV-only Indian shops | D5, A14 | Major |
| 20 | Opt-outs are caught at once by a hard keyword net | The net is English plus a few European words. "band karo", "mat bhejo", "बंद करो", "nahi chahiye" and "dnd" are missed. With the default setup these only raise an alert, with no contact hold, so marketing continues until someone reads the inbox. (Skeptic: WhatsApp's built-in stop button is honoured) | C6 | Major |
| 21 | The LLM safety checks are as strong as the docs say | Latent until an LLM is wired: in the zero-touch path a judge outage approves LLM copy on rules alone, and the rules miss "flat 500 off", BOGO, "ends tonight" and "only 3 left" (C2). The Analyst number check ignores the sign, so a loss can be written as a gain (C4). The Strategist can drop a channel permanently by leaving it off a list, acting on noise (C9). Copy types label one-time buyers "repeat buyer" (C3) | C2, C3, C4, C9 (all major, latent) | Major (latent) |
| 22 | Each extra message is priced at its own marginal value | Holdout comparisons measure the effect of being in a contact *regime*. The 2nd-4th message in a window is credited with an average, not a marginal, effect, which pushes toward "send to everyone" (v2's 94-97% contact failure) | B6 | Major |
| 23 | Engagement features (open and click rates) describe real behaviour | Klaviyo history arrives as DELIVER, never SEND, so every imported rate feature is 0 (skeptic finding, worse than the original claim). Scanner clicks set "ever engaged". No bot or Apple-privacy filtering exists | D7 (major), B11 (minor) | Major |
| 24 | v3 parts can be switched on one at a time to find what helps | Production has one all-or-nothing switch, and `AUTONOMY_LOOKALIKE_MODEL=false` is ignored when v3 is on. There are about 68 code-only settings. The "explain this decision" engine builds a plain v2 agent, so its explanations disagree with live v3 | D9 | Major |

**Dropped or downgraded after the skeptic:** D6 (two workers / API "cold brain": the API cannot tick in production, so minor); E11 (a 20% hold-out is a sales objection, not a defect; shares are configurable, so minor); C5 (the Data Doctor number check is openly documented as a weak arithmetic filter, so minor); B8 and B11 (minor); F7's core mechanical claim (refuted); B4's "refills do not buy organically" (refuted); D3's "API holds a second copy" (refuted).

---

## 5. Minimal data: what the product really needs

### 5a. To run at all

| Need | Why | Today |
|---|---|---|
| Events in the agent's store: orders, consent per channel, sends and deliveries | The agent reads only its event store | Only through the Shopify or Klaviyo connector. A CSV upload is mapped but **not stored** (D5) |
| A goal brief with budget, envelope (allowed channels, offers, caps) and approved content | The decision engine chooses only within these | Works |
| Channel costs and value bars in the shop's own currency | Profit maths and budget enforcement | **Cannot be set**: dollar defaults are hard-wired (row 1) |
| For WhatsApp: Meta-approved templates whose names match what the agent sends, plus quality and cap webhooks | Otherwise sends fail at Meta, or the number is burned | **Not built** (row 3) |
| One worker process | Policies and state live in that process | Undocumented; only a guard is missing (D6, minor) |

### 5b. To switch on each smart part

| Part | Minimum data | 8k-customer, orders-only Shopify + Klaviyo shop | 300k shop, 3 years, app events, CRM team |
|---|---|---|---|
| Plain v2 decision engine | Orders plus sends; better with one past randomised campaign | Runs. Value estimates are noisy; learning comes mostly from cross-client starting points | Runs; value estimate is measurable |
| Profile (52 features) | Events in the store; nightly job | Works; about 15-32 of 52 features stay at defaults; ignores CRM attributes | Works; ignores CRM attributes; memory risk |
| Leaving risk / 90-day value | 2 or more closed month-ends, 150 rows, AUC ≥ 0.60 | Works (probe AUC 0.83), but on recency alone, and the 1 + 0.5 x risk multiplier is not learned | Works; memory risk |
| Look-alike crowd | 150 contacted + 60 holdout closed 7-day windows, with no rebuild in between | About 2-4 weeks to the gate, then wiped by the next content approval or deploy. Uplift unidentifiable (B1) | Same: the cap holds about 1 day of data at 300k |
| Sleeping dogs by traits | 20+ per trait group; sends with purchases around them | Groups collapse to "how much we messaged them" | Runs; confounded by its own targeting |
| Moments | View/cart events, exactly one country in `AUTONOMY_COUNTRIES`, 3+ purchase days for buying rhythm | Only payday and festivals (fixed boosts for everyone) plus rhythm; no cart, view or price-drop moments | Cart and view work if mapped; price drop has no producer |
| Journeys (email then WhatsApp) | WhatsApp consent plus approved templates; value bar in shop currency; a non-click way to count WhatsApp | Breaks: never-read sunset, ₹50 bar, WhatsApp undervalued (no clicks) and underpriced (₹0.03) | Same |
| Offer pricing / budget planner | Discount code mapped to offer; offer variety in history | Redemptions never charged (D1) | Same |
| Frequency plans / tiredness | Hundreds of unsubscribes or complaints (switches on at 20) | About 16 unsubscribes a week, so it fits noise; reset on rebuild | Fits; reset on rebuild; blind to Gmail complaints and WhatsApp blocks |
| Live experiments | Runtime wiring and persistence (both missing), then power | Not wired. If wired, needs about 90-135% relative difference, so never a winner | Not wired. If wired, about 14-23% relative |
| Copy per type | An LLM client (not configurable), wiring, power | Not wired. 300 types of 27 people; never cuts | Not wired. 333 per draft per week; still no cuts |
| Early signals / auto-budget | Wiring, plus validation on randomised history | Not wired; pull-forward bias | Not wired; same bias |
| Images | A real image and vision model | Fakes only | Fakes only |

### 5c. To prove profit (what the holdout can see)

Honest lifts in this industry are 3-10% (CTO brief). The table below is for "did the customer buy", 80% power, 10% holdout against an 80% agent group. Profit per customer is noisier, so treat these numbers as best case.

| Customers | Smallest detectable lift, one 2-4 week window (5% buy rate) | Pooled over about 6 months (20% buy rate) |
|---|---:|---:|
| 8k | 46% | 21% |
| 50k | 18% | 8% |
| 100k | 13% | 6% |
| 300k | 7% | 3% |

Customers needed to see a **10%** lift: about 170k for one window, or about 35k pooled over 6 months. For a **5%** lift: about 670k for one window, or about 140k pooled. So an 8k shop **cannot buy a success fee**. A 50-100k shop can get a verdict only by pooling months. A shop of 300k or more can read real lifts within a quarter. This is why the CMO's pilot floor of 100k or more customers is right.

### 5d. Edge cases that silently go wrong (no alert today)

1. A rupee shop: WhatsApp is priced about 30x too cheap and the budget report undercounts the Meta/BSP bill (row 1).
2. Non-clickers lose email permanently once journeys are on (row 13).
3. WhatsApp quietly stops being chosen, because it never reports clicks (row 2).
4. The WhatsApp quality rating drops to yellow or red and the breaker cannot see it. 131049 cap errors in Diwali week pause the channel as a "delivery problem" (row 3).
5. Discount spend is never charged against the budget (row 4).
6. Every deploy or content approval resets v3 learning and re-contacts frozen sleeping dogs (row 5).
7. "band karo" does not unsubscribe (row 20).
8. Returned COD orders count as profit (row 15).
9. No margin in the file: three different margin assumptions are used across models (F11).
10. The India festival calendar ends after 2027; the 2027 Raksha Bandhan date is listed although its own comment says none was found (D8, E6).
11. A CSV-only shop: the Data Doctor runs, but the agent never receives the events (row 19).
12. Memory grows with every send until the worker crashes, and the restart replays the full history (row 6).
13. Imported Klaviyo engagement makes every rate feature 0 (row 23).

---

## 6. Do the AI agents really have impact?

There are two very different things called "agent" here. Keep them apart.

**A. The statistical decision engine (no LLM).** This is where every measurable effect lives.

| Piece | Live? | Measurable impact so far |
|---|---|---|
| v2 decision engine (uplift x margin - costs, Thompson sampling, which picks options in proportion to the chance each is best; holdout proof) | Yes | **Yes, modest.** It ties the best simple rule on 3 real datasets and beats a segment rule on MegaFon. Calibration 11x → 1.46x on Hillstrom. Not yet better than "a careful marketer's rule" |
| v3 decision parts (profile, look-alike, who, moments, journeys, frequency, offer pricing) | Yes, with `AUTONOMY_V3=1` | **Negative or zero** on the first real run (section 2). The Gym smoke run also lost value on 3 of 3 seeds (F5 skeptic) |
| v3 experiments, early signals, auto-budget | No | None possible today. Underpowered below about 300k even if wired |

**B. The LLM roles.**

| Role | In production today | Can it have measurable impact? |
|---|---|---|
| Data Doctor (onboarding, mapping files) | **Live** (plus v1 generative routes) | Yes, indirectly: fewer silent mapping errors (20 → 2 of 43). Its number check is a weak filter (C5, minor) |
| Creative (writes copy) | **Not live**: no LLM client, so fixed template copy (C1) | Copy lifts are small. Picking a better draft needs thousands of sends per draft (about 17,700 to separate 1.0% from 1.5%, B9). **Not measurable below roughly 300k customers per goal** |
| Critic / judge (approves copy) | Rules only; judge is None | Safety, not lift. Its value is avoided harm, which is hard to measure; its gaps are real (C2) |
| Strategist (weekly plan changes) | Keyword template in production; the LLM version runs in the demo only | Acts on weekly reads that are almost all "not clear yet" at mid-market size (C9). Impact cannot be measured below about 300k |
| Analyst (weekly briefing) | Off; a code template is used | A reporting role, so no lift to measure. Its risk is a mis-stated number (C4) |
| Reply reader (Conversation) | Off; a person reads replies, plus an English keyword net | Compliance value, not lift. A Hindi lexicon matters more than an LLM here (C6) |
| Scientist roles (choose and judge experiments) | Typed and tested with a real model, but experiments are never started in production | Only at enterprise scale, where experiments have power (00_VERDICT.md) |
| Images (vision checker) | Fakes; "95% caught" is 100% by construction | Unknown until a real model is evaluated on real bad images |

**Bottom line:** the only AI with demonstrated impact is the non-LLM decision engine (v2) and the Data Doctor's onboarding help. None of the LLM marketing roles is live, and at shops under about 300k customers per goal most of them *could not* show measurable lift even if switched on. Their honest value is speed and convenience for the marketer, and their main risk is brand and compliance, not lift. Sell them that way, or not at all.

---

## 7. Built vs wired vs proven: every v3 component

"Wired" means it changes what production does when `AUTONOMY_V3=1`. "Proven" means it showed value on real data it could actually see. Not one v3 component is proven (F, summary).

| Component | Built | Wired into production | Proven on real data | Action |
|---|---|---|---|---|
| Phase 0: Data Doctor order-log path | Yes | No: no link to the agent's event store (D5) | Works for the v1 AutoML table only | **Fix**: make it, and CSV uploads, write events into the store (it is the non-Shopify front door) |
| Profile (52 features, nightly) | Yes | Yes | No; invisible on 2 of 4 bench sets (F1) | **Keep** as a data and reporting layer; add static CRM attributes; keep it out of decisions until proven |
| Look-alike learning | Yes | Yes, but wiped on rebuild | No; unidentifiable live (B1) | **Park**. Return only after the contact columns are removed, state is persisted and a real-data win is shown |
| Live experiments | Yes (library) | No (empty book; only the Gym starts one) | No; bar never run | **Park**. For large clients, run marketer-approved fixed-horizon tests instead |
| Who: leaving risk and 90-day value models | Yes | Yes | Synthetic only | **Keep the models** for reporting and audience building; **remove** the fixed 1 + 0.5 x risk urgency (set to 0) |
| Who: sleeping dogs by traits | Yes | Yes | No; confounded (B5, E12) | **Park**. Read harm only from the randomised holdout |
| Moments (cart, refill, payday, festival) | Yes | Yes (fixed multipliers) | No; smoke gain was negative (-5.24) | **Park** as multipliers. Offer the festival calendar as a marketer-chosen rule instead |
| Offers and whole-budget planner | Yes | Yes | No; broken on Shopify codes (D1) | **Fix D1 first** (it hurts v2 too), then park the planner |
| Journeys A-D | Yes | Yes | No; no journey-level credit | **Park**. Fix `never_read` and the value bar before any return |
| Frequency (tiredness, 3-week plans) | Yes | Yes | No; a candidate cause of the bench collapse | **Park**. Keep v2's hard caps |
| Copy per type, scoring, live elimination | Yes (library) | No | No; impossible at 8k | **Remove from roadmap**. Replace with 3-6 human-approved message families with AI-filled slots |
| Generated images | Yes (fakes) | No | No | **Park** |
| Early signals | Yes (library) | No | No; pull-forward bias (A1) | **Remove** from any decision or money use; at most a "leading indicator" after validation on randomised history |
| Auto-budget | Yes (library) | No | No; two-goal scenario never built | **Park** |
| Cross-client priors (pre-existing) | Yes | Yes | n/a (needs at least 5 clients) | **Keep** |
| Batched sweep | Yes | Yes | 300k completed | **Keep** |
| CI_GREEN fixes | Yes | Yes | Yes (CI) | **Keep** |
| Preset switch (`AUTONOMY_V3`) | Yes | Yes, all-or-nothing | n/a | **Fix**: one environment flag per part, all off |
| 1.5x calibration gate (plan) | **No** | No | No | **Build** (see section 8) |

---

## 8. Recommended path

### Stop now
- **Stop building new v3 features.** The plan's next phases would add more unwired, unproven parts on a base that has not yet beaten a simple rule.
- **Turn `AUTONOMY_V3` off for every pilot**, and say so in writing.
- **Stop using Gym-planted wins as evidence.** The Gym stays as a safety and regression test. It is not proof of value.
- **Stop customer-facing claims** of "AI writes your copy", "runs its own experiments", "90-day answer in 1-2 weeks", "email autopilot from day one", and any "paid in results" offer until pricing and pilot proof exist (CMO).

### Fix first (in order)

Sizes: S = days, M = 1-3 weeks, L = 1-2 months.

1. **Tell the truth in the product and docs (S).** Split STATUS.md into "built", "wired" and "proven" columns. Strip unearned numbers from the demo (the CMO's list: 111.6x ROI on a simulated shop with no "simulated" label, spend mismatch, a hardcoded judge verdict on dishonest copy, C12). Enforce the editor gate on the server. Add a weekly "what is actually on" briefing section (A11). *Why:* trust is the product's core differentiator, and today the top-level docs and demo overclaim.
2. **Money in the shop's currency (S).** Make channel costs, the complaint cost and value bars required per-shop settings, read from the mandate catalogue's real provider prices (`mandate/catalogue.py`), and refuse to start without them. *Why:* this is the India launch blocker, the fix is cheap, and the plumbing exists.
3. **Proof that means profit (S-M).** Net refunds and voids within the window. Add one shop-level contribution margin (after shipping, gateway and RTO), asked at onboarding. Show the MDE next to every proof number and pool windows. *Why:* the proof page is the thing the buyer pays for.
4. **Promotion gated by real evidence (S-M), and build the 1.5x calibration gate (M).** No Autopilot without an Approve phase judged against the holdout. Shrink forecasts by the measured calibration ratio. Never word a self-graded forecast as money the agent "would have made". *Why:* the current gate trusts the number that was 10x wrong.
5. **Offer code to offer mapping (M).** Add a code or prefix to each offer, or generate per-send codes, and test through the real Shopify mapping. *Why:* without it, neither v2 nor v3 controls discount spend.
6. **WhatsApp production readiness (M-L), before any WhatsApp pilot:**
   - Meta template sync, with the Critic run on the approved body;
   - quality and template-status webhooks;
   - 131049 mapped to "capped, retry later";
   - WhatsApp valued per delivered message or reply, not per click (S on its own);
   - a Hindi, Hinglish and Devanagari opt-out lexicon, plus a contact hold on unreviewed replies (S).

   *Why:* a burned number cannot be undone (DECISIONS V-1).
7. **File and CSV import into the event store (M).** *Why:* most of the Indian mid-market is not on Shopify.
8. **Diagnose the bench loss (S).** Read the `blocked_by` counts from the agent_v3 run before any further v3 work.
9. **Pre-register before reading (S).** Before the pending Gym results are read, register agent_v3 claims with intervals in TARGETS.json, with the hash recomputed by someone other than the builder. Declare Starbucks spent, and choose a new unopened dataset.
10. **Integration depth (L, CMO).** Read and write integration with MoEngage, CleverTap, WebEngage and Braze (decisions written into their profiles, their sends counted against our caps, one shared holdout). *Why:* most target buyers already run one of these.
11. **Enterprise and India hygiene (M, in parallel, not code-heavy):** a data processing agreement, DPDP processor posture, a residency option, pseudonymised IDs, a SOC 2 plan, and a published price list (CMO).

### Park (keep the code, keep it off, no investment)
Look-alike, sleeping dogs by traits, moments as multipliers, journeys, frequency plans, offer planner, experiments, images, auto-budget. Early signals and per-type copy are removed from the roadmap in their current form.

### What to sell now
A **measurement-first retention decision service**, in line with the CMO's "sell the honesty now and earn the autonomy":
- **Entry product:** a read-only data audit plus Shadow on the shop's own history (weeks 0-3), then **Approve-each-batch** for one **win-back goal, email first**.
- **Three groups:** agent / the client's own business-as-usual / 10% no-contact. The comparison is against what the client already does, not a made-up rule (E11).
- **Who:** an Indian D2C or consumer brand with 100k-2M repeat-purchase customers, discount-heavy win-back, clean opt-ins, a founder or CFO sponsor and no data-science team. Not for shops under 50k, enterprises on SFMC or Adobe, or telcos and banks.
- **The promise:** incremental gross margin per eligible customer with an honest range; or a tie while cutting messages by 30% or more and discounts by 20% or more. "Send less, discount less, prove it" is the claim the evidence can support.
- **Below about 50k customers:** sell measured rules (who to leave alone by holdout evidence, discount depth caps, frequency caps) and the proof report, not "AI" (CTO brief, section 3).
- **WhatsApp:** only after fix 6.
- **Commercial reality:** in the early pilots this is effectively a managed service. Price it that way.

### Evidence that would justify turning each v3 part back on
Use the same rule for every part:
1. It gets its own flag.
2. A claim with an interval is registered *before* the run.
3. It does no harm in adversarial Gym scenarios written by someone other than its builder, with realistic 3-10% lifts, organic-heavy moments and at-risk customers with the *lowest* lift.
4. It beats or ties the plain agent on real data it can actually see.
5. Then it wins a live split inside the agent group (plain vs plain + part) at a pilot of 100k or more customers, with a lower bound above 0, or no harm plus a stated cost or message saving.

| Part | Extra conditions before re-enabling |
|---|---|
| Look-alike | Post-treatment columns removed; state persisted; a null-effect test shows standard deviation below the business-relevant effect; wins on X5 or a semi-synthetic order log |
| Who urgency | A risk x contact interaction learned against the holdout, credibly positive |
| Sleeping dogs by traits | Harm read only from randomised contrasts; zero false flags in a scenario where sends follow propensity |
| Moments | A moment-on/off arm read against the holdout, or a learned moment coefficient |
| Journeys | Journey-level credit wired; purchase-aware never-read rule; WhatsApp fixes in place |
| Frequency plans | Bench `blocked_by` shows it is not the cause of the collapse; fewer unsubscribes than the holdout at equal profit |
| Offer planner | D1 fixed; a planted-budget test on the production path |
| Experiments | A power check before start that refuses tests whose MDE exceeds about 20%; only for shops over about 300k |
| Copy | Family-level win at 300k or more first; scoring model trusted *before* live use; a separate-model judge that fails closed |
| Early signals | Validated as a surrogate on past randomised campaigns (treated-minus-control forecast matches the realised 90-day difference) |
| Auto-budget | Non-overlapping per-goal holdouts; always-valid intervals |

---

## 9. Open questions only the founder can answer

1. **Minimum shop size.** Will you publish and enforce a floor (I recommend 100k for the AI product, and rules-plus-proof below 50k), knowing it shrinks the addressable Indian mid-market?
2. **First design partner.** Who is it, will they accept a live 10% no-contact group during festival season, and is there a founder or CFO sponsor?
3. **Email-first or WhatsApp-first.** WhatsApp needs fix 6 (M-L) before any pilot. Do you launch email-first in India, or delay the launch?
4. **Software or managed service.** Early pilots need hands-on onboarding. Are you willing to price and staff it as a service for the first 3-5 clients?
5. **Standalone sender or layer on top of incumbents.** Do you compete with MoEngage, CleverTap and WebEngage, or sit on top of them as the independent measurement-and-decision layer (00_VERDICT.md's enterprise thesis)? That choice decides whether fix 10 or native sending comes first.
6. **v3's future.** Are you willing to treat v3 as parked research, and to remove copy-per-type and early signals from the roadmap, given that the per-person "brain" fails even at enterprise scale (00_VERDICT.md)?
7. **Commercial model.** Platform fee only, or a capped success fee? A success fee is computable only at 100k+ customers, or with months of pooling.
8. **Team and vendor risk.** The team is the founder plus Claude, and v3 was built in a day. What review capacity (a second human, an external statistician) will you add before a paying pilot?
9. **Cross-client learning.** Will clients agree to share pooled statistics (at least 5 clients) so new shops do not start cold, and how does that sit with your DPDP posture?
