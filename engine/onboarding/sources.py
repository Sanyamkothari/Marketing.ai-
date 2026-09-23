"""Source reading, profiling and role detection (Phase 2 plan section 6.1, M8).

A client's raw table starts life as an opaque file: this module turns it into a
:class:`~engine.onboarding.specs.SourceProfile` - the Phase 1 profile plus three ranked guesses
(what could be the entity key, what could be the event time, what role the table plays) that the
mapping screen shows the user rather than asking them to type from scratch. Nothing here decides
anything; every guess carries a confidence and `reasons`, and the user confirms or overrides it
(the onboarding package's "suggest, never decide silently" rule).

:func:`profile_source` is a thin wrapper around the Phase 1 profiler
(:func:`engine.stages.ingest.profile_dataset`) - reused whole, not re-implemented, so a client's raw
file and a Phase 1 upload are described by exactly the same numbers and inherit the same PII
redaction. The candidate finders it calls (:func:`key_candidates`, :func:`time_candidates`,
:func:`detect_roles`) are free functions taking plain data (a profile, a data frame, a role
catalogue) rather than a `Storage`, so a test can drive them with an in-memory frame and never touch
a filesystem.

:class:`SourceReader` is the seam Phase 4 needs: it names three operations (`read`, `profile`, and
`read_profiled`, which is both in one pass for the build) without saying where the bytes come from, so an S3 or warehouse reader can be dropped in later beside
:class:`FileSourceReader` without a single line of onboarding logic - mapping, role detection,
build - changing to accommodate it.

Two more free functions, :func:`join_coverage` and :func:`key_format_mismatch`, do not belong to a
single source: they compare an event source's key column against the entity source's, which is a
question that can only be asked once both are mapped. They live here because they read the same raw
columns this module already knows how to profile, and because the checks they feed
(`JOIN_KEY_COVERAGE_LOW`, `KEY_FORMAT_MISMATCH`) are the direct sequel to role detection - a role
that turns out to be an event table is only useful once its key actually joins to the entity table.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from engine.config import RoleCatalogue, RoleSpec, UseCaseConfig, get_roles
from engine.onboarding.specs import (
    KeyCandidate,
    RoleCandidate,
    SourceProfile,
    SourceSpec,
    TimeCandidate,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pandas as pd

    from engine.contracts import DatasetFingerprint, DatasetProfile
    from engine.stages.ingest import ReadResult
    from engine.storage import Storage

__all__ = [
    "JOIN_COVERAGE_OK",
    "MIN_TIME_PARSE_RATE",
    "FileSourceReader",
    "ProfiledRead",
    "SourceReader",
    "detect_roles",
    "join_coverage",
    "key_candidates",
    "key_format_mismatch",
    "profile_source",
    "time_candidates",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_TIME_PARSE_RATE: Final[float] = 0.90
"""Floor for a column to count as a time candidate at all.

Looser than Phase 1's own `ingest.DATETIME_PARSE_RATE` (0.95, not part of that module's public
surface) on purpose: Phase 1 is deciding a column's *type*, where a false positive corrupts a
feature; this module is deciding what to *ask the user about*, where a false negative is the worse
mistake - a real event-time column with a handful of stray bad rows must still reach the mapping
screen as a candidate, or nobody ever sees the column that needs a fix.
"""

JOIN_COVERAGE_OK: Final[float] = 0.8
"""The bar `JOIN_KEY_COVERAGE_LOW` uses for "acceptable" (plan section 7). Shared with
`key_format_mismatch` so a transform this module reports is one that actually clears the check that
flagged the mismatch in the first place - a fix that raises coverage from 0.3 to 0.6 is progress but
is not itself the answer."""

_ENTITY_RATIO_MAX: Final[float] = 1.2
"""`rows_per_key` at or below this reads as "about one row per key" - an entity-shaped table."""

_EVENT_RATIO_MIN: Final[float] = 1.5
"""`rows_per_key` at or above this reads as "several rows share a key" - an event-shaped table."""

_FILENAME_WEIGHT: Final[float] = 0.35
_TYPICAL_COLUMNS_WEIGHT: Final[float] = 0.25
_TIME_WEIGHT: Final[float] = 0.20
_RATIO_WEIGHT: Final[float] = 0.20
"""The four role-detection signals (plan section 6.1), weighted so a role matching every signal
scores 1.0 and a table with no signal for any role never silently favours one over another. A role
whose `typical_columns` is empty (`entity`, `other_event`) can never collect
`_TYPICAL_COLUMNS_WEIGHT`, which is deliberate: a generic role has no typical shape to match, so its
ceiling is honestly lower than a role with a real one - `detect_roles` never invents a signal to
close that gap."""

_FALLBACK_CONFIDENCE: Final[float] = 0.05
"""Confidence attached to the `other_event`/`entity` fallback (plan section 6.1): low enough that a
single real signal for any other role would have outscored it, so the UI can show it exactly as what
it is - a shrug, not a finding."""


# ---------------------------------------------------------------------------
# Key candidates
# ---------------------------------------------------------------------------
_ID_LIKE_NAME: Final[re.Pattern[str]] = re.compile(
    r"(^id$|_id$|^id_|_key$|^key$|customer|cust)", re.IGNORECASE
)
"""Same shape as `engine.stages.ingest.DEFAULT_ID_LIKE_PATTERN`, plus a bare `key`/`_key` match:
Phase 1 never needs that spelling because a Phase 1 file already carries a header we control, but a
client's own key column is very often literally called `key` or `entity_key`."""


