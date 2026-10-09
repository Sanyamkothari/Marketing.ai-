"""The audit is linear in the rows (Plan J M103): a million customers take about four times what 250,000 do.

A quadratic step (a Python loop over a join, a pairwise comparison) would take sixteen times as long, so the
check is on the ratio, which does not depend on how fast the machine is: reading the groups and outcomes,
`measure_campaign`, the randomness test (which looks at a fixed sample), the contact readout with its effect
on the contacted, and the programme split. The bound is generous (6, where linear is 4 and quadratic 16)
because the machine is shared; the million-row run is `slow`, so `make test` leaves it to `make test-all`.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from engine.measurement.audit import build_assignment_frame, build_outcomes_frame, check_randomness
from engine.measurement.measure import measure_campaign
from engine.measurement.programme import programme_assignment
from engine.measurement.reconcile import normalise_contacts, reconcile_contacts
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population

LINEAR_RATIO_BOUND = 6.0
"""Four times the rows may take up to six times as long: linear is 4, quadratic 16."""


def audit_seconds(n: int) -> float:
    """Wall time of the audit's whole pipeline on `n` customers (the simulation that makes them is not counted)."""
    sim = population(n, 0.10, 0.04, seed=1, control_share=0.2, compliance=0.6, contamination=0.1)
    rng = np.random.default_rng(0)
    groups = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], 0, 1),
            "age": rng.integers(18, 80, n),
            "region": rng.choice(list("ABCD"), n),
        }
    )
    sent = pd.DataFrame(
        {"customer_id": sim.scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
    )
    started = time.perf_counter()
    built = build_assignment_frame(groups, primary_key="customer_id", arm_column="group")
    outcomes, date_column = build_outcomes_frame(
        sim.outcomes,
        primary_key="customer_id",
        outcome_column="converted",
        treatment_date_column="treatment_date",
        assignment=built,
    )
    report = measure_campaign(
        built.assignment,
        outcomes,
        run_id="scaling",
        primary_key="customer_id",
        outcome_column="converted",
        intended_column="intended",
        treatment_time=AS_OF,
        treatment_date_column=date_column,
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
    )
    check_randomness(built, primary_key="customer_id", threshold=0.6)
    contacts = normalise_contacts(sent, primary_key="customer_id", contacted_column="contacted")
    readout = reconcile_contacts(
        built.assignment,
        outcomes,
        contacts,
        campaign_id="scaling",
        primary_key="customer_id",
        outcome_column="converted",
        positive_label=None,
        outcome_kind="binary",
        treatment_time=AS_OF,
        treatment_date_column=date_column,
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
        report_treated_rows=report.treated_rows,
        report_control_rows=report.control_rows,
        computed_at=AS_OF,
    )
    programme_assignment(
        sim.outcomes[["customer_id"]], primary_key="customer_id", salt="s" * 20, fraction=0.1
    )
    elapsed = time.perf_counter() - started
    assert readout.complier is not None, "the work was real: the effect on the contacted was computed"
    return elapsed


@pytest.mark.slow
def test_a_million_customers_take_about_four_times_what_a_quarter_of_a_million_do() -> None:
    audit_seconds(20_000)  # warm up: imports and the first model fit are not the rows' cost
    small = min(audit_seconds(250_000), audit_seconds(250_000))
    large = audit_seconds(1_000_000)
    assert large / small < LINEAR_RATIO_BOUND, (
        f"1,000,000 rows took {large:.1f}s against {small:.1f}s for 250,000: "
        f"{large / small:.1f} times for four times the rows"
    )
    assert large < 240.0, f"a million-row audit took {large:.0f}s"


def test_the_pipeline_scales_on_a_smaller_pair_too() -> None:
    """The same check at 50,000 and 200,000 rows, quick enough for `make test`."""
    audit_seconds(10_000)
    small = min(audit_seconds(50_000), audit_seconds(50_000))
    large = audit_seconds(200_000)
    assert large / small < LINEAR_RATIO_BOUND, f"{large / small:.1f} times for four times the rows"
