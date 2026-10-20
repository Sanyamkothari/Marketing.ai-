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
* **With `data_doctor.unified` off nothing changes**: a bound dataset trains as built and the run holds no reference,
  and a training upload is read no extra time.

The review fixes (DEC-1813 (q)-(w)):

* **The approved outcome is applied at training** (a file's and a dataset's): Churned is 1, Active 0, Suspended left
  out and counted; the run carries the recipe alone to scoring.
* **A file or a dataset made after PRESCRIBE follows the prescription**: a scheduled training firing on a fresh build
  of the prescribed spec, and next month's upload of a use case whose champion was trained under one.
* **Rows no recipe prepares are checked against the contract at training** (a dataset with no post-build recipe, a
  file with no recipe), as at scoring.
* **A switch-on run a Guided-setup recipe prepared keeps that recipe's money basis** (a conversion's frozen rate).
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
from engine.doctor.outcome_definition import OutcomeDefinition, ValueMeaning, approval_for
from engine.doctor.prescription import (
    BuildPart,
    CasePrescriber,
    FilePart,
    GoalSection,
    LocalePart,
    OutcomePart,
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
    bound,
    dataset_for_training,
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
    goals: tuple[GoalSection, ...] = (),
    build: BuildPart | None = None,
) -> tuple[PrescriptionRef, Prescription]:
    made = make_prescription(
        files=files,
        build=build,
        post_build=post_build,
        contract=contract,
        goals=goals,
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


# ---------------------------------------------------------------------------
# Review fixes: the approved outcome at training
# ---------------------------------------------------------------------------
STATUS = ("Active", "Churned", "Suspended")


def _outcome_goal() -> GoalSection:
    """The churn goal's approved outcome from a column's values (M239): Churned yes, Active no, Suspended left out."""
    definition = OutcomeDefinition(
        kind="column_values",
        name=TARGET,
        column=TARGET,
        map=(
            ValueMeaning(value="Active", meaning="no"),
            ValueMeaning(value="Churned", meaning="yes"),
            ValueMeaning(value="Suspended", meaning="exclude"),
        ),
        exclude=("Suspended",),
    )
    approval = approval_for(definition, f"The model learns '{TARGET}'.", user_id="analyst", at=utc_now())
    return GoalSection(
        goal_id=USE_CASE, active=True, outcome=OutcomePart(definition=definition, approval=approval)
    )


def _with_status(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out[TARGET] = [STATUS[n % 3] for n in range(len(out))]
    return out


def test_an_upload_trains_on_its_approved_outcome_with_the_left_out_rows_removed(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    history = _with_status(_history(built, "₹"))
    upload_id = _upload(client, history)
    ref, _ = _prescribe(storage, files=(_file_part(upload_id),), goals=(_outcome_goal(),))
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
    prepared = _frame(storage, read_job_spec(storage, job_spec_key(run_id)).upload_key)
    kept = history[history[TARGET] != "Suspended"]
    assert set(prepared[TARGET]) == {0, 1}, "it trains on the approved outcome, never on the raw words"
    assert len(prepared) == len(kept), "the rows left out are removed"
    assert int(prepared[TARGET].sum()) == int((kept[TARGET] == "Churned").sum())
    assert "North" not in set(prepared["region"]), "the file slot's recipe ran first"
    kept_recipe = storage.read_model(run_key(run_id, DATA_RECIPE_FILENAME), DataRecipe)
    assert (
        kept_recipe.recipe_hash == _recipe().recipe_hash
    ), "scoring replays the recipe alone, never the outcome"
    assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref


def test_a_dataset_trains_on_its_approved_outcome_and_carries_only_the_post_build_recipe(
    tmp_path: Path, unified: None
) -> None:
    storage = LocalStorage(tmp_path / "data")
    dataset_id = "ds_outcome"
    rows = _with_status(
        pd.DataFrame(
            {
                KEY: [f"C{n}" for n in range(300)],
                "region": [("North", "South", "East")[n % 3 - 1] for n in range(300)],
                "tenure": np.arange(300) % 40,
            }
        )
    )
    with storage.open_write(dataset_key(dataset_id, DATASET_FRAME_FILENAME)) as sink:
        rows.to_parquet(sink, index=False)
    ref, _ = _prescribe(storage, post_build=_recipe(), goals=(_outcome_goal(),))
    bind_dataset(storage, dataset_id, ref)
    training = dataset_for_training(storage, load_use_case(USE_CASE), dataset_id, (KEY,))
    assert training is not None and training.rows is not None
    assert training.rows.source_key.startswith(f"datasets/{dataset_id}/prepared_rx_")
    prepared = _frame(storage, training.rows.source_key)
    assert set(prepared[TARGET]) == {0, 1} and len(prepared) == 200, "Suspended is left out"
    assert "North" not in set(prepared["region"]), "then the post-build recipe"
    assert training.outcome is not None and training.outcome.excluded == 100
    assert training.recipe is not None and training.recipe.recipe_hash == _recipe().recipe_hash
    assert training.receipt is not None, "the run carries the post-build recipe and what it did"
    assert _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME)).equals(
        rows
    ), "the build is untouched"


# ---------------------------------------------------------------------------
# Review fixes: files and datasets made after PRESCRIBE
# ---------------------------------------------------------------------------
def _build_record() -> Any:
    from engine.config import LabelDefinition, LabelType
    from engine.doctor.build_driver import approval_for as build_approval
    from engine.doctor.build_driver import drive_builder, plan_build
    from tests.unit.doctor.m237_support import RecordingBuilder, diagnosis, frames

    label = LabelDefinition(
        name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="bills", horizon_days=60
    )
    loaded = frames()
    plan = plan_build(diagnosis(loaded), loaded, use_case="rca", label_spec=label)
    port = RecordingBuilder()
    drive_builder(plan, loaded, port, build_approval(plan, user_id="analyst", at=utc_now()))
    return port.records["spec_1"]


class _Case:
    """What PRESCRIBE reads of a case to bind it."""

    client_id = "default"
    case_id = CASE

    def upload_ids(self) -> tuple[str, ...]:
        return ()


def test_a_scheduled_training_firing_on_a_fresh_build_of_the_prescribed_spec_follows_the_prescription(
    built: tuple[Path, str], unified: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    manifest = storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)
    made = make_prescription(
        build=BuildPart(record=_build_record(), dataset_id=dataset_id),
        post_build=_recipe(),
        locale=LocalePart(money_basis=BASIS),
        provenance=Provenance(case_id=CASE, client_id="default", created_at=utc_now()),
    )
    monkeypatch.setattr(CasePrescriber, "prescription", lambda self, case, brief: made)
    written = CasePrescriber(storage)(_Case(), None)  # type: ignore[arg-type]
    ref = PrescriptionRef(client_id="default", case_id=CASE, prescription_hash=written)
    spec = (manifest.client_id, manifest.spec_id)
    spec_key = f"cases/{manifest.client_id}/prescribed_specs/{manifest.spec_id}.json"
    try:
        assert bound(storage, spec=spec) == ref, "PRESCRIBE binds the spec that built the approved dataset"
        # A firing's fresh build of that spec is another dataset: bound to nothing itself.
        _unbind(storage, dataset_id)
        record = _fire(root, dataset_id, RunMode.TRAIN)
        _assert_trained_prepared(storage, record.run_id, dataset_id, ref)
        response = _train(root, dataset_id)
        assert response.status_code == 202, response.text
        _assert_trained_prepared(storage, response.json()["run_id"], dataset_id, ref)
    finally:
        _unbind(storage, dataset_id)
        if storage.exists(spec_key):
            storage.delete(spec_key)


def test_next_months_upload_follows_the_prescription_its_champion_was_trained_under(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    ref, _ = _prescribe(storage, files=(_file_part("u_september"),))
    _seed(data_dir, "r_champion", "m_champion", recipe=None)
    storage.write_model(run_key("r_champion", PRESCRIPTION_REF_FILENAME), ref)
    upload_id = _upload(client, _history(built, "₹"))  # next month's file: bound to nothing
    assert bound(storage, upload_id=upload_id) is None
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
    assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref
    prepared = _frame(storage, read_job_spec(storage, job_spec_key(run_id)).upload_key)
    assert "North" not in set(prepared["region"]), "it trains on the prescription's prepared copy"
    dollars = _upload(client, _history(built, "$"))
    stopped = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": dollars,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert stopped.status_code == 409 and stopped.json()["detail"]["code"] == "CURRENCY_FOREIGN"


# ---------------------------------------------------------------------------
# Review fixes: rows no recipe prepares are checked at training
# ---------------------------------------------------------------------------
def test_a_bound_dataset_with_no_post_build_recipe_is_checked_against_its_contract(
    built: tuple[Path, str], unified: None
) -> None:
    root, dataset_id = built
    storage = LocalStorage(root / "data")
    rows = _frame(storage, dataset_key(dataset_id, DATASET_FRAME_FILENAME))
    approved = rows[rows["region"] != "West"]  # at approval the dataset had no West
    ref, _ = _prescribe(storage, contract=learn_contract(approved, primary_key=KEY, target=TARGET))
    bind_dataset(storage, dataset_id, ref)
    try:
        response = _train(root, dataset_id)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert (detail["code"], detail["path"]) == ("PRESCRIPTION_DRIFT", "region")
        with pytest.raises(FiringError) as failed:
            _fire(root, dataset_id, RunMode.TRAIN)
        assert failed.value.code == "PRESCRIPTION_DRIFT"
        # Rows that match their contract train as built, with the reference kept.
        ref, _ = _prescribe(storage, contract=learn_contract(rows, primary_key=KEY, target=TARGET))
        bind_dataset(storage, dataset_id, ref)
        response = _train(root, dataset_id)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        spec = read_job_spec(storage, job_spec_key(run_id))
        assert spec.upload_key == dataset_key(dataset_id, DATASET_FRAME_FILENAME)
        assert storage.read_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), PrescriptionRef) == ref
    finally:
        _unbind(storage, dataset_id)


