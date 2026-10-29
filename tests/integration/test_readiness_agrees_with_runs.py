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
from engine.doctor.currency import MoneyBasis, MoneyColumn
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


# -- review fixes (M234): what POST /runs refuses before it validates -----------------------------------
def test_a_prepared_upload_run_with_other_roles_is_refused_by_both(
    client: TestClient, tmp_path: Path, config_root: Path
) -> None:
    """A prepared copy checked with another outcome than its recipe's: 409 RECIPE_ROLES_MISMATCH, not ready."""
    from api.routes.agent_recipes import write_derived_upload
    from api.routes.uploads import load_upload
    from engine.config import load_use_case
    from tests.integration.agent.test_recipe_runs import KEY, TARGET, _messy, _recipe

    storage = LocalStorage(tmp_path / "data")
    frame = _messy()
    original = load_upload(storage, _upload(client, frame))
    derived = write_derived_upload(storage, load_use_case(USE_CASE, config_root), original, _recipe(frame))
    upload_id = derived.record.upload_id
    cfg = resolve_config(USE_CASE, {}, root=config_root).config
    for target, refused in (("marketing_opt_in", True), (TARGET, False)):
        response = client.post(
            "/runs",
            json={
                "use_case": USE_CASE,
                "mode": "train",
                "upload_id": upload_id,
                "primary_key": KEY,
                "target": target,
            },
        )
        ours = check_readiness(
            inputs_for_upload(
                storage, upload_id, config=cfg, mode=RunMode.TRAIN, primary_key=KEY, target=target
            )
        )
        if refused:
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "RECIPE_ROLES_MISMATCH"
            assert not ours.dry_run_passed and ours.verdict == "not_ready"
            failing = [r for r in ours.requirements if r.status == "fail"]
            assert [(r.id, r.gate) for r in failing] == [("RECIPE_ROLES_MISMATCH", "recipe_replay")]
            assert "RECIPE_ROLES_MISMATCH" in ours.verdict_reason
        else:
            assert response.status_code == 202, response.text
            assert ours.dry_run_passed, ours.verdict_reason
            assert ours.prepared is not None and ours.prepared.source_upload_id == original.upload_id
    # The copy is Parquet (no line to lose); the file the recipe read is reported as that file's facts.
    assert ours.source.rows_physical is None and ours.source.rows_dropped_by_parser is None
    assert ours.source.rows_parsed == derived.profile.row_count
    assert ours.source.original_upload_id == original.upload_id
    assert ours.source.original_rows_physical == len(frame) == ours.source.original_rows_parsed
    assert ours.probe("X_PARSER_ROW_LOSS").details["of_the_original_file"] is True


def test_a_scoring_report_checks_the_copy_the_models_recipe_prepares(
    client: TestClient, tmp_path: Path, config_root: Path
) -> None:
    """Scoring: the model's recipe is replayed as POST /runs and its dry run replay it, then checked."""
    from tests.integration.agent.test_recipe_runs import KEY, _messy, _recipe, _seed_model

    data_dir = tmp_path / "data"
    money = MoneyBasis(
        workspace_currency="INR",
        columns=(MoneyColumn(column="ad_ctr_90d", written_in=("INR",), reported_in="INR"),),
    )
    recipe = _recipe(_messy(), target=None).model_copy(update={"money_basis": money.model_dump(mode="json")})
    version = _seed_model(data_dir, recipe)
    storage = LocalStorage(data_dir)
    cfg = resolve_config(USE_CASE, {}, root=config_root).config

    def scoring(frame: pd.DataFrame) -> str:
        response = client.post(
            "/uploads",
            files={"file": ("scoring.csv", frame.to_csv(index=False).encode(), "text/csv")},
            data={"use_case": USE_CASE, "mode": "score"},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["upload_id"])

    fits = scoring(_messy("scoring", rows=600))
    broken = scoring(_messy("scoring", rows=600).rename(columns={"ad_ctr_90d": "ctr_90d"}))
    dollars = _messy("scoring", rows=600)
    dollars["ad_ctr_90d"] = "$12.0"
    foreign = scoring(dollars)
    for upload_id, key, expected in (
        (fits, KEY, None),
        (broken, KEY, "RECIPE_COLUMN_MISSING"),
        (foreign, KEY, "CURRENCY_FOREIGN"),
        (fits, "snapshot_date", "RECIPE_ROLES_MISMATCH"),
    ):
        ours = check_readiness(
            inputs_for_upload(
                storage,
                upload_id,
                config=cfg,
                mode=RunMode.SCORE,
                primary_key=key,
                target=None,
                model_version=version,
            )
        )
        assert ours.mode == "score"
        if expected is None:
            assert ours.dry_run_passed, ours.verdict_reason
            assert ours.dry_run_report is not None
            # the recipe's steps ran on this file, in memory: the report is about the prepared rows
            assert ours.prepared is not None and ours.prepared.prepares_like_model_recipe is False
            assert [fix.step_kind for fix in ours.fixes_applied] == ["parse_number", "normalise_text"]
            assert ours.scoring_contract is not None and ours.scoring_contract.type_changed == ()
        elif expected == "RECIPE_ROLES_MISMATCH":
            assert not ours.dry_run_passed and ours.verdict == "not_ready"
            assert [r.id for r in ours.requirements if r.status == "fail"] == [expected]
        else:
            assert not ours.dry_run_passed and ours.verdict == "not_ready"
            assert [(r.id, r.column, r.gate) for r in ours.requirements if r.status == "fail"] == [
                (expected, "ad_ctr_90d", "recipe_replay")
            ]
