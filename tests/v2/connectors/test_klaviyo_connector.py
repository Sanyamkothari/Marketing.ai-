"""The Klaviyo connector against a fake Klaviyo that answers in the documented shapes (no network)."""

from __future__ import annotations

import base64
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from autonomy.connectors import ConnectionStatus, Lane, ManualClock, RateLimited, Stream, sync_job
from autonomy.connectors.klaviyo import (
    API_REVISION,
    KLAVIYO_SCOPES,
    EndpointLimit,
    EndpointPacer,
    KlaviyoConnector,
)
from autonomy.contracts import Channel, EventType
from tests.v2.connectors.klaviyo_support import (
    ACCOUNT_ID,
    API_KEY,
    APP,
    REVISION,
    T0,
    FakeKlaviyo,
    connect_key,
    connect_oauth,
    jsonapi_error,
    make_klaviyo_rig,
    requests_to,
    subscriptions,
)


def test_the_pinned_revision_is_the_one_the_fake_requires() -> None:
    assert API_REVISION.value == REVISION and API_REVISION.header == "revision"
    assert API_REVISION.review_by > API_REVISION.doc_date


def test_oauth_connect_uses_pkce_and_basic_client_auth_and_identifies_the_account() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection, url = connect_oauth(rig)
    query = parse_qs(url.split("?", 1)[1])
    assert url.startswith("https://www.klaviyo.com/oauth/authorize?")
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split(" ")) == set(KLAVIYO_SCOPES)
    exchange = rig.fake.token_calls[0]
    assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"]
    assert "client_secret" not in exchange  # it travels in the Basic header, not the body
    token_call = requests_to(rig.fake, "POST", "/oauth/token")[0]
    assert token_call.headers["authorization"] == "Basic " + base64.b64encode(b"kl_client:kl_secret").decode()
    assert connection.external_id == ACCOUNT_ID and connection.label == "Acme Outdoors"
    api_calls = [r for r in rig.fake.calls if r.url.path.startswith("/api/")]
    assert api_calls and all(r.headers["revision"] == REVISION for r in api_calls)
    assert all(r.headers["authorization"].startswith("Bearer kl_at_") for r in api_calls)
    for stream in (Stream.CONSENT, Stream.ENGAGEMENT, Stream.EVENTS):  # seeded: walk away from here
        for lane in (Lane.LIVE, Lane.HISTORY):
            assert rig.queue.get(sync_job(connection.connection_id, stream, lane, T0).job_id) is not None


def test_a_private_key_is_sent_as_klaviyo_api_key_and_never_refreshed() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    api_calls = [r for r in rig.fake.calls if r.url.path.startswith("/api/")]
    assert api_calls and all(r.headers["authorization"] == f"Klaviyo-API-Key {API_KEY}" for r in api_calls)
    rig.clock.advance(timedelta(days=60))
    rig.engine.daily(connection.connection_id, rig.clock.now())
    assert rig.fake.token_calls == []


def test_tokens_refresh_before_the_hour_and_keep_alive_before_ninety_days() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection, _ = connect_oauth(rig)
    rig.clock.advance(timedelta(minutes=56))  # inside the 5-minute margin of the 1 h token
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    assert report.status == "ok"
    assert [c["grant_type"] for c in rig.fake.token_calls] == ["authorization_code", "refresh_token"]
    # Idle for a month: the daily job refreshes so the refresh token is never 90 days unused.
    before = len(rig.fake.token_calls)
    rig.clock.advance(timedelta(days=31))
    rig.engine.daily(connection.connection_id, rig.clock.now())
    assert len(rig.fake.token_calls) == before + 1
    assert rig.fake.token_calls[-1]["refresh_token"] == "kl_rt_1"  # Klaviyo keeps its refresh token


