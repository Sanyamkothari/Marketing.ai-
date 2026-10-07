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
  `POST /runs` answers with its usual 409. `scoring_source` is the rule it shares with the dry run
  and with Guided setup for which file that is; `prepare_in_memory` is the same preparation without
  writing anything, for the dry run.

This module imports nothing from `api.routes.runs`, which imports it.
"""

from __future__ import annotations

import numbers
from dataclasses import dataclass
from typing import Any, Final

from api.routes.uploads import (
    UPLOAD_FINGERPRINT_FILENAME,
    UPLOAD_PROFILE_FILENAME,
    UPLOAD_RECORD_FILENAME,
    http_error,
    load_upload_profile,
    profile_row_cap,
    source_filename,
)
from api.schemas import UploadRecord
from engine.agent.config import AgentLevel
from engine.agent.contracts import DATA_RECIPE_FILENAME, RECIPE_RECEIPT_FILENAME, DataRecipe, RecipeReceipt
from engine.agent.recipe import RecipeError, RecipeRun, run_recipe
from engine.agent.reshape import LeakCheck
from engine.config import PrimaryKey, RunMode, UseCaseConfig
from engine.contracts import DatasetProfile, ModelVersion, Severity, ValidationCheck, ValidationReport
from engine.stages import ingest
from engine.storage import Storage, run_key, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "RECIPE_ROLES_MISMATCH",
    "DerivedUpload",
    "ScoringSource",
    "attach_recipe_to_run",
    "consistent_types",
    "load_recipe",
    "model_recipe",
    "prepare_in_memory",
    "recipe_failure_report",
    "recipe_levels",
    "refuse_other_roles",
    "replay_for_scoring",
    "run_saved_recipe",
    "scoring_source",
    "write_derived_upload",
]

_LOGGER = get_logger(__name__)
_MIXED: Final[frozenset[str]] = frozenset({"mixed", "mixed-integer"})
"""What `pandas.api.types.infer_dtype` calls a column holding text beside numbers or flags."""
_MAX_PREPARED_CHAIN: Final[int] = 8
"""How many prepared uploads `scoring_source` follows back to the file a person sent."""
RECIPE_ROLES_MISMATCH: Final[str] = "RECIPE_ROLES_MISMATCH"
"""A run on prepared data naming another ID column or outcome than its recipe was checked against."""

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


def load_receipt(storage: Storage, upload_id: str) -> RecipeReceipt | None:
    """What the recipe did when it prepared a derived upload, or None for an upload a person sent."""
    key = upload_key(upload_id, RECIPE_RECEIPT_FILENAME)
    return storage.read_model(key, RecipeReceipt) if storage.exists(key) else None


def model_recipe(storage: Storage, version: ModelVersion) -> DataRecipe | None:
    """The recipe the version's training run prepared its data with, or None (DEC-1006)."""
    key = run_key(version.run_id, DATA_RECIPE_FILENAME)
    return storage.read_model(key, DataRecipe) if storage.exists(key) else None


def refuse_other_roles(
    recipe: DataRecipe | None, *, primary_key: PrimaryKey | None, target: str | None, training: bool
) -> None:
    """409 `RECIPE_ROLES_MISMATCH` when a run on prepared data names other roles than `recipe`'s.

    Every safety rule of a recipe - no step changes the ID or the outcome, no computed column reads
    the outcome, combined rows never add the outcome up - was checked against the recipe's own roles
    (DEC-1004). A run naming other roles would train on, or join scores by, columns those rules never
    protected.

    * Training (`recipe` is the prepared upload's): the ID column and the outcome must both be the
      recipe's, a recipe without one included.
    * Scoring (`recipe` is the model's): only the ID column is compared, and only when the recipe
      named one; a scoring run names no outcome.

    `POST /runs`, the dry run and Guided setup's Approve all call this with the same recipe, before
    anything is prepared, so they give the same answer and a refused run writes nothing (DEC-1011).
    """
    if recipe is None:
        return
    key = primary_key if isinstance(primary_key, str) else None  # a recipe names a single column
    target = target or None
    other_key = key != recipe.primary_key and (training or recipe.primary_key is not None)
    if other_key or (training and target != recipe.target):
        raise http_error(
            409,
            RECIPE_ROLES_MISMATCH,
            f"This data was prepared by Guided setup with {recipe.primary_key!r} as the ID column"
            + (f" and {recipe.target!r} as the outcome" if training else "")
            + ". Run it with "
            + ("those" if training else "that ID column")
            + ", or start Guided setup again on the file you sent.",
        )


