# Marketing AI — Plan N: Autonomous within a signed mandate

**Companion to:** the research report behind this plan
([`docs/research/AUTONOMOUS_MARKETING_AGENT_2026_10.md`](../research/AUTONOMOUS_MARKETING_AGENT_2026_10.md), with its notes in
`docs/research/autonomous_agent_2026_10_notes/`), Plan K (`MARKETING_AI_PLAN_K_PROPOSAL.md`) and its technical design
(`PLAN_K_TECHNICAL_DESIGN.md`), the hand-off design (`MARKETING_AI_PLAN_J_LAYER.md`), Plan M
(`MARKETING_AI_PLAN_M_SIMPLE_UI.md`), the positioning memo (`docs/research/POSITIONING_DECISION_2026_10.md`) and
`PARALLEL_WORK_PROTOCOL.md`.
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1700 … 1799 (proposed; not claimed). M200 claims it once D1 and D2 are approved. Plan M holds
DEC-1600 … 1699, and the protocol's §4 lists "DEC-1700 up" as unallocated.
**Milestones:** M200 – M225, for the report's provisional A1–A26 (§5 below). A0 is M184, already merged (DEC-1614).
We start at M200 on purpose. Numbers up to M189 are in use, and M190 may be taken by a small fix. A gap avoids a
collision with work merged while this plan is reviewed.
**Status:** proposal for management review, dated 12 October 2026. **Nothing is built until D1 and D2 are approved.**
Effort figures are the report's own estimates, which it calls unmeasured (report §7.3).

**How to read the citations.** "(report §7.3)" points to a section of the research report. The report's own source tags
(R1 [O], H and so on) are not repeated here; follow the section to see them. The report warns that most of its web
sources were search summaries, not opened pages (report, "Read this first"). This plan inherits that limit.

---

## Plan N at a glance

**The decision we propose.** Marketing AI becomes an agent that **acts unattended inside a mandate the client signs**.
It does so for one decision type at a time (report, executive summary).

- It proves every rupee against a holdout it cannot touch.
- It earns each step up with evidence and drops back automatically on harm.
- A deterministic dispatcher acts. A language model never sends (report §7.1, principle 2).
- It sends only on the client's own rails, never ours (report §5.1, Mode C).
- It never becomes an unbounded agent that sets its own goals, budgets, audiences and content (level L5).

**Why.** 78% of 500 advertisers accept AI acting within rules they set. Only 50% accept autonomy "when performance is
proven" (report §3.1). The market boundary is the mandate, not the AI. No vendor found sells a customer-owned,
no-message holdout reported in rupees with an error band (report §2.2, §2.5). We already have the hardest parts of
that. We lack the action path: mandates, a single writer, breakers that act and send-time state (report, executive
summary; §6.1).

| Phase | Earliest gate | What is true at the gate | Milestones | Effort (Eng + DS, person-weeks) |
|---|---|---|---|---|
| **0 — Make today safe; decide** | G0, week 4 (~8 Nov 2026) | Safety fixes merged and every audit finding closed or accepted. D1–D10 decided. Legal opinions commissioned. | M184 (done), M200–M202 | 3 + 0 |
| **1 — One real cycle at L2** | G1, week 16 (~early Feb 2027) | A real approved list handed off, reconciled and measured | M203–M207 | 20 + 1.5 |
| **2 — Shadow autonomy (L3, no sends)** | G2, week 26 (~mid-Apr 2027) | Two shadow cycles with zero mandate violations; breaker drill passed | M208–M213 | 17 + 7 |
| **3 — L3 pilot in a signed mandate, client rails** | G3, week 40 (~mid-Jul 2027) | One clean quarter; lower bound above zero, or an honest "cannot tell" | M214–M219 | 26 + 0.5 |
| **4 — Govern other engines; widen** | G4, week 64 (~Jan 2028) | Two clients at L3 on two rails; one external engine audited | M220–M222 | 12 + 2 |
| **5 — L4 for narrow portfolios** | G5, 2028, evidence only | A clean L3 quarter and a passing independent verifier | M223–M225 | 10 + 1 |

Sources: phases, gates and dates (report §7.2); effort (report §7.3). Week 0 is 12 October 2026. Dates are the earliest
a gate can pass. Client cycles set the pace, not effort (report §7.2, §7.3).

**Cost to the end of the L3 pilot (G3):** about 75 person-weeks, 66 engineering and 9 data science (report §7.3). Phase
1 is hand-off work needed with or without autonomy. The autonomy-specific cost to G3 is about 53.5 person-weeks (report
§7.3).

**What management must answer first:** D2. May a deterministic dispatcher act inside a signed mandate, on the client's
rails, once G2 passes? Today K1, DEC-1100 and the Plan J layer's no-SEND rule forbid it (report, executive summary; §7.8).

---

## 1. Decisions to take with management

Each answer is the report's recommendation (report §7.8). Plan N records the answers in one DEC entry in M200.

| # | Question | Recommended answer |
|---|---|---|
| **D1** | Is our target "autonomous within a signed mandate" (L3 per decision type), L4 later, never L5? | **Yes.** |
| **D2** | May a deterministic dispatcher act inside a signed mandate, on the client's rails, after G2? This amends K1, DEC-1100, the Plan J layer's §7 and its no-SEND design-rule test (§9 below). | **Yes, from G2.** Language models still never send. |
| **D3** | Rails policy. | **Client rails only.** Never our own (Mode O). |
| **D4** | Who may sign a mandate? Above which budget is a second signer needed? How long may a mandate run before re-certification? | Client Admin plus our Approver. Two signers above an agreed ₹ threshold. 90 days at most. Set the threshold with the pilot. |
| **D5** | Legal and insurance. | Commission the three legal opinions now. Buy affirmative AI cover before route B (M215). |
| **D6** | Pricing for autonomy. | A platform fee plus an optional fee on the measured lower bound. No per-message fees. |
| **D7** | First pilot envelope. | One client, one use case (win-back offer choice), one channel (e-mail), a small audience and budget. |
| **D8** | Staffing. | 3 engineers and 1 data scientist, plus a part-time second data scientist for Phase 2. |
| **D9** | Posture towards Braze, MoEngage and CleverTap. | Govern and audit their engines. Seek partner API access. Do not compete on decisioning. |
| **D10** | External language. | "Autonomous within your mandate". Never "fully autonomous". No unmeasured lift figures. |

**The three legal opinions** under D5 (report §7.4):

1. Are randomised no-message holdouts lawful and adequately noticed under DPDP?
2. Does a signed mandate keep us a processor, or does it make us a joint fiduciary?
3. Does TCCCPR's 15-day bar reach a registered Principal Entity's headers?

**Why D2 waits for G2.** D2 is a decision of principle now and a switch later. No send path is turned on before G2's
evidence exists: two clean shadow cycles, a passed breaker drill and counsel's review (report §7.2).

---

## 2. The target, in one table

A level attaches to **one decision type inside one operating envelope** (report §1.2). The envelope covers the use case,
action set, audience rule, caps, spend, offer ceiling, region, data freshness, certified policy version and expiry.

