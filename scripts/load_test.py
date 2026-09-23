"""Load-test the API: N concurrent users, a realistic request mix, and what the job queue does (M52).

Why this script exists
----------------------
Phase 4b plan M52 asks for "a load test: concurrent users, concurrent jobs, queueing behaviour".
Nothing had ever put more than one request at a time through the API. The questions it answers are
the ones an operator asks before sizing a deployment: how slow does the API get when twenty people
use it at once, does anything fail, and what happens when more scoring runs are submitted than
`Settings.job_max_workers` can run - are they queued and eventually run, or refused, or lost?

What is real and what is stubbed
--------------------------------
**Everything on the request path is real.** The app is `api.main.create_app(data_dir=<tmp>)`,
served by one uvicorn worker on a loopback socket in a spawned child process, and driven over real
HTTP by one `httpx.Client` per simulated user, each on its own thread of this process. So every
request pays for routing, the Phase 4b access dependency and the audit middleware (which writes one
event per mutating request into `platform.db`), the real ingest on `POST /uploads`, and the real
`validate_against_schema` plus the run directory and `job_spec.json` on `POST /runs`.

**The job body is not.** `SyntheticJobRunner` wraps the same `ThreadJobRunner` the API uses, with
the same `max_workers`, and replaces each submitted closure with a fixed-length wait. M52's
question is about the API and the queue, not AutoGluon: a scoring job's own cost depends on the
model and the row count, is measured by `scripts/bench_large_file.py` on a laptop, and is measured
on SageMaker at M50. A real job here would make the numbers a benchmark of predict, and would need
a trained model in every run of the fast suite. The consequence worth knowing: `status.json` of a
run started here stays `pending`, because the stub never writes it; reading it costs the same.

The scoring model is seeded, not trained: a champion `ModelVersion` row and its `FeatureSchema` are
written into the temporary store before the load starts, which is all `POST /runs` reads.

Laptop numbers, not AWS ones
----------------------------
Every report says where it ran (`target`, `machine`) and carries `caveat`. The local target is
one uvicorn worker on loopback with SQLite and the local filesystem; a deployment is Fargate behind
an ALB with Postgres and S3, and the SageMaker runner does not queue in-process at all - it hands
each job to SageMaker, whose own concurrency quota is the queue. These numbers therefore bound the
API's own overhead and prove the thread runner queues rather than refuses; they are not a capacity
figure for AWS. `--base-url` runs the same mix against a deployment for that, on account day.

Starting runs against a deployment is off by default (`--include-runs` turns it on) because there
each scoring run is a SageMaker job, which is billed; a load test must not spend money by default.

Run it with::

    .venv/bin/python -m scripts.load_test                      # 20 users x 50 requests, local
    .venv/bin/python -m scripts.load_test --users 50 --max-workers 4 --job-seconds 1
    .venv/bin/python -m scripts.load_test --base-url https://<alb> --token <bearer>   # account day

The report is written to `reports/load/<UTC date>.json` (or `--output`).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import multiprocessing
import os
import platform
import random
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

from engine import __version__
from engine.config import ColumnRole, ColumnType, Metric, ProblemType, load_use_case
from engine.contracts import FeatureSchema, FeatureSchemaColumn, ModelStatus, ModelVersion, RunState
from engine.jobs import CancelToken, JobFn, JobInfo, JobRunner, ThreadJobRunner
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

COMMAND: Final[str] = "python -m scripts.load_test"

REPORT_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "reports" / "load"
"""Where a report lands by default: one JSON document per UTC day, beside the other `reports/`."""

REPORT_SCHEMA_VERSION: Final[int] = 1

USE_CASE: Final[str] = "targeted-advertisement"
"""The demo use case: its config ships in `configs/` and every integration test of `/runs` uses it."""

FEATURE_COLUMN: Final[str] = "tenure_months"
"""The one feature the seeded schema declares, so an upload carrying it validates cleanly."""

SEEDED_MODEL_ID: Final[str] = f"m_{USE_CASE}_1"

Action = Literal["list_runs", "read_run", "upload", "start_score_run"]

ACTIONS: Final[tuple[Action, ...]] = ("list_runs", "read_run", "upload", "start_score_run")

EXPECTED_STATUS: Final[Mapping[Action, int]] = {
    "list_runs": 200,
    "read_run": 200,
    "upload": 201,
    "start_score_run": 202,
}
"""The one status each action succeeds with. Anything else - a 409, a 503, a timeout - is an error."""

DEFAULT_MIX: Final[Mapping[Action, int]] = {
    "list_runs": 35,
    "read_run": 35,
    "upload": 15,
    "start_score_run": 15,
}
"""Relative weights. Reads dominate, as they do on the Running and Results screens, which poll;
an upload is usually followed by a run, so the two share the rest equally."""

CAVEAT: Final[str] = (
    "Laptop numbers, not AWS ones: one uvicorn worker on loopback, SQLite metadata and the local "
    "filesystem, with a synthetic fixed-length job body. They bound the API's own overhead and show "
    "how the thread runner queues; they are not a capacity figure for Fargate, RDS, S3 or SageMaker."
)
REMOTE_CAVEAT: Final[str] = (
    "Measured from this machine against a deployment, so every latency includes the network path "
    "between them. Scoring runs are real jobs there; queue behaviour is SageMaker's, not reported here."
)

DEFAULT_USERS: Final[int] = 20
DEFAULT_REQUESTS_PER_USER: Final[int] = 50
DEFAULT_JOB_SECONDS: Final[float] = 1.0
DEFAULT_ROWS_PER_UPLOAD: Final[int] = 200
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
SERVER_START_TIMEOUT_SECONDS: Final[float] = 15.0
DRAIN_TIMEOUT_SECONDS: Final[float] = 600.0
QUEUED_AFTER_SECONDS: Final[float] = 0.010
"""A job that waited this long for a worker was queued; a thread handover takes well under it."""


# ---------------------------------------------------------------------------
# The profile and the report
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LoadProfile:
    """What one load test does. Every field is on the report, so a number can always be reproduced."""

    users: int = DEFAULT_USERS
    requests_per_user: int = DEFAULT_REQUESTS_PER_USER
    max_workers: int = 2
    job_seconds: float = DEFAULT_JOB_SECONDS
    rows_per_upload: int = DEFAULT_ROWS_PER_UPLOAD
    think_seconds: float = 0.0
    seed: int = 7
    mix: Mapping[Action, int] = field(default_factory=lambda: dict(DEFAULT_MIX))

    def __post_init__(self) -> None:
        if self.users < 1 or self.requests_per_user < 1 or self.max_workers < 1:
            raise ValueError("users, requests_per_user and max_workers must each be at least 1.")
        if self.job_seconds < 0 or self.think_seconds < 0 or self.rows_per_upload < 1:
            raise ValueError("job_seconds and think_seconds must be >= 0 and rows_per_upload >= 1.")
        unknown = set(self.mix) - set(ACTIONS)
        if unknown or not any(weight > 0 for weight in self.mix.values()):
            raise ValueError(f"mix must weight at least one of {ACTIONS}; unknown: {sorted(unknown)}.")


class ProfileRecord(BaseModel):
    """`LoadProfile` as it is written into the report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    users: int = Field(description="Concurrent simulated users, one thread and one HTTP client each.")
    requests_per_user: int = Field(description="Requests each user sends, back to back.")
    max_workers: int = Field(description="The job runner's worker threads (Settings.job_max_workers).")
    job_seconds: float = Field(description="How long the synthetic job body holds a worker.")
    rows_per_upload: int = Field(description="Data rows in each uploaded CSV.")
    think_seconds: float = Field(description="Pause between one user's requests; 0 is closed-loop.")
    seed: int = Field(description="Seed of every user's action choice.")
    mix: dict[str, int] = Field(description="Relative weight of each action.")


