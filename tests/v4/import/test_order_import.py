"""The order-history import: both file shapes, idempotency, refunds, time zones, skipped rows, consent.

Fixtures are small hand-written files in `fixtures/`; every expected number below is worked out from them
by hand (the comment next to it says how).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from autonomy.contracts import Channel, CustomerState, Event, EventType
from autonomy.data import StateFolder, apply_events
from autonomy.data.order_import import (
    OrderImportError,
    format_report,
    import_orders,
    main,
    parse_orders,
)
from autonomy.fakes.stores import InMemoryEventStore, InMemoryStateStore

FIXTURES = Path(__file__).parent / "fixtures"
UCI = (FIXTURES / "uci_orders.csv").read_bytes()
SHOPIFY = (FIXTURES / "shopify_orders.csv").read_bytes()

SHOPIFY_HEADER = (
    "Name,Email,Financial Status,Created at,Currency,Total,Discount Code,Refunded Amount,Cancelled at,"
    "Accepts Marketing,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n"
)
UCI_HEADER = "InvoiceNo,StockCode,Description,Quantity,InvoiceDate,UnitPrice,CustomerID,Country\n"
EXPORTED = datetime(2024, 4, 1, tzinfo=UTC)
"""When the Shopify fixture was exported: after its last order (5 March 2024)."""


def of_type(events: list[Event], kind: EventType) -> list[Event]:
    return [e for e in events if e.type is kind]


def got(states: InMemoryStateStore, customer: str) -> CustomerState:
    state = states.get(customer)
    assert state is not None
    return state


def fold(events: list[Event]) -> InMemoryStateStore:
    states = InMemoryStateStore()
    apply_events(sorted(events, key=lambda e: e.ts), states, StateFolder())
    return states


# --- the UCI shape ----------------------------------------------------------------------------------


def test_uci_report_counts() -> None:
    report = parse_orders(UCI, shop="uk").report
    assert report.shape == "uci_online_retail"
    assert report.rows_read == 9
    # skipped: the blank CustomerID, the "unknown" placeholder, the zero-price POSTAGE line and the
    # C536379 "Discount" line (StockCode D, not goods)
    assert report.skipped == {
        "missing_customer": 1,
        "placeholder_customer": 1,
        "non_positive_price": 1,
        "non_product_line": 1,
    }
    assert report.rows_used == 5
    assert report.orders == 3  # 536365, 536366, 536367
    assert report.refunds == 1  # C536380
    assert report.customers == 3  # 17850, 0012345, 13047 (14527 had only the Discount line)
    assert report.revenue == pytest.approx(6 * 2.55 + 6 * 3.39 + 6 * 1.85 + 32 * 1.69)
    assert report.refunded_amount == pytest.approx(2 * 4.25)
    assert report.contactable == 0
    assert report.consent_events == 0


def test_uci_ids_stay_text_and_orders_are_not_lines() -> None:
    events = parse_orders(UCI, shop="uk").events
    purchases = of_type(events, EventType.PURCHASE)
    assert {e.customer_id for e in events} == {"17850", "0012345", "13047"}
    assert {e.attrs["order_id"] for e in purchases} == {"536365", "536366", "536367"}
    two_lines = next(e for e in purchases if e.attrs["order_id"] == "536365")
    assert two_lines.attrs["lines"] == 2
    assert two_lines.value == pytest.approx(35.64)
    assert two_lines.item_id == "71053"  # the biggest line


def test_uci_returns_name_no_order_and_have_positive_value() -> None:
    refunds = of_type(parse_orders(UCI, shop="uk").events, EventType.REFUND)
    assert all((e.value or 0) > 0 for e in refunds)
    assert all("order_id" not in e.attrs for e in refunds)
    assert {e.attrs["cancellation_id"] for e in refunds} == {"C536380"}


def test_uci_dates_that_could_be_either_order_are_refused() -> None:
    data = UCI.replace(b"12/13/2010", b"12/10/2010").replace(b"12/14/2010", b"12/11/2010")
    with pytest.raises(OrderImportError, match="--date-order"):
        parse_orders(data, shop="uk")
    day_first = parse_orders(data, shop="uk", date_order="day_first").events
    month_first = parse_orders(data, shop="uk", date_order="month_first").events
    assert {e.ts for e in day_first} != {e.ts for e in month_first}


def test_uci_read_as_windows_1252() -> None:
    data = UCI.replace(b"WHITE HANGING HEART", "CAFÉ SET".encode("cp1252"))
    report = parse_orders(data, shop="uk").report
    assert report.orders == 3
    assert any("Windows-1252" in n for n in report.notes)


# --- the Shopify shape ------------------------------------------------------------------------------


def test_shopify_report_counts() -> None:
    report = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED).report
    assert report.shape == "shopify_orders_export"
    assert report.rows_read == 6
    assert report.skipped == {"missing_customer": 1}  # #1004 has no email
    assert report.orders == 4  # #1001, #1002, #1003, #1005
    assert report.customers == 3  # asha, ravi, sam
    assert report.revenue == 3200.0  # 1500 + 800 + 500 + 400
    assert report.refunds == 2  # #1002 partial refund, #1003 void
    assert report.refunded_amount == 800.0  # 300 + 500
    assert report.currency == "INR"


def test_shopify_purchase_carries_order_id_and_discount_code() -> None:
    events = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED).events
    first = next(e for e in of_type(events, EventType.PURCHASE) if e.attrs["order_id"] == "#1001")
    assert first.customer_id == "asha@example.com"  # lower-cased
    assert first.value == 1500.0
    assert first.attrs["discount_codes"] == "DIWALI10"
    assert first.attrs["offer_id"] == "DIWALI10"
    assert first.attrs["lines"] == 2
    assert first.attrs["quantity"] == 3
    assert first.attrs["currency"] == "INR"


def test_refunds_name_the_order_and_net_out_of_revenue() -> None:
    events = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED).events
    refunds = {e.attrs["order_id"]: e for e in of_type(events, EventType.REFUND)}
    assert refunds["#1002"].value == 300.0
    assert refunds["#1003"].value == 500.0
    assert refunds["#1003"].attrs["refund_id"] == "void"
    states = fold(events)
    assert got(states, "ravi@example.com").revenue == 500.0  # 800 - 300
    assert got(states, "asha@example.com").revenue == 1500.0  # 1500 + 500 - 500 (void)
    assert got(states, "sam@example.com").revenue == 400.0
    assert got(states, "asha@example.com").purchases == 2  # orders, not lines


def test_refund_never_exceeds_the_order() -> None:
    data = (
        SHOPIFY_HEADER + "#1,a@b.co,refunded,2024-01-01 10:00:00 +0000,INR,100.00,,250.00,,,1,X,100.00,X1\n"
    ).encode()
    refunds = of_type(parse_orders(data, shop="s", exported_at=EXPORTED).events, EventType.REFUND)
    assert [e.value for e in refunds] == [100.0]


def test_a_refund_that_grew_adds_only_the_increase() -> None:
    store = InMemoryEventStore()
    import_orders(SHOPIFY, shop="in", exported_at=EXPORTED, store=store)
    grown = SHOPIFY.replace(
        b"refunded,2024-03-02 09:15:00 +0530,INR,800.00,,300.00",
        b"refunded,2024-03-02 09:15:00 +0530,INR,800.00,,500.00",
    )
    report, new = import_orders(grown, shop="in", exported_at=EXPORTED, store=store)
    assert report.events_new == 1
    assert [e.value for e in new] == [200.0]
    held = [e for e in store.for_customer("ravi@example.com") if e.type is EventType.REFUND]
    assert sum(e.value or 0 for e in held) == 500.0
    again, _ = import_orders(grown, shop="in", exported_at=EXPORTED, store=store)
    assert again.events_new == 0


def test_a_different_currency_row_is_left_out_not_added() -> None:
    data = (
        SHOPIFY + b"#1006,lee@example.com,paid,2024-03-06 11:00:00 +0530,USD,50.00,,0.00,,,1,Hat,50.00,H1\n"
    )
    report = parse_orders(data, shop="in", exported_at=EXPORTED).report
    assert report.skipped["other_currency"] == 1
    assert report.currency == "INR"
    assert report.revenue == 3200.0


# --- idempotency --------------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [UCI, SHOPIFY], ids=["uci", "shopify"])
def test_importing_twice_adds_nothing(data: bytes) -> None:
    store = InMemoryEventStore()
    first, _ = import_orders(data, shop="s", store=store, exported_at=EXPORTED)
    second, new = import_orders(data, shop="s", store=store, exported_at=EXPORTED)
    assert first.events_new == first.events_built > 0
    assert second.events_new == 0
    assert second.events_already_present == second.events_built
    assert new == []
    assert len(list(store.scan())) == first.events_built


def test_an_overlapping_reordered_export_adds_only_new_orders() -> None:
    store = InMemoryEventStore()
    import_orders(UCI, shop="uk", store=store)
    lines = UCI.decode().splitlines()
    header, rows = lines[0], lines[1:]
    extra = "536390,22633,HAND WARMER,3,12/15/2010 9:00,1.85,13047,United Kingdom"
    overlapping = "\n".join([header, extra, *reversed(rows)]) + "\n"
    report, new = import_orders(overlapping.encode(), shop="uk", store=store)
    assert report.events_new == 1
    assert [e.attrs["order_id"] for e in new] == ["536390"]


def test_the_same_file_for_another_shop_is_not_a_repeat() -> None:
    store = InMemoryEventStore()
    import_orders(UCI, shop="uk", store=store)
    report, _ = import_orders(UCI, shop="other", store=store)
    assert report.events_new == report.events_built


def test_identical_lines_in_one_return_stay_two_events() -> None:
    bought = "9,22960,JAM,4,12/12/2010 9:45,4.25,17850,UK\n"
    data = (UCI_HEADER + bought + "C9,22960,JAM,-1,12/13/2010 9:45,4.25,17850,UK\n" * 2).encode()
    refunds = of_type(parse_orders(data, shop="uk").events, EventType.REFUND)
    assert len(refunds) == 2
    assert len({e.event_id for e in refunds}) == 2


def test_dry_run_with_a_store_writes_nothing_and_counts_new_events() -> None:
    store = InMemoryEventStore()
    import_orders(SHOPIFY, shop="in", exported_at=EXPORTED, store=store)
    extra = (
        SHOPIFY + b"#1006,lee@example.com,paid,2024-03-06 11:00:00 +0530,INR,50.00,,0.00,,,1,Hat,50.00,H1\n"
    )
    before = len(list(store.scan()))
    report, written = import_orders(extra, shop="in", exported_at=EXPORTED, store=store, dry_run=True)
    assert report.dry_run is True
    assert report.events_new == 1
    assert written == []
    assert len(list(store.scan())) == before


# --- time zones ---------------------------------------------------------------------------------------


def test_offsets_in_the_file_are_kept_as_instants() -> None:
    events = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED, timezone="Asia/Kolkata").events
    first = next(e for e in of_type(events, EventType.PURCHASE) if e.attrs["order_id"] == "#1001")
    assert first.ts == datetime(2024, 3, 1, 4, 30, tzinfo=UTC)  # 10:00 +0530
    assert first.attrs["timezone"] == "Asia/Kolkata"


def test_times_with_no_offset_are_read_in_the_given_zone() -> None:
    in_utc = parse_orders(UCI, shop="uk")
    in_kolkata = parse_orders(UCI, shop="uk", timezone="Asia/Kolkata")

    def at(parsed_events: list[Event]) -> Event:
        return next(
            e for e in parsed_events if e.type is EventType.PURCHASE and e.attrs["order_id"] == "536366"
        )

    assert at(in_utc.events).ts == datetime(2010, 12, 1, 8, 28, tzinfo=UTC)
    assert at(in_kolkata.events).ts == datetime(2010, 12, 1, 2, 58, tzinfo=UTC)  # 08:28 IST
    assert "timezone" not in at(in_utc.events).attrs
    assert in_utc.report.naive_times == 5
    assert any("pass --timezone" in n for n in in_utc.report.notes)


def test_summer_time_is_applied_to_naive_times() -> None:
    data = UCI.replace(b"12/1/2010 8:28", b"7/1/2010 8:28")
    event = next(
        e
        for e in parse_orders(data, shop="uk", timezone="Europe/London").events
        if e.type is EventType.PURCHASE and e.attrs["order_id"] == "536366"
    )
    assert event.ts == datetime(2010, 7, 1, 7, 28, tzinfo=UTC)  # BST is UTC+1


def test_a_time_nobody_can_read_is_skipped_with_its_reason() -> None:
    data = UCI.replace(b"12/13/2010 9:41", b"someday")
    assert parse_orders(data, shop="uk").report.skipped["bad_timestamp"] == 1


# --- rows with no customer ----------------------------------------------------------------------------


def test_blank_customer_rows_are_skipped_with_a_reason_and_never_merged() -> None:
    cells = ["", " ", "0", "guest", "N/A"]
    rows = "".join(f"1{i},A,X,1,12/13/2010 9:00,5.00,{cell},UK\n" for i, cell in enumerate(cells))
    parsed = parse_orders((UCI_HEADER + rows).encode(), shop="s")
    assert parsed.events == []
    assert parsed.report.skipped == {"missing_customer": 2, "placeholder_customer": 3}


def test_a_scientific_notation_id_is_not_trusted() -> None:
    data = (UCI_HEADER + "10,A,X,1,12/13/2010 9:00,5.00,1.23457E+11,UK\n").encode()
    assert parse_orders(data, shop="s").report.skipped == {"bad_customer_id": 1}


def test_a_shopify_order_with_no_email_skips_all_its_lines() -> None:
    data = (
        SHOPIFY_HEADER
        + "#7,,paid,2024-01-01 10:00:00 +0000,INR,30.00,,0.00,,,1,A,10.00,A1\n"
        + "#7,,,,,,,,,,2,B,10.00,B1\n"
    ).encode()
    parsed = parse_orders(data, shop="s")
    assert parsed.report.skipped == {"missing_customer": 2}
    assert parsed.events == []


def test_an_unknown_file_is_refused_and_names_its_columns() -> None:
    with pytest.raises(OrderImportError, match="not an order list"):
        parse_orders(b"a,b,c\n1,2,3\n", shop="s")
    with pytest.raises(OrderImportError, match="empty"):
        parse_orders(b"", shop="s")


# --- consent ------------------------------------------------------------------------------------------


def test_uci_invents_no_consent() -> None:
    events = parse_orders(UCI, shop="uk").events
    assert not [e for e in events if e.type in (EventType.CONSENT_GRANTED, EventType.CONSENT_REVOKED)]
    assert all(not s.contactable_channels() for s in fold(events).scan())


def test_only_an_explicit_yes_grants_email_consent() -> None:
    parsed = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED)
    grants = of_type(parsed.events, EventType.CONSENT_GRANTED)
    assert [(e.customer_id, e.channel) for e in grants] == [("asha@example.com", Channel.EMAIL)]
    assert parsed.report.contactable == 1
    # ravi said "no", sam left it blank: neither has any consent event
    states = fold(parsed.events)
    assert got(states, "asha@example.com").contactable_channels() == (Channel.EMAIL,)
    assert got(states, "ravi@example.com").contactable_channels() == ()
    assert got(states, "sam@example.com").contactable_channels() == ()


def test_consent_and_refunds_are_stamped_at_the_export_not_at_the_order() -> None:
    given = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED).events
    assert of_type(given, EventType.CONSENT_GRANTED)[0].ts == EXPORTED
    refunded = next(e for e in of_type(given, EventType.REFUND) if e.attrs["refund_id"] == "refunded")
    assert refunded.ts == EXPORTED


def test_without_an_export_time_undated_refunds_and_consent_are_refused() -> None:
    # the export holds no refund or consent time: dating them at the latest order would put a refund
    # or a consent before it happened
    with pytest.raises(OrderImportError, match="exported_at"):
        parse_orders(SHOPIFY, shop="in")
    plain = SHOPIFY_HEADER + "#1,a@b.co,paid,2024-01-01 10:00:00 +0000,INR,100.00,,0.00,,,1,X,100.00,X1\n"
    assert parse_orders(plain.encode(), shop="s").report.orders == 1  # nothing undated: no time needed


def test_an_export_time_before_the_latest_order_is_refused() -> None:
    with pytest.raises(OrderImportError, match="earlier than the latest order"):
        parse_orders(SHOPIFY, shop="in", exported_at=datetime(2024, 3, 2, tzinfo=UTC))


def test_cli_default_export_time_puts_no_refund_or_consent_at_or_before_its_order(tmp_path: Path) -> None:
    from autonomy.runtime.settings import engine_for
    from autonomy.store import SqlEventStore

    url = f"sqlite:///{tmp_path / 'shop.db'}"
    assert main([str(FIXTURES / "shopify_orders.csv"), "--shop", "in", "--database-url", url]) == 0
    events = list(SqlEventStore(engine_for(url)).scan())
    bought = {e.attrs["order_id"]: e.ts for e in of_type(events, EventType.PURCHASE)}
    refunds = [e for e in of_type(events, EventType.REFUND) if e.attrs.get("time_known") is False]
    assert refunds
    assert all(e.ts > bought[str(e.attrs["order_id"])] for e in refunds)
    assert all(e.ts > max(bought.values()) for e in of_type(events, EventType.CONSENT_GRANTED))


def test_a_file_without_the_column_has_nobody_contactable() -> None:
    data = SHOPIFY.decode().replace("Accepts Marketing", "Notes").encode()
    parsed = parse_orders(data, shop="in", exported_at=EXPORTED)
    assert parsed.report.contactable == 0
    assert not of_type(parsed.events, EventType.CONSENT_GRANTED)


def test_a_later_no_withdraws_a_stored_consent() -> None:
    store = InMemoryEventStore()
    import_orders(SHOPIFY, shop="in", exported_at=EXPORTED, store=store)
    later = SHOPIFY.replace(b"0.00,2024-03-04 08:00:00 +0530,yes", b"0.00,2024-03-04 08:00:00 +0530,no")
    later = later.replace(b"DIWALI10,0.00,,yes", b"DIWALI10,0.00,,no")
    report, _ = import_orders(later, shop="in", exported_at=EXPORTED, store=store)
    assert report.consent_revoked == 1
    assert got(fold(list(store.scan())), "asha@example.com").contactable_channels() == ()
    # a customer who never consented gets no revoke
    assert not [e for e in store.for_customer("ravi@example.com") if e.type is EventType.CONSENT_REVOKED]


ONE_YES = (
    SHOPIFY_HEADER
    + "#9,asha@example.com,paid,2024-03-01 10:00:00 +0000,INR,100.00,,0.00,,yes,1,X,100.00,X1\n"
).encode()


def held_consent(kind: EventType, at: datetime, source: str = "klaviyo") -> Event:
    return Event(
        event_id=f"held-{kind.value}-{at.isoformat()}",
        customer_id="asha@example.com",
        type=kind,
        ts=at,
        source=source,
        channel=Channel.EMAIL,
    )


def live(store: InMemoryEventStore, states: InMemoryStateStore) -> Callable[[Sequence[Event]], int]:
    """An ingest that folds events in the order they arrive, as `api.v2.deps.make_ingest` does."""
    folder = StateFolder()

    def ingest(batch: Sequence[Event]) -> int:
        new = [e for e in batch if store.append([e])]
        apply_events(sorted(new, key=lambda e: e.ts), states, folder)
        return len(new)

    return ingest


@pytest.mark.parametrize(
    "opt_out",
    [
        (EventType.UNSUBSCRIBE, "klaviyo"),
        (EventType.COMPLAINT, "klaviyo"),
        (EventType.CONSENT_REVOKED, "reply"),
    ],
    ids=["unsubscribe", "complaint", "revoked-elsewhere"],
)
@pytest.mark.parametrize("exported", [EXPORTED, datetime(2025, 7, 1, tzinfo=UTC)], ids=["before", "after"])
def test_an_export_yes_never_overrides_a_held_opt_out(
    opt_out: tuple[EventType, str], exported: datetime
) -> None:
    store, states = InMemoryEventStore(), InMemoryStateStore()
    ingest = live(store, states)
    ingest([held_consent(EventType.CONSENT_GRANTED, datetime(2023, 1, 1, tzinfo=UTC))])
    ingest([held_consent(opt_out[0], datetime(2025, 6, 1, tzinfo=UTC), source=opt_out[1])])
    assert got(states, "asha@example.com").contactable_channels() == ()
    report, _ = import_orders(ONE_YES, shop="s", store=store, ingest=ingest, exported_at=exported)
    assert got(states, "asha@example.com").contactable_channels() == ()
    assert not [e for e in store.for_customer("asha@example.com") if e.source == "order_import" and e.channel]
    assert report.contactable == 0
    assert report.consent_blocked == 1
    assert "Consent not restored: 1" in format_report(report)


def test_a_yes_after_an_imports_own_no_grants_again_once() -> None:
    store, states = InMemoryEventStore(), InMemoryStateStore()
    ingest = live(store, states)
    import_orders(ONE_YES, shop="s", store=store, ingest=ingest, exported_at=EXPORTED)
    no = ONE_YES.replace(b",yes,", b",no,")
    report, _ = import_orders(
        no, shop="s", store=store, ingest=ingest, exported_at=datetime(2024, 5, 1, tzinfo=UTC)
    )
    assert report.consent_revoked == 1
    assert got(states, "asha@example.com").contactable_channels() == ()
    # the same "no" export again, or an older "yes" one, changes nothing
    same_no, _ = import_orders(
        no, shop="s", store=store, ingest=ingest, exported_at=datetime(2024, 5, 1, tzinfo=UTC)
    )
    assert same_no.events_new == 0
    stale, _ = import_orders(ONE_YES, shop="s", store=store, ingest=ingest, exported_at=EXPORTED)
    assert stale.events_new == 0 and stale.consent_blocked == 1
    assert got(states, "asha@example.com").contactable_channels() == ()
    # a later "yes" export grants again, and importing it twice adds nothing
    june = datetime(2024, 6, 1, tzinfo=UTC)
    again, _ = import_orders(ONE_YES, shop="s", store=store, ingest=ingest, exported_at=june)
    assert again.events_new == 1 and again.contactable == 1
    assert got(states, "asha@example.com").contactable_channels() == (Channel.EMAIL,)
    twice, _ = import_orders(ONE_YES, shop="s", store=store, ingest=ingest, exported_at=june)
    assert twice.events_new == 0 and twice.contactable == 1


# --- UCI returns with no purchase ---------------------------------------------------------------------


def test_a_return_with_no_purchase_is_skipped_unless_the_store_holds_one() -> None:
    returned = (UCI_HEADER + "C9,22960,JAM,-1,12/20/2010 9:45,4.25,13047,UK\n").encode()
    parsed = parse_orders(returned, shop="uk")
    assert parsed.events == []
    assert parsed.report.skipped == {"return_without_purchase": 1}
    # 13047 bought nothing in the store yet: the return is left out, so revenue cannot go negative
    empty = InMemoryEventStore()
    report, written = import_orders(returned, shop="uk", store=empty)
    assert written == [] and report.skipped == {"return_without_purchase": 1}
    # once an earlier export with 13047's purchase is held, the same return is written
    store = InMemoryEventStore()
    import_orders(UCI, shop="uk", store=store)
    report, written = import_orders(returned, shop="uk", store=store)
    assert [e.type for e in written] == [EventType.REFUND]
    assert report.skipped == {} and report.refunds == 1 and report.customers == 1


# --- the command line ---------------------------------------------------------------------------------


def test_cli_dry_run_prints_the_report_and_opens_no_database(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([str(FIXTURES / "uci_orders.csv"), "--shop", "uk", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry run, nothing written" in out
    assert "Orders:           3" in out
    assert "Contactable:      0" in out
    assert "missing_customer: 1" in out


def test_cli_writes_once_and_a_second_run_adds_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from autonomy.runtime.settings import engine_for
    from autonomy.store import SqlEventStore, SqlStateStore

    url = f"sqlite:///{tmp_path / 'shop.db'}"
    args = [str(FIXTURES / "shopify_orders.csv"), "--shop", "in", "--database-url", url]
    assert main(args) == 0
    first = capsys.readouterr().out
    built = parse_orders(SHOPIFY, shop="in", exported_at=EXPORTED).report.events_built
    assert re.search(rf"Events new:\s+{built} \(0 already held\)", first)
    assert main(args) == 0
    assert re.search(r"Events new:\s+0 ", capsys.readouterr().out)
    engine = engine_for(url)
    assert len(list(SqlEventStore(engine).scan())) == built
    state = SqlStateStore(engine).get("ravi@example.com")
    assert state is not None
    assert state.revenue == 500.0


def test_cli_reports_a_bad_file_without_a_traceback(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    assert main([str(bad), "--shop", "s", "--dry-run"]) == 2
    assert "not an order list" in capsys.readouterr().err


def test_format_report_has_every_line_the_spec_asks_for() -> None:
    text = format_report(parse_orders(UCI, shop="uk").report)
    for needle in ("Rows read", "Orders", "Customers", "Refunds", "Skipped rows", "Contactable"):
        assert needle in text
