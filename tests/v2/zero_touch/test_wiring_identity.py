"""Klaviyo profiles are linked to the Shopify customers they belong to, by email or phone, never by
guess: the Shopify id stays the key, two people are never merged, and Klaviyo's consent, engagement
and deliveries meet the Shopify-keyed decisions."""

from __future__ import annotations

from datetime import timedelta

from autonomy.channels.klaviyo import PROVIDER
from autonomy.connectors import Lane, Stream
from autonomy.connectors.identity import (
    KLAVIYO,
    SHOPIFY,
    AliasedEvents,
    IdentityFact,
    IdentityLinker,
    IdentityMap,
    IdentityStore,
    address_book,
    contact_hash,
    create_identity_tables,
    normalize_email,
    normalize_phone,
)
from autonomy.connectors.klaviyo.setup import EMAIL_FLOW
from autonomy.contracts import Channel, Event, EventType
from autonomy.store import SqlEventStore, create_all, memory_engine
from tests.v2.connectors.shopify_support import gid
from tests.v2.zero_touch.wiring_support import BUSINESS, SWEEP, T0, make_shop


def email(value: str) -> str:
    normal = normalize_email(value)
    assert normal is not None
    return contact_hash("email", normal)


def phone(value: str) -> str:
    normal = normalize_phone(value)
    assert normal is not None
    return contact_hash("phone", normal)


def shopify(key: str, *, mail: str | None = None, tel: str | None = None) -> IdentityFact:
    return IdentityFact(
        BUSINESS,
        SHOPIFY,
        key,
        email_hash=email(mail) if mail else None,
        phone_hash=phone(tel) if tel else None,
    )


def klaviyo(
    key: str, *, mail: str | None = None, tel: str | None = None, external: str | None = None
) -> IdentityFact:
    return IdentityFact(
        BUSINESS,
        KLAVIYO,
        key,
        email_hash=email(mail) if mail else None,
        phone_hash=phone(tel) if tel else None,
        external_id=external,
    )


def linker() -> tuple[IdentityStore, IdentityLinker, list[str]]:
    db = memory_engine()
    create_identity_tables(db)
    store = IdentityStore(db)
    changed: list[str] = []
    return store, IdentityLinker(store, on_change=lambda business, now: changed.append(business)), changed


def test_addresses_are_normalised_and_never_guessed() -> None:
    assert normalize_email("  Ann@Example.COM ") == "ann@example.com"
    assert normalize_email("not an address") is None
    assert normalize_phone("+44 20 7946-0958") == "+442079460958"
    assert (
        normalize_phone("020 7946 0958") is None
    )  # no country code: the same digits are other people elsewhere


def test_profiles_link_by_email_or_phone_to_exactly_one_shopify_customer() -> None:
    store, link, changed = linker()
    link.record([shopify("7001", mail="ann@example.com"), shopify("7002", tel="+447700900123")], T0)
    changes = link.record(
        [
            klaviyo("ann@example.com", mail="Ann@Example.com"),  # Klaviyo keys its events by the email
            klaviyo("+447700900123", tel="+44 7700 900123"),  # an SMS-only profile
            klaviyo("zed@example.com", mail="zed@example.com"),  # no Shopify customer: stays its own
        ],
        T0,
    )
    assert changes.linked == 2 and changed == [BUSINESS]
    assert store.links(BUSINESS) == {"ann@example.com": "7001", "+447700900123": "7002"}
    summary = store.summary(BUSINESS)
    assert (summary.shopify_customers, summary.klaviyo_profiles, summary.linked, summary.unlinked) == (
        2,
        3,
        2,
        1,
    )


def test_two_different_people_are_never_merged_and_conflicts_are_counted() -> None:
    store, link, _ = linker()
    link.record(
        [
            shopify("7001", mail="ann@example.com"),
            shopify("7002", mail="ann@example.com"),  # a couple sharing one address
            shopify("7003", mail="bo@example.com"),
            shopify("7004", tel="+447700900999"),
        ],
        T0,
    )
    changes = link.record(
        [
            klaviyo("ann@example.com", mail="ann@example.com"),
            klaviyo(
                "bo@example.com", mail="bo@example.com", tel="+447700900999"
            ),  # email -> 7003, phone -> 7004
            klaviyo("7003", mail="bo@example.com"),  # keyed by a Shopify id itself: never an alias of another
        ],
        T0,
    )
    assert changes.conflicts == 2 and store.links(BUSINESS) == {}
    assert store.summary(BUSINESS).conflicts == 2
    assert store.summary(BUSINESS).same_id == 1
    identity = IdentityMap(store, BUSINESS)
    assert identity.canonical("7003") == "7003" and identity.canonical("ann@example.com") == "ann@example.com"


