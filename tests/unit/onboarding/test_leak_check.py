"""M55, ruling R1 on DEC-096: the full future-data leak check beside the narrowed one.

R1 accepted the narrowed probe (DEC-096) as the default for builds on three conditions: the full
check - every snapshot row rebuilt against event tables carrying future-dated rows - stays available
as `full_leak_check: true`; it is forced on for the first build of any new recipe; and it runs in
the nightly suite and in the golden tests. This module holds the unit half of that:

1. What "the same recipe" means (`specs.recipe_hash`): ids, timestamps and who-decided-what are not
   part of it, the logic that decides what a feature reads is.
2. When a build is the first of its recipe (`build.is_first_build_of_recipe`), and which check a
   build then runs and why (`build._leak_check_choice`).
3. The full probe rebuilds every snapshot row; the narrowed one does not.
4. The reason R1 keeps the full check (DEC-872): a join-key bug that makes one entity read one other
   entity's events with no time bound. It is built through the real compiler, it passes the real
   SQL guard, and it moves the feature of an entity that was neither given a future event nor
   drawn into the control group. The full check reports it; the narrowed check does not.
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from engine.onboarding import build as build_module
from engine.onboarding import features as feature_engine
from engine.onboarding.datasets import build_manifest
from engine.onboarding.features import (
    POINT_IN_TIME_MARKER,
    SNAPSHOT_VIEW,
    assert_point_in_time,
    build_features,
    compile_role_query,
)
from engine.onboarding.specs import (
    AggFunction,
    ColumnTransform,
    DatasetManifest,
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    LabelDefinition,
    LabelType,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    StandardType,
    TransformKind,
    recipe_hash,
)
from engine.utils.time import utc_now
from tests.unit.onboarding.test_datasets import _frame, _single_columns, _source_profile

CLIENT = "acme"
USE_CASE = "telco-churn"


# ---------------------------------------------------------------------------
# A recipe, and the same recipe as a monthly replay would re-point it
# ---------------------------------------------------------------------------
def _column(source: str, standard: str, transform: ColumnTransform | None = None) -> MappingColumn:
    return MappingColumn(
        source=source, standard=standard, transform=transform, confidence=0.9, decided_by=DecidedBy.AUTO
    )


def _mappings(*, suffix: str = "1", client_id: str = CLIENT) -> tuple[MappingSpec, ...]:
    return (
        MappingSpec(
            mapping_id=f"map_entity_{suffix}",
            client_id=client_id,
            source_id=f"src_entity_{suffix}",
            use_case=USE_CASE,
            role="entity",
            columns=(_column("Cust ID", "entity_key"),),
            created_at=utc_now(),
        ).with_hash(),
        MappingSpec(
            mapping_id=f"map_usage_{suffix}",
            client_id=client_id,
            source_id=f"src_usage_{suffix}",
            use_case=USE_CASE,
            role="usage",
            columns=(
                _column("Cust ID", "entity_key"),
                _column(
                    "Used On",
                    "event_time",
                    ColumnTransform(
                        kind=TransformKind.CAST, to_type=StandardType.DATE, date_format="%Y-%m-%d"
                    ),
                ),
                _column("MB", "data_mb"),
            ),
            unmapped_source=("Tower",),
            created_at=utc_now(),
        ).with_hash(),
    )


_FEATURES = FeatureSpec(
    features=(FeatureDef(name="usage_30d", role="usage", function=AggFunction.COUNT, window_days=30),)
)


def _spec(mappings: tuple[MappingSpec, ...], *, spec_id: str = "spec_1", **changes: object) -> OnboardingSpec:
    entity, *events = mappings
    fields: dict[str, object] = {
        "spec_id": spec_id,
        "client_id": entity.client_id,
        "use_case": USE_CASE,
        "entity_source_id": entity.source_id,
        "event_source_ids": tuple(sorted(mapping.source_id for mapping in events)),
        "mapping_ids": tuple(sorted(mapping.mapping_id for mapping in mappings)),
        "feature_spec": _FEATURES,
        "label_spec": LabelDefinition(
            name="churned", type=LabelType.EVENT_ABSENCE, role="usage", horizon_days=60
        ),
        "snapshot_spec": SnapshotDefinition(mode=SnapshotMode.PERIODIC, max_snapshots=6),
        "created_at": utc_now(),
    }
    fields.update(changes)
    return OnboardingSpec.model_validate(fields).with_hash()


def _replayed(mappings: tuple[MappingSpec, ...]) -> tuple[MappingSpec, ...]:
    """What `engine.onboarding.replay._copied` does to a mapping next month: new ids, new source,
    new time, this month's unmapped columns - and the same column decisions."""
    return tuple(
        mapping.model_copy(
            update={
                "mapping_id": mapping.mapping_id.replace("_1", "_2"),
                "source_id": mapping.source_id.replace("_1", "_2"),
                "unmapped_source": ("Tower", "Region"),
                "created_at": utc_now(),
                "columns": tuple(
                    column.model_copy(update={"confidence": 1.0, "decided_by": DecidedBy.USER})
                    for column in mapping.columns
                ),
            }
        ).with_hash()
        for mapping in mappings
    )


