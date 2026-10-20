# Marketing AI v2: Autonomous Marketing Agent

**Status:** plan for the POC, written 20 Oct 2026
**Team:** the founder and Claude
**Time box:** 6 to 8 weeks to a working proof. Each phase ends with something we can run and show.
**Built on:** the research in [`docs/research/v2/`](docs/research/v2/) (start with `00_CTO_BRIEF.md`) and the v1 engine in `engine/`.

---

## 0. The plan on one page

**What we are building.** A general marketing agent that works for any business. It looks after every customer one to one. For each person it decides whether to contact them at all, and if so when, on which channel, with which offer, in which format and with what message. It sends the message, watches what happens, learns, and proves the profit it *caused* against a group it left alone.

**What we are not building yet.** Pricing, sales material, a partner programme or many integrations. First we prove the thing works.

**How we prove it without clients.** In four ways, all of which must pass:
1. **The Gym.** Simulated customers whose right answers we planted. The agent has to find them, beat simple rules, and never break a rule.
2. **Real public data.** Replay real logged marketing data (Open Bandit Dataset, Hillstrom, Criteo uplift) and show our policy beats the policy that generated the data.
3. **A live loop.** Real emails and WhatsApp messages to a few test phones and inboxes (ours). Events come back, the agent reacts, and the whole loop runs on its own for a week.
4. **The marketer test.** Someone who isn't us types a goal in plain English, approves a plan, and understands the daily briefing without help.

**The shape.**

```
               ┌────────────── OUTER LOOP: LLM agents (think, plan, write, explain) ──────────────┐
  Marketer ──▶ │ Strategist   Data Doctor   Creative ⇄ Critic   Analyst   Conversation            │
  "goal +      └──────┬──────────────┬──────────────┬─────────────────┬──────────────────────────────┘
   budget"            │ envelope     │ clean events │ labelled         │ insights, briefing
                      ▼              ▼              ▼ content library  │
               ┌────────────── INNER LOOP: no LLM, fast maths (decide for every customer) ───────────┐
               │ customer state → goal bidders → attention auction → fatigue price → hard rules      │
               │ → Thompson sampling → action or DO NOTHING (probability logged for every choice)    │
               └──────┬─────────────────────────────────────────────────────────────────────────────┘
                      ▼
               Dispatcher (email, WhatsApp, webhook to the client's own tool, CSV)
                      ▼
               Events back (delivered, clicked, replied, bought, unsubscribed) → contact memory
                      ▼
               Fast learning (nightly) ──▶ inner loop        Slow learning (weekly) ──▶ Strategist
                      ▼
               Proof: agent vs simple baseline vs no-contact holdout, in money, with ranges
```

---

## 1. Principles (few, and we can break them if they get in the way)

1. **LLMs think, maths decides.** Agents plan, write copy and explain. A fast, auditable function makes the per-customer choice. An LLM per customer per day is too slow and expensive, and it can't learn from a holdout.
2. **Do nothing is a real action.** It competes with every message and often wins. Sending less is a feature.
3. **Money, not clicks.** We reward incremental profit. Opens are inflated by Apple Mail Privacy Protection and clicks by bots, so neither is a reward.
4. **Log the probability of every decision.** Without it we can never prove a new policy is better. This costs nothing on day one and can't be added later.
5. **Autonomy is a dial, earned with evidence.** Shadow → approve each batch → autonomous within an envelope → agent proposes goals.
6. **v1 is a library, not a cage.** Reuse what works. Write v2 in a new package with less ceremony.

---

## 2. The agents

Six LLM agents in the outer loop. The orchestrator that runs them is **a plain state machine, not an agent**. Agents never chat freely with each other: each reads and writes typed records in a shared workspace (goals, briefs, content library, decisions, results).