class LatencyStats(BaseModel):
    """Latency and outcome of one action, or of all of them together."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int = Field(description="Requests sent.")
    errors: int = Field(description="Requests that did not answer the expected status, or raised.")
    error_rate: float = Field(description="errors / requests; 0.0 when nothing was sent.")
    p50_ms: float | None = Field(description="Median latency (nearest rank), milliseconds.")
    p95_ms: float | None = Field(description="95th percentile latency (nearest rank), milliseconds.")
    p99_ms: float | None = Field(description="99th percentile latency (nearest rank), milliseconds.")
    max_ms: float | None = Field(description="Slowest request, milliseconds.")
    mean_ms: float | None = Field(description="Mean latency, milliseconds.")
    statuses: dict[str, int] = Field(description="Count per HTTP status, or per exception class.")
    error_codes: dict[str, int] = Field(description="Count per error-envelope `code` of the failures.")


class QueueStats(BaseModel):
    """What the job runner did with the scoring runs the load submitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_workers: int = Field(description="Worker threads the runner had.")
    job_seconds: float = Field(description="Length of each synthetic job.")
    submitted: int = Field(description="Jobs the API submitted to the runner.")
    finished: int = Field(description="Jobs that reached a terminal state before the drain timeout.")
    failed: int = Field(description="Jobs that ended failed or cancelled.")
    peak_running: int = Field(description="Most jobs running at once; must never exceed max_workers.")
    peak_waiting: int = Field(description="Most queued jobs (submitted, not started) at any instant.")
    queued_jobs: int = Field(description="Jobs that waited at least QUEUED_AFTER_SECONDS to start.")
    wait_p50_ms: float | None = Field(description="Median time from submit to start, milliseconds.")
    wait_p95_ms: float | None = Field(description="95th percentile time from submit to start, ms.")
    wait_max_ms: float | None = Field(description="Longest time from submit to start, milliseconds.")
    drain_seconds: float = Field(description="From the last request answered to the last job finished.")
    drained: bool = Field(description="True when every submitted job finished before the timeout.")


