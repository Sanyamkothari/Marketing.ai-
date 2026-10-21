"""The Scientist's campaign type, its adapter from the shop's history, and the package-wide guards."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import Channel
from autonomy.scientist import Campaign, campaign_from_history
from tests.v2.realfix.test_uplift_support import NOW, campaign, content

REPO = Path(__file__).resolve().parents[3]
SCIENTIST_FILES = [
    *sorted((REPO / "autonomy" / "scientist").glob("*.py")),
    REPO / "scripts" / "v2_realbench" / "scientist.py",
]


def make_campaign(
    n: int = 6000,
    *,
    seed: int = 0,
    targeting: bool = True,
    blanket_gain: float = 0.0,
    arm_probs: tuple[float, float, float] = (0.4, 0.3, 0.3),
) -> Campaign:
    """A randomised three-arm campaign (none, A, B) with known truth.

    Everyone converts with chance 0.10 (0.20 if x2 == 'a'); a conversion is worth 10 and a message
    costs 1. Arm A lifts the chance by 0.15 where x1 > 0.5 when `targeting` (so sending A pays +0.5
    there and loses 1 elsewhere), plus `blanket_gain` for everybody; arm B adds 0.02 to everyone
    (never pays). x3 is noise."""
    rng = np.random.default_rng(seed)
    x1 = rng.uniform(size=n)
    x2 = rng.choice(["a", "b"], size=n)
    x3 = rng.normal(size=n)
    arm = rng.choice(3, size=n, p=list(arm_probs))
    prop = np.asarray(arm_probs)[arm]
    base = 0.10 + 0.10 * (x2 == "a")
    lift_a = (np.where(x1 > 0.5, 0.15, 0.0) if targeting else 0.0 * x1) + blanket_gain
    p = base + np.where(arm == 1, lift_a, 0.0) + np.where(arm == 2, 0.02, 0.0)
    conv = (rng.uniform(size=n) < p).astype(np.float64)
    return Campaign(
        customer_ids=tuple(f"c{i:06d}" for i in range(n)),
        arm=arm.astype(np.int64),
        propensity=prop.astype(np.float64),
        arm_names=("no message", "A", "B"),
        arm_cost=(0.0, 1.0, 1.0),
        converted=conv,
        margin=10.0 * conv,
        features=pd.DataFrame({"x1": x1, "x2": x2, "x3": x3}),
    )


def test_campaign_reward_take_and_counts() -> None:
    c = make_campaign(500)
    assert c.n == 500 and c.n_arms == 3 and c.identified
    assert np.allclose(c.reward(), c.margin - np.asarray([0.0, 1.0, 1.0])[c.arm])
    sub = c.take([3, 1, 2])
    assert sub.customer_ids == (c.customer_ids[3], c.customer_ids[1], c.customer_ids[2])
    assert sub.features.iloc[0]["x1"] == c.features.iloc[3]["x1"]
    assert c.arm_counts().sum() == 500
    assert c.margin_per_conversion() == pytest.approx(10.0)
    logged = c.logged()
    assert logged.n_rows == 500 and logged.action_names == ("no message", "A", "B")


@pytest.mark.parametrize(
    "change",
    [
        {"arm_cost": (1.0, 1.0, 1.0)},  # arm 0 must be free
        {"arm_cost": (0.0, -1.0, 1.0)},
        {"arm_names": ("only",), "arm_cost": (0.0,)},
    ],
)
def test_campaign_refuses_malformed_arms(change: dict[str, object]) -> None:
    c = make_campaign(50)
    fields = {
        "customer_ids": c.customer_ids,
        "arm": c.arm,
        "propensity": c.propensity,
        "arm_names": c.arm_names,
        "arm_cost": c.arm_cost,
        "converted": c.converted,
        "margin": c.margin,
        "features": c.features,
    }
    fields.update(change)
    with pytest.raises(ValueError):
        Campaign(**fields)


def test_campaign_refuses_bad_rows() -> None:
    c = make_campaign(50)
    base = {
        "customer_ids": c.customer_ids,
        "arm": c.arm,
        "propensity": c.propensity,
        "arm_names": c.arm_names,
        "arm_cost": c.arm_cost,
        "converted": c.converted,
        "margin": c.margin,
        "features": c.features,
    }
    with pytest.raises(ValueError, match="once"):
        Campaign(**{**base, "customer_ids": ("x",) * 50})
    with pytest.raises(ValueError, match="propensity"):
        Campaign(**{**base, "propensity": np.zeros(50)})
    with pytest.raises(ValueError, match="missing"):
        Campaign(**{**base, "margin": np.full(50, np.nan)})
    with pytest.raises(ValueError, match="index"):
        Campaign(**{**base, "arm": np.full(50, 7, dtype=np.int64)})
    unlogged = Campaign(**{**base, "propensity": None})
    assert not unlogged.identified
    with pytest.raises(ValueError, match="propensit"):
        unlogged.logged()


def test_campaign_from_history_reads_the_latest_closed_campaign() -> None:
    events, states = campaign(n=1500, lift_a=0.15, base=0.05)
    by_id = {c.content_id: c for c in content()}
    c, notes = campaign_from_history(events, states, by_id, NOW, costs={Channel.EMAIL: 0.5}, margin_rate=0.4)
    assert c is not None, notes
    assert c.identified and c.randomised
    assert c.arm_names[0] == "no message" and c.n_arms == 2
    assert c.arm_cost == (0.0, 0.5)
    counts = c.arm_counts()
    assert counts[0] > 100 and counts[1] > 100 and counts.sum() == c.n == 1500
    assert "tier" in c.features.columns and "age" in c.features.columns
    assert pd.api.types.is_numeric_dtype(c.features["age"])  # numbers in text become numbers
    assert c.propensity is not None and np.allclose(c.propensity, 0.5)
    treated_conv = c.converted[c.arm == 1].mean()
    control_conv = c.converted[c.arm == 0].mean()
    assert treated_conv > control_conv  # the planted lift is visible
    assert (c.margin[c.converted == 0] == 0).all()


def test_campaign_from_history_says_why_when_there_is_none() -> None:
    events, states = campaign(n=1500, logged=False)  # sends that logged no probability
    c, notes = campaign_from_history(
        events, states, {x.content_id: x for x in content()}, NOW, costs={Channel.EMAIL: 0.5}
    )
    assert c is None and notes and "probab" in notes[0]


# --- guards over the whole package ----------------------------------------------------------------


def test_no_dataset_names_in_the_scientist_files() -> None:
    pattern = re.compile(r"hillstrom|x5|retailhero|lenta|starbucks|criteo|megafon", re.IGNORECASE)
    hits = [
        f"{f.name}:{i}: {line.strip()}"
        for f in SCIENTIST_FILES
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, hits


def test_nothing_in_the_scientist_runs_text_from_a_model() -> None:
    pattern = re.compile(r"\b(eval|exec|compile)\(|__import__|importlib|pickle|subprocess|os\.system")
    hits = [
        f"{f.name}:{i}: {line.strip()}"
        for f in SCIENTIST_FILES
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, hits
