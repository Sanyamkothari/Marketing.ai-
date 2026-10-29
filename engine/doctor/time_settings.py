r"""Time settings, owned by Data Doctor (Plan O M244, DEC-1814; the design §3.5, [REQ] 10).

"We do not know what to expect." Nobody should have to guess how long training takes or which dates to use. This
module is the one place that says, from the data and this deployment's own past runs:

**(a) The training time budget.**

* :func:`estimate` predicts the minutes a run takes from rows x columns x the model families the goal allows x this
  deployment's past runs. Its source is the one M189's progress bar reads: the stage durations each finished run
  stored in its `status.json` (`engine/pipeline.py` `_StatusWriter.finish`, `record_stage_duration`), read through
  `engine.run_progress.past_durations` for every stage but training. The training stage's natural length follows
  a power of the work (``seconds = a x work^b``, ``work = rows x columns x families``), fitted on the past runs that
  finished before their budget; with no such run the public library's runs are the prior (the telco and bank
  reports in `library/`) and the estimate says so: "from similar public datasets". The search stops at its budget,
  so the training stage is expected to take the smaller of the two.
* :func:`propose_budget` finds the point of diminishing returns: past runs' leaderboards give each run's best
  validation score against the time spent fitting (scaled to this file's size); **Balanced** is the first budget
  after which the similar runs gained less than `data_doctor.time_settings.score_step_per_minute` per extra minute.
  **Quick** and **Thorough** are Balanced times the configured ratios. Each choice says "about N minutes" - the whole
  run, every stage - and what it buys ("Thorough has found a better model on 2 of 9 similar past runs"). It is
  never a raw minutes field. Balanced is `sure` (offered ticked) only when at least `sure_min_runs` past runs here
  were close in size; otherwise `check`.
* **The promise.** The offer (all three choices with their expectations) is kept beside the data
  (`uploads/<id>/time_offers/<use case>.json`, or `datasets/<id>/...`), every offer for that file and goal, newest
  first, so a choice already taken stays matchable after a newer run moves the next offer. Guided setup's advisor
  (`engine.agent.advisor`, with the deployment's store) and the visit's detector make the offer from the same
  computation as the setting they suggest. A training run whose budget is one of the offered choices takes that
  choice's expectation as its progress estimate (:func:`promised_tracker`, called from `engine.run_progress`), so
  the bar's first ETA is the promise, to the second; a limit set some other way is promised :func:`estimate` of it
  from the same history. The run keeps the promise (`runs/<id>/time_promise.json`).
* **Learning.** When the run ends, actual against expected is recorded per stage (`runs/<id>/time_record.json`,
  :func:`record_run_time`). The next estimate corrects each stage by the median of actual over the rules' own
  estimate in the last ten records, so a bias (a search that runs past its budget, a slow explanation stage) is
  learnt rather than repeated.

**(b) The data time settings** (:func:`propose_time_settings`): the date each row describes (as-of: decided only
when the goal or its settings name it, or the person chose it; a candidate must have a date on nearly every row, so
an outcome's own date - filled only where the outcome is yes - is never one), feasible
time-split cut points (the split's own arithmetic: a date never straddles a cut, and a cut that leaves a part empty
is never proposed - `X_TIME_CUTS_FEASIBLE`), whether the newest slice is mature (`X_LABEL_MATURITY`: "Your newest
month's outcomes are not final yet. Leave it out of testing?"), the outcome window and the lapse definition (both
part of the outcome definition, APPROVE-class: settled through `define_outcome`, never here), and the snapshot
cadence. :func:`time_settings_asks` turns them into M246's asks (:class:`engine.doctor.protocol.Ask`), so the
protocol decides AUTO, a stated assumption or one question, and M246's standing answers remember the person's answer
per client for the as-of date, the cut points, the maturity choice and the cadence (`AS_OF_COLUMN`, `TIME_CUTS`,
`LABEL_MATURITY`, `SNAPSHOT_CADENCE`). Each answer's item carries the change itself - a setting's path and value,
the recipe step that leaves the newest rows out (with its cut-off, worked out again on each file), the build's
snapshot frequency - and :func:`time_setting_changes` / :func:`apply_time_settings` read them back.

The budgets replace the old file-size rule (`_time_rules`, removed), and the progress bar follows them.
"""

from __future__ import annotations

import json
import math
import statistics
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field, JsonValue

from engine.config import RunMode, StrictBase
from engine.contracts import RunState, RunStatus, StageKey
from engine.doctor.decisions import decisions
from engine.run_progress import (
    DEFAULT_STAGE_SECONDS,
    MIN_STAGE_SECONDS,
    Expectations,
    PastDurations,
    ProgressTracker,
    expected_run_minutes,
)
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.doctor.case import Case
    from engine.doctor.config import TimeSettingsConfig
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.protocol import Ask
    from engine.doctor.visit import Detected, DetectorPort
    from engine.storage import Storage

__all__ = [
    "CHOICES",
    "MAX_OFFERS",
    "OFFERS_DIR",
    "PRIOR_RUNS",
    "PROMISE_FILENAME",
    "RECORD_FILENAME",
    "SNAPSHOT_SPEC_PATH",
    "TIME_CODES",
    "TIME_SETTING_NAMES",
    "TRAIN_STAGE_KEYS",
    "AsOf",
    "BudgetChoice",
    "Cadence",
    "CutPoints",
    "Lapse",
    "Maturity",
    "OutcomeWindow",
    "PastRun",
    "PromiseTracker",
    "Shape",
    "StageTime",
    "TimeChanges",
    "TimeInputs",
    "TimeOffer",
    "TimeOffers",
    "TimePromise",
    "TimeRecord",
    "TimeSettings",
    "TimeSettingsError",
    "TrainingBudget",
    "TrainingTimeEstimate",
    "apply_time_settings",
    "choice_label",
    "date_values",
    "estimate",
    "leave_out_immature",
    "maturity_step",
    "promised_tracker",
    "propose_budget",
    "propose_time_settings",
    "read_history",
    "read_offer",
    "read_offers",
    "record_run_time",
    "save_offer",
    "setting_path",
    "time_inputs",
    "time_setting_changes",
    "time_settings_asks",
    "time_settings_detector",
]

_LOGGER = get_logger(__name__)

TIME_CODES: Final[frozenset[str]] = frozenset({"TIME_COLUMN_UNREADABLE", "TIME_BUDGET_NOT_USED"})
"""The codes M244 raises (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration)."""

Choice = Literal["quick", "balanced", "thorough"]
CHOICES: Final[tuple[Choice, ...]] = ("quick", "balanced", "thorough")
PromisedChoice = Literal["quick", "balanced", "thorough", "custom"]
"""What a run was promised from: one of the three choices, or `custom` - a limit set some other way (Settings >
Advanced), estimated from the same history when the run starts."""
CUSTOM_LABEL: Final[str] = "Your own time limit"
_NAMES: Final[Mapping[Choice, str]] = {"quick": "Quick", "balanced": "Balanced", "thorough": "Thorough"}

TRAIN_STAGE_KEYS: Final[tuple[StageKey, ...]] = (
    StageKey.INGEST,
    StageKey.VALIDATE,
    StageKey.PREPARE,
    StageKey.SPLIT,
    StageKey.TRAIN,
    StageKey.EVALUATE,
    StageKey.EXPLAIN,
    StageKey.REGISTER,
)
"""A training run's stages, in order (`engine.pipeline.TRAIN_STAGES`, repeated so this module does not load the
pipeline)."""

MIN_MINUTES: Final[int] = 1
MAX_MINUTES: Final[int] = 240
"""`model_search.time_limit_minutes` and `uplift.time_limit_minutes` accept 1 to 240."""
NO_CURVE_MAX_MINUTES: Final[int] = 120
"""With no past leaderboard, Balanced is the time the search needs to finish on its own, at most this."""
GRID_MINUTES: Final[tuple[int, ...]] = (
    1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 45, 60, 75, 90, 120, 150, 180, 240,
)  # fmt: skip
"""The budgets the diminishing-returns rule looks at."""
CAPPED_SHARE: Final[float] = 0.9
"""A past search that took at least this share of its budget was stopped by it: its natural length is unknown."""
CLOSE_FACTOR: Final[float] = 3.0
"""A past run is close in size when its work is within this factor of this file's."""
HISTORY_RUNS: Final[int] = 20
"""The newest finished training runs read (at most `SCANNED_RECORDS` records are looked at)."""
SCANNED_RECORDS: Final[int] = 80
RECORD_RUNS: Final[int] = 10
"""How many of the newest actual-against-expected records the correction is the median of."""
FACTOR_BOUNDS: Final[tuple[float, float]] = (0.5, 2.0)
BETTER_BY: Final[float] = 0.001
"""A model is "better" when its validation score is higher by more than this."""
MIN_PART_ROWS: Final[int] = 100
MIN_PART_POSITIVES: Final[int] = 10
"""Below either, a validation or test part of a time split is too small to judge a model on: a question."""
MIN_SNAPSHOTS: Final[int] = 3
"""A cadence must leave at least this many snapshot dates to be offered."""
READABLE_SHARE: Final[float] = 0.95
"""A column is a date column when at least this share of its filled cells read as dates; it can say which date each
row describes only when it also has a date on at least this share of all rows."""
OUTCOME_DATE_RATE: Final[float] = 0.5
"""A sparsely filled date column whose filled rows are at least this share 'yes' (and twice the file's own rate) is
the outcome's own date - the day a customer left or a fault happened - never the date a row describes."""
MATURITY_WARN_RATIO: Final[float] = 0.5
MATURITY_Z: Final[float] = 3.0
"""The readiness probe's own thresholds (`engine.doctor.readiness_probes.LABEL_MATURITY_*`)."""

