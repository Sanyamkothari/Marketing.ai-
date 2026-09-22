"""Design rules 3 and 4, proved end to end through the real flows rather than at stage level.

Two rules are load-bearing enough that a unit test of the function that implements them is not
enough - what matters is that no *wiring* between the stages reintroduces what they forbid:

**Rule 4 - every transform is fitted on training rows only, and the test split is final-decision
only.** Proved by perturbation, in both directions, so it cannot pass vacuously. The same file is
run through the whole train flow three times: untouched, with only the HOLD-OUT rows (validation
and test) wrecked, and with only the TRAINING rows wrecked. The first two must record transform
parameters that are byte-identical; the third must not. The score flow gets the same treatment:
a wrecked scoring file must be replayed with the training run's parameters unchanged, and the
recorded fill value must NOT equal the median of the file being scored - which is exactly what it
would equal if anything were fitted at score time.

**Rule 3 - promotion re-scores the current champion and the challenger on the same held-out frame;
a stored score from an older test split is never an input, by any path including approval.** Proved
by planting an absurd stored `test_score` on the reigning champion. Two worlds are built from one
approved champion by copying the whole storage and registry; one world's champion is planted at
0.999999 (which, if it were read, would make promotion impossible), the other's at 0.000001 (which
would make promotion automatic). The same challenger is then trained in each, and the two runs must
reach the same decision, for the same reason: the outcome matches the rule applied to the RE-SCORED
number in both worlds, and in at least one of them the rule applied to the STORED number would have
answered differently.

The approval path is then walked to its stale end, from a row the register stage really produced:
the first model of the use case waits for approval stamped with the champion its decision was taken
against, which was none. A copy of that world is taken while it is still waiting; in the copy a
different model takes the title, and `approve` refuses with `CHAMPION_CHANGED` and moves nothing.
In the original, where nothing took the title underneath it, the same row approves and becomes
champion - which is the guard that says the check refuses staleness rather than approval.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from sqlmodel import Session, create_engine, select

from engine.config import ColumnRole, ResolvedConfig, RunMode, UseCaseConfig, resolve_config
from engine.contracts import (
    NO_CHAMPION_AT_DECISION,
    ModelStatus,
    ModelVersion,
    PrepareReport,
    RunState,
    Transform,
)
from engine.jobs import CancelToken
from engine.pipeline import MANIFEST_FILENAME, PREPARE_FILENAME, Pipeline, StageContext
from engine.registry import LocalModelRegistry, ModelVersionRow, RegistryError
from engine.stages import ingest, prepare
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = [pytest.mark.slow, pytest.mark.integration]

USE_CASE: str = "targeted-advertisement"
PRIMARY_KEY: str = "customer_id"
TARGET: str = "converted_30d"
TRAIN_ROWS: int = 2_500
SCORE_ROWS: int = 400

# plan §10's settings, narrowed to one model family: this module measures preparation and the
# champion rule, and a wider search would only make the same point more slowly.
OVERRIDES: dict[str, object] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.ensemble": False,
    "model_search.candidates": ["LightGBM"],
    "model_search.tuning_trials": 5,  # DEC-057: a trial is a real fit; 5 is the schema floor
    # `fill` records a fitted value for EVERY feature column and `clip` records fitted percentiles
    # for every numeric one, so the report below carries the widest set of fitted parameters the
    # prepare stage can produce - the most there is for a hold-out row to contaminate.
    "prepare.missing_values": "fill",
    "prepare.outliers": "clip",
}

NUMERIC_SHIFT: float = 1_000.0
NUMERIC_OFFSET: float = 7_777.0
TEXT_REPLACEMENT: str = "perturbed_zz"


# ---------------------------------------------------------------------------
# Running the flows
# ---------------------------------------------------------------------------
class _NoJobs:
    """The flows run on this thread; the runner is only there to satisfy the constructor."""

    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the pipeline must not submit jobs")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the pipeline must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@dataclass(frozen=True)
class World:
    """One storage directory and registry, and the config every run in it resolves."""

    root: Path
    storage: LocalStorage
    registry: LocalModelRegistry
    resolved: ResolvedConfig

    @property
    def config(self) -> UseCaseConfig:
        return self.resolved.config

    def prepare_report(self, run_id: str) -> PrepareReport:
        return self.storage.read_model(run_key(run_id, PREPARE_FILENAME), PrepareReport)


def build_world(root: Path, config_root: Path) -> World:
    root.mkdir(parents=True, exist_ok=True)
    return World(
        root=root,
        storage=LocalStorage(root / "data"),
        registry=LocalModelRegistry(root / "registry.db"),
        resolved=resolve_config(USE_CASE, OVERRIDES, root=config_root),
    )


def clone_world(source: World, root: Path, config_root: Path) -> World:
    """A byte-for-byte copy of a world, so two histories can diverge from the same past."""
    shutil.copytree(source.root, root)
    return build_world(root, config_root)


def upload(world: World, frame: pd.DataFrame, *, name: str) -> str:
    key = upload_key(f"u_{name}", "source.csv")
    world.storage.write_text(key, frame.to_csv(index=False, lineterminator="\n"))
    return key


def run_flow(
    world: World, frame: pd.DataFrame, *, name: str, run_id: str, mode: RunMode
) -> tuple[str, RunState]:
    """Run one whole flow over `frame`, exactly as `POST /runs` would, and return its outcome."""
    world.storage.write_model(run_key(run_id, "run_config.json"), world.resolved)
    ctx = StageContext(
        run_id=run_id,
        mode=mode,
        config=world.config,
        resolved=world.resolved,
        storage=world.storage,
        registry=world.registry,
        cancel=CancelToken(),
        primary_key=PRIMARY_KEY,
        target=TARGET if mode is RunMode.TRAIN else None,
        upload_key=upload(world, frame, name=name),
        model_version_id=None,
    )
    pipeline = Pipeline(world.storage, world.registry, _NoJobs())
    runner = pipeline.run_train if mode is RunMode.TRAIN else pipeline.run_score
    record = runner(ctx)
    assert record.state is RunState.DONE, f"{name}: {record.error}"
    return record.model_version_id or "", record.state


# ---------------------------------------------------------------------------
# Perturbation
# ---------------------------------------------------------------------------
def feature_columns(config: UseCaseConfig) -> tuple[tuple[str, bool], ...]:
    """Every template feature column, with whether it holds a number, from the config alone."""
    return tuple(
        (column.name, column.type.value in {"integer", "float"})
        for column in config.template.by_role(ColumnRole.FEATURE)
    )


def wreck(frame: pd.DataFrame, keys: frozenset[str], config: UseCaseConfig) -> pd.DataFrame:
    """Wreck every feature value of the named rows, and nothing else about the file.

    Numbers are shifted far out of their own range so both the fitted median and the fitted 1st/99th
    percentiles would move; text columns are collapsed onto one novel level so the fitted mode would
    move too. The primary key, the target, the consent column, the time column, the row order and
    the row count are all left exactly as they were, so the row-phase decisions and the split - the
    two things that happen *before* any statistic is fitted - cannot move for any other reason.
    """
    wrecked = frame.copy()
    rows = wrecked[PRIMARY_KEY].astype(str).isin(keys)
    assert rows.any(), "the perturbation matched no rows"
    for column, numeric in feature_columns(config):
        if column not in wrecked.columns:
            continue
        if numeric:
            values = pd.to_numeric(wrecked[column], errors="coerce")
            wrecked[column] = values.where(~rows, values * NUMERIC_SHIFT + NUMERIC_OFFSET)
        else:
            wrecked.loc[rows, column] = TEXT_REPLACEMENT
    assert not wrecked.equals(frame), "the perturbation changed nothing"
    return wrecked


def partition_keys(world: World, frame: pd.DataFrame, *, run_id: str) -> dict[str, frozenset[str]]:
    """Which primary keys the flow's own split will put in each part, by asking the flow's own code.

    `prepare_rows` then `split_dataset`, with this run's id, is verbatim what `_TrainFlow` does
    between the row phase and the train-only fit, so the partition this returns is the partition the
    run will use.
    """
    rows, _ = prepare.prepare_rows(frame, world.config, primary_key=PRIMARY_KEY, target=TARGET)
    parts, _ = prepare.split_dataset(rows, world.config, run_id=run_id, target=TARGET)
    return {name: frozenset(part[PRIMARY_KEY].astype(str)) for name, part in parts.items()}


def transforms_json(report: PrepareReport) -> tuple[str, ...]:
    """Every recorded transform as the bytes it was stored as - order, kind, columns, parameters."""
    return tuple(transform.model_dump_json() for transform in report.transforms)


def fitted_fill(report: PrepareReport, column: str) -> float | None:
    """The median this column was filled with, as recorded, or None when none was fitted."""
    for transform in report.transforms:
        if transform.kind == "fill_median" and transform.columns == (column,):
            value = transform.parameters.get("value")
            return float(value) if isinstance(value, (int, float)) else None
    return None


# ---------------------------------------------------------------------------
# Rule 4, the train flow
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleFourTrain:
    """One file run through the whole train flow three times, perturbed differently each time."""

    config: UseCaseConfig
    untouched: PrepareReport
    holdout_wrecked: PrepareReport
    train_wrecked: PrepareReport
    train_keys: frozenset[str]
    holdout_keys: frozenset[str]


@pytest.fixture(scope="module")
def rule_four_train(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> RuleFourTrain:
    root = tmp_path_factory.mktemp("rule-four-train")
    base = build_world(root / "base", config_root)
    frame = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant="clean", seed=20260921))
    # Read the file back exactly as the ingest stage will, so the partition computed below is the
    # partition the flow computes, dtype for dtype.
    source = upload(base, frame, name="probe")
    frame = ingest.read_table(base.storage, source)

    # One run id for all three runs: `split_dataset` seeds from it, so the three runs partition the
    # same rows the same way and the only difference between them is the perturbation.
    run_id = new_run_id()
    parts = partition_keys(base, frame, run_id=run_id)
    train_keys = parts["train"]
    holdout_keys = parts["validation"] | parts["test"]
    assert train_keys and holdout_keys
    assert not (train_keys & holdout_keys), "a row cannot be both fitted on and held out"

    reports: dict[str, PrepareReport] = {}
    for name, rows in (
        ("untouched", None),
        ("holdout_wrecked", holdout_keys),
        ("train_wrecked", train_keys),
    ):
        world = build_world(root / name, config_root)
        payload = frame if rows is None else wreck(frame, rows, world.config)
        run_flow(world, payload, name=name, run_id=run_id, mode=RunMode.TRAIN)
        reports[name] = world.prepare_report(run_id)

    return RuleFourTrain(
        config=base.config,
        untouched=reports["untouched"],
        holdout_wrecked=reports["holdout_wrecked"],
        train_wrecked=reports["train_wrecked"],
        train_keys=train_keys,
        holdout_keys=holdout_keys,
    )


def test_the_train_flow_really_fitted_something(rule_four_train: RuleFourTrain) -> None:
    """Without this, "identical" below could mean "there were no parameters to differ"."""
    report = rule_four_train.untouched
    kinds = {transform.kind for transform in report.transforms}
    assert {"fill_median", "clip_percentile"} <= kinds, kinds
    fitted = [
        transform
        for transform in report.transforms
        if transform.parameters.get("applied") is True and "value" in transform.parameters
    ]
    assert fitted, "no fitted value was recorded at all"
    assert report.feature_columns


def test_wrecking_the_hold_out_moves_no_fitted_parameter(rule_four_train: RuleFourTrain) -> None:
    """Design rule 4: the validation and test rows may not reach a single fitted statistic."""
    assert transforms_json(rule_four_train.holdout_wrecked) == transforms_json(rule_four_train.untouched)


def test_wrecking_the_training_rows_moves_them(rule_four_train: RuleFourTrain) -> None:
    """The guard against a vacuous pass: the same wrecking, applied to fit rows, must show up."""
    assert transforms_json(rule_four_train.train_wrecked) != transforms_json(rule_four_train.untouched)


def test_every_numeric_parameter_moved_when_the_training_rows_moved(
    rule_four_train: RuleFourTrain,
) -> None:
    """Not "something changed" but "each fitted number changed", column by column."""
    config_numeric = {name for name, numeric in feature_columns(rule_four_train.config) if numeric}
    untouched = _by_column(rule_four_train.untouched)
    wrecked = _by_column(rule_four_train.train_wrecked)
    checked = 0
    for (kind, column), before in untouched.items():
        if column not in config_numeric or kind not in {"fill_median", "clip_percentile"}:
            continue
        if before.parameters.get("applied") is not True:
            continue
        after = wrecked[(kind, column)]
        assert before.parameters != after.parameters, f"{kind} on {column} did not move"
        checked += 1
    assert checked >= 4, "too few fitted numeric parameters to make this test mean anything"


def test_the_three_runs_prepared_the_same_rows_and_columns(rule_four_train: RuleFourTrain) -> None:
    """The perturbation changed values only: the row phase and the split saw the same table."""
    for report in (rule_four_train.holdout_wrecked, rule_four_train.train_wrecked):
        assert report.rows_in == rule_four_train.untouched.rows_in
        assert report.rows_out == rule_four_train.untouched.rows_out
        assert report.feature_columns == rule_four_train.untouched.feature_columns
        assert report.dropped_columns == rule_four_train.untouched.dropped_columns


def _by_column(report: PrepareReport) -> dict[tuple[str, str], Transform]:
    """Every per-column transform, keyed by kind and column; `dedupe` and friends name no column."""
    return {
        (transform.kind, transform.columns[0]): transform
        for transform in report.transforms
        if transform.columns
    }


# ---------------------------------------------------------------------------
# Rule 4, the score flow
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleFourScore:
    """A champion, then the same scoring file scored twice: as generated, and wrecked."""

    world: World
    training_run_id: str
    clean: PrepareReport
    wrecked: PrepareReport
    wrecked_frame: pd.DataFrame


@pytest.fixture(scope="module")
def rule_four_score(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> RuleFourScore:
    world = build_world(tmp_path_factory.mktemp("rule-four-score") / "world", config_root)
    frame = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant="clean", seed=20260921))
    training_run_id = new_run_id()
    model_id, _ = run_flow(world, frame, name="train", run_id=training_run_id, mode=RunMode.TRAIN)
    if world.registry.get(model_id).status is ModelStatus.PENDING_APPROVAL:
        world.registry.approve(model_id, by="tests")

    scoring = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant="scoring", seed=20270405))
    scoring = ingest.read_table(world.storage, upload(world, scoring, name="probe-score"))
    every_row = frozenset(scoring[PRIMARY_KEY].astype(str))
    wrecked_frame = wreck(scoring, every_row, world.config)

    clean_id, wrecked_id = new_run_id(), new_run_id()
    run_flow(world, scoring, name="score-clean", run_id=clean_id, mode=RunMode.SCORE)
    run_flow(world, wrecked_frame, name="score-wrecked", run_id=wrecked_id, mode=RunMode.SCORE)
    return RuleFourScore(
        world=world,
        training_run_id=training_run_id,
        clean=world.prepare_report(clean_id),
        wrecked=world.prepare_report(wrecked_id),
        wrecked_frame=wrecked_frame,
    )


def test_a_scoring_run_replays_the_training_runs_parameters(rule_four_score: RuleFourScore) -> None:
    """Design rule 4 through the score flow: nothing is fitted, so nothing can move."""
    training = rule_four_score.world.prepare_report(rule_four_score.training_run_id)
    assert transforms_json(rule_four_score.clean) == transforms_json(training)
    assert transforms_json(rule_four_score.wrecked) == transforms_json(training)


def test_a_scoring_runs_prepare_report_is_the_training_runs_record(
    rule_four_score: RuleFourScore,
) -> None:
    """Not a lookalike recomputed on the scoring file: the training run's own report (DEC-049)."""
    for report in (rule_four_score.clean, rule_four_score.wrecked):
        assert report.run_id == rule_four_score.training_run_id