| Level | Who decides each action | Who is the fallback | In our product today (report §1.2) | Plan N |
|---|---|---|---|---|
| L0 Manual | A person | A person | Before the product | — |
| L1 Recommend | A person | A person | **Built** (Plan J) | Unchanged |
| L2 Propose and approve | A person approves each action | A person | **Largely built** (Plan K), mostly off by default | Phase 1 runs a real L2 cycle |
| L3 Act inside a mandate | The system, inside one signed mandate | The system pauses and reverts; a person resumes | **Not built** | Phases 2–3 |
| L4 Self-directed in a portfolio | The system, inside a portfolio mandate | Per-campaign breakers; a portfolio breaker returns all to L2 | Research-grade | Phase 5, narrow programmes only |
| L5 Unbounded | The system, with no envelope | None | **Not recommended** | **Never** |

**The highest level per decision type** (report §1.3):

| Decision type | Highest level |
|---|---|
| Reporting and "what changed and why" | L3 (read-only) |
| Send time inside allowed hours; frequency, including "do not send" | L3 |
| Channel among consented channels; audience refresh inside a rule | L3 |
| Offer choice from an approved menu; pick among approved copy variants | L3 |
| Budget pacing inside a set total | L3 |
| Writing new SMS or WhatsApp text | **L2 forever** |
| Discount depth or new offers | **L2** |
| Total budget, goal, strategy | **L1–L2** |
| Compliance checks | Deterministic rules at every level; the override is never automated |
| Price, eligibility, credit (BFSI) | **Excluded** |

**Promotion** moves one level at a time, per envelope. It is a signed Admin act that quotes evidence ids. **Widening**
an envelope is a new mandate, never an edit (report §1.2).

**Automatic demotion to L2** happens on any of these eight triggers (report §1.2):

1. a harm alarm on the primary metric or any guardrail;
2. a spend or volume breaker;
3. a data-freshness breaker;
4. the compliance-block rate above its threshold;
5. a mismatch between our send record and the channel's send log;
6. drift above threshold;
7. mandate expiry;
8. a policy-version change that was not certified.

---

## 3. The architecture Plan N builds

### 3.1 Two loops and a single writer (report §4.6)

```
OUTER LOOP (days; LLM-assisted; L2 by default)
  goal or warning card -> planner -> draft mandate or campaign proposal
  content: LLM drafts -> guardrails + claims library -> registered templates -> variant arms
  policy (default deny) -> signed approval of the mandate (Trusted Surface) -> ledger

INNER LOOP (per cycle or event; no LLM; L3 inside a mandate)
  events/files -> decision-state store -> holdout and explore (fixed, logged probabilities)
  -> policy runtime: value per action -> stochastic choice + propensity
  -> arbitration + pacing + pressure caps -> mandate check + compliance decision -> OUTBOX
  -> DISPATCHER (single writer; idempotent; token buckets) -> client's CEP or client's rails
  -> receipts -> fixed-window outcomes -> measure / certify -> learn
  -> monitors (harm alarm, guardrails, anomalies, drift, reconciliation) -> BREAKER -> safe state, demote to L2
  everything -> signed hash-chained ledger -> client receipts; verifier bundle
```

The language model works only in the outer loop. The inner loop has none. Exactly one component commits actions
(report §4.3).

### 3.2 The six new invariants (report §4.6)

Plan K's invariants stay. Each new one has a test named in a milestone below.

| # | Invariant | Enforced by | Milestone |
|---|---|---|---|
| I3a | Every executed action cites a valid, unexpired, signed mandate and lies inside its envelope | The dispatcher re-checks each action | M208, M215 |
| I3b | Holdout and explore membership come only from the holdout service | The dispatcher refuses any held-out key | M210, M215 |
| I3c | A breaker trip reaches the safe state within one dispatcher cycle; only a person resumes | Breaker state machine; drilled | M209, M212 |
| I3d | Every action records its propensity, policy-version hash and mandate id | Outbox schema; evaluation refuses rows without them | M210, M211 |
| I3e | The dispatcher alone holds channel credentials | Scoped secrets; a test for adapter imports | M215 |
| I3f | The mandate shown to the signer is rendered by code from canonical JSON | The approval screen never shows LLM text as the mandate | M208 |

### 3.3 The safe state (report §4.5)

1. The dispatcher refuses new items at once.
2. Queued items go only with a valid mandate and a passed compliance decision. Others are cancelled where possible.
3. The holdout is untouched and measurement continues.
4. The policy reverts to the last certified version, or to business-as-usual where a customer expects a message.
5. The trip and the evidence go to the ledger, and a person must resume.

### 3.4 What a mandate contains (report §5.6)

- purposes and objective, set by the client;
- channels, and registered templates or approved creatives per channel;
- audience rule, maximum size, and excluded cohorts (minors, vulnerability flags, BFSI-restricted products);
- frequency and pressure caps; quiet hours (product default 10:00–20:00 IST, stricter than the legal band);
- spend per day and per period; offer-value ceiling and margin floor;
- the certified policy version, the registered test plan, and the holdout;
- escalation triggers; expiry date; signers.

A mandate can only narrow global policy, never widen it ("forbid wins"). A two-person rule applies above a budget
threshold (report §5.6; D4).

### 3.5 Where it lives in the code

Today's modules come from the report's capability map (report §6.1). New paths are **proposed**; each milestone
confirms them in its DEC.

| Part | Today (report §6.1) | Plan N |
|---|---|---|
| Holdout and explore slice with logged propensities | Built: `engine/holdout/assign.py` | Read-only source of membership (I3b) |
| Offer and channel choice | Built, deterministic: `engine/decide/` | Stays for L2; a stochastic runtime is added beside it (M210) |
| Doubly robust OPE (binary) | Built: `engine/uplift/ope.py` | Extended to several actions (M211) |
| Harm alarm (warns only) | Built, off: `engine/measurement/harm.py` | Wired to the breaker (M209) |
| Default-deny policy; proposals; ledger | Built: `engine/agentic/{policy,proposals,ledger}.py` | New rules (M201); new ledger kinds (M208) |
| Canonical JSON for hashes | Built: `engine/agentic/canonical.py` (sorted keys; not RFC 8785) | A versioned RFC 8785 canonicaliser (M208) |
| Compliance gate | Built, off: `engine/compliance/` | Re-run at dispatch time (M218) |
| Send-log reconciliation | Partial, manual upload: `engine/measurement/reconcile.py` | Real importer for the pilot's CEP (M207) |
| Activation Contract, destinations, release rule | Designed only (J_LAYER M94, M97, M100): `engine/activation/` | Built in Phase 1 (M204–M206) |
| Mandates, levels, breakers, halt API | Missing | `engine/autonomy/` (new, proposed) |
| Dispatcher and channel adapters | Missing | `engine/dispatch/` (new, proposed) |
| Contact history, quiet hours, per-day throttle | Missing (frequency partial) | M213, M218 |
| Real-time events, decision-state store | Missing | M217 |

