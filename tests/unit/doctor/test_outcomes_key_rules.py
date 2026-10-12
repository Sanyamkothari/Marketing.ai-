"""The M157 outcome offer through `normalise_key` (Plan O M237, DEC-1807; M237 review).

A customer list keyed `CUST-0001` and a recharge log keyed `1` meet on no ID as written, so until M237 the offer
was None and a person had no Data Doctor route to agree the outcome. The offer now measures the two files through
the rule `propose_key_rules` proposes - never a near match - and its counts are the same as on files whose IDs
were written alike to begin with.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tests.unit.doctor.test_outcomes import as_file, customers, rca_offer, recharges


def _bare(frame: pd.DataFrame) -> pd.DataFrame:
    """`C0007` written `7`: the recharge log's export drops the letter and the zeros."""
    return frame.assign(customer_id=frame["customer_id"].str.slice(1).astype(int).astype(str))


def _prefixed(frame: pd.DataFrame) -> pd.DataFrame:
    """`C0007` written `CUST-0007`: the list's export says what the number is."""
    return frame.assign(customer_id="CUST-" + frame["customer_id"].str.slice(1))


def test_ids_written_differently_are_offered_through_the_rule_with_the_same_counts(config_root: Path) -> None:
    alike = rca_offer(
        [
            as_file(customers(), "customers.csv", config_root, "u_list"),
            as_file(recharges(), "recharges_2025.csv", config_root, "u_events"),
        ],
        config_root,
    )
    ruled = rca_offer(
        [
            as_file(_prefixed(customers()), "customers.csv", config_root, "u_list"),
            as_file(_bare(recharges()), "recharges_2025.csv", config_root, "u_events"),
        ],
        config_root,
    )
    assert alike is not None and ruled is not None, "the IDs meet once written one way"
    assert ruled.event_id_column == "customer_id" and ruled.customers == alike.customers
    assert ruled.matched_customers == alike.matched_customers and ruled.event_rows_matched == 1.0
    assert [(c.days, c.yes, c.no, c.measured_on) for c in ruled.choices] == [
        (c.days, c.yes, c.no, c.measured_on) for c in alike.choices
    ]
    assert [c.label_spec for c in ruled.choices] == [c.label_spec for c in alike.choices]
    (note,) = [note for note in ruled.notes if "written one way" in note]
    assert "remove 'CUST-' from the start" in note and "near match" in note
    assert not any("written one way" in note for note in alike.notes), "IDs that meet as written: no note"


def test_ids_that_meet_on_no_rule_are_still_not_offered(config_root: Path) -> None:
    strangers = recharges().assign(customer_id=lambda frame: "X" + frame["customer_id"])
    files = [
        as_file(_prefixed(customers()), "customers.csv", config_root, "u_list"),
        as_file(strangers, "recharges_2025.csv", config_root, "u_events"),
    ]
    assert rca_offer(files, config_root) is None
