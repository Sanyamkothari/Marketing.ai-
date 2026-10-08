"""`engine.holdout.assign.selection_masks` and `treated_flags` (Plan J M98).

M98's treat list reuses M92's definition of "selected before the holdout" and of the logged action instead of
copying them, so the two are public. `assignment_frame` calls them (its own tests, unchanged, pin its output);
these tests pin the functions on small frames.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.config import load_use_case
from engine.holdout.assign import selection_masks, treated_flags

CONFIG = load_use_case("win-back-campaign")  # bands High, Medium, Low (the floor)


def test_a_propensity_run_selects_every_band_but_the_lowest_and_has_no_sleeping_dogs() -> None:
    banded = pd.DataFrame({"band": ["High", "Medium", "Low", "Low", "High"], "action": ["a"] * 5})
    selected, sleeping = selection_masks(banded, CONFIG)
    assert selected.tolist() == [True, True, False, False, True]
    assert sleeping.tolist() == [False] * 5


def test_an_uplift_run_selects_the_intended_treatment_and_marks_sleeping_dogs() -> None:
    banded = pd.DataFrame(
        {
            "segment": ["persuadable", "sleeping_dog", "persuadable", "lost_cause"],
            "intended_treatment": [
                True,
                False,
                True,
                False,
            ],  # the third was held out but would have been treated
            "action": ["Treat", "Never treat (contact makes it worse)", "Control (hold out)", "Hold"],
        }
    )
    selected, sleeping = selection_masks(banded, CONFIG)
    assert selected.tolist() == [True, False, True, False]
    assert sleeping.tolist() == [False, True, False, False]


def test_without_intended_treatment_an_uplift_run_selects_its_treat_action() -> None:
    banded = pd.DataFrame(
        {
            "segment": ["persuadable", "persuadable", "sleeping_dog"],
            "action": ["Treat", "Don't treat (over budget)", "Never treat (contact makes it worse)"],
        }
    )
    selected, sleeping = selection_masks(banded, CONFIG)
    assert selected.tolist() == [True, False, False] and sleeping.tolist() == [False, False, True]


def test_treated_is_eligible_selected_and_not_in_control_or_explored() -> None:
    eligible = np.array([True, True, True, False, True])
    selected = np.array([True, True, False, True, False])
    control = np.array([False, True, False, False, False])
    explore = np.array([False, False, False, False, True])
    treated = treated_flags(eligible, selected, control, explore)
    assert treated.tolist() == [True, False, False, False, True]
