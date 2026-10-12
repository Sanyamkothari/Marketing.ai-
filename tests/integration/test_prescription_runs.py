"""A prescription replays at training and at scoring, through `POST /runs` and a scheduled firing (Plan O M243,
DEC-1813).

The plan's acceptance, where runs start:

* **A built dataset trains with its post-build recipe** (the gap in [INV] §8 is closed): `POST /runs` with a
  `dataset_id` bound to a prescription trains on the dataset as the post-build recipe prepares it, and the run keeps
  the recipe (so the model prepares every dataset it scores the same way), the prescription's reference and its money
  basis (M242's `save_money_basis`). A scheduled training firing does the same.
* **A file trains on its prescription's prepared copy**, after the intake, locale and currency checks; a file in a
  currency the prescription never saw stops with a 409 and its question.
* **Scoring checks the file against the prescription** the model was trained under: a new value in a closed column
  is a 409 `PRESCRIPTION_DRIFT` naming the column (and a firing's failure with that code); nothing is re-learnt.
* **Scoring with a single-file recipe is byte-identical to today**, with the switch on, and with a one-part
  prescription bound to the model.
* **With `data_doctor.unified` off nothing changes**: a bound dataset trains as built and the run holds no reference.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from api.main import create_app
from api.routes import runs
from engine.agent.config import AgentLevel
from engine.agent.contracts import (
    DATA_RECIPE_FILENAME,
    RECIPE_RECEIPT_FILENAME,
    DataRecipe,
    RecipeReceipt,
    RecipeStep,
    RecipeStepKind,
    recipe_hash,
)
from engine.config import RunMode, get_catalog, load_use_case, resolve_config
from engine.contracts import RunRecord
from engine.doctor import registry as doctor_registry
from engine.doctor.currency import MoneyBasis, MoneyColumn, load_money_basis
from engine.doctor.prescription import (
    FilePart,
    LocalePart,
    Prescription,
    Provenance,
    make_prescription,
    one_part,
    save_prescription,
)
from engine.doctor.prescription_contract import DataContract, learn_contract
from engine.doctor.prescription_runs import (
    PRESCRIPTION_CHECK_FILENAME,
    PRESCRIPTION_REF_FILENAME,
    PrescriptionRef,
    bind_dataset,
    bind_upload,
)
from engine.jobs import CancelToken
from engine.onboarding.datasets import DATASET_FRAME_FILENAME, dataset_key
from engine.onboarding.specs import DatasetManifest
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.runs import job_spec_key, read_job_spec
from engine.scheduling.firing import FiringError, start_dataset_run
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.agent.test_dataset_recipe_refusal import _fresh_registry, _seed
from tests.integration.test_runs_from_dataset import USE_CASE, _build
from tests.unit.production.scheduling_support import RecordingJobs

pytestmark = pytest.mark.integration

CASE = "case_00000000000000aa"
KEY = "entity_key"
TARGET = "churn_next_60d"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("prescription-runs")
    _, dataset_id, report = _build(root)
    assert report.passed
    return root, dataset_id


@pytest.fixture
def unified(monkeypatch: pytest.MonkeyPatch) -> None:
    """`data_doctor.unified: true` for every use case (the switch is read at call time)."""
    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: True)


def _frame(storage: LocalStorage, key: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(storage.read_bytes(key)))


def _recipe(column: str = "region", mapping: dict[str, JsonValue] | None = None) -> DataRecipe:
    given: dict[str, JsonValue] = mapping or {"North": "Northern"}
    steps = (RecipeStep(order=1, kind=RecipeStepKind.MAP_VALUES, column=column, params={"mapping": given}),)
    return DataRecipe(
        recipe_id="rc-post-build",
        use_case_id=USE_CASE,
        source_fingerprint="sha256:built",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=KEY,
        target=TARGET,
        levels=(AgentLevel.CLEAN,),
        max_failure_pct=5.0,
        created_at=utc_now(),
    )


BASIS = MoneyBasis(
    workspace_currency="INR", columns=(MoneyColumn(column="amount", written_in=("INR",), reported_in="INR"),)
)


def _prescribe(
    storage: LocalStorage,
    *,
    post_build: DataRecipe | None = None,
    contract: DataContract | None = None,
    files: tuple[FilePart, ...] = (),
    basis: MoneyBasis | None = BASIS,
) -> tuple[PrescriptionRef, Prescription]:
    made = make_prescription(
        files=files,
        post_build=post_build,
        contract=contract,
        locale=None if basis is None else LocalePart(money_basis=basis),
        provenance=Provenance(case_id=CASE, client_id="default", created_at=utc_now()),
    )
    save_prescription(storage, "default", CASE, made)
    return PrescriptionRef(client_id="default", case_id=CASE, prescription_hash=made.prescription_hash), made


def _client(root: Path) -> TestClient:
    app = create_app(data_dir=root / "data")
    app.state.jobs = RecordingJobs()
    return TestClient(app)


def _train(root: Path, dataset_id: str) -> Any:
    return _client(root).post("/runs", json={"use_case": USE_CASE, "mode": "train", "dataset_id": dataset_id})


def _fire(root: Path, dataset_id: str, mode: RunMode, version: Any = None) -> RunRecord:
    storage = LocalStorage(root / "data")
    manifest = storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)
    return start_dataset_run(
        storage=storage,
        registry=LocalModelRegistry(root / "data" / REGISTRY_FILENAME),
        jobs=RecordingJobs(),
        resolved=resolve_config(USE_CASE, {}),
        catalog=get_catalog(None),
        manifest=manifest,
        mode=mode,
        version=version,
        client_tag=None,
    )


def _unbind(storage: LocalStorage, dataset_id: str) -> None:
    key = dataset_key(dataset_id, PRESCRIPTION_REF_FILENAME)
    if storage.exists(key):
        storage.delete(key)


def _assert_trained_prepared(
    storage: LocalStorage, run_id: str, dataset_id: str, ref: PrescriptionRef
) -> None:
    spec = read_job_spec(storage, job_spec_key(run_id))
    assert spec.upload_key.startswith(f"datasets/{dataset_id}/prepared_"), "it trains on the prepared rows"
    built_rows = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME))
    prepared = _frame(storage, spec.upload_key)
    assert len(prepared) == len(built_rows) and "North" not in set(prepared["region"])
    assert (prepared["region"] == "Northern").sum() == (built_rows["region"] == "North").sum()
    record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    assert record.dataset_id == dataset_id, "the lineage is the dataset's"
    kept = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert kept.recipe_hash == _recipe().recipe_hash, "the model carries the post-build recipe to scoring"
    assert storage.read_model(run_key(run_id, RECIPE_RECEIPT_FILENAME), RecipeReceipt).rows_out == len(
        built_rows
    )
    assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref
    assert (
        load_money_basis(storage, run_id) == BASIS
    ), "save_money_basis on a prepared run, with the switch on"


# ---------------------------------------------------------------------------
# Training from a built dataset
# ---------------------------------------------------------------------------
def test_a_built_dataset_trains_with_its_post_build_recipe(built: tuple[Path, str], unified: None) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    ref, _ = _prescribe(storage, post_build=_recipe())
    bind_dataset(storage, dataset_id, ref)
    try:
        response = _train(root, dataset_id)
        assert response.status_code == 202, response.text
        _assert_trained_prepared(storage, response.json()["run_id"], dataset_id, ref)
    finally:
        _unbind(storage, dataset_id)


def test_a_scheduled_training_firing_trains_with_the_post_build_recipe(
    built: tuple[Path, str], unified: None
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    ref, _ = _prescribe(storage, post_build=_recipe())
    bind_dataset(storage, dataset_id, ref)
    try:
        record = _fire(root, dataset_id, RunMode.TRAIN)
        _assert_trained_prepared(storage, record.run_id, dataset_id, ref)
    finally:
        _unbind(storage, dataset_id)


def test_with_the_switch_off_a_bound_dataset_trains_as_built(built: tuple[Path, str]) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    ref, _ = _prescribe(storage, post_build=_recipe())
    bind_dataset(storage, dataset_id, ref)
    try:
        run_id = _train(root, dataset_id).json()["run_id"]
        spec = read_job_spec(storage, job_spec_key(run_id))
        assert spec.upload_key == dataset_key(dataset_id, DATASET_FRAME_FILENAME)
        for name in (PRESCRIPTION_REF_FILENAME, DATA_RECIPE_FILENAME, "money_basis.json"):
            assert not storage.exists(run_key(run_id, name)), name
        fired = _fire(root, dataset_id, RunMode.TRAIN)
        assert read_job_spec(storage, job_spec_key(fired.run_id)).upload_key == spec.upload_key
    finally:
        _unbind(storage, dataset_id)


def test_a_post_build_recipe_on_a_column_that_is_gone_is_prescription_drift(
    built: tuple[Path, str], unified: None
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    approved = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME)).assign(city="Pune")
    contract = learn_contract(approved, primary_key=KEY, target=TARGET)
    ref, _ = _prescribe(
        storage, post_build=_recipe(column="city", mapping={"Pune": "Poona"}), contract=contract
    )
    bind_dataset(storage, dataset_id, ref)
    try:
        response = _train(root, dataset_id)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert (detail["code"], detail["path"]) == ("PRESCRIPTION_DRIFT", "city")
        assert "called something else now?" in detail["message"], "it asks; it never re-learns"
        with pytest.raises(FiringError) as failed:
            _fire(root, dataset_id, RunMode.TRAIN)
        assert failed.value.code == "PRESCRIPTION_DRIFT"
    finally:
        _unbind(storage, dataset_id)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _score(root: Path, dataset_id: str) -> Any:
    return _client(root).post("/runs", json={"use_case": USE_CASE, "mode": "score", "dataset_id": dataset_id})


def _model_under(root: Path, ref: PrescriptionRef | None, recipe: DataRecipe) -> Any:
    _fresh_registry(root / "data")
    version = _seed(root / "data", "r_prescribed", "m_prescribed", recipe=recipe)
    storage = LocalStorage(root / "data")
    key = run_key(version.run_id, PRESCRIPTION_REF_FILENAME)
    if ref is not None:
        storage.write_model(key, ref)
    elif storage.exists(key):
        storage.delete(key)
    return version


def test_scoring_a_dataset_with_a_value_the_prescription_never_saw_stops_with_prescription_drift(
    built: tuple[Path, str], unified: None
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    prepared = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME)).replace(
        {"region": {"North": "Northern"}}
    )
    approved = prepared[prepared["region"] != "West"]  # at approval the file had no West
    ref, _ = _prescribe(
        storage, post_build=_recipe(), contract=learn_contract(approved, primary_key=KEY, target=TARGET)
    )
    version = _model_under(root, ref, _recipe())
    response = _score(root, dataset_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["path"]) == ("PRESCRIPTION_DRIFT", "region")
    assert "What do they mean?" in detail["message"]
    with pytest.raises(FiringError) as failed:
        _fire(root, dataset_id, RunMode.SCORE, version)
    assert failed.value.code == "PRESCRIPTION_DRIFT"


def test_scoring_a_dataset_that_matches_its_prescription_keeps_the_reference_and_the_basis(
    built: tuple[Path, str], unified: None
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    prepared = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME)).replace(
        {"region": {"North": "Northern"}}
    )
    ref, _ = _prescribe(
        storage, post_build=_recipe(), contract=learn_contract(prepared, primary_key=KEY, target=TARGET)
    )
    _model_under(root, ref, _recipe())
    response = _score(root, dataset_id)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref
    assert load_money_basis(storage, run_id) == BASIS
    assert not storage.exists(run_key(run_id, PRESCRIPTION_CHECK_FILENAME)), "no warning, no check file"


def _prepared_bytes(root: Path, dataset_id: str) -> tuple[bytes, dict[str, Any]]:
    response = _score(root, dataset_id)
    assert response.status_code == 202, response.text
    storage = LocalStorage(root / "data")
    run_id = response.json()["run_id"]
    spec = read_job_spec(storage, job_spec_key(run_id))
    receipt = storage.read_model(run_key(run_id, RECIPE_RECEIPT_FILENAME), RecipeReceipt)
    return storage.read_bytes(spec.upload_key), receipt.model_dump(exclude={"created_at"})


def test_scoring_with_a_single_file_recipe_is_byte_identical_with_the_switch_on(
    built: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, dataset_id = built
    recipe = _recipe()
    _model_under(root, None, recipe)
    today = _prepared_bytes(root, dataset_id)
    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: True)
    assert _prepared_bytes(root, dataset_id) == today, "a model with no prescription scores exactly as today"
    storage = LocalStorage(root / "data")
    single = one_part(recipe)
    save_prescription(storage, "default", CASE, single)
    _model_under(
        root,
        PrescriptionRef(client_id="default", case_id=CASE, prescription_hash=single.prescription_hash),
        recipe,
    )
    assert (
        _prepared_bytes(root, dataset_id) == today
    ), "nor does one whose recipe reads as a one-part prescription"


# ---------------------------------------------------------------------------
# Training from a file
# ---------------------------------------------------------------------------
@pytest.fixture
def api(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    """Real uploads, recipes and validation; a run's job does nothing, so nothing trains."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    data_dir = tmp_path / "data"
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield client, data_dir


