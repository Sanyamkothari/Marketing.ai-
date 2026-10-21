"""The worker: claims due jobs from the `JobQueue` and runs them, retrying failures with backoff.

Three kinds of job keep Autonomy moving between requests:

- `tick` runs one runtime tick (fold new events, decide, batch or dispatch, learn, brief, check the
  breakers). A tick job can carry `every_minutes`, and then queues its own successor, so one seed
  job keeps the daily sweep going.
- `send_message` finishes a send the dispatcher queued for its `send_at`; the dispatcher re-checks
  every rule first, so a customer who unsubscribed while the message waited is not contacted.
- `webhook_event` stores a provider callback that was accepted quickly and parsed later.
- `gateway_send_decision` (only when the runtime sends through a `DispatchGateway`) finishes a
  decision the gateway held for quiet hours, a rate limit or a retry; the gateway re-runs its
  compliance gate and the dispatcher its checks (`gateway_handler`).
- `connector_sync`, `connector_webhook` and `connector_daily` (only when SaaS connectors are wired)
  keep Shopify, Klaviyo and the other connectors in sync: each sync job queues its successor on the
  stream's cadence, so seeding a connection once keeps it running (`connector_handlers`, and
  `docs/v2/CONNECTORS.md`).

Both send handlers take an optional `gate`: the runtime's mandate check, run again when a held
message comes due, so a mandate revoked (or a daily cap reached) while it waited stops it. They also
take an optional `hold` (the runtime's `data_send_gate`): while the shop's connected data cannot be
trusted, a due send is put back on the queue for `HOLD_RETRY` instead of going out, for up to
`MAX_HOLD` past its send time; after that it is skipped (a decision that old is out of date).

The library code never sleeps and never reads the clock: `Worker.run_once(now)` and
`Worker.drain(now)` take `now`, so the demo and the tests drive it on simulated time. Only the
`python -m autonomy.runtime.worker` entry point loops on the wall clock, with a sleep between
polls.

**Send jobs have one owner: a `Worker` with a send handler.** The runtime's tick never claims a
`send_message` job on its own. A runtime ticked with no worker at all (the tests, the API's
operator tick) runs this same handler inside its tick, through a worker of its own marked
`hosted`; as soon as a (non-hosted) `Worker` in the process has a send handler for the runtime's
dispatcher, the tick leaves send jobs to it (see `worker_owns_sends`). Production builds the
runtime with `run_send_jobs=False`, so only the worker process runs sends.

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
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from autonomy.channels import SEND_JOB_KIND, Dispatcher, DispatchReport, DispatchResult, DispatchStatus
from autonomy.contracts import Channel, Decision, Event, Job, stable_id
from autonomy.interfaces import ChannelAdapter, DecisionLog, JobQueue, StateStore

if TYPE_CHECKING:
    from autonomy.channels.gateway import DispatchGateway
    from autonomy.connectors.shopify import ShopifyPrivacyDesk
    from autonomy.connectors.sync import ConnectorEngine
    from autonomy.interfaces import EventStore

__all__ = [
    "CONNECTOR_KINDS",
    "DEFAULT_KINDS",
    "GATEWAY_KIND",
    "HOLD_RETRY",
    "MAX_HOLD",
    "SEND_KIND",
    "TICK_KIND",
    "WEBHOOK_KIND",
    "GatewayHandler",
    "JobHandler",
    "JobOutcome",
    "PermanentJobError",
    "SendGate",
    "SendHandler",
    "Worker",
    "connector_handlers",
    "gateway_handler",
    "main",
    "next_tick_at",
    "retry_delay",
    "send_handler",
    "tick_handler",
    "tick_job",
    "webhook_handler",
    "webhook_job",
    "worker_owns_sends",
]

log = logging.getLogger(__name__)

TICK_KIND = "tick"
SEND_KIND = SEND_JOB_KIND
WEBHOOK_KIND = "webhook_event"
GATEWAY_KIND = "gateway_send_decision"
"""The gateway's held-decision job kind (`autonomy.channels.gateway.GATEWAY_DECISION_JOB`)."""
DEFAULT_KINDS: tuple[str, ...] = (TICK_KIND, SEND_KIND, WEBHOOK_KIND)
CONNECTOR_KINDS: tuple[str, ...] = ("connector_sync", "connector_webhook", "connector_daily")
"""The SaaS connector job kinds (`autonomy.connectors.sync.JOB_KINDS`)."""

