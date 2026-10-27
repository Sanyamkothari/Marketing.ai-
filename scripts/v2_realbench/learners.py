"""Two stronger baselines for the bench's "simple rules" table (docs/v4/BASELINES.md), numpy only.

Both are fitted on TRAIN rows and read nothing else; the bench then scores them on TEST with the same
doubly-robust estimator as every other policy.

* **DR-learner** (`dr_learner`). Doubly-robust pseudo-outcomes of the margin, per arm:
  `phi_a(i) = q_a(x_i) + 1[A_i = a] (Y_i - q_a(x_i)) / e_i`, where `q_a` is a ridge regression of the margin
  on one-hot binned features fitted on the other fold (cross-fitting) and `e_i` is the design probability of
  the arm the customer received. The effect of arm `a` over no contact is regressed on the same features
  (ridge, all of train). A customer gets the arm with the largest predicted effect, and only when it is above
  that arm's contact cost (the cost threshold); otherwise no contact.
* **Policy tree** (`policy_tree`). A depth-2 tree whose leaves each name an arm, fitted by exhaustive search
  on the same pseudo-outcomes (net of arm cost): every root split and every split of each child among the
  candidate splits, each leaf taking the arm with the largest summed score. Candidate splits are the binned
  numeric thresholds (quantile cuts of train) and the top levels of each categorical, one-vs-rest.

Cross-fit folds come from a BLAKE2b hash of the customer id (`FOLD_SALT`), so a run is deterministic and a
customer's fold does not depend on who else is in the file. The tree is fitted on at most `TREE_MAX_ROWS`
train rows (the smallest hashes), which keeps its search small on the 150,000-row train sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from scripts.v2_realbench.common import (
    LEARNED_BASELINES,
    BenchData,
    Design,
    FloatArray,
    IntArray,
    unit_hash,
)

__all__ = [
    "DR_LEARNER_NAME",
    "FOLD_SALT",
    "LEARNED_BASELINES",
    "TREE_NAME",
    "TreeNode",
    "dr_learner",
    "dr_scores",
    "policy_tree",
]

DR_LEARNER_NAME, TREE_NAME = LEARNED_BASELINES

FOLD_SALT = "autonomy-realbench-v4:dr-folds"
TREE_SALT = "autonomy-realbench-v4:tree-rows"
FOLDS = 2
RIDGE = 10.0
"""L2 penalty on every coefficient but the intercept (features are 0/1 indicators)."""
TREE_MAX_ROWS = 100_000
TREE_LEVELS = 8
"""Categorical levels turned into one-vs-rest split candidates, most frequent first."""
MIN_LEAF_SHARE = 0.01
MIN_LEAF_FLOOR = 50
PROPENSITY_FLOOR = 1e-3


def _ridge(x: FloatArray, y: FloatArray, lam: float = RIDGE) -> FloatArray:
    """Ridge coefficients; column 0 is the unpenalised intercept. Zero rows: all-zero coefficients."""
    k = x.shape[1]
    if len(y) == 0:
        return np.zeros(k, dtype=np.float64)
    reg = lam * np.eye(k)
    reg[0, 0] = 0.0
    out: FloatArray = np.asarray(np.linalg.solve(x.T @ x + reg + 1e-9 * np.eye(k), x.T @ y), dtype=np.float64)
    return out


def _folds(ids: Any) -> IntArray:
    return np.minimum((unit_hash(ids, FOLD_SALT) * FOLDS).astype(np.int64), FOLDS - 1)


def dr_scores(data: BenchData, train: IntArray, design: Design | None = None) -> FloatArray:
    """Doubly-robust pseudo-outcomes of the margin for every arm, one row per train customer.

    Shape (len(train), n_arms). The outcome models are cross-fitted: a row's `q_a` comes from a ridge
    fitted on the rows of the other folds that received arm a. Reads TRAIN rows only."""
    if design is None:
        design = Design(data.numeric, data.categorical).fit(data.features.iloc[train])
    x = design.matrix(data.features.iloc[train])
    y = data.margin_value[train].astype(np.float64)
    arm = data.arm[train]
    prop = np.maximum(data.propensity[train].astype(np.float64), PROPENSITY_FLOOR)
    fold = _folds(data.customer_ids[train])
    k = data.n_arms
    q = np.zeros((len(train), k), dtype=np.float64)
    for f in range(FOLDS):
        held = fold == f
        for a in range(k):
            fit_rows = (~held) & (arm == a)
            beta = _ridge(x[fit_rows], y[fit_rows])
            q[held, a] = x[held] @ beta
    rows = np.arange(len(train))
    phi = q.copy()
    phi[rows, arm] += (y - q[rows, arm]) / prop
    return phi


def dr_learner(data: BenchData, train: IntArray, test: IntArray) -> tuple[FloatArray, dict[str, Any]]:
    """The DR-learner policy on TEST rows (deterministic, one arm per customer). See the module docstring."""
    design = Design(data.numeric, data.categorical).fit(data.features.iloc[train])
    x_tr = design.matrix(data.features.iloc[train])
    x_te = design.matrix(data.features.iloc[test])
    phi = dr_scores(data, train, design)
    k = data.n_arms
    cost = np.asarray(data.arm_cost, dtype=np.float64)
    gain = np.zeros((len(test), k), dtype=np.float64)
    for a in range(1, k):
        gain[:, a] = x_te @ _ridge(x_tr, phi[:, a] - phi[:, 0]) - cost[a]
    gain[:, 0] = 0.0  # no contact: the threshold itself, so an arm is sent only when it clears its cost
    pi = np.zeros_like(gain)
    pi[np.arange(len(test)), gain.argmax(axis=1)] = 1.0
    info = {
        "folds": FOLDS,
        "ridge": RIDGE,
        "columns": int(x_tr.shape[1]),
        "train_rows": len(train),
        "mean_predicted_net_gain_of_contacted": (
            float(gain.max(axis=1)[gain.argmax(axis=1) > 0].mean())
            if np.any(gain.argmax(axis=1) > 0)
            else None
        ),
    }
    return pi, info


# --- the tree ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """One candidate split: customers with `column <= threshold` (numeric) or `column == level` (categorical)."""

    column: str
    threshold: float | None
    level: str | None

    def mask(self, frame: pd.DataFrame) -> npt.NDArray[np.bool_]:
        if self.level is not None:
            eq: npt.NDArray[np.bool_] = frame[self.column].astype(str).to_numpy() == self.level
            return eq
        le: npt.NDArray[np.bool_] = frame[self.column].to_numpy(dtype=np.float64) <= float(
            self.threshold or 0.0
        )
        return le

    def text(self) -> str:
        return (
            f"{self.column} == {self.level}"
            if self.level is not None
            else f"{self.column} <= {self.threshold:.6g}"
        )


@dataclass
class TreeNode:
    """A node of the fitted tree: a leaf names an arm, a split sends `yes` / `no` customers on."""

    arm: int
    split: Split | None = None
    yes: TreeNode | None = None
    no: TreeNode | None = None

    def route(self, frame: pd.DataFrame) -> IntArray:
        n = len(frame)
        if self.split is None or self.yes is None or self.no is None:
            return np.full(n, self.arm, dtype=np.int64)
        m = self.split.mask(frame)
        out = np.empty(n, dtype=np.int64)
        out[m] = self.yes.route(frame[m])
        out[~m] = self.no.route(frame[~m])
        return out

    def describe(self, names: tuple[str, ...], depth: int = 0) -> list[str]:
        pad = "  " * depth
        if self.split is None or self.yes is None or self.no is None:
            return [f"{pad}-> {names[self.arm]}"]
        return [
            f"{pad}if {self.split.text()}:",
            *self.yes.describe(names, depth + 1),
            f"{pad}else:",
            *self.no.describe(names, depth + 1),
        ]


def _candidates(data: BenchData, design: Design) -> list[Split]:
    """The splits the search may use, from TRAIN rows only (the design's edges and levels are train's)."""
    out: list[Split] = []
    for col in data.numeric:
        for edge in design.edges[col]:
            out.append(Split(col, float(edge), None))
    for col in data.categorical:
        for level in design.levels[col][:TREE_LEVELS]:
            out.append(Split(col, None, level))
    return out


def policy_tree(
    data: BenchData, train: IntArray, test: IntArray, *, max_rows: int = TREE_MAX_ROWS
) -> tuple[FloatArray, dict[str, Any]]:
    """The depth-2 policy tree on TEST rows (deterministic, one arm per customer). See the module docstring.

    The search is exact over the candidate splits. For a root split s and a child split t the four leaves'
    score sums come from one matrix product of the split indicators with the scores, so the whole search is
    a few array operations on a (splits x splits) table."""
    design = Design(data.numeric, data.categorical).fit(data.features.iloc[train])
    phi = dr_scores(data, train, design)
    cost = np.asarray(data.arm_cost, dtype=np.float64)
    net = phi - cost[None, :]
    if len(train) > max_rows:
        keep = np.sort(np.argsort(unit_hash(data.customer_ids[train], TREE_SALT), kind="stable")[:max_rows])
    else:
        keep = np.arange(len(train))
    frame = data.features.iloc[train[keep]].reset_index(drop=True)
    score = net[keep]
    n, k = score.shape
    splits = _candidates(data, design)
    min_leaf = max(MIN_LEAF_FLOOR, int(MIN_LEAF_SHARE * n))
    root = TreeNode(arm=int(score.sum(axis=0).argmax()))
    value0 = float(score.sum(axis=0).max())
    best_value, best_tree = value0, root
    if splits and n >= 2 * min_leaf:
        m = np.column_stack([s.mask(frame) for s in splits]).astype(np.float64)  # n x S
        z = np.hstack([score, np.ones((n, 1))])  # scores and a count column
        # c[j][s, t] = sum over customers in split s and split t of z[:, j]
        c = np.stack([(m * z[:, j : j + 1]).T @ m for j in range(k + 1)])
        tot = z.sum(axis=0)
        sing = np.stack([np.diag(c[j]) for j in range(k + 1)])  # (k+1, S): customers in split s alone
        n_s = len(splits)
        for s in range(n_s):
            yes_tot = sing[:, s]
            no_tot = tot - yes_tot
            if yes_tot[k] < min_leaf or no_tot[k] < min_leaf:
                continue
            # both children: best of "leaf" or "split on t" (every t), with at least min_leaf per leaf
            t_idx = np.arange(n_s)
            # yes side: s&t, s&~t; no side: ~s&t, ~s&~t
            y_t = c[:, s, :]  # (k+1, S) in s and t
            y_nt = yes_tot[:, None] - y_t
            n_t = sing - y_t  # in t, not in s
            n_nt = no_tot[:, None] - n_t
            ok_yes = (y_t[k] >= min_leaf) & (y_nt[k] >= min_leaf)
            ok_no = (n_t[k] >= min_leaf) & (n_nt[k] >= min_leaf)
            leaf_yes = float(yes_tot[:k].max())
            leaf_no = float(no_tot[:k].max())
            split_yes = np.where(ok_yes, y_t[:k].max(axis=0) + y_nt[:k].max(axis=0), -np.inf)
            split_no = np.where(ok_no, n_t[:k].max(axis=0) + n_nt[:k].max(axis=0), -np.inf)
            ty = int(t_idx[split_yes.argmax()]) if np.isfinite(split_yes).any() else -1
            tn = int(t_idx[split_no.argmax()]) if np.isfinite(split_no).any() else -1
            use_y = ty >= 0 and split_yes[ty] > leaf_yes
            use_n = tn >= 0 and split_no[tn] > leaf_no
            value = (float(split_yes[ty]) if use_y else leaf_yes) + (
                float(split_no[tn]) if use_n else leaf_no
            )
            if value > best_value + 1e-12:
                best_value = value
                best_tree = _build(
                    s,
                    ty if use_y else None,
                    tn if use_n else None,
                    splits,
                    yes_tot,
                    no_tot,
                    y_t,
                    y_nt,
                    n_t,
                    n_nt,
                    k,
                )
    arms = best_tree.route(data.features.iloc[test].reset_index(drop=True))
    pi = np.zeros((len(test), data.n_arms), dtype=np.float64)
    pi[np.arange(len(test)), arms] = 1.0
    info = {
        "rows_used": int(n),
        "candidate_splits": len(splits),
        "min_leaf": int(min_leaf),
        "tree": best_tree.describe(data.arm_names),
        "train_net_value_per_customer": best_value / n,
        "no_split_net_value_per_customer": value0 / n,
    }
    return pi, info


def _build(
    s: int,
    ty: int | None,
    tn: int | None,
    splits: list[Split],
    yes_tot: FloatArray,
    no_tot: FloatArray,
    y_t: FloatArray,
    y_nt: FloatArray,
    n_t: FloatArray,
    n_nt: FloatArray,
    k: int,
) -> TreeNode:
    """The tree for root split `s` and child splits `ty` (yes side) and `tn` (no side), leaf arms by summed score."""

    def leaf(sums: FloatArray) -> TreeNode:
        return TreeNode(arm=int(sums[:k].argmax()))

    def child(t: int | None, tot: FloatArray, a: FloatArray, b: FloatArray) -> TreeNode:
        if t is None:
            return leaf(tot)
        return TreeNode(arm=-1, split=splits[t], yes=leaf(a[:, t]), no=leaf(b[:, t]))

    return TreeNode(
        arm=-1,
        split=splits[s],
        yes=child(ty, yes_tot, y_t, y_nt),
        no=child(tn, no_tot, n_t, n_nt),
    )
