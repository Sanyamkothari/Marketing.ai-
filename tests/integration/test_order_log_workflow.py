"""The order-log workflow on a seeded sample of the real UCI Online Retail log (v3 phase 0, B6 pieces and B7).

`tests/fixtures/online_retail/` holds about 1,000 of the file's 4,372 customers, every line of each, and guest
checkouts in proportion (114,440 lines), and a slice of 46 customers that is too small. Through the in-process port
(`LocalBuilderPort`: the onboarding routes' own functions, in this process), `build_from_order_log` rolls the log up,
builds the lapsed-shopper table month-end by month-end and runs the readiness check on it. What is shown:

* the sample reaches `ready` (or `ready_with_warnings`) with the engine's minimums unchanged (1,000 rows, 200 positives);
* every row is a lapsed shopper at its month-end, and every such shopper has a row, worked out independently from the
  raw lines (the audience is point in time, at train and at score time);
* at 2011-08-31 the built table is `library/online-retail/fetch.py`'s own table for that day, formula by formula, with
  the differences the path's rules give (named in `test_the_library_cross_check`);
* the slice honestly comes out `ROWS_TOO_FEW`.

Everything here is slow (the date profiling of 114,440 lines alone takes seconds): CI runs it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI

from api.main import create_app
from api.routes.clients import get_client_store
from api.routes.schedules import app_request
from engine.access.roles import Principal, Role
from engine.config import get_roles, load_use_case
from engine.doctor.build_driver import approval_for as build_approval
from engine.doctor.build_driver import replay_build
from engine.doctor.builder_local import LocalBuilderPort
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.order_log import blank_customer_question, derive_customer_list, order_log_reading
from engine.doctor.order_log_workflow import (
    GuestsAnswer,
    OrderLogApprovals,
    OrderLogBuild,
    OrderLogWorkflowError,
    build_from_order_log,
    propose_order_log,
)
from engine.doctor.outcome_definition import approval_for as outcome_approval
from tests.fixtures.online_retail import small_log, uci_sample
from tests.fixtures.order_log import order_log_config_root
from tests.unit.doctor.order_log_support import USE_CASE, loaded

pytestmark = [pytest.mark.integration, pytest.mark.slow]

NOW = datetime(2026, 10, 26, tzinfo=UTC)
NAME = "online-retail.csv"
MONTH_ENDS = (
    date(2011, 3, 31),
    date(2011, 4, 30),
    date(2011, 5, 31),
    date(2011, 6, 30),
    date(2011, 7, 31),
    date(2011, 8, 31),
)
MIN_ROWS, MIN_POSITIVE = 1_000, 200
"""`min_rows` and `min_positive` of `configs/engine.yaml`: unchanged by the path, and met by the sample on their own."""
READY = {"ready", "ready_with_warnings"}
ANALYST = Principal(user_id="analyst-7", username="asha", roles=frozenset({Role.ANALYST}))
FETCH = Path(__file__).resolve().parents[2] / "library" / "online-retail" / "fetch.py"


@dataclass
class World:
    app: FastAPI
    root: Path
    client_id: str
    port: LocalBuilderPort
    log: pd.DataFrame
    file: LoadedFile
    answer: GuestsAnswer
    built: OrderLogBuild
    dataset: pd.DataFrame


def guests_answer(file: LoadedFile, root: Path) -> GuestsAnswer:
    """The yes a person gives to leaving out the lines with no shopper, tied to the guard's finding."""
    reading = order_log_reading(file, load_use_case(USE_CASE, root))
    assert reading is not None
    derived = derive_customer_list(
        file.frame, "CustomerID", reading.date_column, order_column="InvoiceNo", quantity_column="Quantity"
    )
    question = blank_customer_question(reading, derived)
    assert (
        question is not None and question[2] is not None
    ), "over the guard's limit, so the yes carries its key"
    return GuestsAnswer(option_id="leave_out", finding_key=question[2].key)


def approve_and_build(
    file: LoadedFile, root: Path, port: LocalBuilderPort
) -> tuple[OrderLogBuild, GuestsAnswer]:
    """What a person does: read both sentences (`propose_order_log`), approve them, and build."""
    config = load_use_case(USE_CASE, root)
    diagnosis = diagnose_file(file, config, get_roles(root))
    answer = guests_answer(file, root)
    proposal = propose_order_log(file, "CustomerID", answer, USE_CASE, config_root=root, diagnosis=diagnosis)
    assert proposal.outcome.definition is not None
    approvals = OrderLogApprovals(
        outcome=outcome_approval(
            proposal.outcome.definition, proposal.outcome_sentence, user_id="analyst-7", at=NOW
        ),
        build=build_approval(proposal.plan, user_id="analyst-7", at=NOW),
    )
    built = build_from_order_log(
        file,
        "CustomerID",
        answer,
        USE_CASE,
        approvals,
        port,
        approver=ANALYST,
        config_root=root,
        diagnosis=diagnosis,
    )
    return built, answer