HOLD_RETRY = timedelta(minutes=30)
"""How long a send held for stale data waits before it is checked again."""
MAX_HOLD = timedelta(hours=24)
"""How long past its send time a held send may still go out; older ones are skipped."""

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF = timedelta(minutes=1)
DEFAULT_BACKOFF_CAP = timedelta(hours=1)
MAX_ERROR_LEN = 500
DEMO_TICK_MINUTES = 60
"""The live demo ticks hourly: a daily tick that happened to fall before the sweep hour would
never decide anything."""

JobHandler = Callable[[Job, datetime], None]
"""Runs one claimed job at `now`. Returning means done; raising means failed (retried unless permanent)."""

SendGate = Callable[[Decision, datetime], str | None]
"""(decision, now) -> a reason not to send it now (it is skipped, and the job is done), or None."""


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
    hosted: bool = False
    """True for the worker a runtime runs inside its own tick. A hosted worker never takes over
    send jobs from the runtime; any other worker with a `SendHandler` does (`worker_owns_sends`)."""

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.handlers = dict(self.handlers)
        sender = self.handlers.get(SEND_KIND)
        if not self.hosted and isinstance(sender, SendHandler):
            _SEND_OWNERS.add(sender.dispatcher)

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


_SEND_OWNERS: weakref.WeakSet[Dispatcher] = weakref.WeakSet()
"""Dispatchers whose send jobs a (non-hosted) `Worker` in this process runs."""


def worker_owns_sends(dispatcher: Dispatcher) -> bool:
    """True when a `Worker` built in this process runs this dispatcher's send jobs.

    The runtime asks this before running send jobs in its tick, so one process never has two
    claimers (with two sets of retry rules) for the same queue."""
    return dispatcher in _SEND_OWNERS


def _gated(job: Job, decisions: DecisionLog, gate: SendGate | None, now: datetime) -> DispatchReport | None:
    """A report skipping the job's decision when `gate` says no; None when it may go ahead."""
    if gate is None:
        return None
    decision_id = str(job.payload.get("decision_id") or "")
    decision = decisions.get(decision_id) if decision_id else None
    if decision is None:
        return None  # the send path reports a missing decision in its own words
    reason = gate(decision, now)
    if not reason:
        return None
    return DispatchReport(
        results=[
            DispatchResult(
                decision_id=decision.decision_id,
                customer_id=decision.customer_id,
                status=DispatchStatus.SKIPPED,
                reason=reason,
                detail=f"job {job.job_id} was held back by the send gate",
            )
        ]
    )


def _held(
    job: Job, decisions: DecisionLog, hold: SendGate | None, queue: JobQueue | None, now: datetime
) -> DispatchReport | None:
    """While `hold` says the data cannot be trusted: put the job back for later (or, once it is
    `MAX_HOLD` past due, skip it). None when it may go ahead."""
    if hold is None:
        return None
    decision_id = str(job.payload.get("decision_id") or "")
    decision = decisions.get(decision_id) if decision_id else None
    if decision is None:
        return None
    reason = hold(decision, now)
    if not reason:
        return None
    due = decision.action.send_at if decision.action is not None else decision.made_at
    if queue is not None and now - due <= MAX_HOLD:
        later = now + HOLD_RETRY
        queue.enqueue(
            Job(
                job_id=stable_id("job", "held", job.job_id, later.isoformat()),
                kind=job.kind,
                payload=dict(job.payload),
                run_at=later,
            )
        )
        status, detail = DispatchStatus.QUEUED, f"held until {later.isoformat()}: the data is not fresh"
    else:
        status, detail = DispatchStatus.SKIPPED, "held too long for stale data; not sent"
    return DispatchReport(
        results=[
            DispatchResult(
                decision_id=decision.decision_id,
                customer_id=decision.customer_id,
                status=status,
                reason=reason,
                detail=detail,
            )
        ]
    )


