"""The goal fit judges what can be built: an event table fits a goal whose outcome it can define, counted
(`engine.doctor.buildable`), and the floors are checked on the table the goal would train on."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from engine.config import load_use_case
from engine.doctor.buildable import buildable_outcome, compile_build, snapshot_for
from engine.doctor.diagnose import LoadedFile, Offer, readiness
from engine.stages.ingest import profile_dataset

GOAL = "retail-win-back"


def log(shoppers: int, days: int = 420, seed: int = 1) -> pd.DataFrame:
    """An order log: a row per order line, ISO dates, a quantity and a unit price."""
    rng = np.random.default_rng(seed)
    rows = []
    for shopper in range(shoppers):
        first = int(rng.integers(0, max(days - 60, 1)))
        for day in sorted({first, *map(int, rng.integers(first, days, size=int(rng.integers(1, 6))))}):
            rows.append(
                {
                    "customer_id": f"C{shopper}",
                    "order_date": (pd.Timestamp("2023-01-01") + pd.Timedelta(days=day)).strftime("%Y-%m-%d"),
                    "quantity": int(rng.integers(1, 5)),
                    "unit_price": round(float(rng.uniform(1, 30)), 2),
                }
            )
    return pd.DataFrame(rows)


def loaded(frame: pd.DataFrame, config_root: Path) -> LoadedFile:
    profile = profile_dataset(
        frame,
        load_use_case(GOAL, config_root),
        upload_id="u_log",
        file_name="orders.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id="u_log", file_name="orders.csv", frame=frame, profile=profile)


def test_the_snapshot_is_the_latest_month_end_whose_window_is_over() -> None:
    assert snapshot_for(date(2023, 1, 1), date(2024, 2, 29), 90) == date(2023, 11, 30)
    assert snapshot_for(date(2023, 1, 1), date(2023, 3, 1), 90) is None, "history shorter than the window"


def test_an_event_table_is_buildable_with_counted_facts(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([loaded(log(1_500), config_root)], config, config_root)
    assert found is not None and found.enough
    assert (found.entity_key, found.time_column) == ("customer_id", "order_date")
    assert (found.quantity_column, found.amount_column) == ("quantity", "unit_price")
    assert found.window_days == 90 and found.label_type == "event_presence"
    assert 1_000 <= found.active_before <= found.ids and 200 <= found.yes <= found.active_before


def test_a_goal_that_may_only_clean_is_not_offered_a_build(config_root: Path) -> None:
    config = load_use_case("targeted-advertisement", config_root)
    assert "reshape" not in {str(level) for level in config.agent.levels}  # it may not combine rows
    assert buildable_outcome([loaded(log(1_500), config_root)], config, config_root) is None


def test_the_floors_are_counted_on_the_table_the_goal_would_train_on(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    offer = Offer(config=config, stage="Win-back")
    (big,) = readiness([loaded(log(1_500), config_root)], [offer], config_root)
    assert big.status == "buildable" and big.reason_code == "BUILDABLE"
    assert big.customers == big.buildable.active_before and big.yes_count == big.buildable.yes  # type: ignore[union-attr]
    (small,) = readiness([loaded(log(300), config_root)], [offer], config_root)
    assert small.status == "too_small" and small.reason_code == "NOT_ENOUGH"
    assert "at least 1,000" in small.why and small.needs


def test_a_log_with_no_second_snapshot_to_follow_is_too_small_not_buildable(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    short = log(1_500, days=60)
    (verdict,) = readiness(
        [loaded(short, config_root)], [Offer(config=config, stage="Win-back")], config_root
    )
    assert verdict.status == "too_small" and "not longer than" in verdict.why


def test_a_date_column_the_plan_already_parsed_is_still_the_date(config_root: Path) -> None:
    """After an accepted 'read it as dates' fix the column is a real date type, which a number reading would take
    for a count of nanoseconds: the build must still be found on the file as the plan leaves it."""
    parsed = log(1_500).assign(order_date=lambda f: pd.to_datetime(f["order_date"]))
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([loaded(parsed, config_root)], config, config_root)
    assert found is not None and found.time_column == "order_date" and found.enough
    assert found.amount_column == "unit_price", "the date is not offered as an amount"


def test_a_freed_frames_verdict_is_never_handed_to_a_new_frame(config_root: Path) -> None:
    from engine.doctor.buildable import _events

    for _ in range(50):  # a freed frame's id is reused soon: the new event table must still read as events
        assert _events(pd.DataFrame({"x": [1, 2, 3]})) is None
        assert _events(pd.DataFrame(log(300).to_dict("list"))) is not None


def heavy_log(customers: int = 600, rows_each: int = 1_000) -> pd.DataFrame:
    """`customers` shoppers with `rows_each` order lines each, over 14 months: few customers, many rows."""
    rng = np.random.default_rng(7)
    days = rng.integers(0, 420, size=customers * rows_each)
    return pd.DataFrame(
        {
            "customer_id": np.repeat([f"C{i:04d}" for i in range(customers)], rows_each),
            "order_date": (pd.Timestamp("2023-01-01") + pd.to_timedelta(days, unit="D")).strftime("%Y-%m-%d"),
            "quantity": rng.integers(1, 5, size=customers * rows_each),
            "unit_price": np.round(rng.uniform(1, 30, size=customers * rows_each), 2),
        }
    )


def sampled(whole: pd.DataFrame, rows: int, config_root: Path, *, reader: bool = True) -> LoadedFile:
    """`whole` as the diagnosis holds a large file: a random sample of its rows, the file's row count, and (with
    `reader`) every row of the ID and date columns read back on demand, in chunks."""
    sample = whole.sample(n=rows, random_state=3).sort_index().reset_index(drop=True)
    plain = loaded(sample, config_root)

    def chunks(columns: Sequence[str]) -> Iterator[pd.DataFrame]:
        for start in range(0, len(whole), 100_000):
            yield whole.iloc[start : start + 100_000][list(columns)].astype(str)

    return replace(plain, file_rows=len(whole), column_chunks=chunks if reader else None)


def test_customers_are_counted_on_every_row_never_scaled_up_from_a_sample(config_root: Path) -> None:
    """600 customers with 1,000 rows each: a 50,000-row sample has nearly all 600, and scaling that by
    600,000 / 50,000 would claim 7,200 and a goal that fails after approval. The floor (1,000 rows) is a hard rule:
    the customers are counted exactly on all rows, so the goal is too small."""
    whole = heavy_log()
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([sampled(whole, 50_000, config_root)], config, config_root)
    assert found is not None and found.exact
    assert found.ids == 600 and found.active_before <= 600 and not found.enough
    assert found.file_rows == found.rows_read == 600_000
    (verdict,) = readiness(
        [sampled(whole, 50_000, config_root)], [Offer(config=config, stage="Win-back")], config_root
    )
    assert verdict.status == "too_small" and verdict.customers == found.active_before


def test_a_sample_counted_on_its_own_rows_matches_the_whole_file(config_root: Path) -> None:
    """The counts on a sample with the whole file's reader are those of the whole file read at once."""
    whole = log(3_000)
    config = load_use_case(GOAL, config_root)
    at_once = buildable_outcome([loaded(whole, config_root)], config, config_root)
    from_sample = buildable_outcome([sampled(whole, 2_000, config_root)], config, config_root)
    assert at_once is not None and from_sample is not None and from_sample.exact
    keep = ("ids", "active_before", "yes", "snapshot", "enough")
    assert {k: getattr(from_sample, k) for k in keep} == {k: getattr(at_once, k) for k in keep}
    assert [a.model_dump() for a in from_sample.audiences] == [a.model_dump() for a in at_once.audiences]