**A naming warning** (report §6.1). The Plan J layer design numbers its milestones M90–M120. Those numbers mean
different, built work in the README. This plan always writes "J_LAYER M94" for the design.

---

## 4. Phases and gates

### 4.1 The gates (report §7.2)

| Gate | Earliest | Evidence needed to pass |
|---|---|---|
| **G0** | ~8 Nov 2026 | • M184 merged; every S1–S10 item closed or signed off as accepted<br>• D1–D10 decided<br>• DPDP holdout and sender-role opinions commissioned<br>• a pilot candidate named, with its stack known |
| **G1** | ~early Feb 2027 | • a real approved list handed off<br>• contamination <5% of holdout IDs; ≥70% of the treat list contacted<br>• a Value Proof Pack on a real campaign<br>• usability test ≥4 of 5 per task (M181) |
| **G2** | ~mid-Apr 2027 | • ≥2 consecutive shadow cycles with **zero** mandate violations<br>• shadow policy evaluated against the approved L2 list, effective sample size above the plan minimum<br>• pass^k = 1 on autonomy scenarios<br>• breaker drill reaches the safe state within one cycle<br>• counsel has reviewed the mandate and contract<br>• management approves D2 |
| **G3** | ~mid-Jul 2027 | • one quarter (≥3 cycles) of L3 with zero mandate violations<br>• no DLT notice, no WhatsApp template pause, spam rate <0.1%<br>• every demotion handled correctly<br>• universal-holdout lower bound >0 in rupees, **or** an honest "cannot tell" that keeps the envelope narrow<br>• the pilot CFO accepts the readout<br>• sender operations ≤0.5 person per client per month |
| **G4** | ~Jan 2028 | • two clients at L3 on two different CEPs or rails<br>• one external engine audited against our no-message holdout<br>• a second channel certified on two consecutive cycles |
| **G5** | 2028, evidence only | • ≥1 clean L3 quarter<br>• portfolio lower bound >0 against the universal holdout<br>• verifier bundle passing every cycle<br>• incident drills<br>• a written oversight procedure |

### 4.2 Kill and step-back triggers (report §7.2)

Each trigger pauses the named work and is reported to management.

- 2 of the first 5 CFOs discount a readout because "you sent it yourselves": stop at route A (hand-off only).
- Any client receives a DLT violation notice, or SES review is triggered on more than one client: freeze dispatch.
- pass^k below 100% after one fix round: pause promotion.
- A major platform ships an equivalent neutral audit first: re-plan Phase 4 as integration, not competition.

---

## 5. Milestones

**Effort basis.** Figures are person-weeks from the report, "Eng" for engineering and "DS" for data science (report
§7.3). The report marks them as its own, unmeasured estimates. Where the basis is the J_LAYER design, its own figure
is used. Each milestone records one DEC entry with lettered sub-decisions, inside DEC-1700 … 1799.

**Mapping.** Report ID A*n* becomes M(199 + *n*), so A1 is M200 and A26 is M225. A0 keeps its existing number, M184.

### 5.1 Milestone table

| Milestone | Report ID | Name | Phase | Gate | Eng | DS | Depends on |
|---|---|---|---|---|---|---|---|
| M184 | A0 | Safety fixes from the autonomy audit (**merged**, DEC-1614) | 0 | G0 | done | – | – |
| M200 | A1 | Autonomy decision record, mandate schema, holdout glossary | 0 | G0 | 1 | – | D1, D2 |
| M201 | A2 | Policy rules M184 leaves open; sign-in required for autonomy | 0 | G0 | 2 | – | M184 |
| M202 | A3 | Primary-source verification sprint (V1–V11) | 0 | G0 | 0 (1 analyst-week) | – | – |
| M203 | A4 | First deployment in the pilot's account | 1 | G1 | 2 (+ Minfy cloud team) | – | M184 |
| M204 | A5 | Activation Contract | 1 | G1 | 3.5 | – | M203 |
| M205 | A6 | Destination and send-log SDK | 1 | G1 | 4 | – | M204 |
| M206 | A7 | Hand-off with the release rule; erasure and withdrawal propagation | 1 | G1 | 5 | – | M204, M205 |
| M207 | A8 | Send-log importer and reconciliation for the pilot's CEP | 1 | G1 | 5.5 | 1.5 | M205 |
| M208 | A9 | Mandate model, pure checker, signing, Trusted Surface, ledger kinds, RFC 8785 canonicaliser | 2 | G2 | 4 | – | M200, M201 |
| M209 | A10 | Levels, promotion and demotion; breaker; global halt API; harm alarm wired | 2 | G2 | 3 | – | M208 |
| M210 | A11 | Policy runtime: stochastic choice, logged propensities, exploration floor, signed policy versions | 2 | G2 | 3 | 2 | M208 |
| M211 | A12 | Multi-action doubly robust OPE; fixed-window labels; non-inferiority guardrails | 2 | G2 | 1 | 4 | M210 |
| M212 | A13 | Shadow runner, replay, failure injection, breaker drill | 2 | G2 | 3 | 0.5 | M209, M210 |
| M213 | A14 | Contact history, pressure state, pacing on spend and sends | 2 | G2 | 3 | 0.5 | M207 |
| M214 | A15 | Route A: mandate-gated scheduled hand-off to the client's CEP | 3 | G3 | 7 | – | M206, M209 |
| M215 | A16 | Route B: dispatcher on client rails, with an SES e-mail adapter | 3 | G3 | 5 | – | M209, M213 |
| M216 | A17 | SMS (DLT) and WhatsApp (client BSP) adapters, template-only | 3 | G3 | 4 | – | M215 |
| M217 | A18 | Event intake, decision-state store, suppression in minutes | 3 | G3 | 4 | – | M213 |
| M218 | A19 | Dispatch-time compliance; telemetry into breakers | 3 | G3 | 3 | – | M215, M217 |
| M219 | A20 | Verifiable evidence bundle and stand-alone verifier | 3 | G3 | 3 | 0.5 | M208 |
| M220 | A21 | Governor for Braze Decisioning Studio | 4 | G4 | 5 | 1 | M214, M219 |
| M221 | A22 | Governor for MoEngage Merlin / Aampe | 4 | G4 | 5 | 1 | M214, M219 |
| M222 | A23 | Second channel and second client mandates; optional Cedar compile | 4 | G4 | 2 | – | G3 |
| M223 | A24 | Portfolio mandate: create, retire and fund campaigns inside it | 5 | G5 | 4 | 1 | G4 |
| M224 | A25 | Independent verifier service with its own key | 5 | G5 | 3 | – | M219 |
| M225 | A26 | Claims library bound to template slots; automatic variant withdrawal | 5 | G5 | 3 | – | M208 |

Names, phases, effort and dependencies: report §7.3, with A-numbers replaced by M-numbers.

### 5.2 Phase 0 — Make today safe; decide (weeks 0–4, gate G0)

**Goal.** Close the holes that matter once nobody reviews each list. Take the decisions. Check the claims the plan
rests on (report §7.2).

#### M184 — Safety fixes from the autonomy audit (A0; merged)

