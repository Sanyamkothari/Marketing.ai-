# Marketing AI — Plan K: Agents that check, not agents that send

**Companion to:** Plan J (`MARKETING_AI_PLAN_J_PRODUCT.md`), the hand-off design
(`MARKETING_AI_PLAN_J_LAYER.md`), the research behind this plan
([`docs/research/AGENTIC_AI_OPPORTUNITIES_2026_10.md`](../research/AGENTIC_AI_OPPORTUNITIES_2026_10.md), with its notes in
`docs/research/agentic_ai_2026_10_notes/`), the market research and the positioning memo
(`docs/research/MARKET_RESEARCH_2026_10.md`, `docs/research/POSITIONING_DECISION_2026_10.md`), Plan G's agent rules
(`docs/AGENTS.md`) and `PARALLEL_WORK_PROTOCOL.md`.
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1400 … 1499 (proposed; claimed by M130 once the plan is approved)
**Milestones:** M130 – M144 (M112 – M129 are left for the integration plan, which is not written yet)
**Status:** proposal for the manager review, written on 10 October 2026. Nothing in it is built until it is
approved, **except M131**, which this plan recommends doing now because the rule it follows takes effect in
late October 2026 (§1.2, K3).

---

## Plan K at a glance

**The decision we propose.** Marketing AI does not become one more "autonomous marketing agent". That
market is crowded and buyers already distrust it. Instead, Plan K adds **agents that check**:

- they plan and propose, and a person approves;
- they never send a message and never write into a client's systems;
- every tool they call is a deterministic function Plan J already ships;
- every number they show comes from one of those tools;
- the same tools are offered to **other companies' agents** through MCP, so those agents can ask
  Marketing AI "is this worth doing, and is it allowed?" before they act, and "did it work?" afterwards.

Plan K also makes measurement faster, so a pilot reaches a confident answer with fewer customers. And it
turns India's tightening rules (TRAI's Third Amendment, DPDP) into checks the product runs before every
send.

| Phase | When | What the product can do at the end | Milestones | Effort (person-weeks) |
|---|---|---|---|---|
| **1 — Safe, compliant, callable** | gate at week 6 | It follows TRAI's Third Amendment for calls, inquiry consents and legacy consents. Every agent action is a proposal in a signed, hash-chained log, and limits are enforced by the server, not by a prompt. A read-only MCP server answers other agents' questions on the demo library. A pre-send gate records, per customer, channel and purpose, why a message may or may not go. | M130–M134 | 12.5 |
| **2 — Prove faster** | gate at week 11 | Smaller holdouts for the same confidence (ML covariates). Honest estimates for small campaigns (pooling across campaigns). A "safe to roll out" check in rupees. An early alarm when an offer is doing harm. | M135–M138 | 10.0 |
| **3 — The planner and the verifier** | gate when the integration plan's first measured cycle is in, about week 16–20 | A warning card or a goal becomes a drafted, approvable campaign plan. Message variants are tested against the control group. Clients' own agents can call the verifier on their data. Setup helps map integration sources. | M139–M142 | 12.0 |
| **4 — Only with evidence** | no fixed week | A tabular foundation model challenger, shipped only if the library shows a gain in rupees. A save-desk card for call-centre staff, shipped only after a randomised pilot. | M143–M144 | 4.5 |

**Team assumed:** 3 engineers and 1 data scientist dedicated to Plan K, beside the integration plan's own
team. Phases 1–3 need about 39 person-weeks in total (30 engineering, 9 data science, §6.2). The calendar is
set less by effort than by **one dependency**: Phase 3 needs the first campaign that has gone through the
integration plan's hand-off and been measured. All estimates are ours and unmeasured.

**What the managers must answer first:** the decisions K1–K12 (§1.2).

---

## 1. The decision this plan implements

### 1.1 Why "agents that check"

The research (October 2026, with its key claims checked against primary sources) found four things.

1. **Buyers want proof, not more autonomy.**
   - Gartner (29 Oct 2025, 413 martech leaders): 45% of those with AI agents in pilot or production say
     vendor-offered agents do not meet the business performance promised.
   - Gartner (18 Sep 2024, 378 senior marketers): only 52% can prove marketing's value and get credit for it.
   - TransUnion (21 Oct 2025, 196 US marketers): 67% now prioritise proving incremental ROI.
   - Dentsu (2026, 1,950 marketing leaders): 46% of Indian CMOs are uncomfortable handing high-stakes tasks to
     AI agents for reputational reasons (63% globally).
2. **The competitors' own evidence shows engagement, not money.**
   - Aampe, which MoEngage bought on 24 June 2026, ran an 11-month randomised trial on 8.8 million users. It
     reports +65.3% in days with push-driven app opens, but only **+0.3% (±0.24) in days with conversions**.
     The trial was written by Aampe's own staff, on one anonymised app.
   - Mercari tested AI-written e-mail titles on over a million lapsed users at random: taps rose, and
     purchases did not move measurably.
   - Braze (OfferFit) offers a control group as one option, and publishes headline lifts with no method.
     Netcore.ai ties its fees to "agreed business KPIs" and discloses no measurement method.
3. **Agents are not yet reliable enough to act alone.** On repeated retail tasks, success falls from about
   61% on one try to about 25% when the same task must succeed eight times in a row (tau-bench, 2025-era
   models). Gartner forecasts that over 40% of agentic AI projects will be cancelled by the end of 2027.
4. **The rules are tightening on exactly the channels we decide.**
   - TRAI's Third Amendment (notified 18 Sep 2026, in force 30 days after Gazette publication):
     - automated and AI-voice calls must be declared to the operator in advance;
     - AI spam flags plus three complaints in ten days trigger action;
     - inquiry-based messages are allowed for seven days only;
     - misused headers or templates are suspended within six hours;
     - legacy consents count only if they are verifiable and registered with the operators.
   - DPDP: consent-manager rules apply from about 13 November 2026, and the main duties from about 13 May 2027.

