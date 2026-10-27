"""Quality rating and Meta's limits (item 3): YELLOW halves the budget, RED pauses, 131049 is not a failure."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx

from autonomy.channels import Guards, evaluate
from autonomy.channels.dispatcher import SkipReason
from autonomy.channels.whatsapp import ACCOUNT_CUSTOMER, WhatsAppCloudAdapter, encode_opaque
from autonomy.channels.whatsapp_guard import WhatsAppGuard
from autonomy.channels.whatsapp_health import (
    BREAKER_IGNORE_ATTR,
    HOLD_UNTIL_ATTR,
    QUALITY_ALERT_CODE,
    WhatsAppHealth,
    WhatsAppLimits,
    error_code_in,
    load_limits,
)
from autonomy.channels.whatsapp_templates import TemplateCatalogue
from autonomy.contracts import (
    Channel,
    Event,
    EventType,
    SendReceipt,
    StopMetric,
    StopRule,
)
from autonomy.fakes import FakeChannelAdapter
from autonomy.runtime.agent import Breaker
from tests.v2.channels.test_dispatcher import NOW, World, decision, state

PNID = "106540352242922"
TS = int(NOW.timestamp())


def envelope_of(*changes: dict[str, Any], time: int = TS) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba", "time": time, "changes": list(changes)}],
        }
    ).encode()


def quality_change(**value: Any) -> dict[str, Any]:
    return {"field": "phone_number_quality_update", "value": {"display_phone_number": "15550001111", **value}}


def failed_status(code: int, *, decision_id: str = "dec_1", customer: str = "c1", at: int = TS) -> bytes:
    status = {
        "id": f"wamid.{decision_id}",
        "status": "failed",
        "timestamp": str(at),
        "recipient_id": "15550001111",
        "biz_opaque_callback_data": encode_opaque(decision_id, customer, f"msg_{decision_id}"),
        "errors": [{"code": code, "title": f"error {code}"}],
    }
    return envelope_of(
        {"field": "messages", "value": {"metadata": {"phone_number_id": PNID}, "statuses": [status]}}
    )


def adapter(**kw: Any) -> WhatsAppCloudAdapter:
    return WhatsAppCloudAdapter(PNID, "token", "secret", **kw)


# --- quality rating -------------------------------------------------------------------------------


def test_the_budget_is_halved_on_yellow_paused_on_red_and_never_below_one() -> None:
    health = WhatsAppHealth()
    assert health.weekly_cap(4) == 4 and not health.paused
    health.update("YELLOW", "TIER_1K", NOW)
    assert health.weekly_cap(4) == 2 and health.weekly_cap(1) == 1 and not health.paused
    health.update("RED", None, NOW + timedelta(hours=1))
    assert health.paused and health.weekly_cap(4) == 0 and health.tier == "TIER_1K"
    health.update("GREEN", "TIER_10K", NOW + timedelta(hours=2))
    assert health.weekly_cap(4) == 4 and health.tier == "TIER_10K"


def test_an_older_report_does_not_undo_a_newer_one() -> None:
    health = WhatsAppHealth()
    health.update("RED", None, NOW)
    health.update("GREEN", None, NOW - timedelta(days=1))
    assert health.rating == "RED"


def test_red_raises_one_alert_and_pauses_until_a_person_resets_it() -> None:
    health = WhatsAppHealth()
    alert = health.update("RED", None, NOW)
    assert alert is not None and alert.code == QUALITY_ALERT_CODE and alert.auto_paused
    assert health.update("RED", None, NOW + timedelta(hours=1)) is None  # not again for the same state
    assert [a.code for a in health.drain_alerts()] == [QUALITY_ALERT_CODE] and health.drain_alerts() == []
    health.reset()
    assert not health.paused


def test_a_quality_webhook_sets_the_rating_the_tier_and_an_event_the_breaker_reads() -> None:
    wa = adapter()
    body = envelope_of(quality_change(event="FLAGGED", current_limit="TIER_1K"))
    [event] = wa.parse_webhook({}, body)
    assert wa.health.rating == "RED" and wa.health.tier == "TIER_1K" and wa.health.paused
    assert event.customer_id == ACCOUNT_CUSTOMER and event.decision_id is None
    assert event.channel is Channel.WHATSAPP and event.attrs["quality_rating"] == "RED"
    assert [a.code for a in wa.drain_alerts()] == [QUALITY_ALERT_CODE]
    # The existing circuit breaker's whatsapp_quality rule trips on that same event.
    rule = StopRule(metric=StopMetric.WHATSAPP_QUALITY, threshold=2, action="pause_channel")
    result = evaluate([rule], [event], {}, 0, 0, NOW + timedelta(hours=1), guards=Guards())
    assert result.paused_channels == {Channel.WHATSAPP}


def test_an_explicit_rating_in_the_payload_wins_over_the_event_name() -> None:
    wa = adapter()
    wa.parse_webhook(
        {}, envelope_of(quality_change(event="DOWNGRADE", quality_rating="YELLOW", current_limit="TIER_250"))
    )
    assert wa.health.rating == "YELLOW" and wa.health.tier == "TIER_250" and not wa.health.paused
    wa.parse_webhook({}, envelope_of(quality_change(event="UPGRADE", current_limit="TIER_10K"), time=TS + 60))
    assert (
        wa.health.rating == "YELLOW" and wa.health.tier == "TIER_10K"
    )  # a tier change alone keeps the rating
    wa.parse_webhook({}, envelope_of(quality_change(event="UNFLAGGED"), time=TS + 120))
    assert wa.health.rating == "YELLOW"


def test_the_rating_and_tier_can_be_read_from_the_api() -> None:
    seen: list[httpx.Request] = []

    def graph(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"quality_rating": "YELLOW", "messaging_limit_tier": "TIER_1K", "id": PNID}
        )

    wa = adapter(transport=httpx.MockTransport(graph))
    assert wa.get_phone_status(NOW) == ("YELLOW", "TIER_1K")
    assert wa.health.rating == "YELLOW" and wa.health.tier == "TIER_1K"
    assert (
        seen[0].url.path.endswith(f"/{PNID}")
        and seen[0].url.params["fields"] == "quality_rating,messaging_limit_tier"
    )


# --- the dispatcher enforces it ------------------------------------------------------------------


def world_with(health: WhatsAppHealth) -> World:
    w = World()
    w.dispatcher.guards = {Channel.WHATSAPP: WhatsAppGuard(TemplateCatalogue(), health)}
    return w


def prior_sends(w: World, count: int) -> None:
    w.events.append(
        [
            Event(
                event_id=f"old_{i}",
                customer_id="c1",
                type=EventType.SEND,
                ts=NOW - timedelta(days=1, minutes=i),
                source="t",
                channel=Channel.WHATSAPP,
                decision_id=f"old_dec_{i}",
            )
            for i in range(count)
        ]
    )


def wa_decision() -> Any:
    return decision(channel=Channel.WHATSAPP, content_id="ci_3")


def test_yellow_halves_the_weekly_whatsapp_budget_at_dispatch() -> None:
    health = WhatsAppHealth()
    health.update("YELLOW", None, NOW - timedelta(hours=1))
    w = world_with(health)
    weekly = min(b.envelope.max_contacts_per_week for b in w.ws.briefs("goal_winback"))
    allowed = health.weekly_cap(weekly)
    assert allowed < weekly or weekly == 1
    prior_sends(w, allowed)
    report = w.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW)
    assert report.skip_reasons() == {SkipReason.QUALITY_BUDGET.value: 1}
    # Green, with the same history: the policy's own cap is the only limit, and it is not reached.
    green = world_with(WhatsAppHealth())
    prior_sends(green, allowed)
    assert green.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW).sent


def test_red_pauses_whatsapp_but_not_email() -> None:
    health = WhatsAppHealth()
    health.update("RED", None, NOW - timedelta(hours=1))
    w = world_with(health)
    report = w.dispatcher.dispatch([wa_decision(), decision(content_id="ci_0")], {"c1": state()}, NOW)
    assert report.skip_reasons() == {SkipReason.QUALITY_PAUSED.value: 1}
    assert [r.decision_id for r in report.sent] == ["dec_c1_email_ci_0"]


# --- error codes ----------------------------------------------------------------------------------


def test_131049_is_recorded_as_a_meta_limit_with_a_24_hour_hold_and_no_breaker_count() -> None:
    [event] = adapter().parse_webhook({}, failed_status(131049))
    assert event.type is EventType.FAIL and event.attrs["error_code"] == 131049
    assert event.attrs["not_delivered"] == "not delivered: Meta limit"
    assert event.attrs[BREAKER_IGNORE_ATTR] is True
    assert datetime.fromisoformat(str(event.attrs[HOLD_UNTIL_ATTR])) == event.ts + timedelta(hours=24)


def test_131026_and_131047_follow_metas_meaning() -> None:
    [undeliverable] = adapter().parse_webhook({}, failed_status(131026))
    assert undeliverable.attrs[BREAKER_IGNORE_ATTR] is True
    assert "cannot receive" in str(undeliverable.attrs["not_delivered"])
    assert datetime.fromisoformat(str(undeliverable.attrs[HOLD_UNTIL_ATTR])) == undeliverable.ts + timedelta(
        hours=168
    )
    [reengage] = adapter().parse_webhook({}, failed_status(131047))
    assert (
        BREAKER_IGNORE_ATTR not in reengage.attrs and HOLD_UNTIL_ATTR not in reengage.attrs
    )  # our bug: it counts
    assert "template" in str(reengage.attrs["not_delivered"])
    [other] = adapter().parse_webhook({}, failed_status(131000))
    assert other.type is EventType.FAIL and "not_delivered" not in other.attrs
    [blocked] = adapter().parse_webhook({}, failed_status(131050))
    assert blocked.type is EventType.BLOCK  # unchanged: the person stopped marketing messages


def sent_then_failed(code_attrs: dict[str, Any], n: int) -> list[Event]:
    events: list[Event] = []
    for i in range(n):
        events.append(
            Event(
                event_id=f"send_{i}",
                customer_id=f"c{i}",
                type=EventType.SEND,
                ts=NOW - timedelta(hours=3),
                source="t",
                channel=Channel.WHATSAPP,
                decision_id=f"d{i}",
            )
        )
        events.append(
            Event(
                event_id=f"fail_{i}",
                customer_id=f"c{i}",
                type=EventType.FAIL,
                ts=NOW - timedelta(hours=2),
                source="t",
                channel=Channel.WHATSAPP,
                decision_id=f"d{i}",
                attrs=code_attrs,
            )
        )
    return events


def test_131049_failures_do_not_trip_the_delivery_breaker_but_other_failures_do() -> None:
    rule = StopRule(metric=StopMetric.DELIVERY_RATE_BELOW, threshold=0.8, action="pause_channel")
    good = [
        Event(
            event_id=f"{kind.value}_ok_{i}",
            customer_id=f"ok{i}",
            type=kind,
            ts=NOW - timedelta(hours=3 - j),
            source="t",
            channel=Channel.WHATSAPP,
            decision_id=f"ok{i}",
        )
        for i in range(300)
        for j, kind in enumerate((EventType.SEND, EventType.DELIVER))
    ]
    meta = good + sent_then_failed({"error_code": 131049, BREAKER_IGNORE_ATTR: True}, 300)
    ordinary = good + sent_then_failed({"error_code": 131000}, 300)
    assert evaluate([rule], meta, {}, 0, 0, NOW).paused_channels == set()
    assert evaluate([rule], ordinary, {}, 0, 0, NOW).paused_channels == {Channel.WHATSAPP}


def test_the_runtime_breaker_skips_a_failure_flagged_as_not_ours() -> None:
    breaker = Breaker()
    flagged = sent_then_failed({BREAKER_IGNORE_ATTR: True}, 1)[1]
    plain = sent_then_failed({}, 1)[1]
    breaker.record(flagged, "goal_winback", Channel.WHATSAPP)
    assert breaker.outcomes == []
    breaker.record(plain, "goal_winback", Channel.WHATSAPP)
    assert len(breaker.outcomes) == 1


def test_a_customer_is_not_retried_on_whatsapp_for_24_hours_after_131049() -> None:
    w = world_with(WhatsAppHealth())
    [fail] = adapter().parse_webhook(
        {}, failed_status(131049, at=int((NOW - timedelta(hours=2)).timestamp()))
    )
    w.events.append([fail])
    held = w.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW)
    assert held.skip_reasons() == {SkipReason.PROVIDER_HOLD.value: 1}
    assert "Meta limit" in held.skipped[0].detail
    # Email to the same person is not held, and the hold ends after 24 hours.
    assert w.dispatcher.dispatch([decision(content_id="ci_0")], {"c1": state()}, NOW).sent
    later = NOW + timedelta(hours=23)
    assert w.dispatcher.dispatch([wa_decision()], {"c1": state()}, later).sent


def test_a_131049_returned_on_send_gets_the_same_treatment() -> None:
    class Limited(FakeChannelAdapter):
        def send(self, message: Any) -> SendReceipt:
            return SendReceipt(accepted=False, error="(#131049) This message was not delivered")

    w = world_with(WhatsAppHealth())
    w.dispatcher.adapters[Channel.WHATSAPP] = Limited(Channel.WHATSAPP)
    report = w.dispatcher.dispatch([wa_decision()], {"c1": state()}, NOW)
    [failed] = report.failed
    [event] = report.events
    assert failed.reason.startswith("(#131049)") and event.type is EventType.FAIL
    assert (
        event.attrs[BREAKER_IGNORE_ATTR] is True
        and event.attrs["not_delivered"] == "not delivered: Meta limit"
    )
    again = w.dispatcher.dispatch(
        [decision("c1", channel=Channel.WHATSAPP, content_id="ci_3", did="dec_retry")],
        {"c1": state()},
        NOW + timedelta(hours=1),
    )
    assert again.skip_reasons() == {SkipReason.PROVIDER_HOLD.value: 1}


def test_error_codes_are_read_from_adapter_errors_and_the_config_is_valid() -> None:
    assert error_code_in("(#131049) This message was not delivered") == 131049
    assert error_code_in("HTTP 500") is None
    limits = load_limits()
    assert isinstance(limits, WhatsAppLimits) and limits.yellow_budget_factor == 0.5
    assert limits.rule_for(131049) is not None and limits.rule_for(131049).hold == timedelta(hours=24)  # type: ignore[union-attr]