def key_candidates(profile: DatasetProfile) -> tuple[KeyCandidate, ...]:
    """Every column that could identify a row of `profile`, best first (plan section 6.1).

    Nothing is filtered down to "the" key: `detect_roles` reads the best candidate's
    `rows_per_key` to tell an entity table (about 1) from an event table (far above), and a column
    that never becomes the entity key can still turn out to be an event table's join key once the
    entity source is known, so throwing away plausible columns here would silently narrow both
    later questions. Only a column with zero non-null values is dropped, because `rows_per_key`
    divides by `distinct_count` and a column nobody ever filled in identifies nothing.

    Ranked by, in order: lower null rate, an id-like name, and `rows_per_key` closer to 1 - the
    order plan section 6.1 gives, and the one that puts a clean, fully-populated, one-row-per-value
    column (the shape an entity key actually has) ahead of a column that merely happens to look
    id-like on its name.
    """
    row_count = profile.row_count
    scored: list[tuple[tuple[float, int, float, int], KeyCandidate]] = []
    for column in profile.columns:
        if column.distinct_count == 0:
            continue
        rows_per_key = row_count / column.distinct_count
        candidate = KeyCandidate(
            column=column.name,
            distinct_count=column.distinct_count,
            null_rate=column.null_rate,
            rows_per_key=round(rows_per_key, 4),
        )
        id_like_rank = 0 if _ID_LIKE_NAME.search(column.name) else 1
        closeness = abs(rows_per_key - 1.0)
        key = (round(column.null_rate, 6), id_like_rank, round(closeness, 6), column.position)
        scored.append((key, candidate))
    scored.sort(key=lambda item: item[0])
    return tuple(candidate for _, candidate in scored)


# ---------------------------------------------------------------------------
# Time candidates
# ---------------------------------------------------------------------------
_TIME_CANDIDATE_TYPES: Final[frozenset[str]] = frozenset({"date", "datetime", "string", "text"})
"""Types worth trying to parse as dates. Deliberately excludes `numeric` and `boolean`: Phase 1's own
`infer_column_type` resolves numeric text before it ever tries a datetime parse (its branch 8
precedes branch 9), so a column it already typed as numeric would disagree with itself if this
module then called it a time candidate."""


def _day_first_ambiguous(values: pd.Series[Any]) -> bool:
    """True when parsing `values` day-first and month-first both succeed on every row, and at
    least one row reads differently the two ways - `03/04/2025` is such a row, `15/04/2025` is not,
    because 15 cannot be a month and the two readings collapse to the same date (plan section 6.1,
    `DATE_FORMAT_AMBIGUOUS`).

    Comparing the two parsed results directly, rather than inspecting each value's two numeric
    components for `<= 12`, is the same test made robust to delimiter and field order: it also
    catches a value like `2025-04-03` staying unambiguous under both readings (ISO order pins the
    year, so `dayfirst` cannot change the answer) without this function having to know the format.
    """
    import pandas as pd

    day_first = pd.to_datetime(values, errors="coerce", dayfirst=True, format="mixed")
    month_first = pd.to_datetime(values, errors="coerce", dayfirst=False, format="mixed")
    if bool(day_first.isna().any()) or bool(month_first.isna().any()):
        return False
    return bool((day_first != month_first).any())