def test_a_bound_upload_with_no_recipe_is_checked_against_its_contract(
    api: tuple[TestClient, Path], built: tuple[Path, str], unified: None
) -> None:
    client, data_dir = api
    storage = LocalStorage(data_dir)
    history = _history(built, "₹")
    upload_id = _upload(client, history)
    rare = str(history["plan_type"].value_counts().index[-1])
    contract = learn_contract(history[history["plan_type"] != rare], primary_key=KEY, target=TARGET)
    part = FilePart(slot="history", upload_ids=(upload_id,), contract=contract)
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
    assert set(storage.list_keys("uploads/")) == before, "nothing is written for a stopped run"


# ---------------------------------------------------------------------------
# Review fixes: switch off reads nothing more; a Guided run keeps its money basis
# ---------------------------------------------------------------------------
def test_with_the_switch_off_a_training_upload_is_not_read_again(
    api: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes import run_prescriptions
    from api.routes.uploads import load_upload
    from engine.stages import ingest

    client, data_dir = api
    storage = LocalStorage(data_dir)
    sent = _upload(client, pd.DataFrame({KEY: ["C1", "C2"], TARGET: [0, 1]}))
    record = load_upload(storage, sent)
    reads: list[str] = []
    real = ingest.read_upload

    def counted(*args: Any, **kwargs: Any) -> Any:
        reads.append(str(args[1]))
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "read_upload", counted)
    config = load_use_case(USE_CASE)
    for switch in (False, True):  # off; on with nothing bound
        monkeypatch.setattr(doctor_registry, "unified_on", lambda _config, on=switch: on)
        found = run_prescriptions.prescribed_training(
            storage, config, upload=record, dataset_id=None, primary_key=KEY, target=TARGET
        )
        assert found is None
    assert reads == [], "the file is read only once the switch and a binding are confirmed"


