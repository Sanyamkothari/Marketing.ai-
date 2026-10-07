"""A feature that reads inside a lapse label's grace period is FUTURE_EVENTS_LEAKED (Plan J M93).

A 30-day lapse label with 7 days' grace looks at the days (T+30, T+37] after each prediction date. A
feature that reads those days - "recharges in the label's grace period", written by a future edit to
the query builder - has seen the outcome. The SQL guard cannot see it (the guard line is intact; the
leak sits in an `OR` branch), and the probe's usual future rows, dated the day after the last
prediction date, cannot either: they lie outside (T+30, T+37]. The build therefore also dates a copy
of the probe rows inside the last prediction date's grace period whenever the recipe's label has
one, and that copy is what catches it.

Through the whole of `build_dataset`, with real files and mappings, like
`tests/integration/test_onboarding_leak_check.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.config import RunMode, StandardType, load_use_case
from engine.contracts import Severity
from engine.onboarding import features as feature_engine
from engine.onboarding.build import build_dataset
from engine.onboarding.datasets import LocalDatasetRegistry
from engine.onboarding.features import POINT_IN_TIME_MARKER
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    AggFunction,
    BuildReport,
    ColumnTransform,
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    LabelSpec,
    LabelType,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
    TransformKind,
)
from engine.stages.ingest import dataset_fingerprint
from engine.storage import LocalStorage

pytestmark = [pytest.mark.integration]

USE_CASE = "win-back-campaign"
CLIENT = "cl_grace"
CUSTOMERS = 600
DATA_START = date(2024, 1, 1)
HORIZON, GRACE = 30, 7


def _customers() -> pd.DataFrame:
    return pd.DataFrame({"Cust ID": list(range(1, CUSTOMERS + 1))})


def _activity() -> pd.DataFrame:
    rows = [
        (key, DATA_START + timedelta(days=(key * 7 + visit * 41) % 180))
        for key in range(1, CUSTOMERS + 1)
        for visit in range(key % 3 + 1)
    ]
    rows.sort(key=lambda row: (row[1], row[0]))
    return pd.DataFrame({"Cust ID": [k for k, _ in rows], "Event Date": [d.isoformat() for _, d in rows]})


def _column(source: str, standard: str, transform: ColumnTransform | None = None) -> MappingColumn:
    return MappingColumn(
        source=source, standard=standard, transform=transform, confidence=1.0, decided_by=DecidedBy.USER
    )


DATE = ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.DATE)
MAPPINGS: dict[str, tuple[str, tuple[MappingColumn, ...]]] = {
    "src_customers": ("entity", (_column("Cust ID", "entity_key"),)),
    "src_activity": (
        "activity",
        (_column("Cust ID", "entity_key"), _column("Event Date", "event_time", DATE)),
    ),
}
FEATURES = FeatureSpec(
    features=(FeatureDef(name="activity_count", role="activity", function=AggFunction.COUNT),)
)


def _label(grace_days: int | None) -> LabelSpec:
    return LabelSpec(
        name="lapsed",
        type=LabelType.EVENT_ABSENCE,
        role="activity",
        horizon_days=HORIZON,
        grace_days=grace_days,
    )


def _build(root: Path, label: LabelSpec) -> BuildReport:
    config = load_use_case(USE_CASE)
    storage = LocalStorage(root)
    sources: list[SourceSpec] = []
    for source_id, frame in (("src_customers", _customers()), ("src_activity", _activity())):
        key = f"sources/{source_id}/raw.csv"
        storage.write_text(key, frame.to_csv(index=False))
        sources.append(
            SourceSpec(
                source_id=source_id,
                client_id=CLIENT,
                file_name=f"{source_id}.csv",
                storage_key=key,
                file_format="csv",
                role=MAPPINGS[source_id][0],
                rows=len(frame),
                columns=tuple(str(name) for name in frame.columns),
                fingerprint=dataset_fingerprint(frame),
                created_at=datetime.now(UTC),
            )
        )
    mappings = tuple(
        MappingSpec(
            mapping_id=f"map_{source_id}",
            client_id=CLIENT,
            source_id=source_id,
            use_case=USE_CASE,
            role=role,
            columns=columns,
            created_at=datetime.now(UTC),
        ).with_hash()
        for source_id, (role, columns) in MAPPINGS.items()
    )
    spec = OnboardingSpec(
        spec_id="spec_grace",
        client_id=CLIENT,
        use_case=USE_CASE,
        entity_source_id="src_customers",
        event_source_ids=("src_activity",),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
        feature_spec=FEATURES,
        label_spec=label,
        snapshot_spec=SnapshotDefinition(mode=SnapshotMode.SINGLE),
        created_at=datetime.now(UTC),
    ).with_hash()
    registry = LocalDatasetRegistry(storage)
    return build_dataset(
        spec=spec,
        config=config,
        sources=tuple(sources),
        mappings=mappings,
        reader=FileSourceReader(storage, config),
        registry=registry,
        dataset_id=registry.new_dataset_id(CLIENT, USE_CASE),
        mode=RunMode.SCORE,
        full_leak_check=True,
    )


def _grace_period_join(event: str, snapshot: str, *, inclusive: bool) -> str:
    """ "Own events up to the prediction date, or own events in the label's grace period."

    The guard line is exactly what `point_in_time_join_clause` writes, so `assert_point_in_time`
    accepts the query; the `OR` branch reads only (T+30, T+37], the days a 30-day lapse label with
    7 days' grace adds to its window.
    """
    bound = "<=" if inclusive else "<"
    return (
        f"{event}.entity_key = {snapshot}.entity_key\n"
        f" AND {event}.event_time {bound} {snapshot}.snapshot_date  {POINT_IN_TIME_MARKER}\n"
        f" OR {event}.entity_key = {snapshot}.entity_key"
        f" AND {event}.event_time > {snapshot}.snapshot_date + INTERVAL {HORIZON} DAY"
        f" AND {event}.event_time <= {snapshot}.snapshot_date + INTERVAL {HORIZON + GRACE} DAY"
    )


@pytest.fixture
def grace_period_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_engine, "point_in_time_join_clause", _grace_period_join)


def _leaks(report: BuildReport) -> list[str]:
    return [check.code for check in report.checks if check.code == "FUTURE_EVENTS_LEAKED"]


def test_a_sound_recipe_with_a_grace_period_passes_the_leak_check(tmp_path: Path) -> None:
    report = _build(tmp_path, _label(GRACE))
    assert _leaks(report) == []
    assert report.leak_check is not None and report.leak_check.scope == "full"


def test_a_feature_inside_the_grace_window_is_future_events_leaked(
    tmp_path: Path, grace_period_bug: None
) -> None:
    report = _build(tmp_path, _label(GRACE))
    leaked = [check for check in report.checks if check.code == "FUTURE_EVENTS_LEAKED"]
    assert leaked, [check.code for check in report.checks]
    assert all(check.severity is Severity.ERROR and not check.acknowledged for check in leaked)
    assert not report.passed


def test_without_a_grace_period_the_probe_rows_lie_outside_the_buggy_window(
    tmp_path: Path, grace_period_bug: None
) -> None:
    """Why the probe dates a second copy inside the grace period: the day-after rows alone miss it."""
    report = _build(tmp_path, _label(None))
    assert _leaks(report) == []