class LoadTestReport(BaseModel):
    """One load test: what was run, where, and what it measured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=REPORT_SCHEMA_VERSION, description="Version of this document.")
    generated_at: datetime = Field(description="UTC time the load finished.")
    engine_version: str = Field(description="The engine version under test.")
    command: str = Field(description="How to run it again.")
    target: str = Field(description="What was loaded: the local app, or a deployment's URL.")
    machine: str = Field(description="The machine the numbers belong to.")
    caveat: str = Field(description="What these numbers are not.")
    profile: ProfileRecord = Field(description="The load that was applied.")
    wall_seconds: float = Field(description="From the first request sent to the last answered.")
    throughput_rps: float = Field(description="Requests answered per second of wall time.")
    overall: LatencyStats = Field(description="Every request together.")
    by_action: dict[str, LatencyStats] = Field(description="Each action on its own.")
    queue: QueueStats | None = Field(description="The job queue; None when it cannot be observed.")
    notes: tuple[str, ...] = Field(description="Anything a reader needs to interpret the numbers.")


class Sample(NamedTuple):
    """One request, as the client saw it."""

    action: Action
    status: str
    seconds: float
    ok: bool
    error_code: str | None


# ---------------------------------------------------------------------------
# The synthetic job runner
# ---------------------------------------------------------------------------
class SyntheticJobRunner:
    """`ThreadJobRunner` with the same worker count, running a fixed-length wait for every job.

    The closure the API submits is dropped (see the module docstring for why). The wait polls the
    job's `CancelToken`, so `POST /runs/{id}/cancel` behaves exactly as it does for a real job. The
    runner counts how many jobs run at once, because "never more than `max_workers`" is the half of
    queueing behaviour that timestamps alone cannot prove.
    """

    def __init__(self, *, max_workers: int, job_seconds: float) -> None:
        self._inner = ThreadJobRunner(max_workers=max_workers)
        self.job_seconds = job_seconds
        self._lock = threading.Lock()
        self._running = 0
        self._submitted: list[str] = []
        self.peak_running = 0

    def submit(self, job_id: str, fn: JobFn) -> JobInfo:
        """Queue the synthetic job under `job_id`; `fn` is deliberately not called."""
        del fn
        info = self._inner.submit(job_id, self._job)
        with self._lock:
            self._submitted.append(job_id)
        return info

    def status(self, job_id: str) -> JobInfo:
        """The inner runner's answer."""
        return self._inner.status(job_id)

    def cancel(self, job_id: str) -> bool:
        """The inner runner's answer."""
        return self._inner.cancel(job_id)

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut the inner runner down."""
        self._inner.shutdown(wait=wait)

    def submitted(self) -> tuple[str, ...]:
        """Every job id submitted so far, in submission order."""
        with self._lock:
            return tuple(self._submitted)

    def drain(self, timeout: float) -> bool:
        """Wait until every submitted job has finished; False when `timeout` ran out first."""
        deadline = time.monotonic() + timeout
        for job_id in self.submitted():
            remaining = deadline - time.monotonic()
            try:
                self._inner.wait(job_id, max(remaining, 0.0))
            except TimeoutError:
                return False
        return True

    def infos(self) -> tuple[JobInfo, ...]:
        """The state of every submitted job."""
        return tuple(self._inner.status(job_id) for job_id in self.submitted())

    def _job(self, token: CancelToken) -> None:
        with self._lock:
            self._running += 1
            self.peak_running = max(self.peak_running, self._running)
        try:
            token.wait(self.job_seconds)
            token.raise_if_cancelled()
        finally:
            with self._lock:
                self._running -= 1


_: type[JobRunner] = SyntheticJobRunner
"""`SyntheticJobRunner` must satisfy the protocol `api.deps.get_jobs` returns; mypy checks this line."""


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value at least `pct` percent of the values are <= to.

    Nearest rank rather than interpolation so that every reported number is a latency some request
    really had; with the few hundred samples a load test takes, interpolating would invent one.
    """
    if not values:
        return None
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100].")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 3)