def test_with_no_reader_of_the_whole_file_a_sample_never_reaches_the_floors_by_scaling(
    config_root: Path,
) -> None:
    whole = heavy_log()
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([sampled(whole, 50_000, config_root, reader=False)], config, config_root)
    assert found is not None and not found.exact and not found.enough
    assert "Counted on 50,000 of 600,000 rows only." in found.sentence
    assert not any(a.enough for a in found.audiences if a.audience_id == "quiet"), "quiet needs every row"


def test_a_goal_with_a_narrower_audience_counts_both_readings(config_root: Path) -> None:
    """retail-win-back is for shoppers who have gone quiet (`configs/outcomes.yaml`: audience): the facts count both
    readings, by code, and the code never picks one."""
    frame = log(1_500)
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([loaded(frame, config_root)], config, config_root)
    assert found is not None and found.snapshot is not None
    active, quiet = found.audiences
    assert (active.audience_id, quiet.audience_id, quiet.quiet_days) == ("active", "quiet", 90)
    assert (active.customers, active.yes) == (found.active_before, found.yes)
    days = pd.to_datetime(frame["order_date"])
    cut = pd.Timestamp(found.snapshot)
    known = set(frame.loc[days <= cut, "customer_id"])
    recent = set(frame.loc[(days > cut - pd.Timedelta(days=90)) & (days <= cut), "customer_id"])
    later = set(frame.loc[(days > cut) & (days <= cut + pd.Timedelta(days=90)), "customer_id"])
    assert quiet.customers == len(known - recent) and quiet.yes == len((known - recent) & later)
    assert f"{quiet.customers:,}" in found.sentence and "yours to say" in found.sentence
    assert "gone quiet" in quiet.label


def test_the_quiet_build_keeps_only_the_quiet_customers(config_root: Path) -> None:
    from engine.agent.sql_step import MAX_COMPILED_SQL_CHARS, run_sql

    frame = log(1_500)
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([loaded(frame, config_root)], config, config_root)
    assert found is not None and found.snapshot is not None
    _active, quiet = found.audiences
    for audience, want in ((None, found.active_before), (quiet, quiet.customers)):
        sql, description = compile_build(found, config, "t_orders", audience)
        built = run_sql(
            sql, {"orders": frame}, output_name="t", as_of=found.snapshot, max_chars=MAX_COMPILED_SQL_CHARS
        ).frame
        assert len(built) == want
        assert int(built["reactivated_90d"].sum()) == (found.yes if audience is None else quiet.yes)
    assert "no row in the 90 days" in description


def test_a_goal_that_names_no_audience_is_counted_one_way(config_root: Path) -> None:
    from engine.config import config_root as root_of
    from engine.doctor.outcomes import load_outcome_catalogue

    named = {k for k, t in load_outcome_catalogue(root_of(config_root)).templates.items() if t.audience}
    assert named == {GOAL}, "only the goals whose configuration names a narrower audience"
