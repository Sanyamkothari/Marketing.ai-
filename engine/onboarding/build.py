"""The build stage (Phase 2 plan section 6.5, M13): one onboarding spec, executed end to end.

    apply mappings -> snapshot dates -> features -> labels -> assemble -> validate -> write

Every other module in this package answers one question about a client's data. This one is the only
thing that runs them in order, and so it is the only thing that can get the *order* wrong - which is
the failure mode that matters here, because it is invisible. A dataset built with the label read
before the features were bounded, or with a censored snapshot left in, trains a model that scores
beautifully and predicts nothing, and nothing downstream will ever say so.

Three things are therefore done here and nowhere else.

**The leak probe.** `engine.onboarding.features` generates the point-in-time clause once and asserts
it is present in every query it hands back. That assertion reads the SQL; it cannot see a leak that
the SQL does not spell - a `derive` feature that reads a client's whole event history, a snapshot
frame whose dates were widened after the queries were compiled. So after the frame is built, the
same feature spec is run a second time against tables carrying extra rows dated *after the last
snapshot*, and any feature whose value moves is FUTURE_EVENTS_LEAKED: an error, never a warning, and
never acknowledgeable. A build that has seen the future is not a build with a caveat.

**What stops a build.** `engine.onboarding.validate`'s checks are pure functions that never raise and
never decide; deciding is this module's job. Any check of severity `error` means `passed=False`, no
`dataset.parquet` and no manifest - only a `build_report.json` saying what was wrong, because the
user's next action is reading it. The structural checks are run twice on purpose: once as soon as the
mapped tables exist, so a recipe that cannot be built (no entity table, an unmapped entity key, a
customer master with repeated ids) is refused before a single aggregate is computed rather than
crashing the aggregation; and once at the validate stage with everything the build measured, which
is the run whose findings the report carries.

**It is a job.** `build_status.json` is rewritten at every stage transition, exactly as
`engine.pipeline` rewrites a run's `status.json`, and `cancel` is polled either side of every stage,
so the Build screen's poll always reads a whole document and a cancelled build stops at a boundary
with a status that says so.

Nothing here branches on a use-case id or a client id: the roles come from `configs/roles.yaml`, the
thresholds from `UseCaseConfig.onboarding`, and the shape of the dataset from the spec being executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from engine.config import ColumnType, RunMode, StandardType, get_roles
from engine.contracts import RunState, Severity
from engine.onboarding.datasets import DatasetError, build_manifest
from engine.onboarding.features import (
    SNAPSHOT_VIEW,
    build_features,
    compile_feature_sql,
    render_sql_file,
)
from engine.onboarding.labels import LabelError, build_labels
from engine.onboarding.mapping import apply_mapping
from engine.onboarding.snapshots import build_snapshot_frame, plan_snapshots, scoring_snapshot
from engine.onboarding.sources import join_coverage
from engine.onboarding.specs import (
    BuildReport,
    BuildStage,
    BuildStatus,
    DatasetColumn,
    FeatureSpec,
    FeatureStat,
    MappingSpec,
    OnboardingCheck,
    OnboardingSpec,
    SnapshotMode,
    SnapshotStat,
    SourceProfile,
    SourceSpec,
    SourceStat,
    TransformKind,
)
from engine.onboarding.transforms import TransformError, cast_series, derive
from engine.onboarding.validate import OnboardingCheckParams, SourceFacts, run_onboarding_checks
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import datetime
    from collections.abc import Mapping, Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd
    from duckdb import DuckDBPyConnection

    from engine.config import UseCaseConfig
    from engine.jobs import CancelToken
    from engine.onboarding.datasets import DatasetRegistry
    from engine.onboarding.sources import SourceReader

__all__ = ["BUILD_STAGES", "build_dataset"]

logger = get_logger(__name__)

ENTITY_KEY: Final[str] = "entity_key"
EVENT_TIME: Final[str] = "event_time"
SNAPSHOT_COLUMN: Final[str] = "snapshot_date"

SIGNUP_COLUMN: Final[str] = "signup_date"
"""The entity's start date, under the name `configs/roles.yaml` gives it in the entity role's
`optional_columns`. Naming it here is not a branch on a use case - it is the same engine-owned
vocabulary as `entity_key` and `event_time` - and it is read only to keep snapshot rows dated before
a customer existed out of the spine (`snapshots.build_snapshot_frame`). A client whose mapping does
not produce it simply gets no such filter, and that module says so."""

BUILD_STAGES: Final[Mapping[str, str]] = {
    "apply_mappings": "Reading your tables",
    "snapshots": "Choosing the dates to build",
    "features": "Building features",
    "labels": "Working out the outcome",
    "assemble": "Assembling the dataset",
    "validate": "Checking the dataset",
    "write": "Saving the dataset",
}
"""Stage key to the Build-screen row it belongs to. A feature stage is `features:<role>` - one row of
work per table the client actually mapped - and every one of them groups under `features`."""

_PROBE_ROWS: Final[int] = 500
"""Real event rows re-dated after the last snapshot for the leak probe. A broken time bound is not
selective - it leaks for every row of every entity - so a few hundred genuine rows, carrying values
that satisfy whatever `where` filters the features use, are as decisive as a copy of the whole table
and do not double a client's extract in memory to prove it."""

_PROBE_CONTROL_ENTITIES: Final[int] = 500
"""Entities given *no* future row whose snapshot rows the leak probe rebuilds anyway, beside every
entity that was given one (`_probe_rows`). A time bound that is missing leaks within each entity and
the injected entities alone would show it; these are there for a query that reads across entities,
which would move a feature of an entity that had nothing added. Five hundred is the same order as
`_PROBE_ROWS`: a join that lost its entity condition moves every entity, so any few hundred are
decisive."""

_STANDARD_TYPES: Final[Mapping[ColumnType, StandardType]] = {
    ColumnType.INTEGER: StandardType.NUMERIC,
    ColumnType.FLOAT: StandardType.NUMERIC,
    ColumnType.BOOLEAN: StandardType.BOOLEAN,
    ColumnType.DATE: StandardType.DATE,
    ColumnType.DATETIME: StandardType.DATE,
    ColumnType.STRING: StandardType.CATEGORICAL,
    ColumnType.TEXT: StandardType.TEXT,
}

