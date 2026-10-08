"""`engine.decide.value`: a propensity run's expected gross value, labelled "not incremental" (Plan J M97).

The review moved this out of Phase 1's `apply_actions` (DEC-1307 (g)): Phase 1's stage is not edited, an
uplift run never gets it, and a run that does not opt in is unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.contracts import StageKey
from engine.decide.value import (
    EXPECTED_GROSS_VALUE_FILENAME,
    GROSS_VALUE_METRIC,
    GROSS_VALUE_MISSING_METRIC,
    NOT_INCREMENTAL,
    ExpectedGrossValue,
    expected_gross_value_report,
    expected_gross_values,
    install_gross_value,
)
from engine.pilot.roi import ValueCosts
from engine.stages.actions import OUTPUT_COLUMNS, apply_actions

RUN_ID = "r_20261008_cccccccc"
COSTS = ValueCosts(offer_cost=2.0, contact_cost=1.0)


def use_case(**policy: object) -> UseCaseConfig:
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    document["template"] = {"columns": []}
    document["actions"]["control_group_fraction"] = 0.0
    document["actions"]["suppression"]["suppress_recently_contacted"] = False
    document["uplift"] = {"policy": dict(policy)}
    return UseCaseConfig.model_validate(document)


def uploaded() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["C-1", "C-2", "C-3", "C-4"],
            "propensity": [0.5, 0.2, 0.9, 0.1],
            "order_value": [100.0, None, "n/a", 40.0],
        }
    )


def test_phase_1_apply_actions_adds_nothing_when_a_value_column_is_set() -> None:
    """The partner's edit added `expected_gross_value` inside `apply_actions`; Phase 1's stage stays as it was."""
    frame = uploaded()
    result = apply_actions(
        frame,
        use_case(value_column="order_value", cost_per_contact=10.0),
        run_id=RUN_ID,
        primary_key="customer_id",
    )
    assert list(result.columns) == [*frame.columns, *OUTPUT_COLUMNS]


def test_the_arithmetic_and_missing_values() -> None:
    config = use_case(value_column="order_value", margin_pct=50.0, horizon_months=2)
    gross = expected_gross_values(
        np.array([0.5, 0.2, 0.9]), np.array([100.0, np.nan, 10.0]), config, value_costs=COSTS
    )
    # p × value × 0.5 × 2 − (1 + 2 × p): 50 − 2 = 48; missing; 9 − 2.8 = 6.2.
    assert gross[0] == pytest.approx(48.0)
    assert np.isnan(gross[1])
    assert gross[2] == pytest.approx(6.2)
    own_cost = use_case(value_column="order_value", cost_per_contact=10.0)
    assert expected_gross_values(np.array([0.5]), np.array([100.0]), own_cost, value_costs=COSTS)[
        0
    ] == pytest.approx(50.0 - 10.0 - 1.0)


def test_the_report_counts_missing_values_and_is_labelled_not_incremental() -> None:
    config = use_case(value_column="order_value")
    frame = uploaded()
    banded = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    report = expected_gross_value_report(
        banded, frame, config, run_id=RUN_ID, row_key="customer_id", value_costs=COSTS
    )
    assert isinstance(report, ExpectedGrossValue)
    assert report.label == NOT_INCREMENTAL == "not incremental"
    assert "not incremental" in report.note.lower()
    assert report.rows == 4 and report.rows_missing_value == 2  # a blank and a word: never filled in
    assert report.contact_cost == 1.0 and report.offer_cost == 2.0
    # C-1: 0.5 × 100 − (1 + 1) = 48; C-4: 0.1 × 40 − (1 + 0.2) = 2.8.
    assert report.total == pytest.approx(48.0 + 2.8)
    assert sum(band.total for band in report.by_band) == pytest.approx(report.total)
    assert sum(band.rows for band in report.by_band) == report.contactable_rows


def test_no_report_without_the_option_or_the_column() -> None:
    frame = uploaded()
    plain = use_case()
    banded = apply_actions(frame, plain, run_id=RUN_ID, primary_key="customer_id")
    assert expected_gross_value_report(banded, frame, plain, run_id=RUN_ID, row_key="customer_id") is None
    other = use_case(value_column="balance")
    assert expected_gross_value_report(banded, frame, other, run_id=RUN_ID, row_key="customer_id") is None


@dataclass
class _Manifest:
    metrics: dict[str, float] = field(default_factory=dict)

    def add_metrics(self, values: dict[str, float]) -> None:
        self.metrics.update(values)


@dataclass
class _Ctx:
    config: UseCaseConfig
    run_id: str = RUN_ID
    row_key: str = "customer_id"


class _FakeFlow:
    """The parts of `_ScoreFlow` the seam touches: the stage table, the frames, `_write`, the manifest."""

    def __init__(self, config: UseCaseConfig) -> None:
        self._ctx = _Ctx(config)
        self._frame: pd.DataFrame | None = uploaded()
        self._scored: pd.DataFrame | None = None
        self._manifest = _Manifest()
        self.written: dict[str, Any] = {}
        self.ran: list[StageKey] = []

    def _bodies(self) -> tuple[tuple[StageKey, Callable[[], str]], ...]:
        return ((StageKey.ACTIONS, self._act), (StageKey.EXPORT, self._export))

    def _act(self) -> str:
        self.ran.append(StageKey.ACTIONS)
        assert self._frame is not None
        self._scored = apply_actions(self._frame, self._ctx.config, run_id=RUN_ID, primary_key="customer_id")
        return "acted"

    def _export(self) -> str:
        self.ran.append(StageKey.EXPORT)
        return "exported"

    def _write(self, filename: str, model: Any) -> str:
        self.written[filename] = model
        return filename


def _run(flow: _FakeFlow) -> list[str]:
    return [body() for _key, body in flow._bodies()]


def test_the_seam_writes_the_report_and_the_manifest_metrics_after_export() -> None:
    class Flow(_FakeFlow):
        pass

    install_gross_value(Flow)
    install_gross_value(Flow)  # idempotent
    flow = Flow(use_case(value_column="order_value"))
    assert _run(flow) == ["acted", "exported"]
    report = flow.written[EXPECTED_GROSS_VALUE_FILENAME]
    assert isinstance(report, ExpectedGrossValue) and report.label == "not incremental"
    assert flow._manifest.metrics[GROSS_VALUE_METRIC] == report.total
    assert flow._manifest.metrics[GROSS_VALUE_MISSING_METRIC] == 2.0


def test_the_seam_leaves_a_default_run_untouched() -> None:
    class Flow(_FakeFlow):
        pass

    install_gross_value(Flow)
    flow = Flow(use_case())
    original = _FakeFlow._bodies(flow)
    assert [(key, body.__func__) for key, body in Flow._bodies(flow)] == [  # type: ignore[attr-defined]
        (key, body.__func__) for key, body in original  # type: ignore[attr-defined]
    ]
    _run(flow)
    assert flow.written == {} and flow._manifest.metrics == {}


def test_the_seam_is_installed_on_the_propensity_score_flow_and_skips_uplift_runs() -> None:
    import engine.pipeline as pipeline
    from engine.decide.value import _is_uplift_flow
    from engine.uplift.flow import UpliftScoreFlow

    assert getattr(pipeline._ScoreFlow, "_gross_value_installed", False)
    assert _is_uplift_flow(UpliftScoreFlow.__new__(UpliftScoreFlow))
    assert not _is_uplift_flow(pipeline._ScoreFlow.__new__(pipeline._ScoreFlow))