OFFERS_DIR: Final[str] = "time_offers"
SNAPSHOT_SPEC_PATH: Final[str] = "snapshots.frequency"
"""The build's setting the snapshot cadence answer sets (`engine.config.SnapshotDefinition.frequency`)."""
TIME_SETTING_NAMES: Final[frozenset[str]] = frozenset(
    {"training_budget", "cuts", "label_maturity", "snapshot_cadence"}
)
"""The `setting` an answered time question's item names in its payload (:func:`time_setting_changes`)."""
MAX_OFFERS: Final[int] = 20
"""Offers kept per file and goal, newest first: an earlier offer the person took stays matchable."""
PROMISE_FILENAME: Final[str] = "time_promise.json"
RECORD_FILENAME: Final[str] = "time_record.json"


class TimeSettingsError(Exception):
    """A coded refusal (`TIME_CODES`); the tools turn it into their own error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The prior: the public library's runs (library/*/run_report.md)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LibraryRun:
    """One real run of the public library, with engine defaults (a 30-minute budget it did not reach)."""

    name: str
    rows: int
    columns: int
    families: int
    seconds: float
    source: str


PRIOR_RUNS: Final[tuple[LibraryRun, ...]] = (
    LibraryRun("Telco customer churn", 7_043, 21, 4, 624.3, "library/telco-customer-churn/run_report.md"),
    LibraryRun("UCI bank marketing (A)", 41_188, 22, 4, 1_314.0, "library/uci-bank-marketing/run_report.md"),
    LibraryRun("UCI bank marketing (B)", 41_188, 21, 4, 1_147.6, "library/uci-bank-marketing/run_report.md"),
)
"""Whole-run wall clocks of the library's training runs: the prior when this deployment has no run to go by."""

PUBLIC_LABEL: Final[str] = "from similar public datasets"


# ---------------------------------------------------------------------------
# Contracts (the plan's §5: `TimeSettings`, `TrainingTimeEstimate`)
# ---------------------------------------------------------------------------
Basis = Literal["this_deployment", "public_library"]


class TrainingTimeEstimate(StrictBase):
    """How long a training run is expected to take under one budget, per stage, and on what basis."""

    minutes: int = Field(description="The search's time limit this estimate is for.")
    rows: int
    columns: int
    families: int = Field(description="The model families the goal allows.")
    expected_seconds: int = Field(description="The whole run, every stage.")
    about_minutes: int = Field(
        description="`expected_seconds` in whole minutes, at least 1: what the person reads."
    )
    stages: dict[str, int] = Field(description="Expected seconds per stage (`StageKey` value).")
    base: dict[str, float] = Field(
        description="The rules' own estimate per stage, before the correction learnt from past records."
    )
    train_natural_seconds: float = Field(description="How long the search would run with no limit.")
    capped: bool = Field(description="True when the search is expected to stop at its limit.")
    basis: Basis
    basis_label: str = Field(description="'from N past runs here' or 'from similar public datasets'.")
    runs_used: int = Field(description="Past runs of this deployment the estimate rests on.")
    close_runs: int = Field(description="Of them, runs close in size to this file.")
    records_used: int = Field(description="Actual-against-expected records the correction rests on.")


class BudgetChoice(StrictBase):
    """One of the three choices: never a raw minutes field, always "about N minutes" and what it buys."""

    choice: Choice
    minutes: int = Field(description="The time limit it sets (`model_search.time_limit_minutes`).")
    label: str = Field(description="'Quick (about 5 min)', 'Balanced (about 15 min, recommended)'.")
    about_minutes: int = Field(
        description="The whole run's minutes as said everywhere (`expected_run_minutes`)."
    )
    expected_seconds: int
    stages: dict[str, int]
    base: dict[str, float]
    buys: str = Field(description="What the choice buys, from past runs.")
    recommended: bool


class TrainingBudget(StrictBase):
    """`propose_training_budget`'s proposal: three choices, Balanced recommended."""

    setting_path: str = Field(description="The setting the choice sets.")
    choices: tuple[BudgetChoice, ...]
    recommended: Choice = "balanced"
    confidence: Literal["sure", "check"]
    basis: Basis
    basis_label: str
    reason: str
    current: Literal["quick", "balanced", "thorough", "custom"] = Field(
        description="What the use case's own setting is now: one of the three, or 'custom' (Settings > Advanced)."
    )
    current_minutes: int
    step_per_minute: float = Field(description="The diminishing-returns step used.")
    curves_used: int = Field(description="Past leaderboards the diminishing-returns rule read.")


class TimeOffer(StrictBase):
    """The three choices as they were shown, kept beside the data: the run whose budget is one of them keeps its
    expectation as the promise (`uploads/<id>/time_offers/<use case>.json`, in :class:`TimeOffers`)."""

    use_case_id: str
    source_kind: Literal["upload", "dataset"]
    source_id: str
    offered_at: AwareDatetime
    rows: int
    columns: int
    budget: TrainingBudget


class TimeOffers(StrictBase):
    """Every offer made for one file and goal, newest first (at most `MAX_OFFERS`): a newer run in the history
    moves the next offer's minutes, never the choice the person already took."""

    offers: tuple[TimeOffer, ...] = ()


class TimePromise(StrictBase):
    """What a run was promised: the chosen budget's expectation, per stage (`runs/<id>/time_promise.json`)."""

    run_id: str
    use_case_id: str
    source_id: str
    choice: PromisedChoice
    minutes: int
    label: str
    expected_seconds: int
    stages: dict[str, int]
    base: dict[str, float]
    basis: Basis
    offered_at: AwareDatetime = Field(
        description="When the offer it rests on was made (the newest, for custom)."
    )
    promised_at: AwareDatetime


class StageTime(StrictBase):
    expected: float
    base: float
    actual: float | None = Field(default=None, description="Null for a stage that did not finish.")


class TimeRecord(StrictBase):
    """Actual against expected, per stage, after a run ends (`runs/<id>/time_record.json`)."""

    run_id: str
    use_case_id: str
    state: str
    choice: PromisedChoice
    minutes: int
    capped: bool = Field(description="Whether the rules expected the search to stop at its limit.")
    stages: dict[str, StageTime]
    expected_seconds: int
    actual_seconds: float | None = Field(default=None, description="Null unless the run finished.")
    ratio: float | None = Field(default=None, description="Actual over expected, for a finished run.")
    recorded_at: AwareDatetime


class AsOf(StrictBase):
    column: str | None = Field(description="The date each row describes; null when none could be chosen.")
    candidates: tuple[str, ...] = Field(
        default=(), description="Every column that reads as dates and has a date on nearly every row."
    )
    left_out: tuple[str, ...] = Field(
        default=(),
        description=(
            "Columns that read as dates but cannot say which date a row describes: filled on too few rows, the "
            "outcome itself, or a date filled mostly where the outcome is yes."
        ),
    )
    sure: bool = Field(
        default=False, description="Only when the goal or its settings name it, or you chose it."
    )
    reason: str


class CutPoints(StrictBase):
    """Where a time split would cut, by the split's own rule, and whether that works."""

    column: str
    feasible: bool
    distinct_dates: int
    undated_rows: int = Field(
        default=0,
        description="Rows with no date in the column: placed in no part, so the cuts are a question.",
    )
    validation_from: str | None = None
    test_from: str | None = None
    train_rows: int
    validation_rows: int
    test_rows: int
    validation_positives: int | None = None
    test_positives: int | None = None
    too_small: bool = Field(description="A feasible cut leaving a part too small to judge a model on.")
    proposed_split: Literal["time_based", "random_stratified"]
    reason: str


class Maturity(StrictBase):
    """Whether the newest rows' outcomes can be known yet."""

    column: str
    window_days: int | None
    latest: str
    immature_from: str | None = Field(default=None, description="Rows dated after this are not final yet.")
    immature_rows: int = 0
    immature_months: int = 0
    newest_rate: float | None = None
    older_rate: float | None = None
    mature: bool
    fails_probe: bool = Field(
        description="Whether `X_LABEL_MATURITY` would fail: the newest rows have no yes."
    )
    question: str | None = None
    reason: str


class OutcomeWindow(StrictBase):
    days: int | None
    choices: tuple[int, ...] = ()
    through: Literal["define_outcome"] = "define_outcome"
    reason: str


class Lapse(StrictBase):
    activity_column: str
    days: int
    grace_days: int
    sentence: str
    already_quiet: int
    through: Literal["define_outcome"] = "define_outcome"


class Cadence(StrictBase):
    proposed: Literal["weekly", "monthly", "as_is"]
    weekly_snapshots: int = 0
    monthly_snapshots: int = 0
    entities: int = 0
    ask: bool = False
    reason: str


class TimeSettings(StrictBase):
    """The time settings of one goal on one file (the design §3.5): the budget, and the data time settings."""

    budget: TrainingBudget | None = None
    as_of: AsOf
    cuts: CutPoints | None = None
    maturity: Maturity | None = None
    outcome_window: OutcomeWindow
    lapse: Lapse | None = None
    cadence: Cadence | None = None
    questions: tuple[str, ...] = Field(default=(), description="What would be asked, in plain words.")


def choice_label(choice: Choice, about_minutes: int) -> str:
    """'Quick (about 5 min)', 'Balanced (about 15 min, recommended)', 'Thorough (about 40 min)'."""
    tail = ", recommended" if choice == "balanced" else ""
    return f"{_NAMES[choice]} (about {about_minutes} min{tail})"


# ---------------------------------------------------------------------------
# This deployment's past runs (the same stored durations M189 reads)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PastRun:
    """One finished training run, as the estimate reads it."""

    run_id: str
    use_case_id: str
    rows: int
    columns: int
    families: int
    budget_seconds: float
    """`inf` for a trainer with no limit."""
    train_seconds: float
    curve: tuple[tuple[float, float], ...] = ()
    """(seconds spent fitting so far, best validation score so far), higher is better."""
    record: TimeRecord | None = None

    @property
    def work(self) -> float:
        return _work(self.rows, self.columns, self.families)

    @property
    def capped(self) -> bool:
        return self.train_seconds >= CAPPED_SHARE * self.budget_seconds


