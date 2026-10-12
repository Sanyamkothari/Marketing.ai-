"""Plan O M242 (DEC-1812): a money column keeps its currency in the recipe, and a recipe never adds two currencies.

`parse_number`'s `unit` records the currency and stops (`CURRENCY_MIXED`) on a value in another; `convert_currency`
converts at a frozen, person-given rate, row by row. A recipe without either is exactly what it was.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.currency_steps import CurrencyStepError, convert_currency, foreign_values
from engine.agent.formats import currency_codes, parse_numbers
from engine.agent.recipe import STEP_PHASE, RecipeError, check_recipe, run_recipe

RATE: dict[str, Any] = {
    "from": "USD",
    "to": "INR",
    "rate": 83.1,
    "rate_date": "2026-10-10",
    "source": "RBI reference rate, 10 Oct 2026",
}


def step(order: int, kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=kind, column=column, params=params)


def run(frame: pd.DataFrame, *steps: RecipeStep, max_failure_pct: float = 5.0) -> pd.DataFrame:
    return run_recipe(
        frame,
        list(steps),
        upload_id="u_1",
        primary_key="id",
        target=None,
        levels=[AgentLevel.CLEAN],
        max_failure_pct=max_failure_pct,
    ).frame


def test_currency_codes_name_each_sign_and_code() -> None:
    assert currency_codes("₹1,200") == {"INR"}
    assert currency_codes("Rs. 3,50,000/-") == {"INR"}
    assert currency_codes("US$ 12") == {"USD"}
    assert currency_codes("$12") == {"USD"}
    assert currency_codes("12 EUR") == {"EUR"}
    assert currency_codes("£4") == {"GBP"}
    assert currency_codes("1,200") == frozenset()
    assert currency_codes("$12 (₹1,000)") == {"USD", "INR"}


# ---------------------------------------------------------------------------
# parse_number's unit
# ---------------------------------------------------------------------------
def test_a_rupee_column_read_in_rupees_reads_exactly_as_without_the_unit() -> None:
    frame = pd.DataFrame({"id": [1, 2, 3, 4], "amount": ["₹1,200", "Rs 45", "1,20,000", None]})
    plain = run(frame, step(1, RecipeStepKind.PARSE_NUMBER, "amount"))
    with_unit = run(frame, step(1, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"))
    pd.testing.assert_frame_equal(plain, with_unit)
    assert with_unit["amount"].tolist()[:3] == [1200.0, 45.0, 120000.0]


def test_a_dollar_in_a_rupee_column_stops_the_run_instead_of_becoming_a_rupee() -> None:
    """Acceptance (stop half): ₹ and $ in one column are never read into one column of numbers."""
    frame = pd.DataFrame({"id": [1, 2, 3], "amount": ["₹1,200", "$15", "₹300"]})
    with pytest.raises(RecipeError) as stopped:
        run(frame, step(1, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"), max_failure_pct=100)
    assert stopped.value.code == "CURRENCY_MIXED"
    assert "1 values" in stopped.value.message and "never added together" in stopped.value.message
    assert stopped.value.column == "amount" and stopped.value.order == 1


def test_without_a_unit_parse_number_still_strips_every_sign_as_before() -> None:
    """The switch-off behaviour, pinned: the old step reads `$15` as 15 (Data Doctor always proposes `unit`)."""
    frame = pd.DataFrame({"id": [1, 2], "amount": ["₹1,200", "$15"]})
    assert run(frame, step(1, RecipeStepKind.PARSE_NUMBER, "amount"))["amount"].tolist() == [1200.0, 15.0]


def test_a_value_naming_two_currencies_is_mixed_even_when_one_is_the_unit() -> None:
    assert foreign_values(pd.Series(["$12 (₹1,000)", "₹5"]), "INR")[0] == 1
    assert foreign_values(pd.Series([1.0, 2.0]), "INR") == (0, ())


def test_the_unit_is_a_currency_code() -> None:
    frame = pd.DataFrame({"id": [1], "amount": ["12"]})
    for bad in ("inr", "rupees", 7, "US$"):
        with pytest.raises(RecipeError) as refused:
            run(frame, step(1, RecipeStepKind.PARSE_NUMBER, "amount", unit=bad))
        assert refused.value.code == "RECIPE_STEP_INVALID", bad


# ---------------------------------------------------------------------------
# convert_currency
# ---------------------------------------------------------------------------
def test_a_dollar_column_converts_exactly_at_the_persons_rate() -> None:
    frame = pd.DataFrame({"id": [1, 2, 3, 4], "amount": ["$12.10", "$1,200", "15", None]})
    out = run(
        frame,
        step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),
        step(2, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"),
    )
    assert out["amount"].tolist()[:3] == [1005.51, 99720.0, 1246.5]
    assert pd.isna(out["amount"].iloc[3])
    assert out["amount"].dtype == "float64"


def test_a_numeric_column_converts_too() -> None:
    frame = pd.DataFrame({"id": [1, 2], "spend": [10.0, 0.5]})
    out = run(frame, step(1, RecipeStepKind.CONVERT_CURRENCY, "spend", **RATE))
    assert out["spend"].tolist() == [831.0, 41.55]


def test_converting_all_rows_stops_on_a_row_in_another_currency() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["$12", "₹1,000"]})
    with pytest.raises(RecipeError) as stopped:
        run(frame, step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE))
    assert stopped.value.code == "CURRENCY_MIXED"


def test_the_rows_a_column_names_are_converted_and_the_rest_read_in_rupees() -> None:
    """Acceptance (answer half): "`ccy` says which rows are which" converts the dollar rows; nothing is summed."""
    frame = pd.DataFrame(
        {"id": [1, 2, 3, 4], "amount": ["1,200", "15", "300", "2"], "ccy": ["IN", "US", "IN", "us "]}
    )
    out = run(
        frame,
        step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows={"column": "ccy", "values": ["US"]}),
        step(2, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"),
    )
    assert out["amount"].tolist() == [1200.0, 1246.5, 300.0, 166.2]


def test_marked_rows_are_converted_by_their_sign_and_the_rupees_left_for_parse() -> None:
    frame = pd.DataFrame({"id": [1, 2, 3], "amount": ["₹1,200", "$15", "₹300"]})
    out = run(
        frame,
        step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows="marked"),
        step(2, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"),
    )
    assert out["amount"].tolist() == [1200.0, 1246.5, 300.0]


def test_a_selected_row_whose_sign_says_otherwise_stops() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["₹15", "1,200"], "ccy": ["US", "IN"]})
    with pytest.raises(RecipeError) as stopped:
        run(
            frame,
            step(
                1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows={"column": "ccy", "values": ["US"]}
            ),
        )
    assert stopped.value.code == "CURRENCY_MIXED"


def test_a_marked_conversion_never_leaves_a_euro_to_be_read_as_rupees() -> None:
    """Review major (DEC-1812 (c)): before the fix, convert {USD to INR, marked} then a `parse_number` with no unit
    gave [1005.51, 100.0, 1000.0, 5.0] - the €5 read as 5 rupees. A partial conversion now guards itself."""
    frame = pd.DataFrame({"id": [1, 2, 3, 4], "amount": ["$12.10", "₹100", "1,000", "€5"]})
    with pytest.raises(RecipeError) as stopped:
        run(
            frame,
            step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows="marked"),
            step(2, RecipeStepKind.PARSE_NUMBER, "amount"),
        )
    assert stopped.value.code == "CURRENCY_MIXED"
    assert stopped.value.order == 1 and "€5" in stopped.value.message


def test_a_row_the_column_does_not_choose_still_showing_dollars_stops() -> None:
    frame = pd.DataFrame({"id": [1, 2, 3], "amount": ["15", "$20", "300"], "ccy": ["US", "IN", "IN"]})
    with pytest.raises(RecipeError) as stopped:
        run(
            frame,
            step(
                1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows={"column": "ccy", "values": ["US"]}
            ),
            step(2, RecipeStepKind.PARSE_NUMBER, "amount"),
        )
    assert stopped.value.code == "CURRENCY_MIXED"


def test_a_later_conversion_may_take_the_rows_an_earlier_one_left() -> None:
    """Dollars then euros, both to rupees, each by its sign: the first step leaves the euros for the second."""
    euro = {**RATE, "from": "EUR", "rate": 90.0}
    frame = pd.DataFrame({"id": [1, 2, 3, 4], "amount": ["$12.10", "₹100", "1,000", "€5"]})
    out = run(
        frame,
        step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows="marked"),
        step(2, RecipeStepKind.CONVERT_CURRENCY, "amount", **euro, rows="marked"),
        step(3, RecipeStepKind.PARSE_NUMBER, "amount"),
    )
    assert out["amount"].tolist() == [1005.51, 100.0, 1000.0, 450.0]


def test_the_partial_guard_is_the_steps_own() -> None:
    """`convert_currency` given its `to` refuses a leftover foreign row; given a pending source it lets it be."""
    series = pd.Series(["$1", "€1", "₹1"])
    with pytest.raises(CurrencyStepError) as stopped:
        convert_currency(series, source_code="USD", rate=83.1, rows="marked", target="INR")
    assert stopped.value.code == "CURRENCY_MIXED" and stopped.value.rows == 1
    out = convert_currency(
        series, source_code="USD", rate=83.1, rows="marked", target="INR", pending=frozenset({"EUR"})
    )
    assert out.values.tolist() == [83.1, "€1", "₹1"]


def test_a_conversion_counts_what_it_could_not_read() -> None:
    frame = pd.DataFrame({"id": [1, 2], "amount": ["$12", "$abc"]})
    with pytest.raises(RecipeError) as stopped:
        run(frame, step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE))
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"
    receipt = run_recipe(
        frame,
        [step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE)],
        upload_id="u",
        primary_key="id",
        target=None,
        levels=[AgentLevel.CLEAN],
        max_failure_pct=60,
    ).receipt
    assert (receipt.steps[0].changed, receipt.steps[0].failed) == (1, 1)


def test_the_runner_is_row_wise() -> None:
    """A row's output depends only on its own cells and the frozen rate (DEC-1004)."""
    whole = pd.Series(["$1", "$2", "$3", "$4"])
    full = convert_currency(whole, source_code="USD", rate=83.1).values
    for i in range(4):
        alone = convert_currency(whole.iloc[i : i + 1], source_code="USD", rate=83.1).values
        assert alone.iloc[0] == full.iloc[i]
    with pytest.raises(CurrencyStepError):
        convert_currency(pd.Series(["$1", "€1"]), source_code="USD", rate=83.1)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"from": "usd"}, "codes"),
        ({"to": "USD"}, "different"),
        ({"rate": 0}, "above 0"),
        ({"rate": -83.1}, "above 0"),
        ({"rate": float("inf")}, "above 0"),
        ({"rate": True}, "above 0"),
        ({"rate": 5_000_000}, "above 0"),
        ({"rate_date": "10/10/2026"}, "YYYY-MM-DD"),
        ({"rate_date": "2026-02-30"}, "real date"),
        ({"source": "  "}, "where the rate comes from"),
        ({"source": "x" * 201}, "where the rate comes from"),
        ({"rows": "some"}, "rows"),
        ({"rows": {"column": "ccy", "values": []}}, "rows names"),
        ({"decimal": ";"}, "decimal"),
        ({"rate_url": "https://example.com"}, "unknown parameter"),
    ],
)
def test_a_rate_is_frozen_complete_and_never_a_link(change: dict[str, Any], why: str) -> None:
    frame = pd.DataFrame({"id": [1], "amount": ["$1"], "ccy": ["US"]})
    with pytest.raises(RecipeError) as refused:
        run(frame, step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **{**RATE, **change}))
    assert refused.value.code == "RECIPE_STEP_INVALID"
    assert why in refused.value.message


