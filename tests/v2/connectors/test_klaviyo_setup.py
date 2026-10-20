"""The send flows Autonomy creates in the customer's Klaviyo on connect, and the guided fallback."""

from __future__ import annotations

import json
from datetime import timedelta

from autonomy.channels.klaviyo import ALERT_SETUP, ensure_klaviyo_setup
from autonomy.connectors import ConnectionStatus
from autonomy.connectors.klaviyo import DECISION_METRIC, EMAIL_FLOW, EMAIL_TEMPLATE, SMS_FLOW
from autonomy.contracts import Channel
from tests.v2.connectors.klaviyo_support import (
    T0,
    FakeKlaviyo,
    connect_key,
    connect_oauth,
    connect_ready,
    jsonapi_error,
    make_klaviyo_rig,
    requests_to,
)


def _flow_body(fake: FakeKlaviyo, name: str) -> dict[str, object]:
    for request in requests_to(fake, "POST", "/api/flows"):
        body = json.loads(request.content)["data"]["attributes"]
        if body["name"] == name:
            return body
    raise AssertionError(f"no Create Flow for {name}")


def test_connect_creates_the_metric_template_and_a_live_email_flow_with_no_person() -> None:
    fake = FakeKlaviyo(metric_lag=1)  # Klaviyo creates the metric asynchronously
    rig = make_klaviyo_rig(fake)
    connection, _ = connect_oauth(rig)
    # At connect: the metric did not exist, so one setup event created it without triggering anything.
    probe = fake.created_events[0]
    assert probe["metric"]["data"]["attributes"]["name"] == DECISION_METRIC
    assert probe["backfill"] is True and "email" not in probe["profile"]["data"]["attributes"]
    assert fake.flows == {}
    # Klaviyo is still processing the event at the first check; the next one finishes the setup.
    rig.clock.advance(timedelta(minutes=10))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "pending" and not status.ready_for(Channel.EMAIL)
    rig.clock.advance(timedelta(hours=1))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "ready" and status.ready_for(Channel.EMAIL)
    assert len(fake.created_events) == 1  # the setup event was recorded once (unique_id)
    assert [t["attributes"]["name"] for t in fake.templates.values()] == [EMAIL_TEMPLATE]
    template = next(iter(fake.templates.values()))["attributes"]
    assert template["editor_type"] == "CODE" and "{{ event.body" in template["html"]
    assert "{% unsubscribe" in template["html"]
    body = _flow_body(fake, EMAIL_FLOW)
    definition = body["definition"]
    assert isinstance(definition, dict)
    trigger = definition["triggers"][0]
    assert trigger["type"] == "metric" and trigger["id"] == fake.metric_id(DECISION_METRIC)
    condition = trigger["trigger_filter"]["condition_groups"][0]["conditions"][0]
    assert condition == {
        "type": "metric-property",
        "metric_id": fake.metric_id(DECISION_METRIC),
        "field": "channel",
        "filter": {"type": "string", "operator": "equals", "value": "email"},
    }
    action = definition["actions"][0]
    assert action["type"] == "send-email" and action["data"]["status"] == "live"
    message = action["data"]["message"]
    assert message["subject_line"] == "{{ event.subject }}" and message["smart_sending_enabled"] is False
    assert message["from_email"] == "hello@acme.example" and message["template_id"] in fake.templates
    assert definition["entry_action_id"] == action["temporary_id"]
    # every decision enters the flow again (alltime + 0); the default could let each person in once
    assert definition["reentry_criteria"] == {"duration": 0, "unit": "alltime"}
    # Klaviyo created it as a draft: Autonomy switched the flow live (PATCH status).
    flow = next(iter(fake.flows.values()))
    assert flow["attributes"]["status"] == "live"
    assert all(
        r.headers["content-type"] == "application/vnd.api+json"
        for r in requests_to(fake, "POST", "/api/flows")
    )
    assert rig.alerts.list_alerts() == []


def test_setup_is_idempotent() -> None:
    rig = make_klaviyo_rig()
    connection = connect_ready(rig)
    for _ in range(3):
        rig.clock.advance(timedelta(hours=1))
        status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
        assert status is not None and status.state == "ready"
    assert len(requests_to(rig.fake, "POST", "/api/flows")) == 1
    assert len(requests_to(rig.fake, "POST", "/api/templates")) == 1
    assert len(rig.fake.flows) == 1


def test_sms_flow_is_created_when_sms_is_switched_on() -> None:
    rig = make_klaviyo_rig()
    connection = connect_ready(rig, sms="on")
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, T0)
    assert status is not None and status.ready_for(Channel.EMAIL) and status.ready_for(Channel.SMS)
    sms = _flow_body(rig.fake, SMS_FLOW)["definition"]
    assert isinstance(sms, dict)
    message = sms["actions"][0]["data"]["message"]
    assert sms["actions"][0]["type"] == "send-sms" and message["body"] == "{{ event.body }}"
    assert message["sms_quiet_hours_enabled"] is True and message["smart_sending_enabled"] is False


def test_a_flow_a_person_turned_off_is_not_switched_back_on() -> None:
    rig = make_klaviyo_rig()
    connection = connect_ready(rig)
    flow = next(iter(rig.fake.flows.values()))
    flow["attributes"]["status"] = "manual"  # someone paused it in Klaviyo
    rig.clock.advance(timedelta(hours=2))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "guided" and not status.ready_for(Channel.EMAIL)
    assert flow["attributes"]["status"] == "manual"
    assert not requests_to(rig.fake, "PATCH", f"/api/flows/{flow['id']}")[1:]  # only the first switch-on
    alerts = rig.alerts.list_alerts()
    assert [a.code for a in alerts] == [ALERT_SETUP] and "set the flow" in alerts[0].message


