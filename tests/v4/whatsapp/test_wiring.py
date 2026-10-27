"""The WhatsApp checks are wired into the product, survive process boundaries, and stay out of states.

Everything runs against fakes and `httpx.MockTransport`: no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx

from autonomy.agents.conversation import Conversation
from autonomy.channels.dispatcher import SkipReason
from autonomy.channels.gateway import DispatchGateway, Provider
from autonomy.channels.whatsapp import WhatsAppCloudAdapter, sign_body
from autonomy.channels.whatsapp_guard import (
    WhatsAppGuard,
    WhatsAppUpkeep,
    whatsapp_guards,
    whatsapp_upkeep,
)
from autonomy.channels.whatsapp_health import (
    ACCOUNT_CUSTOMER,
    HOLD_UNTIL_ATTR,
    QUALITY_ALERT_CODE,
    ErrorRule,
    WhatsAppHealth,
    WhatsAppLimits,
)
from autonomy.channels.whatsapp_templates import TemplateCatalogue, WhatsAppTemplate
from autonomy.contracts import Alert, Channel, ChannelStats, CustomerState, Event, EventType
from autonomy.data.folder import StateFolder
from autonomy.data.rebuild import apply_events
from autonomy.fakes import FakeChannelAdapter, InMemoryEventStore, InMemoryStateStore, KeywordTextSignals
from tests.v2.channels.test_dispatcher import NOW, World, address, decision, state

PNID = "106540352242922"
WABA = "104996122399130"
OUR_NUMBER = "15550001111"
TS = int(NOW.timestamp())


def quality_body(number: str, event: str, *, at: int = TS) -> bytes:
    change = {
        "field": "phone_number_quality_update",
        "value": {"display_phone_number": number, "event": event, "current_limit": "TIER_1K"},
    }
    return json.dumps(
        {"object": "whatsapp_business_account", "entry": [{"id": WABA, "time": at, "changes": [change]}]}
    ).encode()


def wa_decision(did: str = "dec_c1_whatsapp_ci_3") -> Any:
    return decision(channel=Channel.WHATSAPP, content_id="ci_3", did=did)


# --- wiring ---------------------------------------------------------------------------------------


def test_the_gateway_builds_the_whatsapp_guard_from_the_cloud_adapter() -> None:
    wa = WhatsAppCloudAdapter(PNID, "t", "s", language="hi")
    w = World()
    gw = DispatchGateway(
        {Channel.WHATSAPP: [Provider("whatsapp", wa)], Channel.EMAIL: [Provider("email", w.email)]},
        events=w.events,
    )
    dispatcher = gw.attach_dispatcher(address, w.ws, w.events)
    guard = dispatcher.guards[Channel.WHATSAPP]
    assert isinstance(guard, WhatsAppGuard)
    assert guard.catalogue is wa.templates and guard.health is wa.health and guard.language == "hi"
    assert set(dispatcher.guards) == {Channel.WHATSAPP}
    # No WhatsApp Cloud adapter: no guard, other channels as before.
    plain = DispatchGateway({Channel.EMAIL: [Provider("email", w.email)]}, events=w.events)
    assert plain.attach_dispatcher(address, w.ws, w.events).guards == {}
    assert whatsapp_guards([FakeChannelAdapter(Channel.WHATSAPP)]) == {}


def test_the_upkeep_step_syncs_templates_and_rating_and_hands_on_red_alerts() -> None:
    calls: list[str] = []

    def graph(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/message_templates"):
            row = {"name": "winback_3", "language": "en", "category": "MARKETING", "status": "APPROVED"}
            return httpx.Response(200, json={"data": [row]})
        return httpx.Response(
            200,
            json={
                "quality_rating": "RED",
                "messaging_limit_tier": "TIER_1K",
                "display_phone_number": "+1 555-000 1111",
            },
        )

    wa = WhatsAppCloudAdapter(PNID, "t", "s", transport=httpx.MockTransport(graph), waba_id=WABA)
    put: list[Alert] = []
    upkeep = whatsapp_upkeep([wa], put.extend)
    assert isinstance(upkeep, WhatsAppUpkeep)
    upkeep(NOW)
    assert wa.templates.synced and [t.name for t in wa.templates.usable()] == ["winback_3"]
    assert wa.health.paused and [a.code for a in put] == [QUALITY_ALERT_CODE]
    assert wa.display_phone_number == OUR_NUMBER  # learnt from the API
    upkeep(NOW + timedelta(hours=1))  # not due yet: no call to Meta
    assert len(calls) == 2
    upkeep(NOW + timedelta(hours=7))
    assert len(calls) == 4
    assert whatsapp_upkeep([FakeChannelAdapter(Channel.EMAIL)], put.extend) is None


# --- quality across processes and restarts --------------------------------------------------------


def test_a_red_report_stored_by_another_process_pauses_this_one() -> None:
    api_side = WhatsAppCloudAdapter(PNID, "t", "s")
    events = api_side.parse_webhook({}, quality_body(OUR_NUMBER, "FLAGGED", at=TS - 3600))
    w = World()
    w.events.append(events)
    # The runtime process has its own adapter, never told about the webhook.
    w.dispatcher.guards = {Channel.WHATSAPP: WhatsAppGuard(TemplateCatalogue(), WhatsAppHealth())}
    report = w.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW)
    assert report.skip_reasons() == {SkipReason.QUALITY_PAUSED.value: 1}
    # A person resumes it: the stored RED report from before then does not pause it again.
    guard = w.dispatcher.guards[Channel.WHATSAPP]
    assert isinstance(guard, WhatsAppGuard)
    guard.health.reset(NOW)
    assert w.dispatcher.dispatch([wa_decision("dec_2")], {"c1": state()}, NOW).sent


def is_paused(health: WhatsAppHealth) -> bool:
    return health.paused  # a call, so a type checker does not carry the answer past `reset`


def test_red_does_not_lift_on_its_own_when_meta_reports_recovery() -> None:
    wa = WhatsAppCloudAdapter(PNID, "t", "s")
    wa.parse_webhook({}, quality_body(OUR_NUMBER, "FLAGGED"))
    wa.parse_webhook({}, quality_body(OUR_NUMBER, "UNFLAGGED", at=TS + 60))
    assert wa.health.rating == "YELLOW" and is_paused(wa.health)
    wa.health.reset(NOW + timedelta(minutes=2))
    assert not is_paused(wa.health) and wa.health.rating == "YELLOW"


def test_a_quality_report_for_another_number_of_the_account_is_ignored() -> None:
    wa = WhatsAppCloudAdapter(PNID, "t", "s", display_phone_number="+1 (555) 000-1111")
    assert wa.parse_webhook({}, quality_body("15559998888", "FLAGGED")) == []
    assert not wa.health.paused
    assert len(wa.parse_webhook({}, quality_body(OUR_NUMBER, "FLAGGED"))) == 1
    assert wa.health.paused


def test_an_account_level_event_is_not_folded_into_a_customer_state() -> None:
    [report] = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, quality_body(OUR_NUMBER, "FLAGGED"))
    assert report.customer_id == ACCOUNT_CUSTOMER
    states = InMemoryStateStore()
    send = Event(
        event_id="s1", customer_id="c1", type=EventType.SEND, ts=NOW, source="t", channel=Channel.WHATSAPP
    )
    assert apply_events([report, send], states, StateFolder()) == 1
    assert states.get(ACCOUNT_CUSTOMER) is None and states.get("c1") is not None


# --- holds ----------------------------------------------------------------------------------------


def held_fail(until: str, *, at: datetime) -> Event:
    return Event(
        event_id=f"fail_{until}",
        customer_id="c1",
        type=EventType.FAIL,
        ts=at,
        source="t",
        channel=Channel.WHATSAPP,
        decision_id="old",
        attrs={HOLD_UNTIL_ATTR: until, "not_delivered": "not delivered: test"},
    )


def test_a_hold_longer_than_a_week_is_honoured_and_a_malformed_one_is_ignored() -> None:
    limits = WhatsAppLimits(errors=(ErrorRule("long", frozenset({1}), "long", False, timedelta(days=30)),))
    w = World()
    w.dispatcher.guards = {
        Channel.WHATSAPP: WhatsAppGuard(TemplateCatalogue(), WhatsAppHealth(limits), limits=limits)
    }
    w.events.append([held_fail("not-a-time", at=NOW - timedelta(days=1))])
    assert w.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW).sent  # no exception, no hold
    w.events.append([held_fail((NOW + timedelta(days=10)).isoformat(), at=NOW - timedelta(days=20))])
    report = w.dispatcher.dispatch([wa_decision("dec_2")], {"c1": state()}, NOW)
    assert report.skip_reasons() == {SkipReason.PROVIDER_HOLD.value: 1}


# --- template rules come from the config ----------------------------------------------------------


def test_the_usable_categories_and_status_come_from_the_config() -> None:
    utility = WhatsAppTemplate(name="order_update", language="en", category="UTILITY", status="APPROVED")
    limits = WhatsAppLimits(usable_categories=frozenset({"MARKETING"}))
    wa = WhatsAppCloudAdapter(PNID, "t", "s", limits=limits)
    wa.templates.replace([utility], at=NOW)
    assert "only MARKETING" in wa.templates.refusal("order_update", "en")
    assert wa.templates.usable() == []
    assert TemplateCatalogue([utility]).refusal("order_update", "en") == ""  # the defaults allow UTILITY


# --- one UNSUBSCRIBE per channel for an opt-out reply ---------------------------------------------


def test_a_stop_reply_through_the_gateway_and_the_conversation_agent_unsubscribes_once_per_channel() -> None:
    wa = WhatsAppCloudAdapter(PNID, "t", "secret")
    events = InMemoryEventStore()
    gw = DispatchGateway({Channel.WHATSAPP: [Provider("whatsapp", wa)]}, events=events)
    for text, wamid in (("STOP", "wamid.A"), ("band karo", "wamid.B")):
        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": WABA,
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": PNID},
                                    "messages": [
                                        {
                                            "from": OUR_NUMBER,
                                            "id": wamid,
                                            "timestamp": str(TS),
                                            "type": "text",
                                            "text": {"body": text},
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()
        outcome = gw.handle_webhook("whatsapp", {"X-Hub-Signature-256": sign_body("secret", body)}, body, NOW)
        [reply] = [e for e in outcome.events if e.type is EventType.REPLY]
        known = CustomerState(
            customer_id=reply.customer_id,
            as_of=NOW,
            channels={Channel.WHATSAPP: ChannelStats(), Channel.EMAIL: ChannelStats()},
        )
        handled = Conversation(KeywordTextSignals()).handle_reply(reply, known, NOW)
        assert handled.intent == "stop"
        events.append(handled.events_to_record)
        unsubs = [
            e
            for e in events.for_customer(reply.customer_id)
            if e.type is EventType.UNSUBSCRIBE
            and (
                e.attrs.get("provider_message_id") == wamid or e.attrs.get("reply_event_id") == reply.event_id
            )
        ]
        assert sorted(e.channel.value for e in unsubs if e.channel is not None) == ["email", "whatsapp"], text