def time_candidates(frame: pd.DataFrame, profile: DatasetProfile) -> tuple[TimeCandidate, ...]:
    """Every column of `frame` that parses as a date, best (highest parse rate) first.

    Needs the raw frame and not just `profile`: a `ColumnProfile` never carries a date column's
    actual values (`minimum`/`maximum` are numeric-only), and `day_first_ambiguous` can only be
    answered by parsing every value two ways and comparing, which a five-value sample could not
    tell honestly either way.
    """
    import pandas as pd

    candidates: list[TimeCandidate] = []
    for column in profile.columns:
        if column.inferred_type.value not in _TIME_CANDIDATE_TYPES:
            continue
        observed = frame[column.name].dropna()
        if observed.empty:
            continue
        text = observed.astype(str)
        parsed = pd.to_datetime(text, errors="coerce", format="mixed")
        parse_rate = float(parsed.notna().mean())
        if parse_rate < MIN_TIME_PARSE_RATE:
            continue
        valid = parsed.dropna()
        candidates.append(
            TimeCandidate(
                column=column.name,
                parse_rate=round(parse_rate, 4),
                earliest=valid.min().date() if not valid.empty else None,
                latest=valid.max().date() if not valid.empty else None,
                day_first_ambiguous=_day_first_ambiguous(text),
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.parse_rate, candidate.column))
    return tuple(candidates)


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------
def _filename_hits(file_name: str, role: RoleSpec) -> tuple[str, ...]:
    """`role.name_tokens` found in `file_name`'s stem, substring match on the lower-cased whole.

    A token match, not substring, would miss "CUST-Master (v2).csv" against `master` - real client
    file names carry version tags, spaces and separators `roles.yaml`'s tokens were never written to
    anticipate, and a stray non-match here costs only a smaller filename-signal weight, not a wrong
    role, because three other signals still vote.
    """
    stem = Path(file_name).stem.lower()
    return tuple(token for token in role.name_tokens if token in stem)


def _typical_column_hits(columns: Sequence[str], role: RoleSpec) -> tuple[str, ...]:
    lowered = {name.lower() for name in columns}
    return tuple(name for name in role.typical_names if name.lower() in lowered)


def _score_role(profile: SourceProfile, role_name: str, role: RoleSpec) -> RoleCandidate | None:
    score = 0.0
    reasons: list[str] = []

    filename_hits = _filename_hits(profile.file_name, role)
    if filename_hits:
        score += _FILENAME_WEIGHT
        reasons.append(f"file name contains {filename_hits[0]!r}")

    typical_hits: tuple[str, ...] = ()
    if role.typical_names:
        column_names = tuple(column.name for column in profile.profile.columns)
        typical_hits = _typical_column_hits(column_names, role)
        if typical_hits:
            share = len(typical_hits) / len(role.typical_names)
            score += _TYPICAL_COLUMNS_WEIGHT * share
            reasons.append(f"has typical column(s) {', '.join(typical_hits)}")

    best_time_rate = profile.time_candidates[0].parse_rate if profile.time_candidates else 0.0
    if role.is_event:
        # A timestamp says "this table is dated", not "this table is bills rather than payments" -
        # every event role would otherwise collect the same credit from the same one column, and the
        # role list would carry no information beyond "something with a date". Requiring a
        # role-specific hit first (a name token or a typical column) keeps the timestamp as
        # confirming evidence for a role the other two signals already pointed at, and leaves a
        # table with a date but no other clue to the tie-break below and, failing that, the fallback.
        if best_time_rate > 0 and (filename_hits or typical_hits):
            score += _TIME_WEIGHT * best_time_rate
            column = profile.time_candidates[0].column
            reasons.append(f"{column!r} parses as a date at {best_time_rate:.0%}, a usable event time")
    elif best_time_rate < 0.5:
        score += _TIME_WEIGHT * 0.5
        reasons.append("no dominant timestamp column, consistent with one row per entity")

    if profile.key_candidates:
        best_key = profile.key_candidates[0]
        ratio = best_key.rows_per_key
        if role.is_event and ratio >= _EVENT_RATIO_MIN:
            score += _RATIO_WEIGHT
            reasons.append(f"about {ratio:.1f} rows per {best_key.column}, typical of an event log")
        elif not role.is_event and ratio <= _ENTITY_RATIO_MAX:
            score += _RATIO_WEIGHT
            reasons.append(f"about 1 row per {best_key.column}")

    if score <= 0.0:
        return None
    return RoleCandidate(role=role_name, confidence=round(min(score, 1.0), 4), reasons=tuple(reasons))


