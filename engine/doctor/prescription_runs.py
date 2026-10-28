"""Prescriptions meet uploads, datasets and runs (Plan O M243, DEC-1813).

PRESCRIBE (`engine.doctor.prescription.CasePrescriber`) binds each upload of the case, the dataset its approved
build made, and the onboarding spec that built it, to the prescription it wrote: a small reference
(`prescription_ref.json`: client, case, hash; no value, no user id). A file added to the case later is bound to the
case's latest prescription (`engine.doctor.visit.add_files`), so next month's file replays it. The run paths read the
references:

* **Training from a built dataset** (`POST /runs` with `dataset_id`, and a scheduled training firing):
  :func:`dataset_for_training` finds the prescription bound to the dataset - or, for a dataset a firing built fresh
  from a spec, the one bound to that spec - and replays the design's steps 6-8 on every row
  (`engine.doctor.prescription_replay.replay_after_build`): the approved outcome (M239: a column's values become
  1/0, rows left out are removed and counted), the post-build recipe (with the training file's full future-data
  check), then the contract. The run trains on the rows it wrote (`datasets/<id>/prepared_rx_<hash16>.parquet`)
  and carries the post-build recipe alone (`attach_dataset_recipe`), so the model prepares every dataset it scores
  the same way, the outcome left out. Until now "a dataset cannot get a Guided recipe for training" ([INV] §8). When
  nothing changes the rows, the built rows are checked against the contract all the same.
* **Training from a file** (:func:`upload_for_training`): the upload bound to a prescription - or, for an unbound
  upload, the prescription the use case's champion was trained under - is checked as sent (intake, locale,
  currency: `engine.doctor.prescription_replay.check_files`, the file read only once the binding is
  confirmed), prepared by its file slot's recipe, checked against the slot's contract, then given the outcome, the
  post-build recipe and the final contract; the API writes the result as the run's prepared copy.
* **Every prepared run** (:func:`attach_to_run`): the run keeps the reference (`runs/<id>/prescription_ref.json`) and,
  when the prescription has one, its money basis (M242's `save_money_basis`), so reports print each money column's
  unit and frozen rate. A run a Guided-setup recipe prepared under no stored prescription keeps the basis that recipe
  gives (:func:`recipe_money_basis`).
* **Scoring** (:func:`check_for_scoring`): a model whose training run carries a prescription checks each scoring
  file against it - the raw-file checks and the contract of the prepared rows. A stop is a `PrescriptionError`
  (`PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN`, `CURRENCY_MIXED`), which the route answers with a 409 and the question;
  warnings are returned. Nothing is re-learnt.

With nothing bound, every function here returns None (or nothing) before reading anything, so a run with no
prescription is exactly as before: byte-identical, which the tests pin.
"""

from __future__ import annotations

import hashlib
import re
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

    from engine.agent.contracts import DataRecipe, RecipeReceipt
    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile
    from engine.doctor.case import Case
    from engine.doctor.currency import MoneyBasis
    from engine.doctor.outcome_definition import OutcomeReceipt
    from engine.intake.contracts import IntakeReport
    from engine.onboarding.specs import DatasetManifest
    from engine.storage import Storage

