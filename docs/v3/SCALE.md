# v3 scale: the daily sweep in batches

[`plan-v3.md`](../../plan-v3.md) section 7, "Stream the daily sweep in batches". The cause is
[`REAL_DATA_RESULTS.md`](../v2/REAL_DATA_RESULTS.md) failure 11: the sweep read every customer's
state into one list, decided all of them, and kept every `Decision` and a copy of every state until the
tick ended (about 7.4 KB per decision there). A sweep of 300,000 customers finished; one of 1,000,000
was OOM-killed twice in a shared 14 GB cgroup.

## What changed

All of it is in `autonomy/runtime/service.py`, plus one setting in `autonomy/runtime/settings.py`.

| Before | Now |
|---|---|
| `everyone = [refresh(s) for s in states.scan()]`, shared by every goal | Each goal's audience is read from the state store a page at a time (`_members`, a generator) in customer order |
| One `_decide_goal` over all candidates, then one `_act` over all decisions | `_decide_goal` takes `RuntimeConfig.sweep_batch` candidates (default 5,000), picks, enriches, decides, records them, then `_act` sends or holds them; the batch is dropped before the next is read |
| `prepare_day(everyone_list, ctx)` every day a policy has the hook | `prepare_day(view, ctx)`, where `view` (`_StateView`) is a `Sequence` that re-reads the store each time it is iterated. A policy that loops over it holds one state at a time; one that indexes it reads the population into a list once (the old cost) |
| `TickReport.decisions` held every decision | Same by default. `RuntimeConfig.report_decisions = N` keeps the first N in full and counts the rest (`decision_count`, `act_count`, and `summary()` use the counts) |
| `_assign_groups(None)` listed every state | Streams the scan and writes the changed states back `sweep_batch` at a time |

`sweep_batch=None` is one batch holding the whole audience: the memory behaviour of before (kept so the
two can be compared). The setting is `AUTONOMY_SWEEP_BATCH` for a deployment.

## What needs the whole population, and how it still works

Nothing was dropped; each of these is carried across the batches of one goal's pass in a `_GoalPass` and
settled once at the end (`_finish_goal`), in the order a single batch raised them.

- **The budget.** `ctx.budget_left` is read once before the first batch, as it was read once before
  deciding. A batch's sends do not change what the next batch's `decide` sees.
- **The mandate's daily cap and ceiling.** The spend is read once for the pass, and what each batch lets
  through is added to it (`extra`). The token-bucket pacer is stored back after every batch, so the next
  batch draws from what is left. Blocked decisions are written back to the log at once (the batch that
  made them); the report's copies are fixed in place; one `mandate.paced` or `mandate.violation` alert is
  raised at the end with the same key and counts as before.
- **The one pending `Batch` for approval.** A `_BatchTally` counts each chunk's ids, channels, offers,
  spend and expected net and drops the decisions. The ids (strings) are kept, because the batch lists
  them. `make_batch` is the same tally over a whole list, so the batch is identical.
- **The one-contact rule.** A customer an earlier goal contacted this tick is skipped, as before. The
  customers this goal contacts are merged into the tick's set when the goal's pass ends, so a batch never
  sees its own goal's contacts. With `one_contact_per_tick=False` the sweep judges a contacted customer
  on their state from before the send (`before_sends`), as the shared snapshot did.
- **Alerts.** `decide.error`, `decide.wrong_goal`, `enrich.error`, `dispatch.missing`, `dispatch.error`
  and `dispatch.refused` are each raised once per goal, not once per batch.
- **`prepare_day`.** The agent's `Segmenter.fit` and `OrganicRate.fit` loop once over the states and keep
  floats, so they run on the lazy view; after the first day they do not loop at all. The
  agent's first-day warm start still builds `{customer_id: state}` for the whole population (it also
  holds a `HistoryDigest` of the whole history, which is the larger part); that is the policy's own
  first-day cost, in `autonomy/runtime/agent.py`, not changed here.

## Proof that the result is the same

`tests/v3/scale/scenario.py` is a small business: three goals at SHADOW, APPROVE and AUTOPILOT, customers
who differ in consent, history and cart activity, a policy that passes on some customers and raises for
others, a mandate that either caps daily spend (paced) or excludes some customers (violation), and four
ticks (the sweep, a repeat, a lapse check three hours on, the next morning with the held batch approved).
`fingerprint()` turns everything a run did into plain data: each tick's `summary()`, the report's
decisions in order, the dispatch results, the decision log, every event, the held batches, the alerts, the
stored states, what `prepare_day` was handed and how many customers each policy decided.

- `tests/v3/scale/test_sweep_batches.py` runs it at batch sizes 1, 7, 25, the default and None (the whole
  audience), with `one_contact_per_tick` on and off and with no mandate, a pacing mandate and an
  excluding mandate, and asserts the fingerprints are equal. It repeats the check on SQL state and
  decision stores (paged in tens), and with a group assigner that rewrites every state.
