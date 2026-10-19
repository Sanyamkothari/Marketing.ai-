# 04 · Multi-agent architecture for v2

Research date: 2026-10-19. Vendor claims are marked as claims. Anything I could not confirm from a primary source is marked "(unverified)".

## 1. The short answer

The founder's split is right, and the market has already settled on it. Every serious vendor separates two things. **LLM agents** work with the marketer: they plan, write, explain and answer replies. A **decision engine** works on each customer: it uses bandits, reinforcement learning (RL) or uplift models, not an LLM. Aampe's "one agent per user" is Thompson sampling plus per-user causal inference, not a language model. Hightouch AI Decisioning and Braze Decisioning Studio are RL. Salesforce, HubSpot, Klaviyo and Adobe put their LLM agents on the planning and content side.

What we should change: fewer free-roaming agents, more deterministic workflow. Agents should hand off typed artefacts on shared state, never chat transcripts. Every number must come from code.

## 2. What the builders learned (2025-2026)

**Anthropic, "Building effective agents" (Dec 2024).** It separates *workflows* (code paths you define in advance) from *agents* (the LLM directs its own steps). Its advice is to start with the simplest thing that works. Useful patterns: prompt chaining, routing, parallelisation, orchestrator-workers, and evaluator-optimizer (one model generates, another critiques against clear criteria).

**Anthropic, multi-agent research system (Jun 2025).** An Opus 4 lead with Sonnet 4 subagents beat a single Opus 4 agent by **90.2%** on their research eval. The cost is high: agents use about **4x** the tokens of chat, and multi-agent systems about **15x**. Token use alone explained **80%** of performance variance on BrowseComp. The pattern fits broad, parallel, read-heavy work. It fits poorly where agents share a lot of context or depend on each other. Failures they saw: spawning 50 subagents for simple queries, searching endlessly for sources that do not exist, and subagents duplicating each other's work. Their fixes: save the plan to external memory, have subagents write outputs to storage and pass back *references*, and use rainbow deploys so running agents are not broken mid-task.

**Cognition (Devin), "Don't build multi-agents" (Jun 2025).** Splitting a task across agents is a game of telephone. Parallel workers make conflicting decisions. Their default is one agent on a single thread with full context. In the April 2026 follow-up, "Multi-agents: what's actually working", they say what works is several agents *contributing intelligence while writes stay single-threaded*. Examples are read-only search subagents, a separate reviewer agent (reported to catch about 2 bugs per Devin PR, from a ZenML summary, unverified), and "smart friend" escalation to a bigger model. Swarms of parallel writers still do not work.

**Berkeley MAST study ("Why do multi-agent LLM systems fail?", NeurIPS 2025).** It found 14 failure modes across 1,642 traces from 7 frameworks. Most failures come from **system design (44%)** and inter-agent misalignment, not from model weakness. Gains over a single agent were often small.

**Gartner (Jun 2025).** It predicts more than 40% of agentic AI projects will be cancelled by end of 2027, because of cost, unclear value and weak controls. It also says only about 130 of thousands of "agentic" vendors are real.

**Hallucinated numbers.** An LLM querying raw tables invents joins, columns and metric definitions, so its answers vary from run to run. The 2026 fix is a governed metric layer (semantic layer) that agents call as a tool (dbt and Cube benchmarks, arXiv "GROUND" 2026). For us, that layer is the v1 measurement code.

**Frameworks.**

| Option | Good at | Weak at |
|---|---|---|
| OpenAI Agents SDK | Light handoffs, guardrails, built-in tracing | No checkpoint or resume of the LangGraph kind |
| LangGraph | Durable graphs, checkpoints after every node, `interrupt` for human approval | Heavier; you buy into its graph model |
| Claude Agent SDK | Agent loop, subagents with isolated context, hooks (PreToolUse etc.), skills, `maxTurns` and `maxBudgetUsd` caps | Subagents inherit the parent's permission mode |
| MCP | Standard tool and data interface, under the Linux Foundation's Agentic AI Foundation since Dec 2025, used by ChatGPT, Claude, Gemini and Copilot | A protocol, not an orchestrator |

v1 already contains most of what a framework would give us. `engine/agentic/` has a planner state machine with one human interrupt, a default-deny policy engine, proposals, a hash-chained decision ledger, a claim verifier for narratives, and pass^k scenario evals. **We should extend this, not adopt LangGraph wholesale.** We can call models through the Claude Agent SDK or the raw API.

## 3. How marketing vendors define their agents

