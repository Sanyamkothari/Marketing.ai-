"""The two learned baselines of the bench (scripts/v2_realbench/learners.py, docs/v4/BASELINES.md):
they recover a planted effect, are deterministic and fit on TRAIN rows only. Inline data, no download."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from scripts.v2_realbench import adapters as A  # noqa: N812
from scripts.v2_realbench import common as C  # noqa: N812
from scripts.v2_realbench import learners as L  # noqa: N812


def _planted(n: int = 6000, seed: int = 11, *, both: bool = False) -> C.BenchData:
    """A 50/50 trial where the message adds conversions only for x == 1 (and, with `both`, only when the
    integer feature z is at least 1 as well); everyone else converts at 5% whatever they get."""
    rng = np.random.default_rng(seed)
    ids = np.array([f"p{i}" for i in range(n)], dtype=object)
    x = rng.integers(0, 2, n)
    z = rng.integers(0, 4, n)
    arm = rng.integers(0, 2, n).astype(np.int64)
    responder = (x == 1) & (z >= 1) if both else (x == 1)
    conv = (rng.random(n) < 0.05 + 0.35 * arm * responder).astype(float)
    feats = pd.DataFrame({"x": x.astype(float), "z": z.astype(float), "noise": rng.normal(size=n)})
    return A._feature_rct(
        "planted", ids, arm, 0.5, feats, conv, None, (("x",), ("z",)), ("inline fixture",), {"rows": n}
    )


def _split(data: C.BenchData) -> tuple[C.IntArray, C.IntArray]:
    return C.split_by_hash(data.customer_ids)


def test_dr_learner_recovers_the_planted_uplift_sign() -> None:
    data = _planted()
    train, test = _split(data)
    pi, info = L.dr_learner(data, train, test)
    x = data.features["x"].to_numpy()[test]
    contacted = pi[:, 1] > 0
    assert contacted[x == 1].mean() > 0.9  # the message pays for them
    assert contacted[x == 0].mean() < 0.1  # it does not pay its cost for the rest
    assert pi.shape == (len(test), data.n_arms) and np.allclose(pi.sum(axis=1), 1.0)
    assert info["train_rows"] == len(train)


def test_dr_learner_never_sends_an_arm_below_its_cost() -> None:
    """With a contact cost above any effect the threshold keeps everyone out."""
    data = _planted()
    dear = dataclasses.replace(data, arm_cost=(0.0, 1e6))
    train, test = _split(dear)
    pi, _ = L.dr_learner(dear, train, test)
    assert pi[:, 0].min() == 1.0


def test_tree_finds_the_planted_split() -> None:
    data = _planted()
    train, test = _split(data)
    pi, info = L.policy_tree(data, train, test)
    x = data.features["x"].to_numpy()[test]
    assert info["tree"][0].startswith("if x <= 0")
    assert np.all(pi[x == 1, 1] == 1.0) and np.all(pi[x == 0, 0] == 1.0)
    assert info["train_net_value_per_customer"] > info["no_split_net_value_per_customer"]


def test_tree_finds_a_planted_depth_two_interaction() -> None:
    data = _planted(n=12000, both=True)
    train, test = _split(data)
    pi, info = L.policy_tree(data, train, test)
    x = data.features["x"].to_numpy()[test]
    z = data.features["z"].to_numpy()[test]
    want = (x == 1) & (z >= 1)
    got = pi[:, 1] > 0
    assert (got == want).mean() > 0.95
    assert sum(line.lstrip().startswith("if ") for line in info["tree"]) <= 3  # a root and two child splits


def test_both_are_deterministic() -> None:
    data = _planted(n=3000)
    train, test = _split(data)
    for fit in (L.dr_learner, L.policy_tree):
        a, info_a = fit(data, train, test)
        b, info_b = fit(data, train, test)
        assert np.array_equal(a, b)
        assert info_a == info_b
    assert np.array_equal(L.dr_scores(data, train), L.dr_scores(data, train))


def test_neither_sees_test_rows_when_fitting() -> None:
    """Outcomes, arms and propensities of the TEST rows are replaced by junk: nothing fitted changes."""
    data = _planted(n=3000)
    train, test = _split(data)
    rng = np.random.default_rng(99)
    junk_margin = data.margin_value.copy()
    junk_margin[test] = rng.normal(1e4, 1e3, len(test))
    junk_conv = data.conversion.copy()
    junk_conv[test] = rng.integers(0, 2, len(test)).astype(float)
    junk_arm = data.arm.copy()
    junk_arm[test] = rng.integers(0, 2, len(test))
    junk_prop = data.propensity.copy()
    junk_prop[test] = rng.uniform(0.1, 0.9, len(test))
    poisoned = dataclasses.replace(
        data, margin_value=junk_margin, conversion=junk_conv, arm=junk_arm, propensity=junk_prop
    )
    for fit in (L.dr_learner, L.policy_tree):
        clean_pi, _ = fit(data, train, test)
        poisoned_pi, _ = fit(poisoned, train, test)
        assert np.array_equal(clean_pi, poisoned_pi), fit.__name__
    assert np.array_equal(L.dr_scores(data, train), L.dr_scores(poisoned, train))


def test_bin_edges_and_split_candidates_come_from_train_rows() -> None:
    """The TEST rows' features are replaced by out-of-range values: the bin edges, the split candidates,
    the fitted tree and both policies on TRAIN rows stay the same (only TEST predictions may move)."""
    data = _planted(n=3000)
    train, test = _split(data)
    feats = data.features.copy()
    feats.iloc[test] = 1e6
    poisoned = dataclasses.replace(data, features=feats)

    def fitted(d: C.BenchData) -> C.Design:
        return C.Design(d.numeric, d.categorical).fit(d.features.iloc[train])

    clean, dirty = fitted(data), fitted(poisoned)
    assert clean.edges.keys() == dirty.edges.keys()
    assert all(np.array_equal(clean.edges[c], dirty.edges[c]) for c in clean.edges)
    assert all(e.max() < 1e6 for e in clean.edges.values())
    assert L._candidates(data, clean) == L._candidates(poisoned, dirty)
    _, tree_clean = L.policy_tree(data, train, test)
    _, tree_dirty = L.policy_tree(poisoned, train, test)
    assert tree_clean == tree_dirty
    for fit in (L.dr_learner, L.policy_tree):
        assert np.array_equal(fit(data, train, train)[0], fit(poisoned, train, train)[0]), fit.__name__


def test_the_report_carries_both_rows_and_a_separate_best_learned_line() -> None:
    from scripts.v2_realbench.run import render

    data = _planted(n=600, seed=4)
    out = C.evaluate_dataset(data, seeds=1, timings=C.Timings(quiet=True))
    for name in C.LEARNED_BASELINES:
        p = out["policies"][name]
        assert p["dr"]["low"] <= p["dr"]["point"] <= p["dr"]["high"]
        assert name in out["conversion"] and name in out["learned_baselines"]
    assert out["best_learned_baseline"]["name"] in C.LEARNED_BASELINES
    assert out["best_simple"]["name"] not in C.LEARNED_BASELINES  # "best simple" keeps its meaning
    md = render({"datasets": {"planted": out}})
    assert "Best learned baseline" in md
    assert "| Policy tree (depth 2) |" in md and "| DR-learner (ridge) |" in md


def test_the_tree_search_is_exact_on_a_small_case() -> None:
    """Brute force over every root and child split gives the value the search reports."""
    data = _planted(n=1500, both=True)
    train, test = _split(data)
    _, info = L.policy_tree(data, train, test)
    design = C.Design(data.numeric, data.categorical).fit(data.features.iloc[train])
    net = L.dr_scores(data, train, design) - np.asarray(data.arm_cost)[None, :]
    frame = data.features.iloc[train].reset_index(drop=True)
    masks = [s.mask(frame) for s in L._candidates(data, design)]
    min_leaf = info["min_leaf"]

    def leaf(rows: np.ndarray) -> float:
        return float(net[rows].sum(axis=0).max())

    def best_child(rows: np.ndarray) -> float:
        best = leaf(rows)
        for m in masks:
            a, b = rows & m, rows & ~m
            if a.sum() >= min_leaf and b.sum() >= min_leaf:
                best = max(best, leaf(a) + leaf(b))
        return best

    everyone = np.ones(len(train), dtype=bool)
    best = leaf(everyone)
    for m in masks:
        a, b = everyone & m, everyone & ~m
        if a.sum() >= min_leaf and b.sum() >= min_leaf:
            best = max(best, best_child(a) + best_child(b))
    assert abs(best / len(train) - info["train_net_value_per_customer"]) < 1e-9