def detect_roles(profile: SourceProfile, roles: RoleCatalogue) -> tuple[RoleCandidate, ...]:
    """Every role `profile` could plausibly play, confidence descending, never empty (plan section 6.1).

    Scores four signals per role - a file-name token match, a share of `typical_columns` present,
    the strongest time candidate's parse rate (for an event role, evidence for, once a name or
    column hit has already pointed at that role; for the entity role, evidence a dominant one is
    *absent*), and the best key candidate's `rows_per_key` sitting near 1 (entity) or well above it
    (event) - and returns every role that collected any of them, strongest first. A role that
    matches nothing scores nothing and is left out, rather than padded to a baseline: a list the
    caller can read as "every real signal found" only holds when a zero score truly means zero
    evidence. Ties (a table whose only signal - a date, a rows-per-key ratio - is shared by every
    event role equally, none of them named or typed by a hit) resolve to `other_event` first: that
    role's whole reason to exist is "fits none of the roles above" (`configs/roles.yaml`), so a tie
    among generic evidence is exactly its case, not an arbitrary one to break alphabetically.

    When every role scores zero the table still has to go somewhere on the mapping screen, so this
    falls back to `other_event` when a time candidate exists and the catalogue has an event role at
    all, or to the catalogue's `entity` role otherwise, at `_FALLBACK_CONFIDENCE` - a number chosen
    low enough that any real signal for any other role would have outscored it. In practice a dated
    table with an event role in the catalogue is already caught by the tie-break above (that role
    collects the ratio and, once something has named it, the time signal too); reaching this point at
    all means the catalogue itself is too thin to name anything, which is also why the entity branch
    gives one reason rather than two - "no event role available" and "no date column found" would
    both be true of a catalogue with no event role, and only one of them is honest to say when a date
    column *is* present but has nowhere in that catalogue to go (`_score_role`'s own absence-of-a-
    dominant-time bonus already means the entity role only reaches this branch scoreless when a
    dominant date column exists, so "no date column" would be a fabrication - house rule 2 - on the
    one path this branch is ever reached).
    """
    scored = [
        candidate
        for role_name, role in roles.roles.items()
        if (candidate := _score_role(profile, role_name, role)) is not None
    ]
    scored.sort(
        key=lambda candidate: (-candidate.confidence, candidate.role != "other_event", candidate.role)
    )
    if scored:
        return tuple(scored)

    if profile.time_candidates and roles.event_roles:
        fallback_role = "other_event" if "other_event" in roles.roles else roles.event_roles[0]
        reason = (
            "the detector was not confident about any role; this table has a date column, so it "
            "defaulted to a generic event log"
        )
    else:
        fallback_role = roles.entity_role
        reason = "the detector was not confident about any role, so it defaulted to the entity table"
    return (RoleCandidate(role=fallback_role, confidence=_FALLBACK_CONFIDENCE, reasons=(reason,)),)


# ---------------------------------------------------------------------------
# profile_source
# ---------------------------------------------------------------------------
def profile_source(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    source_id: str,
    client_id: str,
    file_name: str,
    file_format: Literal["csv", "parquet"] = "csv",
    file_size_bytes: int = 0,
    delimiter: str | None = None,
    encoding: str = "utf-8",
    row_count: int | None = None,
    fingerprint: DatasetFingerprint | None = None,
    roles: RoleCatalogue | None = None,
) -> SourceProfile:
    """`SourceProfile` of `frame`: the Phase 1 profile plus key, time and role candidates.

    `config` is only ever read for its `.catalog` (`profile_dataset`'s time-like column-name
    pattern) - genuinely use-case-independent engine data (DEC-038's `UseCaseConfig.catalog`
    resolves to the one root-wide `Catalog` whichever use case loaded it) - because a source is
    profiled before any use case has been chosen for it; `config.primary_key_hints` and the like are
    Phase 1 training-file hints this module does not read, since `key_candidates` and
    `time_candidates` rank every source independently of them.

    `DatasetProfile.upload_id` carries `source_id`: a source *is* the Phase 2 counterpart of a Phase
    1 upload, so the same field means the same thing rather than gaining a second name.
    """
    from engine.stages.ingest import profile_dataset

    dataset_profile = profile_dataset(
        frame,
        config,
        upload_id=source_id,
        file_name=file_name,
        file_format=file_format,
        file_size_bytes=file_size_bytes,
        delimiter=delimiter,
        encoding=encoding,
        row_count=row_count,
        fingerprint=fingerprint,
    )
    partial = SourceProfile(
        source_id=source_id,
        client_id=client_id,
        file_name=file_name,
        profile=dataset_profile,
        key_candidates=key_candidates(dataset_profile),
        time_candidates=time_candidates(frame, dataset_profile),
    )
    catalogue = get_roles() if roles is None else roles
    return partial.model_copy(update={"role_candidates": detect_roles(partial, catalogue)})


