"""Plan O M234 acceptance: the nine gaps of [RDY] §1.7 (DEC-1804 (b)).

For each gap a fixture passes `check_plan` - exactly what `POST /runs` validates - and then fails the
run, or silently loses rows, at a later step; the readiness report on it is `not_ready` and names the
probe. "Fails the run" is shown with the engine's own code: the prepare stage, the split and the reader
are called directly; the evaluate stage is called on the real test part with a canned scorer (gap 6:
`EVAL_PRIMARY_METRIC_UNDEFINED`); and a real training run on a temporary store fails at train (gap 7:
`TRAIN_NO_MODEL_FITTED`, marked slow because it starts AutoGluon).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import Metric, ProblemType, RunMode, SplitType, ThresholdMode, UseCaseConfig
from engine.contracts import CalibrationSummary, RunState
from engine.doctor.readiness_contracts import DataReadinessReport
from engine.doctor.readiness_probes import scan_source
from engine.stages import ingest, prepare
from engine.stages.evaluate import EvaluationError, evaluate
from engine.storage import LocalStorage
from tests.unit.doctor.readiness_fixtures import assets, config, customers, dry_run, report


def _not_ready_naming(result: DataReadinessReport, code: str) -> None:
    assert result.verdict == "not_ready", result.verdict_reason
    assert result.probe(code).status == "fail", result.probe(code)
    assert code in result.verdict_reason
    assert result.probe(code).fix, "a failing probe says what to do"


def _rows(frame: pd.DataFrame, cfg: UseCaseConfig, key: str, target: str) -> pd.DataFrame:
    rows, _plan = prepare.prepare_rows(frame, cfg, primary_key=(key,), target=target)
    return rows


# 1 -------------------------------------------------------------------------------------------------
def test_gap_1_a_partly_empty_date_column_passes_the_checks_and_stops_the_split() -> None:
    cfg = config("fault-prediction")
    frame = assets()
    frame.loc[frame.sample(frac=0.03, random_state=1).index, "snapshot_date"] = None
    assert dry_run(frame, cfg).passed  # TIME_COLUMN_UNPARSEABLE ignores empty cells
    with pytest.raises(ValueError, match="needs a date in every row"):
        prepare.split_dataset(
            _rows(frame, cfg, "asset_id", "fault_next_72h"), cfg, run_id="r1", target="fault_next_72h"
        )
    result = report(frame, cfg)
    _not_ready_naming(result, "X_TIME_ALL_PARSE")
    assert result.probe("X_TIME_ALL_PARSE").measured == 72.0


# 2 -------------------------------------------------------------------------------------------------
def test_gap_2_tie_blocks_at_the_date_cut_empty_a_part() -> None:
    cfg = config("fault-prediction")
    frame = assets()
    rng = np.random.default_rng(2)
    frame["snapshot_date"] = np.where(rng.random(len(frame)) < 0.9, "2026-08-01", "2026-08-02")
    assert dry_run(frame, cfg).passed
    with pytest.raises(ValueError, match="too many rows share the same value"):
        prepare.split_dataset(
            _rows(frame, cfg, "asset_id", "fault_next_72h"), cfg, run_id="r1", target="fault_next_72h"
        )
    result = report(frame, cfg)
    _not_ready_naming(result, "X_TIME_CUTS_FEASIBLE")
    assert result.probe("X_TIME_CUTS_FEASIBLE").details["distinct_dates"] == 2


# 3 -------------------------------------------------------------------------------------------------
def test_gap_3_too_few_groups_for_a_grouped_split() -> None:
    cfg = config("targeted-advertisement", split={"group_column": "region"})
    frame = customers()
    frame["region"] = np.where(np.arange(len(frame)) % 2 == 0, "north", "south")
    assert dry_run(frame, cfg).passed
    with pytest.raises(ValueError, match="at least 3 distinct values"):
        prepare.split_dataset(
            _rows(frame, cfg, "customer_id", "converted_30d"), cfg, run_id="r1", target="converted_30d"
        )
    result = report(frame, cfg)
    _not_ready_naming(result, "X_GROUPS_ENOUGH")
    assert result.probe("X_GROUPS_ENOUGH").measured == 2.0
    assert result.probe("X_SPLIT_CLASSES").status == "skipped"  # the split itself is the problem


# 4 -------------------------------------------------------------------------------------------------
def test_gap_4_consent_spelled_granted_denied_removes_every_row_at_training() -> None:
    cfg = config("targeted-advertisement", governance={"consent_column": "consent"})
    frame = customers()
    frame["consent"] = np.where(np.arange(len(frame)) % 5 == 0, "Denied", "Granted")
    assert dry_run(frame, cfg).passed  # CONSENT_COLUMN_MISSING checks only that the column is there
    assert _rows(frame, cfg, "customer_id", "converted_30d").empty
    result = report(frame, cfg)
    _not_ready_naming(result, "X_CONSENT_VOCAB")
    probe = result.probe("X_CONSENT_VOCAB")
    assert probe.column == "consent" and probe.details["spellings_not_understood"] == 2
    assert "Granted" not in result.model_dump_json(exclude={"dry_run_report"}), "no cell value in the report"
    assert result.post_filter_counts is not None and result.post_filter_counts.rows_after_consent == 0


# 5 -------------------------------------------------------------------------------------------------
def test_gap_5_malformed_csv_lines_are_dropped_without_a_word(tmp_path: Path) -> None:
    cfg = config("targeted-advertisement")
    frame = customers()
    text = io.StringIO()
    frame.to_csv(text, index=False)
    lines = text.getvalue().splitlines()
    for number in (10, 500, 1500):  # a comma inside an unquoted value: one field too many
        lines[number] = lines[number].replace(",", ",Mumbai, Andheri,", 1)
    storage = LocalStorage(tmp_path)
    storage.write_bytes("uploads/u1/source.csv", ("\n".join(lines) + "\n").encode())
    read = ingest.read_upload(storage, "uploads/u1/source.csv")
    assert read.row_count == len(frame) - 3, "the reader dropped them"
    assert dry_run(read.frame, cfg, row_count=read.row_count).passed
    scan = scan_source(
        storage,
        "uploads/u1/source.csv",
        file_format="csv",
        rows_parsed=read.row_count,
        encoding=read.encoding,
        delimiter=read.delimiter,
        key_columns=("customer_id",),
        full_keys=False,
    )
    result = report(read.frame, cfg, row_count=read.row_count, scan=scan)
    _not_ready_naming(result, "X_PARSER_ROW_LOSS")
    probe = result.probe("X_PARSER_ROW_LOSS")
    assert probe.measured == 3.0
    assert probe.details["lines"] == "11, 501, 1501"


# 6 -------------------------------------------------------------------------------------------------
def test_gap_6_immature_newest_outcomes_leave_a_one_class_test_part() -> None:
    cfg = config("fault-prediction")
    frame = assets(days=120)
    newest = pd.to_datetime(frame["snapshot_date"]) >= pd.Timestamp("2026-04-01") + pd.Timedelta(days=100)
    frame.loc[newest, "fault_next_72h"] = 0  # the outcome window has not ended for the newest dates
    assert dry_run(frame, cfg).passed
    parts, _ = prepare.split_dataset(
        _rows(frame, cfg, "asset_id", "fault_next_72h"), cfg, run_id="r1", target="fault_next_72h"
    )
    test = parts["test"]
    assert test["fault_next_72h"].nunique() == 1
    scorer = _CannedScorer(
        target_column="fault_next_72h",
        primary_metric=cfg.model_search.metric,
        scores=np.linspace(0.05, 0.95, len(test)),
    )
    with pytest.raises(EvaluationError) as stopped:  # the evaluate stage itself, on the real test part
        evaluate(scorer, test, cfg.evaluation, run_id="r1")
    assert stopped.value.code == "EVAL_PRIMARY_METRIC_UNDEFINED"
    result = report(frame, cfg)
    _not_ready_naming(result, "X_LABEL_MATURITY")
    assert result.probe("X_SPLIT_CLASSES").status == "fail"
    assert result.post_filter_counts is not None
    assert result.post_filter_counts.test_primary_metric_defined is False


@dataclass
class _CannedScorer:
    """A fitted model's scorer, serving fixed scores: evaluate measures it and fits nothing."""

    target_column: str
    primary_metric: Metric
    scores: Any
    threshold: float = 0.5
    threshold_mode: ThresholdMode = ThresholdMode.FIXED
    threshold_detail: str = "Fixed: 0.50"
    calibration: CalibrationSummary | None = None
    classes: tuple[object, object] | None = (0, 1)
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION

    def predict_proba(self, frame: pd.DataFrame) -> Any:
        return np.asarray(self.scores, dtype=float)


