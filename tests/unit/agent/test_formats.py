"""Messy-format detection and the stateless parsers (Plan G M71, §6.2)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from engine.agent.formats import (
    FormatIssueKind,
    date_order,
    find_format_issues,
    map_booleans,
    normalise_texts,
    parse_dates,
    parse_numbers,
)


def _kinds(frame: pd.DataFrame) -> dict[str, FormatIssueKind]:
    return {issue.column: issue.kind for issue in find_format_issues(frame)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("₹1,200", 1200.0),
        ("Rs. 1,20,000", 120000.0),
        ("INR 99", 99.0),
        ("$3.50", 3.5),
        ("45%", 0.45),
        ("(300)", -300.0),
        ("-12", -12.0),
        ("+7", 7.0),
        ("1 200", 1200.0),
        ("  8  ", 8.0),
    ],
)
def test_parse_numbers_reads_what_people_type(text: str, expected: float) -> None:
    outcome = parse_numbers(pd.Series([text]))
    assert outcome.failed == 0
    assert outcome.values.iloc[0] == pytest.approx(expected)


def test_parse_numbers_with_a_comma_decimal_mark() -> None:
    outcome = parse_numbers(pd.Series(["1.200,50", "3,25", "7"]), decimal=",")
    assert outcome.values.tolist() == pytest.approx([1200.5, 3.25, 7.0])


def test_parse_numbers_counts_failures_and_keeps_empties_empty() -> None:
    outcome = parse_numbers(pd.Series(["12", None, "", "twelve"]))
    assert outcome.failed == 1
    assert outcome.failed_examples == ("twelve",)
    assert outcome.values.iloc[0] == 12.0
    assert all(math.isnan(v) for v in outcome.values.iloc[1:])


def test_parse_numbers_is_row_wise() -> None:
    """DEC-1004: a value's result does not depend on the other rows."""
    whole = parse_numbers(pd.Series(["₹1,200", "abc", "45%"])).values
    alone = [parse_numbers(pd.Series([v])).values.iloc[0] for v in ["₹1,200", "abc", "45%"]]
    for a, b in zip(whole.tolist(), alone, strict=True):
        assert (math.isnan(a) and math.isnan(b)) or a == b


def test_failed_examples_are_masked() -> None:
    outcome = parse_numbers(pd.Series(["call me on 9876543210", "5"]))
    assert outcome.failed == 1
    assert "9876543210" not in outcome.failed_examples[0]


def test_parse_dates_uses_the_decided_order() -> None:
    series = pd.Series(["01/02/2024", "2024-03-05", "07 Jan 2024"])
    day_first = parse_dates(series, dayfirst=True).values
    month_first = parse_dates(series, dayfirst=False).values
    assert day_first.iloc[0] == pd.Timestamp("2024-02-01")
    assert month_first.iloc[0] == pd.Timestamp("2024-01-02")
    assert day_first.iloc[1] == month_first.iloc[1] == pd.Timestamp("2024-03-05")


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["25/12/2023", "01/02/2024"], True),
        (["12/25/2023", "01/02/2024"], False),
        (["01/02/2024", "03/04/2024"], None),
        (["2024-01-02"], None),
    ],
)
def test_date_order_is_decided_only_by_the_values(values: list[str], expected: bool | None) -> None:
    assert date_order(pd.Series(values)) is expected


def test_map_booleans_and_its_guard() -> None:
    outcome = map_booleans(
        pd.Series(["Y", "no", "TRUE", "maybe", None]), true_values=["y", "true"], false_values=["no"]
    )
    assert outcome.values.tolist()[:3] == [1.0, 0.0, 1.0]
    assert outcome.failed == 1
    with pytest.raises(ValueError):
        map_booleans(pd.Series(["y"]), true_values=["y"], false_values=["Y"])


def test_normalise_texts_strips_and_merges() -> None:
    outcome = normalise_texts(pd.Series([" Delhi", "delhi", "Pune", None]), merge={"delhi": "Delhi"})
    assert outcome.values.tolist()[:3] == ["Delhi", "Delhi", "Pune"]
    assert outcome.changed == 2


