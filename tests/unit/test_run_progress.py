"""Plan M M189 (DEC-1619): the time-weighted progress estimate, as a pure rule and from stored runs.

The user's report: "99% of the time is training, so the bar gets stuck on 50%". `progress_pct` weights
every stage the same; `estimated_pct` weights each by its expected duration and credits the running
stage for the time it has run. These tests pin the rule's paths: the time budget, past runs, no basis at
all, a stage that overruns, monotonicity, and 100 only when done.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from engine.config import ProblemType, RunMode, load_use_case
from engine.contracts import RunError, RunRecord, RunState, RunStatus, StageKey, StageStatus
from engine.pipeline import RUN_FILENAME, STATUS_FILENAME, TRAIN_STAGES, stages_for
from engine.run_progress import (
    DEFAULT_STAGE_SECONDS,
    OVERRUN_FRACTION,
    PastDurations,
    ProgressTracker,
    estimate_progress,
    expected_durations,
    past_durations,
    train_budget_seconds,
    trainer_of,
)
from engine.runs import RUN_CONFIG_FILENAME, cancel_run, restore_ending, update_stage
from engine.storage import LocalStorage, run_key
from engine.uplift.config import UpliftBaseModel
from engine.utils.time import utc_now

USE_CASE = "targeted-advertisement"
BUDGET = 1800.0
NO_PAST = PastDurations(stages={})


def rows(
    states: dict[StageKey, RunState], keys: tuple[StageKey, ...] = TRAIN_STAGES
) -> tuple[StageStatus, ...]:
    return tuple(
        StageStatus(key=key, title=key.value, group_label="g", state=states.get(key, RunState.PENDING))
        for key in keys
    )


def running_train(**done: RunState) -> tuple[StageStatus, ...]:
    """The four stages before train done, train running."""
    states = dict.fromkeys(TRAIN_STAGES[: TRAIN_STAGES.index(StageKey.TRAIN)], RunState.DONE)
    states[StageKey.TRAIN] = RunState.RUNNING
    return rows(states)


# ---------------------------------------------------------------------------
# Expected durations
# ---------------------------------------------------------------------------
def test_budget_path_the_train_stage_is_expected_to_take_its_time_budget() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=BUDGET, past=NO_PAST)
    assert expectations is not None
    assert expectations.basis == "time_budget"
    assert expectations.seconds[StageKey.TRAIN] == BUDGET
    assert expectations.seconds[StageKey.INGEST] == DEFAULT_STAGE_SECONDS[StageKey.INGEST]


def test_budget_path_the_bar_moves_through_the_train_stage_instead_of_sitting_at_50() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=BUDGET, past=NO_PAST)
    assert expectations is not None
    stages = running_train()
    readings = [
        estimate_progress(stages, expectations.seconds, running_elapsed=elapsed).pct
        for elapsed in (0.0, 450.0, 900.0, 1350.0)
    ]
    # Train is 1800 of the run's 1880 expected seconds: a quarter of the way through it is ~25 points on.
    assert readings == sorted(readings) and len(set(readings)) == 4
    assert readings[0] < 5
    assert 45 <= readings[2] <= 52
    assert readings[3] < 99


def test_the_eta_counts_the_running_stages_remainder_and_every_stage_after_it() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=600.0, past=NO_PAST)
    assert expectations is not None
    estimate = estimate_progress(running_train(), expectations.seconds, running_elapsed=200.0)
    after = sum(
        DEFAULT_STAGE_SECONDS[key] for key in (StageKey.EVALUATE, StageKey.EXPLAIN, StageKey.REGISTER)
    )
    assert estimate.eta_seconds == 400 + after
    assert estimate.fraction == pytest.approx(200 / 600)
    assert estimate.overrun is False


def test_past_runs_path_the_median_of_past_train_durations_when_the_trainer_has_no_budget() -> None:
    past = PastDurations(stages={StageKey.INGEST: (2.0, 4.0, 9.0)}, train=(100.0, 120.0, 400.0))
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=None, past=past)
    assert expectations is not None
    assert expectations.basis == "past_runs"
    assert expectations.seconds[StageKey.TRAIN] == 120.0
    assert expectations.seconds[StageKey.INGEST] == 4.0
    assert expectations.seconds[StageKey.EXPLAIN] == DEFAULT_STAGE_SECONDS[StageKey.EXPLAIN]


def test_the_budget_wins_over_past_runs_whenever_the_trainer_honours_one() -> None:
    for train_history in ((5000.0,), (100.0, 120.0, 400.0)):
        past = PastDurations(stages={StageKey.INGEST: (2.0, 4.0, 9.0)}, train=train_history)
        expectations = expected_durations(TRAIN_STAGES, budget_seconds=BUDGET, past=past)
        assert expectations is not None
        assert expectations.seconds[StageKey.TRAIN] == BUDGET and expectations.basis == "time_budget"
        # the other stages still come from history
        assert expectations.seconds[StageKey.INGEST] == 4.0


def test_past_runs_under_a_smaller_budget_do_not_hold_back_a_larger_budget_now() -> None:
    """Review fix: a user raises `time_limit_minutes` from 5 to 60. The past runs took ~300 s under the old
    budget; expecting 300 s now would put the bar on "Finishing up" after five minutes, for the 55 left."""
    past = PastDurations(stages={}, train=(300.0, 290.0, 310.0))
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=3600.0, past=past)
    assert expectations is not None
    assert expectations.seconds[StageKey.TRAIN] == 3600.0
    assert expectations.basis == "time_budget"
    ten_minutes_in = estimate_progress(running_train(), expectations.seconds, running_elapsed=600.0)
    assert ten_minutes_in.overrun is False and ten_minutes_in.eta_seconds is not None
    assert ten_minutes_in.eta_seconds > 50 * 60


def test_no_basis_path_a_training_run_with_neither_budget_nor_history_gets_no_estimate() -> None:
    assert expected_durations(TRAIN_STAGES, budget_seconds=None, past=NO_PAST) is None


def test_a_scoring_run_without_history_uses_the_documented_stage_defaults() -> None:
    expectations = expected_durations(stages_for(RunMode.SCORE), budget_seconds=None, past=NO_PAST)
    assert expectations is not None
    assert expectations.basis == "stages"
    assert expectations.seconds[StageKey.PREDICT] == DEFAULT_STAGE_SECONDS[StageKey.PREDICT]


def test_the_budget_follows_the_trainer(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    assert trainer_of(config) == "automl"
    assert train_budget_seconds(config) == config.model_search.time_limit_minutes * 60
    uplift = config.model_copy(update={"problem_type": ProblemType.UPLIFT})
    assert trainer_of(uplift).startswith("uplift:")
    on_lightgbm = uplift.model_copy(
        update={"uplift": uplift.uplift.model_copy(update={"base_model": UpliftBaseModel.LIGHTGBM})}
    )
    assert train_budget_seconds(on_lightgbm) is None
    on_autogluon = uplift.model_copy(
        update={"uplift": uplift.uplift.model_copy(update={"base_model": UpliftBaseModel.AUTOGLUON_FAST})}
    )
    assert train_budget_seconds(on_autogluon) == uplift.uplift.time_limit_minutes * 60


# ---------------------------------------------------------------------------
# The rule's limits
# ---------------------------------------------------------------------------
def test_overrun_caps_the_running_stage_at_095_and_drops_the_eta() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=60.0, past=NO_PAST)
    assert expectations is not None
    estimate = estimate_progress(running_train(), expectations.seconds, running_elapsed=600.0)
    assert estimate.overrun is True
    assert estimate.fraction == OVERRUN_FRACTION
    assert estimate.eta_seconds is None
    assert estimate.pct < 99


def test_never_more_than_99_before_the_run_is_done_and_100_only_when_it_is() -> None:
    expectations = dict.fromkeys(TRAIN_STAGES, 1.0)
    almost = rows(dict.fromkeys(TRAIN_STAGES[:-1], RunState.DONE) | {StageKey.REGISTER: RunState.RUNNING})
    assert estimate_progress(almost, expectations, running_elapsed=50.0).pct == 99
    done = rows(dict.fromkeys(TRAIN_STAGES, RunState.DONE))
    final = estimate_progress(done, expectations, running_elapsed=None)
    assert (final.pct, final.eta_seconds) == (100, None)  # an ended run has no time left (the contract)


def test_monotonic_a_reading_never_goes_below_the_last_one() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=BUDGET, past=NO_PAST)
    assert expectations is not None
    # Between stages (train done, evaluate not yet started) the raw rule could dip below an overrun reading.
    assert estimate_progress(running_train(), expectations.seconds, running_elapsed=0.0, floor=61).pct == 61


def test_monotonic_through_a_tracker_across_an_overrun_and_the_stage_ending() -> None:
    now = [0.0]
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=100.0, past=NO_PAST)
    assert expectations is not None
    tracker = ProgressTracker(expectations, clock=lambda: now[0])
    status = RunStatus(
        run_id="r_x",
        mode=RunMode.TRAIN,
        state=RunState.RUNNING,
        updated_at=utc_now(),
        stages=running_train(),
        current_stage=StageKey.TRAIN,
        progress_pct=50,
    )
    tracker.stage_started(StageKey.TRAIN)
    readings = []
    for step in range(12):
        now[0] = step * 20.0
        readings.append(tracker.decorate(status).estimated_pct)
    tracker.stage_ended()
    states = dict.fromkeys(TRAIN_STAGES[:5], RunState.DONE)
    ended = status.model_copy(update={"stages": rows(states), "current_stage": None, "progress_pct": 62})
    readings.append(tracker.decorate(ended).estimated_pct)
    assert readings == sorted(readings)
    assert all(reading is not None and reading <= 99 for reading in readings)


def test_a_failed_or_cancelled_run_keeps_its_last_reading_and_has_no_time_left() -> None:
    stages = rows({StageKey.INGEST: RunState.DONE, StageKey.VALIDATE: RunState.CANCELLED})
    estimate = estimate_progress(stages, {}, running_elapsed=None, floor=3)
    assert (estimate.pct, estimate.eta_seconds, estimate.fraction) == (3, None, None)


def test_the_tracker_marks_only_the_running_stage_with_a_fraction() -> None:
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=100.0, past=NO_PAST)
    assert expectations is not None
    now = [0.0]
    tracker = ProgressTracker(expectations, clock=lambda: now[0])
    tracker.stage_started(StageKey.TRAIN)
    now[0] = 50.0
    status = RunStatus(
        run_id="r_x",
        mode=RunMode.TRAIN,
        state=RunState.RUNNING,
        updated_at=utc_now(),
        stages=running_train(),
        current_stage=StageKey.TRAIN,
        progress_pct=50,
    )
    decorated = tracker.decorate(status)
    fractions = {row.key: row.fraction for row in decorated.stages}
    assert fractions.pop(StageKey.TRAIN) == 0.5
    assert set(fractions.values()) == {None}
    assert decorated.estimate_basis == "time_budget"
    assert decorated.progress_pct == 50  # untouched


# ---------------------------------------------------------------------------
# Past runs, read from the store
# ---------------------------------------------------------------------------
def _seed_run(
    storage: LocalStorage,
    run_id: str,
    *,
    seconds: dict[StageKey, float],
    problem_type: str,
    state: RunState = RunState.DONE,
    use_case: str = USE_CASE,
) -> None:
    started = utc_now() - timedelta(hours=1)
    storage.write_model(
        run_key(run_id, RUN_FILENAME),
        RunRecord(
            run_id=run_id,
            use_case_id=use_case,
            use_case_name="Targeted",
            mode=RunMode.TRAIN,
            state=state,
            created_at=started,
            upload_id="u_1",
            file_name="f.csv",
            primary_key="id",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="__automl__",
            engine_version="0",
        ),
    )
    stages = tuple(
        StageStatus(
            key=key,
            title=key.value,
            group_label="g",
            state=RunState.DONE,
            duration_seconds=seconds.get(key, 1.0),
        )
        for key in TRAIN_STAGES
    )
    storage.write_model(
        run_key(run_id, STATUS_FILENAME),
        RunStatus(
            run_id=run_id,
            mode=RunMode.TRAIN,
            state=state,
            updated_at=started,
            stages=stages,
            progress_pct=100,
        ),
    )
    storage.write_text(
        run_key(run_id, RUN_CONFIG_FILENAME),
        json.dumps({"config": {"problem_type": problem_type, "uplift": {}}}),
    )


def test_past_durations_reads_finished_runs_of_the_same_use_case_and_keeps_train_for_the_same_trainer(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    _seed_run(
        storage,
        "r_20261001_00000001",
        seconds={StageKey.TRAIN: 100.0, StageKey.INGEST: 3.0},
        problem_type="binary_classification",
    )
    _seed_run(
        storage,
        "r_20261002_00000002",
        seconds={StageKey.TRAIN: 140.0, StageKey.INGEST: 5.0},
        problem_type="binary_classification",
    )
    _seed_run(
        storage,
        "r_20261003_00000003",
        seconds={StageKey.TRAIN: 999.0},
        problem_type="binary_classification",
        state=RunState.FAILED,
    )
    _seed_run(
        storage,
        "r_20261004_00000004",
        seconds={StageKey.TRAIN: 777.0},
        problem_type="binary_classification",
        use_case="other",
    )
    _seed_run(
        storage,
        "r_20261005_00000005",
        seconds={StageKey.TRAIN: 555.0, StageKey.INGEST: 4.0},
        problem_type="uplift",
    )
    past = past_durations(
        storage, use_case_id=USE_CASE, mode=RunMode.TRAIN, trainer="automl", exclude_run_id="r_now"
    )
    assert sorted(past.train) == [100.0, 140.0]
    assert sorted(past.stages[StageKey.INGEST]) == [3.0, 4.0, 5.0]
    expectations = expected_durations(TRAIN_STAGES, budget_seconds=None, past=past)
    assert expectations is not None and expectations.seconds[StageKey.TRAIN] == 120.0


def test_past_durations_on_an_empty_store_is_empty(tmp_path: Path) -> None:
    past = past_durations(
        LocalStorage(tmp_path), use_case_id=USE_CASE, mode=RunMode.TRAIN, trainer="automl", exclude_run_id="r"
    )
    assert past.train == () and dict(past.stages) == {}


# ---------------------------------------------------------------------------
# The writers in engine/runs.py leave no stale "time left" on an ended run
# ---------------------------------------------------------------------------
def test_cancel_run_and_a_failure_from_outside_clear_the_time_left(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _seed_run(
        storage,
        "r_20261006_00000006",
        seconds={},
        problem_type="binary_classification",
        state=RunState.RUNNING,
    )
    key = run_key("r_20261006_00000006", STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    live = status.model_copy(
        update={
            "stages": tuple(
                (
                    row.model_copy(update={"state": RunState.RUNNING, "fraction": 0.4})
                    if row.key is StageKey.TRAIN
                    else row.model_copy(update={"state": RunState.PENDING})
                )
                for row in status.stages
            ),
            "state": RunState.RUNNING,
            "estimated_pct": 40,
            "eta_seconds": 300,
            "estimate_basis": "time_budget",
        }
    )
    storage.write_model(key, live)
    cancel_run(storage, "r_20261006_00000006")
    after = storage.read_model(key, RunStatus)
    assert after.eta_seconds is None
    assert after.estimated_pct == 40
    assert all(row.fraction is None for row in after.stages)
    storage.write_model(key, live)
    failed = update_stage(storage, "r_20261006_00000006", StageKey.TRAIN, state=RunState.FAILED)
    assert failed.eta_seconds is None
    assert all(row.fraction is None for row in failed.stages)


def _live_training(storage: LocalStorage, run_id: str) -> RunStatus:
    """A seeded run whose status.json says train is running, as a stray heartbeat write would leave it."""
    _seed_run(storage, run_id, seconds={}, problem_type="binary_classification", state=RunState.RUNNING)
    key = run_key(run_id, STATUS_FILENAME)
    status = storage.read_model(key, RunStatus)
    live = status.model_copy(
        update={
            "stages": tuple(
                (
                    row.model_copy(update={"state": RunState.RUNNING, "fraction": 0.4})
                    if row.key is StageKey.TRAIN
                    else row.model_copy(update={"state": RunState.PENDING})
                )
                for row in status.stages
            ),
            "state": RunState.RUNNING,
            "current_stage": StageKey.TRAIN,
            "estimated_pct": 40,
            "eta_seconds": 300,
        }
    )
    storage.write_model(key, live)
    return live


def test_restore_ending_puts_a_cancel_or_a_failure_back_and_leaves_everything_else_alone(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    run_id = "r_20261007_00000007"
    status_key, record_key = run_key(run_id, STATUS_FILENAME), run_key(run_id, RUN_FILENAME)
    _live_training(storage, run_id)
    running = storage.read_model(record_key, RunRecord)
    assert restore_ending(storage, running) is None, "a run that has not ended is left alone"

    moment = utc_now()
    cancelled = running.model_copy(update={"state": RunState.CANCELLED, "finished_at": moment})
    storage.write_model(record_key, cancelled)
    restored = restore_ending(storage, cancelled)
    assert restored is not None and storage.read_model(status_key, RunStatus) == restored
    assert (restored.state, restored.current_stage, restored.eta_seconds) == (RunState.CANCELLED, None, None)
    assert restored.updated_at == moment and all(row.fraction is None for row in restored.stages)
    assert restore_ending(storage, cancelled) is None, "an ended status.json is never rewritten"

    _live_training(storage, run_id)
    error = RunError(code="E_TEST", message="The instance was reclaimed.", stage=StageKey.TRAIN)
    failed = running.model_copy(update={"state": RunState.FAILED, "finished_at": moment, "error": error})
    after = restore_ending(storage, failed)
    assert after is not None and after.state is RunState.FAILED
    train_row = next(row for row in after.stages if row.key is StageKey.TRAIN)
    assert train_row.state is RunState.FAILED and train_row.error == error

    _live_training(storage, run_id)
    done = running.model_copy(update={"state": RunState.DONE, "finished_at": moment})
    assert restore_ending(storage, done) is None, "done is the flow's own ending, never restored from outside"
    assert storage.read_model(status_key, RunStatus).state is RunState.RUNNING


def test_a_status_written_before_m189_still_reads_with_the_new_fields_null() -> None:
    old = {
        "run_id": "r",
        "mode": "train",
        "state": "running",
        "updated_at": "2026-10-01T00:00:00Z",
        "stages": [{"key": "ingest", "title": "t", "group_label": "g", "state": "done"}],
        "progress_pct": 12,
    }
    status = RunStatus.model_validate(old)
    assert (status.estimated_pct, status.eta_seconds, status.estimate_basis) == (None, None, None)
    assert status.stages[0].fraction is None


def test_one_estimate_of_the_whole_run_is_said_before_it_starts_and_counted_down_in_it() -> None:
    """The approval card, the model offer and the run page say one time: the search's budget, the stages around it and
    the steps after it, rounded the way the run page rounds its countdown (`ui/run_progress.js` `etaText`)."""
    from engine.run_progress import LATER_STEP_SECONDS, about_minutes, expected_run_minutes

    assert [expected_run_minutes(m) for m in (5, 15, 40)] == [8, 20, 45]
    assert expected_run_minutes(15) >= 15, "never less than the search's own budget"
    # the rounding of the countdown: to the minute under ten, to five under an hour, to ten beyond
    assert [about_minutes(s) for s in (30, 90, 540, 600, 1065, 3000, 5400)] == [1, 2, 9, 10, 20, 50, 90]
    assert LATER_STEP_SECONDS == {"score": 60, "list": 20, "campaign": 10}
