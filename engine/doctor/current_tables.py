"""The case's current tables: what the plan leaves, which is what every rule, detector and look should read.

Until now the rules, the detectors and the AI's look tools read the upload as it came in, so an accepted fix was
flagged again, a column a query built was a list of names the AI could not look at, and the goal check guessed at
the outcome from a step's recorded column names. :func:`current_tables` is the one place that answers "what are
the case's tables now", on :func:`engine.doctor.materialize.materialize`:

* **files** (`F1`..`Fn`) - each upload as the plan's accepted fixes leave it (dates parsed, money read, rows left out),
  not the tables a query makes. A file the plan does not touch is the very same :class:`LoadedFile`; a file whose
  steps could not run stays as uploaded (the run says why, once, at the stop).
* **built tables** (`T1`..`Tn`) - the table each query makes (an accepted one, or one the AI proposed that waits for a
  yes, as if it were accepted), by its name, with its columns, its key and its time column. Each is a
  :class:`LoadedFile` of its own, so every look tool, the goal check and the egress gate treat it as a file.
* **raw** - the uploads, explicitly (``current_tables(..., raw=True)``): the intake, parser and format tools, and the
  trial of a query, which reads the uploads and applies the plan itself.

Results are cached by the plan's digest. Nothing here writes: a table is in memory only. A built table is personal in
the columns its sources have as personal (by name: strict, a column a query renames is shown as a shape only when
the gate finds it so on its own), and long text and sensitive columns are found again from the frame by the context.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import pandas as pd

from engine.agent.contracts import ProposalSource, ProposalState, RecipeStep, RecipeStepKind
from engine.agent.recipe import STEP_PHASE
from engine.agent.sql_step import SqlStepError, sql_params
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.content_cache import ContentCache, frame_digest
from engine.contracts import DatasetFingerprint

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "BUILT_PREFIX",
    "BuiltTable",
    "CurrentTables",
    "current_tables",
    "is_built",
    "two_valued_columns",
]

_LOG = logging.getLogger(__name__)
BUILT_PREFIX: Final[str] = "built:"
"""A built table's `upload_id` is this and its name: it is no upload, and no stored item may name it as one."""
_CACHE_SIZE: Final[int] = 8
_DATE_NAMED: Final[tuple[str, ...]] = ("date", "time", "_at", "day")
_VALUE_PHASE: Final[int] = 3
"""The last phase of the steps that fix values and rows (parse, set missing, fill, remove rows): the files are shown
after these only. Combining, deriving, dropping and renaming come later in the run, so a fix proposed on a table
that had them applied would name columns and rows that do not exist yet when it runs."""


def is_built(upload_id: str | None) -> bool:
    return upload_id is not None and upload_id.startswith(BUILT_PREFIX)


@dataclass(frozen=True)
class BuiltTable:
    """A table a query step makes: its rows as a file, the file it was made from, its key and its time column."""

    name: str
    loaded: LoadedFile
    origin: LoadedFile
    """The upload the query reads (the one a decision about its columns is recorded against)."""
    made_by: str | None
    key: str | None
    time_column: str | None
    two_valued: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(str(c) for c in self.loaded.frame.columns)


@dataclass(frozen=True)
class CurrentTables:
    """The case's tables as the plan leaves them."""

    files: tuple[LoadedFile, ...]
    built: tuple[BuiltTable, ...]
    raw: tuple[LoadedFile, ...]
    digest: str
    failed: tuple[str, ...] = field(default=())
    """Uploads whose steps could not run on the rows read: they stay as uploaded in :attr:`files`."""

    @property
    def tables(self) -> tuple[LoadedFile, ...]:
        """`F1`..`Fn`, then `T1`..`Tn`."""
        return (*self.files, *(b.loaded for b in self.built))

    def built_with(self, column: str) -> BuiltTable | None:
        return next((b for b in self.built if column in b.columns), None)

    def built_named(self, upload_id: str) -> BuiltTable | None:
        return next((b for b in self.built if b.loaded.upload_id == upload_id), None)


_CACHE: OrderedDict[str, CurrentTables] = OrderedDict()
_LOCK = threading.Lock()


def two_valued_columns(frame: pd.DataFrame) -> tuple[str, ...]:
    """The columns with exactly two different values (blanks aside): the shape of a yes/no."""
    found: list[str] = []
    for name in frame.columns:
        try:
            if frame[name].nunique(dropna=True) == 2:
                found.append(str(name))
        except (TypeError, ValueError):
            continue
    return tuple(found)


