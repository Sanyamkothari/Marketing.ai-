"""Recipes meet uploads and runs (Plan G M72, DEC-1005, DEC-1006).

Three jobs, all through the storage the rest of the API uses:

* `write_derived_upload` runs an approved recipe on **every** row of an upload and stores the result
  as a new upload beside it - `source.parquet`, `profile.json`, `fingerprint.json`, `upload.json`,
  plus `data_recipe.json` and `recipe_receipt.json`. The original is never touched. A run is then
  started from the new upload exactly as Manual setup would.
* `attach_recipe_to_run` copies a derived upload's recipe into the run directory when `POST /runs`
  creates a run from it, so the model version that run registers carries its recipe with it.
* `replay_for_scoring` looks up the recipe of the model version a scoring run will use and, when it
  has one, runs it on the scoring upload before the file is checked against the model's schema. A
  file the recipe cannot prepare comes back as a validation report with a `RECIPE_*` error, which
  `POST /runs` answers with its usual 409.

This module imports nothing from `api.routes.runs`, which imports it.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.routes.uploads import (
    UPLOAD_FINGERPRINT_FILENAME,
    UPLOAD_PROFILE_FILENAME,
    UPLOAD_RECORD_FILENAME,
    load_upload_profile,
    profile_row_cap,
    source_filename,
)
from api.schemas import UploadRecord
from engine.agent.contracts import DATA_RECIPE_FILENAME, RECIPE_RECEIPT_FILENAME, DataRecipe, RecipeReceipt
from engine.agent.recipe import RecipeError, run_recipe
from engine.config import RunMode, UseCaseConfig
from engine.contracts import DatasetProfile, ModelVersion, Severity, ValidationCheck, ValidationReport
from engine.stages import ingest
from engine.storage import Storage, run_key, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

__all__ = [
    "DerivedUpload",
    "attach_recipe_to_run",
    "load_recipe",
    "model_recipe",
    "recipe_failure_report",
    "replay_for_scoring",
    "write_derived_upload",
]

_SUGGESTIONS: dict[str, str] = {
    "RECIPE_COLUMN_MISSING": (
        "Add the column back under the name the model was trained with, or open Guided setup to tell the "
        "helper what it is called now."
    ),
    "RECIPE_VALUES_UNCONVERTED": (
        "Check how this column is written in the new file. Open Guided setup to see the values that could "
        "not be read."
    ),
    "RECIPE_STEP_INVALID": "Retrain the model with Guided setup, so its preparation steps are saved again.",
}


@dataclass(frozen=True)
class DerivedUpload:
    record: UploadRecord
    profile: DatasetProfile
    recipe: DataRecipe
    receipt: RecipeReceipt


def load_recipe(storage: Storage, upload_id: str) -> DataRecipe | None:
    """The recipe a derived upload was prepared with, or None for an upload a person sent as it is."""
    key = upload_key(upload_id, DATA_RECIPE_FILENAME)
    return storage.read_model(key, DataRecipe) if storage.exists(key) else None


def model_recipe(storage: Storage, version: ModelVersion) -> DataRecipe | None:
    """The recipe the version's training run prepared its data with, or None (DEC-1006)."""
    key = run_key(version.run_id, DATA_RECIPE_FILENAME)
    return storage.read_model(key, DataRecipe) if storage.exists(key) else None


