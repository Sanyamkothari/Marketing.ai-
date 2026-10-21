"""The weekly monitor: forecast against actual, re-run when off by more than 2x or the proof is negative."""

from __future__ import annotations

import pytest

from autonomy.contracts import Estimate
from autonomy.scientist import (
    BlanketPolicy,
    Forecast,
    Monitor,
    SegmentRule,
    UpliftPolicy,
    WeeklyActual,
    check_week,
    forecast_of,
    run_scientist,
)
from tests.v2.scientist.test_menu_campaign import make_campaign
from tests.v2.scientist.test_menu_evaluate import replace


def rerun(m: Monitor) -> bool:
    return m.should_rerun


def interval(point: float, half: float) -> Estimate:
    return Estimate(point=point, low=point - half, high=point + half)


FORECAST = Forecast("segment rule", interval(0.50, 0.10), 5000)


def week(point: float, half: float = 0.05, n: int = 2000, name: str = "2026-W43") -> WeeklyActual:
    return WeeklyActual(name, interval(point, half), n)


@pytest.mark.parametrize(
    ("actual", "status", "rerun"),
    [
        (0.50, "ok", False),
        (0.99, "ok", False),  # 1.98x: inside the band
        (1.01, "rerun", True),  # 2.02x
        (1.00, "ok", False),  # exactly 2x is not "more than"
        (0.26, "ok", False),  # 0.52x
        (0.24, "rerun", True),  # 0.48x
        (0.0, "rerun", True),  # the gain vanished
        (-0.01, "rerun", True),  # point below zero, interval still touches it
    ],
)
def test_the_two_x_rule(actual: float, status: str, rerun: bool) -> None:
    s = check_week(FORECAST, week(actual, half=0.02))
    assert (s.status, s.rerun) == (status, rerun), s.explanation


def test_proof_negative_means_run_again_whatever_the_sample() -> None:
    s = check_week(FORECAST, week(-0.30, half=0.10, n=50))  # interval -0.40 to -0.20, wholly below zero
    assert s.rerun and "proof is negative" in s.reasons[0]
    just_touching = check_week(FORECAST, week(-0.10, half=0.10, n=2000))
    assert "proof is negative" not in " ".join(
        just_touching.reasons
    )  # the upper end is exactly zero: not negative


def test_a_small_or_noisy_week_waits_instead_of_raising_a_false_alarm() -> None:
    few = check_week(FORECAST, week(1.5, half=0.02, n=80))
    assert few.status == "wait" and not few.rerun and "80 customers" in few.reasons[0]
    noisy = check_week(
        FORECAST, week(1.5, half=0.60, n=3000)
    )  # known only to within 0.6, wider than the forecast
    assert noisy.status == "wait" and not noisy.rerun


def test_a_zero_forecast_has_only_the_proof_rule() -> None:
    none = Forecast("send nothing", interval(0.0, 0.0), 5000)
    assert check_week(none, week(0.0)).status == "ok"
    assert check_week(none, week(3.0, half=0.1)).status == "ok"  # no ratio to drift from
    assert check_week(none, week(-1.0, half=0.1)).rerun  # but a negative proof is still a re-run


def test_the_band_must_be_above_one() -> None:
    with pytest.raises(ValueError):
        check_week(FORECAST, week(0.5), band=1.0)
    s = check_week(FORECAST, week(0.70, half=0.02), band=1.3)
    assert s.rerun  # 1.4x with a tighter band


def test_monitor_remembers_weeks_and_replaces_a_week_seen_twice() -> None:
    m = Monitor(FORECAST)
    assert not rerun(m)
    m.observe(week(0.50, name="2026-W43"))
    m.observe(week(0.45, name="2026-W44"))
    assert not rerun(m) and len(m.history) == 2
    m.observe(week(0.10, half=0.02, name="2026-W45"))
    assert rerun(m) and len(m.history) == 3
    m.observe(week(0.50, name="2026-W45"))  # the late-arriving corrected figure replaces it
    assert len(m.history) == 3 and not rerun(m)
    assert "on track" in m.history[-1].explanation


def test_the_forecast_comes_from_what_the_selection_will_run() -> None:
    c = make_campaign(20000, seed=31)
    sel = run_scientist(c, [SegmentRule(), UpliftPolicy()])
    f = forecast_of(sel)
    assert f is not None and sel.outcome == "candidate"
    assert f.gain.point > 0 and f.n_held_out == sel.evaluations.n_held  # type: ignore[union-attr]
    assert f.label == sel.run.label(sel.evaluations.arm_names)  # type: ignore[union-attr]
    stay = run_scientist(make_campaign(20000, seed=32, targeting=False), [SegmentRule()])
    g = forecast_of(stay)
    assert g is not None and stay.run == BlanketPolicy(0) and g.gain.point == 0.0
    unlogged = replace(make_campaign(100), propensity=None)
    assert forecast_of(run_scientist(unlogged, [SegmentRule()])) is None
