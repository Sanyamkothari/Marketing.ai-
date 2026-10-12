"""A model's recipe replayed on a built dataset before it is scored (Plan L M155, DEC-1553 … DEC-1555).

Guided setup prepares a model's training file with a recipe, and every file that model later scores is
prepared the same way first (DEC-1006). Until M155 only an upload could be: a built dataset - `POST /runs`
with `dataset_id`, or a scheduled score firing - was refused (`RECIPE_DATASET_UNSUPPORTED`, DEC-1044). This
module closes that, at the one place both paths read the dataset:

* `prepare_dataset` runs the model's recipe on **every** row of the dataset's frame, under the levels and
  failure limit it was approved with, with the narrow future-data check a scoring replay uses, and stores the
  result beside the dataset as `datasets/<id>/prepared_<identity>.parquet`. The dataset's own files are never
  touched. The name is a hash of what a replay depends on (`replay_identity`: the steps, the ID, outcome and
  snapshot columns, the levels and the limit - what `DataRecipe.prepares_like` compares), so two scoring runs
  of one dataset by one recipe write the same file with the same rows, and the file lives and dies with its
  dataset: retention deletes it with the dataset's directory, erasure finds it by the dataset's key column
  (a recipe never changes the ID), and deleting the dataset deletes it.
* `dataset_key_conflict` is `api.routes.agent_recipes.refuse_other_roles`' scoring rule for a dataset, shared
  by `POST /runs` and the scheduled path (which may not import `api`): a model recipe that named an ID column
  scores only a dataset whose customers are named by that column, and none of its steps may touch the rest
  of a periodic dataset's key (the snapshot date).
* `attach_dataset_recipe` writes the recipe and its receipt into the scoring run's directory, as
  `attach_recipe_to_run` does for a run of a prepared upload.

The run keeps its dataset lineage (`run.json` names the dataset, its client and fingerprint); only the rows
its job reads are the prepared ones. A recipe that cannot run on the dataset (a column it needs is missing,
values it cannot read) raises `RecipeError`, which `POST /runs` answers with its usual 409 and a firing
records as its failure code - never a 500, never an unprepared score.

`recipe_levels`, `consistent_types` and `run_saved_recipe` moved here from `api/routes/agent_recipes.py`
(which re-exports them) so the scheduled path can run a recipe exactly as the API does.
"""

from __future__ import annotations

import hashlib
import json
import numbers
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import DATA_RECIPE_FILENAME, RECIPE_RECEIPT_FILENAME
from engine.agent.recipe import RecipeError, RecipeRun, run_recipe
from engine.stages import ingest
from engine.storage import run_key
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.agent.config import AgentLevel
    from engine.agent.contracts import DataRecipe, RecipeReceipt
    from engine.agent.reshape import LeakCheck
    from engine.config import UseCaseConfig
    from engine.contracts import DatasetProfile
    from engine.storage import Storage

__all__ = [
    "PREPARED_PREFIX",
    "PreparedDataset",
    "attach_dataset_recipe",
    "consistent_types",
    "dataset_key_conflict",
    "prepare_dataset",
    "prepared_dataset_key",
    "recipe_levels",
    "replay_identity",
    "run_saved_recipe",
]

_LOGGER = get_logger(__name__)

PREPARED_PREFIX: Final[str] = "prepared_"
"""File-name prefix of a dataset prepared by a recipe, inside the dataset's own directory."""

_MIXED: Final[frozenset[str]] = frozenset({"mixed", "mixed-integer"})
"""What `pandas.api.types.infer_dtype` calls a column holding text beside numbers or flags."""


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


def dataset_key_conflict(recipe: DataRecipe | None, key: Sequence[str]) -> str | None:
    """Why a dataset keyed by `key` may not be scored with `recipe` (`RECIPE_ROLES_MISMATCH`), or None.

    `refuse_other_roles`' scoring rule (DEC-1011), for a dataset: the recipe's ID column, when it named one,
    must be the column that names the dataset's customers (`key[0]`), because every safety rule of a recipe
    was checked against that column. A periodic dataset is also keyed by its snapshot date (`key[1:]`), which
    no recipe was checked against, so a step that names such a column - to change, rename or hide it - is
    refused too; a recipe that leaves it alone prepares the dataset as it prepares a file.
    """
    if recipe is None or not key:
        return None
    if recipe.primary_key is not None and recipe.primary_key != key[0]:
        return (
            f"This model's data was prepared by Guided setup with {recipe.primary_key!r} as the ID column, "
            f"and this dataset names its customers by {key[0]!r}."
        )
    others = set(key[1:])
    touched = [step for step in recipe.steps if step.column in others or step.new_column in others]
    if touched:
        step = touched[0]
        name = step.column if step.column in others else step.new_column
        return (
            f"Step {step.order} of the model's preparation changes {name!r}, which is part of each row's key in "
            "this dataset."
        )
    return None