_Origin: TypeAlias = Literal["key", "mapped", "derived", "feature", "label"]

_SEVERITY_RANK: Final[Mapping[Severity, int]] = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

_PANEL_INAPPLICABLE_CODES: Final[frozenset[str]] = frozenset({"PK_NOT_UNIQUE"})
"""Phase 1 findings that a periodic dataset answers by design rather than by fault.

`CheckParams.primary_key` is a single column, so the Phase 1 re-run is told the entity key; on a
periodic dataset that key repeats once per snapshot date, which is what `snapshot_mode: periodic`
means. The row identifier is `(entity_key, snapshot_date)` and `DatasetManifest.primary_key` records
it as such. Carrying the finding anyway would block every panel build on a question the params cannot
express - see `docs/CROSS_BRANCH_REQUESTS.md` on the composite key. Every other Phase 1 finding is
carried across untouched."""


# ---------------------------------------------------------------------------
# build_status.json
# ---------------------------------------------------------------------------
class _StatusWriter:
    """Holds the `BuildStatus` and rewrites `build_status.json` on every transition.

    The same shape as `engine.pipeline._StatusWriter`, for the same reason: the write is atomic, so
    the Build screen's poll always reads a whole document rather than a half-written one.
    """

    def __init__(self, registry: DatasetRegistry, status: BuildStatus) -> None:
        self._registry = registry
        self._status = status
        self._current: str | None = None
        self._write()

    def start(self, key: str) -> None:
        self._current = key
        self._apply(key, {"state": RunState.RUNNING, "started_at": utc_now()}, current=key)

    def finish(self, key: str, *, detail: str, seconds: float) -> None:
        self._current = None
        self._apply(
            key,
            {
                "state": RunState.DONE,
                "ended_at": utc_now(),
                "duration_seconds": round(seconds, 3),
                "detail": detail,
            },
            current=None,
            detail=detail,
        )

    def stop(self, state: RunState, *, error: str | None, detail: str) -> None:
        """End the build: the stage that was running takes `state`, every pending stage is skipped."""
        changes: dict[str, object] = {"state": state, "ended_at": utc_now()}
        self._apply(self._current, changes, current=None, skip_pending=True, run_state=state, detail=detail)
        if error is not None:
            self._status = self._status.model_copy(update={"error": error})
            self._write()
        self._current = None

    def _apply(
        self,
        key: str | None,
        changes: dict[str, object],
        *,
        current: str | None,
        skip_pending: bool = False,
        run_state: RunState | None = None,
        detail: str | None = None,
    ) -> None:
        rows: list[BuildStage] = []
        for row in self._status.stages:
            if row.key == key:
                rows.append(row.model_copy(update=changes))
            elif skip_pending and row.state is RunState.PENDING:
                rows.append(row.model_copy(update={"state": RunState.SKIPPED}))
            else:
                rows.append(row)
        stages = tuple(rows)
        done = sum(1 for row in stages if row.state is RunState.DONE)
        self._status = self._status.model_copy(
            update={
                "stages": stages,
                "state": _build_state(stages) if run_state is None else run_state,
                "current_stage": current,
                "progress_pct": round(100 * done / len(stages)) if stages else 0,
                "detail": self._status.detail if detail is None else detail,
                "updated_at": utc_now(),
            }
        )
        self._write()

    def _write(self) -> None:
        self._registry.write_status(self._status.dataset_id, self._status)


def _build_state(stages: Sequence[BuildStage]) -> RunState:
    """The build-level state implied by its stages while it is still running.

    A build that stopped names its own state (`_StatusWriter.stop`), so the only states reachable
    here are the ones a stage passes through on the way to finishing.
    """
    states = {row.state for row in stages}
    if states == {RunState.DONE}:
        return RunState.DONE
    return RunState.PENDING if states == {RunState.PENDING} else RunState.RUNNING


def _stage_keys(spec: OnboardingSpec, *, feature_roles: Sequence[str], mode: RunMode) -> tuple[str, ...]:
    """Every stage this build will run, in order.

    The list is settled before any data is read, because `progress_pct` is stages done over stages
    total and a denominator that grows as the build discovers work would make the bar go backwards.
    A scoring build has no `labels` stage at all rather than a skipped one: there is no label to
    work out, and a row on the screen for work that was never going to happen is not progress.
    """
    labels = () if mode is RunMode.SCORE or spec.label_spec is None else ("labels",)
    return (
        "apply_mappings",
        "snapshots",
        *(f"features:{role}" for role in feature_roles),
        *labels,
        "assemble",
        "validate",
        "write",
    )


def _initial_status(spec: OnboardingSpec, *, dataset_id: str, keys: Sequence[str]) -> BuildStatus:
    return BuildStatus(
        dataset_id=dataset_id,
        client_id=spec.client_id,
        spec_id=spec.spec_id,
        state=RunState.PENDING,
        updated_at=utc_now(),
        stages=tuple(
            BuildStage(
                key=key,
                title=_stage_title(key),
                group_label=BUILD_STAGES[key.split(":", 1)[0]],
                state=RunState.PENDING,
            )
            for key in keys
        ),
        progress_pct=0,
        detail="Waiting to start.",
    )


def _stage_title(key: str) -> str:
    head, _, role = key.partition(":")
    return f"Features from {role}" if role else BUILD_STAGES[head]


# ---------------------------------------------------------------------------
# Stage 1: the mapped tables
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Mapped:
    """One source after its mapping was applied: what the checks read, and what the queries read.

    `frame` is the mapped table exactly as `mapping.apply_mapping` produced it, because that is what
    EVENT_TIME_UNPARSEABLE and DATE_FORMAT_AMBIGUOUS have to see - a date the client wrote and nobody
    pinned a format for is still text there. `table` is the same frame with `event_time` cast, which
    is what the point-in-time comparison needs to mean anything. Keeping both is the difference
    between reporting an unreadable date column and quietly dropping it.
    """

    source: SourceSpec
    mapping: MappingSpec
    rows_read: int
    frame: pd.DataFrame
    table: pd.DataFrame
    coverage: float | None

    @property
    def role(self) -> str:
        return self.mapping.role