def new_port(app: FastAPI) -> tuple[str, LocalBuilderPort]:
    client_id = get_client_store(app_request(app)).create_client("Gifts Co", "ecommerce", notes="").client_id
    return client_id, LocalBuilderPort.for_app(app, client_id)


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> World:
    tmp = tmp_path_factory.mktemp("uci")
    root = order_log_config_root(config_root, tmp)
    app = create_app(config_root=root, data_dir=tmp / "data")
    client_id, port = new_port(app)
    log = uci_sample()
    file = loaded(log, NAME, root)
    built, answer = approve_and_build(file, root, port)
    dataset = port.read_dataset(built.dataset_id).frame
    return World(app, root, client_id, port, log, file, answer, built, dataset)


def test_the_sample_is_a_real_by_customer_sample_of_the_uci_log(world: World) -> None:
    log = world.log
    named = log["CustomerID"].notna()
    assert log["CustomerID"][named].nunique() == 1_000
    assert 0.2 < (~named).mean() < 0.3, "guest checkouts in proportion (24.9% of the full file's lines)"
    # Sampled by customer: each shopper's order numbers belong to that shopper alone and are all there.
    owners = log.loc[named].groupby("InvoiceNo")["CustomerID"].nunique()
    assert int(owners.max()) == 1
    assert list(log.columns) == [
        "InvoiceNo",
        "StockCode",
        "Description",
        "Quantity",
        "InvoiceDate",
        "UnitPrice",
        "CustomerID",
        "Country",
    ]


def test_the_sample_reaches_ready_with_the_minimums_unchanged(world: World) -> None:
    built = world.built
    assert built.readiness.verdict in READY, built.readiness.verdict_reason
    assert built.readiness.dry_run_passed
    assert built.counts.rows >= MIN_ROWS and built.counts.yes >= MIN_POSITIVE
    assert built.counts.rows == len(world.dataset) == built.counts.yes + built.counts.no
    assert built.counts.snapshot_dates == MONTH_ENDS
    # Every line of the log is accounted for: used, or left out for one named reason.
    log = built.counts.log
    assert log["lines"] == len(world.log)
    assert log["lines_without_shopper"] == int(world.log["CustomerID"].isna().sum())
    assert log["shoppers"] == 1_000
    assert world.dataset["reactivated_90d"].isin([0, 1]).all()
    # The figures docs/v3/ORDER_LOG.md quotes for the sample, pinned here so the page cannot drift from the build.
    assert (built.counts.rows, built.counts.yes, built.counts.no) == (1_190, 413, 777)
    per_month = pd.to_datetime(world.dataset["snapshot_date"]).dt.date.value_counts().sort_index()
    assert tuple(per_month) == (83, 133, 166, 223, 257, 328)
    shares = world.dataset[["days_between_orders_mean", "returned_items"]].isna().mean().round(2)
    assert tuple(shares) == (0.76, 0.73)
    # The three readiness warnings: the two mostly blank columns, and the probe that could not count dropped lines.
    report = built.readiness.dry_run_report
    assert report is not None and "with 3 warning(s)" in built.readiness.verdict_reason
    warned = [(c.code, c.column) for c in report.checks if c.severity.value == "warning"]
    assert sorted(warned) == [
        ("HIGH_NULL_COLUMN", "days_between_orders_mean"),
        ("HIGH_NULL_COLUMN", "returned_items"),
    ]
    assert [probe.code for probe in built.readiness.probes if probe.status == "warn"] == ["X_PARSER_ROW_LOSS"]
    # The build report's own three warnings.
    built_report = world.port.read_dataset(built.dataset_id).report
    assert sorted(c.code for c in built_report.checks if c.severity.value == "warning") == [
        "ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
        "HIGH_NULL_COLUMN",
        "HIGH_NULL_COLUMN",
    ]


def orders_of(log: pd.DataFrame) -> pd.DataFrame:
    """The log's orders as the path reads them: lines with a shopper and a quantity above zero, timestamps parsed."""
    lines = log.loc[log["CustomerID"].notna() & (log["Quantity"] > 0)]
    return pd.DataFrame(
        {
            "shopper": lines["CustomerID"].astype(int).astype(str),
            "at": pd.to_datetime(lines["InvoiceDate"], format="%m/%d/%Y %H:%M"),
        }
    )


