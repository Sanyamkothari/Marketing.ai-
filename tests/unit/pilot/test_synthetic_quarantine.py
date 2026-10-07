"""A planted effect can never be presented as a result: the synthetic quarantine (Plan J M95).

`RunRecord.synthetic` marks a run that read generated data. Every report drawn from such a run - the
results report and the value view, in HTML and in PDF - carries "Synthetic data: planted effect, not a
forecast". A run that did not read generated data carries nothing, and a `run.json` written before the
field existed reads as not synthetic.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine import __version__
from engine.config import ProblemType, RunMode, load_use_case
from engine.contracts import ModelStatus, ModelVersion, RunRecord, RunState
from engine.pilot.demo import mark_runs_synthetic
from engine.pilot.document import (
    SYNTHETIC_HEADLINE,
    Paragraph,
    ReportDocument,
    render_html,
    render_pdf,
)
from engine.pilot.results import ResultsFacts, results_document
from engine.pilot.roi import RoiInputs, compute_roi, roi_document
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, StorageError, run_key
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    ConfidenceValue,
    IncrementalityReport,
    IncrementalityStatus,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
PHRASE = "Synthetic data: planted effect, not a forecast"
CHURN_RUN = "r_20260923_aa000001"
OTHER_RUN = "r_20260923_aa000002"


def record(run_id: str = CHURN_RUN, *, mode: RunMode = RunMode.SCORE, **extra: object) -> RunRecord:
    base: dict[str, object] = {
        "run_id": run_id,
        "use_case_id": "telco-churn",
        "use_case_name": "Telco Customer Churn",
        "mode": mode,
        "state": RunState.DONE,
        "created_at": NOW - timedelta(days=90),
        "finished_at": NOW - timedelta(days=90),
        "file_name": "scores.csv",
        "primary_key": "customer_id",
        "problem_type": ProblemType.BINARY_CLASSIFICATION,
        "model_choice": "auto",
        "engine_version": __version__,
    }
    base.update(extra)
    return RunRecord.model_validate(base)


def pdf_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return " ".join((page.extract_text() or "") for page in reader.pages)


def mature_report() -> IncrementalityReport:
    return IncrementalityReport(
        run_id=CHURN_RUN,
        outcome_column="retained_60d",
        outcome_window_days=60,
        as_of=NOW,
        status=IncrementalityStatus.MATURE,
        results_available_on=None,
        treated_rows=1000,
        treated_conversions=800,
        treated_rate=0.8,
        control_rows=100,
        control_conversions=74,
        control_rate=0.74,
        absolute_lift=ConfidenceValue(value=0.06, ci_low=0.03, ci_high=0.09),
        relative_lift=0.06 / 0.74,
        incremental_conversions=ConfidenceValue(value=60.0, ci_low=30.0, ci_high=90.0),
        p_value=0.1,
        rows_immature=0,
        rows_without_outcome=0,
        rows_suppressed_or_untreated=0,
        causal=True,
        summary="measured",
        computed_at=NOW,
    )


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


# --- the contract -------------------------------------------------------------------------------------


def test_a_run_json_written_before_the_field_existed_is_not_synthetic() -> None:
    document = json.loads(record().model_dump_json())
    del document["synthetic"]
    assert RunRecord.model_validate(document).synthetic is False
    assert record().synthetic is False
    assert record(synthetic=True).synthetic is True


def test_a_report_json_written_before_the_field_existed_is_not_synthetic() -> None:
    document = ReportDocument(kind="roi", title="t", generated_at=NOW)
    old = json.loads(document.model_dump_json())
    del old["synthetic"]
    assert ReportDocument.model_validate(old).synthetic is False


# --- the renderers ------------------------------------------------------------------------------------


def test_the_html_draws_the_block_when_synthetic_and_never_otherwise() -> None:
    synthetic = render_html(
        ReportDocument(kind="roi", title="Campaign value", generated_at=NOW, synthetic=True)
    )
    plain = render_html(ReportDocument(kind="roi", title="Campaign value", generated_at=NOW))
    assert PHRASE == SYNTHETIC_HEADLINE
    assert PHRASE in synthetic and 'data-synthetic="true"' in synthetic
    assert "Synthetic data" not in plain and "planted" not in plain
    assert synthetic.index(PHRASE) < synthetic.index("Generated ")  # near the top, not buried in the footer
    assert "<script" not in synthetic


def test_the_pdf_draws_the_block_when_synthetic_and_never_otherwise() -> None:
    blocks = (Paragraph(text="Body text."),)
    synthetic = render_pdf(
        ReportDocument(kind="results", title="Results", generated_at=NOW, blocks=blocks, synthetic=True)
    )
    plain = render_pdf(ReportDocument(kind="results", title="Results", generated_at=NOW, blocks=blocks))
    assert synthetic.startswith(b"%PDF-")
    assert PHRASE in " ".join(pdf_text(synthetic).split())
    assert "planted" not in pdf_text(plain) and "Synthetic data" not in pdf_text(plain)


# --- the value view ----------------------------------------------------------------------------------


@pytest.mark.parametrize("synthetic", [True, False])
def test_the_value_report_follows_the_run_record(storage: LocalStorage, synthetic: bool) -> None:
    storage.write_model(run_key(CHURN_RUN, RUN_FILENAME), record(synthetic=synthetic))
    storage.write_model(run_key(CHURN_RUN, INCREMENTALITY_FILENAME), mature_report())
    view = compute_roi(storage, CHURN_RUN, inputs=RoiInputs(value_per_outcome=4000.0))
    assert view.status == "measured" and view.synthetic is synthetic
    document = roi_document(view, now=NOW)
    assert document.synthetic is synthetic
    assert (PHRASE in render_html(document)) is synthetic
    assert (PHRASE in " ".join(pdf_text(render_pdf(document)).split())) is synthetic


def test_a_run_not_yet_measured_is_still_marked(storage: LocalStorage) -> None:
    storage.write_model(run_key(CHURN_RUN, RUN_FILENAME), record(synthetic=True))
    view = compute_roi(storage, CHURN_RUN)
    assert view.status == "not_measured" and view.synthetic
    assert PHRASE in render_html(roi_document(view, now=NOW))


# --- the results report ------------------------------------------------------------------------------


def facts(train: RunRecord, score: RunRecord | None = None) -> ResultsFacts:
    config = load_use_case("telco-churn")
    model = ModelVersion(
        model_id="m1",
        use_case_id="telco-churn",
        version=1,
        run_id=train.run_id,
        created_at=NOW,
        status=ModelStatus.CHAMPION,
        metric=config.model_search.metric,
        metric_label="ROC-AUC",
        test_score=0.8,
        model_display_name="LightGBM",
        schema_key="schema.json",
        run_config_key="run_config.json",
        predictor_key="model",
        engine_version="0.1.0",
        autogluon_version="1.0.0",
    )
    return ResultsFacts(
        model=model,
        train_run=train,
        config=config,
        client_name="",
        evaluation=None,
        deciles=None,
        baseline=None,
        importance=None,
        split=None,
        manifest=None,
        score_run=score,
        summary=None,
        examples=(),
        examples_from="",
        uplift=None,
        segments=None,
        feature_names={},
    )


@pytest.mark.parametrize(
    ("train", "score", "expected"),
    [
        (True, None, True),
        (False, True, True),
        (True, False, True),
        (False, False, False),
        (False, None, False),
    ],
)
def test_the_results_report_is_synthetic_when_the_model_or_its_scoring_run_is(
    train: bool, score: bool | None, expected: bool
) -> None:
    train_run = record("r_train", mode=RunMode.TRAIN, synthetic=train)
    score_run = None if score is None else record(OTHER_RUN, synthetic=score)
    document = results_document(facts(train_run, score_run), now=NOW)
    assert document.synthetic is expected
    assert (PHRASE in render_html(document)) is expected
    assert (PHRASE in " ".join(pdf_text(render_pdf(document)).split())) is expected


# --- marking the demo's runs -------------------------------------------------------------------------


def test_marking_runs_is_idempotent_and_keeps_the_rest_of_the_record(storage: LocalStorage) -> None:
    storage.write_model(run_key(CHURN_RUN, RUN_FILENAME), record(headline_score=0.81))
    storage.write_model(run_key(OTHER_RUN, RUN_FILENAME), record(OTHER_RUN, synthetic=True))
    assert mark_runs_synthetic(storage, [CHURN_RUN, OTHER_RUN]) == (CHURN_RUN,)
    assert mark_runs_synthetic(storage, [CHURN_RUN, OTHER_RUN]) == ()
    stored = storage.read_model(run_key(CHURN_RUN, RUN_FILENAME), RunRecord)
    assert stored.synthetic and stored.headline_score == 0.81


def test_marking_a_run_that_does_not_exist_is_an_error_not_a_skip(storage: LocalStorage) -> None:
    with pytest.raises(StorageError):
        mark_runs_synthetic(storage, ["r_nope"])
