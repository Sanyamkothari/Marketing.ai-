"""Tests for the fabricated-run fixture itself (plan §10).

`make_run.py` is what keeps the Phase 3a tests out of the slow suite: every generative flow reads a
finished predictive run, and this is the only place one is produced without AutoGluon. A fixture in
that position earns the same treatment as engine code, because everything it gets wrong shows up
later as a generative bug that is not one.

Four properties matter, and each is proved rather than assumed:

**Every file validates through the contract registered for its name.** Not through a copy of the
contract written here - through `engine.contracts.load_artefact` and
`engine.stages.explain.read_row_explanations`, the same readers a flow uses. A field added to a
model, or renamed, has to break here.

**The run reads as finished.** `state` is `done`, the stage list is the mode's own list and every
stage in it is `done`, and `run.json` names every file the directory actually holds. A flow that
refuses to read an unfinished run must find nothing to refuse.

**The semantics a generative flow depends on are the engine's.** A suppressed row keeps its score
and its band and loses only its action; a control row is suppressed by nothing and carries the
control action; the bands are the configured ones, in the configured order, and none of them is
empty. Those are the three things win-back copy must respect, and a fixture that got them wrong
would let a flow that ignores them pass.

**A reason points the way its contribution does.** The root-cause evidence pack aggregates signed
contributions, so a file where every reason pointed the same way, or where the direction and the
sign disagreed, would make the pack's arithmetic meaningless and prove nothing about it.

Every sweep is computed from `configs/`, never listed by hand, and the narrowed ones - the use
cases that configure a complaint column, the ones that configure a suppression rule - are guarded
by `test_a_narrowed_sweep_still_covers_something`, because a sweep that shrank to nothing would
look exactly like a passing suite.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from engine.config import RunMode, UseCaseConfig, load_use_case
from engine.contracts import (
    Direction,
    DriftReport,
    FeatureImportance,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    ScoringSummary,
    load_artefact,
    scores_csv_columns,
)
from engine.pipeline import stages_for
from engine.stages.actions import (
    ACTION_COLUMN,
    BAND_COLUMN,
    CONTROL_ACTION,
    CONTROL_GROUP_COLUMN,
    SUPPRESSED_ACTION,
    SUPPRESSED_REASON_COLUMN,
    suppression_rules,
)
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.storage import LocalStorage, run_key
from tests.fixtures.make_data import predictive_use_case_ids
from tests.fixtures.make_run import (
    MODEL_DISPLAY_NAME,
    RunSpec,
    complaint_column,
    feature_columns,
    primary_key_of,
    source_key,
    target_of,
    write_run,
)

if TYPE_CHECKING:
    from pathlib import Path

USE_CASE_IDS: tuple[str, ...] = predictive_use_case_ids()
"""Every use case the fixture can fabricate a run for, computed from the configs."""

TEXT_USE_CASE_IDS: tuple[str, ...] = tuple(
    use_case_id for use_case_id in USE_CASE_IDS if complaint_column(load_use_case(use_case_id)) is not None
)
"""Those that configure a complaint column, which is the only place `with_text` has to write."""

SUPPRESSING_USE_CASE_IDS: tuple[str, ...] = tuple(
    use_case_id for use_case_id in USE_CASE_IDS if suppression_rules(load_use_case(use_case_id))
)
"""Those that configure at least one suppression rule; the public Telco file configures none."""

SMALL_ROWS: int = 200
"""Rows for the checks that are about bytes rather than about counts, so they stay cheap."""

PLANTED_EMAIL_DOMAIN: str = "@example.invalid"
"""One of `make_docs`' planted identifiers; its presence is what `with_text` exists to provide."""