def test_a_revoked_grant_marks_needs_reconnect_with_exactly_one_alert() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection, _ = connect_oauth(rig)
    rig.fake.revoked = True
    rig.clock.advance(timedelta(minutes=58))
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    assert report.status == "reconnect"
    stored = rig.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.NEEDS_RECONNECT
    for stream in (Stream.ENGAGEMENT, Stream.EVENTS, Stream.CONSENT):
        assert (
            rig.engine.sync(connection.connection_id, stream, Lane.LIVE, rig.clock.now()).status == "skipped"
        )
    alerts = rig.alerts.list_alerts()
    assert [a.code for a in alerts] == ["connector.needs_reconnect"]
    assert "Klaviyo" in alerts[0].message and "kl_rt_1" not in alerts[0].message


def _consent_world(fake: FakeKlaviyo) -> None:
    at = T0 - timedelta(hours=1)
    fake.add_profile(
        "P1",
        email="Ann@Example.com",
        updated=at,
        phone="+447700900001",
        subs=subscriptions("SUBSCRIBED", sms="SUBSCRIBED", can_sms=True, at=at),
    )
    fake.add_profile(
        "P2", email="bob@example.com", updated=at, subs=subscriptions("UNSUBSCRIBED", can_email=False, at=at)
    )
    fake.add_profile("P3", email="cy@example.com", updated=at, subs=subscriptions("NEVER_SUBSCRIBED", at=at))
    fake.add_profile(
        "P4",
        email="di@example.com",
        updated=at,
        subs=subscriptions("SUBSCRIBED", can_email=False, at=at, suppressed="HARD_BOUNCE"),
    )


def test_profiles_become_signups_and_per_channel_consent() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    _consent_world(rig.fake)
    connection = connect_key(rig)
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert report.status == "ok" and report.rows == 4 and report.problems == 0
    request = requests_to(rig.fake, "GET", "/api/profiles")[0]
    assert request.url.params["additional-fields[profile]"] == "subscriptions"
    assert request.url.params["filter"].startswith("and(greater-than(updated,")

    def kinds(customer: str) -> set[tuple[EventType, Channel | None]]:
        return {(e.type, e.channel) for e in rig.events.for_customer(customer)}

    assert kinds("ann@example.com") == {
        (EventType.SIGNUP, None),
        (EventType.CONSENT_GRANTED, Channel.EMAIL),
        (EventType.CONSENT_GRANTED, Channel.SMS),
    }
    assert kinds("bob@example.com") == {(EventType.SIGNUP, None), (EventType.CONSENT_REVOKED, Channel.EMAIL)}
    assert kinds("cy@example.com") == {(EventType.SIGNUP, None)}  # implied consent is not consent by default
    revoked = [e for e in rig.events.for_customer("di@example.com") if e.type is EventType.CONSENT_REVOKED]
    assert revoked and revoked[0].attrs["reason"] == "HARD_BOUNCE"
    signup = next(e for e in rig.events.for_customer("ann@example.com") if e.type is EventType.SIGNUP)
    assert signup.attrs["timezone"] == "Europe/London" and signup.attrs["country"] == "GB"
    assert all(e.source == "klaviyo" and e.attrs["provider"] == "klaviyo" for e in rig.events.scan())


def test_implied_email_consent_is_an_explicit_per_connection_choice() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    _consent_world(rig.fake)
    connection = connect_key(rig, email_consent="can_receive")
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert any(e.type is EventType.CONSENT_GRANTED for e in rig.events.for_customer("cy@example.com"))


def test_a_reread_is_free_and_a_consent_change_is_a_new_event() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    _consent_world(rig.fake)
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    before = rig.events.count()
    rig.clock.advance(timedelta(hours=1))
    again = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    assert again.rows == 4 and again.new_records == 0 and rig.events.count() == before  # the overlap re-read
    changed = rig.clock.now() - timedelta(minutes=10)
    rig.fake.add_profile(
        "P2", email="bob@example.com", updated=changed, subs=subscriptions("SUBSCRIBED", at=changed)
    )
    rig.clock.advance(timedelta(hours=1))
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    bob = sorted(rig.events.for_customer("bob@example.com"), key=lambda e: e.ts)
    assert [e.type for e in bob if e.channel is Channel.EMAIL] == [
        EventType.CONSENT_REVOKED,
        EventType.CONSENT_GRANTED,
    ]


