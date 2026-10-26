# v3 scale: the daily sweep in batches

[`plan-v3.md`](../../plan-v3.md) section 7, "Stream the daily sweep in batches". The cause is
[`REAL_DATA_RESULTS.md`](../v2/REAL_DATA_RESULTS.md) failure 11: the sweep read every customer's
state into one list, decided all of them, and kept every `Decision` and a copy of every state until the
tick ended (about 7.4 KB per decision there). A sweep of 300,000 customers finished; one of 1,000,000
was OOM-killed twice in a shared 14 GB cgroup.

## What changed

All of it is in `autonomy/runtime/service.py`, plus two settings in `autonomy/runtime/settings.py`.

| Before | Now |
|---|---|
| `everyone = [refresh(s) for s in states.scan()]`, taken the first time a goal needed the population and shared by every later goal | Each goal's audience is read from the state store a page at a time (`_members`, a generator) in customer order, through a `_Snapshot` that keeps the old list's contents (below) |
| One `_decide_goal` over all candidates, then one `_act` over all decisions | `_decide_goal` takes `RuntimeConfig.sweep_batch` candidates (default 5,000), picks, enriches, decides, records them, then `_act` sends or holds them; the batch is dropped before the next is read |
| `prepare_day(everyone_list, ctx)` every day a policy has the hook | `prepare_day(view, ctx)`, where `view` (`_StateView`) is a `Sequence` that re-reads the store, through the same `_Snapshot`, each time it is iterated. A policy that loops over it holds one state at a time; one that indexes it reads the population into a list once (the old cost) |
| `TickReport.decisions` held every decision | `RuntimeConfig.report_decisions = N` keeps the first N in full and counts the rest (`decision_count`, `act_count`, and `summary()` use the counts). A runtime made in code keeps them all, as before (`RuntimeConfig.report_decisions = None`). The deployed worker (`build_runtime`) keeps 1,000: `RuntimeSettings.report_decisions`, `AUTONOMY_REPORT_DECISIONS`; the worker's tick job never reads the report back |
| `_assign_groups(None)` listed every state | Streams the scan and writes the changed states back `sweep_batch` at a time |

`sweep_batch=None` is one batch holding the whole audience: the memory behaviour of before (kept so the
two can be compared). The setting is `AUTONOMY_SWEEP_BATCH` for a deployment.

### The snapshot (`_Snapshot`)

The old sweep read every state once, the first time in a tick that a goal needed the whole population
(its `prepare_day`, its sweep or a lapse check), and gave that one list to every later goal. A goal
prepared or swept after another goal had sent therefore saw those customers as they were before the
sends (no new `recent_contacts`, `last_contact_at`, `contacts_7d`), whatever `one_contact_per_tick` was;
a send made earlier in the tick (an event trigger of a goal with nothing to sweep) was already in the
list. Reading the store page by page would show the sends instead, so from that same first point on,
the runtime keeps the state of each customer it is about to send to (the first send if there are
several) and reads the store with those in place of the live ones. Sends before that point are not
kept: the old list already showed them. The memory follows this tick's sends, not the population, and it is
only spent while a later goal will still read the population this tick (with one goal, never).
`test_a_later_goal_prepares_and_sweeps_on_the_population_as_the_tick_first_read_it` and
`test_a_send_before_the_tick_first_reads_the_population_is_seen_by_the_later_sweep` hold both halves.

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
  sees its own goal's contacts. Audience membership is judged on the snapshot (above), in both modes.
- **Alerts.** `decide.error`, `decide.wrong_goal`, `enrich.error`, `dispatch.missing`, `dispatch.error`
  and `dispatch.refused` are each raised once per goal, not once per batch.
- **The enricher's recent events.** The first batch of a goal's pass reads them as the single batch did
  (one scan of the `enrich_lookback` window when it holds more than `ENRICH_PER_CUSTOMER_MAX`, 200,
  customers); the later batches read each customer's through the indexed `for_customer`. Both give the
  same lists, and a pass of many batches costs one window scan, not one per batch
  (`test_a_pass_of_many_batches_scans_the_event_window_once`).
- **`prepare_day`.** The agent's `Segmenter.fit` and `OrganicRate.fit` loop once over the states and keep
  floats, so they run on the lazy view; after the first day they do not loop at all. The
  agent's first-day warm start still builds `{customer_id: state}` for the whole population (it also
  holds a `HistoryDigest` of the whole history, which is the larger part); that is the policy's own
  first-day cost, in `autonomy/runtime/agent.py`, not changed here.

## Proof that the result is the same

