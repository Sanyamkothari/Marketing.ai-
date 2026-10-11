# Agents that check: Plan K

**Owner:** Plan K (`engine/agentic/**`, `engine/compliance/**`, `api/mcp/**`, `api/routes/agentic.py`,
`ui/modules/agentic/**`, `configs/agentic/**`, `tests/**/{agentic,compliance,mcp}/**`, this file).
**Decisions:** DEC-1400 … DEC-1499 in `docs/DECISIONS.md`. **Plans:** `docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md`
(what and why) and `docs/plans/PLAN_K_TECHNICAL_DESIGN.md` (how).

---

## 1. In plain words

An agent - ours, or another company's connected over MCP (M133) - may **look** and **suggest**. It looks
through *tools*: typed functions over numbers the product has already worked out (a test's size, who may be
contacted, what a campaign measured). It suggests through *proposals*: "register this test plan", "send this
list by email". A proposal is checked against the deployment's rules by the server, and it becomes real only
when a person with the Approver role approves it. Everything - each look, each suggestion, each refusal, each
approval - is written to a ledger that cannot be edited without it showing.

Everything here is switched off by default (`configs/agentic/agentic.yaml`, `agentic.enabled: false`; the MCP
server has its own switch, `configs/agentic/mcp.yaml`, `mcp.enabled: false`). Off, the product is exactly what
it was before Plan K: no route, and the app creates no table and writes no file (`api.main` imports only the
switch readers, `engine.agentic.config` and `api.mcp.config`). A migrated database is the one
exception: `alembic upgrade head` adds the empty `agentic_decision_log` table (revision 0009) whatever the
switch says, because a migration runs on every deployment.

## 2. The five invariants

Every Plan K change keeps these, and a test enforces each one (design §0).

| # | Invariant | Enforced by | Test |
|---|---|---|---|
| I1 | No tool can write outside our storage or send anything. | `ToolKind` has `read` and `propose` only; `ToolRegistry.register` refuses anything else. | `tests/unit/agentic/test_registry.py::test_there_is_no_write_kind`, `::test_every_registered_tool_reads_or_proposes`, `::test_the_registry_refuses_a_write_tool_and_other_lax_tools` |
| I2 | Every number a person or another agent sees comes from a deterministic tool result. | Tools read stored artefacts and never call a language model; results carry a content-hash `evidence_id`. M139 adds the claim verifier (design §4). | `test_registry.py::test_the_evidence_id_is_a_content_hash`, `tests/agentic/test_read_tools.py` |
| I3 | Nothing becomes real without a person; limits are enforced by the server. | Proposal → default-deny policy → `engine.approvals.decide_proposal` (Approver, re-checked) → the product's own route function. | `tests/unit/agentic/test_policy.py`, `tests/agentic/test_proposal_routes.py` |
| I4 | The holdout cannot be shrunk, skipped or reassigned by any agent path. | `holdout.immutable`, plus the holdout service's own epoch and fingerprint checks (DEC-1302). | `test_policy.py::test_each_rule_refuses_with_its_code[holdout …]`, the `*holdout*` scenarios |
| I5 | Every proposal, approval, refusal and evidence label is on a tamper-evident record. | `engine.agentic.ledger`: hash-chained, signed, verified by `python -m engine.agentic.ledger verify`. | `tests/unit/agentic/test_ledger.py` |

## 3. The tool registry (`engine/agentic/registry.py`, `tools.py`; M132)

A tool is an `AgentTool`: a snake_case `name`, a `kind` (`read` or `propose`), an input model that refuses
unknown fields, an output model, the `RoutePolicy` of the HTTP route that serves the same data (`mirrors`;
a test checks the two are identical, so an agent never sees more than the person could), an evidence label
(`Causal`, `Estimated`, `Not proven`, `Descriptive`) and `run`, a thin wrapper over an existing engine function.

`ToolRegistry.call(name, input, ctx)`:

1. refuses an unknown tool (`TOOL_UNKNOWN`), a principal without the route's role (`TOOL_ROLE_REQUIRED`) and an
   input its model refuses (`TOOL_INPUT_INVALID`; ids are letters, digits, `_` and `-`, so no id names a path);
