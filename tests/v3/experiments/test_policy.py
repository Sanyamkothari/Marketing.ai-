"""ExperimentPolicy: the arm restricts the options, everything else still applies, probabilities are logged."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from autonomy.contracts import (
    Action,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Group,
    Reward,
)
from autonomy.decide import rules
from autonomy.decide.policy import AutonomousPolicy
from autonomy.experiments import (
    EXPERIMENT_ARM,
    ExperimentBook,
    ExperimentPolicy,
    Outcomes,
    bucket_of,
    collect_outcomes,
)
from autonomy.interfaces import Policy
from autonomy.proof.ope import from_decisions
from autonomy.scientist.questions import QuestionKind
from tests.v3.experiments.helpers import GOAL, NOW, LibraryBidder, brief, ctx, customers_in, spec, state


def inner(floor: float = 0.0) -> AutonomousPolicy:
    return AutonomousPolicy([LibraryBidder()], lambda s: 0.0, floor, version="inner-v1")


def offer_of(d: Decision) -> str | None:
    return d.action.offer_id if d.action is not None else None


def running_book(**kw: object) -> tuple[ExperimentBook, ExperimentPolicy]:
    book = ExperimentBook()
    book.start(spec("exp1", share=0.10, **kw), brief(), NOW)  # type: ignore[arg-type]
    return book, ExperimentPolicy(inner(), book)


def test_it_is_a_policy() -> None:
    _, policy = running_book()
    assert isinstance(policy, Policy)
    assert policy.version == "inner-v1"


def test_without_a_running_experiment_it_is_exactly_the_inner_policy() -> None:
    plain = inner()
    wrapped = ExperimentPolicy(inner(), ExperimentBook())
    for i in range(50):
        s, c = state(f"c{i}"), ctx(seed=i)
        assert wrapped.decide(s, c) == plain.decide(s, c)


def test_an_assigned_customer_gets_only_their_arms_offer() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    for arm in ("pct10", "freeship"):
        for cid in customers_in(sp, arm, 10):
            d = policy.decide(state(cid), ctx())
            assert d.kind is DecisionKind.ACT
            assert d.action is not None and d.action.offer_id == arm
            assert bucket_of(d.policy_version, "exp1") == arm


def test_everyone_else_still_gets_the_best_option_whatever_it_is() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    for cid in customers_in(sp, None, 10):
        d = policy.decide(state(cid), ctx())
        assert d.action is not None and d.action.offer_id == "freeship"  # the bidder's favourite
        assert d.policy_version == "inner-v1"
        assert d.probability == 1.0


def test_the_logged_probability_includes_the_randomisation() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    cid = next(customers_in(sp, "pct10", 1))
    d = policy.decide(state(cid), ctx())
    assert d.probability == 0.5  # 1 of 2 arms, and the inner choice is certain within the arm

    book3 = ExperimentBook()
    book3.start(spec("exp3", arms=("none", "pct10", "freeship"), share=0.10), brief(), NOW)
    cid3 = next(customers_in(book3.get("exp3").spec, "freeship", 1))
    d3 = ExperimentPolicy(inner(), book3).decide(state(cid3), ctx())
    assert abs(d3.probability - 1 / 3) < 1e-12


def test_the_inner_probability_is_kept_as_a_factor() -> None:
    """With an exploration floor the inner policy spreads its choice over what is left in the arm."""
    book = ExperimentBook()
    book.start(spec("exp1", share=0.10), brief(), NOW)
    policy = ExperimentPolicy(inner(floor=0.4), book)
    sp = book.get("exp1").spec
    for cid in customers_in(sp, "freeship", 20):
        d = policy.decide(state(cid), ctx())
        restricted = ctx().model_copy(
            update={"content": tuple(c for c in ctx().content if c.offer_id == "freeship")}
        )
        direct = inner(floor=0.4).decide(state(cid), restricted)
        assert d.probability == direct.probability / 2
        assert 0.0 < d.probability <= 0.5


def test_probabilities_stay_valid_for_the_proof_code() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    decisions = [
        policy.decide(state(cid), ctx(NOW + timedelta(days=i)))
        for i, cid in enumerate([*customers_in(sp, "pct10", 20), *customers_in(sp, "freeship", 20)])
    ]
    rewards = [
        Reward(
            decision_id=d.decision_id,
            customer_id=d.customer_id,
            goal_id=GOAL,
            window_end=NOW + timedelta(days=30),
            converted=False,
            net=1.0 if d.action and d.action.offer_id == "freeship" else 0.0,
        )
        for d in decisions
    ]
    logged = from_decisions(decisions, rewards)
    assert len(logged.decisions) == 40
    assert np.all((logged.logged_prob > 0) & (logged.logged_prob <= 1))
    # a target that always chooses free shipping: importance weights are finite and IPS reads the planted gap
    report = logged.evaluate(lambda d: 1.0 if d.action and d.action.offer_id == "freeship" else 0.0)
    assert report.ips.max_weight == 2.0
    assert abs(report.ips.value.point - 1.0) < 1e-9


def test_hard_rules_still_apply_inside_an_arm() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    cid = next(customers_in(sp, "freeship", 1))
    nobody = ChannelStats(address_known=True, consent=False)
    no_consent = state(cid, channels={Channel.EMAIL: nobody, Channel.WHATSAPP: nobody})
    d = policy.decide(no_consent, ctx())
    assert d.kind is DecisionKind.NOTHING
    assert bucket_of(d.policy_version, "exp1") == "freeship"  # still counted in the arm (intention to treat)
    quiet = ctx(NOW.replace(hour=23))  # quiet hours 21:00 to 09:00, customer in UTC: every send is held back
    held = policy.decide(state(cid), quiet)
    assert held.action is None or held.action.send_at.hour >= 9
    unsubscribed = ChannelStats(address_known=True, consent=True, unsubscribed=True)
    d2 = policy.decide(
        state(cid, channels={Channel.EMAIL: unsubscribed, Channel.WHATSAPP: unsubscribed}), ctx()
    )
    assert d2.kind is DecisionKind.NOTHING
    assert rules.UNSUBSCRIBED in d2.blocked_by


def test_holdout_is_never_touched_and_the_matching_holdout_is_tagged() -> None:
    book, policy = running_book()
    sp = book.get("exp1").spec
    for cid in customers_in(sp, "freeship", 10):
        d = policy.decide(state(cid, Group.HOLDOUT), ctx())
        assert d.kind is DecisionKind.NOTHING
        assert d.probability == 1.0
        assert d.blocked_by == (rules.HOLDOUT,)
        assert bucket_of(d.policy_version, "exp1") == "holdout"
    baseline = policy.decide(state("b1", Group.BASELINE), ctx())
    assert bucket_of(baseline.policy_version, "exp1") is None


def test_the_pick_rule_is_on_record_before_the_first_send() -> None:
    book, policy = running_book()
    exp = book.get("exp1")
    assert exp.first_assignment_at is None
    sp = exp.spec
    now = NOW + timedelta(days=2)
    d = policy.decide(state(next(customers_in(sp, "pct10", 1))), ctx(now))
    after = book.get("exp1")
    assert after.registered_at < after.first_assignment_at  # type: ignore[operator]
    assert after.first_assignment_at == now
    assert d.action is not None and d.action.send_at >= after.registered_at
    assert after.digest_matches() and after.rules == exp.rules
    assert [e.kind for e in book.log[:2]] == ["registered", "started"]
    assert book.log[0].at <= d.made_at


def test_a_policy_that_ignores_the_arm_is_stopped_from_acting_outside_it() -> None:
    class Stubborn:
        version = "stubborn-v1"

        def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
            action = Action(goal_id=GOAL, channel=Channel.EMAIL, offer_id="freeship", send_at=ctx.now)
            return Decision(
                decision_id="d1",
                customer_id=state.customer_id,
                made_at=ctx.now,
                kind=DecisionKind.ACT,
                action=action,
                probability=0.5,
                reason="always free shipping",
                policy_version=self.version,
            )

        def learn(self, rewards: object, decisions: object) -> None:
            return None

    book = ExperimentBook()
    book.start(spec("exp1", share=0.10), brief(), NOW)
    policy = ExperimentPolicy(Stubborn(), book)
    cid = next(customers_in(book.get("exp1").spec, "pct10", 1))
    d = policy.decide(state(cid), ctx())
    assert d.kind is DecisionKind.NOTHING and d.action is None
    assert EXPERIMENT_ARM in d.blocked_by
    assert policy.stats["outside_arm"] == 1
    other = next(customers_in(book.get("exp1").spec, "freeship", 1))
    assert policy.decide(state(other), ctx()).kind is DecisionKind.ACT


def test_it_passes_everything_else_to_the_inner_policy() -> None:
    class WithNightly:
        version = "n-v1"

        def __init__(self) -> None:
            self.nightly_calls = 0
            self.learned = False

        def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
            raise AssertionError("not called")

        def learn(self, rewards: object, decisions: object) -> None:
            self.learned = True

        def nightly(self) -> str:
            self.nightly_calls += 1
            return "ok"

    inner_policy = WithNightly()
    policy = ExperimentPolicy(inner_policy, ExperimentBook())
    assert policy.nightly() == "ok"
    policy.learn([], {})
    assert inner_policy.learned is True


def test_a_promoted_offer_becomes_the_default_for_everyone_outside_the_test() -> None:
    book = ExperimentBook()
    # the agent would pick free shipping; promote 10% off to see the default applied (and "none" kept)
    book.start(spec("exp1", share=0.05, arms=("none", "pct10")), brief(), NOW)
    rng = np.random.default_rng(1)
    book.review(
        "exp1",
        Outcomes(
            arms={"none": rng.normal(0, 1, 400), "pct10": rng.normal(2, 1, 400)},
            holdout=rng.normal(0, 1, 400),
        ),
        NOW + timedelta(days=9),
    )
    book.promote("exp1", brief(), NOW + timedelta(days=9))
    policy = ExperimentPolicy(inner(), book)
    assert book.default_for(GOAL, QuestionKind.OFFER) == "pct10"
    for i in range(20):
        assert offer_of(policy.decide(state(f"x{i}"), ctx(NOW + timedelta(days=10)))) in ("pct10", "none")
    assert policy.stats["defaulted"] == 20
    off = ExperimentPolicy(inner(), book, apply_defaults=False)
    assert offer_of(off.decide(state("x0"), ctx(NOW + timedelta(days=10)))) == "freeship"
    # and undoing the promotion puts the agent back as it was
    book.revert(book.promotions()[0].promotion_id, NOW + timedelta(days=11), "undo")
    assert offer_of(policy.decide(state("x0"), ctx(NOW + timedelta(days=12)))) == "freeship"


def test_the_whole_loop_finds_the_planted_better_offer() -> None:
    """Decisions made by the wrapper, rewards planted (free shipping earns more), read from the log alone."""
    book, policy = running_book()
    sp = book.get("exp1").spec
    rng = np.random.default_rng(11)
    decisions: list[Decision] = []
    rewards: list[Reward] = []
    members = {
        "pct10": list(customers_in(sp, "pct10", 400)),
        "freeship": list(customers_in(sp, "freeship", 400)),
        "holdout": list(customers_in(sp, None, 400, prefix="h")),
    }
    for bucket, ids in members.items():
        group = Group.HOLDOUT if bucket == "holdout" else Group.AGENT
        mean = {"pct10": 0.3, "freeship": 1.3, "holdout": 0.0}[bucket]
        for cid in ids:
            d = policy.decide(state(cid, group), ctx(NOW + timedelta(days=1)))
            decisions.append(d)
            buys = rng.random() < 0.1
            rewards.append(
                Reward(
                    decision_id=d.decision_id,
                    customer_id=cid,
                    goal_id=GOAL,
                    window_end=NOW + timedelta(days=15),
                    converted=bool(buys),
                    net=float(rng.normal(mean * 10, 2)) if buys else 0.0,
                )
            )
    now = NOW + timedelta(days=16)
    review = book.review("exp1", collect_outcomes(book.get("exp1"), decisions, rewards, now), now)
    assert review.experiment.winner == "freeship"
    assert review.reading.arm("freeship").n == 400
    assert review.reading.holdout_n == 400
