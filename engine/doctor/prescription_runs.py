"""Prescriptions meet uploads, datasets and runs (Plan O M243, DEC-1813).

PRESCRIBE (`engine.doctor.prescription.CasePrescriber`) binds each upload of the case, and the dataset its approved
build made, to the prescription it wrote: a small reference beside the file (`prescription_ref.json`: client, case,
hash; no value, no user id). The run paths read it, **only while `data_doctor.unified` is on**:

* **Training from a built dataset** (`POST /runs` with `dataset_id`, and a scheduled training firing):
  :func:`dataset_for_training` prepares the dataset with the prescription's post-build recipe
  (`engine.agent.dataset_recipe.prepare_dataset`, the same function and file a scoring replay uses, with the training
  file's full future-data check) and checks the prepared rows against the prescription's contract. The run trains on
  the prepared rows and carries the recipe (`attach_dataset_recipe`), so the model it registers prepares every dataset
  it later scores the same way. Until now "a dataset cannot get a Guided recipe for training" ([INV] §8).
* **Training from a file**: :func:`upload_for_training` names the file slot whose recipe prepares the upload (the API
  writes the prepared copy with `write_derived_upload`, as Guided setup's Approve does), after the raw-file checks
  (intake, locale, currency) of `engine.doctor.prescription_replay.check_files`.
* **Every prepared run** (:func:`attach_to_run`): the run keeps the reference (`runs/<id>/prescription_ref.json`) and,
  when the prescription has one, its money basis (M242's `save_money_basis`), so reports print each money column's
  unit and frozen rate.
* **Scoring** (:func:`check_for_scoring`): a model whose training run carries a prescription checks each scoring
  file against it - the raw-file checks and the contract of the prepared rows. A stop is a `PrescriptionError`
  (`PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN`, `CURRENCY_MIXED`), which the route answers with a 409 and the question;
  warnings are returned. Nothing is re-learnt.

With the switch off, or with nothing bound, every function here returns None (or nothing) before reading anything,
so every run is exactly as before: byte-identical, which the tests pin.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, ValidationError

from engine.config import StrictBase
from engine.doctor.prescription import (
    HASH_PATTERN,
    FilePart,
    Prescription,
    PrescriptionError,
    load_prescription,
)
from engine.doctor.prescription_contract import DriftFinding, check_contract
from engine.storage import StorageError, run_key, upload_key

if TYPE_CHECKING:
    import pandas as pd

    from engine.agent.contracts import DataRecipe
    from engine.agent.dataset_recipe import PreparedDataset
    from engine.config import UseCaseConfig
    from engine.intake.contracts import IntakeReport
    from engine.storage import Storage

__all__ = [
    "PRESCRIPTION_CHECK_FILENAME",
    "PRESCRIPTION_REF_FILENAME",
    "DatasetTraining",
    "PrescriptionCheck",
    "PrescriptionRef",
    "ScoringCheck",
    "UploadTraining",
    "attach_to_run",
    "bind_dataset",
    "bind_upload",
    "bound",
    "check_for_scoring",
    "check_prepared",
    "dataset_for_training",
    "resolve",
    "run_ref",
    "upload_for_training",
]

PRESCRIPTION_REF_FILENAME: Final[str] = "prescription_ref.json"
PRESCRIPTION_CHECK_FILENAME: Final[str] = "prescription_check.json"


class PrescriptionRef(StrictBase):
    """Which prescription a file, a dataset or a run follows: ids and a hash only."""

    client_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    case_id: str = Field(min_length=1, max_length=80)
    prescription_hash: str = Field(pattern=HASH_PATTERN)


def _on(config: UseCaseConfig | None) -> bool:
    from engine.doctor.registry import unified_on

    return unified_on(config)


def _dataset_ref_key(dataset_id: str) -> str:
    from engine.onboarding.datasets import dataset_key

    return dataset_key(dataset_id, PRESCRIPTION_REF_FILENAME)


def bind_upload(storage: Storage, upload_id: str, ref: PrescriptionRef) -> None:
    """`uploads/<id>/prescription_ref.json`: retention and erasure remove it with the upload."""
    storage.write_model(upload_key(upload_id, PRESCRIPTION_REF_FILENAME), ref)


def bind_dataset(storage: Storage, dataset_id: str, ref: PrescriptionRef) -> None:
    """`datasets/<id>/prescription_ref.json`: it lives and dies with the dataset, like its prepared files."""
    storage.write_model(_dataset_ref_key(dataset_id), ref)


def _read_ref(storage: Storage, key: str) -> PrescriptionRef | None:
    try:
        if not storage.exists(key):
            return None
        return storage.read_model(key, PrescriptionRef)
    except (StorageError, ValidationError, ValueError):
        return None


def bound(
    storage: Storage, *, upload_id: str | None = None, dataset_id: str | None = None
) -> PrescriptionRef | None:
    """The prescription an upload or a dataset is bound to, or None."""
    if upload_id is not None:
        return _read_ref(storage, upload_key(upload_id, PRESCRIPTION_REF_FILENAME))
    if dataset_id is not None:
        return _read_ref(storage, _dataset_ref_key(dataset_id))
    return None


def run_ref(storage: Storage, run_id: str) -> PrescriptionRef | None:
    """The prescription a run was prepared under, or None."""
    return _read_ref(storage, run_key(run_id, PRESCRIPTION_REF_FILENAME))


def resolve(storage: Storage, ref: PrescriptionRef) -> Prescription:
    """The prescription a reference names, its hash checked (`PRESCRIPTION_NOT_FOUND` otherwise)."""
    return load_prescription(storage, ref.client_id, ref.case_id, ref.prescription_hash)


class PrescriptionCheck(StrictBase):
    """`runs/<id>/prescription_check.json`: what a run's replay checks found that breaks nothing - each warning with
    its question - for the screen to show. Written only when there is a warning."""

    prescription_hash: str
    mode: Literal["train", "score"]
    warnings: tuple[DriftFinding, ...]


def attach_to_run(
    storage: Storage,
    run_id: str,
    ref: PrescriptionRef,
    prescription: Prescription,
    *,
    mode: Literal["train", "score"] = "train",
    warnings: Sequence[DriftFinding] = (),
) -> None:
    """A run prepared under a prescription keeps its reference, its money basis (M242's `save_money_basis`) when it
    has one, and the warnings its checks found."""
    from engine.doctor.currency import save_money_basis

    storage.write_model(run_key(run_id, PRESCRIPTION_REF_FILENAME), ref)
    basis = None if prescription.locale is None else prescription.locale.money_basis
    if basis is not None:
        save_money_basis(storage, run_id, basis)
    if warnings:
        storage.write_model(
            run_key(run_id, PRESCRIPTION_CHECK_FILENAME),
            PrescriptionCheck(
                prescription_hash=prescription.prescription_hash, mode=mode, warnings=tuple(warnings)
            ),
        )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DatasetTraining:
    """A built dataset prepared for training by its prescription's post-build recipe."""

    ref: PrescriptionRef
    prescription: Prescription
    recipe: DataRecipe | None
    prepared: PreparedDataset | None
    warnings: tuple[DriftFinding, ...] = ()


def dataset_for_training(
    storage: Storage, config: UseCaseConfig, dataset_id: str, key: Sequence[str] = ()
) -> DatasetTraining | None:
    """The dataset prepared by its prescription for training, or None (switch off, or nothing bound).

    Raises `PrescriptionError` when the post-build recipe names another ID column than the dataset's `key`, or touches
    the rest of its key (`RECIPE_ROLES_MISMATCH`, before anything is prepared), cannot run on the dataset (a column it
    needs is gone or changed), or the prepared rows break the contract.
    """
    if not _on(config):
        return None
    ref = bound(storage, dataset_id=dataset_id)
    if ref is None:
        return None
    from engine.agent.dataset_recipe import dataset_key_conflict, prepare_dataset
    from engine.agent.recipe import RecipeError

    prescription = resolve(storage, ref)
    recipe = prescription.post_build
    prepared: PreparedDataset | None = None
    warnings: tuple[DriftFinding, ...] = ()
    if recipe is not None:
        conflict = dataset_key_conflict(recipe, tuple(key))
        if conflict is not None:
            raise PrescriptionError("RECIPE_ROLES_MISMATCH", conflict, part="post_build")
        try:
            prepared = prepare_dataset(storage, config, dataset_id, recipe, leak_check="full")
        except RecipeError as exc:
            from engine.doctor.prescription_replay import step_drift

            raise step_drift(exc, prescription.contract, part="post_build") from exc
    if prescription.contract is not None and prepared is not None:
        found = check_contract(prepared.frame, prescription.contract, mode="train")
        stops = [f for f in found if f.stops]
        if stops:
            raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part="contract")
        warnings = found
    return DatasetTraining(
        ref=ref, prescription=prescription, recipe=recipe, prepared=prepared, warnings=warnings
    )


