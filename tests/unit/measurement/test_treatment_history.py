"""The treatment-history check and the sufficiency verdict (Plan J M93).

On a treatment column the user names, `engine.uplift.checks.treatment_history` says whether past
campaigns chose their customers at random, by a model, or cannot be told, and whether the history is
enough to learn who a campaign changes now ("uplift now") or only from the next cycle ("propensity +
random control first"). The verdict uses the same floors as `TREATMENT_ARM_TOO_SMALL`
(`uplift.min_arm_rows`, `uplift.min_arm_positives`) and must switch exactly at them.
"""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.contracts import Severity, check_code_table
from engine.pilot.plain import jargon_in
from engine.uplift.checks import TreatmentHistory, sufficiency_verdict, treatment_history
from tests.fixtures.make_uplift_data import make_uplift_data

PK = "customer_id"
TARGET = "reactivated_90d"


def use_case(**uplift: Any) -> UseCaseConfig:
    document = copy.deepcopy(load_use_case_document("win-back-campaign"))
    document["template"] = {"columns": []}
    document["uplift"] = {**(document.get("uplift") or {}), **uplift}
    return UseCaseConfig.model_validate(document)


def history(frame: pd.DataFrame, column: str = "treatment", **uplift: Any) -> TreatmentHistory:
    return treatment_history(
        frame, use_case(**uplift), treatment_column=column, primary_key=PK, target=TARGET, seed=0
    )


@pytest.fixture(scope="module")
def randomised() -> pd.DataFrame:
    return make_uplift_data(4000, seed=21).frame


@pytest.fixture(scope="module")
def model_selected() -> pd.DataFrame:
    """The randomised customers, re-contacted the way a propensity model would choose them: the 40%
    with the highest score of a linear model on their own data, with a little noise."""
    frame = make_uplift_data(4000, seed=21).frame.copy()
    rng = np.random.default_rng(5)
    score = (
        0.04 * frame["tenure_months"]
        + 0.3 * frame["visits_30d"]
        + 0.01 * frame["monthly_spend"]
        - 0.2 * frame["support_tickets_90d"]
        + rng.normal(scale=0.3, size=len(frame))
    )
    frame["contacted"] = (score >= score.quantile(0.6)).astype(int)
    return frame


def test_model_selected_treatment_is_reported_not_random(model_selected: pd.DataFrame) -> None:
    found = history(model_selected, "contacted")
    assert found.assignment == "model_selected"
    assert found.score is not None and found.score > found.threshold
    assert found.check is not None and found.check.code == "TREATMENT_HISTORY_NOT_RANDOM"
    assert found.check.severity is Severity.WARNING
    assert check_code_table("TREATMENT_HISTORY_NOT_RANDOM") == "plan_j"
    assert "was not random" in found.message
    assert found.verdict == "propensity_first"


def test_the_generators_rule_targeted_campaign_is_not_random_either() -> None:
    frame = make_uplift_data(4000, seed=21, targeted=True).frame
    assert history(frame).assignment == "model_selected"


def test_a_randomised_fixture_is_reported_random(randomised: pd.DataFrame) -> None:
    found = history(randomised, min_arm_rows=100, min_arm_positives=10)
    assert found.assignment == "random"
    assert found.score is not None and found.score <= found.threshold
    assert found.check is None
    assert "looks random" in found.message
    assert found.verdict == "uplift_now"


@pytest.mark.parametrize("column", ["no_such_column", "plan"])
def test_a_column_that_cannot_be_read_is_unknown_not_random(randomised: pd.DataFrame, column: str) -> None:
    found = history(randomised, column)
    assert found.assignment == "unknown"
    assert found.score is None and found.check is None
    assert found.verdict == "propensity_first"


def test_too_few_customers_in_a_group_is_unknown(randomised: pd.DataFrame) -> None:
    frame = randomised.copy()
    frame["treatment"] = 1
    frame.loc[frame.index[:10], "treatment"] = 0
    assert history(frame).assignment == "unknown"


def counts(frame: pd.DataFrame) -> tuple[int, int, int, int]:
    treated = frame["treatment"] == 1
    return (
        int(treated.sum()),
        int((~treated).sum()),
        int(frame.loc[treated, TARGET].sum()),
        int(frame.loc[~treated, TARGET].sum()),
    )


def test_the_verdict_switches_exactly_at_the_row_floor(randomised: pd.DataFrame) -> None:
    treated, control, _, _ = counts(randomised)
    smallest = min(treated, control)
    assert history(randomised, min_arm_rows=smallest, min_arm_positives=1).verdict == "uplift_now"
    assert history(randomised, min_arm_rows=smallest + 1, min_arm_positives=1).verdict == "propensity_first"


def test_the_verdict_switches_exactly_at_the_positives_floor(randomised: pd.DataFrame) -> None:
    _, _, treated_positives, control_positives = counts(randomised)
    fewest = min(treated_positives, control_positives)
    assert history(randomised, min_arm_rows=1, min_arm_positives=fewest).verdict == "uplift_now"
    late = history(randomised, min_arm_rows=1, min_arm_positives=fewest + 1)
    assert late.verdict == "propensity_first"
    assert f"at least {fewest + 1:,} needed" in late.verdict_message


@pytest.mark.parametrize(
    ("treated", "control", "treated_positives", "control_positives", "expected"),
    [
        (1000, 1000, 50, 50, "uplift_now"),
        (999, 1000, 50, 50, "propensity_first"),
        (1000, 999, 50, 50, "propensity_first"),
        (1000, 1000, 49, 50, "propensity_first"),
        (1000, 1000, 50, 49, "propensity_first"),
        (1000, 1000, None, 50, "propensity_first"),
    ],
)
def test_the_pure_verdict_switches_at_the_default_floors(
    treated: int, control: int, treated_positives: int | None, control_positives: int, expected: str
) -> None:
    config = use_case()
    assert (config.uplift.min_arm_rows, config.uplift.min_arm_positives) == (1000, 50)
    verdict = sufficiency_verdict(
        assignment="random",
        treated=treated,
        control=control,
        treated_positives=treated_positives,
        control_positives=control_positives,
        min_arm_rows=config.uplift.min_arm_rows,
        min_arm_positives=config.uplift.min_arm_positives,
    )
    assert verdict == expected


@pytest.mark.parametrize("assignment", ["model_selected", "unknown"])
def test_history_that_is_not_random_is_never_enough(assignment: str) -> None:
    verdict = sufficiency_verdict(
        assignment=assignment,  # type: ignore[arg-type]
        treated=10**6,
        control=10**6,
        treated_positives=10**5,
        control_positives=10**5,
        min_arm_rows=1000,
        min_arm_positives=50,
    )
    assert verdict == "propensity_first"


def test_every_sentence_is_plain(randomised: pd.DataFrame, model_selected: pd.DataFrame) -> None:
    for found in (
        history(randomised, min_arm_rows=1, min_arm_positives=1),
        history(randomised),
        history(model_selected, "contacted"),
        history(randomised, "no_such_column"),
        history(randomised, "plan"),
    ):
        texts = [found.message, found.verdict_message]
        if found.check is not None:
            texts += [found.check.message, found.check.suggestion]
        assert [text for text in texts if jargon_in(text)] == [], texts
