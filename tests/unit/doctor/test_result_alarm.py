"""The too-good-to-be-true alarm and "Things we checked" (Plan L follow-up, DEC-1599).

`RESULT_SUSPICIOUSLY_GOOD` (`engine.model_gates.suspiciously_good`) is a warning beside the champion
decision, never a block: above the use case's line it fails and names the column the model leans on
most; below it passes; without importances it says it cannot name one; a model with no measure that
has a line is not judged at all. `engine.run_checks.things_checked` lists only what the run's own
artefacts show ran, and says "not run" for a check its settings switched off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import (
    Metric,
    ModelFamily,
    ProblemType,
    RunMode,
    SplitType,
    ThresholdMode,
    resolve_config,
)
from engine.contracts import (
    BaselineComparison,
    BaselineMetric,
    EvaluationReport,
    FairnessReport,
    FeatureImportance,
    FeatureImportanceItem,
    MetricValue,
    ModelStatus,
    ModelVersion,
    RunRecord,
    RunState,
    Severity,
    SplitPart,
    SplitReport,
    ValidationCheck,
    ValidationReport,
)
from engine.model_gates import (
    DEFAULT_SUSPICION_THRESHOLDS,
    MODEL_CHECKS_FILENAME,
    RESULT_SUSPICIOUSLY_GOOD,
    SuspicionConfig,
    approval_checks,
    load_suspicion_config,
    suspiciously_good,
)
from engine.pilot.plain import jargon_in
from engine.run_checks import things_checked
from engine.storage import LocalStorage, run_key

RUN_ID = "r_20261010_a1a1a1a1"
USE_CASE = "targeted-advertisement"
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
LINES = {Metric.ROC_AUC: 0.97}


def evaluation(
    score: float, *, metric: Metric = Metric.ROC_AUC, roc: float | None = None
) -> EvaluationReport:
    label = {Metric.ROC_AUC: "ROC-AUC", Metric.PR_AUC: "PR-AUC", Metric.RMSE: "RMSE"}[metric]
    metrics = [MetricValue(id=metric, label=label, value=score, greater_is_better=metric is not Metric.RMSE)]
    if roc is not None:
        metrics.append(MetricValue(id=Metric.ROC_AUC, label="ROC-AUC", value=roc, greater_is_better=True))
    return EvaluationReport(
        run_id=RUN_ID,
        problem_type=ProblemType.REGRESSION if metric is Metric.RMSE else ProblemType.BINARY_CLASSIFICATION,
        rows_evaluated=1_000,
        positive_rate=0.2,
        primary_metric=metric,
        primary_metric_label=label,
        headline_score=score,
        metrics=tuple(metrics),
        threshold=0.5,
        threshold_mode=ThresholdMode.FIXED,
        threshold_detail="fixed at 0.5",
        calibration=None,
        evaluated_at=NOW,
    )


def importance(*names: str) -> FeatureImportance:
    shares = [60.0, 25.0, 15.0][: len(names)]
    # Written in reverse to show the strongest is picked by rank, not by position in the file.
    items = [
        FeatureImportanceItem(rank=rank, feature=name, importance=share / 100, share_pct=share)
        for rank, (name, share) in enumerate(zip(names, shares, strict=True), start=1)
    ]
    return FeatureImportance(
        run_id=RUN_ID, method="permutation", top_n=len(items), items=tuple(reversed(items)), caption="."
    )


# ---------------------------------------------------------------------------
# The alarm
# ---------------------------------------------------------------------------
def test_above_the_line_it_warns_and_names_the_strongest_column() -> None:
    alarm = suspiciously_good(evaluation(0.991), importance("cancel_date", "tenure", "plan"), LINES)
    assert alarm is not None and alarm.code == RESULT_SUSPICIOUSLY_GOOD
    assert alarm.passed is False, "a warning: failed, shown as 'Check', never a block"
    assert "0.991" in alarm.message and "0.97" in alarm.message
    assert "'cancel_date'" in alarm.message and "60%" in alarm.message
    assert "known only after the outcome" in alarm.message
    assert "'tenure'" not in alarm.message, "only the strongest column is named"
    assert "nothing is blocked" in alarm.message


def test_at_or_below_the_line_it_passes() -> None:
    for score in (0.84, 0.97):
        alarm = suspiciously_good(evaluation(score), importance("tenure"), LINES)
        assert alarm is not None and alarm.passed is True
        assert "tenure" not in alarm.message


def test_without_importances_it_says_it_cannot_name_a_column() -> None:
    for saved in (None, FeatureImportance(run_id=RUN_ID, method="shap", top_n=0, items=(), caption=".")):
        alarm = suspiciously_good(evaluation(0.995), saved, LINES)
        assert alarm is not None and alarm.passed is False
        assert "cannot name the one it leans on most" in alarm.message


def test_a_main_measure_with_no_line_is_judged_on_the_roc_auc_and_otherwise_not_at_all() -> None:
    alarm = suspiciously_good(evaluation(0.60, metric=Metric.PR_AUC, roc=0.985), importance("refund"), LINES)
    assert alarm is not None and alarm.passed is False and "ROC-AUC on the test set is 0.985" in alarm.message
    assert suspiciously_good(evaluation(1.2, metric=Metric.RMSE), importance("refund"), LINES) is None
    own_line = suspiciously_good(evaluation(0.60, metric=Metric.PR_AUC), None, {Metric.PR_AUC: 0.5})
    assert own_line is not None and own_line.passed is False


def test_the_alarm_speaks_plainly() -> None:
    alarm = suspiciously_good(evaluation(0.99), importance("cancel_date"), LINES)
    assert alarm is not None
    # The measure's own name is the run's label, shown as the Model page shows it; the rest is plain.
    assert jargon_in(alarm.message.replace("ROC-AUC", "")) == ()


# ---------------------------------------------------------------------------
# The line is configuration, per use case
# ---------------------------------------------------------------------------
def test_the_shipped_line_is_the_default_and_a_use_case_can_have_its_own(
    config_root: Path, tmp_path: Path
) -> None:
    shipped = load_suspicion_config(config_root)
    assert shipped.thresholds_for(USE_CASE) == DEFAULT_SUSPICION_THRESHOLDS == {Metric.ROC_AUC: 0.97}
    (tmp_path / MODEL_CHECKS_FILENAME).write_text(
        "schema_version: 1\ndefault: {roc_auc: 0.97}\nuse_cases: {fault-prediction: {roc_auc: 0.995}}\n",
        encoding="utf-8",
    )
    own = load_suspicion_config(tmp_path)
    assert own.thresholds_for("fault-prediction") == {Metric.ROC_AUC: 0.995}
    assert own.thresholds_for(USE_CASE) == {Metric.ROC_AUC: 0.97}
    assert (
        load_suspicion_config(tmp_path / "nowhere").thresholds_for(USE_CASE) == DEFAULT_SUSPICION_THRESHOLDS
    )


@pytest.mark.parametrize("bad", [{"rmse": 0.5}, {"roc_auc": 1.5}, {"roc_auc": 0}])
def test_a_line_must_be_on_a_higher_is_better_measure_between_0_and_1(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SuspicionConfig.model_validate({"default": bad})


# ---------------------------------------------------------------------------
# On the Approver's screen
# ---------------------------------------------------------------------------
def version(metric: Metric = Metric.ROC_AUC) -> ModelVersion:
    return ModelVersion(
        model_id="m_1",
        use_case_id=USE_CASE,
        version=1,
        run_id=RUN_ID,
        created_at=NOW,
        status=ModelStatus.PENDING_APPROVAL,
        metric=metric,
        metric_label="ROC-AUC",
        test_score=0.99,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(RUN_ID, "schema.json"),
        run_config_key=run_key(RUN_ID, "run_config.json"),
        predictor_key=run_key(RUN_ID, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


def test_a_propensity_model_carries_the_alarm_and_never_without_its_evaluation(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    assert approval_checks(storage, version(), config_root) == (), "nothing measured, nothing claimed"
    storage.write_model(run_key(RUN_ID, "evaluation.json"), evaluation(0.993))
    (alone,) = approval_checks(storage, version(), config_root)
    assert alone.passed is False and "cannot name" in alone.message, "importances missing: handled"
    storage.write_model(run_key(RUN_ID, "feature_importance.json"), importance("closed_reason", "tenure"))
    (alarm,) = approval_checks(storage, version(), config_root)
    assert alarm.code == RESULT_SUSPICIOUSLY_GOOD and alarm.passed is False
    assert "'closed_reason'" in alarm.message


# ---------------------------------------------------------------------------
# Things we checked
# ---------------------------------------------------------------------------
def record(mode: RunMode = RunMode.TRAIN) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        use_case_id=USE_CASE,
        use_case_name="Targeted Advertisement",
        mode=mode,
        state=RunState.DONE,
        created_at=NOW,
        upload_id="u_1",
        file_name="train.csv",
        primary_key="entity_key",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        engine_version="0.1.0",
    )


def write_training_run(storage: LocalStorage, config_root: Path, *, leakage_check: bool = True) -> None:
    storage.write_model(run_key(RUN_ID, "run.json"), record())
    storage.write_model(run_key(RUN_ID, "evaluation.json"), evaluation(0.991))
    storage.write_model(run_key(RUN_ID, "feature_importance.json"), importance("cancel_date", "tenure"))
    resolved = resolve_config(USE_CASE, root=config_root)
    validation_settings = resolved.config.validation.model_copy(update={"leakage_check": leakage_check})
    storage.write_model(
        run_key(RUN_ID, "run_config.json"),
        resolved.model_copy(
            update={"config": resolved.config.model_copy(update={"validation": validation_settings})}
        ),
    )
    storage.write_model(
        run_key(RUN_ID, "validation.json"),
        ValidationReport(
            run_id=RUN_ID,
            upload_id="u_1",
            mode=RunMode.TRAIN,
            checks=(
                ValidationCheck(
                    code="LEAKAGE_SUSPECTED",
                    severity=Severity.WARNING,
                    message="Column 'refund_flag' is named like an outcome.",
                    column="refund_flag",
                ),
            ),
            error_count=0,
            warning_count=1,
            passed=True,
            validated_at=NOW,
        ),
    )
    storage.write_model(
        run_key(RUN_ID, "split.json"),
        SplitReport(
            run_id=RUN_ID,
            type=SplitType.RANDOM_STRATIFIED,
            time_column=None,
            group_column=None,
            parts=(
                SplitPart(name="train", rows=7_000, share=0.7),
                SplitPart(name="validation", rows=1_500, share=0.15),
                SplitPart(name="test", rows=1_500, share=0.15),
            ),
            validation_fraction=0.15,
            test_fraction=0.15,
            train_cutoff=None,
            test_cutoff=None,
            seed=1,
            detail="random",
            split_at=NOW,
        ),
    )
    storage.write_model(
        run_key(RUN_ID, "baseline.json"),
        BaselineComparison(
            run_id=RUN_ID,
            baseline_name="Logistic regression",
            baseline_family=ModelFamily.LOGISTIC_REGRESSION,
            rows=(
                BaselineMetric(
                    id=Metric.ROC_AUC,
                    label="ROC-AUC",
                    model_value=0.991,
                    baseline_value=0.8,
                    delta=0.191,
                    model_better=True,
                ),
            ),
            model_beats_baseline=True,
        ),
    )
    storage.write_model(
        run_key(RUN_ID, "fairness.json"),
        FairnessReport(
            run_id=RUN_ID,
            column=None,
            evaluated=False,
            reason_not_evaluated="No sensitive column was set for this use case.",
            groups=(),
            max_positive_rate_gap=None,
            max_recall_gap=None,
            max_precision_gap=None,
            note="Not evaluated.",
        ),
    )


def test_things_checked_lists_what_the_artefacts_show_with_the_alarm_first(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    write_training_run(storage, config_root)
    view = things_checked(storage, record(), config_root)
    by_id = {item.id: item for item in view.checks}
    assert [item.id for item in view.checks] == [
        "too_good",
        "before_training",
        "leak_columns",
        "split",
        "baseline",
        "fairness",
    ]
    assert by_id["too_good"].status == "warning" and by_id["too_good"].code == RESULT_SUSPICIOUSLY_GOOD
    assert "'cancel_date'" in by_id["too_good"].message
    assert by_id["leak_columns"].status == "warning" and "'refund_flag'" in by_id["leak_columns"].message
    assert by_id["split"].status == "passed" and "1,500 rows" in by_id["split"].message
    assert by_id["baseline"].status == "passed"
    assert by_id["fairness"].status == "not_run", "a check that did not run is never shown as passed"
    assert by_id["fairness"].message.startswith("Not run: no sensitive column")
    assert view.warnings == 2


def test_a_check_switched_off_is_not_run_and_a_missing_report_is_left_out(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    write_training_run(storage, config_root, leakage_check=False)
    (tmp_path / run_key(RUN_ID, "baseline.json")).unlink()
    view = things_checked(storage, record(), config_root)
    by_id = {item.id: item for item in view.checks}
    assert by_id["leak_columns"].status == "not_run" and "switched off" in by_id["leak_columns"].message
    assert "baseline" not in by_id, "no report, no line: a check that did not run is never claimed"


def test_a_scoring_run_lists_nothing_and_says_why(tmp_path: Path, config_root: Path) -> None:
    view = things_checked(LocalStorage(tmp_path), record(RunMode.SCORE), config_root)
    assert view.checks == () and view.note is not None and "training run" in view.note


def test_the_route_serves_the_checks_and_404s_an_unknown_run(tmp_path: Path, config_root: Path) -> None:
    write_training_run(LocalStorage(tmp_path), config_root)
    client = TestClient(create_app(config_root=config_root, data_dir=tmp_path))
    body = client.get(f"/runs/{RUN_ID}/checks").json()
    assert body["run_id"] == RUN_ID and body["warnings"] == 2
    assert body["checks"][0]["code"] == RESULT_SUSPICIOUSLY_GOOD
    missing = client.get("/runs/r_20261010_ffffffff/checks")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "RUN_NOT_FOUND"
