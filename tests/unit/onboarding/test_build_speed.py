"""M37, the build made faster without the dataset it builds changing.

Every change M37 made to the build is a shortcut to an answer the build already computed the long
way, so every test here states the same thing in a different place: the shortcut and the long way
agree. They are the regression tests for the speed work (docs/PERFORMANCE.md, DEC-096, DEC-097):
if one of them fails, a later edit has made the fast path answer a different question, which is
worse than it being slow.

1. One read per source: `read_upload(keep_all_rows=True)` hands back the capped frame and the
   fingerprint a plain profiling read would, plus every row an uncapped read would; and
   `FileSourceReader.read_profiled` is `read` and `profile` in one pass.
2. The text casts: `transforms._as_text` is `str()` of every non-null cell, as `_map_non_null` was;
   `join_coverage` answers the same with and without the copy it now skips.
3. The write stage: the fingerprint is computed once, from types inferred once, and is the same.
4. The leak probe: rebuilt for fewer snapshot rows, it still catches a missing time bound, and the
   entities it adds that were given no future rows are what catch a query reading across entities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pytest

from engine.config import StandardType, UseCaseConfig, load_use_case
from engine.onboarding import build as build_module
from engine.onboarding import datasets as datasets_module
from engine.onboarding import features as feature_engine
from engine.onboarding.datasets import LocalDatasetRegistry, build_manifest, dataset_fingerprint_of
from engine.onboarding.features import SNAPSHOT_VIEW, build_features
from engine.onboarding.sources import FileSourceReader, join_coverage
from engine.onboarding.specs import AggFunction, FeatureDef, FeatureSpec, SourceSpec
from engine.onboarding.transforms import _as_text, _map_non_null, cast_series
from engine.stages.ingest import dataset_fingerprint, infer_column_type, read_upload
from engine.storage import LocalStorage
from tests.fixtures.make_data import predictive_use_case_ids
from tests.unit.onboarding.test_datasets import (
    _frame,
    _mapping,
    _single_columns,
    _source_profile,
    _spec,
)


# ---------------------------------------------------------------------------
# 1. One read per source
# ---------------------------------------------------------------------------
def _events(n: int) -> pd.DataFrame:
    """An event table with a text key, an integer, a float with gaps and a date string."""
    return pd.DataFrame(
        {
            "cust_id": [f"C{i % 7:03d}" for i in range(n)],
            "units": list(range(n)),
            "amount": [None if i % 5 == 0 else i * 1.25 for i in range(n)],
            "event_date": [f"2024-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)],
        }
    )


def _store(tmp_path: Path, frame: pd.DataFrame, *, parquet: bool) -> tuple[LocalStorage, str]:
    storage = LocalStorage(tmp_path / "store")
    if parquet:
        key = "sources/events.parquet"
        path = storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    else:
        key = "sources/events.csv"
        storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    return storage, key


@pytest.mark.parametrize("parquet", [False, True], ids=["csv", "parquet"])
@pytest.mark.parametrize("rows", [5, 20], ids=["under-the-cap", "over-the-cap"])
def test_keeping_every_row_changes_neither_the_profiled_rows_nor_the_fingerprint(
    tmp_path: Path, parquet: bool, rows: int
) -> None:
    """The build's one read is the profiling read plus the rows above its cap - nothing else moves.

    The cap and the chunk size are tiny so that the over-the-cap case really cuts a chunk in two,
    which is where a capped frame and a whole frame part company.
    """
    storage, key = _store(tmp_path, _events(rows), parquet=parquet)
    plain = read_upload(storage, key, row_cap=7, chunk_rows=3)
    kept = read_upload(storage, key, row_cap=7, chunk_rows=3, keep_all_rows=True)
    uncapped = read_upload(storage, key, row_cap=10_000, chunk_rows=3)

    pd.testing.assert_frame_equal(kept.frame, plain.frame)
    assert kept.fingerprint == plain.fingerprint
    assert kept.row_count == plain.row_count == rows
    assert kept.truncated is plain.truncated is (rows > 7)
    assert kept.all_rows is not None
    pd.testing.assert_frame_equal(kept.all_rows, uncapped.frame)
    assert plain.all_rows is None


def test_a_bounded_preview_never_keeps_every_row(tmp_path: Path) -> None:
    storage, key = _store(tmp_path, _events(20), parquet=False)
    preview = read_upload(storage, key, max_rows=4, keep_all_rows=True)
    assert preview.all_rows is None
    assert len(preview.frame) == 4


@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    return load_use_case(predictive_use_case_ids()[0])


def _source(frame: pd.DataFrame, key: str) -> SourceSpec:
    return SourceSpec(
        source_id="src_events",
        client_id="cli_test",
        file_name="events.csv",
        storage_key=key,
        file_format="csv",
        role=None,
        rows=len(frame),
        columns=tuple(str(name) for name in frame.columns),
        fingerprint=dataset_fingerprint(frame),
        created_at=datetime.now(UTC),
    )


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_timestamps(v) for k, v in value.items() if k != "profiled_at"}
    if isinstance(value, list):
        return [_without_timestamps(v) for v in value]
    return value


def test_read_profiled_is_read_and_profile_in_one_pass(tmp_path: Path, config: UseCaseConfig) -> None:
    frame = _events(60)
    storage, key = _store(tmp_path, frame, parquet=False)
    source = _source(frame, key)
    reader = FileSourceReader(storage, config)

    profiled = reader.read_profiled(source)

    pd.testing.assert_frame_equal(profiled.frame, reader.read(source))
    assert _without_timestamps(profiled.profile.model_dump(mode="json")) == _without_timestamps(
        reader.profile(source).model_dump(mode="json")
    )


# ---------------------------------------------------------------------------
# 2. The text casts
# ---------------------------------------------------------------------------
_TEXT_CASES: dict[str, pd.Series[Any]] = {
    "int64": pd.Series([3, 0, -12, 2**62], dtype="int64"),
    "uint8": pd.Series([0, 7, 255], dtype="uint8"),
    "strings": pd.Series(["C001", "", " padded ", "007"], dtype=object),
    "strings-with-none": pd.Series(["a", None, "b"], dtype=object),
    "strings-with-nan": pd.Series(["a", np.nan, "b"], dtype=object),
    "mixed-objects": pd.Series(["a", 1, 2.5, True, None], dtype=object),
    "float-with-nan": pd.Series([1.0, np.nan, 2.5, 1e20], dtype="float64"),
    "datetimes": pd.Series(pd.to_datetime(["2024-01-31", None, "2024-02-29 13:45"], format="ISO8601")),
    "bools": pd.Series([True, False, True]),
    "nullable-int": pd.Series([1, None, 3], dtype="Int64"),
    "string-dtype": pd.Series(["x", None, "y"], dtype="string"),
    "empty-object": pd.Series([], dtype=object),
}


@pytest.mark.parametrize("name", sorted(_TEXT_CASES))
def test_the_text_cast_shortcut_is_str_of_every_non_null_cell(name: str) -> None:
    series = _TEXT_CASES[name]
    expected = _map_non_null(series, str)
    actual = _as_text(series)
    pd.testing.assert_series_equal(actual, expected, check_exact=True)
    assert [type(value) for value in actual] == [type(value) for value in expected]
    assert [repr(value) for value in actual] == [repr(value) for value in expected]


def test_the_text_cast_shortcut_does_not_hand_back_the_source_column() -> None:
    """The mapped frame must never share a column with the raw one a caller may still hold."""
    series = pd.Series(["a", "b"], dtype=object)
    cast = cast_series(series, StandardType.CATEGORICAL)
    assert cast.values is not series
    cast.values.iloc[0] = "changed"
    assert series.iloc[0] == "a"


def _reference_coverage(event_keys: pd.Series[Any], entity_keys: pd.Series[Any]) -> float:
    """`join_coverage` as it was written before M37, copied here as the oracle."""
    non_null = event_keys.dropna()
    if len(non_null) == 0:
        return 1.0
    universe = set(entity_keys.dropna().astype(str))
    matched = non_null.astype(str).isin(universe).sum()
    return round(float(matched) / len(non_null), 4)


@pytest.mark.parametrize(
    ("events", "entities"),
    [
        (pd.Series(["C1", "C2", "C9", None], dtype=object), pd.Series(["C1", "C2", "C3"], dtype=object)),
        (pd.Series([1, 2, 9]), pd.Series(["1", "2", "3"], dtype=object)),
        (pd.Series(["1", "2", "9"], dtype=object), pd.Series([1, 2, 3])),
        (pd.Series([1.0, np.nan, 2.0]), pd.Series([1, 2])),
        (pd.Series(["C1", 2, None], dtype=object), pd.Series(["C1", "2"], dtype=object)),
        (pd.Series([], dtype=object), pd.Series(["C1"], dtype=object)),
        (pd.Series([1, 2, 9, 2**62 + 1]), pd.Series([1, 2, 3, 2**62])),
        (pd.Series([1, 2, 9], dtype="int32"), pd.Series([1, 2, 3], dtype="int64")),
        (pd.Series([1, 2, 255], dtype="uint8"), pd.Series([1, 2, 3], dtype="uint8")),
    ],
)
def test_join_coverage_answers_what_it_always_answered(
    events: pd.Series[Any], entities: pd.Series[Any]
) -> None:
    assert join_coverage(events, entities) == _reference_coverage(events, entities)
    distinct = entities.dropna().drop_duplicates()
    assert join_coverage(events, distinct) == _reference_coverage(events, entities)


# ---------------------------------------------------------------------------
# 3. The write stage
# ---------------------------------------------------------------------------
def test_write_frame_with_the_types_already_inferred_returns_the_same_fingerprint(tmp_path: Path) -> None:
    registry = LocalDatasetRegistry(LocalStorage(tmp_path))
    frame = _frame(6)
    types = {str(name): infer_column_type(frame[name]) for name in frame.columns}
    assert registry.write_frame("ds_a", frame, types=types) == registry.write_frame("ds_b", frame)
    assert registry.write_frame("ds_a", frame, types=types) == dataset_fingerprint_of(frame)


def test_build_manifest_records_the_fingerprint_it_is_handed_without_recomputing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame(5)
    handed = dataset_fingerprint_of(frame)

    def refuse(_: pd.DataFrame) -> None:
        raise AssertionError("build_manifest fingerprinted a frame it had been handed the fingerprint of")

    monkeypatch.setattr(datasets_module, "dataset_fingerprint_of", refuse)
    manifest = build_manifest(
        "ds_1",
        _spec("sp_1"),
        mappings={"map_1": _mapping("map_1", "src_1")},
        sources={"src_1": _source_profile("src_1")},
        frame=frame,
        columns=_single_columns(),
        entity_key="entity_key",
        snapshot_column="snapshot_date",
        target=None,
        fingerprint=handed,
    )
    assert manifest.fingerprint == handed


def test_the_write_stage_infers_each_column_type_once_and_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame(5)
    calls: list[str] = []
    real = infer_column_type

    def counting(series: pd.Series[Any]) -> Any:
        calls.append(str(series.name))
        return real(series)

    monkeypatch.setattr("engine.stages.ingest.infer_column_type", counting)
    types = build_module._column_types(frame)
    assert sorted(calls) == sorted(str(name) for name in frame.columns)
    assert types == {str(name): real(frame[name]) for name in frame.columns}


# ---------------------------------------------------------------------------
# 4. The leak probe
# ---------------------------------------------------------------------------
_SNAPSHOT_DATES = ("2024-03-31", "2024-06-30")
_COUNT = FeatureSpec(
    features=(FeatureDef(name="usage_count", role="usage", function=AggFunction.COUNT),),
)


def _probe_fixture() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Four customers; every one of the probe's future rows will belong to `a`.

    `a` owns the first 600 usage rows, so `frame.head(_PROBE_ROWS)` - the rows the probe re-dates -
    are all `a`'s, and `b`, `c` and `d` are given nothing. That is what lets a test tell apart a
    leak the injected entity can show from one only the others can.
    """
    entities = ["a", "b", "c", "d"]
    own = [("a", f"2024-01-{(i % 28) + 1:02d}") for i in range(600)]
    others = [(key, f"2024-0{month}-15") for key in ("b", "c", "d") for month in (2, 4, 5)]
    usage = pd.DataFrame(own + others, columns=["entity_key", "event_time"])
    usage["event_time"] = pd.to_datetime(usage["event_time"])
    snapshots = pd.DataFrame(
        [(key, pd.Timestamp(day)) for day in _SNAPSHOT_DATES for key in entities],
        columns=["entity_key", "snapshot_date"],
    )
    return {"entity": pd.DataFrame({"entity_key": entities}), "usage": usage}, snapshots