# ---------------------------------------------------------------------------
# 1. "The same recipe"
# ---------------------------------------------------------------------------
def test_a_monthly_replay_of_a_recipe_is_the_same_recipe() -> None:
    first = _mappings()
    again = _replayed(first)
    spec, replayed = _spec(first), _spec(again, spec_id="spec_2")

    assert (
        spec.hash != replayed.hash
    ), "spec_hash hashes the ids a replay changes; that is why it cannot serve"
    assert recipe_hash(spec, first) == recipe_hash(replayed, again)
    assert recipe_hash(spec, first) == recipe_hash(spec, tuple(reversed(first))), "mapping order is not logic"


def _changed_feature() -> OnboardingSpec:
    return _spec(
        _mappings(),
        feature_spec=FeatureSpec(
            features=(FeatureDef(name="usage_30d", role="usage", function=AggFunction.COUNT, window_days=60),)
        ),
    )


def _changed_mapping() -> tuple[MappingSpec, ...]:
    entity, usage = _mappings()
    columns = tuple(
        _column("Account", "entity_key") if column.standard == "entity_key" else column
        for column in usage.columns
    )
    return (entity, usage.model_copy(update={"columns": columns}).with_hash())


@pytest.mark.parametrize(
    ("case", "other"),
    [
        ("a feature's window", lambda: (_changed_feature(), _mappings())),
        ("the snapshot rule", lambda: (_spec(_mappings(), snapshot_spec=SnapshotDefinition()), _mappings())),
        ("the label", lambda: (_spec(_mappings(), label_spec=None), _mappings())),
        (
            "the column a standard column is read from",
            lambda: (_spec(_changed_mapping()), _changed_mapping()),
        ),
    ],
)
def test_a_change_to_what_a_feature_reads_is_a_new_recipe(case: str, other: object) -> None:
    base = _mappings()
    spec, mappings = other()  # type: ignore[operator]
    assert recipe_hash(spec, mappings) != recipe_hash(_spec(base), base), case


# ---------------------------------------------------------------------------
# 2. The first build of a recipe, and the check each build runs
# ---------------------------------------------------------------------------
def _manifest(spec: OnboardingSpec, *, recipe: str | None, client_id: str = CLIENT) -> DatasetManifest:
    """A registered manifest of some earlier build, carrying `recipe` (or none, as before M55).

    `is_first_build_of_recipe` reads only its client and its `recipe_hash`, so the rest is the
    smallest dataset `build_manifest` will accept."""
    one_table = {
        "mapping_ids": ("map_1",),
        "entity_source_id": "src_1",
        "event_source_ids": (),
        "snapshot_spec": SnapshotDefinition(mode=SnapshotMode.SINGLE),
    }
    manifest = build_manifest(
        "ds_earlier",
        spec.model_copy(update=one_table),
        mappings={"map_1": _mappings()[0].model_copy(update={"mapping_id": "map_1"})},
        sources={"src_1": _source_profile("src_1")},
        frame=_frame(3),
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
        recipe_hash=recipe,
    )
    return manifest.model_copy(update={"client_id": client_id})


