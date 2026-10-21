# 01 · Agent engineering: what the best writing says, and what it means for Autonomy

Research date: 21 Oct 2026. Scope: how to build reliable LLM agents and long-running agent systems,
read against Autonomy v2 (`docs/v2/ARCHITECTURE.md`, `docs/v2/REAL_DATA_RESULTS.md`) and the way we
build it (builder + skeptical reviewer + finisher per task, worktrees, CI as the judge).

Not repeated here: Anthropic's two harness posts (planner/generator/evaluator, sprint contracts,
feature-list JSON, progress file, init.sh). We already use them.

**How this was read.** Anthropic and Claude docs pages were read in full. This sandbox could not reach
cognition.ai, manus.im, openai.com, hamel.dev or eugeneyan.com. For Cognition, Manus and OpenAI, the
quotes come from search excerpts and secondary write-ups; they are marked *(secondary)*. For Hamel
Husain we read his own published eval skills on GitHub, which encode his LLM-judge method step by step.

Each section: 3-5 takeaways, then **(a) how we build** and **(b) the product's agents**.

---

## 1. Anthropic, "Building effective agents" (Dec 2024)

[anthropic.com/research/building-effective-agents](https://www.anthropic.com/research/building-effective-agents)

- Workflows ("predefined code paths") and agents (the LLM steers itself) are different things. Find
  "the simplest solution possible" and add complexity only when it "demonstrably improves outcomes".
- Five patterns: prompt chaining *with programmatic gates*, routing, parallelisation (sectioning or
  voting), orchestrator-workers, evaluator-optimizer. The last works only with "clear evaluation criteria".
- Invest in the agent-computer interface (ACI) as much as in a UI. "Poka-yoke" the tools: change the
  arguments so mistakes are hard to make (absolute paths removed a whole class of errors).
- Frameworks hide prompts; "incorrect assumptions about what's under the hood" cause most customer errors.

**(a) Build.** Our build pipeline is orchestrator-workers plus evaluator-optimizer, and that is the
right call because CI is a clear criterion. Keep the pipeline a script, not a chatty swarm.

**(b) Product.** Our outer loop is a *workflow*, not an agent, and should stay one: Strategist →
Creative ⇄ Critic → approval is prompt chaining with gates. The Critic loop is an evaluator-optimizer;
it is only as good as its criteria (see §5 and §10). Poka-yoke the Strategist: it already picks only
enumerated values. Extend that idea to the brand kit (enumerated tone and banned-claim lists, not prose).

## 2. Anthropic, "Writing effective tools for agents" (Sep 2025)

[anthropic.com/engineering/writing-tools-for-agents](https://www.anthropic.com/engineering/writing-tools-for-agents)

- "More tools don't always lead to better outcomes." Build a few tools around whole workflows; do not
  wrap every API endpoint.
- "Return only high signal information": natural-language names over opaque ids; pagination,
  filtering and a `response_format` (concise / detailed) enum.
- Errors should give "specific and actionable improvements", not codes or tracebacks.
- Evaluate tools with realistic multi-step tasks, then "concatenate the transcripts" and let Claude
  rewrite the tool descriptions.

**(a) Build.** Our `make v2-check` and test output are the builder's main "tool". Make failures
actionable (the failing contract, the file, the fix) and short; a 400-line pytest dump wastes the
builder's context.

**(b) Product.** Two concrete uses. (1) The `/v2` API will be driven by outside agents (Braze
already ships MCP-style access). Design an MCP surface of ~5 workflow tools
(`explain_decision(customer)`, `propose_goal(text)`, `proof_summary(goal)`, ...) that return reasons
in words, not a CRUD mirror of the OpenAPI file. (2) The Analyst should get a governed metrics tool
("profit vs holdout for goal X, last 7 days") rather than tables; that is how the 10x value
over-estimate stays out of prose.

## 3. Anthropic, "Effective context engineering for AI agents" (Sep 2025)

[anthropic.com/engineering/effective-context-engineering-for-ai-agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

- Context rot: recall falls as tokens grow; "every new token introduced depletes this budget".
- System prompts at "the right altitude": not brittle if-else rules, not vague slogans.
- Few-shot: "examples are the 'pictures' worth a thousand words". Use a few diverse canonical
  examples instead of a laundry list of edge cases.
- Long tasks: compaction, structured notes "persisted to memory outside of the context window", and
  sub-agents with "clean context windows" that return condensed summaries.
- Prefer just-in-time retrieval (keep references, load on demand) for open-ended agents.

**(a) Build.** Builders should read `CONTRACTS.md` and their stream's files, not the whole repo.
Reviewers get the diff and the task contract only. Progress lives in files, not in the chat.

**(b) Product.** Our agents are short and bounded, so *pre-fetch* beats just-in-time here (see
disagreements). Give the Creative the brief, the brand kit and 3-5 approved example messages chosen
for diversity, not a 60-rule prompt. Rules that can be code (length, slots, links, numbers) stay in
code, as they are now.

## 4. Anthropic, "How we built our multi-agent research system" (Jun 2025)

[anthropic.com/engineering/multi-agent-research-system](https://www.anthropic.com/engineering/multi-agent-research-system)

- Lead + subagents beat a single agent by 90.2% on breadth-first research, at about "15× more tokens
  than chats". Token spend explained 80% of the variance.
- Not a good fit when agents must "share the same context or involve many dependencies between agents".
- "Without detailed task descriptions, agents duplicate work." Give every subagent an objective, an
  output format, tool guidance and boundaries; scale effort to the task.
- "End-state evaluation rather than turn-by-turn"; start evals small; keep humans testing.
- Production: errors compound in stateful agents, so resume from checkpoints, deploy gradually
  ("rainbow deployments"), and add "full production tracing". "Minor changes cascade into large
  behavioral changes."

**(a) Build.** Parallel streams work for us because Wave 0 froze contracts, which removes the
"many dependencies" problem. Where a task touches two streams' contracts, don't parallelise it.
Task briefs to builders must name the objective, files owned, done-when and out-of-scope;
otherwise parallel builders duplicate helpers or leave gaps.

**(b) Product.** Do not make the outer loop multi-agent research. The one place breadth-first fan-out
fits is the Analyst's opportunity scout (several independent segment scans, merged by code). Trace
every LLM call with goal id, prompt version, model id and verdict, and roll prompt changes out per
client (Shadow first), because small prompt changes cascade.

## 5. Anthropic, "Demystifying evals for AI agents" (Jan 2026)

[anthropic.com/engineering/demystifying-evals-for-ai-agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

- Combine code, model and human graders. "Grade what the agent produced, not the path it took."
- pass@k for "one success is enough"; pass^k when consistency matters, as with customer-facing agents.
- Capability evals start at "a low pass rate"; regression evals run near 100%. "20-50 simple tasks
  drawn from real failures is a great start." Balance the set: test when a behaviour should *and
  should not* happen.
- Model graders: rubric per dimension, "an isolated LLM-as-judge" per dimension, allow "Unknown",
  calibrate with human experts. "A good task is one where two domain experts would independently reach
  the same pass/fail verdict."
- Swiss cheese: evals, production monitoring, A/B tests and human review each miss things; layer them.

**(a) Build.** "Read the transcripts" applies to reviewer verdicts too: sample a few reviewer
rejections each round and check they were right.

**(b) Product.** Our `autonomy/agents/evals` already reports pass^k, which is right. Gaps: its cases
are hand-written, not taken from failures; it lacks balanced "should block / should pass"
pairs for the Critic and "STOP / not STOP" pairs for Conversation; and it has no capability tier.
Add the REAL_DATA_RESULTS failures as eval tasks now: the 17 silent file-mapping errors (onboarding /
Data Doctor), the "retention means bought in window" misread, and the currency VIP bar.

## 6. Claude Code best practices (code.claude.com, current)

[code.claude.com/docs/en/best-practices](https://code.claude.com/docs/en/best-practices)

- "Give Claude a check it can run"; gate the stop with a Stop hook or `/goal`; have Claude "show
  evidence rather than asserting success".
- CLAUDE.md: for each line ask "Would removing this cause Claude to make mistakes?" Long files get
  ignored; rules that must always hold belong in hooks, not prose.
- After two failed corrections, `/clear` and restart with a better prompt.
- Writer/Reviewer in fresh contexts; but "a reviewer prompted to find gaps will usually report some,
  even when the work is sound". Tell it to flag only gaps that affect correctness or stated requirements.
- Fan-out: test the prompt on 2-3 items, then run all.

**(a) Build.** This is the most actionable piece for our workflow. (1) Our skeptical reviewer
should be told what is *not* a finding, or the finisher over-engineers. (2) Turn our honesty rules
("never read Gym truth", "no seed tuning") into a CI check or hook, not just reviewer prose.
(3) Finishers must paste the command and output that proves done. (4) Pilot each new stream prompt on
one task before launching six.

**(b) Product.** Same lesson for the Critic: a judge told to find problems finds problems. Measure its
false-reject rate (see §10), not only its catches.

## 7. Cognition, "Don't build multi-agents" (Jun 2025) and follow-up (Apr 2026) *(secondary)*

Original: [cognition.ai/blog/dont-build-multi-agents](https://cognition.ai/blog/dont-build-multi-agents) (blocked here; read via excerpts)

- Principle 1: "Share context, and share full agent traces, not just individual messages."
- Principle 2: "Actions carry implicit decisions, and conflicting decisions carry bad results."
  Flappy Bird example: two subagents build a Mario-style background and a mismatched bird; each was
  fine alone, the failure appears at integration.
- Default: a single-threaded linear agent; for very long tasks, a model that compresses history.
- April 2026 follow-up ("Multi-Agents: What's Actually Working"): Devin now uses a few specific
  multi-agent flows, but still warns that parallel agents make implicit choices about style and edge cases.

**(a) Build.** This is the strongest warning against our parallel streams. Our mitigation is the
right one: freeze contracts first, one integrator, reviewers that only *read*. Add one more: each
stream writes its implicit decisions (naming, error style, units, time zones) into its merge summary
so the integrator can spot conflicts before they are merged.

**(b) Product.** Creative and Critic pass a typed `ContentItem`, not a chat; Strategist → Creative pass
a typed `Brief`. That is Cognition-compatible. The risk is the weekly Analyst → Strategist step: the
Strategist sees only the Analyst's summary and may "narrow" on a misread. Pass the computed numbers
and the evidence table, not just prose.

## 8. Manus, "Context engineering for AI agents: lessons from building Manus" (Jul 2025) *(secondary)*

Original: [manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) (blocked here)

- "KV-cache hit rate is the single most important metric" in production. Keep the prompt prefix
  stable (no timestamp at the top), context append-only, serialisation deterministic.
- "Mask, don't remove": keep the tool list fixed and constrain choices at decoding time.
- Use the file system as unlimited memory; compress reversibly (keep the URL, drop the page).
- Recitation: a `todo.md` rewritten each step keeps goals in recent attention.
- "Keep the wrong stuff in": leave failed actions in context so the model updates. "Don't get
  few-shotted": uniform examples cause repetitive behaviour, so add structured variation.

**(a) Build.** Long builder sessions: keep a task checklist file and re-read it, which we do.

**(b) Product.** Creative generates many variants against the same brief and brand kit: put the
brand kit and brief first, byte-identical every call (sorted-key JSON), and use prompt caching; put
per-call items last. Never put `now` in the system prompt. "Don't get few-shotted" matters doubly
for us: the bandit can only learn if variants really differ on the labelled axes (theme, tone, offer),
so vary the few-shot examples and check label diversity in code.

## 9. OpenAI, "A practical guide to building agents" (Apr 2025) *(secondary)*

PDF: [cdn.openai.com/.../a-practical-guide-to-building-agents.pdf](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) (blocked here)

- Build an agent only for complex judgement, rules that are hard to maintain, or unstructured data.
- Prototype with the most capable model to set a baseline, then swap smaller models where evals allow.
- Maximise a single agent first; split only for complex logic or tool overload. Two multi-agent
  shapes: manager (agents as tools) or decentralised handoffs.
- Layered guardrails: relevance, safety, PII, moderation, tool safeguards rated low/medium/high by
  reversibility and money at stake, rules (regex, blocklists), output validation.
- Human intervention when failure thresholds are exceeded or actions are high-risk.

**(a) Build.** Use the top model for reviewers and contract work; cheaper models only for mechanical
fan-out after a pilot.

**(b) Product.** Rate every *product action* low/medium/high and tie the autonomy dial to that rating
rather than one global switch (see §12). Model tiering: the Critic judge and Strategist on a strong
model; Conversation's classifier can be small once its eval pass^k holds.

## 10. Hamel Husain on LLM-as-judge (his evals skills, 2025-26)

[github.com/ai-evals-course/evals-skills](https://github.com/ai-evals-course/evals-skills) · original post [hamel.dev/blog/posts/llm-judge](https://hamel.dev/blog/posts/llm-judge/) (blocked here)

- Error analysis comes first: you "should only write evals after" reading traces and naming failure modes.
- One judge per failure mode: "Each judge checks exactly one thing." "Strictly binary: Pass or Fail.
  No Likert scales." "The output must include a critique before the verdict." Exhaust code checks first.
- Labels from one principal domain expert, ~100 traces, ~50 Pass / ~50 Fail; split train / dev / test;
  never use dev or test cases as few-shot examples.
- Measure TPR and TNR, target >90% each (minimum 80%), run the test set "exactly once". Raw accuracy
  misleads under class imbalance. Correct production pass rates for judge error (Rogan-Gladen) and
  report a confidence interval.
- Pin dated model versions; re-validate after any prompt or model change.

**(a) Build.** Our skeptical reviewer is a judge too. Keep a small labelled set of past reviews
("real bug" / "noise") and check its precision now and then.

**(b) Product.** The Critic disagrees with this method today: it scores brand_voice and honesty
together on 1-5 scales with a threshold. Change it to two (or more) separate binary judges with
critique-first output, each calibrated on ~100 marketer-labelled messages, and report TPR/TNR in the
eval runner. The founder or the first design partner is the "principal domain expert". The
double-run with shuffled order can stay as a cheap stability check.

## 11. Dex Horthy, "12-Factor Agents" (HumanLayer, 2025)

[github.com/humanlayer/12-factor-agents](https://github.com/humanlayer/12-factor-agents)

- Good agents are "mostly deterministic code, with LLM steps sprinkled in". Teams hit a "70-80%
  quality bar" fast and find "80% isn't good enough" for customer-facing features.
- Own your prompts, context window and control flow; "tools are just structured outputs".
- "Contact humans with tool calls"; launch / pause / resume with simple APIs; make the agent "a
  stateless reducer"; compact errors into context; small agents of ~3-20 steps.

**(a) Build.** Matches our worktree tasks: small, scoped, restartable.

**(b) Product.** Autonomy already *is* this design (`tick(now)` is a reducer, the Orchestrator is
plain code, LLMs pick enums). The missing factor is 7: when the Strategist is unsure (ambiguous goal,
conflicting budget), it should return a typed `NeedsHuman{question, options}` that the UI shows,
instead of falling back to a template silently.

## 12. Anthropic, "Measuring AI agent autonomy in practice" (Feb 2026) and "How we built Claude Code auto mode" (Mar 2026)

[anthropic.com/research/measuring-agent-autonomy](https://www.anthropic.com/research/measuring-agent-autonomy) · [anthropic.com/engineering/claude-code-auto-mode](https://www.anthropic.com/engineering/claude-code-auto-mode)

- Trust is earned with use: auto-approve rises from ~20% of sessions for new users to "over 40%" for
  experienced ones, and experienced users also interrupt more (active monitoring, not abdication).
- Oversight is about "whether humans are in a position to effectively monitor and intervene", not
  approving every action. Agent-initiated pauses are "an important kind of oversight".
- Auto mode: users accepted "93%" of prompts anyway (approval fatigue). Three tiers: safe allowlist,
  routine edits without a check, a classifier for the rest.
- The classifier is "reasoning-blind by design": it sees the user's request and the action, not the
  agent's prose. Blocked actions return a reason and the agent continues; after "3 consecutive
  denials or 20 total" it escalates to a human. They publish "the honest number": 17% false negatives.

**(a) Build.** Our reviewer should see the task contract and the diff, never the builder's
explanation of why it is fine. That is the reasoning-blind principle.

**(b) Product.** Three changes. (1) Autonomy level per action risk tier, not per account: e.g.
Autopilot for email within the envelope, Approve for any new offer or WhatsApp template, and promote
a tier after N approved batches with no edits (that is the trust curve above). (2) The Critic judge
should see the brief, brand kit and message only, not the Creative's rationale. (3) Critic rejections
go back to Creative with the reason; after 3 rejections of a slot, escalate to the marketer rather
than looping. Publish the Critic's false-reject and miss rates the way auto mode did.

## 13. Anthropic, "Scaling Managed Agents: decoupling the brain from the hands" (Apr 2026)

[anthropic.com/engineering/managed-agents](https://www.anthropic.com/engineering/managed-agents)

- Separate harness (brain), sandbox (hands) and session log; each can fail and be replaced. "The
  container became cattle."
- The durable session log lives outside the context window; a crashed harness resumes from the last event.
- "The harness is never made aware of any credentials"; secrets come from a vault via a proxy.
- Harness assumptions "can go stale as models improve" (a context-reset workaround for Sonnet 4.5 was
  unnecessary on Opus 4.5).

**(a) Build.** Re-test our workflow scaffolding (reviewer count, retries) on each new model; remove
what no longer earns its cost.

**(b) Product.** We already have the event log and idempotent dispatch. Add: no LLM agent ever holds
SES/WhatsApp credentials or calls a send API; only the Dispatcher does. Re-run agent evals and
re-check the template fallbacks on every model upgrade.

## 14. Anthropic, "Building agents with the Claude Agent SDK" (Sep 2025) and Claude structured outputs docs

[claude.com/blog/building-agents-with-the-claude-agent-sdk](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) · [platform.claude.com/docs/en/build-with-claude/structured-outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

- Loop: "gather context -> take action -> verify work -> repeat". Verification in order of strength:
  rules, visual checks, then LLM-as-judge ("less robust" and slower).
- To improve an agent, read its failures and ask which tool or rule is missing.
- Structured outputs use constrained decoding: "No retries needed for schema violations". Limits: no
  `minimum`/`maximum`/`minLength`, `additionalProperties` must be false, refusals and `max_tokens`
  can still break the schema, enum values may differ in case.

**(a) Build.** Our CI-first verification order (rules, then tests, then reviewer) is the same order.

**(b) Product.** `ask_json` pastes the schema into the prompt and repairs once. Where the provider
supports it, switch to constrained decoding and keep pydantic validation for the constraints the
grammar cannot express; handle `refusal` and `max_tokens` stop reasons explicitly; compare enums
case-insensitively. Note that changing the output schema breaks the prompt cache.

---

## Where the articles disagree, and which side fits us

| Question | Side A | Side B | For Autonomy |
|---|---|---|---|
| Multi-agent or single agent? | Anthropic research system: parallel subagents win on breadth-first work | Cognition, OpenAI: one agent until it breaks; parallel writers make conflicting decisions | **Product:** workflow of typed steps, no peer chat (B). **Build:** parallel only after contracts are frozen, reviewers read-only (A with B's safeguards). |
| Load context early or on demand? | Anthropic context eng.: just-in-time retrieval | 12-factor: pre-fetch everything you might need | **Product agents:** pre-fetch (short, bounded, numbers from code). **Build agents:** just-in-time. |
| Errors in context | Manus, 12-factor: keep the failure in so the model adapts | Claude Code: after two failed corrections, clear and restart | **Product:** one repair retry with the compact error in context. **Build:** restart with a better prompt. |
| One judge or many? | Anthropic research system: one call scoring several criteria 0-1 plus pass/fail | Demystifying evals, Hamel: one binary judge per failure mode, calibrated | **Many binary judges** for the Critic; it gates real sends. |
| How much human approval? | OpenAI: humans on high-risk actions and failure thresholds | Anthropic autonomy study: approval of everything causes fatigue; monitor and intervene | **Both:** risk-tiered approval plus a good monitor and a pause button; tiers graduate with track record. |

## What to do next (ordered)

1. Split the Critic judge into binary per-failure-mode judges with critique-first output; label ~100
   messages with the founder; report TPR/TNR and a corrected pass rate. (§5, §10)
2. Seed the eval set from real failures (the 17 silent mapping errors, retention misread, currency bar)
   and add balanced should-block/should-pass pairs. (§5)
3. Autonomy dial per action risk tier, with graduation after clean approved batches and escalation
   after 3 rejections. (§9, §12)
4. Critic and reviewers are reasoning-blind: they see inputs and outputs, never the author's rationale. (§12, §6)
5. Strategist returns `NeedsHuman` instead of a silent template fallback when the goal is ambiguous. (§11)
6. Constrained decoding in `ask_json`, with explicit refusal and max-token handling. (§14)
7. Stable, cached prompt prefixes for Creative; diverse few-shot examples; a code check on label diversity. (§8)
8. Reviewer prompts state what is not a finding; finishers paste evidence; honesty rules become CI checks. (§6)
9. Trace every LLM call (prompt version, model id, verdict) and roll prompt changes out Shadow-first. (§4)
10. Each stream's merge summary lists its implicit decisions for the integrator. (§7)
