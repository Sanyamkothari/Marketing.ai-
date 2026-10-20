# Autonomy: Engineering Overview

An autonomous marketing agent that decides, for every customer, every day, whether to act and how
(offer, channel, time, format, message), executes it, learns from the outcome, and proves the
incremental profit against a randomised holdout.

**Scale today:** about 32k lines of Python in `autonomy/`, 1.8k in `api/v2/`, a no-build JS app in
`ui/v2/`, about 960 fast tests, plus the v1 engine (`engine/`, about 190k lines) reused as a library.

---

## 1. The shape of the system

```
                         OUTER LOOP: LLM agents (plan, write, check, explain)
 Marketer goal ──▶ Strategist ──▶ Brief + Envelope (the signed mandate) ──▶ approve once
                   Creative ⇄ Critic ──▶ labelled content library (theme, tone, offer, format)
                   Analyst ──▶ daily briefing, weekly insights ──▶ Strategist narrows the brief
                   Conversation ──▶ reply intent (stop / not now / question / complaint)
                                  │
                         INNER LOOP: no LLM, deterministic and seeded, logs probabilities
 events ─▶ state fold ─▶ goal bidders ─▶ hard rules ─▶ attention price ─▶ Thompson + floor
                                                             │
                                          ACT {goal, offer, channel, send_at, content, item, asset}
                                          or DO NOTHING, with P(choice) and a one-line reason
                                  │
 Dispatcher (idempotent) ─▶ SES / WhatsApp Cloud / SMS / webhook to the client's own tool
                                  │
 webhooks + purchases ─▶ event store ─▶ nightly learning (fast loop) + proof (agent vs baseline vs holdout)
```

**The core decision:** LLMs think, maths decides. A per-customer LLM call is too slow and too
expensive (about $3k a day at 1M customers), can't be audited, and can't learn from a holdout.
LLMs only ever choose from enumerated values or write text that a rules engine and a second model
check.

---

## 2. Design principles (and how the code enforces them)

| Principle | Enforced by |
|---|---|
| **Contracts first** | `autonomy/contracts/`: frozen pydantic models with `extra="forbid"` and timezone-aware datetimes. Every stream was built against these, and changes are additive only. |
| **Time is injected** | No logic reads the clock; `now` is always passed in. The Gym runs 90 days in about 60 s, and every run repeats exactly by seed. |
| **Every decision logs its probability** | `Decision.probability` is required, including for "do nothing". Without it, off-policy evaluation and honest proof are impossible. |
| **Seams have fakes** | `autonomy/interfaces.py` (Protocols) plus `autonomy/fakes/` (in-memory versions of every one), so any component runs and tests alone. |
| **Light core, heavy bridges** | The core needs only pydantic, numpy, pandas and SQLAlchemy. Imports of v1 AutoGluon, LightGBM or DuckDB sit in `autonomy/bridges/` and are tested under `@v1bridge`. |
| **Never fabricate numbers** | Briefing numbers are computed by code; `agents/claims.py` rejects any number in LLM prose that isn't in the computed set. Every money figure is an `Estimate(point, low, high)`. |

---

## 3. The inner loop: how one decision is made (`autonomy/decide`, `autonomy/runtime/agent.py`)