def test_the_recorded_fill_is_not_the_scoring_files_own_median(rule_four_score: RuleFourScore) -> None:
    """The guard against a vacuous pass: a value fitted at score time WOULD equal this median."""
    numeric = [name for name, is_numeric in feature_columns(rule_four_score.world.config) if is_numeric]
    compared = 0
    for column in numeric:
        recorded = fitted_fill(rule_four_score.wrecked, column)
        if recorded is None or column not in rule_four_score.wrecked_frame.columns:
            continue
        observed = pd.to_numeric(rule_four_score.wrecked_frame[column], errors="coerce").dropna()
        if observed.empty:
            continue
        assert recorded != pytest.approx(float(observed.median())), column
        compared += 1
    assert compared >= 2, "too few numeric columns to make this test mean anything"


# ---------------------------------------------------------------------------
# Rule 3, the training-time promotion path
# ---------------------------------------------------------------------------
def plant_stored_score(registry: LocalModelRegistry, model_id: str, score: float) -> None:
    """Overwrite a registry row's stored `test_score` with a number from nowhere.

    Written straight into SQLite, because no engine API offers to do this: the whole point of design
    rule 3 is that this number is never read when a championship is decided, so an absurd one must
    change nothing about the outcome.
    """
    engine = create_engine(f"sqlite:///{registry.db_path}")
    with Session(engine) as session:
        row = session.exec(select(ModelVersionRow).where(ModelVersionRow.model_id == model_id)).one()
        row.test_score = score
        session.add(row)
        session.commit()
    engine.dispose()
    assert registry.get(model_id).test_score == pytest.approx(score)


