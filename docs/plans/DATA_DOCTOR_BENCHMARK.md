# Data Doctor benchmark

Date: 2026-10-27. Harness: `scripts/bench_doctor.py` (roadmap section 3.1, order step 1). Test: `tests/integration/test_bench_doctor.py`.

## What it does

For each library dataset the harness fetches the raw file with the library's own `fetch.py` (into a data dir outside the repo),
then drives the real visit through the API (FastAPI TestClient, fresh data dir, unified Data Doctor on) with a scripted person:
first offered goal (the dataset's own use case when it is offered), the recommended (else first) option for every open
question, yes to every pending proposal, confirmations, a yes to every stop that may be acknowledged, then approval.
It records `reached_ready`, state, the reason when not ready, wall time, peak memory (peak resident set of the dataset's own
process), AI calls and tokens (from the case counters).

```
.venv/bin/python scripts/bench_doctor.py --out-dir bench-out                 # all six, fake grounded AI
.venv/bin/python scripts/bench_doctor.py --only online-retail --local online-retail=/path/to/log.csv
.venv/bin/python scripts/bench_doctor.py --live                                # the configured model
```

The default AI is the fake grounded one. It does not read the data, so these numbers measure the rules, the visit and the
plumbing, not the quality or the cost of a real model (the AI call and token columns are the fake's bookkeeping).

## Result of the run on 2026-10-27 (fake AI, raw files as downloaded)

| Dataset | Rows (file) | Ready | State | Why not | Wall s | Peak MB | AI calls | AI tokens |
|---|---|---|---|---|---|---|---|---|
| uci-bank-marketing | 41,188 | no | approve | hard stop PLAN_STEP_CANNOT_RUN ("'y' is the ID or the outcome") | 293 | 450 | 8 | 57,333 |
| hillstrom-email | 64,000 | no | approve | verdict not_ready: the final whole-file check found stops (codes not recorded by the first harness version) | 173 | 439 | 12 | 82,943 |
| uci-credit-default | 30,000 | no | approve | verdict not_ready: same as Hillstrom | 66 | 467 | 10 | 72,823 |
| telco-customer-churn | 7,043 | no | approve | hard stop PLAN_STEP_CANNOT_RUN ("'Churn' is the ID or the outcome") | 275 | 298 | 8 | 58,209 |
| health-insurance-cross-sell | 381,109 | no result | - | worker killed by the operating system (out of memory) on the two attempts made under load | - | - | - | - |
| online-retail (raw log, 541,909 lines) | 541,909 | no result | - | same: killed on four attempts under load | - | - | - | - |

Wall times were taken while three to four other sessions shared the 16 GB machine and the CPUs, so they are inflated and
noisy (credit default 66 s next to telco 275 s for a file a fifth of the size is that noise, not a finding). Treat the memory
column as the reliable one; treat nothing here as a performance baseline until the harness is rerun alone on a quiet machine.

## What did not work, and why

- **Online Retail and Health Insurance: no recorded result.** The harness ran Online Retail once, alone, to the end (about
  12 minutes of wall time; the case reached `preview` with 10 AI calls and about 68,000 tokens), but that run's record was
  lost to a bug in the first version of the harness (the parent waited on a pipe that never closed after the child had
  finished). The harness now returns the record through a file. Every later attempt on the two large files, and later also on
  the small ones, was killed with signal 9 by the operating system: the machine had about 1.5 GB free because other sessions
  held the rest. The row is therefore "no result", not a claim that either file fails. Rerun both on a quiet machine first.
- **No dataset reached ready.** The four that completed all stopped at approval:
  - `PLAN_STEP_CANNOT_RUN` on bank marketing and telco churn: a step in the plan touches the outcome column and the plan check
    refuses it (not acknowledgeable). It may be an artefact of the scripted person accepting every pending proposal, including a
    recoding of the outcome; it still shows the visit offering a proposal that the plan check then refuses. Needs a look.
  - Hillstrom and credit default end `not_ready` after the final whole-file check finds stops. Approval answers 200 even
    when that check fails, so the first version of the harness never looked at the stops it added. It now re-reads the screen
    after every approval, acknowledges what may be acknowledged and approves again, and records the stops that may not be as
    hard stops. The table above is from the first version, so these two reasons are not yet itemised by stop code; they are
    probably hard stops, not stops the script missed.
- **Goal offered.** Telco churn offered `rca` first, not `telco-churn` (the roadmap's `skip: [telco-churn]` list); the harness
  took the first offered goal there. Hillstrom offered `targeted-advertisement` first, not its own `hillstrom-email` use case.
- **Fetching.** All six files fetched from the network. The canonical Hillstrom URL did not resolve; the fetcher fell back to its
  checksummed mirror, as designed. Criteo uplift has a fetcher but is out of the six named and was not run.
- **Not measured:** the live model (`--live`), and any per-step timing inside the visit.