def replay_identity(recipe: DataRecipe, config: UseCaseConfig) -> str:
    """A short hash of everything a replay of `recipe` depends on, under the levels and limit it runs with."""
    levels, limit = recipe_levels(recipe, config)
    payload = {
        "recipe_hash": recipe.recipe_hash,
        "primary_key": recipe.primary_key,
        "target": recipe.target,
        "snapshot_column": recipe.snapshot_column,
        "levels": sorted(str(level) for level in levels),
        "max_failure_pct": limit,
    }
    if recipe.text_columns:  # absent when empty, so a recipe that recorded none keeps its identity
        payload["text_columns"] = list(recipe.text_columns)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def prepared_dataset_key(dataset_id: str, recipe: DataRecipe, config: UseCaseConfig) -> str:
    """Where `dataset_id` prepared by `recipe` is stored: a flat file in the dataset's own directory."""
    from engine.onboarding.datasets import dataset_key

    return dataset_key(dataset_id, f"{PREPARED_PREFIX}{replay_identity(recipe, config)}.parquet")


@dataclass(frozen=True)
class PreparedDataset:
    """A built dataset prepared by a model's recipe: where its rows are, and what they look like."""

    source_key: str
    profile: DatasetProfile
    frame: Any
    """The prepared rows up to the use case's profiling cap - what a run validates."""
    receipt: RecipeReceipt


def prepare_dataset(
    storage: Storage,
    config: UseCaseConfig,
    dataset_id: str,
    recipe: DataRecipe,
    *,
    leak_check: LeakCheck = "narrow",
) -> PreparedDataset:
    """Run `recipe` on every row of the built dataset and store the result beside it. Raises `RecipeError`.

    `leak_check` is the scoring replay's narrow check by default; Plan O M243 (DEC-1813) passes `full` when a
    prescription's post-build recipe prepares a dataset for training, as a training file's first build does.
    """
    from engine.onboarding.datasets import DATASET_FRAME_FILENAME, dataset_key

    cap = ingest.profile_row_cap(config)
    read_all = ingest.read_upload(
        storage,
        dataset_key(dataset_id, DATASET_FRAME_FILENAME),
        file_format="parquet",
        row_cap=cap,
        keep_all_rows=True,
    )
    frame = read_all.all_rows if read_all.all_rows is not None else read_all.frame
    run = run_saved_recipe(frame, recipe, config, upload_id=dataset_id, leak_check=leak_check)
    key = prepared_dataset_key(dataset_id, recipe, config)
    try:
        with storage.open_write(key) as sink:
            consistent_types(run.frame).to_parquet(sink, index=False)
    except (ValueError, TypeError) as exc:  # pyarrow's ArrowInvalid and ArrowTypeError among them
        # The error text quotes values from the data, so it is logged by type only, never shown.
        _LOGGER.warning("recipe.dataset_write_failed dataset_id=%s error=%s", dataset_id, type(exc).__name__)
        if storage.exists(key):
            storage.delete(key)
        raise RecipeError(
            "RECIPE_STEP_INVALID",
            "The prepared dataset could not be saved: a column holds values of more than one kind.",
        ) from exc
    read = ingest.read_upload(storage, key, file_format="parquet", row_cap=cap)
    profile = ingest.profile_dataset(
        read.frame,
        config,
        upload_id=dataset_id,
        file_name=f"{dataset_id}.parquet (prepared)",
        file_format="parquet",
        file_size_bytes=storage.size_bytes(key),
        delimiter=None,
        encoding=read.encoding,
        row_count=read.row_count,
        fingerprint=read.fingerprint,
    )
    _LOGGER.info(
        "recipe.dataset_prepared dataset_id=%s recipe=%s rows=%d",
        dataset_id,
        recipe.recipe_id,
        read.row_count,
    )
    return PreparedDataset(source_key=key, profile=profile, frame=read.frame, receipt=run.receipt)


def attach_dataset_recipe(storage: Storage, run_id: str, recipe: DataRecipe, receipt: RecipeReceipt) -> None:
    """Write the recipe a dataset was prepared with, and what it did, into the scoring run's directory."""
    storage.write_model(run_key(run_id, DATA_RECIPE_FILENAME), recipe)
    storage.write_model(run_key(run_id, RECIPE_RECEIPT_FILENAME), receipt)