2. runs the tool and validates the output; more than 16 KB of canonical JSON is refused
   (`TOOL_OUTPUT_TOO_LARGE`): aggregates only, a row never leaves a tool;
3. returns a `ToolResult` whose `evidence_id` is `"ev_" + sha256(canonical([tool, input, output]))[:16]` - the
   same question on the same artefacts is the same id - and writes `evidence.label` to the ledger, signed;
4. writes one audit event (`engine.audit.store`) for a call its mirrored route's request would have audited -
   a `propose` tool (its route is a POST) and a read whose policy has `audit_reads` (`check_contactable`,
   `value_segment`) - with the route's `action` and object, `route: tool:<name>`, `trigger: <agent>` and the
   outcome (`success`, `denied`, `failed` with its `reason_code`). The other reads are not audited, as their
   routes are not; the signed `evidence.label` entry records every call.

| Tool | Kind | Reads (computed once, never again) | Mirrors |
|---|---|---|---|
| `plan_test` | read | `engine.measurement.planner.power_preview` (counts only) | `POST /measurement/power-preview` (Viewer) |
| `check_contactable` | read | the scoring run's `channel_contactability.json`: per channel, contactable and not-contactable rows, `reasons` as row counts by code (`not_contactable`, and M131's `inquiry_lapsed`, `legacy_unregistered` when the run wrote them) and, apart, `columns_missing` (the configured columns the file lacked, by name) | `GET /runs/{run_id}/artefacts/{name}` (Viewer, audited) |
| `value_segment` | read | the run's `expected_gross_value.json` (M97), overall or one band; labelled "not incremental". No route serves this file by name (the treat list summary carries its total); the tool takes the run artefact route's policy | `GET /runs/{run_id}/artefacts/{name}` (Viewer, audited) |
| `measure` | read | the campaign's stored `incrementality_report.json` (M94's one measurement path); never measures again | `GET /campaigns/{campaign_id}` (Viewer) |
| `audit_campaign` | read | the campaign's stored `audit.json` and report (M103) | `GET /campaigns/{campaign_id}` (Viewer) |
| `get_proof_pack` | read | `engine.pilot.proof.build_proof` (M104), in short: the claim, the headline, each section's traced figures; its build time is left out | `GET /pilot/proof/{campaign_id}` (Viewer) |
| `evaluate_policy` | read | the run's stored `ope_report.json` and `risk_comparison.json` (M96; M137's rollout bound when present) | `GET /runs/{run_id}/risk-comparison` (Viewer) |
| `draft_test_plan` | propose | `engine.measurement.plan.freeze_plan` in dry mode over the campaign's assignment | `POST /campaigns/{campaign_id}/plan` (Analyst) |

Something not computed yet is answered with `null` and a plain sentence saying which route computes it, never
with a 0. Something stored that no longer reads (a partial write, a file from an older schema) is refused with
`TOOL_ARTEFACT_UNREADABLE`, naming the file (`engine.agentic.artefacts`), never reported as "not computed". A
run that does not exist is `RUN_NOT_FOUND`. `evaluate_policy` is labelled `Not proven` when either stored
artefact says the treatment was not random. Adding a tool means a registry entry, its `RoutePolicy`, tests through the product's routes and, from
M133, the MCP surface test - in one change.

## 4. The policy (`engine/agentic/policy.py`, `configs/agentic/policy.yaml`; M132)

A rule is a pure function `(ProposalSubject, PolicyContext) -> Decision | None`. The context is computed by the
server from stored artefacts (`build_context`), never taken from the payload when the server can compute it:
the use case's holdout and the holdout ledger's epoch, the M108 cost estimate and cap, the scoring run's
per-channel contactability, the arbitration caps (`configs/decide/arbitration.yaml`), the campaign's frozen test
plan and, at approval, the approver. Prompts never reach the policy.

**Default deny.** A kind with no rule set is refused (`POLICY_NO_RULES`); a payload not shaped as its kind is
refused (`POLICY_PAYLOAD_INVALID`); otherwise every rule of the set is evaluated and the proposal is allowed only
when every one that applies allows it. A refusal lists every rule it breaks, with counts (never a customer).