def _is_query(item: CaseItem) -> bool:
    step = item.payload.get("step")
    return isinstance(step, dict) and step.get("kind") == RecipeStepKind.SQL_QUERY.value


def _is_after_build(item: CaseItem) -> bool:
    return item.payload.get("stage") == "post_build"


def _live(item: CaseItem) -> bool:
    return item.state is not ProposalState.REJECTED and isinstance(item.payload.get("step"), dict)


def _fix_items(items: Sequence[CaseItem]) -> list[CaseItem]:
    """The plan that leaves each file as a file, for a fix proposed on it: accepted value and row fixes only (up to
    :data:`_VALUE_PHASE`), without a query and what runs after one, so a trial is the plan plus the candidate.
    """
    return [
        i
        for i in items
        if i.state is ProposalState.ACCEPTED
        and not _is_query(i)
        and not _is_after_build(i)
        and _phase_of(i) <= _VALUE_PHASE
    ]


def _phase_of(item: CaseItem) -> int:
    step = item.payload.get("step")
    try:
        return STEP_PHASE[RecipeStepKind(str(step["kind"]))] if isinstance(step, dict) else 0
    except (KeyError, ValueError):
        return 0


def _built_plan(items: Sequence[CaseItem], queries: Sequence[CaseItem]) -> list[CaseItem]:
    """The plan a built table is made by: what the person accepted, plus the queries as if accepted (the AI's waits
    for a yes). Other steps that wait stay out: a table must not show a change nobody approved."""
    return [
        i.model_copy(update={"state": ProposalState.ACCEPTED}) if i in queries else i
        for i in items
        if i.state is ProposalState.ACCEPTED or i in queries
    ]


def _rows_hash(frame: pd.DataFrame) -> int:
    """A number that changes when the rows read change (the same upload read again gives the same one)."""
    try:
        return int(pd.util.hash_pandas_object(frame, index=True).sum())
    except TypeError:  # unhashable cells: the shape is all that can be said
        return len(frame)


def _config_identity(config: UseCaseConfig | None) -> str:
    if config is None:
        return ""
    try:
        return sha256_hex(config.model_dump_json().encode())[:16]
    except Exception:  # an odd config is told apart by its id alone
        return str(getattr(config, "id", ""))


def _cache_key(
    case: Case,
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    decided: str | None,
    config: UseCaseConfig | None = None,
) -> str:
    """Everything the tables and their profiles read: every item of the plan (the key and outcome decisions too),
    the rows read, what is decided (`decisions(case).digest`, the as-of date among it) and the use case."""
    plan = sorted((i.item_id, i.state.value, i.payload_digest) for i in items)
    shape = [(f.upload_id, _rows_hash(f.frame), [str(c) for c in f.frame.columns]) for f in files]
    identity = [str(getattr(config, "id", "")), _config_identity(config)]
    return sha256_hex(canonical_bytes([[f.upload_id for f in case.files], plan, shape, decided, identity]))[
        :32
    ]


def _same_rows(built: pd.DataFrame, uploaded: pd.DataFrame) -> bool:
    """Whether the plan left the table as uploaded: compared by content digest (a column the plan did not touch is
    hashed once for the step), else cell by cell."""
    if built.shape != uploaded.shape:
        return False
    built_digest, uploaded_digest = frame_digest(built), frame_digest(uploaded)
    if built_digest is not None and uploaded_digest is not None:
        return built_digest == uploaded_digest
    return bool(built.equals(uploaded))


_BUILT_FINGERPRINT: Final[str] = "sha256:columns:v1"
_PROFILES: Final[ContentCache[Any]] = ContentCache(8)
"""Profiles of built tables by what the table holds, the file it came from and the use case: a plan change that
leaves a table as it was does not profile it again."""


def _reprofiled(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    like: LoadedFile,
    *,
    upload_id: str,
    file_name: str,
    row_count: int | None,
    personal: dict[str, tuple[str, ...]],
) -> Any:
    """The profile of `frame`, with the personal-data marks its sources had (by column name)."""
    whole = frame_digest(frame)
    key = (
        None
        if whole is None
        else sha256_hex(
            canonical_bytes(
                [
                    whole,
                    upload_id,
                    file_name,
                    row_count,
                    [[k, list(v)] for k, v in sorted(personal.items())],
                    _config_identity(config),
                    like.profile.file_format,
                    like.profile.file_size_bytes,
                    like.profile.delimiter,
                    like.profile.encoding,
                ]
            )
        )
    )
    return _PROFILES.get_or_compute(
        key,
        lambda: _profile_built(
            frame,
            config,
            like,
            upload_id=upload_id,
            file_name=file_name,
            row_count=row_count,
            personal=personal,
        ),
    )