def _history(built: tuple[Path, str], amount: str) -> pd.DataFrame:
    root, dataset_id = built
    rows = _frame(LocalStorage(root / "data"), dataset_key(dataset_id, DATASET_FRAME_FILENAME))
    history = rows.sample(n=1_500, replace=True, random_state=243).reset_index(drop=True)
    history[KEY] = [f"C{n}" for n in np.arange(5_000_000, 5_001_500)]
    history[TARGET] = np.random.default_rng(243).integers(0, 2, size=len(history))
    history["amount"] = [f"{amount} {100 + n % 900}" for n in range(len(history))]
    return history


def _upload(client: TestClient, frame: pd.DataFrame) -> str:
    sent = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert sent.status_code == 201, sent.text
    return str(sent.json()["upload_id"])


def _file_part(upload_id: str) -> FilePart:
    return FilePart(slot="history", upload_ids=(upload_id,), recipe=_recipe())


def test_an_upload_trains_on_its_prescriptions_prepared_copy(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    upload_id = _upload(client, _history(built, "₹"))
    ref, _ = _prescribe(storage, files=(_file_part(upload_id),))
    bind_upload(storage, upload_id, ref)
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    assert record.upload_id != upload_id, "it trains on the prepared copy, written beside the file as sent"
    assert (
        storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe).recipe_hash
        == _recipe().recipe_hash
    )
    assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref
    assert load_money_basis(storage, run_id) == BASIS
    prepared = _frame(storage, read_job_spec(storage, job_spec_key(run_id)).upload_key)
    assert "North" not in set(prepared["region"]) and "Northern" in set(prepared["region"])


