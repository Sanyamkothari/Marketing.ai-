# Marketing AI — Plan J: Decide, hand off, prove

**Companion to:** Plan H (`MARKETING_AI_PLAN_H_SIMPLE.md`), the positioning decision memo ([`docs/research/POSITIONING_DECISION_2026_10.md`](../research/POSITIONING_DECISION_2026_10.md): Option H, a decide-and-prove layer with a gated starter), the market research ([`docs/research/MARKET_RESEARCH_2026_10.md`](../research/MARKET_RESEARCH_2026_10.md)), `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1300 … 1399
**Milestones:** M90 – M120
**Status:** proposal for review. It is waiting for the founders' decision on positioning (§1). Phases 1–3 are a firm commitment. Phases 4–5 are provisional and will be re-planned at the Phase 3 gate.

## Plan J at a glance

**The decision.** Marketing AI does **not** bundle an open-source CRM or try to replace the client's platform. It becomes a **decide-and-prove layer** on top of whatever the client already uses: a CRM, an engagement platform, a messaging provider or a warehouse. It decides who to contact and who to leave alone, puts an approved list into the client's own tool, and proves the result in rupees against a random holdout. Sending messages ourselves ("starter activation") is decided later on evidence (M120), and is never in this plan. The product is industry-agnostic; each industry is a configuration pack.

| Phase | Weeks | A client gets | Milestones | Effort (person-weeks) |
|---|---|---|---|---|
| **1 — Safe to hand off** | 0–6 | A paid Data Readiness Assessment in their own AWS account: data checked and fixed, a signed outcome definition, a power sheet (what effect is detectable at which holdout, and what it costs), and a timed dry run of the hand-off into their real tool | M90–M96: set-up, consent fixes, deployment, Activation Contract 0.9, persistent holdout service, readiness kit | 21.0 |
| **2 — First list live** | 6–10 (list about weeks 10–11) | An approved model's list in their own bucket, in their tool's format, behind a release rule (approved model, persistent holdout, fresh consent, registered test plan) | M97–M101: connector SDK (S3, webhook), campaign record, test plan, hand-off in the product, measurement validity harness | 16.0 |
| **3 — Reconcile and prove** | 10–16 | Reconciliation of their send log (who was really contacted, holdout contamination), a **Value Proof Pack** for finance (net value with a 95% range), audit readouts of campaigns other tools ran, a read-only customer view | M102–M108: send-log importer, reconciliation, audit readouts, Proof Pack, customer view, connection-backed inputs, revenue outcomes and CUPED | 21.0 |
| **4 — Cycle two, client two** *(provisional)* | 16–24 | A monthly loop that needs a person only to approve; uplift learned from their own data and checked against risk ranking; lists ranked by net value and channel consent; client 2 onboarded on a different tool by configuration only | M109–M115: action catalogue and channel consent, value weighting, learn from cycle one, beats-risk check, the unattended loop, second hand-off route, pod tools | 27.5 |
| **5 — Buy through AWS, any industry** *(provisional)* | 24–28 | Purchase through an AWS Marketplace private offer, install with zero send permission, industry pack and region profile, cost before each run, DPDP evidence pack | M116–M120: hardening and FTR, industry and region packs, cost cap, DPDP evidence, the starter-gate decision | 11.0 |

**Team assumed:** 4 engineers and 1 data scientist for about 28 weeks (96.5 person-weeks; Phases 1–3 committed, Phases 4–5 re-planned at the Phase 3 gate). With 3 engineers it takes about 38 weeks; §6.4 gives the cut order.

**What we never build here:** message sending, journeys, a CRM, a CDP or reverse-ETL engine, per-vendor push APIs, multi-tenant SaaS (§7).

**What the founders must answer first:** the decisions J1–J16 (§1.3) and the open questions (§10). The most urgent: who pilot 1 is and which tool they load lists into, whose AWS account hosts it, and whether the 4+1 team is funded.

---

> Marketing AI already makes good decisions. It has:
> - uplift learners;
> - sleeping-dog suppression;
> - a profit curve;
> - a human Approver;
> - 95% campaign intervals.
>
> Two things hold it back. Its output stops at `scores.csv`, and the control group is drawn again on every run. This plan turns the product into a **decide-and-prove layer**. The layer sits on top of whatever the client already pays for: a CRM, an engagement platform (CEP), a messaging provider or a warehouse. The layer:
> - decides who to contact and who to leave alone;
> - writes one governed **Activation Contract** into the client's own storage;
> - reads the client's send log back;
> - proves the result in rupees against a random holdout that survives the hand-off.
>
> The product sends nothing, holds no CEP credentials and builds no journeys. It stays a single-company tool with one generic lifecycle journey and four pages (Plan H).
>
> This plan merges three drafts:
> - **Value-first ordering.** It works backwards from the first paying client's calendar.
> - **Risk-first items.** These protect measurement integrity and compliance before any list leaves the building.
> - **Architecture-first contracts.** It defines five interfaces, each with a fake behind it, so that later features are configuration rather than rework.
>
> §3.4 lists every conflict between the drafts and how it was resolved. Three reviews (feasibility, completeness, sequencing) then revised the plan. Findings that were not applied are listed in "Review notes" at the end.

---

## 1. The decision this plan implements

### 1.1 Positioning in one paragraph

We do not bundle an open-source CRM, and we do not sell Marketing AI as a replacement for the client's platform. We build a neutral **decide-and-prove layer** for any B2C business with customer data. That covers retail and e-commerce, BFSI, subscriptions, travel, utilities and telecom. Each is one industry pack among several, and pilot 1 may come from any of them. The layer:

- decides who to contact and who to leave alone;
- hands an approved list to the client's own tool through one standard Activation Contract;
- proves the result in net INR, with a 95% range, against a random holdout that survives the hand-off.

The **starter activation** is a one-shot send through the client's own AWS or provider account. We build it only if at least 2 of the first 5 qualified prospects meet all three conditions:

- they have no tool that can take in a list;
- they will pay for activation;
- they accept being the legal sender.

Plan J records that decision (M120) and contains no sender code. If the gate passes, the starter becomes a separate Plan K. Multi-step journeys are never part of the product; they are a Minfy services contract.

**What we say to a buyer:** "Keep your CRM and engagement tool. Marketing AI decides who to contact and who to leave alone, puts the approved list into the tool you already use, and proves what it earned in rupees against a random control group, all inside your own AWS account."

### 1.2 The product boundary (from the decision memo)

| Area | We build (product) | We integrate with | We never build |
|---|---|---|---|
| Data | Read-only connections; connection-backed sources and pulls (M107); Guided setup; Data Doctor; one row per customer | The client's warehouse or CDP; reverse ETL (Hightouch, Fivetran, RudderStack) | A CDP, identity resolution, a reverse-ETL engine |
| Decide | AutoML, uplift (S/T/X), sleeping-dog suppression, value under a budget, approval, explanations in business language | The client's LLM for copy and summaries | A real-time decisioning engine (for now) |
| Hand-off | Activation Contract, write-scoped destination, per-tool profiles, HMAC webhook (an MCP server comes later, outside Plan J) | CEP ingestion: Braze CDI, MoEngage S3, CleverTap Custom List, Netcore S3, SFMC SFTP | Per-client custom ETL as a product |
| Prove | Persistent holdout, send-log importer, reconciliation, Value Proof Pack, audit mode, programme readout | CEP send-log exports | Self-graded attribution without a random control |
| Starter (gated; **not in Plan J**) | One-shot sender and a SendPlan with a second approver (Plan K, if the M120 decision says build) | The client's AWS account, DLT entity, WhatsApp Business Account, provider | Waits, branches, triggers, follow-up sequences |
| Engagement | — | The client's CEP for journeys, push, in-app, frequency caps | Journey builder, inbox, template or landing-page designer, push SDK |
| Records | Read-only customer view inside our app: band, reason, history (M106) | The client's CRM as the system of record | A CRM, a contact-management UI |
| Infrastructure | CDK stacks in the client's account; the write role in its own stack, off by default | AWS services on the client's bill | Mail server, SMS gateway, push server, shared IP pool, unofficial WhatsApp APIs |

### 1.3 Decisions to confirm with the founders

| # | Question | Recommended answer |
|---|---|---|
| J1 | Do we adopt Option H, with Plan J building only the layer? | **Yes.** Plan J builds no sender. The starter is decided on evidence in M120 and, if built, is a separate Plan K. |
| J2 | May the product write into client systems? | **Yes, narrowly.** It writes only six things: the Activation Contract, its manifest, a holdout roster, deletion manifests, suppression deltas, and a signed "list ready" webhook carrying ids, counts and hashes only. Writes go through a separate write-scoped Destination with its own credentials, into one prefix or schema in the client's account. On AWS those credentials live only in the off-by-default activation stack. Source connections stay read-only. DEC-1300 narrowly supersedes the "never writes" sentence in DEC-1100 and the Plan H §6 guard rail. |
| J3 | Do we accept the release rule? Under it, no real list leaves without all of: an approved model, a persistent or agreed external holdout, a fresh consent snapshot, any required legal opinion, a pre-registered test plan, and sign-in. | **Yes.** The first live list moves from the memo's weeks 3–6 to about **weeks 10–11**. The range is weeks 10–12, depending on which tool pilot 1 uses (§10 Q1). In exchange, every readout is defensible. The prerequisites are built in parallel during the assessment. |
| J4 | What holdout scope and size for new deployments? | **Universal** scope: one holdout across all use cases, salted per deployment and drawn over the full customer master. The fraction is set from the M96 power sheet: default 10% for pilot 1, never below 3%. Raising the fraction keeps every existing member. Lowering it or rotating the salt starts a new epoch, which needs Admin approval and is audited. |
| J5 | The client's CEP may run its own global control group (Braze GCG, CleverTap SCG). Whose holdout is then the source of truth? | **Ours.** Either the CEP's control group is switched off for our audience, or the two are reconciled in writing. Scope `external` (the CEP's own group) is allowed only under a signed agreement. |
| J6 | Do we run an explore slice? That is a small random share of eligible customers outside the target who are treated anyway. | **Yes: 5% in cycle 1 if the sponsor agrees** (cap 10%). Its cost appears on the signed plan. Without it, cycle 2 cannot learn uplift for the lower bands of a client that has no randomised history. |
| J7 | What identity goes in the contract? | **The client's own customer key, in clear**, kept as text, because the CEP joins on it. This departs from the memo's "customer token" and is recorded as a DEC signed by the client's DPO at the Phase 1 exit. Channel IDs are off unless configured. Model features and their values never appear. |
| J8 | Must an Approver approve each delivery, or is the approved model enough? | **The approved model plus a registered test plan is enough.** Separation of duties already applies to the model, so a per-list approval adds friction without adding evidence. |
| J9 | Should "uplift beats risk ranking at equal budget" block anything? | **It should block delivery of an uplift-ranked list from Phase 4.** When it blocks, the list falls back to the designated propensity model. The check stays advisory on the approval screen, and the frozen champion rule is untouched. |
| J10 | Which use case runs first? | **The one with the shortest outcome window (30 days or less) and enough volume** to detect a 1–3 point effect at the agreed holdout, for example a payment reminder or a 30-day repurchase. Not 60–90-day churn. The pod writes its use-case YAML in Phase 1. |
| J11 | Are audit readouts a paid SKU? An audit readout measures a campaign that another tool targeted. | **Free for the first 2 prospects** as a discovery tool (experiment E4), then paid once M104 ships. |
| J12 | What team and horizon? | **4 engineers + 1 data scientist for about 28 weeks.** That is about 96.5 person-weeks of milestone work: 82.5 engineering, 14 data science. Phases 1–3 (about 58) are committed. Phases 4–5 (about 38.5) are provisional. Minfy pod time is budgeted separately, at about 1 person per active pilot. §6.4 shows a 3-engineer variant. |
| J13 | Are these in Plan J: multi-offer, the frontline lookup API, service-account API keys, the MCP server, multi-tenant SaaS and cross-use-case arbitration? | **No.** They wait for a later plan. Multi-offer and the lookup API are pulled forward only if strategy checkpoint 5 fires: 2 of the first 3 target clients make them a precondition. Arbitration is pulled forward when one client signs a second, overlapping use case. |
| J14 | Do we add our own PLAN-J block to the 12 shared files? | **Yes, once, in M90.** Lanes merge into one integration branch, and all shared-file edits go through one owner's daily contracts PR (§3.2). |
| J15 | Do we run live equal-budget arms (risk top-N against uplift top-N, with a shared holdout) for pilot 1, and from which cycle? | **Yes, pre-registered in the test plan for the first uplift-ranked cycle after M112 lands (cycle 4, about weeks 23–24).** The live-arms code arrives with M112, so no earlier cycle can run them. Until then the comparison is cross-fitted off-policy evaluation. The uplift arm still needs the beats-risk release check (J9); if that check fails, the cycle runs the fallback ranking without arms, and the arms move to the next cycle in which uplift passes. Strategy checkpoint 2 and the memo's re-examine trigger need live evidence. Off-policy evaluation stays as the early read. Without live arms, M112 drops by about 1 engineer-week, and the checkpoint rests on cross-fitted off-policy evaluation only. |
| J16 | May sign-in be on for client deployments? This reverses Plan H H5. | **Yes.** DEC-1308 supersedes Plan H H5 ("Sign-in stays off; admin screens are hidden"). DEC-1112's sign-in-off display rule still applies to installs that leave sign-in off. An Admin creates the Analyst and Approver users with `scripts/create_user.py` or the existing Settings → Users entry. The top bar keeps four items. |

---

## 2. What a client gets at the end of each phase

Each exit gate has two parts:
- **Build gate.** Code and a demonstration, on fakes, synthetic data or staging. Passing it unlocks the next phase's build.
- **Pilot gate.** The pilot gate is met on the pilot's real data and has a decision date on the client calendar. When it is missed, the date slips; the gate does not.

| Phase | Weeks | A client can use | We demonstrate | Exit gate in one line |
|---|---|---|---|---|
| **1 — Safe to hand off** | 0–6 | A paid Data Readiness Assessment inside their own AWS account. It covers Guided setup and Data Doctor, a signed label definition, and a power sheet showing what effect is detectable at which holdout and what the holdout and explore slice cost. It also includes a timed hand-off dry run in their real tool, with a key-match check. | The assessment on their data, in their account. A draft-format contract loaded into their real CEP with the holdout excluded, and their real keys matched. | Deployed with sign-in on. Dry run passed within 3 weeks with ≥95% key match. Agreed in writing: label, holdout fraction, consent feed, send-log export. Contract frozen at 1.0. |
| **2 — First list live** | 6–10 (list about weeks 10–11) | An approved model's list in their own bucket, in their tool's format. They filter `mai_treat_flag = 1`, exclude `mai_holdout_flag = 1` and launch from the tool they know. They also get a signed "list ready" webhook and a test plan the sponsor has signed. | Delivery through the release rule. The receipt's sha256 matching the object in their bucket. The campaign live in their CEP. | First real list ingested with no reformatting. Plan registered before delivery. Treat rows exist, and none lack consent. |
| **3 — Reconcile and prove** | 10–16 | A reconciliation report (contact rate, contamination). A **Value Proof Pack** for finance, covering: incremental results; naive against measured credit; offer money spent on sure things; the cost of the holdout; backfire by segment; net INR (or revenue) with a 95% range. Also: audit and programme readouts, and a read-only customer view. | Reconciliation from their real send log. The Proof Pack read by the sponsor and finance without a walkthrough. An audit readout on a prospect's past campaign. | Contamination under 5% and contact rate of at least 70%, or a corrective cycle agreed. Proof Pack generated on the registered analysis date. |
| **4 — Cycle two, client two** (provisional) | 16–24 | A monthly loop that needs a person only to approve. Uplift learned from their own cycle-1 randomised data. A written answer to "does uplift beat risk ranking at equal budget?". A channel-aware list ranked by net INR. Client 2 is onboarding on a different CEP with configuration only. | A scheduled cycle (score → contract → send-log pull) on real data. Measurement on fakes with a controlled clock. Client 2's onboarding pull request contains configuration only. | Cycle 4 delivered and its send log pulled by schedule. The beats-risk result is written, whichever way it falls. Client 2's first contract ingested natively. |
| **5 — Buy through AWS, any industry** (provisional) | 24–28 | Purchase through an AWS Marketplace INR private offer. Install with zero send permission. Start from an industry pack and a region profile. A DPDP evidence pack for the DPO. A cost estimate and cap before each run. | A fresh install by someone outside the team. Client 2, from a different industry than pilot 1, running the journey with no engine change. The evidence pack. | Private offer submitted and the FTR (AWS Foundational Technical Review) complete. Client 2 live with its own reconciliation. Cycle 4 measured by schedule. Starter-gate DEC recorded. |

---

## 3. Principles and rules

### 3.1 Product principles

1. **Work backwards from the client's calendar.** Anything that cannot be fixed once a list has left the building ships before the first delivery:
   - the consent gate and the consent snapshot;
   - the persistent holdout and the explore slice;
   - the contract fields, including the intended-treatment flag;
   - the pre-registered plan.

   Anything needed only at readout ships while the campaign is in the field: send-log import, reconciliation and the Proof Pack.
2. **Retire the risk first, cheaply.** Each phase opens with the cheapest test that could kill its premise before the expensive build. Examples: a draft-format dry run, a key-match check, a power sheet, a coverage simulation, a retrospective readout.
3. **Contracts before features.** The five interfaces in §5 land in Phases 1–2, each with a fake and a conformance test. A later feature that needs a change to one of them requires a DEC and an additive minor version bump. Fields are never renamed or removed (the DEC-668 pattern). The Activation Contract stays at `0.9` until the Phase 1 dry run and the builder's golden files agree. It is frozen at `1.0` at the Phase 1 pilot gate, not the build gate. Until then, Phase 2 builds on `0.9` with additive-only changes (DEC-1302).
4. **One contract, many tools.** Per-tool differences live in configuration, not vendor code: profile YAML (`configs/activation/profiles/`) and send-log mapping YAML.
   - We ship files and tables before APIs.
   - A profile or destination is built only when a paying client runs that tool.
   - We never hold CEP credentials.
5. **We decide and prove; the client sends.** Source connections stay read-only. The only writes are the contract, its manifest, the holdout roster, deletion manifests, suppression deltas and the "list ready" webhook. They go through a separate write-scoped Destination (DEC-1300). Nothing in Plan J sends a message.
6. **Activation reads finished runs.** The contract builder is a pure function over a finished scoring run's artefacts. It follows the same one-way rule the generative jobs follow (DEC-210/211): it reads a run's artefacts, adds files beside them and is never imported by the pipeline. It adds no new method rebind in `engine/pipeline.py` that could silently drop the consent gate (DEC-1307).
7. **The holdout is decided once per customer.** It uses a hash threshold with a persistent, fingerprinted salt, and nothing downstream may redraw it. `scope: run` stays the default, so DEC-A4 and `test_a_different_run_id_draws_a_different_holdout` hold unchanged.
8. **One canonical measurement path.** Every measured result belongs to a Campaign record and goes through `measure_campaign`, which wraps `measure_incrementality`. All three routes call the same function: `POST /runs/{id}/campaign-results`, `POST /runs/{id}/measure` and `POST /campaigns/{id}/measure`. A test (M98) pins them together where they measure the same population. On an uplift run with every row mature and a fixed `as_of`, the three reports are identical except for `computed_at` and `campaign_id`. On a propensity run, the campaign route equals `campaign-results` called with `bands = activation.treat_bands`. M49's `/outcomes` remains model monitoring only.
9. **Measure what actually happened, and say what was measured.**
   - Intent-to-treat (ITT), restricted to the intended population, is always the primary result.
   - Complier-adjusted lift (CACE) is secondary, labelled, and shown only when a send log exists.
   - Contamination and contact rate are always reported against the memo's targets: under 5%, and at least 70%.
   - Every number is a 95% range. Every interval shown to a client has a nightly coverage test.
   - A number that cannot be measured is null with a plain reason, never 0.
   - Synthetic runs are flagged, and the Proof Pack refuses them. The planted 12.7-point demo effect never appears outside demo docs.
   - "Causal" is claimed only for random assignment the engine made or verified.
10. **The release rule is enforced in code (DEC-1303), not by process.** This includes legal-opinion gating and row-level downloads. See §3.3.
11. **Opt-in, additive change only.**
    - New behaviour defaults to today's behaviour: `holdout.scope: run`, `activation.enabled: false`, `auto_deliver: false`, `explore_fraction: 0`, and the activation write stack off.
    - Contracts only gain optional fields.
    - Frozen files stay frozen.
12. **Keep Plan H's simplicity.**
    - The top bar stays Home, Connections, Results and Settings, and Home shows one generic journey.
    - Hand-off is a run action and panel. Destinations are a group on Connections.
    - The test plan, reconciliation and Proof Pack sit on the run and campaign screens.
    - Audits, programme readouts and the customer view live in Results. The holdout, catalogue, industry pack and region are Settings entries.
13. **Config over code, for any industry and region.** Nothing under `engine/` branches on a use-case, industry or region id (`tests/unit/test_no_use_case_branching.py`). Telecom is a pack. India's rules are a region profile.
14. **Each phase removes one manual pod step.** By the end of Phase 4, a monthly cycle needs a human only to approve the model. M113's automation makes cycle 4 need only the Approver. Strategy checkpoint 7 (pod effort at or below 1.5 people per client by cycle 3) is measured from the pod-effort log, which M90 starts and the pod fills in from cycle 1.
15. **Every phase ends with a build gate and a pilot gate.**
    - The build gate is tests in `MILESTONE_TESTS` plus a demonstration on fakes, synthetic data or staging. It unlocks the next phase's build.
    - The pilot gate is measured on a real prospect or pilot and has a decision date.
    - A missed pilot gate moves its date, never its content. No pilot gate can pass before the previous phase's pilot gate.

### 3.2 Parallel-work protocol rules that apply

- **Claim first.**
  - M90 adds the row `DEC-1300…1399 | Plan J — decide, hand off, prove (M90–M120)` to `PARALLEL_WORK_PROTOCOL.md` §4 before the first DECISIONS entry.
  - DEC-1300…1309 are the foundational decisions written in M90. After that, each milestone records **one** DEC entry whose sub-decisions are lettered (a), (b), (c)…, so Plan J needs about 50 numbers and fits inside DEC-1300…1399. Each entry takes the next free number when it is written, with no sub-ranges and no pinned numbers (DEC-1309).
  - If the range runs out, Plan J claims the next free hundred by adding its §4 row first.
- **Branches.** M90 adds a §1 row: integration branch `plan-j-layer` from `main`, with short-lived lane branches merged into it at least daily. `plan-j-layer` merges into `main` at each phase's build gate, with a merge commit.
- **Shared files.** M90 does three things:
  - adds a `---- PLAN-J (layer) — append only below this line ----` / `---- END PLAN-J ----` block to all 12 shared files;
  - appends `PLAN-J` to `PHASES` in `tests/unit/test_shared_file_markers.py`;
  - records the existing PLAN-I marker gap in `docs/CROSS_BRANCH_REQUESTS.md`.

  All shared-file edits go through one daily contracts PR owned by the Engineer A lane. Lanes never edit a shared file directly.
- **Declarations that cannot live in the block.** A pydantic field cannot be added from a block at the foot of a file. `UseCaseConfig.model_rebuild()` runs inside the PHASE-2 block, before later blocks. So Plan J follows the Plan G/3b pattern: one defaulted declaration in place, with its type defined in Plan J's own module.
  - Every such edit is listed in §3.5.
  - Each is covered by DEC-1304 and announced in `docs/CROSS_BRANCH_REQUESTS.md` the day it lands.
  - Test updates these edits need are reviewed edits, never loosening: the `CHECK_CODE_TABLES` set and `PHASES`.
- **Stage modules.** Protocol §3 allows only Phase 2's six changes to Phase 1 stage code. DEC-1306 amends §3 to name the stage functions Plan J may edit, and nothing else:
  - in `engine/stages/actions.py`: `_control_mask`, `_entity_control_mask`, `_holdout_size` and `suppression_rules`;
  - in `engine/stages/export.py`: `_suppression_counts` (M109 fills `SuppressionCount.channel_counts`). `_REASON_CODES` stays named for review, but M109 leaves it unchanged.

  Under default config, behaviour must stay byte-identical, pinned by the existing tests. Only the protocol owner can widen the protocol, so DEC-1306 needs the human reviewer's sign-off before M95 starts.
- **Ownership.** Plan J owns:
  - `engine/activation/**`, `engine/holdout/**`, `engine/measurement/**`;
  - `api/routes/{activation,campaigns,holdout,customers}.py`;
  - `ui/modules/activation/**`, `configs/activation/**`;
  - `tests/**/{activation,holdout,measurement}/**`, `tests/statistical/**`, `tests/conformance/**`;
  - `docs/ACTIVATION.md`, `docs/activation/**`;
  - `docs/pilot/PROSPECT_LEDGER.md` and `docs/pilot/templates/pod_effort_log.csv`, carved out of Plan E's `docs/pilot/**`;
  - `infra/activation.py`, carved out of Phase 4a's `infra/**`.

  These carve-outs are recorded as Plan J rows in protocol §3 by M90's DEC, together with the `infra/app.py` stack registration as a pre-approved edit. `configs/pilot/help.yaml` (Plan E's area) is a standing pre-approved, append-only edit for every Plan J milestone that adds a code in §5.7.

  Edits anywhere else are listed per milestone as **pre-approved edits** in the owning workstream's area. They stay backward compatible and are announced in `docs/CROSS_BRANCH_REQUESTS.md` the day they land (the Phase 2 §6.5 pattern).
- **Frozen files stay frozen:** `engine/stages/train.py`, `evaluate.py`, `explain.py`, and the champion rule in `engine/registry.py`. New gates go beside them: in a new module `engine/model_gates.py` and in the existing `engine/approvals.py`.
- **Every external call sits behind a protocol with a fake:**
  - `FakeDestination` and `LocalDirDestination`;
  - moto for S3 and STS;
  - an IAM-simulation fake (moto does not evaluate IAM);
  - the SQLite FakeServer for the warehouse;
  - an in-process SFTP fake;
  - a fake webhook receiver;
  - a `ConsentManagerSource` fake.

  Send-log fixtures are **shaped from vendor documentation** and labelled as such. A real, anonymised export is recorded as a golden file once the pilot supplies one. The fast suite needs no network, and live-vendor and AWS tests are opt-in markers.
- **Every new route** has a `RoutePolicy`, with `audit_reads=True` for row-level reads.
  - The middleware audits mutating routes; routes never write their own audit events.
  - New `DETAIL_KEYS` are reviewed changes with a DEC.
  - Customer ids go in request bodies, never in URLs (DEC-746).
- **Every new row-level artefact** is registered in the same change in `configs/privacy.yaml`, in `engine/privacy/layout.py` and, on AWS, in `infra/naming.py` `OBJECT_PREFIXES`/`ROW_LEVEL_PREFIXES`.
- **New platform tables** are SQLModel tables appended to `PLATFORM_TABLES`, each with an Alembic migration, in order: 0006 (M92), 0007 (M98), 0008 (M100), 0009 (M109). `tests/unit/production/test_platform_migration.py` stays green.
- **Every user-facing error code** has an entry in `configs/pilot/help.yaml`, added under the standing pre-approved edit above.
- **Run `make lint test` before every commit and `make test-all` before every merge.** Never skip, delete or loosen a test. A Phase 1 failure that is not ours means stop and report.
- **Generated docs and README.**
  - `docs/API.md` is regenerated (`make generate`) whenever a model changes.
  - M90 adds README rows (status: pending) for M90–M120 and a `MILESTONE_TESTS` entry for M90 only. `check_readme` fails on any mapped path that does not exist, so each later milestone adds its own entry in the same change that adds its tests.
- **Docs move with the code.** `docs/UPLIFT.md` (§3 checks, §14 config, §15 limits, §17 codes) and `docs/DATA_CONTRACT.md` §11 are updated in the same change as the behaviour they describe.
- **Nothing fabricated reaches the UI.** Panels render server JSON only, and sliders step only through computed points (DEC-1204).
- **Logs, alerts and webhooks** carry ids, counts and hashes only.

### 3.3 The release rule (DEC-1303)

**Where the rule applies.**
- `POST /runs/{run_id}/activation/deliveries`.
- The scheduled EXPORT firing.
- Every row-level download of a contacting use case with `activation.enabled: true`: `GET /runs/{run_id}/activation.csv`, `GET /runs/{run_id}/scores.csv`, `GET /runs/{run_id}/copy_messages.csv`, and the generic `GET /runs/{run_id}/artefacts/{name}` when `name` is `scores.csv`, `scores.parquet` or `copy_messages.csv`. The check sits inside `api/routes/runs.py::read_artefact`, which both `read_scores` and `read_copy_messages` (`api/routes/generative.py`) already call, so no path to those files skips it (M100).

A download that fails the rule is returned only as an Analyst **review copy**: `action`, `control_group` and the treat flag are blanked, and the audit event records a reason.

**Exemptions.** `fake` and `local_dir` destinations are exempt only for synthetic runs, or when `settings.env` is `local` or `dev`.

**Roster, deletion and suppression deliveries** follow a shorter rule:
- Roster deliveries are checked only for `auth_mode=local` (`DELIVERY_REFUSED_AUTH_OFF`), a write-purpose destination (`DESTINATION_READ_ONLY_CONNECTION`) and contract expiry (`ACTIVATION_CONTRACT_EXPIRED`).
- Deletion manifests and withdrawal suppression deltas are checked only for `auth_mode=local` and a write-purpose destination. They are never refused for model approval, holdout scope, consent age, legal opinion, test plan, expiry or the beats-risk check.

A delivery or download is refused unless **all** of these hold:

| Condition | Error code |
|---|---|
| The model is an approved version for the use case (approved by someone other than its trainer, DEC-862). That means the champion or, from M112, the designated propensity fallback. | `RELEASE_MODEL_NOT_APPROVED` |
| The run's holdout scope is `use_case`, `universal` or `external`. An external scope also needs its signed agreement recorded in `HoldoutSpec.external_agreement_ref` (set only through `PUT /holdout`, Admin, audited). | `RELEASE_HOLDOUT_NOT_PERSISTENT` |
| The manifest carries a `consent_snapshot_id`. | `RELEASE_CONSENT_SNAPSHOT_MISSING` |
| The snapshot's `as_of` is within `activation.max_consent_age_days` (default 2) of the delivery. | `RELEASE_CONSENT_SNAPSHOT_STALE` |
| A purpose or use case marked `requires_legal_opinion` has an opinion reference recorded by an Admin. | `RELEASE_LEGAL_OPINION_MISSING` |
| The campaign has a registered test plan (when `activation.require_test_plan`, default true). | `TEST_PLAN_REQUIRED` |
| `auth_mode=local`. | `DELIVERY_REFUSED_AUTH_OFF` |
| The contract is inside `valid_until`. | `ACTIVATION_CONTRACT_EXPIRED` |
| The destination is a write-purpose connection. | `DESTINATION_READ_ONLY_CONNECTION` |
| *(From M112)* An uplift-ranked list's model passed the beats-risk check. | `RELEASE_UPLIFT_NOT_BETTER_THAN_RISK` |

### 3.4 How the three drafts were merged: conflicts and resolutions

R0–R5 (risk-first) and A1–A5 (architecture-first) are the drafts' own phase names, not §1.3 decisions.

| # | Conflict | Value-first | Risk-first | Architecture-first | **Plan J** |
|---|---|---|---|---|---|
| 1 | When the first real list leaves | Weeks 3–6 | Weeks 10–12, behind the release rule | Phase A2 | **About weeks 10–11.** The release rule is kept. Its prerequisites are built in parallel during the assessment: holdout, consent fixes and snapshot, contract, deployment code, planner. |
| 2 | How the contract is produced | Rebind `_ScoreFlow._export` | Rebind both exports | Post-run builder over finished artefacts | **Post-run builder** (principle 6). It avoids new pipeline rebinds and the block-order risk to the consent gate, and covers propensity and uplift runs alike. |
| 3 | Holdout scopes | `run \| persistent` | `run \| persistent \| external` | `run \| use_case \| universal` with epochs | **`run \| use_case \| universal \| external`, with epochs and a roster** drawn from the customer master. The default stays `run`. |
| 4 | Where measurement lives | Run-keyed `/measure` made canonical | `/measure` plus `/campaigns` | Campaign record | **Campaign record** (M98). All three measure routes call `measure_campaign`, and a test pins their outputs together on the same population (principle 8). |
| 5 | What the test plan attaches to | The run | The run | The campaign | **The campaign.** A campaign is created from a run's contract before delivery. |
| 6 | Audit mode timing | Phase 3 | R0 | A3 | **Built in Phase 3** (M104), during the outcome wait. In Phase 1 the data scientist runs E4 retrospectives **by hand** with the existing `measure_incrementality`. |
| 7 | "Beats risk ranking" gate | Advisory | Blocking in the champion rule | Advisory | **Advisory on the approval screen; blocking at delivery** for uplift-ranked lists (M112), with a designated propensity fallback. The frozen champion rule is untouched. |
| 8 | Explore slice | Phase 1 | R1 | A4 | **Phase 1**, with the holdout service (M95), so cycle 1 creates randomised history. |
| 9 | Risk-vs-uplift arms | — | Live arms in R1 | — | **Phase 4.** Cross-fitted off-policy evaluation on cycle-1 randomised rows gives the early read. Live equal-budget arms are recommended for pilot 1 from cycle 4, once M112 lands (J15). |
| 10 | Action catalogue and channel consent | Phase 5 | R1 and R3 | A1 | **Phase 4 (M109), before value weighting.** Contract 1.0 already carries `mai_action_id`, `mai_channel`, `mai_contactable_channels` and `mai_channel_reason` as nullable. Lists before M109 use purpose-level consent, which M91 makes correct and snapshots. |
| 11 | Destination order | Pilot tool only | S3, warehouse, SFTP, webhook in R1 | Warehouse, SFTP and webhook in A2 | **S3 plus the signed webhook in Phase 2. Warehouse and SFTP in Phase 4** (M114). If the pilot's tool needs a warehouse or SFTP, that destination is added to Phase 2 at about +2.5 engineer-weeks, and the first list moves by about 1 week. |
| 12 | Starter activation | Decision only; separate plan | Gated phase R4 | Gated milestones in A5 | **Decision only (M120).** If built, it is a separate Plan K with its own off-by-default stack. |
| 13 | Multi-offer, frontline lookup API, API keys, MCP, arbitration | Not built | R5 | A4–A5 | **Out of Plan J** (§7). Pulled forward only on the triggers in J13. |
| 14 | Value weighting timing | Phase 4 | R2 | A2 | **Phase 4 (M110)**, before cycle 4. The cycle-1 Proof Pack is priced with the existing `RoiInputs`, so it does not wait for value weighting. |
| 15 | Revenue outcomes and CUPED | Phase 5 | R2 | A4 | **Phase 3 (M108)**, before the first Proof Pack. Test plans can pre-register a revenue outcome and a covariate from M99. |
| 16 | Label sign-off and treatment-history audit | — | R0 (4 engineer-weeks) | — | **Included, trimmed, in M96** with the power sheet. Agent treatment hints move to M104. |
| 17 | Coverage harness and synthetic quarantine | — | R0 | — | **M101 in Phase 2**, before any real readout. |
| 18 | Client store on AWS | EFS, or a DEC disabling raw tables | Postgres | Postgres | **Postgres** (alembic 0006), in M92. |
| 19 | Contract naming and versioning | Plain names | Plain names | `mai_*`, 0.x until the dry run | **`mai_*`** (the memo's names), **0.9 → 1.0 at the Phase 1 pilot gate**. |
| 20 | Delta and erasure | Diff against the last delivered file | Per-row state table and tombstones | Delivery-state table and tombstones | **A small delivery-state table plus a diff against the last delivered file.** On erasure, a deletion manifest goes **immediately** to every destination that received that use case or its roster, and the next delta carries `row_status=delete`. On consent withdrawal, a suppression delta goes immediately. |
| 21 | Package layout | `engine/activation`, `engine/measurement` | Same | Separate `destinations/`, `sendlogs/`, `holdout/`, `campaigns/` | **Three packages:** `engine/activation/` (contract, profiles, reasons, catalogue, `destinations/`, `sendlog/`), `engine/holdout/` and `engine/measurement/` (campaign, planner, reconcile, simulate, audit, compare). |
| 22 | Effort basis | 57 engineer-weeks in 30 weeks | 89.5 engineer-weeks in 27 weeks | About 3.2 net per week | **About 96.5 person-weeks (82.5 engineering, 14 data science) over 28 calendar weeks.** Capacity is planned separately per role (§4). |

### 3.5 Register of in-place declarations (DEC-1304)

Each row adds a defaulted declaration in place: in a shared file outside the PLAN-J block, inside another workstream's block, or on a model, enum or registry that another workstream owns. Types live in Plan J modules wherever possible. Each is announced in `docs/CROSS_BRANCH_REQUESTS.md` the day it lands. Line numbers are as of today's `main`.

| File and declaration | What is added | Milestone |
|---|---|---|
| `engine/config.py` `CampaignCopyConfig` (:1004) | `sms_sender: one_way \| two_way = two_way` | M91 |
| `engine/config.py` `UseCaseConfig` (:1246) | `activation: ActivationConfig` (type in `engine/activation/config.py`) | M94 |
| `engine/config.py` `ActionsConfig` (:772) | `holdout: HoldoutSpec`, `explore_fraction` (types in `engine/holdout/spec.py`) | M95 |
| `engine/config.py` `LabelDefinition` (:3364, PHASE-2 block) | `grace_days`, `exclude_roles` | M96 |
| `engine/contracts.py` `CHECK_CODE_TABLES` (:1664, PHASE-2 block) | `LABEL_RATE_UNSTABLE`, `TREATMENT_HISTORY_NOT_RANDOM`; `test_one_check_contract.py` updated as a reviewed edit | M96 |
| `engine/contracts.py` `RunRecord` (:243) | `synthetic: bool = False` | M101 |
| `engine/config.py` `Band` (:758), `SuppressionConfig` (:764) | `action_id`; `channels` | M109 |
| `engine/contracts.py` `SuppressionCount` (:1112) | `channel_counts: dict[str, int] \| None = None`, per-channel suppression counts such as `{"consent_denied_sms": n}` (the 3-value `reason` Literal is unchanged; per-row channel reasons go in the contract's `mai_channel_reason`, §5.1) | M109 |
| `api/schemas.py` `UpliftRunRequest` (:1086, PHASE-3B block) | `dataset_id: str \| None = None` | M111 |
| `engine/config.py` `GovernanceConfig` (:834) | `max_run_cost_usd` | M118 |
| `engine/config.py` `IndustryConfig` (:1488), `Source` Literal alias (:1999) | `IndustryConfig` gains a `defaults` overlay; the `Source` alias gains the value `"industry"`, so a leaf set by the industry pack reports its layer | M117 |
| `engine/settings.py` `Settings` (:203), `ENV_VARS` (:107), `SECRET_FIELDS` (:586); mirrored in `infra/naming.py` `SETTINGS_FIELDS` (:118, Phase 4a) | `activation_enabled: bool = False` (M92); `holdout_salt: SecretStr \| None = None`, added to `SECRET_FIELDS` and given a `holdout_salt_secret_name` in `infra/naming.py` (M95); each new field gets its `ENV_VARS` and `SETTINGS_FIELDS` entry in the same change, so `tests/infra/test_settings_contract.py` stays green. M119 adds a validator: `audit_retention_days` (:322) is at least 365 outside `dev` | M92, M95, M119 |
| `engine/pilot/help.py` `known_codes()` (:88, Plan E) | Unions a frozen `PLAN_J_CODES` set (defined in `engine/activation/codes.py`), so every §5.7 code with a `help.yaml` entry counts as raisable | M90 (codes added per milestone; M97 adds `CONNECTION_NEEDS_ADDON`) |
| `engine/connections/store.py` `ConnectionRecord` (:70, Plan H) | `purpose: Literal["read", "write"] = "read"`, `secret_ref: str \| None = None` (M97); `schedulable: bool = False` (M113; Admin-set, audited; write connections only) | M97, M113 |
| `engine/connections/base.py` `KindInfo` (:137), `StepName` (:90), `STEP_LABELS` (:100) | `KindInfo.group` gains `destination`; `StepName.PROBE_WRITE` with its label | M97 |
| `engine/privacy/contracts.py` `ConsentReport` (:102, Phase 4b) | `consent_snapshot_id: str \| None = None` | M91 |
| `engine/uplift/contracts.py` `IncrementalityReport` (:509, Phase 3b) | `test_plan_hash: str \| None = None`, `early_look: bool = False` (M99); the reconciliation and CACE fields (M103); the continuous and CUPED fields (M108); `arms`, with a new `ArmSummary` model (M112). All optional, `schema_version` unchanged (§5.6) | M99, M103, M108, M112 |
| `engine/uplift/contracts.py` `UpliftEvaluation` (:257), `ProfitCurve` (:453), `PolicyRecommendation` (:386); `engine/uplift/config.py` `UpliftPolicyConfig` (:85) | Value fields (M110); `baseline_comparison`, `calibration_by_decile`, `fold_auuc` and `UpliftPolicyConfig.arms` (M112) | M110, M112 |
| `api/routes/measure.py` `MeasureRequest` (:130) | `outcome_kind`, `covariate_column` | M108 |
| `engine/pilot/document.py` `ReportDocument` (:123, Plan E) | `synthetic: bool = False` (M101); the kind Literal gains `reconciliation` (M103), `proof` (M105) and `evidence` (M119) | M101, M103, M105, M119 |
| `engine/scheduling/alerts.py` `AlertRow` (:175), `Alert` (:108), `AlertKind` (:73) (Phase 4b) | `AlertRow.campaign_id` nullable column, with alembic 0007 (M98); `Alert.campaign_id: str \| None = None`, and `HOLDOUT_CONTAMINATED`, `LOW_CONTACT_RATE` with `SEVERITY_FOR` and `_TITLES` entries (M103) | M98, M103 |
| `engine/scheduling/schedules.py` `ScheduleKind` (:100), `ScheduleParameters` (:175) (Phase 4b) | `EXPORT`, `SEND_LOG_PULL`, `OUTCOMES_PULL`, `MEASURE`, `LEARN`; the optional parameters in M113 | M113 |
| `engine/onboarding/specs.py` `SourceSpec` (:317, Phase 2) | `binding: {connection_id, selection, rule} \| None = None` (type in a Plan J module) | M107 |
| `engine/agent/config.py` `AgentColumnHints` (:92, Plan G) | treatment hints | M104 |
| `pyproject.toml` `[tool.pytest.ini_options].markers` | `statistical` | M90 |
| `pyproject.toml` `[project.optional-dependencies]` | `sftp` extra (paramiko, pinned range) | M114 |
| `engine/uplift/metrics.py` `HoldoutUplift`, `engine/uplift/flow.py` `HOLDOUT_COLUMNS` | Optional per-row value columns for value weighting (additive) | M110 |
| `engine/approvals.py` `ApprovalItem` | `checks` (the beats-risk and stability results shown beside the champion decision; advisory) | M112 |
| `engine/stages/actions.py`, `engine/stages/export.py` | Only the functions DEC-1306 names (§3.2): `_control_mask`, `_entity_control_mask`, `_holdout_size` (M95); `suppression_rules` and `_suppression_counts` (M109) | M95, M109 |

---

## 4. Phases

**Effort basis.**
- **The team is 4 engineers (A, B, C, D) plus 1 data scientist (DS).**
  - Engineering holds back 0.75 person-weeks per week for pilot support, reviews, on-call for deployments and corrective cycles. That leaves **3.25 engineer-weeks of milestone work per calendar week**.
  - The data scientist holds back 0.2 per week for by-hand E2/E4 work and pilot readouts. That leaves **0.8 DS-weeks per week**.
  - Capacity is planned separately for the two roles, because data-science weeks cannot absorb engineering work.
- **What the figures include.** Each milestone's figure includes tests, docs, DEC entries, README and API regeneration and shared-file paperwork, which is about 15% of it. Data-science weeks are counted inside the figure and shown as "DS n".
- **What the figures exclude.**
  - Minfy pod and services time (budget about 1 person per active pilot).
  - The Minfy cloud and alliance teams: account, VPC, Bedrock, Marketplace listing, ACE.
  - Client 2's onboarding. That is pod-run, with about 2 engineer-weeks of engineering support budgeted as its own line in §6.2.
- All estimates are ours and unmeasured.

**Start condition.** Phase 1 assumes two things are in place by week 0: the P1 account owner, and a signed pilot.
- If either is missing, M90, M91, M94, M95 and M96 start on fakes and M93 is held.
- The pilot-gate dates then move with the signature.
- If pilot 1 slips by more than 4 weeks, the founders decide between continuing with audit-led sales (E4 and M104) and picking another pilot (§10 Q16).

---

### Phase 1 — Safe to hand off (weeks 0–6)

**Goal.** Phase 1 sets up the pilot so a list can leave safely:
- Deploy into the pilot's AWS account.
- Fix the defects that would make any export non-compliant, and snapshot consent.
- Land the Activation Contract and the holdout service with fakes.
- Prove the hand-off, and the join key, with a timed dry run in the client's real tool.
- Sign the label and the power sheet before any model or list is approved.
- Make the pilot kit industry-neutral.

**Client value.** A paid Data Readiness Assessment inside the client's own AWS account, on their data. It covers:
- a read-only connection, Guided setup (including Data Doctor's placeholder-code check);
- a readiness report with:
  - label sign-off;
  - a treatment-history verdict (uplift now, or propensity plus a random control first);
  - a power sheet that includes holdout and explore costs;
- a timed dry-run report from their real CEP, CRM or provider, with the key-match rate.

The DS also gives a by-hand retrospective readout (E4) to any prospect who shares a past campaign.

#### M90 — Plan J set-up, the boundary and the process decisions

- **Why.**
  - **Four written statements forbid writing to client systems today:** the "never writes" sentence in DEC-1100, the Plan H §6 guard rail, the `engine/connections/base.py` module docstring ("It only reads"), and `docs/CONNECTIONS.md` (plus "It sends nothing" in `docs/V1_READINESS.md`).
  - **The process rules need to be settled once, before any lane starts.** Plan J must edit stage functions, which protocol §3 forbids. It must add fields above shared-file blocks. It runs four lanes in parallel.
  - **The measurement paths must become one.** There are three outcome paths (`/outcomes`, `/campaign-results`, `/measure`). They must be reduced to one before connectors and automation build on them.
  - **Sign-in must come on.** Real deployments need it, which reverses Plan H H5.
- **Scope.**
  - Claim DEC-1300…1399. Add the §1 branch row. Add the PLAN-J blocks to all 12 shared files and append `PLAN-J` to `PHASES`.
  - Write `docs/plans/MARKETING_AI_PLAN_J_LAYER.md` and index it in `docs/plans/README.md`. Add README rows (pending) for M90–M120, and a `MILESTONE_TESTS` entry for M90 only.
  - Record these DECs:
    - DEC-1300: the write path. It narrowly supersedes the "never writes" sentence of DEC-1100 and Plan H §6.
    - DEC-1301: the memo's boundary table becomes a code-review rule, backed by a design-rule test.
    - DEC-1302: contract versioning, major.minor with additive minors only.
    - DEC-1303: the release rule (§3.3).
    - DEC-1304: the ownership map, pre-approved edits, the §3.5 register, the lanes and the daily contracts PR.
    - DEC-1305: the canonical measurement path (built in M98).
    - DEC-1306: the protocol §3 amendment naming the stage functions Plan J may edit (`_control_mask`, `_entity_control_mask`, `_holdout_size` and `suppression_rules` in `engine/stages/actions.py`; `_suppression_counts` and `_REASON_CODES` in `engine/stages/export.py`; §3.2). Needs the human reviewer's sign-off.
    - DEC-1307: the post-run builder, with no new pipeline rebind.
    - DEC-1308: sign-in on for client deployments. It supersedes Plan H H5 and the sign-in clause of DEC-1112. Users are created by an Admin with `scripts/create_user.py` or Settings → Users.
    - DEC-1309: DEC numbers are taken in order. From M91 on, each milestone records one DEC entry whose sub-decisions are lettered (a), (b), (c)…, so Plan J fits inside DEC-1300…1399. A new hundred is claimed only if this one is used up.
  - Add a test to the existing `tests/integration/test_design_rules.py` (DEC-306): `test_no_journey_or_send_primitives`. It fails if any of these is true:
    - the `ScheduleKind` values are not a subset of the allow-list {score, drift_check, retrain, export, send_log_pull, outcomes_pull, measure, learn};
    - a config key under `engine/activation`, `engine/holdout` or `engine/measurement` contains a whole-word token `wait`, `delay`, `branch`, `trigger`, `follow_up`, `send_after`, or `send` other than as the prefix of `send_log`;
    - any module under `engine/` references SES `SendBulkEmail`, `pinpoint-sms-voice-v2` or `socialmessaging` send calls.
  - Add Makefile targets `activation-test` and `measurement-test`. Add the `statistical` pytest marker (pre-approved edit to `pyproject.toml`). Wire a nightly `statistical` job into `.github/workflows/nightly.yml`.
  - Add a frozen, initially empty `PLAN_J_CODES` set in `engine/activation/codes.py`, and make `engine.pilot.help.known_codes()` include it (§3.5; pre-approved in Plan E's area). Each later milestone adds its §5.7 codes to the set and to `configs/pilot/help.yaml` in the same change, so both checks in `tests/unit/pilot/test_help.py` keep passing.
  - Write `docs/pilot/PROSPECT_LEDGER.md`, one row per prospect. It records:
    - the stack survey: CEP or CRM, messaging provider, sender registration (for example a DLT entity in India), WhatsApp Business Account, send-log export;
    - E3 (legal read), E5 (packaging choice), E7 (is the CEP's own control group enough for finance?);
    - the CFO's view of a readout the vendor sent itself;
    - proof-of-value start and conversion dates;
    - publication terms.
  - Write `docs/pilot/templates/pod_effort_log.csv`, one row per person-day per client and cycle. The pod logs into it from cycle 1, so strategy checkpoint 7 (≤1.5 people by cycle 3) can be measured. M115 adds the summary and reads the log.
  - Reword `docs/CONNECTIONS.md`, `docs/V1_READINESS.md` and the `engine/connections/base.py` module docstring to cite DEC-1300.
- **Where in the code.**
  - `PARALLEL_WORK_PROTOCOL.md` §1, §3 and §4; `tests/unit/test_shared_file_markers.py`; `docs/DECISIONS.md`; `docs/plans/`; `README.md`; `scripts/check_readme.py`; `docs/CROSS_BRANCH_REQUESTS.md`
  - `Makefile`, `pyproject.toml` (markers), `.github/workflows/nightly.yml`, `tests/integration/test_design_rules.py`, `engine/activation/codes.py` (new), `engine/pilot/help.py` `known_codes()` (§3.5)
  - `engine/connections/base.py` (module docstring only, pre-approved), `docs/CONNECTIONS.md`, `docs/V1_READINESS.md`, `docs/pilot/PROSPECT_LEDGER.md` (new), `docs/pilot/templates/pod_effort_log.csv` (new)
- **Contracts, APIs, config, error codes.** No runtime contract. DEC-1300…1309. The marker strings above.
- **Acceptance.**
  - `test_shared_file_markers` passes, with PLAN-J present once, in order after PLAN-G, in all 12 files.
  - The design-rule test fails on a planted `ScheduleKind.SEND_AFTER_DELAY` and on a planted config key `wait_days`. It passes with a planted `SEND_LOG_PULL`.
  - A new test asserts that `docs/CONNECTIONS.md`, `docs/V1_READINESS.md` and the `engine/connections/base.py` module docstring each cite DEC-1300, and no longer say a connector never writes without that qualification.
  - `make lint test` and `check_readme` are green, with only M90's tests mapped.
- **Effort:** 2 engineer-weeks. **Depends on:** —.

#### M91 — Consent fixes and the purpose-level consent snapshot

- **Why.**
  - **Uplift runs skip the consent ledger.** `UpliftScoreFlow._actions` (`engine/uplift/flow.py` ~line 1110) overrides the method that `engine/pipeline.py` ~line 1874 rebinds to `_consent_gated_actions`. Uplift runs therefore never consult the ledger and write no `consent_report.json`.
  - **Four contacting use cases have no purpose.** `configs/privacy.yaml` `use_case_purposes` omits `retail-win-back`, `bank-term-deposit`, `insurance-cross-sell` and `card-default-propensity`, so none of them is ever gated.
  - **The SMS footer is wrong for India.** "Reply STOP to opt out" (`engine/config.py:995`, `configs/engine.yaml:255`) does not work with India's one-way DLT sender IDs.
  - **Copy approval records the wrong person.** It stores `body.approved_by` instead of the signed-in principal.
  - **No list can be released yet.** The release rule needs a consent snapshot id, and no code produces one.

  None of this can be repaired once a list is inside a CEP.
- **Scope.**
  - `UpliftScoreFlow._actions` calls the existing `consent_gate_for_run` and `apply_consent_gate` (`engine/privacy/consent.py`) before `apply_uplift_actions`. This is a pre-approved edit in Phase 3b's area; the PHASE-4B block is not touched. Uplift runs then write `consent_report.json`.
  - `ConsentReport` gains optional `consent_snapshot_id`. It is a hash of four things: the purpose, the client, `ledger_as_of`, and the sorted ledger rows read (principal hash, status, recorded_at). It is computed in `apply_consent_gate` (pre-approved edit in Phase 4B's area). The no-ledger path is covered by the builder in M94.
  - Add purposes:
    - `retail-win-back`, `bank-term-deposit`, `insurance-cross-sell` → `marketing_communication`;
    - `card-default-propensity` → `account_servicing` (collections and early-intervention reminders), confirmed by legal.

    A new test fails when any use case with `actions.contacts_customers: true` lacks a purpose.
  - `configs/privacy.yaml` gains optional `requires_legal_opinion: true` per purpose or use case (read by the release rule in M100).
  - Add `generative.campaign_copy.sms_sender: one_way | two_way`, defaulting to `two_way` (today's behaviour).
    - With `one_way`, the SMS required line is the `{{opt_out_link}}` field.
    - Add `OPT_OUT_LINK_FIELD = "opt_out_link"` beside `UNSUBSCRIBE_FIELD` in `engine/generative/win_back.py` (pre-approved, Phase 3a's area). Add it to `_RESERVED_FIELDS`. `allowed_placeholder_fields` returns it for SMS when `sms_sender == one_way`, and it is filled like `unsubscribe_link`. Without this, `allowed_fields_only` blocks every one-way template, or the strict render fails.
    - A new deterministic guardrail rule, `sms_reply_stop_one_way`, in `configs/guardrails.yaml` blocks "Reply STOP".
    - A pilot that sends SMS through one-way sender IDs (for example India's DLT) sets `one_way` in M93. The v1 prompt files are not edited.
  - Copy approval identity comes from the principal. The existing `GET /runs/{run_id}/copy_messages.csv` gains an `approved_only` query parameter (default false, so today's response is unchanged).
- **Where in the code.**
  - `engine/uplift/flow.py` (`UpliftScoreFlow._actions`, pre-approved), `engine/privacy/{consent,contracts}.py` (pre-approved, additive)
  - `configs/privacy.yaml`, `engine/privacy/config.py`, `tests/unit/production/test_privacy_purpose_coverage.py` (new)
  - `engine/config.py` (§3.5: `CampaignCopyConfig.sms_sender`), `configs/guardrails.yaml`, `engine/generative/win_back.py` and `api/routes/generative.py` (pre-approved, Phase 3a), `docs/UPLIFT.md`
- **Contracts, APIs, config, error codes.**
  - DEC (one entry):
    - (a) consent gate on uplift runs through the existing helpers;
    - (b) consent snapshot definition;
    - (c) purpose-coverage rule, including `card-default-propensity`;
    - (d) one-way SMS opt-out link;
    - (e) approver identity from the principal.
  - Error code `CONSENT_PURPOSE_MISSING`, raised at config load.
  - Guardrail code `SMS_REPLY_STOP_ONE_WAY`.
- **Acceptance.**
  - A new integration test, which **fails on today's `main`**: an uplift scoring run of a purpose-mapped use case with a `withdrawn` ledger record suppresses that customer as `consent_false` and writes `consent_report.json` with a `consent_snapshot_id`.
  - On the same input and ledger, a propensity run and an uplift run suppress the same rows.
  - The same ledger state gives the same snapshot id. One changed ledger row changes it.
  - Removing a contacting use case from `use_case_purposes` fails the coverage test.
  - With `sms_sender: one_way`, generated SMS copy containing "Reply STOP" is blocked. A template ending in `{{opt_out_link}}` passes `allowed_fields_only` and `required_lines`, and renders. With the default, existing generative tests pass unchanged.
  - `approve_copy_template` records the signed-in principal whatever the body says.
- **Effort:** 2 engineer-weeks. **Depends on:** M90.

#### M92 — Deployment code: storage prefixes, client store, security findings and the off-by-default write stack

- **Why.** Nothing has run on real AWS yet: M50 is waiting on P1. Known blockers would fail on day one:
  - `infra/naming.py` `OBJECT_PREFIXES` covers only `uploads/`, `runs/`, `models/` and `_bootstrap/`. Several prefixes the product writes would get AccessDenied: `connections/` (the assessment's first step), `ai_service/`, `reference_sets/`, `pilot/feedback/`, `clients/`, `datasets/` and `indexes/`.
  - The client store is SQLite on ephemeral Fargate disk.
  - `docker-compose.yml` sets `MARKETING_AI_DATABASE_URL`, which is not a setting.
  - SECURITY_REVIEW F-6 logs bind parameters to CloudWatch.
  - F-4 runs scheduled jobs with the API task role.
  - F-3 lets that role defeat S3 versioning.
  - F-1 does not verify the database certificate.

  Real personal data sits in the account from week 1, so the high finding F-6 and the medium findings F-1, F-3 and F-4 close now. A CIO must also be able to install with zero write or send permission.
- **Scope.**
  - Add `clients/`, `datasets/`, `indexes/`, `connections/`, `ai_service/`, `reference_sets/`, `pilot/feedback/`, `campaigns/` (reserved for M98) and `activation/` to `OBJECT_PREFIXES`. The `activation/` prefix holds rosters, deletion manifests and delivery state. Add the row-level prefixes (`clients/`, `datasets/`, `campaigns/`, `activation/`) to `ROW_LEVEL_PREFIXES`.
  - Add a new test, `tests/infra/test_storage_prefixes.py`. It enumerates every key builder and `*_PREFIX` constant the engine writes with and fails if one is outside `OBJECT_PREFIXES`.
  - Move `ClientStore` to the platform Postgres (SQLModel, alembic `0006_client_store.py`). This is a pre-approved edit in Phase 2's area. SQLite stays for local runs.
  - Fix `docker-compose.yml` and the `migrate` help line in `Makefile` to name `MARKETING_AI_POSTGRES_DSN`.
  - Close four security findings together:
    - F-1: `sslmode=verify-full`, with a certificate required on any environment holding real data.
    - F-3: move lifecycle and `DeleteObjectVersion` grants to the job role.
    - F-4: give the job task role its own role.
    - F-6: `log_parameter_max_length=0` and `log_parameter_max_length_on_error=0` in the parameter group in `infra/database.py`, with a CDK assertion beside the `rds.force_ssl` one.
  - Add `infra/activation.py`, an `ActivationStack` that is **off by default**. When on, it holds `ActivationWriteRole`:
    - `s3:PutObject` and `s3:GetObject` (which also covers HEAD) on one prefix of the client's activation bucket;
    - `secretsmanager:GetSecretValue` on `marketing-ai/<env>/destinations/*`;
    - a trust policy that lets the API and job task roles assume it.

    Context keys: `activation_enabled`, `activation_bucket`, `activation_prefix`.
  - Add the runtime setting `MARKETING_AI_ACTIVATION_ENABLED` (add-only, default false). M97 uses it to refuse any write-purpose connection while it is false.
  - Write the threat-model notes for write credentials. The full review lands in M116.
- **Where in the code.**
  - `infra/{naming,database,operations,policies,context,app}.py`, `infra/activation.py` (new), `tests/infra/`
  - `engine/clients.py` (Phase 2's area, pre-approved), `engine/platform_db.py`, `alembic/versions/0006_client_store.py`, `engine/settings.py` (§3.5: `Settings.activation_enabled` and its `ENV_VARS` entry, add-only), `infra/naming.py` `SETTINGS_FIELDS` += `activation_enabled` (kept in step for `tests/infra/test_settings_contract.py`), `docker-compose.yml`, `Makefile` (`migrate` help line)
  - `docs/{SECURITY_REVIEW,CLIENT_ISOLATION}.md`

  Infra edits are pre-approved in Phase 4a's area.
- **Contracts, APIs, config, error codes.**
  - The `ClientStore` protocol is unchanged; a Postgres implementation is added beside the SQLite one.
  - CDK context keys are declared in `infra/context.py`. IAM statement `ActivationWritePolicy`.
  - DEC (one entry): (a) client store on Postgres; (b) write role in a separate, off-by-default stack, plus the runtime switch; (c) closure of F-1, F-3, F-4 and F-6.
- **Acceptance.**
  - `cdk synth` and cdk-nag are clean with activation on and off.
  - `tests/infra` checks the role grants:
    - With activation off, no role can write outside `OBJECT_PREFIXES`.
    - With it on, the only new grants are exactly `ActivationWritePolicy` on the configured prefix.
    - The job task role differs from the API task role and alone holds `DeleteObjectVersion` and lifecycle grants.
  - The storage-prefix test passes.
  - `ClientStore` contract tests pass on SQLite and Postgres (postgres marker; `check_no_skips` green).
- **Effort:** 4.5 engineer-weeks. **Depends on:** M90.

#### M93 — First deployments: Minfy staging, then the pilot's account

- **Why.** The assessment is sold as running inside the client's account. Its first deployment will surface failures nobody has seen, so it goes to staging first. It needs its own milestone so that the code milestones (M98 onwards) do not wait on the client's calendar.
- **Scope.**
  - Run `docs/M50_CHECKLIST.md` end to end:
    - first in a Minfy staging account;
    - then in the pilot's account, or in a Minfy-hosted account the client approves in writing.

    A Minfy-hosted account means **one deployment per client**: one stack set and one data store, never two clients in one deployment. Record the results in `docs/M50_CHECKLIST.md` §9 (Results).
  - Turn on `auth_mode=local` with separate Analyst and Approver users (P6, DEC-1308). If the pilot sends SMS through one-way sender IDs (for example India's DLT), set `sms_sender: one_way` in the pilot deployment's config.
  - Add `scripts/smoke_client_account.py`, beside the outside-only `scripts/smoke_deployment.py`. It runs, on synthetic data and logging no data values:
    1. connection;
    2. import;
    3. Guided setup;
    4. train;
    5. approve, as a second user;
    6. score.

    It also asserts that the deployment holds one `client_id`.
  - Replace NOT YET MEASURED cells in `docs/AWS_DEPLOYMENT.md` only with values measured through `engine/aws/cost_capture.py`.
- **Where in the code.** `scripts/smoke_client_account.py` (new), `docs/{M50_CHECKLIST,AWS_DEPLOYMENT}.md`, the pilot's use-case config.
- **Contracts, APIs, config, error codes.** None new. DEC (one entry): (a) rule for Minfy-hosted accounts.
- **Acceptance.**
  - The smoke script passes in staging and in at least 1 client or client-approved account.
  - The trainer approving their own model gets `403 SEPARATION_OF_DUTIES`.
  - A CloudWatch Logs Insights search for `parameters:` returns nothing after a write.
  - The database connection fails without a valid certificate.
- **Effort:** 2 engineer-weeks, plus the Minfy cloud team (outside these figures). **Depends on:** M91, M92. M93 starts only after both halves of M92 (infra and the Postgres client store) have merged.

#### M94 — Activation Contract 0.9: schema, pure builder, business reasons, profiles, dry-run and key-match kit

- **Why.**
  - **The hand-off is how campaign managers judge the product first**, and five research areas flag it.
  - **One table reaches most tools.** One versioned table is read natively by about 8 of the 15 platforms researched, so no per-platform API code and no CEP credentials are needed.
  - **The hand-off can be tested before the code exists.** Checkpoint 1 (the tool cannot take in the list within 3 weeks) is tested in weeks 1–3 with a hand-made file in the draft format.
  - **Other failures sit in the details:**
    - The join key can silently fail to match in the CEP.
    - Reason text built from raw features would leak feature names and values into the client's tool.
    - Measurement needs the "would have been selected" flag (DEC-624) carried through the hand-off.
- **Scope.**
  - **Week 1.** Publish `docs/activation/contract_v0_9_draft.md`, with a hand-made `generic_csv` example, for E1 (the dry run).
  - **New package `engine/activation/`:** `contract.py` (`ActivationManifest`, `ActivationRow`, `ContractBundle`; §5.1), `build.py`, `delta.py`, `config.py`, `profiles.py`, `reasons.py`, `validate.py`, `errors.py`.
  - **`build_contract(run_artefacts, config, catalogue, now)` is pure.** It reads only a finished scoring run's artefacts, for propensity and uplift runs alike:
    - `scores.parquet`, `scoring_summary.json`, `run.json`;
    - `row_explanations.parquet`;
    - `consent_report.json` when present.

    It writes `holdout_assignment.parquet` beside the contract. In M94, `holdout_member` comes from today's `control_group` and `explore` is 0. Whichever of M94 and M95 merges second switches it to the holdout service (M95). `scores.*` stay byte-identical.
  - **Consent fields:**
    - With a ledger report, `consent_snapshot_id` and its `as_of` come from it.
    - With only a mapped consent column, the builder computes `file:` plus a hash of the consent and opt-out columns and the source upload's fingerprint. Its `as_of` is the upload's extract time.
    - With neither, the builder refuses the run with `ACTIVATION_CONSENT_SOURCE_MISSING`, rather than emit an empty list.
  - **Flag rules:**
    - `treat_flag`:
      - uplift runs: `action == "Treat"`;
      - propensity runs: the row is eligible, not a holdout member, and its band is in `activation.treat_bands` (default: the top band);
      - or the row is in the explore slice.
    - `intended_flag`: selected, or a holdout member the policy or `treat_bands` would have selected. Explore rows are excluded. This follows DEC-624 and comes from `intended_treatment` on uplift runs.
  - **Rows:**
    - Holdout rows are **exported** with `mai_holdout_flag=1`, never left out.
    - Suppressed rows are exported with their reason.
  - **Reasons.** `configs/activation/reasons.yaml` maps each feature or feature family to a plain phrase with no values. It reuses `engine/pilot/results._feature_names`/`_humanise`. `mai_reason` holds the phrase only. An unmapped reason is null and counted in the manifest.
  - **Key check.** `customer_key` must be the source's own text. A key column stored as a number (which loses leading zeros) is refused with `ACTIVATION_KEY_NOT_TEXT` unless `activation.key_must_be_text: false`.
  - **Payload.** The payload follows an allow-list of about 10 business fields. Model feature columns and values never appear.
  - **Delta.** `diff_contract(previous, current)` is pure. It returns the changed rows plus `row_status=delete` for customers who left.
  - **Profiles:**
    - `configs/activation/profiles/{generic_csv,generic_parquet}.yaml`;
    - plus the pilot's tool once it is known: one of `moengage_s3`, `clevertap_list`, `braze_cdi`, `netcore_s3`, `sfmc_sftp_csv`.

    Profiles rename and encode only. Each profile carries its vendor's limits in `max_attributes`/`max_row_bytes` (for example, Netcore 20 attributes; Braze CDI 250 attributes and 1 MB per row).
  - **Storage.** Artefacts go to `runs/<id>/activation/{activation.parquet, activation_manifest.json, holdout_assignment.parquet}`. They are registered in a new `ACTIVATION_ARTEFACTS` / `ACTIVATION_TABULAR_SCHEMAS` map (not the pinned `ARTEFACT_REGISTRY`), and in `configs/privacy.yaml` and `engine/privacy/layout.py`.
  - **Dry-run kit.** `scripts/activation_dry_run.py` writes an obviously synthetic contract (`synthetic=true`, `SYN-` keys) for any profile. It also writes a **key-match sample**: a small DPO-approved set of real keys, or hashed CEP ids to compare. The dry-run report records the match rate (target ≥95%) and the key format: string, leading zeros, case.
  - **Docs:**
    - `docs/ACTIVATION.md`: the spec, with loading recipes for MoEngage S3, CleverTap list import, Braze CDI, Netcore S3 and SFMC SFTP;
    - `docs/activation/contract_v1.schema.json`, generated from the models;
    - `docs/activation/DRY_RUN.md`: timing, error log, key-match result;
    - templates for the Minfy team: the holdout source-of-truth agreement, and the send-log clause naming the export (Braze Currents, MoEngage S3 Exports, CleverTap Event Exports).
  - **Fields reserved now.** Holdout fields and catalogue fields (`mai_action_id`, `mai_action_label`, `mai_channel`, `mai_contactable_channels`, `mai_channel_reason`) are defined now as nullable, so M95 and M109 fill them without a schema change.
- **Where in the code.**
  - `engine/activation/*` (new), `engine/config.py` (§3.5: `UseCaseConfig.activation`), `configs/engine.yaml`, `configs/activation/reasons.yaml` (new)
  - Read only: `engine/contracts.py` `scores_csv_columns` and `ScoringSummary`, `engine/uplift/flow.py` `scores_columns`, `engine/stages/explain.py` `ROW_EXPLANATIONS_FILENAME`
  - `configs/privacy.yaml`, `engine/privacy/layout.py`, `scripts/activation_dry_run.py`, `docs/ACTIVATION.md`, `docs/activation/`, `tests/unit/activation/`, `tests/fixtures/activation/`
- **Contracts, APIs, config, error codes.**
  - §5.1. No route yet; M100 adds them.
  - Error codes: `ACTIVATION_RUN_NOT_SCORED`, `ACTIVATION_DISABLED`, `ACTIVATION_CONSENT_SOURCE_MISSING`, `ACTIVATION_KEY_NOT_TEXT`, `ACTIVATION_PROFILE_UNKNOWN`, `ACTIVATION_PROFILE_ATTRIBUTE_CAP`, `ACTIVATION_ROW_TOO_LARGE`, `ACTIVATION_PROFILE_COLUMN_MISSING`.
  - DEC (one entry):
    - (a) field list and `mai_*` naming;
    - (b) `treat_flag` and `intended_flag` rules;
    - (c) holdout exported as an attribute and as a roster;
    - (d) customer key in clear and kept as text, a departure from the memo's "customer token", signed by the DPO;
    - (e) reasons as phrases with no values;
    - (f) profiles rename and encode only.
- **Acceptance.**
  - **Row coverage.** Every scored entity appears exactly once.
  - **Invariants.** On every row:
    - `treat_flag=1` implies `holdout_flag=0`, `suppress_reason` null and consent `granted`;
    - `intended_flag=1` implies `explore_flag=0`;
    - no sleeping dog is ever treated (re-checked with a `RuntimeError`).
  - **No feature leaks:**
    - A test reads `prepare.json` for every trainable use case and asserts that no feature column appears in any profile output.
    - A second test asserts that no feature name and no source cell value appears in `mai_reason` or `payload`.
  - **Key handling:**
    - A key column with leading zeros survives byte-for-byte.
    - A numeric key column is refused with `ACTIVATION_KEY_NOT_TEXT`.
    - A run with no ledger and no consent column is refused with `ACTIVATION_CONSENT_SOURCE_MISSING`.
  - **Determinism.** The same input gives byte-identical `activation.parquet` whatever the row order, and the manifest's sha256 values match the files.
  - **Delta.** Two identical runs give an empty delta. A customer missing from the new run gives exactly one `delete` row.
  - **Profiles.** The golden file for each shipped profile matches. A payload one attribute over a test-fixture profile's limit (`tests/fixtures/activation/profiles/cap20.yaml`, `max_attributes: 20`) fails with `ACTIVATION_PROFILE_ATTRIBUTE_CAP`.
  - **Off by default.** With `activation.enabled=false`, nothing is written and `scores.*` are byte-identical to `main`.
  - **Erasure.** Erasure's whole-store scan finds and rewrites a principal in `activation.parquet`.
- **Effort:** 3.5 engineer-weeks. **Depends on:** M90, M91.

#### M95 — Holdout service: persistent hash-threshold holdout, roster and explore slice

- **Why.** Today the control group is salted with `seed_from(run_id)` and taken as the smallest digests **among eligible rows**. So:
  - monthly re-scoring reshuffles who is held out;
  - the holdout differs between use cases;
  - membership shifts whenever eligibility changes.

  Once a list is inside a CEP, a wrong holdout cannot be repaired, and every long-run claim is contaminated. Cycle 2 also needs randomised history outside the target before it can learn uplift, and an explore slice creates that history.
- **Scope.**
  - New package `engine/holdout/` (§5.3). Membership uses a threshold rule:
    `int(sha256(f"{salt}:{scope_key}:{entity}")[:16], 16) < fraction × 2^64`.
    It is decided per entity over the whole population, independent of run, eligibility and row order. Smaller fractions nest inside larger ones (5% ⊂ 10%).
  - Scopes:
    - `run`: the default, today's rule;
    - `use_case`: `scope_key` is the use-case id;
    - `universal`: `scope_key = "universal"`;
    - `external`: reads the client's control-group flag column.
  - **Salt.** `MARKETING_AI_HOLDOUT_SALT` (SecretStr, an add-only setting) is fingerprinted in `platform_setting`, like `privacy_salt`.
    - A missing salt with a persistent scope raises `SettingsError`.
    - A changed fingerprint refuses scoring with `HOLDOUT_SALT_CHANGED`.
  - **Epochs:**
    - raising the fraction keeps every member;
    - lowering it, or rotating the salt, starts a new epoch, which is Admin-only and audited;
    - a delivery or roster built under a different epoch than the stored `HoldoutSpec` is refused (`HOLDOUT_EPOCH_MISMATCH`). Campaigns that span two epochs are refused separately in M98 (`CAMPAIGN_EPOCH_MISMATCH`).
  - **Wiring into scoring.**
    - `_control_mask` and `_entity_control_mask` delegate to the service when the scope is not `run` (DEC-1306 edit). This makes `control_group = eligible ∧ member`.
    - Uplift inherits this through `apply_actions` unchanged.
    - Under `scope: run`, behaviour is byte-identical.
  - **`holdout_assignment.parquet`.** M94 and M95 run in parallel. Whichever merges second switches the builder (M94) to take this file from the service, recording `holdout_member` for every row including suppressed rows. Its columns: key, `holdout_member`, `explore`, `explore_propensity`. `scores_csv_columns` is not changed.
  - **The explore slice.** `actions.explore_fraction` runs from 0 to 0.10, default 0. A second salt label, `explore`, marks rows that are all of these: eligible, not a holdout member, not selected, not suppressed and not a predicted sleeping dog. The per-row propensity is recorded for `ope.evaluate_policy`.
  - `measure_offered` reads the effective fraction.
  - **The roster.** A `holdout_roster` contract kind lists every held-out key in the **customer master**. That is the entity source of the client's onboarding spec, or the scored population when there is none. It is refreshed each cycle, so the CEP can exclude new customers too, from **every** campaign.
  - **Routes.** `GET /holdout` (Viewer: spec, fingerprint, epoch, roster size) and `PUT /holdout` (Admin, audited). `PUT /holdout` is the only way to set `external_agreement_ref`, the recorded agreement an `external` scope needs. A Settings entry, "Holdout".
  - **DS:** design review, binomial tolerances, and the nested-fraction trade-off note.
- **Where in the code.**
  - `engine/holdout/{__init__,spec,assign,salt}.py` (new)
  - `engine/stages/actions.py` `_control_mask`, `_entity_control_mask`, `_holdout_size` (DEC-1306; DEC-A4 behaviour kept for `run`)
  - `engine/config.py` (§3.5: `ActionsConfig.holdout`, `explore_fraction`), `engine/settings.py` (§3.5: `Settings.holdout_salt` and its `ENV_VARS` entry, add-only, plus `SECRET_FIELDS` += `holdout_salt`, so `redacted()` and `summary()` hide it), `infra/naming.py` (`SETTINGS_FIELDS` and a `holdout_salt_secret_name`), `engine/privacy/config.py` (pattern), `engine/uplift/measure.py` `measure_offered` (pre-approved)
  - `api/routes/holdout.py` (new), `api/main.py` (PLAN-J), `ui/modules/activation/`, `configs/engine.yaml`, `infra/` (Secrets Manager entry), `docs/UPLIFT.md` §14
- **Contracts, APIs, config, error codes.**
  - §5.3.
  - Error codes: `HOLDOUT_SALT_MISSING` (SettingsError, 503), `HOLDOUT_SALT_CHANGED`, `HOLDOUT_EPOCH_MISMATCH`, `HOLDOUT_EXTERNAL_COLUMN_MISSING`.
  - DEC (one entry): (a) threshold holdout and scopes; (b) salt lifecycle and epochs; (c) external scope needs a recorded agreement (`external_agreement_ref`); (d) explore slice; (e) roster from the customer master.
- **Acceptance.**
  - **Stability.** On 10k keys with universal scope, membership is identical across all of these changes:
    - 12 synthetic monthly run ids;
    - 2 use cases;
    - 20% of rows becoming suppressed;
    - shuffled row order.
  - **Share and nesting.** On 1M keys, the realised share is within binomial 99.9% bounds. The 5% members are a strict subset of the 10% members.
  - **Composite keys.** Each entity has one membership, and no entity appears in two arms.
  - **Default unchanged.** With `scope: run`, every test in these files passes unchanged:
    - `tests/unit/test_actions.py`, including `test_a_different_run_id_draws_a_different_holdout`;
    - `tests/unit/uplift/test_profit_curve.py`.
  - **Consistency.** Propensity and uplift runs give the same membership for the same entity.
  - **Roster coverage.** A customer in the master but not scored this cycle still appears on the roster when they are a member.
  - **Sleeping dogs.** No sleeping dog is treated, explore rows included.
- **Effort:** 3 engineer-weeks (DS 0.5). **Depends on:** M90; DEC-1306 signed.

#### M96 — Readiness sign-off and a neutral pilot kit

- **Why.**
  - **Effects are small.** Realistic effects are 1–3 points. An underpowered pilot gives a range that crosses zero even when the campaign worked. That reads as failure and is "the likeliest way to lose the deal".
  - **A wrong label fails silently.** A wrong outcome label is the likeliest silent pilot failure. Labels have no grace window today, and lapse-style outcomes exist in every industry: no purchase, recharge or renewal within N days.
  - **Uplift may have no history to learn from.** It needs randomised history the client may not have.
  - **The kit still speaks telecom.** The assessment is sold to any B2C business from week 0, but several pieces still default to telecom or contradict this plan:
    - the data request (`default_use_cases [telco-churn, …]`);
    - the pre-flight line;
    - the demo name ("Demo Telecom");
    - the copy prompts, which are written for win-back only;
    - `docs/pilot/PLAYBOOK.md`, which is telecom-scoped and on a 10-week calendar;
    - the data request's pseudonymisation guidance (the client HMACs every customer ID with a secret key "Minfy never needs"), which conflicts with J7.
- **Scope.**
  - **Planner.** `engine/measurement/planner.py` is pure and uses `statistics.NormalDist`, as `incrementality.py` does. Functions:
    - `mde_two_proportions(n_treat, n_control, base_rate, alpha, power)`;
    - `n_for_mde`;
    - `holdout_for_mde`;
    - `achieved_power`;
    - `cost_of_holdout(n_holdout, uplift_per_contact, value_per_conversion)`;
    - `cost_of_explore(n_explore, contact_cost, offer_cost)`.

    Both cost functions return a Money range, or null with a reason.
  - `POST /measurement/power-preview` (Viewer; numbers only, no customer data).
  - **Readiness report.** `engine/pilot/readiness.py` gains two sections:
    - **"Can we measure it?"**: MDE at 3/5/10/15% holdouts, from the data request's counts. The base rate comes from the dataset, labelled with its source.
    - **The label**: the definition in words, base rate per snapshot month, positive count, the leak-probe verdict, and `LABEL_RATE_UNSTABLE` when the month-over-month rate jumps beyond tolerance.
  - **Lapse labels.** Optional `LabelDefinition.grace_days` and `exclude_roles`, compiled in `engine/onboarding/labels.py`. `future_window_clause` and `LABEL_WINDOW_NOT_ENFORCED` are kept.
  - **Treatment-history audit** in the readiness report (`engine/pilot/readiness.py`). Data Doctor (DEC-1220, the placeholder-code step) is not changed. On the treatment column the analyst names, it runs `engine/uplift/checks.treatment_predictability` and reports random, model-selected or unknown (`TREATMENT_HISTORY_NOT_RANDOM`). Agent hints for detecting the column follow in M104.
  - **Sufficiency verdict** against `uplift.min_arm_rows` and `min_arm_positives`. Either "uplift now", or "propensity + random control (+ explore) first, uplift from cycle 2".
  - **Neutral pilot kit** (pre-approved edits in Plan E's area):
    - `configs/pilot/data_request.yaml`: a generic default use case, a neutral pre-flight line, and pseudonymisation guidance rewritten for in-account deployments. The guidance offers two options: the clear key (J7), or a client-held HMAC stored as a CEP attribute, and says which applies.
    - `engine/pilot/demo.py` and `scripts/seed_demo.py`: demo name "Demo Company".
    - New `.v2.md` prompts with `campaign_goal` and reader-role variables. The v1 files are never edited.
    - `docs/pilot/PLAYBOOK.md` rewritten for Plan J: phases, release rule, plan registration, reconciliation, Proof Pack, and success criteria tied to the phase gates.
    - A `configs/use_cases/repurchase_30d.yaml` template for J10.
  - **DS:**
    - runs E2 (power feasibility) and E4 (retrospective readouts, by hand) for every qualified prospect;
    - with the pod, writes the pilot's use-case YAML: label with grace window, purpose, outcome window.
- **Where in the code.**
  - `engine/measurement/{__init__,planner}.py` (new), `engine/uplift/incrementality.py` (`Z_95` reused unchanged)
  - `engine/pilot/{readiness,demo}.py`, `engine/config.py` (§3.5: `LabelDefinition`), `engine/onboarding/labels.py`, `engine/uplift/checks.py`, `engine/contracts.py` (§3.5: `CHECK_CODE_TABLES`)
  - `configs/pilot/data_request.yaml`, `configs/prompts/*.v2.md`, `configs/use_cases/repurchase_30d.yaml`, `scripts/seed_demo.py`, `docs/pilot/PLAYBOOK.md`
  - `api/routes/measurement.py` (new: `POST /measurement/power-preview`), `api/main.py` and `api/schemas.py` (PLAN-J: router registration, `PowerPreviewRequest`/`PowerPreview`), `configs/pilot/help.yaml`, `docs/UPLIFT.md` §3, `docs/DATA_CONTRACT.md` §11
  - The `Makefile` `demo-seed` help and the `engine/settings.py` demo_mode description (wording only)

  These are pre-approved edits in the Phase 2, Phase 3a (`configs/prompts/*.v2.md`), Plan E and Phase 3b areas.
- **Contracts, APIs, config, error codes.**
  - `PowerPreviewRequest{eligible, base_rate, holdout_shares[], explore_share=0, alpha=0.05, power=0.8, value_per_conversion?, contact_cost?, offer_cost?}`.
  - `PowerPreview{points[{holdout_share, n_treat, n_control, mde_pp, cost_of_holdout|null, cost_of_explore|null, reason}], basis}`.
  - Check codes `LABEL_RATE_UNSTABLE` and `TREATMENT_HISTORY_NOT_RANDOM`.
  - DEC (one entry): (a) power formula and base-rate source; (b) lapse labels; (c) sufficiency thresholds; (d) treatment-history audit; (e) neutral pilot defaults and pseudonymisation guidance.
- **Acceptance.**
  - **Planner accuracy.** The planner matches the closed-form two-proportion formula to within 1%. At 80% power and two-sided 95%:
    - 4% → 3% needs about 5,300 per arm;
    - 10% → 8% needs about 3,200 per arm.
  - An unknown base rate gives null with a reason.
  - **Labels.** A golden test: a 30-day lapse label with a 7-day grace period gives the hand-computed positive count. A feature inside the grace window is caught as `FUTURE_EVENTS_LEAKED`.
  - **Treatment audit.** Model-selected treatment is reported "not random"; a randomised fixture is reported "random".
  - **Sufficiency.** The verdict switches exactly at the thresholds.
  - **Pilot kit:**
    - `make pilot-check` is green with no `telco-churn` default.
    - `test_one_check_contract` is updated in the same change.
    - `jargon_in` finds nothing in any planner or readiness message.
- **Effort:** 4 engineer-weeks (DS 1.5). **Depends on:** M90.

**Phase 1 exit gate.**
- *Build gate:*
  - M90–M96 are merged, and `make lint test` and `make test-all` are green.
  - The 0.9 contract JSON schema is published, with golden files for every shipped profile.
- *Pilot gate* (decision date: week 6):
  - The instance in the client's (or client-approved) account passes M50 acceptance with sign-in on.
  - The readiness report has been delivered from the client's own data.
  - **Hand-off dry run (E1):**
    - The draft-format contract was loaded into the client's real tool with the holdout excluded, with the time and every error logged, in **3 weeks or less**.
    - The key-match rate is **≥95%** and the key format is recorded.

    If either fails, the checkpoint-1 discussion is held before Phase 2.
  - The contract is **frozen at 1.0** (recorded in a DEC) once the E1 dry run and the builder's golden files agree. Until then, Phase 2 builds on 0.9 with additive-only changes (DEC-1302).
  - Agreed and signed in writing:
    - the label definition and the first use case (J10);
    - the holdout source of truth and fraction, at an MDE no larger than the written 1–3 point expectation (or the outcome or segment is changed);
    - the explore share and its cost;
    - the named send-log export;
    - the DPO's acceptance of plain customer keys (J7);
    - **the consent feed**: the client's withdrawals, DND and any consent-manager feed reach our ledger, or a mapped consent column, at least every `max_consent_age_days`;
    - publication terms for the result, anonymised if needed;
    - the legal opinions for any purpose marked `requires_legal_opinion`, or that use case waits.
  - The prospect ledger is recorded for every qualified prospect.

---

### Phase 2 — First list live (weeks 6–10; list about weeks 10–11)

**Goal.** Deliver an approved model's list into the client's bucket, in their tool's format, through the release rule. The campaign is pre-registered on a Campaign record and launches from the client's own tool.

**Client value.**
- The campaign manager builds the audience in the tool they know, with no reformatting.
- The sponsor signs a plan that states three things: what effect is detectable, when results arrive, and what the holdout and explore slice cost in rupees.
- A signed webhook can trigger their Zapier or n8n flow.

**Capacity note.** Phase 2 is booked at its full engineering capacity of 13 engineer-weeks, so it has no slack. If the pilot's tool needs a warehouse or SFTP destination, that is +2.5 engineer-weeks and moves the first list by about 1 week.

#### M97 — Connector SDK: Destination and SendLogSource protocols, conformance kit, Fake, Local and S3, signed webhook

- **Why.**
  - **Define each side once.** Define the write side and the send-log read side once, each with a fake and a shared conformance suite. Every later tool then costs a profile file, not a new integration.
  - **Keep the read-only promise.** A separate protocol keeps the read-only `Connector` promise intact.
  - **Most tools read from S3.** S3 plus a webhook covers most CEPs.
  - **No write credential outside the activation stack.** Write credentials must live only where the activation stack governs them. Otherwise "zero write permission when off" is untrue for connections that carry their own keys.
- **Scope.**
  - `engine/activation/destinations/base.py`: the `Destination` protocol (§5.2), stateless like `Connector`, with `DeliveryReceipt` and `DestinationError(code, message, fix)`.
  - Implementations:
    - `FakeDestination`: in memory, with injectable failures.
    - `LocalDirDestination`.
    - `S3Destination`:
      - on AWS it assumes `ActivationWriteRole` through STS;
      - inline keys are accepted only when `settings.env` is `local` or `dev`;
      - writes go to one prefix, with `PutObject` and `ContentMD5`;
      - data files are written first and the manifest last;
      - delivery is idempotent on the manifest sha;
      - it reuses `S3Connector._session` and `_client` and the `engine/audit/export.py` sink pattern.
    - `WebhookDestination`:
      - an HMAC-SHA256-signed "list ready" POST carrying ids, counts and hashes only;
      - headers `X-MAI-Signature` and `X-MAI-Timestamp`, with a 5-minute replay window;
      - a new additive `net.check_webhook_url` (https only). It refuses loopback, private (RFC 1918 and fc00::/7), link-local, multicast and unspecified addresses on every resolved address. It connects to the pinned resolved address, so DNS rebinding cannot bypass the check. It raises `WEBHOOK_TARGET_REFUSED`. No redirects, bounded retries. `check_url` is unchanged, because read connections may reach databases on a private network;
      - on AWS the signing secret is a Secrets Manager reference.
  - **Connection records.**
    - `KindInfo.group` gains `destination`.
    - `ConnectionRecord` gains `purpose: read | write` (default `read`) and `secret_ref` (additive).
    - On AWS a write connection must use `secret_ref` (`DESTINATION_INLINE_SECRET_REFUSED`).
    - While `MARKETING_AI_ACTIVATION_ENABLED` is false, creating or using a write connection is refused with `ACTIVATION_WRITES_DISABLED`.
    - Write kinds test reach, sign-in and a probe write (a new `StepName.PROBE_WRITE` step). They never run the read-only check and never delete.
    - `S3Destination.test_write` refuses credentials that can read the source prefixes. It asks through an `IamSimulator` protocol, with a fake in tests. On real AWS an unknown answer is a warning.
    - `/connections/{id}/import` refuses a write connection with `CONNECTION_PURPOSE_MISMATCH`. Delivery to a read connection is refused by the release rule with `DESTINATION_READ_ONLY_CONNECTION` (§3.3, M100).
  - **Send-log side.** `engine/activation/sendlog/` holds the `SendLogSource` protocol, `SendLogEvent`, `SendLogMapping`, and a pure `normalise_send_log(frame, mapping)` (§5.5). M102 ships the formats.
  - **Conformance suites.** `tests/conformance/{destination_suite,send_log_suite}.py`, parametrised over every registered kind. They check:
    - idempotent re-delivery;
    - manifest written last;
    - no manifest after a partial failure;
    - bounded timeouts;
    - no secret in any error or log.
  - **Docs.** The webhook verification recipe for n8n and Zapier goes in `docs/ACTIVATION.md`. The egress entry goes in `docs/SECURITY_REVIEW.md`.
- **Where in the code.**
  - `engine/activation/destinations/{base,registry,fake,local,s3,webhook,iam}.py` and `engine/activation/sendlog/{contract,mapping}.py` (new)
  - `engine/connections/base.py` (`KindInfo.group` += `destination`; `StepName.PROBE_WRITE` and its `STEP_LABELS` entry, additive), `store.py` (`purpose`, `secret_ref`), `registry.py`, `s3.py` (reuse), `net.py` (pre-approved, additive: `check_webhook_url`)
  - `api/routes/connections.py` (write-test wording), `ui/modules/connections/` (render the `destination` group), `tests/conformance/` (new), `docs/CONNECTIONS.md`
- **Contracts, APIs, config, error codes.**
  - §5.2 and §5.5.
  - Error codes: `CONNECTION_PURPOSE_MISMATCH`, `DESTINATION_WRITE_DENIED`, `DESTINATION_NOT_WRITE_SCOPED`, `DESTINATION_UNREACHABLE`, `DESTINATION_INLINE_SECRET_REFUSED`, `ACTIVATION_WRITES_DISABLED`, `WEBHOOK_TARGET_REFUSED`. The existing `CONNECTION_NEEDS_ADDON` (raised when boto3 is missing) gets its first `help.yaml` entry and joins `PLAN_J_CODES`.
  - DEC (one entry): (a) Destination protocol and write-purpose connections; (b) write secrets only through the activation stack; (c) delivery atomicity (manifest last); (d) webhook payload, signature and target check; (e) canonical send-log schema.
- **Acceptance.**
  - The conformance suite is green for fake, local, S3 (moto with STS) and webhook (fake receiver).
  - With the IAM fake answering "can read the source bucket", `test_write` refuses the credential.
  - A manifest never exists without all its data objects, including when an upload fails partway.
  - Write connections are refused in two cases: with activation disabled, and with an inline secret when `env` is `prod`.
  - **Webhook:**
    - A webhook to `169.254.169.254`, a loopback or a private address, or to a host that resolves to one, is refused with `WEBHOOK_TARGET_REFUSED`.
    - The documented recipe verifies the signature.
    - A canary test proves the payload holds no customer value.
  - **Nothing else changes.** Test output for existing read connections is byte-identical to `main`. The fast suite needs no network, and boto3 is imported lazily.
- **Effort:** 4 engineer-weeks. **Depends on:** M90, M92, M94.

#### M98 — Campaign record and the one measurement path

- **Why.**
  - **One object to attach to.** Measurement is tied to our own scoring run today (`_finished_scoring_run`, `RUN_NOT_SCORED`). Many later features need one object to attach to: the send log, the test plan, reconciliation, the Proof Pack, audit and programme readouts, and any outcome fee. Without it, each would build its own store.
  - **One computation.** The canonical computation today is `create_campaign_results` in `api/routes/uplift.py`, and `/runs/{id}/measure` delegates to it. All three routes must end in one function.
  - **Like for like.** A Campaign assignment must compare like with like. That means the intended population, not every band.
  - **Real treatment time.** Treatment time defaults to the scoring run's `finished_at`. When a CEP activates days later, maturity dates are wrong.
- **Scope.**
  - **Campaign store.** `engine/measurement/campaign.py` holds `Campaign` (§5.6) and the `CampaignStore` protocol, with a SQL implementation and an in-memory fake (alembic `0007_campaigns.py`). The same migration adds a nullable `alert.campaign_id` column, declared on `AlertRow` in the same change so the migration drift test stays green, and unused until M103's alerts. No later milestone edits a merged migration. Kinds: `activated`, `external` and `programme`. The latter two are used from M104.
  - **Artefacts** live under `campaigns/<id>/`: `assignment.parquet`, `send_log.parquet`, outcomes, `incrementality_report.json`, `test_plan.json`. They are registered in privacy and layout.
    - `assignment.parquet` is built from the contract: key, arm, `intended_flag`, band, explore.
    - Treated and control are compared inside `intended_flag = 1`, per DEC-624.
  - **The measure function.** `engine/measurement/measure.py::measure_campaign(assignment, outcomes, *, run_id, primary_key, outcome_column, positive_label=None, intended_column=None, bands=None, treatment_time, treatment_date_column=None, outcome_window_days=None, as_of, campaign_id=None, send_log=None, plan=None) -> IncrementalityReport` is a pure wrapper over `measure_incrementality` and `campaign_verdict`. `as_of` is always supplied by the caller: `utc_now()` lives in the route, never in the function.
  - **Routes:**
    - `POST /campaigns {run_id}` (Analyst): kind `activated`, from a run's contract;
    - `GET /campaigns`, `GET /campaigns/{id}`;
    - `POST /campaigns/{id}/outcomes {upload_id}`;
    - `POST /campaigns/{id}/measure`.
  - **DEC-1305 is implemented:**
    - `create_campaign_results` calls `measure_campaign` (pre-approved edit in Phase 3b's file);
    - `POST /runs/{id}/measure` keeps its behaviour;
    - `/outcomes` (M49) is model monitoring. It keeps writing `incrementality_input.json` as a descriptive input only, never presented as a campaign result. Every measured result goes through `measure_campaign`;
    - `engine/pilot/roi.py` reads the campaign report first and takes a measurement key (run or campaign).
  - **Treatment start.** `treatment_start` is the first send event if a send log exists, else the first delivered receipt's `finished_at`. `results_available_on` follows from it.
  - **Epochs.** A campaign whose deliveries span two holdout epochs is refused (`CAMPAIGN_EPOCH_MISMATCH`).
  - **Results.** Results lists campaigns beside runs through a new list source in `ui/modules/simple/pages.js` (pre-approved edit in Plan H's area). `registerResultLink` only links under one run.
  - **DS:** checks that `measure_campaign` reproduces the existing numbers.
- **Where in the code.**
  - `engine/measurement/{campaign,measure}.py` (new), `api/routes/campaigns.py` (new), `api/main.py` / `api/schemas.py` (PLAN-J), `api/access_policy.py`
  - `api/routes/uplift.py` `create_campaign_results` and `api/routes/measure.py` (pre-approved: delegate), `engine/uplift/incrementality.py` and `measure.py` (reused unchanged), `engine/scheduling/outcomes.py` (`outcome_window` reused)
  - `engine/pilot/roi.py` (pre-approved), `engine/platform_db.py`, `alembic/versions/0007_campaigns.py`, `engine/scheduling/alerts.py` (`AlertRow.campaign_id` column only; pre-approved, §3.5), `configs/privacy.yaml`, `engine/privacy/layout.py`, `ui/modules/simple/pages.js`, `docs/UPLIFT.md`
- **Contracts, APIs, config, error codes.**
  - §5.6.
  - Error codes: `CAMPAIGN_NOT_FOUND`, `CAMPAIGN_NOT_MATURED` (with `results_available_on`), `CAMPAIGN_EPOCH_MISMATCH`.
  - DEC (one entry): (a) Campaign object and kinds; (b) assignment from the contract with the intended flag; (c) treatment-start rule; (d) ROI reads the campaign report first.
- **Acceptance.**
  - `tests/integration/uplift/test_measure_campaign.py` and `test_campaign_results_phase1.py` pass unchanged.
  - On an uplift run with every row mature and a fixed `as_of`, all three routes give `IncrementalityReport`s that are identical except for `computed_at` and `campaign_id`: `/campaign-results`, `/runs/{id}/measure` (compared through `MeasureView.report`) and `/campaigns/{id}/measure`.
  - On a propensity run, `/campaigns/{id}/measure` equals `/runs/{id}/campaign-results` called with `bands = activation.treat_bands`.
  - On an uplift run, the campaign's estimate equals the run's own `intended_column` estimate.
  - Outcomes before maturity give `CAMPAIGN_NOT_MATURED` with a date, never a partial number.
  - Erasure removes a principal from `campaigns/` (whole-store scan).
  - Route-policy and denied-role tests are green.

  Assertions that need a delivery are in M100.
- **Effort:** 3 engineer-weeks (DS 0.5). **Depends on:** M90, M92, M94, M95.

#### M99 — Test plan and pre-registration

- **Why.** Peeking and moving goalposts turn a real effect into a disputed one. The plan must be fixed in the audit log before the list leaves: metric, outcome kind, covariate, arms, holdout, analysis dates, MDE. It is also a precondition for settling any outcome fee.
- **Scope.**
  - `GET /campaigns/{id}/plan-preview?holdout=` (Viewer) returns computed points only. They come from M96's planner and the run's eligible counts, with holdout and explore costs.
  - `POST /campaigns/{id}/plan` (Analyst) freezes the `TestPlan` (§5.6) with a `plan_hash`.
    - The plan carries `outcome_kind`, an optional `covariate_column` (so a revenue or CUPED analysis can be pre-registered for campaign 1) and optional `secondary_analysis_dates` (for example 60 and 90 days).
    - The route calls `set_audit_context(request, after_hash=content_hash(plan), details={"plan_hash": plan.plan_hash})`, so the event's `after_hash` equals `content_hash(plan)`. By default the middleware would hash the raw response bytes instead. `plan_hash` is added to `DETAIL_KEYS` as a reviewed change.
  - **Amendments.** Re-POSTing an identical body returns the stored plan with the same `plan_hash` and writes no new version. A different body gives `409 TEST_PLAN_EXISTS`. An amendment is `POST /campaigns/{id}/plan/amendments {reason, ...}` (Analyst, audited). It writes version n+1 with `amends` set, and both versions are kept.
  - **At measurement** the realised holdout, window, population, outcome kind and covariate are compared with the plan (`TEST_PLAN_CHANGED`, modelled on `RETENTION_PLAN_CHANGED`; the bare `PLAN_CHANGED` is already the retention route's audit `reason_code`). A read before `analysis_date` is labelled "early look", with no final verdict.
  - `PLAN_UNDERPOWERED` is a plain warning, never a silent block.
  - The UI adds a "Plan the test" card on the campaign's page, with a slider over computed points (DEC-1204). M99 creates the `ui/modules/activation/` module and its registration, because the hand-off panel does not exist yet. M100 embeds the same card in the hand-off panel.
- **Where in the code.** `engine/measurement/plan.py` (new), `api/routes/campaigns.py`, `engine/audit/events.py` `DETAIL_KEYS` (pre-approved), `engine/uplift/contracts.py` `IncrementalityReport` (+ `test_plan_hash: str | None = None`, `early_look: bool = False`; pre-approved, additive), `ui/modules/activation/{index,plan}.js` (new), `ui/index.html` and `ui/modules/router.js` (PLAN-J registration only), `configs/pilot/help.yaml`.
- **Contracts, APIs, config, error codes.**
  - `TestPlan` (§5.6).
  - Error codes: `TEST_PLAN_EXISTS`, `TEST_PLAN_REQUIRED`, `TEST_PLAN_CHANGED`; warning `PLAN_UNDERPOWERED`.
  - DEC (one entry): (a) pre-registration through the audit `after_hash` and the `plan_hash` detail key; (b) early look against final; (c) underpowered as a warning; (d) secondary analysis dates; (e) amendments as new versions.
- **Acceptance.**
  - Re-POSTing an identical body returns the same `plan_hash` and writes no new version. A different body gives `409 TEST_PLAN_EXISTS`. An amendment writes version 2 with `amends` set, and version 1 is still readable.
  - The audit event's `after_hash` equals `content_hash(plan)`.
  - Measuring before `analysis_date` returns "early look", and jsdom confirms that the UI shows no final verdict.
  - Changing the holdout or the covariate after the plan gives `TEST_PLAN_CHANGED`.
  - The slider shows only server points.
- **Effort:** 1.5 engineer-weeks (DS 0.5). **Depends on:** M96, M98.

#### M100 — Hand-off in the product: activation API, release rule, delta, erasure and withdrawal propagation, run screens

- **Why.**
  - **A surface for the campaign manager.** The interfaces need a governed, auditable surface that a campaign manager can use inside Plan H's four pages.
  - **The rule becomes code.** This milestone is where DEC-1303 becomes code. That covers deliveries and also every row-level download path, which would otherwise bypass the rule.
  - **Withdrawals must follow the list.** Consent withdrawn after a list has gone out must reach the client's tool, not only erasures.
- **Scope.**
  - **Routes** in `api/routes/activation.py`:
    - `POST /runs/{run_id}/activation` (Analyst): builds the contract;
    - `GET /runs/{run_id}/activation` (Viewer): manifest summary and receipts;
    - `GET /runs/{run_id}/activation.csv` (Analyst, `audit_reads=True`, release-checked);
    - `POST /runs/{run_id}/activation/deliveries {destination_id, campaign_id, mode: full|delta}` (Analyst; audit action `activation.deliver`) and `GET …/deliveries`;
    - `POST /holdout/roster/deliveries {destination_id}` (Analyst);
    - `PUT /activation/legal-opinions {purpose | use_case_id, reference, valid_until}` (Admin, audited).
  - **Release rule** enforcement (§3.3), with its own code and help entry for each refusal.
    - Where a use case has `activation.enabled: true`, the rule also gates every row-level download: `GET /runs/{id}/scores.csv`, `GET /runs/{id}/copy_messages.csv` and `GET /runs/{id}/artefacts/{name}` for `scores.csv`, `scores.parquet` and `copy_messages.csv`. The check goes inside `api/routes/runs.py::read_artefact`, which `read_scores` already calls (pre-approved edits in `api/routes/{runs,generative}.py`). When the rule fails, an Analyst may take a review copy with the treat and holdout columns blanked and a reason recorded.
    - `fake` and `local_dir` are exempt only for synthetic runs (`RunRecord.synthetic`, from M101) or `env` in {local, dev}.
    - Roster deliveries are checked only for `auth_mode=local` (`DELIVERY_REFUSED_AUTH_OFF`), a write-purpose destination (`DESTINATION_READ_ONLY_CONNECTION`) and contract expiry (`ACTIVATION_CONTRACT_EXPIRED`). Deletion manifests and withdrawal suppression deltas are checked only for `auth_mode=local` and a write-purpose destination. They are never refused for model approval, holdout scope, consent age, legal opinion, test plan, expiry or the beats-risk check.
  - **Delta state.** An `activation_delivery` table, keyed `(destination_id, use_case_id, kind)`, holds the last delivered manifest sha. A `legal_opinion` table sits beside it. Both are in alembic `0008_activation.py`. Delta mode diffs against the last delivered file, and a full re-sync is available.
  - **Re-delivery** is idempotent by sha256. `runs/<id>/activation/receipts.json` is append-only. The first delivery sets the campaign `live` and records `treatment_start`.
  - **Erasure hook.** When a principal is erased, a `deletion` manifest (customer keys only) goes immediately to every destination that received that use case **or a roster**. The next delta carries `row_status=delete`. The erasure completion report lists those destinations as a record of processors.
  - **Withdrawal hook.** When the ledger records a withdrawal for a principal treated in a live contract (before `valid_until`), a suppression delta (`treat_flag=0`, `consent_status=denied`) goes immediately to every destination that received it.
  - **Webhook.** It fires after each successful delivery when configured.
  - **Audit details.** `DETAIL_KEYS` gains `destination_id`, `rows_written` and `manifest_sha256` (reviewed). A review copy records its reason as the existing `reason_code`, from a closed list in `configs/activation/`. Any free-text note is stored with the receipt, not in the audit log (DEC-705).
  - **Pilot profile.** The pilot's tool profile and recipe are updated with what the dry run actually needed.
  - **UI** in `ui/modules/activation/` (the module M99 created):
    - a run action, "Hand off the list", on finished scoring runs of contacting use cases with an approved model;
    - a panel with server counts (treat, intended, holdout, suppressed, explore), valid-until, the profile's filter instructions and receipts;
    - M99's "Plan the test" card, embedded in the panel;
    - no new navigation item.
- **Where in the code.**
  - `api/routes/activation.py`, `api/main.py` and `api/schemas.py` (PLAN-J), `api/access_policy.py`, `api/routes/{runs,generative}.py` (pre-approved: release check on downloads), `engine/approvals.py` (`separation_refusal` reused)
  - `engine/activation/{deliver,state,release}.py` (new), `engine/platform_db.py`, `alembic/versions/0008_activation.py`
  - `engine/privacy/{erasure,erasure_jobs,consent}.py` (pre-approved hooks), `engine/audit/events.py`, `configs/privacy.yaml`
  - `ui/modules/activation/{api,views}.js` (new) and `index.js` (extended from M99)
  - `configs/activation/profiles/<pilot_tool>.yaml`, `docs/activation/recipes/<pilot_tool>.md`
- **Contracts, APIs, config, error codes.**
  - The routes above, `DeliveryReceipt` (§5.2) and §3.3's codes.
  - DEC (one entry): (a) release-rule implementation, including downloads and exemptions; (b) delta rule; (c) erasure deletion manifests, including roster destinations; (d) withdrawal suppression deltas; (e) legal-opinion record; (f) new `DETAIL_KEYS` and the review-copy `reason_code` list.
- **Acceptance.**
  - **Release rule.** Each refusal has its own test, and the full valid path delivers to moto S3.
    - A snapshot older than `max_consent_age_days` is refused.
    - A use case requiring a legal opinion with none recorded is refused.
    - `scope=external` with no `external_agreement_ref` is refused with `RELEASE_HOLDOUT_NOT_PERSISTENT`.
    - A deletion manifest for an expired contract with a stale consent snapshot is still delivered.
    - A run with no `synthetic` flag is not exempt on `fake` or `local_dir` when `env` is `prod`.
  - **Roles.** A Viewer gets 403. The trainer of an unapproved model cannot hand it off.
  - **Download gate.** A route-walking test fails if any row-level contact download for an activation-enabled use case skips the release check, including `GET /runs/{id}/artefacts/{name}` for `scores.csv`, `scores.parquet` and `copy_messages.csv`. `scores.csv` for use cases without activation is byte-identical to `main`.
  - **Delta.** A second delivery in delta mode, on a fixture with 3 changed customers, writes exactly 3 upserts plus one delete per erased principal.
  - **Erasure and withdrawal:**
    - The principal's key is in a deletion manifest sent to each destination that received the use case or the roster.
    - A withdrawal produces one suppression delta per such destination.
  - **Ordering.** The plan's audit event precedes the first delivery event.
  - **Treatment start.** Delaying the first delivery by N days moves `results_available_on` by N days.
  - **Audit.** Every delivery has one `activation.deliver` audit event whose `after_hash` matches the receipt.
  - **UI (jsdom).** The action appears only for contacting use cases with an approved model. Panels render server JSON only. The top bar still has exactly four items.
  - `docs/API.md` is regenerated.
- **Effort:** 5 engineer-weeks. **Depends on:** M91, M92, M94, M95, M97, M98, M99, M101.

#### M101 — Measurement validity harness and synthetic quarantine

- **Why.**
  - **No real-world validation yet.** All validation so far is synthetic. The demo's 12.7-point effect is planted, and the Criteo acceptance run cannot be done from this environment: `docs/LIBRARY.md` records 403s from every source.
  - **Two things to show before the first real readout:**
    - that our 95% intervals really cover 95%;
    - that planted numbers cannot be presented as results.
  - **A noisy nightly would get loosened.** A flaky nightly job would invite loosening a test. So the bands follow the Monte Carlo error.
- **Scope.**
  - **Simulator.** `engine/measurement/simulate.py` builds populations with a known effect, base rate and share of immature outcomes. M103 extends it with non-compliance and contamination.
  - **Statistical tests.** `tests/statistical/` (marker `statistical`) runs nightly with **fixed seeds**. In the `Makefile`, `make test` and `make test-all` add `and not statistical` to their `-m` filter, and the nightly job runs `pytest -m statistical tests/statistical`. It covers:
    - ITT coverage of `measure_incrementality`;
    - type-I error at zero effect;
    - bias when immature rows are excluded;
    - achieved power at M96's n, computed with the **same Newcombe decision rule** the measurement uses.

    Bands are set at ±4 Monte Carlo standard errors. Type-I uses 10,000 simulations. The rule is recorded in a DEC.
  - **Criteo is conditional.** If an owner obtains the sample through an approved channel before week 8, for example a manual download into a private bucket, the internal benchmark runs and is recorded in `docs/LIBRARY.md` (ruling R2: internal only). It ranks uplift learners against ranking by `p_control`. Otherwise the skip is recorded there, as Plan D M57 did. The existing tests that pin Criteo as planned stay unchanged until a run exists.
  - **Synthetic quarantine:**
    - `run.json` gains `synthetic: bool`, set by `scripts/seed_demo.py`, `engine/pilot/demo.py` and synthetic uploads;
    - `ReportDocument` gains `synthetic: bool = False`, and `render_html`/`render_pdf` in `engine/pilot/document.py` draw the "Synthetic data: planted effect, not a forecast" block when it is true. `engine/pilot/results.py` and `roi.py` set it from `RunRecord.synthetic`;
    - `test_docs_honesty` asserts that the phrase "12.7 points" appears in `docs/` and `README.md` only in demo docs, with `V1_READINESS.md`'s labelled demo line explicitly allowed.
- **Where in the code.**
  - `engine/measurement/simulate.py` (new), `tests/statistical/` (new), `library/run_engine.py`, `docs/LIBRARY.md`
  - `engine/contracts.py` (§3.5: `RunRecord.synthetic`), `engine/pilot/{demo,document,results,roi}.py` and `scripts/seed_demo.py` (pre-approved)
  - `tests/unit/test_docs_honesty.py`, `.github/workflows/nightly.yml`, `Makefile` (exclude `statistical` from `test` and `test-all`)
- **Contracts, APIs, config, error codes.**
  - `RunRecord.synthetic: bool = False`.
  - `ReportDocument.synthetic: bool = False`.
  - `simulate.population(n, base_rate, effect, *, compliance=1.0, contamination=0.0, immature_share=0.0, seed)`.
  - DEC (one entry): (a) statistical acceptance bands and seeds; (b) synthetic flag and quarantine.
- **Acceptance.**
  - **Coverage.** Nightly with fixed seeds, the 95% interval covers the true effect within ±4 standard errors of 95% (2,000 simulations), at base rates 2%, 5% and 20%.
  - **Type-I.** The false-positive rate at zero effect is within ±4 standard errors of 5% (10,000 simulations).
  - **Power.** Achieved power at the planner's n is within ±4 standard errors of 0.80, or the planner is corrected.
  - **Criteo.** Either the entry exists, marked "internal validation, non-commercial licence", or the skip is recorded.
  - **Banner.** ROI and results reports from a seeded demo run show the synthetic banner in HTML and PDF.
- **Effort:** 2.5 engineer-weeks (DS 2). **Depends on:** M96.

**Phase 2 exit gate.**
- *Build gate:*
  - M97–M101 are merged, and `make lint test` and `make test-all` are green.
  - An end-to-end test on fakes passes in the real order: score → contract → campaign → plan → delivery to moto S3 through the release rule → measure.
  - The nightly coverage harness is green.
  - `MILESTONE_TESTS` lists M90–M101.
- *Pilot gate* (decision date: week 11):
  - A real approved model's list was delivered by `POST /runs/{id}/activation/deliveries` into the client's bucket, and their CEP ingested it natively **with no manual reformatting**.
  - The receipt's sha256 matches the object in the bucket.
  - The campaign was built with `mai_treat_flag = 1`, excluding `mai_holdout_flag = 1`, and launched from the client's tool. The holdout roster was delivered as an exclusion segment.
  - The test plan was registered (an audit event with `plan_hash`) **before** the delivery timestamp, and the sponsor signed the written 1–3 point expectation.
  - On the real delivered file, treat rows exist, and none carries consent other than granted. The consent snapshot is no older than the configured age.

---

### Phase 3 — Reconcile and prove (weeks 10–16)

**Goal.** Phase 3 shows whether the list ran as planned and what it earned:
- Read the client's send log back and show whether the list ran as planned.
- Turn the matured outcome into a Value Proof Pack, in net INR or revenue.
- Sell audit and programme readouts while the window matures.
- Give staff a read-only customer view.
- Let sources, outcomes and consent come straight from saved connections.

**Client value.**
- By about week 13–14, the sponsor sees contact rate and contamination against the memo's targets.
- At maturity, finance gets the document that converts the pilot.
- Prospects can buy an audit readout of a past campaign within days.
- Care and campaign staff can look up why one customer is, or is not, on the list.

#### M102 — Send-log importer

- **Why.** Without the client's send log, nobody knows who was actually contacted:
  - Treated customers who were never reached dilute the lift.
  - Contamination of the holdout is invisible.

  We read the CEP's own export, and we hold no CEP credentials.
- **Scope.**
  - **Mapping files** in `configs/activation/sendlogs/`: `generic_csv` plus the pilot CEP's export only. That is one of Braze Currents, MoEngage S3 Exports, CleverTap Event Exports, Netcore Egress or the SFMC Tracking Extract.
    - Each comes with a fixture shaped from vendor documentation and labelled as such.
    - A pilot task records the first real anonymised export as a golden file.
  - **Native containers.** Most CEP exports are not CSV or Parquet. Braze Currents writes Avro, MoEngage and CleverTap write gzipped JSON lines, and the SFMC Tracking Extract is a zip of CSVs. The import and upload paths accept only CSV and Parquet today.
    - Each mapping declares a new `container` field: `csv`, `parquet`, `avro`, `jsonl_gz` or `zip_csv`.
    - `engine/activation/sendlog/ingest.py` reads the pilot CEP's native container and writes a canonical Parquet upload.
    - If the container is Avro, the `fastavro` dependency is added to the PLAN-J `pyproject.toml` block.
  - **Route.** `POST /campaigns/{id}/send-logs {upload_id | connection_id + selection, mapping_id, campaign_refs?}` (Analyst, audited).
    - Pulling fetches the raw object through the connector's `fetch` inside the send-log route. It does not go through `/connections/{id}/import`, which stays unchanged and CSV/Parquet-only.
    - Upload limits are those of the campaign's `use_case_id`. A campaign without one (external or programme, M104) names a use case in the request, whose upload limits apply.
    - It writes `campaigns/<id>/send_log.parquet` (row-level, registered) and a `SendLogImportReport`.
    - `first_sent_at` sets `Campaign.treatment_start`.
  - **Unknown or bad rows are counted, never filled or silently dropped:** unknown events, unmatched customers and unparseable timestamps.
  - **Aggregate fallback.** A fallback mapping, `granularity: aggregate`, handles the CEP's campaign-report exports. It is labelled "aggregate — no per-customer reconciliation", and CACE is null with a reason.
- **Where in the code.**
  - `engine/activation/sendlog/{ingest,formats}.py`, `configs/activation/sendlogs/` (new), `tests/fixtures/activation/sendlogs/` (new), `pyproject.toml` (PLAN-J block: `fastavro`, only if the pilot CEP exports Avro)
  - `api/routes/campaigns.py`; read only and unchanged: `api/routes/connections.py` `import_from_connection`, `configs/roles.yaml` `campaign_events` (vocabulary)
  - `configs/privacy.yaml`, `engine/privacy/layout.py`
- **Contracts, APIs, config, error codes.**
  - §5.5.
  - Error codes: `SEND_LOG_MAPPING_UNKNOWN`, `SEND_LOG_COLUMN_MISSING`.
  - DEC (one entry): (a) send-log formats and containers as config; (b) aggregate fallback labelling; (c) send-log retention.
- **Acceptance.**
  - A golden test per shipped format turns a vendor-shaped fixture, in its native container, into the canonical frame.
  - A log with 2% unknown customer ids reports them as unmatched, and the totals reconcile.
  - A 1M-row log imports within the existing upload limits, with only counts and timings in the logs.
  - Erasure removes the principal from `send_log.parquet`.
  - Audit details carry counts only (`DETAIL_KEYS` allow-list test).
- **Effort:** 2.5 engineer-weeks. **Depends on:** M97, M98.

#### M103 — Reconciliation, complier-adjusted lift and per-row treatment time

- **Why.**
  - **The memo sets two targets:** under 5% of holdout IDs contacted, and at least 70% of the treat list contacted.
  - **The CFO needs two numbers:** ITT, and the effect on the customers who were actually reached.
  - **Any contamination counts.** Contamination by **any** campaign, not just ours, would break the main differentiator.
- **Scope.**
  - **New inputs and fields.** `measure_incrementality` takes an optional `reached_column` (pre-approved additive edit), joined in `measure_campaign` from the send log. It is not named `contacted`: in this codebase `contacted` already means the assigned arm (`engine/uplift/measure.py` `TREATMENT_COLUMN`) and is a default treatment hint. `IncrementalityReport` gains these optional fields:
    - `contact_rate_treated` and `contact_rate_control`, counting sends of the campaign's own `campaign_refs` only;
    - `holdout_contacted_rows` and `holdout_contacted_rate` (contamination, counting any campaign in the log);
    - `compliance`;
    - `cace`: ITT ÷ (`contact_rate_treated` − `contact_rate_control`), with a 95% within-arm bootstrap interval;
    - `cace_reason`, `reconciliation_verdict`, `reconciliation_basis`.
  - **Per-row treatment time** is the first `sent` timestamp for treated rows the send log shows as reached. Every other row (control rows, and treated rows with no `sent` event) uses the campaign's first send date (`Campaign.treatment_start`).
    - `measure_campaign` writes this date into the outcomes frame's existing `treatment_date_column`.
    - So no assigned row is lost to a missing date. Today a row with no readable date is dropped into `rows_without_outcome`, which would turn ITT into a contacted-only comparison.
  - **Reconciliation report.** `ReconciliationReport` (§5.6) gives a verdict of pass, warn or fail against `activation.reconciliation {max_holdout_contacted: 0.05, min_treat_contacted: 0.70}`, broken down by channel and by campaign. It is served at `GET /campaigns/{id}/reconciliation?format=html|pdf|json`, as ReportDocument kind `reconciliation`.
  - **Alerts:**
    - `AlertKind.HOLDOUT_CONTAMINATED` (critical above the threshold) and `LOW_CONTACT_RATE` (warning), with `SEVERITY_FOR` and `_TITLES` entries (the SNS sink looks up `_TITLES[alert.kind]`);
    - ids and counts only;
    - the alert carries the campaign id in a new nullable `Alert.campaign_id` (additive), stored in the nullable `alert.campaign_id` column that M98's alembic `0007` already added (no new migration);
    - deduplicated on (kind, campaign_id);
    - raised only for campaigns with a `use_case_id`, because `Alert.use_case_id` is required.
  - **UI.** The Campaign results page's row-count-only `reconciliationLine` (`ui/modules/uplift/views.js`) becomes a "Did the list run as planned?" card. The measure panel (`ui/modules/measure/index.js`) shows the same card from the server's reconciliation JSON.
  - **Simulator.** `simulate.py` gains non-compliance and contamination, with nightly CACE coverage under the M101 band rule.
  - **DS** owns the estimator, the interval and its coverage.
- **Where in the code.**
  - `engine/uplift/incrementality.py` and `contracts.py` (pre-approved, additive), `engine/measurement/{reconcile,measure,simulate}.py`
  - `engine/scheduling/alerts.py` (pre-approved; §3.5: `Alert.campaign_id`, the two `AlertKind` values, `SEVERITY_FOR` and `_TITLES`), `engine/pilot/document.py` (kind Literal; §3.5), `api/routes/campaigns.py`
  - `ui/modules/measure/index.js`, `ui/modules/uplift/views.js`, `docs/UPLIFT.md` §15/§17
- **Contracts, APIs, config, error codes.**
  - §5.6. `AlertKind.HOLDOUT_CONTAMINATED` and `LOW_CONTACT_RATE`.
  - DEC (one entry): (a) contamination counts any campaign, contact rates count only the campaign's own refs; (b) CACE estimator and interval; (c) thresholds and alerts; (d) per-row treatment time.
- **Acceptance.**
  - **Exact counts.** A simulated log with 3.0% contamination and 80% contact reports exactly 3.0% and 80.0%.
  - **Alert threshold.** The alert fires at 5.1% and not at 4.9%, exactly once per campaign.
  - **CACE coverage.** Nightly, the CACE 95% interval meets the coverage band at compliance 50–90%. A planted ITT of 1 point at 70% compliance gives a CACE of about 1.43 points.
  - **Maturity.** With a send log, maturity uses per-row sent dates. A test covers outcomes that `finished_at` would call mature but the sent dates do not.
  - **No row lost to a missing date.** A treated row with no send event stays in the ITT denominator, and `rows_without_outcome` does not grow.
  - **No send log, no change.** Without a send log, every report field is identical to today's (except `computed_at`), and `cace` is null with reason `NO_SEND_LOG`.
  - `test_profit_curve.py` is untouched and green.
- **Effort:** 3 engineer-weeks (DS 1.5). **Depends on:** M101, M102.

#### M104 — Audit and programme readouts

- **Why.**
  - **Audit readouts.** Audit mode is the fastest route to "net value proven against a control": a prospect's past randomised campaign gets a readout within days (E4, checkpoint 3). It also makes us the neutral auditor if host CEPs ship their own uplift.
  - **Programme lift.** The universal holdout's main research use is programme lift: does the whole CRM programme pay? It can also grade other tools' campaigns.
  - **Cheap to build.** The Campaign record already separates measurement from our scoring runs, so neither readout needs a fake run.
- **Scope.**
  - **Audit.** `POST /campaigns/audit` (Analyst) takes:
    - an assignment upload: key, arm 0/1, optional `sent_at`, optional `campaign_ref`, optional `intended_column` or `band_column`. It can come from a file or a connection import, including the CEP's control-group export;
    - an outcomes upload;
    - an optional send log;
    - column mappings.

    It creates a Campaign of kind `external` with source `audit | cep_export`. It maps the assignment to a scores-shaped frame (control = arm 0, intended from the optional column) and calls `measure_campaign` unchanged.
  - **No run behind the report.** For external and programme campaigns, the required `run_id` carries the campaign id, `campaign_id` is set, and no RunRecord is written.
  - **Causal flag set from the basis.** `measure_incrementality` sets `causal` whenever a control group exists. So `engine/measurement/audit.py` overrides `report.causal` from `Campaign.causal_basis` after `measure_campaign` returns.
  - **"Campaign X against our universal holdout."** The treated group comes from the client's send log for that campaign, picked by `CampaignAuditRequest.campaign_refs`. The control is universal-holdout members, optionally restricted to a client-supplied audience file. The basis is labelled.
  - **Programme readout.** `POST /campaigns/programme {period_start, period_end, outcome}` (Analyst) creates kind `programme`. It measures all non-holdout customers against universal-holdout members over the period on a business outcome: ITT, with CUPED once M108 lands.
  - **Causal labels:**
    - `causal=true` only for `engine_random` or `verified_random` (by `checks.treatment_predictability` when features are present).
    - `declared_random` is shown as "random by the client's statement, not verified".
    - Non-random assignment gets the `roi_document` "descriptive only" wording.
  - **Agent treatment hints.** Treatment hints are added to `AgentColumnHints` and `tools.find_roles`, reusing `uplift.treatment_column_hints`. The agent benchmark diff is reviewed and committed.
  - **Results.** Audits and programme readouts appear in Results beside runs (`ui/modules/simple/pages.js` list source).
- **Where in the code.** `engine/measurement/audit.py` (new), `api/routes/campaigns.py`, `engine/uplift/checks.py` (reused), `engine/pilot/roi.py`, `engine/agent/{config,tools,advisor,egress}.py` and `tests/fixtures/agent_bench/expected.json` (pre-approved, Plan G area), `ui/modules/activation/audit.js`, `ui/modules/simple/pages.js`.
- **Contracts, APIs, config, error codes.**
  - `CampaignAuditRequest` (with `campaign_ref_column` and `campaign_refs`) and `ProgrammeRequest` (§5.6).
  - Error codes: `CAMPAIGN_ARM_NOT_BINARY`, `CAMPAIGN_ARMS_TOO_SMALL`.
  - DEC (one entry): (a) causal-basis labels, and `causal` overridden from the basis; (b) external and programme campaigns share the Campaign store, with `run_id` carrying the campaign id and no RunRecord; (c) the "against the universal holdout" basis.
- **Acceptance.**
  - **Reproduction:**
    - An assignment plus outcomes built from an existing **propensity** scoring run, measured over all eligible rows, reproduces that run's `IncrementalityReport` field for field, except `run_id`, `campaign_id` and `computed_at`.
    - For an uplift run, the same holds when `intended_column` is supplied.
  - **Planted files.** A planted randomised file recovers its effect inside the 95% interval. A propensity-assigned file is labelled not causal, with the plain reason.
  - **Declared random.** A `declared_random` file is never labelled `causal=true`.
  - **Programme readout.** A programme readout on a simulated programme effect recovers it.
  - **Isolation.** `RunIndex` is unchanged. Erasure covers the new artefacts. The egress canaries and denied-role tests pass.
- **Effort:** 3 engineer-weeks (DS 0.5). **Depends on:** M95, M98, M102.

#### M105 — Value Proof Pack v1

- **Why.**
  - **The buyer is sceptical.** CFOs are the most sceptical buyers (40% in Gartner's survey).
  - **One view persuades most.** "Naive against incremental" is the most persuasive single view.
  - **It must trace.** This document converts the pilot and later settles any outcome fee, so every number in it must trace to a measured artefact.
  - **It cannot wait.** It must be ready on the first analysis date, so it is priced with the existing `RoiInputs` and does not wait for value weighting.
- **Scope.**
  - `engine/pilot/proof.py::build_proof(campaign_id)` composes these artefacts:
    - `test_plan.json`;
    - `incrementality_report.json`, with reconciliation;
    - the activation manifest and receipts;
    - `segments.json` (uplift runs);
    - `pilot_roi_inputs.json`;
    - `campaigns/<id>/segment_effects.json` (new). `measure_campaign` writes it. Per band and per segment (from `assignment.parquet`), it gives the treated and control rows, conversions, and the Newcombe interval of the difference. No other artefact holds a measured per-band or per-segment effect: `segments.json` has only predicted means.

    Sections:
    1. The plan as registered (hash, MDE, analysis dates) against what ran.
    2. Whether the list ran (contact rate, contamination).
    3. Incremental outcomes with 95% ranges: ITT, and CACE when available. Revenue outcomes are shown as INR ranges when the report is continuous (M108).
    4. Gross against incremental conversions.
    5. Naive credit (treated conversions × value) beside measured credit (incremental × value). Beside them, credit as the CEP reports it, entered and labelled as the CEP's figure.
    6. Offer cost spent on sure things and sleeping dogs: uplift runs only, otherwise "not measured" with the reason. Treated counts come from `segment_effects.json`.
    7. The cost of the holdout and of the explore slice.
    8. Backfire per band or segment, read from the intervals in `segment_effects.json`. An interval entirely below zero is flagged, with a "suppress next cycle" suggestion that an Analyst must approve.
    9. Net INR as a 95% range.
    10. Persistence: results at each pre-registered secondary analysis date, or "not yet" with the date.
    11. Method and limits: holdout scope, salt fingerprint, epoch, audit event ids, the causal basis (with a watermark for `declared_random`), and the approximation labels from `docs/UPLIFT.md` §15.
  - ReportDocument kind `proof`, served at `GET /pilot/proof/{campaign_id}?format=html|pdf|json` (Viewer). Synthetic runs are refused. The plain-language check applies. A Results link is added.
- **Where in the code.** `engine/pilot/proof.py` (new; pre-approved addition in Plan E's area), `engine/measurement/measure.py` (writes `segment_effects.json`), `configs/privacy.yaml` and `engine/privacy/layout.py` (register the new artefact), `engine/pilot/{document,roi,plain}.py`, `api/routes/pilot.py` `POLICIES`, `configs/pilot/help.yaml`, `ui/modules/pilot/screen.js`, `ui/modules/simple/pages.js`.
- **Contracts, APIs, config, error codes.**
  - `ProofView` and `SegmentEffects` (`segment_effects.json`) (§5.6).
  - Error codes: `PROOF_SYNTHETIC_DATA`, `PROOF_NOT_MATURE`.
  - DEC (one entry): (a) proof contents; (b) naive-against-measured definitions; (c) backfire rule, read from measured per-segment intervals; (d) provenance rule.
- **Acceptance.**
  - **Provenance.** A test walks `ProofView` and resolves every number to an artefact field. Any number it cannot resolve fails the build. A missing artefact renders "not measured" with its reason, never 0.
  - **Backfire.** A segment with a planted negative effect is flagged as backfire; a neutral one is not.
  - **Value checks.** On the planted fixture, `naive_value ≥ measured_value`, and the net range equals `compute_roi` on the same inputs.
  - **Refusals and labels.** A synthetic run gives `409 PROOF_SYNTHETIC_DATA`. A `declared_random` audit carries the watermark.
  - **Rendering.** HTML and PDF render, and `jargon_in` finds nothing. The pack holds no row-level data.
- **Effort:** 3.5 engineer-weeks (DS 1). **Depends on:** M99, M100, M101, M103, M108.

#### M106 — Read-only customer view

- **Why.**
  - **The memo calls for it.** The memo puts a "read-only customer view (band, reason, history) inside our app" in the "we build" column.
  - **Frontline staff need it.** Care and save-desk staff need to see why one customer is, or is not, on the list. They should see it without a CRM or a lookup API, both of which stay out of Plan J.
- **Scope.**
  - `POST /customers/lookup {customer_key}` (Viewer). It is a POST with the id in the body, per DEC-746, so the middleware audits every lookup as it does `/privacy/consent/lookup` (`audit_reads` applies only to GET). It returns:
    - the latest band, segment and business-reason phrase per use case;
    - holdout membership and epoch;
    - the suppression reason;
    - contract, delivery and campaign history: receipts and campaign assignments.
  - It reads precomputed artefacts only: the latest contract per use case, sorted by key so a lookup reads one row group, plus the campaign assignments. It never scores on request.
  - A "Look up a customer" sub-screen in Results. It is not a fifth navigation item.
  - An optional `care_desk_csv` profile (top reasons, action, "likely sure thing, do not discount"), delivered over SFTP in M114, **only if** the pilot's care team asks for it.
- **Where in the code.** `api/routes/customers.py` (new), `engine/activation/lookup.py` (new), `ui/modules/activation/lookup.js`, `ui/modules/simple/pages.js`.
- **Contracts, APIs, config, error codes.** `CustomerView{principal_hash, by_use_case[...], holdout{member, epoch}, history[...]}`. The key is never echoed, per DEC-746. The UI keeps the typed key only in the input, per DEC-756. Error code `CUSTOMER_NOT_FOUND`. DEC (one entry): (a) customer view scope; (b) audit.
- **Acceptance.**
  - A lookup of a held-out customer shows membership, no treat flag, and the epoch.
  - Every lookup writes exactly one audit event (`customers.lookup`, `object_id` the lookup id, the customer only as `principal_hash`). No key appears in a URL, a log or a response.
  - The UI shows only server JSON, and the top bar still has four items.
- **Effort:** 2 engineer-weeks. **Depends on:** M94, M98, M100.

#### M107 — Connection-backed inputs: sources, outcomes and consent from saved connections

- **Why.** The "layer on your stack" claim fails if a person must download and upload files every cycle. Today:
  - A connection import becomes only a flat upload.
  - `POST /clients/{id}/sources` accepts only a multipart file.
  - Scheduled scoring re-reads only uploaded tables.
  - A Guided-setup (`DataRecipe`) model cannot be scheduled at all (`RECIPE_DATASET_UNSUPPORTED`).
  - Outcomes, which live in the client's warehouse, are uploaded by hand.
  - The consent ledger fills only from a CSV upload (`POST /privacy/consent/imports`) or one record at a time (`POST /privacy/consent`). Nothing pulls from a saved connection or a consent manager.
- **Scope.**
  - **Sources.** `POST /clients/{id}/sources` accepts `{connection_id, selection}`. `SourceSpec` can bind to a connection with a selection rule: a fixed table, or the newest object under a prefix.
    - `latest_recipe_inputs` fetches a bound source fresh before `build_dataset`.
    - These are pre-approved edits in Phase 2's and Phase 4b's areas.
  - **Outcomes.** `POST /campaigns/{id}/outcomes` accepts `{connection_id, selection, date_from, date_to}`.
  - **Consent.** `POST /privacy/consent/imports` accepts `{connection_id, selection}`, so withdrawals, DND and consent-manager exports reach the ledger.
  - **Consent managers.** A `ConsentManagerSource` protocol with a fake (consume only), feeding the same ledger import.
  - **Query rule.** The milestone's DEC amends DEC-1105. Listing "newest under prefix" is allowed for S3 and Azure Blob. Outcome, send-log and consent tables may add one quoted date-window `WHERE` on a declared column for the SQL kinds (PostgreSQL, MySQL, Snowflake). BigQuery uses no SQL today (it reads with `list_rows`), so its date window is a filtered query job. There are still no projections and no free SQL.
  - **Scheduling rule.** The same DEC states that only `OnboardingSpec`-built models can be scheduled. The pod trains pilot 1's model through an onboarding spec. Guided setup stays the quick path for flat files.
- **Where in the code.** `api/routes/sources.py`, `engine/onboarding/{specs,sources,replay}.py`, `engine/scheduling/firing.py` `latest_recipe_inputs` (pre-approved), `engine/connections/{sql,s3,azure_blob,bigquery}.py` (date window: SQL `WHERE` for `SqlConnector` kinds, a filtered query job for BigQuery; newest under prefix for S3 and Azure Blob), `api/routes/{campaigns,privacy}.py`, `engine/privacy/consent.py`, `docs/{CONNECTIONS,ONBOARDING}.md`.
- **Contracts, APIs, config, error codes.**
  - `SourceSpec.binding{connection_id, selection, rule}` (additive).
  - Error code `CONNECTION_SELECTION_EMPTY`.
  - DEC (one entry): (a) the DEC-1105 amendment; (b) connection-bound sources; (c) scheduling only through onboarding specs; (d) consent-manager seam (consume only).
- **Acceptance.**
  - Replaying a spec whose source is bound to a fake S3 prefix picks up the newest object, with no upload.
  - A date-window outcome pull on the SQLite FakeServer returns only rows in the window, on a read-only session.
  - A consent-manager fake's withdrawal reaches the ledger and changes the next snapshot id.
  - Existing multipart sources behave exactly as on `main`.
- **Effort:** 3.5 engineer-weeks. **Depends on:** M90, M98.

#### M108 — Revenue outcomes and CUPED

- **Why.**
  - **Binary effects are hard to see.** Binary effects of 1–3 points are hard to detect, and holdouts are resented as money left on the table.
  - **CVM heads are judged on revenue.** Today a continuous outcome reads "No outcomes have been recorded" in `roi.py`.
  - **CUPED shrinks the holdout.** CUPED narrows ranges on continuous metrics, so a smaller holdout can detect the same effect.
  - **It must land before the first Proof Pack.** A sponsor whose power sheet is weak can then pre-register revenue for campaign 1.
- **Scope.**
  - `measure_incrementality` takes `outcome_kind: binary | continuous`, with a Welch interval on the difference in means.
  - **CUPED** through an optional `covariate_column` (the pre-period metric; the same name as in `TestPlan` and `MeasureRequest`), with theta fitted on the pooled sample.
    - The covariate is read point-in-time from the run's dataset snapshot.
    - New fields: `adjusted_lift`, `adjusted_interval` and `variance_reduction` (additive).
    - CUPED runs only when the test plan pre-registered the covariate. Otherwise it gives `TEST_PLAN_CHANGED`.
  - **Planner.** It gains a continuous MDE with an expected ρ², shown in the plan card.
  - **"Smaller holdout possible."** An advisory recommendation once variance reduction is measured. An Admin applies it as an audited epoch change.
  - **ROI.** `compute_roi` gets a continuous branch (INR ranges valued 1:1). The Proof Pack's revenue view in M105 reads it.
  - `MeasureRequest` gains `outcome_kind` and `covariate_column`.
- **Where in the code.** `engine/uplift/{incrementality,contracts}.py` (pre-approved, additive), `engine/measurement/{planner,measure,simulate}.py`, `engine/pilot/roi.py`, `api/routes/{measure,campaigns}.py`, `tests/statistical/`, `docs/UPLIFT.md` §15.
- **Contracts, APIs, config, error codes.**
  - `IncrementalityReport` additive fields: `outcome_kind`, `mean_difference`, `mean_difference_ci`, `adjusted_lift`, `adjusted_interval`, `variance_reduction`.
  - DEC (one entry): (a) continuous interval; (b) CUPED theta, pre-registration and reporting; (c) advisory holdout reduction.
- **Acceptance.**
  - Nightly simulation: coverage meets the M101 band, and with ρ = 0.6, `variance_reduction` is within ±0.03 of 0.36.
  - An uncorrelated covariate gives approximately the unadjusted result.
  - On a binary outcome, every pre-existing `IncrementalityReport` field is identical to the M103 result (except `computed_at`), and every M108 field is null.
  - A continuous outcome renders INR ranges in the ROI view.
- **Effort:** 3.5 engineer-weeks (DS 1.5). **Depends on:** M98, M99, M101.

**Phase 3 exit gate.**
- *Build gate:*
  - M102–M108 are merged, and `make test-all` is green.
  - The nightly ITT, CACE and continuous coverage are within their bands.
- *Pilot gate* (decision date: the registered `analysis_date`, about week 16 for a 30-day window):
  - A reconciliation report from the pilot's real send log, by about week 13–14, with **contamination under 5% and contact rate of at least 70%**. Otherwise a corrective cycle is agreed in writing (checkpoint 1).
  - On the registered `analysis_date`, the Value Proof Pack is generated from real data and read by the sponsor and finance **without a walkthrough**. The result is reported whichever way it falls. If the window is longer, this gate's date moves; its content does not.
  - At least one audit readout has been produced on a prospect's past campaign (E4), paid or free.
  - Finance has signed the value inputs.
  - No demo magnitude appears anywhere.
- *Re-plan:* Phases 4–5 are re-planned at this gate, using the first readout and the prospect ledger.

---

### Phase 4 — Cycle two, client two (weeks 16–24; provisional)

**Goal.** Make each monthly cycle better and cheaper:
- channel-aware lists ranked by net INR;
- uplift learned from cycle-1 randomised data;
- a computed answer to whether uplift beats risk ranking;
- a loop that needs a human only to approve.

Then bring a second client onto a different tool with configuration only.

**Client value.**
- Client 1's list respects per-channel consent and optimises rupees.
- The list is learned from client 1's own randomised result. Either it comes with written evidence that it beats plain risk ranking, or it falls back to risk ranking.
- The pod stops downloading, uploading and clicking.
- Client 2 onboards on its own CEP without engineering work.

#### M109 — Action catalogue and channel-aware contactability

- **Why.** Several gaps block regulated, channel-aware lists:
  - Today one consent flag covers every channel, and the action is free text that a CEP cannot map.
  - Regulated clients need consent per purpose and channel. Each action needs its template, message category and cost.
  - The DLT/TCCCPR rules bind every SMS sender in India, not only telecom companies. They are a region rule.
  - Value weighting reads channel and offer costs from the catalogue.
  - Copy messages render every channel for every row.

  Contract 1.0 already reserves the fields.
- **Scope.**
  - **Catalogue.** `configs/activation/catalogue.yaml` is validated by a frozen `ActionCatalogue` model (the `privacy.yaml` pattern). `catalogue_sha256` is stamped into every manifest.
  - **Use cases reference action ids additively:** `Band.action_id` and `uplift.policy.treat_action_id`. The free-text `Band.action` stays as the label.
  - **Suppression:**
    - `SuppressionConfig.channels {channel: {consent_column, contactable_column}}`, with per-channel suppression counts in a new optional `SuppressionCount.channel_counts: dict[str, int] | None` (for example `{"consent_denied_sms": n}`);
    - per-row channel reasons live in the activation contract (`mai_contactable_channels` plus `mai_channel_reason`, both reserved as nullable since 0.9, §5.1), never in `ScoreRow.suppressed_reason`;
    - a row with no contactable planned channel is left out of the activation file, but is not suppressed in `scores.csv`;
    - the 3-value `reason` Literal and DEC-A2 precedence are unchanged;
    - `engine/stages/export.py` `_REASON_CODES` is unchanged. `_suppression_counts` adds the per-channel counts in `channel_counts` (a DEC-1306 edit; `_suppression_counts` is on DEC-1306's list, §3.2).
  - **Ledger.** `consent_record.channel` is a nullable column (null = all channels; alembic `0009_consent_channel.py`), with CSV and connection import support. `ConsentLedger.classify(client_id, purpose, principal_ids, at, channel=None)`.
  - `consent_snapshot_id` is computed per purpose and channel set.
  - **Contract.** It fills `mai_action_id`, `mai_channel`, `mai_contactable_channels` and `mai_channel_reason` (set when a planned channel was dropped). A row is treated only on a channel it is contactable on.
  - **Copy.** For activation-enabled use cases, `copy_messages.csv` defaults to `approved_only=true` and is filtered to each row's `mai_contactable_channels` (pre-approved edit in `api/routes/generative.py`).
  - **Region rule.** The rule is data-driven. A stub `configs/regions/in.yaml` declares `sms_requires: [dlt_template_id, message_category]`. Catalogue validation applies the `sms_requires` list of the configured region, without naming any region in code. With the India region, an SMS action without `dlt_template_id`, or without a P/S/T category, is refused at config load, whatever the industry. M117 extends the stub into the full region profile.
- **Where in the code.**
  - `engine/activation/catalogue.py` (new), `configs/activation/catalogue.yaml` (new), `configs/regions/in.yaml` (new stub)
  - `engine/config.py` (§3.5: `Band.action_id`, `SuppressionConfig.channels`), `engine/stages/actions.py` `suppression_rules` and `engine/stages/export.py` `_suppression_counts` (DEC-1306), `engine/contracts.py` (§3.5: `SuppressionCount.channel_counts`)
  - `engine/privacy/{consent,contracts,tables}.py`, `alembic/versions/0009_consent_channel.py`, `engine/platform_db.py`, `api/routes/generative.py` (pre-approved), `configs/privacy.yaml`, `configs/pilot/help.yaml`
- **Contracts, APIs, config, error codes.**
  - §5.4.
  - Error codes: `CATALOGUE_INVALID`, `CATALOGUE_ACTION_UNKNOWN`, `ACTION_DLT_TEMPLATE_MISSING`.
  - DEC (one entry): (a) action catalogue; (b) channel dimension in the ledger; (c) `channel_counts` beside the unchanged Literal, and per-row channel reasons in the contract; (d) per-channel consent snapshot; (e) DLT as a data-driven region rule.
- **Acceptance.**
  - **Mixed consent.** A customer opted out of SMS but in for email, with SMS and email both planned, is eligible, with `mai_contactable_channels = ["email"]` and `mai_channel_reason = "consent_denied_sms"`, and is never treated on an SMS action.
  - **No contactable channel.** A customer whose only planned channel is SMS, and who opted out of SMS, is left out of the activation file, is not suppressed in `scores.csv`, and is counted in `channel_counts["consent_denied_sms"]`.
  - **Old records.** Existing consent records (channel null) apply to all channels, and `test_platform_migration.py` is green.
  - **Config errors.** An unknown `action_id` fails config load. Editing the catalogue changes `catalogue_sha256` in the next manifest.
  - **Compatibility.** Contract files written before this change still validate. Every existing suppression test and the `SuppressionCount` schema test pass unchanged.
  - **Copy filter.** The copy download excludes a row's non-contactable channels.
- **Effort:** 3.5 engineer-weeks. **Depends on:** M91, M94, M100, M107.

#### M110 — Value weighting v1 (net INR per customer, under a budget or ROI floor)

- **Why.**
  - **Best evidence, lowest cost.** It is the best-evidenced and cheapest gap: ranking by expected net rupees gave much higher profit in offline evaluations.
  - **Costs vary widely.** Channel costs differ by about 6.6×.
  - **Two value models disagree today.** The list optimises heads, and the uplift policy and the ROI report use two value models that can disagree.
- **Scope.**
  - **Config.** `UpliftPolicyConfig` gains optional `value_column`, `horizon_months`, `margin_pct` and `min_roi` (agent-editable). Contact and offer costs come from the catalogue action the row maps to (the cheapest consented channel).
  - **Per-row net value** = uplift_i × value_i × margin − offer_cost × p_treated_i − contact_cost_i.
  - **Policy.** `ranking`, `choose_contacts`, `recommend_policy` and `profit_curve` take the vector together. The persuadable-only and sleeping-dog guards are kept.
  - **Holdout file.** `HOLDOUT_COLUMNS`, `_write_holdout` and `HoldoutUplift` gain the value column, so expected value is a value-weighted observed uplift.
  - **Propensity runs** get an optional `expected_gross_value` per row (p × value − cost) to rank the treat bands. It is labelled "not incremental" everywhere it appears.
  - **One value block** feeds both the policy and the `RoiInputs` defaults. India channel-cost defaults go in `configs/pilot/value.yaml`, labelled as editable defaults.
  - **Recommendation.** A `recommend.py` rule proposes a value-like column (order value, premium, balance, ARPU) as a "check" suggestion.
  - **Finance sign-off.** The finance-signed inputs are recorded through the existing `PUT /pilot/roi/{run_id}`, which already stamps `entered_by` from the principal.
- **Where in the code.**
  - `engine/uplift/{config,policy,flow,metrics}.py` (pre-approved, Phase 3b area)
  - `engine/pilot/roi.py`, `configs/pilot/value.yaml`, `engine/agent/recommend.py`, `tests/fixtures/agent_bench/expected.json`
  - `ui/modules/uplift/views.js` `profitCard`, `configs/engine.yaml` `defaults.uplift.policy`, `docs/UPLIFT.md` §14
- **Contracts, APIs, config, error codes.**
  - `UpliftPolicyConfig` additive fields.
  - `ProfitCurve` additive fields: `value_weighted`, `value_basis`. `PolicyRecommendation.expected_net_value` as a range.
  - `GET /runs/{id}/uplift/profit-curve` gains optional `min_roi` and value parameters.
  - DEC (one entry): (a) net-value formula; (b) one value block; (c) ROI floor; (d) the "not incremental" label for propensity value.
- **Acceptance.**
  - **Identity holds.** The identity in `tests/unit/uplift/test_profit_curve.py` (the profit-curve point at the budget equals the recommendation, field for field) holds with and without `value_column`. The file is extended, never loosened.
  - **Scalar path.** A constant value column reproduces the scalar path exactly.
  - **Ranking.** On a fixture where high-value persuadables have lower raw uplift, they rank higher.
  - **Guards.** `min_roi` is respected, and no sleeping dog is treated.
  - **Missing inputs.** Money fields are null with a reason when value inputs are missing.
- **Effort:** 3 engineer-weeks (DS 1). **Depends on:** M109.

#### M111 — Learn from cycle one

- **Why.**
  - **Cycle 1 created randomised history.** Uplift needs randomised history, and cycle 1's holdout and explore slice provide it.
  - **The experiment frame uses the wrong population.** `build_experiment_frame` uses "non-control" intent-to-treat.
  - **Retraining cannot read datasets.** Uplift retraining cannot read built datasets, because `UpliftRunRequest` takes only `upload_id`.
  - **The approver needs live calibration.** The Approver should see how the current model's predictions held up on a live randomised campaign.
- **Scope.**
  - `build_experiment_frame` uses `mai_intended_flag`, explore and the holdout, plus the send log's delivered flag when present.
  - Learning from a propensity cycle with `explore_fraction = 0` is refused with a plain overlap reason.
  - **Dataset-backed uplift runs.** `UpliftRunRequest` takes either `upload_id` or `dataset_id` (§3.5). The `POST /uplift/runs` handler reads the dataset manifest when `dataset_id` is set, instead of calling `load_upload`. The check seed and lineage come from the dataset id.
  - The learn result is a candidate waiting for the Approver.
  - **Predicted against measured.** A "predicted against measured uplift by decile" block is computed on each campaign's randomised rows and shown on the approval screen for the next challenger.
- **Where in the code.** `engine/uplift/measure.py` (`build_experiment_frame`, `learn_readiness`), `api/routes/measure.py` `create_learn`, `api/routes/uplift.py` (the `POST /uplift/runs` handler), `engine/uplift/flow.py` (seed and lineage from the dataset id), `api/schemas.py` (§3.5), `engine/measurement/measure.py`, `engine/approvals.py`, `ui/modules/production/approvals.js`, `docs/UPLIFT.md` §15.
- **Contracts, APIs, config, error codes.**
  - `UpliftRunRequest.upload_id: str | None` and `UpliftRunRequest.dataset_id: str | None`. Exactly one is required (422 otherwise).
  - `CampaignCalibration{deciles[{predicted, measured, interval}]}`.
  - Code `LEARN_NO_OVERLAP`.
  - DEC (one entry): (a) experiment frame from intent and delivery; (b) `dataset_id` on uplift runs; (c) live calibration shown to the Approver.
- **Acceptance.**
  - With explore of at least 5%, the frame passes `TREATMENT_NOT_RANDOM`. With 0 it is refused, with the reason.
  - A dataset-backed uplift run trains. A request with both ids, or neither, gets 422.
  - Every test in `test_measure_campaign.py` passes unchanged.
  - The calibration block on a planted fixture shows the planted miscalibration.
- **Effort:** 2.5 engineer-weeks (DS 0.5). **Depends on:** M98, M102.

#### M112 — Does uplift beat risk? Uncertainty-aware approval, the equal-budget comparison and a fallback model

- **Why.**
  - **Approval evidence is thin.** Approval rests on one hold-out split, with no fold variance, no comparison with propensity ranking and no calibration check. Uplift models swing across folds.
  - **The comparison must be computed.** The memo's re-examine trigger is "uplift fails to beat risk targeting at equal budget", so the comparison must be computed, not asserted, and not in-sample.
  - **A fallback model must stay releasable.** With one champion per use case (DEC-609/649), once an uplift model holds the slot, the propensity fallback would fail the release rule. Delivery therefore needs a designated fallback.
- **Scope.**
  - **New evaluation fields.** `UpliftEvaluation` gains these optional fields:
    - `baseline_comparison`: AUUC when ranking by `p_control`, by `p_treated` and by the propensity model on the same hold-out, with a paired bootstrap of the difference reusing `metrics._bootstrap`;
    - `calibration_by_decile` (weighted |observed − predicted|);
    - `fold_auuc`: LightGBM base learner only, **off by default**, with its refit cost estimated on the approval screen.
  - **Approval checks.** `engine/model_gates.py` computes these as advisory checks shown on the Approver screen (`ApprovalItem.checks`). `should_promote` stays frozen, and the champion tests are unchanged.
  - **Fallback model.** Sub-decision (c) of the DEC entry defines the designated fallback: the last approved propensity version of the use case stays releasable after an uplift model takes the champion slot. A test covers it.
  - **Release rule.** It gains `RELEASE_UPLIFT_NOT_BETTER_THAN_RISK`. An uplift-ranked list is delivered only if the paired difference's `ci_low > 0`. Otherwise the Analyst is offered the fallback ranking.
  - **Equal-budget comparison.** Uplift top-N against risk top-N at equal budget, by incremental conversions per rupee, with intervals. It uses `ope.evaluate_policy` with the recorded per-row propensities, and is evaluated either:
    - **cross-fitted** on cycle-1 randomised rows (holdout plus explore): train on folds, evaluate out of fold; or
    - on the next cycle's randomised rows.
  - **Live arms (J15).** `uplift.policy.arms {risk_share, uplift_share}` splits non-holdout eligible rows, by a salted hash, into equal-budget risk and uplift arms that share the holdout. Predicted sleeping dogs are excluded in both arms. Per-arm results go in `IncrementalityReport.arms` (`ArmSummary`, the DEC-668 path). The arms are pre-registered in `TestPlan.arms` (or, for a scheduled cycle, in `TestPlanTemplate.arms`). For pilot 1 they start with the first uplift-ranked cycle after M112 lands, cycle 4 (J15). A list with arms counts as uplift-ranked for the release rule: when the beats-risk check fails, the cycle runs the fallback ranking without arms.
  - **DS** owns the paired bootstrap, the cross-fitting, the overlap rule and the wording.
- **Where in the code.**
  - `engine/uplift/{metrics,flow,contracts,config,ope,actions,policy}.py` (pre-approved)
  - `engine/model_gates.py` (new), `engine/approvals.py`, `engine/registry.py` (read only; the champion rule is untouched), `ui/modules/production/approvals.js`
  - `engine/activation/{deliver,release}.py`, `engine/measurement/compare.py` (new), `api/routes/campaigns.py` and `api/access_policy.py` (the policy-comparison route), `docs/UPLIFT.md`
- **Contracts, APIs, config, error codes.**
  - `UpliftEvaluation` additive fields. `ApprovalItem.checks[{code, passed, message}]`. `GET /campaigns/{id}/policy-comparison`. `UpliftPolicyConfig.arms` (locked path).
  - Codes: `UPLIFT_NOT_BETTER_THAN_RISK`, `UPLIFT_UNSTABLE_ACROSS_FOLDS`, `RELEASE_UPLIFT_NOT_BETTER_THAN_RISK`.
  - DEC (one entry): (a) beats-risk check; (b) fold stability and calibration; (c) designated fallback model; (d) release-rule extension; (e) cross-fitted comparison and optional live arms.
- **Acceptance.**
  - **Gate behaviour:**
    - On a fixture where uplift ≡ −risk, delivery of the uplift ranking is refused and the fallback ranking is releasable.
    - On a heterogeneous-effect fixture, delivery passes.
    - On an equal-effect fixture, the interval covers 0 and the screen says "does not beat risk ranking".
  - **Cross-fitting.** The cross-fitted comparison never scores a row with a model trained on it (a fold-leak test).
  - **Arms on.** The arms are disjoint, sized to config ± 1, with equal budgets, and holdout membership is identical to M95's.
  - **Arms off.** Profit-curve and recommendation outputs are byte-identical.
  - **Existing tests.** Every existing champion and approvals test passes unchanged.
  - **Interval coverage.** Nightly (`statistical` marker, M101 band rule), the equal-budget comparison interval meets its coverage band on simulated heterogeneous-effect and null-effect populations.
- **Effort:** 6 engineer-weeks (DS 2.5). Without live arms: 5. **Depends on:** M100, M101, M103, M111.

#### M113 — The loop runs itself: fresh inputs, export, send-log and outcome pulls, measure, learn

- **Why.**
  - **The pod does the clicking.** Each monthly cycle needs a person to download, upload and click, and the pod ends up doing that work. Removing those steps makes cycle 4 need only the Approver. Checkpoint 7 (≤1.5 people by cycle 3) is measured from the pod-effort log started in M90.
  - **The firer only knows runs.** The `ScheduleFirer` is built around runs: a firing without a `run_id` is abandoned, and `_execute` treats unknown kinds as drift checks. Firings that produce no run therefore need their own completion path.
- **Scope.**
  - **New kinds and parameters.**
    - `ScheduleKind` gains `EXPORT`, `SEND_LOG_PULL`, `OUTCOMES_PULL`, `MEASURE` and `LEARN`. These are text columns, so no migration is needed, and all are on M90's allow-list.
    - `ScheduleParameters` gains `destination_id`, `connection_id`, `mapping_id`, `campaign_id`, `plan_template_id` and `selection`.
  - **Firing outcomes.** A non-run firing outcome path: these kinds set `succeeded` or `failed` synchronously and are never abandoned for lacking a `run_id`.
  - **Same code as the routes.** `ScheduleFirer._execute` branches call the same functions the routes call (DEC-766).
  - **Fresh inputs.** A SCORE firing of a connection-bound spec (M107) fetches fresh tables first.
  - **Test-plan template.** A `TestPlanTemplate` object, with its own sub-decision (c), holds the metric, outcome kind, covariate, holdout, window and analysis-date offsets. Before each EXPORT, a campaign's `TestPlan` is instantiated from it with that run's counts, and registered.
  - **Export chaining.** A score firing's `settle()` enqueues EXPORT. EXPORT is subject to the full release rule and runs only when all three hold:
    - `activation.auto_deliver = true`;
    - an Admin has marked the destination's write connection schedulable (`ConnectionRecord.schedulable`);
    - a plan template is attached to the schedule.
  - **Pulls.** `SEND_LOG_PULL` and `OUTCOMES_PULL` fetch through read-only connections, using M107's selection rules.
  - **Measure and learn.** `MEASURE` fires on the first slot after both the outcome window and `analysis_date`. `LEARN` calls `create_learn` when `learn_readiness.ready`, producing a candidate.
  - **Approval stays human.** `SYSTEM_SCHEDULER` (analyst role) never approves.
  - **Alert dedupe.** At most one open alert per kind per campaign.
  - **UI.** The schedule form gains the new kinds.
- **Where in the code.**
  - `engine/scheduling/{schedules,firing,service,alerts}.py`, `api/routes/schedules.py`, `api/schemas.py` (PLAN-J)
  - `engine/measurement/plan.py` (templates), `ui/modules/production/schedules.js`, `scripts/fire_schedule.py`
  - `engine/connections/store.py` (§3.5: `ConnectionRecord.schedulable`), `api/routes/connections.py` (the Admin toggle)

  The scheduling files are pre-approved edits in Phase 4b's area; the connection files are pre-approved edits in Plan H's area.
- **Contracts, APIs, config, error codes.**
  - The kinds and parameters above. `TestPlanTemplate` (§5.6). `ActivationConfig.auto_deliver = false`. `ConnectionRecord.schedulable: bool = False` (Admin-set, audited; write connections only). No `SEND` kind (design-rule test).
  - Code `TEST_PLAN_TEMPLATE_MISSING`.
  - DEC (one entry): (a) non-run firings; (b) export chaining; (c) test-plan templates; (d) pull selection; (e) measure timing; (f) alert dedupe; (g) schedulable destinations.
- **Acceptance.**
  - **End-to-end on fakes.** A `LocalScheduler` test runs the whole cycle:
    1. a bound source drops a new table;
    2. score;
    3. plan from the template;
    4. export;
    5. a fake CEP drops a send log into moto S3;
    6. pull;
    7. outcome pull;
    8. a controlled clock passes maturity, then measure;
    9. learn.

    The only human step is approval.
  - **No unsafe export.** An unapproved model never exports, `auto_deliver=false` never exports, and a schedule without a template never exports.
  - **Slot rules.** A non-run firing is never marked abandoned. Each slot fires at most once (DEC-763), and a missed EXPORT gets one catch-up (DEC-764).
  - **Design rule.** The design-rule test still forbids any send kind.
  - **AWS tests.** The moto-backed EventBridge tests (`tests/unit/production/test_eventbridge_scheduler.py`) and `tests/infra/test_phase4b_scheduler.py` cover the new kinds.
- **Effort:** 6 engineer-weeks. **Depends on:** M100, M102, M103, M107, M111.

#### M114 — Second hand-off route and the second CEP recipe

- **Why.**
  - **Client 2 runs a different tool.** The memo's connector order is file drop, then the pilot's CEP, then MoEngage and CleverTap, then Braze.
  - **Other paths need other routes.** Path-B clients (SMS or WhatsApp provider, Zoho) and SFMC need SFTP. Braze CDI and CleverTap's warehouse import read a table. A table plus the client's own reverse-ETL sync reaches tools that have no file import (WebEngage).
  - **First test of the cost claim.** With the profile architecture this should be configuration plus two adapters. It is the first real measurement of that claim; the memo estimated 3–6 engineer-weeks per CEP.
- **Scope.**
  - `WarehouseDestination` (Postgres and Redshift):
    - writes only into one granted schema (default `mai_activation`), using a secret held by the activation stack;
    - loads into a staging table, then swaps atomically into `mai_activation_rows` and `mai_activation_manifest`;
    - refuses DDL outside that schema, and refuses a role that can write elsewhere;
    - opens a write session separate from the forced-read-only connector sessions.
  - `SftpDestination`:
    - paramiko as the optional extra `sftp` (§3.5), loaded with the existing `load_sdk` / `addon_missing` → `409 CONNECTION_NEEDS_ADDON`;
    - upload to a temporary name, atomic rename, manifest last.
  - **The second CEP's recipe pack.** MoEngage S3 or CleverTap Custom List first, per the founders, or client 2's tool. It contains:
    - `configs/activation/profiles/<tool>.yaml` and `configs/activation/sendlogs/<tool>.yaml`;
    - golden files;
    - `docs/activation/recipes/<tool>.md`: where the file lands, the native import, the holdout-exclusion segment, the tool's GCG/SCG note, and the export to enable.
  - **Reverse-ETL recipe.** `docs/activation/recipes/reverse_etl.md` covers the warehouse table plus the client's own Hightouch, Fivetran or RudderStack sync. It needs no code.
  - Conformance for each new kind.
- **Where in the code.**
  - `engine/activation/destinations/{warehouse,sftp}.py` (new), `engine/connections/{sql,postgres}.py` (session code reuse), `pyproject.toml` (§3.5: `sftp` extra)
  - `configs/activation/`, `docs/activation/recipes/`, `tests/conformance/`, `tests/fixtures/sendlogs/`
- **Contracts, APIs, config, error codes.**
  - Destination kinds `postgres_table`, `redshift_table`, `sftp`.
  - Error code `DESTINATION_SCHEMA_REFUSED`; the existing `CONNECTION_NEEDS_ADDON` is reused.
  - DEC (one entry): (a) warehouse upsert semantics; (b) SFTP extra; (c) second-CEP recipe; (d) recipe definition of done.
- **Acceptance.**
  - The conformance suite is green on the warehouse (SQLite/Postgres fake) and SFTP (in-process fake) destinations.
  - Re-delivering the same manifest changes nothing. A delta with 3% changed rows writes only those rows plus deletes.
  - A git-diff check: adding the second CEP touched no Python outside tests.
  - Client 2's onboarding pull request contains only configuration: profile, mapping, destination.
- **Effort:** 5 engineer-weeks. **Depends on:** M97, M100, M102.

#### M115 — Pod tools: proven value to date, insight cards and the pod-effort log

- **Why.**
  - **Checkpoint 7 needs a readout.** Checkpoint 7 (no more than 1.5 pod people per client by cycle 3) is logged from cycle 1 in M90's pod-effort log, but nobody reads a raw CSV.
  - **Pods need help to scale.** Pods scale better with insight cards and a running total of proven value (strategy X8).
- **Scope.**
  - **Campaigns to date.** A Results summary: the sum of measured **lower bounds** across Campaign records, labelled as such, with links.
  - **Insight cards**, computed from existing artefacts only:
    - no control;
    - early look;
    - contamination alert;
    - drift;
    - challenger ready;
    - backfire segment;
    - beats-risk failed.
  - **Pod-effort summary.** The pod-effort log (`docs/pilot/templates/pod_effort_log.csv`) is created in M90 and filled from cycle 1. M115 adds the summary of person-days per client and cycle, and reads the log. The Phase 4 pilot gate reads the same log.
- **Where in the code.** `engine/measurement/summary.py` (new), `api/routes/campaigns.py` (declare `/campaigns/summary` before `/campaigns/{id}`), `api/access_policy.py` (Viewer policy), `ui/modules/simple/pages.js`, `docs/pilot/templates/`.
- **Contracts, APIs, config, error codes.** `GET /campaigns/summary` (Viewer). DEC (one entry): (a) lower-bound summing and labelling.
- **Acceptance.**
  - The summary equals the sum of the campaigns' lower bounds on a fixture.
  - Each card appears only when its artefact condition holds.
  - Nothing renders without a server value.
- **Effort:** 1.5 engineer-weeks. **Depends on:** M98, M103, M105, M111, M112.

**Phase 4 exit gate.**
- *Build gate:*
  - M109–M115 are merged, and `make test-all` is green.
  - The end-to-end scheduled cycle on fakes is green, including MEASURE under a controlled clock.
  - Two CEP recipe packs pass conformance.
- *Pilot gate* (decision date: week 24):
  - Client 1's cycle-4 list (about week 23–24) is value-weighted and channel-aware, and it is **delivered and its send log pulled by schedule**. It is ranked by uplift only if uplift beat risk; otherwise it uses the fallback ranking. The only human step is the Approver.
  - A written result on whether uplift beat risk ranking at equal budget, cross-fitted or live, reported whichever way it falls (a memo re-examine condition).
  - Client 2 has passed its dry run, and its first contract was ingested natively through a configuration-only change.
  - Pod effort per client is logged from cycle 1 (the M90 log), and it is at or below 1.5 people by cycle 3.

---

### Phase 5 — Buy through AWS, any industry (weeks 24–28; provisional)

**Goal.**
- Make the layer purchasable through AWS.
- Make it installable with zero send permission.
- Make it ready for clients in any B2C industry, including regulated ones, through configuration.
- Show cost before every run.
- Take the starter decision, and the other strategy checkpoints, on evidence.

**Client value.**
- New clients buy through their AWS commitment.
- They start from an industry pack and a region profile.
- Their DPO gets an evidence pack.
- They see the cost before each run, with a cap.

#### M116 — Production hardening, FTR and the Marketplace private offer

- **Why.**
  - **Marketplace comes first.** The memo puts an AWS Marketplace INR private offer with ACE co-sell first in distribution, at a 3% fee under $1M.
  - **The install must pass review.** A CIO must be able to install the layer with zero send permission and pass review.
  - **Findings remain.** F-2 and F-5 are still open, and a layer that writes to client systems needs a full egress and credential review.
- **Scope.**
  - Close F-2 (dev TLS) and F-5 (execution-role KMS), or record accepted residuals with reasons. F-1, F-3, F-4 and F-6 were closed in M92.
  - Write the threat model for write-scoped credentials and webhook egress.
  - Add an infra test: no IAM statement in any stack contains `ses:*`, `sms-voice:*`, `social-messaging:*` or `mobiletargeting:*`.
  - Parameterise CDK and CloudFormation for client installs, and run the backup and restore drill again.
  - Run a fresh install by someone outside the team, using the docs only.
  - Write the FTR self-assessment and the Marketplace listing text, checked by `test_docs_honesty` (the `docs/marketplace/` files are added to `DOCUMENTS` in `tests/unit/test_docs_honesty.py`). Pricing is per use case, never per MAU.
- **Where in the code.** `infra/{app,policies,operations,compute,database,storage,context}.py`, `tests/infra/`, `tests/unit/test_docs_honesty.py`, `docs/{SECURITY_REVIEW,AWS_DEPLOYMENT,BACKUP_RESTORE_DRILL}.md`, `docs/marketplace/` (new).
- **Contracts, APIs, config, error codes.** IAM changes only. DEC (one entry): (a) zero-send install; (b) accepted security residuals; (c) Marketplace packaging and pricing dimension.
- **Acceptance.**
  - cdk-nag is clean, and the infra tests prove no send actions exist.
  - F-1 to F-6 are closed or accepted with reasons.
  - The fresh install succeeds, with its time recorded.
  - The FTR checklist is fully evidenced.
- **Effort:** 3 engineer-weeks. **Depends on:** M92, M93.

#### M117 — Industry overlay and region profile

- **Why.** The product is industry-agnostic, but three things block that in practice:
  - **No industry defaults.** An industry cannot set defaults today (`resolve_config` has no industry layer).
  - **Guided setup knows no industry vocabulary.**
  - **India's rules must not ride on an industry pack.** DLT, one-way SMS, INR, Aadhaar and PAN, and the DPDP dates are country rules, not industry rules. They apply whatever the client's industry is.
- **Scope.**
  - **(a) Industry overlay.** An optional `IndustryConfig.defaults` overlay, with an `industry` layer in `resolve_config` between engine and use case. `Source` gains `industry` (additive). The overlay covers:
    - holdout, value block, outcome windows and purposes;
    - catalogue examples and the activation profile;
    - onboarding aliases, role name tokens, suggested features and label templates (churn, lapse, repurchase, renewal);
    - reason phrases and value-column aliases (order value, premium, balance, margin);
    - data-request wording.
  - **Packs:**
    - generic (default);
    - the pack pilot 1 needs, whatever its industry, with M96's lapse labels as its label templates;
    - the pack client 2 needs, with its use-case YAMLs;
    - telecom, migrated from the existing `configs/industries/telecom.yaml` (prepaid lapse labels), as one pack among the others.

    A pack adds a `defaults:` block to an existing industry file. Today `configs/industries/` holds `generic`, `telecom`, `banking`, `insurance`, `ecommerce` and `ad_tech`, as journey documents with no defaults. Further packs add a `defaults:` block to `banking.yaml`, `insurance.yaml` and `ecommerce.yaml` (BFSI, retail/e-commerce), or a new file for subscriptions, travel and utilities. Each costs about 1 engineer-week and is added when a paying client needs it.
  - **(b) Region profile**, chosen separately in Settings. M117 extends M109's stub `configs/regions/in.yaml` into the full profile. `IN` sets:
    - `sms_requires: [dlt_template_id, message_category]` (DLT validation) and `sms_sender: one_way`;
    - INR;
    - Aadhaar/PAN `do_not_send`;
    - the DPDP dates.

    Packs carry no currency or legal rules. Non-INR currencies are out of scope (`RoiInputs.currency` stays `INR`).
  - The pack is chosen in Settings, and Home keeps one generic journey (Plan H, H1).
- **Where in the code.** `engine/config.py` (§3.5), `configs/industries/*.yaml`, `configs/regions/in.yaml` (extended from M109's stub), `configs/pilot/{data_request,value}.yaml`, `configs/privacy.yaml`, `configs/activation/reasons.yaml`, `ui/modules/simple/pages.js`, `docs/ONBOARDING.md`, `tests/unit/test_no_use_case_branching.py`.
- **Contracts, APIs, config, error codes.** `IndustryConfig.defaults`, `RegionConfig`, `Source` Literal += `industry`. DEC (one entry): (a) industry layer; (b) region profile; (c) pack scope.
- **Acceptance.**
  - `tests/unit/test_no_use_case_branching.py` is extended to scan `engine/` for every industry id (`configs/industries/*.yaml` stems) and every region id (`configs/regions/*.yaml` stems), and it stays green. Two exceptions are explicit: `generic`, the default named once in `engine/config.py` `DEFAULT_INDUSTRY`, is allowed there; region ids are matched only as quoted string literals in any case (`"IN"`, `'in'`), since `in` is also an English word. The one telecom mention in an `engine/generative/retrieval.py` docstring is reworded.
  - A pack from a different industry than telecom resolves holdout, value and purpose defaults, with `Source = industry` in `run_config.json`. Switching pack changes defaults only.
  - Old `run_config.json` files load unchanged, and the Home UI test still shows one journey.
  - Each shipped pack, pilot 1's and client 2's included, runs the full layer journey on fakes.
  - With region `IN`, an SMS action without a DLT template is refused for every industry.
- **Effort:** 3.5 engineer-weeks (DS 0.5). **Depends on:** M94, M109, M110.

#### M118 — Cost before each run, with a cap

- **Why.** We run on the client's AWS bill, and bill shock during a pilot destroys trust. Today cost is recorded only after a run, and the price table is in USD.
- **Scope.**
  - **Estimate.** A pure pre-run estimate in `engine/aws/prices.py`: list price × `model_search.time_limit_minutes` × instances, plus a scoring estimate, plus the LLM call ceiling from the generative budget.
    - It is shown in **USD** and labelled `list_price_estimate`.
    - An INR figure appears only when an Admin configures an FX rate with its source, named in `basis`.
    - The estimate is null when prices are missing.
  - **Route.** `GET /use-cases/{use_case_id}/cost-estimate` (Viewer), shown beside the Run button through a registered run panel. There is no edit to `app.js`.
  - **Cap.** Optional `governance.max_run_cost_usd`.
    - Above it, a run is refused with `RUN_COST_ABOVE_CAP` unless an Analyst confirms.
    - A confirmed run is bounded by the time limit, and is stopped with `cancel_run` when its running cost passes the cap.
  - **Spend view.** A monthly spend view in Settings, built from `cost_capture`.
- **Where in the code.** `engine/aws/{prices,cost_capture}.py`, `configs/aws_prices.yaml`, `engine/config.py` (§3.5: `GovernanceConfig`), `api/routes/use_cases.py` (the cost-estimate route, beside `/use-cases/{use_case_id}/template.csv`), `api/access_policy.py` (Viewer policy), `api/schemas.py` (PLAN-J: `CostEstimateView`), `engine/runs.py` (`cancel_run` reused), `ui/modules/activation/`.
- **Contracts, APIs, config, error codes.**
  - `CostEstimateView{estimate_usd_range|null, estimate_inr_range|null, basis, reason}`.
  - Error code `RUN_COST_ABOVE_CAP`.
  - DEC (one entry): (a) estimate basis and FX source; (b) cost cap and stop.
- **Acceptance.**
  - On the M93 smoke journey, the estimate is at least the measured cost, and the ratio is recorded in `AWS_DEPLOYMENT.md`.
  - The estimate is null without prices. No INR figure appears without a configured rate.
  - A capped run is refused unless confirmed. A confirmed run that passes the cap is stopped.
  - Route-policy tests pass.
- **Effort:** 2 engineer-weeks. **Depends on:** M93.

#### M119 — DPDP evidence pack

- **Why.** Once lists leave the product, DPDP duties need evidence, and DPOs ask for packs ready for a DPIA (data protection impact assessment). The rest of DPDP applies from 13 May 2027. Receipts and deletion manifests already exist (M100), so this milestone assembles them.
- **Scope.**
  - **The pack.** A per-model and per-campaign evidence pack (ReportDocument kind `evidence`) covering:
    - purpose and sources;
    - PII fields seen or masked;
    - builder against approver;
    - drift history;
    - consent snapshot ids and their ages;
    - legal-opinion references;
    - suppression counts by reason and channel;
    - every delivery receipt (the record of processors);
    - every deletion manifest and suppression delta sent after an erasure or withdrawal.
  - **Retention.** Today `Settings.audit_retention_days` defaults to 2555 days but accepts 1, and dev ships with 1. It gains a floor of 365 days for any non-dev environment (the prod default stays 2555). `infra/context.py` refuses a prod value below 365. Dev keeps 1, with a synth-time note.
- **Where in the code.** `engine/privacy/evidence.py` (new; pre-approved), `engine/audit/export.py`, `api/routes/privacy.py`, `engine/pilot/document.py`, `engine/settings.py` (§3.5: the `audit_retention_days` floor on `Settings`), `infra/context.py`, `tests/infra/test_phase4b_context.py`.
- **Contracts, APIs, config, error codes.** `GET /privacy/evidence/{run_id|campaign_id}` (Viewer). DEC (one entry): (a) evidence-pack contents; (b) audit retention floor.
- **Acceptance.**
  - The pack lists every receipt, deletion manifest and suppression delta for the campaign.
  - A prod context with `audit_retention_days` below 365 fails synth. Dev still synthesises with 1.
  - The pilot's DPO reviews the pack (business).
- **Effort:** 2 engineer-weeks. **Depends on:** M100, M107, M109.

#### M120 — Starter gate and strategy checkpoints (decision only, no sender code)

- **Why.** The memo says to build nothing for the starter until at least 2 of the first 5 qualified prospects meet all three conditions. It also lists conditions for:
  - moving to the pure layer;
  - moving toward replacement;
  - re-examining the whole position.

  The strategy adds seven checkpoints. Each needs an owner and a tally.
- **Scope.**
  - Tally the prospect ledger against conditions (a), (b) and (c).
  - Review the memo's other triggers:
    - 3 CEP pilots ingesting within 3 weeks;
    - CFO discounting;
    - CEP partnership refusal;
    - Amazon Connect in Mumbai;
    - 3 of 5 prospects lacking a send-log export.
  - Review all seven strategy checkpoints, including "prove has no buyer" (E7 and the CFO answers), "host platform first", and "proof of value converts within 3 months".
  - Record the starter-gate decision (build or do not build) as sub-decision (a) of M120's DEC entry.
  - **If the gate passes,** write Plan K. Our outline estimate is 9–15 engineer-weeks. Plan K covers:
    - the five-condition rule;
    - SendPlan with a second approver;
    - a separate off-by-default send stack;
    - `SesSender`, `EumSmsSender`, `EumWhatsAppSender` and `BspSender`, each with a fake;
    - the costed India SMS opt-out link;
    - the freeze monitors.
  - Re-plan cross-use-case arbitration if a client has signed a second, overlapping use case.
- **Where in the code.** `docs/DECISIONS.md`; `docs/pilot/PROSPECT_LEDGER.md`; `docs/plans/` (Plan K, only if the gate passes). The M90 design-rule test stays in force.
- **Contracts, APIs, config, error codes.** DEC (one entry): (a) the starter gate: build or do not build; (b)–(h) one lettered sub-decision per strategy checkpoint outcome.
- **Acceptance.** The DEC cites the ledger counts for each condition, each memo trigger and each strategy checkpoint.
- **Effort:** 0.5 engineer-weeks. **Depends on:** M90, plus the prospect-ledger data.

**Phase 5 exit gate.**
- *Build gate:*
  - M116–M120 are merged, and `make test-all` is green.
  - README rows M90–M120 are mapped in `check_readme.py`, and `docs/plans/README.md` lists Plan J.
  - `tests/infra` proves the zero-send install.
- *Pilot gate* (decision date: week 28):
  - The Marketplace private offer is submitted, and the FTR self-assessment is complete.
  - Client 2, from a different industry than pilot 1 and on a different CEP, is live from a pack with no engine change, with its own reconciliation.
  - Client 1's cycle-4 campaign is measured by schedule.
  - The evidence pack has been generated for one model and one campaign.
  - M120's DEC entry is recorded, with the starter gate and every checkpoint outcome.

---

## 5. Shared contracts introduced

All are frozen pydantic models with `extra="forbid"`, tuples, tz-aware datetimes and `Field(description=…)` on every field. They are extended additively only. Sketches written as `BaseModel` subclass `engine.contracts.StrictBase` (or `Artefact` for files). Every `X | None` field defaults to None unless marked required.

### 5.1 Activation Contract (M94; 0.9 → frozen 1.0 at the Phase 1 pilot gate, once the E1 dry run and the golden files agree)

```python
class FileDigest(BaseModel):
    path: str; sha256: str; bytes: int

class ActivationManifest(Artefact):            # activation_manifest.json, schema_version=1
    contract_version: str                       # "0.9" → "1.0" at the Phase 1 pilot gate; minors additive only (DEC-1302)
    kind: Literal["full", "delta", "holdout_roster", "deletion", "suppression"]
    run_id: str | None                          # None for roster/deletion/suppression
    campaign_id: str | None
    use_case_id: str
    model_version_id: str | None
    consent_snapshot_id: str | None             # ledger hash (M91) or "file:" hash (M94); required for real delivery
    consent_snapshot_as_of: datetime | None     # checked against activation.max_consent_age_days
    holdout_scope: Literal["run", "use_case", "universal", "external"]
    holdout_salt_id: str | None                 # fingerprint, never the salt
    holdout_epoch: int | None
    holdout_fraction: float | None
    explore_fraction: float = 0.0
    catalogue_sha256: str | None                # filled from M109
    profile_id: str; profile_version: int
    created_at: datetime; valid_until: datetime
    row_count: int; treat_rows: int; intended_rows: int; holdout_rows: int
    suppressed_rows: int; explore_rows: int; delete_rows: int
    reasons_unmapped_rows: int = 0
    delta_base_sha256: str | None
    files: tuple[FileDigest, ...]
    synthetic: bool = False

class ActivationRow(BaseModel):                 # canonical names; profiles may rename/encode only
    customer_key: str                           # the client's own key, as source text, in clear (J7)
    # optional channel id columns only if activation.channel_id_columns is set
    mai_use_case: str
    mai_model_version: str | None
    mai_band: str | None
    mai_segment: Literal["persuadable", "sure_thing", "lost_cause", "sleeping_dog"] | None
    mai_reason: str | None                      # plain phrase from reasons.yaml, ≤ 120 chars; never a feature name or value
    mai_action_id: str | None                   # catalogue id (M109); null before
    mai_action_label: str | None
    mai_channel: str | None                     # M109
    mai_contactable_channels: tuple[str, ...] | None   # M109
    mai_channel_reason: str | None              # reserved now, filled from M109: why a planned channel was dropped, e.g. "consent_denied_sms"
    mai_treat_flag: Literal[0, 1]
    mai_intended_flag: Literal[0, 1]            # selected, or held out but would have been selected (DEC-624); explore excluded
    mai_holdout_flag: Literal[0, 1]
    mai_explore_flag: Literal[0, 1]
    mai_consent_status: Literal["granted", "denied", "unknown"]
    mai_suppress_reason: str | None
    mai_valid_until: datetime
    updated_at: datetime
    row_status: Literal["upsert", "delete"] = "upsert"
    payload: str                                # JSON of allow-listed business fields (Braze CDI PAYLOAD)
```

**Beside the contract:** `holdout_assignment.parquet`, with columns key, `holdout_member`, `explore`, `explore_propensity` (row-level, registered).

**Invariants (tested):**
- `treat_flag=1` ⇒ `holdout_flag=0`, `suppress_reason` is null, and `consent_status="granted"`;
- `intended_flag=1` ⇒ `explore_flag=0`;
- no row is a sleeping dog with `treat_flag=1`;
- no model feature column, feature name or source cell value appears in any column or the payload;
- every scored entity appears exactly once per full contract.

**Configuration (`configs/engine.yaml` → `defaults.activation`):**

```yaml
activation:
  enabled: false
  valid_for_days: 7            # 1..30
  profile: generic_csv
  treat_bands: null            # null = top band (propensity runs)
  channel_id_columns: []
  payload_fields: [band, segment, reason, action_id]
  key_must_be_text: true
  max_consent_age_days: 2      # release rule
  require_test_plan: true
  auto_deliver: false          # M113
  reconciliation: {max_holdout_contacted: 0.05, min_treat_contacted: 0.70}   # M103
```

**Profile YAML (`configs/activation/profiles/<id>.yaml`):** `profile_id`, `version`, `format: csv|parquet|jsonl`, `headers{canonical: vendor}`, `booleans`, `timestamp_format`, `payload_column`, `max_attributes`, `max_row_bytes`, `default_mode`, `filter_instructions`.

**Reasons YAML (`configs/activation/reasons.yaml`):** `{feature_or_family_pattern: phrase}`. Industry packs add entries.

### 5.2 Connector interface: write side (M97)

```python
class Destination(Protocol):                    # stateless; config and secrets on every call
    def info(self) -> KindInfo: ...             # group="destination"
    def test_write(self, config: Mapping, secrets: Mapping) -> TestReport: ...   # probe write; never deletes
    def deliver(self, bundle: ContractBundle, config: Mapping, secrets: Mapping,
                mode: Literal["full", "delta"]) -> DeliveryReceipt: ...          # data first, manifest last

class DeliveryReceipt(BaseModel):
    receipt_id: str; destination_id: str; campaign_id: str | None
    kind: Literal["full", "delta", "holdout_roster", "deletion", "suppression"]   # matches ActivationManifest.kind
    mode: Literal["full", "delta"]
    profile_id: str; manifest_sha256: str
    objects: tuple[FileDigest, ...]; rows_written: int
    status: Literal["delivered", "unchanged", "failed"]
    started_at: datetime; finished_at: datetime; delivered_by_hash: str
```

- **Kinds:** `fake`, `local_dir`, `s3` (M97), `webhook` (M97), `postgres_table`, `redshift_table`, `sftp` (M114).
- **Connection records:** `ConnectionRecord.purpose: read | write` (default `read`) and `secret_ref` (a Secrets Manager reference; required for write connections outside `local`/`dev`). `ConnectionRecord.schedulable: bool = False` (M113; Admin-set, audited; write connections only). S3 destinations on AWS assume `ActivationWriteRole`.
- **Webhook body:** `{schema_version, event: "list_ready", manifest_sha256, run_id, campaign_id, use_case_id, kind, row_count, location, valid_until}`, with headers `X-MAI-Signature: sha256=<hmac>` and `X-MAI-Timestamp`.

### 5.3 Holdout service (M95)

```python
class HoldoutSpec(BaseModel):
    scope: Literal["run", "use_case", "universal", "external"] = "run"
    fraction: float | None = None               # 0..0.5; persistent scopes require it. Under scope run it stays None and actions.control_group_fraction applies
    salt_id: str | None = None                  # fingerprint of MARKETING_AI_HOLDOUT_SALT; set from settings, never from config
    epoch: int = 1
    external_column: str | None = None          # scope=external only
    external_agreement_ref: str | None = None   # scope=external only; set only through PUT /holdout (Admin, audited)

def holdout_mask(keys: Sequence[str], spec: HoldoutSpec, salt: SecretStr, scope_key: str) -> np.ndarray: ...
    # member iff int(sha256(f"{salt}:{scope_key}:{key}")[:16], 16) < fraction * 2**64
def explore_mask(keys, eligible, holdout_member, selected, sleeping_dog, fraction, salt) -> tuple[np.ndarray, np.ndarray]: ...  # eligible ∧ ¬holdout_member ∧ ¬selected ∧ ¬sleeping_dog; salt label "explore"
    # (mask, per-row propensity); separate salt label "explore"
def roster(master_keys, spec, salt, scope_key) -> pd.DataFrame: ...
    # over the customer master (entity source), refreshed each cycle
```

- **Config:** `actions.holdout {scope, fraction}` and `actions.explore_fraction` (0..0.10). The existing `actions.control_group_fraction` (0..0.50, default 0.10) keeps its meaning under `scope: run`, so today's behaviour is unchanged. Under a persistent scope `actions.holdout.fraction` is required and wins. `measure_offered`, `engine/scheduling/outcomes.py` and the help.yaml settings entry read the effective fraction through one helper, `effective_holdout_fraction(actions)`, so no reader sees two numbers.
- **Routes:** `GET /holdout` and `PUT /holdout` (Admin; a decrease or rotation increments `epoch`). `PUT /holdout` is the only way to record `external_agreement_ref`. The release rule refuses `scope=external` without it (`RELEASE_HOLDOUT_NOT_PERSISTENT`).

### 5.4 Action catalogue (M109)

```yaml
# configs/activation/catalogue.yaml
region: IN                              # example region; its rules come from configs/regions/in.yaml (stub in M109, full profile in M117)
actions:
  - action_id: winback_sms_10pct
    label: "10% off next order (SMS)"
    channel: sms                          # email|sms|whatsapp|push|call|in_app|none
    purpose: marketing_communication      # must exist in configs/privacy.yaml
    message_category: promotional         # promotional|service|transactional (P/S/T)
    dlt_template_id: "1107…"              # required because in.yaml declares sms_requires: [dlt_template_id, message_category]; no region id in code
    template_ref: null                    # CEP template name, if any
    offer_id: OFFER10
    offer_cost_inr: 45
    contact_cost_inr: 0.13
    priority: 50                          # reserved for later arbitration
    eligibility: "band in ['High'] and tenure_months >= 3"   # closed grammar over contract-safe columns
```

Use cases reference it through `Band.action_id` and `uplift.policy.treat_action_id`. The ledger is queried with `ConsentLedger.classify(client_id, purpose, principal_ids, at, channel=None)`; `consent_record.channel` null means all channels. `SuppressionCount.channel_counts: dict[str, int] | None` (for example `{"consent_denied_sms": n}`) sits beside the unchanged `reason` Literal and is filled by `engine/stages/export.py` `_suppression_counts` (DEC-1306). Per-row channel reasons live only in the activation contract, in `mai_contactable_channels` and `mai_channel_reason` (§5.1), never in `ScoreRow.suppressed_reason`. A row with no contactable planned channel is left out of the activation file, is not suppressed in `scores.csv`, and is counted in `channel_counts`.

### 5.5 Outcomes ingestion: the send-log side (M97, M102)

```python
class SendLogEvent(BaseModel):
    customer_key: str; campaign_ref: str; channel: str
    event: Literal["sent", "delivered", "failed", "bounced", "opened",
                   "clicked", "converted", "unsubscribed", "complained", "other"]
    ts: datetime                                 # tz-aware, UTC
    source: str                                  # mapping_id

class SendLogMapping(BaseModel):                 # configs/activation/sendlogs/<id>.yaml
    mapping_id: str; vendor: str
    columns: dict[str, str]                      # canonical -> vendor column
    event_values: dict[str, str]                 # vendor value -> canonical event
    ts_format: str; timezone: str
    granularity: Literal["per_customer", "aggregate"] = "per_customer"
    fixture_basis: Literal["vendor_docs", "recorded_export"]

class SendLogSource(Protocol):
    def read(self, storage: Storage, upload_id: str, mapping: SendLogMapping) -> pd.DataFrame: ...   # canonical frame

class SendLogImportReport(BaseModel):
    rows: int; matched: int; unmatched: int
    by_event: dict[str, int]; by_channel: dict[str, int]
    first_sent_at: datetime | None; rejected: int; granularity: str
```

**Route:** `POST /campaigns/{id}/send-logs {upload_id | connection_id + selection, mapping_id, campaign_refs?}`.

### 5.6 Campaign record, test plan and measurement (M98, M99, M103, M104, M105, M108, M112, M113)

```python
class Campaign(BaseModel):
    campaign_id: str
    kind: Literal["activated", "external", "programme"]
    source: Literal["activation", "audit", "cep_export", "universal_holdout"]
    use_case_id: str | None; run_ids: tuple[str, ...]; manifest_shas: tuple[str, ...]
    holdout_spec: HoldoutSpec; holdout_epoch: int
    treatment_start: datetime | None; outcome_window_days: int | None
    period_start: date | None; period_end: date | None        # programme only
    test_plan_hash: str | None
    causal: bool                                               # true only for engine_random or verified_random
    causal_basis: Literal["engine_random", "declared_random", "verified_random", "not_random"]
    status: Literal["planned", "live", "matured", "measured"]

class TestPlan(Artefact):                        # campaigns/<id>/test_plan.json
    metric: str; outcome_column: str; positive_label: str | None
    outcome_kind: Literal["binary", "continuous"] = "binary"
    covariate_column: str | None = None          # pre-registered CUPED covariate
    arms: tuple[str, ...]                        # ("treat","holdout") [+ "explore", "risk", "uplift"]
    holdout_fraction: float; n_treat: int; n_holdout: int; n_explore: int
    base_rate: float; base_rate_source: str
    alpha: float = 0.05; power: float = 0.8; mde_pp: float
    cost_of_holdout: Money | None; cost_of_explore: Money | None
    outcome_window_days: int; analysis_date: date
    secondary_analysis_dates: tuple[date, ...] = ()
    expectation: str                             # the sponsor's written 1–3 point expectation
    version: int = 1; amends: str | None = None; amendment_reason: str | None = None
    template_id: str | None = None               # M113
    plan_hash: str

class TestPlanTemplate(BaseModel):               # M113; instantiated per scheduled campaign
    template_id: str; metric: str; outcome_column: str
    outcome_kind: Literal["binary", "continuous"] = "binary"
    positive_label: str | None = None
    arms: tuple[str, ...] = ("treat", "holdout")  # live arms (M112) are pre-registered here
    covariate_column: str | None; holdout_fraction: float
    outcome_window_days: int; analysis_offset_days: int; secondary_offsets_days: tuple[int, ...]
    expectation: str

class CampaignAuditRequest(BaseModel):
    assignment_upload_id: str; outcomes_upload_id: str; send_log_upload_id: str | None
    key: str; arm_column: str; outcome_column: str; positive_label: str | None
    intended_column: str | None; band_column: str | None
    sent_at_column: str | None; outcome_window_days: int | None
    declared_random: bool
    control_source: Literal["arm_column", "universal_holdout"] = "arm_column"
    audience_upload_id: str | None               # restricts universal-holdout controls
    campaign_ref_column: str | None = None       # optional campaign_ref column in the send log
    campaign_refs: tuple[str, ...] = ()          # picks "Campaign X" out of the send log

class ProgrammeRequest(BaseModel):               # M104
    period_start: date; period_end: date
    outcomes_upload_id: str; outcome_column: str
    outcome_kind: Literal["binary", "continuous"] = "binary"
    positive_label: str | None = None
    covariate_column: str | None = None

def measure_campaign(assignment: pd.DataFrame, outcomes: pd.DataFrame, *,
                     run_id: str, primary_key: PrimaryKey, outcome_column: str,
                     positive_label: str | None = None, intended_column: str | None = None,
                     bands: Sequence[str] | None = None, treatment_time: datetime,
                     treatment_date_column: str | None = None, outcome_window_days: int | None = None,
                     as_of: datetime, campaign_id: str | None = None,
                     send_log: pd.DataFrame | None = None,
                     plan: TestPlan | None = None) -> IncrementalityReport: ...
    # pure wrapper over measure_incrementality (same keyword names) and campaign_verdict (M98).
    # as_of always comes from the caller; utc_now() lives in the route, never here.
    # Campaign callers pass treatment_time = Campaign.treatment_start (period_start for programme campaigns)
    # and, for external and programme campaigns, run_id = the campaign_id.
    # M103 derives the reached flag and per-row treatment dates from send_log. M108 adds the optional
    # keywords outcome_kind and covariate_column, passed through to measure_incrementality from the
    # plan or from MeasureRequest.
```

For `external` and `programme` campaigns, `IncrementalityReport.run_id` carries the campaign_id. The field stays a required `str`, so the change remains additive.

**`IncrementalityReport` additive fields** (`schema_version` unchanged; §3.5 register row):
- M99: `test_plan_hash: str | None = None`, `early_look: bool = False`;
- M103: `contact_rate_treated`, `contact_rate_control`, `holdout_contacted_rows`, `holdout_contacted_rate`, `compliance`, `cace {estimate, ci_low, ci_high} | null`, `cace_reason`, `reconciliation_verdict`, `reconciliation_basis`;
- M108: `outcome_kind`, `mean_difference`, `mean_difference_ci`, `adjusted_lift`, `adjusted_interval`, `variance_reduction`;
- M112: `arms: tuple[ArmSummary, ...] | None` (a new `ArmSummary` model in `engine/uplift/contracts.py`, the shape DEC-668 reserved).

**Reports:**
- `ReconciliationReport{campaign_id, treat_rows, treat_contacted, contact_rate, holdout_rows, holdout_contacted, contamination_rate, by_channel, by_campaign, thresholds, verdict: pass|warn|fail}`.
- `ProofView{plan, execution, itt, cace, gross, naive_value, cep_reported_value, measured_value, sure_thing_cost, backfire_segments, holdout_cost, explore_cost, persistence, net_value, limits}`. Every money field is a `Money` range, or null with a reason. Every number carries `(artefact, field)` provenance.
- `SegmentEffects` (M105; `campaigns/<id>/segment_effects.json`, written by `measure_campaign`, counts only, registered in privacy and layout like every campaign artefact): per band and per segment, `{treated_rows, control_rows, treated_conversions, control_conversions, difference, ci_low, ci_high}` with the Newcombe interval. The Proof Pack's sure-thing cost and backfire sections read it.
- `CustomerView` (M106), `CampaignCalibration` (M111), `CampaignsSummary` (M115).

### 5.7 Error-code register (each code gets an entry in `configs/pilot/help.yaml`; M90 adds a frozen `PLAN_J_CODES` set in `engine/activation/codes.py`, imported by `engine.pilot.help.known_codes()` (§3.5), so `test_the_catalogue_explains_no_code_the_engine_cannot_raise` keeps passing)

| Area | Codes |
|---|---|
| Release rule | `RELEASE_MODEL_NOT_APPROVED`, `RELEASE_HOLDOUT_NOT_PERSISTENT`, `RELEASE_CONSENT_SNAPSHOT_MISSING`, `RELEASE_CONSENT_SNAPSHOT_STALE`, `RELEASE_LEGAL_OPINION_MISSING`, `TEST_PLAN_REQUIRED`, `DELIVERY_REFUSED_AUTH_OFF`, `ACTIVATION_CONTRACT_EXPIRED`, `DESTINATION_READ_ONLY_CONNECTION`, `RELEASE_UPLIFT_NOT_BETTER_THAN_RISK` |
| Contract and profiles | `ACTIVATION_DISABLED`, `ACTIVATION_RUN_NOT_SCORED`, `ACTIVATION_CONSENT_SOURCE_MISSING`, `ACTIVATION_KEY_NOT_TEXT`, `ACTIVATION_PROFILE_UNKNOWN`, `ACTIVATION_PROFILE_ATTRIBUTE_CAP`, `ACTIVATION_ROW_TOO_LARGE`, `ACTIVATION_PROFILE_COLUMN_MISSING` |
| Destinations | `CONNECTION_PURPOSE_MISMATCH`, `DESTINATION_WRITE_DENIED`, `DESTINATION_NOT_WRITE_SCOPED`, `DESTINATION_UNREACHABLE`, `DESTINATION_INLINE_SECRET_REFUSED`, `ACTIVATION_WRITES_DISABLED`, `DESTINATION_SCHEMA_REFUSED`, `WEBHOOK_TARGET_REFUSED` (existing and reused: `CONNECTION_NEEDS_ADDON`, which has no help.yaml entry today; M97 adds one and adds the code to `PLAN_J_CODES`) |
| Connections and inputs | `CONNECTION_SELECTION_EMPTY` |
| Holdout | `HOLDOUT_SALT_MISSING`, `HOLDOUT_SALT_CHANGED`, `HOLDOUT_EPOCH_MISMATCH`, `HOLDOUT_EXTERNAL_COLUMN_MISSING` |
| Campaigns and plans | `CAMPAIGN_NOT_FOUND`, `CAMPAIGN_NOT_MATURED`, `CAMPAIGN_EPOCH_MISMATCH`, `CAMPAIGN_ARM_NOT_BINARY`, `CAMPAIGN_ARMS_TOO_SMALL`, `TEST_PLAN_EXISTS`, `TEST_PLAN_TEMPLATE_MISSING`, `TEST_PLAN_CHANGED`, `PLAN_UNDERPOWERED` (warning), `LEARN_NO_OVERLAP` |
| Send logs, proof, lookup | `SEND_LOG_MAPPING_UNKNOWN`, `SEND_LOG_COLUMN_MISSING`, `PROOF_SYNTHETIC_DATA`, `PROOF_NOT_MATURE`, `CUSTOMER_NOT_FOUND` |
| Consent and catalogue | `CONSENT_PURPOSE_MISSING`, `SMS_REPLY_STOP_ONE_WAY`, `CATALOGUE_INVALID`, `CATALOGUE_ACTION_UNKNOWN`, `ACTION_DLT_TEMPLATE_MISSING` |
| Checks and cost | `LABEL_RATE_UNSTABLE`, `TREATMENT_HISTORY_NOT_RANDOM`, `UPLIFT_NOT_BETTER_THAN_RISK`, `UPLIFT_UNSTABLE_ACROSS_FOLDS`, `RUN_COST_ABOVE_CAP` |

---

## 6. Sequencing and timeline

### 6.1 Dependencies

| Milestone | Depends on |
|---|---|
| M90 Set-up and decisions | — |
| M91 Consent fixes and snapshot | M90 |
| M92 Deployment code | M90 |
| M93 First deployments | M91, M92 |
| M94 Contract 0.9 | M90, M91 |
| M95 Holdout service | M90 (DEC-1306 signed) |
| M96 Readiness and neutral pilot kit | M90 |
| M97 Connector SDK | M90, M92, M94 |
| M98 Campaign record | M90, M92, M94, M95 |
| M99 Test plan | M96, M98 |
| M100 Hand-off in the product | M91, M92, M94, M95, M97, M98, M99, M101 |
| M101 Validity harness | M96 |
| M102 Send-log importer | M97, M98 |
| M103 Reconciliation | M101, M102 |
| M104 Audit and programme readouts | M95, M98, M102 |
| M105 Value Proof Pack | M99, M100, M101, M103, M108 |
| M106 Customer view | M94, M98, M100 |
| M107 Connection-backed inputs | M90, M98 |
| M108 Revenue and CUPED | M98, M99, M101 |
| M109 Catalogue and channel consent | M91, M94, M100, M107 |
| M110 Value weighting | M109 |
| M111 Learn from cycle one | M98, M102 |
| M112 Beats risk and fallback model | M100, M101, M103, M111 |
| M113 Unattended loop | M100, M102, M103, M107, M111 |
| M114 Second route and second CEP | M97, M100, M102 |
| M115 Pod tools | M98, M103, M105, M111, M112 |
| M116 Hardening, FTR, Marketplace | M92, M93 |
| M117 Industry overlay and region | M94, M109, M110 |
| M118 Cost before run | M93 |
| M119 DPDP evidence | M100, M107, M109 |
| M120 Starter gate and checkpoints | M90, plus the prospect-ledger data |

**Critical paths:**
- **To first list:** M90 → M91 → M94, with M92 and M95 in parallel → M98 → M99 → M100 → delivery (about weeks 10–11). M97 and M101 (after M96) run in parallel into M100; M101 must merge first because the release rule's `fake`/`local_dir` exemption reads `RunRecord.synthetic`.
- **To first proof:** M100 → the client's outcome window → M102 → M103 → M105, with M108 feeding M105.
- **To the unattended loop:** M107 and M111 → M113 (which also needs M100, M102 and M103).
- **To the beats-risk answer:** M111 → M112 (which also needs M100, M101 and M103) → M115's 'beats-risk failed' card and the cycle-4 list.

After week 10, the outcome window, not code, is the binding constraint.

### 6.2 Effort summary

Engineering capacity is 3.25 engineer-weeks per week. Data-science capacity is 0.8 DS-weeks per week.

| Phase | Weeks | Milestones | Total (DS inside) | Engineering | Engineering capacity | Engineering slack | DS demand / capacity |
|---|---|---|---|---|---|---|---|
| 1 Safe to hand off | 0–6 | M90–M96 | 21.0 (DS 2.0) | 19.0 | 19.5 | 0.5 | 2.0 / 4.8 (+ E2/E4 by hand) |
| 2 First list live | 6–10 | M97–M101 | 16.0 (DS 3.0) | 13.0 | 13.0 | **0** | 3.0 / 3.2 |
| 3 Reconcile and prove | 10–16 | M102–M108 | 21.0 (DS 4.5) | 16.5 + 0.5 client-2 support | 19.5 | 2.5 | 4.5 / 4.8 |
| 4 Cycle two, client two | 16–24 | M109–M115 | 27.5 (DS 4.0) | 23.5 + 1.5 client-2 support | 26.0 | 1.0 | 4.0 / 6.4 |
| 5 Buy through AWS | 24–28 | M116–M120 | 11.0 (DS 0.5) | 10.5 | 13.0 | 2.5 | 0.5 / 3.2 |
| **Total** | **28** | **31 milestones** | **96.5 (DS 14.0)** | **82.5 + 2.0** | **91.0** | **6.5** | **14.0 / 22.4** |

**Slack.**
- Phases 1 and 2 have almost none. The 0.75 per week holdback covers first-deployment on-call there.
- Phase 3's slack (2.5, down from 3.0 since M102 grew to 2.5 for the native send-log containers) absorbs the pilot's corrective cycle.
- Phase 4 is tight. If it slips, drop live arms (−1) or move M115 to Phase 5.
- Spare data-science time goes to E2/E4 retrospectives, cycle readouts and prospect audits.

### 6.3 Week-by-week view (4 engineers A–D, 1 DS)

| Weeks | Engineer A (platform, infra) | Engineer B (activation) | Engineer C (measurement, holdout) | Engineer D (UI, privacy, pilot kit) | Data scientist | Client calendar |
|---|---|---|---|---|---|---|
| 0–½ | M90 (all hands) | M90 | M90 | M90 | E2 power feasibility; E4 by hand | Prospect ledger; data request; P1 account opened; pilot signed |
| ½–3 | M92 prefixes, F-1/F-3/F-4/F-6, ActivationStack | M94 draft schema (week 1), builder, reasons | M95 holdout service | M91 consent fixes and snapshot | M96 planner and thresholds; M95 review | **E1 dry run with the hand-made draft file, weeks 1–3**; key-match sample |
| 3–6 | M92 infra finish (weeks 3–4); M93 staging (weeks 4–5, after the client-store merge), then pilot account (weeks 5–6) | M94 profiles, dry-run kit, key check; M96 lapse labels | M96 readiness sections and treatment audit; deployment support | M92 client store on Postgres (weeks 3–4); M96 neutral pilot kit, playbook, data request | M96 label stability; E4 | Assessment in their account; label, holdout, explore, send-log clause, DPO key sign-off, consent feed, publication terms signed; **contract 1.0 frozen (week 6)** |
| 6–8 | M97 S3 (STS) and webhook | M97 conformance kit, IAM fake, send-log protocol | M98 Campaign record | M100 UI skeleton; Connections destination group | M101 simulator and harness; M98 check | Model trained through an onboarding spec, then approved |
| 8–10 | M100 routes, release rule, download gate | M100 delta, erasure and withdrawal hooks, pilot profile | M101 engineering, `RunRecord.synthetic` first (weeks 8–8.5, before M100's release rule needs it); M99 test plan (weeks 8.5–9.5) | M100 panels; M99 plan card | M101 bands; M99 review; Criteo if a sample exists | **Plan registered → first list delivered (weeks 10–11)** |
| 10–13 | M107 connection-backed inputs; M102 native-container readers (with B, weeks 10–12) | M102 send-log importer (weeks 10–12); M103 reconciliation (weeks 12–13.5) | M104 audit and programme readouts | M108 revenue and CUPED (with DS); M104 UI | M108 CUPED; first audit readouts | Send log exported (~week 12); audit readout for a prospect; client 2 signed |
| 13–16 | M107 finish; client-2 deployment support | M105 Proof Pack | M106 customer view; M105 | M105 rendering; Results | M103 CACE and coverage; M105 review | **Reconciliation (~weeks 13–14)**; cycle 2 (~week 14–15, same model, manual delivery); **Proof Pack on the analysis date (30-day window: ~week 16)**; client 2 deployment and assessment (pod) |
| 16–20 | M113 loop | M109 catalogue | M111 learn (weeks 16–18); M112 beats risk | M114 warehouse and SFTP | M111; M112 cross-fitting and bootstrap | Cycle-1 outcomes learned from; cycle 3 (~week 19, manual); client 2 dry run |
| 20–24 | M113 finish | M110 value weighting; client-2 support | M112 finish | M114 second CEP recipe (client 2); M115 pod tools | M110; M112 wording | **Cycle 4 (~weeks 23–24): value-weighted, channel-aware, uplift only if it beat risk; delivered and send log pulled by schedule**; client 2's first contract ingested through configuration only |
| 24–28 | M116 hardening, FTR | M117 industry overlay and region | M118 cost; M120 tallies | M119 evidence pack | M117 label templates; cycle-4 readout; checkpoint tallies | Private offer submitted; client 2 live with reconciliation; cycle 4 measured by schedule; starter-gate DEC |

**What runs in parallel:**
- **Phase 1:** M92, M94, M95 and M96 are independent after M90 (M94 waits for M91's snapshot field). M93 starts only after both halves of M92 (infra and the client store) have merged.
- **Phase 2:** M97, M98 and M101 are independent. M99 follows M98, and M100 joins them all; M101's `RunRecord.synthetic` merges before M100's release rule.
- **Phase 3:** M102 → M103 → M105 is one lane. M106, M107 and M108 are separate lanes, and M108 must merge before M105. M104's audit and programme readouts start in week 10, and its 'against the universal holdout' basis follows M102 (week 12).
- **Phase 4:** M114, M109 → M110 and M111 → M112 are separate lanes. M109 starts after M107 merges (week 16). M113 starts on its scheduling plumbing and takes M111's learn step after week 18. M115 builds its other parts first and adds the 'challenger ready' and 'beats-risk failed' cards as M111 and M112 merge.
- **Phase 5:** all five milestones are independent.

Migrations must merge in order: 0006 (M92), 0007 (M98), 0008 (M100), 0009 (M109).

### 6.4 Scaling the plan

- **3 engineers + 1 DS** (about 2.25 net engineering per week): about 38 weeks (84.5 engineer-weeks, including client-2 support, at 2.25 per week).
  - **Cut first, in this order:**
    1. M119;
    2. M118;
    3. M117 beyond generic plus one pack;
    4. M112's live arms;
    5. M115;
    6. M114's SFTP half, unless client 2 needs it.
  - **Never cut:** M91, M95, M99, M100's release rule, M101 and M103.
- **5 engineers + 1 DS:** Phases 3–5 compress by about a third. Phases 1–2 do not, because the dry run, the sign-offs and the outcome window set their pace.
- **The starter (Plan K, only if the M120 decision says build):** add 9–15 engineer-weeks, staffed separately. It never takes capacity from the Phase 3–4 measurement work.
- **Arbitration (later plan):** about 3.5 engineer-weeks (DS 1). It is pulled in when one client signs a second, overlapping use case.

---

## 7. Do not build (and why)

- **Any message sending in Plan J** (SES, SMS, WhatsApp, push, OBD). The starter waits for the M120 decision and, if the gate passes, is a separate Plan K with one-shot sends only. Sending would also undermine "independent proof": "you marked your own homework".
- **Journeys:** waits, branches, triggers, follow-up sequences, a journey builder, an inbox, a template or landing-page designer, frequency-cap engines. The M90 design-rule test enforces this. Journey requests go to the client's CEP or a Minfy services contract.
- **A CDP, identity resolution, a reverse-ETL engine, or per-client custom ETL sold as product.** That layer is being bought up (Census, mParticle, ActionIQ). We write one governed table or file into the client's own storage, and the client's reverse ETL can carry it.
- **Per-vendor push API code, or holding CEP credentials.** We use files and tables only, until a paying client cannot ingest either.
- **A profile, send-log mapping or destination for a tool no paying client runs.** This includes the care-desk profile unless the pilot's care team asks for it.
- **Writing through source connections, or widening the `Connector` or `Storage` protocols.** Writes go only through the Destination protocol, with credentials governed by the activation stack.
- **A CRM, contact-management UI or any fifth navigation page.** The client's CRM stays the system of record. Plan H's four pages stay, and the customer view is a read-only Results sub-screen.
- **A real-time streaming decision engine, per-user reinforcement learning, a frontline lookup API or service-account API keys.** These need machine credentials, which DEC-765 chose to avoid, plus a threat model. They belong in a later plan, pulled forward only if checkpoint 5 fires.
- **Multi-offer arms (offer-level arms on the DEC-668 path) and cross-use-case arbitration.** Deferred to a later plan, per J13. M112's risk/uplift policy arms reuse `ArmSummary` and are in scope. Plan J's catalogue and contract already carry `mai_action_id` and `priority`, so both arrive later without a contract change.
- **Making campaign copy and RCA attachable to every use case.** Copy belongs to the client's CEP and LLM, and the change touches Phase 3A's contract.
- **An MCP server, CEP marketplace listings or AppExchange** before the memo's install counts: 3 installs for HubSpot, 5 for Klaviyo, AppExchange after 2 Salesforce clients.
- **Multi-tenant SaaS (M51, 16–23 engineer-weeks).** We stay at one deployment per client AWS account, including in Minfy-hosted fallbacks.
- **A consent manager.** We consume consent managers through an adapter (M107).
- **Several other things:**
  - money-precise CLV;
  - per-MAU or per-profile pricing;
  - CVM-suite extras such as loyalty points;
  - an "agent" headline;
  - automatic model promotion as the default;
  - non-INR currencies.
- **Unproven results and claims:**
  - self-graded attribution without a random control;
  - any "causal" label without random assignment we made or verified;
  - any result figure not measured on a real client, including the planted 12.7-point demo effect and Criteo results outside internal validation.
- **Industry- or region-specific logic in engine code** (for example telecom or India). Industry knowledge, such as telecom prepaid lapse, is a pack, and a region's rules (for example India's) are a region profile.
- **Changing the per-run holdout default, or loosening any existing test** to make room for the persistent holdout.
- **Embedding Dittofeed, Mautic or other copyleft or source-available engines.** They run only unmodified, as separate programs, in services work.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **No pilot or no P1 account at week 0.** M50 has waited on P1 since September. | Phase 1 code starts on fakes, M93 is held, and pilot-gate dates move with the signature. If the pilot slips by more than 4 weeks, the founders choose between audit-led sales and another pilot (§10 Q16). |
| **The hand-off fails.** The pilot's tool cannot take in the contract within 3 weeks, the keys do not match, or IT will not grant a write-scoped bucket or schema. | The E1 dry run uses a hand-made file in weeks 1–3, before any destination code exists, and includes a key-match step. S3 and SFTP are the default paths, with the warehouse optional. If checkpoint 1 fires, build a thin push for that one tool or drop the layer for that account. |
| **Contamination of 5% or more, or contact rate under 70%,** persists after a corrective cycle on 2 consecutive CEP clients. This is the memo's re-examine trigger. | The roster, drawn from the customer master, is delivered as an exclusion segment. M103 counts contacts from **any** campaign and alerts once per campaign. Phase 3 slack absorbs the corrective cycle. |
| **The CEP's own control group conflicts with ours** (CleverTap SCG, Braze GCG). | A written source-of-truth agreement at the Phase 1 exit. Scope `external` exists only under that agreement, recorded as `external_agreement_ref` through `PUT /holdout`. Reconciliation detects overlap. |
| **The release rule delays the first list** beyond the memo's weeks 3–6. | Its prerequisites are built during the assessment, so the first list lands at about weeks 10–11. E4 by-hand readouts give real-data proof from Phase 1, and M104 product audit readouts from about week 13. A short-window first use case (J10) lands the Proof Pack at about week 16. |
| **The first campaign is underpowered** or has a long window. | M96's power sheet is signed before any model is approved, and the written expectation is 1–3 points. A revenue outcome with a CUPED covariate can be pre-registered for campaign 1 (M99, M108). If the sponsor refuses any random holdout (checkpoint 4), sell only the assessment and audit readouts. |
| **Uplift does not beat risk targeting,** or the client has no randomised history. | Month one is propensity + control + explore. M112 computes the comparison out of sample and blocks uplift-ranked lists that do not beat risk. The designated fallback model stays releasable. Live arms (J15) give the strongest evidence. The result is reported whichever way it falls. |
| **Consent defects ship a non-compliant list:** the uplift bypass, missing purposes, the India "Reply STOP" default, stale consent, unpropagated withdrawals. | M91 lands before any delivery route. The release rule checks snapshot age. Withdrawals send suppression deltas (M100). The consent feed is agreed at the Phase 1 exit. Legal opinions are enforced in code (`RELEASE_LEGAL_OPINION_MISSING`). |
| **Copies in client systems escape DPDP erasure** (DEC-741). | Deliveries go only into the client's own bucket or schema, so the client is the controller. There is a receipt per delivery. Deletion manifests go out immediately, including to roster destinations, and `row_status=delete` follows (M100). The record of processors is in the evidence pack (M119). The DPO accepts at the Phase 1 exit. |
| **Shared-rule regressions:** the persistent holdout touches DEC-A4, and a new pipeline rebind could drop the consent gate. | Holdout changes are opt-in, with existing tests pinned unchanged and the stage edits limited by DEC-1306. The post-run builder adds no rebind (DEC-1307). The M91 test covers both flows. |
| **Parallel lanes collide** in shared files and common modules. | One integration branch and lanes merged daily. Shared-file edits go only through the daily contracts PR. Every in-place declaration is in the §3.5 register. |
| **The first real AWS deployment surfaces unknown failures** (EventBridge substitution, Object Lock, RDS migrations), or the client's security review is slower than the assessment. | M93 deploys to Minfy staging first. F-1, F-3, F-4 and F-6 are closed before real data arrives. A Minfy-hosted account (one deployment per client), approved by the client in writing, is an accepted fallback. |
| **No send-log export** is available, or it is a paid add-on (Braze Currents, MoEngage exports). | The aggregate campaign-report fallback is labelled "no per-customer reconciliation", with CACE null and a reason. 3 of 5 prospects lacking an export is the memo's "strengthen the starter" trigger, tallied in M120. |
| **The outcome paths diverge** once automation starts. | DEC-1305 and M98 make the Campaign path canonical before M102 and M113 build on it. A test pins the three measure routes together: identical reports on a mature uplift run at a fixed `as_of` (apart from `computed_at` and `campaign_id`), and on a propensity run the campaign route equals `campaign-results` with `bands = treat_bands`. |
| **Credibility, while all validation is synthetic;** Criteo cannot be downloaded or used externally. | M101 runs nightly coverage with seeded bands and the synthetic quarantine. Proof Packs refuse synthetic runs. External proof waits for audit readouts and the first pilot, with publication terms signed at the Phase 1 exit. |
| **Host platforms close the gap first** (MoEngage with Aampe, Braze with OfferFit). | Speed to the first real readout. Audit and programme readouts (M104) make us the neutral auditor. The trigger is reviewed every two quarters (M120). |
| **Capacity and key people:** a team of 4 engineers and 1 data scientist also carries pilot support, on-call for client deployments and client security reviews, and losing one person removes a quarter of a lane's capacity. | No sending, so no sender-reputation operations. Automation lands in Phase 4. Capacity is planned per role, with a holdback. The §6.4 cut order is agreed in advance. Phases 4–5 are provisional. |
| **Pod work hides product gaps,** and services become the business. | Each phase removes one manual step. The pod-effort log, started in M90 and filled from cycle 1, tracks checkpoint 7: 1.5 people or fewer by cycle 3. M115 adds its summary. |
| **Some CEP ingestion paths need paid tiers on the client side** (Klaviyo, Salesforce Data Cloud, Adobe), and no WebEngage file import is confirmed. | The Phase 1 dry run finds this before model work. The webhook plus the client's own Zapier or n8n covers path B, as do SFTP, the table route and the reverse-ETL recipe. |
| **Process overhead:** 12 shared files, generated docs, README checks, `check_no_skips`. | PLAN-J blocks land once, in M90. `MILESTONE_TESTS` grows milestone by milestone. Overhead is budgeted inside every estimate. |

---

## 9. Measures of success per phase

| Phase | Product measures (tested) | Business measures (observed) |
|---|---|---|
| 1 | Markers, the allow-list design rule and the consent regression are green. Holdout stability property tests are green. Contract golden files exist for each profile. The key and consent refusals are tested. The prefix test passes. The smoke journey passes in a real account. | Dry run done in 3 weeks or less, with zero manual column edits and ≥95% key match. Label, holdout, explore, consent feed, send-log export, DPO sign-off and publication terms in writing. Prospect ledger recorded for 100% of qualified prospects. At least 1 E4 retrospective delivered by hand. |
| 2 | Release-rule refusal tests, one per condition, including stale consent and legal opinion. The download-gate route walk passes. Conformance is green for fake, local, S3 and webhook. Nightly coverage is within its seeded bands. M98's route test passes: the three measure routes agree on a mature uplift run at a fixed `as_of`, and the campaign route equals `campaign-results` at `bands = treat_bands` on a propensity run. | First real list ingested natively by about week 11. Plan registered before delivery. Treat rows present, with zero non-consented treat rows in the real file. Receipt sha matches the bucket. |
| 3 | CACE and continuous coverage within their bands. Proof provenance test passes. Reconciliation equals injected contamination and contact exactly. The `declared_random` audit is never causal. CUPED variance reduction is within ±0.03 of ρ². | Contamination under 5% and contact rate of at least 70%, or a corrective cycle agreed. Proof Pack read by the sponsor and finance without a walkthrough. At least 1 audit readout, paid or free. Finance signed the value inputs. |
| 4 | The scheduled end-to-end cycle passes on fakes. The fallback model stays releasable. The cross-fitting leak test passes. Profit-curve identity holds with value weighting. The second CEP is added with no Python diff outside tests. | Cycle 4 needs only the Approver. A written beats-risk result. Client 2's contract is ingested on configuration only. Pod effort of 1.5 people per client or less by cycle 3. |
| 5 | The zero-send IAM test passes. A pack journey for an industry other than pilot 1's runs on fakes. Region rules apply across industries. The evidence pack lists every receipt, deletion and suppression delta. | Marketplace private offer submitted and FTR complete. Client 2, from a different industry than pilot 1, live with reconciliation. DPO accepts the evidence pack. Starter-gate and checkpoint decisions recorded as lettered sub-decisions of M120's DEC entry, with ledger counts. |

**Plan-level success:** one client result measured against a randomised control and published under signed terms. It must disclose its method and give net INR with a 95% range. That result, not the number of features, decides the next deal.

---

## 10. Open questions for the founders

1. **Pilot 1 and its tool.** Which client is pilot 1, in which industry, and which tool will they load the list into? The options are MoEngage, CleverTap, Braze, Netcore, SFMC, Zoho, or an SMS or WhatsApp provider. The answer decides the only profile and send-log mapping in Phases 1–3, and whether the warehouse or SFTP destination moves into Phase 2 (+2.5 engineer-weeks, +1 week to the first list).
2. **The AWS account.** Which account hosts pilot 1: the client's own, or a Minfy-hosted one they approve in writing (one deployment per client)? Who opens it and owns P1 (account), P2 (Bedrock access), P3 (budget alert) and on-call for client deployments? M93 cannot start without these answers.
3. **The first use case.** Will the sponsor accept the shortest-window use case with enough volume (J10), rather than 60–90-day churn or win-back?
4. **Holdout policy.** Do you accept universal scope at a fraction set by the power sheet (J4)? Will pilot 1 switch off their CEP's global control group for our audience (J5)? Who signs that agreement?
5. **Explore slice.** Will the sponsor accept a 5% explore slice in cycle 1, at its stated cost (J6)?
6. **The release rule.** Do you accept it, including the 2-day consent freshness and legal-opinion gating, and the first list at about weeks 10–11 rather than 3–6 (J3)?
7. **Gating uplift lists and live arms.** Should "beats risk ranking" block delivery of uplift-ranked lists from Phase 4 (J9)? Will pilot 1 run live equal-budget arms (J15), pre-registered in the test plan for the first uplift-ranked cycle after M112 lands (cycle 4, about weeks 23–24)? Until then the comparison is cross-fitted off-policy evaluation. If uplift fails the beats-risk check at cycle 4, that cycle runs the fallback ranking without arms, and the arms wait for the next cycle in which uplift passes.
8. **Legal opinions.** Who provides the written opinions, and by which week? For India, for example, they cover TCCCPR win-back eligibility, the DPDP purpose for modelling, and the record of processors for contract copies. Any use case marked `requires_legal_opinion` waits for them, in code.
9. **The consent feed.** Who at the client sends withdrawals, DND and consent-manager records into our ledger at least every 2 days? Is 2 days the right freshness?
10. **Pricing.** Is the activation hand-off part of the base licence or an add-on (packaging test E5)? Are audit readouts a paid SKU after the first two (J11)? How is the Data Readiness Assessment priced?
11. **Funding.** Is a team of 4 engineers plus 1 data scientist funded for about 28 weeks, plus about 1 pod person per active pilot outside these estimates? Phases 4–5 are provisional until the Phase 3 gate. If not funded, do you accept the §6.4 3-engineer variant and cut order?
12. **Approvers and users.** Who may be the second-person Approver: client staff only, or may a Minfy pod member approve? Who is the Admin who creates users (J16)?
13. **The starter gate and checkpoints.** Who keeps the prospect ledger from week 0? Is the M120 decision date the Phase 5 exit (about week 28), even if fewer than 5 prospects have qualified?
14. **Client 2.** Who is client 2? It should be from a different industry than pilot 1 and on a different CEP. Can it sign by about week 12? If its tool can ingest neither a file nor a table, do we accept the webhook plus their own Zapier or n8n, or decline the client until the M120 decision?
15. **Industry packs and publication.** After generic and pilot 1's industry pack, which pack comes next (retail/e-commerce, BFSI, subscriptions, travel, utilities or telecom), and who supplies the domain review? What publication terms will pilot 1 sign for its result?
16. **If pilot 1 slips by more than 4 weeks.** Do we continue Phase 1–2 code on fakes and sell audit readouts, or switch to another pilot?
17. **Transacting before M116.** Until M116 lands, should the first contracts transact directly, or as a Minfy professional-services private offer on AWS Marketplace?

---

## Review notes

Findings not applied, or applied only in part, one line each:

- **Claim DEC-1300…1499 now (sequencing review).** Not applied: the range DEC-1300…1399 is fixed for this plan. DEC-1300…1309 are M90's foundational entries. After that, each milestone records one DEC entry whose sub-decisions are lettered (a), (b), (c)…, so Plan J needs about 50 numbers. Numbers are taken in order with none pinned. If the range still runs out, the next hundred is claimed through §4 (DEC-1309).
- **Give each lane its own sub-block, PLAN-J-A…D (sequencing review).** Not applied: we chose the reviewer's alternative, a daily contracts PR owned by one person. Four extra blocks would change `PHASES` and all 12 shared files four more times.
- **Land the connection-backed source binding in Phase 1 (completeness review).** Timing not applied. The assessment can run on connection imports into uploads plus the existing multipart sources. Phase 1 has no capacity left, so the binding lands in M107 (Phase 3), before the loop needs it.
- **Pull a `ConsentManagerSource` fake into M91 (completeness review).** Partly applied. The adapter lands in M107. Before then, the consent feed reaches the ledger through the existing CSV import, agreed at the Phase 1 exit and enforced by the release rule's freshness check.
- **Make campaign copy and RCA attachable to any banded use case, turning `GenerativeKind` into a set (completeness review).** Not applied: copy sits outside the decide-and-prove boundary (it is the client's CEP and LLM), and the change alters Phase 3A's contract. It is listed in §7.
- **Move M96's lapse labels out of Phase 1 (sequencing review).** Not applied: label sign-off at the Phase 1 exit needs them. The agent treatment hints did move, to M104.
- **Cut M119, the DPDP evidence pack (sequencing review).** Not applied: it is kept, reduced to 2 engineer-weeks and provisional, and first in the §6.4 cut order. DPO evidence is a CIO-persona need, and its inputs already exist after M100.
- **Cut M118, cost before each run (sequencing review).** Not applied: it is kept in Phase 5 as provisional, and second in the cut order. Visible cost is customer need #5 and strategy item N10.
- **Allow `local_dir` only when `auth_mode=off` (sequencing review).** Applied in a different form: `fake` and `local_dir` are exempt only for synthetic runs or `env` in {local, dev}. Tying the exemption to sign-in being off would reward the less safe configuration.
- **Write-credential model, option (b): a runtime switch only (feasibility review).** Not chosen. We applied option (a), secrets only through the activation stack with STS for S3, plus option (b)'s runtime switch. That keeps "zero write permission when off" true.
- **Make DEC-1399 the starter-gate number (original plan).** Superseded by the in-order numbering rule. The starter gate takes the next free number when M120 is written.
