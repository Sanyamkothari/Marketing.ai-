"""Plan K M138 (DEC-1403): the harm alarm's statistic, its one-sided rule and its settings.

* the statistic is design §9.4's normal-mixture ratio, checked against a computation by hand, alone and through
  the real measurement (`measure_campaign` on frames whose counts are known);
* it is one-sided: a planted benefit never fires, however large, and for an outcome the campaign exists to
  prevent (a churn rate) the harmful side is up, not down;
* a campaign of several offers splits alpha across them; an amount is read from its planned estimate's range;
* the setting is off by default, in the model and in the shipped `configs/measurement.yaml`.

The check written to a campaign, the card, the alert and the scheduled cycle are in
`tests/integration/measurement/test_harm_alarm_loop.py`; the false-alarm rate and the power are nightly
(`tests/statistical/test_harm_alarm.py`).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from engine.measurement.harm import (
    HARM_ALARM,
    HARM_CODES,
    WHOLE_CAMPAIGN,
    HarmAlarmSettings,
    harm_alert_message,
    harm_looks,
    judge,
    load_harm_alarm_settings,
    mixture_log_statistic,
    rate_difference,
)
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import multi_arm_campaign, revenue_campaign
from engine.pilot.plain import jargon_in
from engine.uplift.contracts import IncrementalityReport

ON = HarmAlarmSettings(enabled=True)
START = datetime(2026, 5, 1, tzinfo=UTC)


def _by_hand(difference: float, variance: float, tau: float) -> float:
    """Design §9.4 written out: sqrt(V / (V + tau^2)) * exp(tau^2 d^2 / (2 V (V + tau^2)))."""
    return math.sqrt(variance / (variance + tau**2)) * math.exp(
        tau**2 * difference**2 / (2.0 * variance * (variance + tau**2))
    )


def _counted(treated: tuple[int, int], control: tuple[int, int]) -> IncrementalityReport:
    """Frames with exactly `(rows, conversions)` contacted and held back, every outcome in."""
    rows = treated[0] + control[0]
    keys = [f"C{index:06d}" for index in range(rows)]
    held = np.arange(rows) >= treated[0]
    converted = np.zeros(rows, dtype=int)
    converted[: treated[1]] = 1
    converted[treated[0] : treated[0] + control[1]] = 1
    scores = pd.DataFrame({"customer_id": keys, "control_group": held, "suppressed_reason": ""})
    outcomes = pd.DataFrame({"customer_id": keys, "converted": converted})
    return measure_campaign(
        scores,
        outcomes,
        run_id="hand",
        primary_key="customer_id",
        outcome_column="converted",
        treatment_time=START,
        outcome_window_days=30,
        as_of=START + timedelta(days=45),
    )


# --- the statistic -----------------------------------------------------------------------------------------
def test_the_statistic_is_the_normal_mixture_ratio_computed_by_hand() -> None:
    # 80 of 1,000 contacted and 110 of 1,000 held back converted; the plan's smallest effect is 3 points.
    pt, pc = 0.08, 0.11
    variance = pt * (1 - pt) / 1000 + pc * (1 - pc) / 1000
    assert variance == pytest.approx(0.0001715)
    expected = _by_hand(-0.03, variance, 0.03)
    assert expected == pytest.approx(3.625, abs=0.001)
    assert math.exp(mixture_log_statistic(-0.03, variance, 0.03**2)) == pytest.approx(expected, rel=1e-12)
    assert rate_difference(1000, 1000, pt, pc) == pytest.approx((-0.03, variance))


def test_the_statistic_of_a_measured_campaign_is_read_from_its_own_report() -> None:
    report = _counted((1000, 80), (1000, 110))
    assert (report.treated_rows, report.control_rows) == (1000, 1000)
    (look,) = harm_looks(report, mixing_sd=0.03, outcome_is_good=True, settings=ON)
    variance = 0.08 * 0.92 / 1000 + 0.11 * 0.89 / 1000
    assert look.arm == WHOLE_CAMPAIGN
    assert look.difference == pytest.approx(-0.03)
    assert look.variance == pytest.approx(variance)
    assert look.statistic == pytest.approx(_by_hand(-0.03, variance, 0.03), rel=1e-9)
    assert look.threshold == pytest.approx(20.0)
    assert look.harmful is True and look.fired is False, "3.6 is below 1/alpha = 20: no alarm yet"
    (stronger,) = harm_looks(
        _counted((1000, 60), (1000, 110)), mixing_sd=0.03, outcome_is_good=True, settings=ON
    )
    variance = 0.06 * 0.94 / 1000 + 0.11 * 0.89 / 1000
    assert stronger.statistic == pytest.approx(_by_hand(-0.05, variance, 0.03), rel=1e-9)
    assert stronger.statistic == pytest.approx(385.0, rel=0.01)
    assert stronger.fired is True


def test_too_few_customers_are_not_judged_and_say_so() -> None:
    (look,) = harm_looks(_counted((60, 0), (60, 30)), mixing_sd=0.03, outcome_is_good=True, settings=ON)
    assert look.fired is False and look.statistic is None
    assert look.reason is not None and jargon_in(look.reason) == ()


def test_a_statistic_too_large_for_a_float_keeps_its_logarithm_and_no_made_up_value() -> None:
    (look,) = harm_looks(
        _counted((50_000, 0), (50_000, 25_000)), mixing_sd=0.03, outcome_is_good=True, settings=ON
    )
    assert look.fired is True
    assert look.log_statistic is not None and look.log_statistic > 700
    assert look.statistic is None


# --- one-sided ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("difference", [0.01, 0.05, 0.30])
def test_a_planted_benefit_never_fires_however_strong(difference: float) -> None:
    judged = judge(difference, 1e-6, mixing_sd=0.03, alpha=0.05, outcome_is_good=True)
    assert judged.log_statistic > math.log(20.0), "the evidence is strong..."
    assert judged.harmful is False and judged.fired is False, "...but it is a benefit, never an alarm"
    harm = judge(-difference, 1e-6, mixing_sd=0.03, alpha=0.05, outcome_is_good=True)
    assert harm.log_statistic == pytest.approx(judged.log_statistic), "the statistic itself is symmetric"
    assert harm.fired is True


def test_for_an_outcome_to_prevent_the_harmful_side_is_up() -> None:
    more_churn = judge(0.05, 1e-5, mixing_sd=0.03, alpha=0.05, outcome_is_good=False)
    less_churn = judge(-0.05, 1e-5, mixing_sd=0.03, alpha=0.05, outcome_is_good=False)
    assert more_churn.fired is True and less_churn.fired is False


def test_a_measured_benefit_raises_nothing() -> None:
    (look,) = harm_looks(
        _counted((1000, 200), (1000, 100)), mixing_sd=0.03, outcome_is_good=True, settings=ON
    )
    assert look.statistic is not None and look.statistic > 1e6
    assert (look.harmful, look.fired) == (False, False)


# --- several offers and amounts ----------------------------------------------------------------------------------
def test_several_offers_are_each_judged_against_the_shared_control_at_alpha_over_m() -> None:
    sim = multi_arm_campaign(9_000, 0.12, (0.0, -0.08), seed=1381)
    report = measure_campaign(
        sim.scores, sim.outcomes, intended_column="intended_treatment", **sim.measure_kwargs
    )
    looks = harm_looks(report, mixing_sd=0.03, outcome_is_good=True, settings=ON)
    assert [look.arm for look in looks] == list(sim.levels[1:])
    assert all(look.threshold == pytest.approx(2 / 0.05) for look in looks)
    assert [look.fired for look in looks] == [False, True], "only the harmful offer"
    assert report.arms is not None
    second = report.arms[1]
    assert second.treated_rate is not None and second.control_rate is not None
    assert looks[1].difference == pytest.approx(second.treated_rate - second.control_rate)


def test_an_amount_is_read_from_its_planned_estimate_and_its_range() -> None:
    sim = revenue_campaign(4_000, -12.0, seed=1382)
    report = measure_campaign(sim.scores, sim.outcomes, **sim.measure_kwargs)
    (look,) = harm_looks(report, mixing_sd=10.0, outcome_is_good=True, settings=ON)
    assert report.mean_difference_ci is not None
    ci = report.mean_difference_ci
    assert ci.ci_low is not None and ci.ci_high is not None
    assert look.difference == pytest.approx(ci.value)
    se_from_range = (ci.ci_high - ci.ci_low) / (2 * 1.959963984540054)
    assert look.variance == pytest.approx(se_from_range**2)
    assert look.harmful is True and look.fired is True


# --- the settings -------------------------------------------------------------------------------------------------
def test_the_alarm_is_off_by_default_in_the_model_and_in_the_shipped_file(config_root: Path) -> None:
    assert HarmAlarmSettings().enabled is False
    shipped = load_harm_alarm_settings(config_root)
    assert shipped == HarmAlarmSettings()
    document = yaml.safe_load((config_root / "measurement.yaml").read_text(encoding="utf-8"))
    assert document["harm_alarm"] == HarmAlarmSettings().model_dump()


def test_a_missing_file_is_every_default_and_a_typo_is_refused(tmp_path: Path) -> None:
    assert load_harm_alarm_settings(tmp_path) == HarmAlarmSettings()
    (tmp_path / "measurement.yaml").write_text("harm_alarm:\n  enable: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_harm_alarm_settings(tmp_path)
    (tmp_path / "measurement.yaml").write_text("harm_alarm:\n  alpha: 0.5\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_harm_alarm_settings(tmp_path)
    (tmp_path / "measurement.yaml").write_text("pooling:\n  enabled: true\n", encoding="utf-8")
    assert load_harm_alarm_settings(tmp_path) == HarmAlarmSettings(), "another section is not read"


def test_the_code_is_named_once() -> None:
    assert HARM_CODES == {HARM_ALARM} == {"HARM_ALARM"}


def test_the_alert_says_what_to_do_and_never_reads_as_a_result() -> None:
    from engine.measurement.harm import HarmCheck, HarmOutcome

    check = HarmCheck(
        campaign_id="cmp_1",
        checked_at=START,
        as_of=START,
        status="no_alarm",
        reason="-",
        alpha=0.05,
        outcome_kind="binary",
        early_look=True,
    )
    message = harm_alert_message(HarmOutcome(check, ()), use_case_name="Win-back Campaign")
    assert "cmp_1" in message and "Win-back Campaign" in message
    assert "nothing has been stopped" in message and "not a result" in message
    assert jargon_in(message) == ()