# ---------------------------------------------------------------------------
# Join coverage and key-format mismatch
# ---------------------------------------------------------------------------
def join_coverage(event_keys: pd.Series[Any], entity_keys: pd.Series[Any]) -> float:
    """Share of `event_keys`' non-null values found in `entity_keys` (plan section 6.1,
    `JOIN_KEY_COVERAGE_LOW`).

    The denominator is non-null event rows, not every row: a null join key is
    `JOIN_KEY_UNMAPPED`'s problem, and folding it into this ratio would make one bad mapping look
    like a coverage problem too. An event source with no non-null keys at all has nothing this ratio
    can measure, so it reads as `1.0` (vacuously nothing failed) rather than `0.0` - the honest
    zero-rows-mapped problem is a different check's to raise.
    """
    non_null = event_keys.dropna()
    if len(non_null) == 0:
        return 1.0
    entities = entity_keys.dropna()
    if _integer_keys(non_null) and non_null.dtype == entities.dtype:
        matched = non_null.isin(entities.unique()).sum()
    else:
        matched = _as_key_text(non_null).isin(set(_as_key_text(entities))).sum()
    return round(float(matched) / len(non_null), 4)


def _integer_keys(keys: pd.Series[Any]) -> bool:
    """Whether `keys` is a plain numpy integer column - which can hold no null, and no text."""
    import numpy as np

    return isinstance(keys.dtype, np.dtype) and keys.dtype.kind in "iu"


def _as_key_text(keys: pd.Series[Any]) -> pd.Series[Any]:
    """`keys.astype(str)`, without the copy when every key already is text.

    Keys are compared as text so that `7` and `"7"` join the way a client means them to. Two
    shortcuts give that same answer without turning every key into a string, and a build asks for a
    coverage about five times per event table - once when the tables are mapped and again by both
    key checks, before and after the features are built (docs/PERFORMANCE.md): `str()` of a string
    is that string, so a text column is its own text; and two integer columns hold the same text
    exactly when they hold the same integers, so `join_coverage` compares two integer columns of
    one dtype directly (one dtype, so that nothing is widened to a float on the way).
    """
    import pandas as pd

    if keys.dtype == object and pd.api.types.infer_dtype(keys, skipna=False) == "string":
        return keys
    return keys.astype(str)


_KEY_TRANSFORMS: Final[dict[str, Callable[[str], str]]] = {
    "strip": lambda value: value.strip(),
    "lower": lambda value: value.lower(),
    "lstrip_zeros": lambda value: value.lstrip("0") or "0",
}
"""In `TransformKind` order, so two callers trying the same near-miss key never disagree about which
transform wins when more than one would clear the bar."""


def key_format_mismatch(event_keys: pd.Series[Any], entity_keys: pd.Series[Any]) -> str | None:
    """The `TransformKind` name that would fix a near-miss join, or `None` (plan section 6.1,
    `KEY_FORMAT_MISMATCH`).

    `None` means one of two different things the caller does not need to tell apart: the raw
    coverage already clears `JOIN_COVERAGE_OK` (nothing to fix), or no single transform gets it
    there (the mismatch is not a format problem this module knows how to name). Each candidate
    transform is judged by the same bar `JOIN_KEY_COVERAGE_LOW` itself uses, so a transform this
    function names is one a user who applies it can trust actually clears that check.
    """
    if join_coverage(event_keys, entity_keys) > JOIN_COVERAGE_OK:
        return None
    events = event_keys.dropna().astype(str)
    entities = entity_keys.dropna().astype(str)
    for name, transform in _KEY_TRANSFORMS.items():
        if join_coverage(events.map(transform), entities.map(transform)) > JOIN_COVERAGE_OK:
            return name
    return None


# ---------------------------------------------------------------------------
# SourceReader
# ---------------------------------------------------------------------------
@runtime_checkable
class SourceReader(Protocol):
    """Everything onboarding needs from wherever a source's bytes live.

    Naming only `read`, `profile` and `read_profiled` - never a storage backend, a connection string or a file
    format - is what lets Phase 4 add `S3SourceReader` or `AthenaSourceReader` beside
    `FileSourceReader` without a single line of mapping, role-detection or build logic changing to
    accommodate it; every caller in this package already holds a `SourceReader`, not a `Storage`.
    """

    def read(self, source: SourceSpec, *, max_rows: int | None = None) -> pd.DataFrame: ...

    def profile(self, source: SourceSpec) -> SourceProfile: ...

    def read_profiled(self, source: SourceSpec) -> ProfiledRead: ...