def _event_time_format(mapping: MappingSpec) -> str | None:
    column = mapping.by_standard(EVENT_TIME)
    if column is None or column.transform is None or column.transform.kind is not TransformKind.CAST:
        return None
    return column.transform.date_format


def _apply_mappings(
    *,
    config: UseCaseConfig,
    sources: Sequence[SourceSpec],
    mappings: Sequence[MappingSpec],
    reader: SourceReader,
    entity_role: str,
    dataset_id: str,
    sample_entities: int | None,
) -> tuple[tuple[_Mapped, ...], tuple[OnboardingCheck, ...], dict[str, SourceProfile]]:
    """Read every source, apply its mapping, and measure how well each event table joins.

    Join coverage is measured before any sampling, on the whole of both tables: a preview that
    sampled first would compare an event table against a fraction of the customers it belongs to and
    report a coverage collapse the client's data does not have.

    Each source is read once, with its profile (`SourceReader.read_profiled`), and the profiles are
    returned for the write stage - which needs them for the manifest's source fingerprints and for
    which columns are personal data. It used to ask `reader.profile` for them there, which read and
    fingerprinted every source a second time: 30 of the 152 seconds of a profiled build at 20,000
    customers (docs/PERFORMANCE.md).
    """
    by_source = {mapping.source_id: mapping for mapping in mappings}
    checks: list[OnboardingCheck] = []
    profiles: dict[str, SourceProfile] = {}
    read: list[tuple[SourceSpec, MappingSpec, int, pd.DataFrame]] = []
    for source in sources:
        mapping = by_source.get(source.source_id)
        if mapping is None:
            raise DatasetError(
                "DATASET_MAPPING_MISSING",
                f"The table {source.file_name!r} is part of this recipe but no mapping says what its "
                "columns mean, so it cannot be read. Map it on the mapping screen and build again.",
                dataset_id=dataset_id,
            )
        profiled = reader.read_profiled(source)
        raw = profiled.frame
        profiles[source.source_id] = profiled.profile
        result = apply_mapping(
            raw,
            mapping,
            schema=config.standard_schema,
            auto_accept_confidence=config.onboarding.mapping.auto_accept_confidence,
        )
        checks.extend(check.model_copy(update={"source_id": source.source_id}) for check in result.checks)
        logger.info(
            "onboarding.build.mapped source_id=%s role=%s rows=%d dropped_columns=%d",
            source.source_id,
            mapping.role,
            len(result.frame),
            len(result.dropped),
        )
        read.append((source, mapping, len(raw), result.frame))

    entity_keys = next(
        (
            frame[ENTITY_KEY]
            for _s, mapping, _r, frame in read
            if mapping.role == entity_role and ENTITY_KEY in frame.columns
        ),
        None,
    )
    keep = _sampled_keys(entity_keys, sample_entities)
    universe = None if entity_keys is None else _key_universe(entity_keys)
    tables: list[_Mapped] = []
    for source, mapping, rows, frame in read:
        if ENTITY_KEY not in frame.columns and mapping.role != entity_role:
            continue
        sampled = _only(frame, keep)
        tables.append(
            _Mapped(
                source=source,
                mapping=mapping,
                rows_read=rows,
                frame=sampled,
                table=_castable(sampled, mapping),
                coverage=(
                    None
                    if mapping.role == entity_role or universe is None
                    else join_coverage(frame[ENTITY_KEY], universe)
                ),
            )
        )
    return tuple(tables), tuple(checks), profiles


def _key_universe(entity_keys: pd.Series[Any]) -> pd.Series[Any]:
    """The entity keys once, distinct and non-null, for every event table's coverage to be read against.

    `join_coverage` reduces its second argument to the set of its distinct keys as text, so handing
    it this instead of the whole entity column answers exactly the same question - and the reduction
    is paid once per build rather than once per event table.
    """
    return entity_keys.dropna().drop_duplicates()


def _sampled_keys(keys: pd.Series[Any] | None, sample_entities: int | None) -> set[object] | None:
    if keys is None or sample_entities is None:
        return None
    return set(keys.dropna().drop_duplicates().head(sample_entities))


def _only(frame: pd.DataFrame, keep: set[object] | None) -> pd.DataFrame:
    if keep is None or ENTITY_KEY not in frame.columns:
        return frame
    return frame.loc[frame[ENTITY_KEY].isin(keep)].reset_index(drop=True)


def _castable(frame: pd.DataFrame, mapping: MappingSpec) -> pd.DataFrame:
    """The mapped table with `event_time` as a timestamp, which is what every query compares against.

    A mapping usually pins a `cast`, and casting an already-cast column costs nothing; a mapping that
    did not is the case this exists for. Reaching DuckDB with `event_time` still text would either
    fail the whole build on the first unreadable value or compare strings against a timestamp, and
    neither says "these dates could not be read" - EVENT_TIME_UNPARSEABLE does, from `_Mapped.frame`,
    while the value that could not be read becomes `NaT` here and the guard's `IS NOT NULL` filter
    leaves it out of every aggregate.
    """
    if EVENT_TIME not in frame.columns:
        return frame
    cast = cast_series(frame[EVENT_TIME], StandardType.DATE, date_format=_event_time_format(mapping))
    return frame.assign(**{EVENT_TIME: cast.values})


def _register(con: DuckDBPyConnection, tables: Sequence[_Mapped]) -> dict[str, pd.DataFrame]:
    """One view per role. Two sources mapped to one role are stacked, never silently replaced."""
    import pandas as pd

    by_role: dict[str, list[pd.DataFrame]] = {}
    for mapped in tables:
        by_role.setdefault(mapped.role, []).append(mapped.table)
    views = {
        role: frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        for role, frames in by_role.items()
    }
    for role, frame in views.items():
        con.register(role, frame)
    return views