def lapsed_rows(log: pd.DataFrame, snapshot: date, *, window: int = 90) -> pd.DataFrame:
    """Shoppers with an order before `snapshot` began (its 00:00) whose last such order is `window` or more days
    earlier, with the outcome: an order after that instant and in the `window` days after it (an order later on the
    month-end day included). Worked out from the raw lines alone."""
    orders = orders_of(log)
    day = pd.Timestamp(snapshot)
    last = orders.loc[orders["at"] <= day].groupby("shopper")["at"].max()
    lapsed = last[last <= day - timedelta(days=window)].index
    later = set(orders.loc[(orders["at"] > day) & (orders["at"] <= day + timedelta(days=window)), "shopper"])
    return pd.DataFrame({"entity_key": list(lapsed), "reactivated_90d": [int(s in later) for s in lapsed]})


def test_every_row_is_a_lapsed_shopper_at_its_month_end_and_every_lapsed_shopper_has_one(
    world: World,
) -> None:
    built = world.dataset.assign(
        entity_key=world.dataset["entity_key"].astype(str),
        snapshot_date=pd.to_datetime(world.dataset["snapshot_date"]),
    )
    for snapshot in MONTH_ENDS:
        expected = lapsed_rows(world.log, snapshot).set_index("entity_key")["reactivated_90d"]
        got = built.loc[built["snapshot_date"] == pd.Timestamp(snapshot)].set_index("entity_key")[
            "reactivated_90d"
        ]
        assert set(got.index) == set(expected.index), snapshot
        assert (got.sort_index() == expected.sort_index()).all(), f"outcome at {snapshot}"
        assert len(expected) > 0
    assert built["entity_key"].duplicated().sum() > 0, "a shopper is in several month-ends"
    assert not built.duplicated(["entity_key", "snapshot_date"]).any()
    # The snapshot is the instant the month-end day begins: a lapsed shopper who orders later that very day keeps
    # their row, and that order is their 'yes' (the audience sentence says so). Six rows of the sample are such.
    orders = orders_of(world.log)
    same_day = set(zip(orders["shopper"], orders["at"].dt.normalize(), strict=True))
    on_the_day = built.loc[
        [(key, day) in same_day for key, day in zip(built["entity_key"], built["snapshot_date"], strict=True)]
    ]
    assert len(on_the_day) == 6 and (on_the_day["reactivated_90d"] == 1).all()


def test_a_scoring_build_has_the_same_audience_at_the_end_of_the_data(world: World) -> None:
    """The audience applies at score time too: one snapshot at the last day of the log, the same rule."""
    record = world.port.load_record(world.built.spec_id)
    replayed = replay_build(record, {NAME: [world.log]}, world.port, mode="score")
    assert replayed.dataset_id is not None
    scored = world.port.read_dataset(replayed.dataset_id)
    assert len(scored.manifest.snapshot_dates) == 1
    last = scored.manifest.snapshot_dates[0]
    assert last == date(2011, 12, 9)
    expected = lapsed_rows(world.log, last)
    assert set(scored.frame["entity_key"].astype(str)) == set(expected["entity_key"])
    assert 0 < len(scored.frame) < world.built.counts.rows


def load_fetch(snapshot: pd.Timestamp) -> ModuleType:
    """`library/online-retail/fetch.py` pointed at the sample and at `snapshot`: its own functions, its own formulas."""
    spec = importlib.util.spec_from_file_location("online_retail_fetch", FETCH)
    assert spec is not None and spec.loader is not None
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    fetch.RAW = (
        Path(__file__).resolve().parents[1] / "fixtures" / "online_retail" / "online_retail_sample.csv.gz"
    )
    fetch.SNAPSHOT = snapshot
    fetch.OUTCOME_END = snapshot + pd.Timedelta(days=fetch.OUTCOME_DAYS)
    return fetch