@dataclass(frozen=True)
class Run:
    """One fabricated run and the handles to read it back the way a flow would."""

    spec: RunSpec
    run_id: str
    storage: LocalStorage
    config: UseCaseConfig

    def key(self, name: str) -> str:
        return run_key(self.run_id, name)

    def names(self) -> tuple[str, ...]:
        """Every file in the run directory, by filename, in storage order."""
        prefix = f"runs/{self.run_id}/"
        return tuple(key.removeprefix(prefix) for key in self.storage.list_keys(prefix))

    def artefact(self, name: str) -> object:
        """One artefact, validated through the contract registered for its filename."""
        return load_artefact(name, self.storage.read_text(self.key(name)))

    def record(self) -> RunRecord:
        return self.storage.read_model(self.key("run.json"), RunRecord)

    def status(self) -> RunStatus:
        return self.storage.read_model(self.key("status.json"), RunStatus)

    def summary(self) -> ScoringSummary:
        return self.storage.read_model(self.key("scoring_summary.json"), ScoringSummary)

    def scores(self) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(self.storage.read_bytes(self.key("scores.csv"))))

    def uploaded(self) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(self.storage.read_bytes(source_key(self.record()))))


def build(storage: LocalStorage, spec: RunSpec) -> Run:
    """Fabricate one run and wrap it, so a test reads it by name rather than by key."""
    return Run(spec, write_run(storage, spec), storage, load_use_case(spec.use_case_id, spec.config_root))


def digests(run: Run) -> dict[str, str]:
    """Every file of a run directory by content digest: what byte-identical means here."""
    prefix = f"runs/{run.run_id}/"
    return {
        key.removeprefix(prefix): hashlib.sha256(run.storage.read_bytes(key)).hexdigest()
        for key in run.storage.list_keys(prefix)
    }


@pytest.fixture(scope="module")
def score_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Run]:
    """One scoring run per use case, built once: fabricating them per test proves nothing extra."""
    storage = LocalStorage(tmp_path_factory.mktemp("make_run_score"))
    return {use_case_id: build(storage, RunSpec(use_case_id)) for use_case_id in USE_CASE_IDS}


@pytest.fixture(scope="module")
def train_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Run]:
    """One training run per use case; a training run carries the target and no score files."""
    storage = LocalStorage(tmp_path_factory.mktemp("make_run_train"))
    return {
        use_case_id: build(storage, RunSpec(use_case_id, mode=RunMode.TRAIN)) for use_case_id in USE_CASE_IDS
    }


# ---------------------------------------------------------------------------
# The sweeps themselves
# ---------------------------------------------------------------------------
def test_a_narrowed_sweep_still_covers_something() -> None:
    """A sweep that shrank to nothing would look exactly like a passing suite."""
    assert len(USE_CASE_IDS) >= 1, "no predictive use case to fabricate a run for"
    assert len(TEXT_USE_CASE_IDS) >= 1, "no use case configures a complaint column"
    assert len(SUPPRESSING_USE_CASE_IDS) >= 1, "no use case configures a suppression rule"