def latency_stats(samples: Sequence[Sample]) -> LatencyStats:
    """Summarise `samples` into one `LatencyStats`."""
    seconds = [sample.seconds for sample in samples]
    errors = [sample for sample in samples if not sample.ok]
    return LatencyStats(
        requests=len(samples),
        errors=len(errors),
        error_rate=round(len(errors) / len(samples), 6) if samples else 0.0,
        p50_ms=_ms(percentile(seconds, 50)),
        p95_ms=_ms(percentile(seconds, 95)),
        p99_ms=_ms(percentile(seconds, 99)),
        max_ms=_ms(max(seconds)) if seconds else None,
        mean_ms=_ms(sum(seconds) / len(seconds)) if seconds else None,
        statuses=dict(sorted(Counter(sample.status for sample in samples).items())),
        error_codes=dict(sorted(Counter(sample.error_code or sample.status for sample in errors).items())),
    )


def queue_stats(
    runner: SyntheticJobRunner, *, max_workers: int, drain_seconds: float, drained: bool
) -> QueueStats:
    """What the runner did, from each job's own submit/start/end timestamps.

    A job counts as *queued* when it waited at least `QUEUED_AFTER_SECONDS` between submit and
    start. Every job waits a little - the pool has to hand it to a thread - and counting that
    would report a queue on an idle runner. `peak_waiting` is a sweep over the queued jobs only:
    +1 at submit, -1 when the job left the queue (its start, or its end if it was cancelled first).
    """
    infos = runner.infos()
    waits: list[float] = []
    events: list[tuple[datetime, int]] = []
    for info in infos:
        left_queue = info.started_at or info.ended_at
        if info.started_at is not None:
            waits.append((info.started_at - info.submitted_at).total_seconds())
        if left_queue is not None and (left_queue - info.submitted_at).total_seconds() < QUEUED_AFTER_SECONDS:
            continue
        events.append((info.submitted_at, 1))
        if left_queue is not None:
            events.append((left_queue, -1))
    waiting = peak_waiting = 0
    for _at, delta in sorted(events):  # at equal instants -1 sorts first: a handover is not a wait
        waiting += delta
        peak_waiting = max(peak_waiting, waiting)
    terminal = [info for info in infos if info.ended_at is not None]
    return QueueStats(
        max_workers=max_workers,
        job_seconds=runner.job_seconds,
        submitted=len(infos),
        finished=len(terminal),
        failed=sum(1 for info in terminal if info.state is not RunState.DONE),
        peak_running=runner.peak_running,
        peak_waiting=peak_waiting,
        queued_jobs=sum(1 for wait in waits if wait >= QUEUED_AFTER_SECONDS),
        wait_p50_ms=_ms(percentile(waits, 50)),
        wait_p95_ms=_ms(percentile(waits, 95)),
        wait_max_ms=_ms(max(waits)) if waits else None,
        drain_seconds=round(drain_seconds, 3),
        drained=drained,
    )


# ---------------------------------------------------------------------------
# The simulated users
# ---------------------------------------------------------------------------
class _Pool:
    """Ids the users have created, shared between them so one user can read another's run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.upload_ids: list[str] = []
        self.run_ids: list[str] = []

    def add(self, kind: Literal["upload", "run"], value: str) -> None:
        with self._lock:
            (self.upload_ids if kind == "upload" else self.run_ids).append(value)

    def pick(self, kind: Literal["upload", "run"], rng: random.Random) -> str | None:
        with self._lock:
            values = self.upload_ids if kind == "upload" else self.run_ids
            return rng.choice(values) if values else None


def score_csv(rows: int, *, primary_key: str, offset: int = 0) -> bytes:
    """A small scoring file: the key, a snapshot date and the seeded schema's one feature."""
    lines = [f"{primary_key},snapshot_date,{FEATURE_COLUMN}"]
    lines.extend(f"C-{offset + i},2026-08-01,{(offset + i) % 60}" for i in range(rows))
    return ("\n".join(lines) + "\n").encode()


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return str(code) if code is not None else None


