# Dataset build performance (Plan A M37 / Phase 2 M14)

The onboarding build (`engine/onboarding/build.py`) turns a client's raw tables into one row per
customer per snapshot date. The Phase 2 target is **200,000 customers, 5,000,000 usage events, 12
snapshots and 60 features in 300 s or less on a laptop**. M14 measured **828.9 s** at that size
(README, "Build performance, measured"): the result was correct but too slow.

This document records what M37 measured, what it changed and what it did not change. The full-size
run is **not** in it. The machine these numbers come from was shared with two other agents, so
M37 measured at one tenth of the size, running the old and new code back to back on the same
tables. The full-size benchmark and the Phase 1 one-million-row test are left to be run on an idle
machine.

## How to measure

```bash
# the target size - on an idle machine
.venv/bin/python -m scripts.bench_onboarding --customers 200000 --usage-rows 5000000 --json full.json

# a before/after pair on identical tables: generate once into --workspace, reuse it for every run
.venv/bin/python -m scripts.bench_onboarding --customers 20000 --usage-rows 500000 \
    --workspace /tmp/bench-20k --json run.json
# add --profile run.prof to see which engine functions the time goes to
```

`scripts/bench_onboarding.py` times one `build_dataset` call. It prints the time of each stage (the
`duration_seconds` the build writes to `build_status.json`), the machine (CPUs this process may
use, RAM and Python), the 1/5/15-minute **load average** at the start and end of the build, and the
**dataset fingerprint**. The load average shows whether other work was running on the machine. The
fingerprint shows whether a faster build produced the same dataset. `--profile` runs the same
build under `cProfile`. The profiler slows Python-heavy code much more than code that runs inside
DuckDB or numpy, so profiled seconds show proportions only. Every result below comes from an
unprofiled run.

For the before/after runs, the old code (`git archive` of the M37 base commit `d37fc72`) was given
the new benchmark script, so both sides used the same measuring code and only the engine differed.

## 1. Before: where the time went

Profile of the old code, 20,000 customers, 500,000 usage rows (six CSVs with 3.67 million rows in
total), 12 snapshots, 60 features, 240,000 rows out. Total 137.7 s under the profiler, with a load
average of about 12 on 4 CPUs.

| What | Calls | Seconds (profiled) | Share |
|---|---:|---:|---:|
| Dataset fingerprint (`ingest.dataset_fingerprint`), computed **twice** | 2 | 42.3 | 31 % |
| Reading the source files (`ingest.read_upload`), **each file read twice** | 12 | 36.2 | 26 % |
| - of which the fingerprint of each source, done on every read | | about 30 | |
| Feature queries in DuckDB, including the leak probe's second full build | 6 | 19.1 | 14 % |
| Source profiles (`profile_source`), at the write stage | 6 | 12.4 | 9 % |
| Phase 1 validation of the assembled dataset (`validate_frame`) | 1 | 12.6 | 9 % |
| Leak probe in total (copy the event tables, rebuild every feature) | 1 | 10.7 | 8 % |
| Mapping transforms (`apply_mapping`) | 6 | 5.2 | 4 % |
| Join coverage, measured 25 times | 25 | 4.5 | 3 % |
| pandas merges (features onto the spine, assembly) | 19 | 3.8 | 3 % |
| **Parquet write (`DataFrame.to_parquet`)** | 1 | **0.65** | **0.5 %** |

One function sits under the two largest rows: `ingest.canonical_chunk_bytes`, the Phase 1
fingerprint canonicaliser. It writes every cell to CSV text, formats floats as `%.17g` and hashes
the result. It ran 84 times, for 57.8 s, which is 42 % of the build. `infer_column_type` ran 360
times (25.4 s), because the source types, the dataset types and the manifest's column types were
each inferred more than once.

The unprofiled stage times for the same build were: `write` 49 s, `validate` 30 s,
`apply_mappings` 18 s and all five feature stages 13 s together (113 s in total, under the same
load).