@dataclass(frozen=True)
class UploadTraining:
    """The file slot whose recipe prepares an upload for training, after the raw-file checks."""

    ref: PrescriptionRef
    prescription: Prescription
    part: FilePart
    warnings: tuple[DriftFinding, ...] = ()


def _slot_for(prescription: Prescription, upload_id: str) -> FilePart | None:
    named = next((p for p in prescription.files if upload_id in p.upload_ids), None)
    if named is not None:
        return named
    return prescription.files[0] if len(prescription.files) == 1 else None


def upload_for_training(
    storage: Storage,
    config: UseCaseConfig,
    upload_id: str,
    frame: pd.DataFrame,
    intake: IntakeReport | None = None,
) -> UploadTraining | None:
    """The slot preparing `upload_id` for training (None: switch off, nothing bound, or no slot for it).

    `frame` is the upload as sent (capped), for the intake, locale and currency checks; a stop raises.
    """
    if not _on(config):
        return None
    ref = bound(storage, upload_id=upload_id)
    if ref is None:
        return None
    prescription = resolve(storage, ref)
    part = _slot_for(prescription, upload_id)
    if part is None:
        return None
    from engine.doctor.prescription_replay import ReplayFile, check_files

    warnings = check_files(
        prescription, [ReplayFile(slot=part.slot, frame=frame, intake=intake, upload_id=upload_id)]
    )
    return UploadTraining(ref=ref, prescription=prescription, part=part, warnings=warnings)


