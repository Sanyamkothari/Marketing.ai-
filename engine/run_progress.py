"""An honest progress estimate for a run: stages weighted by how long they take (Plan M M189, DEC-1619).

`status.json`'s `progress_pct` is done stages over total stages, each stage weighted the same, and the
train stage says nothing while it runs. Training is almost all of a training run's time, so the bar sat
at 50% for nearly the whole run and then raced to 100. `progress_pct` keeps that meaning (the API and
its tests pin the 12, 25, 38 ... sequence); this module adds the estimate beside it:

* `expected_durations` gives every stage an expected duration. The train stage takes the median of this
  deployment's finished runs of the same use case, trainer and search budget (what the same settings took
  before); with none, the search's wall-clock budget when the trainer honours one (AutoGluon's `time_limit`),
  which is a limit and the only number the screens say "up to" about. A trainer with neither (an uplift
  learner on LightGBM, never run here) has no estimate at all. Every other stage takes the median of past
  runs, or a small documented default (`DEFAULT_STAGE_SECONDS`).
* `estimate_progress` is the pure rule: the done stages' expected durations, plus the running stage's
  expected duration times the share of it that has elapsed, over the total. A running stage that has
  run past its expectation is credited at most `OVERRUN_FRACTION` of it and has no time left to give
  (`eta_seconds` is null; the screen says "Finishing up"). The estimate never goes backwards, never says
  more than 99 before the run is done and says 100 only when it is.
* `install_progress_estimate` wires that into the pipeline without touching a stage (train.py is
  frozen): each status write is decorated with the estimate, and while a long stage runs a heartbeat
  thread rewrites `status.json` every `HEARTBEAT_SECONDS` so the bar moves. The heartbeat writes under
  the same lock as the flow's own writes, only while its stage is still the running one, and only while
  the stored document is still the flow's own - never over an ending another process wrote (a cancel
  from the API or the cost cap) and never once the run was cancelled.

Nothing here can fail a run: an estimate that cannot be made is left null and logged.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState, RunStatus, StageKey, StageStatus
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.storage import Storage

__all__ = [
    "BEATING_STAGES",
    "DEFAULT_STAGE_SECONDS",
    "HEARTBEAT_SECONDS",
    "OVERRUN_FRACTION",
    "Estimate",
    "EstimateBasis",
    "Expectations",
    "PastDurations",
    "ProgressTracker",
    "about_minutes",
    "estimate_progress",
    "expected_durations",
    "expected_run_minutes",
    "install_progress_estimate",
    "past_durations",
    "told_seconds",
    "train_budget_seconds",
    "trainer_of",
]

_LOGGER = get_logger(__name__)

EstimateBasis = Literal["time_budget", "past_runs", "stages"]

HEARTBEAT_SECONDS: Final[float] = 5.0
"""How often a long stage's heartbeat rewrites `status.json`; the Running screen polls every two seconds."""

OVERRUN_FRACTION: Final[float] = 0.95
"""The most of a running stage's expected duration it is credited with, however long it has run."""

MIN_STAGE_SECONDS: Final[float] = 1.0
"""No stage is expected to take less than this, so a stage that took 0.001 s once cannot weigh nothing."""

DEFAULT_STAGE_SECONDS: Final[Mapping[StageKey, float]] = MappingProxyType(
    {
        # train flow (the train stage itself has no default: see `expected_durations`)
        StageKey.INGEST: 5.0,
        StageKey.VALIDATE: 5.0,
        StageKey.PREPARE: 10.0,
        StageKey.SPLIT: 5.0,
        StageKey.EVALUATE: 15.0,
        StageKey.EXPLAIN: 30.0,
        StageKey.REGISTER: 5.0,
        # score flow
        StageKey.VALIDATE_AGAINST_SCHEMA: 5.0,
        StageKey.PREDICT: 30.0,
        StageKey.EXPLAIN_ROWS: 30.0,
        StageKey.ACTIONS: 5.0,
        StageKey.EXPORT: 5.0,
    }
)
"""Expected seconds per stage when this deployment has no finished run to measure from. Deliberately
small: they only spread the few seconds around the long stage, and a stage that runs longer than its
default is shown as "Finishing up" rather than as a number that stops."""