def _money_recipe(upload: Any) -> DataRecipe:
    steps = (
        RecipeStep(
            order=1,
            kind=RecipeStepKind.CONVERT_CURRENCY,
            column="amount",
            params={
                "from": "USD",
                "to": "INR",
                "rate": 83.1,
                "rate_date": "2026-10-10",
                "source": "RBI reference rate",
            },
            reason="Convert amount from US dollars at 83.1 (RBI reference rate, 10 Oct 2026).",
        ),
    )
    return DataRecipe(
        recipe_id="rc-guided-money",
        use_case_id=USE_CASE,
        source_fingerprint=upload.fingerprint_hash,
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=KEY,
        target=TARGET,
        levels=(AgentLevel.CLEAN,),
        max_failure_pct=5.0,
        approved_by="analyst",
        created_at=utc_now(),
    )


@pytest.mark.parametrize("switch", [True, False])
def test_a_guided_run_with_a_conversion_keeps_its_money_basis_with_the_switch_on(
    api: tuple[TestClient, Path],
    built: tuple[Path, str],
    config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    switch: bool,
) -> None:
    from api.routes.agent_recipes import write_derived_upload
    from api.routes.uploads import load_upload

    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: switch)
    client, data_dir = api
    storage = LocalStorage(data_dir)
    sent = load_upload(storage, _upload(client, _history(built, "$")))
    config = load_use_case(USE_CASE, config_root)
    derived = write_derived_upload(storage, config, sent, _money_recipe(sent))  # Guided setup's Approve
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": derived.record.upload_id,
            "primary_key": KEY,
            "target": TARGET,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert not storage.exists(run_key(run_id, PRESCRIPTION_REF_FILENAME)), "no stored prescription"
    basis = load_money_basis(storage, run_id)
    if not switch:
        assert basis is None, "with the switch off the run is as before"
        return
    assert basis is not None
    (money,) = basis.columns
    assert (money.column, money.written_in, money.reported_in) == ("amount", ("USD",), "INR")
    (rate,) = money.rates
    assert (rate.rate, rate.source, str(rate.rate_date)) == (83.1, "RBI reference rate", "2026-10-10")


