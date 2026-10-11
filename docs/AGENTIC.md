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

Everything here is switched off by default (`configs/agentic/agentic.yaml`, `agentic.enabled: false`). Off,
the product is exactly what it was before Plan K: no route, and the app creates no table and writes no file
(`api.main` imports only the switch reader, `engine.agentic.config`). A migrated database is the one
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

## 8. Milestones

| Milestone | State |
|---|---|
| M130 Plan K set-up | pending (the integrator) |
| M131 TRAI rules and the measurement purpose | built early under Plan J (DEC-1323) |
| M132 The agent-safe core | this document's §3–§7 (DEC-1401) |
| M133 The verifier over MCP, demo data | pending |
| M134 Pre-send compliance gate | pending |
| M135 Prediction covariates | built early under Plan J (DEC-1324) |
| M136 Pooling across campaigns | pending |
| M137 Safe to roll out | built early under Plan J (DEC-1325) |
| M138 Harm alarm | pending |
| M139 Campaign planner | pending |
| M140 – M143 | pending |
| M144 The save-desk card | built as a pilot design (§7a, DEC-1411); ships only on a measured result, which needs clustered standard errors first |