def _engagement_world(fake: FakeKlaviyo) -> None:
    fake.add_profile("P1", email="ann@example.com", updated=T0 - timedelta(days=40), phone="+447700900001")
    for name in (
        "Received Email",
        "Opened Email",
        "Clicked Email",
        "Bounced Email",
        "Marked Email as Spam",
        "Unsubscribed",
        "Received SMS",
        "Placed Order",
        "Viewed Product",
        "Some Custom Thing",
    ):
        fake.add_metric(name)
    at = T0 - timedelta(minutes=50)
    fake.add_event(
        "Received Email", pid="P1", at=at, props={"$flow": "SHOPFLOW", "$message": "m1", "Subject": "Hi"}
    )
    fake.add_event("Opened Email", pid="P1", at=at + timedelta(minutes=1))
    fake.add_event(
        "Clicked Email", pid="P1", at=at + timedelta(minutes=2), props={"URL": "https://acme.example/x"}
    )
    fake.add_event("Bounced Email", pid="P1", at=at + timedelta(minutes=3), props={"Bounce Type": "hard"})
    fake.add_event("Marked Email as Spam", pid="P1", at=at + timedelta(minutes=4))
    fake.add_event("Unsubscribed", pid="P1", at=at + timedelta(minutes=5))
    fake.add_event("Received SMS", pid="P1", at=at + timedelta(minutes=6))
    fake.add_event("Placed Order", pid="P1", at=at + timedelta(minutes=7), props={"$value": 120.0})
    fake.add_event(
        "Viewed Product",
        pid="P1",
        at=at + timedelta(minutes=8),
        props={"ProductID": "sku-9", "Categories": ["Trail", "Shoes"], "brand_new": {"x": 1}},
    )


