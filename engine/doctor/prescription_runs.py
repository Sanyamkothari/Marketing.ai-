"""Prescriptions meet uploads and runs (Plan O M243, DEC-1813).

PRESCRIBE (`engine.doctor.prescription.CasePrescriber`) binds each upload of the case to the prescription it wrote:
a small reference (`prescription_ref.json`: client, case, hash; no value, no user id). A file added to the case later is bound to the
case's latest prescription (`engine.doctor.visit.add_files`), so next month's file replays it. The run paths read the
references (nothing binds a prescription to a built dataset, so a dataset run follows none):

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
    from engine.config import UseCaseConfig
    from engine.doctor.case import Case
    from engine.doctor.currency import MoneyBasis
    from engine.intake.contracts import IntakeReport
    from engine.storage import Storage

__all__ = [
    "PRESCRIPTION_CHECK_FILENAME",
    "PRESCRIPTION_REF_FILENAME",
    "PrescriptionCheck",
    "PrescriptionRef",
    "ScoringCheck",
    "UploadTraining",
    "attach_recipe_basis",
    "attach_to_run",
    "bind_case_uploads",
    "bind_upload",
    "bound",
    "check_for_scoring",
    "check_prepared",
    "recipe_money_basis",
    "resolve",
    "run_ref",
    "upload_for_training",
]

PRESCRIPTION_REF_FILENAME: Final[str] = "prescription_ref.json"
PRESCRIPTION_CHECK_FILENAME: Final[str] = "prescription_check.json"


class PrescriptionRef(StrictBase):
    """Which prescription a file or a run follows: ids and a hash only."""

    client_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    case_id: str = Field(min_length=1, max_length=80)
    prescription_hash: str = Field(pattern=HASH_PATTERN)


def bind_upload(storage: Storage, upload_id: str, ref: PrescriptionRef) -> None:
    """`uploads/<id>/prescription_ref.json`: retention and erasure remove it with the upload."""
    storage.write_model(upload_key(upload_id, PRESCRIPTION_REF_FILENAME), ref)


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


def bound(storage: Storage, *, upload_id: str) -> PrescriptionRef | None:
    """The prescription an upload is bound to, or None."""
    return _read_ref(storage, upload_key(upload_id, PRESCRIPTION_REF_FILENAME))


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
def _stop_on(found: Sequence[DriftFinding], *, part: str) -> tuple[DriftFinding, ...]:
    stops = [f for f in found if f.stops]
    if stops:
        raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part=part)
    return tuple(found)


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
    if part is None:
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
    if contract is None and single is not None:
        contract = single.contract
    if contract is not None:
        found = check_contract(prepared, contract, mode="score")
        stops = [f for f in found if f.stops]
        if stops:
            raise PrescriptionError("PRESCRIPTION_DRIFT", stops[0].summary, findings=found, part="contract")
        warnings.extend(found)
    return ScoringCheck(ref=ref, prescription=prescription, warnings=tuple(warnings))
