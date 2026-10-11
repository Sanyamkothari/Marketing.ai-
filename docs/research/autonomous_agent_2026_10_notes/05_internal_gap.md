# 05 — Internal gap analysis (Marketing.ai repo, read-only survey, 11 Oct 2026)

## Short version
The product is a decide-and-prove layer by design: agents check and propose, never send (Plan K rule K1, docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md:117-133, 658-672).
BUILT (mostly off by default): deciding (propensity, uplift, offer, channel, budget, net value, treat list, cross-use-case arbitration), generative copy with guardrails, variants as randomised arms, default-deny policy engine, signed hash-chained ledger, MCP verifier, campaign planner state machine with ONE mandatory human interrupt, pre-send compliance gate (TRAI/DLT, consent, WhatsApp opt-in, URL whitelist), scheduled monthly loop (score → treat_list → measure → learn), harm alarm, pooling, champion–challenger with Approver separation.
MISSING: activation/sending (no engine/activation/, no SES/SMS/WhatsApp/CEP writer, no send-log importer), real-time/event-driven anything, bandits/continuous optimisation, autonomous trigger for the planner, kill switch beyond config flags, quiet hours, send-time frequency enforcement. Every path from decision to action ends at a person or a download.
Master switches: configs/agentic/agentic.yaml agentic.enabled/compliance.gate.enabled/mcp.enabled false; configs/measurement.yaml pooling/harm_alarm false; engine/settings.py:332 scheduler_backend default "none".