| Rule | Refuses (code) |
|---|---|
| `holdout.immutable` | a campaign plan holding back less than the share in force (the configured share, or the epoch's when larger, less `holdout.tolerance`), a scope other than the use case's (`run` for a persistent holdout, `universal` for a per-use-case one, or the reverse: other customers held back), held-out customers treated, or no holdout; a test plan whose campaign holds nobody back or that asks for another share than the assignment's (`POLICY_HOLDOUT_IMMUTABLE`) |
| `budget.cap` | the M108 estimate above `governance.max_run_cost_usd`, or unpriced while a cap is set (an agent cannot confirm a cost); offers above `uplift.policy.total_budget`; contacts above `uplift.policy.budget_contacts` (`POLICY_BUDGET_CAP`) |
| `consent.channel` | a plan reaching more customers on the channel than may be contacted there: its `planned_contacts` beyond the run's contactable eligible rows on the channel by more than `consent.max_not_contactable` (0); a channel the run did not record; or one recorded without a configured column, whose consent was never read (`POLICY_CONSENT_CHANNEL`). Customers who opted out of the channel are not on the run's list for it - the treat list never sends them anything there (M99) - so opt-outs elsewhere in the run do not refuse a plan that stays within the contactable rows |
| `frequency.cap` | more contacts per customer than `contact_cap_per_customer`, or more on the channel than its `channel_caps` entry (`POLICY_FREQUENCY_CAP`) |
| `test.registered` | a campaign plan with no frozen test plan for its campaign, or not the one cited (`POLICY_TEST_NOT_REGISTERED`); a test plan for a campaign that already has another frozen plan, or was measured before one was registered - what `POST /campaigns/{id}/plan` would refuse - so no Approver is asked to approve what cannot run (`POLICY_TEST_PLAN_FIXED`) |
| `role.separation` | at approval, the person it was proposed for approving it (`POLICY_ROLE_SEPARATION`); `engine.approvals.separation_refusal`, so with sign-in off (one operator holding every role) it cannot be enforced, as for models |

Rule sets at launch: `test_plan: [holdout.immutable, test.registered, role.separation]`; `campaign_plan:` all six. `copy_arms`
(M140) has none, so it is refused. The policy version is the sha256 of `policy.yaml`, written into every ledger
entry, so a past decision can be re-checked against the rules in force when it was made.

## 5. Proposals and their approval (`engine/agentic/proposals.py`, `engine/approvals.py`; M132)

```
draft (agent) ─policy─► refused (codes, on the ledger)
             └────────► pending ─person─► approved ─► executed by the product's own route function
                                  ├─────► rejected
                                  └─────► expired (no decision within proposals.expire_after_days, 14)
```

A `Proposal` holds `kind`, `payload` (canonical JSON), `evidence_ids` (each one must be on the ledger), the
policy's decisions, `payload_digest` (`sha256(canonical(payload))`, the pass^k key), the status, who it was
proposed for and by which agent, the policy version and its expiry. It is stored at
`agentic/proposals/<id>.json`: ids, counts and the plan's own words, never a customer row.

A proposal is drafted only for an Analyst (`PROPOSER_ROLE`), whatever its kind and whatever path drafts it
(the registry, M133's MCP server, M139's planner): `submit_proposal` refuses anyone else with
`TOOL_ROLE_REQUIRED` (403). Every evidence id it cites must be on the ledger (one indexed lookup).

`draft_test_plan` computes the plan `freeze_plan` would freeze for the campaign now - the outcome window resolved
and the analysis date checked as the plan route does - and proposes it with its `plan_hash`. Nothing is
registered.

`engine.approvals.decide_proposal` is the second decision kind beside model decisions (`DECISION_SUBJECTS`). The
stored proposal file is ordinary artefact storage, so before anything else it must be the proposal the ledger
recorded: its payload must hash to its `payload_digest`, and the digest, kind, proposer, agent, evidence ids,
creation time and expiry must equal its `proposal.drafted` entry, which no later entry may have decided
(`engine.agentic.proposals.ledger_mismatch`). Otherwise it is refused as `409 PROPOSAL_TAMPERED`
(`proposal.refused` on the ledger, with the digest the ledger recorded) and nothing runs. On approve it then
checks the proposal is not expired, re-runs the policy with the facts as they are now and the approver in the
context (`role.separation`; a plan frozen meanwhile is `POLICY_TEST_PLAN_FIXED`), refuses a test plan whose dry
hash has changed (`PROPOSAL_STALE`), and calls the executor the caller passes: the `/agentic` route passes
`api.routes.campaigns.register_plan`, the function `POST /campaigns/{id}/plan` runs. A plan that route returns
with another hash than the one proposed is refused as `PROPOSAL_STALE`, never recorded as an approval. An agent
never gets a second path to anything.

| Method and path | Role | Does |
|---|---|---|
| `GET /agentic/proposals` | Viewer | Every proposal, newest first; `?status=` narrows. |
| `GET /agentic/proposals/{proposal_id}` | Viewer | One proposal with its evidence ids and decisions. |
| `POST /agentic/proposals/{proposal_id}/approve` | Approver, audited | `{reason?}`. 200 with the receipt (`ledger_checkpoint`: the ledger head after the approval; keep it). 403 `POLICY_ROLE_SEPARATION`, 409 `POLICY_*`, `PROPOSAL_TAMPERED`, `PROPOSAL_STALE`, `PROPOSAL_EXPIRED`, `PROPOSAL_NOT_PENDING`, `PROPOSAL_NOT_EXECUTABLE` (a campaign plan: nothing carries one out until M139; it stays pending), the executing route's own code; 404 `PROPOSAL_NOT_FOUND`. |
| `POST /agentic/proposals/{proposal_id}/reject` | Approver, audited | `{reason?}`. 200 with the receipt. |

There is no route that drafts or approves for an agent; the routes exist only while `agentic.enabled` is on.

## 6. The decision ledger (`engine/agentic/ledger.py`; M132)

`agentic_decision_log` (platform database; Alembic `0009_agentic_ledger`): `seq`, `prev_hash`, `entry_hash`,
`kind`, `actor`, `body`, `proposal_id`, `evidence_id`, `signature`, `key_id`, `created_at`. `proposal_id` and
`evidence_id` copy the body's own values into indexed columns, so a proposal's entries
(`DecisionLedger.proposal_entries`) and a proposal's cited evidence (`missing_evidence`, one `IN` query) are
index lookups; verification refuses an entry whose column disagrees with its body.

* `entry_hash = sha256(prev_hash ‖ canonical({seq, kind, actor, created_at, body}))`, so any edited field breaks
  the chain at that entry, and a re-hashed forgery breaks the next link or its signature.
* Kinds: `proposal.drafted`, `proposal.refused`, `proposal.approved`, `proposal.rejected`, `proposal.expired`,
  `approval.refused`, `proposal.execution_failed`, `test_plan.frozen` (with the campaign's holdout scope, epoch
  and salt id: the holdout commitment), `evidence.label`. Approvals, frozen test plans and evidence labels are
  signed over `entry_hash`.
* `Signer` protocol: `FileKeySigner` (Ed25519, `cryptography`, already pinned; `<data dir>/agentic/
  ledger_signing_key.pem`, created once with 0600 permissions), `FakeSigner` for tests; AWS KMS on client
  deployments later (design §3.2, open question 2).
* A body holds only the keys in `BODY_KEYS` (ids, digests, codes, labels, the policy version) with scalar
  values: an approver's reason is kept as its digest. Erasure never touches the ledger.
* `python -m engine.agentic.ledger verify [--data-dir DIR] [--key PATH] [--from-checkpoint ENTRY_HASH]` walks
  the chain in batches (linear in the entries) and checks every signature; on the first bad entry it prints
  `LEDGER_CHAIN_BROKEN` or `LEDGER_SIGNATURE_INVALID` with its `seq` and exits 1. `--from-checkpoint` walks only
  the entries after an `entry_hash` already verified (an approval receipt's `ledger_checkpoint`, or the head a
  previous run printed); the first of them must point at it, and a checkpoint no longer in the ledger is
  `LEDGER_CHAIN_BROKEN` (the ledger was rewritten under it).
* Appending is one short Core transaction (about 1.5 ms on SQLite after warm-up; design budget 5 ms). Verifying
  splices each stored canonical body into the hashed content (one canonical write per entry) and costs about
  15 µs per unsigned entry and about 130 µs per signed one, almost all of it the Ed25519 check, on the shared
  4-CPU build machine. Measured end to end on SQLite: a million entries of the real mix (60% signed: evidence
  labels and frozen plans, signed with the Ed25519 file key) verified in 157 s, reading in batches of 10,000
  (building them took 128 s), while other test runs shared the machine. That is above the design's 30 s
  budget for a full walk, so a deployment verifies from its last checkpoint (`--from-checkpoint`), which costs
  only the entries since; a full walk stays linear (`tests/unit/agentic/test_ledger.py`, signed entries).

## 7. Evaluation: scenarios, pass^k and the mutation test (`engine/agentic/evals.py`; M132)

A scenario is `(fixture world, request, expected outcome)` (`tests/agentic/scenarios/`). The world is built by
the product's own code: an uplift model trained and a campaign file scored through `Pipeline`, with SMS and
email consent columns; campaigns created and a test plan registered through the API; a copied configuration root
with an offer budget, a cost cap, arbitration caps and a persistent holdout on a second use case.

* **pass^k**: every scenario runs ten times; it passes only when the ten outcomes (status, codes, payload digest)
  are identical and match the expectation. The harness takes an `LLMClient` (`FakeLLMClient` in CI) and records
  its model id; M132 has no model step, so it is not called and the cost is zero.
* **Mutation test**: each policy rule is removed in turn; at least one scenario - in particular the ones written
  to guard it - must then fail. With every rule removed a kind is still refused (default deny).
* The report is `EvalReport`, written by `write_report` as canonical JSON (`reports/agentic_eval.json` by default).

## 8. The verifier over MCP (`api/mcp/`, `configs/agentic/mcp.yaml`; M133, DEC-1404)

### 8.1 In plain words

Another company's agent - Claude, ChatGPT, an in-house agent - can connect to Marketing AI over the Model
Context Protocol and ask it what the product already knows: how big a test needs to be, who may be contacted,
what a list is worth, what a campaign measured, how an outside campaign audits, its Value Proof Pack. It can
also **draft** a test plan, which then waits for a person to approve it in the product. In M133 it answers
about the **demo library only** (Hillstrom and generated data); client data waits for M141.

### 8.2 Connect a client

1. Switch it on: `agentic.enabled: true` (`configs/agentic/agentic.yaml`) and `mcp.enabled: true`
   (`configs/agentic/mcp.yaml`). Off - the default - nothing is mounted and the MCP SDK is not even imported.
2. Sign-in must be on (`MARKETING_AI_AUTH_MODE=local`); with sign-in off the endpoint answers
   `503 AUTH_NOT_CONFIGURED`, even on a laptop.
3. Sign in as the person the agent acts for and keep the token: `POST /auth/login`
   `{"username": ..., "password": ...}` answers `{"token": ...}`. A Viewer's token reads; an Analyst's also drafts.
4. Point the client at `https://<deployment>/mcp` (streamable HTTP) with the header
   `Authorization: Bearer <token>`. For example, a client that takes a JSON server list:

   ```json
   {"mcpServers": {"marketing-ai": {"type": "http", "url": "https://<deployment>/mcp",
                                    "headers": {"Authorization": "Bearer <token>"}}}}
   ```

The server speaks the stateless 2026-07-28 revision (one self-contained POST per request, no session id), so it
sits behind the existing load balancer with no sticky sessions; clients of the 2025 revisions are served
statelessly too. The endpoint takes POST only: GET (an event stream) and DELETE (ending a session) have no use
on a stateless server and are answered `405` before sign-in. The token is the product's own session, checked by the deployment's identity provider: the
endpoint is a resource server and issues nothing (OAuth client registration and audience-bound tokens come with
M141).

### 8.3 What it exposes

Exactly the agent-safe core's tools (§3) - no tool is written for MCP - each with its input model's JSON schema,
its output model's JSON schema and annotations (`readOnlyHint` on a read; a propose tool is not destructive and
touches no outside system). A caller is shown only the tools its scopes allow.