@dataclass(frozen=True, slots=True)
class _Target:
    base_url: str
    primary_key: str
    headers: Mapping[str, str]
    rows_per_upload: int = DEFAULT_ROWS_PER_UPLOAD


def _request(
    client: httpx.Client, action: Action, target: _Target, pool: _Pool, rng: random.Random
) -> Sample:
    """Send one request of kind `action`; an action whose prerequisite is missing becomes a list."""
    run_id = pool.pick("run", rng) if action == "read_run" else None
    upload_id = pool.pick("upload", rng) if action == "start_score_run" else None
    if (action == "read_run" and run_id is None) or (action == "start_score_run" and upload_id is None):
        action = "list_runs"
    started = time.perf_counter()
    try:
        if action == "list_runs":
            response = client.get("/runs", params={"limit": 20})
        elif action == "read_run":
            response = client.get(f"/runs/{run_id}")
        elif action == "upload":
            payload = score_csv(
                target.rows_per_upload, primary_key=target.primary_key, offset=rng.randrange(1_000_000)
            )
            response = client.post(
                "/uploads",
                files={"file": ("load.csv", payload, "text/csv")},
                data={"use_case": USE_CASE, "mode": "score"},
            )
        else:
            response = client.post(
                "/runs",
                json={
                    "use_case": USE_CASE,
                    "mode": "score",
                    "upload_id": upload_id,
                    "primary_key": target.primary_key,
                },
            )
    except httpx.HTTPError as exc:
        return Sample(action, type(exc).__name__, time.perf_counter() - started, False, type(exc).__name__)
    elapsed = time.perf_counter() - started
    ok = response.status_code == EXPECTED_STATUS[action]
    if ok and action == "upload":
        pool.add("upload", str(response.json()["upload_id"]))
    elif ok and action == "start_score_run":
        pool.add("run", str(response.json()["run_id"]))
    return Sample(action, str(response.status_code), elapsed, ok, None if ok else _error_code(response))


def _choose(mix: Mapping[Action, int], rng: random.Random) -> Action:
    actions = [action for action in ACTIONS if mix.get(action, 0) > 0]
    return rng.choices(actions, weights=[mix[action] for action in actions], k=1)[0]


def _user(
    index: int, profile: LoadProfile, target: _Target, pool: _Pool, barrier: threading.Barrier
) -> list[Sample]:
    rng = random.Random(profile.seed * 1_000 + index)
    samples: list[Sample] = []
    with httpx.Client(
        base_url=target.base_url, headers=dict(target.headers), timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        barrier.wait()
        for _ in range(profile.requests_per_user):
            samples.append(_request(client, _choose(profile.mix, rng), target, pool, rng))
            if profile.think_seconds:
                time.sleep(profile.think_seconds)
    return samples


def _drive(profile: LoadProfile, target: _Target, pool: _Pool) -> tuple[list[Sample], float]:
    """Run every user at once; return every sample and the wall time from first send to last answer."""
    barrier = threading.Barrier(profile.users + 1)
    results: list[list[Sample]] = [[] for _ in range(profile.users)]

    def run(index: int) -> None:
        results[index] = _user(index, profile, target, pool, barrier)

    threads = [threading.Thread(target=run, args=(i,), name=f"load-user-{i}") for i in range(profile.users)]
    for thread in threads:
        thread.start()
    barrier.wait()
    started = time.perf_counter()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started
    return [sample for user in results for sample in user], wall


def _seed_pool(target: _Target, pool: _Pool, *, include_runs: bool) -> None:
    """One upload (and one run) before the clock starts, so reads have something to read at once."""
    with httpx.Client(
        base_url=target.base_url, headers=dict(target.headers), timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        rng = random.Random(0)
        first = _request(client, "upload", target, pool, rng)
        if not first.ok:
            raise RuntimeError(f"The seeding upload failed with {first.status} ({first.error_code}).")
        if include_runs:
            run = _request(client, "start_score_run", target, pool, rng)
            if not run.ok:
                raise RuntimeError(f"The seeding run failed with {run.status} ({run.error_code}).")
        else:
            listed = client.get("/runs", params={"limit": 20})
            if listed.status_code == EXPECTED_STATUS["list_runs"]:
                for run_record in listed.json().get("runs", []):
                    pool.add("run", str(run_record["run_id"]))


# ---------------------------------------------------------------------------
# The local target
# ---------------------------------------------------------------------------
def primary_key_of(use_case: str) -> str:
    """The use case's primary-key column, from its shipped config."""
    config = load_use_case(use_case)
    keys = [column.name for column in config.template.columns if column.role is ColumnRole.PRIMARY_KEY]
    return keys[0]


def seed_scoring_model(data_dir: Path) -> ModelVersion:
    """Register a champion for `USE_CASE` whose schema a scoring upload validates against.

    Only what `POST /runs` reads is written - the registry row and `schema.json`. There is no
    predictor: the synthetic job never loads one, and a real job would fail at `predict`, which is
    the honest outcome for a model nobody trained.
    """
    config = load_use_case(USE_CASE)
    target = config.target.column or "target"
    run_id = "r_loadtest_seed"
    version = ModelVersion(
        model_id=SEEDED_MODEL_ID,
        use_case_id=USE_CASE,
        version=1,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.CHAMPION,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.5,
        validation_score=0.5,
        model_display_name="load-test seed (never trained)",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version=__version__,
        autogluon_version="none",
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)
    LocalStorage(data_dir).write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=USE_CASE,
            model_version_id=version.model_id,
            primary_key=primary_key_of(USE_CASE),
            target=target,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name=FEATURE_COLUMN, inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )
    return version


@contextmanager
def serve(app: object) -> Iterator[str]:
    """Serve `app` with uvicorn on an ephemeral loopback port in a daemon thread; yield its base URL.

    The socket is bound here, before uvicorn starts, so the port is known without parsing a log line
    and two load tests can run side by side. uvicorn skips signal handlers off the main thread.

    It is created with an explicit `IPPROTO_TCP`. asyncio only sets `TCP_NODELAY` on accepted
    connections when the listening socket's `proto` says TCP, and `socket.socket(AF_INET,
    SOCK_STREAM)` leaves it 0; without it every response met the peer's delayed ACK and *every*
    request - `/healthz` included - measured ~44 ms on loopback, a number about the harness rather
    than the API. `uvicorn --host/--port` binds its own socket correctly and is not affected.
    """
    import uvicorn  # a deliberate local import: only the local target needs a server

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    config = uvicorn.Config(app, log_level="warning", access_log=False)  # type: ignore[arg-type]
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="load-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start.")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(SERVER_START_TIMEOUT_SECONDS)
        sock.close()


