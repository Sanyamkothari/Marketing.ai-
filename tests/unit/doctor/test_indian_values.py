"""Plan O M241: lakh, crore, K, M and `/-` read exactly; Hindi and Hinglish yes/no only when both sides are seen.

The acceptance of Plan O §M241 for tools #61 and #62, the recipe's new `parse_number` parameter, and replay.
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.formats import find_format_issues, map_booleans, parse_numbers
from engine.agent.recipe import RecipeError, check_recipe, run_recipe
from engine.doctor.indian_values import (
    boolean_word_family,
    propose_indian_booleans,
    propose_indian_numbers,
)
from engine.doctor.locale import india_defaults

CLEAN = (AgentLevel.CLEAN,)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("₹2.5 L", 250_000.0),
        ("1.2 Cr", 12_000_000.0),
        ("Rs 3,50,000/-", 350_000.0),
        ("12K", 12_000.0),
        ("12k", 12_000.0),
        ("3.5M", 3_500_000.0),
        ("5 lakh", 500_000.0),
        ("5 Lakhs", 500_000.0),
        ("2.25 lac", 225_000.0),
        ("1 crore", 10_000_000.0),
        ("4.75 crores", 47_500_000.0),
        ("Rs. 1.2Cr.", 12_000_000.0),
        ("INR 7,50,000/-", 750_000.0),
        ("350000/-", 350_000.0),
        ("(1.5 lakh)", -150_000.0),
        ("-2 Cr", -20_000_000.0),
        ("0.1 Cr", 1_000_000.0),
        ("1,20,000", 120_000.0),
    ],
)
def test_indian_number_words_parse_exactly(text: str, expected: float) -> None:
    out = parse_numbers(pd.Series([text], dtype=object), indian_scale_words=True)
    assert out.failed == 0
    assert out.values.iloc[0] == expected  # exact, not approximately: 1.2 Cr is 12000000.0


@pytest.mark.parametrize("text", ["₹2.5 L", "1.2 Cr", "Rs 3,50,000/-", "12K", "5 lakh", "350000/-"])
def test_without_the_parameter_the_words_are_not_numbers_as_before(text: str) -> None:
    out = parse_numbers(pd.Series([text], dtype=object))
    assert out.failed == 1 and out.values.isna().all()


@pytest.mark.parametrize("text", ["5 kg", "12 GB", "2.5 L 3", "1 Cr 20 L", "L 2.5", "5%L", "lakh", "2.5 Lk"])
def test_a_word_that_is_not_a_listed_unit_still_fails(text: str) -> None:
    out = parse_numbers(pd.Series([text], dtype=object), indian_scale_words=True)
    assert out.failed == 1


def test_values_without_words_read_the_same_with_and_without_the_parameter() -> None:
    values = pd.Series(["1,200", "₹1,20,000", "45%", "(300)", "1.5", "abc", None, "", "$7"], dtype=object)
    plain = parse_numbers(values)
    indian = parse_numbers(values, indian_scale_words=True)
    pd.testing.assert_series_equal(plain.values, indian.values)
    assert (plain.changed, plain.failed) == (indian.changed, indian.failed)


def _amounts(rows: int = 30) -> pd.DataFrame:
    spellings = ["₹2.5 L", "1.2 Cr", "Rs 3,50,000/-", "12K", "₹75,000", "4.5 lakh"]
    return pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(rows)],
            "annual_income": [spellings[i % len(spellings)] for i in range(rows)],
            "plan": ["Basic", "Gold"] * (rows // 2),
            "churned": [i % 2 for i in range(rows)],
        }
    )


def test_a_column_of_indian_amounts_is_proposed_sure() -> None:
    (proposal,) = propose_indian_numbers(_amounts(), primary_key="customer_id", target="churned")
    assert proposal.column == "annual_income" and proposal.confidence == "sure"
    assert proposal.step is not None and proposal.step.kind is RecipeStepKind.PARSE_NUMBER
    assert proposal.step.params == {"decimal": ".", "percent_to_fraction": True, "indian_scale_words": True}
    assert (proposal.non_empty, proposal.convertible, proposal.failed) == (30, 30, 0)
    assert proposal.examples and all(len(e) <= 80 for e in proposal.examples)
    assert "₹2.5 L" not in proposal.reason  # counts only in the reason


def test_some_letters_not_a_listed_unit_make_it_check_not_sure() -> None:
    frame = _amounts()
    frame.loc[[0, 7], "annual_income"] = ["5 kg", "12 GB"]
    (proposal,) = propose_indian_numbers(frame)
    assert proposal.confidence == "check"
    assert proposal.unknown_words == 2 and proposal.failed == 2


@pytest.mark.parametrize(
    ("header", "values"),
    [
        ("duration", ["30m", "45m", "15m", "60m"]),  # minutes, read as 30,000,000
        ("qty", ["2L", "5L", "1L"]),  # litres, read as 2,00,000
        ("size", ["5M", "10M"]),
        ("pack", ["5k", "10k"]),
    ],
)
def test_a_bare_letter_without_money_evidence_is_check(header: str, values: list[str]) -> None:
    """Review item 3: a bare L, K or M is lakh, thousand or million only when something says the column is money."""
    (proposal,) = propose_indian_numbers(pd.DataFrame({header: values}))
    assert proposal.confidence == "check"
    assert "nothing says this column is money" in proposal.reason


@pytest.mark.parametrize(
    ("header", "values"),
    [
        ("amount", ["2.5 L", "1 L", "3L"]),  # a money header
        ("plan_x", ["₹2.5 L", "₹1 L", "₹3L"]),  # a currency sign
        ("plan_y", ["2.5 L", "1 Cr", "3L"]),  # a word only money uses
        ("ctc", ["12K", "1.5M", "800K"]),
    ],
)
def test_a_bare_letter_with_money_evidence_is_sure(header: str, values: list[str]) -> None:
    (proposal,) = propose_indian_numbers(pd.DataFrame({header: values}))
    assert proposal.confidence == "sure"


def test_a_header_naming_a_physical_unit_makes_it_check() -> None:
    frame = pd.DataFrame({"data_usage": ["2 L", "3 L", "1.5 L", "4 L"]})
    (proposal,) = propose_indian_numbers(frame)
    assert proposal.confidence == "check"  # L may be litres here


def test_plain_numbers_are_left_to_plan_g_and_the_id_and_outcome_are_never_touched() -> None:
    frame = pd.DataFrame(
        {
            "id": ["1 L", "2 L", "3 L"],
            "outcome": ["1 Cr", "2 Cr", "3 Cr"],
            "spend": ["1,200", "₹300", "45"],
            "note": ["call back", "busy", "ok"],
        }
    )
    assert propose_indian_numbers(frame, primary_key="id", target="outcome") == ()


def test_the_profile_gates_the_proposal() -> None:
    frame = _amounts()
    off = india_defaults().model_copy(
        update={"number_words": india_defaults().number_words.model_copy(update={"value": "none"})}
    )
    k_m = india_defaults().model_copy(
        update={"number_words": india_defaults().number_words.model_copy(update={"value": "k_m"})}
    )
    assert propose_indian_numbers(frame, profile=off) == ()
    (proposal,) = propose_indian_numbers(frame, profile=k_m)
    assert proposal.confidence == "check"  # lakh and crore in a K-and-M profile: please check


def test_a_proposed_number_step_runs_replays_and_is_stateless() -> None:
    frame = _amounts()
    (proposal,) = propose_indian_numbers(frame, primary_key="customer_id", target="churned")
    assert proposal.step is not None
    steps = [proposal.step]
    first = run_recipe(
        frame,
        steps,
        upload_id="u1",
        primary_key="customer_id",
        target="churned",
        levels=CLEAN,
        max_failure_pct=5,
    )
    assert first.frame["annual_income"].tolist()[:6] == [
        250_000.0,
        12_000_000.0,
        350_000.0,
        12_000.0,
        75_000.0,
        450_000.0,
    ]
    # Next month's file, rows in another order and half of them: each row's answer depends on that row only.
    later = frame.iloc[::-2].reset_index(drop=True)
    second = run_recipe(
        later,
        steps,
        upload_id="u2",
        primary_key="customer_id",
        target="churned",
        levels=CLEAN,
        max_failure_pct=5,
    )
    expected = first.frame.set_index("customer_id")["annual_income"]
    assert second.frame.set_index("customer_id")["annual_income"].equals(expected.loc[later["customer_id"]])
    assert first.receipt.recipe_hash == second.receipt.recipe_hash == recipe_hash(tuple(steps))


def test_the_recipe_refuses_the_parameter_on_the_id_and_a_non_boolean_value() -> None:
    def step(column: str, value: bool | str) -> RecipeStep:
        return RecipeStep(
            order=1, kind=RecipeStepKind.PARSE_NUMBER, column=column, params={"indian_scale_words": value}
        )

    columns = ["customer_id", "annual_income", "churned"]
    check_recipe(
        [step("annual_income", True)],
        columns=columns,
        primary_key="customer_id",
        target="churned",
        levels=CLEAN,
    )
    for column in ("customer_id", "churned"):
        with pytest.raises(RecipeError) as refused:
            check_recipe(
                [step(column, True)],
                columns=columns,
                primary_key="customer_id",
                target="churned",
                levels=CLEAN,
            )
        assert refused.value.code == "RECIPE_STEP_INVALID"
    with pytest.raises(RecipeError, match="true or false"):
        check_recipe(
            [step("annual_income", "yes")], columns=columns, primary_key=None, target=None, levels=CLEAN
        )


def test_a_recipe_saved_before_m241_hashes_and_runs_as_it_did() -> None:
    old = RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="v", params={"decimal": "."})
    assert recipe_hash((old,)) == (
        "sha256:"
        + __import__("hashlib")
        .sha256(
            b'[{"column":"v","kind":"parse_number","new_column":null,"order":1,"params":{"decimal":"."}}]'
        )
        .hexdigest()
    )
    frame = pd.DataFrame({"v": ["1,200", "₹2.5 L"]})
    with pytest.raises(RecipeError) as refused:  # 50% unconverted: the old step does not read lakh
        run_recipe(
            frame, [old], upload_id="u", primary_key=None, target=None, levels=CLEAN, max_failure_pct=5
        )
    assert refused.value.code == "RECIPE_VALUES_UNCONVERTED"


# ---------------------------------------------------------------------------
# Yes / no
# ---------------------------------------------------------------------------
def _flags(values: list[str | None]) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": [f"C{i}" for i in range(len(values))], "consent_given": values})


@pytest.mark.parametrize(
    ("values", "true_values", "false_values"),
    [
        (["haan", "nahi", "Haan", "NAHI", None, "haan"], ["Haan", "haan"], ["NAHI", "nahi"]),
        (["हाँ", "नहीं", "हाँ", "नहीं"], ["हाँ"], ["नहीं"]),
        (["haa", "nahin", "ha", "nai"], ["ha", "haa"], ["nahin", "nai"]),
        (["Yes", "nahi", "haan", "No"], ["Yes", "haan"], ["No", "nahi"]),
        (["जी हाँ", "जी नहीं", " haan ", "nahi"], ["haan", "जी हाँ"], ["nahi", "जी नहीं"]),
    ],
)
def test_hindi_and_hinglish_yes_no_map_when_both_sides_are_seen(
    values: list[str | None], true_values: list[str], false_values: list[str]
) -> None:
    (proposal,) = propose_indian_booleans(_flags(values), primary_key="customer_id")
    assert proposal.confidence == "sure" and proposal.step is not None
    assert proposal.step.kind is RecipeStepKind.MAP_BOOLEAN
    assert proposal.step.params == {"true_values": true_values, "false_values": false_values}
    mapped = map_booleans(_flags(values)["consent_given"], true_values=true_values, false_values=false_values)
    assert mapped.failed == 0


@pytest.mark.parametrize("values", [["haan", "haan", None], ["nahi", "nahin"], ["हाँ", "हाँ"], ["नहीं"]])
def test_one_side_only_is_never_mapped(values: list[str | None]) -> None:
    (finding,) = propose_indian_booleans(_flags(values))
    assert finding.step is None and finding.confidence == "check"
    assert "both" in finding.reason


@pytest.mark.parametrize(
    "values",
    [
        ["haan", "NA"],  # review item 2: NA alone was taken as the no side, sure
        ["haan", "nahi", "NA"],  # review item 2: NA beside nahi was mapped to no, sure
        ["yes", "haan", "NA", "no"],
        ["yes", "nahi", "NA", "no"],
        ["हाँ", "na", "हाँ"],
        ["ha", "na", "ha", "na"],
    ],
)
def test_na_is_never_mapped_and_never_sure(values: list[str | None]) -> None:
    """`na` may be Hinglish for no or "not available": never the evidence for no, never mapped, always a question."""
    (finding,) = propose_indian_booleans(_flags(values))
    assert finding.step is None and finding.confidence == "check"
    assert finding.unknown_words == sum(1 for v in values if v is not None and v.strip().casefold() == "na")
    assert "not available" in finding.reason


def test_an_unknown_spelling_stops_the_proposal_and_is_counted() -> None:
    (finding,) = propose_indian_booleans(_flags(["haan", "nahi", "shayad", "haan"]))
    assert finding.step is None and finding.unknown_words == 1 and finding.confidence == "check"


def test_english_only_columns_free_text_the_id_and_the_outcome_are_not_this_tools() -> None:
    frame = pd.DataFrame(
        {
            "id": ["haan", "nahi"],
            "won": ["haan", "nahi"],
            "english": ["yes", "no"],
            "notes": ["haan bhai kal call karo", "nahi chahiye"],
        }
    )
    assert propose_indian_booleans(frame, primary_key="id", target="won") == ()
    assert find_format_issues(frame, columns=["english"])  # still Plan G's, unchanged


def test_an_english_only_profile_proposes_no_hindi_booleans() -> None:
    profile = india_defaults().model_copy(
        update={"boolean_words": india_defaults().boolean_words.model_copy(update={"value": ("english",)})}
    )
    assert propose_indian_booleans(_flags(["haan", "nahi"]), profile=profile) == ()


def test_plan_g_detection_is_unchanged_by_the_lexicon() -> None:
    """With the default configuration the Hinglish words are not part of Plan G's yes/no detector."""
    frame = _flags(["haan", "nahi", "haan", "nahi"])
    assert not [i for i in find_format_issues(frame) if i.kind.value == "boolean_as_text"]


def test_word_families() -> None:
    assert boolean_word_family("Y") == "english"
    assert boolean_word_family(" Haan ") == "hinglish"
    assert boolean_word_family("नहीं") == "devanagari"
    assert boolean_word_family("na") is None and boolean_word_family("maybe") is None


def test_a_proposed_boolean_step_runs_on_a_copy_and_replays() -> None:
    frame = _flags(["haan", "nahi", "हाँ", "नहीं", None])
    (proposal,) = propose_indian_booleans(frame, primary_key="customer_id")
    assert proposal.step is not None
    run = run_recipe(
        frame,
        [proposal.step],
        upload_id="u",
        primary_key="customer_id",
        target=None,
        levels=CLEAN,
        max_failure_pct=0,
    )
    assert run.frame["consent_given"].tolist()[:4] == [1.0, 0.0, 1.0, 0.0]
    assert pd.isna(run.frame["consent_given"].iloc[4])
    assert frame["consent_given"].iloc[0] == "haan"  # the input is never modified