What none of the vendors checked offers is what Plan J already has:

- a randomised holdout that is never shrunk away;
- results in rupees with 95% ranges;
- an honest Causal / Estimated / Not proven label;
- independence from the platform that sends the messages.

Neutral measurement is also being absorbed: Smartly signed to acquire INCRMNTAL on 17 March 2026. **The
neutral seat — the system agents call to check their work — is open, and Plan J built most of it.**

The Hillstrom validation (M110) is part of the argument, not a weakness to hide. Uplift did not beat plain
risk ranking there, and the product said so and used risk ranking. Plan K must never be sold as a fix for
that result. It is sold as the system that reports results like it honestly.

Several research figures could not be checked against primary sources. The research report's
"Verification status" table lists them as **Hold**, and this plan does not rely on any of them.

### 1.2 Decisions to confirm with the managers

| # | Question | Recommended answer |
|---|---|---|
| K1 | May an agent ever send a message or write into a client's systems? | **No.** Agents propose; a person approves. The integration plan's hand-off is the only path out of the product, under its release rule. DEC-1100 stays in force. |
| K2 | Does Plan K need an AI service to work? | **No.** As in Plan G, plans, checks and numbers come from rules and tested code. The language model only words a plan or a summary, and every number it writes must be grounded (`numbers_grounded`). With no AI service connected, every feature still works without the wording. |
| K3 | Do we build M131 (TRAI Third Amendment, the measurement purpose) **now**, before the review? | **Yes.** The rule takes effect in late October 2026, and M131 is small and opt-in. It can be built under Plan J's range as a follow-up if Plan K is not yet approved. |
| K4 | Do we add "measuring the effect of our messages with randomised comparison groups" as a consent purpose now, and commission an Indian law-firm opinion on hold-outs under DPDP? | **Yes to both.** No guidance on hold-outs exists. The notice wording costs nothing now and is enforceable from May 2027. Until the opinion arrives, no material calls hold-outs "DPDP-safe". |
| K5 | Is the MCP server read-and-propose only? | **Yes.** It has no tool that sends, writes to a client system or approves. It starts on the demo library (M133) and reaches client data only after the first integrated cycle (M141). |
| K6 | Which AI service runs the agents' wording? | **Amazon Bedrock in the client's account by default**, so data stays there. A third-party service is allowed only through Plan G's default-deny egress gate, as today. |
| K7 | How are faster-measurement figures shown? | **As "Estimated", beside the raw randomised figure, never replacing it.** Only the measured lower bound counts as "proven value to date". |
| K8 | Do we buy a Prior Labs licence for TabPFN-2.5 and later? | **No, not now.** Their licences bar commercial use, including using their outputs for business decisions. M143 uses only AutoGluon's commercial-safe `extreme_quality` models, and only if the library shows a gain. |
| K9 | Do we say "agent" to buyers? | **No.** The market research advised against it, and the distrust data supports that. Buyers hear "checks", "planner" and "verification". "Plan K" and "agent" are internal names. |
| K10 | Do we get a second data scientist? | **Yes, part-time for Phase 2.** Plan J's data scientist was the bottleneck, and Phase 2 is mostly statistics (§6.2). |
| K11 | Do we offer pricing tied to proven value? | **As a renewal option, not the lead model.** In surveys, buyers prefer consumption pricing (43%) to outcome pricing (27%) (Futurum, 2026). A fee settled on the Proof Pack's measured lower bound is distinctive, but it is a commercial decision for the founders. |
| K12 | When does Plan K start? | **Phase 1 alongside the integration plan, as soon as both are approved.** Phase 3 waits for the integration plan's first measured cycle. |

### 1.3 What stays out of Plan K

- **Any agent that sends, or any write into a client's system** (K1). The integration plan owns the hand-off.
- **Per-customer reinforcement learning or real-time bandits.** At our volumes and campaign frequency, even
  vendors that sell bandits advise A/B tests. Learning between monthly cycles, with a random explore slice
  (Plan J M92 and M106), is the right design.
- **A language model deciding cause and effect.** Language models may list questions to ask, such as
  possible confounders or leaks, labelled "Not proven". They never identify an effect.
- **Simulated customers ("synthetic personas") used to predict lift.** Studies find them more positive and
  less varied than real people. At most they may screen message drafts (M140).
- **Becoming a registered DPDP consent manager.** The ledger exports purpose-bound records that a registered
  consent manager can use; integrating with consent managers belongs to the integration plan, from 2027.
- **A2A (agent-to-agent protocol).** No marketing vendor was found using it. It stays on the watch list.
- **Free SQL written by a model on client data** (DEC-1256 holds).

---

## 2. What the product can do at the end of each phase