def test_when_klaviyo_refuses_flow_creation_the_exact_steps_go_out_in_one_alert() -> None:
    fake = FakeKlaviyo()
    fake.flow_create_error["email"] = jsonapi_error(
        400, "invalid", "This account cannot create live flows via API."
    )
    rig = make_klaviyo_rig(fake)
    connection = connect_key(rig)
    for _ in range(4):  # hourly checks: the same problem never alerts twice
        rig.clock.advance(timedelta(hours=1))
        status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "guided"
    email = status.channel(Channel.EMAIL)
    assert email is not None and "cannot create live flows" in (email.detail or "")
    steps = " ".join(email.steps)
    assert EMAIL_FLOW in steps and DECISION_METRIC in steps and "{{ event.subject }}" in steps
    assert "Smart Sending off" in steps and EMAIL_TEMPLATE in steps
    alerts = rig.alerts.list_alerts()
    assert len(alerts) == 1 and alerts[0].code == ALERT_SETUP and "1. In Klaviyo" in alerts[0].message
    # Once a person built the flow by hand, the check finds it by name and Autonomy is ready.
    fake.flows["FLX"] = {
        "type": "flow",
        "id": "FLX",
        "attributes": {"name": EMAIL_FLOW, "status": "live", "archived": False},
    }
    rig.clock.advance(timedelta(hours=1))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "ready"


def test_a_missing_scope_is_a_guided_step_not_a_reconnect() -> None:
    fake = FakeKlaviyo()
    fake.flow_create_error["email"] = jsonapi_error(403, "permission_denied", "Missing scope flows:write")
    rig = make_klaviyo_rig(fake)
    connection = connect_key(rig)
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, T0)
    assert status is not None and status.state == "guided"
    stored = rig.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.ACTIVE  # reading still works


def test_no_default_sender_is_a_guided_step() -> None:
    fake = FakeKlaviyo(default_sender=None)
    rig = make_klaviyo_rig(fake)
    connection = connect_key(rig)
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, T0)
    assert status is not None and status.state == "guided"
    email = status.channel(Channel.EMAIL)
    assert email is not None and "sender" in (email.detail or "")
    # A sender given in the connection's settings is enough.
    other = make_klaviyo_rig(FakeKlaviyo(default_sender=None))
    configured = connect_key(other, from_email="news@acme.example", from_label="Acme")
    done = ensure_klaviyo_setup(other.engine, configured.connection_id, T0)
    assert done is not None and done.state == "ready"


def test_sending_switched_off_creates_nothing() -> None:
    rig = make_klaviyo_rig()
    connection = connect_key(rig, send="off")
    assert ensure_klaviyo_setup(rig.engine, connection.connection_id, T0) is None
    assert rig.fake.flows == {} and rig.fake.created_events == []


def test_a_backfill_event_that_creates_no_metric_is_followed_by_a_plain_one() -> None:
    fake = FakeKlaviyo(backfill_creates_metric=False)
    rig = make_klaviyo_rig(fake)
    connection = connect_key(rig)
    assert [e.get("backfill") for e in fake.created_events] == [True]
    rig.clock.advance(timedelta(minutes=10))  # too soon to tell: Klaviyo may still be processing
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "pending" and len(fake.created_events) == 1
    rig.clock.advance(timedelta(hours=1))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "pending"
    second = fake.created_events[1]
    assert "backfill" not in second and second["profile"]["data"]["attributes"] == {
        "external_id": "autonomy-setup"
    }
    rig.clock.advance(timedelta(hours=1))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "ready" and len(fake.created_events) == 2
    assert rig.alerts.list_alerts() == []


def test_a_duplicate_live_flow_is_set_back_to_draft_so_each_decision_sends_once() -> None:
    rig = make_klaviyo_rig()
    connection = connect_ready(rig)
    original = next(iter(rig.fake.flows.values()))
    copy = {**original, "id": "FLZ", "attributes": {**original["attributes"], "status": "live"}}
    rig.fake.flows["FLZ"] = copy  # e.g. a create that timed out after Klaviyo stored it
    rig.clock.advance(timedelta(hours=2))
    status = ensure_klaviyo_setup(rig.engine, connection.connection_id, rig.clock.now())
    assert status is not None and status.state == "ready"
    email = status.channel(Channel.EMAIL)
    assert email is not None and email.flow_id == original["id"]
    assert original["attributes"]["status"] == "live" and copy["attributes"]["status"] == "draft"
    assert len(requests_to(rig.fake, "POST", "/api/flows")) == 1


def test_the_setup_profile_is_not_a_customer() -> None:
    from autonomy.connectors import Lane, Stream

    rig = make_klaviyo_rig()
    connection = connect_ready(rig)
    assert any(p["attributes"].get("external_id") == "autonomy-setup" for p in rig.fake.profiles.values())
    rig.clock.advance(timedelta(hours=1))
    rig.engine.sync(connection.connection_id, Stream.CONSENT, Lane.LIVE, rig.clock.now())
    assert rig.events.for_customer("autonomy-setup") == []
