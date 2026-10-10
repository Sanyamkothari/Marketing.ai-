"""Plan J M100: several offers against one shared control - measurement and the best-offer policy value.

`measure_campaign(..., arm_column=)` measures each offer against the shared control with the unchanged
`measure_incrementality`; its own fields are the first offer's (DEC-668 (3)) and `arms` lists every
offer. `arm_policy_value` is the paired, out-of-sample comparison a later champion rule would use
(DEC-1310 (h)). Every test here fails on the commit before M100.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from engine.config import Metric
from engine.measurement.arms import (
    MULTI_ARM_CODES,
    MULTI_ARM_PROMOTION_REFUSED,
    PROMOTION_REFUSED_REASON,
    PROMOTION_UNCHECKED_REASON,
    arm_policy_value,
    promotion_refusal,
)
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import ARM_COLUMN, multi_arm_campaign, population
from engine.storage import StorageError
from engine.uplift.contracts import IncrementalityReport
from engine.uplift.flow import model_card_key
from engine.uplift.incrementality import measure_incrementality

if TYPE_CHECKING:
    from engine.contracts import ModelVersion
    from engine.storage import Storage

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_each_offer_is_measured_against_the_shared_control() -> None:
    campaign = multi_arm_campaign(6_000, 0.10, (0.03, 0.08), seed=1)
    report = measure_campaign(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.arms is not None and [a.arm for a in report.arms] == ["offer_1", "offer_2"]
    first, second = report.arms
    # Each summary names the configured control level, not a made-up label (review finding).
    assert first.position == 1 and second.position == 2
    assert first.control == second.control == campaign.levels[0] == "none"
    # The shared control is the same customers for both offers.
    assert first.control_rows == second.control_rows == report.control_rows == 2_000
    assert first.treated_rows == second.treated_rows == 2_000
    # Each offer's measured lift is the unchanged computation on its customers and the control's.
    control = campaign.scores["control_group"].to_numpy()
    for summary, level in zip(report.arms, ("offer_1", "offer_2"), strict=True):
        mine = campaign.scores[control | (campaign.scores[ARM_COLUMN] == level).to_numpy()]
        alone = measure_incrementality(
            mine,
            campaign.outcomes,
            **{
                k: v
                for k, v in campaign.measure_kwargs.items()
                if k not in {"arm_column", "arms", "control_level"}
            },
        )
        assert summary.effect == alone.absolute_lift
        assert summary.treated_rate == alone.treated_rate and summary.p_value == alone.p_value
        assert summary.incremental_conversions == alone.incremental_conversions
    assert second.effect is not None and second.effect.ci_low is not None and second.effect.ci_low > 0.0


def test_the_report_fields_keep_their_meaning_as_the_first_offer_against_the_control() -> None:
    campaign = multi_arm_campaign(3_000, 0.2, (0.05, -0.05), seed=2)
    report = measure_campaign(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.arms is not None
    first = report.arms[0]
    assert report.absolute_lift == first.effect
    assert (report.treated_rows, report.control_rows) == (first.treated_rows, first.control_rows)
    assert report.treated_conversions == first.treated_conversions


def test_the_offers_are_listed_in_the_order_configured_whatever_the_row_order() -> None:
    campaign = multi_arm_campaign(1_200, 0.2, (0.0, 0.0, 0.0), seed=3)
    kwargs = dict(campaign.measure_kwargs)
    given = measure_campaign(
        campaign.scores,
        campaign.outcomes,
        **{**kwargs, "arms": ("offer_3", "offer_1"), "control_level": "none"},
    )
    assert given.arms is not None and [a.arm for a in given.arms] == ["offer_3", "offer_1"]
    assert given.arms[0].control == "none"
    assert given.treated_rows == given.arms[0].treated_rows
    # Review finding: the report's own fields are the first CONFIGURED offer's, so sorting the file
    # differently changes nothing.
    report = measure_campaign(campaign.scores, campaign.outcomes, **kwargs)
    shuffled = campaign.scores.sort_values(ARM_COLUMN, ascending=False, kind="stable")
    again = measure_campaign(shuffled, campaign.outcomes, **kwargs)
    assert report.arms is not None and again.arms is not None
    assert [a.arm for a in report.arms] == ["offer_1", "offer_2", "offer_3"]
    assert again.model_copy(update={"computed_at": report.computed_at}) == report


def test_several_offers_need_the_configured_offers_and_control_level() -> None:
    """Review findings: no implicit order from the file, and no made-up "control" label."""
    campaign = multi_arm_campaign(600, 0.2, (0.0, 0.0), seed=8)
    kwargs = dict(campaign.measure_kwargs)
    for missing in ("arms", "control_level"):
        with pytest.raises(ValueError, match=f"needs `{missing}`"):
            measure_campaign(campaign.scores, campaign.outcomes, **{**kwargs, missing: None})


def test_without_an_arm_column_the_report_is_exactly_the_binary_one() -> None:
    campaign = population(2_000, 0.1, 0.02, seed=4)
    report = measure_campaign(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.arms is None
    assert "arms" not in report.model_dump(mode="json")
    alone = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.model_copy(update={"computed_at": alone.computed_at}) == alone


def test_a_missing_arm_column_or_no_offer_named_is_refused() -> None:
    campaign = multi_arm_campaign(600, 0.2, (0.0, 0.0), seed=5)
    kwargs = dict(campaign.measure_kwargs)
    with pytest.raises(ValueError, match="no column"):
        measure_campaign(campaign.scores, campaign.outcomes, **{**kwargs, "arm_column": "nope"})
    blank = campaign.scores.assign(**{ARM_COLUMN: ""})
    with pytest.raises(ValueError, match="names one of the offers"):
        measure_campaign(blank, campaign.outcomes, **kwargs)


def test_the_arms_round_trip_through_the_report_contract() -> None:
    campaign = multi_arm_campaign(900, 0.2, (0.05, 0.05), seed=6)
    report = measure_campaign(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert IncrementalityReport.model_validate_json(report.model_dump_json()) == report


def test_every_configured_offer_is_added_together_against_the_shared_control_once() -> None:
    """DEC-1314 (s): `offers_combined` is the pooled comparison of every configured offer's customers with the
    shared control: the unchanged `measure_incrementality` on them, whose extra outcomes are exactly the sum of
    the offers' own. An offer the configuration does not name is in no comparison, combined or not."""
    campaign = multi_arm_campaign(2_400, 0.1, (0.02, 0.04, 0.06), seed=9)
    kwargs = dict(campaign.measure_kwargs)
    report = measure_campaign(
        campaign.scores,
        campaign.outcomes,
        **{**kwargs, "arms": ("offer_3", "offer_1"), "control_level": "none"},
    )
    assert report.arms is not None and report.offers_combined is not None
    whole = report.offers_combined
    assert whole.offers == ("offer_3", "offer_1") and whole.control == "none" and whole.method == "pooled"
    control = campaign.scores["control_group"].to_numpy()
    named = campaign.scores[ARM_COLUMN].isin(["offer_3", "offer_1"]).to_numpy()
    pooled = measure_incrementality(
        campaign.scores[control | named],
        campaign.outcomes,
        **{k: v for k, v in kwargs.items() if k not in {"arm_column", "arms", "control_level"}},
    )
    assert whole.absolute_lift == pooled.absolute_lift
    assert whole.incremental_conversions == pooled.incremental_conversions
    assert (whole.treated_rows, whole.control_rows) == (pooled.treated_rows, pooled.control_rows)
    assert whole.treated_conversions == sum(a.treated_conversions or 0 for a in report.arms)
    assert whole.incremental_conversions is not None
    assert whole.incremental_conversions.value == pytest.approx(
        sum(a.incremental_conversions.value for a in report.arms if a.incremental_conversions is not None)
    )
    assert whole.rows_without_outcome == pooled.rows_without_outcome
    assert IncrementalityReport.model_validate_json(report.model_dump_json()) == report