def _built_features(views: dict[str, pd.DataFrame], snapshots: pd.DataFrame) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        for role, frame in views.items():
            con.register(role, frame)
        con.register(SNAPSHOT_VIEW, snapshots)
        return build_features(con, _COUNT, inclusive=True)
    finally:
        con.close()


def _probe(views: dict[str, pd.DataFrame], snapshots: pd.DataFrame) -> dict[str, int]:
    return build_module._leak_probe(
        views,
        snapshots,
        _COUNT,
        _built_features(views, snapshots),
        entity_role="entity",
        inclusive=True,
    )


def _break_the_join(monkeypatch: pytest.MonkeyPatch, clause: str) -> None:
    monkeypatch.setattr(
        feature_engine,
        "point_in_time_join_clause",
        lambda event, snapshot, *, inclusive: clause.format(e=event, s=snapshot),
    )
    monkeypatch.setattr(feature_engine, "assert_point_in_time", lambda sql: None)


def test_a_sound_build_is_not_reported_as_leaking() -> None:
    views, snapshots = _probe_fixture()
    assert _probe(views, snapshots) == {}


@pytest.mark.parametrize("control", [build_module._PROBE_CONTROL_ENTITIES, 0])
def test_a_missing_time_bound_is_caught_by_the_entities_given_future_rows(
    monkeypatch: pytest.MonkeyPatch, control: int
) -> None:
    views, snapshots = _probe_fixture()
    monkeypatch.setattr(build_module, "_PROBE_CONTROL_ENTITIES", control)
    _break_the_join(monkeypatch, "{e}.entity_key = {s}.entity_key")
    assert _probe(views, snapshots) == {"usage": build_module._PROBE_ROWS}


