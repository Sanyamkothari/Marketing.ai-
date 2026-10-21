"""Event mapping, the sample-dataset loaders and the Data Doctor bridge.

The mapping is tested on tiny inline frames; the loaders on the committed sample CSVs (the raw
online-retail log is git-ignored, so its loader is tested on an inline frame in the log's own shape).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from autonomy.contracts import Channel, EventType, stable_id
from autonomy.data.loaders import (
    HILLSTROM_CAMPAIGN_AT,
    HILLSTROM_NOTES,
    LoadResult,
    hillstrom_decision_id,
    hillstrom_events,
    load_hillstrom,
    load_online_retail_sample,
    online_retail_log_events,
    read_hillstrom_sample,
    read_online_retail_sample,
)
from autonomy.data.mapping import (
    EventMapping,
    MappingError,
    ProblemCode,
    customer_key,
    map_frame,
    problem_counts,
)
from autonomy.fakes.folder import SimpleStateFolder

LONDON = ZoneInfo("Europe/London")


def _orders(**extra: object) -> EventMapping:
    return EventMapping(
        customer_id="cid",
        timestamp="at",
        event_type=EventType.PURCHASE,
        value_column="amount",
        **extra,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# map_frame
# ---------------------------------------------------------------------------
def test_maps_rows_to_events_with_stable_ids() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "b"],
            "at": ["2024-01-02T10:00:00", "2024-01-03T11:30:00+02:00"],
            "amount": [12.5, "40"],
            "sku": ["X1", None],
            "store": ["north", ""],
        }
    )
    mapping = _orders(item_column="sku", attrs={"store": "store"}, fixed_attrs={"file": "orders.csv"})
    events, problems = map_frame(frame, mapping, "csv")

    assert problems == []
    first, second = events
    # Without row_key the id comes from the row's content, not its position (a re-export that puts
    # new rows first neither drops them nor counts old rows twice).
    assert first.event_id != stable_id("evt", "csv", "0")
    flipped, _ = map_frame(frame.iloc[::-1].reset_index(drop=True), mapping, "csv")
    assert {e.event_id for e in flipped} == {e.event_id for e in (first, second)}
    assert first.ts == datetime(2024, 1, 2, 10, tzinfo=UTC)  # no offset: the mapping's zone (UTC)
    assert second.ts == datetime(2024, 1, 3, 9, 30, tzinfo=UTC)  # its own offset wins
    assert (first.value, second.value) == (12.5, 40.0)
    assert first.item_id == "X1" and second.item_id is None
    assert first.attrs == {"file": "orders.csv", "store": "north"}
    assert second.attrs == {"file": "orders.csv"}  # an empty cell is left out, not stored as ""

    again, _ = map_frame(frame, mapping, "csv")
    assert [e.event_id for e in again] == [e.event_id for e in events]
    other, _ = map_frame(frame, mapping, "shopify")
    assert {e.event_id for e in other}.isdisjoint(e.event_id for e in events)


def test_row_key_columns_make_the_id_independent_of_row_order() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], "at": ["2024-01-01", "2024-01-02"], "amount": [1, 2]})
    mapping = _orders(row_key=("cid", "at"))
    forward, _ = map_frame(frame, mapping, "csv")
    backward, _ = map_frame(frame.iloc[::-1].reset_index(drop=True), mapping, "csv")
    assert {e.event_id for e in forward} == {e.event_id for e in backward}


def test_bad_rows_are_collected_and_the_rest_still_load() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["ok", None, "c", "d", "e", "f", "g"],
            "at": [
                "2024-01-01",
                "2024-01-01",
                "yesterday",
                "2024-01-01",
                "2024-01-01",
                "2024-01-01",
                "2024-01-01",
            ],
            "kind": ["order", "order", "order", "teleport", "order", "order", "send"],
            "amount": ["5", "5", "5", "5", "N/A", None, None],
            "via": [None, None, None, None, None, None, "pigeon"],
        }
    )
    mapping = EventMapping(
        customer_id="cid",
        timestamp="at",
        event_type_column="kind",
        event_type_values={"order": EventType.PURCHASE},
        value_column="amount",
        channel_column="via",
    )
    events, problems = map_frame(frame, mapping, "csv")

    assert [e.customer_id for e in events] == ["ok"]
    by_row = {p.row: p for p in problems}
    assert by_row["1"].code == ProblemCode.MISSING_CUSTOMER
    assert (by_row["2"].code, by_row["2"].column) == (ProblemCode.BAD_TIMESTAMP, "at")
    assert by_row["3"].code == ProblemCode.UNKNOWN_EVENT_TYPE
    assert (by_row["4"].code, by_row["4"].column) == (ProblemCode.BAD_NUMBER, "amount")
    assert by_row["5"].code == ProblemCode.INVALID_EVENT  # a purchase with no value
    assert by_row["6"].code == ProblemCode.BAD_CHANNEL
    assert problem_counts(problems)[ProblemCode.MISSING_CUSTOMER] == 1
    assert all("N/A" not in p.message for p in problems)  # problems never echo cell values


def test_duplicate_row_keys_are_reported_once_each() -> None:
    frame = pd.DataFrame({"cid": ["a", "a"], "at": ["2024-01-01", "2024-01-01"], "amount": [1, 1]})
    events, problems = map_frame(frame, _orders(row_key=("cid", "at")), "csv")
    assert len(events) == 1
    assert [p.code for p in problems] == [ProblemCode.DUPLICATE_ROW]


def test_event_type_column_reads_the_value_map_then_our_own_names() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a", "a"],
            "at": ["03/01/2024 09:15"] * 3,
            "what": ["Opened", "click", "UNSUB"],
            "dec": ["dec_1", "dec_1", "dec_1"],
        }
    )
    mapping = EventMapping(
        customer_id="cid",
        timestamp="at",
        timestamp_format="%d/%m/%Y %H:%M",
        timezone="Europe/London",
        event_type_column="what",
        event_type_values={"OPENED": EventType.OPEN, "unsub": EventType.UNSUBSCRIBE},
        channel=Channel.EMAIL,
        decision_id_column="dec",
    )
    events, problems = map_frame(frame, mapping, "esp")
    assert problems == []
    assert [e.type for e in events] == [EventType.OPEN, EventType.CLICK, EventType.UNSUBSCRIBE]
    assert all(e.decision_id == "dec_1" and e.channel is Channel.EMAIL for e in events)
    assert events[0].ts == datetime(2024, 1, 3, 9, 15, tzinfo=LONDON)


def test_numeric_event_type_codes_survive_a_float_column() -> None:
    # An empty cell turns an integer code column into floats (1.0); the map's "1" must still match.
    frame = pd.DataFrame(
        {"cid": ["a", "a", "a"], "at": ["2024-01-01"] * 3, "kind": [1, 2, None], "amount": [5, None, None]}
    )
    assert frame["kind"].dtype.kind == "f"
    mapping = EventMapping(
        customer_id="cid",
        timestamp="at",
        event_type_column="kind",
        event_type_values={"1": EventType.PURCHASE, "2": EventType.VISIT},
        value_column="amount",
    )
    events, problems = map_frame(frame, mapping, "csv")
    assert [e.type for e in events] == [EventType.PURCHASE, EventType.VISIT]
    assert [(p.row, p.code) for p in problems] == [("2", ProblemCode.UNKNOWN_EVENT_TYPE)]


def test_a_refund_written_as_a_negative_amount_is_stored_positive() -> None:
    frame = pd.DataFrame(
        {
            "cid": ["a", "a"],
            "at": ["2024-01-01", "2024-01-02"],
            "kind": ["purchase", "refund"],
            "amount": [10, -4],
        }
    )
    mapping = EventMapping(customer_id="cid", timestamp="at", event_type_column="kind", value_column="amount")
    events, _ = map_frame(frame, mapping, "csv")
    assert [(e.type, e.value) for e in events] == [(EventType.PURCHASE, 10.0), (EventType.REFUND, 4.0)]
    state = None
    for event in events:
        state = SimpleStateFolder().fold(state, event)
    assert state is not None and state.revenue == pytest.approx(6.0)  # net of the refund, not 14


def test_quantity_times_price_and_negative_values_become_refunds() -> None:
    frame = pd.DataFrame(
        {"cid": ["a", "a"], "at": ["2024-01-01", "2024-01-02"], "qty": [3, -1], "price": ["2.50", "2.50"]}
    )
    mapping = EventMapping(
        customer_id="cid",
        timestamp="at",
        event_type=EventType.PURCHASE,
        quantity_column="qty",
        unit_price_column="price",
        negative_as=EventType.REFUND,
    )
    events, _ = map_frame(frame, mapping, "csv")
    assert [(e.type, e.value) for e in events] == [(EventType.PURCHASE, 7.5), (EventType.REFUND, 2.5)]


def test_numbers_with_thousands_groups_parse_and_a_decimal_comma_is_refused() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], "at": ["2024-01-01"] * 2, "amount": ["1,234.50", "12,5"]})
    events, problems = map_frame(frame, _orders(), "csv")
    assert [e.value for e in events] == [1234.5]
    assert [(p.row, p.code) for p in problems] == [("1", ProblemCode.BAD_NUMBER)]


def test_customer_keys_lose_a_float_tail() -> None:
    assert customer_key(12346.0) == "12346"
    assert customer_key("17850.0") == "17850"
    assert customer_key(np.int64(7)) == "7"
    assert customer_key(" 007 ") == "007"
    assert customer_key(float("nan")) is None
    assert customer_key("  ") is None


def test_a_missing_column_is_one_clear_error() -> None:
    frame = pd.DataFrame({"cid": ["a"], "when": ["2024-01-01"], "amount": [1]})
    with pytest.raises(MappingError, match="'at'"):
        map_frame(frame, _orders(), "csv")


def test_mapping_rejects_contradictions_and_loads_from_plain_data() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        EventMapping(customer_id="c", timestamp="t")
    with pytest.raises(ValidationError, match="exactly one"):
        EventMapping(customer_id="c", timestamp="t", event_type=EventType.VISIT, event_type_column="k")
    with pytest.raises(ValidationError, match="go together"):
        EventMapping(customer_id="c", timestamp="t", event_type=EventType.PURCHASE, quantity_column="q")
    with pytest.raises(ValidationError, match="time zone"):
        EventMapping(customer_id="c", timestamp="t", event_type=EventType.VISIT, timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        EventMapping(customer_id="c", timestamp="t", event_type=EventType.VISIT, colour="red")  # type: ignore[call-arg]

    from_yaml = EventMapping.model_validate(
        {
            "customer_id": "email",
            "timestamp": "created",
            "event_type_column": "status",
            "event_type_values": {"Paid": "purchase"},
            "value_column": "total",
        }
    )
    assert from_yaml.event_type_values == {"paid": EventType.PURCHASE}


# ---------------------------------------------------------------------------
# Online retail: the raw log (inline, in the log's shape)
# ---------------------------------------------------------------------------
def _log() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "InvoiceNo": ["536365", "536365", "C536379", "536380", "536381", "536382", "536383"],
            "StockCode": ["85123A", "71053", "D", "22633", "POST", "22632", "22631"],
            "Description": ["x"] * 7,
            "Quantity": [6, 2, -1, 4, 1, 1, 1],
            "InvoiceDate": [
                "12/1/2010 8:26",
                "12/1/2010 8:26",
                "12/1/2010 9:41",
                "12/1/2010 9:45",
                "12/1/2010 9:50",
                "12/1/2010 9:55",
                "31/31/2010 9:55",
            ],
            "UnitPrice": [2.55, 3.39, 27.5, 1.85, 0.0, 2.0, 2.0],
            "CustomerID": ["17850.0", "17850.0", "14527.0", None, "17850", "12345", "12345"],
            "Country": ["United Kingdom"] * 7,
        }
    )


def test_online_retail_log_gives_one_event_per_invoice_and_refunds_for_cancellations() -> None:
    result = online_retail_log_events(_log())

    by_invoice = {e.attrs["invoice_no"]: e for e in result.events}
    order = by_invoice["536365"]
    assert order.type is EventType.PURCHASE and order.customer_id == "17850"
    assert order.value == pytest.approx(6 * 2.55 + 2 * 3.39)
    assert order.attrs["lines"] == 2 and order.attrs["items"] == 8
    assert order.ts == datetime(2010, 12, 1, 8, 26, tzinfo=LONDON)
    refund = by_invoice["C536379"]
    assert (refund.type, refund.value) == (EventType.REFUND, 27.5)
    assert set(by_invoice) == {"536365", "C536379", "536382"}

    codes = {p.row: p.code for p in result.problems}
    assert codes["3"] == ProblemCode.EXCLUDED  # guest checkout
    assert codes["4"] == ProblemCode.EXCLUDED  # zero unit price
    assert ProblemCode.BAD_TIMESTAMP in codes.values()  # invoice 536383's impossible date
    assert len(result.problems) == 3


def test_online_retail_log_line_grain_keeps_every_line() -> None:
    result = online_retail_log_events(_log(), grain="line")
    assert [e.item_id for e in result.events] == ["85123A", "71053", "D", "22632"]
    assert result.events[2].type is EventType.REFUND


def test_online_retail_log_refuses_a_table_of_another_shape() -> None:
    with pytest.raises(MappingError, match="transaction log"):
        online_retail_log_events(pd.DataFrame({"customer_id": ["1"]}))


# ---------------------------------------------------------------------------
# Online retail: the committed per-customer sample
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def retail() -> LoadResult:
    return load_online_retail_sample()


def test_online_retail_sample_rebuilds_each_customers_orders(retail: LoadResult) -> None:
    sample = read_online_retail_sample()
    assert retail.problems == []
    assert len({e.event_id for e in retail.events}) == len(retail.events)

    by_customer = defaultdict(list)
    for event in retail.events:
        by_customer[event.customer_id].append(event)
    assert len(by_customer) == len(sample)

    snapshot = datetime(2011, 9, 10, 12, tzinfo=LONDON)
    for row in sample.head(200).itertuples():
        events = by_customer[str(row.customer_id)]
        purchases = [e for e in events if e.type is EventType.PURCHASE]
        refunds = [e for e in events if e.type is EventType.REFUND]
        assert len(purchases) == row.orders_total
        assert sum(e.value or 0 for e in purchases) == pytest.approx(row.spend_total)
        assert max(e.value or 0 for e in purchases) == pytest.approx(row.order_value_max)
        assert max(e.ts for e in purchases) == snapshot - timedelta(days=int(row.recency_days))
        assert min(e.ts for e in purchases) == snapshot - timedelta(days=int(row.tenure_days))
        assert len(refunds) == row.returned_orders
        assert all(e.ts < snapshot for e in events)  # nothing from after the snapshot by default
        assert {e.type for e in events} >= {EventType.SIGNUP, EventType.CONSENT_GRANTED}


def test_online_retail_sample_folds_into_state(retail: LoadResult) -> None:
    folder = SimpleStateFolder()
    state = None
    for event in sorted((e for e in retail.events if e.customer_id == "12348"), key=lambda e: e.ts):
        state = folder.fold(state, event)
    assert state is not None
    assert state.purchases == 3
    assert state.revenue == pytest.approx(1487.24)
    assert Channel.EMAIL in state.contactable_channels()


def test_online_retail_sample_refunds_never_exceed_spend(retail: LoadResult) -> None:
    sample = read_online_retail_sample()
    over = sample[sample["returned_items"] > sample["items_total"]]
    assert len(over) > 0  # the sample has customers who returned more than they bought in the window
    net: dict[str, float] = defaultdict(float)
    for event in retail.events:
        if event.type is EventType.PURCHASE:
            net[event.customer_id] += event.value or 0.0
        elif event.type is EventType.REFUND:
            net[event.customer_id] -= event.value or 0.0
    assert min(net.values()) >= -1e-6
    for row in over.itertuples():
        assert net[str(row.customer_id)] == pytest.approx(0.0, abs=1e-6)


def test_online_retail_sample_outcomes_are_opt_in() -> None:
    sample = read_online_retail_sample().head(100)
    from autonomy.data.loaders import online_retail_sample_events

    with_outcomes = online_retail_sample_events(sample, include_outcomes=True)
    outcomes = [e for e in with_outcomes.events if e.attrs.get("outcome") is True]
    assert len(outcomes) == int(sample["reactivated_90d"].sum())
    assert all(e.ts > datetime(2011, 9, 10, tzinfo=UTC) for e in outcomes)


# ---------------------------------------------------------------------------
# Hillstrom
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def hillstrom() -> LoadResult:
    return load_hillstrom()


def test_hillstrom_sends_carry_a_decision_and_the_design_probability(hillstrom: LoadResult) -> None:
    sample = read_hillstrom_sample()
    assert hillstrom.problems == []
    assert hillstrom.notes == HILLSTROM_NOTES
    assert len({e.event_id for e in hillstrom.events}) == len(hillstrom.events)

    sends = [e for e in hillstrom.events if e.type is EventType.SEND]
    treated = sample[sample["segment"] != "No E-Mail"]
    assert len(sends) == len(treated)
    for send in sends[:100]:
        assert send.channel is Channel.EMAIL and send.ts == HILLSTROM_CAMPAIGN_AT
        assert send.decision_id == hillstrom_decision_id(send.customer_id)
        assert send.attrs["probability"] == pytest.approx(1 / 3)
        assert send.attrs["arm"] == hillstrom.arms[send.customer_id]

    assert Counter(hillstrom.arms.values()) == Counter(
        {"no_email": 2131, "mens_email": 2131, "womens_email": 2139}
    )
    consents = [e for e in hillstrom.events if e.type is EventType.CONSENT_GRANTED]
    assert len(consents) == len(sample) and all(e.channel is Channel.EMAIL for e in consents)


def test_hillstrom_outcomes_and_history(hillstrom: LoadResult) -> None:
    sample = read_hillstrom_sample()
    outcomes = [e for e in hillstrom.events if e.attrs.get("outcome") is True]
    conversions = [e for e in outcomes if e.type is EventType.PURCHASE]
    assert len(conversions) == int(sample["conversion"].sum())
    assert sum(e.value or 0 for e in conversions) == pytest.approx(sample["spend"].sum())
    assert sum(e.type is EventType.VISIT for e in outcomes) == int(sample["visit"].sum())
    assert all(e.ts > HILLSTROM_CAMPAIGN_AT for e in outcomes)

    history = [e for e in hillstrom.events if e.type is EventType.PURCHASE and "outcome" not in e.attrs]
    assert sum(e.value or 0 for e in history) == pytest.approx(sample["history"].sum())
    assert all(HILLSTROM_CAMPAIGN_AT - timedelta(days=365) < e.ts < HILLSTROM_CAMPAIGN_AT for e in history)
    both = sample[(sample["mens"] == 1) & (sample["womens"] == 1)]
    assert len(history) == len(sample) + len(both)


def test_hillstrom_is_deterministic_and_can_leave_outcomes_out() -> None:
    rows = read_hillstrom_sample().head(40)
    when = datetime(2024, 5, 6, 9, tzinfo=UTC)
    once = hillstrom_events(rows, campaign_at=when, include_outcomes=False)
    twice = hillstrom_events(rows, campaign_at=when, include_outcomes=False)
    assert [e.event_id for e in once.events] == [e.event_id for e in twice.events]
    assert all("outcome" not in e.attrs for e in once.events)
    assert all(e.ts == when for e in once.events if e.type is EventType.SEND)


def test_hillstrom_reports_bad_rows_and_needs_an_aware_campaign_time() -> None:
    rows = read_hillstrom_sample().head(3).copy()
    rows.loc[rows.index[0], "segment"] = "Carrier Pigeon"
    rows["history"] = rows["history"].astype(object)
    rows.loc[rows.index[1], "history"] = "lots"
    result = hillstrom_events(rows)
    assert [p.code for p in result.problems] == [ProblemCode.UNKNOWN_EVENT_TYPE, ProblemCode.BAD_NUMBER]
    assert {e.customer_id for e in result.events} == {str(rows.iloc[2]["customer_id"])}
    with pytest.raises(ValueError, match="time zone"):
        hillstrom_events(rows, campaign_at=datetime(2008, 3, 3))


# ---------------------------------------------------------------------------
# The Data Doctor bridge (imports v1's intake and ingest)
# ---------------------------------------------------------------------------
@pytest.mark.v1bridge
def test_doctor_bridge_reads_a_messy_export_then_maps_it(tmp_path) -> None:
    from autonomy.bridges.doctor_bridge import map_file, read_with_intake

    path = tmp_path / "orders.csv"
    path.write_text(
        "Orders export from the shop\n\n"
        "cust;when;amount\n"
        "007;2024-01-02T10:00:00;12.5\n"
        "008;2024-01-03T11:00:00;3\n"
        "Total;;15.5\n"
    )
    read = read_with_intake(path, text_columns=("cust",), work_dir=tmp_path / "scratch")
    assert read.rewritten and read.notes
    assert list(read.frame["cust"]) == ["007", "008"]

    mapped = map_file(
        path,
        EventMapping(customer_id="cust", timestamp="when", event_type="purchase", value_column="amount"),
        "csv",
    )
    assert mapped.problems == []
    assert [(e.customer_id, e.value) for e in mapped.events] == [("007", 12.5), ("008", 3.0)]
    assert mapped.notes == read.notes
