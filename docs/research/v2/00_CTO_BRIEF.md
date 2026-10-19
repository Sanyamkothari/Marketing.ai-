# 00 · CTO Brief: what v2 should be

*2026-10-19. This brief pulls together reports 01-07 in this folder. Most market numbers are vendor-reported; the reports flag the weak ones.*

## 1. The one-paragraph answer

v2 should be a **neutral, profit-first decision brain for mid-market B2C teams** (50k-5M customers), launched in India first, where WhatsApp leads. The marketer sets a goal and a budget. For each customer, the agent decides whether to contact them or leave them alone, and if so with which offer, channel, time and format. It sends through the customer's own email domain and WhatsApp number, or writes its decisions into the tool they already use. It then proves the profit it *caused* against a permanent random holdout. Braze ($325M for OfferFit) and MoEngage (Aampe) proved the demand, but what they sell is enterprise-priced or locked to one sender, and every vendor marks its own homework. We win on a combination nobody offers: **honest holdout-verified profit, competence on small data, and mid-market price and setup speed**, built on v1's measurement stack.

## 2. What we learned

- **The category is consolidating.** Braze bought OfferFit (closed Jun 2025) and MoEngage bought Aampe (Jun 2026). Hightouch is the last big independent and needs a data warehouse. Braze's self-serve Decisioning Studio Go (Sep 2026) is email-only, optimises clicks only, and works inside Braze only.
- **Control before autonomy.** StackAdapt (Aug 2026, n=500): 89% are fine with AI preparing actions for approval, 78% with AI acting inside rules they set, and only 50% with full autonomy even after proof. Epsilon found only 2% run agents with no human involved. Brand risk is the top blocker (63%).
- **Proof decides purchases.** BCG (Jul 2026) found that 20-40% of live next-best-action programmes have marginal or negative lift when properly tested. Incrementality tests are the most trusted method (60% vs 40% for MMM).
- **Honest lifts are small.** Duolingo's bandit gave +0.5% DAU, Nextdoor +1% sessions, and DoorDash matched order lift at about half the cost. Vendors claim 10-450%. Promise a measured 3-10%.
- **The money is in *who* and *how much discount*, and in sending less.** Booking.com and DoorDash show uplift targeting with discount depth works. LinkedIn cut email 40-50% and complaints fell by half or more. Send-time optimisation has no independent evidence of large lift.
- **Raw LLM copy loses.** Nextdoor's raw ChatGPT subject lines got 56% of the baseline's clicks and won only after a reward model was added. Bandits should learn over labels (theme, tone, offer type), not over individual strings.
- **Scale limits the method.** Detecting a 1-point lift on a 5% base rate needs about 8k customers per arm. Below about 50k customers, trust only average and coarse segment effects.
- **Channels differ about 100x in cost and in rules.** Email via SES costs about $0.0001 per message. WhatsApp marketing in India costs about ₹1.02 with GST, needs approved templates, and runs into a hidden per-user cap that counts messages from all businesses (error 131049). Opens (about half are Apple MPP) and clicks (20-87% bots) are unusable as rewards; use margin against the holdout.
- **India mid-market has no decisioning layer.** WhatsApp BSPs (from ₹999/mo) only broadcast. MoEngage and WebEngage cost INR 4-15 lakh a year and are moving upmarket.
- **v1 has the parts but not the backbone.** Measurement, holdout, off-policy evaluation, uplift and Data Doctor can all be reused. There is no event store, no sending, no webhooks and no bandit.

## 3. Decisions I recommend