def _work(rows: int, columns: int, families: int) -> float:
    return float(max(1, rows) * max(1, columns) * max(1, families))


def _raw(storage: Storage, key: str) -> Any:
    from engine.storage import StorageError

    try:
        if not storage.exists(key):
            return None
        return json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return None


def _trainer_of_raw(config: Mapping[str, Any]) -> str:
    if config.get("problem_type") == "uplift":
        return f"uplift:{config.get('uplift', {}).get('base_model')}"
    return "automl"


def _budget_of_raw(config: Mapping[str, Any]) -> float:
    if config.get("problem_type") == "uplift":
        uplift = config.get("uplift", {})
        if uplift.get("base_model") == "autogluon_fast":
            return float(uplift.get("time_limit_minutes", 10)) * 60.0
        return math.inf
    return float(config.get("model_search", {}).get("time_limit_minutes", 30)) * 60.0


def _families_of_raw(config: Mapping[str, Any]) -> int:
    if config.get("problem_type") == "uplift":
        return 1
    return max(1, len(config.get("model_search", {}).get("candidates", ()) or ()))


def _trial(name: str) -> int:
    marker = name.rsplit("/T", 1)
    if len(marker) == 2 and marker[1].isdigit():
        return int(marker[1])
    return 0


def curve_of(leaderboard: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    """A leaderboard as best-score-so-far against fitting time.

    The order the models were fitted in is not stored, so it is approximated the neutral way: lower stack levels
    first, then trial by trial across families (`/T3` of every family before `/T4`), names breaking ties. The
    score is oriented so that higher is better.
    """
    entries = [e for e in leaderboard.get("entries", ()) if isinstance(e, Mapping)]
    greater = bool(leaderboard.get("greater_is_better", True))
    order = sorted(
        entries,
        key=lambda e: (
            int(e.get("stack_level", 1)),
            _trial(str(e.get("model_name", ""))),
            str(e.get("model_name")),
        ),
    )
    spent = 0.0
    best = -math.inf
    points: list[tuple[float, float]] = []
    for entry in order:
        try:
            spent += max(0.0, float(entry.get("fit_time_seconds", 0.0)))
            score = float(entry["validation_score"])
        except (KeyError, TypeError, ValueError):
            continue
        oriented = score if greater else -score
        if oriented > best:
            best = oriented
        points.append((spent, best))
    return tuple(points)


def read_history(
    storage: Storage, *, trainer: str, exclude_run_id: str | None = None, limit: int = HISTORY_RUNS
) -> tuple[PastRun, ...]:
    """This deployment's finished training runs with the same trainer, newest first (the run directory walk M189's
    `past_durations` makes). Unreadable runs are skipped; nothing here can fail."""
    from engine.storage import StorageError, run_key

    found: list[PastRun] = []
    scanned = 0
    try:
        keys = sorted(storage.list_keys("runs/"), reverse=True)
    except StorageError:
        return ()
    for key in keys:
        if not key.endswith("/run.json"):
            continue
        scanned += 1
        if scanned > SCANNED_RECORDS or len(found) >= limit:
            break
        record = _raw(storage, key)
        if not isinstance(record, Mapping):
            continue
        run_id = str(record.get("run_id", ""))
        if (
            not run_id
            or run_id == exclude_run_id
            or record.get("mode") != RunMode.TRAIN.value
            or record.get("state") != RunState.DONE.value
        ):
            continue
        resolved = _raw(storage, run_key(run_id, "run_config.json"))
        config = resolved.get("config") if isinstance(resolved, Mapping) else None
        if not isinstance(config, Mapping) or _trainer_of_raw(config) != trainer:
            continue
        try:
            status = storage.read_model(run_key(run_id, "status.json"), RunStatus)
        except (StorageError, ValueError):
            continue
        train = next(
            (
                row.duration_seconds
                for row in status.stages
                if row.key is StageKey.TRAIN
                and row.state is RunState.DONE
                and row.duration_seconds is not None
            ),
            None,
        )
        if train is None:
            continue
        profile = _raw(storage, run_key(run_id, "profile.json"))
        rows = record.get("row_count")
        columns = None
        if isinstance(profile, Mapping):
            rows = profile.get("row_count", rows)
            columns = profile.get("column_count")
        if not isinstance(rows, int) or not isinstance(columns, int):
            continue
        board = _raw(storage, run_key(run_id, "leaderboard.json"))
        timed = _raw(storage, run_key(run_id, RECORD_FILENAME))
        kept: TimeRecord | None = None
        if isinstance(timed, Mapping):
            try:
                kept = TimeRecord.model_validate(timed)
            except ValueError:
                kept = None
        found.append(
            PastRun(
                run_id=run_id,
                use_case_id=str(record.get("use_case_id", "")),
                rows=rows,
                columns=columns,
                families=_families_of_raw(config),
                budget_seconds=_budget_of_raw(config),
                train_seconds=float(train),
                curve=curve_of(board) if isinstance(board, Mapping) else (),
                record=kept,
            )
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# The estimate (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Shape:
    rows: int
    columns: int
    families: int

    @property
    def work(self) -> float:
        return _work(self.rows, self.columns, self.families)


@dataclass(frozen=True)
class _Natural:
    """The search's natural length, ``a x work^b``, and what it rests on."""

    a: float
    b: float
    basis: Basis
    runs_used: int

    def seconds(self, work: float) -> float:
        return float(self.a * work**self.b)


def _fit_exponent(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    xs = [x for x, _ in points]
    if max(xs) - min(xs) < math.log(2.0):
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(y for _, y in points)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return min(1.2, max(0.2, slope))


def _prior_points() -> list[tuple[float, float]]:
    return [(math.log(_work(r.rows, r.columns, r.families)), math.log(r.seconds)) for r in PRIOR_RUNS]


def _prior_exponent() -> float:
    points = _prior_points()
    lows = [p for p in points if p[0] == min(x for x, _ in points)]
    highs = [p for p in points if p[0] == max(x for x, _ in points)]
    rise = statistics.fmean(y for _, y in highs) - statistics.fmean(y for _, y in lows)
    run = highs[0][0] - lows[0][0]
    return min(1.2, max(0.2, rise / run)) if run > 0 else 0.5


PRIOR_EXPONENT: Final[float] = _prior_exponent()
"""How the library's runs grew with the work (about 0.4: the search fits a fixed number of trials)."""


def _natural(history: Sequence[PastRun]) -> _Natural:
    free = [run for run in history if not run.capped]
    if free:
        points = [(math.log(run.work), math.log(max(run.train_seconds, MIN_STAGE_SECONDS))) for run in free]
        b = _fit_exponent(points) or PRIOR_EXPONENT
        a = math.exp(statistics.median(y - b * x for x, y in points))
        basis: Basis = "this_deployment"
    else:
        b = PRIOR_EXPONENT
        a = math.exp(statistics.median(y - b * x for x, y in _prior_points()))
        basis = "public_library"
    # A search stopped by its budget would have run at least that long: never expect less of the same work.
    for run in history:
        if run.capped and math.isfinite(run.budget_seconds):
            a = max(a, run.budget_seconds / run.work**b)
            basis = "this_deployment"
    return _Natural(a=a, b=b, basis=basis, runs_used=len(history))


@dataclass(frozen=True)
class _Factors:
    stages: Mapping[str, float]
    train_capped: float
    train_free: float
    used: int


def _clamp_factor(values: Sequence[float]) -> float:
    if not values:
        return 1.0
    low, high = FACTOR_BOUNDS
    return min(high, max(low, statistics.median(values)))


def _factors(history: Sequence[PastRun]) -> _Factors:
    records = [run.record for run in history if run.record is not None][:RECORD_RUNS]
    per_stage: dict[str, list[float]] = {}
    capped: list[float] = []
    free: list[float] = []
    for record in records:
        for key, time in record.stages.items():
            if time.actual is None or time.base <= 0:
                continue
            ratio = time.actual / time.base
            if key == StageKey.TRAIN.value:
                (capped if record.capped else free).append(ratio)
            else:
                per_stage.setdefault(key, []).append(ratio)
    return _Factors(
        stages=MappingProxyType({key: _clamp_factor(values) for key, values in per_stage.items()}),
        train_capped=_clamp_factor(capped),
        train_free=_clamp_factor(free),
        used=len(records),
    )


@dataclass(frozen=True)
class TimeInputs:
    """Everything an estimate reads, gathered once (:func:`time_inputs`)."""

    shape: Shape
    current_minutes: int
    setting_path: str
    history: tuple[PastRun, ...]
    others: PastDurations
    settings: TimeSettingsConfig
    from_store: bool = False
    """Whether this deployment's runs were read (False: no store was given, so nothing is known about them)."""
    natural: _Natural = field(init=False)
    factors: _Factors = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "natural", _natural(self.history))
        object.__setattr__(self, "factors", _factors(self.history))


def setting_path(config: UseCaseConfig) -> str:
    """The setting a budget sets: the search's limit, or an uplift learner's on AutoGluon.

    `TIME_BUDGET_NOT_USED` for an uplift learner on LightGBM, which takes no time limit at all."""
    from engine.config import ProblemType
    from engine.uplift.config import UpliftBaseModel

    if config.problem_type is ProblemType.UPLIFT:
        if config.uplift.base_model is UpliftBaseModel.AUTOGLUON_FAST:
            return "uplift.time_limit_minutes"
        raise TimeSettingsError(
            "TIME_BUDGET_NOT_USED",
            "This goal's learner takes no time limit, so there is no training time to choose.",
        )
    return "model_search.time_limit_minutes"


def _current_minutes(config: UseCaseConfig, path: str) -> int:
    if path == "uplift.time_limit_minutes":
        return int(config.uplift.time_limit_minutes)
    return int(config.model_search.time_limit_minutes)


def time_inputs(
    storage: Storage | None,
    config: UseCaseConfig,
    *,
    rows: int,
    columns: int,
    exclude_run_id: str | None = None,
) -> TimeInputs:
    """The inputs of an estimate for `config` on a file of `rows` x `columns`: this deployment's past runs with the
    same trainer, and M189's stage medians for this use case. With no storage, the library prior alone."""
    from engine.config import ProblemType
    from engine.run_progress import past_durations, trainer_of

    path = setting_path(config)
    families = 1 if config.problem_type is ProblemType.UPLIFT else max(1, len(config.model_search.candidates))
    trainer = trainer_of(config)
    history: tuple[PastRun, ...] = ()
    others = PastDurations(stages=MappingProxyType({}))
    if storage is not None:
        history = read_history(storage, trainer=trainer, exclude_run_id=exclude_run_id)
        others = past_durations(
            storage,
            use_case_id=config.id,
            mode=RunMode.TRAIN,
            trainer=None,
            exclude_run_id=exclude_run_id or "",
        )
    return TimeInputs(
        shape=Shape(rows=rows, columns=columns, families=families),
        current_minutes=_current_minutes(config, path),
        setting_path=path,
        history=history,
        others=others,
        settings=config.data_doctor.time_settings,
        from_store=storage is not None,
    )


def _close(inputs: TimeInputs) -> int:
    work = inputs.shape.work
    return sum(1 for run in inputs.history if 1 / CLOSE_FACTOR <= run.work / work <= CLOSE_FACTOR)


def _basis_label(inputs: TimeInputs) -> str:
    if inputs.natural.basis == "public_library":
        return PUBLIC_LABEL
    count = inputs.natural.runs_used
    return f"from {count} past run{'s' if count != 1 else ''} here"


def estimate(inputs: TimeInputs, minutes: int) -> TrainingTimeEstimate:
    """The expected time of a training run under a `minutes` budget, per stage."""
    minutes = min(MAX_MINUTES, max(MIN_MINUTES, int(minutes)))
    budget = minutes * 60.0
    natural = inputs.natural.seconds(inputs.shape.work)
    capped = natural >= budget
    factors = inputs.factors
    stages: dict[str, int] = {}
    base: dict[str, float] = {}
    for key in TRAIN_STAGE_KEYS:
        if key is StageKey.TRAIN:
            raw = min(budget, natural)
            factor = factors.train_capped if capped else factors.train_free
        else:
            durations = inputs.others.stages.get(key, ())
            raw = statistics.median(durations) if durations else DEFAULT_STAGE_SECONDS.get(key, 5.0)
            factor = factors.stages.get(key.value, 1.0)
        raw = max(MIN_STAGE_SECONDS, float(raw))
        base[key.value] = round(raw, 3)
        stages[key.value] = max(int(MIN_STAGE_SECONDS), round(raw * factor))
    total = sum(stages.values())
    return TrainingTimeEstimate(
        minutes=minutes,
        rows=inputs.shape.rows,
        columns=inputs.shape.columns,
        families=inputs.shape.families,
        expected_seconds=total,
        about_minutes=max(1, round(total / 60)),
        stages=stages,
        base=base,
        train_natural_seconds=round(natural, 1),
        capped=capped,
        basis=inputs.natural.basis,
        basis_label=_basis_label(inputs),
        runs_used=inputs.natural.runs_used,
        close_runs=_close(inputs),
        records_used=factors.used,
    )


# ---------------------------------------------------------------------------
# The budget: the point of diminishing returns, three plain choices
# ---------------------------------------------------------------------------
def _scaled_curves(inputs: TimeInputs) -> list[tuple[tuple[float, float], ...]]:
    b = inputs.natural.b
    curves = []
    for run in inputs.history:
        if not run.curve:
            continue
        scale = (inputs.shape.work / run.work) ** b
        curves.append(tuple((seconds * scale, score) for seconds, score in run.curve))
    return curves


def _best(curve: Sequence[tuple[float, float]], minutes: float) -> float | None:
    found = None
    for seconds, score in curve:
        if seconds <= minutes * 60.0:
            found = score
        else:
            break
    return found


def _knee(curves: Sequence[Sequence[tuple[float, float]]], step: float) -> int:
    """The first budget after which the curves gained, on average, less than `step` per extra minute."""
    for index, minutes in enumerate(GRID_MINUTES):
        here = [_best(curve, minutes) for curve in curves]
        if any(value is None for value in here):
            continue
        settled = True
        for later in GRID_MINUTES[index + 1 :]:
            there = [_best(curve, later) for curve in curves]
            gain = statistics.fmean(
                (t if t is not None else h) - h  # type: ignore[operator]
                for h, t in zip(here, there, strict=True)
            )
            if gain / (later - minutes) >= step:
                settled = False
                break
        if settled:
            return minutes
    return GRID_MINUTES[-1]


def _minutes_for(balanced: int, settings: TimeSettingsConfig) -> dict[Choice, int]:
    # Three different limits, so the run's limit always says which choice it was (the promise is found by it).
    balanced = min(MAX_MINUTES - 1, max(MIN_MINUTES + 1, balanced))
    quick = min(balanced - 1, max(MIN_MINUTES, round(balanced * settings.quick_ratio)))
    thorough = min(MAX_MINUTES, max(balanced + 1, round(balanced * settings.thorough_ratio)))
    return {"quick": quick, "balanced": balanced, "thorough": thorough}


def _buys(
    inputs: TimeInputs, curves: Sequence[Sequence[tuple[float, float]]], minutes: Mapping[Choice, int]
) -> dict[Choice, str]:
    if not curves:
        public = inputs.natural.basis == "public_library"
        natural = inputs.natural.seconds(inputs.shape.work)
        return {
            "quick": (
                "A first model soon; the search is stopped before it finishes."
                if natural > minutes["quick"] * 60.0
                else "The search is expected to finish within it on a file this size."
            ),
            "balanced": (
                "Time for the search to finish on a file this size"
                + (f" ({PUBLIC_LABEL})." if public else ", from past runs here.")
            ),
            "thorough": (
                "More models are tried; no past run here can say yet whether that finds a better one."
                if natural > minutes["balanced"] * 60.0
                else "Room for the search to run longer if it needs to; on a file this size it is expected to "
                "finish in about the same time."
            ),
        }
    n = len(curves)
    of = f"of {n} similar past run{'s' if n != 1 else ''}"
    balanced = [_best(curve, minutes["balanced"]) for curve in curves]
    quick = [_best(curve, minutes["quick"]) for curve in curves]
    thorough = [_best(curve, minutes["thorough"]) for curve in curves]
    as_good = sum(
        1
        for q, b in zip(quick, balanced, strict=True)
        if q is not None and b is not None and q >= b - BETTER_BY
    )
    better = sum(
        1
        for t, b in zip(thorough, balanced, strict=True)
        if t is not None and b is not None and t > b + BETTER_BY
    )
    return {
        "quick": f"Quick found a model as good as Balanced on {as_good} {of}.",
        "balanced": f"Similar past runs found almost nothing better after about {minutes['balanced']} minutes.",
        "thorough": f"Thorough has found a better model on {better} {of}.",
    }


def propose_budget(inputs: TimeInputs) -> TrainingBudget:
    """Three choices, Balanced at the point of diminishing returns (see the module docstring)."""
    settings = inputs.settings
    curves = _scaled_curves(inputs)
    if curves:
        balanced = _knee(curves, settings.score_step_per_minute)
    else:
        natural_minutes = inputs.natural.seconds(inputs.shape.work) / 60.0
        balanced = min(NO_CURVE_MAX_MINUTES, max(MIN_MINUTES, math.ceil(natural_minutes)))
    minutes = _minutes_for(balanced, settings)
    buys = _buys(inputs, curves, minutes)
    choices = []
    for choice in CHOICES:
        expected = estimate(inputs, minutes[choice])
        # The minutes said are the whole run's, the same the approval card and the run page say (`expected_run_minutes`);
        # `expected_seconds` stays the data-aware training estimate the run is later measured against.
        told = expected_run_minutes(expected.minutes)
        choices.append(
            BudgetChoice(
                choice=choice,
                minutes=expected.minutes,
                label=choice_label(choice, told),
                about_minutes=told,
                expected_seconds=expected.expected_seconds,
                stages=expected.stages,
                base=expected.base,
                buys=buys[choice],
                recommended=choice == "balanced",
            )
        )
    close = _close(inputs)
    sure = inputs.natural.basis == "this_deployment" and close >= settings.sure_min_runs
    current: Literal["quick", "balanced", "thorough", "custom"] = next(
        (c.choice for c in choices if c.minutes == inputs.current_minutes), "custom"
    )
    label = _basis_label(inputs)
    if inputs.natural.basis == "public_library":
        # Only a store that was read can say there is no run here; without one, the basis is all that is known.
        reason = (
            f"Estimated {PUBLIC_LABEL}: there is no finished training run here to go by yet."
            if inputs.from_store
            else f"Estimated {PUBLIC_LABEL}."
        )
    else:
        reason = (
            f"Estimated {label}; {close} of them close in size to this file. "
            + ("Balanced is where they stopped finding better models." if curves else "")
        ).strip()
    return TrainingBudget(
        setting_path=inputs.setting_path,
        choices=tuple(choices),
        confidence="sure" if sure else "check",
        basis=inputs.natural.basis,
        basis_label=label,
        reason=reason,
        current=current,
        current_minutes=inputs.current_minutes,
        step_per_minute=settings.score_step_per_minute,
        curves_used=len(curves),
    )


# ---------------------------------------------------------------------------
# The offer, the promise and the record
# ---------------------------------------------------------------------------
def offer_key(source_kind: Literal["upload", "dataset"], source_id: str, use_case_id: str) -> str:
    folder = "uploads" if source_kind == "upload" else "datasets"
    return f"{folder}/{source_id}/{OFFERS_DIR}/{use_case_id}.json"


def source_kind_of(storage: Storage, source_id: str) -> Literal["upload", "dataset"]:
    """Whether an id names a built dataset (`datasets/<id>/` holds files) or an upload."""
    from engine.storage import StorageError

    try:
        return "dataset" if storage.list_keys(f"datasets/{source_id}/") else "upload"
    except StorageError:
        return "upload"


_OFFERS_LOCK: Final = threading.Lock()


def save_offer(storage: Storage, offer: TimeOffer) -> str:
    """Keep `offer` as the newest for its file and goal. Earlier offers are kept (an accepted choice stays
    matchable); an earlier offer of exactly the same choices is replaced by this one."""
    key = offer_key(offer.source_kind, offer.source_id, offer.use_case_id)
    with _OFFERS_LOCK:
        earlier = read_offers(storage, offer.source_kind, offer.source_id, offer.use_case_id)
        kept = [o for o in earlier if o.budget.choices != offer.budget.choices]
        storage.write_model(key, TimeOffers(offers=(offer, *kept)[:MAX_OFFERS]))
    return key


def read_offers(
    storage: Storage, source_kind: Literal["upload", "dataset"], source_id: str, use_case_id: str
) -> tuple[TimeOffer, ...]:
    """Every offer kept for this file and goal, newest first; none when unreadable."""
    from engine.storage import StorageError

    key = offer_key(source_kind, source_id, use_case_id)
    try:
        if not storage.exists(key):
            return ()
        return storage.read_model(key, TimeOffers).offers
    except (StorageError, ValueError):
        return ()


def read_offer(
    storage: Storage, source_kind: Literal["upload", "dataset"], source_id: str, use_case_id: str
) -> TimeOffer | None:
    """The newest offer for this file and goal, or None."""
    offers = read_offers(storage, source_kind, source_id, use_case_id)
    return offers[0] if offers else None


_ENDED: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


def record_run_time(storage: Storage, promise: TimePromise, status: RunStatus) -> TimeRecord:
    """Write actual against expected, per stage, for a run that ended (`runs/<id>/time_record.json`)."""
    from engine.storage import run_key

    stages: dict[str, StageTime] = {}
    for row in status.stages:
        key = row.key.value
        actual = row.duration_seconds if row.state is RunState.DONE else None
        stages[key] = StageTime(
            expected=float(promise.stages.get(key, 0)), base=float(promise.base.get(key, 0.0)), actual=actual
        )
    finished = status.state is RunState.DONE and all(time.actual is not None for time in stages.values())
    actual_total = sum(time.actual or 0.0 for time in stages.values()) if finished else None
    train = promise.base.get(StageKey.TRAIN.value, 0.0)
    record = TimeRecord(
        run_id=promise.run_id,
        use_case_id=promise.use_case_id,
        state=status.state.value,
        choice=promise.choice,
        minutes=promise.minutes,
        capped=train >= promise.minutes * 60.0 - 1e-6,
        stages=stages,
        expected_seconds=promise.expected_seconds,
        actual_seconds=None if actual_total is None else round(actual_total, 3),
        ratio=(
            None
            if actual_total is None or promise.expected_seconds <= 0
            else round(actual_total / promise.expected_seconds, 4)
        ),
        recorded_at=utc_now(),
    )
    storage.write_model(run_key(promise.run_id, RECORD_FILENAME), record)
    return record


class PromiseTracker(ProgressTracker):
    """M189's tracker on the promised expectation; when the run ends it records actual against expected, once."""

    def __init__(
        self,
        expectations: Expectations,
        *,
        storage: Storage,
        promise: TimePromise,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(expectations, clock=clock)
        self._storage = storage
        self._promise = promise
        self._recorded = False

    @property
    def promise(self) -> TimePromise:
        return self._promise

    def decorate(self, status: RunStatus) -> RunStatus:
        decorated = super().decorate(status)
        if not self._recorded and decorated.state in _ENDED:
            self._recorded = True
            try:
                record_run_time(self._storage, self._promise, decorated)
            except Exception:  # learning is a nicety; the run's own status write is not
                _LOGGER.exception("time record for run %s could not be written", self._promise.run_id)
        return decorated


def promised_tracker(flow: Any, keys: Sequence[StageKey]) -> ProgressTracker | None:
    """The tracker of a training run on data Data Doctor offered training-time choices for, or None (then M189's own
    estimate stands: a run on data no offer was made for). Writes the promise into the run directory.

    The run's limit is looked up among every kept offer, newest first: a choice the person took is promised exactly
    what was shown, even after a newer run has moved the next offer's minutes. A limit that is none of them (set in
    Settings > Advanced) is promised :func:`estimate` of that limit, from the same history, on the file the offer
    was made for - so every run on offered data has its ETA from one source.
    """
    from engine.run_progress import train_budget_seconds
    from engine.storage import run_key

    ctx = flow._ctx
    config = ctx.config
    if ctx.mode is not RunMode.TRAIN:
        return None
    budget = train_budget_seconds(config)
    if budget is None:
        return None
    record = flow._run.record
    source_kind: Literal["upload", "dataset"] = "dataset" if record.dataset_id else "upload"
    source_id = record.dataset_id or record.upload_id
    if not source_id:
        return None
    offers = read_offers(ctx.storage, source_kind, source_id, config.id)
    if not offers:
        return None
    found = next(
        ((offer, c) for offer in offers for c in offer.budget.choices if c.minutes * 60.0 == budget), None
    )
    if found is not None:
        offer, chosen = found
        choice: PromisedChoice = chosen.choice
        minutes, label, stages, base = chosen.minutes, chosen.label, chosen.stages, chosen.base
        basis = offer.budget.basis
    else:
        offer = offers[0]
        try:
            inputs = time_inputs(
                ctx.storage, config, rows=offer.rows, columns=offer.columns, exclude_run_id=ctx.run_id
            )
        except TimeSettingsError:
            return None
        expected = estimate(inputs, _current_minutes(config, inputs.setting_path))
        choice = "custom"
        minutes, stages, base, basis = expected.minutes, expected.stages, expected.base, expected.basis
        label = f"{CUSTOM_LABEL} (about {expected.about_minutes} min)"
    if any(key.value not in stages for key in keys):
        return None
    promise = TimePromise(
        run_id=ctx.run_id,
        use_case_id=config.id,
        source_id=source_id,
        choice=choice,
        minutes=minutes,
        label=label,
        expected_seconds=sum(stages[key.value] for key in keys),
        stages={key.value: stages[key.value] for key in keys},
        base={key.value: base.get(key.value, 0.0) for key in keys},
        basis=basis,
        offered_at=offer.offered_at,
        promised_at=utc_now(),
    )
    ctx.storage.write_model(run_key(ctx.run_id, PROMISE_FILENAME), promise)
    expectations = Expectations(
        seconds=MappingProxyType({key: float(stages[key.value]) for key in keys}), basis="time_budget"
    )
    _LOGGER.info("progress estimate: run=%s promised %s (%ss)", ctx.run_id, label, promise.expected_seconds)
    return PromiseTracker(expectations, storage=ctx.storage, promise=promise)


# ---------------------------------------------------------------------------
# (b) The data time settings
# ---------------------------------------------------------------------------
def date_values(series: pd.Series[Any]) -> pd.Series[Any] | None:
    """A column as dates (naive), or None when it cannot be read without guessing the day/month order or fewer
    than `READABLE_SHARE` of its filled cells read as dates."""
    import pandas as pd

    from engine.agent.formats import date_order, parse_dates

    if pd.api.types.is_datetime64_any_dtype(series):
        values = pd.to_datetime(series, errors="coerce")
        return values.dt.tz_localize(None) if values.dt.tz is not None else values
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return None
    cells = series.dropna().astype(str)
    cells = cells[cells.str.strip() != ""]
    if cells.empty:
        return None
    order = date_order(cells)
    if order is None and cells.str.match(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}").any():
        return None
    parsed = parse_dates(series, dayfirst=bool(order)).values
    if parsed.notna().sum() < READABLE_SHARE * len(cells):
        return None
    return parsed


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _filled_share(dates: pd.Series[Any], rows: int) -> float:
    return float(dates.notna().sum()) / rows if rows else 0.0


def _why_not(name: str, dates: pd.Series[Any], rows: int, yes: pd.Series[Any] | None) -> str:
    share = _filled_share(dates, rows)
    filled = dates.notna()
    if yes is not None and filled.any():
        hits = yes.reindex(dates.index).fillna(False).astype(bool)
        rate, overall = float(hits[filled].mean()), float(hits.mean())
        if rate >= OUTCOME_DATE_RATE and rate >= 2 * overall:
            return (
                f"'{name}' has a date on only {share:.0%} of rows, nearly all of them where the outcome is yes: it "
                "is the outcome's own date, so testing on it would let the test rows give the answer away"
            )
    return f"'{name}' has a date on only {share:.0%} of rows, so it cannot say which date every row describes"


def _as_of(
    frame: pd.DataFrame,
    names: Sequence[str],
    given: str | None,
    hinted: Sequence[str],
    *,
    exclude: Sequence[str] = (),
    yes: pd.Series[Any] | None = None,
) -> AsOf:
    """The date each row describes. `sure` only when the goal or its settings name the column, or the person chose
    it: being the only date column is a reason to ask, never to decide - an outcome date (filled only where the
    outcome is yes) reads as dates too. A column is a candidate only when it has a date on nearly every row; the
    outcome column (`exclude`) never is."""
    rows = len(frame)
    parsed = {
        str(column): values
        for column in frame.columns
        if str(column) not in exclude and (values := date_values(frame[column])) is not None
    }
    readable = [name for name, values in parsed.items() if _filled_share(values, rows) >= READABLE_SHARE]
    left_out = tuple(name for name in parsed if name not in readable)
    notes = [_why_not(name, parsed[name], rows, yes) for name in left_out]
    if given is not None:
        if given not in frame.columns:
            raise TimeSettingsError("TIME_COLUMN_UNREADABLE", f"There is no column '{given}' in this file.")
        if given not in parsed:
            raise TimeSettingsError(
                "TIME_COLUMN_UNREADABLE",
                f"'{given}' cannot be read as dates, so it cannot say which date each row describes.",
            )
        # Your choice stands; rows it has no date for are counted with the cut points, which then ask.
        return AsOf(
            column=given, candidates=tuple(readable), left_out=left_out, sure=True, reason="You chose it."
        )
    wanted = [_norm(n) for n in names if n]
    named = [column for column in readable if _norm(column) in wanted]
    if named:
        best = min(named, key=lambda c: wanted.index(_norm(c)))
        return AsOf(
            column=best,
            candidates=tuple(readable),
            left_out=left_out,
            sure=True,
            reason=f"'{best}' is the date this goal names.",
        )
    ranked = list(dict.fromkeys([*(c for c in hinted if c in readable), *readable]))
    tail = (" " + "; ".join(notes) + ".") if notes else ""
    if len(readable) == 1:
        return AsOf(
            column=None,
            candidates=tuple(ranked),
            left_out=left_out,
            reason=(
                f"'{readable[0]}' is the only column with a date on every row, but nothing says it is the date "
                f"each row describes: that is a question.{tail}"
            ),
        )
    if readable:
        return AsOf(
            column=None,
            candidates=tuple(ranked),
            left_out=left_out,
            reason=f"{len(readable)} columns read as dates; which one each row describes is a question.{tail}",
        )
    return AsOf(
        column=None,
        candidates=(),
        left_out=left_out,
        reason=(
            ("No column of the file has a date on every row." + tail)
            if notes
            else ("No column of the file reads as dates.")
        ),
    )


def _positive(values: pd.Series[Any], positive: object) -> pd.Series[Any]:
    from engine.doctor.readiness_probes import label_key

    want = label_key(positive) if positive is not None else None
    keys = values.map(
        lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else label_key(v)
    )
    if want is not None:
        return keys == want
    return keys.isin({"1", "true", "yes", "y", "t"})


def _cuts(
    dates: pd.Series[Any],
    column: str,
    *,
    validation_fraction: float,
    test_fraction: float,
    yes: pd.Series[Any] | None,
    undated: int = 0,
) -> CutPoints:
    """The split's own rule (`engine.stages.prepare._part_sizes`, `_tie_boundary`) on the as-of dates. `undated`
    rows have no date: they are in no part, said so, and the cuts are then a question, never decided."""
    from engine.stages.prepare import _part_sizes, _tie_boundary

    order = dates.dropna().sort_values(kind="stable")
    values = order.tolist()
    rows = len(values)
    wanted = _part_sizes(rows, validation_fraction, test_fraction)
    first_validation = _tie_boundary(values, wanted[0], floor=0)
    first_test = _tie_boundary(values, wanted[0] + wanted[1], floor=first_validation)
    got = (first_validation, first_test - first_validation, rows - first_test)
    distinct = len(set(values))
    empty = [
        name
        for name, want, have in zip(("training", "validation", "test"), wanted, got, strict=True)
        if want and not have
    ]

    def day(index: int) -> str | None:
        return values[index].date().isoformat() if 0 <= index < rows else None

    positives: tuple[int | None, int | None] = (None, None)
    if yes is not None:
        hits = yes.reindex(order.index).fillna(False).astype(bool).tolist()
        positives = (sum(hits[first_validation:first_test]), sum(hits[first_test:]))
    unplaced = f" {undated:,} rows have no date in '{column}' and are not in any part." if undated else ""
    if empty:
        return CutPoints(
            column=column,
            feasible=False,
            distinct_dates=distinct,
            undated_rows=undated,
            train_rows=got[0],
            validation_rows=got[1],
            test_rows=got[2],
            too_small=False,
            proposed_split="random_stratified",
            reason=(
                f"Too many rows share one date at a cut ({distinct} different dates in {rows:,} rows): the "
                f"{empty[0]} part would be empty, so the rows are split at random instead."
            )
            + unplaced,
        )
    small_parts = [
        name
        for name, count, hit, want in (
            ("validation", got[1], positives[0], wanted[1]),
            ("test", got[2], positives[1], wanted[2]),
        )
        if want and (count < MIN_PART_ROWS or (hit is not None and hit < MIN_PART_POSITIVES))
    ]
    reason = (
        f"Testing uses the {got[2]:,} rows from {day(first_test)} on"
        + (f" and checking the {got[1]:,} rows from {day(first_validation)}" if got[1] else "")
        + "; no date is split across two parts."
    )
    if small_parts:
        reason += f" That leaves the {small_parts[0]} part too small to judge a model on."
    reason += unplaced
    return CutPoints(
        column=column,
        feasible=True,
        distinct_dates=distinct,
        undated_rows=undated,
        validation_from=day(first_validation) if got[1] else None,
        test_from=day(first_test) if got[2] else None,
        train_rows=got[0],
        validation_rows=got[1],
        test_rows=got[2],
        validation_positives=positives[0],
        test_positives=positives[1],
        too_small=bool(small_parts),
        proposed_split="time_based",
        reason=reason,
    )


def _maturity(dates: pd.Series[Any], column: str, window: int | None, yes: pd.Series[Any] | None) -> Maturity:
    """Which newest rows cannot have their outcome yet: those within the outcome window of the file's newest date,
    and - read from the outcomes themselves - a newest run of dates with no yes at all where the older rows' rate
    says there should be some, or a newest month whose yes rate is far below the older months' (the readiness
    probe's own test)."""
    latest = dates.max()
    if dates.nunique() < 2:
        return Maturity(
            column=column,
            window_days=window,
            latest=latest.date().isoformat(),
            mature=True,
            fails_probe=False,
            reason="Every row describes the same date, so there is no newer slice to compare with older ones.",
        )
    cutoffs: list[Any] = []
    if window is not None:
        cutoffs.append(latest - timedelta(days=window))
    hits = yes.reindex(dates.index).fillna(False).astype(bool) if yes is not None else None
    if hits is not None:
        per_day = hits.groupby(dates).agg(["sum", "count"]).sort_index(ascending=False)
        block_rows = 0
        block_start = None
        for day, row in per_day.iterrows():
            if int(row["sum"]) > 0:
                break
            block_rows += int(row["count"])
            block_start = day
        older = hits[dates < block_start] if block_start is not None else hits.iloc[0:0]
        if block_start is not None and len(older) and older.mean() > 0 and block_rows * older.mean() >= 5:
            before = per_day.index[per_day.index < block_start]
            if len(before):
                cutoffs.append(before.max())
        newest_month = latest.to_period("M")
        in_month = dates.dt.to_period("M") == newest_month
        new_rows, old_rows = int(in_month.sum()), int((~in_month).sum())
        if new_rows and old_rows:
            new_rate, old_rate = float(hits[in_month].mean()), float(hits[~in_month].mean())
            pooled = float(hits.mean())
            spread = (
                math.sqrt(pooled * (1 - pooled) * (1 / new_rows + 1 / old_rows)) if 0 < pooled < 1 else 0.0
            )
            z = (new_rate - old_rate) / spread if spread else 0.0
            if old_rate > 0 and new_rate / old_rate < MATURITY_WARN_RATIO and z <= -MATURITY_Z:
                cutoffs.append(newest_month.to_timestamp() - timedelta(days=1))
    if not cutoffs:
        return Maturity(
            column=column,
            window_days=None,
            latest=latest.date().isoformat(),
            mature=True,
            fails_probe=False,
            reason="The newest rows' outcome rate is in line with the older rows'.",
        )
    cutoff = min(cutoffs)
    immature = dates > cutoff
    count = int(immature.sum())
    newest_rate = older_rate = None
    fails = False
    if hits is not None and count and count < len(dates):
        newest_rate = float(hits[immature].mean())
        older_rate = float(hits[~immature].mean())
        fails = older_rate > 0 and newest_rate == 0
    if not count:
        return Maturity(
            column=column,
            window_days=window,
            latest=latest.date().isoformat(),
            mature=True,
            fails_probe=False,
            reason=f"Every row is at least {window} days older than the newest date: its outcome is final.",
        )
    if count == len(dates):
        return Maturity(
            column=column,
            window_days=window,
            latest=latest.date().isoformat(),
            immature_from=cutoff.date().isoformat(),
            immature_rows=count,
            mature=False,
            fails_probe=False,
            reason=(
                f"Every row is within {window} days of the newest date, so none of their outcomes can be final "
                "yet; leaving them out would leave nothing."
            ),
        )
    span = int(dates[immature].dt.to_period("M").nunique())
    question = (
        "Your newest month's outcomes are not final yet. Leave it out of testing?"
        if span <= 1
        else f"Your newest {span} months' outcomes are not final yet. Leave them out of testing?"
    )
    if window is not None and cutoff == latest - timedelta(days=window):
        reason = (
            f"The outcome is measured over {window} days and the file's newest date is "
            f"{latest.date().isoformat()}: the {count:,} rows dated after {cutoff.date().isoformat()} cannot have "
            "their outcome yet."
        )
    else:
        reason = (
            f"The {count:,} rows dated after {cutoff.date().isoformat()} have a yes rate of {newest_rate or 0:.1%} "
            f"against {older_rate or 0:.1%} before: their outcomes are probably not known yet."
        )
    return Maturity(
        column=column,
        window_days=window,
        latest=latest.date().isoformat(),
        immature_from=cutoff.date().isoformat(),
        immature_rows=count,
        immature_months=span,
        newest_rate=newest_rate,
        older_rate=older_rate,
        mature=False,
        fails_probe=fails,
        question=question,
        reason=reason,
    )


def _cadence(
    frame: pd.DataFrame, dates: pd.Series[Any], key: str | None, window: int | None
) -> Cadence | None:
    """For an event file (a customer on several rows): weekly or monthly snapshots (`plan_snapshots`'s choice)."""
    if key is None or key not in frame.columns:
        return None
    entities = int(frame[key].nunique(dropna=True))
    if entities <= 0 or len(frame) < 2 * entities:
        return None
    span = (dates.max() - dates.min()).days - (window or 0)
    weekly = max(0, span // 7)
    monthly = max(0, int(span // 30.44))
    if weekly < MIN_SNAPSHOTS:
        return Cadence(
            proposed="as_is",
            weekly_snapshots=weekly,
            monthly_snapshots=monthly,
            entities=entities,
            reason=f"The dates span too little time for {MIN_SNAPSHOTS} snapshots after the outcome window.",
        )
    if monthly < MIN_SNAPSHOTS:
        return Cadence(
            proposed="weekly",
            weekly_snapshots=weekly,
            monthly_snapshots=monthly,
            entities=entities,
            reason=f"Only weekly snapshots leave at least {MIN_SNAPSHOTS} dates ({weekly} weeks).",
        )
    return Cadence(
        proposed="monthly",
        weekly_snapshots=weekly,
        monthly_snapshots=monthly,
        entities=entities,
        ask=True,
        reason=(
            f"Both work: {monthly} month-ends ({monthly * entities:,} rows) or {weekly} week-ends "
            f"({weekly * entities:,} rows)."
        ),
    )


def _lapse(frame: pd.DataFrame, file: LoadedFile, brief: GoalBrief | None, root: Any) -> Lapse | None:
    if brief is None or brief.outcome.label_type != "event_absence":
        return None
    from engine.agent.label_drafts import draft_labels

    try:
        drafts = draft_labels(frame, file.profile.columns, root=root)
    except Exception:  # a draft is a help, never a reason to fail the proposal
        return None
    if drafts is None or not drafts.drafts:
        return None
    wanted = brief.time_rules.outcome_window_days
    draft = next((d for d in drafts.drafts if d.window_days == wanted), drafts.drafts[0])
    return Lapse(
        activity_column=drafts.activity_column,
        days=draft.window_days,
        grace_days=draft.grace_days,
        sentence=(
            f"A customer has lapsed when there is no {drafts.activity} for {draft.window_days} days after the date "
            f"their row describes, plus {draft.grace_days} days' grace."
        ),
        already_quiet=draft.already_quiet,
    )


def propose_time_settings(
    file: LoadedFile,
    config: UseCaseConfig,
    *,
    brief: GoalBrief | None = None,
    as_of_column: str | None = None,
    outcome_column: str | None = None,
    primary_key: str | None = None,
    window_days: int | None = None,
    budget: TrainingBudget | None = None,
    config_root: Any = None,
) -> TimeSettings:
    """The data time settings of one file for one goal, each with its reason (see the module docstring)."""
    from engine.config import SplitType

    frame = file.frame
    names = [
        n
        for n in (
            config.split.time_column,
            brief.time_settings.as_of_column if brief is not None else None,
            brief.time_rules.snapshot_column if brief is not None else None,
        )
        if n
    ]
    hints = [*file.profile.time_column_candidates, *config.time_column_hints]
    target = outcome_column or (brief.outcome.column if brief is not None else None) or config.target.column
    yes = None
    if target and target in frame.columns:
        positive = config.target.positive_label if target == config.target.column else None
        yes = _positive(frame[target], positive)
    as_of = _as_of(frame, names, as_of_column, hints, exclude=[target] if target else (), yes=yes)
    window = window_days
    if window is None and brief is not None:
        window = brief.time_rules.outcome_window_days
    choices = brief.outcome.window_days if brief is not None else ()
    outcome_window = OutcomeWindow(
        days=window,
        choices=tuple(choices),
        reason=(
            f"The outcome is measured over {window} days; it is part of the outcome definition you approve."
            if window is not None
            else "The outcome is not measured over a window, or the window is settled with the outcome."
        ),
    )
    questions: list[str] = []
    if as_of.column is None:
        if as_of.candidates or as_of.left_out:
            questions.append("Which date does each row describe? Testing uses the newest dates.")
        return TimeSettings(
            budget=budget,
            as_of=as_of,
            outcome_window=outcome_window,
            lapse=_lapse(frame, file, brief, config_root),
            questions=tuple(questions),
        )
    dates = date_values(frame[as_of.column])
    assert dates is not None  # _as_of chose a readable column
    time_split = config.split.type is SplitType.TIME_BASED or (
        brief is not None and brief.time_rules.split_type == SplitType.TIME_BASED.value
    )
    maturity = None
    must_mature = window is not None or (brief is not None and brief.time_rules.label_must_be_mature)
    if (must_mature or time_split) and dates.notna().any():
        maturity = _maturity(dates.dropna(), as_of.column, window, yes)
        if maturity.question:
            questions.append(maturity.question)
    cuts = None
    if time_split:
        # The cuts are measured on the rows whose outcome is final: the newest rows are asked about on their
        # own, and the recommended answer leaves them out.
        measured = dates
        if maturity is not None and maturity.question and maturity.immature_from is not None:
            measured = dates[dates <= datetime.fromisoformat(maturity.immature_from)]
        cuts = _cuts(
            measured,
            as_of.column,
            validation_fraction=config.split.validation_fraction,
            test_fraction=config.split.test_fraction,
            yes=yes,
            undated=int(dates.isna().sum()),
        )
        if measured is not dates:
            note = " (Measured with the rows whose outcome is not final yet left out.)"
            cuts = cuts.model_copy(update={"reason": cuts.reason + note})
        if cuts.feasible and (cuts.too_small or cuts.undated_rows):
            questions.append(cuts.reason + " Keep testing on the newest dates, or split at random?")
    cadence = _cadence(frame, dates, primary_key, window)
    if cadence is not None and cadence.ask:
        questions.append("Build one row per customer for every month, or for every week?")
    return TimeSettings(
        budget=budget,
        as_of=as_of,
        cuts=cuts,
        maturity=maturity,
        outcome_window=outcome_window,
        lapse=_lapse(frame, file, brief, config_root),
        cadence=cadence,
        questions=tuple(questions),
    )


def leave_out_immature(frame: pd.DataFrame, column: str, after: str) -> pd.DataFrame:
    """The rows whose outcome is final: those dated after `after` are left out (`remove_rows` `after`)."""
    from engine.agent.value_steps import remove_rows

    keep = remove_rows(frame[column], operator="after", values=[after])
    return frame[keep.to_numpy()]


def maturity_step(column: str, after: str, *, order: int = 1) -> Any:
    """The recipe step that leaves the immature rows out, for the prescription to replay (M243)."""
    from engine.agent.contracts import RecipeStep, RecipeStepKind

    return RecipeStep(
        order=order,
        kind=RecipeStepKind.REMOVE_ROWS,
        column=column,
        params={"operator": "after", "values": [after]},
    )


# ---------------------------------------------------------------------------
# M246's asks: the protocol decides AUTO, an assumption, or one question
# ---------------------------------------------------------------------------
def _effect(option_id: str, kind: str | None, title: str = "", **payload: Any) -> Any:
    from engine.doctor.visit_contracts import OptionEffect

    return OptionEffect(option_id=option_id, kind=kind, title=title, payload=payload)


def time_settings_asks(
    settings: TimeSettings,
    *,
    rows: int,
    upload_id: str,
    goal_id: str | None,
) -> list[Ask]:
    """The time settings as asks for M246's `settle`: the budget, the cut points, maturity and the cadence.

    The as-of date is M235's `AS_OF_COLUMN` question and the outcome window and lapse are the outcome definition's
    (APPROVE-class): neither is asked here."""
    from engine.doctor.protocol import Ask, AskOption

    asks: list[Ask] = []
    budget = settings.budget
    if budget is not None:
        options = tuple(
            AskOption(
                label=choice.label,
                effect=_effect(
                    choice.choice,
                    "setting",
                    f"Training: {choice.label}. {choice.buys}",
                    setting="training_budget",
                    path=budget.setting_path,
                    value=choice.minutes,
                    choice=choice.choice,
                    expected_seconds=choice.expected_seconds,
                ),
                safe=choice.recommended,
            )
            for choice in budget.choices
        )
        asks.append(
            Ask(
                code="TRAINING_BUDGET",
                subject={"upload": upload_id, "setting": budget.setting_path},
                text="How long may training take?",
                why=budget.reason,
                options=options,
                rows=rows,
                upload_id=upload_id,
                goal_id=goal_id,
                proven="balanced" if budget.confidence == "sure" else None,
                recommended="balanced",
            )
        )
    column = settings.as_of.column
    cuts = settings.cuts
    if cuts is not None and column is not None:
        # Both answers are the split's own setting, so the run's configuration carries them (M243's prescription
        # reads a `setting` item's path and value). The cut points follow from the split's rule on the date.
        random = _effect(
            "random",
            "setting",
            "Split the rows at random",
            setting="cuts",
            path="split.type",
            value="random_stratified",
        )
        keep = _effect(
            "dates",
            "setting",
            f"Test on the newest dates, from {cuts.test_from}",
            setting="cuts",
            path="split.type",
            value="time_based",
            column=column,
        )
        if not cuts.feasible:
            asks.append(
                Ask(
                    code="TIME_CUTS",
                    subject={"upload": upload_id, "column": column},
                    text="Test on the newest dates?",
                    why=cuts.reason,
                    options=(AskOption("Split at random", random, safe=True),),
                    rows=rows,
                    upload_id=upload_id,
                    column=column,
                    goal_id=goal_id,
                    proven="random",
                )
            )
        elif cuts.too_small or cuts.undated_rows:
            # A part too small to judge a model on, or rows with no date that no part holds: the rules cannot be
            # sure, so the person is asked (random is the safe answer).
            asks.append(
                Ask(
                    code="TIME_CUTS",
                    subject={"upload": upload_id, "column": column},
                    text="Keep testing on the newest dates, or split at random?",
                    why=cuts.reason,
                    options=(
                        AskOption("Keep testing on the newest dates", keep),
                        AskOption("Split at random", random, safe=True),
                    ),
                    rows=rows,
                    upload_id=upload_id,
                    column=column,
                    goal_id=goal_id,
                )
            )
        else:
            asks.append(
                Ask(
                    code="TIME_CUTS",
                    subject={"upload": upload_id, "column": column},
                    text="Test on the newest dates?",
                    why=cuts.reason,
                    options=(AskOption("Test on the newest dates", keep, safe=True),),
                    rows=rows,
                    upload_id=upload_id,
                    column=column,
                    goal_id=goal_id,
                    proven="dates",
                )
            )
    maturity = settings.maturity
    if (
        maturity is not None
        and not maturity.mature
        and maturity.question
        and maturity.immature_from is not None
        and column is not None
    ):
        # The change itself: the rows dated after the cut-off are left out, as a recipe step the prescription
        # replays (M243 reads an item's `step`). The cut-off and the step are worked out again on each file
        # (`per_file`), so next month's standing answer leaves next month's newest rows out.
        leave = _effect(
            "leave_out",
            "time_setting",
            f"Leave out the {maturity.immature_rows:,} rows dated after {maturity.immature_from}: their outcomes "
            "are not final yet.",
            setting="label_maturity",
            choice="leave_out",
            column=column,
            window_days=maturity.window_days,
            after=maturity.immature_from,
            step=maturity_step(column, maturity.immature_from).model_dump(mode="json"),
            per_file=["after", "step"],
        )
        asks.append(
            Ask(
                code="LABEL_MATURITY",
                subject={"upload": upload_id, "column": column},
                text=maturity.question,
                why=maturity.reason,
                options=(
                    AskOption("Leave it out", leave, rows_changed=maturity.immature_rows, safe=True),
                    AskOption("Keep it", _effect("keep", None), rows_changed=0),
                ),
                rows=rows,
                upload_id=upload_id,
                column=column,
                goal_id=goal_id,
                blocking=maturity.fails_probe,
                recommended="leave_out",
            )
        )
    cadence = settings.cadence
    if cadence is not None and cadence.proposed != "as_is" and column is not None:
        # The build's own setting (`DatasetSpec.snapshots.frequency`, M237's build reads `spec_path`).
        monthly = _effect(
            "monthly",
            "time_setting",
            "One row per customer for every month-end",
            setting="snapshot_cadence",
            cadence="monthly",
            column=column,
            spec_path=SNAPSHOT_SPEC_PATH,
            value="monthly",
        )
        weekly = _effect(
            "weekly",
            "time_setting",
            "One row per customer for every week-end",
            setting="snapshot_cadence",
            cadence="weekly",
            column=column,
            spec_path=SNAPSHOT_SPEC_PATH,
            value="weekly",
        )
        options = (
            (AskOption("Every month", monthly, safe=True), AskOption("Every week", weekly))
            if cadence.ask
            else (AskOption("Every week", weekly, safe=True),)
        )
        asks.append(
            Ask(
                code="SNAPSHOT_CADENCE",
                subject={"upload": upload_id, "column": column},
                text="Build one row per customer for every month, or for every week?",
                why=cadence.reason,
                options=options,
                rows=rows,
                upload_id=upload_id,
                column=column,
                goal_id=goal_id,
                proven=None if cadence.ask else "weekly",
                recommended="monthly" if cadence.ask else None,
            )
        )
    return asks


# ---------------------------------------------------------------------------
# The answers, applied: what an accepted time setting changes
# ---------------------------------------------------------------------------
class TimeChanges(StrictBase):
    """What a case's accepted time settings change, read back from its items (:func:`time_setting_changes`).

    `settings` are the run's configuration (`model_search.time_limit_minutes`, `split.type`), `steps` the recipe
    steps per upload (leaving out the rows whose outcome is not final), `spec` the build's (`snapshots.frequency`).
    M243's prescription reads the same items (a `setting` item's path and value, any item's `step`); this is the
    one place that says which items are time settings and what each does."""

    settings: dict[str, JsonValue] = Field(default_factory=dict)
    steps: dict[str, tuple[dict[str, JsonValue], ...]] = Field(default_factory=dict)
    spec: dict[str, JsonValue] = Field(default_factory=dict)


def time_setting_changes(case: Case, goal_id: str) -> TimeChanges:
    """The changes the accepted time settings of `goal_id` make (newest decision first wins a setting)."""
    from engine.agent.contracts import ProposalState

    settings: dict[str, JsonValue] = {}
    steps: dict[str, list[dict[str, JsonValue]]] = {}
    spec: dict[str, JsonValue] = {}
    accepted = [
        item
        for item in case.items
        if item.goal_id == goal_id
        and item.state is ProposalState.ACCEPTED
        and item.payload.get("setting") in TIME_SETTING_NAMES
    ]
    accepted.sort(key=lambda item: item.decided_at or item.proposed_at, reverse=True)
    for item in accepted:
        payload = item.payload
        path, spec_path, step = payload.get("path"), payload.get("spec_path"), payload.get("step")
        if item.kind == "setting" and isinstance(path, str):
            settings.setdefault(path, payload.get("value"))
        if isinstance(spec_path, str):
            spec.setdefault(spec_path, payload.get("value"))
        if isinstance(step, dict) and item.upload_id:
            kept = steps.setdefault(item.upload_id, [])
            if step not in kept:
                kept.append(step)
    return TimeChanges(
        settings=settings, steps={upload: tuple(found) for upload, found in steps.items()}, spec=spec
    )


def apply_time_settings(
    frame: pd.DataFrame, changes: TimeChanges, *, upload_id: str, config: UseCaseConfig
) -> pd.DataFrame:
    """`frame` with the time settings' recipe steps replayed on it, the way a prepared copy is made
    (`engine.doctor.materialize.replay_steps`): the rows whose outcome is not final are left out."""
    from engine.agent.contracts import RecipeStep
    from engine.doctor.materialize import replay_steps

    found = changes.steps.get(upload_id, ())
    if not found:
        return frame
    steps = [
        RecipeStep.model_validate(step).model_copy(update={"order": index + 1})
        for index, step in enumerate(found)
    ]
    return replay_steps(
        frame,
        steps,
        upload_id=upload_id,
        levels=config.agent.levels,
        max_failure_pct=config.agent.max_conversion_failure_pct,
    ).frame


# ---------------------------------------------------------------------------
# The visit's detector (M246's port): time settings in TREAT, for a goal
# ---------------------------------------------------------------------------
def time_settings_detector(
    storage: Storage | None,
    *,
    config_root: Any = None,
    clock: Callable[[], Any] = utc_now,
) -> DetectorPort:
    """The visit's time-settings detector (`engine.doctor.visit.DetectorPort`): for a goal, the training budget
    (the three choices, kept beside the data as the offer a run is promised from) and the data time settings, as
    asks M246's protocol settles. Generic visits (no goal) have nothing to time. Rules only; works with no AI.
    """

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.config import ConfigError, load_use_case
        from engine.doctor.visit import Detected

        if brief is None or not files:
            return Detected()
        goal = brief.goal_id
        outcome = decisions(case).pair("outcome", goal)
        file = next((f for f in files if outcome is not None and f.upload_id == outcome[0]), files[0])
        try:
            config = load_use_case(goal, config_root)
        except ConfigError:
            return Detected()
        budget: TrainingBudget | None = None
        try:
            inputs = time_inputs(
                storage, config, rows=file.profile.row_count, columns=file.profile.column_count
            )
            budget = propose_budget(inputs)
            if storage is not None:
                save_offer(
                    storage,
                    TimeOffer(
                        use_case_id=config.id,
                        source_kind=source_kind_of(storage, file.upload_id),
                        source_id=file.upload_id,
                        offered_at=clock(),
                        rows=file.profile.row_count,
                        columns=file.profile.column_count,
                        budget=budget,
                    ),
                )
        except TimeSettingsError:
            budget = None
        as_of = decisions(case).pair("as_of_column", goal)
        given = as_of[1] if as_of is not None and as_of[0] == file.upload_id else None
        try:
            settings = propose_time_settings(
                file,
                config,
                brief=brief,
                as_of_column=given,
                outcome_column=outcome[1] if outcome is not None and outcome[0] == file.upload_id else None,
                primary_key=decisions(case).key(file.upload_id),
                budget=budget,
                config_root=config_root,
            )
        except TimeSettingsError:
            settings = TimeSettings(
                budget=budget,
                as_of=AsOf(column=None, reason="The chosen date column cannot be read as dates."),
                outcome_window=OutcomeWindow(days=None, reason=""),
            )
        asks = time_settings_asks(
            settings, rows=file.profile.row_count, upload_id=file.upload_id, goal_id=goal
        )
        return Detected(asks=tuple(asks))

    return detect
