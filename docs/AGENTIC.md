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
server has its own switch in the same file, `mcp.enabled: false`, and its settings in `configs/agentic/mcp.yaml`). Off, the product is exactly what
it was before Plan K: no route, and the app creates no table and writes no file (`api.main` imports only the
switch readers, `engine.agentic.config` and `api.mcp.config`). A migrated database is the one
exception: `alembic upgrade head` adds the empty `agentic_decision_log` table (revision 0009) whatever the
switch says, because a migration runs on every deployment.

## 2. The five invariants

Every Plan K change keeps these, and a test enforces each one (design §0).

| # | Invariant | Enforced by | Test |
|---|---|---|---|
| I1 | No tool can write outside our storage or send anything. | `ToolKind` has `read` and `propose` only; `ToolRegistry.register` refuses anything else. | `tests/unit/agentic/test_registry.py::test_there_is_no_write_kind`, `::test_every_registered_tool_reads_or_proposes`, `::test_the_registry_refuses_a_write_tool_and_other_lax_tools` |
| I2 | Every number a person or another agent sees comes from a deterministic tool result. | Tools read stored artefacts and never call a language model; results carry a content-hash `evidence_id`. M139: the claim verifier and renderer (§9) - every number in a narrative is a slot filled from evidence. | `test_registry.py::test_the_evidence_id_is_a_content_hash`, `tests/agentic/test_read_tools.py`, `tests/unit/agentic/test_claims.py`, `tests/agentic/scenarios/test_planner_scenarios.py::test_no_rendered_sentence_carries_an_ungrounded_number` |
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
| `POST /agentic/proposals/{proposal_id}/approve` | Approver, audited | `{reason?}`. 200 with the receipt (`ledger_checkpoint`: the ledger head after the approval; keep it). 403 `POLICY_ROLE_SEPARATION`, 409 `POLICY_*`, `PROPOSAL_TAMPERED`, `PROPOSAL_STALE`, `PROPOSAL_EXPIRED`, `PROPOSAL_NOT_PENDING`, `PROPOSAL_NOT_EXECUTABLE` (a campaign plan the planner did not draft: nothing carries it out; it stays pending), the executing route's own code; 404 `PROPOSAL_NOT_FOUND`. |
| `POST /agentic/proposals/{proposal_id}/reject` | Approver, audited | `{reason?}`. 200 with the receipt. |
| `POST /agentic/plans` | Analyst, audited | M139 (§9): `{card}`, `{goal}` or `{goal_text}`. 201 with the plan and its proposal; a plan that stops answers its code (404 `PLANNER_CARD_NOT_FOUND`, 409 `PLANNER_NOTHING_TO_PLAN`, `PLANNER_NO_CANDIDATE`, `AI_NOT_CONNECTED`, 422 `PLANNER_GOAL_UNPARSED`) and is kept. |
| `GET /agentic/plans/{plan_id}` | Viewer | M139: the plan's state (`agentic_runs/<id>/state.json`) and its proposal. 404 `PLANNER_NOT_FOUND`. |
| `POST /agentic/plans/{plan_id}/resume` | Analyst, audited | M139: a plan a crash left part-way continues from the last step it wrote; a plan at its interrupt, done or stopped is answered as it is. 404 `PLANNER_NOT_FOUND`; a stop answers its code as a draft does. |

No route approves for an agent; the routes exist only while `agentic.enabled` is on. M139 carries out an
approved campaign plan the planner drafted (§9); M132's campaign plans drafted any other way stay
`PROPOSAL_NOT_EXECUTABLE`.

## 6. The decision ledger (`engine/agentic/ledger.py`; M132)

`agentic_decision_log` (platform database; Alembic `0009_agentic_ledger`): `seq`, `prev_hash`, `entry_hash`,
`kind`, `actor`, `body`, `proposal_id`, `evidence_id`, `signature`, `key_id`, `created_at`. `proposal_id` and
`evidence_id` copy the body's own values into indexed columns, so a proposal's entries
(`DecisionLedger.proposal_entries`) and a proposal's cited evidence (`missing_evidence`, one `IN` query) are
index lookups; verification refuses an entry whose column disagrees with its body.

* `entry_hash = sha256(prev_hash ‖ canonical({seq, kind, actor, created_at, body}))`, so any edited field breaks
  the chain at that entry, and a re-hashed forgery breaks the next link or its signature.