BEATING_STAGES: Final[frozenset[StageKey]] = frozenset(
    {StageKey.TRAIN, StageKey.EXPLAIN, StageKey.PREDICT, StageKey.EXPLAIN_ROWS}
)
"""The stages that can run for minutes and say nothing meanwhile; only these get a heartbeat."""

PAST_RUNS: Final[int] = 10
"""How many finished runs of the same use case and mode the medians are taken over (the newest)."""

SCANNED_RECORDS: Final[int] = 60
"""How many run records, newest first, are read at most while looking for those runs."""

CLOCK: Callable[[], float] = time.monotonic
"""The clock elapsed time is measured on; a test replaces it to make a slow stage without waiting."""

_ENDED: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})
"""A stored status in one of these states is an ending the heartbeat must never write over."""

_TRACKER_ATTR: Final[str] = "_run_progress_tracker"
_INSTALLED: Final[str] = "_run_progress_installed"


# ---------------------------------------------------------------------------
# What each stage is expected to take
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PastDurations:
    """Durations of this deployment's finished runs of one use case and mode, newest first."""

    stages: Mapping[StageKey, tuple[float, ...]]
    """Every stage but train, from every matching run."""
    train: tuple[float, ...] = ()
    """The train stage, from the matching runs that used the same trainer and the same search budget only."""


@dataclass(frozen=True)
class Expectations:
    """The expected duration of every stage of one run, and where the train stage's came from."""

    seconds: Mapping[StageKey, float]
    basis: EstimateBasis


def trainer_of(config: UseCaseConfig) -> str:
    """Which trainer a use case's training run uses: the AutoGluon search, or an uplift learner's base model."""
    if config.problem_type is ProblemType.UPLIFT:
        return f"uplift:{config.uplift.base_model.value}"
    return "automl"


def train_budget_seconds(config: UseCaseConfig) -> float | None:
    """The train stage's wall-clock budget in seconds, when its trainer honours one; else `None`.

    The AutoGluon search stops at `model_search.time_limit_minutes`. An uplift learner on AutoGluon shares
    `uplift.time_limit_minutes` across its sub-models; on LightGBM it has no budget at all.
    """
    from engine.uplift.config import UpliftBaseModel

    if config.problem_type is ProblemType.UPLIFT:
        if config.uplift.base_model is UpliftBaseModel.AUTOGLUON_FAST:
            return float(config.uplift.time_limit_minutes * 60)
        return None
    return float(config.model_search.time_limit_minutes * 60)


LATER_STEP_SECONDS: Final[Mapping[str, int]] = MappingProxyType({"score": 60, "list": 20, "campaign": 10})
"""Rough seconds the steps of a campaign run after the learning step need (scoring, the list, the campaign)."""

_TRAIN_FLOW_OVERHEAD: Final[tuple[StageKey, ...]] = (
    StageKey.INGEST,
    StageKey.VALIDATE,
    StageKey.PREPARE,
    StageKey.SPLIT,
    StageKey.EVALUATE,
    StageKey.EXPLAIN,
    StageKey.REGISTER,
)


def about_minutes(seconds: float) -> int:
    """Seconds as the minutes a person is told, rounded up to the figure the run page's countdown shows
    (`ui/run_progress.js` `etaText`): to the minute up to ten, to five up to an hour (never under ten), to ten beyond.
    Up, so a time promised before the run is still the time shown for the first stretch of it, not one that has
    become the next figure down a few seconds in."""
    minutes = seconds / 60
    if minutes <= 10:
        return max(1, math.ceil(minutes))
    if minutes <= 55:
        return math.ceil(minutes / 5) * 5
    return math.ceil(minutes / 10) * 10


def told_seconds(seconds: int) -> int:
    """The time left the run page is given: `seconds` as the whole figure of minutes it counts down in
    (`about_minutes`), so the countdown starts from the minutes the approval card promised. Under a minute as it is.
    """
    return seconds if seconds < 60 else about_minutes(seconds) * 60


def expected_run_minutes(search_minutes: int) -> int:
    """How long a campaign run is expected to take from start to its list when the learning step may search for
    `search_minutes`: the search's own budget (`train_budget_seconds`), the other stages of the learning flow
    (`DEFAULT_STAGE_SECONDS`) and the steps after it (`LATER_STEP_SECONDS`). The one estimate the approval card, the
    model offer and the run page say."""
    seconds = search_minutes * 60 + sum(DEFAULT_STAGE_SECONDS[k] for k in _TRAIN_FLOW_OVERHEAD)
    return about_minutes(seconds + sum(LATER_STEP_SECONDS.values()))