### The plan's hypothesis, checked

The plan expected Parquet writing and column renaming to be most of the cost, and proposed writing
the dataset with DuckDB `COPY … TO … (FORMAT PARQUET)`, renaming in the SQL projection and joining
the per-role results inside DuckDB. The profile does not support that:

* **Parquet writing took 0.65 s of 137.7 s.** The `write` stage was slow because it fingerprinted
  the dataset twice (`write_frame` and `build_manifest` each computed the fingerprint and only one
  result was used) and because it called `reader.profile(source)` for every source, which read,
  parsed and fingerprinted every file a second time.
* **Renaming costs almost nothing.** `apply_mapping` builds the mapped frame from column
  references. Its 5.2 s went to per-cell Python in the text cast (`str()` on every cell of every
  categorical column).
* **Per-role pandas merges took 3.8 s** in total.

The fixes below follow the profile. §3 records what was tried from the plan's list and why it was
not adopted.

## 2. What M37 changed

Each change returns the same answer as the code it replaces. The dataset, the manifest, the build
report, `sample.json` and `features.sql` are unchanged (§4).

1. **Each source is read once** (`SourceReader.read_profiled`, `read_upload(keep_all_rows=True)`).
   The build's read is the profiling read, kept to every row. It returns the same capped rows and
   the same whole-file fingerprint as `reader.profile` did, so the profile, the PII flags and the
   manifest's source fingerprints are unchanged. The profile is now computed in `apply_mappings`
   instead of `write`. This removes one parse and one fingerprint of every source file. It holds
   for the Setup screen's preview too: a preview that passes its checks reaches `write`, because
   `sample.json` is what it shows and the PII flags decide what `sample.json` redacts. The preview
   used to read every source, then read, fingerprint and profile it again at `write`; it now does
   one profiled read. Only a build or preview that the checks stop before `write` pays for a
   profile it did not pay for before.
2. **The dataset is fingerprinted once**, from column types inferred once. `write_frame` takes the
   `types` the build has already inferred, and `build_manifest` takes the fingerprint that
   `write_frame` returned.