| Vendor | Agents (status) | What they really do |
|---|---|---|
| Salesforce Agentforce Marketing | Marketing Goals Agent (pilot): the marketer sets goal, budget, guardrails and approval rules, and the agent builds and tunes campaigns. Content Agent (pilot). Agentic Segmentation (GA). Campaigns in Slack (GA). Hunter and Piper for prospecting and SDR work. | Goal to campaign. Closest to our Strategist. Priced at about $0.10 per action (Flex Credits) or $2 per conversation. |
| HubSpot Breeze | Customer, Prospecting, Data and Content agents (GA), plus many in beta (Customer Health, Social, Cross-sell...) | Data Agent cleans the CRM "before other agents inherit it". That is our Data Doctor. |
| Klaviyo K:AI | Marketing Agent (builds campaigns, flows and forms from a URL, human reviews). Customer Agent (support with escalation rules; "65% resolved" is a vendor claim). Composer (Mar 2026). | Campaign builder plus a reply bot |
| Braze | Operator (copilot), Agent Console (GA Apr 2026: content generated at send time), Decisioning Studio (RL against a chosen KPI), Agentic Standards (pre-send review of campaign setup), Operator Connect (MCP-style access from ChatGPT and Claude) | Copilot, then critic, then RL engine |
| Adobe | Agent Orchestrator: Audience, Journey, Data Insights and Experimentation agents. "CX Enterprise Coworker" (Apr 2026, unverified). | Merkle says fully autonomous execution is "still roadmap" |
| Aampe (acquired by MoEngage, Jun 2026) | One "agent" per user | Thompson sampling, bandits and per-user causal inference over *labelled message parts* (offer, value prop, tone) |
| Hightouch | AI Decisioning | RL chooses message, offer, channel, timing and frequency, including whether to send. Marketer-defined reward. Claims 10-20% lift (vendor). |

**What buyers trust.** StackAdapt's "AI Delegation Gap" survey (Aug 2026, n=500) found **90%** of marketers comfortable with AI recommending, **89%** with AI preparing actions for approval, **78%** with AI acting inside rules they set, and only **50%** with full autonomy. The top blockers were brand risk (63%), data quality (56%) and transparency (36%). So "autonomous" has to mean *acting inside rules a human approved, with an explanation for every action*.

## 4. Recommended agent roster

The orchestrator is **not an agent**. It is a durable state machine (`goal → brief → content → approval → deploy → monitor → report`) with explicit interrupts. This extends v1's `planner.py`.

