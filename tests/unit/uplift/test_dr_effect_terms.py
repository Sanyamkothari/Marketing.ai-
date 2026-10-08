"""Plan J M96: `ope.dr_effect_terms`, the per-row doubly robust effect scores.

For any policy, `evaluate_policy`'s DR value minus its `treat_none_value` must be exactly the mean of
`policy x terms`; that identity is what lets the equal-budget comparison pair two policies row by row
while still going through `evaluate_policy`.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.uplift.ope import dr_effect_terms, evaluate_policy


def _logged(rows: int = 500, seed: int = 4) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    propensity = rng.uniform(0.1, 0.9, rows)
    t = (rng.random(rows) < propensity).astype(int)
    y = (rng.random(rows) < 0.3).astype(int)
    return {
        "t": t,
        "y": y,
        "p_treated": rng.uniform(0.1, 0.6, rows),
        "p_control": rng.uniform(0.1, 0.6, rows),
        "propensity": propensity,
    }


@pytest.mark.parametrize("share", [0.0, 0.2, 0.7, 1.0])
def test_dr_minus_treat_none_is_the_mean_of_policy_times_terms(share: float) -> None:
    data = _logged()
    rows = data["t"].shape[0]
    policy = (np.arange(rows) < round(share * rows)).astype(float)
    report = evaluate_policy(
        data["t"],
        data["y"],
        policy,
        p_treated=data["p_treated"],
        p_control=data["p_control"],
        propensity=data["propensity"],
        run_id="r",
        description="d",
        causal=True,
    )
    terms = dr_effect_terms(
        data["t"],
        data["y"],
        p_treated=data["p_treated"],
        p_control=data["p_control"],
        propensity=data["propensity"],
    )
    dr = next(e.value.value for e in report.estimates if e.method == "dr")
    assert dr - report.treat_none_value.value == pytest.approx(float((policy * terms).mean()), abs=1e-12)


def test_the_terms_refuse_a_propensity_of_zero_or_one() -> None:
    data = _logged(rows=10)
    data["propensity"][0] = 1.0
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        dr_effect_terms(
            data["t"],
            data["y"],
            p_treated=data["p_treated"],
            p_control=data["p_control"],
            propensity=data["propensity"],
        )