# 7 -------------------------------------------------------------------------------------------------
def test_gap_7_a_text_amount_outcome_passes_the_checks_and_cannot_be_trained() -> None:
    cfg = config("targeted-advertisement", top={"problem_type": ProblemType.REGRESSION})
    frame = customers()
    frame["converted_30d"] = np.random.default_rng(7).choice(["low", "medium", "high"], len(frame))
    assert dry_run(frame, cfg).passed  # nothing checks that an amount is a number
    with pytest.raises(ValueError):
        pd.to_numeric(frame["converted_30d"])  # what a regression learner needs (TRAIN_NO_MODEL_FITTED)
    result = report(frame, cfg)
    assert result.use_case_type == "regression"
    _not_ready_naming(result, "X_REGRESSION_TARGET_NUMERIC")


# 8 -------------------------------------------------------------------------------------------------
def test_gap_8_repeated_ids_past_the_row_cap_are_never_seen(tmp_path: Path) -> None:
    cfg = config("targeted-advertisement")
    frame = customers(3000)
    frame.loc[2800:, "customer_id"] = frame.loc[:199, "customer_id"].to_numpy()  # repeats after the cap
    storage = LocalStorage(tmp_path)
    storage.write_bytes("uploads/u1/source.csv", frame.to_csv(index=False).encode())
    cap = 2500  # stands in for the 2,000,000-row profile cap (`ingest.DEFAULT_PROFILE_ROW_CAP`)
    read = ingest.read_upload(storage, "uploads/u1/source.csv", row_cap=cap)
    assert len(read.frame) == cap and read.row_count == 3000
    assert dry_run(read.frame, cfg, row_count=read.row_count).passed  # PK_NOT_UNIQUE saw the first 2,500
    assert frame["customer_id"].duplicated().sum() == 200
    scan = scan_source(
        storage,
        "uploads/u1/source.csv",
        file_format="csv",
        rows_parsed=read.row_count,
        encoding=read.encoding,
        delimiter=read.delimiter,
        key_columns=("customer_id",),
    )
    result = report(read.frame, cfg, row_count=read.row_count, scan=scan)
    _not_ready_naming(result, "X_PK_UNIQUE_FULL")
    assert result.probe("X_PK_UNIQUE_FULL").measured == 200.0
    assert result.source.sampled


