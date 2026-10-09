"""The programme split (Plan J M103): `engine.measurement.programme`.

Membership of the universal holdout is the salted rule every scoring run used (`member_flags`); the programme
readout must split an uploaded customer base by it, customer for customer, and a two-column key by its
entity column alone.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.holdout.assign import member_flags
from engine.keys import key_text
from engine.measurement.programme import ProgrammePeriod, period_window_days, programme_assignment

SALT = "programme-unit-salt-0001"


def customers(n: int) -> pd.DataFrame:
    return pd.DataFrame({"id": [f"C{i:06d}" for i in range(n)]})


def test_the_split_is_the_holdouts_own_rule_customer_for_customer() -> None:
    frame = customers(20_000)
    assignment = programme_assignment(frame, primary_key="id", salt=SALT, fraction=0.1)
    expected = member_flags(frame["id"].tolist(), salt=SALT, scope_key="universal", fraction=0.1)
    assert (assignment["arm"] == "holdout").to_numpy().tolist() == expected.tolist()
    assert assignment["intended"].all() and assignment["id"].tolist() == frame["id"].tolist()
    assert list(assignment.columns) == ["id", "arm", "intended", "band"]
    assert set(assignment["arm"]) == {"holdout", "treated"}
    share = (assignment["arm"] == "holdout").mean()
    assert abs(share - 0.1) < 0.011, "binomial: within 3.3 standard errors of the fraction"


def test_a_smaller_fraction_is_inside_a_larger_one_and_another_salt_is_another_split() -> None:
    frame = customers(5_000)
    small = programme_assignment(frame, primary_key="id", salt=SALT, fraction=0.05)["arm"] == "holdout"
    large = programme_assignment(frame, primary_key="id", salt=SALT, fraction=0.10)["arm"] == "holdout"
    assert bool((large | ~small).all()), "every member at 5% is a member at 10%"
    other = programme_assignment(frame, primary_key="id", salt=SALT + "x", fraction=0.10)["arm"] == "holdout"
    assert not other.equals(large)


def test_a_customer_is_a_member_whatever_the_file_it_comes_in_or_the_order() -> None:
    frame = customers(1_000)
    shuffled = frame.sample(frac=1.0, random_state=3).reset_index(drop=True)
    first = programme_assignment(frame, primary_key="id", salt=SALT, fraction=0.2).set_index("id")["arm"]
    second = programme_assignment(shuffled, primary_key="id", salt=SALT, fraction=0.2).set_index("id")["arm"]
    assert first.sort_index().equals(second.sort_index())


def test_a_two_column_key_is_split_by_its_entity_so_a_customer_stays_on_one_side() -> None:
    entities = [f"C{i:05d}" for i in range(2_000)]
    frame = pd.DataFrame(
        {
            "id": entities * 3,
            "snapshot": ["2026-01-31"] * 2_000 + ["2026-02-28"] * 2_000 + ["2026-03-31"] * 2_000,
        }
    )
    assignment = programme_assignment(frame, primary_key=["id", "snapshot"], salt=SALT, fraction=0.2)
    sides = assignment.groupby("id")["arm"].nunique()
    assert (sides == 1).all(), "all of a customer's snapshots are on one side"
    expected = member_flags(key_text(frame["id"]).tolist(), salt=SALT, scope_key="universal", fraction=0.2)
    assert (assignment["arm"] == "holdout").to_numpy().tolist() == expected.tolist()


def test_the_period_is_the_outcome_window_both_ends_counted() -> None:
    assert period_window_days(ProgrammePeriod(start=date(2026, 1, 1), end=date(2026, 3, 31))) == 90
    assert period_window_days(ProgrammePeriod(start=date(2026, 5, 4), end=date(2026, 5, 4))) == 1
    with pytest.raises(ValidationError, match="cannot end before it starts"):
        ProgrammePeriod(start=date(2026, 3, 1), end=date(2026, 2, 1))
