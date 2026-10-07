"""`engine.measurement.campaign`: the assignment a campaign is measured on, and the two campaign stores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.measurement.campaign import (
    ARM_HOLDOUT,
    ARM_SUPPRESSED,
    ARM_TREATED,
    INTENDED_COLUMN,
    AssignmentCounts,
    Campaign,
    CampaignKind,
    CampaignStatus,
    CampaignStore,
    InMemoryCampaignStore,
    SqlCampaignStore,
    as_scores_frame,
    assignment_counts,
    build_assignment,
    campaign_key,
    new_campaign_id,
)
from engine.platform_db import sqlite_engine

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def propensity_scores() -> pd.DataFrame:
    """Eight customers: two suppressed, three held back, bands High/Low; ids as numbers, as a CSV reads them."""
    return pd.DataFrame(
        {
            "customer_id": [101, 102, 103, 104, 105, 106, 107, 108],
            "band": ["High", "High", "High", "Low", "Low", "High", "Low", "Low"],
            "suppressed_reason": [None, None, "opted_out", None, None, None, "consent_false", None],
            "control_group": [False, True, False, False, True, False, False, True],
        }
    )


def uplift_scores() -> pd.DataFrame:
    frame = propensity_scores()
    frame["segment"] = ["persuadable", "persuadable", "sure_thing", "lost_cause"] * 2
    frame["intended_treatment"] = [True, True, False, False, False, True, False, False]
    return frame


def test_a_propensity_assignment_has_one_row_per_customer_in_three_arms() -> None:
    assignment = build_assignment(propensity_scores(), primary_key="customer_id")
    assert list(assignment.columns) == ["customer_id", "arm", INTENDED_COLUMN, "band"]
    assert assignment["customer_id"].tolist() == [
        101,
        102,
        103,
        104,
        105,
        106,
        107,
        108,
    ], "the key's own dtype"
    assert assignment["arm"].tolist() == [
        ARM_TREATED,
        ARM_HOLDOUT,
        ARM_SUPPRESSED,
        ARM_TREATED,
        ARM_HOLDOUT,
        ARM_TREATED,
        ARM_SUPPRESSED,
        ARM_HOLDOUT,
    ]
    # intent to treat: every eligible customer, never a suppressed one
    assert assignment[INTENDED_COLUMN].tolist() == [True, True, False, True, True, True, False, True]
    assert assignment_counts(assignment) == AssignmentCounts(
        rows=8, suppressed=2, treated=3, holdout=3, intended=6, intended_treated=3, intended_holdout=3
    )


def test_treat_bands_narrow_the_population_on_both_sides() -> None:
    assignment = build_assignment(propensity_scores(), primary_key="customer_id", bands=["High"])
    assert assignment[INTENDED_COLUMN].tolist() == [True, True, False, False, False, True, False, False]
    counts = assignment_counts(assignment)
    assert (counts.intended_treated, counts.intended_holdout) == (2, 1)


def test_an_uplift_assignment_is_its_intended_set_and_keeps_the_segment() -> None:
    assignment = build_assignment(uplift_scores(), primary_key="customer_id")
    assert assignment[INTENDED_COLUMN].tolist() == [True, True, False, False, False, True, False, False]
    assert assignment["segment"].tolist()[:2] == ["persuadable", "persuadable"]
    with pytest.raises(ValueError, match="bands do not apply"):
        build_assignment(uplift_scores(), primary_key="customer_id", bands=["High"])


def test_an_explore_flag_is_carried_when_the_run_has_one() -> None:
    scores = propensity_scores()
    scores["explore"] = [False, False, False, True, False, False, False, False]
    assignment = build_assignment(scores, primary_key="customer_id")
    assert assignment["explore"].tolist() == scores["explore"].tolist()
    assert assignment_counts(assignment).explore == 1


def test_a_repeated_key_or_a_missing_column_is_refused() -> None:
    scores = propensity_scores()
    with pytest.raises(ValueError, match="repeats 1 primary key"):
        build_assignment(pd.concat([scores, scores.head(1)]), primary_key="customer_id")
    with pytest.raises(ValueError, match="control_group"):
        build_assignment(scores.drop(columns=["control_group"]), primary_key="customer_id")


def test_a_composite_key_keeps_every_key_column() -> None:
    scores = propensity_scores()
    scores["snapshot"] = ["2026-01-31"] * 8
    assignment = build_assignment(scores, primary_key=["customer_id", "snapshot"])
    assert list(assignment.columns[:2]) == ["customer_id", "snapshot"]


def test_an_assignment_reads_back_as_the_scores_it_came_from() -> None:
    scores = propensity_scores()
    translated = as_scores_frame(build_assignment(scores, primary_key="customer_id"))
    assert translated["control_group"].tolist() == [False, True, False, False, True, False, False, True]
    suppressed = translated["suppressed_reason"].notna().tolist()
    assert suppressed == scores["suppressed_reason"].notna().tolist()
    assert as_scores_frame(scores) is scores, "a run's scores pass through untouched"


def test_ids_and_keys() -> None:
    assert new_campaign_id(NOW).startswith("c_20261007_") and len(new_campaign_id(NOW)) == 19
    assert campaign_key("c_1", "assignment.parquet") == "campaigns/c_1/assignment.parquet"


# ---------------------------------------------------------------------------
# The stores
# ---------------------------------------------------------------------------
def campaign(
    campaign_id: str, *, run_id: str = "r_1", use_case: str = "win-back-campaign", minutes: int = 0
) -> Campaign:
    counts = AssignmentCounts(
        rows=8, suppressed=2, treated=3, holdout=3, intended=6, intended_treated=3, intended_holdout=3
    )
    return Campaign(
        campaign_id=campaign_id,
        kind=CampaignKind.SCORED,
        name="Win-back, sent 1 May 2026",
        use_case_id=use_case,
        run_ids=(run_id,),
        primary_key="customer_id",
        treatment_start=NOW,
        treatment_start_source="run_finished",
        outcome_window_days=90,
        population="eligible",
        causal=True,
        causal_basis="engine_random",
        counts=counts,
        status=CampaignStatus.LIVE,
        created_at=NOW + timedelta(minutes=minutes),
        created_by="u_1",
    )


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> CampaignStore:
    if request.param == "memory":
        return InMemoryCampaignStore()
    return SqlCampaignStore(sqlite_engine(tmp_path / "platform.db"))


def test_a_store_creates_reads_lists_and_saves(store: CampaignStore) -> None:
    assert isinstance(store, CampaignStore)
    first = store.create(campaign("c_a", minutes=0))
    second = store.create(campaign("c_b", run_id="r_2", use_case="telco-churn", minutes=5))
    assert store.get("c_a") == first and store.get("c_missing") is None
    assert [c.campaign_id for c in store.list()] == ["c_b", "c_a"], "newest first"
    assert [c.campaign_id for c in store.list(run_id="r_1")] == ["c_a"]
    assert [c.campaign_id for c in store.list(use_case_id="telco-churn")] == ["c_b"]
    assert len(store.list(limit=1)) == 1
    measured = second.model_copy(update={"status": CampaignStatus.MEASURED, "test_plan_hash": "ab" * 32})
    store.save(measured)
    assert store.get("c_b") == measured
    with pytest.raises(ValueError, match="already exists"):
        store.create(campaign("c_a"))
    with pytest.raises(KeyError):
        store.save(campaign("c_never_created"))


def test_the_sql_store_creates_its_table_once_and_survives_a_second_open(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / "platform.db")
    SqlCampaignStore(engine).create(campaign("c_a"))
    assert SqlCampaignStore(engine).get("c_a") == campaign("c_a")