def test_a_recipe_is_new_until_a_build_of_it_is_registered_for_the_client() -> None:
    mappings = _mappings()
    spec = _spec(mappings)
    recipe = recipe_hash(spec, mappings)

    assert build_module.is_first_build_of_recipe(spec, mappings, ())
    assert build_module.is_first_build_of_recipe(spec, mappings, (_manifest(spec, recipe="sha256:v1:other"),))
    assert build_module.is_first_build_of_recipe(
        spec, mappings, (_manifest(spec, recipe=recipe, client_id="someone_else"),)
    ), "another client's build of the same recipe says nothing about this client's data"
    assert build_module.is_first_build_of_recipe(
        spec, mappings, (_manifest(spec, recipe=None),)
    ), "a dataset built before M55 recorded no recipe, so it cannot vouch for one"
    assert not build_module.is_first_build_of_recipe(spec, mappings, (_manifest(spec, recipe=recipe),))

    replayed = _replayed(mappings)
    assert not build_module.is_first_build_of_recipe(
        _spec(replayed, spec_id="spec_2"), replayed, (_manifest(spec, recipe=recipe),)
    ), "next month's replay of a recipe already built is not a new recipe"


@pytest.mark.parametrize(
    ("full_leak_check", "first_build", "expected"),
    [
        (False, False, ("narrow", "default")),
        (True, False, ("full", "option")),
        (False, True, ("full", "first_build_of_recipe")),
        (True, True, ("full", "first_build_of_recipe")),
    ],
)
def test_the_first_build_of_a_recipe_runs_the_full_check_whatever_was_asked(
    full_leak_check: bool, first_build: bool, expected: tuple[str, str]
) -> None:
    assert (
        build_module._leak_check_choice(full_leak_check=full_leak_check, first_build_of_recipe=first_build)
        == expected
    )


# ---------------------------------------------------------------------------
# 3 and 4. The probe, full and narrowed, against a join-key bug
# ---------------------------------------------------------------------------
_ENTITIES = 600
"""More entities than the injected one plus `_PROBE_CONTROL_ENTITIES` (500), so the narrowed probe
really leaves some out - as it does in every build larger than a test fixture."""

_INJECTED = _ENTITIES
"""The entity that owns the head of the usage table, so every future row the probe adds is its."""

_VICTIM = _INJECTED - 1
"""The entity the join-key bug below points at `_INJECTED`: it is not given a future row and, being
the last entity but one in spine order, it is not among the first 500 entities given none."""

_SNAPSHOTS = (date(2024, 3, 31), date(2024, 6, 30))

_COUNT = FeatureSpec(features=(FeatureDef(name="usage_count", role="usage", function=AggFunction.COUNT),))


def _fixture() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Integer customer ids 1..600; the first 500 usage rows are all customer 600's."""
    keys = list(range(1, _ENTITIES + 1))
    own = [(_INJECTED, pd.Timestamp("2024-01-01") + pd.Timedelta(days=i % 60)) for i in range(520)]
    others = [(key, pd.Timestamp(f"2024-0{month}-15")) for key in keys[:-1] for month in (2, 5)]
    usage = pd.DataFrame(own + others, columns=["entity_key", "event_time"])
    snapshots = pd.DataFrame(
        [(key, pd.Timestamp(day)) for day in _SNAPSHOTS for key in keys],
        columns=["entity_key", "snapshot_date"],
    )
    return {"entity": pd.DataFrame({"entity_key": keys}), "usage": usage}, snapshots


def _features(views: dict[str, pd.DataFrame], snapshots: pd.DataFrame) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        for role, frame in views.items():
            con.register(role, frame)
        con.register(SNAPSHOT_VIEW, snapshots)
        return build_features(con, _COUNT, inclusive=True)
    finally:
        con.close()


def _probe(*, full: bool) -> build_module._Probe:
    views, snapshots = _fixture()
    return build_module._run_leak_probe(
        views, snapshots, _COUNT, _features(views, snapshots), entity_role="entity", inclusive=True, full=full
    )


def _linked_account_join(event: str, snapshot: str, *, inclusive: bool) -> str:
    """The join-key bug: "an account's own events, or its linked account's" (here, the next id),
    written without parentheses.

    AND binds tighter than OR, so the time bound applies to the own-events branch only and the
    linked branch reads the other entity's whole history, future included. The guard line itself is
    untouched - it is still `AND e.event_time <= s.snapshot_date` followed by the marker - which is
    why `assert_point_in_time`, a check of the SQL text, accepts the query. This is the leak the SQL
    guard cannot see, and the probe exists for.
    """
    bound = "<=" if inclusive else "<"
    return (
        f"{event}.entity_key = {snapshot}.entity_key\n"
        f" AND {event}.event_time {bound} {snapshot}.snapshot_date  {POINT_IN_TIME_MARKER}\n"
        f" OR {event}.entity_key = {snapshot}.entity_key + 1"
    )