def test_a_missing_rows_column_is_named() -> None:
    frame = pd.DataFrame({"id": [1], "amount": ["$1"]})
    with pytest.raises(RecipeError) as refused:
        run(
            frame,
            step(
                1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows={"column": "ccy", "values": ["US"]}
            ),
        )
    assert refused.value.code == "RECIPE_COLUMN_MISSING" and refused.value.column == "ccy"


def _check(*steps: RecipeStep) -> None:
    check_recipe(
        list(steps), columns=["id", "amount", "ccy"], primary_key="id", target=None, levels=[AgentLevel.CLEAN]
    )


def test_one_column_ends_in_one_currency() -> None:
    with pytest.raises(RecipeError, match="converted to INR and to EUR"):
        _check(
            step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),
            step(2, RecipeStepKind.CONVERT_CURRENCY, "amount", **{**RATE, "from": "GBP", "to": "EUR"}),
        )
    with pytest.raises(RecipeError, match="from USD twice"):
        _check(
            step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),
            step(2, RecipeStepKind.CONVERT_CURRENCY, "amount", **{**RATE, "rate": 84.0}),
        )
    with pytest.raises(RecipeError, match="converted to INR but read as USD"):
        _check(
            step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),
            step(2, RecipeStepKind.PARSE_NUMBER, "amount", unit="USD"),
        )
    with pytest.raises(RecipeError, match="before they are read as numbers"):
        _check(
            step(1, RecipeStepKind.PARSE_NUMBER, "amount"),
            step(2, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows="marked"),
        )
    _check(
        step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE, rows="marked"),
        step(
            2,
            RecipeStepKind.CONVERT_CURRENCY,
            "amount",
            **{**RATE, "from": "EUR", "rate": 90.0},
            rows="marked",
        ),
        step(3, RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"),
    )