| Phase | A user can | We demonstrate | Exit gate |
|---|---|---|---|
| **1 — Safe, compliant, callable** | Mark a call channel as declared to the operator. See inquiry-based consents expire after seven days. See which legacy consents are registered. Record "measurement with comparison groups" as a purpose. See, for every planned contact, the signed reason it may or may not be sent. Connect an MCP client (Claude, ChatGPT or an in-house agent) to the demo library, and ask it to plan a test, value a segment or audit a campaign. | Every agent proposal sits in a hash-chained log that a test tampers with and catches. A policy refusal is enforced by the server even when the prompt asks otherwise. The same scenario run ten times gives the same plan every time (pass^k). | M130–M134 merged; `make test-all` green; the TRAI rules have regression tests that fail on today's `main`. |
| **2 — Prove faster** | See how much a prediction covariate shrinks a test's range, measured on their data. See a small campaign's pooled estimate beside its raw result. See "safe to roll out", "not safe" or "cannot tell at this size" in rupees before replacing a policy. Get an alert when an offer is doing harm, without early "win" verdicts. | On Hillstrom and the simulator: the variance reduction actually achieved, reported whichever way it falls; pooled intervals that cover 95% in the nightly suite; a harm alarm that fires on planted harm and rarely on none. | M135–M138 merged; the nightly coverage and false-alarm bands green. |
| **3 — The planner and the verifier** | Click a warning card and get a drafted plan (test plan, treat-list run, arbitration, measurement date) to approve in one step. Test approved message variants against the control template and the no-message group. Let their own agent call the verifier on their data, under their sign-in. Get suggested mappings for a new source. | One real integrated cycle planned, approved, handed off, measured and reported through the planner. A client's agent auditing that campaign through MCP. | M139–M142 merged; one integrated cycle measured; pass^k and claim-check tests green. |
| **4 — Only with evidence** | Use a tabular foundation model where it wins. Use a save-desk card. | A rupee gain at equal budget in the library (M143). A randomised frontline pilot (M144). | Each ships only when its own evidence gate passes. |

---

## 3. Principles and rules

### 3.1 How every Plan K agent is built

1. **Propose, never act.** An agent calls read tools and proposal tools. There are no write tools. A
   proposal becomes real only when a person approves it in `engine/approvals.py`, which the server
   enforces, whatever a prompt says.
2. **The tools are the product.** Each tool is a typed, tested function that already powers the API: the
   test planner, `measure_campaign`, the holdout service, contactability, the catalogue, net value, the audit
   and the Proof Pack. The agent adds no new statistics. Plan K's statistics (Phase 2) are ordinary engine
   code with ordinary tests.
3. **Limits live in the server.** Budget, consent, contact frequency and the holdout are checked by policy
   code with **default deny** before any proposal is shown. An agent cannot propose shrinking or skipping
   the holdout.
4. **Every number is grounded.** Plan G's `numbers_grounded` and claim checks apply to every sentence an
   agent writes. A sentence that fails is replaced, never shown.
5. **Every decision is on the record.** Each proposal, approval, refusal, holdout reservation and evidence
   label goes into an append-only log. Each entry carries the hash of the one before it, and approvals and
   test plans are signed. This follows the "signed mandate" idea from the agent payments protocol (AP2).
6. **Consistency is tested.** Every planning scenario is run *k* times and must give the same decision
   every time (pass^k, borrowed from tau-bench). This is added to the M95 validity harness.
7. **Untrusted content stays data.** Customer text, file content and anything another agent sends through
   MCP are untrusted. Plan G's egress gate and untrusted-content rules apply unchanged. MCP results are
   schema-validated JSON with no third-party prose in them.
8. **Plan J's principles still hold:** nothing fabricated, opt-in and additive change, money as the
   headline, config over code, and Plan H's simple screens.

### 3.2 Parallel-work protocol rules that apply

- **Claim first.** M130 adds `DEC-1400…1499 | Plan K — agents that check (M130–M144)` to
  `PARALLEL_WORK_PROTOCOL.md` §4. Each milestone records one DEC entry with lettered sub-decisions.
- **Shared files.** M130 adds a `---- PLAN-K ----` block to the shared files and `PLAN-K` to `PHASES` in
  `tests/unit/test_shared_file_markers.py`.
- **Frozen files stay frozen.** These are `engine/stages/train.py`, `evaluate.py`, `explain.py` and the
  champion rule in `engine/registry.py`. Plan K needs none of them.
- **Ownership.** Plan K owns:
  - `engine/agentic/**` (new) and `engine/compliance/**` (new);
  - `api/routes/{agentic,mcp}.py` (new) and `ui/modules/agentic/**` (new);
  - `configs/agentic/**` and `tests/**/{agentic,compliance}/**`;
  - `docs/AGENTIC.md` (new).

  Edits in Plan J's areas (`engine/measurement/**`, `engine/decide/**`, `engine/holdout/**`) and Plan G's
  (`engine/agent/**`) are listed per milestone as backward-compatible edits, announced in
  `docs/CROSS_BRANCH_REQUESTS.md`.
- **Error codes** go in `engine/agentic/codes.py` (`PLAN_K_CODES`), with `configs/pilot/help.yaml` entries
  in the same change.
- **Every route** has a `RoutePolicy`, and every MCP tool maps to one. Row-level reads are audited.
- **Every external call sits behind a protocol with a fake**, including the MCP transport and any AI service.
  The fast suite needs no network.
- **`make lint test` before every commit, `make test-all` before every merge.** Never skip, delete or loosen
  a test.

---

## 4. Phases

**Effort basis.** 3 engineers and 1 data scientist on Plan K. Engineering keeps back 0.5 person-weeks a week
for reviews and fixes, leaving **2.5 engineer-weeks of milestone work per week**. The data scientist keeps
back 0.2, leaving **0.8**. Figures include tests, docs, the DEC entry and shared-file paperwork. "DS n" is
the data-science share inside the figure. All estimates are ours and unmeasured.

### Phase 1 — Safe, compliant, callable (gate at week 6)

**Goal.** The rules that take effect this month are followed. The base every agent stands on exists. Other
agents can try the verifier on demo data.

#### M130 — Plan K set-up

- **Scope.** Claim the DEC range. Add the shared-file blocks, `PLAN_K_CODES`, README rows (pending) for
  M130–M144 and `docs/AGENTIC.md` (skeleton). Add a `PLAN-K` entry to `MILESTONE_TESTS`.
- **Acceptance.** `test_shared_file_markers.py` and `test_help.py` green; nothing else changes.
- **Effort:** 0.5 engineer-weeks. **Depends on:** plan approval.

#### M131 — TRAI Third Amendment, and the measurement purpose (do now; see K3)