3. **The leak probe no longer rebuilds the whole spine.** The future rows are stacked onto each
   event table inside DuckDB (a `UNION ALL` view, cast to the table's own column types) instead of
   with `pd.concat`, so a five-million-row table is no longer copied to add 500 rows. Features are
   rebuilt only for the snapshot rows of the entities that received a future row, plus up to 500
   entities that received none. Every feature query joins an event to its own entity, so this
   gives the same result as rebuilding every row for every leak the probe was designed to catch.
   The control entities catch a query that reads other entities' events. If no snapshot row
   matches an injected entity, the probe falls back to the whole spine. The build's own features
   are cut to the probed entities by key, not by snapshot-row position: a `derive` feature over an
   event table makes that frame one row per event, and it must still be reported as
   FUTURE_EVENTS_LEAKED, as the whole-spine probe reported it. This is DEC-096, which also has the
   tests.
4. **Text casts without a per-cell Python call.** `cast_series` to a categorical or text type skips
   the `str()` loop for an integer column and for a column whose values are already all strings.
   The output is the same.
5. **Join coverage without the copy.** `join_coverage` compares two integer key columns of the same
   dtype directly, and uses a text column as it is instead of calling `astype(str)` on it. The
   build computes coverage 25 times, and this took it from 4.4 s to 0.2 s under the profiler.

## 3. What was not changed, and why

* **DuckDB `COPY … TO … (FORMAT PARQUET)` for the dataset.** The assembled frame has to be in
  pandas regardless of which writer is used, because the fingerprint, `sample.json` and the Phase
  1 validation all read it there. A `COPY` would therefore replace only the writer, and the writer
  took 0.65 s. Measured on a 240,000 × 80 frame on this machine, `COPY` from the registered frame
  took 5.0 s and `to_parquet` took 2.0 s. A `COPY` of a small mixed-type frame did round-trip to
  the same fingerprint, so correctness was not the obstacle. It would have been slower.
* **Joining per-role feature results inside DuckDB.** All pandas merges together took 1.9 s of
  67.9 s in the final profile. The change would move row-order and dtype questions into the build
  (the fingerprint depends on row order) to save about 3 % of the build. It was also measured
  directly (the table below): one DuckDB query joining a role's results onto the spine in spine
  order, fetched with one `.df()`, returned a frame equal to the pandas path, dtypes included, and
  was no faster.
* **Not materialising a DataFrame per role, and Arrow instead of `.df()`.** Measured inside a real
  build at one tenth of the size (load average about 2), twice per role, on the queries the build
  runs. "Into DuckDB" runs the same query into a temporary DuckDB table, so no pandas frame is made
  at all. It costs the same as the query plus `.df()`: nearly all of a feature stage is DuckDB
  aggregating, not the conversion to pandas. Fetching through Arrow (`.arrow().to_pandas()`) was
  no faster, and it is not the same frame. A nullable integer (`days_since_last_complaint`) came
  back as `float64` instead of `Int64`, and the `DECIMAL` sums of `days_late` came back as Python
  `Decimal` objects instead of `float64`. Either would change the dataset's column types and its
  fingerprint, which are the dataset's identity. The build keeps `.df()` and the pandas merges.

  | Role (features) | query + `.df()` | into DuckDB, no pandas | Arrow + `to_pandas` | pandas merge | DuckDB join, one `.df()` |
  |---|---:|---:|---:|---:|---:|
  | activity (7) | 1.28 / 1.43 | 1.28 / 1.32 | 1.27 / 1.28 | 0.08 / 0.10 | 1.52 / 1.37 |
  | bills (23) | 1.02 / 1.39 | 1.24 / 1.68 | 1.49 / 1.55 | 0.22 / 0.22 | 1.96 / 2.09 |
  | complaints (9) | 0.35 / 0.30 | 0.40 / 0.30 | 0.30 / 0.34 | 0.05 / 0.06 | 0.36 / 0.41 |
  | payments (19) | 1.18 / 0.93 | 1.10 / 1.04 | 1.43 / 1.09 | 0.31 / 0.23 | 1.23 / 1.26 |
  | usage (2) | 0.27 / 0.29 | 0.27 / 0.26 | 0.29 / 0.26 | 0.04 / 0.03 | 0.29 / 0.29 |

  Seconds, first and second attempt, 240,000 spine rows per role. The "DuckDB join" column
  includes the query itself, so it compares with "query + `.df()`" plus "pandas merge".
* **A faster fingerprint canonicaliser.** Pre-formatting float columns in Python before `to_csv`
  gives byte-identical output. On a 100,000 × 64 dataset-shaped chunk it was 5–25 % faster, which
  was measured and is within the noise of this machine. The canonicaliser defines the identity of
  every Phase 1 upload and every dataset, and a small uncertain gain did not justify changing it.
  A bigger gain would come from rendering chunks in parallel processes; that was done afterwards, keeping every fingerprint byte-identical, and measured in §6.

## 4. Results at one tenth of the target size

The same tables (`--workspace`) were built by the old and new code in the order before, after,
after, before, twice. The first set ran under a load average of 20 to 31 on 4 CPUs. The second set
ran under a load average of 3 to 7. The runs were all on the same shared container:
**4 CPUs · 15.7 GiB RAM · Python 3.11.15 · Linux**.

20,000 customers · 500,000 usage rows · 12 snapshots · 60 features (3 dropped as all-null) · 240,000
rows out · `passed=True` in every run.

| Run | Load (1-min, start) | `apply_mappings` | features (5) | `validate` | `write` | **Total** | Peak RSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| before | 31.5 | 18.7 | 17.2 | 40.4 | 52.0 | **132.8 s** | 1,443 MB |
| after | 24.0 | 27.4 | 15.0 | 22.3 | 12.0 | **80.5 s** | 1,134 MB |
| after | 22.7 | 27.3 | 16.6 | 26.5 | 12.0 | **86.1 s** | 1,143 MB |
| before | 20.1 | 17.9 | 16.6 | 40.7 | 47.8 | **126.4 s** | 1,385 MB |
| before | 6.8 | 17.4 | 5.4 | 12.1 | 46.9 | **84.7 s** | 1,482 MB |
| after | 2.9 | 25.7 | 5.6 | 9.3 | 11.3 | **53.8 s** | 1,148 MB |
| after | 4.0 | 25.5 | 6.6 | 6.5 | 11.9 | **52.0 s** | 1,158 MB |
| before | 4.1 | 16.9 | 6.7 | 13.3 | 47.3 | **87.4 s** | 1,484 MB |

On the quieter set the build went from **86.0 s to 52.9 s**, which is **1.63× faster**. On the
loaded set it went from 129.6 s to 83.3 s (1.56×). Peak memory fell by about 300 MB in both sets.
`write` fell from about 47 s to about 12 s. `apply_mappings` rose by about 8.6 s because the
source profiles are now computed there. Before, they were computed at the write stage together
with a second full read of every file.

**The dataset did not change.** Every run printed the dataset fingerprint
`sha256:v1:6af67bf2d92c…` except one run of the old code, which printed `sha256:v1:305892e46ea0…`.
Comparing that run's `dataset.parquet` with the others cell by cell showed identical keys, nulls,
integers and text. One float column differed, by at most 2.8 × 10⁻¹⁶ relative. This is DuckDB's
parallel summation order, which DEC-109 already records: the old code does not always reproduce
its own fingerprint, and the new code does not either (a profiled run of the new code printed
`305892e46ea0…` as well, and matched that run cell for cell). With the float noise excluded, the old
and new builds agree exactly. The build report without its timings, `sample.json` and
`features.sql` were byte-identical across all eleven builds of these tables. `dataset.parquet` and
the manifest were byte-identical in every run whose fingerprint was `6af67bf2d92c…`.

## 5. After: where the time goes now

The final code under the profiler (same tables, load average about 3): 67.9 s.

| What | Calls | Seconds (profiled) |
|---|---:|---:|
| Fingerprint canonicaliser (`canonical_chunk_bytes`): each source once, the dataset once | 42 | 28.2 |
| - dataset fingerprint (`write_frame`) | 1 | 20.3 |
| Reading the sources, one pass each, fingerprint included | 6 | 17.1 |
| Source profiles (`profile_source`) | 6 | 11.2 |
| `infer_column_type` (sources' fingerprint and profile, dataset once) | 196 | 15.5 |
| Phase 1 validation of the dataset (`validate_frame`) | 1 | 7.3 |
| Feature queries (five roles and the probe) | 6 | 5.7 |
| Leak probe in total | 1 | 0.6 |
| pandas merges | 19 | 1.9 |
| Parquet write | 1 | 0.6 |

Most of the remaining time is Phase 1 work that the build reuses: fingerprinting (every cell of
every source and of the dataset rendered as text), profiling and type inference. DuckDB aggregation
is a small part of it.

## 6. The target size, measured

Three builds of the **same tables**, back to back on one idle machine: 4 CPUs · 15.7 GiB RAM ·
Python 3.11.15 · Linux, a container rather than the laptop the plan names. The tables are the
plan's target shape, generated once with `scripts/bench_onboarding.py --customers 200000
--usage-rows 5000000 --workspace <dir>` and reused by every run: 6 CSVs, 1,376.8 MB, 37.7 million
event rows (activity 18.7M, bills 5.0M, usage 5.0M, payments 4.8M, complaints 3.0M). Each build
produces 2,400,000 rows: 200,000 entities × 12 snapshots, 60 features (3 dropped as all-null).

| Code | Build (`build_dataset`) | `apply_mappings` | `validate` | `write` | Peak RSS |
|---|---:|---:|---:|---:|---:|
| Before M37 (`main` @ `d37fc72`) | 763.5 s | 201.5 s | 93.9 s | 371.6 s | 10,863 MB |
| M37 (§2) | 369.1 s | 156.3 s | 17.8 s | 116.9 s | 8,312 MB |
| M37 + parallel rendering (below) | **275.9 s** | 113.1 s | 18.3 s | 63.7 s | 8,300 MB + ~1,000 MB in 3 workers |

**The target (≤ 300 s) is met: 275.9 s, 2.8 times faster than before M37.** The feature queries
(59–62 s in all three runs), snapshots, labels and assembly are unchanged.

* **The "before" run** used the pre-M37 code with this document's benchmark harness (the same
  `build_dataset` calls; the old script cannot reuse a workspace). A 9-second test run shared the
  machine during it, so its time is a few seconds high, not low. The M14 figure of 828.9 s was the
  same code on different, freshly generated tables.
* **Parallel rendering** is the fix §3 left open. `canonical_chunk_bytes` renders each 100,000-row
  chunk to CSV text on one core, and at this size that is about 40 million source rows and the
  2.4-million-row dataset. `_ContentDigest` now renders chunks in a spawned process pool (every core
  but one) and feeds the bytes to its one sha256 in arrival order, so the bytes hashed are the same
  bytes in the same order: every fingerprint is identical by construction. On this dataset,
  fingerprinting took 114.4 s in-process and 56.0 s with the pool.
* **The fingerprint was checked at full size**: the fingerprint the parallel build recorded
  (`sha256:v1:f79d0551…`) equals the in-process fingerprint of the same `dataset.parquet`.
* **The dataset did not change.** Each run's `dataset.parquet` has the same 66 columns, in the
  same order, with the same types; every non-float value is identical and the largest float
  difference is 3.8 × 10⁻¹⁶ relative, the last bit of DuckDB's parallel summation (DEC-109). That
  is why the three fingerprints differ from each other.
* **Peak RSS** is the build process's high-water mark (`ru_maxrss`), as before. The render workers
  are separate processes: sampled every 0.2 s while fingerprinting this dataset, each peaked at
  about 330 MB, 996 MB together. The build's real peak is therefore about 9.3 GB, still below the
  10.9 GB before M37.
* **Where a pool is not started.** A digest reaches for the pool only at its second chunk, so a file
  under 100,000 rows never starts it. A machine with fewer than three cores never starts it. A
  pool that cannot start, or a worker that fails (for example, a parent whose main module a
  spawned child cannot import, such as a script piped in on stdin), falls back to in-process
  rendering with one warning: slower, never different.

What remains on this path is still Phase 1's fingerprint and type inference, now spread over the
cores, and the source reads: `apply_mappings` is 113.1 s of 275.9 s.

## 7. Correctness gates run for M37

The golden aggregation test and the point-in-time tests (`tests/unit/onboarding/test_features.py`),
every `tests/unit/onboarding` test, `tests/unit/test_ingest.py`, and the onboarding integration
tests (`test_onboarding_flow.py`, including the broken-time-bound leak test, `test_api_datasets.py`,
`test_runs_from_dataset.py`, `test_api_clients.py`, `test_api_uploads.py`) pass unchanged. No
existing assertion was edited. Two new test files cover the changes:
`tests/unit/onboarding/test_build_speed.py` checks each shortcut against the long way it replaces,
and checks that the probe's control entities catch a cross-entity leak that the injected entities
alone miss, and that a `derive` feature over an event table is reported as a leak (it once raised
`IndexError` instead). `test_onboarding_flow.py` builds that `derive` recipe end to end and
checks the build fails with FUTURE_EVENTS_LEAKED and writes no dataset. `tests/integration/test_onboarding_build_reads.py` fails if a build reads any source
twice. `tests/unit/test_fingerprint_parallel.py` holds the parallel rendering of §6 to equality with the
in-process fingerprint: on a mixed-type frame of five chunks, with a pool whose workers fail, on
one core, and for a single chunk.