- Against the code before the change: the same scenario ran on commit `dd878462` (the parent of this
  branch) and on this branch, 300 customers, and the fingerprints were compared byte for byte. Every
  combination was identical: `one_contact_per_tick` on/off x mandate none/paced/violation x state enricher
  on/off x batch 1, 7, 64, None and the default (60 runs), and, with the group assigner on, x mandate
  none/paced x batch 1, 64, None, default (32 runs). To repeat one, copy `tests/v3/scale/` into a checkout
  of `dd878462`, and run in each checkout:
  `python -m tests.v3.scale.dump_fingerprint out.json --mandate paced --groups`
  (add `--batch 37` on this branch), then `cmp` the two files.

## What is not the same, and what stays linear

- **A scan per goal.** The old sweep scanned the store once and gave the snapshot to every goal. Each goal
  now scans for itself, because a goal's batches are sent before the next goal reads. With three goals that
  is three passes over the store, plus one for `prepare_day` on the first day. Time, not memory.
- **The enricher runs once per batch.** The demo's enricher (the only one in the repository) works per
  customer, so its output is the same. An enricher that looked across customers would see a batch, not the audience.
  When a batch holds more than `ENRICH_PER_CUSTOMER_MAX` (200) customers, `_recent_events` scans the event
  log once per batch.
- **A partly failing sender.** If the dispatcher raises for some batches and not others, one
  `dispatch.error` alert names the ones that failed and says how many others went through; the old sweep
  sent all or nothing. When every batch fails, the alert is the old one word for word.
- **Still O(customers), in this order of size** (`python -m tests.v3.scale.measure_memory`, below):
  the day's decided-set `_Day` (about 0.5 KB per decision: the decision id and the (goal, customer) pairs
  that make a sweep decide a customer once a day); the ids of a held batch; `TickReport.dispatch` (one
  result and event per send, so it follows sends, which budgets and caps bound, not the audience); the
  agent's per-customer records (`_segments`, `_timezones`, the lift groups); the agent's first-day warm
  start (above); and the ingest side, not touched here: `rebuild_states` holds one state per customer and
  the in-memory event store about 2 KB per event. `ContactHarm.refit` (the nightly 13 KB per customer of
  failure 11) is `autonomy/decide/bidders.py`, not this stream.

## Measured here

`python -m tests.v3.scale.measure_memory`: one SHADOW goal, the states already folded, a decision log that
keeps nothing, a report that keeps no decisions, Python's `tracemalloc` (Python allocations only). "Peak" is
above where the sweep started; "kept" is what it still holds when it ends.

| customers | batch | peak MB | kept MB | peak bytes per customer | seconds |
|---:|---:|---:|---:|---:|---:|
| 2,000 | 100 | 1.50 | 0.90 | 748 | 0.3 |
| 2,000 | whole audience | 8.61 | 1.01 | 4,303 | 0.3 |
| 5,000 | 500 | 5.69 | 2.65 | 1,139 | 0.7 |
| 20,000 | 500 | 13.66 | 10.14 | 683 | 3.3 |
| 20,000 | whole audience | 88.35 | 10.24 | 4,417 | 3.2 |

Reading it: the whole-audience sweep peaks at about 4.4 KB per customer, so the peak is linear in the
audience. With batches of 500 the memory in flight (peak minus kept) is about 3.0 MB at 5,000 customers and
3.5 MB at 20,000, which is the batch; what grows is "kept", about 0.5 KB per customer. The slow test
`test_the_sweeps_peak_memory_follows_the_batch_not_the_customer_count` asserts exactly this at 20,000.
These are not the REAL_DATA_RESULTS figures (those had the full agent policy, full decisions and a larger
state), so they are not extrapolated to 1,000,000.

## To measure 1,000,000 customers (pending CI)

`scripts/v3_scale_sweep.py` writes N synthetic customers into a SQLite state store (batches of 10,000),
starts a warm runtime on it, runs one Shadow sweep of three goals, and prints one JSON line with the
sweep's seconds, decisions per second and the process's peak resident memory before and after. One process
per setting, so each peak is its own. On the CI runner (or any machine with at least 14 GB):

```
python -m scripts.v3_scale_sweep --customers 1000000 --batch 5000 --policy fake
python -m scripts.v3_scale_sweep --customers 1000000 --batch 5000 --policy agent
python -m scripts.v3_scale_sweep --customers 1000000 --batch 0 --policy fake
```

The first is the sweep's own memory, the second is the production policy (it keeps per-customer records of
its own, so expect it to grow with the population), the third is the whole audience in one batch, the
before. Smoke-run only here (500 and 1,000 customers, to see the script works); no 1,000,000 figure exists
yet. `--log sql` records every decision in the SQLite file as well (slow, a few KB a decision on disk).
The sweep batch, `--report-decisions` (1,000 by default) and `--goals` are options.

The pass condition is that the first two complete at 1,000,000 where the old sweep was killed, and that
the first's peak resident memory sits well under the third's. The numbers go in this file when CI has
produced them.