# ---------------------------------------------------------------------------
# Stage 2: the snapshot dates
# ---------------------------------------------------------------------------
def _event_span(
    views: Mapping[str, pd.DataFrame], *, entity_role: str, dataset_id: str
) -> tuple[datetime.date, datetime.date]:
    """How far the client's event data runs, over every event table at once.

    One span for the whole build, not one per table: the snapshot dates are a property of the
    extract, and taking them per role would place the last snapshot of a client whose complaints file
    stops early somewhere their billing file could still have answered for.
    """
    import pandas as pd

    times = [
        frame[EVENT_TIME].dropna()
        for role, frame in views.items()
        if role != entity_role and EVENT_TIME in frame.columns
    ]
    stamps = pd.concat(times) if times else None
    if stamps is None or stamps.empty:
        raise DatasetError(
            "DATASET_NO_EVENT_DATES",
            "None of the event tables in this recipe carry a date that could be read, so there is "
            "no point in time to build a row at. Map the date column of at least one event table, "
            "or correct the dates it holds, and build again.",
            dataset_id=dataset_id,
        )
    first, last = pd.Timestamp(stamps.min()), pd.Timestamp(stamps.max())
    return first.date(), last.date()


# ---------------------------------------------------------------------------
# Stage 5: assembling
# ---------------------------------------------------------------------------
def _entity_attributes(views: Mapping[str, pd.DataFrame], *, entity_role: str) -> pd.DataFrame:
    """The entity table's mapped columns, less its own `event_time`.

    `event_time` on an entity table is the date its row became true, not an attribute of the
    customer; carrying it into the dataset would put a column nobody asked for in front of the model.
    ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED is what tells the user whether it was there.
    """
    frame = views[entity_role]
    return frame.loc[:, [name for name in frame.columns if name != EVENT_TIME]]


def _derived(frame: pd.DataFrame, config: UseCaseConfig, *, entity_role: str) -> tuple[str, ...]:
    """Compute, in place, each standard column the use case says can be worked out rather than mapped.

    `engine.onboarding.mapping` does not list a derivable column as missing when nobody mapped it,
    precisely because this happens here; without it, a column the use case declares would be absent
    from the dataset and nothing would have said so. A derivation whose inputs the client did not map
    either is left out rather than failed on - an optional column nobody can compute is the same
    absence as an optional column nobody mapped, and a required one is already
    REQUIRED_STANDARD_COLUMN_UNMAPPED.
    """
    added: list[str] = []
    for column in config.standard_schema.columns:
        rule = column.derivable
        if rule is None or rule.from_role != entity_role or column.name in frame.columns:
            continue
        try:
            frame[column.name] = derive(frame, rule.expression, snapshot_column=SNAPSHOT_COLUMN)
        except TransformError as exc:
            logger.info("onboarding.build.derive_skipped column=%s code=%s", column.name, exc.code)
            continue
        added.append(column.name)
    return tuple(added)


def _null_fractions(frame: pd.DataFrame, names: Sequence[str]) -> dict[str, float]:
    return {name: round(float(frame[name].isna().mean()), 4) for name in names}


# ---------------------------------------------------------------------------
# Stage 6: the leak probe
# ---------------------------------------------------------------------------
def _leak_probe(
    views: Mapping[str, pd.DataFrame],
    snapshots: pd.DataFrame,
    spec: FeatureSpec,
    features: pd.DataFrame,
    *,
    entity_role: str,
    inclusive: bool,
) -> dict[str, int]:
    """Rebuild every feature against event tables carrying rows dated after the last snapshot.

    This is the check the SQL assertion cannot make. `assert_point_in_time` reads the generated query
    and refuses one whose guard is missing; it cannot see a feature that never became SQL - a
    `derive` expression reads its role's whole history through a join with no time bound at all - and
    it cannot see a snapshot frame that was widened after compilation. Running the real spec against
    real rows moved into the future answers the only question that matters: did a value change?

    The rows injected are the client's own, re-dated, so whatever `where` filter a feature carries is
    as satisfiable in the future as it was in the past. A feature that moves is attributed to its
    role, and the count reported is the number of future rows that role was given - the events that
    were counted when they should not have been.

    Two things keep the probe from costing a second build (docs/PERFORMANCE.md, DEC-096). The future
    rows are stacked onto the client's table inside DuckDB (`_stack_future_rows`) rather than with
    `pd.concat`, which copied every event table whole - five million usage rows, to add five
    hundred - only for DuckDB to read the copy. And the features are rebuilt only for the snapshot
    rows that can tell: those of the entities the future rows belong to, plus as many again that
    were given none (`_probe_rows`). Every feature query joins an event to its own entity, so a
    value can only move for an entity that was given a future row, and those rows are all probed -
    the verdict for a broken time bound, a `derive` over the whole history or a widened snapshot
    frame is the one the whole spine would give. The entities given nothing are there for the leak
    no feature query was written to have: one that reads *other* entities' events would move them,
    and a probe of only the injected entities could not see it. The build's own features are cut
    to the same entities by key (`_probed_features`), never by position: a `derive` makes that
    frame a row per event rather than a row per snapshot.
    """
    import duckdb
    import pandas as pd

    after = pd.Timestamp(snapshots[SNAPSHOT_COLUMN].max()) + pd.Timedelta(days=1)
    injected: dict[str, int] = {}
    witnesses: list[pd.Series[Any]] = []
    con = duckdb.connect()
    try:
        for role, frame in views.items():
            if role == entity_role or EVENT_TIME not in frame.columns or frame.empty:
                con.register(role, frame)
                continue
            extra = frame.head(_PROBE_ROWS).assign(**{EVENT_TIME: after})
            injected[role] = len(extra)
            if ENTITY_KEY in extra.columns:
                witnesses.append(extra[ENTITY_KEY])
            _stack_future_rows(con, role, frame, extra)
        rows = _probe_rows(snapshots[ENTITY_KEY], witnesses)
        con.register(SNAPSHOT_VIEW, snapshots.loc[rows].reset_index(drop=True))
        probed = build_features(con, spec, inclusive=inclusive)
    finally:
        con.close()

    built = _probed_features(features, snapshots[ENTITY_KEY], rows)
    role_of = {feature.name: feature.role for feature in spec.features}
    moved = {role_of[name] for name in built.columns if name in role_of and _moved(built[name], probed[name])}
    return {role: count for role, count in injected.items() if role in moved}


