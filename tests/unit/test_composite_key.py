"""Plan A M34: a two-column key through every stage (DEC-083).

A periodic dataset has one row per entity per snapshot date, so its key is `(entity_key,
snapshot_date)`. These tests pin what each stage does with that pair, and - just as much - that a
one-column key is handled exactly as it was before the pair existed.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine import keys
from engine.config import RunMode, SplitType, load_use_case, resolve_config
from engine.jobs import CancelToken
from engine.pipeline import StageContext
from engine.registry import LocalModelRegistry
from engine.stages import actions, export, prepare, validate
from engine.storage import LocalStorage, run_key

USE_CASE = "targeted-advertisement"
KEY = ["entity_key", "snapshot_date"]
RUN_ID = "r_20260923_0000c0de"


def _periodic(entities: int = 60, snapshots: int = 3) -> pd.DataFrame:
    """`entities` customers at `snapshots` month-ends; ids with leading zeros, on purpose."""
    dates = pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"][:snapshots])
    rows = [
        {
            "entity_key": f"{i:05d}",
            "snapshot_date": day,
            "visits_last_7d": (i * 3 + j) % 11,
            "plan_tier": ("gold", "silver", "bronze")[i % 3],
            "marketing_opt_in": True,
            "converted_30d": int((i + j) % 4 == 0),
        }
        for i in range(entities)
        for j, day in enumerate(dates)
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# engine.keys
# ---------------------------------------------------------------------------
def test_a_one_column_key_is_the_column_itself_everywhere() -> None:
    assert keys.normalise_key("customer_id") == "customer_id"
    assert keys.normalise_key(["customer_id"]) == "customer_id"
    assert keys.row_key_column("customer_id") == "customer_id"
    assert not keys.is_composite(["customer_id"])
    frame = pd.DataFrame({"customer_id": ["a", "b"]})
    assert keys.with_row_key(frame, "customer_id") is frame


def test_a_composite_key_joins_its_parts_as_text() -> None:
    frame = pd.DataFrame(
        {"entity_key": ["007", "007"], "snapshot_date": pd.to_datetime(["2026-01-31", "2026-02-28"])}
    )
    assert keys.normalise_key(KEY) == KEY
    assert keys.key_label(KEY) == "entity_key + snapshot_date"
    assert keys.row_key_column(KEY) == keys.ROW_KEY_COLUMN
    assert keys.key_series(frame, KEY).tolist() == ["007|2026-01-31", "007|2026-02-28"]


def test_a_whole_float_id_is_never_written_as_one_point_zero() -> None:
    assert keys.key_text(pd.Series([1.0, None, 3.0])).tolist() == ["1", "", "3"]
    assert keys.key_text(pd.Series([1.5, 2.0])).tolist() == ["1.5", "2.0"]


def test_a_malformed_key_is_refused() -> None:
    with pytest.raises(ValueError, match="more than once"):
        keys.normalise_key(["a", "a"])
    with pytest.raises(ValueError, match="at least one"):
        keys.normalise_key([])


def test_a_composite_key_splits_by_entity_and_says_the_engine_chose_it() -> None:
    resolved = resolve_config(USE_CASE)
    assert resolved.config.split.type is SplitType.RANDOM_STRATIFIED
    adjusted = keys.split_config_for_key(resolved, KEY)
    assert adjusted.config.split.group_column == "entity_key"
    assert adjusted.sources["split.group_column"] == "derived"
    assert keys.split_config_for_key(adjusted, KEY) is adjusted  # idempotent
    assert keys.split_config_for_key(resolved, "customer_id") is resolved


def test_a_time_based_use_case_splits_a_periodic_dataset_on_the_snapshot_date() -> None:
    resolved = resolve_config("fault-prediction")
    assert resolved.config.split.type is SplitType.TIME_BASED
    adjusted = keys.split_config_for_key(resolved, ["device_id", "as_of"])
    assert adjusted.config.split.type is SplitType.TIME_BASED
    assert adjusted.config.split.time_column == "as_of"
    assert adjusted.sources["split.time_column"] == "derived"
    assert adjusted.config.split.group_column is None


# ---------------------------------------------------------------------------
# StageContext
# ---------------------------------------------------------------------------
def _ctx(tmp_path: Path, primary_key: object) -> StageContext:
    resolved = resolve_config(USE_CASE)
    storage = LocalStorage(tmp_path)
    return StageContext(
        run_id=RUN_ID,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=LocalModelRegistry(tmp_path / "registry.db"),
        cancel=CancelToken(),
        primary_key=primary_key,  # type: ignore[arg-type]
        target="converted_30d",
        upload_key="uploads/u/source.csv",
        model_version_id=None,
    )


def test_a_string_key_is_normalised_to_a_one_item_list_and_nothing_else_changes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "customer_id")
    assert ctx.primary_key == ["customer_id"]
    assert ctx.key == "customer_id"
    assert ctx.row_key == "customer_id"
    assert ctx.entity_key is None
    assert ctx.carried_columns == ("customer_id",)
    assert ctx.config.split == resolve_config(USE_CASE).config.split


def test_a_composite_context_carries_the_row_key_and_splits_by_entity(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, KEY)
    assert ctx.key == KEY
    assert ctx.row_key == keys.ROW_KEY_COLUMN
    assert ctx.entity_key == "entity_key"
    assert ctx.carried_columns == (*KEY, keys.ROW_KEY_COLUMN)
    assert ctx.config.split.group_column == "entity_key"
    assert ctx.resolved.config.split.group_column == "entity_key"


# ---------------------------------------------------------------------------
# validate: the tuple is checked, not its first column
# ---------------------------------------------------------------------------
def _checks(frame: pd.DataFrame) -> dict[str, list[validate.ValidationCheck]]:
    report = validate.validate_for_training(
        frame, load_use_case(USE_CASE), primary_key=KEY, target="converted_30d", upload_id="u_1"
    )
    found: dict[str, list[validate.ValidationCheck]] = {}
    for check in report.checks:
        found.setdefault(check.code, []).append(check)
    return found


def test_a_customer_at_several_dates_is_unique_on_the_pair() -> None:
    checks = _checks(_periodic())
    assert "PK_NOT_UNIQUE" not in checks
    assert "PK_NULLS" not in checks
    assert "PK_MISSING" not in checks


def test_the_same_customer_twice_at_one_date_is_not_unique() -> None:
    frame = _periodic()
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    (finding,) = _checks(frame)["PK_NOT_UNIQUE"]
    assert finding.details["duplicate_rows"] == 1
    assert finding.details["columns"] == KEY


def test_a_null_in_any_part_of_the_key_is_an_error() -> None:
    frame = _periodic()
    frame.loc[3, "snapshot_date"] = pd.NaT
    (finding,) = _checks(frame)["PK_NULLS"]
    assert finding.details["null_count_by_column"] == {"entity_key": 0, "snapshot_date": 1}


def test_a_missing_key_column_is_named() -> None:
    (finding,) = _checks(_periodic().drop(columns=["snapshot_date"]))["PK_MISSING"]
    assert finding.details["missing"] == ["snapshot_date"]


def test_neither_key_column_is_reported_as_a_feature_problem() -> None:
    checks = _checks(_periodic())
    flagged = {check.column for found in checks.values() for check in found if check.column}
    assert not flagged & set(KEY)


def test_a_one_column_key_validates_exactly_as_before() -> None:
    frame = _periodic(snapshots=1).drop(columns=["snapshot_date"])
    report = validate.validate_for_training(
        frame, load_use_case(USE_CASE), primary_key="entity_key", target="converted_30d", upload_id="u_1"
    )
    params = validate.params_from_config(load_use_case(USE_CASE), primary_key="entity_key")
    assert params.primary_key == "entity_key"
    assert params.key_columns == ()
    assert not [check for check in report.checks if check.code.startswith("PK_")]


# ---------------------------------------------------------------------------
# prepare and split
# ---------------------------------------------------------------------------
def test_no_entity_appears_in_two_splits(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, KEY)
    frame = keys.with_row_key(_periodic(entities=120), KEY)
    rows, plan = prepare.prepare_rows(
        frame, ctx.config, primary_key=ctx.carried_columns, target="converted_30d"
    )
    assert not set(ctx.carried_columns) & set(plan.feature_columns)
    assert set(ctx.carried_columns) <= set(rows.columns)
    parts, report = prepare.split_dataset(rows, ctx.config, run_id=RUN_ID, target="converted_30d")
    owners: dict[str, str] = {}
    for name, part in parts.items():
        for entity in set(part["entity_key"]):
            assert owners.setdefault(entity, name) == name, f"{entity} is in {owners[entity]} and {name}"
    assert report.group_column == "entity_key"


# ---------------------------------------------------------------------------
# actions: per entity, not per row
# ---------------------------------------------------------------------------
def _scored(frame: pd.DataFrame) -> pd.DataFrame:
    scored = keys.with_row_key(frame, KEY)
    scored["propensity"] = [((i * 37) % 100) / 100 for i in range(len(scored.index))]
    return scored


def test_control_assignment_is_stable_per_entity_across_snapshots() -> None:
    config = load_use_case(USE_CASE)
    banded = actions.apply_actions(
        _scored(_periodic(entities=200)),
        config,
        run_id=RUN_ID,
        primary_key=keys.ROW_KEY_COLUMN,
        entity_key="entity_key",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    per_entity = banded.groupby("entity_key")[actions.CONTROL_GROUP_COLUMN].nunique()
    assert (per_entity == 1).all()
    controlled = banded.loc[banded[actions.CONTROL_GROUP_COLUMN], "entity_key"].nunique()
    assert controlled == 20  # 10% of 200 customers, not of 600 rows


def test_a_customer_suppressed_at_one_snapshot_is_suppressed_at_all_of_them() -> None:
    config = load_use_case(USE_CASE)
    frame = _periodic(entities=30)
    frame.loc[
        (frame["entity_key"] == "00004") & (frame["snapshot_date"] == "2026-02-28"), "marketing_opt_in"
    ] = False
    banded = actions.apply_actions(
        _scored(frame),
        config,
        run_id=RUN_ID,
        primary_key=keys.ROW_KEY_COLUMN,
        entity_key="entity_key",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    customer = banded[banded["entity_key"] == "00004"]
    assert customer[actions.SUPPRESSED_REASON_COLUMN].tolist() == ["opted_out"] * 3
    assert not customer[actions.CONTROL_GROUP_COLUMN].any()
    others = banded[banded["entity_key"] != "00004"]
    assert others[actions.SUPPRESSED_REASON_COLUMN].isna().all()


# ---------------------------------------------------------------------------
# export: the client's own ids, as separate columns
# ---------------------------------------------------------------------------
def test_scores_csv_carries_each_key_column_with_its_leading_zeros(tmp_path: Path) -> None:
    config = load_use_case(USE_CASE)
    uploaded = keys.with_row_key(_periodic(entities=12), KEY)
    banded = actions.apply_actions(
        _scored(_periodic(entities=12)),
        config,
        run_id=RUN_ID,
        primary_key=keys.ROW_KEY_COLUMN,
        entity_key="entity_key",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    # A replayed transform may have changed the frame's own key columns; the export reads the
    # uploaded rows instead, so simulate that damage.
    banded["entity_key"] = banded["entity_key"].astype(int)
    storage = LocalStorage(tmp_path)
    export.write_scores(banded, config, run_id=RUN_ID, primary_key=KEY, storage=storage, key_source=uploaded)
    text = storage.read_text(run_key(RUN_ID, export.SCORES_CSV))
    header = text.splitlines()[0].split(",")
    assert header[:3] == ["entity_key", "snapshot_date", "propensity"]
    assert keys.ROW_KEY_COLUMN not in header
    back = pd.read_csv(io.StringIO(text), dtype=str)
    assert back["entity_key"].tolist() == uploaded["entity_key"].tolist()
    assert set(back["snapshot_date"]) == {"2026-01-31", "2026-02-28", "2026-03-31"}
    assert back["entity_key"].iloc[0] == "00000"
