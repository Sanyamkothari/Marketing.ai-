"""Plan J M105 (DEC-1315): the pure parts of the warnings and the value proven to date.

* the "effect fading" rule is a weighted trend over the cycles measured, and noise alone does not raise it:
  the false-alarm rate of a series whose true effect never moves is checked by simulation (the nightly
  `tests/statistical/test_fading_false_alarm.py` does the same with campaigns measured by the real code);
* a total is a figure that names every campaign's lower bound as a source, so the independent resolver of
  the Value Proof Pack's tests (and `check_figures`) can recompute it;
* the card codes are defined once.

The campaigns behind the summary are built by the real code in `tests/integration/measurement/
test_campaign_summary.py`; nothing here writes a campaign.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from engine.measurement.summary import (
    CARD_CODES,
    FADING_MIN_CYCLES,
    SUMMARY_CODES,
    CycleEffect,
    fading_verdict,
    interval_se,
    sum_figures,
)
from engine.pilot.proof import Figure, Source, check_figures, format_value
from engine.storage import LocalStorage
from tests.statistical.bands import band

Z = 1.959963984540054


def _series(truth: list[float], se: float, rng: np.random.Generator) -> list[CycleEffect]:
    return [CycleEffect(value=float(rng.normal(t, se)), se=se) for t in truth]


# --- the rule ---------------------------------------------------------------------------------------------
def test_a_clear_decline_over_four_cycles_is_fading() -> None:
    effects = [CycleEffect(value=v, se=0.01) for v in (0.08, 0.06, 0.04, 0.02)]
    verdict = fading_verdict(effects)
    assert verdict.fading is True
    assert verdict.slope is not None and verdict.slope == pytest.approx(-0.02)
    assert verdict.slope_se is not None and verdict.slope_se > 0


def test_fewer_than_the_minimum_cycles_is_never_fading_however_steep() -> None:
    assert FADING_MIN_CYCLES == 3
    steep = [CycleEffect(value=v, se=0.001) for v in (0.20, 0.01)]
    verdict = fading_verdict(steep)
    assert verdict.fading is False and verdict.slope is None
    assert "at least" in verdict.reason


@pytest.mark.parametrize("values", [(0.02, 0.04, 0.06, 0.08), (0.05, 0.05, 0.05, 0.05)])
def test_a_rising_or_a_flat_effect_is_not_fading(values: tuple[float, ...]) -> None:
    assert fading_verdict([CycleEffect(value=v, se=0.01) for v in values]).fading is False


def test_one_noisy_last_cycle_is_not_a_trend() -> None:
    effects = [CycleEffect(value=v, se=0.02) for v in (0.05, 0.05, 0.05, 0.02)]
    assert fading_verdict(effects).fading is False


def test_a_decline_too_small_for_the_noise_is_not_fading() -> None:
    effects = [CycleEffect(value=v, se=0.05) for v in (0.08, 0.07, 0.06, 0.05)]
    assert fading_verdict(effects).fading is False


def test_a_cycle_with_no_usable_range_is_left_out_not_guessed() -> None:
    effects = [
        CycleEffect(value=0.08, se=0.01),
        CycleEffect(value=0.05, se=0.0),
        CycleEffect(value=0.04, se=float("nan")),
        CycleEffect(value=0.02, se=0.01),
    ]
    verdict = fading_verdict(effects)
    assert verdict.fading is False and verdict.slope is None, "two usable cycles are below the minimum"


@pytest.mark.parametrize("cycles", [3, 4, 5, 6])
def test_noise_alone_does_not_trigger_it(cycles: int) -> None:
    """A true effect that never moves, read with noise: at most the rule's 2.5% one-sided false alarms."""
    rng = np.random.default_rng(105_000 + cycles)
    alarms = sum(fading_verdict(_series([0.05] * cycles, 0.01, rng)).fading for _ in range(4_000))
    _, high = band(0.025, 4_000)
    assert alarms / 4_000 <= high, f"{alarms} false alarms in 4000 flat series of {cycles} cycles"


@pytest.mark.parametrize("cycles", [3, 4, 5, 6])
def test_a_real_decline_is_found_most_of_the_time(cycles: int) -> None:
    """The rule has power: a true decline of 3 points a cycle read at +-1 point of noise is found."""
    rng = np.random.default_rng(105_100 + cycles)
    truth = [0.12 - 0.03 * index for index in range(cycles)]
    found = sum(fading_verdict(_series(truth, 0.01, rng)).fading for _ in range(1_000))
    assert found / 1_000 >= 0.95


def test_the_noise_of_a_cycle_is_read_from_the_width_of_its_range() -> None:
    assert interval_se(0.02, 0.06, 0.95) == pytest.approx(0.04 / (2 * Z))
    assert interval_se(0.02, 0.06, 0.90) == pytest.approx(0.04 / (2 * 1.6448536269514722))
    assert interval_se(0.06, 0.02, 0.95) is None, "a range the wrong way round is not read"
    assert interval_se(0.02, 0.02, 0.95) is None, "a range of no width has no noise to weigh by"
    assert interval_se(0.02, 0.06, 1.5) is None