def write_derived_upload(
    storage: Storage, config: UseCaseConfig, source: UploadRecord, recipe: DataRecipe
) -> DerivedUpload:
    """Run `recipe` on every row of `source` and store the result as a new upload. Raises `RecipeError`."""
    read_all = ingest.read_upload(
        storage,
        source.source_key,
        file_format=source.file_format,
        row_cap=profile_row_cap(config),
        keep_all_rows=True,
    )
    # Every row, never the profiling cap: a recipe prepares the whole file.
    frame = read_all.all_rows if read_all.all_rows is not None else read_all.frame
    derived_id = new_upload_id()
    run = run_recipe(
        frame,
        recipe.steps,
        upload_id=source.upload_id,
        primary_key=recipe.primary_key,
        target=recipe.target,
        levels=config.agent.levels,
        max_failure_pct=config.agent.max_conversion_failure_pct,
        snapshot_column=recipe.snapshot_column,
        derived_upload_id=derived_id,
    )
    source_key = upload_key(derived_id, source_filename("parquet"))
    with storage.open_write(source_key) as sink:
        run.frame.to_parquet(sink, index=False)
    read = ingest.read_upload(storage, source_key, file_format="parquet", row_cap=profile_row_cap(config))
    profile = ingest.profile_dataset(
        read.frame,
        config,
        upload_id=derived_id,
        file_name=f"{source.file_name} (prepared)",
        file_format="parquet",
        file_size_bytes=storage.size_bytes(source_key),
        delimiter=None,
        encoding=read.encoding,
        row_count=read.row_count,
        fingerprint=read.fingerprint,
    )
    record = UploadRecord(
        upload_id=derived_id,
        use_case_id=source.use_case_id,
        mode=source.mode,
        file_name=profile.file_name,
        file_format="parquet",
        file_size_bytes=profile.file_size_bytes,
        delimiter=None,
        encoding=profile.encoding,
        row_count=profile.row_count,
        column_count=profile.column_count,
        source_key=source_key,
        profile_key=upload_key(derived_id, UPLOAD_PROFILE_FILENAME),
        fingerprint_key=upload_key(derived_id, UPLOAD_FINGERPRINT_FILENAME),
        fingerprint_hash=profile.fingerprint.hash,
        created_at=utc_now(),
    )
    storage.write_model(record.profile_key, profile)
    storage.write_model(record.fingerprint_key, profile.fingerprint)
    storage.write_model(upload_key(derived_id, DATA_RECIPE_FILENAME), recipe)
    storage.write_model(upload_key(derived_id, RECIPE_RECEIPT_FILENAME), run.receipt)
    storage.write_model(upload_key(derived_id, UPLOAD_RECORD_FILENAME), record)
    return DerivedUpload(record=record, profile=profile, recipe=recipe, receipt=run.receipt)


def attach_recipe_to_run(storage: Storage, upload_id: str, run_id: str) -> None:
    """Copy a derived upload's recipe and receipt into the run directory; nothing for a plain upload."""
    recipe = load_recipe(storage, upload_id)
    if recipe is None:
        return
    storage.write_model(run_key(run_id, DATA_RECIPE_FILENAME), recipe)
    receipt_key = upload_key(upload_id, RECIPE_RECEIPT_FILENAME)
    if storage.exists(receipt_key):
        storage.write_model(
            run_key(run_id, RECIPE_RECEIPT_FILENAME), storage.read_model(receipt_key, RecipeReceipt)
        )


def recipe_failure_report(error: RecipeError, *, upload_id: str, mode: RunMode) -> ValidationReport:
    """A `RecipeError` as the one-error validation report `POST /runs` answers 409 with."""
    code = error.code if error.code in _SUGGESTIONS else "RECIPE_STEP_INVALID"
    check = ValidationCheck(
        code=code,
        severity=Severity.ERROR,
        message=error.message,
        suggestion=_SUGGESTIONS[code],
        column=error.column,
        details={"step": error.order},
    )
    return ValidationReport(
        run_id=None,
        upload_id=upload_id,
        mode=mode,
        checks=(check,),
        error_count=1,
        warning_count=0,
        passed=False,
        validated_at=utc_now(),
    )


def replay_for_scoring(
    storage: Storage, config: UseCaseConfig, upload: UploadRecord, version: ModelVersion
) -> tuple[UploadRecord, DatasetProfile] | ValidationReport:
    """The upload a scoring run should read: the file as sent, or the file prepared by the model's recipe.

    An upload already prepared with the same recipe is used as it is, so pressing Run twice does not
    prepare a file twice.
    """
    recipe = model_recipe(storage, version)
    if recipe is None:
        return upload, load_upload_profile(storage, upload.upload_id)
    already = load_recipe(storage, upload.upload_id)
    if already is not None and already.recipe_hash == recipe.recipe_hash:
        return upload, load_upload_profile(storage, upload.upload_id)
    try:
        derived = write_derived_upload(storage, config, upload, recipe)
    except RecipeError as exc:
        return recipe_failure_report(exc, upload_id=upload.upload_id, mode=RunMode.SCORE)
    return derived.record, derived.profile
