"""The run lifecycle end to end through `TestClient` (design §6.9) - the M2 acceptance tests.

`engine.stages.validate` is landing in parallel, so the checks are stood in for by a small scripted
verdict defined here: it emits the same `ValidationCheck` shapes the design specifies, including the
`details["override_path"]`, `details["override_value"]` and `details["acknowledge"]` payloads the UI
acts on, and it *respects* those overrides, so the round trips below exercise the real thing under
test - `resolve_config`, the 409 body, and the run directory.

Two properties are asserted against the real engine rather than the stand-in: that every suggested
override path really is a member of `engine.config.overridable_paths`, so following a suggestion can
never come back as a 422; and that a follow-up run carrying the suggestion is accepted.

The parametrised sweep over `tests/fixtures/make_data.VARIANTS` at the bottom needs the real ingest
and validate modules and skips until they land.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from api.schemas import RunDetailResponse, RunListResponse
from engine.config import (
    ColumnRole,
    ColumnType,
    ConfigError,
    Metric,
    ProblemType,
    RunMode,
    UseCaseConfig,
    load_use_case,
    overridable_paths,
    resolve_config,
)
from engine.contracts import (
    FeatureSchema,
    FeatureSchemaColumn,
    ModelStatus,
    ModelVersion,
    RunRecord,
    RunState,
    Severity,
    ValidationCheck,
    ValidationReport,
)
from engine.jobs import CancelToken, ThreadJobRunner
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.stages import score, validate
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_data import (
    LEAKY_COLUMN,
    PII_EMAIL_COLUMN,
    VARIANT_SPECS,
    VARIANTS,
    GenerationSpec,
    generate,
)
from tests.fixtures.planned import PLANNED_ID, planned_config_root
from tests.integration.test_api_uploads import (
    CLEAN_CSV,
    DEMO_ID,
    REAL_INGEST,
    UNKNOWN_ID,
    StubFrame,
    install_ingest_stub,
)

pytestmark = pytest.mark.integration

OTHER_ID = "win-back-campaign"
OTHER_USE_CASE = "payment-propensity"
TARGET = "converted_30d"
PRIMARY_KEY = "customer_id"
NON_BINARY_TARGET_MARKER = "grade"

#: Names of the M2 validate surface this module stands in for; used to skip the real-engine tests.
VALIDATE_NAMES = ("validate_for_training", "validate_against_schema", "validation_detail")

REAL_VALIDATE = all(hasattr(validate, name) for name in VALIDATE_NAMES)

LEAKY_CSV = (
    f"{PRIMARY_KEY},snapshot_date,tenure_months,{LEAKY_COLUMN},{TARGET}\n"
    "C-1,2026-08-01,12,0.99,1\n"
    "C-2,2026-08-01,30,0.01,0\n"
    "C-3,2026-08-01,7,0.02,0\n"
)

DUPLICATE_KEY_CSV = (
    f"{PRIMARY_KEY},snapshot_date,tenure_months,{TARGET}\n"
    "C-1,2026-08-01,12,1\n"
    "C-1,2026-08-01,30,0\n"
    "C-3,2026-08-01,7,0\n"
)

NON_BINARY_CSV = (
    f"{PRIMARY_KEY},snapshot_date,{NON_BINARY_TARGET_MARKER},{TARGET}\n"
    "C-1,2026-08-01,a,1\n"
    "C-2,2026-08-01,b,0\n"
    "C-3,2026-08-01,c,2\n"
)

WARNING_ONLY_CSV = (
    f"{PRIMARY_KEY},snapshot_date,{PII_EMAIL_COLUMN},{TARGET}\n"
    "C-1,2026-08-01,a@example.invalid,1\n"
    "C-2,2026-08-01,b@example.invalid,0\n"
    "C-3,2026-08-01,c@example.invalid,0\n"
)


# ---------------------------------------------------------------------------
# The stand-in for `engine.stages.validate`'s M2 surface
# ---------------------------------------------------------------------------
def stub_validate_for_training(
    frame: StubFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str,
    acknowledged: Any = (),
    upload_id: str,
    row_count: int | None = None,
    now: Any = None,
) -> ValidationReport:
    """A scripted verdict with the design's `details` payloads, honouring overrides and acknowledgements."""
    checks = [
        check
        for check in (
            _leakage_check(frame, config),
            _non_binary_check(frame, config),
            _duplicate_key_check(frame, primary_key, config),
            _pii_check(frame),
        )
        if check is not None
    ]
    marked = tuple(_acknowledge(check, frozenset(acknowledged)) for check in checks)
    errors = sum(1 for c in marked if c.severity is Severity.ERROR and not c.acknowledged)
    return ValidationReport(
        run_id=None,
        upload_id=upload_id,
        mode=RunMode.TRAIN,
        checks=marked,
        error_count=errors,
        warning_count=sum(1 for c in marked if c.severity is Severity.WARNING),
        passed=errors == 0,
        validated_at=utc_now(),
    )