def test_a_changed_address_moves_the_link_instead_of_keeping_a_stale_one() -> None:
    store, link, changed = linker()
    link.record(
        [shopify("7001", mail="ann@example.com"), klaviyo("ann@example.com", mail="ann@example.com")], T0
    )
    assert store.links(BUSINESS) == {"ann@example.com": "7001"}
    link.record([shopify("7001", mail="ann.new@example.com")], T0 + timedelta(days=1))
    assert store.links(BUSINESS) == {}
    link.record([shopify("7009", mail="ann@example.com")], T0 + timedelta(days=2))
    assert store.links(BUSINESS) == {"ann@example.com": "7009"}
    assert changed == [BUSINESS] * 3


def test_reads_go_through_the_link_and_the_hand_off_gets_the_linked_email() -> None:
    db = memory_engine()
    create_all(db)
    create_identity_tables(db)
    store = IdentityStore(db)
    identity = IdentityMap(store, BUSINESS)
    IdentityLinker(store, maps=[identity]).record(
        [shopify("7001", mail="ann@example.com"), klaviyo("ann@example.com", mail="ann@example.com")], T0
    )
    raw = SqlEventStore(db)
    view = AliasedEvents(raw, identity)
    raw.append(
        [
            Event(
                event_id="buy",
                customer_id="7001",
                type=EventType.PURCHASE,
                ts=T0,
                source="shopify",
                value=9.0,
            ),
            Event(
                event_id="ok",
                customer_id="ann@example.com",
                type=EventType.CONSENT_GRANTED,
                ts=T0 + timedelta(minutes=1),
                source="klaviyo",
                channel=Channel.EMAIL,
            ),
        ]
    )
    assert [(e.event_id, e.customer_id) for e in view.for_customer("7001")] == [
        ("buy", "7001"),
        ("ok", "7001"),
    ]
    assert {e.customer_id for e in view.scan()} == {"7001"}
    assert [e.event_id for e in view.for_customer("ann@example.com")] == ["buy", "ok"]
    book = address_book(identity)
    assert book("7001", Channel.EMAIL) == "ann@example.com"
    assert book("7002", Channel.EMAIL) is None  # nobody Klaviyo knows: skipped, not a new empty profile


def test_connected_shopify_and_klaviyo_meet_on_the_shopify_id_end_to_end() -> None:
    shop = make_shop(linking=True, hold=False)
    # Shopify (with its email read: protected customer data level 2) and Klaviyo know the same person.
    node = shop.shopify.add_customer(
        7001, created=T0 - timedelta(days=400), updated=T0 - timedelta(minutes=30)
    )
    node["defaultEmailAddress"]["emailAddress"] = "Ann@Example.com"
    shopify_conn = shop.connect_shopify_oauth()
    shop.engine.store.put_connection(
        shopify_conn.model_copy(update={"config": {**shopify_conn.config, "contact": "read"}})
    )
    shop.klaviyo.add_profile("P1", email="ann@example.com", updated=T0 - timedelta(minutes=20))
    klaviyo_conn = shop.connect_klaviyo()
    for connection, stream in ((shopify_conn, Stream.CUSTOMERS), (klaviyo_conn, Stream.CONSENT)):
        report = shop.engine.sync(connection.connection_id, stream, Lane.LIVE, shop.clock.now())
        assert report.status == "ok", report
    assert shop.wiring.identity_store is not None
    assert shop.wiring.identity_store.links(BUSINESS) == {"ann@example.com": "7001"}
    shop.runtime.events.append(
        [
            Event(
                event_id="buy",
                customer_id="7001",
                type=EventType.PURCHASE,
                ts=T0 - timedelta(days=90),
                source="shopify",
                value=40.0,
            )
        ]
    )
    # One customer, keyed by the Shopify id, with Klaviyo's consent folded in; the email key is hidden.
    report = shop.runtime.tick(shop.at(SWEEP))
    states = {s.customer_id for s in shop.runtime.states.scan()}
    assert "7001" in states and "ann@example.com" not in states
    assert [d.customer_id for d in report.decisions] == ["7001"]
    # The hand-off names the linked Klaviyo profile by its email, so it reaches the consented profile.
    [handed] = [e for e in shop.klaviyo.created_events if e["properties"].get("decision_id")]
    assert handed["profile"]["data"]["attributes"]["email"] == "ann@example.com"
    decision_id = report.decisions[0].decision_id
    # Klaviyo's delivery comes back under the email key and links to the Shopify-keyed decision.
    flow = next(f["id"] for f in shop.klaviyo.flows.values() if f["attributes"]["name"] == EMAIL_FLOW)
    shop.klaviyo.add_event("Received Email", pid="P1", at=SWEEP + timedelta(minutes=2), props={"$flow": flow})
    shop.at(SWEEP + timedelta(hours=1))
    report = shop.engine.sync(klaviyo_conn.connection_id, Stream.ENGAGEMENT, Lane.LIVE, shop.clock.now())
    assert report.status == "ok", report
    delivered = [e for e in shop.runtime.events.for_decision(decision_id) if e.type is EventType.DELIVER]
    assert delivered and delivered[0].customer_id == "7001" and delivered[0].source == PROVIDER
    assert any(e.type is EventType.DELIVER for e in shop.runtime.events.for_customer("7001"))