def test_engagement_reads_each_message_metric_through_cursor_pages() -> None:
    fake = FakeKlaviyo(page_size=2)
    _engagement_world(fake)
    rig = make_klaviyo_rig(fake, connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    statuses = []
    for _ in range(10):
        report = rig.engine.sync(connection.connection_id, Stream.ENGAGEMENT, Lane.LIVE, T0, max_pages=2)
        statuses.append(report.status)
        if report.status == "ok":
            break
    assert statuses[-1] == "ok" and "more" in statuses  # resumed from saved cursors across runs
    got = {(e.type, e.channel) for e in rig.events.for_customer("ann@example.com")}
    assert got == {
        (EventType.DELIVER, Channel.EMAIL),
        (EventType.OPEN, Channel.EMAIL),
        (EventType.CLICK, Channel.EMAIL),
        (EventType.BOUNCE, Channel.EMAIL),
        (EventType.COMPLAINT, Channel.EMAIL),
        (EventType.UNSUBSCRIBE, Channel.EMAIL),
        (EventType.DELIVER, Channel.SMS),
    }
    deliver = next(
        e
        for e in rig.events.for_customer("ann@example.com")
        if e.type is EventType.DELIVER and e.channel is Channel.EMAIL
    )
    assert deliver.decision_id is None  # the shop's own flow: engagement, but not one of our decisions
    assert deliver.attrs["flow_id"] == "SHOPFLOW" and "via_autonomy" not in deliver.attrs
    filters = [r.url.params.get("filter", "") for r in requests_to(rig.fake, "GET", "/api/events")]
    assert not any(rig.fake.metric_id("Placed Order") in f for f in filters)  # orders come from Shopify
    assert all("less-than(datetime," in f for f in filters if f)
    rig.engine.sync(connection.connection_id, Stream.EVENTS, Lane.LIVE, T0)
    view = next(e for e in rig.events.for_customer("ann@example.com") if e.type is EventType.VIEW)
    assert view.item_id == "sku-9" and view.category == "Trail"


def test_engagement_history_backfills_in_weekly_chunks() -> None:
    fake = FakeKlaviyo()
    fake.add_profile("P1", email="ann@example.com", updated=T0 - timedelta(days=90))
    fake.add_metric("Opened Email")
    for days in (1, 9, 20, 40):
        fake.add_event("Opened Email", pid="P1", at=T0 - timedelta(days=days))
    rig = make_klaviyo_rig(fake, connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    for _ in range(12):
        rig.engine.sync(connection.connection_id, Stream.ENGAGEMENT, Lane.HISTORY, T0)
    opens = [e for e in rig.events.for_customer("ann@example.com") if e.type is EventType.OPEN]
    assert len(opens) == 4
    position = rig.store.position(connection.connection_id, Stream.ENGAGEMENT, Lane.HISTORY)
    assert position.reached is not None and position.reached <= T0 - timedelta(days=42)


def test_a_429_with_retry_after_is_waited_out_and_the_page_still_lands() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    _consent_world(rig.fake)
    connection = connect_key(rig)
    rig.fake.failures.append(jsonapi_error(429, "throttled", "Request was throttled.", {"Retry-After": "3"}))
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert report.status == "ok" and report.rows == 4
    assert 3.0 in rig.clock.slept


def test_a_long_retry_after_parks_the_job_until_then() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    rig.fake.failures.append(jsonapi_error(429, "throttled", "", {"Retry-After": "120"}))
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert report.status == "retry" and report.retry_at == T0 + timedelta(seconds=120)


def test_an_expired_page_cursor_restarts_the_window_without_duplicates() -> None:
    fake = FakeKlaviyo(page_size=1)
    _consent_world(fake)
    rig = make_klaviyo_rig(fake, connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0, max_pages=1)
    fake.expired_cursors.add("1")
    for _ in range(8):
        if (
            rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0, max_pages=2).status
            == "ok"
        ):
            break
    signups = [e for e in rig.events.scan() if e.type is EventType.SIGNUP]
    assert len(signups) == 4


def test_schema_drift_is_tolerated_and_counted() -> None:
    fake = FakeKlaviyo()
    at = T0 - timedelta(minutes=30)
    fake.add_profile(
        "P1", email="ann@example.com", updated=at, subs={}, location={}
    )  # no subscriptions block
    # a profile with almost nothing (no created date, email, location or subscriptions) and an unknown field
    fake.profiles["P2"] = {
        "type": "profile",
        "id": "P2",
        "attributes": {"updated": at.isoformat()},
        "extra": True,
    }
    rig = make_klaviyo_rig(fake, connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert report.status == "ok"
    kinds = {e.type for e in rig.events.for_customer("ann@example.com")}
    assert kinds == {EventType.SIGNUP}
    assert rig.events.for_customer("klaviyo:P2") == []  # no created date, no consent: nothing invented


def test_the_endpoint_pacer_keeps_each_endpoint_inside_its_burst_and_steady_limits() -> None:
    clock = ManualClock(T0)
    pacer = EndpointPacer(limits={"flows:create": EndpointLimit(1, 15)}, max_inline_wait=timedelta(minutes=5))
    times = []
    for _ in range(40):
        pacer.acquire("c1", "flows:create", clock)
        times.append(clock.now())
    for start in times:  # Klaviyo counts in fixed windows: no second and no minute may overflow
        assert sum(1 for t in times if start <= t < start + timedelta(seconds=1)) <= 1
        assert sum(1 for t in times if start <= t < start + timedelta(minutes=1)) <= 15
    assert times[-1] - times[0] < timedelta(minutes=4)  # 40 calls at 15 a minute: no slower than needed
    other = clock.now()
    pacer.acquire("c2", "flows:create", clock)  # another connection has its own quota
    assert clock.now() == other
    tight = EndpointPacer(limits={"accounts": EndpointLimit(1, 1)}, max_inline_wait=timedelta(seconds=5))
    tight.acquire("c1", "accounts", clock)
    with pytest.raises(RateLimited) as caught:
        tight.acquire("c1", "accounts", clock)
    assert caught.value.retry_at is not None and caught.value.retry_at > clock.now()


def test_errors_are_secret_free() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    rig.fake.failures.append(
        httpx.Response(400, json={"errors": [{"code": "invalid", "detail": f"bad key {API_KEY}"}]})
    )
    report = rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert report.status == "retry" and report.error is not None
    assert API_KEY not in report.error


def test_audiences_list_lists_and_segments() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    ctx = rig.engine.context(connection, T0)
    audiences = rig.connector.audiences(ctx)
    assert [(a.kind, a.name) for a in audiences] == [("list", "VIP list"), ("segment", "VIP segment")]
    assert APP.client_secret not in repr(audiences)


def test_identity_prefers_the_external_id_then_the_email() -> None:
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    at = T0 - timedelta(minutes=30)
    rig.fake.add_profile("P1", email="Ann@Example.com", updated=at, external_id="7001")
    rig.fake.add_profile("P2", email="Bob@Example.com", updated=at)
    rig.fake.add_profile("P3", email=None, phone="+447700900003", updated=at)
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    customers = {e.customer_id for e in rig.events.scan()}
    assert customers == {"7001", "bob@example.com", "+447700900003"}
    other = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    other.fake.add_profile("P1", email="Ann@Example.com", updated=at, external_id="7001")
    by_email = connect_key(other, identity="email")
    other.engine.sync(by_email.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    assert {e.customer_id for e in other.events.scan()} == {"ann@example.com"}


def test_an_unsubscribe_is_stamped_at_its_change_not_at_the_old_opt_in() -> None:
    # Klaviyo can keep consent_timestamp at the original opt-in; last_updated says when it changed.
    rig = make_klaviyo_rig(connector=KlaviyoConnector(auto_setup=False))
    opted_in = T0 - timedelta(days=200)
    rig.fake.add_profile(
        "P1", email="ann@example.com", updated=T0 - timedelta(hours=1), subs=subscriptions(at=opted_in)
    )
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, T0)
    changed = T0 + timedelta(minutes=20)
    subs = subscriptions("UNSUBSCRIBED", can_email=False, at=opted_in)
    subs["email"]["marketing"]["last_updated"] = changed.isoformat()
    rig.fake.add_profile("P1", email="ann@example.com", updated=changed, subs=subs)
    rig.clock.advance(timedelta(hours=1))
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    consent = sorted(
        (e for e in rig.events.for_customer("ann@example.com") if e.channel is Channel.EMAIL),
        key=lambda e: e.ts,
    )
    assert [e.type for e in consent] == [EventType.CONSENT_GRANTED, EventType.CONSENT_REVOKED]
    assert consent[-1].ts == changed  # a time-ordered rebuild ends revoked, never on a tie


def test_a_metric_created_after_connect_is_read_from_the_next_window() -> None:
    fake = FakeKlaviyo()
    fake.add_profile("P1", email="ann@example.com", updated=T0 - timedelta(days=40))
    fake.add_metric("Opened Email")
    fake.add_event("Opened Email", pid="P1", at=T0 - timedelta(minutes=30))
    rig = make_klaviyo_rig(fake, connector=KlaviyoConnector(auto_setup=False))
    connection = connect_key(rig)
    rig.engine.sync(connection.connection_id, Stream.ENGAGEMENT, Lane.LIVE, T0)
    # The account's first SMS: Klaviyo creates "Received SMS" now, inside the six-hour metric cache.
    fake.add_event("Received SMS", pid="P1", at=T0 + timedelta(minutes=5))
    rig.clock.advance(timedelta(hours=1))
    report = rig.engine.sync(connection.connection_id, Stream.ENGAGEMENT, Lane.LIVE, rig.clock.now())
    assert report.status == "ok"
    kinds = {(e.type, e.channel) for e in rig.events.for_customer("ann@example.com")}
    assert (EventType.DELIVER, Channel.SMS) in kinds