# ---------------------------------------------------------------------------
# What counts as a yes (Q24): the outcome a query step builds from another file
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("capped", [False, True])
def test_an_upload_trains_on_the_outcome_column_a_query_builds_from_the_orders_file(
    api: tuple[TestClient, Path], unified: None, monkeypatch: pytest.MonkeyPatch, capped: bool
) -> None:
    from tests.unit.doctor import visit_support as vs

    client, data_dir = api
    storage = LocalStorage(data_dir)
    rng = np.random.default_rng(24)
    customers = vs.customers(rows=1_500, seed=5).drop(columns=["converted", "churned", "snapshot_date"])
    ids = rng.integers(0, 1_500, 4_000)
    orders = pd.DataFrame(
        {
            "customer_id": [f"C{100000 + i}" for i in ids],
            "order_amount": np.round(rng.gamma(2.0, 260.0, 4_000), 2),
            "order_date": (
                pd.Timestamp("2026-07-01") + pd.to_timedelta(rng.integers(0, 125, 4_000), unit="D")
            )
            .strftime("%Y-%m-%d")
            .to_numpy(),
        }
    )

    def upload(frame: pd.DataFrame, name: str) -> str:
        sent = client.post(
            "/uploads",
            files={"file": (name, frame.to_csv(index=False).encode(), "text/csv")},
            data={"use_case": "targeted-advertisement", "mode": "train"},
        )
        assert sent.status_code == 201, sent.text
        return str(sent.json()["upload_id"])

    customers_id, orders_id = upload(customers, "customers.csv"), upload(orders, "orders.csv")
    sql = (
        "SELECT c.*, COALESCE(w.is_sale, 0) AS is_sale FROM t_customers c LEFT JOIN ("
        "SELECT customer_id, 1 AS is_sale FROM t_orders WHERE order_amount > 500 "
        "AND CAST(order_date AS DATE) > $as_of GROUP BY customer_id) w ON c.customer_id = w.customer_id"
    )
    steps = (
        RecipeStep(
            order=1,
            kind=RecipeStepKind.SQL_QUERY,
            column="customer_sales",
            params={
                "sql": sql,
                "inputs": ["customers", "orders"],
                "output_name": "customer_sales",
                "description": "Mark each customer who ordered over 500 after the as-of date",
                "as_of": "2026-09-30",
                "reads_future": True,
            },
        ),
    )
    post_build = DataRecipe(
        recipe_id="rc-sales",
        use_case_id="targeted-advertisement",
        source_fingerprint="case:sales",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=None,
        target="is_sale",
        levels=(AgentLevel.RESHAPE,),
        max_failure_pct=5.0,
        created_at=utc_now(),
    )
    ref, _ = _prescribe(
        storage,
        files=(
            FilePart(slot="customers", upload_ids=(customers_id,)),
            FilePart(slot="orders", upload_ids=(orders_id,)),
        ),
        post_build=post_build,
        basis=None,
    )
    bind_upload(storage, customers_id, ref)
    if capped:
        monkeypatch.setattr("api.routes.run_prescriptions.profile_row_cap", lambda _config: 2_000)
    response = client.post(
        "/runs",
        json={
            "use_case": "targeted-advertisement",
            "mode": "train",
            "upload_id": customers_id,
            "primary_key": "customer_id",
            "target": "is_sale",
        },
    )
    if capped:  # the orders file has more rows than are read: the newest rows, the outcome, may be cut
        assert response.status_code == 409 and response.json()["detail"]["code"] == "QUERY_INPUT_TOO_LARGE"
        return
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    prepared = _frame(storage, read_job_spec(storage, job_spec_key(run_id)).upload_key)
    day = pd.to_datetime(orders["order_date"])
    expected = orders[(orders["order_amount"] > 500) & (day > "2026-09-30")]["customer_id"].nunique()
    assert len(prepared) == len(customers) and int(prepared["is_sale"].sum()) == expected > 0