- **Why.** The amendment takes effect about 30 days after its Gazette publication of 18 September 2026. Four
  of its rules touch what the product decides. Separately, hold-outs need a named consent purpose before
  DPDP's notice duty applies in May 2027.
- **Scope.**
  - **Calls:** a catalogue action on a call channel carries `a2p_declared: true | false`. An undeclared
    automated-call action is not contactable, and the reason says why (`CALL_NOT_DECLARED`).
  - **Inquiry consents:** the consent ledger gains `basis: explicit | inquiry | legacy`. An inquiry consent
    stops counting seven days after it was recorded, by the region rule (`configs/regions/in.yaml`), never
    by code that names India.
  - **Legacy consents:** a ledger row may carry `operator_registered: true | false | null`. Under the India
    rule, an unregistered legacy consent does not make a customer contactable for promotional messages. A
    null stays null and the reason says "registration not recorded"; it is never counted as registered.
  - **Template suspension:** a catalogue action's DLT template can be marked suspended (by import or by
    hand). A suspended template's action is not contactable until it is cleared.
  - **The measurement purpose:** a ledger purpose `measurement_comparison_groups` and template wording for
    client notices in `docs/DECIDE.md`. No behaviour depends on it yet; it is recorded and exported.
- **Where in the code.**
  - `engine/decide/catalogue.py`, `engine/decide/contactability.py`;
  - `engine/privacy/consent.py`, `configs/regions/in.yaml`;
  - `engine/decide/codes.py` (new codes, Plan J's area), `configs/pilot/help.yaml`;
  - `docs/DECIDE.md`.
- **Acceptance.**
  - On fixtures: an undeclared call action, an inquiry consent on day 8, an unregistered legacy consent and
    a suspended template each make the customer not contactable on that channel, with the reason.
  - Without the new fields, every existing treat list is byte-identical (golden files unchanged).
- **Effort:** 2.5 engineer-weeks. **Depends on:** nothing in Plan K (builds on Plan J M99).

#### M132 — The agent-safe core

- **Why.** Every later agent, ours or another company's through MCP, must stand on the same guarantees:
  proposals only, limits in the server, a tamper-evident record and repeatable plans.
- **Scope.**
  - **A tool registry** (`engine/agentic/tools.py`). Each tool is a typed function with input and output
    models and a class, `read` or `propose`. There is no `write` class. Tools wrap existing functions:
    - the test planner;
    - contactability;
    - net value;
    - `measure_campaign`;
    - the M103 audit;
    - the M104 Proof Pack;
    - the treat-list build in dry-run mode.
  - **Proposal objects** (`engine/agentic/proposals.py`). A proposal states what would be done, cites the
    tool results it rests on, and becomes real only through `engine/approvals.py` (a new proposal kind there).
  - **Policy as code** (`engine/agentic/policy.py`), default deny. It covers:
    - the budget cap (M108's estimate);
    - the consent and contactability result;
    - contact frequency (M101's caps);
    - the holdout, which can never be shrunk or skipped by a proposal;
    - roles.
  - **The decision log** (`engine/agentic/ledger.py`). It is append-only, each entry holds the hash of the
    previous one, and test plans and approvals are signed with a deployment key. It extends M94's plan hash
    and Plan D's audit events (`engine/audit/events.py`).
  - **pass^k evaluation:** a harness that runs each planning scenario *k* times (default 10) and fails on any
    disagreement, added beside the M95 validity harness.
- **Where in the code.**
  - `engine/agentic/` (new);
  - `engine/approvals.py` (a new proposal kind, backward-compatible);
  - `engine/audit/events.py`;
  - `tests/unit/agentic/`.
- **Acceptance.**
  - A proposal that would shrink the holdout or exceed the budget is refused by policy with a code,
    whatever the request says.
  - Editing any log entry breaks the chain, and the verifier names the entry.
  - Ten runs of each fixture scenario give identical proposals.
  - The registry has no tool of class `write` (a test walks it).
- **Effort:** 4 engineer-weeks. **Depends on:** M130.

#### M133 — The verifier over MCP, demo data only

- **Why.** Engagement platforms' MCP servers can now act: MoEngage's can create segments and drafts. That
  makes a "check before you act" call more valuable to every agent working in a client's tools, and the
  neutral seat is open now. A prototype on public data costs little, shows the idea to buyers and the
  managers, and touches no client data.
- **Scope.**
  - An MCP server (`api/mcp/`, using the official MCP Python SDK pinned in `pyproject.toml`). It exposes the
    M132 registry's read and propose tools for the demo library only (Hillstrom and the synthetic library):
    - `plan_test`;
    - `check_contactable`;
    - `value_segment`;
    - `audit_campaign`;
    - `get_proof_pack`;
    - `check_before_send` (the M134 decision record, once M134 lands).
  - It follows the 2026-07-28 specification:
    - stateless;
    - Tasks for long jobs such as an audit;
    - OAuth with least-privilege scopes, with sign-in required.
  - Every result is schema-validated JSON with its evidence label.
- **Where in the code.** `api/mcp/` (new), `api/access_policy.py` (a policy per tool), `docs/AGENTIC.md`.
- **Acceptance.**
  - A scripted MCP client plans a test and audits Hillstrom's campaign, and the numbers match the product's
    own screens exactly.
  - No tool can write: a test walks the tool list.
  - A tool description or argument containing an injected instruction changes nothing (canary test, as in
    Plan G).
- **Effort:** 2.5 engineer-weeks. **Depends on:** M132.

#### M134 — The pre-send compliance gate

- **Why.** Every promotional send in India must clear several checks across separate systems:
  - DPDP consent for this purpose and channel;
  - TRAI consent and its registry;
  - a valid DLT header, template and variable tags;
  - whitelisted links;
  - WhatsApp opt-in.

  The integration plan hands lists to the client's tools, so the evidence must exist before any list leaves
  the product.
- **Scope.**
  - One decision record per customer, channel and purpose, written when a treat list or hand-off is built
    (`compliance_decisions.parquet`, a row-level artefact registered in `configs/privacy.yaml` and
    `engine/privacy/layout.py`). Each record holds:
    - `allowed`;
    - the reasons;
    - the ledger rows, catalogue entry and region rule it used;
    - a signature from the M132 ledger.
  - A DLT matcher checks that an approved message's variables fit the registered template's tagged slots and
    that its links are on the client's whitelist. It is rules only; a language model may suggest which
    registered template fits, as a proposal.
  - The records feed the integration plan's DPDP evidence pack (layer design M119).
- **Where in the code.**
  - `engine/compliance/` (new);
  - `engine/decide/treat_list.py` (writes the records when the gate is on, Plan J's area);
  - `configs/privacy.yaml`, `engine/privacy/layout.py`.
- **Acceptance.**
  - On fixtures covering each rule, the decision and its reasons match the expected file.
  - With the gate off, treat lists are byte-identical.
  - Building the records is linear at 1 million rows (a timing test, best of three).
- **Effort:** 3 engineer-weeks. **Depends on:** M131, M132. It coordinates with the integration plan's
  Activation Contract.

**Phase 1 exit gate.** M130–M134 merged; `make test-all` green; the demo MCP session rehearsed once.

### Phase 2 — Prove faster (gate at week 11)

**Goal.** The same customers give a confident answer sooner. That attacks the most likely way to lose a
pilot: a test too small to show a result. It is ordinary statistics in ordinary engine code; no language
model is involved.

#### M135 — Prediction covariates (MLRATE / CUPAC)

- **Why.** M102's CUPED uses one pre-period column. For yes/no outcomes such as churn or purchase, past
  behaviour predicts the next outcome only weakly, so CUPED gains little. A cross-fitted model prediction
  used as the covariate is never worse than the plain difference, asymptotically.
- **Evidence (primary sources):**
  - Meta's MLRATE (NeurIPS 2021): more than 70% less variance than a plain difference, and **about 19% less
    than CUPED-style adjustment**.
  - Etsy (November 2025): 27% on average, against 7% from CUPED.

  Nobody has published figures for retention offers, so we promise nothing and measure.
- **Scope.**
  - `adjusted_difference` (`engine/measurement/continuous.py`) accepts a cross-fitted prediction as the
    covariate. The prediction recipe (features, folds, seed) is part of the registered test plan and its
    hash (M94), so it cannot be chosen after seeing the result.
  - The validity harness adds coverage tests for the new estimator.
  - The library re-runs Hillstrom and records the variance reduction achieved, whichever way it falls.
- **Where in the code.** `engine/measurement/{continuous,plan,simulate}.py`, `library/hillstrom-email/`,
  `tests/statistical/`.
- **Acceptance.**
  - The nightly suite shows 95% coverage, and false positives within band.
  - With a useless covariate, the interval is no wider than the plain one (within tolerance).
  - Hillstrom's report states the measured reduction.
- **Effort:** 3 person-weeks (DS 2). **Depends on:** M130.

#### M136 — Pooling across campaigns

- **Why.** With effects of 0.5–3 points and noise of the same size, each small campaign's lift is mostly
  noise, and the best-looking one is biased upwards (the "winner's curse"). Empirical-Bayes shrinkage
  towards similar past campaigns corrects this. Recent work recommends pooling **locally**, across comparable
  campaigns only.
- **Scope.**
  - A pooled estimate per campaign and arm, using comparable campaigns only (same use case, outcome and
    channel), with its interval.
  - The pooled figure is shown beside the raw randomised result and labelled "Estimated", never replacing it
    (K7).
  - It feeds M105's warnings and M106's prior.
  - "Proven value to date" stays the sum of measured lower bounds.
- **Where in the code.** `engine/measurement/pooling.py` (new), `engine/measurement/{summary,learn}.py`,
  `engine/pilot/proof.py` (the Estimated line).
- **Acceptance.**
  - On the simulator, pooled intervals cover 95% and are narrower than the raw ones on small campaigns.
  - With one campaign, nothing is pooled and the reason is shown.
  - The Proof Pack's provenance test stays green.
- **Effort:** 2.5 person-weeks (DS 2). **Depends on:** M130.

#### M137 — "Safe to roll out": a lower bound in rupees

- **Why.** Plan J's rule J5 falls back to risk ranking when uplift does not beat it. A CFO needs that answer
  as money with a guarantee: "the new list is worth at least ₹X more than the current one, with 95%
  confidence", or "we cannot tell at this size".
- **Scope.**
  - The doubly robust estimate already in `engine/uplift/ope.py` gives a one-sided lower bound on the
    candidate policy's net value against the current policy (risk ranking or business as usual).
  - Three outcomes: `safe`, `not_safe` and `cannot_tell` (the size needed is shown, from the planner).
  - J5 and M106's challenger approval show this result. The approval rule itself is unchanged; this is
    evidence for the Approver.
- **Where in the code.** `engine/uplift/{ope,checks}.py`, `engine/approvals.py`, `ui/modules/production/approvals.js`.
- **Acceptance.**
  - On the simulator, `safe` is wrong at most at the stated rate.
  - On Hillstrom, the result is reported whichever way it falls (`cannot_tell` is expected).
- **Effort:** 2.5 person-weeks (DS 1.5). **Depends on:** M130.

#### M138 — An early harm alarm

- **Why.** M94 refuses verdicts on early looks, which is right for "did it work?". But an offer that is
  doing harm should be stopped early. Statsig's 50,000 A/A tests show the danger of naive peeking: 17.3%
  false alarms, against 1.1% with a sequential test.
- **Scope.**
  - A one-sided sequential test for **harm only** (mSPRT), run by the M107 loop at each data refresh.
  - When it fires: a warning card and an alert. Nothing stops automatically; a person decides.
  - Wins are still read only at the planned date.
- **Where in the code.** `engine/measurement/{measure,summary,cycle}.py`, `engine/scheduling/` alerts.
- **Acceptance.**
  - On planted harm, the alarm fires.
  - On no effect, the false-alarm rate stays within its band across the nightly suite.
  - No early "win" wording appears anywhere (a test greps the outputs).
- **Effort:** 2 person-weeks (DS 1). **Depends on:** M130.

**Phase 2 exit gate.** M135–M138 merged; the nightly statistical suite green; Hillstrom's report updated
with the measured figures.

### Phase 3 — The planner and the verifier (gate when the first integrated cycle is measured)

**Goal.** Agents earn their place where people lose time: turning a finding into a next step, testing
message drafts properly, letting clients' own agents check their work, and mapping new sources. Phase 3
starts its client-facing parts only after the integration plan's first campaign has been handed off and
measured. An agent is only as credible as the first real Proof Pack behind it.

#### M139 — The campaign planner

- **Why.** M105's warning cards say what needs attention, and M107's loop runs the cycle. Between them, a
  person still works out the next step by hand. The research found this is where findings stall: 49% of B2C
  decision-makers say analytics do not turn into action. That figure comes from a truncated secondary
  snippet; treat it as indicative.
- **Scope.**
  - From a warning card or a typed goal ("win back lapsed customers in Mumbai within ₹2 lakh"), the planner
    drafts one proposal with:
    - a test plan with power and holdout from the planner;
    - the use case and offer set;
    - the treat-list build;
    - arbitration with live campaigns;
    - the measurement date;
    - the cost estimate.
  - The draft is built by rules from the M132 tools. The language model (optional, K2) only writes the
    summary sentence under claim checks.
  - One click sends it to approval. The planner also narrates a finished Proof Pack in plain words, under
    the same checks.
- **Where in the code.** `engine/agentic/planner.py` (new), `api/routes/agentic.py`, `ui/modules/agentic/`
  (inside Results and the warning cards; no new top-bar item, per Plan H).
- **Acceptance.**
  - Each warning-card fixture yields the expected draft.
  - pass^k holds across the scenario set.
  - No sentence with an ungrounded number reaches the screen.
  - With no AI service connected, the same drafts are produced without the summary sentence.
- **Effort:** 4 person-weeks (DS 0.5). **Depends on:** M132, M137; the integration plan's first measured
  cycle for the gate.

#### M140 — Message variants as measured arms

- **Why.** `engine/generative/win_back.py` already writes variants A–D per band, but nothing assigns them at
  random or measures them. The published randomised evidence is sobering:
  - Nextdoor: about +1% sessions with reward-model reranking, and −6.5% for plain model output.
  - Mercari: no measurable effect on purchases.

  So variants are worth having only if each is tested.
- **Scope.**
  - The flow is: approved variants are screened by the guardrails, registered as DLT templates for SMS, and
    entered in the catalogue. They then become M100 treatment levels, randomised against the control
    template and the no-message holdout.
  - `measure_campaign` reports each arm, and the Proof Pack shows the winner only when its interval says so.
  - Simulated personas may be used for screening drafts only, and are labelled as such.
- **Where in the code.** `engine/generative/win_back.py`, `engine/decide/{catalogue,offer_run}.py`,
  `engine/measurement/arms.py`.
- **Acceptance.**
  - On the simulator, a planted best variant is found within its interval.
  - With one variant, behaviour is unchanged.
  - Every SMS variant in a treat list has a registered template id, or the build refuses it.
- **Effort:** 3 person-weeks (DS 1). **Depends on:** M131 (template status); Plan J M100.

#### M141 — The verifier on client data

- **Why.** M133 proves the idea on public data. This makes it real for a client whose own agent (Claude,
  ChatGPT, Agentforce or an in-house agent) needs to check its work against Marketing AI's holdout and
  measurements.
- **Scope.**
  - The M133 server runs against a deployment's own data:
    - sign-in and per-client scopes;
    - audited row-level reads;
    - Tasks for long audits;
    - rate limits.
  - Each answer carries its evidence label and the log entry it was recorded under.
  - Still no write, send or approve tool (K5).
- **Where in the code.** `api/mcp/`, `api/access_policy.py`, `engine/audit/`.
- **Acceptance.**
  - A client agent audits the first integrated campaign and gets the same figures as the Proof Pack.
  - A Viewer-scope token cannot read rows.
  - The injection canary stays green.
- **Effort:** 3 engineer-weeks. **Depends on:** M133, M134; a pilot client that asks for it.

#### M142 — Setup help for integration sources

- **Why.** Plan G's Guided setup already works by suggest-and-confirm. The integration plan brings new
  sources (send logs, CRM exports), and Indian exports often have Hinglish or abbreviated column names that
  no benchmark covers.
- **Scope.**
  - Schema-mapping candidates (top three, with confidence) for integration sources.
  - Drafts of outcome-label definitions, shown with example customers.
  - A language-model "leak reviewer" that adds questions beside the deterministic `LEAKAGE_SUSPECTED` check,
    never replacing it.
  - Hinglish and abbreviated-header cases added to Plan G's benchmark.
- **Where in the code.** `engine/agent/{advisor,recommend}.py` (Plan G's area), `tests/fixtures/agent_bench/`.
- **Acceptance.**
  - The benchmark diff is reviewed.
  - No suggestion is applied without a person's tick.
  - The egress canary stays green.
- **Effort:** 2 person-weeks. **Depends on:** M132; the integration plan's source formats.

**Phase 3 exit gate.** M139–M142 merged; one integrated cycle planned through M139, measured and audited
through M141.

### Phase 4 — Only with evidence

#### M143 — Tabular foundation model challenger

- **Why.** AutoGluon 1.6.3, which the product already pins, includes tabular foundation models. They do
  best on small data, which suits small clients and the thin per-offer data in M100. Today the presets map
  to `medium_quality`, `good_quality` and `best_quality`, so they are unused.
- **Scope.**
  - Add AutoGluon 1.6's commercial-safe `extreme_quality` preset (Nori, TabICLv2 and TabDPT-Turbo) as an
    opt-in strategy, behind a licence allow-list that refuses TabPFN-2.5 and later.
  - Run the library first. Ship only if it shows a reproducible gain in net rupees at equal budget.
- **Where in the code.** `configs/engine.yaml` (`strategy_presets`), `engine/config.py`, `library/`.
- **Acceptance.**
  - The licence allow-list test refuses a non-commercial model.
  - The library report states the result, whichever way it falls.
  - The default strategy is unchanged.
- **Effort:** 1.5 person-weeks (DS 1). **Depends on:** M130. GPU cost goes through M108's estimate.

#### M144 — Save-desk card, as a randomised pilot

- **Why.** The best field evidence for AI assist comes from chat support: +14% resolutions per hour overall
  and +34% for novices (Brynjolfsson et al.). Retention calls are not tested.
- **Scope.**
  - A card for call-centre staff showing:
    - the top reasons (M98's templates);
    - the recommended offer;
    - a "likely to stay anyway, do not discount" flag.
  - An optional paraphrase is written under claim checks.
  - Access to the card is randomised across staff, and the pilot is measured like any campaign.
- **Where in the code.** `engine/decide/reasons.py`, `engine/agentic/`, a read-only lookup route.
- **Acceptance.**
  - A pilot design registered with its test plan.
  - Ships only on a measured result.
- **Effort:** 3 engineer-weeks. **Depends on:** M132; a client with a save desk.

### Watch list (not scheduled)

- **The surrogate index** for long-term value. Netflix's inferences were about 95% consistent with the
  63-day result, but recall for launch decisions was 65–79%, and discounts can break its assumptions.
  Pilot it inside M136 first.
- **Causal PFNs** (CausalPFN, Do-PFN): known to shrink effects towards zero. Research only, never a
  substitute for the holdout.
- **A2A agent cards:** when a marketing platform adopts them.
- **Contact-fatigue experiments:** randomised contact counts inside the global holdout, to replace guessed
  caps (M101).
- **DPDP consent-manager integration:** from November 2026 a consent manager may register, but none can yet.
  This belongs to the integration plan in 2027.

---

## 5. Shared contracts introduced

| Contract | Introduced | Shape (all fields optional or defaulted unless stated) |
|---|---|---|
| Catalogue action additions | M131 | `a2p_declared`, `template_status: active \| suspended` |
| Consent ledger additions | M131 | `basis: explicit \| inquiry \| legacy`, `operator_registered`; purpose `measurement_comparison_groups` |
| `AgentTool` | M132 | `name`, `kind: read \| propose`, input model, output model, `route_policy` |
| `Proposal` | M132 | `proposal_id`, `kind`, `payload`, `evidence_ids`, `policy_checks`, `status` |
| `DecisionLogEntry` | M132 | `seq`, `prev_hash`, `entry_hash`, `kind`, `actor`, `signature`, `payload_ref` |
| `compliance_decisions.parquet` | M134 | key, channel, purpose, `allowed`, reasons, ledger refs, catalogue ref, rule version, signature |
| `IncrementalityReport` additions | M135, M136 | `covariate_kind: none \| pre_period \| prediction`, `variance_reduction`; `pooled_effect`, `pooled_interval`, `pooled_from` |
| `SafeToRollOut` | M137 | `outcome: safe \| not_safe \| cannot_tell`, `lower_bound_inr`, `confidence`, `rows_needed` |
| `HarmAlarm` | M138 | `campaign_id`, `arm`, `fired_at`, `statistic`, `threshold` |

---

## 6. Sequencing and timeline

### 6.1 Dependencies

| Milestone | Depends on |
|---|---|
| M130 Set-up | approval |
| M131 TRAI Third Amendment, measurement purpose | — (Plan J M99) |
| M132 Agent-safe core | M130 |
| M133 Verifier over MCP, demo data | M132 |
| M134 Pre-send compliance gate | M131, M132; integration plan's Activation Contract |
| M135 Prediction covariates | M130 |
| M136 Pooling across campaigns | M130 |
| M137 Safe to roll out | M130 |
| M138 Harm alarm | M130 |
| M139 Campaign planner | M132, M137; integration plan's first measured cycle |
| M140 Variants as arms | M131 |
| M141 Verifier on client data | M133, M134; a pilot client |
| M142 Setup help for sources | M132; integration plan's source formats |
| M143 Foundation model challenger | M130 |
| M144 Save-desk pilot | M132; a client with a save desk |

**Critical path:** M130 → M132 → M134, then the integration plan's first measured cycle → M139 → the Phase 3
gate. Plan K's own effort is not the constraint; the first real cycle is.

### 6.2 Effort and capacity

| Phase | Gate | Milestones | Total (DS inside) | Engineering | DS |
|---|---|---|---|---|---|
| 1 Safe, compliant, callable | week 6 | M130–M134 | 12.5 (DS 0) | 12.5 | 0 |
| 2 Prove faster | week 11 | M135–M138 | 10.0 (DS 6.5) | 3.5 | 6.5 |
| 3 The planner and the verifier | first measured cycle (about week 16–20) | M139–M142 | 12.0 (DS 1.5) | 10.5 | 1.5 |
| 4 Only with evidence | evidence | M143–M144 | 4.5 (DS 1) | 3.5 | 1 |
| **Total** | | **15 milestones** | **39.0 (DS 9)** | **30.0** | **9.0** |

Engineering capacity is 2.5 a week, so about 12 weeks covers 30 engineer-weeks. The data scientist is the
constraint in Phase 2: 6.5 DS-weeks against about 4 available in five weeks. Phase 2 statistics can start in
week 0, because they depend only on M130, which spreads the load. With a part-time second data scientist
(K10), Phase 2 holds its gate. Without one, it slips by about 3 weeks.

### 6.3 Week-by-week (3 engineers A–C, 1 DS)

| Weeks | Engineer A | Engineer B | Engineer C | Data scientist |
|---|---|---|---|---|
| 0–1 | M130 | M131 TRAI rules (urgent) | M131 | M135 design: covariate recipe in the test plan |
| 1–3 | M132 core | M131 finish, then M134 gate | M132 policy and log | M135 estimator and coverage tests |
| 3–6 | M133 MCP prototype | M134 gate | M132 pass^k harness, then screens | M136 pooling |
| 6–11 | Screens for M135–M138; M143 library run | Hardening; integration-plan coordination | M138 alarm plumbing | M137 safe to roll out; M138 statistics; Hillstrom re-run |
| 11 → first measured cycle | M139 planner | M140 variants as arms | M142 setup help | M140 arm analysis; M139 review |
| after the cycle | M141 verifier on client data | M139 finish | M144 pilot design (if a client) | Phase 3 validation |

---

## 7. Do not build in Plan K (and why)

- **An agent that sends, or any write into a client's system.** Buyers distrust it, agents are unreliable
  at it, and "sending and grading" means marking our own homework (K1).
- **Per-customer reinforcement learning or real-time bandits.** These need volume we do not have, and
  vendors themselves advise A/B tests at low traffic.
- **A language model as the judge of cause and effect, or as the writer of SQL on client data.**
- **Simulated customers used to predict lift.**
- **TabPFN-2.5 or later in production** without a Prior Labs licence (K8).
- **Any figure not measured.** This includes vendor headline lifts in our own materials, the MIT "95%",
  BCG's superseded "5%" and any research figure marked Hold.
- **Changing a Plan J default, or loosening a test.**

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **The integration plan slips, so Phase 3 waits.** | Phases 1 and 2 do not depend on it, and both have standalone value: compliance, faster proof and the demo MCP prototype. The planner (M139) can run on the library while it waits; only its gate needs the real cycle. |
| **A platform adds neutral-looking verification first** (MoEngage, CleverTap, Netcore or Braze). | M133 ships in Phase 1. Neutrality must be verifiable, not claimed: signed plans and approvals, a holdout salt the client owns, measured lower bounds as the only "proven" figure, and an MCP endpoint any vendor's agent can be audited through. |
| **Prompt injection or tool poisoning through MCP.** | No write tools. Schema-validated JSON results. Least-privilege scopes. Approvals enforced by the server. Plan G's egress and untrusted-content rules. An injection canary test on every tool. |
| **Faster-measurement methods disappoint on yes/no outcomes.** | Promise nothing. Measure on Hillstrom and report whichever way it falls; MLRATE is never worse than the plain estimate. Pooled figures stay "Estimated". |
| **A regulation reading is wrong.** | M131 and M134 are rules in configuration, versioned and dated, so they change without code. The DPDP hold-out question goes to an Indian law firm (K4). Primary texts are cited in `docs/DECIDE.md`. |
| **Agents look impressive in a demo and fail in use.** | pass^k consistency is a merge gate. Agents only propose. Kill criteria (§9) pause M139 and M141 if consistency or credibility fails. |
| **The data scientist is overloaded again.** | Phase 2 starts in week 0; a part-time second data scientist (K10); otherwise the gate slips by about 3 weeks. |

---

## 9. Measures of success and kill criteria

| Phase | Product (tested) | Evidence for the managers |
|---|---|---|
| 1 | TRAI-rule regression tests; tamper-evident log; policy refusals; pass^k; MCP numbers equal the screens | A live demo: an outside agent asks Marketing AI whether a campaign is worth running, and gets the same answer the product's screens give |
| 2 | Nightly coverage and false-alarm bands for every new estimator | Hillstrom's measured variance reduction and its safe-to-roll-out result, reported whichever way they fall |
| 3 | Claim checks; pass^k; injection canary; one integrated cycle through the planner | A real client campaign planned, approved, measured and audited through MCP by the client's own agent |
| 4 | Licence allow-list; registered pilot design | A rupee gain at equal budget, or a documented "no gain" |

**Kill criteria** (each pauses the milestone named and is reported to the managers):

- pass^k consistency below 100% on the scenario set after one fix round: pause M139 and M141;
- a pilot CFO or finance reviewer discounts an agent-written summary: switch the planner to rules-only
  wording;
- a major engagement platform ships an equivalent neutral audit tool before M141: re-plan M141 as an
  integration with it, not a competitor;
- M135 gives no measurable reduction on Hillstrom and the simulator: keep CUPED as the default and record why.

---

## 10. Open questions for the managers

1. Do you accept K1–K12 as recommended?
2. May M131 start now, before Plan K is approved, so the product follows TRAI's Third Amendment when it takes
   effect (K3)?
3. Who commissions the Indian law-firm opinion on randomised hold-outs under DPDP (K4), and by when?
4. Is a part-time second data scientist available for Phase 2 (K10)?
5. Which pilot client is most likely to use its own AI agent with its engagement platform? That client
   decides when M141 is worth building.
6. Should the integration plan be written next, so Plan K's Phase 3 dependency has a date?
7. Should the roughly 15 research claims still marked "Hold" be checked before the review, so the deck uses
   only verified figures?