def test_a_shopify_redact_also_erases_the_linked_klaviyo_profile() -> None:
    shop = make_shop(linking=True, hold=False)
    assert shop.wiring.identity_store is not None and shop.wiring.privacy_desk is not None
    IdentityLinker(shop.wiring.identity_store).record(
        [shopify("7001", mail="ann@example.com"), klaviyo("ann@example.com", mail="ann@example.com")], T0
    )
    shop.runtime.events.append(
        [
            Event(
                event_id="k1",
                customer_id="ann@example.com",
                type=EventType.OPEN,
                ts=T0,
                source="klaviyo",
                channel=Channel.EMAIL,
            )
        ]
    )
    connection = shop.connect_shopify_oauth()
    removed = shop.wiring.privacy_desk.erase_customers(connection, [gid("Customer", 7001).rsplit("/", 1)[1]])
    assert removed["other"] >= 1
    assert shop.runtime.events.for_customer("ann@example.com") == []
    assert shop.wiring.identity_store.links(BUSINESS) == {}


def test_a_record_without_the_contact_fields_keeps_the_link_it_has() -> None:
    # A REST webhook carried the email; the next poll (no contact=read) and an event profile carry none.
    store, link, changed = linker()
    link.record(
        [shopify("7001", mail="ann@example.com"), klaviyo("ann@example.com", mail="ann@example.com")], T0
    )
    assert store.links(BUSINESS) == {"ann@example.com": "7001"} and changed == [BUSINESS]
    link.record([shopify("7001"), klaviyo("ann@example.com")], T0 + timedelta(hours=1))
    assert store.links(BUSINESS) == {"ann@example.com": "7001"}
    assert changed == [BUSINESS]  # nothing flapped, so no state rebuild was asked for


def test_a_shared_phone_does_not_link_people_whose_emails_differ() -> None:
    store, link, _ = linker()
    link.record([shopify("7001", mail="ann@example.com", tel="+447700900123")], T0)
    changes = link.record(
        [
            klaviyo("bob@example.com", mail="bob@example.com", tel="+447700900123"),  # same home phone
            klaviyo("+447700900123", tel="+447700900123"),  # a phone-only profile: may link
        ],
        T0,
    )
    assert store.links(BUSINESS) == {"+447700900123": "7001"}
    assert changes.conflicts == 1 and store.summary(BUSINESS).conflicts == 1


def test_a_state_hidden_under_a_linked_key_is_not_counted_as_a_second_customer() -> None:
    from autonomy.connectors.identity import AliasedStates
    from autonomy.contracts import CustomerState
    from autonomy.store import SqlStateStore

    db = memory_engine()
    create_all(db)
    create_identity_tables(db)
    store = IdentityStore(db)
    inner = SqlStateStore(db)
    # Klaviyo's consent landed first: a state under the profile's email key, then Shopify's customer.
    inner.put([CustomerState(customer_id=k, as_of=T0) for k in ("ann@example.com", "7001", "7002")])
    identity = IdentityMap(store, BUSINESS)
    IdentityLinker(store, maps=[identity]).record(
        [shopify("7001", mail="ann@example.com"), klaviyo("ann@example.com", mail="ann@example.com")], T0
    )
    states = AliasedStates(inner, identity)
    assert inner.count() == 3
    assert states.count() == 2 == len(list(states.scan()))
