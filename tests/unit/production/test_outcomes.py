"""Outcome ingestion: the window, the metric, the drop alert and the incrementality input (DEC-771/772).

The scoring run is a real one - built from the client's tables by a scheduled firing, with the
champion pinned - and its `scores.parquet` is written by the test in the export stage's shape, with
scores and outcomes chosen so the expected metric is known exactly.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import roc_auc_score

from engine.config import ResolvedConfig
from engine.contracts import RunState
from engine.runs import update_run
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.outcomes import (
    INCREMENTALITY_INPUT_FILENAME,
    OUTCOME_ERROR_STATUS,
    IncrementalityInput,
    OutcomeError,
    OutcomeReport,
    ingest_outcomes,
    read_outcome_report,
)
from engine.scheduling.schedules import ScheduleKind
from engine.storage import run_key
from tests.unit.production.scheduling_support import World, make_world, register_champion
from tests.unit.production.test_firing import training_frame

MATURE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def scored(tmp_path: Path, config_root: Path) -> tuple[World, str, pd.DataFrame]:
    """A finished scoring run of the champion, with a known `scores.parquet`."""
    world = make_world(tmp_path, config_root)
    register_champion(world, training_frame(world))
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None and firing.run_id is not None
    run_id = firing.run_id
    config = world.storage.read_model(run_key(run_id, "run_config.json"), ResolvedConfig).config
    rng = np.random.default_rng(3)
    keys = [f"C{i:06d}" for i in range(1000)]
    score = np.round(rng.uniform(0.0, 1.0, size=len(keys)), 6)
    frame = pd.DataFrame(
        {
            "entity_key": keys,
            config.actions.score_field: score,
            "band": ["High" if s >= 0.8 else "Medium" if s >= 0.5 else "Low" for s in score],
            "action": "Act now",
            "suppressed_reason": [("opted_out" if i % 25 == 1 else None) for i in range(len(keys))],
            "control_group": [i % 10 == 0 for i in range(len(keys))],
        }
    )
    world.storage.write_bytes(run_key(run_id, "scores.parquet"), frame.to_parquet(index=False))
    update_run(world.storage, run_id, state=RunState.DONE, finished_at=datetime(2026, 6, 3, tzinfo=UTC))
    return world, run_id, frame


def csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode()


def outcomes(scores: pd.DataFrame, *, good: bool, name: str = "churn_next_60d") -> pd.DataFrame:
    """Outcomes a perfect model would have ranked (`good`) or ranked exactly backwards."""
    column = next(
        c
        for c in scores.columns
        if c not in {"entity_key", "band", "action", "suppressed_reason", "control_group"}
    )
    positive = scores[column] >= 0.5 if good else scores[column] < 0.5
    return pd.DataFrame({"entity_key": scores["entity_key"], name: np.where(positive, "yes", "no")})


def ingest(world: World, run_id: str, payload: bytes, **changes: object) -> OutcomeReport:
    values: dict[str, object] = {
        "file_format": "csv",
        "storage": world.storage,
        "registry": world.registry,
        "alerts": world.alerts,
        "client_store": world.client_store,
        "config_root": world.config_root,
        "now": MATURE,
    }
    values.update(changes)
    return ingest_outcomes(run_id, payload, **values)  # type: ignore[arg-type]


def test_an_immature_window_is_refused_with_the_date_it_matures(
    scored: tuple[World, str, pd.DataFrame],
) -> None:
    world, run_id, scores = scored
    with pytest.raises(OutcomeError) as caught:
        ingest(world, run_id, csv(outcomes(scores, good=True)), now=datetime(2026, 7, 1, tzinfo=UTC))
    assert caught.value.code == "OUTCOME_WINDOW_NOT_MATURED"
    assert OUTCOME_ERROR_STATUS[caught.value.code] == 409
    assert "60 days after" in caught.value.message


def test_the_window_is_the_recipes_horizon_from_when_the_run_scored(
    scored: tuple[World, str, pd.DataFrame],
) -> None:
    """A single-snapshot build records no snapshot date, so the window starts when the run scored."""
    world, run_id, scores = scored
    report = ingest(world, run_id, csv(outcomes(scores, good=True)))
    window = report.window
    assert (window.horizon_days, window.horizon_source, window.anchor_source) == (
        60,
        "recipe_label",
        "scored_at",
    )
    assert window.anchor == datetime(2026, 6, 3, tzinfo=UTC)
    assert window.matures_at == datetime(2026, 8, 2, tzinfo=UTC)


def test_the_metric_is_the_models_own_on_the_control_group(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    frame = outcomes(scores, good=True)
    frame.loc[::7, "churn_next_60d"] = np.where(
        frame.loc[::7, "churn_next_60d"] == "yes", "no", "yes"
    )  # some noise
    report = ingest(world, run_id, csv(frame))
    assert report.metric.value == "roc_auc"
    assert report.metric_basis == "control_group"
    control = scores["control_group"]
    score_column = next(
        c
        for c in scores.columns
        if c not in {"entity_key", "band", "action", "suppressed_reason", "control_group"}
    )
    expected = roc_auc_score(
        (frame.loc[control, "churn_next_60d"] == "yes").to_numpy(), scores.loc[control, score_column]
    )
    assert report.real_world_score == pytest.approx(expected, abs=1e-6)
    assert report.rows_evaluated == int(control.sum()) == report.control_rows_matched
    assert (report.rows_scored, report.rows_in_file, report.rows_matched) == (1000, 1000, 1000)
    stored = read_outcome_report(world.storage, run_id)
    assert stored == report


def test_a_good_model_raises_no_alert(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    report = ingest(world, run_id, csv(outcomes(scores, good=True)))
    assert report.real_world_score == pytest.approx(1.0)
    assert report.relative_drop_pct is not None and report.relative_drop_pct < 0
    assert report.alert_raised is False
    assert world.alerts.store.query(AlertQuery()) == ()


def test_a_drop_beyond_the_setting_raises_a_performance_alert(
    scored: tuple[World, str, pd.DataFrame],
) -> None:
    world, run_id, scores = scored
    report = ingest(world, run_id, csv(outcomes(scores, good=False)))
    assert report.real_world_score == pytest.approx(0.0)
    assert report.relative_drop_pct == pytest.approx(100.0)
    assert report.alert_threshold_pct == 5
    assert report.alert_raised is True
    (alert,) = world.alerts.store.query(AlertQuery(kind=AlertKind.PERFORMANCE_DROP))
    assert alert.alert_id == report.alert_id
    assert alert.run_id == run_id and alert.model_id == report.model_version_id
    assert "C0" not in alert.message


def test_the_incrementality_input_splits_treated_and_control(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    frame = outcomes(scores, good=True)
    report = ingest(world, run_id, csv(frame))
    assert report.incrementality_key == run_key(run_id, INCREMENTALITY_INPUT_FILENAME)
    data = world.storage.read_model(report.incrementality_key, IncrementalityInput)
    positive = frame["churn_next_60d"] == "yes"
    control = scores["control_group"]
    suppressed = scores["suppressed_reason"].notna()
    treated = ~control & ~suppressed
    assert data.schema_version == 1
    assert data.outcome_kind == "binary"
    assert data.treated.rows == int(treated.sum())
    assert data.treated.positives == int((positive & treated).sum())
    assert data.control.rows == int((control & ~suppressed).sum())
    assert data.control.outcome_rate == pytest.approx(round(positive[control & ~suppressed].mean(), 6))
    assert data.suppressed_rows_excluded == int(suppressed.sum())
    assert [band.band for band in data.by_band] == ["High", "Medium", "Low"]
    assert sum(band.treated.rows for band in data.by_band) == data.treated.rows
    assert data.observed_difference == pytest.approx(data.treated.outcome_rate - data.control.outcome_rate)
    assert data.control_group_fraction == pytest.approx(0.10)


def test_nothing_row_level_is_written(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    report = ingest(world, run_id, csv(outcomes(scores, good=True)))
    written = report.model_dump_json() + world.storage.read_text(report.incrementality_key or "")
    assert "C000" not in written
    assert not any(
        "outcome" in key and key.endswith((".csv", ".parquet")) for key in world.storage.list_keys("runs/")
    )


def test_without_a_control_group_the_metric_uses_every_matched_row(
    scored: tuple[World, str, pd.DataFrame],
) -> None:
    world, run_id, scores = scored
    no_control = scores.assign(control_group=False)
    world.storage.write_bytes(run_key(run_id, "scores.parquet"), no_control.to_parquet(index=False))
    report = ingest(world, run_id, csv(outcomes(scores, good=True).iloc[:400]))
    assert report.metric_basis == "all_matched"
    assert report.rows_evaluated == report.rows_matched == 400
    assert report.incrementality_key is None


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda f: f.drop(columns=["churn_next_60d"]), "OUTCOME_COLUMN_MISSING"),
        (lambda f: pd.concat([f, f.iloc[:3]]), "OUTCOME_DUPLICATE_KEYS"),
        (lambda f: f.assign(churn_next_60d="maybe"), "OUTCOME_VALUES_INVALID"),
        (lambda f: f.assign(entity_key="nobody"), "OUTCOME_DUPLICATE_KEYS"),
        (lambda f: f.assign(entity_key=[f"X{i}" for i in range(len(f))]), "OUTCOME_NO_MATCH"),
    ],
)
def test_file_problems_are_named(scored: tuple[World, str, pd.DataFrame], mutate: object, code: str) -> None:
    world, run_id, scores = scored
    with pytest.raises(OutcomeError) as caught:
        ingest(world, run_id, csv(mutate(outcomes(scores, good=True))))  # type: ignore[operator]
    assert caught.value.code == code
    assert "C000" not in caught.value.message


def test_parquet_and_a_named_outcome_column_are_accepted(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    frame = outcomes(scores, good=True, name="left_us").assign(
        left_us=lambda f: (f["left_us"] == "yes").astype(int)
    )
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    report = ingest(world, run_id, buffer.getvalue(), file_format="parquet", outcome_column="left_us")
    assert report.outcome_name == "left_us"
    assert report.real_world_score == pytest.approx(1.0)


def test_only_a_finished_scoring_run_takes_outcomes(scored: tuple[World, str, pd.DataFrame]) -> None:
    world, run_id, scores = scored
    for bad, code in (("r_19990101_00000000", "OUTCOME_RUN_NOT_FOUND"),):
        with pytest.raises(OutcomeError) as caught:
            ingest(world, bad, csv(outcomes(scores, good=True)))
        assert caught.value.code == code
    update_run(world.storage, run_id, state=RunState.RUNNING)
    with pytest.raises(OutcomeError) as caught:
        ingest(world, run_id, csv(outcomes(scores, good=True)))
    assert caught.value.code == "OUTCOME_RUN_NOT_SCORED"
    with pytest.raises(OutcomeError) as missing:
        read_outcome_report(world.storage, run_id)
    assert missing.value.code == "OUTCOME_REPORT_NOT_FOUND"


def test_the_report_schema_is_stable() -> None:
    """Phase 5's Monitor reads this document: its fields are only ever added to, never renamed."""
    fields = set(OutcomeReport.model_fields)
    assert {
        "schema_version",
        "run_id",
        "model_version_id",
        "metric",
        "test_score",
        "real_world_score",
        "metric_basis",
        "relative_drop_pct",
        "alert_raised",
        "window",
        "rows_evaluated",
        "incrementality_key",
    } <= fields
    assert IncrementalityInput.model_fields["schema_version"].default == 1
    assert OutcomeReport.model_fields["schema_version"].default == 1
