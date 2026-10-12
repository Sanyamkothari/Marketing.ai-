"""Plan O M234 acceptance: the readiness report agrees with `POST /runs`'s own `ValidationReport` on every
code they share (DEC-1804 (d)).

Each file is uploaded through the real app and sent to `POST /runs`; the report `POST /runs` wrote
(`uploads/<id>/validation.json`, the body of its 409) is compared with the readiness report computed from
the same stored upload by `inputs_for_upload`. The training job itself is stubbed: what is compared is
decided before a job is submitted.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from engine.config import RunMode, resolve_config
from engine.contracts import ValidationReport
from engine.doctor.readiness import check_readiness, inputs_for_upload
from engine.storage import LocalStorage, upload_key
from tests.unit.doctor.readiness_fixtures import customers

USE_CASE = "targeted-advertisement"


@pytest.fixture
def client(config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def no_training(*_args: Any, **_kwargs: Any) -> Any:
        return lambda *a, **k: None  # what is compared is decided before a job is submitted

    monkeypatch.setattr(runs, "build_job_fn", no_training)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _files() -> dict[str, tuple[pd.DataFrame, dict[str, Any]]]:
    plain = customers(2000, seed=3)
    small = customers(600, seed=4)
    repeated = customers(2000, seed=5)
    repeated.loc[:99, "customer_id"] = repeated.loc[100:199, "customer_id"].to_numpy()
    leak = customers(2000, seed=6)
    leak["converted_copy"] = leak["converted_30d"]
    no_opt_in = customers(2000, seed=7).drop(columns=["marketing_opt_in"])
    no_opt_in["notes_email"] = [f"user{i}@example.com" for i in range(len(no_opt_in))]
    consent = customers(2000, seed=8)
    consent["marketing_opt_in"] = np.where(np.arange(len(consent)) % 3 == 0, "Denied", "Granted")
    return {
        "passes": (plain, {}),
        "too_few_rows": (small, {}),
        "repeated_ids": (repeated, {}),
        "a_leak": (leak, {}),
        "warnings_only": (no_opt_in, {}),
        "consent_words": (consent, {"governance.consent_column": "marketing_opt_in"}),
    }


def _upload(client: TestClient, frame: pd.DataFrame) -> str:
    response = client.post(
        "/uploads",
        files={"file": ("export.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


@pytest.mark.parametrize("name", sorted(_files()))
def test_the_report_and_post_runs_agree_on_every_shared_code(
    name: str, client: TestClient, tmp_path: Path, config_root: Path
) -> None:
    frame, overrides = _files()[name]
    upload_id = _upload(client, frame)
    body = {
        "use_case": USE_CASE,
        "mode": "train",
        "upload_id": upload_id,
        "primary_key": "customer_id",
        "target": "converted_30d",
        "overrides": overrides,
    }
    response = client.post("/runs", json=body)
    assert response.status_code in (202, 409), response.text
    storage = LocalStorage(tmp_path / "data")
    theirs = storage.read_model(upload_key(upload_id, "validation.json"), ValidationReport)
    if response.status_code == 409:
        assert ValidationReport.model_validate(response.json()["validation"]).checks == theirs.checks

    cfg = resolve_config(USE_CASE, overrides, root=config_root).config
    ours = check_readiness(
        inputs_for_upload(
            storage,
            upload_id,
            config=cfg,
            mode=RunMode.TRAIN,
            primary_key="customer_id",
            target="converted_30d",
            resolved_overrides=overrides,
        )
    )
    assert ours.dry_run_report is not None
    assert ours.dry_run_passed == theirs.passed == (response.status_code == 202)

    def table(checks: Any) -> set[tuple[str, str | None, str, bool]]:
        return {(c.code, c.column, c.severity.value, c.acknowledged) for c in checks}

    assert table(ours.dry_run_report.checks) == table(theirs.checks)
    shared = {check.code for check in theirs.checks}
    for requirement in ours.requirements:
        if requirement.engine_code in shared:
            matching = [
                c for c in theirs.checks if c.code == requirement.id and c.column == requirement.column
            ]
            assert matching, requirement
            expected = (
                "acknowledged"
                if matching[0].acknowledged
                else {"error": "fail", "warning": "warn", "info": "pass"}[matching[0].severity.value]
            )
            assert requirement.status == expected, requirement
    if not theirs.passed:
        assert ours.verdict == "not_ready"
    if name == "consent_words":
        assert (
            theirs.passed and ours.verdict == "not_ready"
        )  # the gap: validation passes, training has no rows
        assert ours.probe("X_CONSENT_VOCAB").status == "fail"
    if name == "passes":
        assert ours.verdict == "ready", ours.verdict_reason
        assert ours.probe("X_PARSER_ROW_LOSS").status == "pass"
        assert ours.source.rows_physical == 2000