def test_the_library_cross_check(world: World) -> None:
    """At 2011-08-31, for every shopper, the built `orders_total`, `spend_total`, `order_value_mean/max`, `items_total`,
    `distinct_products`, `active_months`, `returned_orders`, `returned_items` and the outcome are the library's, from its
    own functions at that day. The audience (328 shoppers) is the same set.

    Four differences, each by a rule of the path, are pinned so that a change to any is seen:

    * `recency_days` and `tenure_days` are calendar days between the day of the order and the snapshot (the feature
      engine's `days_since_last` and `days_since_first`); the library counts whole 24-hour periods from the order's
      time. They differ by exactly one day for every shopper whose order has a time of day (all of them here);
    * `days_between_orders_mean` is the mean of whole days between consecutive orders, so it is that span in calendar
      days over (orders - 1), and is blank for a shopper with one order, where the library writes 0 (DEC-1822 (l));
    * `returned_items` is blank for a shopper with no return, where the library writes 0 (a sum over no rows);
    * `spend_total` and the order values are not rounded, the library rounds to 2 places.
    """
    fetch = load_fetch(pd.Timestamp("2011-08-31"))
    transactions = fetch.read_transactions()
    lib = fetch.build_rows(transactions).set_index("customer_id")
    built = world.dataset.assign(entity_key=world.dataset["entity_key"].astype(str))
    at = built.loc[pd.to_datetime(built["snapshot_date"]) == pd.Timestamp("2011-08-31")].set_index(
        "entity_key"
    )
    assert len(lib) == len(at) == 328
    assert set(lib.index) == set(at.index)
    shoppers = sorted(lib.index)
    lib, at = lib.loc[shoppers], at.loc[shoppers]

    for column in ("orders_total", "items_total", "distinct_products", "active_months", "returned_orders"):
        assert (at[column].astype(int) == lib[column].astype(int)).all(), column
    for column in ("spend_total", "order_value_mean", "order_value_max"):
        assert np.allclose(at[column].astype(float), lib[column].astype(float), atol=0.0051, rtol=0), column
    assert (at["reactivated_90d"].astype(int) == lib["reactivated_90d"].astype(int)).all()
    assert (at["country"] == lib["country"]).all()

    # Recency and tenure: one more day, and exactly the calendar-day count from the order's date.
    purchases = transactions.loc[
        ~transactions["is_cancellation"]
        & (transactions["Quantity"] > 0)
        & (transactions["UnitPrice"] > 0)
        & (transactions["InvoiceDate"] <= fetch.SNAPSHOT)
    ]
    last = purchases.groupby("CustomerID")["InvoiceDate"].max().loc[shoppers]
    first = purchases.groupby("CustomerID")["InvoiceDate"].min().loc[shoppers]
    assert ((at["recency_days"] - lib["recency_days"]) == 1).all()
    assert ((at["tenure_days"] - lib["tenure_days"]) == 1).all()
    assert (at["recency_days"] == (fetch.SNAPSHOT - last.dt.normalize()).dt.days).all()
    assert (at["tenure_days"] == (fetch.SNAPSHOT - first.dt.normalize()).dt.days).all()

    # Days between orders: blank for one order (library: 0); otherwise the calendar span over (orders - 1).
    single = at["orders_total"] == 1
    assert (
        at.loc[single, "days_between_orders_mean"].isna().all()
        and (lib.loc[single, "days_between_orders_mean"] == 0).all()
    )
    span = (last.dt.normalize() - first.dt.normalize()).dt.days
    gaps = (at["orders_total"] - 1).where(~single)
    assert np.allclose(
        at.loc[~single, "days_between_orders_mean"].astype(float), (span / gaps)[~single], atol=0.005, rtol=0
    )

    # Returned items: equal where there is a return, blank (library: 0) where there is none.
    returned = at["returned_orders"] > 0
    assert (
        at.loc[returned, "returned_items"].astype(int) == lib.loc[returned, "returned_items"].astype(int)
    ).all()
    assert (
        at.loc[~returned, "returned_items"].isna().all() and (lib.loc[~returned, "returned_items"] == 0).all()
    )
    assert returned.sum() > 0 and single.sum() > 0 and (~single).sum() > 0
    assert (int(single.sum()), int((~returned).sum())) == (220, 236), "the counts docs/v3/ORDER_LOG.md quotes"


def test_the_small_slice_honestly_comes_out_rows_too_few(world: World) -> None:
    log = small_log()
    assert 3_000 < len(log) < 6_000 and log["CustomerID"].nunique() < 100
    client_id, port = new_port(world.app)
    assert client_id != world.client_id
    with pytest.raises(OrderLogWorkflowError) as raised:
        approve_and_build(loaded(log, NAME, world.root), world.root, port)
    assert "ROWS_TOO_FEW" in str(raised.value) and "TARGET_TOO_FEW_POSITIVES" in str(raised.value)
    assert "At least 1,000 are needed" in raised.value.message, "the engine's own words, minimums unchanged"