@dataclass(frozen=True)
class Challenge:
    """One world's outcome: the planted champion score, and what the challenger run decided."""

    world: World
    champion_id: str
    planted: float
    challenger: ModelVersion
    champion_rescore: float | None


@dataclass(frozen=True)
class RuleThree:
    """The same challenger trained in two worlds that differ only in a number nobody may read.

    `pending` and `stale` carry the approval half: `pending` is the register stage's own output for
    the first model of the use case - `pending_approval`, stamped with the champion its decision was
    taken against - and `stale` is a copy of the world taken while that model was still waiting, so
    a different champion can take the title underneath it without disturbing anything else.
    """

    high: Challenge
    low: Challenge
    pending: ModelVersion
    approved_unstale: ModelVersion
    stale: World


def _challenge(
    world: World, champion_id: str, planted: float, frame: pd.DataFrame, run_id: str, label: str
) -> Challenge:
    plant_stored_score(world.registry, champion_id, planted)
    model_id, _ = run_flow(world, frame, name=f"challenger_{label}", run_id=run_id, mode=RunMode.TRAIN)
    manifest = world.storage.read_text(run_key(run_id, MANIFEST_FILENAME))
    metric = world.config.model_search.metric.value
    rescore = _metric_from_manifest(manifest, f"champion_{metric}")
    return Challenge(
        world=world,
        champion_id=champion_id,
        planted=planted,
        challenger=world.registry.get(model_id),
        champion_rescore=rescore,
    )


