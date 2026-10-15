"""The campaign run: learn from the customers, score them, build the contact list and record the campaign.

The wizard used to chain these four steps in the browser, so the work stopped when the tab closed and a
refresh lost the file. A campaign run is one small record on the server (`campaign_runs/<id>/campaign_run.json`)
with a fixed list of steps. `CampaignRunner.advance` is a tick: it reads the record, does whatever the current
step allows right now and writes the record back. It never waits on a run, so a two-worker job pool is never
starved by a parent waiting for its children. A daemon thread ticks every active record while there is one,
`GET /campaign-runs/{id}` ticks the one it reads, and the app's startup ticks what a restart left behind.

The heavy steps are ordinary runs started through `StepActions` (`POST /runs`' own code, wired in
`api/routes/campaign_runs.py`); this module only sequences them. The first campaign scores the customers the
model just learned from, with the model that was just trained even while it waits for approval: approving it
is the person's decision at "Approve and download", where the plan and the download refuse an unapproved model.

One server per data directory is assumed (the pilot's shape): a training run found "running" whose job this
process does not know is dead, because the job runner is in memory and nothing else could be running it. A
runner that can lose jobs on its own (SageMaker) reconciles instead and is never judged this way.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import AwareDatetime, Field

from engine.config import PrimaryKey
from engine.contracts import Artefact, RunError, RunRecord, RunState, RunStatus
from engine.jobs import JobRunner, ReconcilingJobRunner
from engine.runs import cancel_run, fail_run
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

_LOGGER = get_logger(__name__)

__all__ = [
    "SPEED_MINUTES",
    "CampaignRun",
    "CampaignRunRunner",
    "CampaignRunState",
    "CampaignRunStore",
    "CampaignRunView",
    "StepActions",
    "StepError",
    "overrides_for",
]

RECORD_FILENAME: Final[str] = "campaign_run.json"
PREFIX: Final[str] = "campaign_runs/"


class CampaignRunState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    STOPPED = "stopped"  # it cannot go on and says why (the server restarted, nothing usable was learned)
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE: Final[frozenset[CampaignRunState]] = frozenset({CampaignRunState.RUNNING})


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class Speed(StrEnum):
    QUICK = "quick"
    BALANCED = "balanced"
    THOROUGH = "thorough"


SPEED_MINUTES: Final[dict[Speed, int]] = {Speed.QUICK: 5, Speed.BALANCED: 15, Speed.THOROUGH: 40}
"""`model_search.time_limit_minutes` for each choice: the "about N minutes" the Doctor's time question offers."""

STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("train", "Learning from your customers"),
    ("score", "Scoring your customers"),
    ("list", "Building your contact list"),
    ("campaign", "Preparing your campaign"),
)
_WEIGHT: Final[dict[str, int]] = {"train": 70, "score": 20, "list": 5, "campaign": 5}
_ENDED: Final[dict[RunState, str]] = {
    RunState.DONE: "done",
    RunState.FAILED: "failed",
    RunState.CANCELLED: "cancelled",
}
_LATER_SECONDS: Final[dict[str, int]] = {"score": 60, "list": 20, "campaign": 10}
"""Rough seconds the steps after the running one still need, for the overall time left."""


class CampaignStep(Artefact):
    key: str
    title: str
    state: StepState = StepState.PENDING


class CampaignRunError(Artefact):
    code: str
    message: str
    step: str | None = None


class CampaignRun(Artefact):
    """`campaign_run.json`: what was asked, where each step is and the ids it made. No customer data."""

    campaign_run_id: str
    state: CampaignRunState = CampaignRunState.RUNNING
    created_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    use_case_id: str
    upload_id: str | None = None
    dataset_id: str | None = None
    client_id: str | None = None
    primary_key: PrimaryKey | None = None
    target: str | None = None
    speed: Speed = Speed.BALANCED
    overrides: dict[str, Any] = Field(default_factory=dict)
    confirm_cost: bool = False
    requested_by: str | None = None
    steps: tuple[CampaignStep, ...]
    model_version_id: str | None = Field(
        default=None, description="Given to skip training, else the version the training run made."
    )
    train_run_id: str | None = None
    score_upload_id: str | None = None
    score_run_id: str | None = None
    campaign_id: str | None = None
    cancel_requested: bool = False
    error: CampaignRunError | None = None


class CampaignRunView(Artefact):
    """What `GET /campaign-runs/{id}` answers: the record and the one-line picture of how far it is."""

    record: CampaignRun
    pct: int
    eta_seconds: int | None = None
    summary: str