**Expected net value per candidate action** (this is the founder's ENV):

```
bid = urgency × ( uplift(customer, action) × value_if_converted
                  − offer_cost × P(redeem)            # sure-things redeem discounts too
                  − channel_cost )
net = bid − attention_price(customer)                  # learned fatigue: P(unsub | recent contacts) × future value
```

1. **Goal bidders.** Each active goal is a `GoalBidder`. It checks the audience filter, enumerates
   channel × offer × content, and returns `Bid`s with an uncertainty. Options that can't be the best
   even two standard deviations up are pruned.
2. **Hard rules** (`rules.py`, pure functions returning reason codes). They cover: consent and
   contactability, unsubscribed, open complaint, weekly cap (counting *pending* scheduled sends),
   minimum gap, quiet hours in the customer's own time zone, budget left, snooze, `no_discounts`,
   offers that need approval, and holdout membership (never act).
3. **The attention auction.** Bids from every goal compete. "Do nothing" is a bidder worth 0, and
   only one action per customer wins. The runner-up and the clearing price are exposed, so the
   auction is transparent.
4. **Exploration.** Thompson sampling over the surviving options, with an exploration floor taken
   from the envelope. The chosen option's probability is computed: exactly for the floor's mixing
   part, and by seeded Monte Carlo for the Thompson part.
5. **Timing.** A per-customer reading-hour Dirichlet, shrunk to the segment and then the business,
   never in quiet hours.
6. **Explanation.** `explain.py` turns the winning bid and the blocking codes into one sentence a
   marketer can read.

**Learning** (`autonomy/learning`, nightly, idempotent by decision id):
- **Per-person response.** A hierarchical Gamma–Poisson model of clicks with channel, offer and
  theme factors, shrunk from the individual to the segment to the business. Strengths are refit by
  method of moments.
- **Controls come only from the randomised holdout.** The agent's own "do nothing" decisions are
  not randomised, so they would bias the control rate.
- **CUPED.** Each customer's pre-period purchases are used to reduce variance, with
  DerSimonian–Laird pooling across segments.
- **Warm start.** Learning starts from the client's past campaigns, weighted by inverse propensity.

---

## 4. Proof and self-checking (`autonomy/proof`, `autonomy/gym`)

**Live proof.** Every customer is assigned by a salted hash to one of three groups:
**agent 80% / simple baseline 10% / no-contact holdout 10%**.
- Agent vs holdout is the incremental profit, which proves the marketing works.
- Agent vs baseline is the AI's own contribution, which justifies the fee.
- Intervals use Welch variance or a seeded bootstrap. With too little data the summary refuses to
  claim anything and says why.
- Off-policy evaluation (IPS, SNIPS, doubly robust) scores a new policy on logged decisions before
  rollout.

**The Gym: a planted-truth simulator** (`autonomy/gym`). Its purpose is to prove the agent works
before any real customer is involved.
- Customers carry hidden personas, uplift per channel, offer and theme, best hours, fatigue curves
  and sleeping dogs. Unsubscribes cost the customer's lost future value, sleeping dogs leave faint
  observable clues, and each customer arrives with a past campaign history.
- The library holds 10 scenarios: sleeping dogs, discount cannibalisation, WhatsApp fatigue,
  send-time sensitivity, mid-run drift, a small business, a null effect where nothing works, and a
  complaint storm.
- Baselines: send-to-all, a marketer's rule, the best fixed rule, an oracle restricted to
  observable segments, and the full oracle.
- **The pass bar is staged:** Safe (zero rule breaks, the breaker trips within 2 batches) → Useful
  (beats every baseline in at least 90% of seeds) → Smart (at least 80% of the segment oracle).
- The Gym strips its truth bookkeeping (`lost_value`) from events before any policy sees them.
- **Results are written up honestly**, including what fails and why (`GYM_RESULTS.md`, `AGENT_BRAIN.md`).

**Circuit breakers.**
- Complaint rate, unsubscribe multiple, delivery rate, spend pace and WhatsApp quality. A rule
  trips on a one-sided exact Poisson tail with minimum-count guards, so small samples can't trip it.
- Pacing slows the agent down before it reaches a limit. Breakers stop for harm early but never
  declare an early win.

---

## 5. The outer loop: agents (`autonomy/agents`)

| Agent | Job | Guardrail |
|---|---|---|
| Strategist | Goal text → `Brief` | Picks only enumerated values. If the model output is invalid, it falls back to a deterministic template. Weekly adjustments can only *narrow* the approved envelope. |
| Creative | Builds a labelled content library, including image, video and carousel variants from **approved** assets | Only allowed `{{slots}}` are used; no per-customer free text. |
| Critic | Approval gate | Deterministic rules run first (length, slots, banned claims, unsubscribe line, links, numbers must match the offer, approved media). Then an LLM judge from a different model family runs twice with the rubric order shuffled. |
| Analyst | Briefings and an opportunity scout | All numbers come from code. The number checker plus a retry, then a template fallback. |
| Conversation | Classifies replies | A keyword safety net means STOP always unsubscribes. In v2.0 it sends no free-text replies to customers. |
| Orchestrator | Goal lifecycle state machine | Plain code, not an agent. |

All agents use v1's `LLMClient` protocol (Bedrock, Anthropic or OpenAI-compatible), with
`ask_json` for schema-validated JSON with one repair retry. Tests use `ScriptedLLM`, so no real
model is called in CI.

A **text → signals** slot (reply intent, ticket topics, copy themes) has interchangeable backends:
an LLM, embeddings with a nearest-centroid classifier, or a Jev (TypeSafe) adapter.

---

## 6. Runtime and execution (`autonomy/runtime`, `autonomy/channels`, `api/v2`)

- **`AutonomyRuntime.tick(now)`**:
  1. Fold new events.
  2. Run the breakers.
  3. Per active goal, run the daily sweep plus event triggers (reply, cart, lapse).
  4. Act by autonomy level: **Shadow** logs only, **Approve** creates a pending `Batch`, **Autopilot**
     dispatches.
  5. Run nightly learning and the weekly Analyst → Strategist cycle.
- **Durability.**
  - Postgres stores built on SQLAlchemy Core: the event store dedupes on `event_id`, and the job
    queue claims with `FOR UPDATE SKIP LOCKED`.
  - Learning survives restarts by replaying the log.
  - One owner for send jobs.
- **Dispatcher.**
  - Idempotent per decision (it never sends twice, even when called twice).
  - Consent is re-checked at send time.
  - Messages scheduled for a later `send_at` go through the queue.
- **Adapters.**
  - SES v2 (event publishing via SNS), WhatsApp Cloud API (template sends, HMAC-verified
    webhooks), and a signed webhook or CSV hand-off to the client's own tool (Braze, Klaviyo and
    so on). SMS/DLT is in progress.
  - Every provider callback is mapped back to its `decision_id`.
