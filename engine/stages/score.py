"""Predict stage (M4): batch scoring with a stored predictor and the drift report."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import DriftBaseline, DriftReport, DriftStatus, FeatureDrift
    from engine.storage import Storage

PSI_EPSILON: Final[float] = 1e-6
"""The zero-count floor of the PSI sum.

A bin that is empty on one side makes the log term infinite, so an empty share is read as this
epsilon instead. One part in a million is two to three orders of magnitude below the smallest
share a real file can produce (plan §4.1 asks for at least 1,000 rows, so one row is 1e-3), which
keeps the substitution invisible for bins that are merely small while an emptied bin still scores
a large, finite and reproducible contribution. The floor is applied to both sides, so PSI stays
symmetric, and the shares are not renormalised afterwards: over at most 51 slots the distortion
stays below 1e-4 of the total mass.
"""

PSI_DECIMALS: Final[int] = 4
RATE_DECIMALS: Final[int] = 6


def predict(
    predictor_key: str,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    storage: Storage,
) -> pd.Series:
    """Calibrated scores for every row of `frame`, from the stored predictor."""
    raise NotImplementedError("M4")


def compute_drift(
    baseline: DriftBaseline,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
) -> DriftReport:
    """Population stability index per feature against the training baseline (plan §6.3, predict).

    Every feature is compared **in the baseline's own bins and levels**; the scored file is never
    re-binned, because two files binned independently are not comparable. The projection lives in
    `engine.stages.register`, next to the code that wrote the baseline.

    PSI is `sum((actual - expected) * ln(actual / expected))` over those bins, with `PSI_EPSILON`
    standing in for an empty share on either side. The edge cases, all of them deliberate:

    - **a category unseen at training time** falls into the baseline's `__other__` bucket when the
      column had one, and otherwise into a trailing slot whose expected share is zero - so unseen
      mass scores as drift instead of being dropped;
    - **a numeric value outside every baseline bin** is clamped into the outermost bin, whose tails
      are read as open-ended; a *constant* baseline column has a zero-width bin instead, and values
      that differ from the constant land in the trailing slot;
    - **an empty bin on either side** is floored at `PSI_EPSILON`;
    - **a feature missing from the scored frame** is read as a feature that is entirely null: no
      mass in any bin, a current null rate of one, and therefore a large PSI. Reporting which
      columns are missing is the schema check's job (plan §4.4), not this one's;
    - **a feature present but all-null** behaves identically, and so does an empty frame;
    - **a baseline feature with no distribution at all** (a column that was already entirely null
      at training time) has nothing to compare, so its PSI is zero and it is stable.

    A feature is `drifted` at or above `monitoring.drift_psi_threshold`, `watch` from half that
    threshold, and `stable` below it; the run takes the verdict of its worst feature, and the
    summary is the Output page's line, for example `PSI 0.11, watch`. A run with any drifted
    feature is logged at WARNING.
    """
    from engine.contracts import DriftReport, FeatureDrift
    from engine.stages.register import _feature_shares
    from engine.utils.logging import get_logger
    from engine.utils.time import utc_now

    threshold = config.monitoring.drift_psi_threshold
    drifts: list[FeatureDrift] = []
    for feature in baseline.features:
        expected, actual, null_rate_current = _feature_shares(feature, frame)
        psi = round(_psi(expected, actual), PSI_DECIMALS)
        drifts.append(
            FeatureDrift(
                feature=feature.feature,
                psi=psi,
                status=_drift_status(psi, threshold),
                null_rate_baseline=round(feature.null_rate, RATE_DECIMALS),
                null_rate_current=round(null_rate_current, RATE_DECIMALS),
            )
        )
    drifts.sort(key=lambda drift: (-drift.psi, drift.feature))
    max_psi = max((drift.psi for drift in drifts), default=0.0)
    status = _drift_status(max_psi, threshold)
    drifted = tuple(drift.feature for drift in drifts if drift.psi >= threshold)
    if drifted:
        get_logger(__name__).warning(
            "stage=predict drift=exceeded features=%d max_psi=%.4f threshold=%.2f",
            len(drifted),
            max_psi,
            threshold,
        )
    return DriftReport(
        run_id=run_id,
        baseline_run_id=baseline.run_id,
        model_version_id=baseline.model_version_id,
        threshold=threshold,
        features=tuple(drifts),
        max_psi=max_psi,
        drifted_features=drifted,
        status=status,
        summary=f"PSI {max_psi:.2f}, {status.value}",
        computed_at=utc_now(),
    )


def _psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """The population stability index between two share vectors binned the same way.

    `sum((a - e) * ln(a / e))` with every share floored at `PSI_EPSILON`. Every term is
    non-negative, so the sum is too, and swapping the two vectors leaves it unchanged.
    """
    import math

    total = 0.0
    for expected_share, actual_share in zip(expected, actual, strict=True):
        floored_expected = max(expected_share, PSI_EPSILON)
        floored_actual = max(actual_share, PSI_EPSILON)
        total += (floored_actual - floored_expected) * math.log(floored_actual / floored_expected)
    return max(total, 0.0)


def _drift_status(psi: float, threshold: float) -> DriftStatus:
    """`drifted` at or above the threshold, `watch` from half of it, `stable` below."""
    from engine.contracts import DriftStatus

    if psi >= threshold:
        return DriftStatus.DRIFTED
    if psi >= threshold / 2.0:
        return DriftStatus.WATCH
    return DriftStatus.STABLE
