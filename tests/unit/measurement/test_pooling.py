"""Plan K M136 (DEC-1402): local empirical-Bayes pooling, the statistics and the guards.

* REML and DerSimonian-Laird are checked against hand computations: the closed form with equal standard errors
  (both give `S²_y - s²`), and the published BCG vaccine meta-analysis (Colditz et al. 1994; metafor's `dat.bcg`,
  REML `τ² = 0.3132`, `μ = -0.7145 (0.1798)`; DL `τ² = 0.3088`, `μ = -0.7141 (0.1787)`), from the trials' own counts;
* Morris's corrected share and his own variance, `s²(1 - ((k - 1)/k) B) + (2/(k - 3)) B² (y - μ̂)²`, are computed
  by hand on a fixed set;
* the guards: three campaigns draw nothing and are not pooled (`POOL_TOO_FEW`), no spread draws as far as the
  method allows with a warning, a bad standard error is refused, and DerSimonian-Laird is used (and recorded) when
  REML does not converge;
* the registered primary estimate is read from reports measured by the real code, never written by hand, and the
  adjusted estimate is the registered one for either kind of outcome;
* a file is current only while its own report, every member's report and the settings are unchanged;
* the settings file: off by default, a typo refused with a coded configuration error.

The pooling of stored campaigns, end to end through the campaign routes, is in
`tests/integration/measurement/test_campaign_pooling.py`; the coverage of the intervals is checked nightly by
`tests/statistical/test_pooling_coverage.py`.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.config import ConfigError
from engine.measurement import pooling
from engine.measurement.measure import measure_campaign
from engine.measurement.pooling import (
    MIN_CAMPAIGNS,
    POOL_NO_SPREAD,
    POOL_TOO_FEW,
    POOLING_CODES,
    CellEntry,
    CellMember,
    PoolingCell,
    PoolingSettings,
    dl_between_variance,
    fit_cell,
    is_current,
    load_pooling_settings,
    pool_entries,
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
        # Morris (1983), r = 1: V = s²(1 - ((k - 1)/k) B) + 2/(k - 3) B² (y - μ)².
        variance = noise * (1 - (k - 1) / k * share) + (2 / (k - 3)) * share**2 * (y - mean) ** 2
        assert member.pooled_se == pytest.approx(math.sqrt(variance))
        low, high = member.interval()
        assert (low, high) == pytest.approx(
            (member.pooled_effect - Z * member.pooled_se, member.pooled_effect + Z * member.pooled_se)
        )
    furthest = fit.members[
        3
    ]  # y = 0.05: 0.0001 (1 - 0.16) + 1 (0.04) (0.0004) = 0.000084 + 0.000016 = 0.0001
    assert furthest.pooled_effect == pytest.approx(0.046)
    assert furthest.pooled_se == pytest.approx(0.01), "the furthest is as uncertain as its own figure"
    middle = fit.members[1]  # y = 0.03, the average: 0.000084
    assert middle.pooled_se == pytest.approx(math.sqrt(0.000084))
    assert all(member.pooled_se <= member.se * (1 + 1e-12) for member in fit.members)
    assert sum(member.pooled_se < member.se * (1 - 1e-9) for member in fit.members) == 3


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


def test_a_yes_no_campaign_with_a_registered_adjustment_is_pooled_on_the_adjusted_estimate() -> None:
    """Plan K M135 adjusts a yes/no outcome through the same field: the registered estimate is the primary one."""
    sim = population(4_000, 0.05, 0.02, seed=1364)
    report = measure_incrementality(sim.scores, sim.outcomes, **sim.measure_kwargs)
    adjusted = ConfidenceValue(value=0.018, ci_low=0.004, ci_high=0.032)
    registered = report.model_copy(update={"adjusted_interval": adjusted})
    assert registered.outcome_kind in (None, "binary")
    assert primary_estimate(registered) == ("adjusted_interval", adjusted)


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


# --- which cells are pooled, and when a file is current -----------------------------------------------------------
_AT = datetime(2026, 9, 1, tzinfo=UTC)
_CELL = PoolingCell(
    use_case_id="win-back-campaign",
    outcome_column="converted",
    outcome_kind="binary",
    holdout="run",
    synthetic=False,
)


def _entries(effects: list[float], cell: PoolingCell = _CELL) -> list[CellEntry]:
    return [
        CellEntry(
            campaign_id=f"cmp_{index}",
            cell=cell,
            customers=("runs", f"r_{index}"),
            rank=(_AT.isoformat(), _AT.isoformat(), f"cmp_{index}"),
            estimate_field="absolute_lift",
            effect=effect,
            se=0.01,
            report_computed_at=_AT,
        )
        for index, effect in enumerate(effects)
    ]


def test_three_comparable_campaigns_are_not_pooled_and_say_why() -> None:
    settings = PoolingSettings(enabled=True)
    three = pool_entries(_entries([0.01, 0.04, 0.02]), settings, _AT)
    assert {(item.status, item.code) for item in three} == {("not_pooled", POOL_TOO_FEW)}
    assert all(item.pooled_effect is None and item.reason and "fourth" in item.reason for item in three)
    four = pool_entries(_entries([0.01, 0.04, 0.02, 0.03]), settings, _AT)
    assert {item.status for item in four} == {"pooled"} and all(item.code is None for item in four)
    fewer = pool_entries(
        _entries([0.01, 0.04, 0.02, 0.03]), PoolingSettings(enabled=True, min_campaigns=5), _AT
    )
    assert all(item.code == POOL_TOO_FEW and "fourth" not in (item.reason or "") for item in fewer)


def test_campaigns_of_different_clients_are_never_pooled_together() -> None:
    entries = [
        *_entries([0.01, 0.04], _CELL.model_copy(update={"client_id": "cl_a"})),
        *(
            replace(entry, campaign_id=f"b_{entry.campaign_id}", customers=("b", *entry.customers))
            for entry in _entries([0.02, 0.03], _CELL.model_copy(update={"client_id": "cl_b"}))
        ),
    ]
    estimates = pool_entries(entries, PoolingSettings(enabled=True), _AT)
    assert len({item.cell_id for item in estimates}) == 2
    assert all(len(item.pooled_from) == 2 and item.status == "not_pooled" for item in estimates)
    same = pool_entries(
        [*_entries([0.01, 0.04, 0.02, 0.03], _CELL.model_copy(update={"client_id": "cl_a"}))],
        PoolingSettings(enabled=True),
        _AT,
    )
    assert {item.status for item in same} == {"pooled"}


def test_a_file_is_current_only_while_its_reports_and_settings_are_unchanged() -> None:
    settings = PoolingSettings(enabled=True)
    estimate = pool_entries(_entries([0.01, 0.04, 0.02, 0.03]), settings, _AT)[0]
    document = json.loads(estimate.model_dump_json())
    assert document["member_reports"] == {f"cmp_{index}": "2026-09-01T00:00:00Z" for index in range(4)}
    reports = {f"cmp_{index}": {"computed_at": "2026-09-01T00:00:00Z"} for index in range(4)}
    own = reports["cmp_0"]
    assert is_current(document, own, settings=settings, report_of=reports.get)
    assert not is_current(
        document, own, settings=PoolingSettings(enabled=True, min_campaigns=4)
    ), "settings changed"
    remeasured = {**reports, "cmp_2": {"computed_at": "2026-09-02T00:00:00Z"}}
    assert not is_current(document, own, settings=settings, report_of=remeasured.get), "a member re-measured"
    assert not is_current(
        document, own, settings=settings, report_of=lambda _: None
    ), "a member's report is gone"
    assert not is_current(document, {"computed_at": "2026-09-03T00:00:00Z"}), "its own report re-measured"


# --- settings and codes ------------------------------------------------------------------------------------------
def test_pooling_is_off_by_default_and_in_the_shipped_file(config_root: Path, tmp_path: Path) -> None:
    assert PoolingSettings().enabled is False
    assert load_pooling_settings(tmp_path) == PoolingSettings(), "no file: every default"
    shipped = load_pooling_settings(config_root)
    assert shipped.enabled is False and shipped.min_campaigns == MIN_CAMPAIGNS == 3


def test_a_settings_file_is_read_and_a_typo_is_refused_with_a_code(tmp_path: Path) -> None:
    (tmp_path / "measurement.yaml").write_text("pooling:\n  enabled: true\n  min_campaigns: 4\n")
    assert load_pooling_settings(tmp_path) == PoolingSettings(enabled=True, min_campaigns=4)
    (tmp_path / "measurement.yaml").write_text("# nothing set yet\n")
    assert load_pooling_settings(tmp_path) == PoolingSettings(), "an empty file: every default"
    refused = {
        "pooling:\n  enable: true\n": ("CONFIG_INVALID", "pooling.enable"),
        "pooling:\n  enabled: true\n  min_campaigns: 2\n": ("CONFIG_INVALID", "pooling.min_campaigns"),
        "pooling: [enabled]\n": ("CONFIG_INVALID", "pooling"),
        "- pooling\n": ("CONFIG_NOT_A_MAPPING", None),
        "pooling: {enabled: true\n": ("CONFIG_YAML_ERROR", None),
    }
    for text, (code, path) in refused.items():
        (tmp_path / "measurement.yaml").write_text(text)
        with pytest.raises(ConfigError) as caught:
            load_pooling_settings(tmp_path)
        assert caught.value.code == code, text
        assert path is None or caught.value.path == path
        assert "measurement.yaml" in caught.value.message


def test_the_codes() -> None:
    assert POOLING_CODES == {POOL_TOO_FEW, POOL_NO_SPREAD} == {"POOL_TOO_FEW", "POOL_NO_SPREAD"}
