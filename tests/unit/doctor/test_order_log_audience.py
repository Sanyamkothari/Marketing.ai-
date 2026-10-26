"""The lapsed audience in the order-log path's plan and outcome (v3 phase 0, B5).

The audience (`audience: {role: orders, no_event_days: 90}` on retail-win-back) is passed explicitly, never inferred. It
is in the plan's hash and sentence and in the outcome definition's hash and sentence, it cuts the outcome's counts to
the rows the built dataset will have, and without it nothing moves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.config import get_roles, load_use_case
from engine.doctor.build_driver import BuildDriveError, OrderLogInput, approval_for, drive_builder, plan_build
from engine.doctor.config import AudienceConfig
from engine.doctor.contracts import Diagnosis
from engine.doctor.diagnose import diagnose_file
from engine.doctor.order_log import derive_customer_list, derived_entity, order_log_reading
from engine.doctor.order_roll_up import audience_words, find_log_columns
from engine.doctor.outcome_definition import OutcomeError, propose_event_outcome
from engine.doctor.outcomes import OutcomeFile, load_outcome_catalogue
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.m237_support import RecordingBuilder
from tests.unit.doctor.order_log_support import USE_CASE, loaded

NOW = datetime(2026, 10, 26, tzinfo=UTC)
LOG = "u_log"
NAME = "online-retail.csv"
WINDOW = 90
LAPSED = 90


@pytest.fixture
def world(config_root: Path):  # type: ignore[no-untyped-def]
    config = load_use_case(USE_CASE, config_root)
    roles = get_roles(config_root)
    log = order_log_frame(customers=300)
    file = loaded(log, NAME, config_root, LOG)
    diagnosis = diagnose_file(file, config, roles)
    reading = order_log_reading(file, config)
    assert reading is not None
    derived = derive_customer_list(
        log, "CustomerID", "InvoiceDate", order_column="InvoiceNo", quantity_column="Quantity"
    )
    entity, events = derived_entity(
        OutcomeFile(diagnosis, log), derived, roles, reading, quantity_column="Quantity"
    )
    columns = find_log_columns(log, customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
    whole = Diagnosis(files=(diagnosis,), use_cases=(), links=(), stacks=())
    return config, roles, load_outcome_catalogue(config_root), log, entity, events, whole, columns


def moment(text: str) -> datetime:
    return datetime.strptime(text, "%m/%d/%Y %H:%M")


def expected_counts(log: pd.DataFrame, measured_on: date) -> tuple[int, int]:
    """Shoppers who had ordered by `measured_on` and whose last order on or before it is 90 or more days earlier,
    and of them those with an order in the 90 days after it; counted line by line."""
    orders: dict[float, list[datetime]] = {}
    for row in log.itertuples(index=False):
        if row.CustomerID != row.CustomerID or row.Quantity <= 0:
            continue
        orders.setdefault(row.CustomerID, []).append(moment(row.InvoiceDate))
    start = datetime.combine(measured_on, datetime.min.time())
    lapsed = [
        shopper
        for shopper, times in orders.items()
        if any(when <= start for when in times)
        and max(when for when in times if when <= start) <= start - timedelta(days=LAPSED)
    ]
    yes = [
        shopper
        for shopper in lapsed
        if any(start < when <= start + timedelta(days=WINDOW) for when in orders[shopper])
    ]
    return len(lapsed), len(yes)


def audience(role: str = "orders", days: int = LAPSED) -> AudienceConfig:
    return AudienceConfig(role=role, no_event_days=days)


def test_retail_win_back_declares_the_audience_the_path_passes(world) -> None:  # type: ignore[no-untyped-def]
    config = world[0]
    assert config.audience is not None
    assert (config.audience.role, config.audience.no_event_days) == ("orders", 90)


def test_the_outcome_is_counted_on_the_lapsed_rows_only(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, log, entity, events, _, _ = world
    everyone = propose_event_outcome(
        [events], config, roles, catalogue, kind="event_presence", derived_entity=entity, days=WINDOW
    )
    lapsed = propose_event_outcome(
        [events],
        config,
        roles,
        catalogue,
        kind="event_presence",
        derived_entity=entity,
        days=WINDOW,
        audience=config.audience,
    )
    assert lapsed.blocking == () and lapsed.counts is not None and everyone.counts is not None
    counts = lapsed.counts
    rows, yes = expected_counts(log, counts.measured_on)  # type: ignore[arg-type]
    assert (counts.rows, counts.yes, counts.no) == (rows, yes, rows - yes)
    assert 0 < counts.rows < everyone.counts.rows
    # The audience is in the meaning, so in the hash an approval names, and in the sentence a person reads.
    assert lapsed.definition is not None and everyone.definition is not None
    assert lapsed.definition.definition_hash != everyone.definition.definition_hash
    assert lapsed.definition.audience is not None and everyone.definition.audience is None
    assert lapsed.sentence is not None and everyone.sentence is not None
    assert audience_words("orders", LAPSED, rows="a place in the count") in lapsed.sentence
    assert "lapsed" not in everyone.sentence
    assert f"{counts.yes:,} of {counts.rows:,} shoppers" in lapsed.sentence


def test_without_an_audience_the_definition_dump_and_hash_are_what_they_were(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, _, entity, events, _, _ = world
    plain = propose_event_outcome(
        [events], config, roles, catalogue, kind="event_presence", derived_entity=entity, days=WINDOW
    )
    assert plain.definition is not None
    assert "audience" not in plain.definition.model_dump(mode="json")


def test_an_audience_that_reads_a_table_the_outcome_does_not_is_refused(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, _, entity, events, _, _ = world
    with pytest.raises(OutcomeError) as raised:
        propose_event_outcome(
            [events],
            config,
            roles,
            catalogue,
            kind="event_presence",
            derived_entity=entity,
            days=WINDOW,
            audience=audience("returns"),
        )
    assert raised.value.code == "OUTCOME_CANNOT_MEASURE" and "'returns'" in raised.value.message


def plan_with(world, chosen: AudienceConfig | None):  # type: ignore[no-untyped-def]
    config, _, _, log, _, _, diagnosis, columns = world
    return plan_build(
        diagnosis,
        {LOG: log},
        use_case=USE_CASE,
        label_spec=config.order_log_label,
        order_log=OrderLogInput(LOG, columns),
        suggested=config.order_log_features,
        audience=chosen,
    )


def test_the_audience_is_in_the_plan_hash_and_the_build_sentence(world) -> None:  # type: ignore[no-untyped-def]
    plain = plan_with(world, None)
    lapsed = plan_with(world, world[0].audience)
    assert lapsed.blocking == () and lapsed.audience is not None
    assert lapsed.plan_hash != plain.plan_hash
    assert audience_words("orders", LAPSED, rows="a row") in lapsed.approval_sentence
    assert "lapsed" not in plain.approval_sentence
    other = plan_with(world, audience(days=60))
    assert other.plan_hash != lapsed.plan_hash and other.approval_sentence != lapsed.approval_sentence


def test_a_plan_without_an_audience_has_none_in_its_dump(world) -> None:  # type: ignore[no-untyped-def]
    assert "audience" not in plan_with(world, None).model_dump(mode="json")
    assert plan_with(world, world[0].audience).model_dump(mode="json")["audience"]["no_event_days"] == 90


def test_an_audience_on_a_table_the_log_does_not_give_blocks_the_plan(world) -> None:  # type: ignore[no-untyped-def]
    plan = plan_with(world, audience("activity"))
    assert "BUILD_PLAN_INCOMPLETE" in plan.blocking
    with pytest.raises(BuildDriveError) as raised:
        drive_builder(
            plan,
            {LOG: world[3]},
            RecordingBuilder(),
            approval_for(plan, user_id="analyst-7", at=NOW),
        )
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"


def test_the_recipe_the_builder_is_given_carries_the_audience(world) -> None:  # type: ignore[no-untyped-def]
    plan = plan_with(world, world[0].audience)
    port = RecordingBuilder()
    drive_builder(plan, {LOG: world[3]}, port, approval_for(plan, user_id="analyst-7", at=NOW))
    saved = next(call for call in port.calls if call[0] == "save_spec")
    assert saved[1]["audience"] == {"role": "orders", "no_event_days": 90}
    plain = plan_with(world, None)
    quiet = RecordingBuilder()
    drive_builder(plain, {LOG: world[3]}, quiet, approval_for(plain, user_id="analyst-7", at=NOW))
    assert "audience" not in next(call for call in quiet.calls if call[0] == "save_spec")[1]