- **State.** Merged; README marks it done (DEC-1614). The report wrote "in progress" on 11 October (report §6.3).
- **What it closed, by our reading of DEC-1614.** EXECUTE now keeps to the approved channel and size (S4). Unattended
  steps use only approved models (S2). `compliance.gate.strict` lets the gate fail closed (S6). Copy approval has
  separation of duties and a ledger entry (S7). Decisions with sign-in off are marked, and `agentic.require_sign_in`
  exists (S1, by switch).
- **What stays open for G0** (report §6.3):
  - S3, silent challenger promotion, and S5, cycles without a registered test plan: a fix or a signed acceptance (M201);
  - S8, a payload-only frequency cap: M213;
  - S9, missing policy rules: M201;
  - S10, a harm alarm that only warns: M209.

#### M200 — Autonomy decision record, mandate schema and holdout glossary (A1)

- **Goal.** Record D1–D10 and fix the shape of a mandate before any code depends on it.
- **Builds.**
  - The claim: a `DEC-1700…1799 | Plan N — autonomous within a signed mandate (M200–M225)` row in the protocol's §4.
  - `PLAN-N` blocks after `PLAN-M` in every shared file; `PLAN-N` in `tests/unit/test_shared_file_markers.py`.
  - One DEC entry recording D1–D10 and the S1–S10 status table.
  - The mandate schema (§3.4) as a frozen model with `extra="forbid"`, in `engine/autonomy/mandate.py` (new, proposed).
  - The holdout glossary: no-message, random-arm, business-as-usual and synthetic control (report §2.2), in
    `docs/AUTONOMY.md` (new, proposed).
- **Acceptance.**
  - The schema holds every field in §3.4 and refuses an unknown one (a unit test walks the list).
  - The glossary grades each vendor row of report §2.2 against the four control types.
  - `test_shared_file_markers.py` is green; nothing else changes.
- **Effort:** 1 Eng. **Depends on:** approval of D1 and D2. **Gate:** G0.

#### M201 — Policy rules M184 leaves open (A2)

