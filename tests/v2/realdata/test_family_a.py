"""Tiny checks of the family-A real-data runners (scripts/v2_realdata/a_common.py).

Inline fixtures only: no download, no dataset folder, a few seconds in all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from autonomy.contracts import Channel, EventType
from autonomy.data.loaders import hillstrom_events
from autonomy.learning.observations import logged_sends
from scripts.v2_realdata import a_common as C  # noqa: N812


def _rct(n: int = 4000, effect: float = 0.1, seed: int = 3) -> C.Rct:
    """A two-arm 50/50 trial where treatment adds `effect` to the reward only when x == 1."""
    rng = np.random.default_rng(seed)
    x = rng.integers(0, 2, n)
    arm = rng.integers(0, 2, n)
    y = rng.normal(0.0, 0.2, n) + effect * arm * x
    return C.Rct(
        "fixture",
        np.array([f"c{i}" for i in range(n)]),
        arm,
        np.full(n, 0.5),
        ("none", "send"),
        pd.DataFrame({"x": x.astype(str)}),
        {"y": y},
    )


def test_paired_gain_matches_the_planted_effect() -> None:
    rct = _rct()
    rows = C.evaluate_policies(
        rct, rct.outcomes["y"], "y", {"all": C.fixed(rct.n, 2, 1), "none": C.fixed(rct.n, 2, 0)}
    )
    by = {r["policy"]: r for r in rows}
    gain = by["all"]["dr_vs_none"]
    # sending to all adds effect x share(x == 1), about 0.05, and the interval covers it
    assert gain["low"] < 0.05 < gain["high"]
    assert by["none"]["dr_vs_none"]["point"] == 0.0


def test_segment_rule_and_t_learner_find_the_responsive_half() -> None:
    rct = _rct(n=8000)
    tr, te = C.split_customers(rct.n, seed=0)
    train, test = rct.take(tr), rct.take(te)
    rule, _table = C.best_segment_rule(train, train.outcomes["y"] - 0.02 * train.arm, [["x"]])
    pi_rule = rule(test.logged(test.outcomes["y"], "y"))
    pi_up, _ = C.uplift_t_learner(
        train, test, train.outcomes["y"] - 0.02 * train.arm, C.Design(numeric=(), categorical=("x",))
    )
    responsive = test.features["x"].to_numpy() == "1"
    for pi in (pi_rule, pi_up):
        assert pi[responsive, 1].mean() > 0.9 and pi[~responsive, 1].mean() < 0.1


def test_hillstrom_loader_send_carries_no_logged_probability_for_the_warm_start() -> None:
    """Documents a finding: the loader writes attrs['probability'], the warm start reads
    attrs['logged_probability'], so the propensity is lost (the runner adds it itself)."""
    frame = pd.DataFrame(
        {
            "customer_id": ["a", "b"],
            "recency": [2, 5],
            "history_segment": ["1) $0 - $100"] * 2,
            "history": [50.0, 80.0],
            "mens": [1, 0],
            "womens": [0, 1],
            "zip_code": ["Urban", "Rural"],
            "newbie": [0, 1],
            "channel": ["Web", "Phone"],
            "segment": ["Mens E-Mail", "Womens E-Mail"],
            "visit": [1, 0],
            "conversion": [0, 0],
            "spend": [0.0, 0.0],
        }
    )
    loaded = hillstrom_events(frame, campaign_at=datetime(2008, 3, 3, 14, tzinfo=UTC))
    by: dict[str, list] = {}
    for e in loaded.events:
        by.setdefault(e.customer_id, []).append(e)
    sends = logged_sends(by, {})
    assert len(sends) == 2
    assert all(s.propensity is None for s in sends)
    fixed = {
        c: [
            (
                e.model_copy(update={"attrs": {**e.attrs, "logged_probability": 1 / 3}})
                if e.type is EventType.SEND
                else e
            )
            for e in evs
        ]
        for c, evs in by.items()
    }
    assert all(abs((s.propensity or 0) - 1 / 3) < 1e-12 for s in logged_sends(fixed, {}))
    assert all(s.channel is Channel.EMAIL for s in sends)
    assert loaded.events[0].ts < datetime(2008, 3, 3, 14, tzinfo=UTC) + timedelta(days=5)
