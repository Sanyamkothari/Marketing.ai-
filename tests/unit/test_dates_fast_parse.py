"""Reading a long date column through one shared format gives the answer reading each spelling alone gives."""

from __future__ import annotations

import pandas as pd
import pytest

from engine.agent import formats
from engine.agent.formats import parse_dates


def _alone(cells: list[str], dayfirst: bool) -> list[pd.Timestamp | None]:
    return [formats._one_datetime(text, dayfirst) for text in cells]


@pytest.mark.parametrize("dayfirst", [False, True])
@pytest.mark.parametrize(
    "template",
    ["{m}/{d}/{y} 8:26", "{d}/{m}/{y} 18:05", "{y}-{m:02d}-{d:02d} 08:26:00", "{d:02d}.{m:02d}.{y}"],
)
def test_a_shared_format_reads_every_spelling_as_it_would_be_read_alone(
    template: str, dayfirst: bool
) -> None:
    cells = [
        template.format(d=d, m=m, y=y) for y in range(2005, 2011) for d in range(1, 29) for m in range(1, 13)
    ]
    cells += ["13/1/2010 9:00", "1/13/2010 9:00", "not a date", "now"]  # spellings the format does not fit
    assert len(set(cells)) > formats._FAST_PARSE_MIN
    got = parse_dates(pd.Series(cells), dayfirst=dayfirst).values.tolist()
    want = _alone(cells, dayfirst)
    assert [None if pd.isna(g) else g for g in got] == want


def test_a_first_spelling_that_only_fits_the_other_order_gives_no_shared_format() -> None:
    assert formats._shared_format(pd.Series(["13/1/2010 9:00"]), dayfirst=False) is None
    assert formats._shared_format(pd.Series(["12/1/2010 8:26"]), dayfirst=False) == "%m/%d/%Y %H:%M"
