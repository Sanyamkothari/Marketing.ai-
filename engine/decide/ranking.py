"""Which ranking a contact list uses when the uplift model does not beat risk ranking (Plan J M96, J5).

An uplift model is worth using only where it ranks customers better than plain risk does. Its
training run measures that on its own hold-out (`UpliftEvaluation.baseline_comparison`, a paired
bootstrap of the AUUC difference against the use case's approved propensity model, or against the
model's own `p_control` when there is none). This module turns that verdict into the scoring run's
ranking:

* **The check passed** (`beats_risk`): the list is ranked by predicted uplift, as before.
* **The check failed and the use case has an approved propensity model**: the list is ranked by
  that model's score, with **the same number of contacts** the uplift policy chose (equal budget),
  and the run says so with `UPLIFT_NOT_BETTER_THAN_RISK` and a plain reason.
* **The check failed and there is no approved propensity model** (or it cannot score the file):
  the list keeps the uplift ranking, and the run says plainly that the model does not beat risk
  ranking and that there was nothing to fall back to. No other fallback is invented.
* **The check was never computed** (a model trained before M96): nothing here runs; the run is
  exactly what it was.

**Where the choice is made (DEC sub-decision).** In the uplift scoring flow's actions stage, from the
*training* run's stored verdict: a scoring run never re-measures the model, and an evaluation without
`baseline_comparison` changes nothing. So every run of a model trained before M96 is byte-identical,
and the frozen champion rule (`engine/registry.py`) is not consulted or changed.

**The last approved propensity model** (:func:`last_approved_propensity`) is the use case's most
recently promoted classification model: a version whose metric is a binary-classification metric of
the catalog and that has been put in use (`promoted_at` set: by the champion rule, an Approver or a
deliberate promotion), newest `promoted_at` first. It stays usable after an uplift model takes the
champion slot and archives it.

**The re-ranked list** (:func:`rerank_by_risk`). Phase 1's suppression and control group are kept
exactly as the uplift actions stage drew them. Among the eligible rows (neither suppressed nor held
out) that are not predicted sleeping dogs - a sleeping dog is never treated, whatever ranks it - the
top `contacts` by the propensity score are `Treat`, ties broken by the run's per-customer hash.
`intended_treatment` is the selected rows plus the held-out rows the same ranking would have selected,
so the campaign is still measured inside one population.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from engine.uplift.contracts import UPLIFT_NOT_BETTER_THAN_RISK, RankingChoice
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import ModelVersion
    from engine.registry import ModelRegistry
    from engine.storage import Storage
    from engine.uplift.contracts import BaselineComparison

    FloatArray = npt.NDArray[np.float64]

__all__ = [
    "RankingDecision",
    "cannot_score",
    "decide_ranking",
    "last_approved_propensity",
    "ranking_choice",
    "rerank_by_risk",
    "score_rows",
]

_LOGGER = get_logger(__name__)

_SLEEPING_DOG: Final[str] = "sleeping_dog"


@dataclass(frozen=True)
class RankingDecision:
    """What orders the contact list, and the sentence that says why."""

    ranking: Literal["uplift", "propensity_model"]
    code: str | None
    beats_risk: bool
    propensity_model_id: str | None
    reason: str
    check: str
    """The training run's beats-risk sentence the reason starts from."""

    @property
    def falls_back(self) -> bool:
        return self.ranking == "propensity_model"


def last_approved_propensity(registry: ModelRegistry, use_case_id: str) -> ModelVersion | None:
    """The use case's most recently promoted binary-classification model, or None (module docstring)."""
    from engine.registry import aware_utc

    metrics = _classification_metrics()
    approved = [
        version
        for version in registry.list_versions(use_case_id)
        if version.metric.value in metrics and version.promoted_at is not None
    ]
    if not approved:
        return None

    def newest(version: ModelVersion) -> tuple[datetime, int]:
        assert version.promoted_at is not None
        return aware_utc(version.promoted_at), version.version

    return max(approved, key=newest)


def _classification_metrics() -> frozenset[str]:
    """The catalog's binary-classification metrics: what a propensity model is scored on."""
    from engine.config import ProblemType, get_catalog

    return frozenset(
        metric.value
        for metric, spec in get_catalog().metrics.items()
        if ProblemType.BINARY_CLASSIFICATION in spec.problem_types
    )


def decide_ranking(
    comparison: BaselineComparison | None, propensity: ModelVersion | None
) -> RankingDecision | None:
    """The ranking for a scoring run, from the training run's verdict; None when it was never checked."""
    if comparison is None:
        return None
    if comparison.beats_risk:
        return RankingDecision(
            ranking="uplift",
            code=None,
            beats_risk=True,
            propensity_model_id=None,
            reason=f"Ranked by predicted uplift. {comparison.summary}",
            check=comparison.summary,
        )
    if propensity is not None:
        return RankingDecision(
            ranking="propensity_model",
            code=UPLIFT_NOT_BETTER_THAN_RISK,
            beats_risk=False,
            propensity_model_id=propensity.model_id,
            reason=(
                f"{comparison.summary} So this contact list is ranked by the approved propensity model "
                f"({propensity.model_display_name}, version {propensity.version}), contacting the same "
                f"number of customers the uplift model chose."
            ),
            check=comparison.summary,
        )
    return RankingDecision(
        ranking="uplift",
        code=UPLIFT_NOT_BETTER_THAN_RISK,
        beats_risk=False,
        propensity_model_id=None,
        reason=(
            f"{comparison.summary} This use case has no approved propensity model to rank by instead, "
            f"so the list keeps the uplift ranking: treat its order with caution."
        ),
        check=comparison.summary,
    )


