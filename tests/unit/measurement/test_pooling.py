"""Plan K M136 (DEC-1402): local empirical-Bayes pooling, the statistics and the guards.

* REML and DerSimonian-Laird are checked against hand computations: the closed form with equal standard errors
  (both give `S²_y - s²`), and the published BCG vaccine meta-analysis (Colditz et al. 1994; metafor's `dat.bcg`,
  REML `τ² = 0.3132`, `μ = -0.7145 (0.1798)`; DL `τ² = 0.3088`, `μ = -0.7141 (0.1787)`), from the trials' own counts;
* Morris's corrected share and variance are computed by hand on a fixed set;
* the guards: three campaigns draw nothing, no spread is full pooling with a warning, a bad standard error is
  refused, and DerSimonian-Laird is used (and recorded) when REML does not converge;
* the registered primary estimate is read from reports measured by the real code, never written by hand;
* the settings file: off by default, a typo refused.

The pooling of stored campaigns, end to end through the campaign routes, is in
`tests/integration/measurement/test_campaign_pooling.py`; the coverage of the intervals is checked nightly by
`tests/statistical/test_pooling_coverage.py`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.measurement import pooling
from engine.measurement.measure import measure_campaign
from engine.measurement.pooling import (
    MIN_CAMPAIGNS,
    POOL_NO_SPREAD,
    POOL_TOO_FEW,
    POOLING_CODES,
    CellMember,
    PoolingSettings,
    dl_between_variance,
    fit_cell,
    load_pooling_settings,
    primary_estimate,
    reml_between_variance,
    se_from_interval,
)
from engine.measurement.simulate import multi_arm_campaign, population, revenue_campaign
from engine.uplift.contracts import ConfidenceValue
from engine.uplift.incrementality import measure_incrementality

Z = 1.959963984540054

# The BCG vaccine trials (Colditz et al. 1994): vaccinated TB+, TB-, unvaccinated TB+, TB-.
BCG = (
    (4, 119, 11, 128),
    (6, 300, 29, 274),
    (3, 228, 11, 209),
    (62, 13536, 248, 12619),
    (33, 5036, 47, 5761),
    (180, 1361, 372, 1079),
    (8, 2537, 10, 619),
    (505, 87886, 499, 87892),
    (29, 7470, 45, 7232),
    (17, 1699, 65, 1600),
    (186, 50448, 141, 27197),
    (5, 2493, 3, 2338),
    (27, 16886, 29, 17825),
)


def _bcg() -> tuple[list[float], list[float]]:
    effects, ses = [], []
    for a, b, c, d in BCG:
        effects.append(math.log((a / (a + b)) / (c / (c + d))))
        ses.append(math.sqrt(1 / a - 1 / (a + b) + 1 / c - 1 / (c + d)))
    return effects, ses


def _members(effects: list[float], ses: list[float]) -> list[CellMember]:
    return [CellMember(str(index), y, s) for index, (y, s) in enumerate(zip(effects, ses, strict=True))]


# --- the between-campaign variance -------------------------------------------------------------------------
def test_reml_and_dl_with_equal_errors_are_the_sample_variance_less_the_noise() -> None:
    effects = [0.01, 0.03, 0.02, 0.05, 0.04]  # mean 0.03; squared deviations sum to 0.001
    ses = [0.01] * 5
    expected = 0.001 / 4 - 0.01**2  # S²_y - s² = 0.00025 - 0.0001
    reml = reml_between_variance(effects, ses)
    assert reml is not None and reml == pytest.approx(expected, rel=1e-9)
    assert dl_between_variance(effects, ses) == pytest.approx(expected, rel=1e-9)


def test_reml_matches_the_published_bcg_meta_analysis() -> None:
    effects, ses = _bcg()
    fit = fit_cell(_members(effects, ses))
    assert fit.method == "reml"
    assert fit.between_variance == pytest.approx(0.3132, abs=5e-5)
    assert fit.mean == pytest.approx(-0.7145, abs=5e-5)
    assert fit.mean_se == pytest.approx(0.1798, abs=5e-5)


def test_dl_matches_the_published_bcg_meta_analysis() -> None:
    effects, ses = _bcg()
    assert dl_between_variance(effects, ses) == pytest.approx(0.3088, abs=5e-5)
    fit = fit_cell(_members(effects, ses), method="dl")
    assert fit.method == "dl"
    assert fit.mean == pytest.approx(-0.7141, abs=5e-5)
    assert fit.mean_se == pytest.approx(0.1787, abs=5e-5)


def test_dl_is_used_and_recorded_when_reml_does_not_converge(monkeypatch: pytest.MonkeyPatch) -> None:
    effects, ses = _bcg()
    monkeypatch.setattr(pooling, "reml_between_variance", lambda *_: None)
    fit = fit_cell(_members(effects, ses))
    assert fit.method == "dl"
    assert fit.between_variance == pytest.approx(dl_between_variance(effects, ses))


def test_campaigns_that_differ_less_than_their_noise_have_no_spread() -> None:
    effects = [0.020, 0.021, 0.019, 0.020]
    ses = [0.01] * 4
    assert reml_between_variance(effects, ses) == 0.0
    assert dl_between_variance(effects, ses) == 0.0


# --- Morris's estimate and range, by hand ------------------------------------------------------------------------
def test_morris_estimate_and_range_by_hand() -> None:
    effects = [0.01, 0.03, 0.02, 0.05, 0.04]
    fit = fit_cell(_members(effects, [0.01] * 5))
    tau2, mean, k, noise = 0.00015, 0.03, 5, 0.0001
    share = (k - 3) / (k - 1) * noise / (noise + tau2)  # 0.5 * 0.4 = 0.2
    assert share == pytest.approx(0.2)
    assert fit.mean == pytest.approx(mean)
    for member, y in zip(fit.members, effects, strict=True):
        assert member.shrinkage == pytest.approx(share)
        assert member.pooled_effect == pytest.approx(share * mean + (1 - share) * y)
        # s²(1 - B) + B² / Σw + 2/(k-3) B² (y - μ)², with Σw = k / (s² + σ²) = 20,000.
        variance = noise * (1 - share) + share**2 / 20_000 + (2 / (k - 3)) * share**2 * (y - mean) ** 2
        assert member.pooled_se == pytest.approx(math.sqrt(variance))
        low, high = member.interval()
        assert (low, high) == pytest.approx(
            (member.pooled_effect - Z * member.pooled_se, member.pooled_effect + Z * member.pooled_se)
        )
    furthest = fit.members[3]  # y = 0.05: 0.00008 + 0.000002 + 0.000016 = 0.000098
    assert furthest.pooled_effect == pytest.approx(0.046)
    assert furthest.pooled_se == pytest.approx(math.sqrt(0.000098))
    assert all(member.pooled_se < member.se for member in fit.members), "narrower than each campaign's own"


def test_no_spread_draws_as_far_as_the_method_allows() -> None:
    effects = [0.020, 0.021, 0.019, 0.020, 0.022]
    fit = fit_cell(_members(effects, [0.01] * 5))
    assert fit.no_spread is True and fit.between_variance == 0.0
    for member in fit.members:
        assert member.shrinkage == pytest.approx(
            (5 - 3) / (5 - 1)
        )  # Morris's factor times a full share of one


def test_three_campaigns_draw_nothing_and_keep_their_own_range() -> None:
    """Morris's `(k - 3) / (k - 1)` is zero at three: the estimate is each campaign's own, honestly."""
    effects, ses = [0.01, 0.04, 0.02], [0.01, 0.012, 0.009]
    fit = fit_cell(_members(effects, ses))
    for member, y, s in zip(fit.members, effects, ses, strict=True):
        assert member.shrinkage == 0.0
        assert member.pooled_effect == pytest.approx(y)
        assert member.pooled_se == pytest.approx(s)


def test_a_cell_needs_two_members_and_honest_errors() -> None:
    with pytest.raises(ValueError, match="at least two"):
        fit_cell([CellMember("a", 0.01, 0.01)])
    for bad in (0.0, -0.01, math.nan, math.inf):
        with pytest.raises(ValueError, match="standard error"):
            fit_cell([CellMember("a", 0.01, 0.01), CellMember("b", 0.02, bad)])


# --- reading the registered estimate --------------------------------------------------------------------------
def test_the_noise_of_an_estimate_is_read_from_its_range() -> None:
    assert se_from_interval(ConfidenceValue(value=0.04, ci_low=0.02, ci_high=0.06)) == pytest.approx(
        0.04 / (2 * Z)
    )
    assert se_from_interval(ConfidenceValue(value=0.04, ci_low=None, ci_high=0.06)) is None
    assert se_from_interval(ConfidenceValue(value=0.04, ci_low=0.06, ci_high=0.02)) is None
    assert se_from_interval(ConfidenceValue(value=0.04, ci_low=0.04, ci_high=0.04)) is None


def test_a_yes_no_campaign_is_pooled_on_its_difference_in_rates() -> None:
    sim = population(4_000, 0.05, 0.02, seed=1361)
    report = measure_incrementality(sim.scores, sim.outcomes, **sim.measure_kwargs)
    found = primary_estimate(report)
    assert found is not None and found == ("absolute_lift", report.absolute_lift)


def test_an_amount_uses_the_registered_adjusted_estimate_when_there_is_one() -> None:
    sim = revenue_campaign(2_000, 5.0, seed=1362, rho=0.6)
    adjusted = measure_incrementality(sim.scores, sim.outcomes, **sim.adjusted_kwargs)
    plain = measure_incrementality(sim.scores, sim.outcomes, **sim.measure_kwargs)
    assert adjusted.adjusted_interval is not None
    assert primary_estimate(adjusted) == ("adjusted_interval", adjusted.adjusted_interval)
    assert primary_estimate(plain) == ("mean_difference_ci", plain.mean_difference_ci)


def test_several_offers_pool_every_offer_together_and_never_the_first_alone() -> None:
    sim = multi_arm_campaign(6_000, 0.10, (0.03, 0.01), seed=1363)
    combined = measure_campaign(sim.scores, sim.outcomes, **sim.measure_kwargs, combine_offers=True)
    first_only = measure_campaign(sim.scores, sim.outcomes, **sim.measure_kwargs)
    assert combined.offers_combined is not None
    assert primary_estimate(combined) == (
        "offers_combined.absolute_lift",
        combined.offers_combined.absolute_lift,
    )
    assert (
        primary_estimate(first_only) is None
    ), "a report of several offers not added together is the first offer's"


# --- settings and codes ------------------------------------------------------------------------------------------
def test_pooling_is_off_by_default_and_in_the_shipped_file(config_root: Path, tmp_path: Path) -> None:
    assert PoolingSettings().enabled is False
    assert load_pooling_settings(tmp_path) == PoolingSettings(), "no file: every default"
    shipped = load_pooling_settings(config_root)
    assert shipped.enabled is False and shipped.min_campaigns == MIN_CAMPAIGNS == 3


def test_a_settings_file_is_read_and_a_typo_is_refused(tmp_path: Path) -> None:
    (tmp_path / "measurement.yaml").write_text("pooling:\n  enabled: true\n  min_campaigns: 4\n")
    assert load_pooling_settings(tmp_path) == PoolingSettings(enabled=True, min_campaigns=4)
    (tmp_path / "measurement.yaml").write_text("pooling:\n  enable: true\n")
    with pytest.raises(ValidationError):
        load_pooling_settings(tmp_path)
    (tmp_path / "measurement.yaml").write_text("pooling:\n  enabled: true\n  min_campaigns: 2\n")
    with pytest.raises(ValidationError):
        load_pooling_settings(tmp_path)


def test_the_codes() -> None:
    assert POOLING_CODES == {POOL_TOO_FEW, POOL_NO_SPREAD} == {"POOL_TOO_FEW", "POOL_NO_SPREAD"}