def test_a_query_reading_other_entities_future_is_caught_only_by_the_control_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason `_probe_rows` adds entities that were given nothing.

    This join bounds an entity's *own* events to the snapshot date and reads every other entity's
    events whenever they happened. `a`, the only entity given future rows, sees nothing new - its
    own future rows are bounded out and nobody else was given any - so a probe of `a` alone reports
    a clean build. `b`, `c` and `d` count `a`'s future rows, and the probe that includes them says so.
    """
    views, snapshots = _probe_fixture()
    _break_the_join(
        monkeypatch,
        "({e}.entity_key = {s}.entity_key AND {e}.event_time <= {s}.snapshot_date)"
        " OR {e}.entity_key <> {s}.entity_key",
    )
    assert _probe(views, snapshots) == {"usage": build_module._PROBE_ROWS}

    monkeypatch.setattr(build_module, "_PROBE_CONTROL_ENTITIES", 0)
    assert _probe(views, snapshots) == {}


def test_the_probe_rebuilds_the_injected_entities_and_a_bounded_control_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_module, "_PROBE_CONTROL_ENTITIES", 2)
    keys = pd.Series(["a", "b", "c", "d", "a", "b", "c", "d"])
    rows = build_module._probe_rows(keys, [pd.Series(["a", "a", None])])
    assert rows.tolist() == [True, True, True, False, True, True, True, False]


@pytest.mark.parametrize(
    "witnesses",
    [[], [pd.Series(["zzz"])], [pd.Series([None], dtype=object)]],
    ids=["no-event-table", "no-key-matches", "only-null-keys"],
)
def test_the_probe_falls_back_to_every_row_rather_than_probing_nothing(witnesses: list[Any]) -> None:
    keys = pd.Series(["a", "b", "c"])
    assert build_module._probe_rows(keys, witnesses).tolist() == [True, True, True]