| Scope | Role | Tools |
|---|---|---|
| `marketing-ai:read` | Viewer (every signed-in person) | `plan_test`, `check_contactable`, `value_segment`, `measure`, `audit_campaign`, `get_proof_pack`, `evaluate_policy` |
| `marketing-ai:propose` | Analyst | `draft_test_plan` |

Per tool, the tool's own `RoutePolicy` decides: its role maps to the scope (`api.mcp.scopes.scope_for`). A tool
whose route needs an Approver or an Admin maps to no scope and is never exposed. **There is no scope that
approves.**

A result is `structuredContent` that validates against the tool's output model, a short text written by our code
from numbers only (never an arm's name, a column or a file name), and `_meta`. The text states a campaign's
result in the Value Proof Pack's scope and words: for a campaign of several offers, every offer together against
the shared control (`offers_combined`), or, for a report that does not add them, the first offer alone, said
so - never one offer's figures as the campaign's. Then `_meta` carries `marketing-ai/evidence_id` (the
ledger's evidence id, recorded with the actor `agent:mcp:<client>`), `marketing-ai/label` (Causal, Estimated,
Not proven, Descriptive) and, when something was withheld, `marketing-ai/withheld`. A refusal is a tool error
with `marketing-ai/code`; a fault inside a tool is `MCP_TOOL_FAILED`, never its message.

### 8.4 What it will never do

* Approve, reject, send, export, write to a client system or change a setting: no such tool exists, and the
  registry has no kind that could hold one (I1). A drafted proposal goes through the default-deny policy and an
  Approver in the product, as every proposal does (I3); a malicious one (no holdout) is refused with
  `POLICY_HOLDOUT_IMMUTABLE` on the ledger.
* Answer about anything but the demo library (`MCP_DEMO_ONLY`): a run or upload made for one of
  `mcp.demo_use_cases` (the `library/` datasets), or marked synthetic; never an onboarded client's run. The
  answer is the same for "not demo" and "not there", and it never repeats the id it was given.
* Show a group of fewer than 10 customers. Plan I's `rate_by` rule on every grouped output (bands, channels,
  reason codes, arms, segments, whole reports; `api.mcp.suppress`): a group of 1 to 9 shows no figure, and the
  next smallest joins it until what is withheld adds up to 10. A narrowed question (`value_segment` for one
  band) is answered from the suppressed full answer, so two overlapping questions cannot isolate a small band.
  A campaign with a group of 1 to 9 customers anywhere the Proof Pack reads one - its totals, an offer's arm of
  the stored report, a group of `segment_effects.json` - is not served at all by any tool that reads it
  (`MCP_GROUP_TOO_SMALL`): the Pack prints figures as text, which suppression cannot reach.