@pytest.fixture
def join_key_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the one function that writes the join clause (`point_in_time_join_clause`), as a future
    edit to the query builder would. `assert_point_in_time` is **not** replaced: every query below
    goes through the real compiler and the real guard."""
    monkeypatch.setattr(feature_engine, "point_in_time_join_clause", _linked_account_join)


def test_a_sound_build_passes_both_checks() -> None:
    assert _probe(full=True).leaked == {}
    assert _probe(full=False).leaked == {}


def test_the_full_check_rebuilds_every_snapshot_row_and_the_narrow_one_does_not() -> None:
    full, narrow = _probe(full=True), _probe(full=False)
    assert full.rows_total == narrow.rows_total == _ENTITIES * len(_SNAPSHOTS)
    assert full.rows_probed == full.rows_total
    # the injected entity plus 500 control entities, at both snapshot dates
    assert narrow.rows_probed == (1 + build_module._PROBE_CONTROL_ENTITIES) * len(_SNAPSHOTS)


def test_the_join_key_bug_passes_the_sql_guard(join_key_bug: None) -> None:
    sql = compile_role_query("usage", _COUNT.features, inclusive=True)
    assert " OR e.entity_key = s.entity_key + 1" in sql
    assert_point_in_time(sql)  # raises FEATURE_POINT_IN_TIME_GUARD_MISSING if it sees the bug; it does not


def test_the_join_key_bug_moves_only_an_entity_outside_the_narrowed_rows(join_key_bug: None) -> None:
    """The victim is neither given a future row nor drawn into the control group, and it is the only
    entity whose value moves - which is exactly the case DEC-096's consequences name."""
    views, snapshots = _fixture()
    witnesses = [views["usage"].head(build_module._PROBE_ROWS)["entity_key"]]
    assert set(witnesses[0]) == {_INJECTED}
    narrowed = build_module._probe_rows(snapshots["entity_key"], witnesses)
    assert _VICTIM not in set(snapshots.loc[narrowed, "entity_key"])

    before = _features(views, snapshots).set_index(["entity_key", "snapshot_date"])["usage_count"]
    future = views["usage"].head(build_module._PROBE_ROWS).assign(event_time=pd.Timestamp("2024-07-01"))
    moved_views = {**views, "usage": pd.concat([views["usage"], future], ignore_index=True)}
    after = _features(moved_views, snapshots).set_index(["entity_key", "snapshot_date"])["usage_count"]
    moved = sorted({key for key, _ in before.index[before.to_numpy() != after.to_numpy()]})
    assert moved == [_VICTIM]


def test_the_full_check_catches_the_join_key_bug_and_the_narrow_check_misses_it(join_key_bug: None) -> None:
    """DEC-872: why R1 keeps the full check. Same tables, same bug, same probe; only the rows differ."""
    full = _probe(full=True)
    assert full.leaked == {"usage": build_module._PROBE_ROWS}

    narrow = _probe(full=False)
    assert narrow.leaked == {}, "the narrowed check now sees this bug; DEC-872's premise has changed"


def test_the_leak_check_record_says_which_check_ran_and_why() -> None:
    full = build_module._leak_check_record(
        _probe(full=True),
        scope="full",
        reason="first_build_of_recipe",
        recipe="sha256:v1:r",
        entity="customer",
    )
    assert (full.scope, full.reason, full.rows_probed) == ("full", "first_build_of_recipe", full.rows_total)
    assert full.summary.startswith("Full future-data check")
    assert "first build of this recipe" in full.summary

    narrow = build_module._leak_check_record(
        _probe(full=False), scope="narrow", reason="default", recipe="sha256:v1:r", entity="customer"
    )
    assert narrow.summary.startswith("Narrow future-data check")
    assert f"{narrow.rows_probed:,} of {narrow.rows_total:,}" in narrow.summary
    assert narrow.rows_probed < narrow.rows_total


def test_the_full_rows_are_every_row() -> None:
    assert build_module._full_rows(3).tolist() == [True, True, True]
    assert np.asarray(build_module._full_rows(0)).size == 0
