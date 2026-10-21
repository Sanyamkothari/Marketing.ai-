# Stream: realfix (the real-data fix wave)

21 Oct 2026, branch `v2/realfix`. The stress test in [REAL_DATA_RESULTS.md](../REAL_DATA_RESULTS.md)
showed that the agent did not beat simple policies on real randomised campaigns, valued a message
about 10x too high, started cold despite history, could not use customer features, and that file
mapping went silently wrong. This wave fixed what it could and measured the result with one command,
the real-data scoreboard ([REAL_BENCH.md](../REAL_BENCH.md)). Every number below is from a run in
this wave; the commands are in REAL_BENCH.md.

## What was built

| Part | Files | What it does |
|---|---|---|
| Scoreboard | `scripts/v2_realbench/` (`run`, `common`, `adapters`, `fetch`), `.github/workflows/realbench.yml` | Hillstrom, X5 (100k clients), Criteo (300k rows), MegaFon (200k rows); split by customer-id hash; agent built and warm-started through the product path on TRAIN only; baselines fitted on TRAIN; IPS/SNIPS/DR on TEST with paired intervals; verdict flags per target. Adapters hold every dataset-specific line. `fetch` downloads with SHA-256 checks. CI runs it on `[real]` pushes to `version-2`. |
| Calibrated value | `autonomy/learning/calibration.py`, `autonomy/runtime/agent.py` (`_calibrate`) | Finds randomised past campaigns in the history under an events-only convention (below), measures each arm's extra money per recipient against the left-out pool (CUPED), and sets the lift per click, or per message on a channel that logs no clicks. |
| History reaches the agent | `autonomy/runtime/service.py`, `agent.py` (`nightly`, `HistoryDigest`) | The runtime streams every customer's whole history to a new policy in slices before its first decision and calls `prepare_day` once a day. Outbound events are kept whole; purchases and consent go into a compact digest. |
| No-click channels | `agent.py` (`ClickResponseModel.no_click`, `LiftPerClick` SEND unit) | A channel with 200+ delivered sends and no click is valued per message from purchases (X5's SMS, MegaFon). |
| Thresholds, harm, consent, baseline | `autonomy/data/folder.py`, `autonomy/learning/update.py`, `autonomy/decide/policy.py`, `autonomy/runtime/settings.py` | VIP bar from the shop's own top 10%; the contact-harm model's `data_start` from the whole history; `no_consent` / `unsubscribed` / `no_address` in `blocked_by`; `RuleBaseline` for the production baseline group. |
| Customer-level uplift | `autonomy/uplift/` (`score`, `features`, `campaign`, `learner`, `model`, `warm`), `autonomy/decide/bidders.py`, `autonomy/bridges/uplift_bridge.py` | Learns from the latest closed past campaign how much a message moves each customer (features from `CustomerState.preferences` plus behaviour), cross-fitted, balance-checked and calibrated out of sample. `GoalBidder` blends the score with the response model by precision, never more than one score sd from the measurement. The v1 AutoML learners plug in behind `UpliftLearner`. |
| Safe mapping | `autonomy/data/mapping.py`, `autonomy/data/rebuild.py`, `api/v2/engines/doctor.py`, `autonomy/bridges/doctor_bridge.py` | Mapping settles an ambiguity from the file, from an explicit option, or asks (`MappingQuestionsError`); in library mode it reports the rows. Content-based event ids, placeholder and scientific-notation ids, day/month order, decimal separators, currencies (`MixedCurrencyError` on rebuild), duplicates, date bounds. The Doctor refuses files it would misread and adds 14 checks. |
| Scale | `autonomy/store/sql.py`, `schema.py`, `memory.py` | SQL keyset scans start the index range at the last time (`ix_event_order`, Postgres copies in collation C); `create_all` adds missing indexes to existing databases. `CompactEventStore`: a columnar in-memory `EventStore`. |

## Wiring done in the finishing step

* **The uplift model is attached by the agent itself.** `AgentPolicy._warm_start` calls
  `warm_start_uplift` after calibration (`policy.uplift` keeps its notes). The history it needs is
  rebuilt from the compact digest: `HistoryDigest.as_events` returns the kept outbound events plus
  `DigestEvent` records (purchases, refunds, consent changes, first sighting), about 0.9 us and a
  tenth of an `Event`'s memory each. The uplift code reads events through a small protocol,
  `autonomy.uplift.features.EventLike`, which `Event` and `DigestEvent` both satisfy. Before this
  step the scoreboard gains needed a scratch shim; now they are product behaviour.
* **Bench adapters book window purchases inside the window.** X5, Criteo and MegaFon have no
  purchase time; version 1 of the bench booked them at exactly +7 days, just outside the product's
  half-open 7-day learning window, so no learner saw them. Version 2 books them at +3.5 days
  (`WINDOW_PURCHASE_AFTER`). The BEFORE code gives identical numbers on both versions.
* **The scoreboard reports what the agent learned** (`agent.learned`: calibration and uplift notes)
  and whether the expected value lies inside the measured interval
  (`calibration_expected_inside_measured_ci`).
* **Tests that pinned old bugs now pin the fixes**: `tests/v2/backbone/test_loaders.py` (content
  ids), `tests/v2/realdata/test_family_d.py` (re-export and placeholder ids),
  `tests/v2/realdata/test_family_c.py` (SQLite scan plan is `SEARCH`),
  `tests/v2/engines/test_ledger_doctor_api.py` (the route now asks for a zone).
* `scripts/v2_realdata/d_messy_after.py` is the messy-file runner of record for AFTER;
  `scripts/v2_realdata/c_sqlscan.py` is marked historical (it patches the pre-fix SQL).

## Results

Real data (scoreboard, AFTER vs BEFORE, agent minus the best simple policy chosen on train; 95%
paired intervals, not corrected for multiple comparisons: suggestive):

| Dataset | BEFORE | AFTER | Calibration BEFORE -> AFTER |
|---|---|---|---|
| Hillstrom | -0.0182 [-0.0369, +0.0004] | -0.0253 [-0.0476, -0.0030] (loses to send-all men's: exploration) | 11x -> 1.46x |
| X5 | -1.75 [-3.24, -0.25], contacts nobody | -0.10 [-0.55, +0.35], ties | nothing -> 1.33x |
| Criteo | -0.0189 [-0.0221, -0.0158] | +0.0018 [-0.0012, +0.0048], ties the f8 rule; +0.0209 over send-to-all | 48x -> 0.48x |
| MegaFon | -0.1007 [-0.1723, -0.0291], contacts nobody | **+0.463 [+0.395, +0.531], beats it** | nothing -> 0.84x |

Gym spot-checks, `python -m autonomy.gym.run --scenario S --policy agent --seeds 2 --customers 1000`
(mean true value; BEFORE is `f55c57b3`):

| Scenario | BEFORE | AFTER (fix wave) | Stage |
|---|---|---|---|
| baseline_shop | $10,999.7 (Stage 3) | $10,709.7 (seeds $11,260 / $10,159; 88% of the segment oracle; +$749 over the best fixed rule) | 3 -> 3 |
| whatsapp_fatigue | $12,849.4 (Stage 3) | $12,726.5 (102% of the segment oracle; +$3,535 over the best fixed rule) | 3 -> 3 |
| sleeping_dogs_15 | $8,290.3 (Stage 2: contacts 95% of sleeping dogs, as send-to-all) | $8,049.3 (Stage 2, same miss; 87% of the segment oracle) | 2 -> 2 |

The baseline_shop and whatsapp_fatigue BEFORE figures are the brain builder's runs of `f55c57b3`;
sleeping_dogs_15 BEFORE was run from a `git archive f55c57b3` snapshot in this step. No stage
regressed. The small losses are the no-control click shrink (`HISTORY_CLICK_TRUST`, below): the
Gym's history is targeted, so neither calibration nor the uplift model is used there.

Messy files (`python3 -P scripts/v2_realdata/d_messy_after.py`, 43 cases from real rows): silent
wrong results went from 20 to 2 (F2 and D4; Z1's one remaining silent path is a mapping that states `timezone='UTC'` for New York times, the customer's own statement, not counted). The two left are outside the mapping: F2 (pandas
reads only an Excel file's first sheet; the Doctor route refuses workbooks by name) and D4 (the
built-in Online Retail loader sums lines into invoices before mapping sees them).

Scale: SQLite full scan of 1M RetailRocket events 103.6 s -> 14.6 s; 400k synthetic events 17.5 s
-> 4.7 s with flat time per page; Postgres 16 (200k) 6.4 s -> 2.8 s. `CompactEventStore` holds about
3x less per event than the fake on 1M real rows (peak RSS 3.8 -> 2.5 GB, because states then
dominate).

## Conventions other streams should know

* **A past campaign's control group, events only.** Imported history never names the people a
  campaign left out. Calibration reads a campaign as randomised, and the customers it could have
  reached but did not as its control, only when the sends carry `logged_probability` below 1, that
  probability is constant within each arm, the left-out pool is within 20% of the size the design
  implies, and the two groups bought alike before it. The uplift model uses the same idea (customers
  known before, reachable on the channel, sent nothing around it) and refuses a fit when the sent
  and left-out groups differ. A holdout handed over as `Group.HOLDOUT` / NOTHING decisions before the
  first decision is used as given. A targeted campaign (like the Gym's history) is refused with a
  sentence in `policy.calibration["not_used"]` or `policy.uplift.notes`.
* **Customer features** travel as `CustomerState.preferences` (column -> text). No contract change.
* **`HISTORY_CLICK_TRUST = 0.5`**: without a usable control, the history's click rate moves the
  value scale only halfway. It costs about $320 mean true value on Gym `baseline_shop` (Stage 3
  holds); 1.0 restores the earlier Gym numbers exactly. Not tuned on any dataset; kept as the brief
  asked ("shrink click-implied uplift strongly without a control").
* **Breaking changes**: `/doctor/map-events` asks by default (422 with `questions`) and defaults
  `not_after` to the upload time; mappings without `row_key` get content ids (a file loaded before
  and after this change is stored twice unless re-keyed); `rebuild_states` raises
  `MixedCurrencyError` on a log with two currency codes; opening an existing database builds the
  new order indexes once (a write lock on Postgres while it runs).

## Gaps

* Hillstrom fails "not worse than send-to-all" by a suggestive 0.025 per customer: exploration
  (1.7% floor rests, 3.1% women's e-mails by Thompson sampling on a z of about 1.7). Not tuned away.
* Criteo ties but does not beat the one-feature f8 rule, and its calibration is 0.48x (just outside
  2x, under-predicting the 12% it contacts).
* Peak memory of the full scoreboard rose from 4.7 to 4.97 GB (X5); a CI runner needs about 5 GB.
* The uplift model uses only the latest closed campaign (arms of 100+ customers); the response model
  and the score come from the same campaign, so the blend counts some evidence twice (bounded by
  one score sd). Arms sharing one control are pooled as if independent in calibration (se
  understated, mean unaffected).
* A rebuilt policy re-reads the whole event store, and its warm start treats the agent's own older
  sends as history (the no-control shrink then applies). `HistoryDigest._seen` and
  `bundle.learned_purchases` grow with history.
* Mapping: the time zone still never reaches `CustomerState.timezone`; the file check samples 50,000
  distinct cells; the incremental `apply_events` path does not check currencies;
  `autonomy/data/__init__.py` does not re-export the new names.
* Store: nothing uses `CompactEventStore` yet; the full 2.76M-row RetailRocket runs were not
  repeated; Postgres numbers were measured once on a throwaway cluster.
* The Hillstrom loader still writes `attrs["probability"]` (a test pins it); the bench adds
  `logged_probability` itself. `--with-decision-log` and the Hillstrom $0.25-cost scenario were not
  run. Real data is one-shot campaigns: the agent's weeks-long learning is not exercised.
* The CI workflow's hosts were checked from this environment, not from a GitHub runner.
