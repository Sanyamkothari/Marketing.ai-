"""The outcome from an order log's own customer list (v3 phase 0, B3): the two refusals are lifted only when the list
is passed explicitly, and every count is the label builder's, on shoppers who had started by each month-end.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from engine.config import get_roles, load_use_case
from engine.doctor.diagnose import diagnose_file
from engine.doctor.order_log import derive_customer_list, derived_entity, order_log_reading
from engine.doctor.outcome_definition import OutcomeError, propose_event_outcome
from engine.doctor.outcomes import (
    OutcomeCatalogue,
    OutcomeFile,
    OutcomeTemplate,
    load_outcome_catalogue,
    offer_outcome,
)
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.order_log_support import loaded

USE_CASE = "retail-win-back"
WINDOW = 90


def moment(text: str) -> datetime:
    return datetime.strptime(text, "%m/%d/%Y %H:%M")


@pytest.fixture
def world(config_root: Path):  # type: ignore[no-untyped-def]
    config = load_use_case(USE_CASE, config_root)
    roles = get_roles(config_root)
    log = order_log_frame(customers=300)
    file = loaded(log, "online-retail.csv", config_root)
    diagnosis = diagnose_file(file, config, roles)
    reading = order_log_reading(file, config)
    assert reading is not None
    derived = derive_customer_list(
        log, "CustomerID", "InvoiceDate", order_column="InvoiceNo", quantity_column="Quantity"
    )
    catalogue = load_outcome_catalogue(config_root)
    entity, events = derived_entity(
        OutcomeFile(diagnosis, log), derived, roles, reading, quantity_column="Quantity"
    )
    return config, roles, catalogue, log, OutcomeFile(diagnosis, log), entity, events


def expected_counts(log, measured_on: date) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Shoppers who had placed their first order by `measured_on`, and of them those with an order in the 90 days
    after it: `(T, T + 90 days]`, T being the start of the day, counted line by line."""
    first: dict[float, date] = {}
    orders: dict[float, list[datetime]] = {}
    for row in log.itertuples(index=False):
        if row.CustomerID != row.CustomerID or row.Quantity <= 0:
            continue
        when = moment(row.InvoiceDate)
        first[row.CustomerID] = min(when.date(), first.get(row.CustomerID, when.date()))
        orders.setdefault(row.CustomerID, []).append(when)
    start = datetime.combine(measured_on, datetime.min.time())
    existed = [shopper for shopper, day in first.items() if day <= measured_on]
    yes = [
        shopper
        for shopper in existed
        if any(start < when <= start + timedelta(days=WINDOW) for when in orders[shopper])
    ]
    return len(existed), len(yes)


def test_without_the_explicit_list_a_log_alone_is_still_refused(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, _log, file, _entity, _events = world
    assert (
        offer_outcome(config, catalogue.templates[USE_CASE], [file], catalogue=catalogue, roles=roles) is None
    )
    with pytest.raises(OutcomeError) as raised:
        propose_event_outcome([file], config, roles, catalogue, kind="event_presence", days=WINDOW)
    assert raised.value.code == "OUTCOME_CANNOT_MEASURE"
    assert "No file is a customer list with an ID column" in raised.value.message


def test_with_the_explicit_list_the_outcome_is_measured_on_shoppers_who_had_started(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, log, _file, entity, events = world
    proposal = propose_event_outcome(
        [events], config, roles, catalogue, kind="event_presence", derived_entity=entity, days=WINDOW
    )
    assert proposal.blocking == () and proposal.counts is not None and proposal.definition is not None
    counts = proposal.counts
    existed, yes = expected_counts(log, counts.measured_on)  # type: ignore[arg-type]
    assert (counts.rows, counts.yes, counts.no) == (existed, yes, existed - yes)
    assert 0 < yes < existed
    # The sentence the person approves is the one the label builder measured: name, window, counts.
    assert proposal.sentence is not None
    assert (
        "within 90 days after the snapshot" in proposal.sentence
        and f"{yes} of {existed} shoppers" in proposal.sentence
    )
    # Each month-end has fewer shoppers the earlier it is: the list is filtered by first order, not counted whole.
    assert counts.rows < len(entity.file.frame)


def test_the_offer_for_the_template_is_measured_the_same_way(world) -> None:  # type: ignore[no-untyped-def]
    config, roles, catalogue, log, _file, entity, events = world
    template = catalogue.templates[USE_CASE]
    assert template.order_log and template.order_log_only
    offer = offer_outcome(config, template, [events], catalogue=catalogue, roles=roles, derived_entity=entity)
    assert offer is not None
    choice = next(c for c in offer.choices if c.days == WINDOW)
    existed, yes = expected_counts(log, choice.measured_on)  # type: ignore[arg-type]
    assert (choice.customers, choice.yes) == (existed, yes)
    assert choice.label_spec.role == "orders" and choice.label_spec.name == "reactivated_90d"


def test_only_lines_with_a_quantity_above_zero_are_events(world) -> None:  # type: ignore[no-untyped-def]
    _config, _roles, _catalogue, log, _file, _entity, events = world
    assert len(events.frame) == int((log["Quantity"] > 0).sum()) < len(log)
    assert events.diagnosis.kind == "orders" and events.diagnosis.date_columns[0] == "InvoiceDate"
    assert events.diagnosis.rows == len(events.frame)


def test_the_retail_sentence_is_offered_only_while_the_switch_is_on(config_root: Path) -> None:
    catalogue = load_outcome_catalogue(config_root)
    config = load_use_case(USE_CASE, config_root)
    assert catalogue.template_for(config) is None, "off: the use case is offered what it was before"
    alone = config.model_copy(
        update={"data_doctor": config.data_doctor.model_copy(update={"order_log": True})}
    )
    assert catalogue.template_for(alone) is None, "the path needs `unified` too (order_log_enabled)"
    on = config.model_copy(
        update={"data_doctor": config.data_doctor.model_copy(update={"order_log": True, "unified": True})}
    )
    template = catalogue.template_for(on)
    assert isinstance(template, OutcomeTemplate) and template.default_days == 90
    # Win-back campaign already had its sentence and keeps it with the switch off.
    campaign = load_use_case("win-back-campaign", config_root)
    assert catalogue.template_for(campaign) is catalogue.templates["win-back-campaign"]
    assert (
        catalogue.templates["win-back-campaign"].order_log
        and not catalogue.templates["win-back-campaign"].order_log_only
    )
    # Only the templates marked for the path say so: not every one.
    marked = {name for name, t in catalogue.templates.items() if t.order_log}
    assert marked == {"retail-win-back", "win-back-campaign"}


def test_a_template_marked_only_for_the_path_must_be_marked_for_it() -> None:
    with pytest.raises(ValueError, match="order_log_only needs order_log"):
        OutcomeTemplate(
            question="q?",
            sentence="{An_entity} came back within {days} days.",
            label_type="event_presence",
            event_kinds=("orders",),
            days=(30,),
            default_days=30,
            order_log_only=True,
        )
    assert isinstance(OutcomeCatalogue(), OutcomeCatalogue)