def _metric_from_manifest(payload: str, name: str) -> float | None:
    import json

    metrics = json.loads(payload).get("metrics") or {}
    value = metrics.get(name)
    return None if value is None else float(value)


@pytest.fixture(scope="module")
def rule_three(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> RuleThree:
    root = tmp_path_factory.mktemp("rule-three")
    first = build_world(root / "first", config_root)
    champion_frame = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant="clean", seed=20260921))
    champion_id, _ = run_flow(first, champion_frame, name="champion", run_id=new_run_id(), mode=RunMode.TRAIN)

    # The register stage's own output for the first model of a use case: `approval_required` is on by
    # default, so it waits, stamped with the champion its decision was taken against - the sentinel,
    # because there was none. A copy of the world is taken here, while it is still waiting, so the
    # approval path can be walked both ways from the same real row.
    pending = first.registry.get(champion_id)
    assert pending.status is ModelStatus.PENDING_APPROVAL, pending.status
    stale = clone_world(first, root / "stale", config_root)

    approved = first.registry.approve(champion_id, by="tests")
    assert first.registry.get_champion(USE_CASE) is not None

    # Two worlds with the same past, so the only difference between the runs below is the number
    # planted on the champion's row.
    second = clone_world(first, root / "second", config_root)
    challenger_frame = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant="clean", seed=20270214))
    run_id = new_run_id()
    return RuleThree(
        high=_challenge(first, champion_id, 0.999999, challenger_frame, run_id, "high"),
        low=_challenge(second, champion_id, 0.000001, challenger_frame, run_id, "low"),
        pending=pending,
        approved_unstale=approved,
        stale=stale,
    )