* Kinds: `proposal.drafted`, `proposal.refused`, `proposal.approved`, `proposal.rejected`, `proposal.expired`,
  `approval.refused`, `proposal.execution_failed`, `campaign_plan.executed` (M139), `test_plan.frozen` (with the campaign's holdout scope, epoch
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
* **M139** adds planner scenarios (`PlannerScenarioRequest`; `tests/agentic/scenarios/planner_scenarios.py`): every
  M105 card type, goals through the form and in words, a planted instruction, and models that word their claims
  differently on every call, write a wrong direction (against an interval that excludes zero: `CLAIM_DIRECTION`),
  a number or an unreferenced claim. Their expected payload digests are computed from the stored artefacts
  independently of the planner. Model calls, their cost, claim failures (by code) and AI service errors are
  totalled in the report and are not part of an outcome's key: wording may vary, decisions may not.

## 7a. The save-desk card (`engine/agentic/save_desk.py`, `api/routes/agentic_save_desk.py`; M144)

**What it is.** A read-only lookup for call-centre staff. Before a retention call, an Analyst asks for the
customer's card and gets: the top reasons in business words, the recommended offer and channel, the net value in
rupees, and a "likely to stay anyway, do not discount" flag. Each line carries its evidence label
(`Estimated` for the model's recommendation, `Descriptive` for "kept back on purpose" and "do not contact") and the
stored file it was read from. **No line is `Causal`**: the card is the model's recommendation, and the only proof
that it helps is the pilot below. The card says so in its `caution`.

**Where each line comes from (compute once).** Nothing is computed, trained or written. A finished scoring run's
`treat_list.parquet` (M98) holds the reasons (M98's template phrases), `treat`, `offer`, `channel`,
`contactable_channels`, `net_value` and `expected_gross_value` (M97), `segment` (the uplift segments),
`control_group`, `holdout` and `suppression_reason`. The list's own net-value range is `policy_recommendation.json`
(`net_value_low`, `net_value_high`). The card is those values and fixed sentences.

* **Net value.** `customer_net_value` is the customer's own predicted net value (incremental). The *range* is the
  whole list's, never the customer's: the product does not compute a per-customer interval and the card does not
  invent one. A propensity run has `gross_value` instead, marked not incremental. A figure the run did not compute
  is `null` with a sentence saying why.
* **Likely to stay anyway.** True when the run's segment is `sure_thing` (the uplift model expects the customer to
  convert without an offer). False for the other segments. `null` on a run that ranks by chance to respond (it has
  bands, not segments): the card says it cannot tell, it never says "no".
* **Kept back.** A customer in the run's control group, in M92's persistent holdout (I4), or under a suppression
  rule gets **no offer**, whatever segment they are in.
* **Rules only.** Every sentence is a fixed template (no digit of its own, no jargon, both tested) or M98's phrase.
  The optional paraphrase through M139's claims renderer is **not built**: M139 was developed in parallel and was
  not available. The card says `text_source: "rules"`. Adding it later means a new, claim-checked field; nothing
  here calls a model (a test imports the module in a fresh process and checks no model client is loaded).

**The route.** `POST /agentic/save-desk/card`, body `{"run_id", "customer_key"}`; mounted only when
`agentic.enabled` is on (off: the path does not exist, the OpenAPI document is unchanged).

* The customer key is in the body, never in a URL (DEC-746), and is not echoed in the answer or in any refusal.
  A run keyed by several columns takes the parts in the run's order joined by `|`. `00104` finds `104`, as the
  consent ledger does.
* **Analyst**, because that role may already download the same rows (`ROW_LEVEL_DOWNLOAD`). A narrower `agent_desk`
  role was not added: roles are the closed `Role` enum and a new member would change every access table and "for
  every role" test. A Viewer gets `403 ROLE_REQUIRED`, audited.
* **Audited.** One audit event per call, whatever it answers: actor, run, and `principal_hash`, a salted hash of the
  key (`privacy_salt`), so "who looked at this customer" is answered by recomputing the hash. No key and no value is
  in the trail or in a log line.
* **Rate limited.** `configs/agentic/save_desk.yaml`, `rate_limit_per_minute` (default 30) per signed-in person in a
  sliding minute, in memory per process; past it `429 SAVE_DESK_RATE_LIMITED` with `Retry-After`.
* **A customer picked at random to learn from** (M92's explore slice, which can draw a customer the model expects to
  stay anyway) is still made the offer: the card carries an `explored` line, and "likely to stay anyway" stays on
  the card as the model's view without the instruction "Do not discount", because following that would undo the
  random draw that off-policy evaluation depends on.
* **It builds nothing.** If the run's treat list has not been built (the Results page or `treat_list.csv` builds it
  on first open), the answer is `409 SAVE_DESK_NO_TREAT_LIST`. A lookup must not start a build over a million rows.
* Refusals: `404 SAVE_DESK_CUSTOMER_NOT_FOUND`, `422 SAVE_DESK_KEY_INVALID` (empty, over 200 characters, or the wrong number of key parts; the
  message never holds the key),
  `409 SAVE_DESK_KEY_AMBIGUOUS` (two rows with that key), `403 SAVE_DESK_NOT_IN_PILOT_ARM`, and the existing
  `RUN_NOT_FOUND`, `RUN_NOT_SCORED`, `TOOL_ARTEFACT_UNREADABLE`.

**The pilot (a design, not a result).** The card ships only after a randomised pilot shows staff with the card keep
more customers. `configs/agentic/save_desk_pilot.yaml` is the registered template; copy it and fill it in.

1. **Randomise the staff member, not the call.** The cluster is the staff member: each either has the card for the
   whole pilot or never does. `assign_card_access(staff_ids, salt=..., card_share=...)` splits them exactly (not a
   coin flip each, which can give 5 against 12), independently of the order of the list, reproducibly from the salt.
   Draw once, paste the two lists into the file, never redraw.
2. **Outcome.** Retained within the window (`outcome_window_days`), as `retained_30d` in the outcomes file. The
   customers of staff with the card are the "treated" group of the campaign, the customers of staff without it the
   comparison group.
3. **Register the plan first.** `test_plan` is the body of `POST /campaigns/{campaign_id}/plan`
   (`TestPlanInput`); a test registers the shipped example through that route. Its `mde_pp` is the clustered figure.
4. **Power with clusters.** `pilot_power(planning, card_share, target_pp)` hands the planner
   (`engine.measurement.planner.mde_two_proportions`, `n_for_mde`) the *effective* number of customers,
   `n / DEFF` with `DEFF = 1 + (calls per staff - 1) x ICC`. For 200 staff with 30 calls each, a 60% base
   retention and an assumed ICC of 0.05, the design effect is 2.45 and the smallest change the pilot reliably sees is
   5.6 points; the same customers, wrongly treated as independent, would have seen 3.6. Seeing 5 points needs 121
   staff in each arm (with an uneven split `pilot_power` gives the card arm's and the no-card arm's staff separately). **The ICC is an assumption** (`icc_source` says where it came from); it is not measured here.
   The number of staff, not of calls, is what limits a cluster pilot.
5. **Active means enforced.** With `status: active` only staff in `card_access` are served a card (everyone else
   gets `SAVE_DESK_NOT_IN_PILOT_ARM`); `status: example` is not enforced.

**What the existing measurement can and cannot claim.** `measure_campaign` compares two groups of customers with a
Newcombe interval, as if each customer were independent. There is no cluster-robust standard error anywhere in the
engine (no design effect, no cluster column, no staff-level analysis). Customers handled by one staff member are
not independent, so that interval would be too narrow and a result read from it would be too confident. **The
pilot therefore needs clustered standard errors added to the measurement before a result is read, and until then
no result of this pilot is claimed.** The planning figures above allow for clustering; the measurement does not.
The card is shipped as a pilot tool, off by default behind `agentic.enabled`, and "ships only on a measured result"
is still open.

## 8. The verifier over MCP (`api/mcp/`, `configs/agentic/mcp.yaml`; M133, DEC-1404)

### 8.1 In plain words

Another company's agent - Claude, ChatGPT, an in-house agent - can connect to Marketing AI over the Model
Context Protocol and ask it what the product already knows: how big a test needs to be, who may be contacted,
what a list is worth, what a campaign measured, how an outside campaign audits, its Value Proof Pack. It can
also **draft** a test plan, which then waits for a person to approve it in the product. In M133 it answers
about the **demo library only** (Hillstrom and generated data); since M141 a deployment can serve its clients'
own data instead, one workspace per client (§8.7).

### 8.2 Connect a client

1. Switch it on: `agentic.enabled: true` and `mcp.enabled: true`, both in `configs/agentic/agentic.yaml` (§11;
   `configs/agentic/mcp.yaml` holds the server's settings, not its switch). Off - the default - nothing is mounted
   and the MCP SDK is not even imported.
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
endpoint is a resource server and issues nothing. On a client-data server (§8.7) the token is bound to the
one client workspace its person belongs to; OAuth client registration and audience-bound tokens wait for the
deployment's identity provider (Plan C's decision P5).

### 8.3 What it exposes

Exactly the agent-safe core's tools (§3) - no tool is written for MCP - each with its input model's JSON schema,
its output model's JSON schema and annotations (`readOnlyHint` on a read; a propose tool is not destructive and
touches no outside system). A caller is shown only the tools its scopes allow.

