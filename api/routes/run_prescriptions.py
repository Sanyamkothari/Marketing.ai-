"""`POST /runs` replays a Data Doctor prescription (Plan O M243, DEC-1813).

Three hooks `api.routes.runs.create_run_endpoint` calls, each a no-op - returning None before anything is read - while
`data_doctor.unified` is off or nothing is bound to a prescription (`engine.doctor.prescription_runs`), so every run is
exactly as before:

* :func:`prescribed_training` - a training run of a built dataset bound to a prescription (or built from a spec bound
  to one) trains on the rows the prescription's steps 6-8 make: the approved outcome, the post-build recipe, the
  contract. A training run of an upload bound to one (or, unbound, of a use case whose champion was trained under
  one) trains on the copy the prescription makes - the file slot's recipe, the slot's contract, then the outcome,
  the post-build recipe and the final contract - after the intake, locale and currency checks. Rows nothing changes
  are checked against the contract all the same.
* :func:`prescribed_scoring` - a scoring file of a model trained under a prescription is checked against it (raw-file
  checks, then the contract of the prepared rows).
* :func:`attach` - the run keeps the reference, the money basis and the warnings; a run a Guided-setup recipe
  prepared under no stored prescription keeps that recipe's money basis.

A stop is a 409 with the code (`PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN`, `CURRENCY_MIXED`, an `OUTCOME_*` or `RECIPE_*`
code) and the one question it asks, in the M1 error envelope. Nothing is re-learnt and nothing is guessed.
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
    from engine.agent.contracts import DataRecipe, RecipeReceipt
    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile, ModelVersion
    from engine.doctor.prescription import Prescription, PrescriptionError
    from engine.doctor.prescription_contract import DriftFinding
    from engine.doctor.prescription_runs import PrescriptionRef, TrainingRows
    from engine.onboarding.specs import DatasetManifest
    from engine.registry import ModelRegistry
    from engine.storage import Storage

__all__ = ["Prescribed", "attach", "prescribed_scoring", "prescribed_training", "prescription_conflict"]

_LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class Prescribed:
    """What a prescription changed about a run: the upload or the dataset rows it reads, and what it keeps.

    `dataset_rows`: the rows a dataset trains on when the prescription changed them; `dataset_recipe`: the post-build
    recipe the run carries to scoring, and what it did."""

    ref: PrescriptionRef
    prescription: Prescription
    mode: Literal["train", "score"]
    upload: UploadRecord | None = None
    profile: DatasetProfile | None = None
    dataset_rows: TrainingRows | None = None
    dataset_recipe: tuple[DataRecipe, RecipeReceipt] | None = None
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


def _champion_ref(
    storage: Storage, registry: ModelRegistry | None, use_case_id: str
) -> Callable[[], PrescriptionRef | None] | None:
    """The prescription the use case's champion was trained under, looked up only when an upload is unbound."""
    if registry is None:
        return None

    def found() -> PrescriptionRef | None:
        from engine.doctor.prescription_runs import run_ref

        champion = registry.get_champion(use_case_id)
        return None if champion is None else run_ref(storage, champion.run_id)

    return found


def prescribed_training(
    storage: Storage,
    config: UseCaseConfig,
    *,
    upload: UploadRecord | None,
    dataset_id: str | None,
    primary_key: Any,
    target: str | None,
    manifest: DatasetManifest | None = None,
    registry: ModelRegistry | None = None,
) -> Prescribed | None:
    """The rows a training run reads under its prescription, or None (switch off, nothing bound) before anything is
    read."""
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_runs import dataset_for_training, prescriptions_on

    if not prescriptions_on(config):
        return None

    try:
        if dataset_id is not None:
            from engine.config import key_columns

            training = dataset_for_training(
                storage, config, dataset_id, key_columns(primary_key), manifest=manifest
            )
            if training is None:
                return None
            carried = (
                None
                if training.recipe is None or training.receipt is None
                else (training.recipe, training.receipt)
            )
            return Prescribed(
                ref=training.ref,
                prescription=training.prescription,
                mode="train",
                dataset_rows=training.rows,
                dataset_recipe=carried,
                warnings=training.warnings,
            )
        if upload is None:
            return None
        return _upload_training(
            storage, config, upload, primary_key=primary_key, target=target, registry=registry
        )
    except PrescriptionError as exc:
        raise prescription_conflict(exc) from exc
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc


def _upload_training(
    storage: Storage,
    config: UseCaseConfig,
    upload: UploadRecord,
    *,
    primary_key: Any,
    target: str | None,
    registry: ModelRegistry | None,
) -> Prescribed | None:
    from api.routes.agent_recipes import (
        load_receipt,
        load_recipe,
        refuse_other_roles,
        write_derived_rows,
        write_derived_upload,
    )
    from engine.agent.recipe import RecipeError
    from engine.doctor.prescription_replay import changes_rows, replay_after_build, step_drift
    from engine.doctor.prescription_runs import check_prepared, upload_for_training

    cap = profile_row_cap(config)
    read: list[Any] = []

    def raw() -> Any:
        """The upload as sent (capped), read once, and only after the switch and the binding are confirmed."""
        if not read:
            read.append(
                ingest.read_upload(
                    storage, upload.source_key, file_format=upload.file_format, row_cap=cap
                ).frame
            )
        return read[0]

    found = upload_for_training(
        storage,
        config,
        upload.upload_id,
        raw,
        lambda: _intake(storage, upload),
        fallback=_champion_ref(storage, registry, upload.use_case_id),
    )
    if found is None:
        return None
    part, prescription = found.part, found.prescription
    recipe = part.recipe
    already = load_recipe(storage, upload.upload_id)
    prepared_already = recipe is not None and already is not None and already.prepares_like(recipe)
    # Steps 6-8 on the file: with one file and no build, the file is the built file (a build's sources are replayed
    # through the build, never trained on one by one).
    after = prescription.build is None and changes_rows(prescription, mode="train", goal_id=found.goal_id)
    warnings: list[DriftFinding] = list(found.warnings)

    def finish(rows: Any) -> Any:
        """The slot's prepared rows: its contract (on the rows a run validates), then steps 6-8."""
        warnings.extend(check_prepared(part, rows.head(cap), mode="train"))
        if not after:
            return rows
        replayed = replay_after_build(
            prescription,
            rows,
            mode="train",
            goal_id=found.goal_id,
            config=config,
            upload_id=upload.upload_id,
            tables=_query_tables(storage, config, prescription, part.slot, rows, cap),
        )
        warnings.extend(replayed.warnings)
        return replayed.frame

    try:
        if recipe is None or prepared_already:
            if not after:
                # The rows as they are: checked against the slot's contract, nothing written.
                warnings.extend(check_prepared(part, raw(), mode="train"))
                return Prescribed(
                    ref=found.ref, prescription=prescription, mode="train", warnings=tuple(warnings)
                )
            # The outcome (and a post-build recipe) on rows no recipe prepares here: a copy keeping the recipe
            # the file was prepared with, if any, for scoring.
            record, profile = write_derived_rows(
                storage,
                config,
                upload,
                finish,
                recipe=already if prepared_already else None,
                receipt=load_receipt(storage, upload.upload_id) if prepared_already else None,
            )
        else:
            refuse_other_roles(recipe, primary_key=primary_key, target=target, training=True)
            try:
                derived = write_derived_upload(storage, config, upload, recipe, then=finish)
            except RecipeError as exc:  # a step that breaks on a column the contract knows is drift
                raise step_drift(exc, part.contract, part="recipe", slot=part.slot) from exc
            record, profile = derived.record, derived.profile
    except RecipeError as exc:
        raise http_error(409, exc.code, exc.message, path=exc.column) from exc
    # Every check ran before anything was written: a stopped run leaves no prepared copy behind.
    return Prescribed(
        ref=found.ref,
        prescription=prescription,
        mode="train",
        upload=record,
        profile=profile,
        warnings=tuple(warnings),
    )


def _query_tables(
    storage: Storage, config: UseCaseConfig, prescription: Prescription, slot: str, rows: Any, cap: int
) -> Any:
    """The case's other files, each prepared by its own recipe, for a post-build query step that reads them (the
    outcome of a rule over orders, Q24); None when no such step is there. `rows` are this slot's prepared rows.
    """
    import pandas as pd

    from api.routes.uploads import load_upload
    from engine.agent.contracts import RecipeStepKind
    from engine.doctor.prescription import case_tables
    from engine.doctor.prescription_replay import run_part_recipe

    post = prescription.post_build
    if post is None or not any(step.kind is RecipeStepKind.SQL_QUERY for step in post.steps):
        return None
    frames: dict[str, Any] = {slot: rows}
    for other in prescription.files:
        if other.slot == slot:
            continue
        read = [
            ingest.read_upload(storage, record.source_key, file_format=record.file_format, row_cap=cap).frame
            for record in (load_upload(storage, upload_id) for upload_id in other.upload_ids)
        ]
        frame = read[0] if len(read) == 1 else pd.concat(read, ignore_index=True)
        if other.recipe is not None:
            frame = run_part_recipe(
                other.recipe,
                frame,
                config=config,
                upload_id=other.upload_ids[0],
                leak_check="full",
                contract=other.contract,
                slot=other.slot,
            ).frame
        frames[other.slot] = frame
    return case_tables(frames, prescription.files)


def prescribed_scoring(
    storage: Storage,
    config: UseCaseConfig,
    version: ModelVersion,
    prepared: Any,
    *,
    sent: UploadRecord | None,
) -> Prescribed | None:
    """A scoring file checked against the prescription its model was trained under, or None (before anything is
    read while the switch is off)."""
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_runs import check_for_scoring, prescriptions_on

    if not prescriptions_on(config):
        return None

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


def attach(
    storage: Storage,
    run_id: str,
    prescribed: Prescribed | None,
    *,
    config: UseCaseConfig | None = None,
    recipe: DataRecipe | None = None,
    upload_id: str | None = None,
) -> None:
    """The run keeps its prescription's reference, its money basis and the warnings. Under no stored prescription,
    while the switch is on, a run prepared by a recipe (`recipe`, or the one the run's upload `upload_id` was
    prepared with, read only then) keeps that recipe's money basis; nothing otherwise. A dataset's post-build recipe
    is attached by the route itself, with every other dataset recipe (`attach_dataset_recipe`).
    """
    from engine.doctor.prescription_runs import attach_recipe_basis, attach_to_run, prescriptions_on

    if prescribed is None:
        if config is None or not prescriptions_on(config):
            return
        if recipe is None and upload_id is not None:
            from api.routes.agent_recipes import load_recipe

            recipe = load_recipe(storage, upload_id)
        attach_recipe_basis(storage, run_id, recipe, config)
        return
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
