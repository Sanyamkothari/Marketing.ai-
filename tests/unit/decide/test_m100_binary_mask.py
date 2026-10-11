"""The M100 byte-identity mask hides how long this machine took, and nothing else.

`uplift_evaluation.json` carries M96's fold-refit cost estimate, measured from the run's own fit time,
as a number and as a phrase. The phrase's unit follows the number (seconds under 90, minutes from 90
on), so on a loaded machine the same run says "about 2 minutes" where an idle one says "about 4
seconds". Both must mask to the same bytes, or the identity test fails whenever the machine is busy.
"""

from __future__ import annotations

import pytest

from engine.contracts import dump_artefact
from engine.measurement.compare import OFF_REASON, fold_auuc_not_computed
from tests.fixtures.decide.m100_binary import SPOKEN_DURATION_MASK, normalise


def _estimate(seconds: float) -> str:
    return normalise(
        dump_artefact(fold_auuc_not_computed(folds=5, reason=OFF_REASON, estimated_refit_seconds=seconds))
    )


@pytest.mark.parametrize("slow", [89.4, 99.6, 3_600.0, 86_400.0])
def test_a_slower_fit_masks_to_the_same_bytes_as_an_idle_one(slow: float) -> None:
    assert _estimate(slow) == _estimate(3.8)


def test_the_mask_keeps_the_spelling_the_golden_was_recorded_with() -> None:
    masked = _estimate(3.8)
    assert f"{SPOKEN_DURATION_MASK} on this data." in masked
    assert '"estimated_refit_seconds": <seconds>' in masked


def test_the_mask_touches_nothing_but_durations() -> None:
    assert normalise('{"folds": 5, "reason": "about 5 folds"}') == '{"folds": 5, "reason": "about 5 folds"}'
    assert normalise("about 2 minutes") == normalise("about 1,200 seconds") == SPOKEN_DURATION_MASK