| Agent | Verdict | Job | Model tier |
|---|---|---|---|
| **Strategist** | Keep | Turns "win back churners, budget 1 lakh" into a typed **Brief**: objective and reward (e.g. 90-day profit), eligible audience, arms (offers, channels, formats), constraints (frequency caps, quiet hours), budget pacing, stop rules. Picks only from values the tools enumerate. | Frontier |
| **Data Doctor** | Keep (exists) | Onboarding, schema mapping, drift and quality alarms. Event-triggered, never in the send path. | Mid |
| **Creative Director** | Keep, reshaped | Builds a **content library**, not one-off messages: variants across value prop, tone, format and length, each *labelled* with semantic tags so bandits learn over meanings and transfer between campaigns (the Aampe lesson). Personalisation is slot filling from structured fields at send time, not an LLM call per customer. | Mid/frontier, plus image and video tools |
| **Brand & Compliance Critic** | Keep, as a **gate** not a peer | Evaluator-optimizer with Creative. It has its *own* context (it does not see the Creative's reasoning, the Cognition reviewer pattern). Rubric: brand book, claims, offer terms, channel rules. Deterministic checks run first (links, opt-out text, character limits, template approval). | Mid |
| **Econometric Analyst** + **Opportunity Scout** | **Merge** into one **Analyst** | Read-only. Looking backward, it writes ROI vs holdout reports. Looking forward, a scheduled scan proposes new goals ("high-value repeat buyers lapsing 2x faster this month"). All numbers come from measurement tools; text goes through v1's claim verifier. | Frontier for reports, small for scans |
| **Conversation agent** | **Add** | Handles inbound replies (WhatsApp makes these unavoidable): classify intent, answer from business memory, handle opt-outs and "not now", and hand off to a human. Writes structured preference facts back to customer memory. This is the only LLM in a real-time path. | Small/mid |
| Model / AutoML agent | **Don't** make it an LLM agent | AutoGluon training, retraining and drift checks stay a pipeline. The Analyst or Strategist can *call* it as a tool. | n/a |

Six LLM roles, and only one of them (Conversation) touches customers live.

## 5. What must NOT be an LLM: the inner loop

Per-customer, per-moment decisions are *who, when, which offer, channel, format and frequency, or do nothing*. These belong in code: uplift scores, bandit posteriors (Thompson sampling per arm and segment), a send-time model, a fatigue and contact-memory model, and expected-net-value arithmetic under the Brief's constraints. Four reasons:

1. **Cost.** 1M customers × one daily decision × about 2k input and 200 output tokens on a $1/$5-per-million-token small model ≈ **$3,000 a day**. A bandit costs fractions of a cent. (Estimate; prices are mid-2026 list prices, so check live pricing.)
2. **Learning.** Bandits update posteriors from outcomes. LLMs do not learn from your holdout.
3. **Causality and audit.** We need logged propensities to measure incrementality off-policy. An LLM cannot give a propensity.
4. **Determinism.** The same state gives the same action, and pass^k tests are meaningful.

LLMs feed the inner loop only *before* it runs (Brief, labelled content library, constraints) and read its logs *after* (reports).

## 6. How agents hand off work

- **Blackboard of typed artefacts.** Brief, ContentLibrary, PolicyConfig, Proposal, Evidence, Report. Each is versioned, has a single writer, and is read by id. No agent reads another agent's transcript. This applies both Anthropic's "pass references, not content" and Cognition's "single-threaded writes".
- **Every number is a tool result** with an evidence id on the ledger (v1 already works this way).
- **Proposals, not actions.** Agents draft. The policy engine checks. A human or a pre-approved rule executes.
- **Hard caps per run.** Max turns, max dollars and max subagents. This answers Anthropic's "50 subagents" failure and Gartner's cost concern.

## 7. Memory

| Tier | Contents | Who reads it |
|---|---|---|
| Per-customer state | Features, contact history, bandit posteriors, consents, plus *structured* preference facts with validity dates (e.g. `channel_pref=whatsapp`, `no_promos_until=2026-12-01`), stored the Zep temporal-fact way, not as a vector chat log | Inner loop (structured fields only) and Conversation agent |
| Business memory | Brand book, offer catalogue, margins, approved claims, and a **learnings ledger** ("15% off beat free shipping for lapsed buyers, CI +2.1 to +4.0 pts") | All agents. This is the moat: it compounds. |
| Working memory | Plan file and scratch per run | That run only |

Memory products (Mem0, Zep, Letta) disagree with each other on benchmarks. Build per-customer memory on our own tables and evaluate it ourselves.

## 8. Human-in-the-loop and evaluation

**Autonomy dial per action type.** The defaults should follow the survey above:

- Approve the *Brief + content library + budget* once.
- The inner loop then acts inside those rules.
- Escalate to an **approval inbox** (in the app, plus Slack and email) on: new content, budget or audience changes, anomalies (complaint or unsubscribe spikes, results breaching stop rules), and conversations the agent cannot resolve.

Gate on *action risk and reversibility*, not on LLM self-reported confidence, which is poorly calibrated. Aim for a reviewer load of about 10-15% of items (vendor guidance).

**Evals.**

- Per-agent scenario sets of 20-50 realistic goals, as Anthropic did with about 20 queries.
- Grade *outcomes* (is the Brief valid? does the copy pass the rubric?) rather than exact tool sequences.
- LLM judges calibrated against human labels. Read transcripts.
- pass^k for consistency (v1 runs k=10).
- Online metrics: proposal acceptance rate, edit distance on drafts, time to approve, critic catch rate, and above all **incremental profit vs holdout**, which grades the whole system.

**Cost per run (estimate).**

| Item | Estimated LLM cost |
|---|---|
| Campaign setup: Strategist ~60k tokens, Creative + Critic ~300k, Analyst report ~50k | Roughly $2-10 |
| One reply conversation on a small model | About $0.01-0.05 |

For comparison, Agentforce charges $2 per conversation and a 1 lakh budget is about $1,200. LLM cost is a rounding error *if* LLMs stay out of the per-customer loop.

## What this means for us

**Do**
- Keep the outer/inner split. Make the inner loop pure maths with logged propensities.
- Build the orchestrator as a durable state machine with interrupts by extending v1's `planner`, `policy`, `proposals`, `ledger`, `claims` and `evals`.
- Ship this roster: Strategist, Data Doctor, Creative Director, a Critic gate, a merged Analyst (econometrics + opportunity scouting), and a new Conversation agent.
- Make the Creative output a *labelled* content library so bandits learn over value props, tones and formats.
- Hand off through typed, single-writer artefacts. Every number is a tool result.
- Use "approve the policy, not every message" as the default autonomy, with an approval inbox and anomaly escalation.
- Expose our tools over MCP so marketers can drive the product from Claude, ChatGPT or Slack (Braze Operator Connect is the precedent).
- Treat the learnings ledger and per-customer memory as the long-term moat.

**Don't (or not yet)**
- Don't call an LLM per customer per decision.
- Don't run parallel writer agents or let agents chat freely with each other.
- Don't let LLMs compute or restate numbers without verification.
- Don't make AutoML an LLM agent.
- Don't gate on model confidence scores.
- Don't adopt LangGraph or CrewAI wholesale when v1 already has the core pieces.
- Don't sell "fully autonomous" on day one. Only 50% of marketers accept it.

**Open questions**
- Is a Conversation agent in v2.0 scope, or v2.1? WhatsApp replies arrive the moment we send. Who owns the human handoff on the customer's side?
- Bandits vs full RL for sequences and follow-ups: is a contextual bandit with sequence templates enough at first?
- Per-segment generation vs per-customer generation at send time (the Braze Agent Console approach): when does the lift justify the cost?
- Which model tiers and providers should we use, and do we let customers bring their own LLM key?
- What should the approval inbox look like, and where should it live (app, Slack, WhatsApp to the owner)?
- How should the Strategist pick the reward (profit vs revenue vs retention) when the owner's goal is vague?

## Sources
- https://www.anthropic.com/research/building-effective-agents
- https://www.anthropic.com/engineering/multi-agent-research-system
- https://anthropic.com/engineering/demystifying-evals-for-ai-agents
- https://cognition.ai/blog/dont-build-multi-agents
- https://cognition.com/blog/multi-agents-working
- https://www.zenml.io/llmops-database/multi-agent-systems-in-production-code-generation-and-review-at-scale
- https://arxiv.org/abs/2503.13657 (MAST)
- https://www.outlookbusiness.com/deeptech/artificial-intelligence/over-40-of-agentic-ai-projects-will-be-scrapped-by-2027-says-gartner
- https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026
- https://arxiv.org/pdf/2608.26157 (GROUND)
- https://www.fast.io/resources/openai-agents-sdk-vs-langgraph.md
- https://inference.net/content/claude-agent-sdk-production-guide/
- https://www.flowhunt.io/blog/agentic-ai-foundation-a2a-mcp-standards/
- https://newsbytes.ph/2026/06/08/salesforce-expands-agentforce-with-ai-tools-for-marketing-teams/
- https://boldorange.com/news/salesforce-connections-2026-rise-of-the-marketing-maker/
- https://www.eesel.ai/blog/agentforce-pricing
- https://www.grazitti.com/blog/the-complete-hubspot-breeze-agent-guide-for-gtm-teams/
- https://investors.klaviyo.com/news/news-details/2026/Klaviyo-Expands-AI-Agents-to-Power-the-Autonomous-B2C-CRM/default.aspx
- https://www.mavlers.com/blog/braze-ai-operator-vs-agent-console-vs-decisioning-studio/
- https://itbrief.co.uk/story/braze-launches-ai-tools-for-marketers-with-europe-hosting
- https://experienceleague.adobe.com/en/docs/cx-enterprise-ai/experience-cloud-ai/agents/agent-orchestrator
- https://www.merkle.com/en/merkle-now/articles-blogs/2026/agent-orchestration-adobe-summit.html
- https://docs.aampe.com/docs/core-concepts
- https://www.moengage.com/in-the-news/moengage-acquires-aampe-to-bring-11-agentic-decisioning-to-b2c-marketing-teams/
- https://www.digitalapplied.com/blog/moengage-aampe-per-customer-ai-agents-martech-2026
- https://hightouch.com/es/platform/ai-decisioning
- https://amplifypartners.com/barrchives/how-hightouch-builds-rl-reinforcement-learning-agents-for-marketing-teams
- https://www.advanced-television.com/2026/08/20/report-reveals-marketers-ai-trust-issues/ (StackAdapt survey)
- https://zylos.ai/research/2026-01-30-ai-agent-human-handoff
- https://galileo.ai/blog/human-in-the-loop-agent-oversight
- https://eesel.ai/blog/ai-chat-escalation
- https://atlan.com/know/best-ai-agent-memory-frameworks-2026/
- https://mem0.ai/blog/state-of-ai-agent-memory-2026
- https://www.morphllm.com/anthropic-claude-api-pricing
