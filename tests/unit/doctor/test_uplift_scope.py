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
    (listed,) = [v for v in found if v.use_case_id == "hillstrom-email"]  # listed as needing it, never hidden
    assert listed.status == "missing" and listed.reason_code == "NO_TEST_COLUMN" and listed.balance is None


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


def _hillstrom_verdicts(frame: pd.DataFrame, config_root: Path) -> list:  # type: ignore[type-arg]
    found = list(readiness([loaded(frame, "email.csv", config_root)], offers(config_root), config_root))
    return [v for v in found if v.use_case_id == "hillstrom-email"]


def test_a_customer_segment_that_shares_the_name_is_not_a_campaign_test(config_root: Path) -> None:
    frame = campaign().drop(columns=["conversion"])
    frame["segment"] = np.random.default_rng(3).choice(["Gold", "Silver", "Bronze"], size=len(frame))
    assert [v.reason_code for v in _hillstrom_verdicts(frame, config_root)] == ["NO_TEST_COLUMN"]
    with_outcome = campaign()
    with_outcome["segment"] = frame["segment"]
    # no control group by name: not read as an experiment, the goal waits for the person to name the test column
    assert [v.reason_code for v in _hillstrom_verdicts(with_outcome, config_root)] == ["NO_TEST_COLUMN"]


def test_a_campaign_test_with_another_column_name_is_offered(config_root: Path) -> None:
    for name in ("treatment", "group", "arm"):
        frame = campaign(groups=["control", "mail"]).rename(columns={"segment": name})
        mine = _hillstrom_verdicts(frame, config_root)
        assert len(mine) == 1, name
        assert mine[0].balance is not None and mine[0].balance.control_group == "control", name
        assert mine[0].balance.balanced, name


def test_a_column_named_like_a_treatment_without_the_outcome_is_not_offered(config_root: Path) -> None:
    frame = (
        campaign(groups=["control", "mail"]).rename(columns={"segment": "group"}).drop(columns=["conversion"])
    )
    assert [v.reason_code for v in _hillstrom_verdicts(frame, config_root)] == ["NO_TEST_COLUMN"]


def test_an_outcome_under_another_name_is_not_tested_for_balance(config_root: Path) -> None:
    frame = campaign().rename(columns={"visit": "site_visit"})
    frame["site_visit"] = np.where(
        frame["segment"] == "No E-Mail", 0, frame["site_visit"]
    )  # the campaign works
    scope = uplift_scope(frame, "segment", hillstrom(config_root))
    assert scope.status == "ok", scope.message
    assert scope.balance is not None and "site_visit" not in {c.column for c in scope.balance.flagged}


def test_a_one_group_campaign_file_is_missing_not_random_at_readiness(config_root: Path) -> None:
    one = campaign(groups=["Mens E-Mail"])
    mine = _hillstrom_verdicts(one, config_root)
    if mine and mine[0].reason_code == "NO_TEST_COLUMN":
        return  # no column reads as a test at all: listed as needing one
    (verdict,) = mine
    assert verdict.status == "missing" and verdict.reason_code == "NOT_RANDOM"
    assert verdict.first_fix == out_of_scope("UPLIFT_NOT_RANDOM")


def test_the_facts_list_the_candidate_columns_without_their_group_names(config_root: Path) -> None:
    from engine.doctor.tools_facts import campaign_columns

    frame = campaign(groups=["A", "base"]).rename(columns={"segment": "email_sent"})
    facts = campaign_columns("u1", frame, hillstrom(config_root))
    assert facts.outcome_in_file
    sent = next(c for c in facts.columns if c.column == "email_sent")
    assert [lv.level for lv in sent.levels] == ["L1", "L2"] and sum(lv.rows for lv in sent.levels) == len(
        frame
    )
    assert "base" not in facts.model_dump_json() and '"A"' not in facts.model_dump_json()


def test_a_test_column_the_person_confirmed_is_read_as_they_said(config_root: Path) -> None:
    from datetime import UTC, datetime

    from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
    from engine.doctor.case import CaseItem, payload_digest
    from engine.doctor.tools import TEST_COLUMN_KIND, confirmed_tests
    from engine.doctor.tools_facts import level_labels

    frame = campaign(groups=["A", "base"]).rename(columns={"segment": "email_sent"})
    files = [loaded(frame, "email.csv", config_root)]
    control = f"L{level_labels(frame['email_sent']).index('base') + 1}"
    payload = {"upload_id": files[0].upload_id, "column": "email_sent", "control_level": control}

    def item(state: ProposalState) -> CaseItem:
        return CaseItem(
            item_id="treatment_column.x",
            kind=TEST_COLUMN_KIND,
            title="t",
            source=ProposalSource.AI,
            confidence=AgentConfidence.CHECK,
            state=state,
            upload_id=files[0].upload_id,
            payload=payload,
            payload_digest=payload_digest(payload),
            proposed_at=datetime(2026, 10, 1, tzinfo=UTC),
        )

    waiting = confirmed_tests(offers(config_root), files, [item(ProposalState.PENDING)])
    found = [v for v in readiness(files, waiting, config_root) if v.use_case_id == "hillstrom-email"]
    assert [v.reason_code for v in found] == ["NO_TEST_COLUMN"]  # a proposal waits for the person
    agreed = confirmed_tests(offers(config_root), files, [item(ProposalState.ACCEPTED)])
    (verdict,) = [v for v in readiness(files, agreed, config_root) if v.use_case_id == "hillstrom-email"]
    assert verdict.status in {"ready", "check"} and verdict.balance is not None
    assert verdict.balance.control_group == "base" and verdict.balance.balanced
