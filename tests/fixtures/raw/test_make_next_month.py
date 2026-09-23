"""`make_next_month`: the same client's extract a month on (Plan A M35's score-mode fixture).

Score mode replays last month's recipe onto this month's files, so the fixture has to be what a
monthly export is: the same files, the same columns, the same customers, and every event table a
month longer - with nothing moved in the rows already there.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.raw.make_raw import EVENT_DATES, NEXT_MONTH_DAYS, RawTables, make_next_month, make_raw


@pytest.fixture(scope="module")
def months(tmp_path_factory: pytest.TempPathFactory) -> tuple[RawTables, RawTables]:
    root = tmp_path_factory.mktemp("months")
    first = make_raw(root / "first", customers=60, usage_rows=300)
    return first, make_next_month(first, root / "second")


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def test_every_file_keeps_its_name_and_columns(months: tuple[RawTables, RawTables]) -> None:
    first, second = months
    for old, new in zip(first.paths, second.paths, strict=True):
        assert old.name == new.name
        assert list(read(old).columns) == list(read(new).columns)


def test_the_customer_master_is_unchanged(months: tuple[RawTables, RawTables]) -> None:
    first, second = months
    assert first.customers.read_bytes() == second.customers.read_bytes()


def test_every_event_table_is_last_months_rows_plus_a_month_more(months: tuple[RawTables, RawTables]) -> None:
    first, second = months
    for old, new in zip(first.paths, second.paths, strict=True):
        dated = EVENT_DATES.get(old.name)
        if dated is None:
            continue
        before, after = read(old), read(new)
        pd.testing.assert_frame_equal(after.head(len(before)), before)
        added = after.iloc[len(before) :]
        assert len(added) > 0, old.name
        last = pd.to_datetime(before[dated[0]]).max()
        assert (pd.to_datetime(added[dated[0]]) > last).all(), old.name
        assert pd.to_datetime(after[dated[0]]).max() == last + pd.Timedelta(days=NEXT_MONTH_DAYS)
        assert set(added["CUST_ID"]) <= set(read(first.customers)["CUST_ID"])


def test_the_same_extract_always_gives_the_same_next_month(
    months: tuple[RawTables, RawTables], tmp_path: Path
) -> None:
    first, second = months
    again = make_next_month(first, tmp_path / "again")
    for one, two in zip(second.paths, again.paths, strict=True):
        assert one.read_bytes() == two.read_bytes()


def test_a_month_of_no_days_is_refused(months: tuple[RawTables, RawTables], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_next_month(months[0], tmp_path / "none", days=0)
