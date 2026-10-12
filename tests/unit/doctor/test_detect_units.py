"""`detect_units` (Plan O M238, DEC-1808 (e)): unit words become a verified `convert_units` proposal, a question,
or `UNIT_MISMATCH`; currencies are reported, never converted; a magnitude jump is a doubt."""

from __future__ import annotations

import pandas as pd

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStepKind
from engine.agent.recipe import run_recipe
from engine.doctor.units import detect_units


def test_one_family_of_units_gives_a_verified_convert_units_proposal() -> None:
    series = pd.Series(["2 GB", "500 MB", "1 GB", "3 GB", "1,5 GB", None])
    report = detect_units(series, column="data_used")
    assert [(u.unit, u.count) for u in report.units] == [("gb", 4), ("mb", 1)] and report.families == (
        "data",
    )
    step = report.step
    assert step is not None and step.kind is RecipeStepKind.CONVERT_UNITS
    assert (step.state, step.ticked) == ("pending", False)
    assert step.step_params == {"units": {"gb": 1.0, "mb": 1 / 1024}, "bare": "fail"}
    frame = pd.DataFrame({"id": range(6), "data_used": series})
    run = run_recipe(
        frame, [step.as_step(1)], upload_id="u", primary_key="id", target=None,
        levels=(AgentLevel.CLEAN,), max_failure_pct=5.0,
    )  # fmt: skip
    assert run.frame["data_used"].round(4).tolist()[:5] == [2.0, 0.4883, 1.0, 3.0, 1.5]


def test_plain_numbers_beside_units_become_one_question() -> None:
    report = detect_units(pd.Series(["2 GB", "500 MB", "700", "1 GB"]), column="data_used")
    assert report.finding == "UNIT_MISMATCH" and report.step is None and report.bare_numbers == 1
    question = report.question
    assert question is not None and not question.blocking
    assert [o.label for o in question.options] == ["GB", "MB", "I don't know"]
    gb = question.options[0].step
    assert gb is not None and gb.step_params == {"units": {"gb": 1.0, "mb": 1 / 1024}, "bare": "as_target"}
    assert question.options[-1].step is None


def test_two_kinds_of_measure_in_one_column_are_a_mismatch_with_no_proposal() -> None:
    report = detect_units(pd.Series(["2 GB", "3 kg", "1 GB"]), column="x")
    assert report.finding == "UNIT_MISMATCH" and report.step is None and report.question is None


def test_l_alone_could_be_litres_or_lakh_so_nothing_is_converted() -> None:
    report = detect_units(pd.Series(["2 L", "3 L", "1.5 L"]), column="x")
    assert report.finding == "UNIT_MISMATCH" and report.step is None
    assert "litres or lakh" in report.sentence


def test_l_beside_crore_is_lakh() -> None:
    report = detect_units(pd.Series(["2.5 L", "1 Cr", "40 L"]), column="turnover")
    assert report.families == ("indian_scale",)
    assert report.step is not None and report.step.step_params["units"] == {"cr": 1e7, "l": 1e5}


def test_currency_is_reported_never_converted() -> None:
    report = detect_units(pd.Series(["₹2.5 L", "₹40 L", "$300"]), column="spend")
    assert set(report.currencies) == {"₹", "$"}
    assert report.step is None


def test_a_magnitude_jump_in_numbers_is_a_doubt() -> None:
    values = [2000 + i for i in range(60)] + [2 + i / 10 for i in range(8)]
    report = detect_units(pd.Series(values, dtype="float64"), column="amount")
    assert report.finding == "UNIT_MISMATCH" and report.step is None
    doubt = report.doubt
    assert doubt is not None and "8 rows" in doubt.because and "thousands" in doubt.because
    assert "leave these 8 values as written" in doubt.would_do


def test_smooth_numbers_raise_no_doubt() -> None:
    assert detect_units(pd.Series(range(1, 500), dtype="float64"), column="amount").doubt is None


def test_the_report_is_deterministic() -> None:
    series = pd.Series(["2 GB", "500 MB", "700", "1 GB"])
    assert detect_units(series, column="d") == detect_units(series, column="d")
