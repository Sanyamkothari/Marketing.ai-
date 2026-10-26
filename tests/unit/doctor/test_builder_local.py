"""`LocalBuilderPort` (v3 phase 0, B6): every refusal of a route it calls is a `BuildDriveError` with the route's code.

The routes are stubbed in their own modules (the port imports them inside each call), so these run in a moment. The
real routes, with a build an audience leaves empty, are the last two tests; the happy path on the real sample is
`tests/integration/test_order_log_workflow.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from engine.contracts import RunState, Severity
from engine.doctor.build_driver import BuildDriveError
from engine.doctor.builder_local import InlineJobs, LocalBuilderPort
from engine.jobs import CancelToken
from engine.onboarding.datasets import DATASET_REPORT_FILENAME, dataset_key
from engine.onboarding.specs import BuildReport, OnboardingCheck
from engine.storage import LocalStorage

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def port(tmp_path: Path) -> LocalBuilderPort:
    """A port over plain local storage; the client store and the request are never reached by a stubbed route."""
    return LocalBuilderPort(
        client_id="cl_1",
        storage=LocalStorage(tmp_path / "data"),
        store=None,  # type: ignore[arg-type]
        config_root=tmp_path / "configs",
        request=None,  # type: ignore[arg-type]
    )


def refused(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "path": None})


def test_a_refused_upload_is_the_routes_code_and_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api.routes.sources as sources

    async def create_source(*_: Any, **__: Any) -> Any:
        raise refused(415, "UNSUPPORTED_FORMAT", "Not a table.")

    monkeypatch.setattr(sources, "create_source", create_source)
    with pytest.raises(BuildDriveError) as raised:
        port(tmp_path).add_source(file_name="log.pdf", data=b"x", role="orders")
    assert raised.value.code == "UNSUPPORTED_FORMAT"
    assert "Adding log.pdf was refused (415): Not a table." in str(raised.value)


def test_a_refused_build_request_is_the_routes_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import api.routes.datasets as datasets

    def create_dataset(*_: Any, **__: Any) -> Any:
        raise refused(404, "SPEC_NOT_FOUND", "No such recipe.")

    monkeypatch.setattr(datasets, "create_dataset", create_dataset)
    with pytest.raises(BuildDriveError) as raised:
        port(tmp_path).build(spec_id="spec_1", mode="train")
    assert raised.value.code == "SPEC_NOT_FOUND" and "(404)" in str(raised.value)


def test_a_build_the_checks_stop_is_the_first_blocking_checks_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import api.routes.datasets as datasets

    checks = [
        {"code": "MAPPING_LOW_CONFIDENCE", "severity": "warning", "message": "Unsure."},
        {"code": "ENTITY_KEY_UNMAPPED", "severity": "error", "message": "No customer ID column."},
    ]
    monkeypatch.setattr(
        datasets, "create_dataset", lambda *_, **__: JSONResponse(status_code=409, content={"checks": checks})
    )
    with pytest.raises(BuildDriveError) as raised:
        port(tmp_path).build(spec_id="spec_1", mode="train")
    assert raised.value.code == "ENTITY_KEY_UNMAPPED"
    assert "(409): No customer ID column." in str(raised.value)
    monkeypatch.setattr(
        datasets, "create_dataset", lambda *_, **__: JSONResponse(status_code=409, content={"checks": []})
    )
    with pytest.raises(BuildDriveError) as unnamed:
        port(tmp_path).build(spec_id="spec_1", mode="train")
    assert unnamed.value.code == "VALIDATION_FAILED"


def test_inline_jobs_run_on_the_calling_thread_and_record_a_failure_by_class_name() -> None:
    jobs = InlineJobs()
    ran: list[bool] = []

    def ok(_: CancelToken) -> None:
        ran.append(True)

    def boom(_: CancelToken) -> None:
        raise RuntimeError("a value from a cell")

    assert jobs.submit("j1", ok).state is RunState.DONE and ran == [True]
    failed = jobs.submit("j2", boom)
    assert failed.state is RunState.FAILED and failed.error == "RuntimeError"
    assert jobs.status("j2") is failed and jobs.cancel("j2") is False


def report(dataset_id: str, *checks: OnboardingCheck) -> BuildReport:
    errors = sum(check.severity is Severity.ERROR for check in checks)
    return BuildReport(
        dataset_id=dataset_id,
        checks=checks,
        sources=(),
        snapshots=(),
        features=(),
        rows_out=0,
        entities_out=0,
        duration_s=0.0,
        features_sql_path=None,
        error_count=errors,
        warning_count=len(checks) - errors,
        passed=errors == 0,
        built_at=NOW,
    )


def test_a_build_that_left_no_report_or_an_unreadable_one_is_build_step_failed(tmp_path: Path) -> None:
    local = port(tmp_path)
    with pytest.raises(BuildDriveError) as missing:
        local.read_dataset("ds_1")
    assert missing.value.code == "BUILD_STEP_FAILED" and "left no report" in str(missing.value)
    # A report a reader cannot validate (a feature's share of blanks written as null) is the port's error too.
    body = json.loads(report("ds_2").model_dump_json())
    body["features"] = [{"name": "orders_total", "null_fraction": None}]
    local.storage.write_bytes(dataset_key("ds_2", DATASET_REPORT_FILENAME), json.dumps(body).encode())
    with pytest.raises(BuildDriveError) as unreadable:
        local.read_dataset("ds_2")
    assert unreadable.value.code == "BUILD_STEP_FAILED" and "cannot be read back" in str(unreadable.value)


def test_a_build_that_did_not_pass_is_its_first_blocking_check(tmp_path: Path) -> None:
    local = port(tmp_path)
    stopped = report(
        "ds_3",
        OnboardingCheck(code="TOO_LITTLE_HISTORY", severity=Severity.WARNING, message="Short."),
        OnboardingCheck(code="AUDIENCE_EMPTY", severity=Severity.ERROR, message="No one is in the audience."),
    )
    local.storage.write_model(dataset_key("ds_3", DATASET_REPORT_FILENAME), stopped)
    with pytest.raises(BuildDriveError) as raised:
        local.read_dataset("ds_3")
    assert raised.value.code == "AUDIENCE_EMPTY"
    assert "AUDIENCE_EMPTY: No one is in the audience." in str(raised.value)
    assert "TOO_LITTLE_HISTORY" not in str(raised.value)


@pytest.fixture(scope="module")
def nobody_lapsed(config_root: Path, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """The synthetic log's plan with an audience no one is in (no order in ten years), and a port over a real app."""
    from api.main import create_app
    from api.routes.clients import get_client_store
    from api.routes.schedules import app_request
    from engine.config import get_roles, load_use_case
    from engine.doctor.build_driver import OrderLogInput, plan_build
    from engine.doctor.config import AudienceConfig
    from engine.doctor.contracts import Diagnosis
    from engine.doctor.diagnose import diagnose_file
    from engine.doctor.order_roll_up import find_log_columns
    from tests.fixtures.order_log import order_log_config_root, order_log_frame
    from tests.unit.doctor.order_log_support import USE_CASE, loaded

    tmp = tmp_path_factory.mktemp("nobody")
    root = order_log_config_root(config_root, tmp)
    log = order_log_frame(customers=150)
    file = loaded(log, "online-retail.csv", root)
    config = load_use_case(USE_CASE, root)
    diagnosis = diagnose_file(file, config, get_roles(root))
    plan = plan_build(
        Diagnosis(files=(diagnosis,), use_cases=(), links=(), stacks=()),
        {file.upload_id: log},
        use_case=USE_CASE,
        label_spec=config.order_log_label,
        order_log=OrderLogInput(
            file.upload_id,
            find_log_columns(log, customer="CustomerID", order="InvoiceNo", date="InvoiceDate"),
        ),
        suggested=config.order_log_features,
        audience=AudienceConfig(role="orders", no_event_days=3650),
    )
    assert plan.blocking == ()
    app = create_app(config_root=root, data_dir=tmp / "data")
    client_id = get_client_store(app_request(app)).create_client("Gifts Co", "ecommerce", notes="").client_id
    return plan, {file.upload_id: log}, LocalBuilderPort.for_app(app, client_id)


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["train", "score"])
def test_an_audience_that_keeps_no_one_stops_the_build_with_its_own_code(nobody_lapsed, mode: str) -> None:  # type: ignore[no-untyped-def]
    """Before the stop, an empty spine wrote a report with a null share of blanks per feature that no reader could
    read back (a raw pydantic error). Now the build stops at the snapshots, the report reads, and the code says why.
    """
    from engine.doctor.build_driver import approval_for, drive_builder

    plan, frames, local = nobody_lapsed
    driven = drive_builder(plan, frames, local, approval_for(plan, user_id="analyst-7", at=NOW), mode=mode)
    assert driven.dataset_id is not None
    with pytest.raises(BuildDriveError) as raised:
        local.read_dataset(driven.dataset_id)
    assert raised.value.code == "AUDIENCE_EMPTY"
    written = local.storage.read_model(dataset_key(driven.dataset_id, DATASET_REPORT_FILENAME), BuildReport)
    assert not written.passed and [c.code for c in written.checks if c.severity is Severity.ERROR] == [
        "AUDIENCE_EMPTY"
    ]
    message = next(c.message for c in written.checks if c.code == "AUDIENCE_EMPTY")
    assert ("on the scoring date" if mode == "score" else "at any snapshot date") in message