def test_the_planted_scores_would_have_decided_it_between_them(rule_three: RuleThree) -> None:
    """The guard against a vacuous pass: the two numbers are on opposite sides of any challenger."""
    assert rule_three.high.planted > 0.99
    assert rule_three.low.planted < 0.01
    assert rule_three.high.world.config.evaluation.champion_min_improvement_pct > 0.0
    for challenge in (rule_three.high, rule_three.low):
        assert challenge.world.registry.get(challenge.champion_id).test_score == pytest.approx(
            challenge.planted
        )


def test_the_champion_was_re_scored_on_the_challengers_own_hold_out(rule_three: RuleThree) -> None:
    """Rule 3's positive half: the number the decision used was measured, not looked up."""
    for challenge in (rule_three.high, rule_three.low):
        assert challenge.champion_rescore is not None, "the champion was never re-scored"
        assert challenge.champion_rescore != pytest.approx(challenge.planted)
        assert 0.0 <= challenge.champion_rescore <= 1.0


def test_an_absurd_stored_score_changes_nothing_about_the_decision(rule_three: RuleThree) -> None:
    """Rule 3: a stored score from an older test split is not an input to the decision."""
    high, low = rule_three.high.challenger, rule_three.low.challenger
    assert high.status is low.status
    assert high.test_score == pytest.approx(low.test_score)
    assert high.improvement_pct == low.improvement_pct
    assert high.previous_champion_id == low.previous_champion_id
    assert high.measured_against_champion_id == low.measured_against_champion_id
    assert rule_three.high.champion_rescore == pytest.approx(rule_three.low.champion_rescore)


