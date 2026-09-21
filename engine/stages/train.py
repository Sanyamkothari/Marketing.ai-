"""Train stage (M3): AutoGluon fitting and the leaderboard.

`autogluon` is imported inside `train()`, never at module level, so importing the engine stays fast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import BestModel, Leaderboard
    from engine.jobs import CancelToken
    from engine.storage import Storage


def autogluon_fit_kwargs(config: UseCaseConfig) -> dict[str, Any]:
    """Translate the model-search config into `TabularPredictor.fit` keyword arguments (pure)."""
    raise NotImplementedError("M3")


def train(
    parts: Mapping[str, pd.DataFrame],
    config: UseCaseConfig,
    *,
    run_id: str,
    target: str,
    storage: Storage,
    cancel: CancelToken,
) -> tuple[Leaderboard, BestModel]:
    """Fit the candidate models, save the predictor and return the leaderboard and the best model."""
    raise NotImplementedError("M3")
