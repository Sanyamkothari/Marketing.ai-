"""Assignment: a hash, deterministic, balanced, agent group only, one experiment per customer."""

from __future__ import annotations

from autonomy.contracts import Audience, AudienceFilter, Group
from autonomy.experiments import ExperimentBook, assign, experiment_score
from autonomy.experiments.assign import arm_for_score
from tests.v3.experiments.helpers import NOW, brief, customers_in, spec, state


def test_the_score_is_deterministic_and_depends_on_both_ids() -> None:
    assert experiment_score("e1", "c1") == experiment_score("e1", "c1")
    assert experiment_score("e1", "c1") != experiment_score("e2", "c1")
    assert experiment_score("e1", "c1") != experiment_score("e1", "c2")
    assert 0.0 <= experiment_score("e1", "c1") < 1.0


def test_assignment_repeats_exactly() -> None:
    book = ExperimentBook()
    exp = book.start(spec(share=0.10), brief(), NOW)
    ids = [f"cust{i}" for i in range(500)]
    first = [assign([exp], state(c), NOW) for c in ids]
    again = [assign([exp], state(c), NOW) for c in ids]
    assert [a.arm_id if a else None for a in first] == [a.arm_id if a else None for a in again]


def test_share_and_arms_come_out_as_planned() -> None:
    s = spec(arms=("none", "pct10", "freeship"), share=0.10)
    n = 30_000
    counts: dict[str | None, int] = {}
    for i in range(n):
        arm = arm_for_score(s, experiment_score(s.experiment_id, f"cust{i}"))
        counts[arm] = counts.get(arm, 0) + 1
    in_test = n - counts.get(None, 0)
    assert abs(in_test / n - 0.10) < 0.01
    for arm in ("none", "pct10", "freeship"):
        assert abs(counts[arm] / n - 0.10 / 3) < 0.006


def test_the_chance_of_an_arm_is_one_over_the_number_of_arms() -> None:
    book = ExperimentBook()
    exp = book.start(spec(arms=("none", "pct10", "freeship")), brief(), NOW)
    cid = next(customers_in(exp.spec, "pct10", 1))
    found = assign([exp], state(cid), NOW)
    assert found is not None
    assert found.arm_id == "pct10"
    assert found.probability == 1 / 3


def test_holdout_and_baseline_are_never_assigned() -> None:
    book = ExperimentBook()
    exp = book.start(spec(share=0.10), brief(), NOW)
    for cid in customers_in(exp.spec, "freeship", 20):
        assert assign([exp], state(cid), NOW) is not None  # the hash would admit them...
        assert assign([exp], state(cid, Group.HOLDOUT), NOW) is None  # ...but not the holdout
        assert assign([exp], state(cid, Group.BASELINE), NOW) is None


def test_only_the_audience_is_assigned() -> None:
    book = ExperimentBook()
    narrow = spec().model_copy(
        update={
            "audience": Audience(
                description="big spenders", filters=(AudienceFilter(field="revenue", op="gt", value=100),)
            )
        }
    )
    exp = book.start(narrow, brief(), NOW)
    cid = next(customers_in(exp.spec, "freeship", 1))
    assert assign([exp], state(cid, revenue=50.0), NOW) is None
    found = assign([exp], state(cid, revenue=500.0), NOW)
    assert found is not None and found.arm_id == "freeship"


def test_a_customer_is_in_at_most_one_experiment_and_the_earlier_one_wins() -> None:
    book = ExperimentBook()
    first = book.start(spec("a", share=0.05), brief(), NOW)
    second = book.start(spec("b", share=0.05), brief(), NOW.replace(hour=13))
    running = book.running()
    assert [e.experiment_id for e in running] == ["a", "b"]
    seen = 0
    for i in range(4000):
        found = assign(running, state(f"cust{i}"), NOW)
        if found is None:
            continue
        seen += 1
        in_a = arm_for_score(first.spec, experiment_score("a", f"cust{i}")) is not None
        assert found.experiment.experiment_id == ("a" if in_a else "b")
    assert seen > 0
    assert second.experiment_id == "b"
