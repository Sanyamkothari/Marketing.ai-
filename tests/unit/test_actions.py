"""The actions stage: bands, suppression precedence, and the run-seeded control group.

Plan section 6.3 `actions`. The band invariants are checked with hypothesis over the whole score
range; everything else is checked on small frames whose expected answer is written out by hand.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.config import UseCaseConfig, load_use_case, load_use_case_document
from engine.stages.actions import (
    ACTION_COLUMN,
    APPLIED_RULES_ATTR,
    BAND_COLUMN,
    CONTROL_ACTION,
    CONTROL_GROUP_COLUMN,
    SKIPPED_RULES_ATTR,
    SUPPRESSED_ACTION,
    SUPPRESSED_REASON_COLUMN,
    apply_actions,
    assign_bands,
    suppression_rules,
)

RUN_ID = "r_20260921_aaaaaaaa"
OTHER_RUN_ID = "r_20260921_bbbbbbbb"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def use_case(**patches: object) -> UseCaseConfig:
    """`targeted-advertisement` with dotted paths overridden, e.g. `actions__suppression=...`.

    The template is emptied first: it pins the shipped column names, and these tests need to name
    columns that only exist in their own frames (`_check_template` and the consent cross-check both
    stand down when `template.columns` is empty).
    """
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    document["template"] = {"columns": []}
    for dotted, value in patches.items():
        parts = dotted.split("__")
        node = document
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return UseCaseConfig.model_validate(document)


@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    return load_use_case("targeted-advertisement")


def no_suppression(**patches: object) -> UseCaseConfig:
    """A configuration with every suppression rule off, so a test can turn on exactly one."""
    defaults: dict[str, object] = {
        "actions__suppression__suppress_opted_out": False,
        "actions__suppression__suppress_recently_contacted": False,
        "actions__control_group_fraction": 0.0,
    }
    return use_case(**{**defaults, **patches})


def scored(scores: list[float], **columns: object) -> pd.DataFrame:
    """A scored frame: `customer_id`, `propensity` and whatever extra columns a test needs."""
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{index:04d}" for index in range(len(scores))],
            "propensity": scores,
        }
    )
    for name, values in columns.items():
        frame[name] = values
    return frame


def band_rank(config: UseCaseConfig, name: str) -> int:
    """Position of a band in the configured (descending) order; 0 is the highest band."""
    return [band.name for band in config.actions.bands].index(name)


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------
def test_the_shipped_bands_are_the_ones_these_tests_assume(config: UseCaseConfig) -> None:
    assert [(band.name, band.min_score) for band in config.actions.bands] == [
        ("High", 0.80),
        ("Medium", 0.50),
        ("Low", 0.00),
    ]


def test_a_score_equal_to_a_min_score_belongs_to_that_band(config: UseCaseConfig) -> None:
    scores = pd.Series([0.80, 0.50, 0.00])
    assert list(assign_bands(scores, config)) == ["High", "Medium", "Low"]


def test_a_score_just_below_a_boundary_stays_in_the_lower_band(config: UseCaseConfig) -> None:
    scores = pd.Series([0.7999999, 0.4999999])
    assert list(assign_bands(scores, config)) == ["Medium", "Low"]


def test_scores_outside_zero_to_one_are_clamped_into_the_nearest_band(config: UseCaseConfig) -> None:
    scores = pd.Series([-0.25, -1e-9, 1.0, 1.5, 42.0])
    assert list(assign_bands(scores, config)) == ["Low", "Low", "High", "High", "High"]


def test_the_returned_series_is_aligned_and_named(config: UseCaseConfig) -> None:
    scores = pd.Series([0.9, 0.1], index=[7, 3])
    bands = assign_bands(scores, config)
    assert bands.name == BAND_COLUMN
    assert list(bands.index) == [7, 3]


@pytest.mark.parametrize("bad", [float("nan"), None, "not a number", ""])
def test_a_missing_or_unparseable_score_is_refused(config: UseCaseConfig, bad: object) -> None:
    with pytest.raises(ValueError, match="no usable numeric score"):
        assign_bands(pd.Series([0.9, bad]), config)


def test_the_refusal_message_never_carries_a_data_value(config: UseCaseConfig) -> None:
    with pytest.raises(ValueError) as error:
        assign_bands(pd.Series([0.9, float("nan")]), config)
    assert "0.9" not in str(error.value)


@given(score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
def test_every_score_lands_in_exactly_one_band(score: float) -> None:
    config = load_use_case("targeted-advertisement")
    band = assign_bands(pd.Series([score]), config).iat[0]
    matching = [b.name for b in config.actions.bands if score >= b.min_score]
    assert matching, "the floor band at 0.0 must catch every score"
    assert band == matching[0] == config.actions.band_for(score).name


@given(
    low=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    high=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_a_higher_score_never_lands_in_a_lower_band(low: float, high: float) -> None:
    config = load_use_case("targeted-advertisement")
    first, second = sorted((low, high))
    bands = assign_bands(pd.Series([first, second]), config)
    assert band_rank(config, str(bands.iat[1])) <= band_rank(config, str(bands.iat[0]))


# ---------------------------------------------------------------------------
# Suppression, rule by rule
# ---------------------------------------------------------------------------
def test_opted_out_rows_are_suppressed() -> None:
    config = no_suppression(actions__suppression__suppress_opted_out=True)
    frame = scored([0.9, 0.9, 0.9], marketing_opt_in=[True, False, None])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [None, "opted_out", "opted_out"]
    assert list(result[ACTION_COLUMN]) == ["Serve ad", SUPPRESSED_ACTION, SUPPRESSED_ACTION]


@pytest.mark.parametrize(
    ("value", "suppressed"),
    [
        (True, False),
        (False, True),
        (1, False),
        (0, True),
        ("true", False),
        ("TRUE", False),
        (" yes ", False),
        ("Y", False),
        ("1", False),
        ("false", True),
        ("no", True),
        ("0", True),
        ("", True),
        (None, True),
    ],
)
def test_opt_in_truthiness(value: object, suppressed: bool) -> None:
    config = no_suppression(actions__suppression__suppress_opted_out=True)
    frame = scored([0.9], marketing_opt_in=[value])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert (result[SUPPRESSED_REASON_COLUMN].iat[0] == "opted_out") is suppressed


def test_recently_contacted_rows_are_suppressed_and_the_window_is_exclusive() -> None:
    config = no_suppression(
        actions__suppression__suppress_recently_contacted=True,
        actions__suppression__recently_contacted_days=14,
    )
    frame = scored(
        [0.9, 0.9, 0.9, 0.9],
        last_contacted_at=[
            NOW - timedelta(days=13),
            NOW - timedelta(days=14),
            NOW - timedelta(days=15),
            NOW + timedelta(days=1),
        ],
    )
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [
        "recently_contacted",
        None,
        None,
        "recently_contacted",
    ]


def test_an_unknown_contact_date_does_not_prove_a_recent_contact() -> None:
    config = no_suppression(actions__suppression__suppress_recently_contacted=True)
    frame = scored([0.9, 0.9, 0.9], last_contacted_at=[None, "", "not a date"])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [None, None, None]


def test_naive_contact_dates_are_read_as_utc() -> None:
    config = no_suppression(actions__suppression__suppress_recently_contacted=True)
    frame = scored([0.9, 0.9], last_contacted_at=["2026-09-20", "2026-01-01"])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert list(result[SUPPRESSED_REASON_COLUMN]) == ["recently_contacted", None]


def test_consent_false_rows_are_suppressed() -> None:
    config = no_suppression(governance__consent_column="consent_given")
    frame = scored([0.9, 0.9], consent_given=[True, False])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [None, "consent_false"]


# ---------------------------------------------------------------------------
# Suppression precedence
# ---------------------------------------------------------------------------
def all_rules() -> UseCaseConfig:
    return use_case(
        governance__consent_column="consent_given",
        actions__suppression__suppress_opted_out=True,
        actions__suppression__suppress_recently_contacted=True,
        actions__control_group_fraction=0.0,
    )


def test_the_configured_rules_are_listed_in_precedence_order() -> None:
    assert suppression_rules(all_rules()) == (
        ("consent_false", "consent_given"),
        ("opted_out", "marketing_opt_in"),
        ("recently_contacted", "last_contacted_at"),
    )


def test_a_rule_whose_column_is_null_or_switched_off_is_not_a_rule() -> None:
    config = use_case(
        governance__consent_column=None,
        actions__suppression__suppress_opted_out=False,
        actions__suppression__recently_contacted_column=None,
    )
    assert suppression_rules(config) == ()


def test_consent_outranks_opt_out_which_outranks_recency() -> None:
    config = all_rules()
    recent = NOW - timedelta(days=1)
    old = NOW - timedelta(days=400)
    frame = scored(
        [0.9, 0.9, 0.9, 0.9],
        consent_given=[False, True, True, True],
        marketing_opt_in=[False, False, True, True],
        last_contacted_at=[recent, recent, recent, old],
    )
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [
        "consent_false",  # all three rules broken at once
        "opted_out",  # opted out and recently contacted
        "recently_contacted",
        None,
    ]


def test_a_suppressed_row_keeps_its_score_and_its_band() -> None:
    config = all_rules()
    frame = scored(
        [0.91, 0.64, 0.38],
        consent_given=[False, False, False],
        marketing_opt_in=[True, True, True],
        last_contacted_at=[None, None, None],
    )
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert list(result[ACTION_COLUMN]) == [SUPPRESSED_ACTION] * 3
    assert list(result["propensity"]) == [0.91, 0.64, 0.38]
    assert list(result[BAND_COLUMN]) == ["High", "Medium", "Low"]


def test_every_configured_rule_is_recorded_as_applied() -> None:
    config = all_rules()
    frame = scored([0.9], consent_given=[True], marketing_opt_in=[True], last_contacted_at=[None])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert result.attrs[APPLIED_RULES_ATTR] == ("consent_false", "opted_out", "recently_contacted")
    assert result.attrs[SKIPPED_RULES_ATTR] == ()


# ---------------------------------------------------------------------------
# A suppression column the scoring file does not have
# ---------------------------------------------------------------------------
def test_a_missing_suppression_column_skips_that_rule_and_says_so() -> None:
    config = all_rules()
    frame = scored([0.9, 0.9], marketing_opt_in=[True, False])  # no consent, no contact column
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert result.attrs[APPLIED_RULES_ATTR] == ("opted_out",)
    assert result.attrs[SKIPPED_RULES_ATTR] == (
        ("consent_false", "consent_given"),
        ("recently_contacted", "last_contacted_at"),
    )
    assert list(result[SUPPRESSED_REASON_COLUMN]) == [None, "opted_out"]


def test_a_rule_that_ran_and_matched_nothing_is_not_the_same_as_a_skipped_one() -> None:
    config = all_rules()
    ran = apply_actions(
        scored([0.9], consent_given=[True], marketing_opt_in=[True], last_contacted_at=[None]),
        config,
        run_id=RUN_ID,
        primary_key="customer_id",
        now=NOW,
    )
    skipped = apply_actions(
        scored([0.9], marketing_opt_in=[True], last_contacted_at=[None]),
        config,
        run_id=RUN_ID,
        primary_key="customer_id",
        now=NOW,
    )
    assert "consent_false" in ran.attrs[APPLIED_RULES_ATTR]
    assert "consent_false" not in skipped.attrs[APPLIED_RULES_ATTR]
    assert [code for code, _ in skipped.attrs[SKIPPED_RULES_ATTR]] == ["consent_false"]


def test_every_row_is_not_silently_eligible_when_no_rule_can_run() -> None:
    config = all_rules()
    frame = scored([0.9, 0.2])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id", now=NOW)
    assert result.attrs[APPLIED_RULES_ATTR] == ()
    assert len(result.attrs[SKIPPED_RULES_ATTR]) == 3
    assert not result[SUPPRESSED_REASON_COLUMN].notna().any()


# ---------------------------------------------------------------------------
# The control group
# ---------------------------------------------------------------------------
def population(rows: int = 200, *, opt_out_every: int = 10) -> pd.DataFrame:
    return scored(
        [(index % 100) / 100 for index in range(rows)],
        marketing_opt_in=[index % opt_out_every != 0 for index in range(rows)],
    )


def holdout(result: pd.DataFrame) -> set[str]:
    return set(result.loc[result[CONTROL_GROUP_COLUMN], "customer_id"])


def control_config(fraction: float = 0.10) -> UseCaseConfig:
    return use_case(
        actions__control_group_fraction=fraction,
        actions__suppression__suppress_opted_out=True,
        actions__suppression__suppress_recently_contacted=False,
    )


def test_the_same_run_id_reproduces_the_same_holdout() -> None:
    config = control_config()
    frame = population()
    first = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    second = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert holdout(first) == holdout(second)
    assert first[CONTROL_GROUP_COLUMN].tolist() == second[CONTROL_GROUP_COLUMN].tolist()


def test_a_different_run_id_draws_a_different_holdout() -> None:
    config = control_config()
    frame = population()
    first = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    other = apply_actions(frame, config, run_id=OTHER_RUN_ID, primary_key="customer_id")
    assert holdout(first) != holdout(other)


def test_the_holdout_is_the_fraction_of_eligible_rows_rounded_half_up() -> None:
    config = control_config()
    frame = population(rows=200, opt_out_every=10)  # 20 rows opted out, 180 eligible
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    eligible = int((result[SUPPRESSED_REASON_COLUMN].isna()).sum())
    assert eligible == 180
    assert len(holdout(result)) == 18  # 10% of the eligible rows, not of all 200


def test_a_fraction_that_rounds_to_half_a_row_rounds_up() -> None:
    config = control_config(fraction=0.5)
    frame = scored([0.9, 0.9, 0.9], marketing_opt_in=[True, True, True])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert len(holdout(result)) == 2  # 3 * 0.5 = 1.5 -> 2


def test_a_fraction_that_rounds_down_to_zero_yields_no_control_group() -> None:
    config = control_config(fraction=0.10)
    frame = scored([0.9, 0.9, 0.9], marketing_opt_in=[True, True, True])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert holdout(result) == set()
    assert CONTROL_ACTION not in set(result[ACTION_COLUMN])


def test_a_zero_fraction_yields_no_control_group() -> None:
    config = control_config(fraction=0.0)
    result = apply_actions(population(), config, run_id=RUN_ID, primary_key="customer_id")
    assert holdout(result) == set()


def test_only_eligible_rows_are_held_out() -> None:
    config = control_config()
    result = apply_actions(population(), config, run_id=RUN_ID, primary_key="customer_id")
    held_out = result[result[CONTROL_GROUP_COLUMN]]
    assert not held_out[SUPPRESSED_REASON_COLUMN].notna().any()
    assert set(held_out[ACTION_COLUMN]) == {CONTROL_ACTION}


def test_a_suppressed_row_is_never_relabelled_as_a_control() -> None:
    config = control_config(fraction=0.50)
    result = apply_actions(population(), config, run_id=RUN_ID, primary_key="customer_id")
    suppressed = result[result[SUPPRESSED_REASON_COLUMN].notna()]
    assert set(suppressed[ACTION_COLUMN]) == {SUPPRESSED_ACTION}
    assert not suppressed[CONTROL_GROUP_COLUMN].any()


def test_the_holdout_does_not_move_when_the_input_rows_are_reordered() -> None:
    config = control_config()
    frame = population()
    shuffled = frame.iloc[::-1].reset_index(drop=True)
    straight = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    reversed_order = apply_actions(shuffled, config, run_id=RUN_ID, primary_key="customer_id")
    assert holdout(straight) == holdout(reversed_order)


def test_the_holdout_does_not_move_when_only_some_rows_are_reordered() -> None:
    config = control_config()
    frame = population()
    rotated = pd.concat([frame.iloc[50:], frame.iloc[:50]], ignore_index=True)
    assert holdout(apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")) == holdout(
        apply_actions(rotated, config, run_id=RUN_ID, primary_key="customer_id")
    )


# ---------------------------------------------------------------------------
# The frame apply_actions returns
# ---------------------------------------------------------------------------
def test_every_row_gets_exactly_one_action() -> None:
    config = control_config()
    result = apply_actions(population(), config, run_id=RUN_ID, primary_key="customer_id")
    band_actions = {band.action for band in config.actions.bands}
    assert set(result[ACTION_COLUMN]) <= band_actions | {SUPPRESSED_ACTION, CONTROL_ACTION}
    assert not result[ACTION_COLUMN].isna().any()


def test_an_eligible_row_gets_the_action_configured_for_its_band() -> None:
    config = no_suppression()
    frame = scored([0.91, 0.64, 0.38])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert list(result[ACTION_COLUMN]) == ["Serve ad", "Retarget", "Suppress"]


def test_the_source_columns_survive_so_the_kpi_can_still_sum_one() -> None:
    config = no_suppression()
    frame = scored([0.9], tenure_months=[36])
    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    assert result["tenure_months"].iat[0] == 36
    assert result.index.equals(frame.index)


def test_the_input_frame_is_not_modified() -> None:
    config = control_config()
    frame = population(rows=20)
    before = frame.copy()
    apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")
    pd.testing.assert_frame_equal(frame, before)
    assert BAND_COLUMN not in frame.columns


def test_a_frame_without_the_key_or_the_score_column_is_refused(config: UseCaseConfig) -> None:
    with pytest.raises(ValueError, match="missing 'customer_id', 'propensity'"):
        apply_actions(pd.DataFrame({"other": [1]}), config, run_id=RUN_ID, primary_key="customer_id")


def test_the_control_group_column_is_boolean() -> None:
    result = apply_actions(population(), control_config(), run_id=RUN_ID, primary_key="customer_id")
    assert result[CONTROL_GROUP_COLUMN].dtype == bool
