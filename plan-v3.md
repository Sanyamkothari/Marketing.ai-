# Marketing AI v3: a smarter agent

**Status:** plan, written 26 Oct 2026
**Team:** the founder and Claude
**Replaces:** the nine decisions in [`plan-v2.md`](plan-v2.md) section 4. Everything in plan-v2 not changed here still holds.
**Built on:** what v2 taught us on real data ([`docs/v2/REAL_DATA_RESULTS.md`](docs/v2/REAL_DATA_RESULTS.md), [`docs/v2/REAL_BENCH.md`](docs/v2/REAL_BENCH.md)), in the Gym ([`docs/v2/GYM_RESULTS.md`](docs/v2/GYM_RESULTS.md), [`docs/v2/GYM_BAR.md`](docs/v2/GYM_BAR.md)) and in the enterprise verdict ([`docs/research/v2/data/00_VERDICT.md`](docs/research/v2/data/00_VERDICT.md)), and on top of the latest work on `version-2` (21-26 Oct): the Data Doctor's order-log path ([`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`](docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md)) and the AI-service reliability fixes (section 1.1).

---

## 0. The plan on one page

**The problem with v2.** v2 is careful everywhere. That kept it safe, but it also kept it from being smart:

- It knows each customer by a handful of numbers (purchases, spend, top categories, active hours) and learns in **five hand-made buckets** (new, lapsed, light, medium, heavy).
- It learns about each person mostly from that person's own record, and one person's record is almost always too thin. In the Gym it still contacts 95% of the planted sleeping dogs because each one has about one purchase on record.
- On real randomised campaigns it has not yet beaten the best simple rule (it ties on X5 and Criteo, beats on MegaFon, loses on Hillstrom).
- It writes a fixed set of messages, uses only uploaded images, offers only 2-4 discount levels, starts every goal with two weeks of shadow, and decides one message at a time.

**The v3 idea in one line.** **Learn from everyone who looks like this customer, then adjust for the person as their own history grows**, and let the agent run its own experiments so it keeps getting smarter.

**What changes.**

| Area | v2 | v3 |
|---|---|---|
| Knowing the customer | A handful of numbers, five buckets | A rich profile of about 40 numbers per customer, rebuilt nightly, later a learned taste profile |
| Learning | Per bucket, per person | One shared look-alike model; the person's own record overrides it as it grows |
| Who | Uplift by bucket | Uplift by profile, plus leaving risk and 90-day value; sleeping dogs found by traits |
| When | Quiet hours + usual hour | **Moments** (browsed, cart, refill due, price drop, payday, festival) + a look-alike daily rhythm |
| Offer | 2-4 discount tiers | Many kinds of offer, extra profit per offer per person, the whole budget planned at once |
| Channel | Cheapest that works, per message | **Journeys** (email, then WhatsApp if needed), chosen per person |
| Format | Uploaded images only | Generated images through a checker and batch approval |
| Message | Fixed library, learns themes | AI writes per customer type weekly; a scoring model picks; live tests cut losers |
| Frequency | A price per message + caps | Each person's tolerance from look-alikes; plan 3 weeks, re-plan daily |
| Success | Profit vs holdout after 30-90 days | Early-signal model gives a 90-day answer in 1-2 weeks; budget moves to what works |
| Experiments | The Scientist re-scores the past | The Scientist runs live experiments and keeps winners |
| Autonomy | Shadow first for every goal | Autopilot inside limits from day one on email; shadow only where a mistake is costly |

**What does not change.** The holdout, logging the probability of every decision, consent, quiet hours, WhatsApp limits and "LLMs think, maths decides" for the per-customer choice.

**How we know v3 is smarter.** It has to win on the scoreboards we already have, with tougher bars (section 8). "Smarter" that does not beat a simple rule is not smarter.

---

## 1. What v2 taught us

