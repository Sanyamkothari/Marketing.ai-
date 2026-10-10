"""The intake rules on a grid of text (Plan L M150, DEC-1502): each fires on its shape and leaves a table alone."""

from __future__ import annotations

import pytest

from engine.intake.config import IntakeConfig
from engine.intake.grid import (
    find_footer,
    find_header,
    keep_columns,
    missing_word_columns,
    name_columns,
)

CONFIG = IntakeConfig()
WORDS = frozenset(CONFIG.missing_words)


def rows(text: str) -> list[list[str]]:
    return [line.split(",") if line else [] for line in text.strip("\n").split("\n")]


# ---------------------------------------------------------------------------
# The header row
# ---------------------------------------------------------------------------
def test_an_ordinary_table_keeps_its_first_row() -> None:
    decision = find_header(rows("id,plan,spend\n1,Gold,10\n2,Silver,20"), CONFIG)
    assert (decision.header_index, decision.group_index, decision.moved) == (0, None, False)


def test_title_and_date_rows_above_the_names_are_skipped() -> None:
    grid = rows("Customer report,,\nGenerated 2026-10-01,,\n\nid,plan,spend\n1,Gold,10\n2,Silver,20")
    decision = find_header(grid, CONFIG)
    assert decision.header_index == 3
    assert decision.rows_above == 3
    assert decision.data_start == 4


def test_a_label_and_value_line_counts_as_a_title() -> None:
    grid = rows("Branch:,Pune,,,\nid,plan,spend,region,tier\n1,Gold,10,W,A")
    assert find_header(grid, CONFIG).header_index == 1


def test_a_first_row_with_three_filled_cells_is_the_header_whatever_it_says() -> None:
    """A header with some names missing is still the header: rows below it are never promoted."""
    grid = rows("id,plan,spend,,,,,\nA,B,C,D,E,F,G,H\nI,J,K,L,M,N,O,P")
    assert find_header(grid, CONFIG).header_index == 0


def test_a_file_of_numbers_with_no_names_is_left_alone() -> None:
    assert find_header(rows("1,2,3\n4,5,6\n7,8,9"), CONFIG).header_index == 0


def test_a_single_column_file_is_left_alone() -> None:
    assert find_header(rows("Customers\nC1\nC2"), CONFIG).header_index == 0


def test_names_further_down_than_the_search_are_not_looked_for() -> None:
    grid = [["Note line"]] * 31 + [["id", "plan", "spend"], ["1", "Gold", "10"]]
    assert find_header(grid, CONFIG).header_index == 0


def test_a_header_with_nothing_under_it_is_not_taken() -> None:
    assert find_header(rows("Title,,\n\nid,plan,spend"), CONFIG).header_index == 0


# ---------------------------------------------------------------------------
# Two header rows
# ---------------------------------------------------------------------------
def test_group_labels_join_the_repeated_names_under_them() -> None:
    grid = rows("id,Q1,,Q2,\n,Revenue,Units,Revenue,Units\nC1,1,2,3,4")
    decision = find_header(grid, CONFIG)
    assert (decision.group_index, decision.header_index, decision.data_start) == (0, 1, 2)
    names, renamed = name_columns(grid[1], grid[0], list(range(5)))
    assert names == ["id", "Q1 Revenue", "Q1 Units", "Q2 Revenue", "Q2 Units"]
    assert {change.reason for change in renamed} == {"joined"}


def test_a_small_group_row_above_the_names_is_joined_too() -> None:
    grid = rows("Sales export,,,,\n,Q1,,Q2,\nid,Revenue,Units,Revenue,Units\nC1,1,2,3,4")
    decision = find_header(grid, CONFIG)
    assert (decision.rows_above, decision.group_index, decision.header_index) == (1, 1, 2)


def test_names_that_do_not_repeat_keep_their_own_name_under_a_group() -> None:
    names, _ = name_columns(
        ["id", "Revenue", "Units", "Revenue", "Notes"], ["", "Q1", "", "Q2", ""], list(range(5))
    )
    assert names == ["id", "Q1 Revenue", "Units", "Q2 Revenue", "Notes"]


def test_a_fully_named_row_over_a_repeating_one_is_not_a_group_row() -> None:
    """No merged-cell shadow (no gap) in the upper row: it is the header, and the repeat is a data value."""
    grid = rows("id,city,branch\nC1,Pune,Pune\nC2,Agra,Delhi")
    decision = find_header(grid, CONFIG)
    assert (decision.header_index, decision.group_index) == (0, None)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def test_repeated_names_get_a_number_and_blank_ones_the_pandas_name() -> None:
    names, renamed = name_columns(["id", "spend", "", "spend", "spend_2"], None, list(range(5)))
    assert names == ["id", "spend", "Unnamed: 2", "spend_3", "spend_2"]
    assert [(change.position, change.reason) for change in renamed] == [(3, "blank"), (4, "repeated")]