class StepError(Exception):
    """A step cannot go on, in words the person can read (`code` is for the screen's own handling)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StepActions:
    """What the runner calls to do the work; the API wires them to the code `POST /runs` and `POST /campaigns` use."""

    start_train: Callable[[CampaignRun], str]
    clone_upload: Callable[[CampaignRun], str]
    start_score: Callable[[CampaignRun], str]
    usable_model: Callable[[str], str | None]
    """The model a finished training run made, if the contact list can use it (champion or waiting), else the
    model in use, else None."""
    build_list: Callable[[str], None]
    open_campaign: Callable[[CampaignRun], str | None]
    """Records the campaign once; None for a goal that has no campaign."""


def overrides_for(overrides: dict[str, Any], speed: Speed) -> dict[str, Any]:
    """The training run's overrides: the person's own, plus the time of the chosen speed unless they set one."""
    own = "model_search.time_limit_minutes" in overrides or (
        isinstance(overrides.get("model_search"), dict) and "time_limit_minutes" in overrides["model_search"]
    )
    return overrides if own else {**overrides, "model_search.time_limit_minutes": SPEED_MINUTES[speed]}


def new_campaign_run_id(now: datetime | None = None) -> str:
    return f"cr_{(now or utc_now()).strftime('%Y%m%d')}_{secrets.token_hex(4)}"


class CampaignRunStore:
    """Campaign runs on the same `Storage` as runs; the per-record lock lives in the runner."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def key(self, campaign_run_id: str) -> str:
        return f"{PREFIX}{campaign_run_id}/{RECORD_FILENAME}"

    def save(self, run: CampaignRun) -> CampaignRun:
        stamped = run.model_copy(update={"updated_at": utc_now()})
        self._storage.write_model(self.key(run.campaign_run_id), stamped)
        return stamped

    def get(self, campaign_run_id: str) -> CampaignRun | None:
        try:
            return self._storage.read_model(self.key(campaign_run_id), CampaignRun)
        except (StorageError, ValueError):
            return None

    def list(self, *, state: CampaignRunState | None = None, limit: int = 50) -> list[CampaignRun]:
        """Newest first (ids sort by date)."""
        kept: list[CampaignRun] = []
        for key in sorted(self._storage.list_keys(PREFIX), reverse=True):
            if not key.endswith(f"/{RECORD_FILENAME}"):
                continue
            run = self.get(key[len(PREFIX) : -len(RECORD_FILENAME) - 1])
            if run is None or (state is not None and run.state is not state):
                continue
            kept.append(run)
            if len(kept) == limit:
                break
        return kept

    def create(self, **fields: Any) -> CampaignRun:
        now = utc_now()
        steps = tuple(CampaignStep(key=key, title=title) for key, title in STEPS)
        return self.save(
            CampaignRun(
                campaign_run_id=new_campaign_run_id(now),
                created_at=now,
                updated_at=now,
                steps=steps,
                **fields,
            )
        )