`tests/v3/scale/scenario.py` is a small business: three goals at SHADOW, APPROVE and AUTOPILOT (run in id
order: approve, auto, shadow, so the shadow goal prepares and sweeps after the autopilot goal has sent),
customers who differ in consent, history and cart activity, a policy that passes on some customers and
raises for others, a mandate that either caps daily spend (paced) or excludes some customers
(violation), optionally a state enricher, a group assigner and a send-time gateway that refuses a quarter
of the addresses, and four ticks (the sweep, a repeat, a lapse check three hours on, the next morning
with the held batch approved). `fingerprint()` turns everything a run did into plain data: each tick's
`summary()`, the report's decisions in order, the dispatch results, the decision log, every event, the
held batches, the alerts, the stored states, a digest in full of every state `prepare_day` and `decide`
were handed (with the contact fields summed as well), and how many customers each policy decided.

- `tests/v3/scale/test_sweep_batches.py` runs it (60 customers) at batch sizes 1, 7, 25, the default and
  None (the whole audience), with `one_contact_per_tick` on and off and with no mandate, a pacing mandate
  and an excluding mandate, and asserts the fingerprints are equal. It repeats the check on SQL state and
  decision stores (paged in tens), with a group assigner that rewrites every state, and with the gateway
  (equal but for the order of the dispatch results, below). At 60 customers the default (5,000) is a
  single batch; that case only checks that leaving the setting out works.
- Against the code before the change: `python -m tests.v3.scale.fingerprint_matrix` ran the scenario (300
  customers) in every combination: `one_contact_per_tick` on/off x mandate none/paced/violation x state
  enricher off/on x gateway off/on, plus the group assigner with mandate none/paced (26 combinations).
  On a `git archive` of `dd878462` (the parent of this branch) with this `tests/v3/scale/` copied in, with
  the setting left out (`--batches unset`: 26 runs); on this branch at batch unset (the default), None,
  1, 7, 64 and 250 (156 runs). `--compare` against the `dd878462` runs: 130 identical (sha256 of the whole
  fingerprint), 26 differing only in the order of the dispatch results, all of them with the gateway on
  and a goal's pass split into more than one batch (batch 1, 7, 64 and 250). Every run with
  one batch (unset and None) is identical, gateway included. The same matrix on the first version of
  this stream (`62d343e4`, batch None and 7) gave 16 identical of 52: the snapshot blocker the review
  found, which the earlier, coarser fingerprint had missed. To repeat: copy `tests/v3/scale/` into a
  checkout of `dd878462`, run `python -m tests.v3.scale.fingerprint_matrix old.json --batches unset`
  there and `python -m tests.v3.scale.fingerprint_matrix new.json` here (37 s for the 156 runs here),
  then `python -m tests.v3.scale.fingerprint_matrix --compare old.json new.json`. One combination at a
  time: `python -m tests.v3.scale.dump_fingerprint out.json --mandate paced --groups` in each checkout
  (add `--batch 37` on this branch), then `cmp` the two files.

## What is not the same, and what stays linear

- **A scan per goal, and per look at the population.** The old sweep scanned the store once and gave the
  list to every goal. Now each goal's sweep scans for itself (a goal's batches are sent before the next
  goal reads), each goal's lapse check is its own scan (refresh and two audience matches per customer:
  N goals with time filters are N scans an hour, where the old code made one), and `prepare_day` scans
  each time it iterates its view (the agent iterates three times on its first day: `Segmenter.fit`,
  `OrganicRate.fit` and the warm start). Time, not memory; the CI run below has a `--lapse` tick to
  measure the lapse part. Sharing one scan across the goals' lapse checks would be the next step if it
  shows.
- **The enricher runs once per batch.** The demo's enricher (the only one in the repository) works per
  customer, so its output is the same. An enricher that looked across customers would see a batch, not
  the audience.
- **The gateway's results are listed per batch.** `DispatchGateway.dispatch` lists its own refusals and
  deferrals of a call before that call's sends. A pass of several batches is several calls, so
  `TickReport.dispatch.results` (and `sent`, `queued`) interleave them batch by batch where the single
  call listed every refusal first. The results themselves, the events, the alerts and every decision are
  the same (`test_with_a_send_time_gateway_only_the_order_of_the_dispatch_results_follows_the_batch`, and
  the matrix above). Not fixed: the runtime cannot tell the gateway's results from the dispatcher's
  without changing the gateway. With one batch (an audience up to `sweep_batch`) the order is the old one.
  Not covered by the scenario, by reasoning only: the gateway's per-recipient rate limit is read when a
  decision is gated, so two customers who share one address, in different batches, are deferred by the
  gate in the second batch where the single call deferred the second one after trying to send it.
