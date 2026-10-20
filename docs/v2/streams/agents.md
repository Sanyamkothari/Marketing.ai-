# Stream WF4: Agents

Branch `v2/agents`. Owns `autonomy/agents/`, `autonomy/signals/` and `tests/v2/agents/`.

The outer loop of Autonomy: LLM agents that plan, write, check, explain and talk to customers.
None of them decides anything per customer, and no agent invents a number: code computes every
figure, and the model only picks from fixed lists or writes words around code's numbers. Every agent
falls back to plain code when the model fails.

## What was built

| Module | What it does |
|---|---|
| `agents/base.py` | `ask_json` (ask for JSON that parses into a pydantic model, one repair retry, then `AgentOutputError`), `ScriptedLLM` test double, `load_prompt` (the one prompt loader). |
| `agents/strategist.py` | Goal -> `Brief`. The model picks from fixed lists; code checks every pick and builds the brief. A fixed template per goal kind when the model fails. Weekly `adjust_brief` can only narrow. |
| `agents/orchestrator.py` | Goal lifecycle state machine (DRAFT -> BRIEFED -> APPROVED -> LIBRARY_READY -> ACTIVE <-> PAUSED -> DONE), stage read from the `Workspace`. No LLM. |
| `agents/creative.py` | Brief -> DRAFT `ContentItem` library, one LLM call per channel x offer x theme cell. Code assigns ids, labels, slots, length caps and email unsubscribe footers. |
| `agents/critic.py` | Rules first (lengths, slots, caps, banned phrases, only approved offer amounts, allowed link domains), then an LLM judge run twice with the rubric reordered. Fails closed. |
| `agents/claims.py` | `check_numbers`: lists every number in a text that matches none of code's values. |
| `agents/analyst.py` | Weekly `Briefing` (code builds the numbers, LLM writes words, checked by `check_numbers`, template fallback) and rule-based opportunity finder that proposes DRAFT goals. |
| `agents/conversation.py` | Reply handling: hard STOP keyword net (never overridden by a model), complaints, then intent via `TextSignals`; returns events to record and whether a person must take over. |
| `agents/prompts/*.md` | Every prompt, as a reviewable markdown file. |
| `agents/evals/` | 29 eval cases (`cases.yaml`) and `runner.py` (pass rate, pass^k, consistency; `--fake` or real Bedrock). |
| `signals/llm_signals.py` | `LLMTextSignals`: batched LLM classification, labels enforced by code. |
| `signals/embedding_signals.py` | `EmbeddingTextSignals`: nearest centroid over embeddings of a few examples per label. |
| `signals/jev.py` | `JevTextSignals`: httpx adapter for TypeSafe's Jev. API shape is an assumption, isolated in `jev_exchange`. |

## How other streams use it

All main names are re-exported from `autonomy.agents` (signals from `autonomy.signals`; Jev from
`autonomy.signals.jev`). Time is always passed in.