def stub_validate_against_schema(frame: StubFrame, schema: Any, **kwargs: Any) -> ValidationReport:
    return stub_validate_for_training(
        frame,
        load_use_case(DEMO_ID),
        primary_key=kwargs.get("primary_key", PRIMARY_KEY),
        target="",
        upload_id=kwargs["upload_id"],
    )


def stub_validation_detail(report: ValidationReport) -> str:
    if report.error_count == 0 and report.warning_count == 0:
        return "No problems found"
    parts = []
    if report.error_count:
        parts.append(f"{report.error_count} error{'s' if report.error_count != 1 else ''}")
    if report.warning_count:
        parts.append(f"{report.warning_count} warning{'s' if report.warning_count != 1 else ''}")
    return " · ".join(parts)


def _leakage_check(frame: StubFrame, config: UseCaseConfig) -> ValidationCheck | None:
    if LEAKY_COLUMN not in frame.columns or LEAKY_COLUMN in config.prepare.exclude_columns:
        return None
    return ValidationCheck(
        code="LEAKAGE_SUSPECTED",
        severity=Severity.ERROR,
        message=f"Column '{LEAKY_COLUMN}' almost perfectly predicts the target.",
        suggestion=f"If '{LEAKY_COLUMN}' is only known after the outcome, exclude it.",
        column=LEAKY_COLUMN,
        details={
            "reason": "auc",
            "auc": 0.9903,
            "threshold": config.validation.leakage_auc_threshold,
            "override_path": "prepare.exclude_columns",
            "override_value": [LEAKY_COLUMN],
            "acknowledge": f"LEAKAGE_SUSPECTED:{LEAKY_COLUMN}",
        },
        acknowledgeable=True,
    )


def _non_binary_check(frame: StubFrame, config: UseCaseConfig) -> ValidationCheck | None:
    if NON_BINARY_TARGET_MARKER not in frame.columns:
        return None
    if config.problem_type is not ProblemType.BINARY_CLASSIFICATION:
        return None
    return ValidationCheck(
        code="TARGET_NOT_BINARY",
        severity=Severity.ERROR,
        message=f"'{TARGET}' has 3 different values. A yes/no model needs exactly two.",
        suggestion="Switch this run to a regression model, or map the values to two.",
        column=TARGET,
        details={
            "distinct_count": 3,
            "numeric": True,
            "switch_to": "regression",
            "override_path": "problem_type",
            "override_value": "regression",
        },
    )


def _duplicate_key_check(frame: StubFrame, primary_key: str, config: UseCaseConfig) -> ValidationCheck | None:
    if primary_key not in frame.columns:
        return None
    position = frame.columns.index(primary_key)
    values = [row[position] for row in frame.rows]
    if len(set(values)) == len(values):
        return None
    return ValidationCheck(
        code="PK_NOT_UNIQUE",
        severity=Severity.ERROR,
        message=f"This file has more than one row per {config.entity}.",
        suggestion=f"Combine the rows so each {config.entity} appears once.",
        column=primary_key,
        details={"rows": len(values), "distinct_keys": len(set(values)), "sampled": False},
    )