def recipe_levels(recipe: DataRecipe, config: UseCaseConfig) -> tuple[tuple[AgentLevel, ...], float]:
    """The levels and failure limit a replay of `recipe` runs under: the ones it was approved with.

    A recipe saved before they were recorded has neither, and runs under the use case's current
    ones, as it always did. Otherwise a use case that later drops a level, or tightens the limit,
    would refuse every scoring file of a model already trained on data that level prepared.
    """
    levels = recipe.levels if recipe.levels is not None else tuple(config.agent.levels)
    limit = (
        recipe.max_failure_pct
        if recipe.max_failure_pct is not None
        else config.agent.max_conversion_failure_pct
    )
    return levels, limit


def consistent_types(frame: Any) -> Any:
    """`frame` with every column that mixes text and other values held as text, so it can be saved.

    A CSV is read in chunks of `ingest.CHUNK_ROWS` rows, each typed on its own: IDs that are numbers
    for 100,000 rows and then `C-123` come back as one column of ints and strings, which a Parquet
    file cannot hold. The numbers become text (an integral float without its `.0`); columns that do
    not mix are untouched.

    This is the text of the value the chunked read produced, not of the cell as written: a chunk
    that was read as numbers has already lost any leading zeros (`0000123` is `123`), exactly as the
    same file read without a recipe has. It keeps a prepared file the same as an unprepared read of
    the file; it does not recover what that read lost.
    """
    import pandas as pd
    from pandas.api.types import infer_dtype

    mixed = [
        name
        for name in frame.columns
        if frame[name].dtype == object and infer_dtype(frame[name], skipna=True) in _MIXED
    ]
    if not mixed:
        return frame

    def as_text(value: Any) -> Any:
        if value is None or value is pd.NA or isinstance(value, str):
            return value
        if isinstance(value, float) and value != value:
            return value
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, numbers.Integral):
            return str(int(value))
        if isinstance(value, numbers.Real):
            number = float(value)
            return str(int(number)) if number.is_integer() else repr(number)
        return str(value)

    out = frame.copy()
    for name in mixed:
        out[name] = pd.Series([as_text(v) for v in out[name]], index=out.index, dtype=object)
    return out


def run_saved_recipe(
    frame: Any,
    recipe: DataRecipe,
    config: UseCaseConfig,
    *,
    upload_id: str,
    derived_upload_id: str | None = None,
    leak_check: LeakCheck | None = None,
) -> RecipeRun:
    """Run a saved recipe on `frame` under the levels and limit it was approved with. Raises `RecipeError`."""
    levels, limit = recipe_levels(recipe, config)
    return run_recipe(
        consistent_types(frame),
        recipe.steps,
        upload_id=upload_id,
        primary_key=recipe.primary_key,
        target=recipe.target,
        levels=levels,
        max_failure_pct=limit,
        snapshot_column=recipe.snapshot_column,
        derived_upload_id=derived_upload_id,
        leak_check=leak_check,
    )


def _all_rows(storage: Storage, config: UseCaseConfig, source: UploadRecord) -> Any:
    read_all = ingest.read_upload(
        storage,
        source.source_key,
        file_format=source.file_format,
        row_cap=profile_row_cap(config),
        keep_all_rows=True,
    )
    # Every row, never the profiling cap: a recipe prepares the whole file.
    return read_all.all_rows if read_all.all_rows is not None else read_all.frame


def _leak_check(source: UploadRecord) -> LeakCheck:
    # Combined rows get onboarding's future-data check (ruling R1): full when a training file is
    # prepared - each Approve writes a new recipe, so this is always its first build - and the
    # narrow check when the model's recipe is replayed on a scoring file (M76).
    return "full" if source.mode is RunMode.TRAIN else "narrow"


def write_derived_upload(
    storage: Storage, config: UseCaseConfig, source: UploadRecord, recipe: DataRecipe
) -> DerivedUpload:
    """Run `recipe` on every row of `source` and store the result as a new upload. Raises `RecipeError`."""
    frame = _all_rows(storage, config, source)
    derived_id = new_upload_id()
    run = run_saved_recipe(
        frame,
        recipe,
        config,
        upload_id=source.upload_id,
        derived_upload_id=derived_id,
        leak_check=_leak_check(source),
    )
    source_key = upload_key(derived_id, source_filename("parquet"))
    try:
        with storage.open_write(source_key) as sink:
            consistent_types(run.frame).to_parquet(sink, index=False)
    except (ValueError, TypeError) as exc:  # pyarrow's ArrowInvalid and ArrowTypeError among them
        # The error text quotes values from the file, so it is logged by type only, never shown.
        _LOGGER.warning(
            "recipe.derived_write_failed upload_id=%s error=%s", source.upload_id, type(exc).__name__
        )
        if storage.exists(source_key):
            storage.delete(source_key)
        raise RecipeError(
            "RECIPE_STEP_INVALID",
            "The prepared file could not be saved: a column holds values of more than one kind.",
        ) from exc
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
        synthetic=source.synthetic,
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