def check_prepared(
    part: FilePart, prepared: pd.DataFrame, *, mode: Literal["train", "score"]
) -> tuple[DriftFinding, ...]:
    """A file slot's prepared rows against its contract: raises on a stop, returns the warnings."""
    if part.contract is None:
        return ()
    found = check_contract(prepared, part.contract, mode=mode, slot=part.slot)
    stops = [f for f in found if f.stops]
    if stops:
        raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part="contract")
    return found


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoringCheck:
    """A scoring file checked against the prescription its model was trained under."""

    ref: PrescriptionRef
    prescription: Prescription
    warnings: tuple[DriftFinding, ...] = ()


def check_for_scoring(
    storage: Storage,
    config: UseCaseConfig,
    training_run_id: str,
    prepared: pd.DataFrame,
    *,
    raw: Callable[[], pd.DataFrame | None] | None = None,
    intake: IntakeReport | None = None,
) -> ScoringCheck | None:
    """A scoring file against the prescription the model was trained under: the raw-file checks on `raw()` (the file
    as sent, read only now, for a one-file prescription), then the contract on `prepared` (the rows the model reads;
    the outcome is not read). Raises `PrescriptionError` on a stop; returns the warnings. None - before anything is
    read - with the switch off or for a model trained with no prescription."""
    if not _on(config):
        return None
    ref = run_ref(storage, training_run_id)
    if ref is None:
        return None
    prescription = resolve(storage, ref)
    warnings: list[DriftFinding] = []
    single = prescription.files[0] if len(prescription.files) == 1 else None
    sent = raw() if raw is not None and single is not None else None
    if sent is not None and single is not None:
        from engine.doctor.prescription_replay import ReplayFile, check_files

        warnings.extend(check_files(prescription, [ReplayFile(slot=single.slot, frame=sent, intake=intake)]))
    contract = prescription.contract
    if contract is None and single is not None and prescription.build is None:
        contract = single.contract
    if contract is not None:
        found = check_contract(prepared, contract, mode="score")
        stops = [f for f in found if f.stops]
        if stops:
            raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part="contract")
        warnings.extend(found)
    return ScoringCheck(ref=ref, prescription=prescription, warnings=tuple(warnings))
