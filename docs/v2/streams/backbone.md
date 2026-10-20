# WF1 Backbone & Data

Branch `v2/backbone`. Owns `autonomy/store/`, `autonomy/data/`, `autonomy/priors/` and
`autonomy/bridges/doctor_bridge.py`. Tests are in `tests/v2/backbone/`.

Stream target: load online-retail as events, rebuild state, and fetch any customer's timeline in
under 1 second. `tests/v2/backbone/test_backbone_end_to_end.py` checks exactly that on SQLite.

## What was built

| Module | What it does |
| --- | --- |
| `autonomy/store/schema.py` | One SQLAlchemy `metadata` with the tables event, customer_state, decision, goal, brief, content_item and job. `create_all(engine)` is safe to call twice. `UtcDateTime` refuses naive datetimes and stores UTC. |
| `autonomy/store/sql.py` | `SqlEventStore`, `SqlStateStore`, `SqlDecisionLog` and `SqlWorkspace`. They behave exactly like the fakes in `autonomy.fakes`, on Postgres or SQLite. Also has `memory_engine()` and the shared `insert_ignore` / `upsert` helpers. |
| `autonomy/store/queue.py` | `SqlJobQueue`. On Postgres, `claim` uses `FOR UPDATE SKIP LOCKED`; on SQLite it uses a conditional update. |
| `autonomy/data/folder.py` | The production `StateFolder` (events to `CustomerState`). It agrees with `SimpleStateFolder` and adds timezone and country, local active hours, a shrunk `offer_sensitivity`, segments from `SegmentRules`, and protection against late events. |
| `autonomy/data/rebuild.py` | `rebuild_states` replays the whole event store into the state store, keeping `group` and `scores`. `apply_events` folds new events into the stored states. |
| `autonomy/data/mapping.py` | `EventMapping` (a declarative table-to-event mapping that loads from YAML or JSON) and `map_frame`. Every row becomes either an `Event` with a stable id or a `MappingProblem`. |
| `autonomy/data/loaders.py` | Loaders for the committed online-retail sample, the raw online-retail log (when fetched) and Hillstrom. Each returns a `LoadResult` that includes plain-English notes on what was approximated. |
| `autonomy/priors/pooling.py` | Cross-client priors (plan section 7). `pool` needs at least 5 clients, caps any one client at 30% weight, weights by sqrt(n) and keeps no identities. Also `blend` (prior plus local data) and `seed_from` (Gym or public priors). |
| `autonomy/priors/store.py` | `SqlPriorStore` (its own `prior` table) and `JsonPriorStore` (one file per key). Both behave like `InMemoryPriorStore`. |
| `autonomy/bridges/doctor_bridge.py` | Reads a messy client file through v1's Data Doctor intake, then maps it with an `EventMapping`. v1 is imported only inside functions, and its tests are marked `v1bridge`. |

## How other streams use it

Every store takes a SQLAlchemy `Engine`. Use Postgres in production and CI, and SQLite (or `memory_engine()`) on a laptop.

