# v2 Parallel Build Plan

How we build [`plan-v2.md`](../../plan-v2.md) as fast as possible, with many agents working at once.

**Branch:** `version-2` is the integration branch. Every stream works on its own branch, `v2/<stream>`, in its own git worktree, and merges back into `version-2`.

---

## 1. The idea

The plan has eight phases, but most of them don't really depend on each other. They only need to agree on **shapes**: what an Event, a Customer state, a Decision or a Brief looks like. So:

1. **Wave 0:** freeze the shared shapes (contracts) and interfaces first. This is one small step, done by Claude directly.
2. **Wave 1:** six workflows build six streams **at the same time**, each against the contracts, using fakes for the parts the others are still building.
3. **Wave 2:** plug the real pieces together and run the Gym end to end.
4. **Wave 3:** live sending, the marketer test and the POC pack.

**Why several workflows:** this machine has 4 CPUs, so each workflow runs 2 agents at a time. Six workflows in parallel means about 12 agents working at once. Agents mostly wait on the model, so the CPUs keep up. The one thing that is CPU-heavy is tests, so each stream runs only its **own fast tests**, and the integrator runs the full suite at merge time. Nobody runs AutoGluon training inside a stream.

**Rough speed:** sequential, the plan is 6–8 weeks. In parallel it's **about 1.5–2 weeks**, plus however long it takes to open network access for real sending.

---

## 2. Dependency map

```
Wave 0  CONTRACTS + INTERFACES + FAKES + TEST HARNESS          (Claude, ~half a day)
          │
          ├──▶ WF1 Backbone & Data ─────────┐
          ├──▶ WF2 Gym ─────────────────────┤
Wave 1    ├──▶ WF3 Decide & Proof ──────────┤   all six run at the same time,
          ├──▶ WF4 Agents ──────────────────┤   each with fakes for the others
          ├──▶ WF5 Channels & API ──────────┤
          └──▶ WF6 Marketer UI ─────────────┘
                                            │
Wave 2  INTEGRATION (2–3 workflows): decide() inside the Gym → Gym pass criteria;
        agents on Gym customers; API wired to real services; UI wired to the API
                                            │
Wave 3  LIVE (2 workflows): live loop on our phones and inboxes · marketer test + POC pack
```

**What really is sequential (the critical path):**
contracts → `decide()` → Gym pass → integrated loop → live loop → POC pack.
Everything else runs beside it.

---

## 3. Wave 0: what gets frozen first

Claude builds this directly, before any workflow starts. It's what makes parallel work safe.

| Piece | Contents |
|---|---|
| `autonomy/contracts/` | Pydantic models: `Event`, `EventType`, `CustomerState`, `Goal`, `Brief`, `Envelope`, `ContentItem` (with labels: theme, tone, offer, format, channel), `Action`, `Decision` (with `probability`, `reason`, `decision_id`), `Bid`, `Outcome`, `Prior`, `Briefing`, `HealthStatus` |
| `autonomy/interfaces.py` | Protocols: `EventStore`, `StateStore`, `JobQueue`, `Bidder`, `Policy` (`decide()`), `Environment` (the Gym and the real world look the same to `decide()`), `Dispatcher`, `ChannelAdapter`, `TextSignals`, `PriorStore`, `AgentRole` |
| `autonomy/fakes/` | In-memory versions of every interface, so each stream can run on its own |
| DB schema | Tables: `event`, `customer_state`, `decision`, `goal`, `brief`, `content_item`, `prior`, `job`, plus a migration |
| Test harness | `tests/v2/` layout, fixtures, `make v2-test` (fast, under 2 minutes) |
| `docs/v2/CONTRACTS.md` | One page that each stream reads first |

**Contract change rule:** a stream that needs a new field **adds** it (never renames or removes), notes it in its merge summary, and the integrator applies it to `version-2` first.

---

## 4. Wave 1: six workflows at once

Each stream owns its folders. Nobody edits another stream's folders.

