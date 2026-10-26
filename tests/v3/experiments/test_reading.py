"""Reading: a planted winner is found, a planted null is not promoted, a loser stops the test early."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Decision, DecisionKind, Reward, stable_id
from autonomy.experiments import (
    HOLDOUT_BUCKET,
    EndKind,
    ExperimentBook,
    Outcomes,
    briefing_section,
    collect_outcomes,
    read,
    summary_line,
    tags_of,
)
from autonomy.experiments.model import tag_decision_version
from tests.v3.experiments.helpers import NOW, brief, spec


def outcomes(rng: np.random.Generator, means: dict[str, float], holdout: float, n: int = 300) -> Outcomes:
    """Per-customer profit around the given means; most customers earn nothing, a few a lot."""

    def draw(mean: float) -> npt.NDArray[np.float64]:
        buys = rng.random(n) < 0.1
        return np.where(buys, rng.normal(mean * 10.0, 2.0, n), 0.0)

    return Outcomes(arms={a: draw(m) for a, m in means.items()}, holdout=draw(holdout))


def started(**kw: object) -> tuple[ExperimentBook, str]:
    book = ExperimentBook()
    exp = book.start(spec("t", min_per_arm=100, prior_sd=1.0, **kw), brief(), NOW)  # type: ignore[arg-type]
    return book, exp.experiment_id


def test_a_planted_clear_winner_is_found() -> None:
    rng = np.random.default_rng(3)
    book, eid = started()
    review = book.review(
        eid, outcomes(rng, {"pct10": 0.2, "freeship": 1.2}, 0.0, n=600), NOW + timedelta(days=14)
    )
    assert review.ended
    assert review.experiment.end_kind is EndKind.WINNER
    assert review.experiment.winner == "freeship"
    arm = review.reading.arm("freeship")
    assert arm.vs_holdout_seq is not None and arm.vs_holdout_seq.low > 0
    assert arm.vs_reference_seq is not None and arm.vs_reference_seq.low > 0
    assert arm.vs_holdout is not None and arm.vs_holdout.low > 0  # the plain proof interval agrees


def test_a_planted_null_is_never_promoted() -> None:
    """Same profit in every arm and the holdout: run it 200 times, looking every week; no winner ever."""
    wins = 0
    for seed in range(200):
        rng = np.random.default_rng(seed)
        book, eid = started()
        for week in range(1, 5):
            review = book.review(
                eid, outcomes(rng, {"pct10": 0.5, "freeship": 0.5}, 0.5), NOW + timedelta(days=7 * week)
            )
            if review.ended:
                break
        wins += book.get(eid).end_kind is EndKind.WINNER
    assert wins == 0


def test_nothing_is_called_before_enough_customers_are_measured() -> None:
    rng = np.random.default_rng(4)
    book, eid = started()
    review = book.review(
        eid, outcomes(rng, {"pct10": 0.0, "freeship": 5.0}, 0.0, n=40), NOW + timedelta(days=3)
    )
    assert not review.reading.enough
    assert review.judgement.action == "continue"
    assert not review.ended


def test_time_up_without_a_winner_keeps_the_default() -> None:
    rng = np.random.default_rng(5)
    book, eid = started()
    review = book.review(
        eid, outcomes(rng, {"pct10": 0.5, "freeship": 0.5}, 0.5, n=200), NOW + timedelta(days=28)
    )
    assert review.experiment.end_kind is EndKind.NO_WINNER
    assert review.experiment.winner is None
    assert "default stays 10% off" in summary_line(review.experiment, review.reading)


def test_an_arm_that_loses_money_against_doing_nothing_stops_the_test() -> None:
    rng = np.random.default_rng(6)
    book, eid = started()
    # free shipping is the same as nothing; 10% off (the reference) loses money
    review = book.review(
        eid, outcomes(rng, {"pct10": -1.5, "freeship": 0.0}, 0.0, n=500), NOW + timedelta(days=10)
    )
    assert review.experiment.end_kind is EndKind.STOPPED_LOSING
    assert review.experiment.losing == ("pct10",)
    assert review.experiment.winner is None
    # the losing arm is today's default: a person is asked to look, not told that it stays
    line = summary_line(review.experiment, review.reading)
    assert "needs a person's review" in line and "default stays" not in line
    assert "a person should review it" in review.judgement.reason


def test_a_challenger_clearly_worse_than_the_default_stops_the_test() -> None:
    rng = np.random.default_rng(8)
    book, eid = started()
    review = book.review(
        eid, outcomes(rng, {"pct10": 1.5, "freeship": 0.3}, 0.0, n=500), NOW + timedelta(days=10)
    )
    assert review.experiment.end_kind is EndKind.STOPPED_LOSING
    assert review.experiment.losing == ("freeship",)


def test_stopping_for_a_win_can_be_left_to_the_end() -> None:
    rng = np.random.default_rng(3)
    book, eid = started(stop_for_win=False)
    out = outcomes(rng, {"pct10": 0.2, "freeship": 1.2}, 0.0, n=600)
    assert not book.review(eid, out, NOW + timedelta(days=10)).ended
    assert book.review(eid, out, NOW + timedelta(days=28)).experiment.winner == "freeship"


def test_an_ended_experiment_is_not_changed_by_another_look() -> None:
    rng = np.random.default_rng(3)
    book, eid = started()
    book.review(eid, outcomes(rng, {"pct10": 0.2, "freeship": 1.2}, 0.0, n=600), NOW + timedelta(days=14))
    before = book.get(eid)
    again = book.review(
        eid, outcomes(rng, {"pct10": 5.0, "freeship": -5.0}, 0.0, n=600), NOW + timedelta(days=20)
    )
    assert book.get(eid) == before
    assert again.experiment.winner == "freeship"


def test_the_summary_is_one_sentence_whose_numbers_are_in_the_briefing_section() -> None:
    rng = np.random.default_rng(3)
    book, eid = started()
    review = book.review(
        eid, outcomes(rng, {"pct10": 0.2, "freeship": 1.2}, 0.0, n=600), NOW + timedelta(days=14)
    )
    line = summary_line(review.experiment, review.reading, promoted=True)
    assert line.startswith("Free shipping beat 10% off for lapsed customers")
    assert line.endswith("It is now the default offer.")
    assert "\n" not in line
    section = briefing_section(review.experiment, review.reading, promoted=True)
    assert section.body == line
    gain = section.numbers["gain_vs_default"]
    assert f"${gain:,.2f}" in line
    assert section.numbers["customers_measured"] == 1200.0


def test_reading_from_the_decision_log_matches_the_tags() -> None:
    """collect_outcomes finds arms and the matched holdout in the decision log, by tag, from closed rewards."""
    book = ExperimentBook()
    exp = book.start(spec("t"), brief(), NOW)
    decisions: list[Decision] = []
    rewards: list[Reward] = []
    for i, bucket in enumerate(["pct10"] * 3 + ["freeship"] * 3 + [HOLDOUT_BUCKET] * 3 + [None] * 2):
        cid = f"c{i}"
        version = "agent-v1" if bucket is None else tag_decision_version("agent-v1", "t", bucket)
        d = Decision(
            decision_id=stable_id("dec", cid),
            customer_id=cid,
            made_at=NOW + timedelta(days=1),
            kind=DecisionKind.NOTHING,
            probability=1.0,
            reason="-",
            policy_version=version,
        )
        decisions.append(d)
        rewards.append(
            Reward(
                decision_id=d.decision_id,
                customer_id=cid,
                goal_id=None,
                window_end=NOW + timedelta(days=15),
                converted=True,
                net=float(i),
            )
        )
    late = rewards[0].model_copy(update={"window_end": NOW + timedelta(days=40), "net": 99.0})
    out = collect_outcomes(exp, decisions, [*rewards[1:], late], NOW + timedelta(days=20))
    assert sorted(out.arms["pct10"]) == [1.0, 2.0]  # c0's window had not closed
    assert sorted(out.arms["freeship"]) == [3.0, 4.0, 5.0]
    assert sorted(out.holdout) == [6.0, 7.0, 8.0]
    assert tags_of("agent-v1|exp:t:freeship|exp:u:holdout") == {"t": "freeship", "u": "holdout"}
    assert read(exp, out, NOW + timedelta(days=20)).holdout_n == 3
