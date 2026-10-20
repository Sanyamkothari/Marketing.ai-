# v2 Contracts: read this before writing v2 code

Everything in v2 is built against the shapes in `autonomy/contracts/` and the seams in `autonomy/interfaces.py`. Every seam has an in-memory version in `autonomy/fakes/`, so any stream can build and test on its own.

## The shapes (`autonomy/contracts/`)

| Module | What's in it |
|---|---|
| `base.py` | `Contract` (frozen, unknown keys rejected), `Channel`, `Group` (agent / baseline / holdout), `new_id()`, `stable_id()` |
| `events.py` | `Event`, `EventType`. Outbound events (send, deliver, open, click, reply, bounce, unsubscribe, complaint, block, fail) carry the `decision_id` that caused them |
| `customer.py` | `CustomerState` (one row per customer, a fold over events) and `ChannelStats` (consent, contactable, counts) |
| `goals.py` | `Goal` (the marketer's words + budget), `Brief` (the Strategist's plan), `Envelope` (channels, offers, caps, quiet hours, exploration floor, split shares, stop rules), `OfferOption`, `Audience` / `AudienceFilter`, `FollowUpStep`, `AutonomyLevel` |
| `content.py` | `ContentItem` (labelled by theme, tone, offer, format, channel; `{{slot}}` bodies), `Message` (rendered for a channel), `SendReceipt` |
| `decisions.py` | `Action`, `Bid`, `Decision` (always with `probability` and a plain-English `reason`), `DecisionContext` (what `decide()` may read besides state) |
| `outcomes.py` | `Reward`, `Estimate` (point + 95% interval), `ProofSummary`, `Prior` (pooled patterns, at least 5 clients if client-sourced), `Alert`, `HealthStatus`, `Briefing`, `Job` |

## The seams (`autonomy/interfaces.py`)

`EventStore`, `StateStore`, `StateFolder`, `DecisionLog`, `Workspace`, `JobQueue`, `PriorStore` (WF1) · `Bidder`, `Policy` (WF3) · `Environment` (WF2: the Gym; WF5: the real world) · `ChannelAdapter` (WF5) · `TextSignals` (WF4).

## The fakes (`autonomy/fakes/`)

In-memory stores, `SimpleStateFolder`, `FakeEnvironment` (a tiny world), `NothingPolicy` and `ContactEveryonePolicy`, `FakeChannelAdapter`, `KeywordTextSignals`, and `samples.py` (a ready goal, brief and content library). For an LLM, use `engine.llm.FakeLLMClient`.

`autonomy/loop.py::run_days()` is the reference loop that shows how the pieces meet.

## Rules for every stream

1. **Contracts change by addition only.** Add an optional field; never rename or remove one. Note every addition in your merge summary.
2. **Time is passed in.** Never read the clock inside logic (`now` comes from the caller), so the Gym can run 90 days in seconds.
3. **No LLM in the inner loop.** `decide()`, bidders and learners are maths.
4. **Every decision has a probability.** Including "do nothing".
5. **Stay light.** The `autonomy` core depends only on pydantic, numpy, pandas, SQLAlchemy and the standard library. Anything that imports heavy v1 modules (AutoGluon, LightGBM, DuckDB, `engine.uplift`, `engine.stages`) goes in `autonomy/bridges/`, and its tests carry `@pytest.mark.v1bridge`.
6. **Python 3.11.** CI runs 3.11, so no 3.12+ syntax (`type X = ...`, `def f[T]()`).
7. **Stay in your folders.** Only the integrator edits shared files (`pyproject.toml`, `Makefile`, CI). If you need a dependency, ask for it in your summary.

## Testing: CI does the heavy lifting

- **Locally, run only cheap checks:** `ruff check` and `black --check` on your files, plus **your own new tests** (`python -m pytest tests/v2/<your_dir> -q`). They must finish in seconds.
- **Never run locally:** `make setup`, `make test`, `make test-all`, AutoGluon training, or the whole repository's test suite.
- **CI is the gate.**
  - `.github/workflows/v2.yml` runs ruff, black, strict mypy and `tests/v2` in a light environment within minutes, on `version-2` and every `v2/**` branch.
  - `ci.yml` runs everything, including the v1 bridges.
- **Where tests go:** `tests/v2/<stream>/`, named `test_*.py`.
