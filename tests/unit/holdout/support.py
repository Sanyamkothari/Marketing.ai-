"""Shared helpers of the holdout tests (Plan J M92): configurations, frames and the binomial band."""

from __future__ import annotations

import copy
import math
from statistics import NormalDist
from typing import Any, Final

import numpy as np
import pandas as pd

from engine.config import UseCaseConfig, load_use_case_document
from engine.holdout.assign import ActiveHoldout

SALT: Final[str] = "holdout-test-salt-0001"
"""A test salt (at least 16 characters, as the setting requires)."""

OTHER_SALT: Final[str] = "holdout-test-salt-0002"

CONSENT: Final[str] = "consent"


def use_case(
    use_case_id: str = "targeted-advertisement",
    *,
    scope: str = "run",
    fraction: float | None = None,
    explore: float = 0.0,
    control_fraction: float = 0.10,
    policy: dict[str, Any] | None = None,
) -> UseCaseConfig:
    """A shipped use case with the template emptied, one consent rule and the holdout as asked."""
    document = copy.deepcopy(load_use_case_document(use_case_id))
    document["template"] = {"columns": []}
    document.setdefault("governance", {})["consent_column"] = CONSENT
    suppression = document.setdefault("actions", {}).setdefault("suppression", {})
    suppression["suppress_opted_out"] = False
    suppression["suppress_recently_contacted"] = False
    document["actions"]["score_field"] = "propensity"
    document["actions"]["control_group_fraction"] = control_fraction
    document["actions"]["holdout"] = {"scope": scope, **({} if fraction is None else {"fraction": fraction})}
    document["actions"]["explore_fraction"] = explore
    if policy is not None:
        document["uplift"] = {"policy": policy}
    return UseCaseConfig.model_validate(document)


def active(config: UseCaseConfig, salt: str = SALT) -> ActiveHoldout:
    """The holdout `resolve_holdout` would make active for `config` (no ledger involved)."""
    holdout = config.actions.holdout
    assert holdout.fraction is not None
    key = config.id if holdout.scope == "use_case" else "universal"
    return ActiveHoldout(salt=salt, scope_key=key, fraction=holdout.fraction)


def keys(count: int, prefix: str = "C-") -> list[str]:
    return [f"{prefix}{index:07d}" for index in range(count)]


def scored(ids: list[str], *, seed: int = 0, suppressed_share: float = 0.0) -> pd.DataFrame:
    """A propensity-scored frame: key, score over all bands, and a consent column."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "customer_id": ids,
            "propensity": np.round(rng.uniform(0.0, 1.0, size=len(ids)), 4),
            CONSENT: rng.uniform(0.0, 1.0, size=len(ids)) >= suppressed_share,
        }
    )


def binomial_band(n: int, p: float, level: float = 0.999) -> tuple[float, float]:
    """The two-sided `level` normal band of a Binomial(n, p) count."""
    z = NormalDist().inv_cdf(0.5 + level / 2.0)
    sd = math.sqrt(n * p * (1.0 - p))
    return n * p - z * sd, n * p + z * sd
