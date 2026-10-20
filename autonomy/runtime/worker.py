"""The worker: claims due jobs from the `JobQueue` and runs them, retrying failures with backoff.

Three kinds of job keep Autonomy moving between requests:

- `tick` runs one runtime tick (fold new events, decide, batch or dispatch, learn, brief, check the
  breakers). A tick job can carry `every_minutes`, and then queues its own successor, so one seed
  job keeps the daily sweep going.
- `send_message` finishes a send the dispatcher queued for its `send_at`; the dispatcher re-checks
  every rule first, so a customer who unsubscribed while the message waited is not contacted.
- `webhook_event` stores a provider callback that was accepted quickly and parsed later.

The library code never sleeps and never reads the clock: `Worker.run_once(now)` and
`Worker.drain(now)` take `now`, so the demo and the tests drive it on simulated time. Only the
`python -m autonomy.runtime.worker` entry point loops on the wall clock, with a sleep between
polls.

A failing job is retried with exponential backoff (1, 2, 4, ... minutes, capped at an hour) up to
`max_attempts`, then left FAILED with its last error for an operator. A handler raises
`PermanentJobError` for a job that can never succeed (a bad signature, an unreadable payload), and
that job fails at once, without retries.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from autonomy.channels import SEND_JOB_KIND, Dispatcher
from autonomy.contracts import Channel, Event, Job, stable_id
from autonomy.interfaces import ChannelAdapter, DecisionLog, JobQueue, StateStore

__all__ = [
    "DEFAULT_KINDS",
    "SEND_KIND",
    "TICK_KIND",
    "WEBHOOK_KIND",
    "JobHandler",
    "JobOutcome",
    "PermanentJobError",
    "Worker",
    "main",
    "next_tick_at",
    "retry_delay",
    "send_handler",
    "tick_handler",
    "tick_job",
    "webhook_handler",
    "webhook_job",
]

log = logging.getLogger(__name__)

TICK_KIND = "tick"
SEND_KIND = SEND_JOB_KIND
WEBHOOK_KIND = "webhook_event"
DEFAULT_KINDS: tuple[str, ...] = (TICK_KIND, SEND_KIND, WEBHOOK_KIND)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF = timedelta(minutes=1)
DEFAULT_BACKOFF_CAP = timedelta(hours=1)
MAX_ERROR_LEN = 500
DEMO_TICK_MINUTES = 60
"""The live demo ticks hourly: a daily tick that happened to fall before the sweep hour would
never decide anything."""

JobHandler = Callable[[Job, datetime], None]
"""Runs one claimed job at `now`. Returning means done; raising means failed (retried unless permanent)."""


class PermanentJobError(Exception):
    """A job that can never succeed: fail it now, without retrying."""


def retry_delay(
    attempts: int, *, base: timedelta = DEFAULT_BACKOFF, cap: timedelta = DEFAULT_BACKOFF_CAP
) -> timedelta:
    """How long to wait after the `attempts`-th failed try: base x 2^(attempts-1), at most `cap`."""
    exponent = max(0, attempts - 1)
    if exponent >= 30:  # 2^30 minutes is far past any cap; avoid building a huge timedelta
        return cap
    delay: timedelta = base * (2**exponent)
    return delay if delay < cap else cap


@dataclass(frozen=True)
class JobOutcome:
    """What happened to one claimed job."""

    job_id: str
    kind: str
    status: str
    """'done', 'retry' (back in the queue for `retry_at`) or 'failed' (stopped)."""
    attempts: int
    error: str | None = None
    retry_at: datetime | None = None


@dataclass
class Worker:
    """Claims due jobs of the kinds it has handlers for and runs them. See the module docstring."""

    queue: JobQueue
    handlers: Mapping[str, JobHandler]
    name: str = "worker-1"
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: timedelta = DEFAULT_BACKOFF
    backoff_cap: timedelta = DEFAULT_BACKOFF_CAP
    outcomes: list[JobOutcome] = field(default_factory=list)
    """Every outcome so far, oldest first: the demo and the tests read it."""

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.handlers = dict(self.handlers)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self.handlers))

    def run_once(self, now: datetime) -> JobOutcome | None:
        """Claim and run the next due job; None when nothing is due."""
        job = self.queue.claim(self.kinds, worker=self.name, now=now)
        if job is None:
            return None
        handler = self.handlers.get(job.kind)
        try:
            if handler is None:  # a queue that ignores `kinds` handed us something we cannot run
                raise PermanentJobError(f"no handler for job kind {job.kind!r}")
            handler(job, now)
        except PermanentJobError as exc:
            outcome = self._fail(job, _error_text(exc), retry=False, now=now)
        except Exception as exc:  # any handler bug or outage: record it and retry later
            log.warning("job %s (%s) failed on attempt %d: %s", job.job_id, job.kind, job.attempts, exc)
            outcome = self._fail(job, _error_text(exc), retry=True, now=now)
        else:
            self.queue.complete(job.job_id)
            outcome = JobOutcome(job_id=job.job_id, kind=job.kind, status="done", attempts=job.attempts)
        self.outcomes.append(outcome)
        return outcome

    def drain(self, now: datetime, *, limit: int = 10_000) -> list[JobOutcome]:
        """Run every job due at `now`, up to `limit`. A job retried for later is not due again now."""
        done: list[JobOutcome] = []
        while len(done) < limit:
            outcome = self.run_once(now)
            if outcome is None:
                break
            done.append(outcome)
        return done

    def _fail(self, job: Job, error: str, *, retry: bool, now: datetime) -> JobOutcome:
        if retry and job.attempts < self.max_attempts:
            retry_at = now + retry_delay(job.attempts, base=self.backoff, cap=self.backoff_cap)
            self.queue.fail(job.job_id, error, retry_at=retry_at)
            return JobOutcome(job.job_id, job.kind, "retry", job.attempts, error, retry_at)
        self.queue.fail(job.job_id, error)
        return JobOutcome(job.job_id, job.kind, "failed", job.attempts, error)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LEN]


# --- jobs and handlers ---------------------------------------------------------------------------


def tick_job(run_at: datetime, *, every_minutes: int | None = None) -> Job:
    """A tick due at `run_at`. With `every_minutes`, running it queues the next one that much later.

    The id is derived from the time (in UTC), so queueing the same tick twice keeps one job. A
    repeating tick also carries its scheduled time: a retry moves the job's `run_at`, and the
    successor must stay on the original schedule."""
    payload: dict[str, str | int | float | bool | None] = {}
    if every_minutes is not None:
        if every_minutes < 1:
            raise ValueError("every_minutes must be at least 1")
        payload["every_minutes"] = every_minutes
        payload["scheduled_at"] = run_at.isoformat()
    return Job(
        job_id=stable_id("job", TICK_KIND, run_at.astimezone(UTC).isoformat()),
        kind=TICK_KIND,
        payload=payload,
        run_at=run_at,
    )


def next_tick_at(scheduled: datetime, every: timedelta, now: datetime) -> datetime:
    """The first slot of the schedule `scheduled + k * every` (k >= 1) that is after `now`.

    Slots a stopped or slow worker missed are skipped, not replayed one after another: a tick
    catches up on everything that happened since the last one anyway."""
    if now < scheduled:
        return scheduled + every
    return scheduled + every * ((now - scheduled) // every + 1)


def tick_handler(tick: Callable[[datetime], object], queue: JobQueue | None = None) -> JobHandler:
    """Run `tick(now)` for a tick job, after queueing its successor when the job repeats.

    The successor is queued first, so a tick that keeps failing (and is eventually left FAILED)
    does not end the schedule. It is the next slot after `now` on the job's original schedule, so
    a late worker or a retry does not drift it, and a retry queues the same successor again (the
    id is its time) instead of starting a second chain."""

    def handle(job: Job, now: datetime) -> None:
        every = job.payload.get("every_minutes")
        if queue is not None and isinstance(every, int) and not isinstance(every, bool) and every > 0:
            scheduled = _scheduled_at(job)
            successor = next_tick_at(scheduled, timedelta(minutes=every), now)
            queue.enqueue(tick_job(successor, every_minutes=every))
        tick(now)

    return handle


def _scheduled_at(job: Job) -> datetime:
    """When a repeating tick was due before any retry moved it; its `run_at` for older jobs."""
    run_at: datetime = job.run_at
    raw = job.payload.get("scheduled_at")
    if not isinstance(raw, str):
        return run_at
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return run_at
    return parsed if parsed.tzinfo is not None else run_at


def send_handler(dispatcher: Dispatcher, decisions: DecisionLog, states: StateStore) -> JobHandler:
    """Finish a `send_message` job through the dispatcher.

    A skip (the customer unsubscribed, the goal was paused, ...) is a finished job: the dispatcher
    reported why. A provider failure raises, so the worker retries with backoff."""

    def handle(job: Job, now: datetime) -> None:
        report = dispatcher.run_send_job(job, decisions, states, now)
        if report.failed:
            reasons = "; ".join(r.reason or "provider error" for r in report.failed)
            raise RuntimeError(f"send failed: {reasons}")

    return handle


def webhook_job(channel: Channel, headers: Mapping[str, str], body: bytes, *, received_at: datetime) -> Job:
    """A provider callback to verify, parse and store later. Header names are lowercased.

    The id comes from everything received, so the same delivery queued twice is one job."""
    text = body.decode("utf-8")
    stored = json.dumps({k.lower(): v for k, v in headers.items()}, sort_keys=True)
    return Job(
        job_id=stable_id("job", WEBHOOK_KIND, channel.value, stored, text, received_at.isoformat()),
        kind=WEBHOOK_KIND,
        payload={"channel": channel.value, "headers": stored, "body": text},
        run_at=received_at,
    )


def webhook_handler(
    adapters: Mapping[Channel, ChannelAdapter], ingest: Callable[[Sequence[Event]], int]
) -> JobHandler:
    """Verify a stored callback with its channel's adapter, parse it into events and ingest them.

    A bad signature, an unknown channel or an unreadable body can never get better, so they fail
    the job at once. `ingest` drops events it has already seen, so a retried job counts once."""

    def handle(job: Job, now: datetime) -> None:  # noqa: ARG001
        raw_channel = job.payload.get("channel")
        try:
            channel = Channel(str(raw_channel))
        except ValueError as exc:
            raise PermanentJobError(f"unknown channel {raw_channel!r}") from exc
        adapter = adapters.get(channel)
        if adapter is None:
            raise PermanentJobError(f"no adapter for {channel.value}")
        body = str(job.payload.get("body") or "").encode("utf-8")
        try:
            loaded = json.loads(str(job.payload.get("headers") or "{}"))
        except ValueError as exc:
            raise PermanentJobError("the stored headers are not JSON") from exc
        if not isinstance(loaded, dict):
            raise PermanentJobError("the stored headers are not an object")
        headers = {str(k).lower(): str(v) for k, v in loaded.items()}
        if not adapter.verify_webhook(headers, body):
            raise PermanentJobError("the webhook signature does not match")
        try:
            events = adapter.parse_webhook(headers, body)
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentJobError(f"could not read the {channel.value} webhook: {exc}") from exc
        ingest(events)

    return handle


# --- the command line ----------------------------------------------------------------------------


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m autonomy.runtime.worker",
        description="Run the Autonomy worker: claim tick, send and webhook jobs and run them.",
    )
    parser.add_argument("--demo", action="store_true", help="Run on the in-memory simulated shop")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Sleep between empty polls")
    parser.add_argument(
        "--tick-minutes",
        type=int,
        default=DEMO_TICK_MINUTES,
        help="How often the demo ticks (the production worker takes this from its settings)",
    )
    parser.add_argument("--once", action="store_true", help="Drain what is due now, then exit")
    parser.add_argument("--name", default="worker-1", help="This worker's name in claimed jobs")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Loop over the queue on the wall clock. The only place in the runtime that sleeps.

    `--demo` runs the simulated shop from `autonomy.runtime.demo`. Without it the worker needs the
    production wiring (`autonomy.runtime.service.build_worker` over `autonomy.runtime.settings`),
    which is loaded lazily so this module stays importable on its own. Either way the builder seeds
    the repeating tick job, so a restart does not start a second chain of ticks."""
    import time

    args = _parse(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        worker = _build_worker(args)
    except (ImportError, AttributeError, RuntimeError, ValueError) as exc:  # missing wiring or settings
        print(f"cannot start the worker: {exc}", file=sys.stderr)
        return 2
    while True:
        outcomes = worker.drain(datetime.now(UTC))
        for o in outcomes:
            log.info("%s %s: %s%s", o.kind, o.job_id, o.status, f" ({o.error})" if o.error else "")
        if args.once:
            return 0
        if not outcomes:
            time.sleep(args.poll_seconds)


def _build_worker(args: argparse.Namespace) -> Worker:
    if args.demo:
        from autonomy.runtime.demo import live_demo_worker

        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        return live_demo_worker(name=args.name, start=start, tick_minutes=args.tick_minutes)
    from autonomy.runtime import service

    build = getattr(service, "build_worker", None)
    if build is None:
        raise RuntimeError(
            "autonomy.runtime.service has no build_worker(); run with --demo or wire the service first"
        )
    worker = build(name=args.name)
    if not isinstance(worker, Worker):
        raise RuntimeError("autonomy.runtime.service.build_worker() did not return a Worker")
    return worker


if __name__ == "__main__":
    # Run the importable copy of this module, not `__main__`: the service builds its Worker from
    # `autonomy.runtime.worker`, and a second copy of the class would fail every isinstance check.
    from autonomy.runtime.worker import main as _main

    sys.exit(_main())