def _pii_check(frame: StubFrame) -> ValidationCheck | None:
    if PII_EMAIL_COLUMN not in frame.columns:
        return None
    return ValidationCheck(
        code="PII_DETECTED",
        severity=Severity.WARNING,
        message=f"'{PII_EMAIL_COLUMN}' looks like it contains email addresses.",
        suggestion="Its values will be replaced with [REDACTED] before training.",
        column=PII_EMAIL_COLUMN,
        details={"pii_kinds": ["email"], "handling": "redact"},
        acknowledgeable=True,
    )


def _acknowledge(check: ValidationCheck, acknowledged: frozenset[str]) -> ValidationCheck:
    if not check.acknowledgeable:
        return check
    keys = {check.code} | ({f"{check.code}:{check.column}"} if check.column else set())
    return check.model_copy(update={"acknowledged": bool(keys & acknowledged)})


def install_validate_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validate, "validate_for_training", stub_validate_for_training, raising=False)
    monkeypatch.setattr(validate, "validate_against_schema", stub_validate_against_schema, raising=False)
    monkeypatch.setattr(validate, "validation_detail", stub_validation_detail, raising=False)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh artefact store per test, so `list_keys` assertions mean something."""
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_ingest_stub(monkeypatch)
    install_validate_stub(monkeypatch)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture
def blocked_client(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """An app whose runs block until cancelled, on a single worker, so pending and running are reachable."""
    install_ingest_stub(monkeypatch)
    install_validate_stub(monkeypatch)

    def blocking(_storage: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            cancel.wait(10)
            cancel.raise_if_cancelled()

        return job

    monkeypatch.setattr(runs, "build_m2_job", blocking)
    app = create_app(config_root=config_root, data_dir=data_dir)
    app.state.jobs = ThreadJobRunner(max_workers=1)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


def upload(
    client: TestClient, payload: str = CLEAN_CSV, *, use_case: str = DEMO_ID, mode: str = "train"
) -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", payload.encode(), "text/csv")},
        data={"use_case": use_case, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def run_body(upload_id: str, *, use_case: str = DEMO_ID, **extra: Any) -> dict[str, Any]:
    # The key and target belong to the use case, so a body for a non-demo use case must take them
    # from its own config rather than inheriting the demo's.
    primary_key, target = PRIMARY_KEY, TARGET
    if use_case != DEMO_ID:
        try:
            config = load_use_case(use_case)
        except ConfigError:
            # A planned or unknown use case: the route refuses it before the body matters, and the
            # demo's key and target keep this helper usable for those 404 cases.
            pass
        else:
            keys = [c.name for c in config.template.columns if c.role is ColumnRole.PRIMARY_KEY]
            primary_key = keys[0]
            assert config.target.column is not None
            target = config.target.column
    body: dict[str, Any] = {
        "use_case": use_case,
        "mode": "train",
        "upload_id": upload_id,
        "primary_key": primary_key,
        "target": target,
    }
    body.update(extra)
    return body


def start_run(client: TestClient, payload: str = CLEAN_CSV, **extra: Any) -> Any:
    return client.post("/runs", json=run_body(upload(client, payload), **extra))


def await_job(client: TestClient, run_id: str, timeout: float = 10.0) -> None:
    jobs = client.app.state.jobs
    jobs.wait(run_id, timeout)


def seed_model(
    data_dir: Path,
    *,
    model_id: str,
    use_case_id: str = DEMO_ID,
    status: ModelStatus = ModelStatus.CHAMPION,
) -> ModelVersion:
    """Register one model version in the registry the app reads, with its `schema.json` on disk.

    The app builds its own `LocalModelRegistry` on `data_dir / registry.db`; a second instance on
    the same file is what `LocalModelRegistry` documents as safe, and is how the registry tests
    seed rows too. The schema is the document `POST /runs` validates a scoring file against, so a
    version without one could never start a run.
    """
    run_id = f"r_{model_id}"
    version = ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
        version=1,
        run_id=run_id,
        created_at=utc_now(),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.81,
        validation_score=0.82,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)
    LocalStorage(data_dir).write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=use_case_id,
            model_version_id=model_id,
            primary_key=PRIMARY_KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name="tenure_months", inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )
    return version


# ---------------------------------------------------------------------------
# POST /runs - the happy path
# ---------------------------------------------------------------------------
def test_clean_upload_starts_a_run_that_is_pollable_immediately(
    blocked_client: TestClient, storage: LocalStorage
) -> None:
    response = start_run(blocked_client)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert run_id.startswith("r_")
    assert response.headers["Location"] == f"/runs/{run_id}"

    detail = RunDetailResponse.model_validate(blocked_client.get(f"/runs/{run_id}").json())
    assert len(detail.status.stages) == 8
    assert detail.status.run_id == run_id
    assert 0 <= detail.status.progress_pct <= 100
    assert {stage.state for stage in detail.status.stages} <= {RunState.PENDING, RunState.RUNNING}
    assert len(storage.list_keys(f"runs/{run_id}/")) == 5


def test_run_record_at_creation_matches_the_design_document(blocked_client: TestClient) -> None:
    run_id = start_run(blocked_client).json()["run_id"]
    record = RunDetailResponse.model_validate(blocked_client.get(f"/runs/{run_id}").json()).run
    assert record.use_case_id == DEMO_ID
    assert record.mode is RunMode.TRAIN
    assert record.state is RunState.PENDING
    assert record.primary_key == PRIMARY_KEY
    assert record.target == TARGET
    assert record.model_choice == "__automl__"
    assert record.headline_metric_label
    assert record.headline_score is None
    assert record.best_model is None
    assert record.error is None
    assert record.champion is False
    assert set(record.artefacts) == {
        "run.json",
        "status.json",
        "run_config.json",
        "profile.json",
        "validation.json",
    }


def test_run_directory_holds_exactly_the_five_m2_artefacts(
    blocked_client: TestClient, storage: LocalStorage
) -> None:
    run_id = start_run(blocked_client).json()["run_id"]
    assert set(storage.list_keys(f"runs/{run_id}/")) == {
        f"runs/{run_id}/{name}" for name in runs.CREATED_ARTEFACTS
    }


def test_m2_job_runs_two_stages_then_fails_honestly_at_prepare(client: TestClient) -> None:
    run_id = start_run(client).json()["run_id"]
    await_job(client, run_id)
    detail = RunDetailResponse.model_validate(client.get(f"/runs/{run_id}").json())
    assert detail.run.state is RunState.FAILED
    assert detail.run.error is not None
    assert detail.run.error.code == "STAGE_NOT_IMPLEMENTED"
    assert detail.run.error.stage is not None and detail.run.error.stage.value == "prepare"
    by_key = {stage.key.value: stage for stage in detail.status.stages}
    assert by_key["ingest"].state is RunState.DONE and by_key["ingest"].detail
    assert by_key["validate"].state is RunState.DONE and by_key["validate"].detail
    assert by_key["prepare"].state is RunState.FAILED
    assert detail.status.state is RunState.FAILED


# ---------------------------------------------------------------------------
# POST /runs - the 409 contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (LEAKY_CSV, "LEAKAGE_SUSPECTED"),
        (DUPLICATE_KEY_CSV, "PK_NOT_UNIQUE"),
        (NON_BINARY_CSV, "TARGET_NOT_BINARY"),
    ],
)
def test_a_blocking_error_is_409_with_the_whole_report(
    client: TestClient, storage: LocalStorage, payload: str, code: str
) -> None:
    response = start_run(client, payload)
    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"detail", "validation"}
    assert body["detail"]["code"] == "VALIDATION_FAILED"
    assert "must be fixed" in body["detail"]["message"]
    assert body["detail"]["path"] is None
    report = ValidationReport.model_validate(body["validation"])
    assert report.passed is False
    assert report.error_count >= 1
    assert code in {check.code for check in report.checks}
    assert storage.list_keys("runs/") == ()


def test_the_409_report_is_persisted_on_the_upload_for_a_reload(
    client: TestClient, storage: LocalStorage
) -> None:
    upload_id = upload(client, LEAKY_CSV)
    assert client.post("/runs", json=run_body(upload_id)).status_code == 409
    stored = storage.read_model(f"uploads/{upload_id}/validation.json", ValidationReport)
    assert stored.passed is False


@pytest.mark.parametrize("payload", [LEAKY_CSV, NON_BINARY_CSV])
def test_every_suggested_override_path_is_genuinely_overridable(client: TestClient, payload: str) -> None:
    """A suggestion the UI can follow must be a path `resolve_config` accepts - never a later 422."""
    upload_id = upload(client, payload)
    body = client.post("/runs", json=run_body(upload_id)).json()
    allowed = overridable_paths(load_use_case(DEMO_ID))
    suggested = [
        (check["details"]["override_path"], check["details"]["override_value"])
        for check in body["validation"]["checks"]
        if "override_path" in check["details"]
    ]
    assert suggested, "a blocking check must tell the UI what to change"
    for path, value in suggested:
        assert path in allowed, f"{path!r} is suggested but is not overridable"
        followed = client.post("/runs", json=run_body(upload_id, overrides={path: value}))
        assert followed.status_code == 202, followed.text


def test_acknowledgement_round_trip_starts_the_run(client: TestClient, storage: LocalStorage) -> None:
    upload_id = upload(client, LEAKY_CSV)
    refused = client.post("/runs", json=run_body(upload_id))
    assert refused.status_code == 409
    token = next(
        check["details"]["acknowledge"]
        for check in refused.json()["validation"]["checks"]
        if "acknowledge" in check["details"]
    )
    accepted = client.post(
        "/runs",
        json=run_body(upload_id, overrides={"validation": {"acknowledged": [token]}}),
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    report = storage.read_model(f"runs/{run_id}/validation.json", ValidationReport)
    acknowledged = [check for check in report.checks if check.code == "LEAKAGE_SUSPECTED"]
    assert acknowledged and acknowledged[0].acknowledged is True
    assert acknowledged[0].severity is Severity.ERROR
    assert report.error_count == 0
    assert report.run_id == run_id


def test_problem_type_switch_round_trip_rederives_the_metrics(
    client: TestClient, storage: LocalStorage
) -> None:
    upload_id = upload(client, NON_BINARY_CSV)
    refused = client.post("/runs", json=run_body(upload_id))
    check = next(c for c in refused.json()["validation"]["checks"] if c["code"] == "TARGET_NOT_BINARY")
    assert check["details"]["switch_to"] == "regression"
    accepted = client.post("/runs", json=run_body(upload_id, overrides={"problem_type": "regression"}))
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    stored = storage.read_text(f"runs/{run_id}/run_config.json")
    assert '"problem_type": "regression"' in stored
    report = storage.read_model(f"runs/{run_id}/validation.json", ValidationReport)
    assert "TARGET_NOT_BINARY" not in {c.code for c in report.checks}


def test_a_warning_alone_never_blocks_a_run(client: TestClient, storage: LocalStorage) -> None:
    response = start_run(client, WARNING_ONLY_CSV)
    assert response.status_code == 202, response.text
    report = storage.read_model(f"runs/{response.json()['run_id']}/validation.json", ValidationReport)
    assert report.warning_count == 1
    assert report.error_count == 0
    assert "PII_DETECTED" in {check.code for check in report.checks}


# ---------------------------------------------------------------------------
# POST /runs - the other refusals
# ---------------------------------------------------------------------------
def test_unknown_upload_is_404(client: TestClient) -> None:
    response = client.post("/runs", json=run_body("u_nope"))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "UPLOAD_NOT_FOUND"


def test_planned_and_unknown_use_cases_are_404(
    client: TestClient, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown id on the shipped configuration; a planned one on a root that still has one."""
    upload_id = upload(client)
    response = client.post("/runs", json=run_body(upload_id, use_case=UNKNOWN_ID))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"

    install_ingest_stub(monkeypatch)
    root = planned_config_root(tmp_path / "planned")
    with TestClient(create_app(config_root=root, data_dir=data_dir)) as planned_client:
        planned = planned_client.post("/runs", json=run_body(upload_id, use_case=PLANNED_ID))
    assert planned.status_code == 404
    assert planned.json()["detail"]["code"] == "USE_CASE_PLANNED"