def test_one_offer_or_none_has_nothing_to_add_together() -> None:
    campaign = multi_arm_campaign(1_200, 0.2, (0.05, 0.05), seed=10)
    kwargs = dict(campaign.measure_kwargs)
    one = measure_campaign(campaign.scores, campaign.outcomes, **{**kwargs, "arms": ("offer_1",)})
    assert one.arms is not None and one.offers_combined is None
    assert "offers_combined" not in one.model_dump(mode="json")
    binary = population(2_000, 0.1, 0.02, seed=4)
    plain = measure_campaign(binary.scores, binary.outcomes, **binary.measure_kwargs)
    assert "offers_combined" not in plain.model_dump(mode="json")


# ---------------------------------------------------------------------------
# arm_policy_value
# ---------------------------------------------------------------------------
def _planted(rows: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Two segments, each answering one offer; random arms; a model that predicts the truth."""
    rng = np.random.default_rng(seed)
    segment = rng.random(rows) < 0.5
    tau = np.column_stack([np.where(segment, 0.2, 0.0), np.where(segment, 0.0, 0.2)])
    arm = rng.integers(0, 3, size=rows)
    p = 0.1 + np.where(arm == 0, 0.0, tau[np.arange(rows), np.maximum(arm - 1, 0)])
    y = (rng.random(rows) < p).astype(int)
    return tau, arm, y, segment


def _value(uplift: np.ndarray, arm: np.ndarray, y: np.ndarray, **kwargs: object):  # type: ignore[no-untyped-def]
    return arm_policy_value(
        uplift,
        arm,
        y,
        levels=("none", "a", "b"),
        values=kwargs.pop("values", None),  # type: ignore[arg-type]
        value_column=kwargs.pop("value_column", None),  # type: ignore[arg-type]
        samples=200,
        seed=7,
        run_id="r",
        causal=True,
        now=NOW,
    )


def test_choosing_the_responding_offer_beats_the_first_offer_alone() -> None:
    tau, arm, y, _ = _planted(9_000, 1)
    value = _value(tau, arm, y)
    # Truth: best offer +0.2 for everyone; the first offer alone +0.1 on average.
    assert value.best_offer.ci_low is not None and value.best_offer.ci_low < 0.2 < value.best_offer.ci_high
    assert (
        value.first_treatment.ci_low is not None
        and value.first_treatment.ci_low < 0.1 < value.first_treatment.ci_high
    )
    assert value.difference.ci_low is not None and value.difference.ci_low > 0.0
    assert value.best_offer_better
    assert abs(value.policy_shares["a"] - 0.5) < 0.03 and value.policy_shares["none"] == 0.0
    assert value.promotion_code == MULTI_ARM_PROMOTION_REFUSED and not value.costs_included
    assert value.arm_rows == tuple(int(c) for c in np.bincount(arm))


def test_when_the_first_offer_is_best_for_everyone_the_difference_is_zero() -> None:
    _, arm, y, _ = _planted(3_000, 2)
    uplift = np.column_stack([np.full(len(arm), 0.2), np.zeros(len(arm))])
    value = _value(uplift, arm, y)
    assert value.difference.value == 0.0 and not value.best_offer_better
    assert value.best_offer == value.first_treatment


def test_values_weight_the_conversions_and_a_missing_value_counts_as_zero() -> None:
    tau, arm, y, _ = _planted(3_000, 3)
    values = np.full(len(arm), 2.0)
    values[:10] = np.nan
    weighted = _value(tau, arm, y, values=values, value_column="spend")
    plain = _value(tau, arm, y)
    assert weighted.value_weighted and weighted.value_column == "spend" and weighted.values_missing == 10
    assert not plain.value_weighted and plain.value_column is None
    # Every customer is worth 2 except ten counted at zero: about twice the unweighted value.
    assert weighted.best_offer.value == pytest.approx(2.0 * plain.best_offer.value, abs=0.05)
    everyone = _value(tau, arm, y, values=np.full(len(arm), 2.0), value_column="spend")
    assert everyone.best_offer.value == pytest.approx(2.0 * plain.best_offer.value)


def test_an_arm_without_hold_out_customers_is_refused() -> None:
    tau, arm, y, _ = _planted(300, 4)
    arm[arm == 2] = 1
    with pytest.raises(ValueError, match="Every level"):
        _value(tau, arm, y)


def test_the_paired_bootstrap_is_deterministic_for_a_seed() -> None:
    tau, arm, y, _ = _planted(2_000, 5)
    assert _value(tau, arm, y).difference == _value(tau, arm, y).difference


def test_the_new_code_is_upper_snake_case_and_published_once() -> None:
    assert frozenset({"MULTI_ARM_PROMOTION_REFUSED"}) == MULTI_ARM_CODES


# ---------------------------------------------------------------------------
# promotion_refusal: the hand-promotion guard fails closed (review finding)
# ---------------------------------------------------------------------------
class _Store:
    """A storage whose model card and run configuration are given, or unreadable when None."""

    def __init__(self, card: object | None, resolved: object | None) -> None:
        self._files = {model_card_key("models/m"): card, "runs/r/run_config.json": resolved}

    def read_model(self, key: str, model_type: type) -> object:
        found = self._files.get(key)
        if found is None:
            raise StorageError("KEY_NOT_FOUND", f"no {key}", key=key)
        return found


def _version(metric: Metric = Metric.AUUC) -> ModelVersion:
    return cast(
        "ModelVersion",
        SimpleNamespace(metric=metric, predictor_key="models/m", run_config_key="runs/r/run_config.json"),
    )


def _resolved(levels: tuple[str, ...]) -> object:
    return SimpleNamespace(config=SimpleNamespace(uplift=SimpleNamespace(treatment_levels=levels)))


def test_the_promotion_guard_reads_the_card_then_the_run_config_and_fails_closed() -> None:
    several = SimpleNamespace(treatment_levels=("none", "a", "b"))
    one = SimpleNamespace(treatment_levels=None)

    def refusal(card: object | None, resolved: object | None, metric: Metric = Metric.AUUC) -> str | None:
        return promotion_refusal(cast("Storage", _Store(card, resolved)), _version(metric))

    assert refusal(several, None) == PROMOTION_REFUSED_REASON
    assert refusal(one, _resolved(("none", "a", "b"))) is None  # a readable card is the record
    assert refusal(None, _resolved(("none", "a", "b"))) == PROMOTION_REFUSED_REASON
    assert refusal(None, _resolved(())) is None  # a binary model whose card is lost may still be promoted
    assert refusal(None, None) == PROMOTION_UNCHECKED_REASON
    assert refusal(None, None, Metric.ROC_AUC) is None  # not an uplift model: nothing to check