- **A partly failing sender.** If the dispatcher raises for some batches and not others, one
  `dispatch.error` alert names the ones that failed and says how many others went through; the old sweep
  sent all or nothing. When every batch fails, the alert is the old one word for word.
- **Still O(customers), in this order of size** (`python -m tests.v3.scale.measure_memory`, below):
  the day's decided-set `_Day` (about 0.5 KB per decision: the decision id and the (goal, customer) pairs
  that make a sweep decide a customer once a day); the ids of a held batch; the snapshot's pre-send
  states (one per customer sent to this tick, only while a later goal still reads); `TickReport.dispatch` (one
  result and event per send, so it follows sends, which budgets and caps bound, not the audience); the
  agent's per-customer records (`_segments`, `_timezones`, the lift groups); the agent's first-day warm
  start (above); and the ingest side, not touched here: `rebuild_states` holds one state per customer and
  the in-memory event store about 2 KB per event. `ContactHarm.refit` (the nightly 13 KB per customer of
  failure 11) is `autonomy/decide/bidders.py`, not this stream.

## Measured here

`python -m tests.v3.scale.measure_memory`: one SHADOW goal, the states already folded, a decision log that
keeps nothing, Python's `tracemalloc` (Python allocations only). "Report" is how many decisions the tick
report keeps (`report_decisions`; all is the old behaviour). "Peak" is above where the sweep started;
"kept" is what it still holds when it ends (the report's decisions count here).

| customers | batch | report | peak MB | kept MB | peak bytes per customer | seconds |
|---:|---:|---:|---:|---:|---:|---:|
| 2,000 | 100 | 0 | 1.50 | 0.90 | 749 | 0.3 |
| 2,000 | whole audience | 0 | 8.58 | 0.98 | 4,291 | 0.3 |
| 5,000 | 500 | 0 | 5.68 | 2.64 | 1,137 | 0.8 |
| 20,000 | 500 | 0 | 13.66 | 10.14 | 683 | 3.2 |
| 20,000 | whole audience | 0 | 88.32 | 10.21 | 4,416 | 3.2 |
| 20,000 | 5,000 (deployed default) | 1,000 (deployed default) | 41.98 | 12.19 | 2,099 | 3.4 |
| 20,000 | whole audience (before) | all (before) | 88.32 | 49.49 | 4,416 | 3.3 |

Reading it: the whole-audience sweep peaks at about 4.4 KB per customer, so the peak is linear in the
audience. With batches of 500 the memory in flight (peak minus kept) is about 3.0 MB at 5,000 customers and
3.5 MB at 20,000, which is the batch; what grows is "kept", about 0.5 KB per customer. The slow test
`test_the_sweeps_peak_memory_follows_the_batch_not_the_customer_count` asserts exactly this at 20,000. The
last two rows are the deployed worker against the old sweep: at 20,000 customers a batch of 5,000 is a
quarter of the audience, so the peak is about half; what the old sweep kept to the end of the tick (every
decision in the report, 49 MB) drops to 12 MB. `test_a_worker_built_from_the_default_settings_holds_a_fraction_of_the_old_sweep`
(slow) asserts both.
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
python -m scripts.v3_scale_sweep --customers 1000000 --batch 5000 --policy fake --enrich --lapse
```

The first is the sweep's own memory with the deployed defaults (batches of 5,000, a report of 1,000
decisions), the second is the production policy (it keeps per-customer records of its own, so expect it
to grow with the population), the third is the old sweep: the whole audience in one batch and every
decision kept in the report, as before. The fourth adds a state enricher over one recent visit per
customer in a SQL event store (the per-batch event reads) and a lapse-check tick an hour on (one store
scan per goal; `lapse_seconds` in the output). Only Shadow goals run, so the send, hold and mandate paths
are not in these numbers (they are in the equivalence matrix above, at 300 customers). Smoke-run only
here, at 30 customers (`--batch 7 --enrich --lapse --goals 2` and `--batch 0 --goals 1`), to see the
script works; no 1,000,000 figure exists yet. `--log sql` records every decision in the SQLite file as
well (slow, a few KB a decision on disk). The sweep batch, `--report-decisions` (a number or `all`) and
`--goals` are options.

The pass condition is that the first, second and fourth complete at 1,000,000 where the old sweep was
killed, that the first's peak resident memory sits well under the third's, and that the fourth's
`sweep_seconds` stays within a small factor of the first's (the enricher's reads are linear, not one
window scan per batch). The numbers go in this file when CI has produced them.