def test_mode_mismatch_is_409(client: TestClient) -> None:
    upload_id = upload(client, mode="score")
    response = client.post("/runs", json=run_body(upload_id))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UPLOAD_MODE_MISMATCH"


def test_score_mode_without_a_champion_answers_the_engines_one_no_champion_code(
    client: TestClient, data_dir: Path, config_root: Path
) -> None:
    """One code for "this use case has no champion", on the endpoint and in the stage alike.

    The endpoint used to answer a code of its own, `NO_CHAMPION_MODEL`, while the predict stage
    raised `CHAMPION_NOT_FOUND` for the same condition mid-run, so a UI switching on the code had
    to know both. The endpoint now reports the stage's code and the stage's sentence.
    """
    upload_id = upload(client, mode="score")
    response = client.post("/runs", json=run_body(upload_id, mode="score", target=None))

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == score.CHAMPION_NOT_FOUND
    with pytest.raises(score.ScoreError) as raised:
        score.resolve_model_version(
            resolve_config(DEMO_ID, root=config_root).config,
            registry=LocalModelRegistry(data_dir / REGISTRY_FILENAME),
            model_version_id=None,
        )
    assert detail["message"] == raised.value.message, "the endpoint speaks the engine's words"


def test_a_model_version_of_another_use_case_is_refused_before_the_run_starts(
    client: TestClient, data_dir: Path, config_root: Path
) -> None:
    """Finding 5: one resolver, so what the endpoint accepts is what the predict stage will score.

    The endpoint used to accept any `model_version_id` the registry knew, whatever use case it
    belonged to; the run was created, the file was validated against a foreign model's schema, and
    the run failed at the predict stage with `MODEL_USE_CASE_MISMATCH`. That refusal now happens
    on the request, with the stage's own code and message, and no run is created.
    """
    foreign = seed_model(data_dir, model_id="m_payment-propensity_1", use_case_id=OTHER_USE_CASE)
    upload_id = upload(client, mode="score")

    response = client.post(
        "/runs",
        json=run_body(upload_id, mode="score", target=None, model_version_id=foreign.model_id),
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == score.MODEL_USE_CASE_MISMATCH
    assert OTHER_USE_CASE in detail["message"] and DEMO_ID in detail["message"]
    with pytest.raises(score.ScoreError) as raised:
        score.resolve_model_version(
            resolve_config(DEMO_ID, root=config_root).config,
            registry=LocalModelRegistry(data_dir / REGISTRY_FILENAME),
            model_version_id=foreign.model_id,
        )
    assert (detail["code"], detail["message"]) == (raised.value.code, raised.value.message)
    assert RunListResponse.model_validate(client.get("/runs").json()).runs == (), "no run was started"


def test_an_unknown_model_version_is_a_404_about_that_version(client: TestClient, data_dir: Path) -> None:
    """A body that names a version nobody registered is a missing id, not a missing champion."""
    seed_model(data_dir, model_id="m_targeted-advertisement_1", status=ModelStatus.CHAMPION)
    upload_id = upload(client, mode="score")

    response = client.post(
        "/runs",
        json=run_body(upload_id, mode="score", target=None, model_version_id="m_does_not_exist"),
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == score.MODEL_NOT_FOUND
    assert "m_does_not_exist" in detail["message"]


def test_a_version_of_this_use_case_starts_a_run_whatever_its_status(
    client: TestClient, data_dir: Path
) -> None:
    """A named version is the user's choice, so a candidate scores as readily as the champion."""
    candidate = seed_model(data_dir, model_id="m_targeted-advertisement_2", status=ModelStatus.CANDIDATE)
    upload_id = upload(client, mode="score")

    response = client.post(
        "/runs",
        json=run_body(upload_id, mode="score", target=None, model_version_id=candidate.model_id),
    )

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    record = RunDetailResponse.model_validate(client.get(f"/runs/{run_id}").json()).run
    assert record.model_version_id == candidate.model_id
    assert record.mode is RunMode.SCORE


def test_every_resolver_code_is_mapped_onto_the_error_envelope(client: TestClient) -> None:
    """Finding 12: the codes are registered, so the envelope answers 4xx instead of guessing 500."""
    assert set(runs.SCORE_ERROR_STATUS) == {
        score.CHAMPION_NOT_FOUND,
        score.MODEL_NOT_FOUND,
        score.MODEL_USE_CASE_MISMATCH,
    }
    assert set(runs.SCORE_ERROR_STATUS) <= set(score.SCORE_ERRORS), "each one has a message table entry"
    for code, status in runs.SCORE_ERROR_STATUS.items():
        assert status in runs._RUN_ERRORS, f"{code} answers a status POST /runs documents"
        assert status != runs.UNMAPPED_STATUS


def test_an_unknown_override_path_is_422_and_names_the_path(client: TestClient) -> None:
    upload_id = upload(client)
    response = client.post("/runs", json=run_body(upload_id, overrides={"prepare.nonsense": 1}))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"].startswith("OVERRIDE_") or detail["code"] == "CONFIG_INVALID"
    assert "nonsense" in f"{detail['message']}{detail['path']}"


def test_an_unknown_body_key_is_422(client: TestClient) -> None:
    upload_id = upload(client)
    response = client.post("/runs", json=run_body(upload_id, nonsense=1))
    assert response.status_code == 422
    assert "nonsense" in response.text


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------
def test_run_history_is_newest_first_and_filters(client: TestClient) -> None:
    first = start_run(client).json()["run_id"]
    time.sleep(0.01)
    second = start_run(client).json()["run_id"]
    other = client.post("/runs", json=run_body(upload(client, use_case=OTHER_ID), use_case=OTHER_ID)).json()[
        "run_id"
    ]

    listed = RunListResponse.model_validate(client.get("/runs").json()).runs
    ids = [record.run_id for record in listed]
    assert set(ids) == {first, second, other}
    assert ids == sorted(ids, reverse=True)

    filtered = RunListResponse.model_validate(client.get("/runs", params={"use_case": OTHER_ID}).json())
    assert [record.run_id for record in filtered.runs] == [other]

    by_mode = RunListResponse.model_validate(client.get("/runs", params={"mode": "score"}).json())
    assert by_mode.runs == ()

    limited = RunListResponse.model_validate(client.get("/runs", params={"limit": 1}).json())
    assert len(limited.runs) == 1


def test_run_history_skips_a_corrupt_record(client: TestClient, storage: LocalStorage) -> None:
    good = start_run(client).json()["run_id"]
    storage.write_text("runs/r_20260101_deadbeef/run.json", "{ not json")
    listed = RunListResponse.model_validate(client.get("/runs").json()).runs
    assert [record.run_id for record in listed] == [good]


def test_limit_above_the_maximum_is_422(client: TestClient) -> None:
    assert client.get("/runs", params={"limit": 101}).status_code == 422


# ---------------------------------------------------------------------------
# GET /runs/{id} and its artefacts
# ---------------------------------------------------------------------------
def test_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/runs/r_20260101_00000000")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_artefact_fetch_returns_the_stored_bytes(client: TestClient, storage: LocalStorage) -> None:
    run_id = start_run(client).json()["run_id"]
    response = client.get(f"/runs/{run_id}/artefacts/profile.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.content == storage.read_bytes(f"runs/{run_id}/profile.json")


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("leaderboard.json", "ARTEFACT_NOT_FOUND"),
        ("nonsense.json", "ARTEFACT_UNKNOWN"),
        ("Profile.JSON", "ARTEFACT_UNKNOWN"),
        ("profile.txt", "ARTEFACT_UNKNOWN"),
    ],
)
def test_artefact_whitelist(client: TestClient, name: str, code: str) -> None:
    run_id = start_run(client).json()["run_id"]
    response = client.get(f"/runs/{run_id}/artefacts/{name}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == code


def test_a_path_traversal_never_reaches_the_filesystem(client: TestClient) -> None:
    run_id = start_run(client).json()["run_id"]
    for name in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fpasswd"):
        response = client.get(f"/runs/{run_id}/artefacts/{name}")
        assert response.status_code == 404
        assert "passwd" not in response.text or response.json()["detail"]["code"] == "ARTEFACT_UNKNOWN"


def test_scores_csv_is_registered_and_404_until_m4(client: TestClient) -> None:
    run_id = start_run(client).json()["run_id"]
    response = client.get(f"/runs/{run_id}/scores.csv")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTEFACT_NOT_FOUND"


def test_artefacts_of_an_unknown_run_are_404(client: TestClient) -> None:
    response = client.get("/runs/r_20260101_00000000/artefacts/profile.json")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /runs/{id}/cancel
# ---------------------------------------------------------------------------
def test_cancel_a_pending_and_a_running_run(blocked_client: TestClient, storage: LocalStorage) -> None:
    running = start_run(blocked_client).json()["run_id"]
    pending = start_run(blocked_client).json()["run_id"]
    for run_id in (pending, running):
        response = blocked_client.post(f"/runs/{run_id}/cancel")
        assert response.status_code == 200, response.text
        assert response.json() == {"run_id": run_id, "cancelled": True, "state": "cancelled"}
        record = storage.read_model(f"runs/{run_id}/run.json", RunRecord)
        assert record.state is RunState.CANCELLED
        assert record.finished_at is not None
        detail = RunDetailResponse.model_validate(blocked_client.get(f"/runs/{run_id}").json())
        assert detail.status.state is RunState.CANCELLED
        assert detail.status.current_stage is None
        assert all(stage.state is not RunState.PENDING for stage in detail.status.stages)


def test_cancel_a_finished_run_is_false_and_echoes_its_terminal_state(client: TestClient) -> None:
    run_id = start_run(client).json()["run_id"]
    await_job(client, run_id)
    response = client.post(f"/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "cancelled": False, "state": "failed"}


def test_cancel_an_unknown_run_is_404(client: TestClient) -> None:
    response = client.post("/runs/r_20260101_00000000/cancel")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# The acceptance sweep over the committed generator, once ingest and validate land
# ---------------------------------------------------------------------------
TRAIN_VARIANTS = [name for name, code in VARIANTS.items() if VARIANT_SPECS[name].has_target]
WARNING_VARIANTS = frozenset({"constant_column", "high_null_column", "pii_column", "id_like_column"})

# A few variants only express themselves under a use case configured a particular way.
# TIME_COLUMN_UNPARSEABLE fires when the split is ordered by a time column, and the demo use case
# splits randomly: for it, a date column that will not parse is simply a string column and blocking
# the run would be wrong. So that variant is swept against a time-based use case instead.
VARIANT_USE_CASE = {"unparseable_time": "order-fulfillment"}


def use_case_for(variant: str) -> str:
    return VARIANT_USE_CASE.get(variant, DEMO_ID)


@pytest.mark.skipif(
    not (REAL_INGEST and REAL_VALIDATE),
    reason="engine.stages.ingest / engine.stages.validate have not landed yet",
)
@pytest.mark.parametrize("variant", TRAIN_VARIANTS)
def test_broken_fixture_returns_409_with_the_validation_payload(
    config_root: Path, data_dir: Path, variant: str
) -> None:
    """Design §6.9: every error-severity variant is refused with its code; warnings never block."""
    use_case = use_case_for(variant)
    frame = generate(GenerationSpec(use_case, variant=variant, config_root=config_root))
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    code = VARIANTS[variant]
    store = LocalStorage(data_dir)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        response = client.post(
            "/uploads",
            files={"file": (f"{variant}.csv", payload, "text/csv")},
            data={"use_case": use_case, "mode": "train"},
        )
        assert response.status_code == 201, response.text
        upload_id = response.json()["upload_id"]
        run = client.post("/runs", json=run_body(upload_id, use_case=use_case))

    if code is None or variant in WARNING_VARIANTS:
        assert run.status_code == 202, run.text
        report = store.read_model(f"runs/{run.json()['run_id']}/validation.json", ValidationReport)
        if code is not None:
            assert code in {check.code for check in report.checks}
        return

    assert run.status_code == 409, run.text
    body = run.json()
    assert body["detail"]["code"] == "VALIDATION_FAILED"
    report = ValidationReport.model_validate(body["validation"])
    assert report.passed is False
    assert report.error_count >= 1
    assert code in {check.code for check in report.checks}
    assert store.list_keys("runs/") == ()
