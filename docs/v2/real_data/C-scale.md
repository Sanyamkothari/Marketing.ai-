# Family C: event streams and scale

Written 20 Oct 2026 on branch `v2/realdata`, from runs on this machine (4 CPUs, 16 GB RAM, no swap,
Python 3.13 with numpy 2.5 / pandas 3.0 / SQLAlchemy 2.0 / SQLite 3.45). **No product code was
changed.** Every number below comes from a JSON file in `/home/user/datasets/_results/c/`, written by
the runner named next to it; `c_report.py` prints the scale tables from those files. The machine was
shared with the family-A runner and the Gym the whole time (load average 3 to 5 on 4 CPUs, and
another job holding 6 to 9 GB of RAM for most of it), so **wall times are upper bounds**. The
out-of-memory kills below are by the kernel's **memory cgroup** (`dmesg`: "Memory cgroup out of
memory"), and that cgroup (16 GB) is shared by every runner on the machine: a kill means "did not fit
beside what else was running", not by itself "needs more than 16 GB". Where a kill happened, the run
was repeated later on the same machine when more memory was free, and both are reported.

**Skeptical review (second pass, same day).** The headline numbers were re-run (commands in each
section; re-runs on the same, still shared machine were 1.1x to 1.5x slower in wall time, with
identical event, state and decision counts and the same peak RSS to within 1%). The review found and
corrected: (1) an **adapter bug**: RetailRocket transactions were mapped one purchase per *item
line*, so "2,576 bought twice or more" was wrong (1,063 have two or more transactions); fixed with
`adapt_retailrocket(grain="order")`; (2) the sweep's "peak RSS" included the nightly step that runs
after it; (3) the `data_start` finding was explained wrongly at 10k (the same mis-set `data_start`
is there too); (4) the 1M out-of-memory results were worded as if the software alone needs more
than 16 GB, which was not measured; (5) a few numbers had no stored result (the `ContactHarm`
profile, the SQLite probe), so they now come from new runners (`c_harm.py --profile`,
`c_sqlscan.py`). Details are marked "Review:" below.

```bash
# event types, what is used and ignored, the three event-stream files (needs ~10 GB for all rows)
python3 -P scripts/v2_realdata/c_event_types.py                 # all 2.76M RetailRocket rows
python3 -P scripts/v2_realdata/c_event_types.py --rows 300000   # a quicker trial run (same shape)
# ingestion + state rebuild, in-memory and SQLite, one fresh process per size
python3 -P scripts/v2_realdata/c_ingest.py --curve 10000,100000,1000000,all --backend memory,sqlite
# one daily sweep of the real runtime + nightly learning, synthetic customers from Online Retail II
python3 -P scripts/v2_realdata/c_sweep.py --curve 10000,100000 --nightly --history
python3 -P scripts/v2_realdata/c_sweep.py --customers 300000 --nightly
python3 -P scripts/v2_realdata/c_sweep.py --customers 1000000 --nightly          # killed twice, see 5
python3 -P scripts/v2_realdata/c_sweep.py --customers 10000 --nightly --history --profile
python3 -P scripts/v2_realdata/c_sweep.py --customers 10000 --nightly --history --backend sqlite
python3 -P scripts/v2_realdata/c_sweep.py --customers 100000 --nightly --history --backend sqlite
# ContactHarm.refit on its own, to find where it breaks
python3 -P scripts/v2_realdata/c_harm.py --curve 10000,100000,300000,600000,1000000
python3 -P scripts/v2_realdata/c_harm.py --customers 100000 --profile           # where refit spends time
# added in review: transaction grain, the SQLite plan + fix, where ContactHarm.data_start lands
python3 -P scripts/v2_realdata/c_event_types.py --tx-counts                     # light, pandas only
python3 -P scripts/v2_realdata/c_event_types.py --rows 300000                   # now one purchase per transaction
python3 -P scripts/v2_realdata/c_sqlscan.py --events 400000
python3 -P scripts/v2_realdata/c_harm_start.py --customers 2000                 # also --history, and 10000 --history
python3 -P scripts/v2_realdata/c_report.py                       # the tables below
python3 -m pytest tests/v2/realdata/test_family_c.py -q          # 4 tests, inline data, ~2 s
```

## The answer on one screen

- **What it reads.** Of the three event streams, only **RetailRocket** became events, and only
  through an adapter: its epoch-millisecond times are refused (`bad_timestamp` on every row) and its
  transactions carry no amount (`invalid_event: a purchase event needs a value`). **Taobao** has no
  time column at all (`MappingError`). **REES46** parquet cannot be opened here (no pyarrow); its
  CSV form needs an explicit time format (` UTC` suffix) and has a `remove_from_cart` type Autonomy
  does not have.
