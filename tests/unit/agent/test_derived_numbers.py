"""A number the model works out from two the tools gave (a ratio, a percentage, a sum, a difference), a list's
"Step N" and a count of this turn's own actions are grounded; an invented number still is not."""

from __future__ import annotations

import pytest

from engine.agent.grounding import grounded_numbers, ungrounded_numbers

GROUNDED = grounded_numbers([{"customers": 960, "positives": 412, "rows": 1500, "late": 37}])


@pytest.mark.parametrize(
    "text",
    [
        "About 43% of customers did (412 of 960).",  # a ratio shown as a percent
        "0.43 of them did.",  # the same ratio as a fraction
        "That is 1,372 together.",  # 960 + 412
        "That leaves 548 who did not.",  # 960 - 412
        "Step 3 is to check the dates, and step 12 is the rest.",
    ],
)
def test_a_number_worked_out_from_two_grounded_ones_is_accepted(text: str) -> None:
    assert ungrounded_numbers(text, GROUNDED) == []


@pytest.mark.parametrize("text", ["777 customers did.", "It is 61.7% of them.", "About 5,432 rows."])
def test_an_invented_number_is_still_refused(text: str) -> None:
    assert ungrounded_numbers(text, GROUNDED)


def test_a_percentage_that_is_not_the_ratio_is_refused() -> None:
    assert ungrounded_numbers("About 60% of customers did (412 of 960).", GROUNDED) == ["60%"]