- **Goal.** Close the policy gaps a mandate check would otherwise inherit (report §6.3, S9).
- **Builds.** New default-deny rules in `engine/agentic/policy.py` (Plan K's area, backward-compatible):
  - model approved, reusing `engine/model_approval.py` (M184's rule);
  - consent freshness;
  - quiet hours;
  - spend over time, not only per proposal;
  - sign-in required whenever autonomy is on (`agentic.require_sign_in` forced true).
- **Acceptance.**
  - Each rule refuses its fixture with its own code, and a missing input is refused, never allowed.
  - With autonomy on, an approval with `identity_verified: false` is refused.
  - With autonomy off, every existing policy test and golden file is unchanged.
  - S3 and S5 are fixed under autonomy, or signed off as accepted in the DEC.
- **Effort:** 2 Eng (report §7.3). The report does not estimate S3 and S5 fixes; if they are built here, the DEC adds
  the effort. **Depends on:** M184. **Gate:** G0.

#### M202 — Primary-source verification sprint (A3)

- **Goal.** Check the claims the plan and any sales material would rest on (report, "Read this first", V1–V11).
- **Builds.** No code. One analyst-week, starting with the TRAI and DPDP texts (report §7.4).
- **Acceptance.**
  - Each of V1–V11 is confirmed, corrected or marked "do not use", with the opened source named.
  - The outcomes are added to the research report as a dated correction table; nothing earlier is rewritten.
  - V11 (market size) stays "do not use" unless a method is found.
- **Effort:** 0 Eng (1 analyst-week). **Depends on:** nothing. **Gate:** G0.

**G0 also needs** the three legal opinions commissioned (D5), and a pilot candidate named with its stack known (report
§7.2).

### 5.3 Phase 1 — One real cycle at L2 (weeks 2–16, gate G1)

**Goal.** One real list, approved by a person, handed off, reconciled and measured. This is J_LAYER work needed with or
without autonomy (report §7.3). These milestones carry the J_LAYER design's own scope and acceptance; Plan N adds only
what autonomy needs later.

#### M203 — First deployment in the pilot's account (A4; J_LAYER M93)

- **Goal.** The product runs inside the pilot client's own account, with sign-in on.
- **Builds.** J_LAYER M93's scope: `scripts/smoke_client_account.py` (new) and the deployment documents. The Minfy
  cloud team takes part.
- **Acceptance.**
  - J_LAYER M93's acceptance holds in the pilot's account.
  - The holdout source-of-truth agreement and the send-log clause are signed (report §7.4, weeks 4–6).
- **Effort:** 2 Eng (+ Minfy cloud team). **Depends on:** M184. **Gate:** G1.

#### M204 — Activation Contract (A5; J_LAYER M94)

- **Goal.** One governed contract file that a client's tool can load.
- **Builds.** J_LAYER M94's new package `engine/activation/` (`contract.py`, `build.py`, `delta.py` and the rest).
- **Acceptance.**
  - J_LAYER M94's acceptance, including its dry-run and key-match kit.
  - No holdout key ever appears in a contract (a test plants one; I3b in spirit).
- **Effort:** 3.5 Eng. **Depends on:** M203. **Gate:** G1.

#### M205 — Destination and send-log SDK (A6; J_LAYER M97)

- **Goal.** A tested path to write a contract out and read a send log back.
- **Builds.** J_LAYER M97: `engine/activation/destinations/` and `engine/activation/sendlog/`, with fakes and a
  conformance kit.
- **Acceptance.** J_LAYER M97's conformance suites are green on the fake, local and S3 destinations.
- **Effort:** 4 Eng. **Depends on:** M204. **Gate:** G1.

#### M206 — Hand-off with the release rule (A7; J_LAYER M100)

- **Goal.** An approved list leaves the product only under the release rule, and erasure and withdrawal follow it.
- **Builds.** J_LAYER M100: `engine/activation/{deliver,state,release}.py`, the activation API and run screens.
- **Acceptance.**
  - A valid path delivers on test infrastructure (report §7.4).
  - An erased or withdrawn customer is removed from the next delta (J_LAYER M100).
  - **G1 evidence:** a real approved list handed off in the pilot's tool.
- **Effort:** 5 Eng. **Depends on:** M204, M205. **Gate:** G1.

#### M207 — Send-log importer and reconciliation for the pilot's CEP (A8; J_LAYER M102, M103)

- **Goal.** Know who was really contacted, and whether the holdout stayed clean.
- **Builds.** J_LAYER M102 and M103, on top of today's partial `engine/measurement/reconcile.py`. The report notes this
  may shrink, because part of it is built (report §7.3).
- **Acceptance.**
  - The pilot's real send log is imported and reconciled.
  - **G1 evidence:** contamination <5% of holdout IDs and ≥70% of the treat list contacted, or a corrective cycle
    agreed (report §7.2).
  - **G1 evidence:** a Value Proof Pack on the real campaign.
- **Effort:** 5.5 Eng, 1.5 DS. **Depends on:** M205. **Gate:** G1.

**G1 also needs** Plan M's usability test (M181) at ≥4 of 5 per task (report §7.2). It is run whichever way it falls
(report §7.4).

### 5.4 Phase 2 — Shadow autonomy, L3 with no sends (weeks 8–26, gate G2)

**Goal.** The whole inner loop runs, decides and is checked, but nothing is sent. Phase 2 can be built on the demo
library and synthetic data while G1 waits; only its gate needs real cycles (report §7.7).

#### M208 — Mandates: model, pure checker, signing, Trusted Surface (A9)

- **Goal.** A signed mandate that code checks for every action (report §5.6).
- **Builds.**
  - `engine/autonomy/mandate.py`: the model from M200 and a pure function `check(action, mandate)` returning a verdict
    and reasons.
  - Signing with the ledger's deployment key; new ledger kinds for a mandate signed, expired and superseded.
  - A versioned RFC 8785 canonicaliser, so old ledger entries still verify (report §4.5).
  - The Trusted Surface: an approval screen that renders the mandate from canonical JSON only (report §4.5; I3f), in
    `ui/modules/autonomy/` (new, proposed).
- **Acceptance.**
  - Property tests: the checker is pure; an action outside any field of the envelope is refused with a reason.
  - A mandate that would widen global policy is refused ("forbid wins"). Editing a signed mandate is refused.
  - An expired mandate refuses every action.
  - Above the D4 threshold, one signature is not enough. The signer cannot be the proposer (S1).
  - A test puts LLM text in the proposal; it never appears as the mandate on screen (I3f).
  - Ledger entries written under today's canonical form still verify after the new version lands.
- **Effort:** 4 Eng. **Depends on:** M200, M201. **Gate:** G2.

#### M209 — Levels, breakers and the global halt (A10)

- **Goal.** Autonomy that falls back to a safe state on its own (report §4.5).
- **Builds.**
  - `engine/autonomy/levels.py`: per-envelope level, promotion and demotion.
  - `engine/autonomy/breaker.py`: a breaker state machine on the inputs in report §4.5, with robust baselines and minimum
    samples.
  - A global halt and resume API in `api/routes/autonomy.py` (new, proposed), each with a RoutePolicy.
  - `engine/measurement/harm.py` feeds the breaker (backward-compatible; with autonomy off it still only warns).
- **Acceptance.**
  - Promotion moves one level, needs an Admin signature and cites evidence ids (report §1.2).
  - Each of the eight demotion triggers in §2 demotes a fixture envelope to L2.
  - A trip performs the five safe-state steps of §3.3 within one dispatcher cycle (I3c).
  - Only a signed-in person can resume, and the resume is a ledger entry.
- **Effort:** 3 Eng. **Depends on:** M208. **Gate:** G2.

#### M210 — The policy runtime (A11)

- **Goal.** Stochastic choice with logged propensities, which L3 needs and today's deterministic offer choice blocks
  (report §4.2).
- **Builds.**
  - `engine/autonomy/runtime.py`: stochastic offer × channel choice, with the propensity of each choice logged.
  - An exploration floor, at least 1% per arm (report §4.2).
  - The action set equals the mandate's set, so every action stays inside the logs' support (report §4.2).
  - A signed hash for every policy version.
- **Acceptance.**
  - Every outbox row carries propensity, policy-version hash and mandate id; a row without them is refused (I3d).
  - In a seeded test, observed choice frequencies match the logged propensities within tolerance.
  - No arm falls below the floor. Holdout and explore membership come only from `engine/holdout/assign.py` (I3b).
  - With autonomy off, today's L2 lists are byte-identical.
- **Effort:** 3 Eng, 2 DS. **Depends on:** M208. **Gate:** G2.

#### M211 — Multi-action evaluation (A12)

- **Goal.** Judge a candidate policy honestly, or say "cannot tell" (report §4.2).
- **Builds.**
  - `engine/uplift/ope.py` extended from treat / not-treat to several actions, doubly robust (Plan J's area,
    backward-compatible).
  - Refusal when support is missing or the effective sample size is below the plan minimum.
  - Fixed-window outcome labels; guardrails as non-inferiority margins.
- **Acceptance.**
  - On the simulator, the lower bound is wrong at most at its stated rate in the nightly statistical suite.
  - An action never logged is refused, not estimated. A row without a propensity is refused (I3d).
  - "Cannot tell" is a tested outcome.
  - Hillstrom is re-run and the result recorded, whichever way it falls.
- **Effort:** 1 Eng, 4 DS. **Depends on:** M210. **Gate:** G2.

#### M212 — Shadow runner, replay and failure injection (A13)

- **Goal.** Produce G2's evidence without sending anything.
- **Builds.**
  - `engine/autonomy/shadow.py`: runs the inner loop end to end into an outbox that no dispatcher reads.
  - Replay of past cycles.
  - Failure injection: empty input, stale consent, duplicate rows, 429 storms, template rejection (report §7.3).
  - A scripted breaker drill, run monthly (report §1.2, L3 evidence).
- **Acceptance.**
  - **G2 evidence:** ≥2 consecutive shadow cycles with zero mandate violations.
  - **G2 evidence:** the shadow policy is evaluated against the approved L2 list (M211), above the plan's minimum
    effective sample size.
  - **G2 evidence:** pass^k = 1 on the autonomy scenarios.
  - **G2 evidence:** the breaker drill reaches the safe state within one cycle.
  - Each injected failure ends in a refusal or a trip, never a send record.
- **Effort:** 3 Eng, 0.5 DS. **Depends on:** M209, M210. **Gate:** G2.

#### M213 — Contact history, pressure and pacing (A14)

- **Goal.** Caps that hold across all campaigns, from what was really sent (report §6.3, S8).
- **Builds.**
  - A product-owned contact history fed by reconciled send logs (M207).
  - Pressure state per customer; pacing on spend and sends with a clamped integral (report §4.2).
- **Acceptance.**
  - A customer at the cap in one campaign is not contacted by another (history, not payload).
  - In simulation, pacing never exceeds the daily spend or send ceiling, including at the hard cap.
  - The agent can only send less than a cap, never more (report §1.3).
- **Effort:** 3 Eng, 0.5 DS. **Depends on:** M207. **Gate:** G2.

**G2 also needs** counsel's review of the mandate and contract, and management's approval of D2 at that point (report
§7.2).

### 5.5 Phase 3 — L3 pilot in a signed mandate, on client rails (weeks 20–40, gate G3)

**Goal.** One client, one envelope, one quarter at L3 (D7). Route A hands decisions to a CEP; route B dispatches on the
client's own accounts (report §7.5). **Route A alone saves 16 Eng** of this phase (report §7.3).

#### M214 — Route A: mandate-gated scheduled hand-off (A15; J_LAYER M113 + 1)

- **Goal.** The client's CEP sends; our policy decides inside the mandate. We hold no channel credentials (report
  §7.5).
- **Builds.** J_LAYER M113's scheduled loop, plus a mandate check before each hand-off and a suppression delta on a
  breaker trip.
- **Acceptance.**
  - A scheduled hand-off without a valid mandate is refused and nothing is written.
  - On a trip, a suppression delta reaches the CEP within one cycle (I3c).
  - J_LAYER M113's acceptance holds.
- **Effort:** 7 Eng (J_LAYER M113's 6 + 1). **Depends on:** M206, M209. **Gate:** G3.

#### M215 — Route B: the dispatcher on client rails (A16)

- **Goal.** The single writer, for clients with only their own rails (report §7.5).
- **Builds.**
  - `engine/dispatch/` (new, proposed): a transactional outbox; an idempotency key per (customer, envelope, arm,
    cycle); token buckets per channel; receipts; daily reconciliation against the channel send log (report §4.7).
  - An SES e-mail adapter that runs in the client's AWS account.
- **Acceptance.**
  - The dispatcher re-checks every action against its mandate (I3a) and refuses any held-out key (I3b).
  - Only the dispatcher holds channel credentials: an import test fails if another module reaches an adapter (I3e).
  - Replaying a cycle sends nothing twice.
  - A reconciliation mismatch trips the breaker (§2, trigger 5).
  - E-mail meets the bulk-sender rules in report §4.7, including one-click unsubscribe.
  - Affirmative AI cover is in place before the first live send (D5).
- **Effort:** 5 Eng. **Depends on:** M209, M213. **Gate:** G3.

#### M216 — SMS and WhatsApp adapters, template-only (A17)

- **Goal.** Selection, not authorship, for SMS and WhatsApp (report §5.2, §7.1 principle 6).
- **Builds.** An SMS adapter for DLT via End User Messaging, and a WhatsApp adapter through the client's BSP.
- **Acceptance.**
  - A message that does not match a registered template, its tags or its URL whitelist is refused.
  - WhatsApp error 131049 triggers back-off; a paused template trips the breaker (report §4.7).
  - No path sends free text on either channel.
- **Effort:** 4 Eng. **Depends on:** M215. **Gate:** G3.

#### M217 — Event intake and suppression in minutes (A18)

- **Goal.** Opt-outs and complaints act before the next send (report §5.7).
- **Builds.** Event intake for opt-outs, complaints and receipts; a decision-state store; unified suppression across
  channels.
- **Acceptance.**
  - An opt-out on one channel suppresses the customer on every channel before the next dispatch.
  - The suppression delay is measured and recorded in the DEC; the target is minutes (report §5.7).
  - Complaints and grievances are counted in the breaker inputs.
- **Effort:** 4 Eng. **Depends on:** M213. **Gate:** G3.

#### M218 — Dispatch-time compliance (A19)

- **Goal.** Re-check at dispatch, not only at planning (report §5.7).
- **Builds.** The compliance gate re-run in the dispatcher: quiet hours, template status, consent; complaint and spam
  telemetry into the breakers.
- **Acceptance.**
  - A template suspended after planning is refused at dispatch.
  - Nothing goes outside the mandate's quiet hours (product default 10:00–20:00 IST).
  - The breaker trips on complaints well below 5 in 10 days, any WhatsApp pause, and spam at 0.08–0.1% (report §5.7).
  - The gate fails closed under autonomy (`compliance.gate.strict` on).
- **Effort:** 3 Eng. **Depends on:** M215, M217. **Gate:** G3.

#### M219 — Verifiable evidence bundle and stand-alone verifier (A20; Plan K design M148)

- **Goal.** Proof that does not ask the client to trust us (report §7.1, principle 5).
- **Builds.** Plan K design M148: a bundle with the ledger, mandates, outbox, receipts and readout; a verifier that runs
  on its own.
- **Acceptance.**
  - The verifier recomputes the hash chain, the mandate checks and the holdout readout from the bundle alone.
  - Any edit to the bundle is caught and named.
  - The client owns the holdout salt.
- **Effort:** 3 Eng, 0.5 DS. **Depends on:** M208. **Gate:** G3.

**G3 evidence**, all from the pilot quarter (report §7.2): ≥3 cycles at L3 with zero mandate violations; no DLT notice,
no WhatsApp template pause, spam <0.1%; every demotion handled; a lower bound >0 in rupees or an honest "cannot tell";
the CFO accepts the readout; sender operations ≤0.5 person per client per month.

### 5.6 Phase 4 — Govern other engines; widen (weeks 40–64, gate G4)

**Goal.** Where a client runs Braze or MoEngage decisioning, govern it rather than rival it (report §7.1 principle 4;
§7.5).

#### M220 — Governor for Braze Decisioning Studio (A21) and M221 — Governor for MoEngage Merlin / Aampe (A22)

- **Goal.** Their engine decides; we own the counterfactual (report §7.5).
- **Builds**, per engine:
  - translate the mandate into their action bank and constraints;
  - insist on a no-message arm we own;
  - run our compliance gate on their candidates;
  - pause through their API on a breaker trip;
  - verify from their send logs.
- **Acceptance.**
  - A breaker trip pauses their engine through its API, in a test against a fake of that API.
  - **G4 evidence:** one external engine audited against our no-message holdout.
- **Effort:** 5 Eng, 1 DS each. **Depends on:** M214, M219; partner API access (open question 6). **Gate:** G4.

#### M222 — Second channel and second client (A23)

- **Goal.** Show the design is not one client's special case.
- **Builds.** Mandates for a second channel and a second client. An optional Cedar compile, only if a client asks for
  gateway enforcement (report §7.5).
- **Acceptance.** **G4 evidence:** two clients at L3 on two different CEPs or rails; a second channel certified on two
  consecutive cycles.
- **Effort:** 2 Eng. **Depends on:** G3. **Gate:** G4.

### 5.7 Phase 5 — L4 for narrow portfolios (week 64 onward, gate G5)

**Goal.** For narrow programmes only, let the system create, retire and fund campaigns inside a portfolio mandate.
Evidence decides, not the calendar (report §7.2).

#### M223 — Portfolio mandate (A24)

- **Builds.** A portfolio mandate in which the system creates, retires and funds campaigns.
- **Acceptance.** A portfolio breaker returns every campaign to L2 (report §1.2). **G5 evidence:** a portfolio lower
  bound >0 against the universal holdout.
- **Effort:** 4 Eng, 1 DS. **Depends on:** G4. **Gate:** G5.

#### M224 — Independent verifier service with its own key (A25)

- **Builds.** The M219 verifier as a service that a third party can run, signing with its own key.
- **Acceptance.** **G5 evidence:** the verifier bundle passes every cycle.
- **Effort:** 3 Eng. **Depends on:** M219. **Gate:** G5.

#### M225 — Claims library bound to template slots (A26)

- **Builds.** A claims library bound to template slots; a variant that trips a guardrail is withdrawn automatically.
- **Acceptance.** A planted claim outside the library is refused; a variant that trips a guardrail stops being chosen
  in the next cycle.
- **Effort:** 3 Eng. **Depends on:** M208. **Gate:** G5.

**G5 also needs** incident drills and a written oversight procedure (report §7.2).

---

## 6. Effort and capacity

| Phase | Milestones | Eng | DS | Total |
|---|---|---|---|---|
| 0 | M184 (done), M200–M202 | 3 | 0 | 3 |
| 1 | M203–M207 | 20 | 1.5 | 21.5 |
| 2 | M208–M213 | 17 | 7 | 24 |
| 3 | M214–M219 (route A alone saves 16) | 26 | 0.5 | 26.5 |
| **To the end of the L3 pilot** | | **66** | **9** | **75** |
| 4 | M220–M222 | 12 | 2 | 14 |
| 5 | M223–M225 | 10 | 1 | 11 |

Source: report §7.3.

- **Team (D8):** 3 engineers and 1 data scientist, as Plan K assumed. Effort fits well inside the calendar; client
  cycles are the constraint (report §7.3).
- **Phase 2's 7 DS-weeks** need the part-time second data scientist of decision K10 (report §7.3).
- **Phase 1 overlaps the integration plan.** If it is written first, M203–M207 move into it. Plan N then keeps
  Phases 0 and 2–5.

---

## 7. The first 90 days (12 Oct 2026 – 9 Jan 2027)

From report §7.4, with milestone numbers added.

| When | Work | Owner | Done means |
|---|---|---|---|
| **Weeks 0–1** (to 25 Oct) | M184: record S1–S10 status (code merged, DEC-1614) | Engineering | Each finding closed or accepted in a DEC |
| | Management review of this plan; decisions D1–D10 | Founders, managers | Decisions recorded |
| | Commission the three legal opinions (§1) | Founders | Firms engaged with dates |
| | Confirm voice and A2P stay out of scope as the Third Amendment starts (~18 Oct) | Product | Written in the decision record |
| **Weeks 1–3** (to 1 Nov) | M202: verification sprint V1–V11, starting with TRAI and DPDP texts | Analyst | Each claim confirmed, corrected or marked "do not use" |
| | Publish the holdout glossary and grade vendor claims against it (M200) | Strategy | One page, ready for sales |
| | M200: mandate schema draft; M201: missing policy rules | Engineering | Schema reviewed; rules tested |
| **Weeks 2–4** (to 8 Nov) | Prospect stack survey: CEP? decisioning engine? DLT entity? WABA? send-log export? | Minfy team | Ledger rows for ≥5 prospects |
| | Hand-off dry run with a hand-made contract file in the pilot's tool | Minfy + engineering | Load time and key-match rate recorded |
| | **Gate G0** | Managers | Evidence in §4.1 met |
| **Weeks 4–6** (to 22 Nov) | M203: first deployment in the pilot's account; holdout source-of-truth agreement; send-log clause | Engineering + Minfy cloud | Deployed; agreements signed |
| | Watch DPDP: consent-manager rules start 13 Nov; check for a MeitY compression notice | Strategy | Watch note filed |
| | Watch Third Amendment Regs 4, 6, 8(a), 18 (~17 Nov) | Strategy | Watch note filed |
| **Weeks 4–10** (to 20 Dec) | M204–M206: contract, SDK, hand-off with release rule | Engineering | Conformance suites green; a valid path delivers on test infrastructure |
| | Run Plan M's usability test (M181) | Product | Results recorded, whichever way they fall |
| | Mandate and contract template reviewed by counsel | Founders | Comments received |
| **Weeks 8–13** (to 9 Jan) | First approved list live at L2 (target ~week 10–11) | Minfy + engineering | Delivery receipt; treatment start recorded |
| | M207 begins: send-log import for the pilot's CEP | Engineering | First send log pulled |
| | M208 begins: mandate model and checker | Engineering | Pure checker with property tests |
| | ASCI labelling (~29 Dec) and RBI directions (1 Jan) reflected in the content gate and BFSI defaults | Engineering | Rules in configuration, dated |
| **Day 90 report** (9 Jan 2027) | To management: G0 evidence; list live or the reason not; legal opinions; usability result; V1–V11 outcomes; any change to this plan | Strategy | Report delivered |

---

## 8. Build versus partner (report §7.5)

| Situation | What we do | Milestones |
|---|---|---|
| Client runs Braze Decisioning Studio or MoEngage Merlin / Aampe | **Govern their engine** and audit it against a no-message arm we own | M220, M221 |
| Client has a CEP without decisioning (WebEngage, CleverTap without CleverAI, Netcore, SFMC) | **Route A:** our policy decides; their CEP sends | M214 |
| Client has only its own rails (SES, an SMS aggregator, a WhatsApp BSP) | **Route B:** our dispatcher in the client's AWS account, on the client's DLT entity, WABA and domain | M215, M216 |
| Client has nothing | Not a target. Route to a CEP partner or a Minfy services contract | — |
| Decision algorithms | Build the small core: Thompson sampling or softmax with floors, and doubly robust evaluation | M210, M211 |
| Policy engine | Keep Python rules; compile to Cedar only if a client asks for gateway enforcement | M222 |
| Event backbone | SQS or EventBridge on AWS; Kafka share groups where a client already runs Kafka | M217 |
| Insurance | Affirmative AI cover with an unsolicited-communication carve-back, before route B | M215 (D5) |

---

## 9. What changes in existing decisions if D2 is approved

D2 amends three standing rules (report §7.8). Each amendment is a new DEC in Plan N's range. The old entry is cited,
never rewritten. Nothing changes before G2 passes.

| Standing rule | What it says today | After D2 (from G2) | What stays exactly as it is |
|---|---|---|---|
| **K1** (Plan K §1.2) | No agent may send a message or write into a client's systems. Agents propose; a person approves. | One exception: the deterministic dispatcher (M215) may act inside a valid, signed mandate on the client's rails. | Every language-model agent, the planner and the MCP server still never send. K5 holds: MCP has no write, send or approve tool. |
| **DEC-1100** (Plan H, M80) | A connection imports a snapshot. The platform never writes to a client's systems. | Two named write paths: the J_LAYER Destination protocol (hand-off, route A) and the dispatcher (route B). Both are mandate- or release-gated. | Source connections stay read-only. No write goes through a `Connector`. |
| **Plan J layer §7** and its no-SEND design-rule test (J_LAYER M90) | No message sending in Plan J; no per-vendor push code; no CEP credentials. The designed test `test_no_journey_or_send_primitives` fails on any send call under `engine/`. | Sending stays out of Plan J. Plan N's `engine/dispatch/` is the only place a send call may appear. The test is written with that one allow-listed path. | Journeys stay forbidden: waits, branches, triggers and follow-ups. No Mode O rails. |

**Notes.**

- The no-SEND test is designed (J_LAYER M90) but is not yet in `tests/integration/test_design_rules.py`. Plan N writes
  it with the dispatcher allow-list in M215, if the integration plan has not written it first.
- The governors (M220, M221) need partner API access to pause an engine. That is a CEP credential, which J_LAYER
  §7 forbids "until a paying client cannot ingest" files or tables. D9 covers the posture; the credential needs its own
  sub-decision in M220.
- The J_LAYER "starter" decision (J_LAYER M120) also concerns sending. Route B is not that starter: it is
  mandate-gated, on client rails, and unattended only after G2. Open question 4 asks how the two relate.

---

## 10. Rules of work

- **Claim first.** M200 adds the DEC-1700…1799 row to `PARALLEL_WORK_PROTOCOL.md` §4 before its first entry. Each
  milestone records one DEC entry with lettered sub-decisions.
- **Shared files.** A `PLAN-N (autonomy)` block follows `PLAN-M` in every shared file; `PLAN-N` joins `PHASES` in
  `tests/unit/test_shared_file_markers.py`.
- **Ownership (proposed).** Plan N owns:
  - `engine/autonomy/**` and `engine/dispatch/**` (new);
  - `api/routes/autonomy.py` (new) and `ui/modules/autonomy/**` (new);
  - `configs/autonomy/**`, `tests/**/{autonomy,dispatch}/**` and `docs/AUTONOMY.md` (new).
- **Edits in other plans' areas** are backward-compatible and announced in `docs/CROSS_BRANCH_REQUESTS.md`: Plan K's
  `engine/agentic/{policy,ledger,canonical}.py`; Plan J's `engine/measurement/harm.py`, `engine/uplift/ope.py`,
  `engine/holdout/`. J_LAYER milestones (M203–M207, M214) follow that design's ownership of `engine/activation/`.
- **Frozen files stay frozen.** Plan N needs none of `engine/stages/{train,evaluate,explain}.py` or the champion rule.
- **Off by default.** A new `autonomy.enabled` switch defaults to false. With it off, every existing output is
  byte-identical (golden files unchanged).
- **Error codes** go in `engine/autonomy/codes.py` (`PLAN_N_CODES`), with `configs/pilot/help.yaml` entries in the same
  change.
- **Every route has a RoutePolicy.** Halt and resume are Admin-only and audited.
- **Every external call sits behind a protocol with a fake,** including every channel adapter and partner API. Live
  sends are opt-in test markers; the fast suite sends nothing.
- **`make lint test` before every commit; `make test-all` before every merge.** Never skip, delete or loosen a test.

---

## 11. What we will deliberately NOT build (report §7.6)

- **An unbounded L5 agent** that sets goals, total budgets, audiences or content with no envelope.
- **Our own sending rails (Mode O):** no DLT telemarketer registration, no WABA of our own, no shared IP pools or
  domains.
- **LLM-authored SMS or WhatsApp text sent without registration.** Selection only.
- **Any LLM in the inner loop,** and any LLM that sends. LLMs draft, plan and explain under claim checks.
- **Online per-user reinforcement learning or real-time bandits.** Batch, per-cycle stochastic choice only.
- **Autonomous discount depth, new offers, total budget, price, eligibility or credit decisions.**
- **Voice agents or A2P calls,** until per-client declaration is solved and a client asks.
- **Consent-seeking messages** outside the Digital Consent Acquisition flow.
- **Journeys:** waits, branches, triggers and journey builders stay with the client's CEP.
- **Cross-client pooled training** without a separate lawful purpose and contract.
- **Open-based rewards,** synthetic personas as lift predictors, and LLM causal judgements.
- **Multi-agent writers.** One dispatcher commits actions.
- **Per-message or per-action pricing.**
- **The words "fully autonomous" in sales material.** Say "autonomous within your mandate" (K9; D10).

---

## 12. Honest limits (report §6.4)

- **No client pilot and no client deployment.** All validation is synthetic or on public data.
- **Hillstrom showed no gain from uplift or from Plan K's upgrades.** The risk model's ROC-AUC is 0.534. Uplift did not
  beat risk ranking.
- **The targeted list beat sending nothing** (+0.45 points off-policy, +0.60 in replay). It lost on conversions to
  sending everyone the men's e-mail: −0.24 points (−0.43 to −0.04).
- **Plan K's re-run on Hillstrom changed nothing.** The rollout verdict was "cannot tell".
- **The usability test (M181) is pending.**
- **Most autonomy-relevant features are off by default** and validated mostly on synthetic data.
- **The effort figures are unmeasured estimates** (report §7.3), and most market and legal claims rest on search
  summaries (report, "Read this first").
- **No public case exists of an autonomous CRM agent mass-sending wrong messages.** That is likely because CRM sends
  are still human-launched (report §5.5). We would be early, not proven safe.

None of this argues against the path. It sets the order (report, executive summary).

---

## 13. Risks and mitigations (report §7.7)

| Risk | Likelihood (report's inference) | Mitigation |
|---|---|---|
| **No pilot client signs soon,** so G1 slips and everything behind it | High | Phase 2 builds on the demo library and synthetic data; only its gate needs real cycles |
| **Sending undermines neutrality** ("you marked your own homework") | Medium | Client-owned holdout salt; client rails; a verifier a third party can run (M219, M224); step back to route A if CFOs discount readouts |
| **One bad unattended run costs the client its rails** | Low per run, severe | Breakers well below TRAI and WhatsApp triggers (M218); template-only sends (M216); re-check at dispatch; small first mandates (D7) |
| **Shadow autonomy shows no gain over the L2 list** | Medium–high | "Cannot tell" is an outcome (M211); the envelope stays narrow or stays at L2; we report it, as we did for Hillstrom |
| **Regulation reading is wrong** (V4–V6) | Medium | Rules in dated configuration; the verification sprint (M202); three legal opinions (D5) |
| **DPDP full duties start on 13 Nov 2026** if MeitY compresses | Low–medium | Consent-manager intake and per-send consent proof designed now |
| **Braze or MoEngage ships a neutral-looking holdout report first** | Medium | Ship the glossary (M200), verifier bundle (M219) and governors (M220, M221); position as their auditor |
| **Data scientist overloaded** in Phase 2 | High | Part-time second data scientist (K10, D8) |
| **Mid-market volumes too thin** for per-envelope certification | High | Decide only where data supports it; use Adobe's documented floors as a public benchmark |
| **Agent misuse through MCP or prompt injection** | Medium | No write tools on MCP; the dispatcher is not a tool; Plan G's egress gate and canaries |

---

## 14. Open questions for management

1. Do you accept D1–D10 as recommended (§1)? D1 and D2 are needed before anything is built.
2. May the verification sprint (M202) and the legal opinions (D5) start before D1 and D2? Neither is build work.
3. Who is the pilot candidate for G0, and what is its stack (CEP, decisioning engine, DLT entity, WABA, send-log export)?
4. How does route B relate to the J_LAYER starter decision (J_LAYER M120)? Is that gate's evidence also route B's?
5. Should the integration plan be written first, so Phase 1 (M203–M207) moves into it?
6. Will Braze and MoEngage give a third party enough API access to govern their engines (report §8.2, question 6)?
7. Whose account holds the dispatcher's credentials in route B, and how does the client revoke them (report §8.2,
   question 10)?
8. What is the safe state per use case: stop, or revert to business as usual (report §8.2, question 9)?
9. Can mid-market Indian volumes certify any envelope within a quarter (report §8.2, question 8)? Hillstrom suggests thin
   data often says "cannot tell".