def _profile_built(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    like: LoadedFile,
    *,
    upload_id: str,
    file_name: str,
    row_count: int | None,
    personal: dict[str, tuple[str, ...]],
) -> Any:
    from engine.stages.ingest import profile_dataset

    # A table the plan made is identified by what its columns hold, not by rendering every cell to text again: it
    # is never compared with an uploaded file's `sha256:v1` fingerprint (the scheme in the name says which it is).
    whole = frame_digest(frame)
    fingerprint = (
        None
        if whole is None
        else DatasetFingerprint(
            hash=f"{_BUILT_FINGERPRINT}:{whole}",
            algorithm=_BUILT_FINGERPRINT,
            n_rows=row_count if row_count is not None else len(frame),
            columns=tuple(str(c) for c in frame.columns),
        )
    )
    profile = profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=file_name,
        file_format=like.profile.file_format,
        file_size_bytes=like.profile.file_size_bytes,
        delimiter=like.profile.delimiter,
        encoding=like.profile.encoding,
        row_count=row_count if row_count is not None else len(frame),
        fingerprint=fingerprint,
    )
    columns = tuple(
        (
            c.model_copy(update={"pii_kinds": tuple(dict.fromkeys((*c.pii_kinds, *personal[c.name])))})
            if personal.get(c.name)
            else c
        )
        for c in profile.columns
    )
    return profile.model_copy(update={"columns": columns})


def _personal_of(files: Sequence[LoadedFile]) -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for loaded in files:
        for column in loaded.profile.columns:
            if column.pii_kinds:
                found[column.name] = tuple(dict.fromkeys((*found.get(column.name, ()), *column.pii_kinds)))
    return found


def _built_key(columns: Sequence[str], given: str | None) -> str | None:
    """A built table's key: the one its query names when the table has it, else an ID-named column (not a decision)."""
    if given is not None and given in columns:
        return given
    from engine.doctor.tools_facts import ID_NAMED

    return next((c for c in columns if ID_NAMED.search(c.strip())), None)


def _time_of(frame: pd.DataFrame, named: str | None) -> str | None:
    """The column of a built table that says when: the one the query was told to read the date from, else a date
    column (a real date type first, then a name that says so)."""
    if named is not None and named in frame.columns:
        return named
    for name in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[name]):
            return str(name)
    return next((str(c) for c in frame.columns if any(w in str(c).lower() for w in _DATE_NAMED)), None)


def current_tables(
    case: Case,
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    *,
    config: UseCaseConfig | None,
    raw: bool = False,
) -> CurrentTables:
    """The case's current tables (see the module). `items` are the plan's (the active section); `files` the uploads as
    the visit loaded them; `config` profiles a table the plan made (None: the uploads are returned as they are).

    Never raises for a plan that does not run: a file whose steps fail stays as uploaded and is named in
    :attr:`CurrentTables.failed`."""
    from engine.doctor.decisions import decisions
    from engine.doctor.materialize import Full, materialize

    files = tuple(files)
    if raw or config is None or not files:
        return CurrentTables(files=files, built=(), raw=files, digest="raw")
    key = _cache_key(case, items, files, decisions(case).digest, config)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached

    names: dict[str, str] = {}
    failed: tuple[str, ...] = ()
    current = list(files)
    personal = _personal_of(files)
    fixes = _fix_items(items)
    if any(i.payload.get("step") for i in fixes):
        try:
            prepared = materialize(case, fixes, files, Full(), twice=False)
        except Exception as exc:  # a plan that cannot run is the run's to say; the rules read the upload
            _LOG.info("doctor.current_tables_failed error=%s", type(exc).__name__)
        else:
            names, failed = prepared.by_upload, prepared.failed
            for position, loaded in enumerate(files):
                table = names.get(loaded.upload_id)
                frame = prepared.tables.get(table) if table is not None else None
                if loaded.upload_id in failed or frame is None or frame is loaded.frame:
                    continue
                same = _same_rows(frame, loaded.frame)
                if same:
                    continue
                try:
                    profile = _reprofiled(
                        frame,
                        config,
                        loaded,
                        upload_id=loaded.upload_id,
                        file_name=loaded.file_name,
                        row_count=loaded.file_rows,
                        personal=personal,
                    )
                except Exception as exc:  # a table that cannot be profiled is read as uploaded
                    _LOG.info("doctor.current_profile_failed error=%s", type(exc).__name__)
                    continue
                current[position] = type(loaded)(
                    upload_id=loaded.upload_id,
                    file_name=loaded.file_name,
                    frame=frame,
                    profile=profile,
                    notes=loaded.notes,
                    file_rows=loaded.file_rows,
                )
    built = _built_tables(case, items, files, config, personal)
    found = CurrentTables(
        files=tuple(current),
        built=built,
        raw=files,
        digest=key,
        failed=tuple(failed),
    )
    with _LOCK:
        _CACHE[key] = found
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return found