@dataclass(frozen=True)
class ProfiledRead:
    """Every row of one source and its profile, from one pass over the file.

    What a build needs from a source is both: every row, to aggregate, and the profile, for its
    fingerprint and for which columns are personal data. Asking `read` and then `profile` answers
    the same question with two passes over the same bytes - at the M14 benchmark size that second
    pass was a third of the whole build (docs/PERFORMANCE.md) - so a reader answers it with one.
    `profile` must be exactly what `SourceReader.profile` returns for the same source.
    """

    frame: pd.DataFrame
    profile: SourceProfile


UNBOUNDED_ROWS: Final[int] = sys.maxsize
"""`row_cap` for a read that must return the whole table: a build may never see a truncated source."""


class FileSourceReader:
    """`SourceReader` over an uploaded CSV/Parquet file, reusing `engine.stages.ingest` unchanged.

    `use_case` is required rather than defaulted to some arbitrary shipped config, even though
    `profile_source` reads only its use-case-independent `.catalog` (see `profile_source`'s
    docstring): fabricating a placeholder `UseCaseConfig` here, or silently picking "whichever use
    case loads first", is exactly the kind of invented value house rule 2 forbids, so the caller -
    who already has a real `UseCaseConfig` on hand wherever a `FileSourceReader` is built - names one
    explicitly instead.
    """

    def __init__(self, storage: Storage, use_case: UseCaseConfig) -> None:
        self._storage = storage
        self._use_case = use_case

    def read(self, source: SourceSpec, *, max_rows: int | None = None) -> pd.DataFrame:
        """Every row of the source, unless the caller asks for a bounded preview.

        `read_upload` defaults `row_cap` to the *profiling* cap, which is right for profiling - a
        profile of two million rows describes a file as well as a profile of ten million, and
        `DatasetProfile.row_count` stays exact either way (DEC-046). It is wrong here. A build
        aggregates these rows, so a source read short produces feature values computed from part of
        the history, for a subset of the entities, with nothing anywhere saying so: at 200,000
        customers the benchmark read exactly 2,000,000 of each event table and the build failed
        seven checks it should have passed. The cap is lifted explicitly rather than by accident.
        """
        from engine.stages.ingest import read_upload

        return read_upload(
            self._storage,
            source.storage_key,
            file_format=source.file_format,
            max_rows=max_rows,
            row_cap=UNBOUNDED_ROWS,
        ).frame

    def profile(self, source: SourceSpec) -> SourceProfile:
        from engine.stages.ingest import read_upload

        return self._profile_of(
            source, read_upload(self._storage, source.storage_key, file_format=source.file_format)
        )

    def read_profiled(self, source: SourceSpec) -> ProfiledRead:
        """Every row, as `read` returns them, and the profile, as `profile` returns it - in one pass.

        The read is `profile`'s own read - same default row cap, so the same profiled rows and the
        same fingerprint - asked to keep the rows above the cap as well. The profile is therefore
        not "a profile of the whole table", which would differ from the one the Sources screen showed
        the user for any file past the cap; it is the same profile, arrived at without reading the
        file a second time. The rows are every row: the cap is still never a build's cap (DEC-108).
        """
        from engine.stages.ingest import read_upload

        result = read_upload(
            self._storage, source.storage_key, file_format=source.file_format, keep_all_rows=True
        )
        # `keep_all_rows` on an unbounded read always sets `all_rows`; this narrows the Optional.
        if result.all_rows is None:  # pragma: no cover
            raise RuntimeError(f"the read of {source.source_id} returned no rows beyond its profile")
        return ProfiledRead(frame=result.all_rows, profile=self._profile_of(source, result))

    def _profile_of(self, source: SourceSpec, result: ReadResult) -> SourceProfile:
        return profile_source(
            result.frame,
            self._use_case,
            source_id=source.source_id,
            client_id=source.client_id,
            file_name=source.file_name,
            file_format=result.file_format,
            file_size_bytes=self._storage.size_bytes(source.storage_key),
            delimiter=result.delimiter,
            encoding=result.encoding,
            row_count=result.row_count,
            fingerprint=result.fingerprint,
        )