class ReAimed:
    """The port, with the recipe's audience pointed at another table as it is saved (as an API client could)."""

    def __init__(self, port: LocalBuilderPort, role: str) -> None:
        self._port = port
        self._role = role

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def save_spec(self, body: Any) -> str:
        return self._port.save_spec({**body, "audience": {**body["audience"], "role": self._role}})


@pytest.mark.slow
@pytest.mark.parametrize(("role", "mapped"), [("entity", True), ("activity", False)])
def test_an_audience_on_a_table_with_no_event_time_or_none_mapped_stops_with_its_own_code(  # type: ignore[no-untyped-def]
    nobody_lapsed, role: str, mapped: bool
) -> None:
    from engine.doctor.build_driver import approval_for, drive_builder

    plan, frames, local = nobody_lapsed
    aimed = ReAimed(local, role)
    driven = drive_builder(plan, frames, aimed, approval_for(plan, user_id="analyst-7", at=NOW))  # type: ignore[arg-type]
    assert driven.dataset_id is not None
    with pytest.raises(BuildDriveError) as raised:
        local.read_dataset(driven.dataset_id)
    assert raised.value.code == "AUDIENCE_ROLE_MISSING"
    written = local.storage.read_model(dataset_key(driven.dataset_id, DATASET_REPORT_FILENAME), BuildReport)
    check = next(c for c in written.checks if c.code == "AUDIENCE_ROLE_MISSING")
    assert check.details == {"role": role, "mapped": mapped}
    assert ("has no event time" if mapped else "has not been mapped") in check.message
