"""The goal fit judges what can be built: an event table fits a goal whose outcome it can define, counted
(`engine.doctor.buildable`), and the floors are checked on the table the goal would train on."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from engine.config import load_use_case
from engine.doctor.buildable import buildable_outcome, snapshot_for
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