@dataclass(frozen=True)
class ScoringSource:
    """The upload a scoring run reads, and whether it is already prepared by the model's recipe."""

    upload: UploadRecord
    ready: bool
    """True when `upload` is to be read as it is: prepared by this very recipe, or - for a model
    with no recipe - a file as the person sent it."""


def _gone(upload_id: str) -> ValidationReport:
    return recipe_failure_report(
        RecipeError(
            "RECIPE_STEP_INVALID",
            "This file was prepared with other steps, and the file it was prepared from is gone. "
            "Upload the original file again.",
        ),
        upload_id=upload_id,
        mode=RunMode.SCORE,
    )


def scoring_source(
    storage: Storage, upload: UploadRecord, recipe: DataRecipe | None
) -> ScoringSource | ValidationReport:
    """Which file the model whose recipe is `recipe` (None: it has none) must read for `upload`.

    * An upload prepared by this same recipe - same steps, roles and limits (`prepares_like`) - is
      read as it is, so pressing Run twice does not prepare a file twice.
    * An upload prepared by *other* steps is never prepared again on top of that output, and never
      scored as it is by a model trained on files as sent: the file the person sent, which the
      prepared upload's receipt names, is used instead (M77 hardening, Plan G review).
    * A file as sent is read as it is by a model with no recipe, and prepared for one with a recipe.

    The same rule serves `POST /runs`, the dry run and Guided setup, so the three cannot disagree
    about which file a model reads (DEC-1011).
    """
    already = load_recipe(storage, upload.upload_id)
    if already is not None and recipe is not None and already.prepares_like(recipe):
        return ScoringSource(upload=upload, ready=True)
    source = upload
    for _ in range(_MAX_PREPARED_CHAIN):
        if load_recipe(storage, source.upload_id) is None:
            return ScoringSource(upload=source, ready=recipe is None)
        receipt = load_receipt(storage, source.upload_id)
        if receipt is None or not storage.exists(upload_key(receipt.upload_id, UPLOAD_RECORD_FILENAME)):
            return _gone(upload.upload_id)
        source = storage.read_model(upload_key(receipt.upload_id, UPLOAD_RECORD_FILENAME), UploadRecord)
    return _gone(upload.upload_id)


def prepare_in_memory(
    storage: Storage, config: UseCaseConfig, upload: UploadRecord, recipe: DataRecipe | None
) -> tuple[Any, int] | ValidationReport:
    """The frame and row count `POST /runs` would check for a scoring `upload`, with nothing written.

    `replay_for_scoring` without the derived upload: the same source, the same recipe run on every
    row of it, then capped as `POST /runs` reads its prepared upload. A file the recipe cannot
    prepare is the same one-error report.
    """
    chosen = scoring_source(storage, upload, recipe)
    if isinstance(chosen, ValidationReport):
        return chosen
    source = chosen.upload
    cap = profile_row_cap(config)
    if chosen.ready or recipe is None:
        read = ingest.read_upload(storage, source.source_key, file_format=source.file_format, row_cap=cap)
        return read.frame, load_upload_profile(storage, source.upload_id).row_count
    try:
        run = run_saved_recipe(
            _all_rows(storage, config, source),
            recipe,
            config,
            upload_id=source.upload_id,
            leak_check=_leak_check(source),
        )
    except RecipeError as exc:
        return recipe_failure_report(exc, upload_id=upload.upload_id, mode=RunMode.SCORE)
    frame = consistent_types(run.frame)
    return frame.head(cap).reset_index(drop=True), len(frame)


def replay_for_scoring(
    storage: Storage, config: UseCaseConfig, upload: UploadRecord, version: ModelVersion
) -> tuple[UploadRecord, DatasetProfile] | ValidationReport:
    """The upload a scoring run should read: the file as sent, or the file prepared by the model's recipe.

    `scoring_source` picks the file; when it is not yet prepared by the model's recipe, the recipe
    runs on it into a new derived upload. A model without a recipe reads the file the person sent.
    """
    recipe = model_recipe(storage, version)
    chosen = scoring_source(storage, upload, recipe)
    if isinstance(chosen, ValidationReport):
        return chosen
    if chosen.ready or recipe is None:
        return chosen.upload, load_upload_profile(storage, chosen.upload.upload_id)
    try:
        derived = write_derived_upload(storage, config, chosen.upload, recipe)
    except RecipeError as exc:
        return recipe_failure_report(exc, upload_id=upload.upload_id, mode=RunMode.SCORE)
    return derived.record, derived.profile