def _probe_rows(keys: pd.Series[Any], witnesses: Sequence[pd.Series[Any]]) -> npt.NDArray[np.bool_]:
    """Which snapshot rows the leak probe rebuilds: every row of an entity given a future event, and
    every row of up to `_PROBE_CONTROL_ENTITIES` entities that were given none.

    Should no snapshot row match an entity a future row was given - keys the pandas comparison here
    reads differently from DuckDB's join, say - the answer is every row: the probe is never allowed
    to become weaker than the one that rebuilt the whole spine, only cheaper than it.
    """
    import numpy as np
    import pandas as pd

    if not witnesses:
        return np.ones(len(keys), dtype=bool)
    injected = keys.isin(pd.concat(witnesses, ignore_index=True).dropna().unique())
    if not bool(injected.any()):
        return np.ones(len(keys), dtype=bool)
    control = keys[~injected].drop_duplicates().head(_PROBE_CONTROL_ENTITIES)
    return (injected | keys.isin(control)).to_numpy(dtype=bool)


def _probed_features(
    features: pd.DataFrame, keys: pd.Series[Any], rows: npt.NDArray[np.bool_]
) -> pd.DataFrame:
    """The rows of `features` that belong to the entities of the probed snapshot `rows`, in the
    order `features` has them.

    Selected by entity rather than by position, because `features` is not one row per snapshot row:
    a `derive` feature joins every snapshot row to every event of its role, so the frame the build
    assembled has a row per event, not per snapshot, and a mask of the snapshot rows does not line up
    with it. Every row of a probed entity is kept - its snapshot rows and every event row a `derive`
    joined to them - which is exactly what `build_features` returns for the same entities on the
    probe's side, so the two are compared like for like and a `derive` that gained the future rows
    shows up as a frame that grew. When every snapshot row is probed nothing is selected at all: the
    frame is compared whole, as the probe that rebuilt the whole spine compared it.
    """
    if bool(rows.all()):
        return features.reset_index(drop=True)
    return features.loc[features[ENTITY_KEY].isin(keys[rows].unique())].reset_index(drop=True)


def _stack_future_rows(con: DuckDBPyConnection, role: str, frame: pd.DataFrame, extra: pd.DataFrame) -> None:
    """Make `role` a view of `frame`'s rows followed by `extra`'s, without copying `frame`.

    Registering a pandas frame costs nothing - DuckDB scans it where it lies - so the only question
    is the column types. DuckDB infers an object column's type from its values, and `extra` is five
    hundred rows of the same frame: a text column that happens to be empty in those rows reads as
    INTEGER there, and a stack of two differently typed halves is a different table from the one
    the build queried. So every column of the future rows is cast to the type DuckDB gave the
    client's own table, and the stacked view has the types the build's own queries saw.
    """
    past, future = f"_probe_past_{role}", f"_probe_future_{role}"
    con.register(past, frame)
    con.register(future, extra)
    described = con.execute(f"DESCRIBE {_quoted(past)}").fetchall()
    casts = ", ".join(
        f"CAST({_quoted(str(row[0]))} AS {row[1]}) AS {_quoted(str(row[0]))}" for row in described
    )
    con.execute(
        f"CREATE VIEW {_quoted(role)} AS "
        f"SELECT * FROM {_quoted(past)} UNION ALL SELECT {casts} FROM {_quoted(future)}"
    )


def _quoted(name: str) -> str:
    """A DuckDB identifier, double-quoted with any embedded quote doubled."""
    return '"' + name.replace('"', '""') + '"'


_PROBE_RTOL: Final[float] = 1e-9
"""How far a float feature may differ between two builds of the same data and still be the same.

DuckDB aggregates in parallel, so the order it sums a column in is not fixed between runs: building
the same features twice over identical, untouched tables returns floats that differ in their last
bits. Measured at 240,000 rows over 3,000,000 events, the largest such difference was 5.5e-12 on a
sum of order 500 - about 1e-15 relative, which is arithmetic noise and not a leak.

An exact comparison therefore reports every float feature as moved as soon as a build is large
enough for DuckDB to use more than one thread, and FUTURE_EVENTS_LEAKED is the one finding a user
may never acknowledge - so it would make large builds impossible and blame the engine's own
correctness check for it. This tolerance is a thousand times the observed noise and still many
orders of magnitude below any real leak: an event that should not have been counted moves a count
by a whole 1 and a mean by a real amount, never by a part in a billion.
"""