# 9 -------------------------------------------------------------------------------------------------
def test_gap_9_size_limits_are_measured_before_the_rows_the_run_removes() -> None:
    cfg = config("targeted-advertisement", governance={"consent_column": "marketing_opt_in"})
    frame = customers(1100, positive_rate=0.3)
    frame["marketing_opt_in"] = np.arange(len(frame)) % 4 != 0  # a quarter did not consent
    assert dry_run(frame, cfg).passed  # 1,100 rows >= 1,000 as uploaded
    rows = _rows(frame, cfg, "customer_id", "converted_30d")
    assert len(rows) < cfg.validation.min_rows  # what training actually gets
    result = report(frame, cfg)
    _not_ready_naming(result, "X_POST_FILTER_THRESHOLDS")
    counts = result.post_filter_counts
    assert counts is not None and counts.rows_after_filters == len(rows) == 825


def test_the_nine_fixtures_cover_the_split_kinds_they_claim() -> None:
    assert config("fault-prediction").split.type is SplitType.TIME_BASED
    assert config("targeted-advertisement").split.type is SplitType.RANDOM_STRATIFIED


class _NoJobs:
    """`run_train` runs on this thread; the runner is only there to satisfy the constructor."""

    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not submit jobs")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@pytest.mark.slow
def test_gap_7_the_text_amount_fails_a_real_training_run_at_train(tmp_path: Path) -> None:
    """The run itself, on a temporary store: it passes ingest, validate, prepare and split, and stops at train."""
    from engine.config import resolve_config
    from engine.contracts import RunRecord, StageKey
    from engine.jobs import CancelToken
    from engine.pipeline import Pipeline, StageContext
    from engine.registry import LocalModelRegistry
    from engine.stages.scorer import TrainError
    from engine.storage import run_key, upload_key
    from tests.unit.doctor.readiness_fixtures import CONFIGS

    storage = LocalStorage(tmp_path / "data")
    registry = LocalModelRegistry(tmp_path / "registry.db")
    frame = customers()
    frame["converted_30d"] = np.random.default_rng(7).choice(["low", "medium", "high"], len(frame))
    source = upload_key("u1", "source.csv")
    storage.write_text(source, frame.to_csv(index=False))
    overrides = {
        "problem_type": "regression",
        "model_search.time_limit_minutes": 1,
        "model_search.strategy": "fast",
        "model_search.tuning_trials": 5,
    }
    resolved = resolve_config("targeted-advertisement", overrides, root=CONFIGS)
    storage.write_model(run_key("r-gap-7", "run_config.json"), resolved)
    context = StageContext(
        run_id="r-gap-7",
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=["customer_id"],
        target="converted_30d",
        upload_key=source,
        model_version_id=None,
    )
    with pytest.raises(TrainError) as stopped:
        Pipeline(storage, registry, _NoJobs()).run_train(context)  # type: ignore[arg-type]
    assert stopped.value.code == "TRAIN_NO_MODEL_FITTED"
    record = storage.read_model(run_key("r-gap-7", "run.json"), RunRecord)
    assert record.state is RunState.FAILED and record.error is not None
    assert record.error.code == "TRAIN_NO_MODEL_FITTED" and record.error.stage is StageKey.TRAIN
    assert report(frame, resolved.config).probe("X_REGRESSION_TARGET_NUMERIC").status == "fail"
