"""The contact file and the effect on the contacted (Plan J M103): `engine.measurement.reconcile`.

The arithmetic is checked against values worked out another way - by brute force over a grid of effects for
Fieller's set, by the closed form when everyone who was meant to be reached was - and the rebuilt rows are
checked against `measure_incrementality` itself, so the secondary estimate can never rest on different
customers than the main result.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.measurement.campaign import build_assignment
from engine.measurement.reconcile import (
    CONTACT_UNREADABLE,
    ContactFileError,
    complier_effect,
    contact_counts,
    measured_rows,
    normalise_contacts,
    reconcile_contacts,
)
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.pilot.plain import jargon_in
from engine.uplift.incrementality import Z_95, measure_incrementality

NOW = datetime(2026, 10, 9, tzinfo=UTC)


# --- the file --------------------------------------------------------------------------------------------
def contact_file(values: list[object], *, keys: list[str] | None = None) -> pd.DataFrame:
    return pd.DataFrame({"id": keys or [f"C{i}" for i in range(len(values))], "sent": values})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([1, 0, 1], [True, False, True]),
        (["Yes", "no", "Y"], [True, False, True]),
        (["sent", "not sent", "contacted"], [True, False, True]),
        ([True, False, True], [True, False, True]),
        ([1.0, 0.0, 1.0], [True, False, True]),
    ],
)
def test_the_usual_words_and_numbers_mean_contacted_or_not(raw: list[object], expected: list[bool]) -> None:
    got = normalise_contacts(contact_file(raw), primary_key="id", contacted_column="sent")
    assert got["contacted"].tolist() == expected
    assert list(got.columns) == ["id", "contacted"]


def test_a_blank_is_unknown_and_kept_as_unknown() -> None:
    got = normalise_contacts(contact_file([1, None, 0, ""]), primary_key="id", contacted_column="sent")
    assert got["contacted"].isna().tolist() == [False, True, False, True]


def test_the_value_that_means_contacted_can_be_named() -> None:
    got = normalise_contacts(
        contact_file(["delivered", "bounced", "delivered"]),
        primary_key="id",
        contacted_column="sent",
        contacted_label="Delivered",
    )
    assert got["contacted"].tolist() == [True, False, True]


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (contact_file([1, 0]).drop(columns=["sent"]), "no column 'sent'"),
        (contact_file([1, 0, 1], keys=["a", "b", "a"]), "more than once"),
        (contact_file(["maybe", "yes"]), "neither yes nor no"),
    ],
)
def test_a_contact_file_that_cannot_be_read_is_refused_with_counts_not_values(
    frame: pd.DataFrame, message: str
) -> None:
    with pytest.raises(ContactFileError, match=message) as caught:
        normalise_contacts(frame, primary_key="id", contacted_column="sent")
    assert "maybe" not in str(caught.value) and jargon_in(str(caught.value)) == ()
    assert CONTACT_UNREADABLE == "CONTACT_FILE_UNREADABLE"


def test_ids_match_across_files_as_text() -> None:
    got = normalise_contacts(
        pd.DataFrame({"id": [1.0, 2.0], "sent": [1, 0]}), primary_key="id", contacted_column="sent"
    )
    assert got["id"].tolist() == ["1", "2"]


# --- exact counts ------------------------------------------------------------------------------------------
def assignment_of(arms: list[str], intended: list[bool] | None = None) -> pd.DataFrame:
    n = len(arms)
    return pd.DataFrame(
        {
            "id": [f"C{i}" for i in range(n)],
            "arm": arms,
            "intended": intended if intended is not None else [True] * n,
            "band": pd.Series([pd.NA] * n, dtype="string"),
        }
    )


def test_counts_are_exact_inside_the_measured_population_and_unlisted_customers_are_unknown() -> None:
    arms = ["treated"] * 6 + ["holdout"] * 4 + ["suppressed"] * 2
    intended = (
        [True] * 5 + [False] + [True] * 4 + [False] * 2
    )  # one treated customer was never meant to be reached
    assignment = assignment_of(arms, intended)
    contacts = pd.DataFrame(
        {
            "id": [f"C{i}" for i in (0, 1, 2, 3, 5, 6, 7, 8, 10)],  # C4 and C9 are not listed
            "contacted": pd.array([True, True, False, True, True, True, False, False, True], dtype="boolean"),
        }
    )
    counts = contact_counts(assignment, contacts, primary_key="id")
    # treated and meant: C0..C4; C5 is outside; C4 unlisted. holdout: C6..C9; C9 unlisted. suppressed excluded.
    assert counts == {
        "treated": 5,
        "treated_listed": 4,
        "treated_contacted": 3,
        "holdout": 4,
        "holdout_listed": 3,
        "holdout_contacted": 1,
    }
    as_log = contact_counts(assignment, contacts, primary_key="id", unlisted="not_contacted")
    assert (as_log["treated_listed"], as_log["treated_contacted"]) == (5, 3)
    assert (as_log["holdout_listed"], as_log["holdout_contacted"]) == (4, 1)


# --- Fieller, by brute force ---------------------------------------------------------------------------------
def groups(
    seed: int, n1: int = 400, n0: int = 400, *, reach1: float = 0.6, reach0: float = 0.1, effect: float = 0.05
):
    rng = np.random.default_rng(seed)
    treated = np.r_[np.ones(n1, dtype=bool), np.zeros(n0, dtype=bool)]
    d = np.r_[(rng.random(n1) < reach1), (rng.random(n0) < reach0)].astype(float)
    y = (rng.random(n1 + n0) < 0.2 + effect * d).astype(float)
    return treated, d, y


def grid_interval(treated: np.ndarray, d: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """The set of effects t with (dy - t dd)^2 <= z^2 Var(dy - t dd), found by scanning a fine grid."""
    parts = []
    for flag in (True, False):
        yy, dd = y[treated == flag], d[treated == flag]
        n = len(yy)
        c = np.cov(yy, dd, ddof=1) / n  # [[var y, cov], [cov, var d]] of the group's means
        parts.append((yy.mean(), dd.mean(), c))
    dy, ddelta = parts[0][0] - parts[1][0], parts[0][1] - parts[1][1]
    vyy = parts[0][2][0, 0] + parts[1][2][0, 0]
    vdd = parts[0][2][1, 1] + parts[1][2][1, 1]
    vyd = parts[0][2][0, 1] + parts[1][2][0, 1]
    grid = np.linspace(-1.5, 1.5, 300_001)
    inside = (dy - grid * ddelta) ** 2 <= Z_95**2 * (vyy - 2 * grid * vyd + grid**2 * vdd)
    return float(grid[inside].min()), float(grid[inside].max())


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_interval_is_the_set_of_effects_the_data_cannot_reject(seed: int) -> None:
    treated, d, y = groups(seed)
    effect, reason = complier_effect(treated, d, y, unit="rate")
    assert reason is None and effect is not None
    low, high = grid_interval(treated, d, y)
    assert effect.effect.ci_low == pytest.approx(low, abs=2e-5)
    assert effect.effect.ci_high == pytest.approx(high, abs=2e-5)
    dy = y[treated].mean() - y[~treated].mean()
    dd = d[treated].mean() - d[~treated].mean()
    assert effect.effect.value == pytest.approx(dy / dd)
    assert effect.first_stage.value == pytest.approx(dd)
    assert effect.effect.ci_low <= effect.effect.value <= effect.effect.ci_high
    assert effect.secondary is True and effect.unit == "rate"
    assert (effect.treated_rows, effect.control_rows) == (400, 400)


def test_when_everyone_meant_to_be_reached_was_the_effect_is_the_main_difference() -> None:
    rng = np.random.default_rng(9)
    treated = np.r_[np.ones(500, dtype=bool), np.zeros(300, dtype=bool)]
    d = treated.astype(float)
    y = (rng.random(800) < 0.15 + 0.06 * d).astype(float)
    effect, _ = complier_effect(treated, d, y, unit="rate")
    assert effect is not None
    dy = y[treated].mean() - y[~treated].mean()
    se = math.sqrt(y[treated].var(ddof=1) / 500 + y[~treated].var(ddof=1) / 300)
    assert effect.effect.value == pytest.approx(dy)
    assert effect.effect.ci_low == pytest.approx(dy - Z_95 * se, abs=1e-9)
    assert effect.effect.ci_high == pytest.approx(dy + Z_95 * se, abs=1e-9)
    assert effect.first_stage.value == 1.0


def test_an_amount_is_scaled_and_the_effect_scales_with_it() -> None:
    treated, d, y = groups(6)
    base, _ = complier_effect(treated, d, y, unit="rate")
    scaled, _ = complier_effect(treated, d, y * 250.0, unit="amount")
    assert base is not None and scaled is not None
    assert scaled.unit == "amount"
    for field in ("value", "ci_low", "ci_high"):
        assert getattr(scaled.effect, field) == pytest.approx(250.0 * getattr(base.effect, field))
    assert scaled.first_stage == base.first_stage


@pytest.mark.parametrize(
    ("reach1", "reach0"),
    [(0.04, 0.04), (0.03, 0.05), (0.0, 0.0)],
)
def test_when_the_contact_rates_cannot_be_told_apart_nothing_is_given_and_the_reason_is_plain(
    reach1: float, reach0: float
) -> None:
    treated, d, y = groups(7, n1=300, n0=300, reach1=reach1, reach0=reach0)
    effect, reason = complier_effect(treated, d, y, unit="rate")
    assert effect is None and reason is not None
    assert "too small to tell apart from chance" in reason and "invented" in reason
    assert jargon_in(reason) == ()


def test_a_group_of_fewer_than_two_customers_has_no_effect() -> None:
    effect, reason = complier_effect(
        np.array([True, False, False]), np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 1.0]), unit="rate"
    )
    assert effect is None and "at least two" in str(reason)


# --- the same customers as the main result ---------------------------------------------------------------------
def simulated(seed: int, **options: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, np.ndarray]:
    sim = population(6_000, 0.10, 0.05, seed=seed, control_share=0.2, immature_share=0.2, **options)
    assignment = build_assignment(sim.scores, primary_key="customer_id")
    contacts = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "contacted": pd.array(sim.received_treatment, dtype="boolean"),
        }
    )
    return assignment, sim.outcomes, contacts, sim.received_treatment


def kwargs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "primary_key": "customer_id",
        "outcome_column": "converted",
        "positive_label": None,
        "outcome_kind": "binary",
        "treatment_time": AS_OF,
        "treatment_date_column": "treatment_date",
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF,
    }
    return {**base, **over}


def test_the_rows_rebuilt_are_exactly_the_rows_the_main_result_measured() -> None:
    assignment, outcomes, contacts, _ = simulated(31, compliance=0.7, contamination=0.1)
    report = measure_incrementality(
        assignment.assign(control_group=assignment["arm"].eq("holdout"), suppressed_reason=""),
        outcomes,
        run_id="x",
        primary_key="customer_id",
        outcome_column="converted",
        treatment_time=AS_OF,
        treatment_date_column="treatment_date",
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
    )
    rows, treated_measured, control_measured = measured_rows(assignment, outcomes, contacts, **kwargs())  # type: ignore[arg-type]
    assert (treated_measured, control_measured) == (report.treated_rows, report.control_rows)
    assert report.rows_immature > 0, "the immature customers are left out by both"
    assert (
        int(rows["treated"].sum()) == report.treated_rows
        and int((~rows["treated"]).sum()) == report.control_rows
    )
    assert int(rows.loc[rows["treated"], "y"].sum()) == report.treated_conversions
    assert int(rows.loc[~rows["treated"], "y"].sum()) == report.control_conversions


def test_customers_the_file_does_not_list_leave_the_effect_but_not_the_counts_that_must_match() -> None:
    assignment, outcomes, contacts, _ = simulated(32, compliance=0.7, contamination=0.1)
    partial = contacts.sample(frac=0.8, random_state=1)
    rows, treated_measured, control_measured = measured_rows(assignment, outcomes, partial, **kwargs())  # type: ignore[arg-type]
    assert len(rows.index) < treated_measured + control_measured
    full, full_treated, full_control = measured_rows(assignment, outcomes, contacts, **kwargs())  # type: ignore[arg-type]
    assert (treated_measured, control_measured) == (full_treated, full_control)
    assert len(full.index) == full_treated + full_control


def test_the_readout_holds_the_counts_the_rates_and_the_effect_and_checks_itself_against_the_report() -> None:
    assignment, outcomes, contacts, received = simulated(33, compliance=0.7, contamination=0.1)
    report = measure_incrementality(
        assignment.assign(control_group=assignment["arm"].eq("holdout"), suppressed_reason=""),
        outcomes,
        run_id="x",
        primary_key="customer_id",
        outcome_column="converted",
        treatment_time=AS_OF,
        treatment_date_column="treatment_date",
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
    )
    common: dict[str, object] = {
        "campaign_id": "c_x",
        "positive_label": None,
        "outcome_kind": "binary",
        "treatment_time": AS_OF,
        "treatment_date_column": "treatment_date",
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF,
        "computed_at": NOW,
        "primary_key": "customer_id",
        "outcome_column": "converted",
    }
    readout = reconcile_contacts(
        assignment, outcomes, contacts, report_treated_rows=report.treated_rows, report_control_rows=report.control_rows, **common  # type: ignore[arg-type]
    )
    control = assignment["arm"].eq("holdout").to_numpy()
    assert readout.treated_customers == int((~control).sum()) and readout.holdout_customers == int(
        control.sum()
    )
    assert readout.treated_contacted == int((received & ~control).sum())
    assert readout.holdout_contacted == int((received & control).sum())
    assert readout.contact_rate == readout.treated_contacted / readout.treated_customers
    assert readout.contamination == readout.holdout_contacted / readout.holdout_customers
    assert readout.complier is not None and readout.complier_reason is None
    # a report with other counts: the effect is withheld rather than computed on a different set
    wrong = reconcile_contacts(
        assignment, outcomes, contacts, report_treated_rows=report.treated_rows + 1, report_control_rows=report.control_rows, **common  # type: ignore[arg-type]
    )
    assert wrong.complier is None and "could not be matched" in str(wrong.complier_reason)
    assert wrong.contact_rate == readout.contact_rate, "the rates do not depend on the match"
    # before any measurement: the rates, and why there is no effect
    early = reconcile_contacts(
        assignment, None, contacts, report_treated_rows=None, report_control_rows=None, **{**common, "as_of": None}  # type: ignore[arg-type]
    )
    assert early.complier is None and "not been measured" in str(early.complier_reason)
    assert early.contact_rate == readout.contact_rate
    for sentence in (*readout.notes, str(readout.complier.label), str(wrong.complier_reason)):
        assert jargon_in(sentence) == (), sentence