def expected_durations(
    keys: Sequence[StageKey], *, budget_seconds: float | None, past: PastDurations
) -> Expectations | None:
    """The expected duration of each stage in `keys`, or `None` when the train stage has no basis.

    The train stage: the median of what the same settings took before (`past.train` holds only finished runs of
    the same trainer and the same search budget, so a budget raised since - 5 minutes to 60 - leaves no run to go
    by and cannot hold the bar at "Finishing up" for most of the run), never more than the budget. With no such
    run: the search's wall-clock budget, a limit (`time_budget`, the one basis the screens say "up to" about), or
    for a trainer with no budget nothing, and then there is no estimate - a guess at the stage that is almost all
    of the run would be a number that means nothing. Every other stage: the median of past runs, else
    `DEFAULT_STAGE_SECONDS`.
    """
    seconds: dict[StageKey, float] = {}
    from_history = False
    basis: EstimateBasis | None = None
    for key in keys:
        if key is StageKey.TRAIN:
            if past.train:
                value = statistics.median(past.train)
                limited = budget_seconds is not None and value >= budget_seconds
                value = budget_seconds if limited and budget_seconds is not None else value
                basis = "time_budget" if limited else "past_runs"
            elif budget_seconds is not None:
                value = budget_seconds
                basis = "time_budget"
            else:
                return None
        else:
            durations = past.stages.get(key, ())
            if durations:
                value = statistics.median(durations)
                from_history = True
            else:
                value = DEFAULT_STAGE_SECONDS.get(key, MIN_STAGE_SECONDS * 5)
        seconds[key] = max(MIN_STAGE_SECONDS, float(value))
    if basis is None:
        basis = "past_runs" if from_history else "stages"
    return Expectations(seconds=MappingProxyType(seconds), basis=basis)