| Agent | Job in one line | Reuses from v1 | Model |
|---|---|---|---|
| **Strategist** | Turns "win back lapsed customers, $5k this month" into a typed **Brief**: objective, audience, allowed offers and channels, budget, caps, stop rules. It picks from fixed lists and never invents numbers. | `engine/agentic/planner.py`, `policy.py`, `claims.py` | Big (Claude) |
| **Data Doctor** | Connects data, cleans it, maps it to our event types (purchase, visit, send, open, click, reply, unsubscribe), links records for the same person by email or phone, and raises an alarm when data drifts. | `engine/doctor/`, `engine/intake/`, `engine/onboarding/` | Big |
| **Creative Director** | Builds a **content library**: message variants per goal and channel, each labelled with theme (savings, quality, urgency, social proof, education, convenience), tone, offer and format. Per-customer details are filled into slots at send time. | `engine/generative/` | Big |
| **Critic** | Checks every piece of content before it enters the library: brand voice, claims, links, channel rules, tone. Rules first, then an LLM judge from a *different* model family. | `generative/guardrails.py`, `compliance/` | Medium, different family |
| **Analyst** | Reads results and turns them into plain English: money made against the holdout, what's working, what isn't, and **new opportunities** it spotted ("1,900 customers are drifting away, want a goal for them?"). Every number comes from code, never from the LLM. | `engine/measurement/`, `pilot/proof.py`, `agentic/claims.py` | Big |
| **Conversation** | Handles replies on WhatsApp and email: "stop", "not now", questions, complaints. It tags intent, updates the customer's preferences, and hands off to a person when needed. Minimal in v2.0. | `engine/agent/loop.py` patterns | Small, fast |

**Not agents (plain code):** the inner loop, AutoML training, the dispatcher, the orchestrator, the scheduler.

**A text → signals slot.** One small, swappable service turns text (replies, tickets, reviews, our own copy) into typed signals such as intent, sentiment, topic and theme. We'll test Jev from TypeSafe against our own embeddings plus a simple classifier and against Claude Haiku with structured output, and keep the winner. It feeds customer state and labels copy so the bandits can learn *which value proposition* works for each person.

**How a goal flows:**
`Marketer goal → Strategist brief → Marketer approves envelope → Creative builds library → Critic approves → inner loop runs daily and on events → Dispatcher sends → events return → nightly learning → weekly Analyst report → Strategist adjusts the brief (within the envelope) → repeat.`

---

## 3. The inner loop: how each customer gets a decision

One function, run in a **daily sweep** for everyone and **within minutes of key events** (a reply, a cart, a lapse signal):

```
decide(customer_state, now) → DO NOTHING | {goal, offer, channel, send_time, format, variant}
                               + probability of that choice + reason
```

**Step 1: Goal bidders.** Each active goal (win-back, cart recovery, cross-sell…) is a *bidder*. A bidder is maths, not an LLM. It estimates the **extra** value of its best action for this customer:

`bid = uplift(action) × value × margin − offer cost − channel cost`

Uplift means the change caused by contacting. A customer who would buy anyway gets a bid near zero, which is the whole point.