* Pass on text from outside the product (`api.mcp.gate`): arguments are never echoed, instruction-shaped text
  (also written as `snake_case` or with look-alike letters) is withheld, personal data is masked with Plan G's
  scanners. The product's own sentences pass byte for byte.
* Let one session flood it: `mcp.rate_limit` - 60 calls a minute and 2 at once per sign-in by default
  (`MCP_RATE_LIMITED`, with `retry_after_seconds`).

Every call writes one audit event (`mcp.tool_call`: the principal, `client_id` = `mcp:<the name the client
declared>` - for a client of the 2025 revisions, whose name went with an `initialize` the stateless server does
not keep, `mcp:ua-<its User-Agent>` - the tool, the outcome and its code, and the sha256 of what was served); the HTTP request is
audited as `mcp.request`; a read the product's route would audit is audited again by the registry (§3).

### 8.5 Long jobs

The 2026-07-28 revision moved Tasks into an extension, and the pinned SDK (`mcp==2.3.0`) ships the extension
mechanism but no Tasks implementation. A long tool (`audit_campaign` and `get_proof_pack` read stored artefacts;
the Proof Pack is drawn on request) therefore runs **synchronously** on a worker thread and is refused with
`MCP_TIMEOUT` after `mcp.call_timeout_seconds` (120 s). The thread cannot be stopped, so its call keeps its place
in the session's concurrency budget until it ends.