# --- totals are figures that name every lower bound ---------------------------------------------------------
def _write(storage: LocalStorage, key: str, document: object) -> None:
    storage.write_bytes(key, json.dumps(document).encode())


def test_a_total_names_every_lower_bound_it_adds_and_recomputes(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "campaigns/a/incrementality_report.json", {"incremental_conversions": {"ci_low": 12.0}})
    _write(storage, "campaigns/b/incrementality_report.json", {"incremental_conversions": {"ci_high": -3.0}})
    _write(storage, "campaigns/c/x.json", {"a": 10.0, "b": 4.0, "c": 1.5})
    first = Figure(
        value=12.0,
        text=format_value("signed_count", 12.0),
        format="signed_count",
        sources=(
            Source(artefact="campaigns/a/incrementality_report.json", field="incremental_conversions.ci_low"),
        ),
    )
    turned = Figure(  # a fall in a bad outcome: the lower bound is minus the report's upper end
        value=3.0,
        text=format_value("signed_count", 3.0),
        format="signed_count",
        sources=(
            Source(
                artefact="campaigns/b/incrementality_report.json", field="incremental_conversions.ci_high"
            ),
        ),
        formula="-s0",
    )
    net = Figure(  # a figure with several sources and an arithmetic formula of its own
        value=10.0 * 4.0 - 1.5,
        text=format_value("inr", 38.5),
        format="inr",
        sources=(
            Source(artefact="campaigns/c/x.json", field="a"),
            Source(artefact="campaigns/c/x.json", field="b"),
            Source(artefact="campaigns/c/x.json", field="c"),
        ),
        formula="s0*s1-s2",
    )
    total = sum_figures([first, turned, net], "count")
    assert total.value == pytest.approx(12.0 + 3.0 + 38.5)
    assert len(total.sources) == 5
    assert total.formula is not None
    assert check_figures([first, turned, net, total], [], storage) == []
    assert total.text == format_value("count", 53.5)


def test_a_total_that_is_not_the_sum_of_its_sources_fails_the_check(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "campaigns/a/incrementality_report.json", {"incremental_conversions": {"ci_low": 12.0}})
    source = Source(artefact="campaigns/a/incrementality_report.json", field="incremental_conversions.ci_low")
    one = Figure(value=12.0, text="12", format="count", sources=(source,))
    total = sum_figures([one, one], "count")
    assert check_figures([total], [], storage) == []
    wrong = total.model_copy(update={"value": 25.0, "text": "25"})
    assert check_figures([wrong], [], storage), "a total the sources do not give is refused"


def test_a_number_in_a_sentence_that_no_figure_prints_is_refused(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    _write(storage, "campaigns/a/r.json", {"x": 12.0})
    one = Figure(
        value=12.0, text="12", format="count", sources=(Source(artefact="campaigns/a/r.json", field="x"),)
    )
    assert check_figures([one], ["at least 12 extra outcomes"], storage) == []
    assert check_figures([one], ["at least 13 extra outcomes"], storage), "13 is printed by no figure"


# --- the codes ------------------------------------------------------------------------------------------
def test_the_card_codes_are_defined_once_and_three_reuse_a_code_the_catalogue_has() -> None:
    from engine.decide.codes import PLAN_J_CODES

    assert SUMMARY_CODES == {
        "CAMPAIGN_NO_CONTROL",
        "CAMPAIGN_EARLY_LOOK",
        "CONTROL_GROUP_CONTACTED",
        "CHALLENGER_READY",
        "GROUP_BACKFIRED",
        "EFFECT_FADING",
        "SUMMARY_NOT_TRACEABLE",
    }
    assert CARD_CODES == {
        "no_control": "CAMPAIGN_NO_CONTROL",
        "early_look": "CAMPAIGN_EARLY_LOOK",
        "underpowered": "PLAN_UNDERPOWERED",
        "contamination": "CONTROL_GROUP_CONTACTED",
        "drift": "DRIFT_DRIFTED",
        "challenger_ready": "CHALLENGER_READY",
        "backfire": "GROUP_BACKFIRED",
        "uplift_not_better_than_risk": "UPLIFT_NOT_BETTER_THAN_RISK",
        "uplift_fading": "EFFECT_FADING",
    }
    assert {"PLAN_UNDERPOWERED", "UPLIFT_NOT_BETTER_THAN_RISK"} <= PLAN_J_CODES
    assert not SUMMARY_CODES & {"PLAN_UNDERPOWERED", "UPLIFT_NOT_BETTER_THAN_RISK", "DRIFT_DRIFTED"}
