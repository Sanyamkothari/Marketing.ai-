"""Lapse outcomes (Plan J M93): a grace period and tables that leave a customer out.

The golden files are `tests/fixtures/labels/lapse_recharges.csv` (the label's own table, mapped as
`activity`) and `lapse_port_outs.csv` (mapped as `other_event`). Every expected number below was
worked out by hand from them and written down before the code ran. The prediction date is
31 Mar 2024 and the snapshot time is inclusive, so a 30-day window is (31 Mar, 30 Apr] and a 30-day
window with 7 days' grace is (31 Mar, 7 May].

    customer  recharges                 port-out     30 days   30 + 7 grace   + leave out port-outs
    C01       10 Apr                    -            stays     stays          stays
    C02       31 Mar (the date itself)  -            lapsed    lapsed         lapsed
    C03       30 Apr (day 30)           -            stays     stays          stays
    C04       3 May (day 33, in grace)  -            lapsed    stays          stays
    C05       7 May (day 37, last day)  -            lapsed    stays          stays
    C06       8 May (day 38)            -            lapsed    lapsed         lapsed
    C07       never                     -            lapsed    lapsed         lapsed
    C08       1 Mar only                -            lapsed    lapsed         lapsed
    C09       15 Mar, 20 Apr            -            stays     stays          stays
    C10       never                     12 Apr       lapsed    lapsed         left out
    C11       never                     1 Jun        lapsed    lapsed         lapsed (port-out after)
    C12       never                     20 Mar       lapsed    lapsed         lapsed (port-out before)
    C13       5 Apr                     25 Apr       stays     stays          left out
    C14       30 Jun (the last event)   -            lapsed    lapsed         lapsed

So the 30-day label has 10 lapsed customers of 14, the label with 7 days' grace has 8 of 14, and
with port-outs left out it has 7 of 12. The extract ends on 30 Jun 2024: a second prediction date,
31 May, has a complete 30-day window (to 30 Jun) but not a 37-day one (to 7 Jul), so the grace
period makes it unfinished.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from engine.config import ConfigError
from engine.onboarding.labels import EXCLUDED_COLUMN, compile_label_query, future_window_clause
from engine.onboarding.labels import build_labels as build
from engine.onboarding.specs import LabelSpec, LabelType, SnapshotMode

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "labels"
CUSTOMERS = tuple(f"C{number:02d}" for number in range(1, 15))
PREDICTION_DATE = "2024-03-31"
LATER_DATE = "2024-05-31"

LAPSE_30 = LabelSpec(name="lapsed", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=30)
LAPSE_30_GRACE_7 = LabelSpec(
    name="lapsed", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=30, grace_days=7
)
LAPSE_30_GRACE_7_NO_PORT_OUTS = LabelSpec(
    name="lapsed",
    type=LabelType.EVENT_ABSENCE,
    role="activity",
    horizon_days=30,
    grace_days=7,
    exclude_roles=("other_event",),
)


def _read(name: str) -> pd.DataFrame:
    frame = pd.read_csv(GOLDEN / name, dtype={"entity_key": str})
    frame["event_time"] = pd.to_datetime(frame["event_time"])
    return frame


def _connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("activity", _read("lapse_recharges.csv"))
    con.register("other_event", _read("lapse_port_outs.csv"))
    return con


def _snapshots(*dates: str) -> pd.DataFrame:
    return pd.DataFrame(
        [(customer, pd.Timestamp(day)) for day in dates for customer in CUSTOMERS],
        columns=["entity_key", "snapshot_date"],
    )


def _label(spec: LabelSpec, *dates: str, mapped: frozenset[str] = frozenset({"activity", "other_event"})):
    return build(
        _connection(),
        spec,
        _snapshots(*(dates or (PREDICTION_DATE,))),
        drop_censored=True,
        mapped_roles=mapped,
        mode=SnapshotMode.PERIODIC,
        inclusive=True,
    )


def _lapsed(result) -> list[str]:  # type: ignore[no-untyped-def]
    rows = result.frame[result.frame["snapshot_date"] == pd.Timestamp(PREDICTION_DATE)]
    return sorted(rows.loc[rows["lapsed"] == 1, "entity_key"])


def test_a_30_day_lapse_label_with_7_days_grace_gives_the_hand_computed_count() -> None:
    result = _label(LAPSE_30_GRACE_7)
    assert _lapsed(result) == ["C02", "C06", "C07", "C08", "C10", "C11", "C12", "C14"]
    (stat,) = result.per_snapshot
    assert (stat.entities, stat.positives, stat.positive_rate) == (14, 8, round(8 / 14, 4))


def test_without_grace_the_same_file_gives_the_30_day_count() -> None:
    """The control: what the grace period changes is exactly C04 and C05, who came back inside it."""
    result = _label(LAPSE_30)
    assert _lapsed(result) == ["C02", "C04", "C05", "C06", "C07", "C08", "C10", "C11", "C12", "C14"]
    assert result.per_snapshot[0].positives == 10


def test_a_port_out_inside_the_window_leaves_the_customer_out() -> None:
    result = _label(LAPSE_30_GRACE_7_NO_PORT_OUTS)
    assert _lapsed(result) == ["C02", "C06", "C07", "C08", "C11", "C12", "C14"]
    assert sorted(result.frame["entity_key"]) == sorted(set(CUSTOMERS) - {"C10", "C13"})
    assert result.excluded == 2
    assert EXCLUDED_COLUMN not in result.frame.columns
    (stat,) = result.per_snapshot
    assert (stat.entities, stat.positives, stat.positive_rate) == (12, 7, round(7 / 12, 4))


def test_the_grace_period_counts_towards_an_unfinished_window() -> None:
    plain = _label(LAPSE_30, PREDICTION_DATE, LATER_DATE)
    graced = _label(LAPSE_30_GRACE_7, PREDICTION_DATE, LATER_DATE)
    assert [s.censored for s in plain.per_snapshot] == [False, False]
    assert [s.censored for s in graced.per_snapshot] == [False, True]
    (censored,) = [c for c in graced.checks if c.code == "LABEL_HORIZON_CENSORED"]
    assert "at least 37 days" in censored.suggestion
    assert censored.details["grace_days"] == 7
    assert set(graced.frame["snapshot_date"]) == {pd.Timestamp(PREDICTION_DATE)}


def test_both_windows_are_written_by_the_one_clause_and_the_guard_is_kept() -> None:
    sql = compile_label_query(LAPSE_30_GRACE_7_NO_PORT_OUTS, inclusive=True)
    assert future_window_clause(37, inclusive=True) in sql
    assert future_window_clause(37, inclusive=True, event_alias="x") in sql
    assert "INTERVAL 30 DAY" not in sql, "the grace period was not added to the window"


def test_a_query_without_its_window_is_still_label_window_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LABEL_WINDOW_NOT_ENFORCED` survives the grace period: SQL that lost the window is refused."""
    from engine.onboarding import labels
    from engine.onboarding.labels import LabelError

    monkeypatch.setattr(labels, "_select_sql", lambda **_: "SELECT 1")
    with pytest.raises(LabelError) as caught:
        compile_label_query(LAPSE_30_GRACE_7_NO_PORT_OUTS, inclusive=True)
    assert caught.value.code == "LABEL_WINDOW_NOT_ENFORCED"