def past_durations(
    storage: Storage,
    *,
    use_case_id: str,
    mode: RunMode,
    trainer: str | None,
    exclude_run_id: str,
    budget_seconds: float | None = None,
) -> PastDurations:
    """Stage durations of the newest finished runs of this use case and mode, read from their `status.json`.

    Run ids sort chronologically, so a reversed key listing is newest first (the same walk as `GET
    /runs`). At most `SCANNED_RECORDS` records are read and `PAST_RUNS` runs kept. A train duration is
    kept only when the run's `run_config.json` names the same trainer (`trainer_of`) and the same search
    budget as `budget_seconds` (a search given 60 minutes says nothing of one given 5).
    """
    from engine.pipeline import RUN_FILENAME, STATUS_FILENAME
    from engine.storage import StorageError, run_key

    stages: dict[StageKey, list[float]] = {}
    train: list[float] = []
    scanned = matched = 0
    for key in sorted(storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        scanned += 1
        if scanned > SCANNED_RECORDS or matched >= PAST_RUNS:
            break
        try:
            record = storage.read_model(key, RunRecord)
            if (
                record.run_id == exclude_run_id
                or record.use_case_id != use_case_id
                or record.mode is not mode
                or record.state is not RunState.DONE
            ):
                continue
            status = storage.read_model(run_key(record.run_id, STATUS_FILENAME), RunStatus)
        except (StorageError, ValueError):
            continue
        matched += 1
        same_settings = trainer is not None and _recorded_settings(storage, record.run_id) == (
            trainer,
            budget_seconds,
        )
        for row in status.stages:
            if row.state is not RunState.DONE or row.duration_seconds is None:
                continue
            if row.key is StageKey.TRAIN:
                if same_settings:
                    train.append(row.duration_seconds)
            else:
                stages.setdefault(row.key, []).append(row.duration_seconds)
    return PastDurations(
        stages=MappingProxyType({key: tuple(values) for key, values in stages.items()}), train=tuple(train)
    )


def _recorded_settings(storage: Storage, run_id: str) -> tuple[str, float | None] | None:
    """The trainer a past run used and its search budget in seconds (None for a learner with no budget), read from
    the problem type, the uplift base model and the time limits in its `run_config.json`; None when unreadable.
    """
    import json

    from engine.runs import RUN_CONFIG_FILENAME
    from engine.storage import StorageError, run_key

    try:
        config = json.loads(storage.read_bytes(run_key(run_id, RUN_CONFIG_FILENAME)))["config"]
        if config.get("problem_type") == ProblemType.UPLIFT.value:
            base = config["uplift"]["base_model"]
            limit = config["uplift"].get("time_limit_minutes") if base == "autogluon_fast" else None
            return f"uplift:{base}", None if limit is None else float(limit) * 60
        return "automl", float(config["model_search"]["time_limit_minutes"]) * 60
    except (StorageError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# The estimate itself (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Estimate:
    """One reading of the estimate: what `status.json` carries beside `progress_pct`."""

    pct: int
    eta_seconds: int | None
    fraction: float | None
    """The running stage's credited share of its expected duration; `None` when no stage is running."""
    overrun: bool
    """Whether the running stage has run past its expected duration."""


def estimate_progress(
    stages: Sequence[StageStatus],
    expected: Mapping[StageKey, float],
    *,
    running_elapsed: float | None,
    floor: int = 0,
) -> Estimate:
    """The time-weighted estimate for `stages`, never below `floor` (the last value this run showed).

    `running_elapsed` is how long the running stage has run, in seconds (`None` when it has only just
    started, or when no stage is running). Every stage done is 100 with no time left (`eta_seconds` is
    null for every ended run, as the contract says); a run that failed or was cancelled keeps `floor`.
    """
    if stages and all(row.state is RunState.DONE for row in stages):
        return Estimate(pct=100, eta_seconds=None, fraction=None, overrun=False)
    if any(row.state in (RunState.FAILED, RunState.CANCELLED) for row in stages):
        return Estimate(pct=min(floor, 99), eta_seconds=None, fraction=None, overrun=False)

    def weight(row: StageStatus) -> float:
        return max(MIN_STAGE_SECONDS, expected.get(row.key, DEFAULT_STAGE_SECONDS.get(row.key, 5.0)))

    total = sum(weight(row) for row in stages)
    credited = 0.0
    remaining = 0.0
    fraction: float | None = None
    overrun = False
    for row in stages:
        if row.state is RunState.DONE:
            credited += weight(row)
        elif row.state is RunState.RUNNING and fraction is None:
            share = 0.0 if running_elapsed is None else max(0.0, running_elapsed) / weight(row)
            overrun = share > 1.0
            fraction = min(share, OVERRUN_FRACTION)
            credited += weight(row) * fraction
            remaining += max(0.0, weight(row) - (running_elapsed or 0.0))
        elif row.state is RunState.PENDING or row.state is RunState.RUNNING:
            remaining += weight(row)
    pct = 0 if total <= 0 else math.floor(100 * credited / total)
    pct = min(99, max(floor, pct))
    eta = None if overrun else math.ceil(remaining)
    return Estimate(pct=pct, eta_seconds=eta, fraction=fraction, overrun=overrun)


# ---------------------------------------------------------------------------
# One run's tracker: the estimate on every status write
# ---------------------------------------------------------------------------
class ProgressTracker:
    """Decorates every `status.json` one run writes with the estimate, and keeps it monotonic.

    `lock` is held around every decorated write - the flow's own transitions and the heartbeat's - so
    the two never interleave and the last document written is always the newest state.
    """

    def __init__(self, expectations: Expectations, *, clock: Callable[[], float] | None = None) -> None:
        self.lock = threading.RLock()
        self._expectations = expectations
        self._clock = clock if clock is not None else (lambda: CLOCK())
        self._floor = 0
        self._running: tuple[StageKey, float] | None = None

    @property
    def expectations(self) -> Expectations:
        return self._expectations

    def stage_started(self, key: StageKey) -> None:
        """Start the clock on `key`."""
        with self.lock:
            self._running = (key, self._clock())

    def stage_ended(self) -> None:
        """Stop the clock; the next write credits whatever the stage ended as."""
        with self.lock:
            self._running = None

    def decorate(self, status: RunStatus) -> RunStatus:
        """`status` with `estimated_pct`, `eta_seconds`, `estimate_basis` and the running stage's `fraction`."""
        with self.lock:
            elapsed: float | None = None
            if self._running is not None:
                key, started = self._running
                if any(row.key is key and row.state is RunState.RUNNING for row in status.stages):
                    elapsed = self._clock() - started
            estimate = estimate_progress(
                status.stages, self._expectations.seconds, running_elapsed=elapsed, floor=self._floor
            )
            self._floor = estimate.pct
            rows = tuple(
                row.model_copy(
                    update={
                        "fraction": (
                            round(estimate.fraction, 3)
                            if row.state is RunState.RUNNING and estimate.fraction is not None
                            else None
                        )
                    }
                )
                for row in status.stages
            )
            return status.model_copy(
                update={
                    "stages": rows,
                    "estimated_pct": estimate.pct,
                    "eta_seconds": estimate.eta_seconds,
                    "estimate_basis": self._expectations.basis,
                }
            )


class _EstimatingStore:
    """The status writer's store, seen through the tracker: a `RunStatus` is decorated on its way out.

    Only `write_model` differs; everything else is the store itself. A decoration that fails is logged
    and the document is written as it was, so the estimate can never cost a run its status.
    """

    def __init__(self, inner: Any, tracker: ProgressTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def write_model(self, key: str, model: Any) -> None:
        with self._tracker.lock:
            if isinstance(model, RunStatus):
                try:
                    model = self._tracker.decorate(model)
                except Exception:  # the estimate is a nicety; the status write is not
                    _LOGGER.exception("progress estimate failed; status written without it")
            self._inner.write_model(key, model)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Heartbeat:
    """A daemon thread that calls `beat` every `interval` seconds until `beat` says stop or `stop()`."""

    def __init__(self, beat: Callable[[], bool], interval: float, name: str) -> None:
        self._beat = beat
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._beat():
                    return
            except Exception:  # a heartbeat that cannot write stops; the run carries on
                _LOGGER.exception("progress heartbeat failed; it stops, the run carries on")
                return

    def stop(self) -> None:
        """Stop and wait for the thread, so nothing is written by it after this returns."""
        self._stop.set()
        self._thread.join()


# ---------------------------------------------------------------------------
# Wiring it into the pipeline's flows (called from engine/pipeline.py's PLAN-M block)
# ---------------------------------------------------------------------------
def install_progress_estimate(*flows: type[Any]) -> None:
    """Wrap each flow's `_run_stage` so its status writes carry the estimate and long stages beat.

    Sets `<flow>._run_progress_installed`, which a guard test checks after `import engine.pipeline`.
    A subclass (the uplift flows) inherits the wrapped method.
    """
    for flow in flows:
        if flow.__dict__.get(_INSTALLED, False):
            continue
        original: Callable[[Any, StageKey, Callable[[], Any]], None] = flow._run_stage

        def _run_stage(
            self: Any,
            key: StageKey,
            body: Callable[[], Any],
            _original: Callable[[Any, StageKey, Callable[[], Any]], None] = original,
        ) -> None:
            tracker = _tracker_for(self)
            if tracker is None:
                _original(self, key, body)
                return
            tracker.stage_started(key)
            heartbeat = _start_heartbeat(self, tracker, key) if key in BEATING_STAGES else None
            try:
                _original(self, key, body)
            finally:
                if heartbeat is not None:
                    heartbeat.stop()
                tracker.stage_ended()

        flow._run_stage = _run_stage
        setattr(flow, _INSTALLED, True)


def _tracker_for(flow: Any) -> ProgressTracker | None:
    """The flow's tracker, made on its first stage (and its status writer's store wrapped); `None` if none."""
    if hasattr(flow, _TRACKER_ATTR):
        return cast("ProgressTracker | None", getattr(flow, _TRACKER_ATTR))
    tracker: ProgressTracker | None = None
    try:
        tracker = _make_tracker(flow)
    except Exception:  # never fail a run over its progress bar
        _LOGGER.exception("no progress estimate for this run: its expected durations could not be read")
    if tracker is not None:
        writer = flow._status
        writer._storage = _EstimatingStore(writer._storage, tracker)
    setattr(flow, _TRACKER_ATTR, tracker)
    return tracker


def _make_tracker(flow: Any) -> ProgressTracker | None:
    ctx = flow._ctx
    config = ctx.config
    mode: RunMode = ctx.mode
    keys = tuple(row.key for row in flow._status.status.stages)
    train = mode is RunMode.TRAIN
    if train:
        # Plan O M244 (DEC-1814): a budget chosen from Data Doctor's three choices brings the expectation it was
        # shown with, so the first ETA is the promise; when the run ends, actual against expected is recorded.
        # Any failure here keeps the estimate below.
        try:
            from engine.doctor.time_settings import promised_tracker

            promised = promised_tracker(flow, keys)
        except Exception:  # the promise is a nicety; M189's estimate stands
            _LOGGER.exception("progress estimate: the time promise for run %s could not be read", ctx.run_id)
            promised = None
        if promised is not None:
            return promised
    budget = train_budget_seconds(config) if train else None
    past = past_durations(
        ctx.storage,
        use_case_id=config.id,
        mode=mode,
        trainer=trainer_of(config) if train else None,
        exclude_run_id=ctx.run_id,
        budget_seconds=budget,
    )
    expectations = expected_durations(keys, budget_seconds=budget, past=past)
    if expectations is None:
        _LOGGER.info("progress estimate: none for run %s (no time budget, no past run)", ctx.run_id)
        return None
    _LOGGER.info(
        "progress estimate: run=%s basis=%s expected=%.0fs",
        ctx.run_id,
        expectations.basis,
        sum(expectations.seconds.values()),
    )
    return ProgressTracker(expectations)


def _start_heartbeat(flow: Any, tracker: ProgressTracker, key: StageKey) -> _Heartbeat:
    """Rewrite `status.json` every `HEARTBEAT_SECONDS` while `key` is the running stage of a live run.

    Each beat is a compare-and-write under the tracker's lock. It writes only when the flow's token is not
    cancelled, the flow's own status still has `key` running, `run.json` has not ended, and the stored
    `status.json` is still the one this flow (or this heartbeat) wrote last: not terminal, and not newer.
    `runs.cancel_run` (the cancel route, the cost cap, SageMaker's reconcile) and `runs.fail_run` write
    `status.json` and then `run.json` without the flow's lock, and on SageMaker the flow's token is set
    only at the SIGTERM that follows, so the token alone cannot tell; the stored documents can, and the
    heartbeat stops for good the first time it sees someone else's write.

    Across processes no lock is shared, so one write of the heartbeat's already on its way can still land
    on top of that ending. The next beat then finds `run.json` ended under a `status.json` that is not, and
    puts the ending back (`runs.restore_ending`) before it stops.
    """
    from engine.pipeline import RUN_FILENAME, STATUS_FILENAME
    from engine.runs import restore_ending
    from engine.storage import StorageError, run_key

    writer = flow._status
    cancel = flow._ctx.cancel
    status_key = run_key(flow._ctx.run_id, STATUS_FILENAME)
    record_key = run_key(flow._ctx.run_id, RUN_FILENAME)
    last_beat: list[datetime] = []  # the `updated_at` this heartbeat last wrote

    def beat() -> bool:
        if cancel.cancelled:
            return False  # the cancel has written, or is writing, the ending; never overwrite it
        with tracker.lock:
            if cancel.cancelled:
                return False
            status: RunStatus = writer.status
            if any(row.key is key and row.state is RunState.PENDING for row in status.stages):
                return True  # the flow has not written this stage's start yet; wait for it, do not give up
            if status.state is not RunState.RUNNING or status.current_stage is not key:
                return False
            raw = getattr(writer._storage, "_inner", writer._storage)  # undecorated: an ending is not ours
            try:
                record: RunRecord | None = raw.read_model(record_key, RunRecord)
            except (StorageError, ValueError):
                record = None  # the status check below still guards the write
            if record is not None and record.state in _ENDED:
                if restore_ending(raw, record) is not None:
                    _LOGGER.warning(
                        "progress heartbeat: run %s status.json put back to %s",
                        status.run_id,
                        record.state.value,
                    )
                return False
            try:
                stored = raw.read_model(status_key, RunStatus)
            except (StorageError, ValueError):
                _LOGGER.warning(
                    "progress heartbeat: run %s status.json unreadable; beat skipped", status.run_id
                )
                return True
            ours = max([status.updated_at, *last_beat])
            if stored.state in _ENDED or stored.updated_at > ours:
                _LOGGER.info(
                    "progress heartbeat: run %s status.json was written outside the flow (%s); it stops",
                    status.run_id,
                    stored.state.value,
                )
                return False
            moment = max(utc_now(), ours)
            writer._storage.write_model(status_key, status.model_copy(update={"updated_at": moment}))
            last_beat[:] = [moment]
        return True

    return _Heartbeat(beat, HEARTBEAT_SECONDS, name=f"progress-{flow._ctx.run_id}-{key.value}")