def test_the_decision_follows_the_re_scored_number_and_not_the_planted_one(
    rule_three: RuleThree,
) -> None:
    """The sharp form: two rules, one observed outcome, and the outcome is the re-scored rule's.

    The champion rule promotes when the challenger beats the champion by at least
    `champion_min_improvement_pct` of the champion's score. Applied to the number the register stage
    measured on this run's own hold-out it gives one answer; applied to the number sitting in the
    registry row it gives another - and in at least one of these two worlds the two answers differ.
    The outcome that actually happened matches the first, in both worlds.
    """
    minimum = rule_three.high.world.config.evaluation.champion_min_improvement_pct
    disagreed = False
    for challenge in (rule_three.high, rule_three.low):
        version, rescore = challenge.challenger, challenge.champion_rescore
        assert rescore is not None
        promoted = version.status in {ModelStatus.PENDING_APPROVAL, ModelStatus.CHAMPION}
        by_measurement = (version.test_score - rescore) / abs(rescore) * 100.0 >= minimum
        by_stored = (version.test_score - challenge.planted) / abs(challenge.planted) * 100.0 >= minimum
        assert promoted is by_measurement, (
            f"planted={challenge.planted} rescore={rescore} challenger={version.test_score} "
            f"status={version.status}"
        )
        disagreed = disagreed or by_stored is not by_measurement
    assert disagreed, "neither planted score would have changed the answer, so this proves nothing"