def test_an_unmapped_table_to_leave_out_is_label_role_missing() -> None:
    result = _label(LAPSE_30_GRACE_7_NO_PORT_OUTS, mapped=frozenset({"activity"}))
    (check,) = result.checks
    assert check.code == "LABEL_ROLE_MISSING"
    assert check.details == {"role": "other_event", "label": "lapsed", "excluded_role": True}
    assert result.frame.empty


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "column", "column": "lapsed", "grace_days": 7},
        {"type": "column", "column": "lapsed", "exclude_roles": ("other_event",)},
        {"type": "event_absence", "role": "activity", "horizon_days": 30, "exclude_roles": ("activity",)},
        {"type": "event_absence", "role": "activity", "horizon_days": 30, "grace_days": 0},
    ],
)
def test_a_lapse_field_that_cannot_apply_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises((ConfigError, ValueError)):
        LabelSpec.model_validate({"name": "lapsed", **fields})


def test_an_unset_grace_period_leaves_every_saved_spec_and_hash_unchanged() -> None:
    """Additive: the new fields are not written while unset, so no existing spec's hash moves."""
    dumped = LAPSE_30.model_dump(mode="json")
    assert "grace_days" not in dumped and "exclude_roles" not in dumped
    assert LAPSE_30.window_days == 30 and LAPSE_30_GRACE_7.window_days == 37
    assert LAPSE_30_GRACE_7.model_dump(mode="json")["grace_days"] == 7


def test_a_campaigns_outcome_window_includes_the_grace_period(tmp_path: Path) -> None:
    """Outcomes ingested for a scoring run are complete only once the grace period has passed too."""
    import copy
    from datetime import UTC, datetime, timedelta

    from engine.config import ProblemType, RunMode, UseCaseConfig, load_use_case_document
    from engine.contracts import RunRecord, RunState
    from engine.scheduling.outcomes import outcome_window
    from engine.storage import LocalStorage

    document = copy.deepcopy(load_use_case_document("win-back-campaign"))
    finished = datetime(2026, 9, 1, tzinfo=UTC)
    run = RunRecord(
        run_id="r_20260901_000000_abcd",
        use_case_id="win-back-campaign",
        use_case_name="Win-back Campaign",
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=finished,
        finished_at=finished,
        upload_id="u_1",
        file_name="customers.csv",
        primary_key="customer_id",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="automl",
        engine_version="test",
    )
    storage = LocalStorage(tmp_path)
    plain = outcome_window(run, storage=storage, config=UseCaseConfig.model_validate(document))
    document["label"] = {**document["label"], "grace_days": 7}
    graced = outcome_window(run, storage=storage, config=UseCaseConfig.model_validate(document))
    assert plain.horizon_days == 90 and graced.horizon_days == 97
    assert graced.matures_at - plain.matures_at == timedelta(days=7)