class CampaignRunRunner:
    """Advances campaign runs one step at a time; see the module docstring."""

    def __init__(
        self,
        storage: Storage,
        jobs: JobRunner,
        actions: StepActions,
        *,
        store: CampaignRunStore | None = None,
        tick_seconds: float = 3.0,
    ) -> None:
        self.store = store or CampaignRunStore(storage)
        self._storage = storage
        self._jobs = jobs
        self._actions = actions
        self._tick_seconds = tick_seconds
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- the driver ---------------------------------------------------------------------------------
    def ensure_running(self) -> None:
        """Start the ticking thread when a run is active and none is ticking. It ends when nothing is active."""
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.store.list(state=CampaignRunState.RUNNING, limit=1):
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._drive, name="campaign-run", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _drive(self) -> None:
        while not self._stop.is_set():
            active = self.store.list(state=CampaignRunState.RUNNING)
            if not active:
                with self._guard:
                    if not self.store.list(state=CampaignRunState.RUNNING, limit=1):
                        self._thread = None
                        return
                continue
            for run in active:
                try:
                    self.advance(run.campaign_run_id)
                except Exception as exc:  # one broken record must not stop the others
                    log_failure(_LOGGER, f"campaign_run.tick id={run.campaign_run_id}", exc)
            self._stop.wait(self._tick_seconds)

    # -- the tick -----------------------------------------------------------------------------------
    def advance(self, campaign_run_id: str, *, wait: bool = True) -> CampaignRun | None:
        """Do what the run can do now. With `wait=False` a run another thread is advancing is returned as it is."""
        with self._guard:
            lock = self._locks.setdefault(campaign_run_id, threading.Lock())
        if not lock.acquire(blocking=wait):
            return self.store.get(campaign_run_id)
        try:
            run = self.store.get(campaign_run_id)
            while run is not None and run.state in ACTIVE:
                moved = self._step(run)
                if moved is run:
                    break
                run = self.store.save(moved)
            return run
        finally:
            lock.release()

    def cancel(self, campaign_run_id: str) -> CampaignRun | None:
        with self._guard:
            lock = self._locks.setdefault(campaign_run_id, threading.Lock())
        with lock:
            run = self.store.get(campaign_run_id)
            if run is None or run.state not in ACTIVE:
                return run
            for child in (run.train_run_id, run.score_run_id):
                if child is not None and self._child(child) == "running":
                    self._jobs.cancel(child)
                    cancel_run(self._storage, child)
            return self.store.save(
                run.model_copy(
                    update={
                        "state": CampaignRunState.CANCELLED,
                        "cancel_requested": True,
                        "finished_at": utc_now(),
                    }
                )
            )

    def _step(self, run: CampaignRun) -> CampaignRun:
        """One move of the first unfinished step; returns `run` itself when there is nothing to do yet."""
        current = next((s for s in run.steps if s.state not in (StepState.DONE, StepState.SKIPPED)), None)
        if current is None:
            return self._end(run, CampaignRunState.DONE)
        try:
            handler = {
                "train": self._train,
                "score": self._score,
                "list": self._list,
                "campaign": self._campaign,
            }
            return handler[current.key](run, current.key)
        except StepError as exc:
            return self._end(run, CampaignRunState.FAILED, current.key, exc.code, exc.message)
        except Exception as exc:  # the record says so, the log has the trace
            log_failure(_LOGGER, f"campaign_run.step id={run.campaign_run_id} step={current.key}", exc)
            return self._end(
                run,
                CampaignRunState.FAILED,
                current.key,
                "CAMPAIGN_RUN_STEP_FAILED",
                f"{current.title} did not finish. Try again; if it keeps happening, ask your administrator.",
            )

    def _train(self, run: CampaignRun, key: str) -> CampaignRun:
        if run.model_version_id is not None and run.train_run_id is None:
            return self._mark(run, key, StepState.SKIPPED)  # a model was named: nothing to learn
        if run.train_run_id is None:
            started = self._actions.start_train(run)
            return self._mark(run, key, StepState.RUNNING, train_run_id=started)
        outcome = self._child(run.train_run_id)
        if outcome == "running":
            return run
        if outcome != "done":
            return self._child_ended(run, key, run.train_run_id, outcome)
        model_id = self._actions.usable_model(run.train_run_id)
        if model_id is None:
            return self._end(
                run,
                CampaignRunState.STOPPED,
                key,
                "NO_USABLE_MODEL",
                "Nothing was learned that the contact list can use.",
            )
        return self._mark(run, key, StepState.DONE, model_version_id=model_id)

    def _score(self, run: CampaignRun, key: str) -> CampaignRun:
        if run.score_run_id is None:
            if run.dataset_id is None and run.score_upload_id is None:
                run = run.model_copy(update={"score_upload_id": self._actions.clone_upload(run)})
            return self._mark(run, key, StepState.RUNNING, score_run_id=self._actions.start_score(run))
        outcome = self._child(run.score_run_id)
        if outcome == "running":
            return run
        if outcome != "done":
            return self._child_ended(run, key, run.score_run_id, outcome)
        return self._mark(run, key, StepState.DONE)

    def _list(self, run: CampaignRun, key: str) -> CampaignRun:
        assert run.score_run_id is not None
        self._actions.build_list(run.score_run_id)
        return self._mark(run, key, StepState.DONE)

    def _campaign(self, run: CampaignRun, key: str) -> CampaignRun:
        campaign_id = self._actions.open_campaign(run)
        marked = self._mark(run, key, StepState.DONE, campaign_id=campaign_id)
        return self._end(marked, CampaignRunState.DONE)

    # -- helpers ------------------------------------------------------------------------------------
    def _mark(self, run: CampaignRun, key: str, state: StepState, **changes: Any) -> CampaignRun:
        steps = tuple(s.model_copy(update={"state": state}) if s.key == key else s for s in run.steps)
        return run.model_copy(update={"steps": steps, **changes})

    def _end(
        self,
        run: CampaignRun,
        state: CampaignRunState,
        step: str | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> CampaignRun:
        changes: dict[str, Any] = {"state": state, "finished_at": utc_now()}
        if code is not None and message is not None:
            changes["error"] = CampaignRunError(code=code, message=message, step=step)
            if state is CampaignRunState.FAILED and step is not None:
                changes["steps"] = self._mark(run, step, StepState.FAILED).steps
        return run.model_copy(update=changes)

    def _child_ended(self, run: CampaignRun, key: str, child: str, outcome: str) -> CampaignRun:
        title = dict(STEPS)[key]
        if outcome == "gone":
            # One server: its job runner forgot the job, so the work stopped with the last restart.
            fail_run(
                self._storage,
                child,
                RunError(code="RUN_INTERRUPTED", message="The server restarted while this ran.", stage=None),
            )
            return self._end(
                run,
                CampaignRunState.STOPPED,
                key,
                "RUN_INTERRUPTED",
                f"{title} stopped because the server restarted. Start the campaign again.",
            )
        if outcome == "cancelled":
            return self._end(run, CampaignRunState.STOPPED, key, "RUN_CANCELLED", f"{title} was stopped.")
        record = self._record(child)
        reason = (
            record.error.message if record is not None and record.error is not None else "it did not finish"
        )
        return self._end(run, CampaignRunState.FAILED, key, "RUN_FAILED", f"{title} failed: {reason}")

    def _record(self, run_id: str) -> RunRecord | None:
        try:
            return self._storage.read_model(run_key(run_id, "run.json"), RunRecord)
        except (StorageError, ValueError):
            return None

    def _child(self, run_id: str) -> str:
        """`running`, `done`, `failed`, `cancelled` or `gone` (a run the job runner no longer knows)."""
        if isinstance(self._jobs, ReconcilingJobRunner):
            try:
                self._jobs.reconcile(run_id)
            except Exception:  # a control plane that cannot be reached does not make the stored record untrue
                _LOGGER.warning("campaign_run.reconcile failed run=%s", run_id)
        record = self._record(run_id)
        if record is None:
            return "gone"
        if record.state in _ENDED:
            return _ENDED[record.state]
        if not isinstance(self._jobs, ReconcilingJobRunner):
            try:
                job = self._jobs.status(run_id)
            except KeyError:
                return "gone"
            if job.state in _ENDED:
                # The job ended but its record did not say so (it broke before the pipeline could write it).
                # Read again in case the record was written between the two reads, else record the failure.
                again = self._record(run_id)
                if again is not None and again.state in _ENDED:
                    return _ENDED[again.state]
                fail_run(
                    self._storage,
                    run_id,
                    RunError(
                        code="RUN_FAILED",
                        message="The work stopped early. The server log has the details.",
                        stage=None,
                    ),
                )
                return "failed"
        return "running"

    # -- how far it is ------------------------------------------------------------------------------
    def view(self, run: CampaignRun) -> CampaignRunView:
        live = [s for s in run.steps if s.state is not StepState.SKIPPED]
        total = sum(_WEIGHT[s.key] for s in live) or 1
        current = next((s for s in live if s.state is not StepState.DONE), None)
        gained = float(sum(_WEIGHT[s.key] for s in live if s.state is StepState.DONE))
        eta: int | None = None
        if run.state is CampaignRunState.DONE:
            gained, eta = float(total), 0
        elif current is not None and current.state is StepState.RUNNING:
            child = run.train_run_id if current.key == "train" else run.score_run_id
            status = self._status(child) if child is not None else None
            if status is not None:
                share = status.estimated_pct if status.estimated_pct is not None else status.progress_pct
                gained += _WEIGHT[current.key] * share / 100
                if status.eta_seconds is not None:
                    later = live[live.index(current) + 1 :]
                    eta = status.eta_seconds + sum(_LATER_SECONDS.get(s.key, 0) for s in later)
        pct = 100 if run.state is CampaignRunState.DONE else min(99, round(gained * 100 / total))
        return CampaignRunView(record=run, pct=pct, eta_seconds=eta, summary=_summary(run, current, eta))

    def _status(self, run_id: str) -> RunStatus | None:
        try:
            return self._storage.read_model(run_key(run_id, "status.json"), RunStatus)
        except (StorageError, ValueError):
            return None


def _summary(run: CampaignRun, current: CampaignStep | None, eta: int | None) -> str:
    if run.state is CampaignRunState.DONE:
        return "Your campaign is ready."
    if run.error is not None:
        return run.error.message
    if run.state is CampaignRunState.CANCELLED:
        return "This campaign run was cancelled."
    title = current.title if current is not None else "Finishing"
    if eta is None:
        return f"{title}."
    return f"{title}: about {max(1, round(eta / 60))} min left."