__all__ = [
    "PRESCRIPTION_CHECK_FILENAME",
    "PRESCRIPTION_REF_FILENAME",
    "DatasetTraining",
    "PrescriptionCheck",
    "PrescriptionRef",
    "ScoringCheck",
    "TrainingRows",
    "UploadTraining",
    "attach_recipe_basis",
    "attach_to_run",
    "bind_case_uploads",
    "bind_dataset",
    "bind_spec",
    "bind_upload",
    "bound",
    "check_for_scoring",
    "check_prepared",
    "dataset_for_training",
    "recipe_money_basis",
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


def _dataset_ref_key(dataset_id: str) -> str:
    from engine.onboarding.datasets import dataset_key

    return dataset_key(dataset_id, PRESCRIPTION_REF_FILENAME)


def bind_upload(storage: Storage, upload_id: str, ref: PrescriptionRef) -> None:
    """`uploads/<id>/prescription_ref.json`: retention and erasure remove it with the upload."""
    storage.write_model(upload_key(upload_id, PRESCRIPTION_REF_FILENAME), ref)


def bind_dataset(storage: Storage, dataset_id: str, ref: PrescriptionRef) -> None:
    """`datasets/<id>/prescription_ref.json`: it lives and dies with the dataset, like its prepared files."""
    storage.write_model(_dataset_ref_key(dataset_id), ref)


_SPEC_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CLIENT_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _spec_ref_key(client_id: str, spec_id: str) -> str | None:
    """`cases/<client>/prescribed_specs/<spec_id>.json`: beside the client's cases (never read as one)."""
    if not (_CLIENT_ID.fullmatch(client_id) and _SPEC_ID.fullmatch(spec_id)):
        return None
    from engine.doctor.case import CASE_PREFIX

    return f"{CASE_PREFIX}{client_id}/prescribed_specs/{spec_id}.json"


def bind_spec(storage: Storage, client_id: str, spec_id: str, ref: PrescriptionRef) -> None:
    """The onboarding spec that built the approved dataset: every dataset a firing builds from it later follows the
    same prescription. A spec id that is not a plain id is not bound."""
    key = _spec_ref_key(client_id, spec_id)
    if key is not None:
        storage.write_model(key, ref)


def bind_case_uploads(storage: Storage, case: Case, upload_ids: Sequence[str]) -> int:
    """Bind files added to a case to its latest stored prescription (next month's file replays it); the number
    bound. Nothing when the case has no prescription yet, or its latest was never stored (PRESCRIBE binds them).
    """
    if not case.prescriptions or not upload_ids:
        return 0
    from engine.doctor.prescription import prescription_key

    latest = case.prescriptions[-1].prescription_hash
    try:
        if not storage.exists(prescription_key(case.client_id, case.case_id, latest)):
            return 0
        ref = PrescriptionRef(client_id=case.client_id, case_id=case.case_id, prescription_hash=latest)
    except (StorageError, ValidationError, ValueError):
        return 0
    for upload_id in upload_ids:
        bind_upload(storage, upload_id, ref)
    return len(upload_ids)


def _read_ref(storage: Storage, key: str) -> PrescriptionRef | None:
    try:
        if not storage.exists(key):
            return None
        return storage.read_model(key, PrescriptionRef)
    except (StorageError, ValidationError, ValueError):
        return None


def bound(
    storage: Storage,
    *,
    upload_id: str | None = None,
    dataset_id: str | None = None,
    spec: tuple[str, str] | None = None,
) -> PrescriptionRef | None:
    """The prescription an upload, a dataset or an onboarding spec (`(client_id, spec_id)`) is bound to, or None."""
    if upload_id is not None:
        return _read_ref(storage, upload_key(upload_id, PRESCRIPTION_REF_FILENAME))
    if dataset_id is not None:
        return _read_ref(storage, _dataset_ref_key(dataset_id))
    if spec is not None:
        key = _spec_ref_key(*spec)
        return None if key is None else _read_ref(storage, key)
    return None


def _dataset_ref(
    storage: Storage, dataset_id: str, manifest: DatasetManifest | None
) -> PrescriptionRef | None:
    """The dataset's own reference, else the one bound to the spec that built it (a firing's fresh build)."""
    ref = bound(storage, dataset_id=dataset_id)
    if ref is not None:
        return ref
    if manifest is None:
        from engine.onboarding.datasets import DATASET_MANIFEST_FILENAME, dataset_key
        from engine.onboarding.specs import DatasetManifest as Manifest

        try:
            key = dataset_key(dataset_id, DATASET_MANIFEST_FILENAME)
            if not storage.exists(key):
                return None
            manifest = storage.read_model(key, Manifest)
        except (StorageError, ValidationError, ValueError):
            return None
    return bound(storage, spec=(manifest.client_id, manifest.spec_id))


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


def recipe_money_basis(recipe: DataRecipe | None, config: UseCaseConfig) -> MoneyBasis | None:
    """The money basis a Guided-setup recipe gives a run prepared under no stored prescription (M242): each money
    column's unit from its `parse_number` steps, and each `convert_currency` step's rate, date and source as the
    recipe froze them when it was approved. None when the recipe reads no money, or a conversion carries no rate a
    report could show (a basis never shows a rate nobody approved)."""
    if recipe is None:
        return None
    from engine.agent.contracts import RecipeStepKind
    from engine.doctor.currency import ApprovedRate, CurrencyError, money_basis, workspace_currency_for

    steps = recipe.steps
    if not any(
        step.kind is RecipeStepKind.CONVERT_CURRENCY
        or (step.kind is RecipeStepKind.PARSE_NUMBER and "unit" in step.params)
        for step in steps
    ):
        return None
    rates: list[ApprovedRate] = []
    for step in steps:
        if step.kind is not RecipeStepKind.CONVERT_CURRENCY:
            continue
        params = step.params
        try:
            rates.append(
                ApprovedRate(
                    column=step.column,
                    from_currency=str(params.get("from")),
                    to_currency=str(params.get("to")),
                    rate=float(params.get("rate")),  # type: ignore[arg-type]
                    rate_date=str(params.get("rate_date")),  # type: ignore[arg-type]
                    source=str(params.get("source")),
                    rows=params.get("rows", "all"),  # type: ignore[arg-type]
                    sentence=step.reason
                    or f"Step {step.order} of the approved preparation of '{step.column}'.",
                    approved_by=recipe.approved_by or "guided_setup",
                    approved_at=recipe.created_at,
                )
            )
        except (ValidationError, TypeError, ValueError):
            return None
    workspace = workspace_currency_for(getattr(config, "data_doctor", None), None).code
    try:
        return money_basis(steps, workspace_currency=workspace, rates=rates, units={})
    except CurrencyError:
        return None


def attach_recipe_basis(
    storage: Storage, run_id: str, recipe: DataRecipe | None, config: UseCaseConfig
) -> MoneyBasis | None:
    """A run prepared by a recipe under no stored prescription keeps that recipe's money basis
    (`money_basis.json`); nothing otherwise. Returns what was saved."""
    if recipe is None:
        return None
    from engine.doctor.currency import save_money_basis

    basis = recipe_money_basis(recipe, config)
    if basis is not None:
        save_money_basis(storage, run_id, basis)
    return basis


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
class TrainingRows:
    """Rows a prescription wrote for a training run: where they are, their profile, and the first rows (capped)."""

    source_key: str
    profile: DatasetProfile
    frame: pd.DataFrame


@dataclass(frozen=True)
class DatasetTraining:
    """A built dataset prepared for training by its prescription.

    `rows` are the rows the run trains on when the outcome or the post-build recipe changed them (None: the rows as
    built, checked against the contract); `recipe` and `receipt` are the post-build recipe the run carries to
    scoring and what it did (None without one)."""

    ref: PrescriptionRef
    prescription: Prescription
    recipe: DataRecipe | None
    rows: TrainingRows | None
    receipt: RecipeReceipt | None = None
    outcome: OutcomeReceipt | None = None
    warnings: tuple[DriftFinding, ...] = ()


def _stop_on(found: Sequence[DriftFinding], *, part: str) -> tuple[DriftFinding, ...]:
    stops = [f for f in found if f.stops]
    if stops:
        raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part=part)
    return tuple(found)