def test_a_conversion_never_touches_the_id_or_the_outcome() -> None:
    with pytest.raises(RecipeError, match="ID or the outcome"):
        check_recipe(
            [step(1, RecipeStepKind.CONVERT_CURRENCY, "id", **RATE)],
            columns=["id"],
            primary_key="id",
            target=None,
            levels=[AgentLevel.CLEAN],
        )


def test_a_conversion_names_no_new_column() -> None:
    with pytest.raises(ValueError, match="rewrites the amounts of its own column"):
        RecipeStep(
            order=1, kind=RecipeStepKind.CONVERT_CURRENCY, column="amount", new_column="inr", params=RATE
        )


def test_the_rate_is_part_of_the_recipe_hash() -> None:
    first = (step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),)
    other = (step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **{**RATE, "rate": 83.2}),)
    dated = (step(1, RecipeStepKind.CONVERT_CURRENCY, "amount", **{**RATE, "rate_date": "2026-10-11"}),)
    assert len({recipe_hash(first), recipe_hash(other), recipe_hash(dated)}) == 3


def test_saved_recipes_order_and_hash_as_before() -> None:
    """Additive: a recipe with no unit and no conversion hashes as it did, and the new kind sits with the parsers."""
    saved = (step(1, RecipeStepKind.PARSE_NUMBER, "amount", decimal="."),)
    assert recipe_hash(saved) == recipe_hash(
        (RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="amount", params={"decimal": "."}),)
    )
    assert STEP_PHASE[RecipeStepKind.CONVERT_CURRENCY] == STEP_PHASE[RecipeStepKind.PARSE_NUMBER]
    assert STEP_PHASE[RecipeStepKind.CONVERT_CURRENCY] < STEP_PHASE[RecipeStepKind.SET_MISSING]


def test_parse_numbers_itself_is_unchanged() -> None:
    series = pd.Series(["$12", "₹1,200", "(300)", "45%"])
    assert parse_numbers(series).values.tolist() == [12.0, 1200.0, -300.0, 0.45]
