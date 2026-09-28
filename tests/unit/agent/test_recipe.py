"""The recipe engine (Plan G M72): fixed order, stateless steps, counted results, never a guess."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.recipe import RecipeError, check_recipe, run_recipe

LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["C1", "C2", "C3", "C4"],
            "bill": ["₹1,200", "Rs. 300", "45%", None],
            "orders": [4, 1, 0, 2],
            "signup": ["25/12/2023", "01/02/2024", "2024-03-05", None],
            "flag": ["Y", "no", "TRUE", "n"],
            "city": ["Delhi", "delhi ", "Pune", "Pune"],
            "email": ["a@b.com", "c@d.com", "e@f.com", "g@h.com"],
            "converted_30d": [1, 0, 1, 0],
        }
    )


def _step(order: int, kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    new_column = params.pop("new_column", None)
    return RecipeStep(order=order, kind=kind, column=column, new_column=new_column, params=params)


def _full() -> tuple[RecipeStep, ...]:
    return (
        _step(1, RecipeStepKind.PARSE_NUMBER, "bill", decimal=".", percent_to_fraction=True),
        _step(2, RecipeStepKind.PARSE_DATE, "signup", dayfirst=True),
        _step(3, RecipeStepKind.MAP_BOOLEAN, "flag", true_values=["Y", "TRUE"], false_values=["no", "n"]),
        _step(4, RecipeStepKind.NORMALISE_TEXT, "city", strip=True, merge={"delhi": "Delhi"}),
        _step(5, RecipeStepKind.DERIVE, "bill", new_column="bill_per_order", expression="bill / orders"),
        _step(6, RecipeStepKind.DROP_COLUMN, "email"),
    )


def _run(frame: pd.DataFrame, steps: tuple[RecipeStep, ...], **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "upload_id": "u1",
        "primary_key": "customer_id",
        "target": "converted_30d",
        "levels": LEVELS,
        "max_failure_pct": 5.0,
    }
    options.update(kwargs)
    return run_recipe(frame, steps, **options)


def test_a_full_recipe_prepares_the_copy() -> None:
    result = _run(_frame(), _full())
    out = result.frame
    assert out["bill"].tolist()[:3] == pytest.approx([1200.0, 300.0, 0.45])
    assert out["signup"].iloc[0] == pd.Timestamp("2023-12-25")
    assert out["flag"].tolist() == [1.0, 0.0, 1.0, 0.0]
    assert out["city"].tolist() == ["Delhi", "Delhi", "Pune", "Pune"]
    assert out["bill_per_order"].iloc[0] == pytest.approx(300.0)
    assert "email" not in out.columns
    assert out["customer_id"].tolist() == ["C1", "C2", "C3", "C4"]
    assert result.receipt.rows_in == result.receipt.rows_out == 4
    assert result.receipt.columns_out == result.receipt.columns_in  # one added, one dropped
    assert result.receipt.recipe_hash == recipe_hash(_full())
    assert [s.order for s in result.receipt.steps] == [1, 2, 3, 4, 5, 6]


def test_the_input_frame_is_never_modified() -> None:
    frame = _frame()
    before = frame.copy()
    _run(frame, _full())
    pd.testing.assert_frame_equal(frame, before)


def test_the_same_recipe_gives_the_same_result() -> None:
    first = _run(_frame(), _full()).frame
    second = _run(_frame(), _full()).frame
    pd.testing.assert_frame_equal(first, second)


def test_steps_are_row_wise() -> None:
    """DEC-1004: preparing the rows one at a time gives the same values as preparing them together."""
    whole = _run(_frame(), _full()).frame
    parts = pd.concat([_run(_frame().iloc[[i]], _full()).frame for i in range(4)])
    pd.testing.assert_frame_equal(whole, parts, check_dtype=False)


def test_steps_out_of_order_are_refused() -> None:
    steps = (_step(1, RecipeStepKind.DROP_COLUMN, "email"), _step(2, RecipeStepKind.PARSE_NUMBER, "bill"))
    with pytest.raises(RecipeError) as caught:
        _run(_frame(), steps)
    assert caught.value.code == "RECIPE_STEP_INVALID"


def test_a_missing_column_stops_the_run_and_names_it() -> None:
    frame = _frame().rename(columns={"bill": "bill_amount"})
    with pytest.raises(RecipeError) as caught:
        _run(frame, _full())
    assert caught.value.code == "RECIPE_COLUMN_MISSING"
    assert caught.value.column == "bill"


def test_too_many_unconvertible_values_stop_the_run() -> None:
    frame = _frame()
    frame["bill"] = ["₹1,200", "abc", "def", "12"]
    with pytest.raises(RecipeError) as caught:
        _run(frame, (_step(1, RecipeStepKind.PARSE_NUMBER, "bill"),))
    assert caught.value.code == "RECIPE_VALUES_UNCONVERTED"
    assert "2 of 4" in caught.value.message


def test_failures_under_the_limit_are_counted_with_masked_examples() -> None:
    frame = pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(40)], "bill": ["10"] * 39 + ["call 9876543210"]}
    )
    result = _run(frame, (_step(1, RecipeStepKind.PARSE_NUMBER, "bill"),), target=None)
    (step,) = result.receipt.steps
    assert (step.failed, step.changed) == (1, 39)
    assert "9876543210" not in step.examples_failed[0]


@pytest.mark.parametrize("column", ["customer_id", "converted_30d"])
def test_the_id_and_the_outcome_are_never_changed(column: str) -> None:
    with pytest.raises(RecipeError):
        _run(_frame(), (_step(1, RecipeStepKind.NORMALISE_TEXT, column),))
    with pytest.raises(RecipeError):
        _run(_frame(), (_step(1, RecipeStepKind.DROP_COLUMN, column),))


def test_a_new_column_may_not_read_the_outcome() -> None:
    step = _step(1, RecipeStepKind.DERIVE, "orders", new_column="leak", expression="converted_30d * 2")
    with pytest.raises(RecipeError) as caught:
        _run(_frame(), (step,))
    assert "outcome" in caught.value.message


def test_derive_needs_the_derive_level() -> None:
    step = _step(1, RecipeStepKind.DERIVE, "orders", new_column="double", expression="orders * 2")
    with pytest.raises(RecipeError):
        _run(_frame(), (step,), levels=(AgentLevel.CLEAN,))


def test_snapshot_date_needs_a_snapshot_column() -> None:
    frame = _frame().assign(snapshot=pd.Timestamp("2024-06-01"))
    frame = _run(frame, (_step(1, RecipeStepKind.PARSE_DATE, "signup", dayfirst=True),)).frame
    step = _step(
        1,
        RecipeStepKind.DERIVE,
        "signup",
        new_column="days_since_signup",
        expression="days_between(snapshot_date, signup)",
    )
    with pytest.raises(RecipeError):
        _run(frame, (step,))
    out = _run(frame, (step,), snapshot_column="snapshot").frame
    assert out["days_since_signup"].iloc[0] == 159


def test_a_derived_name_cannot_clash() -> None:
    step = _step(1, RecipeStepKind.DERIVE, "orders", new_column="bill", expression="orders * 2")
    with pytest.raises(RecipeError):
        _run(_frame(), (step,))


@pytest.mark.parametrize(
    "step",
    [
        _step(1, RecipeStepKind.PARSE_DATE, "signup"),
        _step(1, RecipeStepKind.PARSE_NUMBER, "bill", decimal=";"),
        _step(1, RecipeStepKind.MAP_BOOLEAN, "flag", true_values=["y"], false_values=["Y"]),
        _step(1, RecipeStepKind.PARSE_NUMBER, "bill", sneaky=True),
        _step(1, RecipeStepKind.DERIVE, "orders", new_column="x", expression="__import__('os')"),
    ],
)
def test_bad_parameters_are_refused(step: RecipeStep) -> None:
    with pytest.raises(RecipeError):
        _run(_frame(), (step,))


def test_check_recipe_alone_touches_no_data() -> None:
    check_recipe(
        _full(),
        columns=list(_frame().columns),
        primary_key="customer_id",
        target="converted_30d",
        levels=LEVELS,
    )


def test_a_hidden_column_absent_from_a_later_file_is_skipped() -> None:
    """A column Guided setup hid (a leak, often not known at scoring time) used to be required in
    every scoring file: the drop step raised RECIPE_COLUMN_MISSING, a 409 nobody could acknowledge."""
    steps = (
        _step(1, RecipeStepKind.PARSE_NUMBER, "bill", decimal="."),
        _step(2, RecipeStepKind.DROP_COLUMN, "email"),
    )
    frame = _frame().drop(columns=["email"])
    run = _run(frame, steps)
    assert "email" not in run.frame.columns
    drop = run.receipt.steps[1]
    assert (drop.kind, drop.column, drop.changed, drop.skipped) == (
        RecipeStepKind.DROP_COLUMN,
        "email",
        0,
        True,
    )
    assert run.receipt.steps[0].skipped is False
    assert _run(_frame(), steps).receipt.steps[1].skipped is False
    check_recipe(steps, columns=list(frame.columns), primary_key="customer_id", target=None, levels=LEVELS)


def test_the_id_and_the_outcome_still_cannot_be_dropped_when_absent() -> None:
    with pytest.raises(RecipeError) as caught:
        _run(
            _frame().drop(columns=["converted_30d"]), (_step(1, RecipeStepKind.DROP_COLUMN, "converted_30d"),)
        )
    assert caught.value.code == "RECIPE_STEP_INVALID"


def test_a_categorical_column_is_tidied_not_crashed_on() -> None:
    """A Parquet `category` column used to raise TypeError inside normalise_text: a 500 at scoring."""
    frame = _frame()
    frame["city"] = frame["city"].astype("category")
    run = _run(frame, (_step(1, RecipeStepKind.NORMALISE_TEXT, "city", strip=True, merge={"delhi": "DL"}),))
    assert run.frame["city"].tolist() == ["Delhi", "DL", "Pune", "Pune"]
    assert run.receipt.steps[0].changed == 1