class SendHandler:
    """Finish a `send_message` job through the dispatcher: the one code path for queued sends.

    A skip (the customer unsubscribed, the goal was paused, ...) is a finished job: the dispatcher
    reported why. A provider failure raises, so the worker retries with backoff. `on_report`, if
    given, sees every dispatch report first (failures included), which is how a runtime hosting
    the handler folds the SEND and FAIL events into state in the same tick. `gate`, if given, is
    asked first; a reason from it skips the send (the job is done, nothing is retried)."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        decisions: DecisionLog,
        states: StateStore,
        *,
        on_report: Callable[[DispatchReport], None] | None = None,
        gate: SendGate | None = None,
        hold: SendGate | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.decisions = decisions
        self.states = states
        self.on_report = on_report
        self.gate = gate
        self.hold = hold
        self.queue = queue

    def __call__(self, job: Job, now: datetime) -> None:
        report = _held(job, self.decisions, self.hold, self.queue, now)
        if report is None:
            report = _gated(job, self.decisions, self.gate, now)
        if report is None:
            report = self.dispatcher.run_send_job(job, self.decisions, self.states, now)
        if self.on_report is not None:
            self.on_report(report)
        if report.failed:
            reasons = "; ".join(r.reason or "provider error" for r in report.failed)
            raise RuntimeError(f"send failed: {reasons}")


def send_handler(
    dispatcher: Dispatcher,
    decisions: DecisionLog,
    states: StateStore,
    *,
    on_report: Callable[[DispatchReport], None] | None = None,
    gate: SendGate | None = None,
    hold: SendGate | None = None,
    queue: JobQueue | None = None,
) -> SendHandler:
    """A `SendHandler` for a worker. A worker given it owns the dispatcher's send jobs. `hold` puts a
    due send back on `queue` while the data is not fresh."""
    return SendHandler(dispatcher, decisions, states, on_report=on_report, gate=gate, hold=hold, queue=queue)


class GatewayHandler:
    """Finish a decision the `DispatchGateway` held (`gateway_send_decision` jobs).

    The gateway runs its compliance gate again and re-queues what still has to wait, so a held
    decision is never lost; its failures are retried by the gateway itself (with its own backoff),
    so a failed send does not raise here and the worker does not retry it a second way. `gate` and
    `on_report` work as for `SendHandler`."""

    def __init__(
        self,
        gateway: DispatchGateway,
        decisions: DecisionLog,
        states: StateStore,
        *,
        on_report: Callable[[DispatchReport], None] | None = None,
        gate: SendGate | None = None,
        hold: SendGate | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self.gateway = gateway
        self.decisions = decisions
        self.states = states
        self.on_report = on_report
        self.gate = gate
        self.hold = hold
        self.queue = queue

    def __call__(self, job: Job, now: datetime) -> None:
        report = _held(job, self.decisions, self.hold, self.queue, now)
        if report is None:
            report = _gated(job, self.decisions, self.gate, now)
        if report is None:
            report = self.gateway.run_decision_job(job, self.decisions, self.states, now)
        if self.on_report is not None:
            self.on_report(report)


def gateway_handler(
    gateway: DispatchGateway,
    decisions: DecisionLog,
    states: StateStore,
    *,
    on_report: Callable[[DispatchReport], None] | None = None,
    gate: SendGate | None = None,
    hold: SendGate | None = None,
    queue: JobQueue | None = None,
) -> GatewayHandler:
    """A `GatewayHandler` for a worker: register it under `GATEWAY_KIND`."""
    return GatewayHandler(gateway, decisions, states, on_report=on_report, gate=gate, hold=hold, queue=queue)


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


def connector_handlers(
    engine: ConnectorEngine,
    queue: JobQueue,
    *,
    events: EventStore | None = None,
    privacy_desk: ShopifyPrivacyDesk | None = None,
) -> dict[str, JobHandler]:
    """Handlers for the connector jobs, to merge into a `Worker`'s `handlers`.

    Each sync job re-queues its own successor (live lanes on the stream's cadence, history lanes
    until the backfill is done) and the daily job re-seeds any chain that ended, so a connection
    seeded once (`ConnectorEngine.seed`) keeps syncing. Connector jobs handle their own retries,
    so they never ask the worker to retry a platform failure. With `events`, Klaviyo's hourly
    setup-and-delivery watch runs too; with `privacy_desk`, Shopify's privacy job
    (`autonomy.connectors.wiring.worker_handlers`)."""
    from autonomy.connectors.wiring import worker_handlers

    return worker_handlers(engine, queue, events=events, privacy_desk=privacy_desk)


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