def cannot_score(decision: RankingDecision, propensity: ModelVersion, why: str) -> RankingDecision:
    """The fallback model could not score this file: keep the uplift ranking and say why."""
    return replace(
        decision,
        ranking="uplift",
        propensity_model_id=None,
        reason=(
            f"{decision.check} The approved propensity model "
            f"({propensity.model_display_name}, version {propensity.version}) could not score this file "
            f"({why}), so the list keeps the uplift ranking: treat its order with caution."
        ),
    )


def score_rows(
    frame: pd.DataFrame,
    version: ModelVersion,
    *,
    config: UseCaseConfig,
    storage: Storage,
    registry: ModelRegistry,
    run_id: str,
) -> FloatArray:
    """The propensity model's calibrated score for every row of `frame`, in `frame`'s row order.

    Phase 1's own `engine.stages.score.predict` (the version's recorded transforms, its threshold and
    calibrator), so the score is the one a propensity scoring run of the same rows would write. Raises
    whatever `predict` raises, or `ValueError` when a row comes back without a score.
    """
    import numpy as np

    from engine.stages.score import predict

    result = predict(
        frame, config, run_id=run_id, storage=storage, registry=registry, model_version_id=version.model_id
    )
    scores = result.scores.reindex(frame.index)
    values = np.asarray(scores, dtype=np.float64)
    if values.shape[0] != len(frame.index) or not np.isfinite(values).all():
        raise ValueError("the model did not return a score for every row")
    return values


def rerank_by_risk(
    acted: pd.DataFrame,
    risk: npt.ArrayLike,
    *,
    contacts: int,
    tiebreak: npt.ArrayLike,
) -> pd.DataFrame:
    """The uplift actions stage's output with `Treat` and `intended_treatment` chosen by `risk`.

    Suppression, the control group, segments and bands are kept. Returns a copy; raises
    `RuntimeError` if a sleeping dog would be treated or intended (the uplift stage's own guard).
    """
    import numpy as np

    from engine.stages.actions import ACTION_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
    from engine.uplift.actions import (
        INTENDED_TREATMENT_COLUMN,
        OVER_BUDGET_ACTION,
        SEGMENT_COLUMN,
        TREAT_ACTION,
    )
    from engine.uplift.policy import rank_positions

    scores = np.asarray(risk, dtype=np.float64)
    rows = len(acted.index)
    if scores.shape[0] != rows:
        raise ValueError(f"risk has {scores.shape[0]} values for {rows} rows.")
    segments = acted[SEGMENT_COLUMN].astype("object").to_numpy()
    sleeping = segments == _SLEEPING_DOG
    control = acted[CONTROL_GROUP_COLUMN].to_numpy(dtype=bool)
    suppressed = acted[SUPPRESSED_REASON_COLUMN].notna().to_numpy(dtype=bool)
    eligible = ~suppressed & ~control
    positions = rank_positions(scores, np.asarray(tiebreak))
    candidates = np.flatnonzero(eligible & ~sleeping)
    chosen = candidates[np.argsort(positions[candidates], kind="mergesort")][: max(0, contacts)]
    selected = np.zeros(rows, dtype=bool)
    selected[chosen] = True

    actions = acted[ACTION_COLUMN].astype("object").to_numpy().copy()
    # The uplift policy treats persuadables only, so a row it treated that risk does not choose is a
    # persuadable passed over for the budget.
    actions[eligible & (actions == TREAT_ACTION)] = OVER_BUDGET_ACTION
    actions[selected] = TREAT_ACTION

    intended = selected.copy()
    if selected.any():
        intended |= control & ~sleeping & (positions <= int(positions[selected].max()))
    if bool((sleeping & ((actions == TREAT_ACTION) | intended)).any()):
        raise RuntimeError("A sleeping dog was marked for treatment; refusing to export the actions.")
    result = acted.copy()
    result[ACTION_COLUMN] = actions
    result[INTENDED_TREATMENT_COLUMN] = intended
    result.attrs = dict(acted.attrs)
    _LOGGER.info(
        "rerank_by_risk treat_rows=%d intended_rows=%d contacts=%d",
        int(selected.sum()),
        int(intended.sum()),
        contacts,
    )
    return result


def ranking_choice(
    decision: RankingDecision, *, run_id: str, model_version_id: str, contacts: int, now: datetime
) -> RankingChoice:
    """`ranking_choice.json` for a scoring run."""
    return RankingChoice(
        run_id=run_id,
        model_version_id=model_version_id,
        ranking=decision.ranking,
        code=decision.code,
        beats_risk=decision.beats_risk,
        propensity_model_id=decision.propensity_model_id,
        contacts=contacts,
        reason=decision.reason,
        computed_at=now,
    )
