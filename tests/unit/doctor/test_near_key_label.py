"""The label of an offered customer column says in plain words how it repeats and how much of it is blank."""

from __future__ import annotations

import pytest

from engine.doctor.tools_facts import near_key_label


def test_the_repeat_is_rows_per_customer_and_a_third_blank_is_said_as_a_third() -> None:
    key = {"column": "CustomerID", "rows_per_key": 39.7, "null_rate": 0.33}
    assert near_key_label(key, events=False) == "CustomerID (about 40 rows per customer; a third are blank)"
    assert near_key_label(key, events=True).endswith(
        "a third are blank, those rows cannot be tied to a person)"
    )


@pytest.mark.parametrize(
    ("blank", "said"),
    [
        (0.0, ""),
        (0.003, ""),
        (0.5, "; half are blank"),
        (0.25, "; a quarter are blank"),
        (0.12, "; 12% are blank"),
    ],
)
def test_a_share_is_in_words_when_it_is_close_to_one_and_a_percent_otherwise(blank: float, said: str) -> None:
    key = {"column": "id", "rows_per_key": 1.0, "null_rate": blank}
    assert near_key_label(key, events=False) == f"id (about 1 row per customer{said})"
