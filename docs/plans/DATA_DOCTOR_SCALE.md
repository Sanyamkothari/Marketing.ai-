# Data Doctor at scale: the whole-file check

Date: 2026-10-27. Roadmap 2.3 (`DATA_DOCTOR_ROADMAP.md`).

## What changed

* The check of the whole file before Approve (the accepted steps counted on every row, with the guards) is a background
  job (`engine/doctor/full_count.py`, on the `ThreadJobRunner` of `engine/jobs.py`). PREVIEW starts it and rests at
  APPROVE at once with the count of a sample (`PlanCheck.basis` is empty).
* The result is kept next to the case (`cases/<client>/<case>/full_count/<basis>.json`), by the plan's basis (plan digest
  plus the as-of decisions). The same plan is never counted twice; a changed plan starts a new count and stops the old one.
* The screen says where it stands: `view.full_check` is `pending`, `running` (with `steps_done` of `steps_total`), `done`
  or `failed`. The UI polls: each tick asks the visit to continue, which takes a finished result into the case.
* Approve is refused (`409 FULL_CHECK_RUNNING`) until the count of the whole file is in the case. After a failure
  (`FULL_CHECK_FAILED` on the screen) Approve starts the count again.
* A remote job runner (SageMaker) cannot take the closure, so there the count stays inline as before.

* The receipts are cached by step too (`ReceiptMemo` in `engine/doctor/guards.py`, kept by `StoredReceipts` in
  `full_count.py` as `full_count/receipts/<key>.json`). The key is the digest of the table the step read, the step with
  its order, and what the receipt is counted by (ID, outcome, positive value, SQL, date), plus the digests of the other
  tables a query reads. A changed plan counts only the steps from the changed one on; the steps before it take their
  kept receipts. The repeat run of the guards only compares hashes now, so it no longer builds receipts. Receipts hold
  counts, codes and column names only.

Not done: the steps themselves still run on the whole file for every changed plan (only their counting is skipped).
Skipping the run needs the tables kept between ticks, which on a big file is a memory and storage cost this does not
take on. Findings are not cached: they are worked out from the receipts, which is cheap.

## Measurement

`scripts/doctor_scale.py` (run: `PYTHONPATH=. .venv/bin/python scripts/doctor_scale.py <csv>`) reads the file the way
the check does (every row, IDs as text), then counts a simple plan at `Full`: one accepted ID (`CustomerID`), parse
`InvoiceDate`, drop the last column; guards run twice as they do in the product. File: Online Retail, 541,909 rows,
8 columns. Peak memory is the process's `ru_maxrss`.

| | read | plan count | total | peak memory |
|---|---|---|---|---|
| before | 23.1 s | 19.9 s | 43.0 s | 956 MB |
| after | 16.2 s | 7.3 s | 23.5 s | 701 MB |

(One quiet-machine run each; the shared machine was busy afterwards, so later wall-clock times are not comparable.)

Hotspots found with cProfile, and what was done:

1. Date parsing: `format="mixed"` reads each distinct spelling with dateutil, about 8 s per parse and the guards run the
   steps twice. `engine/agent/formats.py` now reads the spellings that fit the first one's format in one pass and only
   the rest one at a time. Safe because the format is used only when it reads day and month in the order the step asks
   for, so each spelling gets the answer it would get alone (`tests/unit/test_dates_fast_parse.py`).
2. Guards: `_key_values` and `_rows_without` (`engine/doctor/guards.py`) turned a 541k-row ID column into text and
   stripped it two or three times per receipt; now once, and not at all for a numeric column. Plan count 19.9 s to 7.3 s
   together with 1.
3. Ingest type inference (`engine/stages/ingest.py`, `_datetime_parse_rate`) parsed every row; it now parses each
   distinct spelling once. Read 23.1 s to 16.2 s.

Still the biggest: reading the CSV with the content fingerprint and the type inference (about 16 s, 350 MB), then
`make_receipt` string work in the guards (about 2 s per receipt). Next steps if needed: infer types on a sample of rows,
and hash the fingerprint in a thread beside the parse. Files over about 2M rows are the "large table" row of the
roadmap (chunked or DuckDB) and are not covered here.