def _server_process(data_dir: str, max_workers: int, job_seconds: float, conn: Connection) -> None:
    """The child process: seed, serve, and on "drain" report what the job queue did.

    Protocol over `conn`: the child sends `{"base_url": ...}` once it is serving (or `{"error": ...}`),
    waits for the parent's `"drain"`, waits for every job to finish, and sends the `QueueStats` as
    JSON. The runner lives here, beside the app that submits to it, so its counters are exact.
    """
    from api.main import create_app  # the child imports the app; the parent never needs to
    from engine.settings import Settings

    try:
        root = Path(data_dir)
        seed_scoring_model(root)
        # WARNING, not INFO: an INFO line per request on stderr would be measured as server time.
        settings = Settings(data_dir=root, job_max_workers=max_workers, log_level="WARNING")
        app = create_app(data_dir=root, settings=settings)
        runner = SyntheticJobRunner(max_workers=max_workers, job_seconds=job_seconds)
        app.state.jobs = runner  # `api.deps.get_jobs` returns a cached runner as it stands
    except Exception as exc:  # reported to the parent by class; it re-raises there
        conn.send({"error": type(exc).__name__})
        raise
    try:
        with serve(app) as base_url:
            conn.send({"base_url": base_url})
            conn.recv()
            drain_started = time.perf_counter()
            drained = runner.drain(DRAIN_TIMEOUT_SECONDS)
            drain_seconds = time.perf_counter() - drain_started
        stats = queue_stats(runner, max_workers=max_workers, drain_seconds=drain_seconds, drained=drained)
        conn.send({"queue": stats.model_dump(mode="json")})
    finally:
        runner.shutdown(wait=False)
        conn.close()


def _receive(conn: Connection, timeout: float, what: str) -> dict[str, object]:
    if not conn.poll(timeout):
        raise RuntimeError(f"The server process did not {what} within {timeout:.0f} seconds.")
    message: dict[str, object] = conn.recv()
    if "error" in message:
        raise RuntimeError(f"The server process failed to start: {message['error']}.")
    return message


