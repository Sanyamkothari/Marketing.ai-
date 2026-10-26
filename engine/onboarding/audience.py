"""The lapsed audience (v3 phase 0, B5): which `(entity, snapshot)` rows of the spine count at all.

A win-back model is asked about shoppers who have stopped buying, so a spine row is kept only where the
customer has had an event in `role` on or before the snapshot (a customer with no history is not "lapsed", they
have not started) and none in the `no_event_days` days up to and including it. That is `MAX(event_time) <=
snapshot - no_event_days days` over the events the point-in-time clause lets in. It is the library's own rule for
`library/online-retail/fetch.py` ("at least 90 whole days earlier, i.e. at or before 2011-06-12 00:00").

The one clause that keeps the future out is `features.point_in_time_join_clause`, the same function the feature
queries use, and the query goes through `features.assert_point_in_time` like theirs: a row can never be kept or
dropped because of something that happened after its snapshot. The filter runs on the spine, after
`build_snapshot_frame`, at train and score time alike, so the dataset a model learns from and the one it scores
describe the same shoppers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from engine.onboarding.features import (
    SNAPSHOT_VIEW,
    assert_point_in_time,
    point_in_time_join_clause,
)

if TYPE_CHECKING:
    import pandas as pd
    from duckdb import DuckDBPyConnection

__all__ = ["apply_audience", "compile_audience_query"]

_ROLE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def compile_audience_query(role: str, no_event_days: int, *, inclusive: bool) -> str:
    """The query naming the `(entity_key, snapshot_date)` rows of `snapshots` that are in the audience."""
    if _ROLE.match(role) is None or no_event_days < 1:
        raise ValueError(
            "The audience names a table by a plain name and a whole number of days of at least 1."
        )
    sql = (
        "SELECT s.entity_key, s.snapshot_date\n"
        f'FROM "{SNAPSHOT_VIEW}" s\n'
        f'JOIN "{role}" e\n'
        f"  ON {point_in_time_join_clause('e', 's', inclusive=inclusive)}\n"
        "GROUP BY s.entity_key, s.snapshot_date\n"
        f"HAVING MAX(e.event_time) <= s.snapshot_date - INTERVAL {int(no_event_days)} DAY"
    )
    assert_point_in_time(sql)
    return sql


def apply_audience(
    con: DuckDBPyConnection,
    snapshots: pd.DataFrame,
    role: str,
    no_event_days: int,
    *,
    inclusive: bool,
) -> pd.DataFrame:
    """`snapshots` less the rows outside the audience, in their order.

    `role` must be registered on `con` with `entity_key` and `event_time`. The rows are registered as the
    `snapshots` view for the query and left registered, so a caller that goes on to build features and labels
    from `con` builds them for the audience only."""
    con.register(SNAPSHOT_VIEW, snapshots)
    kept = con.execute(compile_audience_query(role, no_event_days, inclusive=inclusive)).df()
    out = snapshots.merge(kept, on=["entity_key", "snapshot_date"], how="inner").reset_index(drop=True)
    con.register(SNAPSHOT_VIEW, out)
    return out
