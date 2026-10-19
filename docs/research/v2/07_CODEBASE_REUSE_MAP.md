# 07 · Codebase reuse map for v2

*Internal research, 2026-10-19. Branch `claude/funny-curie-ppf5d9`. Source: reading the repo (no web). Effort sizes for a small team: **S** under 1 week, **M** 1-3 weeks, **L** 1-2 months, **XL** over 2 months.*

## 1. Bottom line

v1 is a large, well-tested **batch decision and proof engine**: about 191k lines of Python in `engine/`, 24k in `api/routes/` (44 route modules), 47k lines of plain JavaScript in `ui/`, and 785 test files (227k lines, plus a nightly statistical suite). For v2's **outer loop**, most of what we need already exists in some form: the data agent, AutoML, uplift, offer economics, holdout, measurement, guardrails, a policy engine and an audit ledger. Every one of these is a library we can call.

What v1 lacks is the **real-time spine** that an autonomous agent needs:

- an event store / contact memory
- a per-customer decision loop that runs on every event
- bandits
- send-time, format and fatigue models
- any write or send path
- inbound webhooks

A grep for `bandit`, `send_time`, `fatigue`, `webhook` and `event_store` across `engine/`, `api/`, `ui/` and `configs/` returns **zero** relevant files. v1's docstrings say plainly "Nothing is sent" (`engine/config.py`, `Channel`). v2 is therefore "new spine, old organs". It is not a rewrite.

## 2. Reuse map, component by component