| Decision | My call | Why | Conf. |
|---|---|---|---|
| Target customer | Mid-market B2C, 50k-5M customers (D2C, apps, fintech, subscriptions), India first. Publish a minimum-size rule; below it, sell measured rules, not AI | Enough data to learn, a CRM owner under ROI pressure, 1-3 month sales cycles, priced out of OfferFit | High |
| Send or plug in | **Both, behind one dispatcher contract.** Native: we drive SES and the WhatsApp Cloud API on the customer's own domain and number, with no message markup. Plug-in: we write decisions to their platform profile and their flow sends. CSV stays as a fallback | Indian mid-market often has no engagement platform. Customer-owned sending accounts keep trust | High |
| First channels | **Email + WhatsApp** plus a generic profile/webhook adapter. Ad-audience suppression in v2.1, push through partner platforms later, SMS last | An India launch needs both | Med-high |
| Agent roster | 6 LLM roles (below). The orchestrator is a state machine, not an agent | The industry has settled on LLMs planning and maths deciding, with one writer at a time | High |
| Inner loop | No LLM. `decide(state, now)` maximises E[uplift × margin] − offer cost − channel cost − fatigue cost, with "do nothing" as an option. Thompson sampling, a 5-10% exploration floor, the probability of every decision logged, hard rules for consent, quiet hours and caps | An LLM per customer costs about $3k/day at 1M customers, cannot learn from the holdout, and cannot be audited | High |
| Real-time vs batch | Near-real-time: the same `decide()` runs in a daily sweep and on events (lapse signal, cart, reply), within minutes. A Postgres event store to start | Sub-second is unnecessary at mid-market volume | Med-high |
| First goal | **Win back lapsing customers within a budget** | Uplift, "leave alone" and discount depth pay most here, and v1 already has the win-back pieces | High |
| Autonomy | Set per goal, earned by evidence: **L0 shadow** (decide and log only) → **L1 approve each batch** → **L2 autonomous inside an approved envelope** (budget, caps, channels, content) → L3 later (agent proposes goals and budget moves). One-click pause and undo | Follows the 89/78/50 trust curve, and doubles as a sales path | High |
| Proving value | Default split: **80% agent / 10% simple baseline or the client's current approach / 10% no-contact holdout**. Report incremental profit with confidence ranges at 30, 60 and 90 days; the client can export group assignments. A Gym with known right answers serves as CI gate and sales demo | Agent vs holdout proves marketing works. Agent vs baseline proves our fee | High |
| Pricing | Platform fee by customer band, messages at cost, and an optional **capped success fee on holdout-verified profit**, with the holdout agreed up front | Buyers know per-contact pricing. An outcome fee only holds up with a pre-agreed comparison group | Medium |

**Agent roster**

| Agent | Job | LLM? |
|---|---|---|
| Strategist | Turns goal + budget into a typed Brief (what to optimise, audience, options, constraints, stop rules), choosing only from fixed lists | Yes |
| Data Doctor | Onboarding, mapping data to events, linking identities, drift alarms (already exists) | Yes |
| Creative Director | Builds a labelled content library and WhatsApp templates; per-customer details are filled into slots at send time | Yes |
| Brand & Compliance Critic | Approval gate with its own context: rule checks first, then a judge | Yes |
| Analyst (Econometric + Opportunity Scout merged) | Read-only. Plain-English ROI vs holdout, and weekly proposals for new goals. Every number comes from code | Yes |
| Conversation (minimal) | WhatsApp and email replies: opt-outs, "not now", tagging intent, handing off to a person | Yes (small) |
| Inner loop, AutoML, dispatcher, orchestrator | Decide, train, send, sequence | **No** |

**Disagreements resolved.** *Channels:* 05 says email first, 01/02 say WhatsApp first for India; India-first means both. *Conversation agent:* 04 adds it, 07 defers it; WhatsApp replies arrive immediately, so v2.0 gets a minimal reply handler and full conversation waits for v2.1. *Own sender:* 02 says don't become an email or WhatsApp provider, 05 says send natively; both hold, because we drive SES and the Cloud API on the customer's accounts.

## 4. The 9 decisions

