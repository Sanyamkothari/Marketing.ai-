"""Failure 10 of REAL_DATA_RESULTS: the mapping's time zone reaches the customer's events and state."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from autonomy.contracts import Channel, ChannelStats, CustomerState, EventType
from autonomy.data.folder import StateFolder
from autonomy.data.mapping import EventMapping, map_frame
from autonomy.decide.rules import customer_zone
from autonomy.decide.timing import choose_send_time
from autonomy.fakes.samples import sample_brief

FRAME = pd.DataFrame(
    {
        "who": ["a", "a", "b"],
        "when": ["2026-03-02 21:30:00", "2026-03-09 21:45:00", "2026-03-03 10:00:00"],
        "what": ["order", "order", "send"],
        "amount": [20.0, 25.0, None],
        "how": [None, None, "email"],
    }
)


def mapping(**kw: object) -> EventMapping:
    base: dict[str, object] = {
        "customer_id": "who",
        "timestamp": "when",
        "event_type_column": "what",
        "event_type_values": {"order": EventType.PURCHASE, "send": EventType.SEND},
        "value_column": "amount",
        "channel_column": "how",
    }
    base.update(kw)
    return EventMapping.model_validate(base)


def test_the_mappings_zone_is_put_on_what_the_customer_did_not_on_our_sends() -> None:
    events, problems = map_frame(FRAME, mapping(timezone="Asia/Kolkata"), "csv")
    assert not problems
    kinds = {e.type: e.attrs.get("timezone") for e in events}
    assert kinds[EventType.PURCHASE] == "Asia/Kolkata"
    assert kinds[EventType.SEND] is None


def test_the_state_folds_it_and_sends_are_timed_in_the_customers_zone() -> None:
    events, _ = map_frame(FRAME, mapping(timezone="Asia/Kolkata"), "csv")
    folder = StateFolder()
    state: CustomerState | None = None
    for e in (e for e in events if e.customer_id == "a"):
        state = folder.fold(state, e)
    assert state is not None
    assert state.timezone == "Asia/Kolkata"
    assert str(customer_zone(state)) == "Asia/Kolkata"
    # With no known reading hours the send goes out at 10:00 *local* (the default), not at 10:00 UTC.
    now = datetime(2026, 3, 10, 3, 0, tzinfo=UTC)
    envelope = sample_brief(now).envelope
    sent = choose_send_time(state, envelope, now)
    assert sent.astimezone(customer_zone(state)).hour == 10
    assert sent.astimezone(UTC).hour != 10


def test_without_a_zone_nothing_changes() -> None:
    events, _ = map_frame(FRAME, mapping(), "csv")
    assert all("timezone" not in e.attrs for e in events)


def test_a_zone_the_file_supplies_per_row_wins() -> None:
    frame = FRAME.assign(zone=["America/New_York", "America/New_York", "UTC"])
    events, _ = map_frame(frame, mapping(timezone="Asia/Kolkata", attrs={"timezone": "zone"}), "csv")
    assert {e.attrs["timezone"] for e in events if e.type is EventType.PURCHASE} == {"America/New_York"}


def test_the_unknown_zone_is_still_utc() -> None:
    state = CustomerState(
        customer_id="x",
        as_of=datetime(2026, 3, 2, tzinfo=UTC),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    assert state.timezone == "UTC"


@pytest.mark.parametrize("zone", ["America/New_York", "America/Los_Angeles"])
def test_the_cdnow_and_telco_cases(zone: str) -> None:
    """06:00 in New York and 02:00 in Los Angeles were what UTC-reading sends came to."""
    events, _ = map_frame(FRAME, mapping(timezone=zone), "csv")
    folder = StateFolder()
    state: CustomerState | None = None
    for e in (e for e in events if e.customer_id == "a"):
        state = folder.fold(state, e)
    assert state is not None
    assert state.timezone == zone
    now = datetime(2026, 3, 10, 3, 0, tzinfo=UTC)
    sent = choose_send_time(state, sample_brief(now).envelope, now)
    assert sent.astimezone(customer_zone(state)).hour == 10