```python
from autonomy.store import (create_all, memory_engine, SqlEventStore, SqlStateStore,
                            SqlDecisionLog, SqlWorkspace, SqlJobQueue)
create_all(engine)                                   # WF1 tables; idempotent
SqlEventStore(engine).append(events: Iterable[Event]) -> int        # new ones only
SqlEventStore(engine).for_customer(customer_id, *, since=None, until=None) -> list[Event]
SqlEventStore(engine).scan(*, since=None, until=None) -> Iterator[Event]  # time order, streamed
SqlEventStore(engine).for_decision(decision_id) -> list[Event]
SqlStateStore(engine).get(customer_id) / put(states) / scan() / count()
SqlDecisionLog(engine).record(decisions) / get(id) / scan(since=, until=) / for_customer(id)
SqlWorkspace(engine).put_goal / goals / put_brief / briefs(goal_id=None) / put_content / content(goal_id=None, approved_only=True)
SqlJobQueue(engine).enqueue(job) / claim(kinds, *, worker, now) -> Job | None / complete(id) / fail(id, error, *, retry_at=None)

from autonomy.data import StateFolder, SegmentRules, rebuild_states, apply_events
StateFolder(*, segments=None, offer_prior=0.3, offer_pseudo_count=3.0)   # satisfies interfaces.StateFolder
rebuild_states(events, states, folder, now, *, batch_size=1000, keep=("group", "scores")) -> int
apply_events(events, states, folder, *, now=None, batch_size=1000) -> int

from autonomy.data import EventMapping, map_frame, load_online_retail_sample, load_hillstrom
map_frame(frame: pd.DataFrame, mapping: EventMapping, source: str) -> tuple[list[Event], list[MappingProblem]]
load_online_retail_sample(path=..., *, include_outcomes=False) -> LoadResult
load_online_retail_log(path=..., *, grain="invoice") -> LoadResult
load_hillstrom(path=..., *, campaign_at=HILLSTROM_CAMPAIGN_AT, include_outcomes=True) -> LoadResult
# LoadResult(events, problems, notes, arms); arms holds Hillstrom customer id -> arm

from autonomy.priors import pool, blend, seed_from, ClientStat, hash_client, SqlPriorStore, JsonPriorStore
from autonomy.priors import create_all as create_prior_tables
pool(stats: Iterable[ClientStat], key, now) -> Prior        # raises NotEnoughClientsError
blend(prior_values, local_values, local_n, prior_strength) -> dict[str, float]
seed_from(source: PriorSource, values, key, now, *, n_observations=0) -> Prior   # GYM / PUBLIC only
SqlPriorStore(engine).get(key) / put(prior) / keys(prefix="")

from autonomy.bridges.doctor_bridge import read_with_intake, map_file   # needs v1 installed
map_file(path, mapping, source, *, sheet=None, work_dir=None) -> DoctorMapped
```

Points to know:
- `autonomy.store.create_all` creates the WF1 tables only. Call `autonomy.priors.create_all` too if you use the prior table.
- Hillstrom's control third has no event of its own. Off-policy evaluation must read their arm (propensity 1/3) from `LoadResult.arms`.
- Rebuilt sample histories are approximations. Each such event has `attrs.approximate = true`, and outcome events have `attrs.outcome = true`.

## Contract additions requested

- `CustomerState.offer_purchases: int = 0`. Today the folder recovers the offer-purchase count from the
  shrunk `offer_sensitivity`. That is exact only while the same prior and pseudo-count are used.
- (Suggestion, not blocking) a `timezone_known` flag on `CustomerState`. Without it, a learned zone of
  UTC cannot be told apart from the UTC default.

## Dependencies requested

None. The stream uses only pydantic, numpy, pandas, sqlalchemy and the standard library. Postgres runs through `psycopg`, which CI already installs.

## Known gaps

- The Postgres test variants have never run locally, because there is no server or driver here. They run in CI, and `check_no_skips` in ci.yml catches any `[postgres]` test that skips. `MARKETING_AI_REQUIRE_POSTGRES=1` turns those skips into failures, but v2.yml does not set it.
- `.gitignore` has a `data/` line that also matches `autonomy/data/`. The files there were committed with `git add -f`. Add `!autonomy/data/` to `.gitignore` (a shared file), or new files there will be silently ignored.
- mypy --strict passes locally on Python 3.13 without pandas-stubs. The run on Python 3.11 with pandas-stubs is CI's.
- The event `seq` comes from `max(seq)+1`. Two writers on Postgres at the same moment can tie, and the tie then falls back to the id order. A Postgres sequence would fix this if it ever matters.
- `SqlEventStore.scan` pages lazily, so events written during a scan can show up in it. Collect the scan before writing, as the reference loop does.
- Id columns are VARCHAR(200), so longer ids fail on Postgres only. `SqlPriorStore` refuses keys over 200 characters itself.
- BLOCK and FAIL events change no state, matching the fake. A WhatsApp BLOCK should probably set unsubscribed; the fake and the folder should change together.
- `rebuild_states` cannot delete customers that have stored state but no events, because the protocol has no delete. It also keeps one state per customer in memory, which is fine at online-retail scale.
- Country is stored as given, except that two-letter codes are uppercased. ISO codes and names such as "United Kingdom" can therefore mix across sources.
- The 5-client minimum and 30% cap do not stop a differencing attack on priors. That would need noise (differential privacy). Opting out is enforced by whoever collects `ClientStat`s.
- There is no Alembic migration yet, only `create_all`.