# ---------------------------------------------------------------------------
# Every artefact validates through its own contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_json_artefact_validates_through_the_model_registered_for_its_name(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    run = score_runs[use_case_id]
    written = [name for name in run.names() if name.endswith(".json")]
    assert written, "the run directory holds no JSON artefact at all"
    for name in written:
        assert run.artefact(name) is not None, f"{name} did not validate through its contract"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_run_carries_every_document_a_generative_flow_reads(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """The list the flows read from, spelled out once: a missing one is a flow that cannot start."""
    expected = {
        "run.json",
        "status.json",
        "run_config.json",
        "profile.json",
        "validation.json",
        "prepare.json",
        "run_manifest.json",
        "scores.csv",
        ROW_EXPLANATIONS_FILENAME,
        "scoring_summary.json",
        "feature_importance.json",
        "drift.json",
    }
    assert expected <= set(score_runs[use_case_id].names())


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_row_of_the_explanations_parquet_validates_through_its_row_contract(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """`read_row_explanations` validates each row, so reading it back is the check."""
    run = score_runs[use_case_id]
    explanations = read_row_explanations(run.key(ROW_EXPLANATIONS_FILENAME), storage=run.storage)
    assert len(explanations) == len(run.scores().index), "one explanation per scored row, or none"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_score_files_carry_the_header_the_contract_computes(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    run = score_runs[use_case_id]
    expected = scores_csv_columns(run.config, primary_key_of(run.config))
    assert tuple(run.scores().columns) == expected
    parquet = pd.read_parquet(io.BytesIO(run.storage.read_bytes(run.key("scores.parquet"))))
    assert tuple(parquet.columns) == expected


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_run_record_names_every_file_the_run_directory_holds(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """Readers navigate by `RunRecord.artefacts`; a file it does not name might as well be absent."""
    run = score_runs[use_case_id]
    record = run.record()
    assert set(record.artefacts) == set(run.names())
    for name, key in record.artefacts.items():
        assert run.storage.exists(key), f"{name} is listed at {key}, which holds nothing"


# ---------------------------------------------------------------------------
# The run reads as finished
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_fabricated_run_reads_as_done_with_every_stage_done(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    run = score_runs[use_case_id]
    record, status = run.record(), run.status()
    assert record.state is RunState.DONE, record.error
    assert record.error is None
    assert status.state is RunState.DONE
    assert tuple(stage.key for stage in status.stages) == stages_for(record.mode)
    assert {stage.state for stage in status.stages} == {RunState.DONE}
    assert status.progress_pct == 100
    assert status.current_stage is None


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_nothing_the_fixture_did_not_measure_is_written_down(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """No model was fitted and no stage ran, so no quality and no duration may be claimed."""
    run = score_runs[use_case_id]
    record = run.record()
    assert record.headline_score is None
    assert record.headline_metric is None
    assert record.best_model == MODEL_DISPLAY_NAME
    manifest = run.storage.read_model(run.key("run_manifest.json"), RunManifest)
    assert manifest.cost_estimate.estimated_usd is None
    assert manifest.duration_s == 0.0
    assert manifest.recipe is None
    assert manifest.leaderboard_path is None
    assert all(stage.duration_seconds is None for stage in run.status().stages)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_rows_the_run_consumed_are_stored_where_the_record_says(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """A root-cause flow reads the complaint column from the upload, by way of the run record."""
    run = score_runs[use_case_id]
    uploaded = run.uploaded()
    assert len(uploaded.index) == run.record().row_count
    assert primary_key_of(run.config) in uploaded.columns


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_same_spec_writes_the_same_run_id_and_the_same_bytes(tmp_path: Path, use_case_id: str) -> None:
    """Two fabrications of one spec must be indistinguishable, or a test cannot pin a value."""
    spec = RunSpec(use_case_id, rows=SMALL_ROWS)
    first = build(LocalStorage(tmp_path / "first"), spec)
    second = build(LocalStorage(tmp_path / "second"), spec)
    assert first.run_id == second.run_id
    assert digests(first) == digests(second)


def test_two_different_specs_do_not_share_a_run_id(tmp_path: Path) -> None:
    """A fabricated id is derived from the whole spec, so two runs cannot overwrite each other."""
    storage = LocalStorage(tmp_path)
    use_case_id = USE_CASE_IDS[0]
    ids = {
        build(storage, RunSpec(use_case_id, rows=SMALL_ROWS)).run_id,
        build(storage, RunSpec(use_case_id, rows=SMALL_ROWS, seed=1)).run_id,
        build(storage, RunSpec(use_case_id, rows=SMALL_ROWS, with_reasons=False)).run_id,
        build(storage, RunSpec(use_case_id, rows=SMALL_ROWS, mode=RunMode.TRAIN)).run_id,
    }
    assert len(ids) == 4, f"two specs shared a run id: {sorted(ids)}"


# ---------------------------------------------------------------------------
# Bands, suppression and the control group
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_bands_are_the_configured_ones_in_order_and_none_is_empty(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """A run whose scores all landed in one band would not exercise a band-driven flow at all."""
    run = score_runs[use_case_id]
    summary = run.summary()
    configured = run.config.actions.bands
    assert tuple(band.name for band in summary.bands) == tuple(band.name for band in configured)
    assert tuple(band.min_score for band in summary.bands) == tuple(band.min_score for band in configured)
    empty = [band.name for band in summary.bands if band.rows == 0]
    assert not empty, f"{use_case_id} produced no rows in {empty}"
    assert sum(band.rows for band in summary.bands) == summary.rows_scored


@pytest.mark.parametrize("use_case_id", SUPPRESSING_USE_CASE_IDS)
def test_a_suppressed_row_keeps_its_band_and_loses_only_its_action(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """Win-back copy must filter on the suppression reason, so the band must survive suppression."""
    run = score_runs[use_case_id]
    scores = run.scores()
    suppressed = scores[scores[SUPPRESSED_REASON_COLUMN].notna()]
    assert len(suppressed.index) > 0, "no row was suppressed, so the rule cannot be tested against"
    assert set(suppressed[ACTION_COLUMN]) == {SUPPRESSED_ACTION}
    assert set(suppressed[BAND_COLUMN]) <= {band.name for band in run.config.actions.bands}
    counted = sum(entry.rows for entry in run.summary().suppressed)
    assert counted == len(suppressed.index), "the summary counts a different number of suppressed rows"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_control_row_is_suppressed_by_nothing_and_carries_the_control_action(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """The holdout is drawn from eligible rows only: a row nobody may contact is not a withheld one."""
    run = score_runs[use_case_id]
    scores = run.scores()
    control = scores[scores[CONTROL_GROUP_COLUMN].astype(bool)]
    assert len(control.index) > 0, "no row was held out, so the control group cannot be tested against"
    assert set(control[ACTION_COLUMN]) == {CONTROL_ACTION}
    assert control[SUPPRESSED_REASON_COLUMN].isna().all()
    assert len(control.index) == run.summary().control_group_rows


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_summary_is_quoted_from_the_drift_report_rather_than_recomputed(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """Two computations of one number disagree in the last digit; the Output page quotes one."""
    run = score_runs[use_case_id]
    drift = run.storage.read_model(run.key("drift.json"), DriftReport)
    summary = run.summary()
    assert summary.drift_max_psi == drift.max_psi
    assert summary.drift_status is drift.status
    assert summary.drift_summary == drift.summary
    assert drift.threshold == run.config.monitoring.drift_psi_threshold
    assert {feature.feature for feature in drift.features} == set(feature_columns(run.config))


# ---------------------------------------------------------------------------
# Reasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_every_reason_points_the_way_its_contribution_does_and_both_directions_occur(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """The evidence pack aggregates signed contributions; one-directional reasons would prove nothing."""
    run = score_runs[use_case_id]
    explanations = read_row_explanations(run.key(ROW_EXPLANATIONS_FILENAME), storage=run.storage)
    reasons = [reason for explanation in explanations for reason in explanation.reasons]
    assert reasons, "no reason was written at all"
    for reason in reasons:
        expected = Direction.UP if reason.contribution > 0.0 else Direction.DOWN
        assert reason.direction is expected, f"{reason.feature} points {reason.direction} at {reason}"
    assert {reason.direction for reason in reasons} == {Direction.UP, Direction.DOWN}


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_reasons_of_a_row_are_ordered_by_the_size_of_their_contribution(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """`Reason` lists arrive pre-sorted, strongest first; the UI renders and never computes."""
    run = score_runs[use_case_id]
    limit = run.config.evaluation.reasons_per_row
    for explanation in read_row_explanations(run.key(ROW_EXPLANATIONS_FILENAME), storage=run.storage):
        sizes = [abs(reason.contribution) for reason in explanation.reasons]
        assert sizes == sorted(sizes, reverse=True), f"{explanation.primary_key} is out of order"
        assert 1 <= len(sizes) <= limit, f"{explanation.primary_key} has {len(sizes)} reasons"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_first_reason_of_every_row_is_the_sentence_the_score_file_prints(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """`scores.csv` holds the text and the parquet holds the object; they must be the same reason."""
    run = score_runs[use_case_id]
    explanations = read_row_explanations(run.key(ROW_EXPLANATIONS_FILENAME), storage=run.storage)
    strongest = {item.primary_key: item.reasons[0].text for item in explanations}
    scores = run.scores().astype({primary_key_of(run.config): "string"})
    assert scores["reason_1"].notna().all(), "a scored row was exported with no reason at all"
    for key, text in zip(scores[primary_key_of(run.config)], scores["reason_1"], strict=True):
        assert text == strongest[str(key)], f"{key} prints a reason its explanation does not carry"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_the_importance_chart_ranks_the_columns_the_reasons_are_about(
    score_runs: dict[str, Run], use_case_id: str
) -> None:
    """`share_pct` is normalised over the returned rows, and the caption names the method that ran."""
    run = score_runs[use_case_id]
    importance = run.storage.read_model(run.key("feature_importance.json"), FeatureImportance)
    assert {item.feature for item in importance.items} <= set(feature_columns(run.config))
    assert [item.rank for item in importance.items] == list(range(1, len(importance.items) + 1))
    assert round(sum(item.share_pct for item in importance.items), 1) == 100.0
    assert importance.caption


# ---------------------------------------------------------------------------
# The switches on the spec
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_run_without_reasons_writes_no_parquet_and_invents_no_importance(
    tmp_path: Path, use_case_id: str
) -> None:
    """`evaluation.shap: false` is a user's choice; what it must never produce is a ranking of zeros."""
    run = build(LocalStorage(tmp_path), RunSpec(use_case_id, rows=SMALL_ROWS, with_reasons=False))
    assert ROW_EXPLANATIONS_FILENAME not in run.names()
    importance = run.storage.read_model(run.key("feature_importance.json"), FeatureImportance)
    assert importance.items == ()
    assert run.scores()["reason_1"].isna().all()


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_a_training_run_carries_its_target_and_writes_no_score_files(
    train_runs: dict[str, Run], use_case_id: str
) -> None:
    """A training run scored nothing, so it has no bands to show and no rows to act on."""
    run = train_runs[use_case_id]
    record = run.record()
    assert record.mode is RunMode.TRAIN
    assert record.state is RunState.DONE
    assert record.target == target_of(run.config)
    assert record.target in run.uploaded().columns
    assert "scores.csv" not in run.names()
    assert "scoring_summary.json" not in run.names()
    assert ROW_EXPLANATIONS_FILENAME in run.names()


@pytest.mark.parametrize("use_case_id", TEXT_USE_CASE_IDS)
def test_the_complaint_column_carries_prose_with_something_to_redact(
    tmp_path: Path, use_case_id: str
) -> None:
    """A redaction test needs an identifier inside a sentence, which the template's phrases have not."""
    run = build(LocalStorage(tmp_path), RunSpec(use_case_id, with_text=True))
    column = complaint_column(run.config)
    assert column is not None
    text = run.uploaded()[column].astype("string")
    assert text.notna().all(), "a complaint column was left with empty cells"
    assert text.str.contains(PLANTED_EMAIL_DOMAIN, regex=False).any(), "nothing to redact was planted"


@pytest.mark.parametrize(
    "use_case_id", [use_case_id for use_case_id in USE_CASE_IDS if use_case_id not in TEXT_USE_CASE_IDS]
)
def test_asking_for_complaint_text_where_none_is_configured_is_refused(
    tmp_path: Path, use_case_id: str
) -> None:
    """Writing the prose into a column the use case never named would be a silent no-op."""
    with pytest.raises(ValueError, match="complaint_text_column"):
        write_run(LocalStorage(tmp_path), RunSpec(use_case_id, rows=SMALL_ROWS, with_text=True))


@pytest.mark.parametrize(("rows", "positive_rate"), [(0, 0.12), (10, 0.0), (10, 1.0), (-1, 0.12), (10, 1.5)])
def test_a_spec_that_could_not_describe_a_run_is_refused_when_it_is_made(
    rows: int, positive_rate: float
) -> None:
    """The spec is checked where it is written, not three artefacts into writing the run."""
    with pytest.raises(ValueError):
        RunSpec(USE_CASE_IDS[0], rows=rows, positive_rate=positive_rate)


def test_a_naive_timestamp_is_refused_because_every_engine_timestamp_is_utc() -> None:
    """A naive datetime reaching an artefact is rejected by the contract; it is rejected earlier here."""
    with pytest.raises(ValueError, match="timezone-aware"):
        RunSpec(USE_CASE_IDS[0], created_at=datetime(2026, 8, 16, 9, 0))