def run_local(profile: LoadProfile, *, data_dir: Path) -> LoadTestReport:
    """Load a fresh app over `data_dir` (which should be empty), served by a child process.

    A child, not a thread of this process: the simulated users are Python threads too, and in one
    interpreter they would take the GIL from the server they are measuring. Spawned rather than
    forked so the child starts from a clean interpreter, as a real `uvicorn` process would.
    """
    context = multiprocessing.get_context("spawn")
    parent_conn, child_conn = context.Pipe()
    process = context.Process(
        target=_server_process,
        args=(str(data_dir), profile.max_workers, profile.job_seconds, child_conn),
        name="load-server",
        daemon=True,
    )
    process.start()
    try:
        started = _receive(parent_conn, SERVER_START_TIMEOUT_SECONDS * 4, "start serving")
        target = _Target(
            base_url=str(started["base_url"]),
            primary_key=primary_key_of(USE_CASE),
            headers={},
            rows_per_upload=profile.rows_per_upload,
        )
        pool = _Pool()
        _seed_pool(target, pool, include_runs=True)
        samples, wall = _drive(profile, target, pool)
        parent_conn.send("drain")
        finished = _receive(parent_conn, DRAIN_TIMEOUT_SECONDS + SERVER_START_TIMEOUT_SECONDS, "drain")
        queue = QueueStats.model_validate(finished["queue"])
    finally:
        process.join(SERVER_START_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
        parent_conn.close()
    notes = [
        "The two seeding requests (one upload, one run) are not counted; the seeding run's job is.",
        "Job bodies are a fixed-length wait (SyntheticJobRunner); run status.json stays 'pending'.",
        "Server and users are separate processes on the same machine and share its CPUs.",
    ]
    if queue.peak_running > profile.max_workers:
        notes.append("FINDING: more jobs ran at once than max_workers allows.")
    if not queue.drained:
        notes.append("FINDING: jobs were still unfinished when the drain timeout ran out.")
    return _report(
        profile,
        samples,
        wall,
        target="local uvicorn (one worker, child process) on 127.0.0.1",
        caveat=CAVEAT,
        queue=queue,
        notes=notes,
    )


def run_remote(
    profile: LoadProfile, *, base_url: str, token: str | None, include_runs: bool
) -> LoadTestReport:
    """Load a deployment at `base_url`. Scoring runs are only started with `include_runs` (they bill)."""
    mix = dict(profile.mix)
    notes = [f"Uploads are shaped for {USE_CASE}; a deployment without its champion answers runs with 409."]
    if not include_runs:
        mix["start_score_run"] = 0
        notes.append("start_score_run was weighted 0: on a deployment each run is a billed SageMaker job.")
    profile = LoadProfile(
        users=profile.users,
        requests_per_user=profile.requests_per_user,
        max_workers=profile.max_workers,
        job_seconds=profile.job_seconds,
        rows_per_upload=profile.rows_per_upload,
        think_seconds=profile.think_seconds,
        seed=profile.seed,
        mix=mix,
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    target = _Target(
        base_url=base_url.rstrip("/"),
        primary_key=primary_key_of(USE_CASE),
        headers=headers,
        rows_per_upload=profile.rows_per_upload,
    )
    pool = _Pool()
    _seed_pool(target, pool, include_runs=include_runs)
    samples, wall = _drive(profile, target, pool)
    return _report(
        profile, samples, wall, target=target.base_url, caveat=REMOTE_CAVEAT, queue=None, notes=notes
    )


def _report(
    profile: LoadProfile,
    samples: Sequence[Sample],
    wall: float,
    *,
    target: str,
    caveat: str,
    queue: QueueStats | None,
    notes: Sequence[str],
) -> LoadTestReport:
    by_action = {action: latency_stats([s for s in samples if s.action == action]) for action in ACTIONS}
    return LoadTestReport(
        generated_at=utc_now(),
        engine_version=__version__,
        command=COMMAND,
        target=target,
        machine=machine_line(),
        caveat=caveat,
        profile=ProfileRecord(
            users=profile.users,
            requests_per_user=profile.requests_per_user,
            max_workers=profile.max_workers,
            job_seconds=profile.job_seconds,
            rows_per_upload=profile.rows_per_upload,
            think_seconds=profile.think_seconds,
            seed=profile.seed,
            mix={action: int(profile.mix.get(action, 0)) for action in ACTIONS},
        ),
        wall_seconds=round(wall, 3),
        throughput_rps=round(len(samples) / wall, 2) if wall > 0 else 0.0,
        overall=latency_stats(samples),
        by_action={action: stats for action, stats in by_action.items() if stats.requests},
        queue=queue,
        notes=tuple(notes),
    )


def machine_line() -> str:
    """The machine the numbers belong to: CPUs this process may use, RAM, Python and OS."""
    total_kib = 0
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_kib = int(line.split()[1])
                break
    memory = f"{total_kib / (1024 * 1024):.1f} GiB RAM" if total_kib else "unknown RAM"
    affinity: Callable[[int], set[int]] | None = getattr(os, "sched_getaffinity", None)
    cpus = len(affinity(0)) if affinity is not None else (os.cpu_count() or 0)
    return f"{cpus} CPUs · {memory} · Python {platform.python_version()} · {platform.system()}"


def write_report(report: LoadTestReport, output: Path | None = None) -> Path:
    """Write `report` as indented JSON to `output`, or to `reports/load/<UTC date>.json`."""
    path = output or REPORT_DIR / f"{report.generated_at.date().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def summary_lines(report: LoadTestReport) -> list[str]:
    """What is printed: one line per action, then the queue."""
    lines = [
        f"{report.target} · {report.machine}",
        f"{report.profile.users} users x {report.profile.requests_per_user} requests in "
        f"{report.wall_seconds:.1f}s ({report.throughput_rps:.1f} req/s), "
        f"error rate {report.overall.error_rate:.2%}",
    ]
    for action, stats in report.by_action.items():
        lines.append(
            f"  {action:<16} n={stats.requests:<5} p50={stats.p50_ms}ms p95={stats.p95_ms}ms "
            f"p99={stats.p99_ms}ms errors={stats.errors}"
        )
    if report.queue is not None:
        q = report.queue
        lines.append(
            f"  jobs: {q.submitted} submitted, {q.finished} finished, peak running {q.peak_running}/"
            f"{q.max_workers}, peak waiting {q.peak_waiting}, wait p95 {q.wait_p95_ms}ms, "
            f"drained in {q.drain_seconds:.1f}s"
        )
    lines.append(f"NOTE: {report.caveat}")
    return lines


def parse_mix(text: str) -> dict[Action, int]:
    """`action=weight,...` -> weights; an action left out weighs 0."""
    mix: dict[Action, int] = {}
    for part in text.split(","):
        name, _, weight = part.partition("=")
        action = next((known for known in ACTIONS if known == name.strip()), None)
        if action is None or not weight.strip().isdigit():
            raise argparse.ArgumentTypeError(
                f"{part!r}: expected <action>=<weight> with action in {ACTIONS}."
            )
        mix[action] = int(weight)
    return mix


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--users", type=int, default=DEFAULT_USERS)
    parser.add_argument("--requests-per-user", type=int, default=DEFAULT_REQUESTS_PER_USER)
    parser.add_argument("--max-workers", type=int, default=None, help="default: Settings().job_max_workers")
    parser.add_argument("--job-seconds", type=float, default=DEFAULT_JOB_SECONDS)
    parser.add_argument("--rows-per-upload", type=int, default=DEFAULT_ROWS_PER_UPLOAD)
    parser.add_argument("--think-seconds", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--mix",
        type=parse_mix,
        default=None,
        help="e.g. list_runs=35,read_run=35,upload=15,start_score_run=15",
    )
    parser.add_argument("--output", type=Path, default=None, help="default: reports/load/<UTC date>.json")
    parser.add_argument("--base-url", default=None, help="load a deployment instead of the local app")
    parser.add_argument("--token", default=None, help="bearer session for --base-url (auth_mode=local)")
    parser.add_argument("--include-runs", action="store_true", help="with --base-url: start billed runs")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the load test, write the report, print the summary; 1 when any request failed."""
    from engine.settings import Settings

    args = parse_args(argv)
    # httpx logs every request at INFO; a thousand lines of that would bury the summary.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    profile = LoadProfile(
        users=args.users,
        requests_per_user=args.requests_per_user,
        max_workers=args.max_workers or Settings().job_max_workers,
        job_seconds=args.job_seconds,
        rows_per_upload=args.rows_per_upload,
        think_seconds=args.think_seconds,
        seed=args.seed,
        mix=args.mix or dict(DEFAULT_MIX),
    )
    if args.base_url:
        report = run_remote(profile, base_url=args.base_url, token=args.token, include_runs=args.include_runs)
    else:
        with tempfile.TemporaryDirectory(prefix="marketing-ai-load-") as tmp:
            report = run_local(profile, data_dir=Path(tmp))
    path = write_report(report, args.output)
    for line in summary_lines(report):
        print(line)
    print(f"report: {path}")
    return 0 if report.overall.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
