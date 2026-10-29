"""Next month's file changed: the rename offered, and the copy made (Plan L follow-up #16, DEC-1597).

`engine.doctor.renames` matches a missing column to a new one; this module finds the profiles it compares
and says where the answer is shown. Nothing here changes a file or a recipe:

* **A file to score** (`model_changes`): the model's saved steps (or, with none, its columns) against the
  file the model was trained on - the upload its training run read, or the file that upload was prepared
  from. A replay that stops with `RECIPE_COLUMN_MISSING` carries the proposal in its check's `details`
  (`with_rename_suggestions`): `POST /runs` answers with it.

What the person's yes does (`write_renamed_copy`): a **copy** of the file with those columns renamed, as a
new upload - the file they sent stays as it is, and the copy names it (`renamed_copy.json`). The model's
saved steps then replay on the copy unchanged, and a copy with a remembered shape is offered that shape's
treatment. A model's recipe is never edited, and a rename step is never slipped into one: a recipe's
`rename_column` runs last (DEC-1530), after every step that reads the old name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from api.routes.uploads import (
    UPLOAD_FINGERPRINT_FILENAME,
    UPLOAD_PROFILE_FILENAME,
    UPLOAD_RECORD_FILENAME,
    profile_row_cap,
    source_filename,
)
from api.schemas import UploadRecord, UploadResponse
from engine.agent.contracts import RECIPE_RECEIPT_FILENAME, DataRecipe, RecipeReceipt
from engine.agent.dataset_recipe import consistent_types
from engine.config import UseCaseConfig
from engine.contracts import DatasetProfile, FeatureSchema, ModelVersion, RunRecord, ValidationReport
from engine.doctor.memory import (
    offer_for,
)
from engine.doctor.renames import (
    RENAMED_COPY_FILENAME,
    ColumnRename,
    FileChanges,
    RenamedCopy,
    SuggestedRename,
    UnresolvedColumn,
    match_renames,
    recipe_columns,
    unseen_values,
)
from engine.intake.contracts import IntakeReport
from engine.stages import ingest
from engine.storage import Storage, run_key, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "model_changes",
    "training_profile",
    "with_rename_suggestions",
    "write_renamed_copy",
]

_LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# The profiles compared
# ---------------------------------------------------------------------------
def _profile(storage: Storage, upload_id: str) -> DatasetProfile | None:
    key = upload_key(upload_id, UPLOAD_PROFILE_FILENAME)
    return storage.read_model(key, DatasetProfile) if storage.exists(key) else None


def _record(storage: Storage, upload_id: str) -> UploadRecord | None:
    key = upload_key(upload_id, UPLOAD_RECORD_FILENAME)
    return storage.read_model(key, UploadRecord) if storage.exists(key) else None


def training_profile(storage: Storage, version: ModelVersion) -> DatasetProfile | None:
    """The profile of the file the version was trained on, as it was sent; None when it is not kept.

    The training run's upload, or - when Guided setup prepared it - the file that upload was prepared from,
    which is what the model's saved steps read.
    """
    key = run_key(version.run_id, "run.json")
    if not storage.exists(key):
        return None
    run = storage.read_model(key, RunRecord)
    if run.upload_id is None:
        return None  # a built dataset: its columns are the onboarding build's, not a file's
    receipt_key = upload_key(run.upload_id, RECIPE_RECEIPT_FILENAME)
    source = (
        storage.read_model(receipt_key, RecipeReceipt).upload_id
        if storage.exists(receipt_key)
        else run.upload_id
    )
    return _profile(storage, source)


def _frame(storage: Storage, config: UseCaseConfig, upload_id: str) -> Any:
    record = _record(storage, upload_id)
    if record is None:
        return None
    return ingest.read_upload(
        storage, record.source_key, file_format=record.file_format, row_cap=profile_row_cap(config)
    ).frame


def _file_name(storage: Storage, upload_id: str) -> str | None:
    record = _record(storage, upload_id)
    return record.file_name if record is not None else None


def _quoted(renames: Sequence[SuggestedRename]) -> str:
    from engine.agent.untrusted import display_name

    parts = [f"'{display_name(r.candidate)}' where it had '{display_name(r.missing)}'" for r in renames]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _match(
    trained: DatasetProfile, new: DatasetProfile, missing: Sequence[str], extras: Sequence[str]
) -> tuple[tuple[SuggestedRename, ...], tuple[UnresolvedColumn, ...]]:
    wanted = set(missing)
    new_names = set(extras)
    return match_renames(
        [c for c in trained.columns if c.name in wanted],
        [c for c in new.columns if c.name in new_names],
        old_rows=trained.row_count,
        new_rows=new.row_count,
    )


# ---------------------------------------------------------------------------
# A file to score
# ---------------------------------------------------------------------------
def model_changes(
    storage: Storage,
    config: UseCaseConfig,
    version: ModelVersion,
    source: UploadRecord,
    recipe: DataRecipe | None,
    *,
    with_values: bool = True,
) -> FileChanges | None:
    """What changed in `source` (the file a scoring run reads, as sent) against the model's training file.

    Only columns the model needs are looked at: those its saved steps read and those it was trained on.
    None when nothing changed, or when the training file is no longer kept to compare with.
    """
    trained = training_profile(storage, version)
    new = _profile(storage, source.upload_id)
    if trained is None or new is None:
        return None
    schema = storage.read_model(version.schema_key, FeatureSchema)
    reads, months = recipe_columns(recipe.steps if recipe is not None else ())
    keys = [schema.primary_key] if isinstance(schema.primary_key, str) else list(schema.primary_key)
    needed = (reads | {c.name for c in schema.columns} | set(keys)) - months
    trained_names = {c.name for c in trained.columns}
    new_names = {c.name for c in new.columns}
    missing = [c.name for c in trained.columns if c.name in needed and c.name not in new_names]
    suggested, unresolved = _match(
        trained, new, missing, [c.name for c in new.columns if c.name not in trained_names]
    )
    unseen = ()
    if with_values and recipe is not None:
        unseen = _unseen(storage, config, recipe, source.upload_id, trained.upload_id, new, suggested)
    name = _file_name(storage, trained.upload_id)
    which = f"the file the model was trained on ({name})" if name else "the file the model was trained on"
    sentence = (
        f"Your file has {_quoted(suggested)} in {which}. If it is the same column, use it under the old name: "
        "a copy of your file is made with that name, and your file is left as it is."
        if suggested
        else f"Your file does not have every column of {which}." if unresolved else ""
    )
    changes = FileChanges(
        basis="model",
        source_upload_id=trained.upload_id,
        source_file_name=name,
        suggested_renames=suggested,
        unresolved=unresolved,
        unseen_values=unseen,
        sentence=sentence or _unseen_sentence(unseen),
    )
    return None if changes.empty else changes


def with_rename_suggestions(
    report: ValidationReport,
    storage: Storage,
    config: UseCaseConfig,
    version: ModelVersion,
    source: UploadRecord,
    recipe: DataRecipe | None,
) -> ValidationReport:
    """A `RECIPE_COLUMN_MISSING` report with the proposed renames in its check's `details` (additive):
    `suggested_renames` `[{missing, candidate, reason}]` and `close_candidates` `[{missing, candidates,
    reason}]`. Any other report, or one nothing is found for, is returned as it is."""
    if not report.checks or report.checks[0].code != "RECIPE_COLUMN_MISSING":
        return report
    try:
        changes = model_changes(storage, config, version, source, recipe, with_values=False)
    except Exception as exc:  # a proposal is a courtesy; it never changes the refusal it adds to
        _LOGGER.warning(
            "doctor.renames.report_failed upload=%s error=%s", source.upload_id, type(exc).__name__
        )
        return report
    if changes is None:
        return report
    check = report.checks[0]
    details = {
        **check.details,
        "suggested_renames": [
            {"missing": r.missing, "candidate": r.candidate, "reason": r.reason}
            for r in changes.suggested_renames
        ],
        "close_candidates": [
            {"missing": u.missing, "candidates": list(u.candidates), "reason": u.reason}
            for u in changes.unresolved
            if u.candidates
        ],
    }
    suggestion = (
        f"{changes.sentence} Guided setup offers it; or rename the column in your file and upload it again."
        if changes.suggested_renames
        else check.suggestion
    )
    updated = check.model_copy(update={"details": details, "suggestion": suggestion})
    return report.model_copy(update={"checks": (updated, *report.checks[1:])})


# ---------------------------------------------------------------------------
# New spellings
# ---------------------------------------------------------------------------
def _unseen(
    storage: Storage,
    config: UseCaseConfig,
    recipe: DataRecipe,
    new_upload_id: str,
    trained_upload_id: str,
    new_profile: DatasetProfile,
    suggested: Sequence[SuggestedRename],
    frame: Any = None,
) -> tuple[Any, ...]:
    from engine.agent.contracts import RecipeStepKind

    if not any(s.kind is RecipeStepKind.MAP_VALUES for s in recipe.steps):
        return ()
    old = _frame(storage, config, trained_upload_id)
    new = frame if frame is not None else _frame(storage, config, new_upload_id)
    if old is None or new is None:
        return ()  # without the training file, every value it held would read as new
    personal = frozenset(c.name for c in new_profile.columns if c.pii_kinds)
    return unseen_values(
        recipe.steps, new, old, renames={r.missing: r.candidate for r in suggested}, personal=personal
    )


def _unseen_sentence(unseen: Sequence[Any]) -> str:
    if not unseen:
        return ""
    from engine.agent.untrusted import display_name

    names = ", ".join(f"'{display_name(u.column)}'" for u in unseen)
    return (
        f"New spellings in {names}: last time's steps merge the spellings they knew and leave these as they are "
        "written. Look at them before you approve."
    )


# ---------------------------------------------------------------------------
# The person's yes: a copy with the old names
# ---------------------------------------------------------------------------


def write_renamed_copy(
    storage: Storage,
    config: UseCaseConfig,
    record: UploadRecord,
    renames: Sequence[ColumnRename],
    *,
    requested_by: str | None = None,
) -> UploadResponse:
    """`record`'s file, every row, with `renames` applied to its column names, stored as a new upload.

    The values are those the file as read holds (as a prepared upload's are, `consistent_types`); how the
    file was read (`intake.json`: the sheet, intake on or off) is carried over, so the copy has the shape
    a remembered treatment was approved on.
    """
    read = ingest.read_upload(
        storage,
        record.source_key,
        file_format=record.file_format,
        row_cap=profile_row_cap(config),
        keep_all_rows=True,
    )
    frame = read.all_rows if read.all_rows is not None else read.frame
    frame = frame.rename(columns={r.column: r.new_name for r in renames})
    copy_id = new_upload_id()
    source_key = upload_key(copy_id, source_filename("parquet"))
    with storage.open_write(source_key) as sink:
        consistent_types(frame).to_parquet(sink, index=False)
    again = ingest.read_upload(storage, source_key, file_format="parquet", row_cap=profile_row_cap(config))
    profile = ingest.profile_dataset(
        again.frame,
        config,
        upload_id=copy_id,
        file_name=record.file_name,
        file_format="parquet",
        file_size_bytes=storage.size_bytes(source_key),
        delimiter=None,
        encoding=again.encoding,
        row_count=again.row_count,
        fingerprint=again.fingerprint,
    )
    intake = (
        storage.read_model(record.intake_key, IntakeReport)
        if record.intake_key is not None and storage.exists(record.intake_key)
        else None
    )
    copy = UploadRecord(
        upload_id=copy_id,
        use_case_id=record.use_case_id,
        mode=record.mode,
        file_name=profile.file_name,
        file_format="parquet",
        file_size_bytes=profile.file_size_bytes,
        delimiter=None,
        encoding=profile.encoding,
        row_count=profile.row_count,
        column_count=profile.column_count,
        source_key=source_key,
        profile_key=upload_key(copy_id, UPLOAD_PROFILE_FILENAME),
        fingerprint_key=upload_key(copy_id, UPLOAD_FINGERPRINT_FILENAME),
        fingerprint_hash=profile.fingerprint.hash,
        created_at=utc_now(),
        synthetic=record.synthetic,
        intake_key=upload_key(copy_id, "intake.json") if intake is not None else None,
    )
    storage.write_model(copy.profile_key, profile)
    storage.write_model(copy.fingerprint_key, profile.fingerprint)
    if intake is not None and copy.intake_key is not None:
        storage.write_model(copy.intake_key, intake)
    storage.write_model(
        upload_key(copy_id, RENAMED_COPY_FILENAME),
        RenamedCopy(
            source_upload_id=record.upload_id,
            renames=tuple(renames),
            requested_by=requested_by,
            created_at=utc_now(),
        ),
    )
    storage.write_model(upload_key(copy_id, UPLOAD_RECORD_FILENAME), copy)
    treatment = offer_for(
        storage,
        use_case_id=copy.use_case_id,
        upload_id=copy_id,
        columns=profile.fingerprint.columns,
        intake=intake,
        mode=copy.mode.value,
    )
    return UploadResponse(upload_id=copy_id, profile=profile, intake=intake, treatment=treatment)
