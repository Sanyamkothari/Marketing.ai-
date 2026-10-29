"""The content cache: a key that changes when the content changes, and a parse that reads the same cold and warm."""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest

from engine.agent.formats import parse_dates, parse_numbers, to_datetime_mixed
from engine.content_cache import ContentCache, clear_content_caches, content_digest, frame_digest

DATES = ["2024-01-05 10:00", "2024-01-06 11:30", "2024-01-05 10:00", None, "2024-02-01 08:15"]


def test_the_same_cells_give_the_same_digest_whichever_object_holds_them() -> None:
    first = pd.Series(DATES, name="when")
    again = pd.Series(list(DATES), name="when")
    assert first is not again
    assert content_digest(first) == content_digest(again)


@pytest.mark.parametrize(
    "changed",
    [
        pd.Series(["2024-01-05 10:01", *DATES[1:]], name="when"),  # one cell
        pd.Series([*DATES[:3], "", DATES[4]], name="when"),  # a blank written as empty text, not missing
        pd.Series([*DATES[:3], "nan", DATES[4]], name="when"),  # the text "nan" is not a missing cell
        pd.Series(DATES[::-1], name="when"),  # the order
        pd.Series(DATES, name="other"),  # the name
        pd.Series(DATES, name="when", index=[10, 11, 12, 13, 14]),  # the labels
        pd.Series(DATES[:4], name="when"),  # the length
    ],
)
def test_the_digest_changes_when_the_content_changes(changed: pd.Series) -> None:
    assert content_digest(changed) != content_digest(pd.Series(DATES, name="when"))


def test_a_number_and_its_text_and_a_dtype_change_are_different_content() -> None:
    assert content_digest(pd.Series([1, 2, 3])) != content_digest(pd.Series(["1", "2", "3"]))
    assert content_digest(pd.Series([1, 2, 3])) != content_digest(pd.Series([1.0, 2.0, 3.0]))
    assert (
        content_digest(pd.Series([1, "1"], dtype=object)) is None
    )  # mixed kinds in one column: never cached


def test_the_digest_of_a_table_follows_every_column_and_their_order() -> None:
    frame = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
    assert frame_digest(frame) == frame_digest(frame.copy())
    assert frame_digest(frame) != frame_digest(frame[["b", "a"]])
    changed = frame.copy()
    changed.loc[1, "a"] = "z"
    assert frame_digest(frame) != frame_digest(changed)


def test_a_cache_keeps_the_newest_values_and_computes_a_missing_key_every_time() -> None:
    cache: ContentCache[int] = ContentCache(2)
    calls: list[str] = []

    def make(name: str) -> int:
        calls.append(name)
        return len(calls)

    assert cache.get_or_compute("a", lambda: make("a")) == cache.get_or_compute("a", lambda: make("a"))
    cache.get_or_compute("b", lambda: make("b"))
    cache.get_or_compute("c", lambda: make("c"))  # "a" is the oldest: gone
    cache.get_or_compute("a", lambda: make("a"))
    assert calls == ["a", "b", "c", "a"]
    cache.get_or_compute(None, lambda: make("n"))
    cache.get_or_compute(None, lambda: make("n"))
    assert calls[-2:] == ["n", "n"]
    assert (cache.hits, cache.misses) == (1, 4)


def test_a_cache_with_a_budget_drops_the_oldest_and_never_keeps_one_that_is_too_big() -> None:
    cache: ContentCache[str] = ContentCache(10, budget=10, cost=len)
    cache.get_or_compute("a", lambda: "x" * 6)
    cache.get_or_compute("b", lambda: "y" * 6)  # 12 > 10: "a" goes
    assert len(cache) == 1
    assert cache.get_or_compute("big", lambda: "z" * 11) == "z" * 11
    assert len(cache) == 1  # too big to keep, and "b" was not pushed out for it
    cache.get_or_compute("b", lambda: "never")
    assert cache.hits == 1


def test_many_threads_asking_for_one_key_all_get_the_value() -> None:
    cache: ContentCache[int] = ContentCache(4)
    got: list[int] = []
    gate = threading.Barrier(6)

    def ask() -> None:
        gate.wait()
        got.append(cache.get_or_compute("k", lambda: 7))

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert got == [7] * 6


def test_a_parse_reads_the_same_cold_and_warm_and_hands_out_its_own_copy() -> None:
    series = pd.Series(DATES, name="when")
    clear_content_caches()
    cold = parse_dates(series, dayfirst=False)
    warm = parse_dates(pd.Series(list(DATES), name="when"), dayfirst=False)
    pd.testing.assert_series_equal(cold.values, warm.values)
    assert (cold.changed, cold.failed) == (warm.changed, warm.failed)
    warm.values.iloc[0] = pd.Timestamp("1999-01-01")  # a caller changing what it got...
    again = parse_dates(series, dayfirst=False)
    pd.testing.assert_series_equal(again.values, cold.values)  # ...changes nothing for the next one


def test_numbers_are_cached_by_how_they_were_read_as_well_as_what_was_read() -> None:
    series = pd.Series(["1.234,5", "2.000,25", "7"], name="amount")
    clear_content_caches()
    dot = parse_numbers(series, decimal=".")
    comma = parse_numbers(series, decimal=",")
    assert dot.values.tolist() != comma.values.tolist()
    assert parse_numbers(series, decimal=",").values.tolist() == comma.values.tolist()


@pytest.mark.parametrize("dayfirst", [None, True, False])
def test_reading_each_distinct_spelling_once_gives_the_cell_by_cell_answer(dayfirst: bool | None) -> None:
    cells = ["03/04/2025", "15/04/2025", "2025-04-03", "", None, "not a date", "03/04/2025"] * 40
    series = pd.Series(cells, name="when")
    expected = pd.to_datetime(series, errors="coerce", format="mixed", dayfirst=bool(dayfirst))
    got = to_datetime_mixed(series, dayfirst=dayfirst)
    assert got.index.equals(series.index) and got.name == "when"
    assert got.isna().tolist() == expected.isna().tolist()
    assert got.dropna().tolist() == expected.dropna().tolist()


@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_cells_with_different_utc_offsets_are_read_the_plain_way() -> None:
    series = pd.Series(["2024-01-01T10:00:00+01:00", "2024-01-01T10:00:00+05:00"] * 20)
    try:
        expected = pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError) as exc:
        with pytest.raises(type(exc)):
            to_datetime_mixed(series)
        return
    assert to_datetime_mixed(series).tolist() == expected.tolist()


def test_a_digest_of_a_column_with_missing_numbers_is_stable() -> None:
    assert content_digest(pd.Series([1.5, np.nan, 3.0])) == content_digest(pd.Series([1.5, np.nan, 3.0]))
