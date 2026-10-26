"""The book: limits, pre-registration, promotion and undo, and carrying the book as data."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from autonomy.contracts import NO_OFFER, OfferOption
from autonomy.contracts.goals import OfferKind
from autonomy.experiments import (
    EndKind,
    ExperimentBook,
    ExperimentRefused,
    Outcomes,
    PromotionRefused,
)
from autonomy.scientist.questions import QuestionKind
from tests.v3.experiments.helpers import NOW, TEN_OFF, brief, spec


def test_a_goal_may_run_two_experiments_and_the_third_is_refused() -> None:
    book = ExperimentBook()
    book.start(spec("a", share=0.03), brief(), NOW)
    book.start(spec("b", share=0.03), brief(), NOW)
    with pytest.raises(ExperimentRefused, match="already has 2 experiments"):
        book.start(spec("c", share=0.03), brief(), NOW)
    assert [e.experiment_id for e in book.running()] == ["a", "b"]
    assert book.log[-1].kind == "refused"


def test_another_goal_has_its_own_quota_but_the_agent_group_cap_is_shared() -> None:
    book = ExperimentBook()
    book.start(spec("a", share=0.04), brief(), NOW)
    book.start(spec("b", goal_id="goal_other", share=0.04), brief("goal_other"), NOW)
    with pytest.raises(ExperimentRefused, match="limit is 10%"):
        book.start(spec("c", goal_id="goal_third", share=0.04), brief("goal_third"), NOW)


def test_ten_percent_in_total_is_allowed_and_a_share_above_it_cannot_be_built() -> None:
    book = ExperimentBook()
    book.start(spec("a", share=0.05), brief(), NOW)
    book.start(spec("b", share=0.05), brief(), NOW)  # exactly 10% together
    with pytest.raises(ValueError, match=r"less than or equal to 0\.1"):
        spec("c", share=0.11)


def test_an_ended_experiment_frees_its_place() -> None:
    book = ExperimentBook()
    book.start(spec("a", share=0.05), brief(), NOW)
    book.start(spec("b", share=0.05), brief(), NOW)
    book.stop("a", NOW + timedelta(days=3), "pressed stop")
    book.start(spec("c", share=0.05), brief(), NOW + timedelta(days=3))
    assert [e.experiment_id for e in book.running()] == ["b", "c"]
    assert book.get("a").end_kind is EndKind.STOPPED_MANUALLY


def test_a_book_may_be_stricter_than_the_plan_but_not_looser() -> None:
    ExperimentBook(max_per_goal=1, max_total_share=0.05)
    with pytest.raises(ValueError, match="never looser"):
        ExperimentBook(max_per_goal=3)
    with pytest.raises(ValueError, match="never looser"):
        ExperimentBook(max_total_share=0.2)


def test_the_rules_are_registered_before_anything_runs() -> None:
    book = ExperimentBook()
    exp = book.start(spec(), brief(), NOW)
    assert exp.registered_at == NOW
    assert exp.first_assignment_at is None
    assert exp.digest_matches()
    registered, started = book.log[0], book.log[1]
    assert (registered.kind, started.kind) == ("registered", "started")
    assert registered.seq < started.seq
    assert exp.digest in registered.detail
    assert "Pick rule" in exp.rules and "Stopping rule" in exp.rules
    assert "0.05" in exp.rules and "28 days" in exp.rules


def test_changing_the_rules_afterwards_is_noticed() -> None:
    book = ExperimentBook()
    exp = book.start(spec(), brief(), NOW)
    tampered = exp.model_copy(
        update={
            "spec": exp.spec.model_copy(
                update={"pick_rule": exp.spec.pick_rule.model_copy(update={"alpha": 0.4})}
            )
        }
    )
    assert exp.digest_matches()
    assert not tampered.digest_matches()


def test_arms_must_be_inside_the_approved_envelope() -> None:
    book = ExperimentBook()
    narrow = brief(offers=(NO_OFFER, TEN_OFF))
    with pytest.raises(ExperimentRefused, match="not inside the approved envelope"):
        book.start(spec(), narrow, NOW)
    needs_ok = OfferOption(
        offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, needs_approval=True
    )
    with pytest.raises(ExperimentRefused, match="needs a person's approval"):
        book.start(spec(), brief(offers=(NO_OFFER, TEN_OFF, needs_ok)), NOW)
    with pytest.raises(ExperimentRefused, match="not inside"):
        book.start(spec(arms=("email", "sms"), kind=QuestionKind.CHANNEL), brief(), NOW)


def test_an_unapproved_or_foreign_brief_is_refused() -> None:
    book = ExperimentBook()
    unapproved = brief().model_copy(update={"approved_at": None})
    with pytest.raises(ExperimentRefused, match="not approved"):
        book.start(spec(), unapproved, NOW)
    with pytest.raises(ExperimentRefused, match="another goal"):
        book.start(spec(), brief("goal_other"), NOW)
    book.start(spec(), brief(), NOW)
    with pytest.raises(ExperimentRefused, match="already exists"):
        book.start(spec(), brief(), NOW)


def _winner(book: ExperimentBook, experiment_id: str) -> None:
    """Drive an experiment to a clear win with synthetic outcomes."""
    rng = np.random.default_rng(1)
    exp = book.get(experiment_id)
    base = rng.normal(0.0, 1.0, 400)
    out = Outcomes(
        arms={exp.spec.arm_ids[0]: base, exp.spec.arm_ids[1]: rng.normal(1.5, 1.0, 400)},
        holdout=rng.normal(0.0, 1.0, 400),
    )
    review = book.review(experiment_id, out, NOW + timedelta(days=10))
    assert review.experiment.end_kind is EndKind.WINNER


def test_promotion_makes_the_winner_the_default_and_can_be_undone() -> None:
    book = ExperimentBook()
    book.start(spec("a"), brief(), NOW)
    _winner(book, "a")
    assert book.default_for("goal_exp", QuestionKind.OFFER) is None
    promo = book.promote("a", brief(), NOW + timedelta(days=10), "free shipping won")
    assert promo.value == "freeship" and promo.previous is None and promo.active
    assert book.default_for("goal_exp", QuestionKind.OFFER) == "freeship"
    assert book.get("a").promoted
    last = book.log[-1]
    assert (last.kind, "free shipping won" in last.detail) == ("promoted", True)
    with pytest.raises(PromotionRefused, match="already been promoted"):
        book.promote("a", brief(), NOW + timedelta(days=11))

    undone = book.revert(promo.promotion_id, NOW + timedelta(days=12), "complaints went up")
    assert not undone.active and undone.reverted_reason == "complaints went up"
    assert book.default_for("goal_exp", QuestionKind.OFFER) is None
    assert [e.kind for e in book.log][-1] == "reverted"
    assert (
        book.revert(promo.promotion_id, NOW + timedelta(days=13), "again") == undone
    )  # undoing twice is harmless


def test_reverting_a_later_promotion_restores_the_one_before() -> None:
    book = ExperimentBook()
    book.start(spec("a", arms=("none", "pct10")), brief(), NOW)
    rng = np.random.default_rng(2)
    book.review(
        "a",
        Outcomes(
            arms={"none": rng.normal(0, 1, 400), "pct10": rng.normal(1.5, 1, 400)},
            holdout=rng.normal(0, 1, 400),
        ),
        NOW + timedelta(days=9),
    )
    first = book.promote("a", brief(), NOW + timedelta(days=9))
    book.start(spec("b", arms=("pct10", "freeship")), brief(), NOW + timedelta(days=10))
    out = Outcomes(
        arms={"pct10": rng.normal(1.5, 1, 400), "freeship": rng.normal(3.5, 1, 400)},
        holdout=rng.normal(0, 1, 400),
    )
    assert book.review("b", out, NOW + timedelta(days=20)).experiment.winner == "freeship"
    second = book.promote("b", brief(), NOW + timedelta(days=20))
    assert second.previous == "pct10"
    book.revert(second.promotion_id, NOW + timedelta(days=21), "back")
    assert book.default_for("goal_exp", QuestionKind.OFFER) == "pct10"
    assert first.active


def test_a_winner_is_promoted_only_inside_the_envelope_as_it_stands_now() -> None:
    book = ExperimentBook()
    book.start(spec("a"), brief(), NOW)
    _winner(book, "a")
    narrowed = brief(offers=(NO_OFFER, TEN_OFF))  # the marketer withdrew free shipping meanwhile
    with pytest.raises(PromotionRefused, match="no longer inside the approved envelope"):
        book.promote("a", narrowed, NOW + timedelta(days=11))
    assert book.default_for("goal_exp", QuestionKind.OFFER) is None
    wrong_goal = brief("goal_other")
    with pytest.raises(PromotionRefused, match="approved brief"):
        book.promote("a", wrong_goal, NOW + timedelta(days=11))


def test_only_a_winner_can_be_promoted() -> None:
    book = ExperimentBook()
    book.start(spec("a"), brief(), NOW)
    with pytest.raises(PromotionRefused, match="ended with a winner"):
        book.promote("a", brief(), NOW)  # still running
    book.stop("a", NOW, "stop")
    with pytest.raises(PromotionRefused, match="ended with a winner"):
        book.promote("a", brief(), NOW)


def test_the_next_experiment_must_use_the_promoted_default_as_its_reference() -> None:
    book = ExperimentBook()
    book.start(spec("a"), brief(), NOW)
    _winner(book, "a")
    book.promote("a", brief(), NOW + timedelta(days=10))
    with pytest.raises(ExperimentRefused, match="must be the reference arm"):
        book.start(spec("b", arms=("pct10", "none")), brief(), NOW + timedelta(days=11))
    book.start(spec("b", arms=("freeship", "none")), brief(), NOW + timedelta(days=11))


def test_the_book_round_trips_as_plain_data() -> None:
    book = ExperimentBook()
    book.start(spec("a"), brief(), NOW)
    _winner(book, "a")
    book.promote("a", brief(), NOW + timedelta(days=10))
    book.start(spec("b", arms=("freeship", "none")), brief(), NOW + timedelta(days=11))
    again = ExperimentBook.restore(book.snapshot())
    assert again.snapshot() == book.snapshot()
    assert again.default_for("goal_exp", QuestionKind.OFFER) == "freeship"
    assert again.get("a").digest_matches()
    assert [e.experiment_id for e in again.running()] == ["b"]
