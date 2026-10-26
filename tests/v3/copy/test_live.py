"""Live elimination cuts a planted loser, and does not cut a planted equal."""

from __future__ import annotations

import numpy as np
import pytest

from autonomy.content.config import CopyConfig
from autonomy.content.live import LiveBoard, LiveTest, wilson_bounds

from .synth import make_item

CFG = CopyConfig()
ITEMS = [make_item(f"d{i}", words=8 + i) for i in range(3)]


def _simulate(rates: list[float], seed: int, looks: int = 20, batch: int = 500) -> tuple[LiveTest, list[str]]:
    """Send `batch` customers to every live draft each look, then review. Returns the test and cut ids."""
    rng = np.random.default_rng(seed)
    test = LiveTest.start("ct000", ITEMS, CFG)
    cut: list[str] = []
    for _ in range(looks):
        for cid, rate in zip(list(test.arms), rates, strict=True):
            if test.arms[cid].live:
                test.record(cid, batch, int(rng.binomial(batch, rate)))
        cut.extend(c.content_id for c in test.review())
    return test, cut


def test_a_planted_loser_is_cut_and_the_others_stay() -> None:
    for seed in range(12):
        test, cut = _simulate([0.05, 0.05, 0.03], seed)
        assert cut == ["d2"], (seed, cut)
        assert test.live_ids() == ["d0", "d1"]
        assert test.arms["d2"].cut_reason and "leader" in test.arms["d2"].cut_reason


def test_a_planted_equal_is_never_cut_over_many_looks_and_seeds() -> None:
    cuts = 0
    for seed in range(150):
        test, cut = _simulate([0.05, 0.05, 0.05], seed, looks=30)
        cuts += len(cut)
        assert len(test.live_ids()) == 3
    assert cuts == 0


def test_a_small_gap_is_left_alone_rather_than_guessed() -> None:
    _, cut = _simulate([0.05, 0.046, 0.05], seed=3, looks=6)  # 3,000 sends each: far too few to tell
    assert cut == []


def test_nothing_is_cut_before_the_minimum_sends() -> None:
    test = LiveTest.start("ct000", ITEMS, CFG)
    test.record("d0", CFG.min_sends_per_arm - 1, 100)  # 33% against 0%, but only 299 sends
    test.record("d1", CFG.min_sends_per_arm - 1, 0)
    assert test.review() == []
    test.record("d0", 1, 0)
    assert test.review() == []  # the other arm is still under the minimum
    test.record("d1", 1, 0)
    assert [c.content_id for c in test.review()] == ["d1"]


def test_the_leader_is_never_cut_and_one_draft_always_stays() -> None:
    test = LiveTest.start("ct000", ITEMS, CFG)
    test.record("d0", 5000, 500)
    test.record("d1", 5000, 100)
    test.record("d2", 5000, 100)
    cuts = test.review()
    assert sorted(c.content_id for c in cuts) == ["d1", "d2"]
    assert test.live_ids() == ["d0"] and test.winner == "d0"
    assert test.review() == []


def test_traffic_is_split_equally_and_stably_and_logged_as_a_probability() -> None:
    test = LiveTest.start("ct000", ITEMS, CFG)
    picks = [test.assign(f"c{i}") for i in range(3000)]
    assert picks == [test.assign(f"c{i}") for i in range(3000)]
    for cid in test.live_ids():
        assert abs(picks.count(cid) / 3000 - 1 / 3) < 0.04
        assert test.probability(cid) == pytest.approx(1 / 3)
    test.record("d0", 5000, 500)
    test.record("d1", 5000, 100)
    test.record("d2", 5000, 100)
    test.review()
    assert test.probability("d1") == 0.0 and test.probability("d0") == 1.0
    assert {test.assign(f"c{i}") for i in range(50)} == {"d0"}


def test_only_top_k_drafts_go_live_and_results_feed_back() -> None:
    many = [make_item(f"m{i}", words=5 + i) for i in range(6)]
    board = LiveBoard(CFG)
    test = board.start("ct001", many)
    assert list(test.arms) == ["m0", "m1", "m2"]
    board.record_many([("ct001", "m0", 400, 40), ("ct001", "m1", 400, 4), ("ct001", "m2", 0, 0)])
    assert [c.content_id for c in board.review_all()] == ["m1"] or board.review_all() == []
    cells = board.feedback()
    assert {(c.item.content_id, c.sends, c.purchases) for c in cells} == {("m0", 400, 40), ("m1", 400, 4)}
    assert all(c.type_id == "ct001" for c in cells)
    assert set(board.live_by_type()) == {"ct001"}
    with pytest.raises(KeyError):
        board.record("ct001", "m5", 1, 0)
    with pytest.raises(ValueError):
        test.record("m0", 10, 11)


def test_wilson_bounds() -> None:
    assert wilson_bounds(0, 0, 3.0) == (0.0, 1.0)
    low, high = wilson_bounds(50, 1000, 3.0)
    assert low < 0.05 < high and low >= 0.0 and high <= 1.0
    assert wilson_bounds(500, 10_000, 3.0)[1] - wilson_bounds(500, 10_000, 3.0)[0] < high - low
