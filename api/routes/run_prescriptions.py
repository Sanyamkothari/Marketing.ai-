"""`POST /runs` replays a Data Doctor prescription (Plan O M243, DEC-1813).

Three hooks `api.routes.runs.create_run_endpoint` calls, each a no-op - returning None before anything is read - while
`data_doctor.unified` is off or nothing is bound to a prescription (`engine.doctor.prescription_runs`), so every run is
exactly as before:

* :func:`prescribed_training` - a training run of a built dataset bound to a prescription trains on the dataset as the
  prescription's post-build recipe prepares it; a training run of an upload bound to one trains on the upload as its
  file slot's recipe prepares it (a derived upload, as Guided setup's Approve writes), after the intake, locale and
  currency checks.
* :func:`prescribed_scoring` - a scoring file of a model trained under a prescription is checked against it (raw-file
  checks, then the contract of the prepared rows).
* :func:`attach` - the run keeps the reference, the money basis and the warnings.

A stop is a 409 with the code (`PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN`, `CURRENCY_MIXED`, a `RECIPE_*` code) and the
one question it asks, in the M1 error envelope. Nothing is re-learnt and nothing is guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException

from api.routes.uploads import http_error, ingest_http, profile_row_cap
from engine.stages import ingest
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from api.schemas import UploadRecord
    from engine.agent.contracts import DataRecipe
    from engine.agent.dataset_recipe import PreparedDataset
    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile, ModelVersion
    from engine.doctor.prescription import Prescription, PrescriptionError
    from engine.doctor.prescription_contract import DriftFinding
    from engine.doctor.prescription_runs import PrescriptionRef
    from engine.storage import Storage

__all__ = ["Prescribed", "attach", "prescribed_scoring", "prescribed_training", "prescription_conflict"]

_LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class Prescribed:
    """What a prescription changed about a run: the upload or the dataset rows it reads, and what it keeps."""

    ref: PrescriptionRef
    prescription: Prescription
    mode: Literal["train", "score"]
    upload: UploadRecord | None = None
    profile: DatasetProfile | None = None
    dataset_prepared: tuple[DataRecipe, PreparedDataset] | None = None
    warnings: tuple[DriftFinding, ...] = field(default=())


def prescription_conflict(exc: PrescriptionError) -> HTTPException:
    """A replay's stop as the 409 `POST /runs` answers: the code, what changed, and the question it asks."""
    question = exc.question
    message = exc.message if question is None else f"{exc.message} {question.text}"
    column = next((f.column for f in exc.findings if f.stops and f.column), None)
    return http_error(409, exc.code, message, path=column)


def _intake(storage: Storage, upload: UploadRecord) -> Any:
    from engine.intake.contracts import IntakeReport

    key = upload.intake_key
    if key is None or not storage.exists(key):
        return None
    return storage.read_model(key, IntakeReport)


def prescribed_training(
    storage: Storage,
    config: UseCaseConfig,
    *,
    upload: UploadRecord | None,
    dataset_id: str | None,
    primary_key: Any,
    target: str | None,
) -> Prescribed | None:
    """The rows a training run reads under its prescription, or None (switch off, nothing bound)."""
    from api.routes.agent_recipes import load_recipe, refuse_other_roles, write_derived_upload
    from engine.agent.recipe import RecipeError
    from engine.config import key_columns
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_replay import step_drift
    from engine.doctor.prescription_runs import check_prepared, dataset_for_training, upload_for_training

    try:
        if dataset_id is not None:
            training = dataset_for_training(storage, config, dataset_id, key_columns(primary_key))
            if training is None:
                return None
            prepared = (
                None
                if training.recipe is None or training.prepared is None
                else (training.recipe, training.prepared)
            )
            return Prescribed(
                ref=training.ref,
                prescription=training.prescription,
                mode="train",
                dataset_prepared=prepared,
                warnings=training.warnings,
            )
        if upload is None:
            return None
        frame = ingest.read_upload(
            storage, upload.source_key, file_format=upload.file_format, row_cap=profile_row_cap(config)
        ).frame
        found = upload_for_training(storage, config, upload.upload_id, frame, _intake(storage, upload))
        if found is None:
            return None
        recipe = found.part.recipe
        already = load_recipe(storage, upload.upload_id)
        if recipe is None or (already is not None and already.prepares_like(recipe)):
            return Prescribed(
                ref=found.ref, prescription=found.prescription, mode="train", warnings=found.warnings
            )
        refuse_other_roles(recipe, primary_key=primary_key, target=target, training=True)
        try:
            derived = write_derived_upload(storage, config, upload, recipe)
        except RecipeError as exc:  # a step that breaks on a column the contract knows is drift
            raise step_drift(exc, found.part.contract, part="recipe", slot=found.part.slot) from exc
        try:
            rows = ingest.read_upload(
                storage, derived.record.source_key, file_format="parquet", row_cap=profile_row_cap(config)
            ).frame
            warnings = (*found.warnings, *check_prepared(found.part, rows, mode="train"))
        except PrescriptionError:
            # A stopped run leaves no prepared copy behind.
            for key in storage.list_keys(f"uploads/{derived.record.upload_id}/"):
                storage.delete(key)
            raise
    except PrescriptionError as exc:
        raise prescription_conflict(exc) from exc
    except RecipeError as exc:
        raise http_error(409, exc.code, exc.message, path=exc.column) from exc
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc
    return Prescribed(
        ref=found.ref,
        prescription=found.prescription,
        mode="train",
        upload=derived.record,
        profile=derived.profile,
        warnings=warnings,
    )


def prescribed_scoring(
    storage: Storage,
    config: UseCaseConfig,
    version: ModelVersion,
    prepared: Any,
    *,
    sent: UploadRecord | None,
) -> Prescribed | None:
    """A scoring file checked against the prescription its model was trained under, or None."""
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_runs import check_for_scoring

    raw: Callable[[], Any] | None = None
    if sent is not None:

        def raw() -> Any:
            return ingest.read_upload(
                storage, sent.source_key, file_format=sent.file_format, row_cap=profile_row_cap(config)
            ).frame

    try:
        checked = check_for_scoring(
            storage,
            config,
            version.run_id,
            prepared,
            raw=raw,
            intake=None if sent is None else _intake(storage, sent),
        )
    except PrescriptionError as exc:
        raise prescription_conflict(exc) from exc
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc
    if checked is None:
        return None
    return Prescribed(
        ref=checked.ref, prescription=checked.prescription, mode="score", warnings=checked.warnings
    )


def attach(storage: Storage, run_id: str, prescribed: Prescribed | None) -> None:
    """The run keeps its prescription's reference, its money basis and the warnings (nothing for None). A dataset's
    post-build recipe is attached by the route itself, with every other dataset recipe (`attach_dataset_recipe`).
    """
    if prescribed is None:
        return
    from engine.doctor.prescription_runs import attach_to_run

    attach_to_run(
        storage,
        run_id,
        prescribed.ref,
        prescribed.prescription,
        mode=prescribed.mode,
        warnings=prescribed.warnings,
    )
    if prescribed.warnings:
        _LOGGER.info("prescription.run_warnings run_id=%s warnings=%d", run_id, len(prescribed.warnings))