def _training_key(
    dataset_id: str, prescription: Prescription, goal_id: str | None, config: UseCaseConfig
) -> str:
    """`datasets/<id>/prepared_rx_<16 hex>.parquet`: the prescription, the goal and the levels its recipe runs under,
    so a training copy is never a scoring replay's file (which holds the outcome as built)."""
    from engine.agent.dataset_recipe import PREPARED_PREFIX, replay_identity
    from engine.onboarding.datasets import dataset_key

    recipe = prescription.post_build
    identity = "|".join(
        (
            prescription.prescription_hash,
            goal_id or "",
            "" if recipe is None else replay_identity(recipe, config),
        )
    )
    short = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return dataset_key(dataset_id, f"{PREPARED_PREFIX}rx_{short}.parquet")


def dataset_for_training(
    storage: Storage,
    config: UseCaseConfig,
    dataset_id: str,
    key: Sequence[str] = (),
    *,
    manifest: DatasetManifest | None = None,
    capped: pd.DataFrame | None = None,
) -> DatasetTraining | None:
    """The dataset prepared by its prescription for training, or None (nothing bound) before anything
    is read. `manifest` names the spec a fresh build came from (read from the dataset when not given); `capped` is the
    built rows up to the profiling cap, when the caller has read them already.

    Raises `PrescriptionError` when the post-build recipe names another ID column than the dataset's `key`, or touches
    the rest of its key (`RECIPE_ROLES_MISMATCH`, before anything is prepared), the outcome cannot run (`OUTCOME_*`),
    a step cannot run on the dataset (a column it needs is gone or changed), or the rows break the contract.
    """
    ref = _dataset_ref(storage, dataset_id, manifest)
    if ref is None:
        return None
    from engine.agent.dataset_recipe import dataset_key_conflict, store_prepared_rows
    from engine.agent.recipe import RecipeError
    from engine.doctor.prescription_replay import changes_rows, replay_after_build, training_goal
    from engine.onboarding.datasets import DATASET_FRAME_FILENAME, dataset_key
    from engine.stages import ingest

    prescription = resolve(storage, ref)
    recipe = prescription.post_build
    goal = training_goal(prescription, getattr(config, "id", None))
    if recipe is not None:
        conflict = dataset_key_conflict(recipe, tuple(key))
        if conflict is not None:
            raise PrescriptionError("RECIPE_ROLES_MISMATCH", conflict, part="post_build")
    frame_key = dataset_key(dataset_id, DATASET_FRAME_FILENAME)
    cap = ingest.profile_row_cap(config)
    if not changes_rows(prescription, mode="train", goal_id=goal):
        # Nothing changes the rows: they are checked against the contract all the same.
        warnings: tuple[DriftFinding, ...] = ()
        if prescription.contract is not None:
            rows = (
                capped
                if capped is not None
                else ingest.read_upload(storage, frame_key, file_format="parquet", row_cap=cap).frame
            )
            warnings = _stop_on(check_contract(rows, prescription.contract, mode="train"), part="contract")
        return DatasetTraining(ref=ref, prescription=prescription, recipe=None, rows=None, warnings=warnings)
    read_all = ingest.read_upload(storage, frame_key, file_format="parquet", row_cap=cap, keep_all_rows=True)
    every = read_all.all_rows if read_all.all_rows is not None else read_all.frame
    replayed = replay_after_build(
        prescription, every, mode="train", goal_id=goal, config=config, upload_id=dataset_id
    )
    try:
        stored = store_prepared_rows(
            storage, config, dataset_id, replayed.frame, _training_key(dataset_id, prescription, goal, config)
        )
    except RecipeError as exc:  # the prepared rows could not be saved
        raise PrescriptionError(exc.code, exc.message, part="post_build") from exc
    return DatasetTraining(
        ref=ref,
        prescription=prescription,
        recipe=recipe,
        rows=TrainingRows(source_key=stored.source_key, profile=stored.profile, frame=stored.frame),
        receipt=replayed.post_build,
        outcome=replayed.outcome,
        warnings=replayed.warnings,
    )


