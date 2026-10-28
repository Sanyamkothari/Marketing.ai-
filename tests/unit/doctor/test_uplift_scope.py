"""Uplift goals in the Data Doctor: offered only for a campaign test with a random control group."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from api.routes.doctor import offers
from engine.config import load_use_case
from engine.doctor.diagnose import readiness
from engine.doctor.out_of_scope import out_of_scope
from engine.doctor.uplift_scope import check_treatment_balance, control_level, uplift_scope
from tests.unit.doctor.test_diagnose import loaded

LEVELS = ["No E-Mail", "Mens E-Mail", "Womens E-Mail"]


def campaign(rows: int = 6000, *, targeted: bool = False, groups: list[str] | None = None) -> pd.DataFrame:
    """A Hillstrom-like campaign file; `targeted` sends the e-mail to the big spenders instead of at random."""
    rng = np.random.default_rng(7)
    history = rng.gamma(2.0, 120.0, size=rows).round(2)
    arms = groups or LEVELS
    if targeted:
        rank = history.argsort().argsort() / rows
        segment = np.where(
            rank > 0.66, arms[-1], np.where(rank > 0.33, arms[-1 if len(arms) < 3 else 1], arms[0])
        )
    else:
        segment = rng.choice(arms, size=rows)
    return pd.DataFrame(
        {
            "customer_id": np.arange(1, rows + 1),
            "recency": rng.integers(1, 13, size=rows),
            "history": history,
            "zip_code": rng.choice(["Urban", "Rural", "Surburban"], size=rows),
            "channel": rng.choice(["Web", "Phone", "Multichannel"], size=rows),
            "segment": segment,
            "visit": rng.integers(0, 2, size=rows),
            "spend": rng.gamma(1.0, 5.0, size=rows).round(2),
            "conversion": (rng.random(rows) < 0.05).astype(int),
        }
    )


def hillstrom(config_root: Path):  # type: ignore[no-untyped-def]
    return load_use_case("hillstrom-email", config_root)


def test_a_random_assignment_is_balanced(config_root: Path) -> None:
    scope = uplift_scope(campaign(), "segment", hillstrom(config_root))
    assert scope.status == "ok"
    balance = scope.balance
    assert balance is not None and balance.balanced and not balance.flagged
    assert balance.control_group == "No E-Mail"
    assert set(balance.groups) == set(LEVELS)
    # the outcomes of the same weeks and the ID are not "before the campaign"
    assert balance.columns_tested == 4
    assert "randomly" in scope.message or "random" in scope.message


def test_a_targeted_assignment_is_not_random(config_root: Path) -> None:
    scope = uplift_scope(campaign(targeted=True), "segment", hillstrom(config_root))
    assert scope.status == "not_random"
    assert scope.balance is not None and not scope.balance.balanced
    assert scope.balance.flagged[0].column == "history"
    assert out_of_scope("UPLIFT_NOT_RANDOM") in scope.message
    assert "history" in scope.message


def test_a_file_with_one_group_has_no_control_group(config_root: Path) -> None:
    frame = campaign(groups=["Mens E-Mail", "Womens E-Mail"])
    scope = uplift_scope(frame, "segment", hillstrom(config_root))
    assert scope.status == "no_control"
    assert scope.balance is not None and scope.balance.control_group is None
    assert out_of_scope("UPLIFT_NOT_RANDOM") in scope.message


def test_a_small_control_group_is_too_small(config_root: Path) -> None:
    frame = campaign(2000)
    frame.loc[frame.index[200:], "segment"] = frame["segment"].replace("No E-Mail", "Mens E-Mail")
    assert uplift_scope(frame, "segment", hillstrom(config_root)).status == "arm_too_small"


def test_control_group_is_found_by_name_or_by_zero() -> None:
    assert control_level(["Mens E-Mail", "No E-Mail"]) == "No E-Mail"
    assert control_level(["treated", "control"]) == "control"
    assert control_level([0, 1]) == "0" and control_level([0.0, 1.0]) == "0"
    assert control_level(["Mens E-Mail", "Womens E-Mail"]) is None
    assert control_level(["B", "A"], ["A", "B"]) == "A"


def test_balance_uses_the_difference_and_its_chance(config_root: Path) -> None:
    config = hillstrom(config_root)
    # a tiny, noisy gap on a small file is chance; the same gap on a huge file is not
    rng = np.random.default_rng(1)
    small = pd.DataFrame({"t": ["c", "x"] * 100, "age": rng.normal(40, 10, 200)})
    small.loc[small.t == "x", "age"] += 2.5
    assert check_treatment_balance(small, "t", config, control="c").balanced
    big = pd.DataFrame({"t": ["c", "x"] * 20000, "age": rng.normal(40, 10, 40000)})
    big.loc[big.t == "x", "age"] += 2.5
    verdict = check_treatment_balance(big, "t", config, control="c")
    assert not verdict.balanced and verdict.flagged[0].column == "age"


def test_a_category_that_differs_by_group_is_flagged(config_root: Path) -> None:
    rng = np.random.default_rng(2)
    frame = pd.DataFrame({"t": rng.choice(["control", "mail"], 4000)})
    frame["zone"] = np.where(
        frame.t == "mail", rng.choice(["a", "b"], 4000, p=[0.8, 0.2]), rng.choice(["a", "b"], 4000)
    )
    verdict = check_treatment_balance(frame, "t", hillstrom(config_root), control="control")
    assert verdict.flagged and verdict.flagged[0].measure == "cramers_v"


def test_uplift_is_offered_only_for_a_file_with_the_treatment_column(config_root: Path) -> None:
    found = list(readiness([loaded(campaign(), "email.csv", config_root)], offers(config_root), config_root))
    mine = [v for v in found if v.use_case_id == "hillstrom-email"]
    assert len(mine) == 1 and mine[0].status in {"ready", "check"}
    assert mine[0].balance is not None and mine[0].balance.balanced
    without = campaign().drop(columns=["segment"])
    found = list(readiness([loaded(without, "email.csv", config_root)], offers(config_root), config_root))
    assert all(v.use_case_id != "hillstrom-email" for v in found)


def test_a_targeted_campaign_file_is_out_of_scope_with_the_sentence(config_root: Path) -> None:
    found = list(
        readiness(
            [loaded(campaign(targeted=True), "email.csv", config_root)], offers(config_root), config_root
        )
    )
    mine = next(v for v in found if v.use_case_id == "hillstrom-email")
    assert mine.status == "missing" and mine.reason_code == "NOT_RANDOM"
    assert mine.first_fix == out_of_scope("UPLIFT_NOT_RANDOM")
    assert mine.balance is not None and mine.balance.flagged