| Scope | Role | Tools |
|---|---|---|
| `marketing-ai:read` | Viewer (every signed-in person) | `plan_test`, `check_contactable`, `value_segment`, `measure`, `audit_campaign`, `get_proof_pack`, `evaluate_policy`, `check_before_contact` (M141, §8.7) |
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
Not proven, Descriptive), `marketing-ai/receipt` (since M141: the ledger chain, seq and entry hash the answer was
recorded under and the holdout salt's fingerprint, §8.7) and, when something was withheld, `marketing-ai/withheld`.
A refusal is a tool error
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
mechanism but no Tasks implementation; M141 added one (`api/mcp/tasks.py`). On both the demo and the client
servers, a client that declares `io.modelcontextprotocol/tasks` gets a **task handle** for the long tools in
`mcp.tasks.tools` (`audit_campaign` and `get_proof_pack` by default) and answers it through `tasks/get`,
`tasks/result` and `tasks/cancel` (§8.7). Every other client still gets the **synchronous** answer: the tool runs
on a worker thread and is refused with `MCP_TIMEOUT` after `mcp.call_timeout_seconds` (120 s). The thread cannot
be stopped, so its call (or task) keeps its place in the session's concurrency budget until it ends.

### 8.6 Limits

* The rate limits are counted in the API process; several API processes divide the budget among them.
* The client id is what the client declares about itself (cleaned to letters, digits, `.`, `_`, `-`): it names,
  it does not authenticate. The principal is the authenticated part.
* What is demo data is decided by the use case a run or upload was filed under (or `synthetic`), not by where
  its bytes came from: the product keeps no other provenance for an upload, and the library's derived files
  (the assignment and outcomes files `library.audit` builds) have no recorded hash. Anyone who may upload could
  file client data under `hillstrom-email`. `mcp.workspace: demo` declares the whole deployment a demo one: a
  deployment that holds client data uses `mcp.workspace: clients` instead (§8.7).
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

### 8.7 On client data (M141, DEC-1408)

**In plain words.** A client's own agent can check its work against what Marketing AI measured for that client:
its campaigns, its lists, its Value Proof Packs, what the compliance gate allowed before a list went out. One
deployment can serve several clients; each client's agent sees its own workspace and nothing else, gets every
figure with its evidence label and a receipt it keeps, and can later check that the record behind those receipts
was not rewritten. Still nothing over MCP writes, sends or approves.

**Set it up.** The switch is the same as the demo server's: `agentic.enabled` and `mcp.enabled` in
`configs/agentic/agentic.yaml` (§8.2, §11). The client workspaces are settings, in `configs/agentic/mcp.yaml`:
`mcp.workspace: clients`, and one entry per client under `mcp.workspaces`:

```yaml
mcp:
  workspace: clients
  workspaces:
    - id: acme                  # names the workspace and its ledger chain
      client_id: c_acme_1       # the onboarded client whose runs (and campaigns made from them) it holds
      members: [u-0123456789abcdef, u-fedcba9876543210]   # user ids (GET /admin/users)
    - id: house
      unattributed: true        # the data no client is recorded on: uploads, upload runs, audited campaigns
      members: [u-1111222233334444]
```

A person is a member of one workspace at most, so every bearer token of theirs reaches exactly that workspace;
a signed-in person in none is refused at `/mcp` with `403 MCP_WORKSPACE_DENIED`. At most one workspace claims the
unattributed data (on a deployment serving one client, that client's). A file that breaks any of this is refused
when it loads (`CONFIG_INVALID`).

**What belongs to a workspace** (`api/mcp/workspace.py`). A run: by the client its `run.json` records (every run
made from an onboarded client's dataset records it), or, with no client recorded, the workspace that claims the
unattributed data. A campaign: where all its scoring runs belong; one with no scoring run (an audited outside
campaign, the programme) to the unattributed workspace, since the product records no client on an upload.
Anything else is `MCP_WORKSPACE_DENIED`, worded the same for "not yours" and "not there", never repeating the
id. A request naming another workspace in `_meta` is refused the same way. Small groups are refused or withheld
exactly as on the demo server (§8.4).

**Aggregates only.** No tool reads or returns a row; the audited row-level downloads stay HTTP routes for a
person, and a Viewer's token reads aggregates and cannot propose.

**`check_before_contact`** (read, Viewer; M141): before a planned list is sent, what the pre-send compliance gate
(§9a) did to it - customers the list would contact, sent, withheld and moved to another channel, the decision rows
(one per customer and planned channel) with no consent evidence, withheld per reason code, the rule version, the decision file's sha256 and the gate's own signed ledger entry - read from
`compliance_decisions.json`, never from the row-level file. A list built with the gate off, a run with no list
yet, or a record that is not the one the current list was built with is answered `checked: false` with the
reason. Over MCP the counts are one group: any of them from 1 to 9 withholds them all (§8.4). The plan named it `check_before_send`; it is `check_before_contact` because no tool name may carry a verb
of action (`send`, `approve`, ...: M133's surface test), so no client's model can read it as one that sends.

**Receipts and one chain per workspace** (`engine/agentic/ledger.py`, `engine/agentic/receipts.py`). Each
workspace keeps its own hash-chained ledger (`agentic_workspace_log`, Alembic `0010`): its own `seq` from 1, its
own genesis, its workspace id inside every hash. Every MCP answer carries `_meta` `marketing-ai/receipt`:

```json
{"ledger_chain": "acme", "ledger_seq": 42, "ledger_checkpoint": "<entry_hash>",
 "holdout_salt_fingerprint": "<sha256>" , "holdout_salt_note": null, "evidence_id": "ev_..."}
```

- the evidence label entry the answer was recorded under, and the holdout salt's fingerprint the platform records
(null, with a note, when no persistent holdout has used a salt). A proposal drafted over MCP is decided in the
product on the same chain - the one the ledger says drafted it, not the one the stored proposal file names: a file
edited to name another workspace, or a name no chain can have, is refused as `PROPOSAL_TAMPERED` on the chain that
holds the draft - and the approval receipt (`POST /agentic/proposals/{id}/approve`) carries the same
fields. The client keeps them; at any time:

```bash
python -m engine.agentic.ledger verify --against receipts.json        # a JSON list, or {"receipts": [...]}
python -m engine.agentic.ledger verify --workspace acme               # the chain alone
```

walks the workspace's chain (every hash and signature) and looks every receipt up in it. Whoever holds the
database and the signing key could rewrite history and redo every hash and signature, and plain verification
would pass - but the receipts the client already holds no longer match: `RECEIPT_MISMATCH`, exit 1.

**Tasks for long audits** (`api/mcp/tasks.py`). A client that supports the tasks extension
(`io.modelcontextprotocol/tasks`, declared per request) gets a task handle for `mcp.tasks.tools`
(`audit_campaign`, `get_proof_pack`), polls `tasks/get`, reads `tasks/result` - the same answer a synchronous call
gives - and may `tasks/cancel`. The record lives in the artefact store, so any API process answers for it; it
answers only the session that started it (`MCP_TASK_NOT_FOUND` to any other), is kept `mcp.tasks.ttl_seconds`
after it was made (a record past that is deleted when it is next read, and every new task sweeps the store of
expired ones), fails at `call_timeout_seconds` (`MCP_TIMEOUT`), and holds its place in the session's concurrency budget until
its work ends. Other clients get the synchronous answer, as before. `api/mcp/task_client.py` is the reference
client side.

**Limits.**

* The binding is per person, from the configuration file: a person who works for two clients needs two
  sign-ins. Tokens are the product's sessions; audience-bound OAuth tokens wait for the identity provider.
* An upload records no client, so on a deployment serving several clients an audited outside campaign belongs
  only to the workspace that claims unattributed data. Recording a client on an upload is Phase 2's (raised as a
  cross-branch request).
* The compliance gate's own signed entry (M134) stays on the deployment's chain: the gate runs inside a list
  build, not for an MCP caller. `check_before_contact` names it by evidence id and `entry_hash`.
* A task runs on a worker thread of the API process that accepted it, not on the scheduler's workers; if that
  process stops, the task fails at its deadline. Rate limits are still counted per API process.

Tests: `tests/unit/mcp/test_workspaces.py`, `tests/unit/mcp/test_tasks.py`, `tests/unit/agentic/test_workspace_chains.py`,
`tests/integration/mcp/test_client_workspaces.py` (an onboarded client's measured campaign through the product's
API: its agent gets the Proof Pack's figures exactly; workspaces; a Viewer; tasks; receipts against a rewritten
history; the injection canary; the switch off) and `tests/integration/compliance/test_check_before_contact.py`.

## 9. The campaign planner and the claim verifier (`engine/agentic/planner.py`, `claims.py`; M139)

A warning card on Results ("Draft a plan", `ui/modules/agentic/`; no new top-bar item) or a goal becomes one
drafted campaign plan for a person to approve. A **state machine with one interrupt** (design §6), each
transition written to `agentic_runs/<plan_id>/state.json` under a lock per plan, so a crash resumes from the last
step written (`resume_plan`, `POST /agentic/plans/{plan_id}/resume`). DRAFT is idempotent: the proposal id is
written to the state (`reserved_proposal_id`) before the proposal is drafted, and a resumed DRAFT reuses the stored
proposal, so a crash inside it never leaves two pending proposals for one plan:

| Step | What happens | Model |
|---|---|---|
| FRAME | A card is found by the server among `GET /campaigns/summary`'s cards (the client names it; its content is not taken from the client). `no_control`, `underpowered`, `contamination`, `uplift_not_better_than_risk`, `uplift_fading` frame the next cycle (`restore_holdout`, `larger_test`, `protect_holdout`, `rank_by_risk`, `confirm_effect`); `early_look`, `drift`, `challenger_ready`, `backfire` plan nothing and say why (`PLANNER_NOTHING_TO_PLAN`). The form takes enumerated values and numbers the person types. | Words only: the model picks each field from the values offered (use cases, objectives, channels, and the amounts code read from the words); anything else is `PLANNER_GOAL_UNPARSED`. Words without an AI service: `AI_NOT_CONNECTED`. |
| SIZE | The newest finished scoring run of the use case with no campaign yet (a list goes out once); `check_contactable` (its eligible rows and, from `scoring_summary.json`, its held-back rows); the use case's measured campaigns, newest first by when each was opened (`measure`); the outcome kind of the newest one - an amount (M102, `continuous`) is planned as an amount, with its smallest effect and spread left for the campaign page (points given for an amount: `PLANNER_NO_CANDIDATE`); for a rate, the base rate is the held-back rate of the card's own campaign when it has one, else the newest; `plan_test` on everyone the campaign compares (eligible plus held back, as the product's preview sizes it) at the holdout in force; the test plan: the use case's outcome window, the analysis date (the later of the run's finish and the draft, plus the window), the smallest effect the test can see, rounded up to a tenth of a point. | - |
| CANDIDATES | One per configured channel: contactable rows, at most the run's own treat count, offer spend from `cost_per_contact` (null when unset); `value_segment`; `evaluate_policy` on the model's training run for M137's verdict. A candidate is excluded with a reason: another channel than the goal's, nobody contactable, an uplift list whose verdict is `not_safe`, or a budget the spend exceeds or cannot be checked against. | - |
| SELECT | Rules only: `safe` first, then the most contactable, then the use case's channel order; the first candidate every policy rule allows; when none is, the first, so the refusal is drafted and on the ledger. Nothing left: `PLANNER_NO_CANDIDATE`. | - |
| CHECK, DRAFT | The M132 policy's decisions; `Proposal(kind=campaign_plan)` citing every tool result, with the test plan to register on approval (`test.registered` allows a new campaign carrying its test plan, DEC-1406 (b)). The narrative is stored beside the payload and its digest on the ledger entry; the payload digest never includes prose. | The narrative, as structured claims. |
| INTERRUPT | A person approves at `POST /agentic/proposals/{id}/approve` (the policy checked again). | - |
| EXECUTE | Through the product's own functions, each result written to the state before the next, so a retried approval opens no second campaign: the treat list (`ensure_treat_list`), the campaign (`open_campaign`, the engine half of `POST /campaigns`, its treatment starting at the approval), the test plan (`register_plan`, signed `test_plan.frozen`, its holdout commitment from the one `engine.agentic.ledger.holdout_commitment`), and the use case's enabled `measure` schedule when there is one; `campaign_plan.executed` on the ledger. An approval later than the drafted analysis date minus the window is `PROPOSAL_STALE` (draft again), so a frozen plan never reads its result before the window closes. **Agreed deviation from the spec's "schedule measure":** no measure schedule is created - one needs a saved connection and a table a person chooses, which an approval does not carry - so the hand-off names the enabled schedule or says when and how to measure. | - |

**Claims, not prose** (design §4, `engine/agentic/claims.py`). The model returns
`{"sentences": [{"template", "claim"}]}`; each claim references evidence by `ev_<id>:<path>`. The verifier checks
`comparison` (gt/lt against the difference's 95% interval; a direction the interval does not support is written
"is not distinguishable from"; the opposite direction fails), `label` (equal to the tool's), `trend` (three or more
points, one direction, endpoints apart) and `recommendation` (a proposal the policy allowed). The renderer fills
every slot - arm names, labels, comparison words and every number - from the evidence. The model's own words may
carry no digit, no spelled-out number or multiplier (`ten`, `twice`, `times`, `a third`, `tenfold`, `lakh`), no
direction or outcome word (`up`, `more`, `grow`, `improve`, `surge`), no cause or certainty (`because`, `sure`,
`win`), and must pass the deterministic guardrails. Codes: `CLAIM_DIRECTION`,
`CLAIM_LABEL`, `CLAIM_UNREFERENCED`, `CLAIM_FREE_TEXT`, `CLAIM_MALFORMED`. One failed sentence is dropped; more
than one, or an answer that is not the JSON asked for, is replaced by the rules-only summary, which code writes
from the same evidence and which is also what is shown without an AI service. LLM calls go through the `Meter`
with `GenerativePurpose.AGENT_PLANNER` and `AGENT_NARRATIVE` (`configs/prompts/agent_planner.v1.md`,
`agent_narrative.v1.md`): the narrative under the use case's own `generative.llm` and `generative.budget`, as every
metered route; goal words, before any use case is known, under the deployment's configured AI and the default
budget. An AI service or budget error writes the rules-only summary and is recorded as `narrative_error`, not as a
claim failure; the eval report counts it apart (`narrative_errors`) and totals the Meter's spend (`cost_usd`).

**On the page.** "Draft a plan" is drawn only when `/auth/me` (read once at boot) lists `POST /agentic/plans`,
which it does only while `agentic.enabled` is on: with the switch off the module sends no request.

## 9a. The pre-send compliance gate (`engine/compliance/`; M134, DEC-1405)

Before a treat list leaves the product, every planned contact gets one decision record: may this message go to
this customer on this channel, for this purpose, and if not, why. Off by default: the switch is
`compliance.gate.enabled` in `configs/agentic/agentic.yaml` (§11), the gate's only switch; its settings
(`opt_in_channels`, `dlt_var_max_chars`) are in `configs/compliance/compliance.yaml`, which refuses an `enabled` of
its own (`COMPLIANCE_SWITCH_MISPLACED`) rather than ignore it. Off, a treat list build reads those two files,
nothing else of `engine/compliance` runs, nothing new is written, and every treat list is byte for byte what it
was. The gate does not need `agentic.enabled`.

**When it runs.** On, `engine.decide.treat_list.build_treat_list` calls `engine.compliance.handoff.gate_treat_list`
after it has chosen every customer's treat flag, offer and channel and before it writes anything. Every route and
schedule that builds a treat list goes through that function. The gate applies when a list is **built**: a list
already stored when the switch is turned on was not decided by it, so `ensure_treat_list` and the treat list route
(`GET /runs/{id}/treat_list.csv`, and the treat list files through `/runs/{id}/artefacts/`) build it again, through
the gate, before handing it off (`treat_list_awaits_gate`: the gate on and no `compliance_rule_version` in the stored
summary). A list the gate decided is served as it is; with the gate off, a stored list is served as it is, as before.

**Which action a customer is planned on.** A band's `action_id` (propensity) or `uplift.policy.treat_action_id`
(uplift); on a run that chose the offer per customer, the catalogue action of the offer the customer was given,
**by its arm** - `offer_choice.json` `arms[].action_id`, else the file's `offer_action_id` - never by its label,
which two actions may share. An offer with no catalogue action is planned, as the choice planned it, on the use
case's configured channels.

**Message variants (M145, DEC-1412).** On a run whose message arms are in force (M140, `docs/DECIDE.md` §29) the
gate checks the words each customer will actually receive. The treat list reads the arms first, once
(`engine.decide.variant_arms.variant_rows`), and hands them to the gate (`engine.compliance.variants`):

* `control_template` - the band's catalogue action and its `message_text`, as above;
* `variant_<letter>` - the approved template the arm sends to the customer's group on the channel, read from the
  run's `copy_batch.json` (never written again), checked against **the variant's own** catalogue action (the one
  whose `template_ref` registers those exact words): its `dlt_template_text` on a DLT channel, and every link of
  the subject and text against the whitelist. Each (arm, group, channel) is checked once;
* `no_message` - held back, never contacted, never gated.

A variant customer is planned on their band action's channels, like the band's other customers; a channel their
variant has no words on is closed to them (`COMPLIANCE_VARIANT_NO_COPY`), so they go on the next planned channel
whose words of the same variant pass, or are withheld - never sent another message in place of their arm. Where
M134 lets an action with no `dlt_template_text` pass unchecked, a variant without one (or without a real
`dlt_template_id`) on a DLT channel is not sent (`COMPLIANCE_VARIANT_UNREGISTERED`): fail closed. The catalogue the
messages are tied to is the one the arms were drawn under (`variant_arms.json` `catalogue_sha256`, which registers
the variants' words): registering them necessarily changed the catalogue after the run, so the run's stamp alone
cannot be the reference. The arms stand in for the stamp only when they were drawn over it: the run must have a
`catalogue_stamp.json` and the arms' `run_catalogue_sha256` must be its sha256. A run with no stamp (which M134
fails closed), arms drawn over another stamp, or any other catalogue withholds every row with a catalogue action
(`COMPLIANCE_CATALOGUE_CHANGED`). The order in `build_treat_list` is unchanged - the gate (5c), then the arm columns
(5d) from what the gate left - because the arms are drawn before the list is built and the gate reads them; a
withheld customer shows no arm, and `variant_arm_counts` counts those sent. A campaign opened from the run sets
aside, whole, every arm the gate stopped by its words (any of its customers withheld for a template mismatch, a link
not on the whitelist, no words or no registered template on the channel) or sent to nobody; every other arm keeps
each drawn customer, sent or withheld for their consent (intention to treat): `docs/DECIDE.md` §29.

**What decides, per customer the list would contact and per planned channel** (`engine.compliance.gate.decide`;
the reasons in this order, `allowed` when there is none):

| Reason | Rule |
|---|---|
| `COMPLIANCE_NO_CHANNEL` | the contact has no planned channel at all: nothing to clear it on |
| `COMPLIANCE_CONSENT` | not contactable on the channel, from the run's own `channel_contactability.parquet` (M99/M131: the uploaded consent columns and the consent ledger with the region's consent-basis rules - an inquiry consent past its validity, a legacy consent not registered), joined on every key column; it is read, not computed again |
| `COMPLIANCE_WHATSAPP_NO_OPT_IN` | a channel of `opt_in_channels` (default `[whatsapp]`) and no valid grant **on that channel itself** in the consent ledger: a ledger channel grant on WhatsApp is the opt-in record; an all-channel grant is not |
| `COMPLIANCE_CATALOGUE_CHANGED` | the catalogue is not the one the run ran under (`catalogue_stamp.json`), so no message can be tied to the run: fail closed (with message arms in force, M145: the catalogue the arms were drawn under, `variant_arms.json` `catalogue_sha256`, and only when they were drawn over the run's own stamp) |
| `COMPLIANCE_TEMPLATE_MISMATCH` | on a channel whose region rule requires a DLT template (`<channel>_requires: [dlt_template_id]`), the action's `message_text` is not its `dlt_template_text` with each slot filled by a value of its tag |
| `COMPLIANCE_URL_NOT_WHITELISTED` | a link in the action's `message_text`, on a channel the whitelist covers, is not on it - an `http(s)://` or `www.` link, or a bare host such as `deals-x.in/a` (in the text or filling a `{#var#}` slot) |
| `COMPLIANCE_VARIANT_NO_COPY` | (M145) a customer of a variant arm, on a channel their variant has no approved words on: they cannot get their arm's message there |
| `COMPLIANCE_VARIANT_UNREGISTERED` | (M145) a variant's words with no registered template to check them against on the channel: no catalogue action registers them there, or the region requires a DLT template and the variant's action has no `dlt_template_text` (or no real `dlt_template_id`) |

For a customer of a variant arm, `COMPLIANCE_TEMPLATE_MISMATCH` and `COMPLIANCE_URL_NOT_WHITELISTED` are read of
the variant's own words and its own registered action, not of the band's `message_text` (M145, below).

An undeclared automated call never reaches the gate: the catalogue does not load (`ACTION_A2P_UNDECLARED`, M131).

**The DLT matcher** (`engine/compliance/dlt.py`). Five tags and no others - `{#var#}` (any text up to
`dlt_var_max_chars`, 30 by default), `{#numeric#}`, `{#alphanumeric#}`, `{#url#}`, `{#cbn#}` (a call-back number);
a template with another tag is refused when the catalogue loads (`CATALOGUE_INVALID`). Literal text must match
exactly. The template is compiled into a Thompson automaton and the message read once, so the work is linear in
the message length with no backtracking. A language model is never used: one may only suggest a template, as a
proposal. Catalogue actions carry two new optional fields, `dlt_template_text` and `message_text` (the approved
message as sent, every variable filled); without them the check is not applied and the summary says so.

**The URL whitelist** (`engine/compliance/urls.py`): `configs/compliance/url_whitelist.yaml`
(`url_whitelist.example.yaml` shows the format: `channels`, and `urls` as hosts, `*.` sub-domains or URL
prefixes), else the region file's `url_whitelist` block, else not applied. A link with no scheme - `www.` or a bare
host (dot-separated labels ending in 2 to 24 letters, not part of an e-mail address) - is matched against the host
entries only. Bare-host reading fails closed: text written with no space after a full stop (`Dr.Smith`) reads as a
host, is not listed, and stops the message; `compliance_decisions.json` `messages` shows the count.

**What the list does.** A customer is sent on the first planned channel the gate allows - the one they had, or
the next - and is not treated when it allows none; never `treat = 1` with no channel. `treat_list_summary.json`
gains `compliance_withheld` (customers withheld per reason; a customer counts under each reason that closed one
of their channels), `compliance_withheld_rows`, `compliance_rerouted_rows`, `compliance_rule_version`,
`compliance_decisions_sha256` and `compliance_evidence_id` - all absent with the gate off. On a run that chose
the offer per customer, a customer the gate withheld keeps no offer, net value or runner-up (nothing was done, as
for an offer the choice did not give), and `offer_counts`, `explore_offer_counts` and `explore_cost` count what is
sent.

**What it writes.**

* `compliance_decisions.parquet` (row-level: `configs/privacy.yaml`, `engine/privacy/layout.py`,
  `ROW_LEVEL_ARTEFACTS`; Analyst, audited): the key columns, `channel`, `purpose`, `action_id`, `allowed`,
  `reasons` (codes), `ledger_ref` (`channel_contactability.parquet#sha256=…`, and `;consent_record.seq=<n>` for
  the opt-in grant used; null where the run's file does not cover the customer), `catalogue_ref`
  (`<catalogue sha256>#<action_id>`) and `rule_version`.
* A **signed** `evidence.label` entry on the decision ledger (actor `system:compliance_gate`, tool
  `compliance_gate`) with the file's `artefact_sha256`, `rule_version`, the run and the codes; the ledger is the
  platform database and the signer the data folder's Ed25519 key, as in §6. A ledger that cannot be written stops
  the build. The gate runs whether or not `agentic.enabled` is on: it needs only the ledger, not the routes.
* `compliance_decisions.json`: counts, the `rules` document (`rule_version` is its canonical sha256: the region
  block's digest, the DLT channels and tags, the whitelist or null, the opt-in channels, the run's consent-basis
  rules digest), what was checked per action and channel and why not, the evidence id and the ledger entry.
* With message arms in force (M145), and only then: the decision file gains `variant_arm` (the arm drawn for the
  customer) and a variant row's `action_id` / `catalogue_ref` name the variant's own action (null where the variant
  has no words on the channel); `rules` gains `variant_arms` (the copy batch, the catalogue the arms were drawn
  under, the run's stamp they were drawn over, and every arm template's `copy:` reference and action), so
  `rule_version` covers the words checked; `compliance_decisions.json` gains `variant_messages` (per arm, group and
  channel: what was checked of its words, and why not), `withheld_by_arm` and `withheld_for_words_by_arm`; the
  treat list summary gains `variant_arm_withheld` and `variant_arm_words_withheld`. The signed ledger entry
  carries the file's sha256, that `rule_version` and the codes, as for any list. A stored list with arms that the
  gate decided before it checked the variants' words (no `variant_arm_withheld`) is built again before it is
  handed off, as (m) does for an ungated list.

**Scale.** `decide` is column operations only: 1,000,000 customers x 3 channels in about 5 s on the shared
4-CPU machine, 100,000 in 0.3 s. The whole hand-off `gate_treat_list` - with the opt-in lookup over a SQLite
consent ledger holding WhatsApp grants for a third of the customers, the decision file, its sha256 and the signed
entry - takes about 23 s at 1,000,000 and 1.7 s at 100,000 (`tests/unit/compliance/test_gate_scale.py`, both
slow tests; design budget 60 s). The opt-in lookup reads the ledger per customer who plans an opt-in channel, in
chunks of 500, as M131's classification does, as plain columns rather than row objects. With message arms in force
(M145) the hand-off at 1,000,000 customers on two channels, four arms and twelve variant templates took 5.2 s
against 3.6 s without arms on the same machine (`tests/unit/compliance/test_gate_variants_scale.py`, slow; the
arms' work is integer codes over the customers, and each variant's words are checked once).

**Not modelled.** One approved message per catalogue action outside message arms (a variant's own words are
checked since M145); the template's DLT header (sender id) is not checked; WhatsApp's own template approval is the provider's; a message's variables
are checked as the product holds them, not as a client's tool fills them per customer at send time.

## 10. Milestones

The milestones are in `docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md` §3.2; the README's Plan K table lists the tests of
each one that is done. M131, M135 and M137 were built ahead of the rest of Plan K, under Plan J's decision range.

| Milestone | State | Decision | Where |
|---|---|---|---|
| M130 Plan K set-up | done | DEC-1400 | the decision range, the `PLAN-K` blocks, `PLAN_K_CODES`, the three switches (§11) |
| M131 TRAI Third Amendment, and the measurement purpose | done (built early under Plan J) | DEC-1323 | `docs/DECIDE.md` §26 |
| M132 The agent-safe core | done | DEC-1401 | this document's §3–§7 |
| M133 The verifier over MCP, demo data only | done | DEC-1404 | this document's §8; switch `mcp.enabled` (§11), settings in `configs/agentic/mcp.yaml` |
| M134 The pre-send compliance gate | done | DEC-1405 | this document's §9a; switch `compliance.gate.enabled` (§11), settings in `configs/compliance/compliance.yaml` |
| M135 Prediction covariates (MLRATE / CUPAC) | done (built early under Plan J) | DEC-1324 | `docs/DECIDE.md` §27 |
| M136 Pooling across campaigns | done | DEC-1402 | `engine/measurement/pooling.py` (module docstring); switch `pooling.enabled` in `configs/measurement.yaml` |
| M137 "Safe to roll out": a lower bound in rupees | done (built early under Plan J) | DEC-1325 | `docs/UPLIFT.md` §12 |
| M138 An early harm alarm | done | DEC-1403 | `docs/DECIDE.md` §28; switch `harm_alarm.enabled` in `configs/measurement.yaml` |
| M139 The campaign planner | done | DEC-1406 | this document's §9; behind `agentic.enabled` |
| M140 Message variants as measured arms | done | DEC-1407 | `docs/DECIDE.md` §29; setting `generative.variant_arms` on the use case |
| M141 The verifier on client data | done | DEC-1408 | this document's §8.7; behind `mcp.enabled` (§11), client workspaces in `configs/agentic/mcp.yaml` |
| M142 Setup help for integration sources | done | DEC-1409 | `docs/AGENTS.md` §13; behind `agentic.enabled` |
| M143 Tabular foundation model challenger | done (a licence guard; `foundation` loads no foundation model yet, not benchmarked) | DEC-1410 | `engine/agentic/model_licences.py` (module docstring), `configs/agentic/model_licences.yaml`; `docs/LIBRARY.md` §9 |
| M144 Save-desk card, as a randomised pilot | done (a pilot design; no result claimed) | DEC-1411 | this document's §7a; behind `agentic.enabled` |
| M145 Message variants pass the compliance gate | done | DEC-1412 | this document's §9a ("Message variants"); `docs/DECIDE.md` §29; with `compliance.gate.enabled` and a run's arms in force |

## 11. The switches (`configs/agentic/agentic.yaml`)

Every switch is off by default, and with all three off the product is byte-identical to Plan J's (design §11).
They live in `configs/agentic/agentic.yaml`, read by `engine/agentic/config.py` from the configuration root like every
other `configs/` file, because they are per-deployment settings with no secret in them; a misspelt switch is
refused (`CONFIG_INVALID`), never ignored. `tests/unit/agentic/test_flags.py` pins all three off.

| Switch | Milestone | Off (the default) means |
|---|---|---|
| `agentic.enabled` | M132 | no `/agentic` route, no table created and no file written by the app (§1) |
| `compliance.gate.enabled` | M134 | no pre-send gate: every treat list byte for byte as before (§9a); its settings (opt-in channels, the longest DLT slot value) are in `configs/compliance/compliance.yaml`, which refuses a switch of its own |
| `mcp.enabled` | M133 | no `/mcp` route and the MCP SDK not imported (§8); its settings (workspace, demo use cases, rate limits, call timeout, and M141's client workspaces and tasks, §8.7) are in `configs/agentic/mcp.yaml` |