def test_the_detector_finds_each_kind_once() -> None:
    frame = pd.DataFrame(
        {
            "bill": ["₹1,200", "Rs. 1,20,000", "450", "(300)", "12.5%", "7"],
            "signup": ["12/03/2024", "25/12/2023", "2024-01-05", "05 Jan 2024", "01/02/2024", "03/03/2024"],
            "flag": ["Y", "yes", "N", "No", "TRUE", "n"],
            "city": ["Delhi", "delhi ", "DELHI", "Mumbai", "Mumbai", "Pune"],
            "note": [" a", "b ", "c", "d", "e", "f"],
            "code": ["C1", "C2", "C3", "C4", "C5", "C6"],
            "amount": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    kinds = _kinds(frame)
    assert kinds == {
        "bill": FormatIssueKind.NUMBER_AS_TEXT,
        "signup": FormatIssueKind.MIXED_DATES,
        "flag": FormatIssueKind.BOOLEAN_AS_TEXT,
        "city": FormatIssueKind.CATEGORY_VARIANTS,
        "note": FormatIssueKind.UNTRIMMED_TEXT,
    }
    city = next(issue for issue in find_format_issues(frame) if issue.column == "city")
    assert city.params["merge"] == {"delhi": "Delhi", "DELHI": "Delhi"}


def test_ambiguous_dates_are_reported_with_no_order() -> None:
    frame = pd.DataFrame({"d": ["01/02/2024", "03/04/2024", "05/06/2024"]})
    (issue,) = find_format_issues(frame)
    assert issue.params["dayfirst"] is None
    assert any("cannot be told" in note for note in issue.notes)


def test_clean_iso_dates_and_plain_ids_are_left_alone() -> None:
    frame = pd.DataFrame({"d": ["2024-01-02", "2024-02-03"], "id": ["A1", "B2"]})
    assert find_format_issues(frame) == ()


def test_numbers_stored_as_text_are_reported_even_when_plain() -> None:
    """A column reaches us as text only when something stopped pandas reading it as numbers - the
    public Telco file's TotalCharges holds a single space for new subscribers - so plain digits in a
    text column still need converting."""
    frame = pd.DataFrame({"total": ["29.85", "1889.5", " ", "108.15"]})
    (issue,) = find_format_issues(frame)
    assert issue.kind is FormatIssueKind.NUMBER_AS_TEXT
    assert (issue.non_empty, issue.convertible, issue.failed) == (3, 3, 0)


def test_a_mostly_unconvertible_column_is_not_called_numbers() -> None:
    frame = pd.DataFrame({"x": ["abc", "def", "ghi", "12"]})
    assert FormatIssueKind.NUMBER_AS_TEXT not in _kinds(frame).values()


# ---------------------------------------------------------------------------
# M77: the parsers run once per distinct value; they must equal the row-by-row loop they replaced
# ---------------------------------------------------------------------------
_MESSY: list[object] = [
    "₹1,200",
    "Rs. 1,20,000",
    "45%",
    "(300)",
    "-12",
    "+7",
    "1 200",
    "  8  ",
    "1.200,50",
    "abc",
    "",
    "   ",
    None,
    float("nan"),
    "1e3",
    ".5",
    "5.",
    "(-4)",
    "12%%",
    "€ 3,5",
    "$-2",
    "1 000",
    "1'000",
    "₹1,200",
    "abc",
    7,
    3.5,
    "INR",
    "--5",
    "0",
    "Y",
    "yes",
    " No ",
    "delhi ",
    "Delhi",
    "DELHI",
]


def _empty(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or not str(value).strip()


@pytest.mark.parametrize("decimal", [".", ","])
def test_parse_numbers_matches_the_row_by_row_loop(decimal: str) -> None:
    from engine.agent.formats import _number_one  # the one parser both versions share

    series = pd.Series(_MESSY * 3, dtype=object)
    expected: list[float] = []
    failures: list[str] = []
    changed = 0
    for value in series:
        if _empty(value):
            expected.append(math.nan)
            continue
        if isinstance(value, int | float):  # a real number is read by its value and not "changed"
            expected.append(float(value))
            continue
        number = _number_one(str(value), decimal, True)
        expected.append(math.nan if number is None else number)
        changed += number is not None
        if number is None:
            failures.append(str(value))
    out = parse_numbers(series, decimal=decimal)
    assert out.values.tolist() == pytest.approx(expected, nan_ok=True)
    assert (out.failed, out.changed) == (len(failures), changed)
    assert list(out.failed_examples) == failures[:5]


def test_map_booleans_and_normalise_texts_match_the_row_by_row_loop() -> None:
    series = pd.Series(_MESSY * 2, dtype=object, index=range(100, 100 + 2 * len(_MESSY)))
    truthy, falsy = {"y", "yes", "1"}, {"no", "n", "0"}
    out = map_booleans(series, true_values=sorted(truthy), false_values=sorted(falsy))
    expected: list[float] = []
    for value in series:
        key = None if _empty(value) else str(value).strip().casefold()
        expected.append(1.0 if key in truthy else 0.0 if key in falsy else math.nan)
    assert out.values.tolist() == pytest.approx(expected, nan_ok=True)
    assert list(out.values.index) == list(series.index)
    assert out.failed == sum(
        1 for v, e in zip(series, expected, strict=True) if not _empty(v) and math.isnan(e)
    )

    merge = {"delhi": "Delhi", "DELHI": "Delhi"}
    tidy = normalise_texts(series, merge=merge)
    changed = 0
    for before, after in zip(series, tidy.values, strict=True):
        if _empty(before):
            assert after is before or (pd.isna(after) and pd.isna(before)) or after == before
            continue
        want = merge.get(str(before).strip(), str(before).strip())
        if want != str(before):
            changed += 1
            assert after == want
        else:
            assert after == before
    assert tidy.changed == changed


def test_parse_dates_parses_each_spelling_once_with_the_same_answer() -> None:
    values = ["03/01/2024", "2024-01-05", "5 Feb 2024", "03/01/2024", None, "not a date", "2024-01-05"]
    series = pd.Series(values * 50, dtype=object)
    out = parse_dates(series, dayfirst=True)
    for value, got in zip(series, out.values, strict=True):
        one = None if value is None else pd.to_datetime(value, format="mixed", dayfirst=True, errors="coerce")
        assert (pd.isna(got) and (one is None or pd.isna(one))) or got == one
    assert out.failed == 50


def test_dates_with_different_utc_offsets_parse_instead_of_failing() -> None:
    """A column mixing UTC offsets used to raise inside pandas' `.dt` and end Guided setup with a 500."""
    series = pd.Series(["2024-01-01T23:00:00+05:30", "2024-01-02T00:00:00+01:00", "2024-01-03", "03/01/2024"])
    out = parse_dates(series, dayfirst=True)
    assert out.failed == 0
    assert out.values.tolist()[0] == pd.Timestamp("2024-01-01 17:30:00")  # converted to UTC
    assert find_format_issues(pd.DataFrame({"when": series}))[0].kind is FormatIssueKind.MIXED_DATES


# ---------------------------------------------------------------------------
# Adversarial review of Plan G (recipe fixer): each test reproduces one confirmed defect
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("values", "first"),
    [
        (["1.200,50", "2.300,75", "3.100,00"], 1200.5),
        (["€1.200,50", "€2.300,75", "€15,20"], 1200.5),
        (["12,50", "7,25", "1.200,50"], 12.5),
    ],
)
def test_european_numbers_are_detected_with_a_comma_decimal_mark(values: list[str], first: float) -> None:
    """A thousands dot (`1.200,50`) or a currency prefix (`€1.200,50`) used to vote for '.', so the
    proposed step read 1.200,50 as 1.2005 and reported no failures."""
    (issue,) = find_format_issues(pd.DataFrame({"spend": values}))
    assert issue.kind is FormatIssueKind.NUMBER_AS_TEXT
    assert issue.params["decimal"] == ","
    parsed = parse_numbers(pd.Series(values), decimal=issue.params["decimal"])
    assert parsed.failed == 0
    assert parsed.values.iloc[0] == pytest.approx(first)


def test_a_decimal_mark_the_values_cannot_decide_is_not_guessed() -> None:
    """`1.200` is 1.2 or 1200: with nothing in the column to tell, no number fix is proposed."""
    frame = pd.DataFrame({"spend": ["1.200", "2.500", "3.750"]})
    assert FormatIssueKind.NUMBER_AS_TEXT not in _kinds(frame).values()
    us = pd.DataFrame({"spend": ["1.200", "2.500", "3.75"]})  # 3.75 decides it
    (issue,) = find_format_issues(us)
    assert issue.params["decimal"] == "."


def test_numeric_cells_in_a_text_column_are_read_by_value() -> None:
    """A chunked CSV read can hand an object column of Python floats and strings: `str(1200.0)` was
    then read as 12000 under a comma decimal mark, and 1.0 / 0.0 never matched a yes / no spelling."""
    mixed = pd.Series([1200.0, "1.200,50", None, 7], dtype=object)
    comma = parse_numbers(mixed, decimal=",")
    assert comma.values.tolist()[:2] == pytest.approx([1200.0, 1200.5])
    assert comma.values.iloc[3] == 7.0
    assert (comma.failed, comma.changed) == (0, 1)
    (issue,) = find_format_issues(pd.DataFrame({"amount": [1200.0, 800.0, "1.200,50", "2.300,75"]}))
    assert issue.params["decimal"] == ","

    flags = map_booleans(
        pd.Series([1.0, 0.0, float("nan"), 1.0], dtype=object),
        true_values=["1", "yes"],
        false_values=["0", "no"],
    )
    assert flags.values.tolist()[:2] == [1.0, 0.0]
    assert (flags.failed, flags.changed) == (0, 3)
    floats = map_booleans(pd.Series([1.0, 0.0, float("nan")]), true_values=["1"], false_values=["0"])
    assert (floats.failed, floats.values.tolist()[:2]) == (0, [1.0, 0.0])


@pytest.mark.parametrize(
    ("decimal", "text"),
    [
        (".", "1,5"),
        (".", "12,5%"),
        (".", "450,00"),
        (".", "1,2,3"),
        (".", "1.200,50"),
        (",", "1.5"),
        (",", "1.200.5"),
        (",", "1,200.50"),
        (",", "12.50"),
    ],
)
def test_a_number_in_the_other_convention_fails_instead_of_being_misread(decimal: str, text: str) -> None:
    outcome = parse_numbers(pd.Series([text]), decimal=decimal)
    assert outcome.failed == 1
    assert math.isnan(outcome.values.iloc[0])


@pytest.mark.parametrize(
    ("decimal", "text", "expected"),
    [
        (".", "1,200.50", 1200.5),
        (".", "12,34,567.5", 1234567.5),
        (".", "1,000,000", 1_000_000.0),
        (",", "1.200,50", 1200.5),
        (",", "1.000.000", 1_000_000.0),
        (",", "3,25", 3.25),
    ],
)
def test_grouping_in_the_chosen_convention_still_reads(decimal: str, text: str, expected: float) -> None:
    outcome = parse_numbers(pd.Series([text]), decimal=decimal)
    assert (outcome.failed, outcome.values.iloc[0]) == (0, pytest.approx(expected))


def test_relative_date_words_are_failures_not_the_current_time() -> None:
    """'now' / 'today' used to become the wall-clock time of the run - a different value every run
    and a date after every real event in a training file."""
    series = pd.Series(["03/01/2024", "today", "now", " Today "])
    first = parse_dates(series, dayfirst=True)
    assert first.values.iloc[0] == pd.Timestamp("2024-01-03")
    assert first.values.iloc[1:].isna().all()
    assert first.failed == 3
    mixed_offsets = pd.Series(["2024-01-01T23:00:00+05:30", "2024-01-02T00:00:00+01:00", "now"])
    assert pd.isna(parse_dates(mixed_offsets, dayfirst=True).values.iloc[2])


def test_dates_with_utc_offsets_are_converted_to_utc_and_naive_ones_kept() -> None:
    """Each aware value is converted to UTC before its zone is dropped, whatever the other rows hold
    (DEC-1004); a value with no offset is kept as written."""
    series = pd.Series(["2024-01-01T23:00:00+05:30", "2024-01-02T00:00:00+01:00", "2024-01-03 10:00"])
    out = parse_dates(series, dayfirst=True)
    assert out.values.tolist() == [
        pd.Timestamp("2024-01-01 17:30:00"),
        pd.Timestamp("2024-01-01 23:00:00"),
        pd.Timestamp("2024-01-03 10:00:00"),
    ]
    same_offset = parse_dates(pd.Series(["2024-01-01T23:00:00+05:30"]), dayfirst=True)
    assert same_offset.values.iloc[0] == pd.Timestamp("2024-01-01 17:30:00")


def test_normalise_texts_accepts_a_categorical_column() -> None:
    """A pandas-written Parquet keeps a dictionary column as `category`; assigning a new spelling
    raised TypeError and the request ended with a 500."""
    series = pd.Series([" Delhi", "delhi", "Pune", None], dtype="category")
    outcome = normalise_texts(series, merge={"delhi": "Delhi"})
    assert outcome.values.tolist()[:3] == ["Delhi", "Delhi", "Pune"]
    assert outcome.changed == 2


def test_category_merge_keys_are_the_spellings_the_parser_sees() -> None:
    """Merge keys used to be whitespace-collapsed ('new delhi'), so 'new  delhi' was never merged and
    re-detection reported the same variants after the approved fix."""
    frame = pd.DataFrame({"city": ["New Delhi"] * 5 + ["new  delhi"] * 3 + ["NEW DELHI", "Pune"]})
    (issue,) = find_format_issues(frame)
    assert issue.kind is FormatIssueKind.CATEGORY_VARIANTS
    assert issue.convertible == 4
    fixed = normalise_texts(frame["city"], merge=issue.params["merge"], strip=True)
    assert fixed.values.value_counts().to_dict() == {"New Delhi": 9, "Pune": 1}
    assert find_format_issues(pd.DataFrame({"city": fixed.values})) == ()