def _moved(built: pd.Series[Any], probed: pd.Series[Any]) -> bool:
    """Did a feature change when events dated after the last snapshot were added?

    Counts, dates and categories are compared exactly - a count that changes at all has counted
    something it should not have. Floats are compared to `_PROBE_RTOL`, for the reason recorded
    there. A value that appears or disappears is a change whatever its type, so the null positions
    are compared exactly in both cases.
    """
    import numpy as np
    import pandas as pd

    if built.isna().to_numpy().tolist() != probed.isna().to_numpy().tolist():
        return True
    if not pd.api.types.is_float_dtype(built) or not pd.api.types.is_float_dtype(probed):
        return not built.equals(probed)
    present = built.notna().to_numpy()
    return not bool(
        np.allclose(
            built.to_numpy(dtype=float)[present],
            probed.to_numpy(dtype=float)[present],
            rtol=_PROBE_RTOL,
            atol=0.0,
            equal_nan=True,
        )
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def _check_params(
    config: UseCaseConfig,
    tables: Sequence[_Mapped],
    *,
    entity_role: str,
    label_name: str,
    snapshot_mode: SnapshotMode,
    feature_names: Sequence[str] = (),
    null_fractions: Mapping[str, float] | None = None,
    future_event_rows: Mapping[str, int] | None = None,
) -> OnboardingCheckParams:
    """Copy the measured facts and the `onboarding.*` thresholds into one flat params object.

    `engine.onboarding.validate` has no `params_from_config` on purpose - a check knows about data,
    never about a config - so the copying happens here, by name, once.
    """
    onboarding = config.onboarding
    return OnboardingCheckParams(
        sources=tuple(
            SourceFacts(
                source_id=mapped.source.source_id,
                role=mapped.role,
                rows=mapped.source.rows,
                mapped_standard=mapped.mapping.standard_names,
                frame=mapped.frame,
                event_time_format=_event_time_format(mapped.mapping),
            )
            for mapped in tables
        ),
        feature_names=tuple(feature_names),
        feature_null_fractions=dict(null_fractions or {}),
        future_event_rows=dict(future_event_rows or {}),
        label_name=label_name,
        required_standard_columns=tuple(column.name for column in config.standard_schema.required_columns),
        entity=config.entity,
        entity_role=entity_role,
        snapshot_mode=snapshot_mode,
        max_features=onboarding.features.max_features,
        drop_if_null_fraction_above=onboarding.features.drop_if_null_fraction_above,
        max_source_rows=onboarding.limits.max_source_rows,
        max_sources=onboarding.limits.max_sources,
    )


def _phase_one_checks(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    dataset_id: str,
    target: str | None,
    mode: RunMode,
    periodic: bool,
) -> tuple[OnboardingCheck, ...]:
    """The full Phase 1 validation, re-run on the assembled dataset (plan section 7).

    The dataset this build just made is the file a Phase 1 run would otherwise have been handed, so
    it is held to exactly the same standard: too few rows, too few positives, a constant column, a
    feature that predicts the target almost perfectly. That last one is why this is not optional - a
    leak the point-in-time probe cannot see, because it came in through a mapped attribute rather
    than an event time, arrives here as LEAKAGE_SUSPECTED, and an error is what it deserves.
    """
    from engine.stages.validate import params_from_config, validate_frame
    from engine.utils.ids import seed_from

    params = params_from_config(
        config,
        primary_key=ENTITY_KEY,
        target=target,
        seed=seed_from(dataset_id),
        row_count=len(frame),
    )
    report = validate_frame(frame, params, mode=mode, upload_id=dataset_id)
    return tuple(
        OnboardingCheck.from_validation_check(check)
        for check in report.checks
        if not (periodic and check.code in _PANEL_INAPPLICABLE_CODES)
    )


def _column_types(frame: pd.DataFrame) -> dict[str, ColumnType]:
    """Every column's Phase 1 type, inferred once for the write stage.

    Two things there need it - the manifest's column types, and the dataset fingerprint, whose
    schema line is exactly these types - and each used to infer them for itself: over a
    60-feature dataset `infer_column_type` is a numeric parse of every value of every column, and
    doing it twice was a tenth of the write stage (docs/PERFORMANCE.md). Same function, same frame,
    so the same answer, once.
    """
    from engine.stages.ingest import infer_column_type

    return {str(name): infer_column_type(frame[name]) for name in frame.columns}


def _dataset_columns(
    frame: pd.DataFrame,
    types: Mapping[str, ColumnType],
    *,
    keys: Sequence[str],
    mapped: Sequence[str],
    derived: Sequence[str],
    features: Sequence[str],
    target: str | None,
) -> tuple[DatasetColumn, ...]:
    origins: dict[str, _Origin] = dict.fromkeys(keys, "key")
    origins.update(dict.fromkeys(mapped, "mapped"))
    origins.update(dict.fromkeys(derived, "derived"))
    origins.update(dict.fromkeys(features, "feature"))
    if target is not None:
        origins[target] = "label"
    return tuple(
        DatasetColumn(name=str(name), type=_STANDARD_TYPES[types[str(name)]], origin=origins[str(name)])
        for name in frame.columns
    )


def _pii_columns(tables: Sequence[_Mapped], profiles: Mapping[str, SourceProfile]) -> frozenset[str]:
    """Standard names whose source column the Phase 1 profiler flagged as personal data.

    `sample.json` is read by a screen, so the redaction the Setup preview already applies has to
    follow the column across the rename: a customer id that was PII in the client's file is still PII
    once it is called `entity_key`.
    """
    flagged: set[str] = set()
    for mapped in tables:
        profile = profiles[mapped.source.source_id]
        pii = {column.name for column in profile.profile.columns if column.pii_kinds}
        flagged.update(column.standard for column in mapped.mapping.columns if column.source in pii)
    return frozenset(flagged)


def _report(
    *,
    dataset_id: str,
    checks: Sequence[OnboardingCheck],
    tables: Sequence[_Mapped],
    snapshots: Sequence[SnapshotStat],
    features: Sequence[FeatureStat],
    frame: pd.DataFrame | None,
    duration_s: float,
    features_sql_path: str | None,
) -> BuildReport:
    ordered = sorted(checks, key=lambda check: _SEVERITY_RANK[check.severity])
    errors = sum(1 for c in ordered if c.severity is Severity.ERROR and not c.acknowledged)
    return BuildReport(
        dataset_id=dataset_id,
        checks=tuple(ordered),
        sources=tuple(
            SourceStat(
                source_id=mapped.source.source_id,
                role=mapped.role,
                rows=mapped.rows_read,
                fingerprint=mapped.source.fingerprint.hash,
                join_coverage=mapped.coverage,
            )
            for mapped in tables
        ),
        snapshots=tuple(snapshots),
        features=tuple(features),
        rows_out=0 if frame is None else len(frame),
        entities_out=0 if frame is None else int(frame[ENTITY_KEY].nunique()),
        duration_s=round(duration_s, 3),
        features_sql_path=features_sql_path,
        error_count=errors,
        warning_count=sum(1 for c in ordered if c.severity is Severity.WARNING),
        passed=errors == 0,
        built_at=utc_now(),
    )


def _cancelled(cancel: CancelToken | None) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------
def build_dataset(
    *,
    spec: OnboardingSpec,
    config: UseCaseConfig,
    sources: Sequence[SourceSpec],
    mappings: Sequence[MappingSpec],
    reader: SourceReader,
    registry: DatasetRegistry,
    dataset_id: str,
    mode: RunMode,
    cancel: CancelToken | None = None,
    sample_entities: int | None = None,
) -> BuildReport:
    """Execute `spec` and return the report; write the dataset only if nothing blocking was found.

    `mode` decides one thing: a scoring build skips labels entirely and stands at a single date at
    the end of the new data (`OnboardingSpec.scoring_snapshot_spec`), because there is no outcome to
    look forward to and nothing to censor. Everything else - the mappings, the snapshot spine, the
    features and the point-in-time boundary between them - is identical in both modes, which is the
    only way a row scored next month is the same shape as the rows the model was fitted on.

    `sample_entities` caps the build to that many entities for the Setup screen's preview. It is a
    filter on the spine and on everything that joins to it, never on the rows read, so the numbers a
    preview reports are the real numbers for the entities it covers rather than a fraction of them.
    A sampled build does not re-run the Phase 1 validation: every judgement it makes - how many rows,
    how many positives, how imbalanced, how predictive - is about the whole dataset, and answering it
    from two hundred customers would put "this file has too few rows" in front of a user whose file
    has plenty. The onboarding checks, which are about the mapping and the recipe, still run.

    A scoring build keeps the recipe's own `snapshot_mode`, and therefore its `snapshot_date` column
    and its primary key, even though it stands at a single date. The scoring frame has to carry the
    columns the model was fitted on; dropping one because this particular build has one date would
    make every scoring run a schema mismatch.

    The report is returned and written whatever happens. A build whose checks found an error writes
    `build_report.json` and nothing else: there is no dataset, and the report is what the user reads.
    """
    import duckdb

    started = perf_counter()
    roles = get_roles()
    entity_role = roles.entity_role
    mapped_roles = {mapping.role for mapping in mappings}
    feature_roles = tuple(sorted(set(spec.feature_spec.roles) & mapped_roles))
    periodic = spec.snapshot_spec.mode is SnapshotMode.PERIODIC
    label = None if mode is RunMode.SCORE else spec.label_spec
    target = None if label is None else label.name
    keys = [ENTITY_KEY, SNAPSHOT_COLUMN]

    status = _StatusWriter(
        registry,
        _initial_status(
            spec, dataset_id=dataset_id, keys=_stage_keys(spec, feature_roles=feature_roles, mode=mode)
        ),
    )
    con = duckdb.connect()
    try:
        # --- apply_mappings -------------------------------------------------
        began = perf_counter()
        status.start("apply_mappings")
        _cancelled(cancel)
        tables, mapping_checks, profiles = _apply_mappings(
            config=config,
            sources=sources,
            mappings=mappings,
            reader=reader,
            entity_role=entity_role,
            dataset_id=dataset_id,
            sample_entities=sample_entities,
        )
        views = _register(con, tables)
        checks: list[OnboardingCheck] = list(mapping_checks)
        _cancelled(cancel)
        status.finish(
            "apply_mappings",
            detail=f"{len(tables)} table(s) read and mapped.",
            seconds=perf_counter() - began,
        )

        structural = run_onboarding_checks(
            _check_params(
                config,
                tables,
                entity_role=entity_role,
                label_name=target or "",
                snapshot_mode=spec.snapshot_spec.mode,
            )
        )
        if any(check.severity is Severity.ERROR for check in structural):
            return _stop(
                status,
                registry,
                _report(
                    dataset_id=dataset_id,
                    checks=checks + list(structural),
                    tables=tables,
                    snapshots=(),
                    features=(),
                    frame=None,
                    duration_s=perf_counter() - started,
                    features_sql_path=None,
                ),
            )

        # --- snapshots ------------------------------------------------------
        began = perf_counter()
        status.start("snapshots")
        _cancelled(cancel)
        first_event, last_event = _event_span(views, entity_role=entity_role, dataset_id=dataset_id)
        horizon = 0 if label is None else label.horizon_days or 0
        if mode is RunMode.SCORE:
            dates = scoring_snapshot(spec.scoring_snapshot_spec(), last_event=last_event)
        else:
            plan = plan_snapshots(
                spec.snapshot_spec,
                first_event=first_event,
                last_event=last_event,
                horizon_days=horizon,
            )
            dates = plan.dates
            checks.extend(plan.checks)
        entities = _entity_attributes(views, entity_role=entity_role)
        signup = SIGNUP_COLUMN if SIGNUP_COLUMN in entities.columns else None
        snapshots = build_snapshot_frame(entities, dates, signup_column=signup)
        con.register(SNAPSHOT_VIEW, snapshots)
        _cancelled(cancel)
        status.finish(
            "snapshots",
            detail=f"{len(dates)} snapshot date(s), {len(snapshots):,} rows to build.",
            seconds=perf_counter() - began,
        )

        # --- features -------------------------------------------------------
        features = snapshots
        for role in feature_roles:
            began = perf_counter()
            status.start(f"features:{role}")
            _cancelled(cancel)
            of_role = spec.feature_spec.for_role(role)
            part = build_features(
                con,
                FeatureSpec(features=of_role),
                inclusive=spec.snapshot_spec.inclusive_snapshot_time,
            )
            features = features.merge(part, on=keys, how="left")
            _cancelled(cancel)
            status.finish(
                f"features:{role}",
                detail=f"{len(of_role)} feature(s) built from {role}.",
                seconds=perf_counter() - began,
            )
        features_sql_path = registry.write_features_sql(
            dataset_id,
            render_sql_file(
                compile_feature_sql(
                    spec.feature_spec,
                    roles=mapped_roles,
                    inclusive=spec.snapshot_spec.inclusive_snapshot_time,
                )
            ),
        )

        # --- labels ---------------------------------------------------------
        labelled: pd.DataFrame | None = None
        per_snapshot: tuple[SnapshotStat, ...] = ()
        if label is not None:
            began = perf_counter()
            status.start("labels")
            _cancelled(cancel)
            result = build_labels(
                con,
                label,
                snapshots,
                drop_censored=config.onboarding.labels.drop_censored,
                mapped_roles=mapped_roles,
                mode=spec.snapshot_spec.mode,
                inclusive=spec.snapshot_spec.inclusive_snapshot_time,
            )
            labelled, per_snapshot = result.frame, result.per_snapshot
            checks.extend(result.checks)
            _cancelled(cancel)
            status.finish(
                "labels",
                detail=f"{len(labelled):,} rows labelled across {len(per_snapshot)} date(s).",
                seconds=perf_counter() - began,
            )

        # --- assemble -------------------------------------------------------
        began = perf_counter()
        status.start("assemble")
        _cancelled(cancel)
        frame = snapshots.merge(entities, on=ENTITY_KEY, how="left")
        attributes = tuple(name for name in entities.columns if name != ENTITY_KEY)
        derived = _derived(frame, config, entity_role=entity_role)
        frame = frame.merge(features, on=keys, how="left")
        if labelled is not None:
            frame = frame.merge(labelled, on=keys, how="inner")
        built = tuple(name for name in spec.feature_spec.names if name in frame.columns)
        fractions = _null_fractions(frame, built)
        limit = config.onboarding.features.drop_if_null_fraction_above
        dropped = tuple(name for name in built if fractions[name] > limit)
        frame = frame.drop(columns=list(dropped))
        if not periodic:
            frame = frame.drop(columns=[SNAPSHOT_COLUMN])
        frame = frame.reset_index(drop=True)
        _cancelled(cancel)
        status.finish(
            "assemble",
            detail=f"{len(frame):,} rows and {len(frame.columns)} columns assembled.",
            seconds=perf_counter() - began,
        )

        # --- validate -------------------------------------------------------
        began = perf_counter()
        status.start("validate")
        _cancelled(cancel)
        leaked = _leak_probe(
            views,
            snapshots,
            spec.feature_spec,
            features,
            entity_role=entity_role,
            inclusive=spec.snapshot_spec.inclusive_snapshot_time,
        )
        checks.extend(
            run_onboarding_checks(
                _check_params(
                    config,
                    tables,
                    entity_role=entity_role,
                    label_name=target or "",
                    snapshot_mode=spec.snapshot_spec.mode,
                    feature_names=spec.feature_spec.names,
                    null_fractions=fractions,
                    future_event_rows=leaked,
                )
            )
        )
        if sample_entities is None:
            checks.extend(
                _phase_one_checks(
                    frame, config, dataset_id=dataset_id, target=target, mode=mode, periodic=periodic
                )
            )
        stats = tuple(
            FeatureStat(
                name=name,
                null_fraction=fractions[name],
                dropped=name in dropped,
                reason=f"empty for more than {limit:.0%} of rows" if name in dropped else None,
            )
            for name in built
        )
        report = _report(
            dataset_id=dataset_id,
            checks=checks,
            tables=tables,
            snapshots=per_snapshot,
            features=stats,
            frame=frame,
            duration_s=perf_counter() - started,
            features_sql_path=features_sql_path,
        )
        _cancelled(cancel)
        status.finish(
            "validate",
            detail=f"{report.error_count} error(s), {report.warning_count} warning(s).",
            seconds=perf_counter() - began,
        )
        if not report.passed:
            return _stop(status, registry, report)

        # --- write ----------------------------------------------------------
        began = perf_counter()
        status.start("write")
        _cancelled(cancel)
        types = _column_types(frame)
        fingerprint = registry.write_frame(
            dataset_id, frame, pii_columns=_pii_columns(tables, profiles), types=types
        )
        registry.write_manifest(
            dataset_id,
            build_manifest(
                dataset_id,
                spec,
                mappings={mapping.mapping_id: mapping for mapping in mappings},
                sources=profiles,
                frame=frame,
                columns=_dataset_columns(
                    frame,
                    types,
                    keys=[ENTITY_KEY, *([SNAPSHOT_COLUMN] if periodic else [])],
                    mapped=attributes,
                    derived=derived,
                    features=built,
                    target=target,
                ),
                entity_key=ENTITY_KEY,
                snapshot_column=SNAPSHOT_COLUMN,
                target=target,
                fingerprint=fingerprint,
            ),
        )
        registry.write_report(dataset_id, report)
        status.finish(
            "write",
            detail=f"{len(frame):,} rows written.",
            seconds=perf_counter() - began,
        )
        status.stop(RunState.DONE, error=None, detail=f"{len(frame):,} rows built.")
        return report
    except BaseException as exc:  # the status document must never outlive the build as "running"
        _fail(status, exc)
        raise
    finally:
        con.close()


def _stop(status: _StatusWriter, registry: DatasetRegistry, report: BuildReport) -> BuildReport:
    """Record a build that finished cleanly but produced nothing, and write only the report.

    Failed, not done: the recipe ran and the data did not survive it, so a Build screen that said
    "done" over an empty dataset directory would be the one screen a user reads before looking for a
    dataset that is not there.
    """
    registry.write_report(report.dataset_id, report)
    detail = f"{report.error_count} problem(s) stopped this build; no dataset was written."
    status.stop(RunState.FAILED, error=detail, detail=detail)
    return report


def _fail(status: _StatusWriter, exc: BaseException) -> None:
    """Leave `build_status.json` saying what happened, in a sentence this engine wrote.

    Only a message from one of our own coded exceptions is carried through. A `str()` of anything
    else can quote a cell value out of a pandas exception and onto a screen (plan section 13.7), and
    "something went wrong here" is worth more than a value nobody meant to publish.
    """
    from engine.jobs import JobCancelledError

    if isinstance(exc, JobCancelledError):
        status.stop(RunState.CANCELLED, error=None, detail="The build was cancelled.")
        return
    coded = exc.message if isinstance(exc, (DatasetError, LabelError, TransformError)) else None
    status.stop(
        RunState.FAILED,
        error=coded or "This build could not be completed.",
        detail="The build stopped before it finished.",
    )