| v2 component | What exists today (path, real capability) | Reuse | Gap | Effort |
|---|---|---|---|---|
| **Strategist agent** | `engine/agentic/planner.py`: a state machine (TRIGGER → FRAME → SIZE → CANDIDATES → SELECT → CHECK → DRAFT → INTERRUPT → EXECUTE) that turns a goal or warning card into one approvable campaign plan. The LLM may only map goal words onto enumerated values and write claim-checked narrative. `policy.py` has 6 default-deny rules (holdout immutable, budget cap, consent per channel, frequency cap, test plan registered, role separation). `ledger.py` is append-only, hash-chained and signed. `claims.py` verifies every sentence against its evidence. | Adapt | Plans **one campaign, one channel, one use case**. It cannot allocate a budget across segments or time, cannot run a multi-week programme, and needs human approval of every campaign. | M |
| **Data Doctor** | `engine/doctor/` (45.7k lines, the biggest package): about 80 registered tools (read / propose / apply), a persisted visit state machine, readiness probes, locale/currency/Indian-ID handling, outcome definition, join/stack of files, and prescriptions that replay identically at train and score time. `engine/intake/` reads messy Excel/CSV/JSON. `engine/agent/recipe.py`, `egress.py` and `grounding.py` handle the recipe engine, the default-deny egress gate and number grounding. | As-is, then extend | Built for "files → one training table". v2 needs mapping to an **event taxonomy** (send, deliver, open, click, reply, purchase, unsubscribe), identity stitching across email/phone/device, and continuous rather than one-off ingestion. | M |
| **Model / AutoML agent** | `engine/stages/`: AutoGluon train (10-30 min budgets in `configs/engine.yaml`), calibration and threshold on validation only (`scorer.py`), SHAP explain, PSI drift, register. Also `engine/registry.py`, `model_gates.py`, `approvals.py` (champion vs challenger with an approver). | As-is for slow models | AutoGluon is too heavy for per-event scoring. Its outputs (churn risk, CLV, propensity) should become **features and priors** for the inner loop, distilled to a LightGBM booster or a lookup table so scoring takes milliseconds. | M |
| **Uplift / causal models** | `engine/uplift/`: S/T/X learners and multi-arm versions over LightGBM or AutoGluon (`learners.py`), Qini/AUUC with bootstrap (`metrics.py`), sleeping-dog segments, top-N in budget (`policy.py`), champion with causal gates, drift of the treated share. | Adapt | **Binary only**: `_validated()` rejects any `y` or `t` that is not 0/1. There is no revenue or profit target and no action space for channel × time × format. | M |
| **Inner-loop decision engine** | `engine/decide/offer_choice.py` has the right maths: net value = uplift × value × margin × horizon − offer cost × p_treated − contact cost. It excludes sleeping dogs, applies min-ROI, and does a greedy budget walk by value per rupee, with "no offer" as an explicit choice and a reason. `arbitrate.py` caps contacts per customer across use cases. `value.py`, `ranking.py` (falls back to risk ranking when uplift does not beat it). | Adapt (keep the maths, change the shape) | Runs as a batch over arrays once per scoring run. v2 needs a per-customer, per-event function `decide(state, now) → {do nothing \| action, channel, time, format, variant}` with a logged propensity. | L |
| **Bandits / exploration** | `engine/holdout/assign.py` has an explore slice (0-10% of eligible customers) with per-row probability recorded for off-policy evaluation. `engine/uplift/ope.py` implements IPS, SNIPS and doubly robust estimates with intervals, plus treat-all and treat-none yardsticks. `decide/variant_arms.py` randomises copy variants A-D as measured arms. | As-is for evaluation | **No bandit policy** (no Thompson sampling, no LinUCB), no online update, and variants are fixed-split A/B tests. OPE is the right foundation for judging bandits safely. | M |
| **Contact memory / event store** | Nothing purpose-built. The nearest pieces: `engine/privacy/consent.py` (a time-aware ledger keyed by hashed principal, purpose, channel and status), `engine/measurement/campaign.py` (campaign record and assignment), `reconcile.py` (reads a send log after the fact), and `engine/onboarding/features.py` (point-in-time DuckDB aggregation over event roles, which **can compute features from an event store**). | New (reuse patterns) | No per-customer timeline of contacts and responses, so no "last contacted", no cooldown and no learned preferences. | L |
| **Send-time optimiser** | Nothing. `engine/scheduling/cron.py` only schedules jobs (defaulting to the Asia/Kolkata timezone). | New | Needs the event store first. Start with a per-customer active-hour histogram plus a population prior. | M |
| **Channel learner** | `decide/contactability.py` (consent and reachability per channel), `catalogue.py` (offer/channel catalogue with costs), `compliance/gate.py` (channels in a fixed order of preference). All rule-based. | Adapt | No learned channel response; the channel is chosen by rules, not by expected value. | M |
| **Fatigue budget** | `agentic/policy.py::frequency_cap` (contacts per customer and per-channel caps on a proposal); `arbitrate.py` `contact_cap_per_customer` per cycle. | Adapt | Caps are counted per cycle with no rolling window and no history, and unsubscribes and complaints are not treated as a cost. | M |
| **Dispatcher + channel connectors** | Nothing sends. `engine/connections/*` (S3, Postgres/Redshift, MySQL, Snowflake, BigQuery, Azure) are **read-only by design** (`SET SESSION TRANSACTION READ ONLY`). The last step before a send already exists: `compliance/handoff.py` writes a signed decision record per contact, and `treat_list.py` exports the list. | New | Provider adapters (WhatsApp BSP, email ESP, SMS), queues, retries, rate limits, idempotency keys, template registration. | L (2-3 providers), XL (many) |
| **Inbound delivery / response webhooks** | None. Outcomes arrive in batches after the window closes: `scheduling/outcomes.py`, `measurement/pull.py`. FastAPI makes routes cheap to add. | New | Signature checks, deduplication, mapping each event back to its decision ID, writing to the event store. | M |
| **Creative Director** | `engine/generative/win_back.py`: one LLM call per band (or segment) × channel, producing a template with `{{placeholders}}` rendered per row without the model, so cost scales with segments, not customers. Up to 4 variants. Also `prompts.py` (versioned prompt files), `budget.py` (cache → budget → tally meter), `cache.py`, and a RAG assistant (`assistant.py`, `retrieval.py`, `rerank.py`). | Adapt | **Text only, 3 channels** (`Channel` = email, sms, whatsapp). No image, video, push or web formats, and no use of the individual's interests beyond placeholders. | L |
| **Brand & Compliance Critic** | `generative/guardrails.py` (deterministic rules first, then LLM judges with thresholds), `compliance/dlt.py` (Indian DLT template matcher), `urls.py` (link whitelist), `optin.py` (WhatsApp opt-in), `variants.py`, `decide/contact_gate.py` (DND, minors, deceased/fraud statuses, test accounts, one per household, suppression list). | As-is, wrapped as an agent | No brand-voice profile and no image checks. | S-M |
| **Econometric Analyst** | `engine/measurement/` (14k lines, our strongest asset): binary and amount incrementality, CUPED and run-score adjustment (`continuous.py`, `run_score.py`), early harm alarm (`harm.py`), empirical-Bayes pooling against the winner's curse (`pooling.py`), complier-adjusted effect (`reconcile.py`), universal-holdout programme readout, power planner, uplift-vs-risk comparison, per-segment effects with false-alarm guard. `pilot/proof.py` (Value Proof Pack) and `roi.py` give ranges in rupees. | As-is | Measures **campaigns with outcome windows**, not an always-on policy. Needs rolling policy value (OPE on logged propensities), sequential readouts, and credit across multi-touch sequences. | M |
| **Conversation agent** | `agent/loop.py` (JSON-action tool loop with grounding and guardrails), `agent/explore.py` ("ask your data"), `agentic/save_desk.py` (one customer's card: reasons, offer, value, "would stay anyway"). | Adapt for marketers | **Customer-facing two-way chat** (WhatsApp replies) does not exist. | L |
| **Goal-based UI** | `ui/` is plain JS with no framework or build step, 19 modules (campaign wizard, overview, datadoctor, measure, agentic). `measurement/overview.py` feeds the Overview page. | Rewrite most pages | The structure is use case → train → score → download list. v2 needs goal → plan → approve envelope → live → briefing. | L |
| **Daily briefing** | `measurement/summary.py` (warning cards, value proven to date), `pilot/document.py` (one report model rendered to HTML and PDF), `pilot/plain.py` (plain-language lint), `configs/pilot/help.yaml` (a plain sentence for every code), `agentic/claims.py`. | Adapt | Needs daily cadence, "what I did and why", and spend vs budget. | S-M |
| **Simulator gym** | `measurement/simulate.py` (campaign populations with known effect, compliance and contamination, seeded), `agentic/evals.py` (pass^k scenarios and a policy mutation test), `doctor/evals.py`, `pilot/demo.py` (synthetic client). | Adapt | Single-shot campaigns only: no customer dynamics over time, channels, timing or fatigue. | M-L |
| **Holdout service** | `engine/holdout/`: salted SHA-256 membership, persistent per use case or universal, salt fingerprint and epoch ledger. | As-is | Make the universal holdout the default for the agent. | S |
| **Scheduler / jobs** | `engine/scheduling/` (cron, local thread, AWS EventBridge; monthly `treat_list → measure → learn` loop in `measurement/cycle.py`), `engine/jobs.py` (`ThreadJobRunner`, in memory). | Adapt | In-memory jobs and an assumption of "one server per data directory" do not survive an always-on agent. | M |
| **LLM layer** | `engine/llm.py` (`LLMClient` protocol, deterministic fake, Bedrock), `llm_http.py` (OpenAI-compatible and Anthropic Messages), `ai_service.py` (separate model slots), `configs/llm_prices.yaml`. | As-is | Add image/video generation providers and native structured tool use. | S |
| **MCP server** | `api/mcp/`: exposes read and propose tools to other companies' agents, with scopes that cannot approve, small-group suppression and rate limits. | As-is | A distribution channel in its own right: other agents can call our decisioning. | S |

## 3. Hard limits v2 must change

1. **Table grain.** Onboarding builds `(entity, snapshot_date)` rows and scoring produces one row per customer per run (`onboarding/snapshots.py`, `stages/score.py`). Keep this for **training**, but add a live per-customer state that is updated on every event.
2. **Binary uplift.** Learners and OPE accept only a 0/1 treatment and outcome. Amounts exist only in measurement (`outcome_kind: continuous`). v2 rewards revenue and profit and has many actions.
3. **Batch run lifecycle.** Everything is a "run" with `status.json` and artefact files, sequenced by a thread pool, at a monthly cadence (`LOOP_KINDS`). v2 needs a long-running service: queue, workers, and minute-level latency.
4. **No write path.** Connections are read-only and the output is `treat_list.csv`. v2 must send, track and retry.
5. **Narrow action space.** v1 decides offer + channel (channel by rules). Time, format, frequency and sequence are absent.
6. **Campaign as the unit of proof.** v2's unit is a policy that runs continuously, so proof must be "the policy vs the universal holdout, to date".
7. **Use-case-centric config.** There are 13 use-case YAMLs, 6 industry packs, and a 4k-line `engine/config.py`. Keep use cases as **templates the Strategist picks**, under a goal.
8. **Approval per campaign.** `INTERRUPT` is the only way forward in the planner. Autonomy means the human approves an **envelope** (budget, caps, channels, brand rules) once, and the ledger records every action inside it.
9. **India defaults.** Rupees, the Asia/Kolkata timezone and DLT are fine for our first market, but they need a locale layer for a general product.
10. **Process weight.** Plan/DEC milestone references and long docstrings slow new work. Put v2 in a new package that imports v1 modules as libraries.

## 4. Tech stack: keep / change

| Keep | Change or add |
|---|---|
| Python 3.11, FastAPI, pydantic frozen contracts | Replace `ThreadJobRunner` with a durable queue and workers (Postgres-backed queue or SQS) |
| pandas + DuckDB (point-in-time features), LightGBM | **Event store**: an append-only, partitioned Postgres table first; a columnar store only if volume demands it |
| AutoGluon, for slow offline models only | **Online state store** (Postgres row per customer; Redis if latency needs it) |
| SQLModel + Postgres, S3 storage, EventBridge | A small in-house bandit module (Thompson / linear), evaluated with the existing `ope.py` |
| `LLMClient` protocol + fakes; Bedrock / OpenAI-compatible / Anthropic | Image (and later video) generation behind the same protocol style |
| MCP server, fpdf2 reports, the test culture and nightly statistical suite | A UI built on a component framework for the goal, live and briefing pages (the current plain-JS UI is large and wizard-shaped) |

## 5. Suggested build order (reuse first)

1. Event store, contact memory and webhooks (L).
2. Dispatcher: one email and one WhatsApp provider, behind the compliance handoff (L).
3. Inner-loop `decide()`: `offer_choice` maths, do-nothing, memory-based caps, propensity logging, Thompson sampling over offer × channel (L).
4. Gym extension (M).
5. Goal/envelope Strategist and daily briefing (M).
6. Send-time, channel and fatigue learners after about one month of events (M each).
7. Image and push formats (L).

## What this means for us

**Do**
- Treat v1 as a library: Data Doctor, measurement, holdout, OPE, `offer_choice` maths, guardrails, compliance gate, ledger, claims verifier and the LLM layer all carry over.
- Build the event store and webhooks first. Every v2 learner (time, channel, fatigue, bandit) depends on them.
- Log a propensity on every decision from day one, so `ope.py` and the measurement stack can prove each policy against the universal holdout.
- Use AutoGluon models as priors and features for a light, millisecond inner loop.
- Change approval from per-campaign to an envelope recorded on the existing ledger.
- Extend `measurement/simulate.py` into a customer-over-time gym before turning autonomy on.

**Don't (or not yet)**
- Don't rewrite Data Doctor or measurement. They are the hardest parts and they are done.
- Don't route per-event decisions through the run/stage pipeline or AutoGluon.
- Don't put an LLM in the inner loop. The planner's rule that the LLM only picks from enumerated values is a good pattern to keep.
- Don't build many channel connectors before one email and one WhatsApp path is proven end to end.
- Don't carry the Plan/DEC milestone process into the v2 package.

**Open questions**
- Do we send through our own provider accounts or through the client's (their ESP or BSP)? This changes the dispatcher and the webhook design.
- What event volume do we design for in year one (10k or 10M events a day)? This decides Postgres-only versus a streaming store.
- Do we keep the plain-JS UI and restyle it, or rebuild the goal, live and briefing pages in a framework?
- How do we extend the uplift learners to amount outcomes and many actions: a direct rewrite, or bandits only for the action choice?
- Should the MCP verifier become a product surface, so other companies' agents call our decide-and-prove tools?

## Sources

Internal files only (no web sources for this topic). Main ones read: `engine/agentic/` (planner, policy, ledger, claims, tools, evals), `engine/doctor/`, `engine/intake/`, `engine/agent/` (loop, recipe, egress, grounding), `engine/stages/`, `engine/uplift/` (learners `_validated`, ope, policy), `engine/decide/` (offer_choice, arbitrate, contactability, contact_gate, variant_arms, treat_list), `engine/holdout/`, `engine/measurement/` (simulate, harm, pooling, continuous, reconcile, cycle, summary), `engine/pilot/` (proof, roi, document), `engine/generative/` (win_back, guardrails, budget), `engine/compliance/`, `engine/privacy/consent.py`, `engine/connections/`, `engine/scheduling/`, `engine/jobs.py`, `engine/config.py`, `engine/llm.py`, `engine/llm_http.py`, `api/mcp/`, `ui/modules/`, `configs/engine.yaml`, `pyproject.toml`.