### 8.6 Limits

* The rate limits are counted in the API process; several API processes divide the budget among them.
* The client id is what the client declares about itself (cleaned to letters, digits, `.`, `_`, `-`): it names,
  it does not authenticate. The principal is the authenticated part.
* What is demo data is decided by the use case a run or upload was filed under (or `synthetic`), not by where
  its bytes came from: the product keeps no other provenance for an upload, and the library's derived files
  (the assignment and outcomes files `library.audit` builds) have no recorded hash. Anyone who may upload could
  file client data under `hillstrom-email`. `mcp.workspace: demo` declares the whole deployment a demo one: a
  deployment that holds client data keeps `mcp.enabled` off until M141 scopes the server per client.
* The text gate's instruction screen is a pattern list: wide on purpose, but a pattern list. What protects a
  client's model is that results are typed aggregates first; the screen is the second line.
* `POST /campaigns/audit` with an assignment column whose name holds a JSON special character (`:`, `"`, `[`,
  ...) fails inside the randomness check (LightGBM refuses the feature name) - found while planting header
  canaries; raised with Plan J as a cross-branch request (M133's hand-off).

Tests: `tests/unit/mcp/` (switch, scopes, guard, suppression and its coverage walk, gate, limits, the in-process
surface) and `tests/integration/mcp/` (a scripted client gets Hillstrom's numbers from the product's own API,
the tool-list walk, sign-in and scopes, POST only, the malicious proposal, demo-only on a client's measured
campaign, a small offer refused, rate limits, audit (also of a 2025 client), injection canaries, the differencing
attack).

## 9. Milestones

The milestones are in `docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md` §3.2; the README's Plan K table lists the tests of
each one that is done. M131, M135 and M137 were built ahead of the rest of Plan K, under Plan J's decision range.

| Milestone | State | Decision | Where |
|---|---|---|---|
| M130 Plan K set-up | done | DEC-1400 | the decision range, the `PLAN-K` blocks, `PLAN_K_CODES`, the three switches (§10) |
| M131 TRAI Third Amendment, and the measurement purpose | done (built early under Plan J) | DEC-1323 | `docs/DECIDE.md` §26 |
| M132 The agent-safe core | done | DEC-1401 | this document's §3–§7 |
| M133 The verifier over MCP, demo data only | done | DEC-1404 | this document's §8 |
| M134 The pre-send compliance gate | pending | | |
| M135 Prediction covariates (MLRATE / CUPAC) | done (built early under Plan J) | DEC-1324 | `docs/DECIDE.md` §27 |
| M136 Pooling across campaigns | pending | | |
| M137 "Safe to roll out": a lower bound in rupees | done (built early under Plan J) | DEC-1325 | `docs/UPLIFT.md` §12 |
| M138 An early harm alarm | pending | | |
| M139 The campaign planner | pending | | |
| M140 Message variants as measured arms | pending | | |
| M141 The verifier on client data | pending | | |
| M142 Setup help for integration sources | pending | | |
| M143 Tabular foundation model challenger | pending | | |
| M144 Save-desk card, as a randomised pilot | pending | | |

## 10. The switches (`configs/agentic/agentic.yaml`)

Every switch is off by default, and with all three off the product is byte-identical to Plan J's (design §11).
They live in `configs/agentic/agentic.yaml`, read by `engine/agentic/config.py` from the configuration root like every
other `configs/` file, because they are per-deployment settings with no secret in them; a misspelt switch is
refused (`CONFIG_INVALID`), never ignored. `tests/unit/agentic/test_flags.py` pins all three off.

| Switch | Milestone | Off (the default) means |
|---|---|---|
| `agentic.enabled` | M132 | no `/agentic` route, no table created and no file written by the app (§1) |
| `compliance.gate.enabled` | M134 | no pre-send gate; declared by M130, read by nothing yet |
| `mcp.enabled` | M133 | no MCP server (§8); lives in `configs/agentic/mcp.yaml` |