def test_an_upload_in_a_currency_its_prescription_never_saw_stops_and_asks(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    upload_id = _upload(client, _history(built, "$"))
    ref, _ = _prescribe(storage, files=(_file_part(upload_id),))
    bind_upload(storage, upload_id, ref)
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["path"]) == ("CURRENCY_FOREIGN", "amount")
    assert "never added" in detail["message"]
    assert not storage.list_keys("runs/"), "a stopped run leaves nothing behind"


def test_with_the_switch_off_a_bound_upload_trains_as_sent(
    api: tuple[TestClient, Path], built: tuple[Path, str]
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    upload_id = _upload(client, _history(built, "$"))
    ref, _ = _prescribe(storage, files=(_file_part(upload_id),))
    bind_upload(storage, upload_id, ref)
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert storage.read_model(run_key(run_id, "run.json"), RunRecord).upload_id == upload_id
    assert not storage.exists(run_key(run_id, PRESCRIPTION_REF_FILENAME))
    assert load_use_case(USE_CASE).data_doctor.unified is False


def test_an_upload_whose_prepared_rows_break_the_contract_stops_and_leaves_no_copy(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    history = _history(built, "₹")
    upload_id = _upload(client, history)
    rare = str(history["plan_type"].value_counts().index[-1])
    approved = history[history["plan_type"] != rare].replace({"region": {"North": "Northern"}})
    contract = learn_contract(approved, primary_key=KEY, target=TARGET)
    part = _file_part(upload_id).model_copy(update={"contract": contract})
    ref, _ = _prescribe(storage, files=(part,))
    bind_upload(storage, upload_id, ref)
    before = set(storage.list_keys("uploads/"))
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["path"]) == ("PRESCRIPTION_DRIFT", "plan_type")
    assert set(storage.list_keys("uploads/")) == before, "the prepared copy of a stopped run is removed"