def test_names_are_compared_trimmed_as_ingest_compares_them() -> None:
    names, _ = name_columns(["spend", "spend "], None, [0, 1])
    assert names == ["spend", "spend_2"]


def test_a_column_with_no_name_and_no_value_is_left_out_and_one_with_values_kept() -> None:
    header = ["id", "plan", "", ""]
    sample = [["1", "Gold", "", "x"], ["2", "Silver", "", ""]]
    assert keep_columns(header, sample) == ([0, 1, 3], 1)


# ---------------------------------------------------------------------------
# Footer rows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "last",
    [
        "Grand Total,,600",
        "TOTAL:,,600",
        "Total (INR),,600",
        ",Sub-total,600",
        "Generated by CRM on 3 Oct,,",
        "Source: billing,,",
    ],
)
def test_total_and_note_rows_at_the_end_are_found(last: str) -> None:
    grid = rows(f"C1,Gold,100\nC2,Silver,200\n{last}")
    span, labels = find_footer(grid, 3, CONFIG)
    assert span == 1
    assert labels == [next(cell for cell in last.split(",") if cell)]


@pytest.mark.parametrize(
    "last",
    [
        "C3,Total Care,300",  # a plan called Total Care
        "John Smith,,",  # a customer with only a name: two words, no colon
        "C3,,",  # an ID alone
    ],
)
def test_data_rows_at_the_end_are_kept(last: str) -> None:
    grid = rows(f"C1,Gold,100\nC2,Silver,200\n{last}")
    assert find_footer(grid, 3, CONFIG) == (0, [])


def test_a_total_word_past_the_first_three_columns_is_data() -> None:
    grid = rows("C1,a,b,c,d\n,,,,Total")
    assert find_footer(grid, 5, CONFIG) == (0, [])


def test_a_note_needs_a_table_at_least_three_columns_wide() -> None:
    assert find_footer(rows("C1,1\nOne more customer here,"), 2, CONFIG) == (0, [])


def test_empty_rows_between_and_after_footers_are_in_the_span() -> None:
    grid = rows("C1,Gold,100\n\nGrand Total,,100\n,,\nPrinted by the export tool,,\n,,")
    span, labels = find_footer(grid, 3, CONFIG)
    assert span == 4  # the empty row above the total is data's, not the footer's
    assert labels == ["Grand Total", "Printed by the export tool"]


def test_footer_rows_are_capped() -> None:
    grid = rows("C1,Gold,100\n" + "\n".join(["Total,,1"] * 12))
    span, labels = find_footer(grid, 3, CONFIG)
    assert len(labels) == CONFIG.max_footer_rows
    assert span == CONFIG.max_footer_rows


def test_the_last_data_row_is_never_left_out() -> None:
    assert find_footer(rows("Total,,1"), 3, CONFIG) == (0, [])


# ---------------------------------------------------------------------------
# Missing words
# ---------------------------------------------------------------------------
def test_missing_words_are_found_in_number_date_and_yes_no_columns_only() -> None:
    sample = [
        ["12", "2024-01-05", "Y", "North", "unknown"],
        ["-", "not known", "N", "unknown", "x"],
        ["24", "2024-02-07", "N/A", "South", "y"],
        ["36", "2024-03-09", "Y", "NA", "z"],
    ]
    typed, seen = missing_word_columns(sample, 5, WORDS)
    assert typed == [0, 1, 2]
    assert seen == {0: {"-"}, 1: {"not known"}, 2: {"n/a"}}


def test_a_numbers_column_without_words_is_typed_but_not_seen() -> None:
    typed, seen = missing_word_columns([["1"], ["2"], ["3"]], 1, WORDS)
    assert (typed, seen) == ([0], {})


def test_a_column_of_only_words_is_not_typed() -> None:
    assert missing_word_columns([["-"], ["unknown"]], 1, WORDS) == ([], {})


def test_negative_numbers_are_numbers_not_dashes() -> None:
    typed, seen = missing_word_columns([["-5"], ["-"], ["3"]], 1, WORDS)
    assert (typed, seen) == ([0], {0: {"-"}})


def test_a_title_above_a_two_column_table_is_skipped() -> None:
    assert (
        find_header(rows("Campaign outcomes,\ncustomer_id,converted\nC1,1\nC2,0"), CONFIG).header_index == 1
    )


def test_a_two_column_table_without_a_title_keeps_its_first_row() -> None:
    assert find_header(rows("customer_id,converted\nC1,1\nC2,0"), CONFIG).header_index == 0