**Step 2: The attention auction** (the founder's design, with four tweaks):
- **Attention has a price.** Every message a customer received recently raises the price of the next one, and a bid must beat it. The price comes from learned fatigue: unsubscribe risk × the customer's future value. A hard weekly cap stays as a backstop.
- **"Do nothing" bids too.** The value of rest can win.
- **Value fades.** Cart recovery is worth a lot today and little in 3 days. Bids carry urgency, and losers re-bid next slot instead of being frozen.
- **Honest bidders.** The Analyst compares each goal's predicted bids with realised lift and shrinks the bids of goals that over-promise.

**Step 3: Hard rules** (never learned, always on): consent per channel, quiet hours in the customer's own time zone, unsubscribes, open complaints, holdout membership, budget left.

**Step 4: Explore.** Thompson sampling picks among the near-best options, with a floor (5–10%) so every option keeps getting tried. The probability of the chosen option is logged.

**Step 5: Timing.** The decision includes *when* to send: the best hour for this person if we know it, otherwise their segment's, otherwise the business default. Every hour respects quiet hours.

---

## 4. The nine decisions: v2.0 and later

| # | Decision | v2.0 (POC) | Later |
|---|---|---|---|
| 1 | **Who / leave alone** | v1 uplift models give the bid. Sleeping dogs (people a message puts off) are never contacted. "Sure things" get no discount. | Per-person uplift as data grows |
| 2 | **When** | Quiet hours + the person's active hours from contact memory + segment prior | A learned per-person send-time model |
| 3 | **Offer** | 2–4 offer tiers + no offer, chosen by net value with Thompson sampling, within budget | Uplift on revenue amounts, discount depth learned |
| 4 | **Channel** | The cheapest channel that works; a costly channel only when its extra uplift pays for it | Channel escalation sequences |
| 5 | **Format** | Pick from the client's assets: text vs text + image, chosen like any other option | Generated images; video much later |
| 6 | **Message** | The content library, with bandits over *labels* (theme, tone), so learning transfers across goals | A reward model that pre-scores new drafts |
| 7 | **Frequency** | The attention price + rolling caps + fixed follow-up steps (e.g. day 0 email, day 3 WhatsApp if no response) | Learned sequences |
| 8 | **Outcome** | The Strategist proposes the metric (default: incremental gross profit); the marketer approves | Predicted 90-day value; the Analyst suggests which goal deserves budget |
| 9 | **Personalisation** | Customer state (recency, frequency, spend, categories, stated and learned preferences) feeds bidders and bandits; slot-filled copy | Shared starting priors across clients (section 7) |

---

## 5. Data: contact memory and customer state

This is the missing backbone from v1. It's the first thing we build.

- **Event store.** An append-only Postgres table: `event(id, customer_id, type, channel, ts, decision_id, payload)`. Types: `purchase, visit, view, cart, send, deliver, open, click, reply, unsubscribe, complaint, bounce, block`. Every send carries the `decision_id` that caused it, so results tie back to decisions.
- **Customer state.** One row per customer, updated on every event: last purchase, spend, categories, contacts in the last 7 and 30 days per channel, active hours, channel responsiveness, offer sensitivity, preferences from replies, consent per channel, time zone, holdout flag.
- **Training tables.** v1's point-in-time feature builder (`engine/onboarding/features.py`) reads the event store to train slow models (AutoGluon churn, value, uplift). These become **features and priors** for the inner loop.
- **Identity.** Simple and good enough: match on email and phone. No full customer data platform (CDP).

---

## 6. Channels: sending and listening

**One dispatcher contract, several adapters:**

| Adapter | v2.0? | Notes |
|---|---|---|
| **Email via Amazon SES** | ✅ | About $0.10 per 1,000. Sends from the client's own domain. Events come back through SNS: delivered, bounced, complained, clicked. |
| **WhatsApp Cloud API (Meta)** | ✅ | Approved templates with slots. The client's own number. Webhooks for delivered, read and replied. The test number allows 5 recipients, which is perfect for the POC. |
| **Webhook / CSV to the client's tool** | ✅ | We decide, and their Klaviyo, Braze or HubSpot sends. Named connectors come later. |
| SMS (Twilio), push, ad audiences | Later | Same contract. |

- **Inbound webhooks:** check signatures, drop duplicates, map each event to its `decision_id`, write to the event store, and update customer state.
- **Circuit breakers (on by default):** pause a goal if complaints exceed 0.08%, unsubscribes run at 3× baseline, delivery drops below 95%, WhatsApp quality turns yellow, or spend runs ahead of plan.

---

## 7. Cross-client learning, done in a way that's safe for us

**The idea:** every new client starts smarter because the agent already knows general patterns: typical fatigue curves, typical send-hour shapes, which copy themes tend to work for win-back, sensible starting priors for discount response.

**What is shared:** only **statistics about patterns**. For example, "win-back emails labelled *education* average X% relative lift, across N clients". These become **starting priors**, and the client's own data overrides them as soon as it has enough.

**What is never shared:** customer rows, customer IDs, content, offers, prices, or any number that comes from fewer than 5 clients.

**Why this isn't bad for us:**
- No client can learn another client's strategy. Patterns are pooled across at least 5 clients and stripped of names.
- No personal data leaves a client's space, so it fits standard data-processing contracts.
- Each client can turn it off, and the agent still works, just with a slower start.
- It's a moat: the more clients we have, the better every new one starts.

**For the POC:** we build the prior store and seed it from the Gym and the public datasets. No real client data is involved yet.

---

## 8. How it checks itself

| Layer | What runs | Pass means |
|---|---|---|
| **The Gym** (every build) | Simulated customers over 90+ days with planted truth: offer response, channel preference, best hours, fatigue, sleeping dogs, drift. Scenario library, e.g. "15% sleeping dogs", "discounts cannibalise full-price buyers", "WhatsApp works but tires people fast". | Captures **≥ 80% of the oracle's value**. Beats send-to-all *and* a sensible rule baseline in **≥ 95% of seeds**. Contacts **< 10% of planted sleeping dogs**. **Zero** consent, quiet-hour or cap violations. 95% intervals cover the truth **93–97%** of the time. The breaker trips within 2 batches in planted-harm scenarios. |
| **Public data replay** | Off-policy evaluation (`engine/uplift/ope.py`) on the Open Bandit Dataset (logged probabilities included), Hillstrom (an email offer randomised trial) and Criteo uplift (13.9M rows, scale). | Our policy's estimated value beats the logging policy with an interval above zero. **Honest note:** in v1, uplift didn't beat risk ranking on Hillstrom. If that repeats we report it and fall back by data size. |
| **Agent evals** | Fixed test sets per agent (goal → brief, brief → copy, copy → pass/fail with planted violations). Each runs 5 times to check consistency. Judges are swapped and order-shuffled. | Strategist briefs are valid and stable across 5 runs. The Critic catches ≥ 95% of planted violations. Analyst numbers match the code exactly. |
| **Live loop** | Real sends to our own test inboxes and phones for 7 days. | Every send has a `decision_id`; every event lands; the agent reacts to replies within minutes; "stop" is honoured immediately; nothing violates quiet hours. |
| **Live proof** (once a real company tries it) | Shadow mode for 2 weeks, then **80% agent / 10% simple baseline / 10% no-contact holdout**. | Incremental profit vs holdout, and lift vs baseline, with ranges, at 30, 60 and 90 days. |

**Daily health page** (what a marketer sees): money made vs holdout · agent vs old way · who we left alone and why · customer health (unsubscribes, complaints, blocks) · what it's learning · alerts and pauses · receipts (tap any action, see why).

---

## 9. What the marketer sees

Five screens:

1. **Connect:** files or a database. The Data Doctor maps them to events (reused from v1).
2. **Goal:** "What do you want?" in plain English, plus a budget. The Strategist answers with a plan.
3. **Plan:** the envelope in plain words: who, which offers, which channels, how often at most, the budget, the stop rules, and sample messages. Choose the autonomy level: *Shadow / Approve each batch / Autopilot*.
4. **Live:** a daily briefing and activity feed ("Sent Priya a refill reminder, no discount, because she always buys at full price"). An inbox for questions the agent wasn't sure about. One-click pause.
5. **Proof:** money vs holdout, lift vs baseline, with ranges, ready to share.

**Software and service together:** an **Operator mode** in the same app, with extra tools (edit briefs, replay decisions, force an exploration change, run the Gym on this client's data). It lets us act as the "growth engineer" for early companies, and it's the same product, not a separate service.

---

## 10. Reuse vs build

| Keep from v1 (import as a library) | Build new |
|---|---|
| Data Doctor, intake, onboarding features | Event store, customer state, inbound webhooks |
| AutoGluon training, uplift learners (as priors) | `decide()`: bidders, auction, attention price, Thompson sampling, propensity log |
| `offer_choice` net-value maths, contact gate | Dispatcher + SES + WhatsApp Cloud API adapters |
| Holdout service, OPE, measurement, harm alarm, Proof Pack | The Gym (customers over time), grown from `measurement/simulate.py` |
| Guardrails, compliance checks, claims verifier, ledger, policy | Goal / envelope Strategist, content library with labels, Analyst briefing |
| LLM layer (`llm.py`, `llm_http.py`), MCP server | Reply handler, text → signals slot, cross-client prior store |
| | Durable job queue (Postgres-based), five new UI screens |

**Where the code goes:** a new top-level package `autonomy/`, next to `engine/` and `api/`. It imports v1 modules freely, has its own lighter tests, and doesn't follow v1's DEC/milestone process.

---

## 11. Not now (and why). We revisit when the POC works.

- **Pricing, sales material, partner onboarding:** not until it works.
- **Real-time under one second:** minutes is plenty for marketing.
- **A full CDP or identity graph:** email and phone matching is enough.
- **Many channel connectors:** two that work end to end beat ten that half work.
- **An LLM per customer, or deep reinforcement learning:** too expensive, hard to prove, and bandits get most of the value.
- **Generated video:** asset choice first, generated images later.
- **LLM-simulated customers to prove lift:** fine for testing copy, not for proving money.

---

## 12. Build order

> **We build this in parallel:** see [`docs/v2/PARALLEL_BUILD.md`](docs/v2/PARALLEL_BUILD.md). Wave 0 freezes the contracts, then six workflows build the streams at once. The table below is the logical order. The parallel plan takes about 1.5–2 weeks instead of 6–8.

Each phase ends with a **demo** and a **check**. Claude writes the code; the founder reviews the demo and makes the calls.

| Phase | Time | What gets built | Demo / check at the end |
|---|---|---|---|
| **1. Backbone** | Days 1–3 | `autonomy/` package; contracts (Event, CustomerState, Goal, Brief, Envelope, Decision, Action); event store and state tables; Postgres job queue; test harness | Load Hillstrom and the online-retail data as events, rebuild customer state, and query any customer's timeline |
| **2. The Gym** | Days 4–7 | Customers over time with planted truth; scenario library; baselines (send-to-all, simple rules, oracle); scoreboard | A scoreboard showing baselines vs oracle across 4 scenarios |
| **3. Inner loop** | Week 2 | `decide()`: bidders, attention auction, attention price, do-nothing, hard rules, Thompson sampling, timing, probability logging; nightly learning | **Gym pass criteria met** on the scenario library |
| **4. Proof engine** | Week 3 | Three-way split, the Analyst's numbers (reusing measurement), OPE on Open Bandit, Hillstrom and Criteo; cross-client prior store seeded from the Gym and public data | Replay report: our policy vs the logging policy, with intervals |
| **5. Agents** | Week 4 | Strategist (goal → brief), Creative + Critic (labelled library), Analyst briefing, Data Doctor event mapping, Conversation (minimal); text → signals slot; agent eval sets | Type a goal, get a brief, library and first briefing, all running on Gym customers. Agent evals green |
| **6. Real sending** | Week 5 | Dispatcher, SES and WhatsApp adapters, inbound webhooks, circuit breakers, webhook/CSV adapter | **Live loop: 7 days on our own phones and inboxes** with zero rule breaks |
| **7. Marketer app** | Week 6 | The five screens, autonomy dial, activity feed with "why", pause, Operator mode | **Marketer test:** someone else sets a goal and reads the briefing unaided |
| **8. POC pack** | Weeks 7–8 | Hardening, demo script, one-page POC results (Gym + replay + live loop), a short deck for the first companies we approach | We can show a company "here's the proof, try it in shadow mode" |

**Environment note:** this cloud environment blocks most outside hosts. Real SES and WhatsApp calls in phase 6 need those hosts allowed in the environment's network settings, or a local run.

---

## 13. Open questions we decide along the way

1. Which LLM for each agent? Default: Claude for the Strategist, Creative and Analyst, a smaller model for Conversation, and a different family as the Critic's judge.
2. UI stack: keep v1's plain JavaScript for speed, or rebuild the five screens in a framework? Default: plain JS, decided at phase 7.
3. Which test business story for the demo? Default: an online retailer, using the online-retail dataset, since everyone understands it.
4. Does the Gym also get a "fit to this client's history" mode? Useful for sales, but it must not leak the answer we're testing for.