- **What it uses from a view / cart / purchase stream.** Purchases drive everything. A **view** or
  **cart** only moves `last_seen_at` (and `first_seen_at`, `version`); a cart in the last day also
  triggers a same-tick decision. Item ids on views and carts, sessions, prices on views, and view
  counts are dropped. 99.2% of RetailRocket's 1.41M visitors end up as `prospect` states holding
  nothing but a first and last seen date.
  The cart-recovery template's audience is "visited in the last 3 days", so on RetailRocket's last
  days **96% of the people a cart-recovery goal would decide for had never added anything to a cart**.
- **How far it scales** (first bottleneck -> breaking point, exact functions):
  1. **Ingestion is linear and memory-bound in memory**: about 2 KB per in-memory `Event` and
     3 KB per `CustomerState`; all of RetailRocket (2.76M events, 1.41M customers) rebuilt in
     123 s at a **10.3 GB** peak. `map_frame` and `StateFolder.fold` (pydantic `model_copy` per
     event) are the CPU cost, about 33 µs and 45 µs per event.
  2. **SQLite is quadratic.** `SqlEventStore.scan` (`autonomy/store/sql.py`) pages with a keyset
     `OR` condition that SQLite, with bound parameters, plans as a **full index scan on every page**:
     scanning 100k events takes 2.9 s, 1M take 114 s, 2.76M take 709 s. `rebuild_states` and every
     nightly `scan(since=...)` inherit it. Appends run at 6,600 to 9,600 events a second, slowing as the table grows.
  3. **The daily sweep** runs at **890 to 1,220 decisions a second** (one process; 3 goals, about
     1.27 decisions per customer), roughly linear: 100k customers in 142 s, 300k in 313 s. Memory is
     the breaker: about **7.4 KB per decision** stays in RAM for the whole tick
     (`AutonomyRuntime._decide_goal` appends every `Decision` to `TickReport.decisions`, the
     in-memory log holds them too, and `_decide` keeps a refreshed copy of every state in `everyone`).
     **The 1M-customer run did not complete: the shared memory cgroup killed it twice, both times
     after the states were loaded and before the tick finished** (at 11.5 GB and at 9.7 GB resident,
     with other runners' jobs in the same cgroup; their sizes were not logged). A straight-line
     projection from the 300k run (not a measurement) puts the tick alone near 12 GB, which might fit
     an otherwise idle 16 GB machine; the tick plus the nightly step in one process would not. What is
     established is the growth (about 7.4 KB per decision held for the whole tick), not the exact
     breaking size.
  4. **Nightly learning**: its memory breaker is **`ContactHarm.refit`** (`autonomy/decide/bidders.py`):
     dense `n x 241` float64 grids, about 13 KB of peak memory per customer with a contact record
     (10k: 1.6 s, 100k: 22.8 s, 300k: 68.8 s, 600k: 152 s at 8.5 GB; 15x from 10k to 100k, then
     linear). In time it is the largest single function but not most of the step: in the profiled
     10k nightly it is 1.3 s of 4.8 s, inside `learn_contact_harm` (2.3 s, which also builds the
     evidence); larger nightly steps were not profiled. **1M records did not complete: killed twice by
     the shared cgroup** (at 13.4 GB and at 12.3 GB resident); a straight line through the 600k run
     puts it near 14 GB, so it is unlikely to fit 16 GB even alone (projection). Inside the real
     nightly step: 2.3 s at 10k customers, 42 s at 100k, 135 s at 300k (process peak 7.0 GB).
  So on this shared 16 GB machine the in-memory runtime completed 300k customers in one process
  (7.0 GB with the nightly step) and did not complete 1M; the exact limit in between was not found.
  The SQLite path reaches tens of minutes per rebuild from about a million events (549 s at 1M,
  2,454 s at 2.76M) until the scan is fixed.
- **A learning finding found on the way** (not a scale issue): `learn_contact_harm` fixes each
  goal's `data_start` from the first purchase in the events it is handed, which for a win-back goal
  falls *after* the agent's start; sends before that moment are never counted as exposure. Checked
  with `c_harm_start.py`: at 2,000 customers the win-back model counted 45 sends where its goal made
  851, and read "about 100% of customers put off" on outcomes where messages do nothing; at 10,000 it
  counted 235 where its goal made 4,451 (the reading happened not to flip). Mechanism confirmed in the
  code (section 6).

## 1. Datasets: what worked

| Dataset | Rows / customers | Result | What it took |
|---|---|---|---|
| **RetailRocket** events (`retailrocket/retailrocket.zip`) | 2,756,101 rows (2,664,312 views, 69,332 carts, 22,457 transactions), 1,407,580 visitors | **Worked with an adapter.** 2,755,641 events (460 exact repeats dropped as `duplicate_row`), 1,407,580 states | Time from epoch ms (`pd.to_datetime(unit="ms", utc=True)`), a 0.0 placeholder amount on every transaction (labelled `attrs.value_placeholder`), a row key of the four columns. Revenue is 0 for every buyer. Review: a transaction is one row **per item**, so the first adapter made 22,457 purchase events from 17,672 transactions; `grain="order"` now collapses them (section 2). |
| **Taobao** subset (`taobao_sample/`) | 1,952,931 rows (1,548,162 pv, 193,747 cart, 211,022 buy), 48,749 users | **Failed**: `MappingError: the table has no column 'time'; it has 'uid', 'sid', 'behaviour'` | Cannot be done honestly: an `Event` needs a time and the file has none. Inventing one would invent recency, segments and every window. |
| **REES46** parquet (`rees46_sample/*.parquet.snappy`, 2 x 100k rows) | not readable | **Failed**: `ImportError: Unable to find a usable engine; tried using: 'pyarrow', 'fastparquet'` | Needs pyarrow (a heavy install, not done). |
| **REES46** CSV sample (`rees46_csv_tiny/`, 169 rows, downloaded for this step from `delta-incubator/delta-lake-definitive-guide`) | 169 rows (168 view, 1 cart) | **Worked with a format**: ISO parsing gave `bad_timestamp` on all 169 rows (`2019-10-01 00:00:00 UTC`); `timestamp_format="%Y-%m-%d %H:%M:%S UTC"` mapped all 169. A `remove_from_cart` row (inline, one row: the sample has none) is `unknown_event_type`. | Too small for scale; kept only for the format checks. |
| Criteo (13.98M rows) and X5 (45.8M lines) | | Not used here: Criteo has no events or customers over time (one row per person), so it says nothing about streams; X5's ingestion curve is in `B-transactions.md` section 6 (linear, 4.7 GB at 100k clients). | |

Wanted but not reachable for this family: REES46 full months (`kaggle.com` blocked; GitHub code
search found only samples of under 14 KB), Taobao UserBehavior with timestamps
(`tianchi.aliyun.com` blocked), Open Bandit full (`research.zozo.com` blocked).

## 2. Event types: used, ignored, refused

From `c_event_types.py` on all 2,756,101 RetailRocket rows (`event_types.json`; the 300k-row trial
run, `event_types_300000.json`, has the same shape) and a grep of `autonomy/` (outside the Gym and fakes) for each type:

| Event | Read by | What it changes |
|---|---|---|
| `purchase` | folder, catalogue, learning (windows, lift, value, contact harm), runtime, proof | everything: purchases, revenue, margin, last purchase, segment, categories, the organic rate |
| `cart` | `data/folder.py` (last seen), `runtime/service.py` (`TRIGGER_TYPES`, `INGEST_TYPES`) | `last_seen_at`; a cart inside `trigger_window` (1 day) gets the customer an event decision this tick |
| `view` | `data/folder.py`, `runtime/service.py` (`INGEST_TYPES`, staleness only) | `last_seen_at` only. **Not** a `visit`: the contact-harm clues (`learning/observations.py`) count `visit`s, so a view stream feeds no clue |
| `visit` | folder, contact-harm evidence ("direct visits"), demo | `last_seen_at`, and a clue for who is put off by messages |
| item id on a view or cart | nothing | dropped (the catalogue reads purchases only) |
| price on a view (REES46) | nothing | dropped (`value` is only kept on purchase / refund) |
| session id, category on a view | nothing | dropped |
| `remove_from_cart`, `fav` (Taobao) | refused | `unknown_event_type` unless mapped to something else |

All rows (1,407,580 visitors): 1,368,715 only ever viewed, 37,722 added to a cart and 27,146 of
those never bought; 11,719 bought, **1,063 in two or more transactions**. State fields set: `last_seen_at` and
`first_seen_at` for all 1,407,580; `purchases` / `last_purchase_at` for 11,719; `revenue`,
`top_categories`, channels, hours, country and time zone for none (no amounts, no categories on
transactions, no consent, no messages, no location).

**Review: an adapter bug in the first pass.** RetailRocket writes one `transaction` row per item, all
sharing a `transactionid` (1,788 transaction ids even span several timestamps). The first adapter
mapped every row to a purchase, so `purchases` counted item lines: 22,457 purchase events for 17,672
transactions, and "2,576 bought twice or more" counted visitors with two or more *lines*; 1,063 have
two or more transactions (`c_event_types.py --tx-counts`, `tx_counts.json`). The product's own
online-retail loader already says one event per invoice is what customer state wants, so this was
the adapter's fault, not the software's. Segments shift with it: on the first 300k rows,
`active` falls from 282 to 117 and `new` rises from 1,000 to 1,165 (`event_types_300000.json` at line
grain against `event_types_300000_order.json`); the cart-trigger numbers are identical. The full-file
`event_types.json` (segments `new` 5,859, `lapsing` 4,178, `active` 1,682) was made at line grain and
was **not re-run** at order grain (it needs about 10 GB, and less was free during the review), so its
segment counts are overstated for `active`. `c_event_types.py` now uses `grain="order"`; the ingest
timings in section 3 stay at line grain (0.17% more events, no effect on the scale conclusions).

**The cart trigger in the real runtime** (`c_event_types.py` section 3; the visitors active in
RetailRocket's last 3 days: 23,864, with all 101,145 of their events, e-mail consent invented, one
cart-recovery goal from the Strategist template, Shadow, one tick at 23:59 UTC on 18 Sep 2015, the
day the data ends at 02:59): 36 cart events in the 1-day trigger window from 25 customers gave **25
event decisions**, and the daily sweep decided **13,657** more customers, of whom **13,129 (96%) had
only ever viewed**. 7,625 got a send decision (Shadow: logged, not sent). The 300k-row trial gave the same picture (23,022 of
23,753, 97%). Cause: the template audience is
`last_seen_at days_since_lt 3` ("Recent visitors stand in for cart abandoners until customer state
carries carts", `autonomy/agents/strategist.py`), and `CustomerState` has no cart field. Fix:
`autonomy/data/folder.py` + `autonomy/contracts/customer.py`: add `last_cart_at` (set on `cart`,
cleared by a later `purchase`) and point the cart-recovery template's filter at it.

## 3. Ingestion and state rebuild (RetailRocket)

First N rows of `events.csv` in file order (not time order, so a customer's history can be partial
below "all"). Item-line grain (one purchase event per transaction row; see section 2). Review re-runs:
100k memory 10.3 s / 462 MB, 100k SQLite 45.1 s (scan 3.9 s) / 398 MB, 1M memory 80.8 s / 3,826 MB;
same event and state counts. `memory` = the in-memory stores; `sqlite` = file-backed SQLite with default settings,
rows read, mapped and appended in chunks of 100k (events never all in memory).

`c_report.py` output (seconds; scan = a plain `scan()` of the log before the rebuild):

| rows | backend | events | states | map_frame s | append s | scan s | rebuild s | total s | peak RSS MB | DB MB |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | memory | 10,000 | 6,957 | 0.233 | 0.007 | - | 0.233 | 0.77 | 122 | - |
| 10,000 | sqlite | 10,000 | 6,957 | 0.24 | 1.04 | 0.198 | 1.025 | 3.06 | 137 | 10.2 |
| 100,000 | memory | 99,992 | 54,913 | 2.76 | 0.136 | - | 2.895 | 6.4 | 462 | - |
| 100,000 | sqlite | 99,992 | 54,913 | 2.75 | 11.74 | 2.862 | 10.819 | 30.68 | 398 | 90.9 |
| 1,000,000 | memory | 999,818 | 529,792 | 29.27 | 2.362 | - | 34.211 | 72.54 | 3,826 | - |
| 1,000,000 | sqlite | 999,818 | 529,792 | 31.71 | 136.38 | 113.914 | 247.796 | 548.55 | 1,504 | 897.6 |
| 2,756,101 | memory | 2,755,641 | 1,407,580 | 92.04 | 5.632 | - | 123.449 | 239.48 | 10,312 | - |
| 2,756,101 | sqlite | 2,755,641 | 1,407,580 | 90.81 | 419.74 | 709.309 | 1179.933 | 2453.6 | 3,545 | 2437.7 |

- **In memory**, time and memory are linear: about 33 µs per event in `map_frame` and 45 µs per
  event in `rebuild_states`; about 2.0 KB per `Event` and 3.0 KB per `CustomerState` at peak (all
  rows: 6.0 GB after mapping, 10.3 GB at the end of the rebuild). The rebuild holds every state in a
  dict before writing (`rebuild_states`, "one entry per customer").
- **Profile of the fold** (10k-customer sweep, cProfile, so slower than plain): `StateFolder.fold`
  took 55 µs per event (3.97 s for 71,644 events), much of it pydantic `model_copy`: each event
  copies the state at least twice (`fold`'s update, then `refresh`), and the tick made 213,914
  copies in all.

## 4. SQLite store behaviour

- **The keyset scan is quadratic.** Per 100k events scanned, a probe log of 400k synthetic events
  took 2.1 s, 3.7 s, 5.3 s and 6.8 s (later pages are slower; `c_sqlscan.py`, `sqlscan_400000.json`;
  the first pass quoted 2.8 / 4.0 / 6.3 / 8.2 s from a probe that left no result file). The SQL the
  store sends with its bound parameters,
  `WHERE ts > ? OR ts = ? AND (seq > ? OR seq = ? AND event_id > ?) ORDER BY ts, seq, event_id LIMIT ?`,
  is planned as `SCAN event USING INDEX ix_event_ts_seq` (walk the index from the start, filter
  every row); the same query with literal values is `SEARCH ... (ts>?)`. Each page therefore costs
  in proportion to its position. The test `test_finding_sqlite_event_scan_pages_are_full_index_scans`
  pins this. **Fix** (`autonomy/store/sql.py`, `SqlEventStore.scan` and `SqlDecisionLog.scan`): add
  a leading `t.ts >= ts` conjunct to the keyset condition (or use a row-value
  `tuple_(ts, seq, event_id) > tuple_(...)`), which SQLite plans as `SEARCH`. Measured on the raw
  SQL (no pydantic) over the same 400k events: paging with the store's query 13.8 s, with the
  leading `ts >= ?` 0.8 s (`c_sqlscan.py`; product code unchanged, the fixed query is only run
  there). `SqlDecisionLog.scan` uses the same `OR` keyset and gets the same `SCAN` plan (checked with
  `EXPLAIN QUERY PLAN`, not timed). A `scan(since=...)` (the nightly path) gets `SEARCH` from
  `since`, but every page still re-walks from `since`, so it is quadratic in the window, not the
  whole log. Postgres was not measured here.
- **Appends**: 9,600 events a second at 10k, 8,500 at 100k, 7,300 at 1M (136 s), 6,600 for all rows
  (420 s); each
  call is one transaction in 128-row statements (`_chunk_size`: 900 bound parameters / 7 columns).
- **On disk**: about 900 bytes per event after a rebuild (1M events: 526 MB of events, 898 MB with
  530k states; all rows 2.76M events and 1.41M states: 2,438 MB, 885 bytes per event).
- **Memory stays low for events** (peak 0.5 GB while appending all 2.76M) but `rebuild_states`
  still holds every state in memory (1.5 GB at 530k states, 3.5 GB at 1.41M).
- **Point reads are fine**: `for_customer` 0.7 ms and `states.get` 0.4 ms each at 1M events.
- **The runtime on SQLite**: a 10k-customer first day (71,644 history events, rebuild + sweep +
  nightly) took 21.8 s for the tick and 6.4 s for the nightly step against 11.7 s and 2.3 s in
  memory. At 100k customers (727,979 history events): appending the history took 112 s, the tick 317 s (401 decisions a second against 892 in memory), the nightly step 90 s against 42 s; peak RSS 4.1 GB (the tick still holds every decision in memory); database 887 MB.

## 5. The daily sweep

`c_sweep.py`: synthetic customers, each a copy of a random real **Online Retail II** customer (5,881
customers: order count, revenue, first and last order dates), dates shifted to end yesterday,
+-3 days jitter; **invented**: e-mail consent for everyone, margin = 35% of revenue. Three goals from
the Strategist's templates (win-back, repeat purchase, cross-sell; e-mail; no offer, 10% off, free
shipping), Shadow, the production 10% / 10% split, one `tick` at 10:00 UTC (which, being the first
tick of the day, also runs the nightly rebuild and the nightly learning on what it just decided).
`--history` puts each customer's consent and orders in the event log so the tick rebuilds state
from events (the realistic first day); without it states are written directly (to reach larger N).

| customers | backend | history in log | decisions | tick s | decisions/s | peak RSS MB at end of tick | nightly s | process peak RSS MB (incl. nightly) |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 2,000 | memory | False | 2,537 | 1.893 | 1340.2 | 132 | 0.31 | 156 |
| 2,000 | memory | True | 2,537 | 2.16 | 1174.5 | 163 | 0.468 | 180 |
| 10,000 | memory | True | 12,705 | 11.679 | 1087.8 | 378 | 2.318 | 460 |
| 10,000 | sqlite | True | 12,705 | 21.748 | 584.2 | 357 | 6.444 | 510 |
| 100,000 | memory | True | 126,899 | 142.272 | 891.9 | 2,838 | 42.266 | 3,647 |
| 100,000 | sqlite | True | 126,899 | 316.713 | 400.7 | 2,527 | 90.006 | 4,067 |
| 300,000 | memory | False | 380,326 | 312.698 | 1216.3 | 3,621 | 134.876 | 7,038 |
| 1,000,000 | memory | False | - | did not complete | - | - | - | killed by the shared cgroup at 11,552 and 9,724 resident, both before the tick finished |

Review: the first pass's single "peak RSS" column was the process peak *after* the nightly step, so
"100k: 142 s (3.6 GB)" overstated the sweep; the tick itself peaked at 2.8 GB. Decisions per second
compare runs with and without history in the log (the 300k run has none), so the range is loose.
Review re-run: `--customers 10000 --nightly --history` 16.4 s tick, 3.6 s nightly, 460 MB, the same
12,705 decisions; `--customers 2000 --nightly` (with and without `--history`) the same 2,537.

- **Throughput** is 890 to 1,220 decisions a second in one process and about linear; 1.27
  decisions per customer (three goals, audiences overlap). There is no single hot spot: per
  decision 0.8 to 1.1 ms of pure Python spread over `AgentPolicy.decide` -> `AutonomousPolicy.decide`
  (`autonomy/decide/policy.py`), `GoalBidder.quote` / `_raw_options`, the auction,
  `choose_send_time` / `candidate_times` (up to 25 hourly candidates built per decision),
  `thompson_with_floor` and two `seeded_rng` calls (a fresh numpy `Generator` each, about 85 µs
  each under the profiler). Profile: `sweep_10000_memory_history_profile.json`.
- **Memory is the breaking point.** At 300k customers the sweep went from 0.8 GB to 3.6 GB: about
  7.4 KB per decision in all, held for the whole tick: the decisions themselves (`TickReport.decisions`,
  the in-memory decision log, `_Day` ids) and a refreshed copy of every state (`_decide` builds
  `everyone = [self.folder.refresh(s, now) for s in self.states.scan()]`); 7.4 KB is the whole
  growth of the tick divided by its decisions, not the size of one `Decision`. **The 1M-customer run
  did not complete, twice**: the shared memory cgroup killed it after the states were loaded and
  before the tick finished, at 11.5 GB resident (`dmesg`: "Memory cgroup out of memory: Killed
  process 27807 ... anon-rss:11551820kB"; the first pass says another runner held about 6 GB, which
  was not logged) and on the retry at 9.7 GB (`Killed process 20379 ... anon-rss:9724444kB`; 13.7 GB
  was available when it started, then other runners' jobs started beside it; log
  `sweep_1m_retry.log`). A straight line from the 300k run (not measured: 2.4 GB of states at 1M plus
  7.4 KB x about 1.27M decisions) puts the 1M tick near 12 GB, which might fit an idle 16 GB machine;
  the nightly step after it in the same process (it added 2.6 GB at 300k) would not. So "1M does not
  fit" is a projection for the tick plus nightly, and an observation only for this shared machine.
  **Fix**: `autonomy/runtime/service.py` `_decide`: stream the sweep in batches of customers (scan,
  decide, `decisions.record`, drop) instead of materialising `everyone` and every decision; keep
  only counts and the ACT decisions in `TickReport`.
- What it decided (these are invented customers; the decisions are not a result): at 300k, 285,654
  agent-group ACT (all with no offer), 18,583 agent DO_NOTHING, 37,818 holdout and 38,271 baseline
  DO_NOTHING (the production baseline group is never contacted, B failure 5); every decision has a
  probability in (0, 1].

## 6. Nightly learning

Measured two ways: inside the real nightly step (`AutonomyRuntime._nightly`, after appending
**synthetic outcome events for timing only**: a send and a deliver per agent ACT decision, a click
for 3%, purchases at each customer's own rate, +20% after a click), and `ContactHarm.refit` alone on
synthetic records (`c_harm.py`).

| customers | nightly step (s) | process peak RSS after it (MB) | of which `update_models` / `learn_contact_harm` / `ContactHarm.refit` (10k profile) |
|---:|---:|---:|---|
| 10,000 | 2.3 | 460 | 2.8 s / 2.3 s / 1.3 s (nested) of 4.8 s profiled |
| 100,000 | 42.3 | 3,647 | |
| 300,000 | 134.9 | 7,038 | |

`ContactHarm.refit` alone (`c_harm.py`, synthetic records; peak RSS of the process):

| customers | refit s | peak RSS MB | extra peak MB |
|---:|---:|---:|---:|
| 10,000 | 1.564 | 207 | 152 |
| 100,000 | 22.754 | 1,476 | 1,339 |
| 300,000 | 68.81 | 4,252 | 3,950 |
| 600,000 | 151.681 | 8,487 | 7,941 |
| 1,000,000 | killed after 111 s and again after 100 s | 13,447 and 12,281 at the kill | |

- **`ContactHarm.refit` is the memory breaking point of the nightly loop.** At 100k records
  (`c_harm.py --customers 100000 --profile`, `harm_100000_profile.json`, 20.8 s profiled):
  `_normal_on_grid` 9.5 s (10 calls: once per entry of the 9 `HARM_TAUS` plus the final posterior,
  each an `n x 241` exp), `refit`'s own array maths 5.4 s, `_neighbours` 3.4 s (an `n x 241`
  cumulative sum and posterior; 5.5 s in a second profiled run, so treat these as rough). The first
  pass quoted 9.3 / 6.7 / 7.0 s of 25.3 s from a profile it did not store. Review re-runs without
  the profiler: 100k 18.6 s / 1,476 MB, 300k 64.1 s / 4,270 MB. Memory is about 13 KB per
  customer at peak (several `n x 241` float64 arrays alive at once: `ll_grid`, `lik`, `cum`, `score`,
  `post`, `pri * lik`). 600k records took 152 s at an 8.5 GB peak. **At 1M records the process was
  killed twice by the shared cgroup**, after 111 s at 13.4 GB (another job resident) and after 100 s
  at 12.3 GB. (The first pass said 13.7 GB was available when the retry started; the `free` reading
  in `harm_600k_1m_retry.log` was taken after the kill, so that is not shown.) A straight line through
  600k gives about 14 GB at 1M, so it would likely not fit 16 GB even alone; that is a projection.
  Fix:
  `autonomy/decide/bidders.py` `ContactHarm.refit`: choose `tau` on a fixed random subsample (say
  50k), then compute the per-person posteriors in chunks of about 50k rows; or keep only a few
  moments per person instead of the 241-point grid.
- Each goal has its own policy and so its own `ContactHarm` (the runtime builds one policy per goal),
  so a customer in three audiences is fitted three times.
- **`data_start` after the agent's start (correctness, not scale).** On the 2,000-customer run the
  win-back goal's harm model said "About 100% of customers look put off by messages ... they buy at
  least 55% less", on synthetic outcomes where messages do nothing. Mechanism, checked with
  `c_harm_start.py` (results in `harm_start/`): `learn_contact_harm` (`autonomy/learning/update.py`)
  sets each goal's `harm.data_start` once, from the first purchase in the events it is handed (only
  that goal's decided customers, only the last 30 days, `nightly_lookback`). At the launch tick the
  win-back customers have no purchase in that window, so `data_start` stays unset; at the next
  nightly it is set to the first purchase *after* the launch (2,000 customers: 10:01:00, one minute
  after the 10:00 start; 10,000: 10:03:28). `contact_evidence` (`autonomy/learning/observations.py`)
  then starts every record at `max(first event, data_start)` and drops what came before, including
  the sends, most of which are at 10:00: the win-back evidence counted 45 sends where the goal made
  851 (2,000 customers, 322 exposed days against 12,733 open days) and 235 where it made 4,451
  (10,000 customers). **Review:** the first pass guessed that at 10k "some win-back customer had a
  purchase in the window before the start"; that was wrong: `data_start` is after the start at 10k
  too, and the model read "no sign" on about 5% of its exposure. With states written directly (no
  history in the log, as in the 2,000 and 300,000 runs) all three goals' `data_start` falls after the
  start (at 2,000 all three read "about 100% put off"; at 300,000 all three read "no sign", which here
  only says the reading did not flip). Repeat-purchase and cross-sell with history in the log get a
  `data_start` a month before the start and count nearly all sends. So the defect is systematic for
  win-back; whether its headline reading flips depends on how much exposure survives. A live
  win-back goal would hit the same path: by definition its audience bought nothing in the 30 days
  before launch. Fix: `autonomy/learning/update.py` `learn_contact_harm`: take `data_start` from the
  business's full history (the earliest `first_seen_at` in state, passed in by the runtime) and never
  let it fall after `split_at`; and in `autonomy/runtime/service.py` `_learn` hand a new policy its
  customers' pre-start history (B failure 3).
- Nothing else about learning quality can be read from these runs: the outcomes are invented.

## 7. Failures, causes and the fix I would make

1. **Epoch-millisecond times** (RetailRocket): `bad_timestamp: cannot read the timestamp as ISO 8601`
   on every row. Fix: `autonomy/data/mapping.py` `EventMapping`: a `timestamp_unit` option
   (`s` / `ms`) read with `datetime.fromtimestamp(x / 1000, UTC)`.
2. **A purchase without an amount**: `invalid_event: Value error, a purchase event needs a value`
   on every transaction. A click-stream export rarely carries order amounts. Fix:
   `autonomy/contracts/events.py`: allow a purchase with `value=None` flagged as amount-unknown, and
   have the value model skip it, rather than forcing adapters to write a fake 0.
3. **No time column** (Taobao): `MappingError`. Correct behaviour; nothing to fix in the product.
4. **Parquet**: `ImportError` (no pyarrow) in the customer's own `pd.read_parquet`; Autonomy never
   reads files itself. Fix: none in the light core; say in `docs/v2/real_data/INPUTS.md` and in the Data
   Doctor's error for a non-CSV upload that parquet must be exported as CSV first.
5. **` UTC` suffix** (REES46): ISO parsing refuses `2019-10-01 00:00:00 UTC`. Fix:
   `autonomy/data/mapping.py` `_timestamp`: also strip a trailing ` UTC` / ` GMT` before
   `fromisoformat`.
6. **Event types with no Autonomy word** (`remove_from_cart`, Taobao `fav`): `unknown_event_type`.
   Fix: `autonomy/data/mapping.py`: an `ignore_types` list so a customer can drop them on purpose
   without counting them as problems.
7. **Views and carts barely used; cart recovery targets viewers** (section 2). Fix: a `last_cart_at`
   state field (`autonomy/contracts/customer.py`, `autonomy/data/folder.py`) and the template filter
   on it (`autonomy/agents/strategist.py`); count `view` as a visit in
   `autonomy/learning/observations.py` (or tell the customer to map page views to `visit`).
8. **SQLite keyset scan is quadratic** (section 4). Fix in `autonomy/store/sql.py` as given there.
9. **The sweep holds every decision and a copy of every state** (section 5). Fix in
   `autonomy/runtime/service.py` `_decide` / `_decide_goal` as given there.
10. **`ContactHarm.refit` memory** (section 6). Fix in `autonomy/decide/bidders.py` as given there.
11. **`ContactHarm` `data_start` after the start** (section 6). Fix in `autonomy/learning/update.py`.
12. **The fold copies each state twice per event** (`StateFolder.fold` -> `refresh`, pydantic
    `model_copy`). Fix: `autonomy/data/folder.py`: fold into a mutable working copy per customer
    during `rebuild_states` and validate once at the end; it is the cost of every rebuild.

## 8. What a customer would need to do

- Write an adapter (pandas) for an event-stream export: convert epoch times, add an amount to
  purchases (join their orders table; RetailRocket's has none), choose what views mean (`view` keeps
  only "last seen"; `visit` also feeds the harm clues), drop event types Autonomy has no word for,
  and add consent events (none of these files has any; without them nobody is contactable).
- Load through Python, not the API: the Data Doctor takes one CSV of up to 20 MB (RetailRocket's
  `events.csv` is 94 MB).
- Size the machine: in memory, plan on about 5 KB per event plus state during a rebuild, and about
  10 KB per customer in a daily sweep plus 13 KB per customer with a contact record in the nightly
  harm refit; or use SQLite only up to about a million events until the scan is fixed. Postgres is
  the production store and was not measured here.
- Not use the cart-recovery template as is: its audience is everyone seen in the last 3 days.

## 9. Files

- `scripts/v2_realdata/c_common.py`: timing, peak RSS, JSON output, SQLite engine
- `scripts/v2_realdata/c_adapters.py`: RetailRocket, Taobao and REES46 readers and mappings
- `scripts/v2_realdata/c_event_types.py`: event-type handling, the three files, the cart trigger
- `scripts/v2_realdata/c_ingest.py`: ingestion and rebuild curve, memory and SQLite
- `scripts/v2_realdata/c_sweep.py`: daily sweep and nightly learning at N synthetic customers
- `scripts/v2_realdata/c_harm.py`: `ContactHarm.refit` alone at N records
- `scripts/v2_realdata/c_report.py`: prints the tables in this note from the JSON results
- `scripts/v2_realdata/c_sqlscan.py` (review): the SQLite keyset plan, per-page cost and the fixed query
- `scripts/v2_realdata/c_harm_start.py` (review): where each goal's `ContactHarm.data_start` lands
- `tests/v2/realdata/test_family_c.py`: four tests on inline data (one added in review: one purchase
  per transaction id at `grain="order"`)
- Results: `/home/user/datasets/_results/c/{ingest,sweep,harm,event_types,sqlscan,harm_start}/*.json`; logs in
  `/home/user/datasets/_results/c/logs/`; the Online Retail II customer pool in
  `/home/user/datasets/_results/c/pool_online_retail_ii.csv`
- New data: `/home/user/datasets/rees46_csv_tiny/` (169 rows, with `SOURCE.txt`)