@dataclass(frozen=True)
class UploadTraining:
    """The file slot whose recipe prepares an upload for training, after the raw-file checks; `goal_id` is the goal
    section whose outcome the training copy takes."""

    ref: PrescriptionRef
    prescription: Prescription
    part: FilePart
    warnings: tuple[DriftFinding, ...] = ()
    goal_id: str | None = None


def _slot_for(prescription: Prescription, upload_id: str) -> FilePart | None:
    named = next((p for p in prescription.files if upload_id in p.upload_ids), None)
    if named is not None:
        return named
    return prescription.files[0] if len(prescription.files) == 1 else None


def upload_for_training(
    storage: Storage,
    config: UseCaseConfig,
    upload_id: str,
    frame: Callable[[], pd.DataFrame],
    intake: Callable[[], IntakeReport | None] | None = None,
    *,
    fallback: Callable[[], PrescriptionRef | None] | None = None,
) -> UploadTraining | None:
    """The slot preparing `upload_id` for training (None: nothing bound, or no slot for it).

    The upload's own reference, else `fallback()` (the prescription the use case's champion was trained under, for
    next month's file sent straight to a run). `frame()` reads the upload as sent (capped) and `intake()` its intake
    report, for the intake, locale and currency checks - each called only once the binding is
    confirmed. A stop raises.
    """
    ref = bound(storage, upload_id=upload_id)
    if ref is None and fallback is not None:
        ref = fallback()
    if ref is None:
        return None
    prescription = resolve(storage, ref)
    part = _slot_for(prescription, upload_id)
    if part is None or (prescription.build is not None and upload_id not in part.upload_ids):
        # A file that is one source of a build is replayed through the build, never on its own.
        return None
    from engine.doctor.prescription_replay import ReplayFile, check_files, training_goal

    warnings = check_files(
        prescription,
        [
            ReplayFile(
                slot=part.slot,
                frame=frame(),
                intake=None if intake is None else intake(),
                upload_id=upload_id,
            )
        ],
    )
    return UploadTraining(
        ref=ref,
        prescription=prescription,
        part=part,
        warnings=warnings,
        goal_id=training_goal(prescription, getattr(config, "id", None)),
    )


def check_prepared(
    part: FilePart, prepared: pd.DataFrame, *, mode: Literal["train", "score"]
) -> tuple[DriftFinding, ...]:
    """A file slot's prepared rows against its contract: raises on a stop, returns the warnings."""
    if part.contract is None:
        return ()
    return _stop_on(check_contract(prepared, part.contract, mode=mode, slot=part.slot), part="contract")


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
    training_run_id: str,
    prepared: pd.DataFrame,
    *,
    raw: Callable[[], pd.DataFrame | None] | None = None,
    intake: IntakeReport | None = None,
) -> ScoringCheck | None:
    """A scoring file against the prescription the model was trained under: the raw-file checks on `raw()` (the file
    as sent, read only now, for a one-file prescription), then the contract on `prepared` (the rows the model reads;
    the outcome is not read). Raises `PrescriptionError` on a stop; returns the warnings. None - before anything is
    read - for a model trained with no prescription."""
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