- **Spend ledger.** Channel cost per send plus offer cost per redemption, with budgets paced evenly.
- **API.** FastAPI under `/v2` (goals, plans, approvals, batches, decisions with reasons, customers,
  briefing, proof, health, alerts, webhooks). Standalone engine APIs are coming under `/v2/engines`.
  The OpenAPI file is committed.

---

## 7. Data, products and formats

- **Data** (`autonomy/data`, plus v1 `engine/doctor`). Messy files go to the Data Doctor (PII,
  leakage and point-in-time checks). `EventMapping` turns flat tables into events, which a
  `StateFolder` turns into `CustomerState` (RFM segment, time zone, offer sensitivity, active
  hours, contact memory).
- **Catalogue** (`autonomy/catalog`), validated on the **real UCI Online Retail log** (386k product
  lines):
  - A per-customer refill-cycle model (empirical-Bayes log-normal gaps plus a pace term). It is
    22% better than the naive gap for customers with history.
  - An item-to-item co-purchase recommender: **34.7% hit-rate@5 vs 22.0% for best-sellers.**
- **Formats** (`autonomy/assets`). An approved asset library and a channel × format support matrix
  with the providers' documented limits. Format choice uses approved media only; nothing is
  generated yet.
- **Cross-client priors** (`autonomy/priors`). Only pooled patterns are shared, refused below 5
  clients, with any one client capped at 30% of the weight. No customer rows ever leave a client.

---

## 8. Mapping to the six-engine product architecture

| Product engine | Code |
|---|---|
| P1 Data Doctor | `engine/doctor` (v1), `autonomy/data`, `autonomy/store` |
| P2 Mandate & Catalogue | `contracts/goals.py` (Brief, Envelope), `autonomy/mandate` (signed mandates, two-person rule, margin floor; in progress) |
| P3 Decision Hub & Clearinghouse | `autonomy/decide`, `autonomy/learning`, `runtime/agent.py` |
| P4 Creative & Compliance Studio | `autonomy/agents` (Creative, Critic), `autonomy/assets`, compliance packs (in progress) |
| P5 Dispatch Gateway | `autonomy/channels` |
| P6 Causal Incrementality Ledger | `autonomy/proof`, `autonomy/gym`, breakers |

They are separate packages with their own contracts, deployed as one app. Each can be split into a
service later without a rewrite.

---

## 9. Engineering process

- **Contracts first, then parallel streams.** Wave 0 froze the shapes and fakes. Then 6 streams
  were built at once in separate git worktrees and merged by one integrator, followed by
  integration and improvement rounds: about 20 streams in total, each built, adversarially
  reviewed and fixed.
- **CI is the gate.**
  - A light v2 job (ruff, black, `mypy --strict` on Python 3.11, about 960 tests in a few minutes).
  - The full v1 suite.
  - A report-only Gym matrix (10 scenarios × 3 seeds × 3,000 customers) that uploads JSON
    scorecards.
- **Honesty rules in code review.** The agent never reads the Gym's truth, nothing is tuned to a
  seed, and nothing hard-codes the Gym's planted parameters. A failing criterion is reported with
  numbers, not gamed.

---

## 10. Honest status

| Area | Status |
|---|---|
| Safety | **Done.** Zero rule breaks, breakers stop harm within a batch, intervals cover the truth |
| Beats send-to-all and simple rules | **Done**, where messaging pays |
| Beats the best fixed rule | **Mostly** (3 of 4 measured scenarios). It loses where discounts are mostly wasted, because the first month of learning costs about 12% |
| Per-person ranking | **Weak.** About 2–3 clicks per customer in 90 days is thin evidence |
| Real sends | **Not yet.** The code is ready; it needs SES and WhatsApp credentials and network access |
| Real client | **None yet.** The plan is Shadow mode on one company's data first |

**Next:** the full realistic-Gym verdict from CI, the standalone engine APIs (in progress), the
mandate signing and margin floor, SMS/DLT plus compliance packs, failure-isolation tests, and a
live loop on our own test inboxes and phones.