def test_the_decision_names_the_champion_it_was_measured_against(rule_three: RuleThree) -> None:
    """Finding 15: a row says which champion the head-to-head was against, or says nothing at all."""
    for challenge in (rule_three.high, rule_three.low):
        version = challenge.challenger
        if version.status is ModelStatus.PENDING_APPROVAL:
            assert version.measured_against_champion_id == challenge.champion_id
            assert version.improvement_pct is not None
        elif version.status is ModelStatus.CHAMPION:
            assert version.previous_champion_id == challenge.champion_id
            assert version.improvement_pct is not None
        else:
            # A version the rule did not promote claims nothing: no percentage, and no champion to
            # read it against. That is what stops a later approval from acting on a stale number.
            assert version.status is ModelStatus.CANDIDATE
            assert version.improvement_pct is None
            assert version.measured_against_champion_id is None
            assert version.previous_champion_id is None


# ---------------------------------------------------------------------------
# Rule 3, the approval path
# ---------------------------------------------------------------------------
def test_the_register_stage_stamps_the_champion_the_decision_was_taken_against(
    rule_three: RuleThree,
) -> None:
    """Finding 15, on real register-stage output: a waiting row says what it was measured against."""
    pending = rule_three.pending
    assert pending.status is ModelStatus.PENDING_APPROVAL
    assert pending.measured_against_champion_id == NO_CHAMPION_AT_DECISION, "there was no champion yet"


def test_approval_refuses_a_decision_measured_against_a_champion_that_has_moved(
    rule_three: RuleThree,
) -> None:
    """Rule 3 arriving through approval: the stale-approval sequence of M4_FIXES finding 4.

    The waiting row is the register stage's own: it was put forward when the use case had no
    champion. A different model then takes the title, so approving this one would crown it over a
    model it was never compared with - which is precisely what design rule 3 forbids.
    """
    world = rule_three.stale
    pending = rule_three.pending
    assert world.registry.get(pending.model_id).status is ModelStatus.PENDING_APPROVAL
    assert world.registry.get_champion(USE_CASE) is None, "nothing held the title when it was decided"

    usurper = world.registry.register(
        pending.model_copy(
            update={
                "model_id": f"{pending.model_id}_usurper",
                "version": world.registry.next_version(USE_CASE),
                "status": ModelStatus.CANDIDATE,
                "measured_against_champion_id": None,
                "previous_champion_id": None,
                "improvement_pct": None,
            }
        )
    )
    world.registry.promote(usurper.model_id, by="tests", note="a different champion takes the title")
    champion = world.registry.get_champion(USE_CASE)
    assert champion is not None and champion.model_id == usurper.model_id

    with pytest.raises(RegistryError) as raised:
        world.registry.approve(pending.model_id, by="tests")
    assert raised.value.code == "CHAMPION_CHANGED"
    assert usurper.model_id in str(raised.value), str(raised.value)

    # Refused means refused: neither row moved, and the incumbent keeps the title.
    assert world.registry.get(pending.model_id).status is ModelStatus.PENDING_APPROVAL
    assert world.registry.get(pending.model_id).approved_at is None
    still = world.registry.get_champion(USE_CASE)
    assert still is not None and still.model_id == usurper.model_id


def test_approval_still_works_when_the_champion_has_not_moved(rule_three: RuleThree) -> None:
    """The guard against a vacuous pass: the check refuses staleness, not approval.

    Same row, same registry code, one difference - nothing took the title underneath it. The
    fixture approved it and it became champion, which is what the whole rule-three world is built on.
    """
    approved = rule_three.approved_unstale
    assert approved.model_id == rule_three.pending.model_id
    assert approved.status is ModelStatus.CHAMPION
    assert approved.approved_by == "tests"
    assert approved.approved_at is not None
    for challenge in (rule_three.high, rule_three.low):
        champion = challenge.world.registry.get_champion(USE_CASE)
        assert champion is not None and champion.model_id == approved.model_id