| # | v2.0 | Later |
|---|---|---|
| 1 Who / leave alone | v1 uplift models by segment + exclude "sleeping dogs" (people a message puts off) + budget optimiser | Per-person uplift once data allows |
| 2 Time | Quiet hours, best hour per segment, the customer's last active hour; on WhatsApp, let Meta time delivery | Per-person send-time model |
| 3 Offer | 2-4 discount tiers + no discount; pick the highest net value; Thompson sampling within budget | Uplift on amounts; dynamic discount depth |
| 4 Channel | Cheapest consented channel that responds; costly channel only when the extra uplift pays for it | Channel as a learned choice; escalation sequences |
| 5 Format | Choose among the customer's existing assets (text vs text + image) | Generated images; video much later |
| 6 Message | LLM writes the library → Critic → bandit over labels, with learning shared across campaigns | A reward model pre-scores drafts |
| 7 Frequency | Fatigue cost per send (unsubscribe risk × customer value), rolling caps, aware of 131049 blocks; fixed follow-up templates | Per-person caps; reinforcement learning for sequences |
| 8 Objective | Strategist proposes (default: incremental gross margin with guardrails); a person approves | Predict 90-day value from early signals; Analyst advises which goal deserves budget (no competitor does this) |
| 9 Personalisation | Recency, frequency, spend, category interests and stated preferences feed the bandit; slot-filled copy | Starting priors shared across clients |

## 5. Reuse vs build

**Keep from v1** (imported as libraries into a new v2 package): Data Doctor, measurement, holdout, off-policy evaluation, `offer_choice` maths, uplift learners (as priors), AutoGluon for slow models, guardrails and compliance gate, policy engine, ledger, claim checker, LLM layer, MCP server, `simulate.py`.

**Build new** (L = 1-2 months, M = 1-3 weeks): event store, contact memory and webhooks (L, first); dispatcher with SES and WhatsApp (L); `decide()` with bandit, channel cost and fatigue (L); durable job queue (M); Gym simulating customers over time (M-L); envelope Strategist and daily briefing (M); template workflow and reply handler (M); goal-based UI (L).

## 6. Biggest risks and how we de-risk them

1. **Incumbents bundle "good enough" decisioning.** Counter with sender neutrality, profit (not clicks), auditable proof, India pricing, value in under 2 weeks.
2. **On small data the AI doesn't beat rules.** Choose methods by data size and say which level applies; pool learning across campaigns; the baseline group reports honestly when we lose.
3. **One bad send burns the list or WhatsApp number.** Envelopes, Critic gate, template library, auto-pause (complaints ≥0.08%, WhatsApp quality yellow, delivery <95%, overspend); a person approves restarts.
4. **Holdout cost and attribution politics.** Agree the holdout before launch, show revenue at risk up front, sell to the founder or CFO.
5. **Slow time to value.** Shadow mode on history in week 1; files or read-only DB; CSV before named integrations.
6. **Meta changes WhatsApp rules.** Client owns the account; rate cards and constraints live in config; always more than one channel.
7. **The backbone takes longer than planned.** v2.0 is a thin slice: one goal, two channels, L0-L2.

## 7. Questions only the founder can answer

1. India first (WhatsApp + email, native sending) or global Klaviyo/Braze teams first (adapters)? I recommend India.
2. Who are the first 5-10 design partners, and will they accept a holdout?
3. Pure software, or software plus a "growth engineer" service for early clients (as Netcore and Hightouch do)?
4. How much commercial risk do you want in outcome-based fees?
5. What are the team size and target date for v2.0?
6. May we share anonymised learning across clients so new clients don't start from zero?

## Sources

The seven underlying reports, each with its own URLs: `01_COMPETITORS.md`, `02_BUYERS_AND_MARKET.md`, `03_DECISION_SCIENCE.md`, `04_AGENT_ARCHITECTURE.md`, `05_CHANNELS_AND_EXECUTION.md`, `06_MEASUREMENT_AND_SELF_CHECK.md`, `07_CODEBASE_REUSE_MAP.md`.