| What we saw | Where | What it means for v3 |
|---|---|---|
| The agent's expected value per message was about 11x the measured value on Hillstrom; the fix wave brought it to 1.46x | REAL_DATA_RESULTS, REAL_BENCH | Every new model must pass a calibration check before it is used live (section 8) |
| Per-person records are too thin: about one purchase per customer in a recency-weighted record | `docs/v2/CLICK_HARM.md` | Learn from look-alikes, not from the person alone |
| Even at enterprise scale, after 260 interactions a per-person policy captures about 6% of the per-person gain | 00_VERDICT | Value comes from **features shared across people**. This is the core of v3 |
| Where customer features carry signal, a T-learner beats blanket sending (MegaFon +0.486 per customer) | REAL_DATA_RESULTS | The profile must reach every decision |
| Learning from clicks lost money; clicks are a weak stand-in for purchases | `docs/v2/SCIENTIST.md` | Reward is always purchases and margin; clicks are at most a hint |
| The attention price started at about two cents, so the agent sent about 1.8 times a week; after the fix it beats the fixed rule in four scenarios | `docs/v2/CONTACT_LESS.md` | Frequency is worth getting right, and a learned tolerance works |
| Send-time optimisation has no independent evidence of large lift | `docs/research/v2/03_DECISION_SCIENCE.md` | Timing by *moments*, not by clever hours |
| Raw LLM subject lines lost (56% of the baseline's clicks at Nextdoor) and won after a reward model was added | `docs/research/v2/00_CTO_BRIEF.md` | AI writing needs a scoring model in front of it |

### 1.1 The latest work we build on (21-26 Oct)

After the Scientist merge, work on `version-2` moved to the Data Doctor, the part that reads a client's files. Two pieces matter for v3.

**1. The order-log path (Phase B, proposed, not yet built).** A raw order log (one row per order line, like UCI Online Retail) is refused today because no column says who came back. The proposal ([`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`](docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md)) builds what v3 needs most:

| Phase B builds | v3 uses it for |
|---|---|
| Asking which column is the shopper (Q1) and what to do with blank IDs (Q2), never guessing | Every profile is keyed on a confirmed customer column; no silent identity errors (REAL_DATA_RESULTS failure 4) |
| Line → order → shopper roll-up; returns read from negative quantity, not an invoice prefix; no number parsing on key columns | The buying, price and returns part of the profile (foundation A) |
| One row per shopper per month-end, using only what happened on or before that date | Point-in-time training rows for the slow models (leaving risk, 90-day value, early signals) |
| Future-window labels with censoring (`build_labels`) and an approved outcome sentence | The labels for leaving risk (decision 1) and 90-day value (decision 8) |
| A lapsed-audience filter on the spine | The win-back audience, defined the same way in training and in the agent |
| Time-based split on `snapshot_date`, and the leak probe on every build | Honest held-out checks for every v3 model |

So v3 does **not** build its own file-to-customer logic. The Data Doctor stays the one front door, and the agent reads what it builds.

**2. AI-service reliability.** `engine/llm_http.py` and the Doctor now say why an AI call failed, keep throttles retried, never resend a completion that reached the service and timed out, count calls that ran out of room, give each role its own output allowance, and run a connection test that sends what real calls send. v3 adds many AI steps (copy per customer type, a vision checker, live experiments). They all go through this layer, each with its own allowance, and each one says why it did not answer.

Also from this week: Indian number formats are read by the Doctor (lakh and crore), with product codes never read as amounts. v3's India-first offers and budgets use those readings.

---

## 2. Principles

1. **Be bold where a mistake can be undone; be strict where it cannot.** A weak message, a poor offer or a wrong send hour costs a little and teaches us something. A send without consent, a burned WhatsApp number or a false claim cannot be undone. Only the second kind gets hard rules.
2. **Trust the crowd until you know the person.** Every estimate starts from customers who look alike and moves toward the person's own record as it grows.
3. **Money, not clicks.** Unchanged from v2.
4. **Log the probability of every decision.** Unchanged. It is what lets the agent prove and improve itself.
5. **The agent experiments on itself.** It always has a question it is testing, small and randomised, measured against the holdout.
6. **Calibrated before it is live.** A model whose promises are not close to what it delivers does not decide anything.
7. **LLMs think, maths decides.** The per-customer choice stays a fast, auditable function. LLMs work per customer *type* (hundreds), never per customer (millions).

---

## 3. Three foundations

Everything in section 4 sits on these. They are built first.

### A. The customer profile

**What.** Each night, turn every customer's history into about 40 numbers, a fingerprint of who they are:

| Group | Examples |
|---|---|
| Buying | Orders in 30 / 90 / 365 days, spend, average order value, margin, months active |
| Trend | Is buying speeding up or slowing down (recent gap vs their usual gap), days since last order vs their usual cycle |
| Taste | Top categories, price level (premium vs budget within the shop), share of repeat items |
| Price | Share of orders with a discount, average discount used, buys at full price after a discount ends |
| Time | Active hours, weekday vs weekend, payday buyer |
| Channels | Per channel: delivered, opened, clicked, replied, last engaged, complaints |
| Responses | Replies ("not now", "stop", questions), complaints, returns |
| Moments | Last view, last cart, refill due date, items viewed but not bought |

**Later: a learned taste profile.** Learn a short list of numbers for every product from which products are bought together and in which order (like a music app learning taste from songs played). A customer's taste is built from their products. "People who buy like Priya" becomes a calculation.

**How.**
- New field on `CustomerState` (`autonomy/contracts/customer.py`): `features: dict[str, float]` plus `features_as_of`, filled by a nightly profile builder.
- The builder reuses the Data Doctor's order-log path (section 1.1): the confirmed customer column, the line → order → shopper roll-up, and v1's point-in-time feature builder (`engine/onboarding/features.py`), so a profile never sees the future. Phase B's retail-win-back feature list (recency, tenure, order count, order value sum/mean/max, distinct products, returns, active months, average gap between orders) is the first slice of the profile.
- The same feature definitions serve the nightly profile and the slow models' training rows, so what a model learned from and what the agent feeds it can never drift apart.
- Every feature is defined once, in one list, with a plain-language description (shown in "why" explanations).
- It runs in batches (streamed), so it does not add to the memory problem at 1M customers (REAL_DATA_RESULTS, failure 11).

**Pass test.**
- Point-in-time: a profile built for date D uses no event after D (a test plants future events).
- Speed: 1M customers profiled in under 15 minutes on a 4-CPU machine, in under 4 GB.
- Signal: on MegaFon, X5 and Criteo, a simple model on the profile predicts purchases better than one on v2's state (held-out AUC).

### B. Look-alike learning

**What.** One shared model learns from all customers: for someone with *this* profile, what happens if we take *this* action (channel, offer, theme, format, moment)? A new customer gets the crowd's answer on day one. As their own record grows, it overrides the crowd.

**Example.** Priya has bought twice. The agent has never seen her react to WhatsApp. It has seen 8,000 customers with a similar profile, and they respond to evening WhatsApp. That is the starting guess. If she ignores three evening messages, her own record takes over.

**How.**
- A new `LookalikeResponseModel` implements the existing `ResponseModel` protocol in `autonomy/decide/models.py` (`predict` gives mean and spread of uplift, `treated_rate`, `update`). It is a drop-in for `BetaResponseModel`, so the policy, the auction and the explanations do not change.
- Inside: a model of purchase given profile and action, fitted on randomised data (holdout, exploration and any past campaign with logged probabilities), with treated and control predicted separately so the difference is the uplift.
- Spread (how sure it is) comes from fitting several versions on resampled data. Thompson sampling uses that spread, so it explores where it is unsure and exploits where it is sure.
- Personal adjustment: each customer carries a small correction that starts at zero and grows with their own evidence, weighted by how much evidence there is. This replaces the five buckets in `Segmenter` (`autonomy/runtime/agent.py`).
- The same pattern feeds the timing model (`ReadingHourModel` already shrinks a person toward a group; the group becomes "people like them"), the tiredness model (`HazardModel`) and the value model.
- Refit nightly. Small shops start from cross-client starting points (`autonomy/priors/pooling.py`, never fewer than 5 clients, no customer data shared).

**Pass test.**
- Real bench: beats v2's response model on all four dev datasets (Hillstrom, X5, Criteo, MegaFon) on held-out profit.
- Calibration: expected value per message within 1.5x of measured on Hillstrom and Criteo.
- Gym: share of the oracle higher than v2's agent in every scenario on the staged bar.

### C. The agent runs its own experiments

**What.** Every week the Scientist picks one question and tests it live. Example: "For lapsing customers, does 15% off or free shipping make more profit?" It splits a small random group, runs the test, reads the result against the holdout and keeps the winner.

**How.**
- The question comes from a fixed typed menu (`autonomy/scientist/menu.py`), never free text from a model.
- Limits: at most 10% of the agent group in experiments at once, at most 2 experiments per goal, and each one stops early if it is clearly losing.
- Results are read with the existing proof code (`autonomy/proof/`) from logged probabilities. The pick rule is written down before the test starts, as the Scientist already does.
- A winner becomes the new default inside the envelope, logged and reversible. The Analyst reports it in the weekly briefing in one line.

**Pass test.**
- Gym: a scenario where one offer is planted as clearly better. The Scientist finds it within 4 weeks on at least 9 of 10 seeds and never promotes the worse one.
- Every live experiment has a pre-registered pick rule in its log before its first send.

---

## 4. The nine decisions

Each decision lists what v3 does, how, what we build on, and the test it must pass.

### 1. Who to contact, and who to leave alone

**What.** Contact people a message actually changes, before they drift away, and spend most on people worth most.

**How.**
- **Uplift by profile** (foundation B): sales if we message them minus sales if we don't.
- **Leaving risk**: the chance this customer stops buying in the next 60 days, from the profile's trend features. High risk + positive uplift = act now.
- **90-day value**: what this customer is likely worth over 90 days. It sets how much an action for them may cost.
- **Sleeping dogs by traits**: a customer is left alone when the look-alike model is confident their uplift is below zero. Example: discount-only buyers who got 5+ messages last month buy less after a message.
- v1's AutoGluon trains the leaving-risk and value models (slow models, weekly) on the month-end rows the Data Doctor's order-log path builds, with its approved outcome sentence, time-based split and leak probe. Their outputs go into `CustomerState.scores`, which already exists.

**Example.** Rahul buys monthly at full price, so his uplift is near zero: leave him alone. Anita's orders are slowing and people like her respond: contact her now.

**Builds on.** `autonomy/uplift/`, `autonomy/decide/bidders.py` (`leave_alone`), v1 AutoGluon.

**Pass test.**
- Real bench: beats the best simple policy on MegaFon and Criteo (corrected lower bound above 0), and at least ties on Hillstrom and X5.
- Gym `sleeping_dogs_15`: contacts under 50% of planted sleeping dogs (v2: 95.2%, `docs/v2/CLICK_HARM.md`), in scenarios where the Gym's `sleeping_dog_signal` makes them observable.

### 2. When to send: moments

**What.** Send when a message is worth most: at a moment, at the person's hour.

**How.**
- **A list of moments**, each raising the value of a message and fading over time (`Urgency` in `bidders.py` already does the fading):

| Moment | Signal | Fades in |
|---|---|---|
| Viewed a product twice | view events | 2 days |
| Left a cart | cart event (`last_cart_at`, REAL_DATA_RESULTS failure 12) | 1-3 days |
| Refill due | `autonomy/catalog/cycle.py` refill model | the refill window |
| Price drop on something viewed | catalogue price change + view | 3 days |
| Back after a long absence | visit after a gap longer than usual | 2 days |
| Payday | 1st and last days of the month, learned per profile | 3 days |
| Festival | a calendar per country (Diwali, Eid, Christmas, sale days) | the festival window |

- **The hour**: the person's own active hours, shrunk toward people like them (foundation B), always inside quiet hours in their time zone.
- Event-driven decisions run within minutes (plan-v2 already runs `decide()` on events).

**Example.** Priya's face cream lasts about 34 days. On day 31, at 8 pm, she gets "Running low?" with no discount, because she buys at full price.

**Builds on.** `autonomy/decide/timing.py`, `catalog/cycle.py`, the runtime's event path.

**Pass test.**
- Gym `send_time_matters`: beats v2's agent.
- A new Gym scenario with planted moments (carts, refills): moment-triggered sends beat the same messages in the daily sweep.
- Real data: on RetailRocket, browse moments predict purchases better than the daily sweep's ranking.
- Time zone bug fixed first (REAL_DATA_RESULTS failure 10).

### 3. Which offer

**What.** The cheapest offer that changes behaviour, with the whole budget planned at once.

**How.**
- **A wider menu of offers**, each with a known cost: % off tiers, flat amount off, free shipping, a bundle (from co-purchase data), loyalty points, early access, a free gift, and no offer.
- The look-alike model predicts **extra profit per offer per person** (uplift on the order and its value, minus the offer's cost).
- **Plan the budget at once**: rank every customer-offer pair by extra profit per unit of offer cost and fill the budget from the top. A "price" on budget makes this work day by day without seeing everyone at once. `autonomy/decide/pacing.py` already paces spend.
- "Sure things" never get a discount: their extra profit from any offer is negative.
- Offers stay within the envelope the marketer approved.

**Example.** Anita responds to free shipping as well as to 20% off. Free shipping costs ₹40, 20% off costs ₹180. She gets free shipping.

**Builds on.** `offer_choice` net-value maths from v1, `pacing.py`, `autonomy/catalog/recommend.py` for bundles, the offer catalogue in `autonomy/mandate/catalogue.py`.

**Pass test.**
- Gym `discount_cannibalisation`: beats the best fixed rule on every seed (v2: some seeds).
- Starbucks promo (sealed): beats the segment rule. It is a sealed dataset, so it is run once at the end and recorded first.

### 4. Channel: journeys

**What.** Choose a short journey per person, judged by the purchase at the end minus every cost.

**How.**
- A few journeys, defined in config:
  - A: email only;
  - B: email, wait 3 days, WhatsApp if no purchase;
  - C: WhatsApp straight away (high value only);
  - D: WhatsApp, then email reminder.
- The agent picks a journey like any other option, with its probability logged. A journey stops the moment the customer buys or replies "stop".
- Reward is the purchase within the journey's window minus all channel and offer costs.
- People who never open email skip email. WhatsApp (about 100x the cost of email) goes where its extra uplift pays for it.

**Builds on.** The dispatcher (`autonomy/channels/`), channel costs and per-channel prices in `autonomy/decide/policy.py`, the follow-up steps already in plan-v2.

**Pass test.**
- Gym `whatsapp_fatigue`: beats v2's agent and the best fixed rule.
- Lower cost per extra order than single messages in the Gym `baseline_shop`.

### 5. Format: generated images

**What.** Images made for the product and the customer type, never shown before a person approves them.

**How.**
1. Take the product photo from the catalogue.
2. An image model makes a banner: a seasonal background, the category's style.
3. **Prices and offer text are drawn by code onto the image**, never by the image model, so numbers are always exact.
4. A vision checker: is it the right product, is the logo intact, any garbled text, are the brand colours right.
5. **Batch approval screen**: 20 thumbnails, approve all or reject single ones.
6. Approved images enter the existing library (`autonomy/assets/library.py`) with `status=approved` and `approved_by`, the one path in that `docs/v2/FORMATS.md` already designed.

Images are made per product x customer type, not per person, so the cost stays small. Each person's hero product comes from the recommender.

**Pass test.**
- The vision checker catches at least 95% of planted bad images (wrong product, broken logo, wrong text) in a fixed eval set.
- Live: an email test of generated image vs the brand's own image vs text only, read against the holdout.

### 6. Message: AI writes, a scoring model picks, live tests decide

**What.** Copy written for each type of customer, pre-scored, then tested.

**How (weekly).**
1. **Customer types**: group customers into 50-300 types from their profiles.
2. **Write**: the Creative agent writes 5-10 drafts per type and goal, with personal slots (name, last product, refill date, points balance).
3. **Check**: the Critic, rules first, then a judge from a different model family (unchanged).
4. **Score**: a scoring model trained on our own past sends predicts each draft's purchase rate for each type, from the draft's theme, tone, offer, length and text. Only the top 3 per type go live.
5. **Test live**: sends are split among the 3; a clear loser is cut early; results go back into the scoring model.

**Cost.** About 300 types x 10 drafts = 3,000 AI writes a week, not millions.

**Real limit.** WhatsApp templates need Meta's approval, so they are submitted in weekly batches.

**Builds on.** `autonomy/agents/creative.py`, `critic.py`, theme labels, the bandit over labels.

**Pass test.**
- Before it is trusted, the scoring model ranks held-out past messages better than chance (and better than theme labels alone).
- Live: its picks beat the v2 library on purchases per send, read against the holdout.
- The Critic still catches at least 95% of planted violations (unchanged bar).

### 7. Frequency: plan weeks, not messages

**What.** Know each person's tolerance and plan ahead.

**How.**
- The tiredness model (`HazardModel`) learns from look-alikes: the risk of an unsubscribe or complaint for this person's next message, given how many they had this week and month.
- Instead of "send now?", compare a few **3-week plans** per customer: one message now; one now and one in 10 days; nothing for 2 weeks then a stronger offer; nothing. Pick the plan with the best total value minus tiredness. Re-plan every day, so the plan bends when something happens (a purchase, a moment, a reply).
- Hard caps stay as a backstop.

**Builds on.** `HazardModel`, `autonomy/decide/pacing.py` (`step_aside`), the attention price.

**Pass test.**
- Gym `null_effect`: loss within the staged bar's allowance (v2: just outside it).
- Gym `whatsapp_fatigue`: unsubscribes no higher than the best fixed rule's.
- Live: unsubscribe and complaint rates in the agent group no higher than in the holdout plus a small margin.

### 8. Measuring success: faster answers, money moves to what works

**What.** Know in 1-2 weeks what used to take 90 days, and move budget to the goal that earns most.

**How.**
- **Early-signal model**: predicts each customer's 90-day margin from first-week signals (second visit, second order, basket size, reply). It is trained on the shop's own history, with labels built by the same future-window label builder as Phase B (`engine/onboarding/labels.py`, censoring included), and checked on past groups of customers before it is used.
- Experiments and the Analyst read early-signal results first, then confirm with the real 30/60/90-day numbers.
- **Auto-budget**: each goal reports its proven extra profit per unit of budget, with ranges. The Analyst moves budget from the weakest goal to the strongest: at most 20% a week, only when ranges do not overlap, inside the envelope, logged and reversible.
- The default objective stays incremental gross profit against the holdout; a person approves the goal (unchanged).

**Pass test.**
- On past data (Online Retail II, CDNOW), the early-signal model's 90-day forecast lands within an agreed error band for at least 80% of monthly customer groups.
- Gym: with two goals of planted different value, auto-budget moves money to the better goal within 4 weeks and never to the worse one.

### 9. Personalisation

**What.** Every decision and every message uses who the customer is.

**How.**
- The profile (foundation A) feeds every model.
- Message slots can use any profile field: favourite category, last product, refill due, points balance, a viewed product.
- **Cross-client starting points from day one** for small and new clients, through the existing pooling (`autonomy/priors/pooling.py`: at least 5 clients, statistics only, each client can turn it off).

**Pass test.** Covered by decisions 1-8: personalisation is worth only what it adds to them.

---

## 5. Autonomy: bolder, with the same safety net

| Level | v2 | v3 |
|---|---|---|
| Start | Shadow for every goal | **Autopilot inside the envelope from day one on email**, because email is cheap and a mistake is small |
| WhatsApp | Same as email | Shadow for one week, then approve each batch for one week, then autopilot. A burned number cannot be undone |
| Experiments | None live | Up to 10% of the agent group (foundation C) |
| Budget moves | A person | The Analyst, within 20% a week (decision 8) |
| Images | Uploaded only | Generated, after batch approval (decision 5) |

**What stays strict (never learned, always on):** consent per channel, quiet hours in the customer's time zone, unsubscribes and open complaints, the holdout, the budget, WhatsApp template and per-user limits, the circuit breakers (complaints over 0.08%, unsubscribes at 3x baseline, delivery under 95%, WhatsApp quality yellow, overspend), and the Critic gate on everything a customer sees. One-click pause and undo.

---

## 6. Honest levels by data size

The smart parts need data. The product says plainly which level a shop is at.

| Customers | What works | What the agent says |
|---|---|---|
| Under 20k | Moments, journeys, frequency limits, offers by broad type; cross-client starting points | "Your shop is small, so I start from what works for similar shops and learn slowly" |
| 20k-500k | Everything in section 4; experiments answer in weeks | Full v3 |
| 500k+ | Everything, faster and finer; the learned taste profile pays off | Full v3, plus warehouse-native running later (00_VERDICT) |

---

## 7. What has to change underneath

| What | Why | Where |
|---|---|---|
| One front door for files: the Data Doctor's order-log path builds the customer column, roll-up and month-end rows; the agent's event mapper reads the confirmed choices instead of guessing | No second, weaker copy of the identity and money rules (REAL_DATA_RESULTS failure 4) | `engine/doctor/order_log.py` (Phase B), `autonomy/bridges/doctor_bridge.py`, `autonomy/data/mapping.py` |
| Every new AI step gets its own output allowance and failure reason | The reliability layer from this week (section 1.1) | `engine/llm_http.py`, `configs/engine.yaml` |
| Stream the daily sweep in batches | 1M customers ran out of memory (REAL_DATA_RESULTS failure 11) | `autonomy/runtime/service.py` |
| Fix the SQLite scan, then test Postgres | The scan slows quadratically | `autonomy/store/sql.py` |
| Profile builder and model store | Foundations A and B | new `autonomy/profile/`, `autonomy/decide/models.py` |
| View, cart and price events reach the state | Moments need them; today views only update "last seen" | `autonomy/data/mapping.py`, `autonomy/contracts/customer.py` |
| A journey as an action | Decision 4 | `autonomy/contracts/decisions.py`, the dispatcher |
| Image generation and vision check behind one interface | Decision 5; the model can be swapped | new `autonomy/assets/generate.py` |

---

## 8. How we check ourselves

Same three scoreboards as v2, with tougher bars. Each new claim goes into [`docs/v2/TARGETS.json`](docs/v2/TARGETS.json) before the work starts, so we cannot move the goalposts afterwards.

| Scoreboard | Command | v3 bar |
|---|---|---|
| **Real bench** | `python -m scripts.v2_realbench.run --dataset all ...` | Beats the best simple policy where features carry signal (MegaFon, Criteo); at least ties everywhere else, Hillstrom included (v2 still fails Hillstrom and Criteo non-inferiority) |
| **Calibration** | the same report | Expected value per message within 1.5x of measured on every dev dataset |
| **Gym** | `python -m autonomy.gym.run ...` (staged bar, `GYM_BAR.md`) | Beats the best fixed rule on every seed of every scenario where messages make money; higher share of the oracle than v2 in every scenario |
| **Sealed data** | `python -m scripts.v2_realbench.sealed ...` | Lenta and Starbucks run once at the end, recorded before the data is read |

**Gym changes.** New scenarios: planted moments (carts, refills), sleeping dogs that share profile traits (on top of today's `sleeping_dog_signal`, which only lets an annoyed customer leave traces in their own history), two goals with different value for auto-budget, and one clearly better offer for the Scientist to find. The staged bar's sleeping-dog line gets stricter: at least 40 points below send-to-all's share, not 10. Every change must be defensible as "real customers behave like this", as `GYM_BAR.md` requires.

**Calibration gate.** No model decides live until its expected value is within 1.5x of measured on held-out data. A model that drifts out of that band is switched back to the last calibrated one, and the Analyst says so.

---

## 9. Build order

Each phase ends with a demo and a check. Claude builds; the founder reviews the demo and makes the calls.

| Phase | What | Demo / check |
|---|---|---|
| **0. The Data Doctor's order-log path** (Phase B, B1-B7) | Already designed in `docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`, behind its own switch. Needs your answers to its 8 open questions first | Its own bar: UCI Online Retail reaches **ready** end to end; the parity files and the M160 benchmark show no change |
| **1. Foundations A + B** | Profile builder, `LookalikeResponseModel`, look-alike timing, tiredness and value; streamed sweep; time zone fix | Real bench and calibration: beats v2's agent on all four dev datasets. Profile pass tests green |
| **2. Who, offer, frequency** (decisions 1, 3, 7) | Leaving risk and 90-day value, sleeping dogs by traits, wider offer menu, whole-budget planner, 3-week plans | Gym: beats the best fixed rule on every seed in `discount_cannibalisation`, `sleeping_dogs_15`, `whatsapp_fatigue`, `null_effect` |
| **3. Experiments, moments, journeys** (foundation C, decisions 2, 4) | Live Scientist, moment list, view/cart/price events, journeys | New Gym scenarios pass; RetailRocket browse-moment check |
| **4. Message and format** (decisions 6, 5) | Customer types, scoring model, live elimination; image generation, vision check, batch approval | Scoring model beats chance on held-out sends; vision checker catches at least 95% of planted bad images |
| **5. Success and budget** (decision 8) | Early-signal model, auto-budget | Early-signal forecasts within band on Online Retail II and CDNOW; auto-budget Gym scenario passes |
| **6. Proof pack** | Re-run every scoreboard, the sealed runs, a one-page results sheet | We can show a company "here is the proof, start on email autopilot" |

---

## 10. Not now (and why)

- **An LLM per customer:** still too slow and costly, and it cannot learn from the holdout. LLMs work per customer type.
- **Deep reinforcement learning for whole lifetimes:** 3-week plans re-planned daily get most of the value and can be checked.
- **Generated video:** after images have a clean record.
- **Full per-person models with no sharing:** the verdict shows they do not work even at enterprise scale.
- **Warehouse-native running (Snowflake, BigQuery):** needed for big companies, after v3 proves itself.

---

## 11. Open questions for the founder

0. The order-log path (phase 0) waits on its own 8 questions in `docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md` section 11. For v3 the important ones are: monthly snapshots (proposed; more rows for the slow models) and which use cases offer the path.
1. Email autopilot from day one: are you comfortable starting there, with WhatsApp staged (section 5)?
2. Which image model for decision 5, and may it run outside the client's account?
3. Experiments: is 10% of the agent group the right ceiling for the first clients?
4. Auto-budget: is 20% a week the right limit, or should a person approve every move at first?
5. The festival and payday calendar: India only first, or India plus one other country?
6. Cross-client starting points: on by default for new clients (they can turn it off), or off by default?