| WF | Stream | Owns | Builds | Done when |
|---|---|---|---|---|
| **WF1** | **Backbone & Data** | `autonomy/store/`, `autonomy/data/`, `autonomy/priors/` | Postgres event store and state store, state builder (events → `CustomerState`), Postgres job queue, loaders that turn Hillstrom and online-retail into events, Data Doctor bridge (map files to our event types), cross-client prior store | Load online-retail as events, rebuild state, and fetch any customer's timeline in under 1 second |
| **WF2** | **Gym** | `autonomy/gym/` | Customers over time with planted truth (offer response, channel preference, best hours, fatigue, sleeping dogs, drift); scenario library (at least 6); baselines (send-to-all, rules, oracle); scoreboard with the plan's pass criteria | Baselines and oracle scored on every scenario; the scoreboard is reproducible by seed |
| **WF3** | **Decide & Proof** | `autonomy/decide/`, `autonomy/proof/` | Goal bidders, attention auction, attention price, do-nothing bidder, hard rules, Thompson sampling with exploration floor, send-time choice, probability logging, nightly learner; three-way split, wrappers for v1 measurement and OPE, public-data replay (Open Bandit, Hillstrom, Criteo) | `decide()` passes its unit tests and runs against `FakeEnvironment`; the replay report is produced |
| **WF4** | **Agents** | `autonomy/agents/`, `autonomy/signals/`, `tests/v2/evals/` | Strategist (goal → Brief), Creative (labelled library), Critic (rules + judge from a different model family), Analyst (briefing, numbers from code only, opportunity finder), Conversation (reply intent, stop, hand-off), text → signals slot (Haiku / embeddings / Jev adapters), orchestrator state machine, eval sets | All agents run on the fake LLM in tests and on Claude in evals; eval sets pass 5/5 consistency checks |
| **WF5** | **Channels & API** | `autonomy/channels/`, `api/v2/` | Dispatcher, SES adapter, WhatsApp Cloud API adapter, webhook/CSV adapter, inbound webhooks (signatures, dedupe, `decision_id` mapping), circuit breakers; FastAPI routes for goals, briefs, approvals, decisions, events, briefing, proof, pause | All adapters pass against recorded provider responses, with no network; the API contract (OpenAPI) is published for the UI |
| **WF6** | **Marketer UI** | `ui/v2/` | The five screens (Connect, Goal, Plan, Live, Proof), the autonomy dial, an activity feed with "why", pause, Operator mode; built against the API contract with mock data | Every screen works on mock data; screenshots of each |

**Inside each workflow (2 agents at a time):** the stream is split into 3–5 tasks. Each task goes through **build → adversarial review → fix**, as a pipeline, so one agent builds the next task while the other reviews the last one. Each workflow commits to its own `v2/<stream>` branch and returns a merge summary covering what was built, contract additions, test results and known gaps.

**Integrator (Claude in the main session):** merges streams into `version-2` as they finish, in this order: WF1 → WF3 → WF2 → WF4 → WF5 → WF6 (the order that means the fewest fakes to replace). It runs the full v2 suite after each merge and fixes any breakage in the joins.

---

## 5. Wave 2: integration (three workflows at once)

| WF | Joins | Done when |
|---|---|---|
| **WF7** | `decide()` + real store + the Gym's scenario library | **Gym pass criteria met** (≥80% of oracle value, beats baselines in ≥95% of seeds, <10% of sleeping dogs contacted, zero rule breaks, intervals cover the truth 93–97% of the time, breaker trips) |
| **WF8** | Agents + `decide()` + the Gym: goal → brief → library → daily loop → weekly Analyst report → Strategist adjustment | A full 90-simulated-day run with a plain-English briefing at each step |
| **WF9** | API wired to the real services; UI wired to the API | The full click-through in a browser (Playwright screenshots) |

---

## 6. Wave 3: going live (two workflows at once)

| WF | What | Needs |
|---|---|---|
| **WF10** | **Live loop:** 7 days of real sends to our own test inboxes and phones, events returning, replies handled, "stop" honoured | Network access for SES and graph.facebook.com in the environment settings (or a local run), an SES-verified domain, a WhatsApp test number |
| **WF11** | **Marketer test + POC pack:** usability script, one-page results (Gym, replay, live loop), demo script, short deck | The founder finds one person who isn't us to try it |

---

## 7. What the founder does in parallel

These need a human and nobody else can do them:
- **Before Wave 3:** an AWS account with SES (a verified sending domain), a Meta developer app with a WhatsApp test number, and network access opened for those hosts.
- **An API key** for Claude, for the agent evals (otherwise the evals run on the fake model only).
- **Review the demo** at the end of each wave (about 30 minutes each).
- **Find one non-technical person** for the marketer test.

---

## 8. Risks of going parallel, and the fix for each

| Risk | Fix |
|---|---|
| Streams drift away from each other | Contracts are frozen first and changes are additive only; the integrator merges often |
| Merge conflicts | Strict folder ownership plus separate worktrees and branches; shared files (`pyproject.toml`, `Makefile`) are edited only by the integrator |
| CPU contention from tests | Each stream runs only its fast tests; AutoGluon is never run inside a stream; the full suite runs at merge |
| Fakes that hide real problems | Wave 2 exists for exactly this; the Gym is the judge |
| Token cost of about 12 agents | Each task is scoped tightly; each workflow returns a short summary, not file dumps |