## Capability table (B built, P partial, D designed only, M missing)
### Deciding
- Propensity scoring + bands + suppression — B — engine/stages/score.py, engine/stages/actions.py — batch scoring; control group held out; champion promotion needs approval when governance.approval_required (default true, engine/config.py:875) — schedule kind `score`.
- Uplift (S/T/X, multi-arm) + targeting policy — B — engine/uplift/learners.py:421-858, policy.py, flow.py — persuadables only, sleeping dogs never chosen, N limited by budget_contacts and value ≥ cost.
- Offer choice — B — engine/decide/offer_choice.py (M100) — best eligible net value ≥ min_roi×cost; greedy budget walk.
- Channel choice / per-channel consent — B — engine/decide/contactability.py (M99, M131 TRAI consent basis); no real catalogue ships.
- Net value — B — engine/decide/value.py — uplift × value × margin × horizon − offer cost × p − contact cost (₹).
- Treat list — B — engine/decide/treat_list.py:380/511 — a DOWNLOAD; nothing sent. Schedule kind `treat_list` (engine/measurement/cycle.py:898).
- Cross-use-case arbitration / contact caps per cycle — B — engine/decide/arbitrate.py (M101) — analyst-triggered POST /decide/arbitrate; NO schedule kind.
- Budget/cost caps — B — uplift policy budgets, governance.max_run_cost_usd (M108), policy rule budget.cap (engine/agentic/policy.py:317).
### Creating content
- Generative drafts — B — engine/generative/win_back.py; prompts configs/prompts/copy_*; approval route api/routes/generative.py:1702 — one LLM call per band×channel, templates rendered with jinja2, no PII in prompts, up to 4 variants; EVERY template needs human approval; on-demand only.
- Brand/claims checks — P — engine/generative/guardrails.py, configs/guardrails.yaml — deterministic rules + LLM judges; brand is only a brand_name string; no brand-voice model/asset library.
- Structured claim verifier — B — engine/agentic/claims.py (M139).
- Variants as measured arms — B opt-in — engine/decide/variant_arms.py (M140), engine/compliance/variants.py (M145) — Bonferroni; winner only when interval beats all.
- Images/creative/landing pages — M.
### Planning and acting
- Campaign planner — B off by default — engine/agentic/planner.py (start_plan :426, resume :453, select :1030, execute :1322); routes api/routes/agentic.py:379-459 — FRAME→SIZE→CANDIDATES→SELECT→CHECK→DRAFT→INTERRUPT→EXECUTE; EXECUTE builds treat list, opens campaign, freezes test plan; NO measure schedule created; only via POST /agentic/plans (Analyst); never from scheduling/cycle.
- Approvals — B — engine/approvals.py (decide_proposal :502) — separation of duties (DEC-862).
- Proposals + default-deny policy — B — engine/agentic/proposals.py, policy.py (rules :258-530: holdout.immutable, budget.cap, consent.channel, frequency.cap, test.registered, role.separation), configs/agentic/policy.yaml — facts from server artefacts; proposals expire 14 days.
- Signed ledger + receipts — B — engine/agentic/ledger.py (append-only, SHA chain, Ed25519/KMS later), receipts.py.
- Tool registry — B — engine/agentic/tools.py, registry.py — 7 read + 1 propose tool; NO write/send kind.
- MCP server — B off — api/mcp/* — read/propose only; workspace scoped; small-group suppression; rate limits; receipts.
- Eval harness — B — engine/agentic/evals.py (pass^k, policy mutation test).
- Data Doctor / Guided setup — B — engine/doctor/diagnose.py, engine/agent/* — LLM can only propose pending settings; default-deny egress.
- Save-desk card — B off — engine/agentic/save_desk.py.
### Running without a person
- Scheduler — B — engine/scheduling/scheduler.py (Null :131, Local :150, EventBridge :277) — cron+TZ, each slot once, catch-up; default backend none.
- Schedule kinds — score, drift_check, retrain, treat_list, measure, learn (engine/scheduling/schedules.py:102-122); firings act as SYSTEM_SCHEDULER which cannot approve (engine/access/roles.py:85).
- Monthly loop — B — engine/measurement/cycle.py (treat_list_step :898, measure_step :947, interim_reading :1177, learn_step :1232) — "The list stays a download: nothing is sent."
- Retraining policy — B — engine/scheduling/retraining.py:231 — manual/on_drift/weekly/monthly; promotion needs Approver.
- Alerts — B — engine/scheduling/alerts.py:77 — 8 kinds incl. harm_alarm; notify only, no automated reaction except drift→retrain.
- Monitoring — B — engine/scheduling/outcomes.py, engine/uplift/drift.py, engine/model_gates.py, engine/measurement/summary.py.
- Connection pulls — B — engine/measurement/pull.py, engine/connections/* (S3, Azure Blob, BigQuery, Snowflake, Postgres, MySQL; read-only) — consent import is an Admin action, NOT a schedule kind.
- Cost guard — B — engine/aws/run_cost.py (M108).
### Activation and sending
- Any sender — M — no SES/Twilio/Gupshup/Kaleyra/Graph API code; UI says "Nothing is sent from here".
- Push to CEP — D — docs/plans/MARKETING_AI_PLAN_J_LAYER.md M94/M97/M100/M113/M114; engine/activation/ and configs/activation/ do not exist.
- Send-log import/reconciliation — P — engine/measurement/reconcile.py (M103) manual contact upload only; SendLogSource design only.
- Activation Contract design (J_LAYER §3.3 :243-272, M94 :557-639, M97 :802-861, M100 :934-992, M113 :1472-1522, M120 :1718-1762): build_contract → activation.parquet + manifest; mai_* fields; holdout rows exported flagged, never dropped; diff_contract deltas; vendor profiles (generic_csv/parquet, moengage_s3, clevertap_list, braze_cdi, netcore_s3, sfmc_sftp_csv); Destination protocol (Fake, LocalDir, S3 via STS role, Webhook HMAC); write-purpose connections, refused while MARKETING_AI_ACTIVATION_ENABLED=false; release rule (approved model by non-trainer, persistent holdout, consent snapshot ≤2 days, legal opinion where required, registered test plan, auth on, within valid_until, write-purpose destination, model beats risk); erasure/withdrawal deltas to every destination; M113 kinds EXPORT, SEND_LOG_PULL, OUTCOMES_PULL, MEASURE, LEARN with auto_deliver gate, "approval stays human", a design-rule test forbids any SEND kind; M120 starter-sender gate is a decision only (SesSender, EumSmsSender, EumWhatsAppSender, BspSender with second approver only if gate passes). None of the activation package is built.
### Safety
- Compliance gate — B off — engine/compliance/{gate,handoff,dlt,optin,urls,variants}.py — inside build_treat_list, per customer×channel, reroute or withhold, decisions signed on the ledger, fails closed.
- Consent/erasure/retention — B — engine/privacy/*.
- Harm alarm — B off — engine/measurement/harm.py (mSPRT) — "Nothing is paused or stopped: a person decides."
- Frequency/rate limits — P — recently_contacted from a client column (configs/engine.yaml:145, 14 days); per-cycle arbitration cap; policy frequency.cap (:428); no product-owned contact history, no per-day throttle.
- Quiet hours — M.
- Kill switch — P — config flags only; no global halt API; no auto-pause on harm.
- Audit log — B — engine/audit/* (append-only DB triggers; S3 Object Lock export).
- Roles — B — engine/access/roles.py (Viewer, Analyst, Approver, Admin); proposer/trainer cannot approve; SYSTEM_SCHEDULER cannot approve.
- LLM egress — B — engine/agent/egress.py, api/mcp/gate.py, suppress.py.
### Learning loop
- Retraining, champion–challenger (engine/registry.py, engine/uplift/champion.py, engine/model_gates.py), learn from last campaign (engine/measurement/learn.py M106: randomised frame from holdout+explore only), OPE (engine/uplift/ope.py IPS/SNIPS/DR), pooling (engine/measurement/pooling.py), persistent holdout + 0–10% explore with logged propensities (engine/holdout/*), CUPED/safe-to-roll-out (engine/measurement/continuous.py, run_score.py, compare.py) — all B.
- Bandits/continuous experimentation — M, excluded on purpose (Plan K §1.3).
### Real-time
- Event ingestion/streaming — M. Low-latency scoring — M (save-desk is a lookup of a stored list). Triggered journeys — M.

## 10 biggest gaps for "autonomous within guardrails" (most important first)
1. No activation/sending path (design exists in J_LAYER; K1 and M113's no-SEND test forbid it today — a product decision too).
2. No unattended EXECUTE: needs a human-signed standing mandate (spend, audience size, channels) under which proposals auto-execute, still signed on the ledger; policy engine + ledger are the substrate.
3. Planner never triggered autonomously; needs a scheduled/alert-driven "draft a plan" kind and EXECUTE creating the measure schedule.
4. Send-log ingestion/reconciliation not automated (SendLogSource, SEND_LOG_PULL).
5. No runtime kill switch / circuit breakers: global halt API checked by every firing and delivery; auto-pause on harm_alarm, performance_drop, cost cap, compliance-withheld spikes; alert de-dup.
6. No send-time frequency/fatigue/quiet-hours enforcement; no product-owned contact history.
7. No real-time/event layer (low-latency scoring, event-triggered decisions).
8. Content not autonomous; thin brand control (no brand-voice spec, approved-claims library, creative generation, scheduled variant refresh).
9. Decision steps outside the scheduled loop: arbitration, consent import, variant-arm drawing, copy generation, test-plan templates.
10. Continuous optimisation limited to monthly champion–challenger; no adaptive allocation; explore + OPE give the logging needed for safe policy improvement. Safety features all off by default and validated mostly on synthetic/demo data; no pilot client.

Strongest foundations: default-deny policy + signed ledger + receipts; compliance gate in the treat list; slot-safe scheduler with SYSTEM_SCHEDULER unable to approve; persistent holdout + explore + measure/learn/harm statistics; fully specified but unbuilt Activation Contract and release rule.

## DEEP PASS ADDITIONS (supersede where they conflict)
Cross-cutting facts:
- A2: with sign-in off every request is LOCAL_OPERATOR holding every role; separation_refusal (engine/approvals.py:216-223) returns None unless principal.kind=="user" → separation of duties NOT enforced with sign-in off (model approval, proposal approval, role.separation rule policy.py:517). SYSTEM_SCHEDULER holds only Analyst.
- A4: NO model-approved check on the list path: resolve_model_version (engine/stages/score.py ~333-353) honours any named version whatever its status; scheduled score can name parameters.model_version_id (firing.py:1102-1115); treat_list_step, planner _plan_run (planner.py:724-752), ensure_treat_list never check; no policy rule. (Plan M final integration is adding a server check on POST /campaigns/{id}/plan and treat-list download.)
- A5: with governance.approval_required false, register stage (engine/stages/register.py:259-279) promotes a winning challenger with no person; learn step forces approval_required True (cycle.py:1363).
- Planner EXECUTE (planner.py:1322-1420) builds the run's WHOLE treat list, not restricted to the selected channel or planned_contacts → policy checks apply to the payload, not the produced list. Does not create the measure schedule.
- treat_list_step opens campaigns with no registered test plan and treatment_start=None.
- Compliance gate fails OPEN in places (handoff.py:249-310): no catalogue → no message checks; action without message_text/dlt_template_text "not checked" but allowed; no whitelist → links unchecked. Only variant arms fail closed.
- Copy approval: Approver needed but NO separation of duties and NOT on the ledger; with sign-in off approver name is free text.
- frequency.cap only checks payload contacts_per_customer, hard-coded to 1 by the planner; no history-based fatigue.
- Missing policy rules: model approved, quiet hours, spend cap over time, consent freshness.
- MCP auth is product session bearer, not OAuth 2.1/RFC 8707; planner not exposed as MCP tool; KMS signer not built; copy_arms proposal kind not built; M146 policy search with offline certifier and M148 evidence bundles not built; M147 schema matching partial (92.3% top-3 < 95% target).
- J_LAYER §7 "do not build": any sending; journeys/waits/triggers/frequency-cap engines; CDP/reverse ETL; vendor push APIs or holding CEP credentials; real-time decisioning; per-user RL; automatic model promotion as default.
Revised top gaps: 1 activation path; 2 unattended approval (standing human-signed mandate + narrowly scoped system approver on the ledger) + autonomous triggers; 3 model-approved check on the list path; 4 separation of duties off with sign-in off, copy approvals off-ledger → autonomous mode must require sign-in; 5 executed list ≠ checked payload, no test plan from treat_list_step; 6 no circuit breakers/kill switch; 7 compliance gate off by default and fails open, no quiet hours/fatigue; 8 consent freshness and send-log feedback not automated; 9 content not unattended, thin brand; 10 no real-time, no continuous optimisation.