```python
Strategist(llm, model_id=None)
  .draft_brief(goal, *, business_facts: BusinessFacts, now) -> Brief         # unapproved, version 1
  .adjust_brief(brief, insights: Briefing, now) -> Brief                     # version + 1, only narrows
brief_within(new: Brief, approved: Brief) -> list[str]                       # empty = inside the envelope

Orchestrator(workspace, *, analyst=None, adjust=None)
  .create(goal) / .attach_brief(goal, brief) / .approve(goal, approved_by=..., now=...)
  .add_library(goal, items) / .activate(goal, *, now) / .pause(goal) / .resume(goal, *, now) / .finish(goal)
  .stage(goal) -> GoalStage
  .weekly_cycle(goal, *, now) -> WeeklyOutcome
  # analyst: (goal, brief, now) -> Briefing ; adjust: (brief, briefing, now) -> Brief
  # wrong-stage moves raise IllegalTransitionError; unknown goals raise UnknownGoalError

Creative(llm, *, model_id=None).build_library(brief, *, brand_voice, n_per_cell=2, now, themes=None)
  -> list[ContentItem]                                                       # all DRAFT
Critic(judge_llm, judge_model_id=None, *, threshold=3, seed=0).review(items, brand_rules) -> list[ContentItem]
BrandRules.from_brief(brief, *, brand_voice, allowed_domains=())
check_rules(item, rules) -> list[str]

Analyst(llm, model_id=None)
  .briefing(goal | None, proof, health, facts, period, now) -> Briefing      # never raises on bad model output
  .write_briefing(...) -> BriefingResult                                     # + source and problems, for evals
  .opportunities(states_summary, now, *, business_id, currency, existing, max_goals=3) -> list[Goal]
check_numbers(text, numbers: Mapping[str, float], *, rel_tol=0.05) -> list[str]

Conversation(signals: TextSignals, *, stop_all_channels=True)
  .handle_reply(event, state | None, now) -> ReplyOutcome  # intent, events_to_record, needs_human, suggested_reply

LLMTextSignals(llm, model_id=None, *, batch_size=20)          # all implement interfaces.TextSignals
EmbeddingTextSignals(llm_for_embed, examples: {label: [texts]}, *, temperature=0.05)
JevTextSignals(base_url, api_key, timeout=10.0)

python -m autonomy.agents.evals.runner --fake [--repeats 5 --fail-under 1.0 --json out.json]
```

The Orchestrator's `analyst` callable is `(goal, brief, now)`, while `Analyst.briefing` also needs
proof, health, facts and a period. Whoever wires the weekly job (WF5/WF8) passes a small lambda that
fetches those and calls `Analyst.briefing`.

## Contract additions requested

None are required. Optional:
- A cart signal on `CustomerState` (for example `last_cart_at`) so cart recovery can target real cart
  abandoners. The template uses "seen in the last 3 days" for now.
- A stored `GoalStage` on `Goal` (or a Workspace method), if the API/UI want the stage stored rather
  than read off the workspace.
- A way for `Goal` to say whether the marketer chose the objective, so the Strategist does not
  override an explicit choice (today it cannot tell it from the `GROSS_PROFIT` default).
- A shared shape for the segment summary the Analyst's opportunity finder reads (now the local
  `SegmentSummary`: customers, contactable, avg_value, margin_rate).

## Dependencies requested

None new. `httpx` (already in the v2 CI job) is needed only by `autonomy.signals.jev`; `pyyaml` by
the eval runner.

## Known gaps

- mypy `--strict` on Python 3.11 is CI-only (local Python is 3.13). Code uses no 3.12+ syntax.
- The 3 Jev tests skip locally (no httpx here); they ran through an alias shim and run in CI.
- Jev's real API shape is unverified; only `jev_exchange` should need changing.
- `Orchestrator.approve` re-stores the same brief version with approval filled in. The Postgres
  Workspace must upsert on `(goal_id, version)`, like the in-memory fake.
- Weekly narrowing is a ratchet: something narrowed comes back only with a newly approved brief.
- `not_now` replies store `preferences['snooze_until']`; decide() (WF3) must honour it.
- Reply complaints are recorded as `COMPLAINT` events (`attrs.reason='reply_complaint'`). WF1/WF5
  should decide whether they count toward the complaint-rate breaker.
- `check_numbers` checks size, not sign or meaning; number words and ranges are not handled.
- The Critic's money/percent rule is strict ("free shipping over $50" is rejected), bare-domain link
  detection uses a fixed TLD list, and lengths are measured before slots are filled.
- `autonomy/fakes/samples.py::sample_content` emails have no unsubscribe hint, so the Critic rejects them.
- Prompts are loaded by path: if Autonomy ships as a wheel, `autonomy/agents/prompts/*.md` must be package data.
- Opportunity planning numbers (response rates, 0.4 margin, 0.3 budget share) are assumptions for the Gym or prior store to replace.
- Fake-mode eval scores only prove the plumbing. Real scores need a run with a judge model from a
  different family than the Creative's. The integrator may add `runner --fake --fail-under 1.0` to CI.
- Eval sets live in `autonomy/agents/evals/`, not `tests/v2/evals/` as PARALLEL_BUILD.md lists.