def _built_tables(
    case: Case,
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    config: UseCaseConfig,
    personal: dict[str, tuple[str, ...]],
) -> tuple[BuiltTable, ...]:
    from engine.doctor.materialize import Full, materialize

    queries = [
        i
        for i in items
        if _is_query(i) and _live(i) and (i.state is ProposalState.ACCEPTED or i.source is ProposalSource.AI)
    ]
    if not queries:
        return ()
    plan = _built_plan(items, queries)
    try:
        prepared = materialize(case, plan, files, Full(), twice=False)
    except Exception as exc:
        _LOG.info("doctor.built_tables_failed error=%s", type(exc).__name__)
        return ()
    made: list[BuiltTable] = []
    for item in queries:
        try:
            params = sql_params(RecipeStep.model_validate(item.payload["step"]).params)
        except (SqlStepError, ValueError):
            continue
        frame = prepared.tables.get(params.output_name)
        if frame is None:
            continue
        origin = next(
            (f for f in files if item.upload_id is not None and f.upload_id == item.upload_id),
            files[0] if item.upload_id is None else None,
        )
        if origin is None:
            continue
        mine = next((f for f in prepared.files if f.upload_id == origin.upload_id), None)
        columns = [str(c) for c in frame.columns]
        name = params.output_name
        try:
            from engine.doctor.built_lineage import personal_outputs

            marks = personal_outputs(params.sql, columns, personal)
            profile = _reprofiled(
                frame,
                config,
                origin,
                upload_id=BUILT_PREFIX + name,
                file_name=name,
                row_count=len(frame),
                personal=marks,
            )
        except Exception as exc:
            _LOG.info("doctor.built_profile_failed error=%s", type(exc).__name__)
            continue
        loaded = type(origin)(
            upload_id=BUILT_PREFIX + name, file_name=name, frame=frame, profile=profile, notes=()
        )
        made.append(
            BuiltTable(
                name=name,
                loaded=loaded,
                origin=origin,
                made_by=item.item_id,
                key=_built_key(columns, mine.key if mine is not None else None),
                time_column=_time_of(
                    frame, params.as_of_rule.column if params.as_of_rule is not None else None
                ),
                two_valued=two_valued_columns(frame),
            )
        )
    return tuple(made)


def labels_for(current: CurrentTables) -> list[str]:
    """`F1`..`Fn`, then `T1`..`Tn`: what the AI calls each table."""
    return [
        *(f"F{n}" for n in range(1, len(current.files) + 1)),
        *(f"T{n}" for n in range(1, len(current.built) + 1)),
    ]


def current_for(
    case: Case, files: Sequence[LoadedFile], context_for: Callable[[Case, tuple[LoadedFile, ...]], Any]
) -> CurrentTables:
    """The current tables for a caller that has only the case's files and `context_for` (the AI review and chat):
    the use case config comes from a context over the first file."""
    from engine.doctor.visit import active_section

    if not files:
        return CurrentTables(files=(), built=(), raw=(), digest="raw")
    try:
        config = context_for(case, (files[0],)).config
        return current_tables(case, active_section(case)[0], files, config=config)
    except Exception as exc:  # never costs the review its files
        _LOG.info("doctor.current_for_failed error=%s", type(exc).__name__)
        return CurrentTables(files=tuple(files), built=(), raw=tuple(files), digest="raw")
